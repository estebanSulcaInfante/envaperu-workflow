import hashlib
import json
import uuid
from datetime import datetime, timezone

from app.extensions import db
from app.models.legacy_pesaje import (
    EstacionCierreOpLegacy,
    EstacionComandoPiloto,
    EstacionDeltaPesajeLegacy,
    EstacionPesajeLegacy,
)
from app.services.legacy_history import (
    _capture_values,
    _latest_complete_imports,
    _local_datetime,
    normalize_text,
)


ACTIONS = {"VOID_CAPTURE", "CLOSE_OP", "REOPEN_OP"}
OPEN_COMMAND_STATES = {"PENDING", "DELIVERED"}


class LegacyContinuityError(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _now():
    return datetime.now(timezone.utc)


def _uuid(value, field):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LegacyContinuityError("INVALID_UUID", f"{field} debe ser UUID") from exc


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _current_import(station_id):
    record = _latest_complete_imports().get(station_id)
    if record is None:
        raise LegacyContinuityError(
            "INITIAL_IMPORT_REQUIRED",
            "La estacion requiere una importacion historica completa antes del continuo",
            409,
        )
    return record


def _high_watermark(station_id):
    return (
        db.session.query(db.func.max(EstacionPesajeLegacy.legacy_pesaje_id))
        .filter(EstacionPesajeLegacy.station_id == station_id)
        .scalar()
        or 0
    )


def history_sync_state(station_id):
    current_import = _current_import(station_id)
    return {
        "station_id": station_id,
        "initial_import_id": current_import.import_id,
        "high_watermark": _high_watermark(station_id),
        "contract_version": "station-legacy-continuity-v1",
    }


def _replace_closure_snapshot(station_id, import_id, closures):
    seen = set()
    parsed = []
    for closure in closures:
        if not isinstance(closure, dict):
            raise LegacyContinuityError("PAYLOAD_REJECTED", "closure debe ser objeto")
        op_raw = str(closure.get("op") or "").strip()
        if not op_raw or len(op_raw) > 50:
            raise LegacyContinuityError("PAYLOAD_REJECTED", "closure.op es invalido")
        key = normalize_text(op_raw)
        if key in seen:
            raise LegacyContinuityError("PAYLOAD_REJECTED", "closure.op esta duplicado")
        seen.add(key)
        closed_at_local = closure.get("closed_at_local")
        parsed.append(
            {
                "op_raw": op_raw,
                "op_normalized": key,
                "mold_raw": closure.get("mold"),
                "reason_raw": closure.get("reason"),
                "closed_at_local": closed_at_local,
                "closed_at_utc": _local_datetime(closed_at_local, "closed_at_local"),
            }
        )

    EstacionCierreOpLegacy.query.filter_by(
        import_id=import_id,
        station_id=station_id,
    ).delete(synchronize_session=False)
    for values in parsed:
        db.session.add(
            EstacionCierreOpLegacy(
                import_id=import_id,
                station_id=station_id,
                **values,
            )
        )


def process_history_delta(station, payload, batch_id, idempotency_key):
    if not isinstance(payload, dict):
        raise LegacyContinuityError("PAYLOAD_REJECTED", "JSON invalido")
    canonical_batch_id = _uuid(batch_id, "batch_id")
    if canonical_batch_id != _uuid(payload.get("batch_id"), "batch_id"):
        raise LegacyContinuityError("PAYLOAD_REJECTED", "batch_id no coincide con la ruta")
    if idempotency_key != canonical_batch_id:
        raise LegacyContinuityError(
            "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key debe coincidir con batch_id"
        )
    if payload.get("contract_version") != "station-legacy-continuity-v1":
        raise LegacyContinuityError("CONTRACT_CONFLICT", "contract_version no soportado")
    rows = payload.get("rows")
    closures = payload.get("closures")
    if not isinstance(rows, list) or len(rows) > 500:
        raise LegacyContinuityError("PAYLOAD_REJECTED", "rows debe contener hasta 500 filas")
    if not isinstance(closures, list) or len(closures) > 500:
        raise LegacyContinuityError(
            "PAYLOAD_REJECTED", "closures debe contener hasta 500 filas"
        )
    row_ids = [row.get("legacy_id") for row in rows if isinstance(row, dict)]
    if len(row_ids) != len(rows) or len(row_ids) != len(set(row_ids)):
        raise LegacyContinuityError("PAYLOAD_REJECTED", "legacy_id duplicado o invalido")

    payload_hash = _hash(payload)
    existing_batch = db.session.get(EstacionDeltaPesajeLegacy, canonical_batch_id)
    if existing_batch is not None:
        if existing_batch.station_id != station.station_id or existing_batch.payload_hash != payload_hash:
            raise LegacyContinuityError(
                "IDEMPOTENCY_CONFLICT", "batch_id ya fue usado con otro contenido", 409
            )
        return _delta_ack(existing_batch)

    current_import = _current_import(station.station_id)
    created = 0
    for row in rows:
        values = _capture_values(station.station_id, current_import.import_id, row)
        capture = EstacionPesajeLegacy.query.filter_by(
            station_id=station.station_id,
            legacy_pesaje_id=row["legacy_id"],
        ).one_or_none()
        if capture is None:
            db.session.add(EstacionPesajeLegacy(**values))
            created += 1
        elif capture.row_hash != values["row_hash"]:
            raise LegacyContinuityError(
                "LEGACY_ROW_CONFLICT",
                f"legacy_pesaje_id {row['legacy_id']} cambio fuera del flujo auditado",
                409,
            )

    _replace_closure_snapshot(station.station_id, current_import.import_id, closures)
    db.session.flush()
    receipt = EstacionDeltaPesajeLegacy(
        batch_id=canonical_batch_id,
        station_id=station.station_id,
        payload_hash=payload_hash,
        rows_received=len(rows),
        rows_created=created,
        high_watermark=_high_watermark(station.station_id),
        received_at_utc=_now(),
    )
    db.session.add(receipt)
    db.session.commit()
    return _delta_ack(receipt)


def _delta_ack(receipt):
    return {
        "accepted": True,
        "station_id": receipt.station_id,
        "batch_id": receipt.batch_id,
        "rows_received": receipt.rows_received,
        "rows_created": receipt.rows_created,
        "high_watermark": receipt.high_watermark,
        "received_at_utc": receipt.received_at_utc.isoformat(),
    }


def create_pilot_command(station_id, action, payload):
    if action not in ACTIONS:
        raise LegacyContinuityError("INVALID_ACTION", "Accion piloto no soportada")
    if not isinstance(payload, dict):
        raise LegacyContinuityError("PAYLOAD_REJECTED", "JSON invalido")
    command_id = _uuid(payload.get("command_id"), "command_id")
    requested_by = str(payload.get("requested_by") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not requested_by or len(requested_by) > 120:
        raise LegacyContinuityError("REQUESTED_BY_REQUIRED", "requested_by es requerido")
    if not reason or len(reason) > 500:
        raise LegacyContinuityError("REASON_REQUIRED", "reason es requerido")

    legacy_id = payload.get("legacy_pesaje_id")
    op_raw = str(payload.get("op") or "").strip() or None
    if action == "VOID_CAPTURE":
        if isinstance(legacy_id, bool):
            raise LegacyContinuityError("TARGET_REQUIRED", "legacy_pesaje_id es requerido")
        try:
            legacy_id = int(legacy_id)
        except (TypeError, ValueError) as exc:
            raise LegacyContinuityError(
                "TARGET_REQUIRED", "legacy_pesaje_id es requerido"
            ) from exc
        capture = EstacionPesajeLegacy.query.filter_by(
            station_id=station_id, legacy_pesaje_id=legacy_id
        ).one_or_none()
        if capture is None:
            raise LegacyContinuityError("CAPTURE_NOT_FOUND", "Pesaje no encontrado", 404)
        if capture.is_deleted:
            raise LegacyContinuityError("CAPTURE_ALREADY_VOID", "El pesaje ya esta anulado", 409)
        op_raw = capture.op_raw
    else:
        legacy_id = None
        if not op_raw or len(op_raw) > 50:
            raise LegacyContinuityError("TARGET_REQUIRED", "op es requerida")
        if not EstacionPesajeLegacy.query.filter_by(
            station_id=station_id,
            op_raw=op_raw,
        ).first():
            raise LegacyContinuityError(
                "LEGACY_ORDER_NOT_FOUND", "OP legacy no encontrada", 404
            )

    canonical = {
        "command_id": command_id,
        "station_id": station_id,
        "action": action,
        "legacy_pesaje_id": legacy_id,
        "op": op_raw,
        "requested_by": requested_by,
        "reason": reason,
    }
    payload_hash = _hash(canonical)
    existing = db.session.get(EstacionComandoPiloto, command_id)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise LegacyContinuityError(
                "IDEMPOTENCY_CONFLICT", "command_id ya fue usado con otro contenido", 409
            )
        return command_dict(existing)

    open_query = EstacionComandoPiloto.query.filter(
        EstacionComandoPiloto.station_id == station_id,
        EstacionComandoPiloto.status.in_(OPEN_COMMAND_STATES),
    )
    if action == "VOID_CAPTURE":
        open_query = open_query.filter(
            EstacionComandoPiloto.action == action,
            EstacionComandoPiloto.legacy_pesaje_id == legacy_id,
        )
    else:
        open_query = open_query.filter(
            EstacionComandoPiloto.action.in_(("CLOSE_OP", "REOPEN_OP")),
            EstacionComandoPiloto.op_raw == op_raw,
        )
    open_command = open_query.first()
    if open_command is not None:
        raise LegacyContinuityError(
            "COMMAND_ALREADY_PENDING", "Ya existe un comando pendiente para el objetivo", 409
        )

    if action in {"CLOSE_OP", "REOPEN_OP"}:
        current_import = _current_import(station_id)
        closure = EstacionCierreOpLegacy.query.filter_by(
            import_id=current_import.import_id,
            station_id=station_id,
            op_raw=op_raw,
        ).one_or_none()
        if action == "CLOSE_OP" and closure is not None:
            raise LegacyContinuityError(
                "ORDER_ALREADY_CLOSED", "La OP ya esta cerrada", 409
            )
        if action == "REOPEN_OP" and closure is None:
            raise LegacyContinuityError(
                "ORDER_ALREADY_OPEN", "La OP ya esta abierta", 409
            )

    command = EstacionComandoPiloto(
        command_id=command_id,
        station_id=station_id,
        action=action,
        legacy_pesaje_id=legacy_id,
        op_raw=op_raw,
        requested_by=requested_by,
        reason=reason,
        payload_hash=payload_hash,
        status="PENDING",
    )
    db.session.add(command)
    db.session.commit()
    return command_dict(command)


def pending_commands(station_id, limit=20):
    commands = (
        EstacionComandoPiloto.query.filter(
            EstacionComandoPiloto.station_id == station_id,
            EstacionComandoPiloto.status.in_(OPEN_COMMAND_STATES),
        )
        .order_by(EstacionComandoPiloto.requested_at_utc, EstacionComandoPiloto.command_id)
        .limit(limit)
        .all()
    )
    now = _now()
    for command in commands:
        if command.status == "PENDING":
            command.status = "DELIVERED"
            command.delivered_at_utc = now
    db.session.commit()
    return [command_dict(command) for command in commands]


def acknowledge_command(station_id, command_id, payload):
    command_id = _uuid(command_id, "command_id")
    command = db.session.get(EstacionComandoPiloto, command_id)
    if command is None or command.station_id != station_id:
        raise LegacyContinuityError("COMMAND_NOT_FOUND", "Comando no encontrado", 404)
    if command.status in {"APPLIED", "FAILED"}:
        return command_dict(command)
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"APPLIED", "FAILED"}:
        raise LegacyContinuityError("INVALID_STATUS", "status debe ser APPLIED o FAILED")
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        raise LegacyContinuityError("PAYLOAD_REJECTED", "result debe ser objeto")

    if status == "APPLIED":
        _apply_result(command, result)
    command.status = status
    command.applied_at_utc = _now()
    command.error_code = payload.get("error_code") if status == "FAILED" else None
    command.result_json = _canonical_json(result)
    db.session.commit()
    return command_dict(command)


def _apply_result(command, result):
    current_import = _current_import(command.station_id)
    if command.action == "VOID_CAPTURE":
        capture = EstacionPesajeLegacy.query.filter_by(
            station_id=command.station_id,
            legacy_pesaje_id=command.legacy_pesaje_id,
        ).one()
        deleted_at_local = result.get("deleted_at_local")
        capture.deleted_at_local = deleted_at_local
        capture.deleted_at_utc = _local_datetime(deleted_at_local, "deleted_at_local")
        capture.is_deleted = True
        return

    existing = EstacionCierreOpLegacy.query.filter_by(
        import_id=current_import.import_id,
        station_id=command.station_id,
        op_raw=command.op_raw,
    ).one_or_none()
    if command.action == "REOPEN_OP":
        if existing is not None:
            db.session.delete(existing)
        return
    closed_at_local = result.get("closed_at_local")
    values = {
        "op_normalized": normalize_text(command.op_raw),
        "mold_raw": result.get("mold"),
        "reason_raw": command.reason,
        "closed_at_local": closed_at_local,
        "closed_at_utc": _local_datetime(closed_at_local, "closed_at_local"),
    }
    if existing is None:
        db.session.add(
            EstacionCierreOpLegacy(
                import_id=current_import.import_id,
                station_id=command.station_id,
                op_raw=command.op_raw,
                **values,
            )
        )
    else:
        for name, value in values.items():
            setattr(existing, name, value)


def command_dict(command):
    return {
        "command_id": command.command_id,
        "station_id": command.station_id,
        "action": command.action,
        "legacy_pesaje_id": command.legacy_pesaje_id,
        "op": command.op_raw,
        "requested_by": command.requested_by,
        "reason": command.reason,
        "status": command.status,
        "requested_at_utc": command.requested_at_utc.isoformat(),
        "delivered_at_utc": (
            command.delivered_at_utc.isoformat() if command.delivered_at_utc else None
        ),
        "applied_at_utc": (
            command.applied_at_utc.isoformat() if command.applied_at_utc else None
        ),
        "error_code": command.error_code,
    }
