from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, request

from app.extensions import db
from app.models.estacion_pesaje import EstacionPesaje
from app.services.station_auth import require_station_auth
from app.services.station_monitoring import (
    HeartbeatIdempotencyConflict,
    HeartbeatValidationError,
    process_heartbeat,
    station_monitor_dict,
)
from app.services.station_production_progress import (
    ProgressIdempotencyConflict,
    ProgressValidationError,
    process_production_progress,
    production_progress_dashboard,
)
from app.services.legacy_history import (
    LegacyHistoryConflict,
    LegacyHistoryValidationError,
    legacy_production_order_detail,
    legacy_production_orders,
    process_legacy_history_chunk,
)


integration_station_bp = Blueprint("integration_station", __name__)
monitoring_station_bp = Blueprint("monitoring_station", __name__)


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


@integration_station_bp.get("/capabilities")
@require_station_auth
def capabilities():
    return jsonify(
        {
            "api_version": "integration-v1",
            "server_time_utc": _utc_now_iso(),
            "minimum_station_version": current_app.config.get(
                "MINIMUM_STATION_VERSION",
                "1.1.0",
            ),
            "supported_contracts": {
                "heartbeat": ["station-heartbeat-v1"],
                "catalog": ["station-catalog-v1"],
                "weight_event": [
                    "sync-pesajes-legacy-v1",
                    "station-production-progress-v1",
                    "station-legacy-history-v1",
                ],
            },
            "features": {
                "monitoring": True,
                "catalog_snapshot": bool(
                    current_app.config.get("STATION_CATALOG_ENABLED", False)
                ),
                "legacy_weight_ingest_enabled": False,
                "remote_hardware_commands": False,
            },
        }
    )


@integration_station_bp.put("/stations/<station_id>/heartbeat")
@require_station_auth
def heartbeat(station_id):
    station = g.authenticated_station
    if station.station_id != station_id:
        return (
            jsonify(
                {
                    "code": "STATION_ID_MISMATCH",
                    "message": "El token no pertenece al station_id solicitado",
                }
            ),
            403,
        )
    if not request.is_json:
        return (
            jsonify(
                {
                    "code": "JSON_REQUIRED",
                    "message": "Content-Type application/json es requerido",
                }
            ),
            415,
        )

    try:
        ack = process_heartbeat(
            station,
            request.get_json(silent=True),
            request.headers.get("Idempotency-Key"),
            next_heartbeat_seconds=current_app.config.get(
                "HEARTBEAT_SECONDS",
                30,
            ),
        )
    except HeartbeatValidationError as exc:
        return jsonify({"code": exc.code, "message": exc.message}), 422
    except HeartbeatIdempotencyConflict:
        return (
            jsonify(
                {
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": "heartbeat_id ya fue usado con otro payload",
                }
            ),
            409,
        )
    return jsonify(ack)


@integration_station_bp.put("/stations/<station_id>/production-progress")
@require_station_auth
def production_progress(station_id):
    station = g.authenticated_station
    if station.station_id != station_id:
        return (
            jsonify(
                {
                    "code": "STATION_ID_MISMATCH",
                    "message": "El token no pertenece al station_id solicitado",
                }
            ),
            403,
        )
    if not request.is_json:
        return (
            jsonify(
                {
                    "code": "JSON_REQUIRED",
                    "message": "Content-Type application/json es requerido",
                }
            ),
            415,
        )
    try:
        ack = process_production_progress(
            station,
            request.get_json(silent=True),
            request.headers.get("Idempotency-Key"),
        )
    except ProgressValidationError as exc:
        return jsonify({"code": exc.code, "message": exc.message}), 422
    except ProgressIdempotencyConflict:
        return (
            jsonify(
                {
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": "report_id ya fue usado con otro payload",
                }
            ),
            409,
        )
    return jsonify(ack)


@integration_station_bp.put(
    "/stations/<station_id>/legacy-history/imports/<import_id>/chunks/<int:chunk_index>"
)
@require_station_auth
def legacy_history_chunk(station_id, import_id, chunk_index):
    station = g.authenticated_station
    if station.station_id != station_id:
        return (
            jsonify(
                {
                    "code": "STATION_ID_MISMATCH",
                    "message": "El token no pertenece al station_id solicitado",
                }
            ),
            403,
        )
    if not request.is_json:
        return (
            jsonify(
                {
                    "code": "JSON_REQUIRED",
                    "message": "Content-Type application/json es requerido",
                }
            ),
            415,
        )
    try:
        ack = process_legacy_history_chunk(
            station,
            request.get_json(silent=True),
            import_id,
            chunk_index,
            request.headers.get("Idempotency-Key"),
        )
    except LegacyHistoryValidationError as exc:
        db.session.rollback()
        return jsonify({"code": exc.code, "message": exc.message}), 422
    except LegacyHistoryConflict as exc:
        db.session.rollback()
        return jsonify({"code": exc.code, "message": exc.message}), 409
    return jsonify(ack)


def _monitor_item(station):
    return station_monitor_dict(
        station,
        delayed_seconds=current_app.config.get(
            "HEARTBEAT_DELAYED_SECONDS",
            90,
        ),
        disconnected_seconds=current_app.config.get(
            "HEARTBEAT_DISCONNECTED_SECONDS",
            300,
        ),
    )


@monitoring_station_bp.get("/weighing-stations")
def list_weighing_stations():
    stations = EstacionPesaje.query.order_by(EstacionPesaje.codigo).all()
    return jsonify({"items": [_monitor_item(station) for station in stations]})


@monitoring_station_bp.get("/weighing-stations/<station_id>")
def get_weighing_station(station_id):
    station = EstacionPesaje.query.filter_by(station_id=station_id).one_or_none()
    if station is None:
        return (
            jsonify(
                {
                    "code": "STATION_NOT_FOUND",
                    "message": "Estacion de pesaje no encontrada",
                }
            ),
            404,
        )
    return jsonify(_monitor_item(station))


@monitoring_station_bp.get("/production-progress")
def get_production_progress():
    raw_date = request.args.get("date")
    try:
        operational_date = (
            date.fromisoformat(raw_date)
            if raw_date
            else datetime.now(ZoneInfo("America/Lima")).date()
        )
    except ValueError:
        return (
            jsonify(
                {
                    "code": "INVALID_DATE",
                    "message": "date debe usar el formato AAAA-MM-DD",
                }
            ),
            422,
        )
    return jsonify(
        production_progress_dashboard(
            operational_date,
            op=request.args.get("op"),
            machine_code=request.args.get("machine_code"),
            shift=request.args.get("shift"),
        )
    )


def _pagination_args(default_per_page):
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", default_per_page))
    except (TypeError, ValueError) as exc:
        raise ValueError("page y per_page deben ser enteros") from exc
    if page < 1 or not 1 <= per_page <= 200:
        raise ValueError("page debe ser >= 1 y per_page debe estar entre 1 y 200")
    return page, per_page


@monitoring_station_bp.get("/legacy-production-orders")
def get_legacy_production_orders():
    try:
        page, per_page = _pagination_args(50)
    except ValueError as exc:
        return jsonify({"code": "INVALID_PAGINATION", "message": str(exc)}), 422
    return jsonify(
        legacy_production_orders(
            page=page,
            per_page=per_page,
            query=request.args.get("q"),
            status=request.args.get("status"),
        )
    )


@monitoring_station_bp.get("/legacy-production-orders/detail")
def get_legacy_production_order_detail():
    station_id = request.args.get("station_id", "").strip()
    op_raw = request.args.get("op", "").strip()
    if not station_id or not op_raw:
        return (
            jsonify(
                {
                    "code": "DETAIL_FILTER_REQUIRED",
                    "message": "station_id y op son requeridos",
                }
            ),
            422,
        )
    try:
        page, per_page = _pagination_args(100)
    except ValueError as exc:
        return jsonify({"code": "INVALID_PAGINATION", "message": str(exc)}), 422
    detail = legacy_production_order_detail(
        station_id,
        op_raw,
        page=page,
        per_page=per_page,
    )
    if detail is None:
        return (
            jsonify(
                {
                    "code": "LEGACY_ORDER_NOT_FOUND",
                    "message": "OP legacy no encontrada",
                }
            ),
            404,
        )
    return jsonify(detail)
