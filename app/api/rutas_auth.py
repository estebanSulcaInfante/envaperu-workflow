from flask import Blueprint, g, jsonify


auth_bp = Blueprint("auth", __name__)


@auth_bp.get("/me")
def current_identity():
    actor = g.scm_actor
    payload = actor.to_dict()
    claims = getattr(g, "scm_auth_claims", {})
    payload["correo"] = claims.get("email")
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-store"
    return response

