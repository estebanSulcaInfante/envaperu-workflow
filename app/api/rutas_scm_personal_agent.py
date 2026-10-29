from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from app.services.scm_personal_agent_access import (
    PersonalAgentAccessError,
    agent_state,
    authorize_bridge,
    issue_handoff,
    verify_bridge_credential,
)


scm_personal_agent_bp = Blueprint("scm_personal_agent", __name__)
MAX_AGENT_BODY_BYTES = 8 * 1024


def _error(error):
    response = jsonify({"error": {"code": error.code, "message": error.message}})
    response.headers["Cache-Control"] = "private, no-store"
    return response, error.status


def _ok(payload):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _bearer():
    value = request.headers.get("Authorization", "")
    if not value.lower().startswith("bearer "):
        return None
    return value[7:].strip() or None


def _check_body_size():
    request.max_content_length = MAX_AGENT_BODY_BYTES
    if request.content_length and request.content_length > MAX_AGENT_BODY_BYTES:
        raise PersonalAgentAccessError(
            "AGENT_REQUEST_TOO_LARGE",
            "La solicitud del agente excede el limite permitido.",
            413,
        )


@scm_personal_agent_bp.after_request
def _no_store_agent_responses(response):
    response.headers["Cache-Control"] = "private, no-store"
    return response


@scm_personal_agent_bp.errorhandler(RequestEntityTooLarge)
def _request_too_large(_exception):
    return _error(
        PersonalAgentAccessError(
            "AGENT_REQUEST_TOO_LARGE",
            "La solicitud del agente excede el limite permitido.",
            413,
        )
    )


@scm_personal_agent_bp.post("/agente/acceso")
def personal_agent_access():
    try:
        _check_body_size()
        if request.data.strip():
            body = request.get_json(silent=True)
            if body != {}:
                raise PersonalAgentAccessError(
                    "AGENT_REQUEST_INVALID",
                    "La solicitud de acceso no acepta identidad del cliente.",
                    400,
                )
        return _ok(issue_handoff(current_app.config))
    except PersonalAgentAccessError as error:
        return _error(error)


@scm_personal_agent_bp.post("/agente/autorizacion")
def personal_agent_authorization():
    try:
        _check_body_size()
        # Validate bridge credentials before configuration or parsing.
        from app.services.scm_personal_agent_access import validate_configuration

        verify_bridge_credential(current_app.config, _bearer())
        validate_configuration(current_app.config)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {"assertion", "phase"}:
            raise PersonalAgentAccessError(
                "AGENT_REQUEST_INVALID",
                "La solicitud del puente no es valida.",
                400,
            )
        result = authorize_bridge(
            current_app.config,
            assertion=body["assertion"],
            phase=body["phase"],
        )
        return _ok(result)
    except PersonalAgentAccessError as error:
        return _error(error)


@scm_personal_agent_bp.get("/agente/estado")
def personal_agent_state():
    try:
        return _ok(agent_state(current_app.config))
    except PersonalAgentAccessError as error:
        return _error(error)
