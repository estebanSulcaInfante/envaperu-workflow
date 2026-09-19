"""HTTP contract for KG availability and the PT manual Kardex pilot."""

from uuid import UUID

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.services.scm_auth import request_actor_id
from app.services.scm_kg_pt_availability_service import (
    list_piece_kg_availability,
    list_pt_availability,
    list_pt_manual_balances,
    list_pt_manual_movements,
    list_pt_manual_movements_all,
    register_pt_manual_movement,
)
from app.services.scm_service_support import ScmServiceError


scm_kg_pt_bp = Blueprint("scm_kg_pt", __name__)


@scm_kg_pt_bp.errorhandler(ScmServiceError)
def handle_scm_kg_pt_error(error):
    return jsonify({"error": error.to_dict()}), error.status_code


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
        return UUID(request.headers.get("Idempotency-Key", ""))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key debe contener un UUID válido.",
            status_code=400,
        ) from error


def _body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ScmServiceError("JSON_OBJECT_REQUIRED", "Se requiere un objeto JSON.", status_code=400)
    return payload


@scm_kg_pt_bp.get("/disponibilidad-piezas-kg")
@scm_kg_pt_bp.get("/disponibilidad/piezas-kg")
@scm_kg_pt_bp.get("/disponibilidad/piezas")
def disponibilidad_piezas_kg():
    return jsonify(list_piece_kg_availability(
        db.session,
        actor_id=_actor_id(),
        query=request.args.get("q"),
        location=request.args.get("ubicacion"),
    ))


@scm_kg_pt_bp.get("/disponibilidad-pt")
@scm_kg_pt_bp.get("/disponibilidad/producto-terminado")
@scm_kg_pt_bp.get("/disponibilidad/productos-terminados")
def disponibilidad_pt():
    return jsonify(list_pt_availability(
        db.session,
        actor_id=_actor_id(),
        query=request.args.get("q"),
        location=request.args.get("ubicacion"),
    ))


@scm_kg_pt_bp.get("/kardex-pt-manual")
@scm_kg_pt_bp.get("/inventario/pt")
def kardex_pt_manual_saldos():
    return jsonify(list_pt_manual_balances(
        db.session,
        actor_id=_actor_id(),
        article_id=request.args.get("articulo_scm_id"),
        location=request.args.get("ubicacion"),
    ))


@scm_kg_pt_bp.get("/kardex-pt-manual/<uuid:balance_id>/movimientos")
@scm_kg_pt_bp.get("/inventario/pt/movimientos/<uuid:balance_id>")
def kardex_pt_manual_movimientos(balance_id):
    return jsonify(list_pt_manual_movements(
        db.session, actor_id=_actor_id(), balance_id=balance_id,
    ))


@scm_kg_pt_bp.post("/kardex-pt-manual/movimientos")
@scm_kg_pt_bp.post("/inventario/pt/movimientos")
def kardex_pt_manual_registrar():
    return jsonify(register_pt_manual_movement(
        db.session,
        actor_id=_actor_id(),
        operation_id=_operation_id(),
        data=_body(),
    )), 201


@scm_kg_pt_bp.get("/inventario/pt/movimientos")
def inventario_pt_movimientos():
    return jsonify(list_pt_manual_movements_all(
        db.session,
        actor_id=_actor_id(),
        article_id=request.args.get("articulo_scm_id"),
        location=request.args.get("ubicacion"),
    ))
