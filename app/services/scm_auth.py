from uuid import UUID

import jwt
from flask import current_app, g, jsonify, request
from jwt import PyJWKClient
from sqlalchemy import select

from app.extensions import db
from app.models.trabajador import Trabajador


AUTH_MODE_LOCAL = "local_actor"
AUTH_MODE_SUPABASE = "supabase"
AUTH_MODES = {AUTH_MODE_LOCAL, AUTH_MODE_SUPABASE}


class ScmAuthConfigurationError(RuntimeError):
    pass


class SupabaseJwtVerifier:
    def __init__(self, *, supabase_url, audience, issuer=None):
        normalized_url = str(supabase_url or "").strip().rstrip("/")
        if not normalized_url:
            raise ScmAuthConfigurationError(
                "SUPABASE_URL es obligatorio cuando SCM_AUTH_MODE=supabase."
            )
        self.audience = audience or "authenticated"
        self.issuer = issuer or f"{normalized_url}/auth/v1"
        self.jwks_client = PyJWKClient(
            f"{normalized_url}/auth/v1/.well-known/jwks.json",
            cache_jwk_set=True,
            lifespan=300,
        )

    def verify(self, token):
        signing_key = self.jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "sub", "aud"]},
        )


def _error(code, message, status):
    response = jsonify({"error": {"code": code, "message": message}})
    response.headers["Cache-Control"] = "private, no-store"
    return response, status


def _bearer_token():
    authorization = request.headers.get("Authorization", "").strip()
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


def _is_public_request():
    if request.method == "OPTIONS":
        return True
    if request.path in {"/api/health", "/api/ready"}:
        return True
    if request.path.startswith("/api/integration/v1/"):
        return True
    if request.method == "GET" and request.path.endswith("/imagen"):
        return True
    if request.method == "GET" and request.path.endswith("/qr"):
        return True
    return False


ARTICLE_MASTER_PREFIXES = (
    "/api/productos",
    "/api/piezas",
    "/api/piezas-color",
    "/api/moldes",
    "/api/formas",
    "/api/colores",
    "/api/familias-color",
    "/api/catalogo/lineas",
    "/api/catalogo/familias",
    "/api/catalogo/recetas-color",
    "/api/catalogo/ingredientes-receta-color",
    "/api/configurar-producto",
    "/api/importar",
)

GESTOR_SCM_WRITE_PREFIXES = (
    "/api/scm/v1/articulos",
    "/api/scm/v1/estructuras",
    "/api/scm/v1/centros-trabajo",
    "/api/scm/v1/productos",
    "/api/scm/v1/rutas",
    "/api/scm/v1/tipos-contenedor",
    "/api/scm/v1/perfiles-empacables",
    "/api/scm/v1/reglas-empaque",
    "/api/scm/v1/proveedores",
    "/api/scm/v1/categorias-recepcion",
    "/api/scm/v1/materiales",
)


def _has_any(actor, capabilities):
    return any(actor.tiene_capacidad(code) for code in capabilities)


def _authorize_request(actor):
    path = request.path
    method = request.method

    if path.startswith((
        "/api/catalogo/roles-operativos",
        "/api/catalogo/capacidades",
    )) and not actor.tiene_capacidad("AUTORIZACION_SCM_ADMINISTRAR"):
        return _error(
            "CAPABILITY_REQUIRED",
            "Esta acción requiere administrar autorizaciones SCM.",
            403,
        )

    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        if path.startswith("/api/catalogo/trabajadores"):
            required = ("AUTORIZACION_SCM_ADMINISTRAR",)
        elif path.startswith((
            "/api/catalogo/maquinas",
            "/api/catalogo/tipos-maquina",
        )):
            required = (
                "CATALOGO_PLANTA_ADMINISTRAR",
                "CONFIG_RECEPCION_ADMINISTRAR",
                "OF_EDITAR_BORRADOR",
            )
        elif path.startswith(ARTICLE_MASTER_PREFIXES):
            required = ("ARTICULO_ADMINISTRAR",)
        else:
            required = ()

        if required and not _has_any(actor, required):
            return _error(
                "CAPABILITY_REQUIRED",
                "Tu perfil no puede modificar este catálogo.",
                403,
            )

        is_master_steward = any(
            role.codigo == "GESTOR_MAESTROS" and role.activo
            for role in actor.roles
        )
        if is_master_steward:
            allowed = (
                path.startswith(ARTICLE_MASTER_PREFIXES)
                or path.startswith((
                    "/api/catalogo/maquinas",
                    "/api/catalogo/tipos-maquina",
                ))
                or path.startswith(GESTOR_SCM_WRITE_PREFIXES)
            )
            if not allowed:
                return _error(
                    "MASTER_STEWARD_SCOPE",
                    "El gestor de maestros no puede ejecutar operaciones productivas.",
                    403,
                )
    return None


def configure_scm_auth(app):
    mode = app.config.get("SCM_AUTH_MODE", AUTH_MODE_LOCAL)
    if mode not in AUTH_MODES:
        raise ScmAuthConfigurationError(
            f"SCM_AUTH_MODE debe ser uno de {sorted(AUTH_MODES)}."
        )

    if mode == AUTH_MODE_SUPABASE:
        app.extensions["scm_token_verifier"] = SupabaseJwtVerifier(
            supabase_url=app.config.get("SUPABASE_URL"),
            audience=app.config.get("SUPABASE_JWT_AUDIENCE"),
            issuer=app.config.get("SUPABASE_JWT_ISSUER") or None,
        )

    @app.before_request
    def authenticate_scm_request():
        if (
            current_app.config.get("SCM_AUTH_MODE") != AUTH_MODE_SUPABASE
            or not request.path.startswith("/api/")
            or _is_public_request()
        ):
            return None

        token = _bearer_token()
        if token is None:
            return _error(
                "AUTH_REQUIRED",
                "Inicia sesión para continuar.",
                401,
            )

        try:
            claims = current_app.extensions["scm_token_verifier"].verify(token)
            auth_user_id = UUID(str(claims.get("sub")))
        except (jwt.PyJWTError, ValueError, TypeError):
            return _error(
                "AUTH_TOKEN_INVALID",
                "La sesión no es válida o ha vencido.",
                401,
            )

        actor = db.session.scalar(
            select(Trabajador).where(Trabajador.auth_user_id == auth_user_id)
        )
        if actor is None:
            return _error(
                "AUTH_WORKER_NOT_LINKED",
                "La cuenta no está vinculada con un participante SCM.",
                403,
            )
        if not actor.activo:
            return _error(
                "AUTH_WORKER_INACTIVE",
                "El participante está desactivado.",
                403,
            )

        g.scm_actor = actor
        g.scm_actor_id = actor.id
        g.scm_auth_claims = claims
        return _authorize_request(actor)


def request_actor_id():
    authenticated_actor_id = getattr(g, "scm_actor_id", None)
    if authenticated_actor_id is not None:
        return authenticated_actor_id

    try:
        actor_id = int(request.headers.get("X-Actor-Id"))
    except (TypeError, ValueError) as error:
        raise ValueError(
            "X-Actor-Id debe identificar un trabajador válido."
        ) from error
    if actor_id <= 0:
        raise ValueError(
            "X-Actor-Id debe identificar un trabajador válido."
        )
    return actor_id
