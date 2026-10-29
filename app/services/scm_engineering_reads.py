import base64
import binascii
import json
import unicodedata

from sqlalchemy import func, or_, select

from app.models.scm_articulos import ScmArticulo
from app.models.scm_empaque import (
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_estructuras import (
    ESTADO_ESTRUCTURA_PENDIENTE,
    ScmEstructuraRevision,
)
from app.models.scm_rutas import ESTADO_RUTA_BORRADOR, ScmRutaRevision
from app.services.scm_service_support import ScmServiceError, load_actor


ENDPOINT = "ingenieria/aprobaciones"
CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 2048
MAX_LIMIT = 100
DEFAULT_LIMIT = 25
DOMAINS = {
    "ESTRUCTURA": ("ESTRUCTURA_VER", ESTADO_ESTRUCTURA_PENDIENTE),
    "RUTA": ("RUTA_VER", ESTADO_RUTA_BORRADOR),
    "EMPAQUE": ("EMPAQUE_VER", "BORRADOR"),
}


def _iso(value):
    return value.isoformat() if value is not None else None


def _decimal(value):
    return format(value, "f") if value is not None else None


def _normalize(value, *, field, max_length=200):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            f"El campo {field} debe ser texto.",
            status_code=400,
        )
    value = " ".join(value.strip().split())
    if len(value) > max_length:
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            f"El campo {field} supera el limite permitido.",
            status_code=400,
        )
    return value or None


def _fold(value):
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


def _sql_fold(column):
    expression = func.lower(column)
    for source, target in (
        ("á", "a"),
        ("Á", "a"),
        ("é", "e"),
        ("É", "e"),
        ("í", "i"),
        ("Í", "i"),
        ("ó", "o"),
        ("Ó", "o"),
        ("ú", "u"),
        ("Ú", "u"),
        ("ü", "u"),
        ("Ü", "u"),
        ("ñ", "n"),
        ("Ñ", "n"),
    ):
        expression = func.replace(expression, source, target)
    return expression


def _like_literal(column, query):
    escaped = query.replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%").replace("_", "\\_")
    return _sql_fold(column).like(f"%{escaped}%", escape="\\")


def _parse_limit(limit):
    try:
        parsed = int(limit)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "limite debe ser un entero entre 1 y 100.",
            status_code=400,
        ) from error
    if parsed < 1 or parsed > MAX_LIMIT:
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "limite debe ser un entero entre 1 y 100.",
            status_code=400,
        )
    return parsed


def _encode_cursor(*, actor_id, capability, domain, query, limit, last_id):
    payload = {
        "v": CURSOR_VERSION,
        "actor_id": actor_id,
        "capability": capability,
        "endpoint": ENDPOINT,
        "domain": domain,
        "q": query,
        "limit": limit,
        "order": "revision_id_desc",
        "last_id": last_id,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    if len(encoded) > MAX_CURSOR_LENGTH:
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "El cursor excede el limite permitido.",
            status_code=400,
        )
    return encoded


def _decode_cursor(cursor, *, actor_id, capability, domain, query, limit):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "El cursor no es valido.",
            status_code=400,
        )
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.b64decode(
            (cursor + padding).encode(),
            altchars=b"-_",
            validate=True,
        ).decode())
    except (
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "El cursor no es valido.",
            status_code=400,
        ) from error
    if not isinstance(payload, dict):
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "El cursor no es valido.",
            status_code=400,
        )
    expected = {
        "v": CURSOR_VERSION,
        "actor_id": actor_id,
        "capability": capability,
        "endpoint": ENDPOINT,
        "domain": domain,
        "q": query,
        "limit": limit,
        "order": "revision_id_desc",
    }
    if (
        not isinstance(payload.get("v"), int)
        or isinstance(payload.get("v"), bool)
        or not isinstance(payload.get("actor_id"), int)
        or isinstance(payload.get("actor_id"), bool)
        or not all(
            isinstance(payload.get(key), str)
            for key in ("capability", "endpoint", "domain", "order")
        )
        or payload.get("q") is not None
        and not isinstance(payload.get("q"), str)
        or not isinstance(payload.get("limit"), int)
        or isinstance(payload.get("limit"), bool)
    ):
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "El cursor no es valido.",
            status_code=400,
        )
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "El cursor no corresponde al actor, dominio o filtros actuales.",
            status_code=400,
        )
    last_id = payload.get("last_id")
    if not isinstance(last_id, int) or isinstance(last_id, bool) or last_id <= 0:
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "El cursor no es valido.",
            status_code=400,
        )
    return last_id


def _base_item(row, *, domain):
    return {
        "dominio": domain,
        "revision_id": row.revision_id,
        "numero_revision": row.numero_revision,
        "estado": row.estado,
        "version": row.version,
        "creada_por_id": row.creada_por_id,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _where_search(query, columns):
    if query is None:
        return None
    return or_(*(_like_literal(column, query) for column in columns))


def _query_page(session, *, domain, query, last_id, limit):
    if domain == "ESTRUCTURA":
        revision = ScmEstructuraRevision
        article = ScmArticulo
        count_filters = [revision.estado == ESTADO_ESTRUCTURA_PENDIENTE]
        search = _where_search(query, [article.codigo, article.nombre, article.clase])
        if search is not None:
            count_filters.append(search)
        filters = list(count_filters)
        if last_id is not None:
            filters.append(revision.id < last_id)
        base = select(revision.id).join(
            article, article.id == revision.articulo_resultado_id
        ).where(*count_filters)
        total = session.scalar(select(func.count()).select_from(base.subquery()))
        rows = session.execute(
            select(
                revision.id.label("revision_id"),
                revision.numero_revision,
                revision.estado,
                revision.version,
                revision.creada_por_id,
                revision.created_at,
                revision.updated_at,
                article.id.label("article_id"),
                article.codigo.label("article_code"),
                article.nombre.label("article_name"),
                article.clase.label("article_class"),
            ).join(article, article.id == revision.articulo_resultado_id)
            .where(*filters)
            .order_by(revision.id.desc())
            .limit(limit + 1)
        ).all()
        items = []
        for row in rows:
            item = _base_item(row, domain=domain)
            item["objetivo"] = {
                "id": row.article_id,
                "codigo": row.article_code,
                "nombre": row.article_name,
                "clase": row.article_class,
            }
            items.append(item)
        return total, items

    if domain == "RUTA":
        revision = ScmRutaRevision
        article = ScmArticulo
        count_filters = [revision.estado == ESTADO_RUTA_BORRADOR]
        search = _where_search(query, [article.codigo, article.nombre, article.clase])
        if search is not None:
            count_filters.append(search)
        filters = list(count_filters)
        if last_id is not None:
            filters.append(revision.id < last_id)
        base = select(revision.id).join(
            article, article.id == revision.articulo_objetivo_id
        ).where(*count_filters)
        total = session.scalar(select(func.count()).select_from(base.subquery()))
        rows = session.execute(
            select(
                revision.id.label("revision_id"),
                revision.numero_revision,
                revision.estado,
                revision.version,
                revision.creada_por_id,
                revision.created_at,
                revision.updated_at,
                article.id.label("article_id"),
                article.codigo.label("article_code"),
                article.nombre.label("article_name"),
                article.clase.label("article_class"),
            ).join(article, article.id == revision.articulo_objetivo_id)
            .where(*filters)
            .order_by(revision.id.desc())
            .limit(limit + 1)
        ).all()
        items = []
        for row in rows:
            item = _base_item(row, domain=domain)
            item["objetivo"] = {
                "id": row.article_id,
                "codigo": row.article_code,
                "nombre": row.article_name,
                "clase": row.article_class,
            }
            items.append(item)
        return total, items

    revision = ScmReglaEmpaqueRevision
    rule = ScmReglaEmpaque
    profile = ScmPerfilEmpacable
    container = ScmTipoContenedor
    count_filters = [revision.estado == "BORRADOR"]
    search = _where_search(query, [
        profile.codigo,
        profile.nombre,
        profile.descripcion_fisica,
        container.codigo,
        container.nombre,
        container.clase,
    ])
    if search is not None:
        count_filters.append(search)
    filters = list(count_filters)
    if last_id is not None:
        filters.append(revision.id < last_id)
    joins = (
        select(revision.id)
        .join(rule, rule.id == revision.regla_id)
        .join(profile, profile.id == rule.perfil_empacable_id)
        .join(container, container.id == rule.tipo_contenedor_id)
        .where(*count_filters)
    )
    total = session.scalar(select(func.count()).select_from(joins.subquery()))
    rows = session.execute(
        select(
            revision.id.label("revision_id"),
            revision.numero_revision,
            revision.estado,
            revision.version,
            revision.creada_por_id,
            revision.created_at,
            revision.updated_at,
            profile.id.label("profile_id"),
            profile.codigo.label("profile_code"),
            profile.nombre.label("profile_name"),
            profile.descripcion_fisica.label("profile_description"),
            profile.activo.label("profile_active"),
            profile.version.label("profile_version"),
            profile.created_at.label("profile_created_at"),
            profile.updated_at.label("profile_updated_at"),
            container.id.label("container_id"),
            container.codigo.label("container_code"),
            container.clase.label("container_class"),
            container.nombre.label("container_name"),
            container.material.label("container_material"),
            container.dimensiones_json.label("container_dimensions"),
            container.tara_nominal_g.label("container_tare"),
            container.tolerancia_tara_g.label("container_tare_tolerance"),
            container.peso_bruto_max_kg.label("container_gross_max"),
            container.activo.label("container_active"),
            container.version.label("container_version"),
            container.created_at.label("container_created_at"),
            container.updated_at.label("container_updated_at"),
        )
        .join(rule, rule.id == revision.regla_id)
        .join(profile, profile.id == rule.perfil_empacable_id)
        .join(container, container.id == rule.tipo_contenedor_id)
        .where(*filters)
        .order_by(revision.id.desc())
        .limit(limit + 1)
    ).all()
    items = []
    for row in rows:
        item = _base_item(row, domain=domain)
        item["perfil"] = {
            "id": row.profile_id,
            "codigo": row.profile_code,
            "nombre": row.profile_name,
            "descripcion_fisica": row.profile_description,
            "activo": row.profile_active,
            "version": row.profile_version,
            "created_at": _iso(row.profile_created_at),
            "updated_at": _iso(row.profile_updated_at),
        }
        item["contenedor"] = {
            "id": row.container_id,
            "codigo": row.container_code,
            "clase": row.container_class,
            "nombre": row.container_name,
            "material": row.container_material,
            "dimensiones": row.container_dimensions,
            "tara_nominal_g": _decimal(row.container_tare),
            "tolerancia_tara_g": _decimal(row.container_tare_tolerance),
            "peso_bruto_max_kg": _decimal(row.container_gross_max),
            "activo": row.container_active,
            "version": row.container_version,
            "created_at": _iso(row.container_created_at),
            "updated_at": _iso(row.container_updated_at),
        }
        items.append(item)
    return total, items


def list_engineering_approvals(
    session,
    *,
    actor_id,
    domain,
    query=None,
    limit=DEFAULT_LIMIT,
    cursor=None,
):
    domain = _normalize(domain, field="dominio", max_length=32)
    domain = domain.upper() if domain else None
    if domain not in DOMAINS:
        raise ScmServiceError(
            "INVALID_ENGINEERING_APPROVAL_PAGE",
            "dominio debe ser ESTRUCTURA, RUTA o EMPAQUE.",
            status_code=400,
        )
    query = _normalize(query, field="q")
    query = _fold(query) if query is not None else None
    limit = _parse_limit(limit)
    capability, state = DOMAINS[domain]
    load_actor(session, actor_id, capability=capability)
    last_id = _decode_cursor(
        cursor,
        actor_id=actor_id,
        capability=capability,
        domain=domain,
        query=query,
        limit=limit,
    )
    total, items = _query_page(
        session,
        domain=domain,
        query=query,
        last_id=last_id,
        limit=limit,
    )
    has_more = len(items) > limit
    if has_more:
        items = items[:limit]
    next_cursor = (
        _encode_cursor(
            actor_id=actor_id,
            capability=capability,
            domain=domain,
            query=query,
            limit=limit,
            last_id=items[-1]["revision_id"],
        )
        if has_more
        else None
    )
    return {
        "scope": {
            "dominio": domain,
            "estado": state,
            "capacidad": capability,
        },
        "items": items,
        "total": total,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }
