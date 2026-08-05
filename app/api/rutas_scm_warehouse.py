"""API de recepción de mangas producidas en Almacén."""

from uuid import UUID

from flask import Blueprint, jsonify, request

from app.extensions import db
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
        value = int(request.headers.get("X-Actor-Id"))
    except (TypeError, ValueError):
        value = None
    if value is None or value <= 0:
        raise ScmServiceError(
            "ACTOR_HEADER_REQUIRED",
            "X-Actor-Id debe identificar un trabajador válido.",
            status_code=400,
        )
    return value


def _operation_id():
    try:
        return UUID(str(request.headers.get("Idempotency-Key")))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key debe contener un UUID válido.",
            status_code=400,
        ) from error


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
