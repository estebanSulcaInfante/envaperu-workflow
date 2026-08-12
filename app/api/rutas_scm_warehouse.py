"""API de recepción de mangas producidas en Almacén."""

from uuid import UUID

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.services.scm_auth import request_actor_id
from app.services.scm_service_support import ScmServiceError
from app.services.scm_warehouse_service import (
    close_receiving_session,
    create_receiving_session,
    decide_manga_quality,
    list_warehouse_receiving,
    receive_manga,
    reject_manga_receiving,
    resolve_receiving_code,
    resolve_receiving_label,
    request_receipt_reversal,
    resolve_receipt_reversal,
)
from app.services.scm_warehouse_scope_service import (
    assign_worker,
    create_location,
    create_warehouse,
    get_warehouse,
    list_warehouses,
    my_warehouse_scope,
)
from app.services.scm_inventory_transfer_service import (
    confirm_operation_session,
    create_operation_session,
    get_operation_session,
    inventory_summary,
    list_transfers,
    remove_operation_item,
    receive_transfer,
    scan_operation_item,
    start_transfer_return,
    trace_logistic_unit,
)


scm_warehouse_bp = Blueprint("scm_warehouse", __name__)


@scm_warehouse_bp.errorhandler(ScmServiceError)
def handle_service_error(error):
    db.session.rollback()
    return jsonify({"error": error.to_dict()}), error.status_code


def _body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ScmServiceError(
            "JSON_OBJECT_REQUIRED", "Se requiere un objeto JSON.", status_code=400
        )
    return payload


def _actor_id():
    try:
        return request_actor_id()
    except ValueError as error:
        raise ScmServiceError(
            "ACTOR_HEADER_REQUIRED",
            "X-Actor-Id debe identificar un trabajador válido.",
            status_code=400,
        ) from error


def _operation_id():
    try:
        return UUID(str(request.headers.get("Idempotency-Key")))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key debe contener un UUID válido.",
            status_code=400,
        ) from error


@scm_warehouse_bp.get("/almacenes")
def warehouses_list():
    return jsonify(list_warehouses(db.session, actor_id=_actor_id()))


@scm_warehouse_bp.post("/almacenes")
def warehouse_create():
    return jsonify(create_warehouse(
        db.session, actor_id=_actor_id(), operation_id=_operation_id(), data=_body()
    )), 201


@scm_warehouse_bp.get("/almacenes/<uuid:warehouse_id>")
def warehouse_detail(warehouse_id):
    return jsonify(get_warehouse(db.session, actor_id=_actor_id(), warehouse_id=warehouse_id))


@scm_warehouse_bp.post("/almacenes/<uuid:warehouse_id>/ubicaciones")
def warehouse_location_create(warehouse_id):
    return jsonify(create_location(
        db.session, actor_id=_actor_id(), warehouse_id=warehouse_id,
        operation_id=_operation_id(), data=_body(),
    )), 201


@scm_warehouse_bp.post("/almacenes/<uuid:warehouse_id>/trabajadores")
def warehouse_worker_assign(warehouse_id):
    return jsonify(assign_worker(
        db.session, actor_id=_actor_id(), warehouse_id=warehouse_id,
        operation_id=_operation_id(), data=_body(),
    )), 201


@scm_warehouse_bp.get("/mi-alcance-almacen")
def my_warehouse_reach():
    return jsonify(my_warehouse_scope(db.session, actor_id=_actor_id()))


@scm_warehouse_bp.post("/operaciones-almacen/sesiones")
def operation_session_create():
    return jsonify(create_operation_session(
        db.session, actor_id=_actor_id(), operation_id=_operation_id(), data=_body()
    )), 201


@scm_warehouse_bp.get("/operaciones-almacen/sesiones/<uuid:session_id>")
def operation_session_detail(session_id):
    return jsonify(get_operation_session(db.session, actor_id=_actor_id(), session_id=session_id))


@scm_warehouse_bp.post("/operaciones-almacen/sesiones/<uuid:session_id>/escanear")
def operation_session_scan(session_id):
    return jsonify(scan_operation_item(
        db.session, actor_id=_actor_id(), session_id=session_id,
        operation_id=_operation_id(), data=_body(),
    ))


@scm_warehouse_bp.post("/operaciones-almacen/sesiones/<uuid:session_id>/confirmar")
def operation_session_confirm(session_id):
    return jsonify(confirm_operation_session(
        db.session, actor_id=_actor_id(), session_id=session_id,
        operation_id=_operation_id(), data=_body(),
    )), 201


@scm_warehouse_bp.delete("/operaciones-almacen/sesiones/<uuid:session_id>/items/<uuid:item_id>")
def operation_session_item_remove(session_id, item_id):
    return jsonify(remove_operation_item(
        db.session, actor_id=_actor_id(), session_id=session_id, item_id=item_id,
        operation_id=_operation_id(), data=_body(),
    ))


@scm_warehouse_bp.get("/transferencias")
def transfers_list():
    return jsonify(list_transfers(
        db.session, actor_id=_actor_id(), limit=request.args.get("limit", 100)
    ))


@scm_warehouse_bp.post("/transferencias/<uuid:transfer_id>/recibir")
def transfer_receive(transfer_id):
    return jsonify(receive_transfer(
        db.session, actor_id=_actor_id(), transfer_id=transfer_id,
        operation_id=_operation_id(), data=_body(),
    ))


@scm_warehouse_bp.post("/transferencias/<uuid:transfer_id>/retorno")
def transfer_return(transfer_id):
    return jsonify(start_transfer_return(
        db.session, actor_id=_actor_id(), transfer_id=transfer_id,
        operation_id=_operation_id(), data=_body(),
    )), 201


@scm_warehouse_bp.get("/inventario/resumen")
def inventory_summary_route():
    payload = inventory_summary(db.session, actor_id=_actor_id())
    payload["as_of"] = db.session.scalar(db.func.now()).isoformat()
    return jsonify(payload)


@scm_warehouse_bp.get("/unidades-logisticas/<string:code>/trazabilidad")
def logistic_unit_trace(code):
    return jsonify(trace_logistic_unit(db.session, actor_id=_actor_id(), code=code))


@scm_warehouse_bp.get("/recepcion-mangas")
def receiving_list():
    return jsonify(list_warehouse_receiving(db.session, actor_id=_actor_id()))


@scm_warehouse_bp.get("/recepcion-mangas/resolver-etiqueta/<uuid:label_id>")
def receiving_resolve_label(label_id):
    return jsonify(resolve_receiving_label(
        db.session, actor_id=_actor_id(), label_id=label_id
    ))


@scm_warehouse_bp.get("/recepcion-mangas/resolver-codigo/<string:code>")
def receiving_resolve_code(code):
    return jsonify(resolve_receiving_code(
        db.session, actor_id=_actor_id(), code=code
    ))


@scm_warehouse_bp.post("/recepcion-mangas/sesiones")
def receiving_session_create():
    return jsonify(create_receiving_session(
        db.session,
        actor_id=_actor_id(),
        operation_id=_operation_id(),
        data=_body(),
    )), 201


@scm_warehouse_bp.post("/recepcion-mangas/sesiones/<uuid:session_id>/cerrar")
def receiving_session_close(session_id):
    return jsonify(close_receiving_session(
        db.session,
        actor_id=_actor_id(),
        session_id=session_id,
        operation_id=_operation_id(),
    ))


@scm_warehouse_bp.post("/recepcion-mangas/confirmar")
def receiving_confirm():
    return jsonify(receive_manga(
        db.session,
        actor_id=_actor_id(),
        operation_id=_operation_id(),
        data=_body(),
    )), 201


@scm_warehouse_bp.post("/recepcion-mangas/rechazar")
def receiving_reject():
    return jsonify(reject_manga_receiving(
        db.session,
        actor_id=_actor_id(),
        operation_id=_operation_id(),
        data=_body(),
    )), 201


@scm_warehouse_bp.post("/recepcion-mangas/<uuid:existence_id>/calidad")
def receiving_quality(existence_id):
    return jsonify(decide_manga_quality(
        db.session,
        actor_id=_actor_id(),
        existence_id=existence_id,
        operation_id=_operation_id(),
        data=_body(),
    ))


@scm_warehouse_bp.post("/recepcion-mangas/<uuid:existence_id>/reversiones")
def receiving_reversal_request(existence_id):
    return jsonify(request_receipt_reversal(
        db.session, actor_id=_actor_id(), existence_id=existence_id,
        operation_id=_operation_id(), data=_body(),
    )), 201


@scm_warehouse_bp.post("/recepcion-mangas/reversiones/<uuid:reversal_id>/resolver")
def receiving_reversal_resolve(reversal_id):
    return jsonify(resolve_receipt_reversal(
        db.session, actor_id=_actor_id(), reversal_id=reversal_id,
        operation_id=_operation_id(), data=_body(),
    ))
