from flask import Blueprint, g, jsonify

from app.extensions import db
from app.models.trabajador import Trabajador
from app.services.scm_auth import request_actor_id


auth_bp = Blueprint("auth", __name__)


@auth_bp.get("/me")
def current_identity():
    actor = getattr(g, "scm_actor", None)
    if actor is None:
        try:
            actor_id = request_actor_id()
        except ValueError:
            response = jsonify({
                "error": {
                    "code": "ACTOR_HEADER_REQUIRED",
                    "message": (
                        "X-Actor-Id debe identificar un trabajador valido."
                    ),
                }
            })
            response.headers["Cache-Control"] = "private, no-store"
            return response, 400
        actor = db.session.get(Trabajador, actor_id)
        if actor is None or not actor.activo:
            response = jsonify({
                "error": {
                    "code": "ACTOR_NOT_AUTHORIZED",
                    "message": "El participante no existe o esta inactivo.",
                }
            })
            response.headers["Cache-Control"] = "private, no-store"
            return response, 403
    payload = actor.to_dict()
    claims = getattr(g, "scm_auth_claims", {})
    payload["correo"] = claims.get("email")
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-store"
    return response
