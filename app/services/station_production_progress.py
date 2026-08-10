import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import current_app
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from app.extensions import db
from app.models.estacion_pesaje import (
    EstacionAvanceProduccion,
    EstacionPesaje,
    EstacionReporteAvanceRecepcion,
)
from app.models.legacy_pesaje import EstacionPesajeLegacy
from app.models.orden import OrdenProduccion
from app.services.station_monitoring import classify_communication


SOURCE = "LOCAL_REPORTED_LEGACY"
LIMA = ZoneInfo("America/Lima")


class ProgressValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class ProgressIdempotencyConflict(RuntimeError):
    pass


def _utc_now():
    return datetime.now(timezone.utc)


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value):
    value = _utc(value)
    return value.isoformat() if value else None


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
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            f"{field} debe ser UUID",
        ) from exc


def _parse_datetime(value, field):
    if not isinstance(value, str):
        raise ProgressValidationError("PAYLOAD_REJECTED", f"{field} invalido")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            f"{field} invalido",
        ) from exc
    if parsed.tzinfo is None:
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            f"{field} debe incluir zona horaria",
        )
    return parsed.astimezone(timezone.utc)


def _parse_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            f"{field} invalido",
        ) from exc


def _dimension(value):
    if value is None:
        return None
    normalized = " ".join(str(value).strip().split()).upper()
    return normalized or None


def _decimal_kg(value):
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            "weight_kg invalido",
        ) from exc
    if parsed <= 0:
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            "weight_kg debe ser mayor que cero",
        )
    return parsed.quantize(Decimal("0.001"))


def _format_kg(value):
    return format(Decimal(value or 0).quantize(Decimal("0.001")), "f")


@lru_cache(maxsize=1)
def _progress_validator():
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "station-production-progress-v1"
        / "contract.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema["$defs"]["request"],
        format_checker=FormatChecker(),
    )


def _validate_schema(payload):
    if not isinstance(payload, dict):
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            "Se requiere un objeto JSON",
        )
    errors = sorted(
        _progress_validator().iter_errors(payload),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "payload"
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            f"{location}: {error.message}",
        )


def _normalize_rows(payload, window_start, window_end):
    normalized = []
    seen = set()
    latest_capture = None
    for raw in payload["rows"]:
        operational_date = _parse_date(raw["operational_date"], "operational_date")
        if operational_date < window_start or operational_date > window_end:
            raise ProgressValidationError(
                "PAYLOAD_REJECTED",
                "operational_date queda fuera de la ventana declarada",
            )
        first_capture = _parse_datetime(
            raw["first_capture_at_utc"],
            "first_capture_at_utc",
        )
        last_capture = _parse_datetime(
            raw["last_capture_at_utc"],
            "last_capture_at_utc",
        )
        if first_capture > last_capture:
            raise ProgressValidationError(
                "PAYLOAD_REJECTED",
                "first_capture_at_utc no puede superar last_capture_at_utc",
            )
        if (
            first_capture.astimezone(LIMA).date() != operational_date
            or last_capture.astimezone(LIMA).date() != operational_date
        ):
            raise ProgressValidationError(
                "PAYLOAD_REJECTED",
                "las capturas deben pertenecer a operational_date en America/Lima",
            )

        dimensions = {
            "op": _dimension(raw["op"]),
            "ot": _dimension(raw["ot"]),
            "mold": _dimension(raw["mold"]),
            "color": _dimension(raw["color"]),
            "machine_code": _dimension(raw["machine_code"]),
            "shift": _dimension(raw["shift"]),
        }
        identity = (operational_date, *dimensions.values())
        if identity in seen:
            raise ProgressValidationError(
                "PAYLOAD_REJECTED",
                "el snapshot contiene un grupo dimensional duplicado",
            )
        seen.add(identity)
        group_key = hashlib.sha256(
            _canonical_json(dimensions).encode("utf-8")
        ).hexdigest()
        normalized.append(
            {
                "operational_date": operational_date,
                "group_key": group_key,
                **dimensions,
                "bags": raw["bags"],
                "weight_kg": _decimal_kg(raw["weight_kg"]),
                "first_capture_at_utc": first_capture,
                "last_capture_at_utc": last_capture,
            }
        )
        latest_capture = max(
            filter(None, (latest_capture, last_capture)),
            default=None,
        )
    return normalized, latest_capture


def _ack(receipt):
    return {
        "accepted": True,
        "station_id": receipt.station_id,
        "report_id": receipt.report_id,
        "received_at_utc": _iso(receipt.received_at_utc),
        "rows_applied": receipt.rows_count,
        "window_start_date": receipt.window_start_date.isoformat(),
        "window_end_date": receipt.window_end_date.isoformat(),
    }


def process_production_progress(
    station,
    payload,
    idempotency_key,
    *,
    received_at=None,
):
    _validate_schema(payload)
    header_id = _canonical_uuid(idempotency_key, "Idempotency-Key")
    payload_id = _canonical_uuid(payload.get("report_id"), "report_id")
    if header_id != payload_id:
        raise ProgressValidationError(
            "IDEMPOTENCY_KEY_MISMATCH",
            "Idempotency-Key debe coincidir con report_id",
        )

    normalized_payload = dict(payload)
    normalized_payload["report_id"] = payload_id
    payload_json = _canonical_json(normalized_payload)
    digest = _payload_hash(payload_json)

    existing = EstacionReporteAvanceRecepcion.query.filter_by(
        report_id=payload_id
    ).one_or_none()
    if existing is not None:
        if existing.station_id != station.station_id or existing.payload_hash != digest:
            raise ProgressIdempotencyConflict(payload_id)
        return _ack(existing)

    window_start = _parse_date(payload["window_start_date"], "window_start_date")
    window_end = _parse_date(payload["window_end_date"], "window_end_date")
    if window_start > window_end or (window_end - window_start).days > 30:
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            "la ventana debe contener entre 1 y 31 dias",
        )
    generated_at = _parse_datetime(payload["generated_at_utc"], "generated_at_utc")
    rows, latest_capture = _normalize_rows(payload, window_start, window_end)
    if latest_capture and generated_at < latest_capture:
        raise ProgressValidationError(
            "PAYLOAD_REJECTED",
            "generated_at_utc no puede ser anterior a la ultima captura",
        )

    received_at = _utc(received_at or _utc_now())
    receipt = EstacionReporteAvanceRecepcion(
        report_id=payload_id,
        payload_hash=digest,
        station_id=station.station_id,
        generated_at_utc=generated_at,
        received_at_utc=received_at,
        window_start_date=window_start,
        window_end_date=window_end,
        rows_count=len(rows),
        payload_json=payload_json,
    )

    EstacionAvanceProduccion.query.filter(
        EstacionAvanceProduccion.station_id == station.station_id,
        EstacionAvanceProduccion.operational_date >= window_start,
        EstacionAvanceProduccion.operational_date <= window_end,
    ).delete(synchronize_session=False)
    try:
        db.session.add(receipt)
        # No ORM relationship links the receipt to its rows. Flush the parent
        # explicitly so PostgreSQL can enforce the report_id foreign key.
        db.session.flush()
        for row in rows:
            db.session.add(
                EstacionAvanceProduccion(
                    station_id=station.station_id,
                    report_id=payload_id,
                    report_received_at_utc=received_at,
                    **row,
                )
            )
        db.session.commit()
    except IntegrityError:
        current_app.logger.exception(
            "No se pudo persistir el reporte de avance %s de la estacion %s",
            payload_id,
            station.station_id,
        )
        db.session.rollback()
        concurrent = EstacionReporteAvanceRecepcion.query.filter_by(
            report_id=payload_id
        ).one_or_none()
        if (
            concurrent is None
            or concurrent.station_id != station.station_id
            or concurrent.payload_hash != digest
        ):
            raise ProgressIdempotencyConflict(payload_id)
        return _ack(concurrent)
    return _ack(receipt)


def _station_status(station, now):
    current = station.estado_actual
    return classify_communication(
        current.received_at_utc if current else None,
        now=now,
    )


def _month_bounds(operational_date):
    period_start = operational_date.replace(day=1)
    next_month = (
        date(period_start.year + 1, 1, 1)
        if period_start.month == 12
        else date(period_start.year, period_start.month + 1, 1)
    )
    return period_start, next_month - timedelta(days=1)


def _monthly_progress_summary(
    operational_date,
    *,
    op=None,
    machine_code=None,
    shift=None,
):
    period_start, period_end = _month_bounds(operational_date)
    detailed_station_dates = set(
        db.session.query(
            EstacionPesajeLegacy.station_id,
            EstacionPesajeLegacy.operational_date,
        )
        .filter(EstacionPesajeLegacy.operational_date.between(period_start, period_end))
        .distinct()
        .all()
    )

    capture_query = EstacionPesajeLegacy.query.options(
        load_only(
            EstacionPesajeLegacy.station_id,
            EstacionPesajeLegacy.op_normalized,
            EstacionPesajeLegacy.ot_normalized,
            EstacionPesajeLegacy.mold_normalized,
            EstacionPesajeLegacy.color_normalized,
            EstacionPesajeLegacy.machine_normalized,
            EstacionPesajeLegacy.shift_normalized,
            EstacionPesajeLegacy.weight_kg,
            EstacionPesajeLegacy.captured_at_utc,
            EstacionPesajeLegacy.operational_date,
            EstacionPesajeLegacy.imported_at_utc,
        )
    ).filter(
        EstacionPesajeLegacy.operational_date.between(period_start, period_end),
        EstacionPesajeLegacy.is_deleted.is_(False),
    )
    if op:
        capture_query = capture_query.filter(
            EstacionPesajeLegacy.op_normalized == _dimension(op)
        )
    if machine_code:
        capture_query = capture_query.filter(
            EstacionPesajeLegacy.machine_normalized == _dimension(machine_code)
        )
    if shift:
        capture_query = capture_query.filter(
            EstacionPesajeLegacy.shift_normalized == _dimension(shift)
        )
    captures = capture_query.all()

    aggregate_query = EstacionAvanceProduccion.query.filter(
        EstacionAvanceProduccion.operational_date.between(period_start, period_end)
    )
    if op:
        aggregate_query = aggregate_query.filter(
            EstacionAvanceProduccion.op == _dimension(op)
        )
    if machine_code:
        aggregate_query = aggregate_query.filter(
            EstacionAvanceProduccion.machine_code == _dimension(machine_code)
        )
    if shift:
        aggregate_query = aggregate_query.filter(
            EstacionAvanceProduccion.shift == _dimension(shift)
        )
    aggregate_rows = [
        row
        for row in aggregate_query.all()
        if (row.station_id, row.operational_date) not in detailed_station_dates
    ]

    total_weight = sum(
        (Decimal(row.weight_kg) for row in captures),
        Decimal("0"),
    ) + sum(
        (Decimal(row.weight_kg) for row in aggregate_rows),
        Decimal("0"),
    )
    production_days = {row.operational_date for row in captures} | {
        row.operational_date for row in aggregate_rows
    }
    production_orders = {
        row.op_normalized for row in captures if row.op_normalized
    } | {row.op for row in aggregate_rows if row.op}
    daily_average = (
        total_weight / len(production_days)
        if production_days
        else Decimal("0")
    )
    return {
        "month": period_start.strftime("%Y-%m"),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "bags": len(captures) + sum(row.bags for row in aggregate_rows),
        "weight_kg": _format_kg(total_weight),
        "production_orders": len(production_orders),
        "production_days": len(production_days),
        "average_daily_weight_kg": _format_kg(daily_average),
    }


def production_progress_dashboard(
    operational_date,
    *,
    period="DAY",
    op=None,
    machine_code=None,
    shift=None,
    now=None,
):
    now = _utc(now or _utc_now())
    period = period.upper()
    period_start, period_end = (
        _month_bounds(operational_date)
        if period == "MONTH"
        else (operational_date, operational_date)
    )
    query = EstacionAvanceProduccion.query.filter(
        EstacionAvanceProduccion.operational_date.between(period_start, period_end)
    )
    if op:
        query = query.filter(EstacionAvanceProduccion.op == _dimension(op))
    if machine_code:
        query = query.filter(
            EstacionAvanceProduccion.machine_code == _dimension(machine_code)
        )
    if shift:
        query = query.filter(EstacionAvanceProduccion.shift == _dimension(shift))
    aggregate_rows = query.order_by(
        EstacionAvanceProduccion.op,
        EstacionAvanceProduccion.ot,
        EstacionAvanceProduccion.machine_code,
        EstacionAvanceProduccion.shift,
    ).all()

    detailed_station_dates = set(
        db.session.query(
            EstacionPesajeLegacy.station_id,
            EstacionPesajeLegacy.operational_date,
        )
        .filter(EstacionPesajeLegacy.operational_date.between(period_start, period_end))
        .distinct()
        .all()
    )
    capture_query = EstacionPesajeLegacy.query.options(
        load_only(
            EstacionPesajeLegacy.station_id,
            EstacionPesajeLegacy.op_normalized,
            EstacionPesajeLegacy.ot_normalized,
            EstacionPesajeLegacy.mold_normalized,
            EstacionPesajeLegacy.color_normalized,
            EstacionPesajeLegacy.machine_normalized,
            EstacionPesajeLegacy.shift_normalized,
            EstacionPesajeLegacy.weight_kg,
            EstacionPesajeLegacy.captured_at_utc,
            EstacionPesajeLegacy.operational_date,
            EstacionPesajeLegacy.imported_at_utc,
        )
    ).filter(
        EstacionPesajeLegacy.operational_date.between(period_start, period_end),
        EstacionPesajeLegacy.is_deleted.is_(False),
    )
    if op:
        capture_query = capture_query.filter(
            EstacionPesajeLegacy.op_normalized == _dimension(op)
        )
    if machine_code:
        capture_query = capture_query.filter(
            EstacionPesajeLegacy.machine_normalized == _dimension(machine_code)
        )
    if shift:
        capture_query = capture_query.filter(
            EstacionPesajeLegacy.shift_normalized == _dimension(shift)
        )
    captures = capture_query.all()
    aggregate_rows = [
        row
        for row in aggregate_rows
        if (row.station_id, row.operational_date) not in detailed_station_dates
    ]

    observations = [
        {
            "station_id": row.station_id,
            "operational_dates": {row.operational_date},
            "op": row.op,
            "ot": row.ot,
            "mold": row.mold,
            "color": row.color,
            "machine_code": row.machine_code,
            "shift": row.shift,
            "bags": row.bags,
            "weight_kg": Decimal(row.weight_kg),
            "first_capture_at_utc": _utc(row.first_capture_at_utc),
            "last_capture_at_utc": _utc(row.last_capture_at_utc),
            "received_at_utc": _utc(row.report_received_at_utc),
        }
        for row in aggregate_rows
    ]
    detailed_groups = defaultdict(
        lambda: {
            "bags": 0,
            "weight_kg": Decimal("0"),
            "operational_dates": set(),
            "first_capture_at_utc": None,
            "last_capture_at_utc": None,
            "received_at_utc": None,
        }
    )
    for capture in captures:
        key = (
            capture.station_id,
            capture.op_normalized,
            capture.ot_normalized,
            capture.mold_normalized,
            capture.color_normalized,
            capture.machine_normalized,
            capture.shift_normalized,
        )
        bucket = detailed_groups[key]
        bucket["bags"] += 1
        bucket["weight_kg"] += Decimal(capture.weight_kg)
        bucket["operational_dates"].add(capture.operational_date)
        bucket["first_capture_at_utc"] = min(
            filter(None, (bucket["first_capture_at_utc"], _utc(capture.captured_at_utc))),
            default=None,
        )
        bucket["last_capture_at_utc"] = max(
            filter(None, (bucket["last_capture_at_utc"], _utc(capture.captured_at_utc))),
            default=None,
        )
        bucket["received_at_utc"] = max(
            filter(None, (bucket["received_at_utc"], _utc(capture.imported_at_utc))),
            default=None,
        )
    for key, bucket in detailed_groups.items():
        station_id, item_op, ot, mold, color, machine, item_shift = key
        observations.append(
            {
                "station_id": station_id,
                "op": item_op,
                "ot": ot,
                "mold": mold,
                "color": color,
                "machine_code": machine,
                "shift": item_shift,
                **bucket,
            }
        )

    op_keys = sorted({row["op"] for row in observations if row["op"]})
    orders = {}
    if op_keys:
        for order in OrdenProduccion.query.filter(
            func.upper(OrdenProduccion.numero_op).in_(op_keys)
        ).all():
            orders[_dimension(order.numero_op)] = order

    grouped = defaultdict(
        lambda: {
            "bags": 0,
            "weight_kg": Decimal("0"),
            "details": defaultdict(
                lambda: {
                    "bags": 0,
                    "weight_kg": Decimal("0"),
                    "station_ids": set(),
                    "ots": set(),
                    "molds": set(),
                    "machine_codes": set(),
                    "shifts": set(),
                    "operational_dates": set(),
                    "first_capture_at_utc": None,
                    "last_capture_at_utc": None,
                }
            ),
            "station_ids": set(),
            "last_capture_at_utc": None,
            "last_report_received_at_utc": None,
        }
    )
    reporting_stations = set()
    latest_report = None
    stations_by_id = {
        station.station_id: station
        for station in EstacionPesaje.query.filter(
            EstacionPesaje.station_id.in_(
                sorted({row["station_id"] for row in observations})
            )
        ).all()
    } if observations else {}
    for row in observations:
        key = row["op"] or "__UNASSIGNED__"
        group = grouped[key]
        group["bags"] += row["bags"]
        group["weight_kg"] += row["weight_kg"]
        group["station_ids"].add(row["station_id"])
        reporting_stations.add(row["station_id"])
        group["last_capture_at_utc"] = max(
            filter(None, (group["last_capture_at_utc"], row["last_capture_at_utc"])),
            default=None,
        )
        group["last_report_received_at_utc"] = max(
            filter(
                None,
                (
                    group["last_report_received_at_utc"],
                    row["received_at_utc"],
                ),
            ),
            default=None,
        )
        latest_report = max(
            filter(None, (latest_report, row["received_at_utc"])),
            default=None,
        )
        detail_key = row["color"] or "__NO_COLOR__"
        detail = group["details"][detail_key]
        detail["bags"] += row["bags"]
        detail["weight_kg"] += row["weight_kg"]
        detail["station_ids"].add(row["station_id"])
        if row["ot"]:
            detail["ots"].add(row["ot"])
        if row["mold"]:
            detail["molds"].add(row["mold"])
        if row["machine_code"]:
            detail["machine_codes"].add(row["machine_code"])
        if row["shift"]:
            detail["shifts"].add(row["shift"])
        detail["operational_dates"].update(row["operational_dates"])
        detail["first_capture_at_utc"] = min(
            filter(
                None,
                (detail["first_capture_at_utc"], row["first_capture_at_utc"]),
            ),
            default=None,
        )
        detail["last_capture_at_utc"] = max(
            filter(
                None,
                (detail["last_capture_at_utc"], row["last_capture_at_utc"]),
            ),
            default=None,
        )

    status_rank = {
        "RECIENTE": 0,
        "NUNCA_REPORTO": 1,
        "ATRASADA": 2,
        "SIN_COMUNICACION": 3,
    }
    items = []
    for key, group in grouped.items():
        item_op = None if key == "__UNASSIGNED__" else key
        order = orders.get(item_op)
        raw_target = Decimal(str(order.calculo_peso_produccion or 0)) if order else None
        if order is None:
            target_status = "OP_NOT_FOUND"
            target = None
        elif raw_target <= 0:
            target_status = "NO_TARGET"
            target = None
        else:
            target_status = "AVAILABLE"
            target = raw_target.quantize(Decimal("0.001"))
        progress_percent = (
            float(
                (group["weight_kg"] / target * 100).quantize(
                    Decimal("0.1"),
                    rounding=ROUND_HALF_UP,
                )
            )
            if target is not None
            else None
        )

        stations = []
        for station_id in sorted(group["station_ids"]):
            station = stations_by_id[station_id]
            stations.append(
                {
                    "station_id": station_id,
                    "code": station.codigo,
                    "name": station.nombre,
                    "communication_status": _station_status(station, now),
                }
            )
        communication_status = max(
            (entry["communication_status"] for entry in stations),
            key=lambda status: status_rank[status],
        )
        details = []
        detail_entries = sorted(
            group["details"].items(),
            key=lambda entry: entry[0],
        )
        for color_key, detail in detail_entries:
            color = None if color_key == "__NO_COLOR__" else color_key
            station_ids = sorted(detail["station_ids"])
            station_codes = sorted(
                stations_by_id[station_id].codigo for station_id in station_ids
            )
            ots = sorted(detail["ots"])
            molds = sorted(detail["molds"])
            machine_codes = sorted(detail["machine_codes"])
            shifts = sorted(detail["shifts"])
            details.append(
                {
                    "station_id": station_ids[0] if len(station_ids) == 1 else None,
                    "station_code": (
                        station_codes[0] if len(station_codes) == 1 else None
                    ),
                    "station_ids": station_ids,
                    "station_codes": station_codes,
                    "color": color,
                    "ot": ots[0] if len(ots) == 1 else None,
                    "ots": ots,
                    "mold": molds[0] if len(molds) == 1 else None,
                    "molds": molds,
                    "machine_code": (
                        machine_codes[0] if len(machine_codes) == 1 else None
                    ),
                    "machine_codes": machine_codes,
                    "shift": shifts[0] if len(shifts) == 1 else None,
                    "shifts": shifts,
                    "operational_dates": sorted(
                        value.isoformat() for value in detail["operational_dates"]
                    ),
                    "bags": detail["bags"],
                    "weight_kg": _format_kg(detail["weight_kg"]),
                    "first_capture_at_utc": _iso(
                        detail["first_capture_at_utc"]
                    ),
                    "last_capture_at_utc": _iso(detail["last_capture_at_utc"]),
                }
            )
        items.append(
            {
                "op": item_op,
                "product": order.producto if order else None,
                "bags": group["bags"],
                "weight_kg": _format_kg(group["weight_kg"]),
                "target_kg": _format_kg(target) if target is not None else None,
                "target_status": target_status,
                "progress_percent": progress_percent,
                "communication_status": communication_status,
                "last_capture_at_utc": _iso(group["last_capture_at_utc"]),
                "last_report_received_at_utc": _iso(
                    group["last_report_received_at_utc"]
                ),
                "stations": stations,
                "colors": sorted(
                    {detail["color"] for detail in details if detail["color"]}
                ),
                "details": details,
            }
        )

    items.sort(key=lambda item: (item["op"] is None, item["op"] or ""))
    total_weight = sum(
        (Decimal(item["weight_kg"]) for item in items),
        Decimal("0"),
    )
    return {
        "source": SOURCE,
        "operational_date": (
            operational_date.isoformat() if period == "DAY" else None
        ),
        "period": {
            "type": period,
            "start_date": period_start.isoformat(),
            "end_date": period_end.isoformat(),
        },
        "generated_at_utc": _iso(latest_report),
        "monthly_summary": _monthly_progress_summary(
            operational_date,
            op=op,
            machine_code=machine_code,
            shift=shift,
        ),
        "summary": {
            "bags": sum(item["bags"] for item in items),
            "weight_kg": _format_kg(total_weight),
            "production_orders": len([item for item in items if item["op"]]),
            "stations_reporting": len(reporting_stations),
        },
        "items": items,
    }
