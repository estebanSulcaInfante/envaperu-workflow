from datetime import date, datetime, timezone
from uuid import UUID
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
from app.services.legacy_continuity import (
    LegacyContinuityError,
    acknowledge_command,
    create_pilot_command,
    history_sync_state,
    pending_commands,
    process_history_delta,
)
from app.services.scm_ot_service import (
    acknowledge_station_print_job,
    claim_station_print_job,
    get_station_print_job,
    list_station_print_jobs,
)
from app.services.scm_service_support import ScmServiceError
from app.services.scm_inventory_opening_service import (
    capture_physical_opening_unit,
    resolve_physical_opening_target,
)
from app.services.scm_prepared_material_service import (
    record_preparation_reading,
    resolve_preparation_source_unit,
)
from app.services.scm_weighing_service import (
    confirm_manga_weighing,
    get_label_print_payload,
    get_operation_result,
    resolve_manga_label,
    register_manga_weighing_control,
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
                    "station-legacy-continuity-v1",
                ],
                "manga_prelabel": ["scm-manga-prelabel-v1"],
                "manga_weighing": ["scm-manga-weighing-v1"],
                "manga_weighing_control": [
                    "scm-manga-weighing-control-v1",
                    "scm-manga-weight-progress-v2",
                ],
                "inventory_opening_weighing": ["scm-inventory-opening-weighing-v1"],
                "prepared_material_weighing": ["scm-prepared-material-weighing-v1"],
            },
            "features": {
                "monitoring": True,
                "catalog_snapshot": bool(
                    current_app.config.get("STATION_CATALOG_ENABLED", False)
                ),
                "legacy_weight_ingest_enabled": False,
                "remote_hardware_commands": False,
                "pilot_data_commands": True,
                "scm_manga_prelabel": True,
                "scm_manga_weighing": True,
                "scm_manga_weighing_control": True,
                "scm_inventory_opening_weighing": True,
                "scm_prepared_material_weighing": True,
            },
        }
    )


@integration_station_bp.post("/stations/<station_id>/inventory-opening-units")
@require_station_auth
def capture_inventory_opening_unit(station_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"code": "JSON_REQUIRED", "message": "JSON requerido"}), 415
    try:
        operation_id = UUID(request.headers.get("Idempotency-Key", ""))
    except (TypeError, ValueError):
        return jsonify({
            "code": "IDEMPOTENCY_KEY_REQUIRED",
            "message": "Idempotency-Key debe ser un UUID.",
        }), 422
    try:
        return jsonify(capture_physical_opening_unit(
            db.session, station_id=station_id, operation_id=operation_id, data=payload,
        ))
    except ScmServiceError as exc:
        return _integration_error(exc)


@integration_station_bp.post("/stations/<station_id>/inventory-opening-targets/resolve")
@require_station_auth
def resolve_inventory_opening_target(station_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"code": "JSON_REQUIRED", "message": "JSON requerido"}), 415
    try:
        return jsonify(resolve_physical_opening_target(db.session, data=payload))
    except ScmServiceError as exc:
        return _integration_error(exc)


@integration_station_bp.post("/stations/<station_id>/prepared-material-readings")
@require_station_auth
def capture_prepared_material_reading(station_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"code": "JSON_REQUIRED", "message": "JSON requerido"}), 415
    try:
        operation_id = UUID(request.headers.get("Idempotency-Key", ""))
        order_id = UUID(str(payload.get("orden_preparacion_id")))
    except (TypeError, ValueError, AttributeError):
        return jsonify({"code": "INVALID_UUID", "message": "IDs de captura inválidos."}), 422
    if payload.get("reading_stable") is not True:
        return jsonify({"code": "SCALE_READING_UNSTABLE", "message": "La lectura debe ser estable."}), 422
    try:
        return jsonify(record_preparation_reading(
            db.session,
            actor_id=payload.get("pesado_por_id"),
            operation_id=operation_id,
            order_id=order_id,
            data={
                "version": payload.get("version"),
                "tipo_uso": payload.get("tipo_uso"),
                "metodo": "BALANZA_ESTACION",
                "bruto_kg": payload.get("peso_bruto_kg"),
                "tara_kg": payload.get("tara_kg"),
                "neto_kg": payload.get("peso_neto_kg"),
                "motivo": "Pesaje estable capturado en estación de Preparación",
                "evidencia_ref": f"station:{station_id}:{operation_id}",
                "asignacion_requerimiento_id": payload.get("asignacion_requerimiento_id"),
                "unidades_origen_qr": payload.get("unidades_origen_qr") or [],
            },
        ))
    except ScmServiceError as exc:
        return _integration_error(exc)


@integration_station_bp.post("/stations/<station_id>/prepared-material-source-units/resolve")
@require_station_auth
def resolve_prepared_material_source_unit(station_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"code": "JSON_REQUIRED", "message": "JSON requerido"}), 415
    try:
        order_id = UUID(str(payload.get("orden_preparacion_id")))
    except (TypeError, ValueError, AttributeError):
        return jsonify({"code": "INVALID_UUID", "message": "La OPM es inválida."}), 422
    try:
        return jsonify(resolve_preparation_source_unit(
            db.session,
            actor_id=payload.get("actor_id"),
            order_id=order_id,
            qr_value=payload.get("qr_value"),
        ))
    except ScmServiceError as exc:
        return _integration_error(exc)


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


def _continuity_error(exc):
    db.session.rollback()
    return jsonify({"code": exc.code, "message": exc.message}), exc.status


def _station_matches(station_id):
    if g.authenticated_station.station_id == station_id:
        return True, None
    return False, (
        jsonify(
            {
                "code": "STATION_ID_MISMATCH",
                "message": "El token no pertenece al station_id solicitado",
            }
        ),
        403,
    )


def _integration_error(exc):
    db.session.rollback()
    return jsonify({"error": exc.to_dict()}), exc.status_code


@integration_station_bp.get("/manga-labels/<uuid:label_id>/resolve")
@require_station_auth
def manga_label_resolve(label_id):
    try:
        return jsonify(resolve_manga_label(db.session, label_id=label_id))
    except ScmServiceError as exc:
        return _integration_error(exc)


@integration_station_bp.post("/manga-weighings")
@require_station_auth
def manga_weighing_confirm():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            "code": "JSON_REQUIRED",
            "message": "Se requiere un objeto JSON.",
        }), 415
    try:
        operation_id = request.headers.get("Idempotency-Key")
        operation_id = UUID(str(operation_id))
        actor_id = int(payload.get("pesado_por_id"))
    except (TypeError, ValueError, AttributeError):
        return jsonify({
            "code": "IDENTITY_REQUIRED",
            "message": (
                "Idempotency-Key UUID y pesado_por_id son obligatorios."
            ),
        }), 400
    try:
        result = confirm_manga_weighing(
            db.session,
            station_id=g.authenticated_station.station_id,
            operation_id=operation_id,
            actor_id=actor_id,
            data=payload,
        )
        return jsonify(result)
    except ScmServiceError as exc:
        return _integration_error(exc)


@integration_station_bp.post("/manga-weighing-controls")
@require_station_auth
def manga_weighing_control_register():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            "code": "JSON_REQUIRED",
            "message": "Se requiere un objeto JSON.",
        }), 415
    try:
        operation_id = UUID(str(request.headers.get("Idempotency-Key")))
        actor_id = int(payload.get("pesado_por_id"))
    except (TypeError, ValueError, AttributeError):
        return jsonify({
            "code": "IDENTITY_REQUIRED",
            "message": (
                "Idempotency-Key UUID y pesado_por_id son obligatorios."
            ),
        }), 400
    try:
        return jsonify(register_manga_weighing_control(
            db.session,
            station_id=g.authenticated_station.station_id,
            operation_id=operation_id,
            actor_id=actor_id,
            data=payload,
        ))
    except ScmServiceError as exc:
        return _integration_error(exc)


@integration_station_bp.get("/operations/<uuid:operation_id>")
@require_station_auth
def manga_operation_result(operation_id):
    try:
        return jsonify(get_operation_result(
            db.session, operation_id=operation_id
        ))
    except ScmServiceError as exc:
        return _integration_error(exc)


@integration_station_bp.get("/labels/<uuid:label_id>/print-payload")
@require_station_auth
def manga_label_print_payload(label_id):
    try:
        return jsonify(get_label_print_payload(
            db.session, label_id=label_id
        ))
    except ScmServiceError as exc:
        return _integration_error(exc)


@integration_station_bp.get(
    "/stations/<station_id>/print-jobs"
)
@require_station_auth
def station_print_jobs(station_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    try:
        return jsonify(list_station_print_jobs(
            db.session,
            station_id=station_id,
            status=request.args.get("status", "PENDING"),
            limit=request.args.get("limit", 20),
        ))
    except ScmServiceError as exc:
        db.session.rollback()
        return jsonify({"error": exc.to_dict()}), exc.status_code


@integration_station_bp.get(
    "/stations/<station_id>/print-jobs/<uuid:print_job_id>"
)
@require_station_auth
def station_print_job(station_id, print_job_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    try:
        return jsonify(get_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=print_job_id,
        ))
    except ScmServiceError as exc:
        db.session.rollback()
        return jsonify({"error": exc.to_dict()}), exc.status_code


@integration_station_bp.post(
    "/stations/<station_id>/print-jobs/<uuid:print_job_id>/claim"
)
@require_station_auth
def station_print_job_claim(station_id, print_job_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    try:
        return jsonify(claim_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=print_job_id,
        ))
    except ScmServiceError as exc:
        db.session.rollback()
        return jsonify({"error": exc.to_dict()}), exc.status_code


@integration_station_bp.put(
    "/stations/<station_id>/print-jobs/<uuid:print_job_id>/result"
)
@require_station_auth
def station_print_job_result(station_id, print_job_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    try:
        return jsonify(acknowledge_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=print_job_id,
            data=request.get_json(silent=True) or {},
        ))
    except ScmServiceError as exc:
        db.session.rollback()
        return jsonify({"error": exc.to_dict()}), exc.status_code


@integration_station_bp.get("/stations/<station_id>/legacy-history/sync-state")
@require_station_auth
def legacy_history_sync_state(station_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    try:
        return jsonify(history_sync_state(station_id))
    except LegacyContinuityError as exc:
        return _continuity_error(exc)


@integration_station_bp.put(
    "/stations/<station_id>/legacy-history/deltas/<batch_id>"
)
@require_station_auth
def legacy_history_delta(station_id, batch_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    try:
        return jsonify(
            process_history_delta(
                g.authenticated_station,
                request.get_json(silent=True),
                batch_id,
                request.headers.get("Idempotency-Key"),
            )
        )
    except LegacyContinuityError as exc:
        return _continuity_error(exc)


@integration_station_bp.get("/stations/<station_id>/pilot-commands")
@require_station_auth
def station_pilot_commands(station_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    limit = min(max(request.args.get("limit", 20, type=int), 1), 100)
    return jsonify({"items": pending_commands(station_id, limit=limit)})


@integration_station_bp.post(
    "/stations/<station_id>/pilot-commands/<command_id>/ack"
)
@require_station_auth
def station_pilot_command_ack(station_id, command_id):
    matches, error = _station_matches(station_id)
    if not matches:
        return error
    try:
        return jsonify(
            acknowledge_command(
                station_id,
                command_id,
                request.get_json(silent=True),
            )
        )
    except LegacyContinuityError as exc:
        return _continuity_error(exc)


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
    raw_period = (request.args.get("period") or "day").strip().lower()
    raw_date = request.args.get("date")
    raw_month = request.args.get("month")
    if raw_period not in {"day", "month"}:
        return (
            jsonify(
                {
                    "code": "INVALID_PERIOD",
                    "message": "period debe ser day o month",
                }
            ),
            422,
        )
    try:
        today = datetime.now(ZoneInfo("America/Lima")).date()
        if raw_period == "month":
            month = raw_month or today.strftime("%Y-%m")
            if len(month) != 7:
                raise ValueError
            operational_date = date.fromisoformat(f"{month}-01")
            if operational_date.strftime("%Y-%m") != month:
                raise ValueError
        else:
            operational_date = date.fromisoformat(raw_date) if raw_date else today
    except ValueError:
        field = "month" if raw_period == "month" else "date"
        expected = "AAAA-MM" if raw_period == "month" else "AAAA-MM-DD"
        return (
            jsonify(
                {
                    "code": f"INVALID_{field.upper()}",
                    "message": f"{field} debe usar el formato {expected}",
                }
            ),
            422,
        )
    return jsonify(
        production_progress_dashboard(
            operational_date,
            period=raw_period.upper(),
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


@monitoring_station_bp.post("/pilot-commands")
def post_pilot_command():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"code": "JSON_REQUIRED", "message": "JSON requerido"}), 415
    station_id = payload.get("station_id")
    if db.session.get(EstacionPesaje, station_id) is None:
        return (
            jsonify({"code": "STATION_NOT_FOUND", "message": "Estacion no encontrada"}),
            404,
        )
    try:
        return jsonify(
            create_pilot_command(station_id, payload.get("action"), payload)
        ), 202
    except LegacyContinuityError as exc:
        return _continuity_error(exc)
