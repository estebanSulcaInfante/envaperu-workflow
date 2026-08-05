"""API de reproceso y alertas, separada del blueprint SCM legado por tamaño."""

from uuid import UUID

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.services.scm_auth import request_actor_id
from app.services.scm_alert_service import (
    approve_alert_rule_revision,
    create_alert_rule_revision,
    list_alert_rules,
    list_alerts,
    transition_alert,
)
from app.services.scm_reprocessing_service import (
    add_grinding_input,
    approve_compatibility_rule,
    approve_grinding_exception,
    authorize_custody_difference,
    close_grinding_order,
    create_compatibility_rule,
    create_grinding_order,
    create_reprocessing_master,
    list_compatibility_rules,
    list_grinding_orders,
    list_recovered_lots,
    list_reprocessing_masters,
    list_reprocessing_references,
    list_scrap_lots,
    list_scrap_movements,
    record_pre_mill_weights,
    register_scrap_lot,
    release_recovered_lot,
    start_grinding_order,
    update_reprocessing_master,
    validate_grinding_order,
)
from app.services.scm_service_support import ScmServiceError


scm_reprocessing_bp = Blueprint("scm_reprocessing", __name__)


@scm_reprocessing_bp.errorhandler(ScmServiceError)
def handle_service_error(error):
    return jsonify({"error": error.to_dict()}), error.status_code


def _body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ScmServiceError("JSON_OBJECT_REQUIRED", "Se requiere un objeto JSON.", status_code=400)
    return payload


def _actor_id():
    try:
        return request_actor_id()
    except ValueError as error:
        raise ScmServiceError("ACTOR_HEADER_REQUIRED", "X-Actor-Id debe identificar un trabajador valido.", status_code=400) from error


def _operation_id():
    try:
        return UUID(str(request.headers.get("Idempotency-Key")))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key debe contener un UUID valido.", status_code=400) from error


@scm_reprocessing_bp.get("/reproceso/referencias")
def references():
    return jsonify(list_reprocessing_references(db.session, actor_id=_actor_id()))


@scm_reprocessing_bp.get("/reproceso/maestros/<string:master_type>")
def masters_list(master_type):
    return jsonify(list_reprocessing_masters(db.session, actor_id=_actor_id(), master_type=master_type))


@scm_reprocessing_bp.post("/reproceso/maestros/<string:master_type>")
def masters_create(master_type):
    return jsonify(create_reprocessing_master(db.session, actor_id=_actor_id(), master_type=master_type, data=_body())), 201


@scm_reprocessing_bp.patch("/reproceso/maestros/<string:master_type>/<int:item_id>")
def masters_update(master_type, item_id):
    return jsonify(update_reprocessing_master(db.session, actor_id=_actor_id(), master_type=master_type, item_id=item_id, data=_body()))


@scm_reprocessing_bp.get("/reproceso/reglas-compatibilidad")
def compatibility_rules_list():
    return jsonify(list_compatibility_rules(db.session, actor_id=_actor_id()))


@scm_reprocessing_bp.post("/reproceso/reglas-compatibilidad")
def compatibility_rules_create():
    return jsonify(create_compatibility_rule(db.session, actor_id=_actor_id(), data=_body())), 201


@scm_reprocessing_bp.post("/reproceso/reglas-compatibilidad/<int:rule_id>/aprobar")
def compatibility_rules_approve(rule_id):
    return jsonify(approve_compatibility_rule(db.session, actor_id=_actor_id(), rule_id=rule_id))


@scm_reprocessing_bp.get("/reproceso/mermas")
def scrap_list():
    return jsonify(list_scrap_lots(db.session, actor_id=_actor_id(), state=request.args.get("estado")))


@scm_reprocessing_bp.post("/reproceso/mermas")
def scrap_create():
    return jsonify(register_scrap_lot(db.session, actor_id=_actor_id(), operation_id=_operation_id(), data=_body())), 201


@scm_reprocessing_bp.get("/reproceso/mermas/<uuid:lot_id>/movimientos")
def scrap_movements(lot_id):
    return jsonify(list_scrap_movements(db.session, actor_id=_actor_id(), lot_id=lot_id))


@scm_reprocessing_bp.get("/reproceso/ordenes-molienda")
def grinding_orders_list():
    return jsonify(list_grinding_orders(db.session, actor_id=_actor_id()))


@scm_reprocessing_bp.post("/reproceso/ordenes-molienda")
def grinding_orders_create():
    return jsonify(create_grinding_order(db.session, actor_id=_actor_id(), data=_body())), 201


@scm_reprocessing_bp.post("/reproceso/ordenes-molienda/<uuid:order_id>/aportes")
def grinding_orders_add_input(order_id):
    return jsonify(add_grinding_input(db.session, actor_id=_actor_id(), order_id=order_id, data=_body())), 201


@scm_reprocessing_bp.post("/reproceso/ordenes-molienda/<uuid:order_id>/validar")
def grinding_orders_validate(order_id):
    return jsonify(validate_grinding_order(db.session, actor_id=_actor_id(), order_id=order_id))


@scm_reprocessing_bp.post("/reproceso/ordenes-molienda/<uuid:order_id>/aprobar-excepcion")
def grinding_orders_approve_exception(order_id):
    return jsonify(approve_grinding_exception(db.session, actor_id=_actor_id(), order_id=order_id, data=_body()))


@scm_reprocessing_bp.post("/reproceso/ordenes-molienda/<uuid:order_id>/pesos-pre-molino")
def grinding_orders_preweights(order_id):
    return jsonify(record_pre_mill_weights(db.session, actor_id=_actor_id(), order_id=order_id, data=_body()))


@scm_reprocessing_bp.post("/reproceso/aportes/<int:contribution_id>/autorizar-diferencia")
def grinding_input_authorize_difference(contribution_id):
    return jsonify(authorize_custody_difference(db.session, actor_id=_actor_id(), contribution_id=contribution_id, data=_body()))


@scm_reprocessing_bp.post("/reproceso/ordenes-molienda/<uuid:order_id>/iniciar")
def grinding_orders_start(order_id):
    return jsonify(start_grinding_order(db.session, actor_id=_actor_id(), order_id=order_id))


@scm_reprocessing_bp.post("/reproceso/ordenes-molienda/<uuid:order_id>/cerrar")
def grinding_orders_close(order_id):
    return jsonify(close_grinding_order(db.session, actor_id=_actor_id(), order_id=order_id, operation_id=_operation_id(), data=_body()))


@scm_reprocessing_bp.get("/reproceso/lotes-recuperados")
def recovered_lots_list():
    return jsonify(list_recovered_lots(db.session, actor_id=_actor_id(), state=request.args.get("estado")))


@scm_reprocessing_bp.post("/reproceso/lotes-recuperados/<uuid:lot_id>/liberar")
def recovered_lots_release(lot_id):
    return jsonify(release_recovered_lot(db.session, actor_id=_actor_id(), lot_id=lot_id, data=_body()))


@scm_reprocessing_bp.get("/alertas")
def alerts_list():
    return jsonify(list_alerts(
        db.session, actor_id=_actor_id(), state=request.args.get("estado"),
        severity=request.args.get("severidad"), alert_type=request.args.get("tipo"),
    ))


@scm_reprocessing_bp.get("/alertas/reglas")
def alert_rules_list():
    return jsonify(list_alert_rules(db.session, actor_id=_actor_id()))


@scm_reprocessing_bp.post("/alertas/reglas/<string:rule_code>/revisiones")
def alert_rules_create_revision(rule_code):
    return jsonify(create_alert_rule_revision(db.session, actor_id=_actor_id(), rule_code=rule_code, data=_body())), 201


@scm_reprocessing_bp.post("/alertas/reglas/<string:rule_code>/revisiones/<int:revision_id>/aprobar")
def alert_rules_approve_revision(rule_code, revision_id):
    del rule_code
    return jsonify(approve_alert_rule_revision(db.session, actor_id=_actor_id(), revision_id=revision_id))


@scm_reprocessing_bp.post("/alertas/<uuid:alert_id>/<string:action>")
def alerts_transition(alert_id, action):
    return jsonify(transition_alert(db.session, actor_id=_actor_id(), alert_id=alert_id, action=action, data=_body()))
