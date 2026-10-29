import base64
import binascii
import json
import unicodedata

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from app.models.scm_articulos import (
    CLASE_PIEZA_COLOR,
    CLASE_PRODUCTO_TERMINADO,
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
    ScmArticuloPiezaColor,
    ScmArticuloProducto,
    ScmDefinicionWip,
)
from app.models.scm_empaque import (
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_estructuras import (
    ESTADO_ESTRUCTURA_APROBADA,
    ESTADO_ESTRUCTURA_PENDIENTE,
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_rutas import ESTADO_RUTA_BORRADOR, ScmRutaRevision
from app.services.scm_service_support import ScmServiceError, load_actor
from app.services.scm_structure_service import serialize_structure


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


def _parse_limit(limit, *, code="INVALID_ENGINEERING_APPROVAL_PAGE"):
    try:
        parsed = int(limit)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            code,
            "limite debe ser un entero entre 1 y 100.",
            status_code=400,
        ) from error
    if parsed < 1 or parsed > MAX_LIMIT:
        raise ScmServiceError(
            code,
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


# ---------------------------------------------------------------------------
# B1H/C1/C2 additive readers
# ---------------------------------------------------------------------------

ARTICLE_PAGE_ENDPOINT = "articulos/pagina"
ARTICLE_RESOLUTION_ENDPOINT = "articulos/resolver"
STRUCTURE_HYDRATION_ENDPOINT = "estructuras/hidratacion"
PACKAGING_PAGE_ENDPOINT = "reglas-empaque/pagina"
ARTICLE_CLASSES = (
    CLASE_PIEZA_COLOR,
    CLASE_SUBENSAMBLE_WIP,
    CLASE_PRODUCTO_TERMINADO,
)


def _invalid_read(code, message, *, details=None):
    raise ScmServiceError(code, message, status_code=400, details=details)


def _page_cursor_encode(*, endpoint, actor_id, capability, filters, order, last, code):
    payload = {
        "v": CURSOR_VERSION,
        "actor_id": actor_id,
        "capability": capability,
        "endpoint": endpoint,
        "filters": filters,
        "order": order,
        "last": last,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    if len(encoded) > MAX_CURSOR_LENGTH:
        _invalid_read(code, "El cursor excede el limite permitido.")
    return encoded


def _page_cursor_decode(
    cursor,
    *,
    endpoint,
    actor_id,
    capability,
    filters,
    order,
    code,
    last_shape,
):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        _invalid_read(code, "El cursor no es valido.")
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.b64decode(
                (cursor + padding).encode(),
                altchars=b"-_",
                validate=True,
            ).decode()
        )
    except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as error:
        raise ScmServiceError(code, "El cursor no es valido.", status_code=400) from error
    if not isinstance(payload, dict):
        _invalid_read(code, "El cursor no es valido.")
    if (
        type(payload.get("v")) is not int
        or payload.get("v") != CURSOR_VERSION
        or type(payload.get("actor_id")) is not int
        or payload.get("actor_id") != actor_id
        or payload.get("capability") != capability
        or payload.get("endpoint") != endpoint
        or payload.get("filters") != filters
        or payload.get("order") != order
        or not isinstance(payload.get("last"), list)
        or len(payload.get("last")) != len(last_shape)
        or any(
            type(value) is not expected_type
            for value, expected_type in zip(payload.get("last"), last_shape)
        )
    ):
        _invalid_read(code, "El cursor no corresponde a los filtros actuales.")
    return payload["last"]


def _parse_query(value, *, code):
    try:
        normalized = _normalize(value, field="q")
    except ScmServiceError as error:
        raise ScmServiceError(code, error.message, status_code=error.status_code) from error
    return _fold(normalized) if normalized is not None else None


def _parse_id_csv(value, *, field, code, required=True, max_items=100):
    if value is None:
        if required:
            _invalid_read(code, f"{field} es obligatorio.")
        return []
    if not isinstance(value, str) or not value.strip():
        _invalid_read(code, f"{field} debe contener IDs positivos.")
    parsed = []
    seen = set()
    for raw in value.split(","):
        token = raw.strip()
        if not token or not token.isdigit() or int(token) <= 0:
            _invalid_read(code, f"{field} debe contener IDs positivos.")
        item = int(token)
        if item not in seen:
            seen.add(item)
            parsed.append(item)
    if len(parsed) > max_items:
        _invalid_read(code, f"{field} supera el limite de {max_items} IDs.")
    return parsed


def _article_page_classes(raw):
    if raw is None:
        return tuple(sorted(ARTICLE_CLASSES))
    if not isinstance(raw, str) or not raw.strip():
        _invalid_read("INVALID_ARTICLE_PAGE", "clases debe ser un CSV no vacio.")
    values = tuple(sorted({token.strip().upper() for token in raw.split(",")}))
    if not values or any(value not in ARTICLE_CLASSES for value in values):
        _invalid_read("INVALID_ARTICLE_PAGE", "clases contiene una clase invalida.")
    return values


def _article_page_active(raw):
    value = "all" if raw is None else raw.strip().lower()
    if value not in {"all", "true", "false"}:
        _invalid_read("INVALID_ARTICLE_PAGE", "activo debe ser all, true o false.")
    return value


def _article_search_filters(query):
    if query is None:
        return []
    columns = [
        ScmArticulo.codigo,
        ScmArticulo.nombre,
        ScmArticulo.clase,
        ScmArticuloPiezaColor.pieza_color_sku,
        ScmDefinicionWip.descripcion,
        ScmArticuloProducto.producto_terminado_id,
        func.replace(_sql_fold(ScmArticulo.clase), "_", ""),
    ]
    return [
        or_(*(_like_literal(column, term) for column in columns))
        for term in query.split(" ")
    ]


def _article_base(filters):
    return (
        select(ScmArticulo.id)
        .select_from(ScmArticulo)
        .outerjoin(ScmArticuloPiezaColor, ScmArticuloPiezaColor.articulo_id == ScmArticulo.id)
        .outerjoin(ScmDefinicionWip, ScmDefinicionWip.articulo_id == ScmArticulo.id)
        .outerjoin(ScmArticuloProducto, ScmArticuloProducto.articulo_id == ScmArticulo.id)
        .where(*filters)
    )


def _article_projection(article):
    return article.to_dict()


def list_article_page(
    session,
    *,
    actor_id,
    query=None,
    classes=None,
    active="all",
    limit=DEFAULT_LIMIT,
    cursor=None,
):
    capability = "ARTICULO_VER"
    load_actor(session, actor_id, capability=capability)
    limit = _parse_limit(limit, code="INVALID_ARTICLE_PAGE")
    classes = _article_page_classes(classes)
    active = _article_page_active(active)
    query = _parse_query(query, code="INVALID_ARTICLE_PAGE")
    filters = [ScmArticulo.clase.in_(classes)]
    if active != "all":
        filters.append(ScmArticulo.activo.is_(active == "true"))
    filters.extend(_article_search_filters(query))
    base_filters = list(filters)
    cursor_filters = {
        "q": query,
        "clases": list(classes),
        "activo": active,
        "limit": limit,
    }
    last = _page_cursor_decode(
        cursor,
        endpoint=ARTICLE_PAGE_ENDPOINT,
        actor_id=actor_id,
        capability=capability,
        filters=cursor_filters,
        order="codigo_asc_id_asc",
        code="INVALID_ARTICLE_PAGE",
        last_shape=(str, int),
    )
    page_filters = list(base_filters)
    if last is not None:
        page_filters.append(
            or_(
                ScmArticulo.codigo > last[0],
                and_(ScmArticulo.codigo == last[0], ScmArticulo.id > last[1]),
            )
        )
    base = _article_base(base_filters)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    statement = (
        select(ScmArticulo)
        .select_from(ScmArticulo)
        .outerjoin(ScmArticuloPiezaColor, ScmArticuloPiezaColor.articulo_id == ScmArticulo.id)
        .outerjoin(ScmDefinicionWip, ScmDefinicionWip.articulo_id == ScmArticulo.id)
        .outerjoin(ScmArticuloProducto, ScmArticuloProducto.articulo_id == ScmArticulo.id)
        .where(*page_filters)
        .options(
            selectinload(ScmArticulo.pieza_color),
            selectinload(ScmArticulo.definicion_wip),
            selectinload(ScmArticulo.producto),
        )
        .order_by(ScmArticulo.codigo, ScmArticulo.id)
        .limit(limit + 1)
    )
    articles = session.scalars(statement).unique().all()
    has_more = len(articles) > limit
    articles = articles[:limit]
    items = [_article_projection(article) for article in articles]
    next_cursor = (
        _page_cursor_encode(
            endpoint=ARTICLE_PAGE_ENDPOINT,
            code="INVALID_ARTICLE_PAGE",
            actor_id=actor_id,
            capability=capability,
            filters=cursor_filters,
            order="codigo_asc_id_asc",
            last=[items[-1]["codigo"], items[-1]["id"]],
        )
        if has_more
        else None
    )
    return {
        "items": items,
        "total": total,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def resolve_articles(session, *, actor_id, ids):
    capability = "ARTICULO_VER"
    load_actor(session, actor_id, capability=capability)
    article_ids = _parse_id_csv(
        ids,
        field="ids",
        code="INVALID_ARTICLE_RESOLUTION",
    )
    articles = session.scalars(
        select(ScmArticulo)
        .where(ScmArticulo.id.in_(article_ids))
        .options(
            selectinload(ScmArticulo.pieza_color),
            selectinload(ScmArticulo.definicion_wip),
            selectinload(ScmArticulo.producto),
        )
        .order_by(ScmArticulo.id)
    ).unique().all()
    found = {article.id for article in articles}
    return {
        "items": [_article_projection(article) for article in articles],
        "missing_ids": [article_id for article_id in article_ids if article_id not in found],
    }


def hydrate_structures(session, *, actor_id, article_ids=None, revision_ids=None):
    capability = "ESTRUCTURA_VER"
    load_actor(session, actor_id, capability=capability)
    article_ids = _parse_id_csv(
        article_ids,
        field="articulo_ids",
        code="INVALID_STRUCTURE_HYDRATION",
        required=False,
    )
    revision_ids = _parse_id_csv(
        revision_ids,
        field="revision_ids",
        code="INVALID_STRUCTURE_HYDRATION",
        required=False,
    )
    if not article_ids and not revision_ids:
        _invalid_read(
            "INVALID_STRUCTURE_HYDRATION",
            "Se requiere articulo_ids o revision_ids.",
        )
    if len(article_ids) + len(revision_ids) > 100:
        _invalid_read(
            "INVALID_STRUCTURE_HYDRATION",
            "La hidratacion supera el limite combinado de 100 IDs.",
        )
    options = (
        joinedload(ScmEstructuraRevision.articulo_resultado),
        selectinload(ScmEstructuraRevision.componentes).joinedload(
            ScmEstructuraComponente.articulo_componente
        ),
    )
    approved = session.scalars(
        select(ScmEstructuraRevision)
        .where(
            ScmEstructuraRevision.articulo_resultado_id.in_(article_ids),
            ScmEstructuraRevision.estado == ESTADO_ESTRUCTURA_APROBADA,
        )
        .options(*options)
        .order_by(ScmEstructuraRevision.articulo_resultado_id)
    ).unique().all() if article_ids else []
    referenced = session.scalars(
        select(ScmEstructuraRevision)
        .where(ScmEstructuraRevision.id.in_(revision_ids))
        .options(*options)
        .order_by(ScmEstructuraRevision.id)
    ).unique().all() if revision_ids else []
    approved_found = {revision.articulo_resultado_id for revision in approved}
    referenced_found = {revision.id for revision in referenced}
    return {
        "approved": [serialize_structure(revision) for revision in approved],
        "referenced": [serialize_structure(revision) for revision in referenced],
        "missing_article_ids": [article_id for article_id in article_ids if article_id not in approved_found],
        "missing_revision_ids": [revision_id for revision_id in revision_ids if revision_id not in referenced_found],
    }


def _packaging_search_filters(query):
    if query is None:
        return []
    columns = (
        ScmPerfilEmpacable.codigo,
        ScmPerfilEmpacable.nombre,
        ScmPerfilEmpacable.descripcion_fisica,
        ScmTipoContenedor.codigo,
        ScmTipoContenedor.nombre,
        ScmTipoContenedor.clase,
    )
    return [
        or_(*(_like_literal(column, term) for column in columns))
        for term in query.split(" ")
    ]


def _packaging_page_item(row):
    return {
        "revision_id": row.revision_id,
        "regla_id": row.rule_id,
        "numero_revision": row.numero_revision,
        "estado": row.estado,
        "version": row.version,
        "medicion_fisica_probada": row.measured,
        "cantidad_objetivo_un": row.target,
        "cantidad_maxima_probada_un": row.tested,
        "peso_neto_operativo_max_kg": _decimal(row.net_max),
        "creada_por_id": row.created_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "perfil_empacable": {
            "id": row.profile_id,
            "codigo": row.profile_code,
            "nombre": row.profile_name,
            "descripcion_fisica": row.profile_description,
            "activo": row.profile_active,
        },
        "tipo_contenedor": {
            "id": row.container_id,
            "codigo": row.container_code,
            "clase": row.container_class,
            "nombre": row.container_name,
            "material": row.container_material,
            "activo": row.container_active,
        },
    }


def list_packaging_rule_page(
    session,
    *,
    actor_id,
    view="actual",
    query=None,
    profile_id=None,
    container_id=None,
    limit=DEFAULT_LIMIT,
    cursor=None,
):
    capability = "EMPAQUE_VER"
    load_actor(session, actor_id, capability=capability)
    limit = _parse_limit(limit, code="INVALID_PACKAGING_RULE_PAGE")
    view = "actual" if view is None else view.strip().lower()
    if view not in {"actual", "historial"}:
        _invalid_read("INVALID_PACKAGING_RULE_PAGE", "vista debe ser actual o historial.")
    query = _parse_query(query, code="INVALID_PACKAGING_RULE_PAGE")
    try:
        profile_id = int(profile_id) if profile_id is not None else None
        container_id = int(container_id) if container_id is not None else None
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_PACKAGING_RULE_PAGE",
            "Los IDs de perfil y contenedor deben ser positivos.",
            status_code=400,
        ) from error
    if profile_id is not None and profile_id <= 0:
        _invalid_read("INVALID_PACKAGING_RULE_PAGE", "perfil_empacable_id debe ser positivo.")
    if container_id is not None and container_id <= 0:
        _invalid_read("INVALID_PACKAGING_RULE_PAGE", "tipo_contenedor_id debe ser positivo.")
    states = ("BORRADOR", "APROBADA") if view == "actual" else ("RETIRADA",)
    filters = [ScmReglaEmpaqueRevision.estado.in_(states)]
    if profile_id is not None:
        filters.append(ScmReglaEmpaque.perfil_empacable_id == profile_id)
    if container_id is not None:
        filters.append(ScmReglaEmpaque.tipo_contenedor_id == container_id)
    filters.extend(_packaging_search_filters(query))
    cursor_filters = {
        "vista": view,
        "q": query,
        "perfil_empacable_id": profile_id,
        "tipo_contenedor_id": container_id,
        "limit": limit,
    }
    last = _page_cursor_decode(
        cursor,
        endpoint=PACKAGING_PAGE_ENDPOINT,
        actor_id=actor_id,
        capability=capability,
        filters=cursor_filters,
        order="perfil_codigo_asc_contenedor_codigo_asc_numero_revision_desc_revision_id_desc",
        code="INVALID_PACKAGING_RULE_PAGE",
        last_shape=(str, str, int, int),
    )
    joins = (
        select(ScmReglaEmpaqueRevision.id)
        .join(ScmReglaEmpaque, ScmReglaEmpaque.id == ScmReglaEmpaqueRevision.regla_id)
        .join(ScmPerfilEmpacable, ScmPerfilEmpacable.id == ScmReglaEmpaque.perfil_empacable_id)
        .join(ScmTipoContenedor, ScmTipoContenedor.id == ScmReglaEmpaque.tipo_contenedor_id)
        .where(*filters)
    )
    total = session.scalar(select(func.count()).select_from(joins.subquery()))
    if last is not None:
        filters.append(
            or_(
                ScmPerfilEmpacable.codigo > last[0],
                and_(
                    ScmPerfilEmpacable.codigo == last[0],
                    ScmTipoContenedor.codigo > last[1],
                ),
                and_(
                    ScmPerfilEmpacable.codigo == last[0],
                    ScmTipoContenedor.codigo == last[1],
                    ScmReglaEmpaqueRevision.numero_revision < last[2],
                ),
                and_(
                    ScmPerfilEmpacable.codigo == last[0],
                    ScmTipoContenedor.codigo == last[1],
                    ScmReglaEmpaqueRevision.numero_revision == last[2],
                    ScmReglaEmpaqueRevision.id < last[3],
                ),
            )
        )
    rows = session.execute(
        select(
            ScmReglaEmpaqueRevision.id.label("revision_id"),
            ScmReglaEmpaqueRevision.regla_id.label("rule_id"),
            ScmReglaEmpaqueRevision.numero_revision,
            ScmReglaEmpaqueRevision.estado,
            ScmReglaEmpaqueRevision.version,
            ScmReglaEmpaqueRevision.medicion_fisica_probada.label("measured"),
            ScmReglaEmpaqueRevision.cantidad_objetivo_un.label("target"),
            ScmReglaEmpaqueRevision.cantidad_maxima_probada_un.label("tested"),
            ScmReglaEmpaqueRevision.peso_neto_operativo_max_kg.label("net_max"),
            ScmReglaEmpaqueRevision.creada_por_id.label("created_by"),
            ScmReglaEmpaqueRevision.created_at,
            ScmReglaEmpaqueRevision.updated_at,
            ScmPerfilEmpacable.id.label("profile_id"),
            ScmPerfilEmpacable.codigo.label("profile_code"),
            ScmPerfilEmpacable.nombre.label("profile_name"),
            ScmPerfilEmpacable.descripcion_fisica.label("profile_description"),
            ScmPerfilEmpacable.activo.label("profile_active"),
            ScmTipoContenedor.id.label("container_id"),
            ScmTipoContenedor.codigo.label("container_code"),
            ScmTipoContenedor.clase.label("container_class"),
            ScmTipoContenedor.nombre.label("container_name"),
            ScmTipoContenedor.material.label("container_material"),
            ScmTipoContenedor.activo.label("container_active"),
        )
        .join(ScmReglaEmpaque, ScmReglaEmpaque.id == ScmReglaEmpaqueRevision.regla_id)
        .join(ScmPerfilEmpacable, ScmPerfilEmpacable.id == ScmReglaEmpaque.perfil_empacable_id)
        .join(ScmTipoContenedor, ScmTipoContenedor.id == ScmReglaEmpaque.tipo_contenedor_id)
        .where(*filters)
        .order_by(
            ScmPerfilEmpacable.codigo,
            ScmTipoContenedor.codigo,
            ScmReglaEmpaqueRevision.numero_revision.desc(),
            ScmReglaEmpaqueRevision.id.desc(),
        )
        .limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_packaging_page_item(row) for row in rows]
    next_cursor = (
        _page_cursor_encode(
            endpoint=PACKAGING_PAGE_ENDPOINT,
            code="INVALID_PACKAGING_RULE_PAGE",
            actor_id=actor_id,
            capability=capability,
            filters=cursor_filters,
            order="perfil_codigo_asc_contenedor_codigo_asc_numero_revision_desc_revision_id_desc",
            last=[
                items[-1]["perfil_empacable"]["codigo"],
                items[-1]["tipo_contenedor"]["codigo"],
                items[-1]["numero_revision"],
                items[-1]["revision_id"],
            ],
        )
        if has_more
        else None
    )
    return {
        "items": items,
        "total": total,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }
