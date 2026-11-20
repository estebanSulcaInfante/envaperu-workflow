"""Conteo físico de piezas desde una sesión SCM."""
from uuid import UUID

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select

from app.extensions import db
from app.models.scm_inventory_operations import ScmAlmacen
from app.services import scm_mesa_inventory as inventory
from app.services.scm_service_support import ScmServiceError, load_actor
from app.services.scm_warehouse_scope_service import warehouse_scope


conteo_c66_bp = Blueprint("conteo_c66", __name__)


@conteo_c66_bp.before_request
def authorize():
    if request.method == "OPTIONS":
        return None
    actor_id = getattr(g, "scm_actor_id", None)
    if actor_id is None:
        raise ScmServiceError("AUTH_REQUIRED", "Inicia sesión en SCM.", status_code=401)
    # Reviewing a candidate is an observation operation. Confirming a
    # missing manga is deliberately checked again by ``_missing`` with the
    # stronger INVENTARIO_AJUSTAR capability.
    if request.path.endswith("/faltantes"):
        capability = "INVENTARIO_AJUSTAR"
    elif request.path.endswith("/revision"):
        # Review is a read/review workflow in SCM web. It must not grant the
        # station's physical count capability as a side effect.
        capability = "INVENTARIO_VER"
    else:
        capability = "INVENTARIO_CONTEO_C66"
    load_actor(db.session, actor_id, capability=capability)
    scope = warehouse_scope(db.session, actor_id=actor_id)
    if scope["configured"] and not scope["transversal"]:
        warehouse_id = db.session.scalar(select(ScmAlmacen.id).where(
            ScmAlmacen.codigo == "ALM-PZ", ScmAlmacen.activo.is_(True)))
        if "PIEZA_COLOR" not in scope["classes"].get(warehouse_id, set()):
            raise ScmServiceError("SCOPE_REQUIRED", "Se requiere acceso al almacén de piezas.", status_code=403)


@conteo_c66_bp.errorhandler(ScmServiceError)
def service_error(exc):
    db.session.rollback()
    return jsonify(error=exc.to_dict()), exc.status_code


@conteo_c66_bp.get("/estado")
def state():
    return jsonify(inventory.vista_state_conteo(db.session, g.scm_actor_id, request.args.get("sesion_id")))


@conteo_c66_bp.get("/revision")
def revision_state():
    """Review queue for SCM web, next to the C66 count workspace."""
    return jsonify(inventory.vista_state_revision(db.session, g.scm_actor_id))


def command(path, fields):
    body = request.get_json(silent=True)
    if body is None and request.method == "DELETE" and not request.data:
        body = {}
    if not isinstance(body, dict) or set(body) - fields:
        raise ScmServiceError("INVALID_BODY", "Revisa los datos del conteo.")
    try:
        key = UUID(request.headers.get("Idempotency-Key", ""))
    except ValueError as exc:
        raise ScmServiceError("INVALID_KEY", "La operación requiere una clave válida.") from exc
    if path == "/sesiones":
        body = {**body, "clave": str(key)}
    else:
        session_id = (request.view_args or {}).get("session_id")
        if session_id is not None:
            inventory.require_named_count(db.session, session_id)
    result = inventory.dispatch(db.session, g.scm_actor_id, request.method, path, body, key)
    payload, status = result if isinstance(result, tuple) else (result, 200)
    return jsonify(payload), status


@conteo_c66_bp.post("/sesiones")
def start():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("piezas"), list) or not body["piezas"] or not isinstance(body.get("nombre"), str) or not body["nombre"].strip():
        raise ScmServiceError("MISSING_SCOPE", "Indica el nombre y las piezas del conteo.")
    return command("/sesiones", {"nombre", "piezas", "zona", "clave"})


@conteo_c66_bp.post("/sesiones/<uuid:session_id>/lecturas")
def read(session_id):
    return command(f"/sesiones/{session_id}/lecturas", {"codigo", "zona", "confirmar_zona", "zona_anterior"})


@conteo_c66_bp.delete("/sesiones/<uuid:session_id>/lecturas/<code>")
def remove(session_id, code):
    return command(f"/sesiones/{session_id}/lecturas/{code}", set())


@conteo_c66_bp.post("/sesiones/<uuid:session_id>/cerrar")
def close(session_id):
    return command(f"/sesiones/{session_id}/cerrar", {"cobertura", "marcar_no_habidas", "no_habidas"})


@conteo_c66_bp.post("/revision")
def review():
    return command("/revision", {"codigo", "cobertura", "movimientos_revisados", "busqueda"})


@conteo_c66_bp.post("/faltantes")
def missing():
    return command("/faltantes", {"codigo", "token", "motivo", "clave"})
