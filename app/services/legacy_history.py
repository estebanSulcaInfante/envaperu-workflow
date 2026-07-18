import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker

from app.extensions import db
from app.models.legacy_pesaje import (
    EstacionComandoPiloto,
    EstacionCierreOpLegacy,
    EstacionImportacionPesajeLegacy,
    EstacionImportacionPesajeLegacyChunk,
    EstacionImportacionPesajeLegacyFila,
    EstacionPesajeLegacy,
)
from app.models.orden import OrdenProduccion


LIMA = ZoneInfo("America/Lima")
SOURCE = "LOCAL_REPORTED_LEGACY"


class LegacyHistoryValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class LegacyHistoryConflict(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now():
    return datetime.now(timezone.utc)


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _hash_json(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _uuid(value, field):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            f"{field} debe ser UUID",
        ) from exc


def normalize_text(value):
    if value is None:
        return None
    normalized = " ".join(str(value).strip().upper().split())
    return normalized or None


def op_resolution_status(value):
    return (
        "FORMATO_VALIDO"
        if value is not None and re.fullmatch(r"OP-\d{4}", value)
        else "PENDIENTE_MAPEO"
    )


def _local_datetime(value, field):
    if not isinstance(value, str):
        raise LegacyHistoryValidationError("PAYLOAD_REJECTED", f"{field} invalido")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            f"{field} invalido",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LIMA)
    return parsed.astimezone(timezone.utc)


def _weight(value):
    try:
        parsed = Decimal(str(value)).quantize(Decimal("0.001"))
    except (InvalidOperation, TypeError) as exc:
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            "weight_kg invalido",
        ) from exc
    if parsed <= 0:
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            "weight_kg debe ser mayor que cero",
        )
    return parsed


def _format_kg(value):
    return format(Decimal(value or 0).quantize(Decimal("0.001")), "f")


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@lru_cache(maxsize=1)
def _validator():
    path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "station-legacy-history-v1"
        / "contract.schema.json"
    )
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    selected_schema = {
        **schema,
        "$ref": "#/$defs/request",
    }
    return Draft202012Validator(
        selected_schema,
        format_checker=FormatChecker(),
    )


def _validate_payload(payload):
    errors = sorted(
        _validator().iter_errors(payload),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "payload"
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            f"payload invalido en {location}: {error.message}",
        )
    manifest = payload["manifest"]
    if (
        manifest["source_active_rows"] + manifest["source_deleted_rows"]
        != manifest["source_total_rows"]
    ):
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            "active_rows + deleted_rows debe coincidir con total_rows",
        )


def _ack(import_record, chunk_index, received_at):
    return {
        "accepted": True,
        "station_id": import_record.station_id,
        "import_id": import_record.import_id,
        "chunk_index": chunk_index,
        "chunks_received": import_record.chunks_received,
        "total_chunks": import_record.total_chunks,
        "status": import_record.status,
        "received_at_utc": _iso(received_at),
    }


def _create_import(station, payload):
    manifest = payload["manifest"]
    manifest_json = _canonical_json(manifest)
    existing_source = EstacionImportacionPesajeLegacy.query.filter_by(
        station_id=station.station_id,
        source_sha256=manifest["source_sha256"].lower(),
    ).one_or_none()
    if existing_source is not None and existing_source.import_id != payload["import_id"]:
        raise LegacyHistoryConflict(
            "SOURCE_ALREADY_IMPORTED",
            "La misma fuente ya fue registrada con otro import_id",
        )
    return EstacionImportacionPesajeLegacy(
        import_id=payload["import_id"],
        station_id=station.station_id,
        source_sha256=manifest["source_sha256"].lower(),
        source_size_bytes=manifest["source_size_bytes"],
        source_schema_version=manifest["source_schema_version"],
        source_total_rows=manifest["source_total_rows"],
        source_active_rows=manifest["source_active_rows"],
        source_deleted_rows=manifest["source_deleted_rows"],
        source_first_capture_local=manifest["source_first_capture_local"],
        source_last_capture_local=manifest["source_last_capture_local"],
        manifest_json=manifest_json,
        total_chunks=payload["total_chunks"],
        status="RECEIVING",
    )


def _assert_import_matches(import_record, station, payload):
    if import_record.station_id != station.station_id:
        raise LegacyHistoryConflict(
            "IMPORT_STATION_CONFLICT",
            "import_id pertenece a otra estacion",
        )
    if (
        import_record.manifest_json != _canonical_json(payload["manifest"])
        or import_record.total_chunks != payload["total_chunks"]
    ):
        raise LegacyHistoryConflict(
            "IMPORT_MANIFEST_CONFLICT",
            "import_id fue usado con otro manifiesto",
        )


def _capture_values(station_id, import_id, row):
    captured_at_utc = _local_datetime(row["captured_at_local"], "captured_at_local")
    deleted_at_utc = (
        _local_datetime(row["deleted_at_local"], "deleted_at_local")
        if row["deleted_at_local"]
        else None
    )
    op_raw = row["op"]
    op_normalized = normalize_text(op_raw)
    return {
        "station_id": station_id,
        "legacy_pesaje_id": row["legacy_id"],
        "first_import_id": import_id,
        "row_hash": _hash_json(row),
        "weight_kg": _weight(row["weight_kg"]),
        "captured_at_local": row["captured_at_local"],
        "captured_at_utc": captured_at_utc,
        "operational_date": captured_at_utc.astimezone(LIMA).date(),
        "deleted_at_local": row["deleted_at_local"],
        "deleted_at_utc": deleted_at_utc,
        "is_deleted": deleted_at_utc is not None,
        "op_raw": op_raw,
        "op_normalized": op_normalized,
        "op_resolution_status": op_resolution_status(op_normalized),
        "ot_raw": row["ot"],
        "ot_normalized": normalize_text(row["ot"]),
        "mold_raw": row["mold"],
        "mold_normalized": normalize_text(row["mold"]),
        "color_raw": row["color"],
        "color_normalized": normalize_text(row["color"]),
        "machine_raw": row["machine_code"],
        "machine_normalized": normalize_text(row["machine_code"]),
        "shift_raw": row["shift"],
        "shift_normalized": normalize_text(row["shift"]),
        "operator_raw": row["operator"],
        "operator_normalized": normalize_text(row["operator"]),
        "raw_payload_json": _canonical_json(row["raw"]),
    }


def process_legacy_history_chunk(station, payload, import_id, chunk_index, idempotency_key):
    if not isinstance(payload, dict):
        raise LegacyHistoryValidationError("PAYLOAD_REJECTED", "JSON invalido")
    _validate_payload(payload)
    canonical_import_id = _uuid(payload["import_id"], "import_id")
    if canonical_import_id != _uuid(import_id, "import_id"):
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            "import_id no coincide con la ruta",
        )
    if payload["chunk_index"] != chunk_index:
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            "chunk_index no coincide con la ruta",
        )
    if idempotency_key != f"{canonical_import_id}:{chunk_index}":
        raise LegacyHistoryValidationError(
            "IDEMPOTENCY_KEY_INVALID",
            "Idempotency-Key no coincide con import_id y chunk_index",
        )

    payload_hash = _hash_json(payload)
    import_record = db.session.get(
        EstacionImportacionPesajeLegacy,
        canonical_import_id,
    )
    if import_record is None:
        import_record = _create_import(station, payload)
        db.session.add(import_record)
        db.session.flush()
    else:
        _assert_import_matches(import_record, station, payload)

    existing_chunk = EstacionImportacionPesajeLegacyChunk.query.filter_by(
        import_id=canonical_import_id,
        chunk_index=chunk_index,
    ).one_or_none()
    if existing_chunk is not None:
        if existing_chunk.payload_hash != payload_hash:
            raise LegacyHistoryConflict(
                "IDEMPOTENCY_CONFLICT",
                "El fragmento ya fue usado con otro contenido",
            )
        return _ack(import_record, chunk_index, existing_chunk.received_at_utc)

    row_ids = [row["legacy_id"] for row in payload["rows"]]
    if len(row_ids) != len(set(row_ids)):
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            "legacy_id duplicado dentro del fragmento",
        )
    closure_ops = [normalize_text(item["op"]) for item in payload["closures"]]
    if len(closure_ops) != len(set(closure_ops)):
        raise LegacyHistoryValidationError(
            "PAYLOAD_REJECTED",
            "cierre OP duplicado dentro del fragmento",
        )

    for row in payload["rows"]:
        values = _capture_values(station.station_id, canonical_import_id, row)
        capture = EstacionPesajeLegacy.query.filter_by(
            station_id=station.station_id,
            legacy_pesaje_id=row["legacy_id"],
        ).one_or_none()
        if capture is None:
            capture = EstacionPesajeLegacy(**values)
            db.session.add(capture)
            db.session.flush()
        elif capture.row_hash != values["row_hash"]:
            raise LegacyHistoryConflict(
                "LEGACY_ROW_CONFLICT",
                f"legacy_pesaje_id {row['legacy_id']} ya existe con otro contenido",
            )

        linked = EstacionImportacionPesajeLegacyFila.query.filter_by(
            import_id=canonical_import_id,
            capture_id=capture.id,
        ).one_or_none()
        if linked is None:
            db.session.add(
                EstacionImportacionPesajeLegacyFila(
                    import_id=canonical_import_id,
                    capture_id=capture.id,
                )
            )

    for closure in payload["closures"]:
        normalized_op = normalize_text(closure["op"])
        db.session.add(
            EstacionCierreOpLegacy(
                import_id=canonical_import_id,
                station_id=station.station_id,
                op_raw=closure["op"],
                op_normalized=normalized_op,
                mold_raw=closure["mold"],
                reason_raw=closure["reason"],
                closed_at_local=closure["closed_at_local"],
                closed_at_utc=_local_datetime(
                    closure["closed_at_local"],
                    "closed_at_local",
                ),
            )
        )

    received_at = _utc_now()
    db.session.add(
        EstacionImportacionPesajeLegacyChunk(
            import_id=canonical_import_id,
            chunk_index=chunk_index,
            payload_hash=payload_hash,
            rows_count=len(payload["rows"]),
            received_at_utc=received_at,
        )
    )
    db.session.flush()
    import_record.chunks_received = EstacionImportacionPesajeLegacyChunk.query.filter_by(
        import_id=canonical_import_id
    ).count()

    if import_record.chunks_received == import_record.total_chunks:
        links = EstacionImportacionPesajeLegacyFila.query.filter_by(
            import_id=canonical_import_id
        ).all()
        capture_ids = [item.capture_id for item in links]
        captures = (
            EstacionPesajeLegacy.query.filter(EstacionPesajeLegacy.id.in_(capture_ids)).all()
            if capture_ids
            else []
        )
        active_count = sum(not item.is_deleted for item in captures)
        deleted_count = sum(item.is_deleted for item in captures)
        if (
            len(captures) != import_record.source_total_rows
            or active_count != import_record.source_active_rows
            or deleted_count != import_record.source_deleted_rows
        ):
            raise LegacyHistoryValidationError(
                "IMPORT_COUNTS_MISMATCH",
                "Los conteos importados no coinciden con el manifiesto",
            )
        import_record.status = "COMPLETE"
        import_record.completed_at_utc = received_at

    db.session.commit()
    return _ack(import_record, chunk_index, received_at)


def _latest_complete_imports():
    records = EstacionImportacionPesajeLegacy.query.filter_by(status="COMPLETE").all()
    latest = {}
    for record in records:
        current = latest.get(record.station_id)
        ordering = record.completed_at_utc or record.started_at_utc
        current_ordering = (
            current.completed_at_utc or current.started_at_utc
            if current is not None
            else None
        )
        if current is None or ordering > current_ordering:
            latest[record.station_id] = record
    return latest


def _current_snapshot_captures():
    imports = _latest_complete_imports()
    station_ids = list(imports)
    if not station_ids:
        return imports, []
    captures = EstacionPesajeLegacy.query.filter(
        EstacionPesajeLegacy.station_id.in_(station_ids)
    ).all()
    return imports, [
        (imports[capture.station_id].import_id, capture) for capture in captures
    ]


def legacy_production_orders(*, page=1, per_page=50, query=None, status=None):
    imports, pairs = _current_snapshot_captures()
    import_ids = [record.import_id for record in imports.values()]
    closures = (
        EstacionCierreOpLegacy.query.filter(
            EstacionCierreOpLegacy.import_id.in_(import_ids)
        ).all()
        if import_ids
        else []
    )
    closure_map = {
        (item.import_id, item.station_id, item.op_raw): item for item in closures
    }
    pending_commands = EstacionComandoPiloto.query.filter(
        EstacionComandoPiloto.status.in_(("PENDING", "DELIVERED"))
    ).all()
    pending_by_order = {
        (item.station_id, item.op_raw): item
        for item in pending_commands
        if item.action in {"CLOSE_OP", "REOPEN_OP"}
    }
    central_orders = {
        item.numero_op: item
        for item in OrdenProduccion.query.filter(
            OrdenProduccion.numero_op.in_(
                sorted({capture.op_raw for _, capture in pairs if capture.op_raw})
            )
        ).all()
    }

    groups = {}
    for import_id, capture in pairs:
        key = (import_id, capture.station_id, capture.op_raw or "SIN OP")
        group = groups.setdefault(
            key,
            {
                "import_id": import_id,
                "station_id": capture.station_id,
                "op_raw": capture.op_raw or "SIN OP",
                "op_normalized": capture.op_normalized,
                "resolution_status": capture.op_resolution_status,
                "active_bags": 0,
                "deleted_bags": 0,
                "active_weight_kg": Decimal("0"),
                "first_capture_at_utc": capture.captured_at_utc,
                "last_capture_at_utc": capture.captured_at_utc,
                "molds": set(),
                "colors": set(),
                "machines": set(),
            },
        )
        group["first_capture_at_utc"] = min(
            group["first_capture_at_utc"], capture.captured_at_utc
        )
        group["last_capture_at_utc"] = max(
            group["last_capture_at_utc"], capture.captured_at_utc
        )
        if capture.mold_normalized:
            group["molds"].add(capture.mold_normalized)
        if capture.color_normalized:
            group["colors"].add(capture.color_normalized)
        if capture.machine_normalized:
            group["machines"].add(capture.machine_normalized)
        if capture.is_deleted:
            group["deleted_bags"] += 1
        else:
            group["active_bags"] += 1
            group["active_weight_kg"] += Decimal(capture.weight_kg)

    items = []
    for key, group in groups.items():
        import_id, station_id, op_raw = key
        central = central_orders.get(op_raw)
        closure = closure_map.get((import_id, station_id, op_raw))
        pending = pending_by_order.get((station_id, op_raw))
        if pending is not None:
            visible_status = (
                "CIERRE_PENDIENTE"
                if pending.action == "CLOSE_OP"
                else "REAPERTURA_PENDIENTE"
            )
        elif closure is not None:
            visible_status = "CERRADA_LEGACY"
        else:
            visible_status = "ABIERTA_PILOTO"
        items.append(
            {
                **{
                    name: value
                    for name, value in group.items()
                    if name
                    not in {
                        "active_weight_kg",
                        "first_capture_at_utc",
                        "last_capture_at_utc",
                        "molds",
                        "colors",
                        "machines",
                    }
                },
                "status": visible_status,
                "mapping_status": group["resolution_status"],
                "central_status": (
                    ("ACTIVA_CENTRAL" if central.activa else "CERRADA_CENTRAL")
                    if central is not None
                    else None
                ),
                "active_weight_kg": _format_kg(group["active_weight_kg"]),
                "first_capture_at_utc": _iso(group["first_capture_at_utc"]),
                "last_capture_at_utc": _iso(group["last_capture_at_utc"]),
                "molds": sorted(group["molds"]),
                "colors": sorted(group["colors"]),
                "machines": sorted(group["machines"]),
                "closure": (
                    {
                        "reason": closure.reason_raw,
                        "closed_at_utc": _iso(closure.closed_at_utc),
                    }
                    if closure is not None
                    else None
                ),
                "source": SOURCE,
                "pending_command": (
                    {
                        "command_id": pending.command_id,
                        "action": pending.action,
                        "status": pending.status,
                    }
                    if pending is not None
                    else None
                ),
            }
        )

    query_normalized = normalize_text(query)
    if query_normalized:
        items = [
            item
            for item in items
            if query_normalized in normalize_text(item["op_raw"])
            or any(query_normalized in value for value in item["molds"])
        ]
    if status:
        if status == "PENDIENTE_MAPEO":
            items = [item for item in items if item["mapping_status"] == status]
        else:
            items = [item for item in items if item["status"] == status]
    items.sort(
        key=lambda item: (item["last_capture_at_utc"] or "", item["op_raw"]),
        reverse=True,
    )
    total = len(items)
    start = (page - 1) * per_page
    paged = items[start : start + per_page]
    return {
        "items": paged,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page if total else 0,
        },
        "summary": {
            "raw_orders": len(groups),
            "active_bags": sum(item["active_bags"] for item in items),
            "active_weight_kg": _format_kg(
                sum((Decimal(item["active_weight_kg"]) for item in items), Decimal("0"))
            ),
            "closed_orders": sum(
                item["status"] in {"CERRADA_LEGACY", "CERRADA_CENTRAL"}
                for item in items
            ),
            "pending_mapping_orders": sum(
                item["mapping_status"] == "PENDIENTE_MAPEO" for item in items
            ),
        },
        "source": SOURCE,
    }


def legacy_production_order_detail(station_id, op_raw, *, page=1, per_page=100):
    imports, pairs = _current_snapshot_captures()
    current_import = imports.get(station_id)
    if current_import is None:
        return None
    captures = [
        capture
        for import_id, capture in pairs
        if import_id == current_import.import_id
        and capture.station_id == station_id
        and (capture.op_raw or "SIN OP") == op_raw
    ]
    if not captures:
        return None
    captures.sort(key=lambda item: (item.captured_at_utc, item.legacy_pesaje_id), reverse=True)
    start = (page - 1) * per_page
    selected = captures[start : start + per_page]
    pending_voids = {
        item.legacy_pesaje_id: item
        for item in EstacionComandoPiloto.query.filter(
            EstacionComandoPiloto.station_id == station_id,
            EstacionComandoPiloto.action == "VOID_CAPTURE",
            EstacionComandoPiloto.status.in_(("PENDING", "DELIVERED")),
        ).all()
    }
    return {
        "station_id": station_id,
        "op_raw": op_raw,
        "source": SOURCE,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": len(captures),
            "pages": (len(captures) + per_page - 1) // per_page,
        },
        "captures": [
            {
                "legacy_id": item.legacy_pesaje_id,
                "weight_kg": _format_kg(item.weight_kg),
                "captured_at_utc": _iso(item.captured_at_utc),
                "captured_at_local": item.captured_at_local,
                "is_deleted": item.is_deleted,
                "deleted_at_utc": _iso(item.deleted_at_utc),
                "ot": item.ot_raw,
                "mold": item.mold_raw,
                "color": item.color_raw,
                "machine_code": item.machine_raw,
                "shift": item.shift_raw,
                "operator": item.operator_raw,
                "pending_command": (
                    {
                        "command_id": pending_voids[item.legacy_pesaje_id].command_id,
                        "status": pending_voids[item.legacy_pesaje_id].status,
                    }
                    if item.legacy_pesaje_id in pending_voids
                    else None
                ),
            }
            for item in selected
        ],
    }
