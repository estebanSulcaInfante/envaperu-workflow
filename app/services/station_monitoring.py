import hashlib
import json
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.estacion_pesaje import (
    EstacionEstadoActual,
    EstacionEstadoHistorial,
    EstacionHeartbeatRecepcion,
)


class HeartbeatValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class HeartbeatIdempotencyConflict(RuntimeError):
    pass


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    value = _utc(value)
    return value.isoformat() if value else None


def _parse_datetime(value):
    if not isinstance(value, str):
        raise HeartbeatValidationError(
            "PAYLOAD_REJECTED",
            "Fecha UTC invalida",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HeartbeatValidationError(
            "PAYLOAD_REJECTED",
            "Fecha UTC invalida",
        ) from exc
    if parsed.tzinfo is None:
        raise HeartbeatValidationError(
            "PAYLOAD_REJECTED",
            "La fecha debe incluir zona horaria",
        )
    return parsed.astimezone(timezone.utc)


def classify_communication(
    last_received_at,
    *,
    now=None,
    delayed_seconds=90,
    disconnected_seconds=300,
):
    if last_received_at is None:
        return "NUNCA_REPORTO"
    now = _utc(now or _utc_now())
    age_seconds = max(0, (now - _utc(last_received_at)).total_seconds())
    if age_seconds <= delayed_seconds:
        return "RECIENTE"
    if age_seconds <= disconnected_seconds:
        return "ATRASADA"
    return "SIN_COMUNICACION"


@lru_cache(maxsize=1)
def _heartbeat_validator():
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "station-heartbeat-v1"
        / "contract.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema["$defs"]["request"],
        format_checker=FormatChecker(),
    )


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise HeartbeatValidationError(
            "PAYLOAD_REJECTED",
            "Se requiere un objeto JSON",
        )
    errors = sorted(
        _heartbeat_validator().iter_errors(payload),
        key=lambda item: list(item.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "payload"
    raise HeartbeatValidationError(
        "PAYLOAD_REJECTED",
        f"{location}: {error.message}",
    )


def _canonical_json(payload):
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _payload_hash(payload_json):
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _canonical_uuid(value, field):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HeartbeatValidationError(
            "PAYLOAD_REJECTED",
            f"{field} debe ser UUID",
        ) from exc


def _history_event(current, payload):
    if current is None:
        return "FIRST_HEARTBEAT"
    if current.boot_id != payload["boot_id"]:
        return "BOOT_CHANGED"
    components = payload["components"]
    if (
        current.process_state != components["process"]
        or current.database_state != components["database"]
        or current.scale_state != components["scale"]
        or current.printer_state != components["printer"]
        or current.catalog_state != components["catalog"]
    ):
        return "COMPONENT_STATE_CHANGED"
    communication = payload["communication"]
    if (
        current.communication_state != communication["state"]
        or current.last_error_code != communication["last_error_code"]
    ):
        return "COMMUNICATION_STATE_CHANGED"
    if current.app_version != payload["app_version"]:
        return "APP_VERSION_CHANGED"
    return None


def _apply_current_state(
    station_id,
    current,
    payload,
    payload_json,
    generated_at,
    received_at,
):
    components = payload["components"]
    communication = payload["communication"]
    event_type = _history_event(current, payload)
    if current is None:
        current = EstacionEstadoActual(station_id=station_id)
        db.session.add(current)

    current.heartbeat_id = payload["heartbeat_id"]
    current.boot_id = payload["boot_id"]
    current.sequence = payload["sequence"]
    current.generated_at_utc = generated_at
    current.received_at_utc = received_at
    current.clock_skew_seconds = (received_at - generated_at).total_seconds()
    current.app_version = payload["app_version"]
    current.mode = payload["mode"]
    current.process_state = components["process"]
    current.database_state = components["database"]
    current.scale_state = components["scale"]
    current.printer_state = components["printer"]
    current.catalog_state = components["catalog"]
    current.communication_state = communication["state"]
    current.last_central_ack_utc = _parse_datetime(
        communication["last_central_ack_utc"]
    ) if communication["last_central_ack_utc"] else None
    current.legacy_unsynced_count = communication["legacy_unsynced_count"]
    current.oldest_legacy_unsynced_at_utc = _parse_datetime(
        communication["oldest_legacy_unsynced_at_utc"]
    ) if communication["oldest_legacy_unsynced_at_utc"] else None
    current.last_error_code = communication["last_error_code"]
    current.context_json = _canonical_json(payload["context"])
    current.last_capture_json = (
        _canonical_json(payload["last_capture"])
        if payload["last_capture"] is not None
        else None
    )
    current.local_summary_json = _canonical_json(payload["local_summary"])
    current.payload_json = payload_json

    if event_type:
        db.session.add(
            EstacionEstadoHistorial(
                station_id=station_id,
                heartbeat_id=payload["heartbeat_id"],
                event_type=event_type,
                occurred_at_utc=received_at,
                summary_json=_canonical_json(
                    {
                        "app_version": payload["app_version"],
                        "boot_id": payload["boot_id"],
                        "components": components,
                        "last_error_code": communication["last_error_code"],
                    }
                ),
            )
        )


def _ack(receipt, next_heartbeat_seconds):
    return {
        "accepted": True,
        "station_id": receipt.station_id,
        "heartbeat_id": receipt.heartbeat_id,
        "received_at_utc": _iso(receipt.received_at_utc),
        "next_heartbeat_seconds": next_heartbeat_seconds,
    }


def process_heartbeat(
    station,
    payload,
    idempotency_key,
    *,
    received_at=None,
    next_heartbeat_seconds=30,
):
    _validate_payload(payload)
    header_id = _canonical_uuid(idempotency_key, "Idempotency-Key")
    payload_id = _canonical_uuid(payload.get("heartbeat_id"), "heartbeat_id")
    if header_id != payload_id:
        raise HeartbeatValidationError(
            "IDEMPOTENCY_KEY_MISMATCH",
            "Idempotency-Key debe coincidir con heartbeat_id",
        )

    normalized = dict(payload)
    normalized["heartbeat_id"] = payload_id
    normalized["boot_id"] = _canonical_uuid(payload.get("boot_id"), "boot_id")
    payload_json = _canonical_json(normalized)
    digest = _payload_hash(payload_json)

    existing = EstacionHeartbeatRecepcion.query.filter_by(
        heartbeat_id=payload_id
    ).one_or_none()
    if existing is not None:
        if existing.station_id != station.station_id or existing.payload_hash != digest:
            raise HeartbeatIdempotencyConflict(payload_id)
        return _ack(existing, next_heartbeat_seconds)

    received_at = _utc(received_at or _utc_now())
    generated_at = _parse_datetime(normalized["generated_at_utc"])
    current = db.session.get(EstacionEstadoActual, station.station_id)
    applies = (
        current is None
        or current.boot_id != normalized["boot_id"]
        or normalized["sequence"] > current.sequence
    )
    receipt = EstacionHeartbeatRecepcion(
        heartbeat_id=payload_id,
        payload_hash=digest,
        station_id=station.station_id,
        boot_id=normalized["boot_id"],
        sequence=normalized["sequence"],
        generated_at_utc=generated_at,
        received_at_utc=received_at,
        applied_to_current=applies,
        payload_json=payload_json,
    )
    db.session.add(receipt)
    if applies:
        _apply_current_state(
            station.station_id,
            current,
            normalized,
            payload_json,
            generated_at,
            received_at,
        )

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        concurrent = EstacionHeartbeatRecepcion.query.filter_by(
            heartbeat_id=payload_id
        ).one_or_none()
        if (
            concurrent is None
            or concurrent.station_id != station.station_id
            or concurrent.payload_hash != digest
        ):
            raise HeartbeatIdempotencyConflict(payload_id)
        return _ack(concurrent, next_heartbeat_seconds)
    return _ack(receipt, next_heartbeat_seconds)


def station_monitor_dict(
    station,
    *,
    now=None,
    delayed_seconds=90,
    disconnected_seconds=300,
):
    current = station.estado_actual
    now = _utc(now or _utc_now())
    received_at = _utc(current.received_at_utc) if current else None
    age_seconds = (
        max(0, int((now - received_at).total_seconds()))
        if received_at
        else None
    )
    body = {
        "station_id": station.station_id,
        "code": station.codigo,
        "name": station.nombre,
        "location": station.ubicacion,
        "admin_status": station.estado_admin,
        "communication_status": classify_communication(
            received_at,
            now=now,
            delayed_seconds=delayed_seconds,
            disconnected_seconds=disconnected_seconds,
        ),
        "last_received_at_utc": _iso(received_at),
        "age_seconds": age_seconds,
        "app_version": current.app_version if current else None,
        "mode": current.mode if current else None,
        "boot_id": current.boot_id if current else None,
        "sequence": current.sequence if current else None,
        "components": None,
        "communication": None,
        "context": None,
        "last_capture": None,
        "local_summary": None,
        "clock_skew_seconds": current.clock_skew_seconds if current else None,
    }
    if current is None:
        return body

    body["components"] = {
        "process": current.process_state,
        "database": current.database_state,
        "scale": current.scale_state,
        "printer": current.printer_state,
        "catalog": current.catalog_state,
    }
    body["communication"] = {
        "state_reported": current.communication_state,
        "last_central_ack_utc": _iso(current.last_central_ack_utc),
        "legacy_unsynced_count": current.legacy_unsynced_count,
        "oldest_legacy_unsynced_at_utc": _iso(
            current.oldest_legacy_unsynced_at_utc
        ),
        "last_error_code": current.last_error_code,
    }
    body["context"] = EstacionEstadoActual.decode_json(current.context_json)
    body["last_capture"] = EstacionEstadoActual.decode_json(
        current.last_capture_json
    )
    body["local_summary"] = EstacionEstadoActual.decode_json(
        current.local_summary_json
    )
    return body
