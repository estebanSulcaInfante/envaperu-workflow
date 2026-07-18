from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, request

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
