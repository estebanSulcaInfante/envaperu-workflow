"""Per-person native OpenClaw handoff for SCM.

This module only mints a short bridge assertion.  It deliberately does not
issue or return a Supabase JWT, does not trust browser identity fields, and
does not persist bridge replay state.  Durable one-use ``jti`` consumption is
owned by the bridge consuming this assertion.
"""

from datetime import datetime, timezone
import hmac
import secrets
import time
from urllib.parse import urlparse
from uuid import UUID

import jwt
from sqlalchemy import select

from app.extensions import db
from app.models.scm_catalogos import ScmCapacidad, scm_rol_capacidad
from app.models.trabajador import RolOperativo, Trabajador, trabajador_rol


CAPABILITY = "ASISTENTE_PRODUCCION_USAR"
ISSUER = "api-scm"
AUDIENCE = "scm-agent-bridge"
AGENT_ID_PREFIX = "scm-"
IDENTITY_EMAIL_DOMAIN = "internal.envaperu.invalid"
HANDOFF_SECONDS = 30
ALLOWED_ASSERTION_CLAIMS = {
    "iss",
    "aud",
    "sub",
    "actor_id",
    "agent_id",
    "jti",
    "iat",
    "exp",
    "handoff_exp",
}


class PersonalAgentAccessError(RuntimeError):
    def __init__(self, code, message, status):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _config_value(config, *names, default=None):
    for name in names:
        value = config.get(name)
        if value not in (None, ""):
            return value
    return default


def _enabled(config):
    # The canonical flag wins whenever present, including explicit false.
    value = config.get("SCM_PERSONAL_AGENT_ACCESS_ENABLED")
    if value is None:
        value = config.get("SCM_PERSONAL_AGENT_ENABLED")
    return value is True or str(value or "").strip().lower() == "true"


def _origin(config):
    return str(
        _config_value(config, "SCM_PERSONAL_AGENT_BRIDGE_ORIGIN", default="")
        or ""
    ).strip().rstrip("/")


def _valid_origin(config):
    origin = _origin(config)
    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path not in ("", "/"):
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http" or not config.get("TESTING"):
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _secrets(config):
    assertion_key = str(
        _config_value(
            config,
            "SCM_PERSONAL_AGENT_ASSERTION_SECRET",
            "SCM_PERSONAL_AGENT_ASSERTION_KEY",
            "SCM_PERSONAL_AGENT_HANDOFF_SIGNING_KEY",
            "SCM_PERSONAL_AGENT_HANDOFF_SECRET",
            default="",
        )
        or ""
    )
    bridge_credential = str(
        _config_value(
            config,
            "SCM_PERSONAL_AGENT_BRIDGE_CREDENTIAL",
            "SCM_PERSONAL_AGENT_SERVICE_CREDENTIAL",
            "SCM_PERSONAL_AGENT_SERVICE_SECRET",
            default="",
        )
        or ""
    )
    return assertion_key, bridge_credential


def validate_configuration(config):
    """Return no value when the handoff is safe to use, otherwise fail closed."""
    if not _enabled(config):
        raise PersonalAgentAccessError(
            "AGENT_ACCESS_DISABLED",
            "El acceso al agente personal no esta habilitado.",
            403,
        )
    if not _valid_origin(config):
        raise PersonalAgentAccessError(
            "AGENT_ACCESS_MISCONFIGURED",
            "El origen del puente del agente no es valido.",
            503,
        )
    assertion_key, bridge_credential = _secrets(config)
    if len(assertion_key.encode("utf-8")) < 32 or len(
        bridge_credential.encode("utf-8")
    ) < 32:
        raise PersonalAgentAccessError(
            "AGENT_ACCESS_MISCONFIGURED",
            "Las credenciales del puente del agente no cumplen el minimo requerido.",
            503,
        )
    if hmac.compare_digest(assertion_key, bridge_credential):
        raise PersonalAgentAccessError(
            "AGENT_ACCESS_MISCONFIGURED",
            "Las credenciales de firma y del puente deben ser distintas.",
            503,
        )
    try:
        max_session = int(
            _config_value(
                config,
                "SCM_PERSONAL_AGENT_MAX_SESSION_SECONDS",
                "SCM_PERSONAL_AGENT_SESSION_MAX_SECONDS",
                default=3600,
            )
        )
    except (TypeError, ValueError):
        max_session = 0
    if max_session <= 0 or max_session > 86400:
        raise PersonalAgentAccessError(
            "AGENT_ACCESS_MISCONFIGURED",
            "La duracion maxima de sesion del agente no es valida.",
            503,
        )


def _max_session_seconds(config):
    return int(
        _config_value(
            config,
            "SCM_PERSONAL_AGENT_MAX_SESSION_SECONDS",
            "SCM_PERSONAL_AGENT_SESSION_MAX_SECONDS",
            default=3600,
        )
    )


def _now_epoch():
    return int(time.time())


def _as_epoch(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PersonalAgentAccessError(
            "AGENT_AUTH_INVALID",
            f"La afirmacion del agente contiene un {field} invalido.",
            403,
        )
    result = int(value)
    if result != value:
        raise PersonalAgentAccessError(
            "AGENT_AUTH_INVALID",
            f"La afirmacion del agente contiene un {field} invalido.",
            403,
        )
    return result


def _agent_id(auth_user_id):
    return f"{AGENT_ID_PREFIX}{auth_user_id.hex}"


def _identity_email(auth_user_id):
    return f"scm+{auth_user_id}@{IDENTITY_EMAIL_DOMAIN}"


def _has_capability(actor_id):
    capability_id = db.session.scalar(
        select(ScmCapacidad.id)
        .join(
            scm_rol_capacidad,
            scm_rol_capacidad.c.capacidad_id == ScmCapacidad.id,
        )
        .join(
            RolOperativo,
            RolOperativo.id == scm_rol_capacidad.c.rol_operativo_id,
        )
        .join(
            trabajador_rol,
            trabajador_rol.c.rol_operativo_id == RolOperativo.id,
        )
        .where(
            trabajador_rol.c.trabajador_id == actor_id,
            RolOperativo.activo.is_(True),
            ScmCapacidad.activo.is_(True),
            ScmCapacidad.codigo == CAPABILITY,
        )
        .limit(1)
    )
    return capability_id is not None


def _fresh_actor(auth_user_id, actor_id=None):
    query = (
        select(Trabajador)
        .where(
            Trabajador.auth_user_id == auth_user_id,
            Trabajador.activo.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    if actor_id is not None:
        query = query.where(Trabajador.id == actor_id)
    actor = db.session.scalar(query)
    if actor is None or not _has_capability(actor.id):
        raise PersonalAgentAccessError(
            "AGENT_ACCESS_FORBIDDEN",
            "El participante no esta autorizado para usar su agente.",
            403,
        )
    return actor


def _verified_session_actor():
    """Resolve the already verified Supabase session without trusting client data."""
    from flask import current_app, g

    if current_app.config.get("SCM_AUTH_MODE") != "supabase":
        raise PersonalAgentAccessError(
            "AGENT_AUTH_REQUIRED",
            "Se requiere una sesion Supabase verificada.",
            401,
        )
    actor = getattr(g, "scm_actor", None)
    claims = getattr(g, "scm_auth_claims", None) or {}
    if actor is None or not isinstance(claims, dict):
        raise PersonalAgentAccessError(
            "AGENT_AUTH_REQUIRED",
            "Se requiere una sesion SCM verificada.",
            401,
        )
    try:
        auth_user_id = UUID(str(claims.get("sub")))
        original_exp = _as_epoch(claims.get("exp"), "exp")
        issued_at = _as_epoch(claims.get("iat"), "iat")
    except (ValueError, TypeError):
        raise PersonalAgentAccessError(
            "AGENT_AUTH_REQUIRED",
            "Se requiere una sesion SCM verificada.",
            401,
        )
    now = _now_epoch()
    if original_exp <= now or issued_at > now + 60:
        raise PersonalAgentAccessError(
            "AGENT_AUTH_REQUIRED",
            "La sesion SCM no es valida o ha vencido.",
            401,
        )
    if actor.auth_user_id != auth_user_id:
        raise PersonalAgentAccessError(
            "AGENT_AUTH_REQUIRED",
            "La identidad SCM no coincide con el participante.",
            401,
        )
    fresh = _fresh_actor(auth_user_id, actor.id)
    return fresh, auth_user_id, original_exp, issued_at


def issue_handoff(config):
    validate_configuration(config)
    actor, auth_user_id, original_exp, _issued_at = _verified_session_actor()
    now = _now_epoch()
    handoff_exp = min(now + HANDOFF_SECONDS, original_exp)
    session_exp = min(
        now + _max_session_seconds(config),
        original_exp,
    )
    if handoff_exp <= now or session_exp <= now:
        raise PersonalAgentAccessError(
            "AGENT_ACCESS_FORBIDDEN",
            "La sesion SCM no permite abrir el agente.",
            403,
        )
    auth_user_text = str(auth_user_id)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": auth_user_text,
        "actor_id": actor.id,
        "agent_id": _agent_id(auth_user_id),
        "jti": str(UUID(bytes=secrets.token_bytes(16))),
        "iat": now,
        "exp": session_exp,
        "handoff_exp": handoff_exp,
    }
    assertion_key, _bridge_credential = _secrets(config)
    assertion = jwt.encode(payload, assertion_key, algorithm="HS256")
    return {
        "exchange_url": f"{_origin(config)}/_scm/exchange",
        "assertion": assertion,
        "agent_id": payload["agent_id"],
        "expires_at": datetime.fromtimestamp(
            session_exp, tz=timezone.utc
        ).isoformat(),
    }


def _verify_assertion(config, assertion, phase):
    assertion_key, _bridge_credential = _secrets(config)
    try:
        claims = jwt.decode(
            assertion,
            assertion_key,
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={
                "require": sorted(ALLOWED_ASSERTION_CLAIMS),
                "verify_exp": False,
            },
        )
    except (jwt.PyJWTError, TypeError, ValueError):
        raise PersonalAgentAccessError(
            "AGENT_ASSERTION_INVALID",
            "La afirmacion del agente no es valida.",
            403,
        )
    if set(claims) != ALLOWED_ASSERTION_CLAIMS:
        raise PersonalAgentAccessError(
            "AGENT_ASSERTION_INVALID",
            "La afirmacion del agente contiene campos no permitidos.",
            403,
        )
    try:
        auth_user_id = UUID(str(claims["sub"]))
        if isinstance(claims["actor_id"], bool) or not isinstance(
            claims["actor_id"], int
        ):
            raise ValueError
        actor_id = claims["actor_id"]
        UUID(str(claims["jti"]))
        iat = _as_epoch(claims["iat"], "iat")
        exp = _as_epoch(claims["exp"], "exp")
        handoff_exp = _as_epoch(claims["handoff_exp"], "handoff_exp")
    except (ValueError, TypeError, KeyError):
        raise PersonalAgentAccessError(
            "AGENT_ASSERTION_INVALID",
            "La afirmacion del agente contiene identidad o tiempos invalidos.",
            403,
        )
    if (
        claims["iss"] != ISSUER
        or claims["aud"] != AUDIENCE
        or claims["agent_id"] != _agent_id(auth_user_id)
        or exp <= iat
        or handoff_exp <= iat
        or handoff_exp > exp
        or handoff_exp > iat + HANDOFF_SECONDS
        or exp > iat + _max_session_seconds(config)
    ):
        raise PersonalAgentAccessError(
            "AGENT_ASSERTION_INVALID",
            "La afirmacion del agente no cumple el contrato temporal.",
            403,
        )
    now = _now_epoch()
    if phase == "exchange" and now >= handoff_exp:
        raise PersonalAgentAccessError(
            "AGENT_ASSERTION_EXPIRED",
            "La ventana de intercambio del agente ha vencido.",
            403,
        )
    if phase == "session" and now >= exp:
        raise PersonalAgentAccessError(
            "AGENT_ASSERTION_EXPIRED",
            "La sesion del agente ha vencido.",
            403,
        )
    actor = _fresh_actor(auth_user_id, actor_id)
    return actor, auth_user_id, exp, str(claims["jti"])


def authorize_bridge(config, *, assertion, phase):
    validate_configuration(config)
    if (
        not isinstance(phase, str)
        or phase not in {"exchange", "session"}
        or not isinstance(assertion, str)
    ):
        raise PersonalAgentAccessError(
            "AGENT_REQUEST_INVALID",
            "La solicitud del puente no es valida.",
            400,
        )
    actor, auth_user_id, expires_at, claims_jti = _verify_assertion(
        config, assertion, phase
    )
    return {
        "identity": {
            "auth_user_id": str(auth_user_id),
            "actor_id": actor.id,
            "email": _identity_email(auth_user_id),
        },
        "agent_id": _agent_id(auth_user_id),
        "jti": claims_jti,
        "expires_at": datetime.fromtimestamp(
            expires_at, tz=timezone.utc
        ).isoformat(),
    }


def verify_bridge_credential(config, supplied):
    _assertion_key, expected = _secrets(config)
    if not supplied or not hmac.compare_digest(str(supplied), expected):
        raise PersonalAgentAccessError(
            "AGENT_BRIDGE_UNAUTHORIZED",
            "El puente del agente no esta autorizado.",
            401,
        )


def agent_state(config):
    validate_configuration(config)
    actor, auth_user_id, original_exp, _issued_at = _verified_session_actor()
    return {
        "available": True,
        "status": "READY",
        "agent_id": _agent_id(auth_user_id),
        "expires_at": datetime.fromtimestamp(
            original_exp, tz=timezone.utc
        ).isoformat(),
    }
