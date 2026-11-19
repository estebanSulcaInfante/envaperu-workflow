"""Interfaz de Estación 2. La credencial de dispositivo determina su cuenta SCM."""
import hashlib
import hmac
import os
from uuid import UUID

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select

from app.extensions import db
from app.models.estacion_pesaje import EstacionPesaje
from app.models.trabajador import Trabajador
from app.services.scm_service_support import ScmServiceError


mesa_bp = Blueprint("mesa_central", __name__)


@mesa_bp.before_request
def authenticate_device():
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return jsonify(error="Credencial de Estación 2 inválida."), 401
    station = db.session.scalar(select(EstacionPesaje).where(EstacionPesaje.codigo == "MESA-ESTACION-2"))
    if station is None or not hmac.compare_digest(station.token_hash, hashlib.sha256(token.encode()).hexdigest()):
        return jsonify(error="Credencial de Estación 2 inválida."), 401
    if station.estado_admin != "ACTIVA":
        return jsonify(error="Estación 2 deshabilitada."), 403
    actor = db.session.scalar(select(Trabajador).where(Trabajador.codigo == "ESTACION-2",
                                                        Trabajador.activo.is_(True)))
    if actor is None:
        return jsonify(error="Cuenta de Estación 2 deshabilitada."), 403
    g.mesa_actor_id = actor.id
    g.mesa_station_id = station.station_id


@mesa_bp.errorhandler(ScmServiceError)
def handle_error(exc):
    db.session.rollback()
    return jsonify(error=exc.to_dict()), exc.status_code


@mesa_bp.route("/<path:operation>", methods=["GET", "POST", "DELETE"])
def mesa_operation(operation):
    from app.services import scm_mesa_inventory as inventory
    from app.services import scm_mesa_returns as returns

    path = "/" + operation
    if request.method == "GET":
        if path == "/state":
            return jsonify(returns.vista_state(db.session, g.mesa_actor_id))
        if path == "/inventario/estado":
            return jsonify(inventory.vista_state(db.session, g.mesa_actor_id))
        parts = operation.split("/")
        if len(parts) == 3 and parts[1] == "etiqueta" and parts[0] in ("inventario", "retornos"):
            service = inventory if parts[0] == "inventario" else returns
            return jsonify(service.label(db.session, g.mesa_actor_id, parts[2]))
        return jsonify(error="Consulta de estación desconocida."), 404
    if os.environ.get("SCM_MESA_MOVIMIENTOS_HABILITADOS") != "1":
        return jsonify(error="Estación 2 está en puesta en marcha. Los registros se habilitarán tras probar la balanza, la impresora y el lector."), 409
    try:
        key = UUID(request.headers["Idempotency-Key"])
    except (KeyError, ValueError):
        return jsonify(error="Se requiere una clave de operación válida."), 422
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) & {"actor_id", "station_id", "ubicacion_codigo"}:
        return jsonify(error="Solicitud de estación inválida."), 422
    if path.startswith("/inventario/"):
        result = inventory.dispatch(db.session, g.mesa_actor_id, request.method, path[len("/inventario"):], body, key)
    elif request.method == "POST" and path in ("/unions", "/receive"):
        result = returns.dispatch(db.session, g.mesa_actor_id, g.mesa_station_id, path, body, key)
    else:
        return jsonify(error="Operación de estación desconocida."), 404
    payload, status = result if isinstance(result, tuple) else (result, 200)
    return jsonify(payload), status
