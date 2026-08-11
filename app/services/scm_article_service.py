from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.producto import PiezaColor, ProductoTerminado
from app.models.scm_articulos import (
    CLASE_PIEZA_COLOR,
    CLASE_PRODUCTO_TERMINADO,
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
    ScmArticuloPiezaColor,
    ScmArticuloProducto,
    ScmDefinicionWip,
)
from app.models.scm_auditoria import ScmEvento
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)


@dataclass(frozen=True)
class ArticleSyncResult:
    articulos_creados: int
    subtipos_creados: int


def _article_name(value, fallback):
    normalized = str(value or "").strip()
    return normalized or fallback


def _ensure_article(session, *, code, name, article_class):
    article = session.scalar(
        select(ScmArticulo).where(ScmArticulo.codigo == code)
    )
    created = 0
    if article is None:
        article = ScmArticulo(
            codigo=code,
            nombre=_article_name(name, code),
            clase=article_class,
            unidad_base="UN",
        )
        session.add(article)
        session.flush()
        created = 1
    elif article.clase != article_class:
        raise ScmServiceError(
            "ARTICLE_SUBTYPE_MISMATCH",
            "El codigo ya pertenece a otra clase de articulo.",
            status_code=409,
            details={
                "codigo": code,
                "expected": article_class,
                "actual": article.clase,
            },
        )
    return article, created


def _ensure_piece_article(session, piece):
    article, article_created = _ensure_article(
        session,
        code=piece.sku,
        name=piece.piezas,
        article_class=CLASE_PIEZA_COLOR,
    )
    link = session.scalar(
        select(ScmArticuloPiezaColor).where(
            ScmArticuloPiezaColor.pieza_color_sku == piece.sku
        )
    )
    subtype_created = 0
    if link is None:
        if any((article.definicion_wip, article.producto)):
            raise ScmServiceError(
                "ARTICLE_SUBTYPE_MISMATCH",
                "El articulo ya posee un subtipo incompatible.",
                status_code=409,
            )
        session.add(ScmArticuloPiezaColor(
            articulo=article,
            pieza_color_sku=piece.sku,
        ))
        subtype_created = 1
    elif link.articulo_id != article.id:
        raise ScmServiceError(
            "ARTICLE_SUBTYPE_MISMATCH",
            "La PiezaColor ya esta vinculada a otro articulo.",
            status_code=409,
        )
    return article_created, subtype_created


def _ensure_product_article(session, product):
    article, article_created = _ensure_article(
        session,
        code=product.cod_sku_pt,
        name=product.producto,
        article_class=CLASE_PRODUCTO_TERMINADO,
    )
    link = session.scalar(
        select(ScmArticuloProducto).where(
            ScmArticuloProducto.producto_terminado_id
            == product.cod_sku_pt
        )
    )
    subtype_created = 0
    if link is None:
        if any((article.pieza_color, article.definicion_wip)):
            raise ScmServiceError(
                "ARTICLE_SUBTYPE_MISMATCH",
                "El articulo ya posee un subtipo incompatible.",
                status_code=409,
            )
        session.add(ScmArticuloProducto(
            articulo=article,
            producto_terminado_id=product.cod_sku_pt,
        ))
        subtype_created = 1
    elif link.articulo_id != article.id:
        raise ScmServiceError(
            "ARTICLE_SUBTYPE_MISMATCH",
            "El ProductoTerminado ya esta vinculado a otro articulo.",
            status_code=409,
        )
    return article_created, subtype_created


def sync_catalog_articles(session):
    """Backfill idempotente; no confirma la transaccion."""
    articles_created = 0
    subtypes_created = 0
    for piece in session.scalars(
        select(PiezaColor).order_by(PiezaColor.sku)
    ):
        article_count, subtype_count = _ensure_piece_article(session, piece)
        articles_created += article_count
        subtypes_created += subtype_count
    for product in session.scalars(
        select(ProductoTerminado).order_by(ProductoTerminado.cod_sku_pt)
    ):
        article_count, subtype_count = _ensure_product_article(
            session,
            product,
        )
        articles_created += article_count
        subtypes_created += subtype_count
    session.flush()
    return ArticleSyncResult(articles_created, subtypes_created)


def list_articles(session, *, actor_id, active=None):
    load_actor(session, actor_id, capability="ARTICULO_VER")
    statement = select(ScmArticulo)
    if active is not None:
        statement = statement.where(ScmArticulo.activo.is_(active))
    articles = session.scalars(
        statement.order_by(ScmArticulo.codigo)
    ).all()
    return {"items": [article.to_dict() for article in articles]}


def get_article(session, *, actor_id, article_id):
    load_actor(session, actor_id, capability="ARTICULO_VER")
    article = session.get(ScmArticulo, article_id)
    if article is None:
        raise ScmServiceError(
            "ARTICLE_NOT_FOUND",
            "El articulo no existe.",
            status_code=404,
        )
    return article.to_dict()


def create_wip_article(session, *, actor_id, data, commit=True):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ARTICULO_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={"nombre", "descripcion", "requiere_calidad"},
        )
        name = required_text(
            data.get("nombre"),
            field="nombre",
            max_length=200,
        )
        description_value = data.get("descripcion")
        description = (
            required_text(
                description_value,
                field="descripcion",
                max_length=2000,
            )
            if description_value not in (None, "")
            else None
        )
        requires_quality = data.get("requiere_calidad", False)
        if not isinstance(requires_quality, bool):
            raise ScmServiceError(
                "INVALID_QUALITY_FLAG",
                "requiere_calidad debe ser booleano.",
                status_code=400,
            )

        article = ScmArticulo(
            codigo=generar_codigo_catalogo(
                "SUBENSAMBLE_WIP",
                session=session,
            ),
            nombre=name,
            clase=CLASE_SUBENSAMBLE_WIP,
            unidad_base="UN",
        )
        article.definicion_wip = ScmDefinicionWip(
            descripcion=description,
            requiere_calidad=requires_quality,
        )
        session.add(article)
        session.flush()
        session.add(ScmEvento(
            aggregate_type="SCM_ARTICULO",
            aggregate_id=article.id,
            tipo="ARTICLE_CREATED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=article.to_dict(),
        ))
        if commit:
            session.commit()
        return article.to_dict()
    except ScmServiceError:
        if commit:
            session.rollback()
        raise
    except IntegrityError as error:
        if commit:
            session.rollback()
        raise ScmServiceError(
            "ARTICLE_CONFLICT",
            "El articulo entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_wip_article(session, *, actor_id, article_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ARTICULO_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={
                "version",
                "nombre",
                "descripcion",
                "requiere_calidad",
                "activo",
            },
        )
        article = session.scalar(
            select(ScmArticulo)
            .where(ScmArticulo.id == article_id)
            .with_for_update()
        )
        if article is None:
            raise ScmServiceError(
                "ARTICLE_NOT_FOUND",
                "El articulo no existe.",
                status_code=404,
            )
        if (
            article.clase != CLASE_SUBENSAMBLE_WIP
            or article.definicion_wip is None
        ):
            raise ScmServiceError(
                "ARTICLE_SUBTYPE_MISMATCH",
                "Solo un articulo WIP admite esta operacion.",
                status_code=422,
            )
        received = expected_version(data.get("version"))
        if received != article.version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version del articulo esta desactualizada.",
                status_code=409,
                details={
                    "expected": article.version,
                    "received": received,
                },
            )

        before = article.to_dict()
        if "nombre" in data:
            article.nombre = required_text(
                data.get("nombre"),
                field="nombre",
                max_length=200,
            )
        if "descripcion" in data:
            raw_description = data.get("descripcion")
            article.definicion_wip.descripcion = (
                required_text(
                    raw_description,
                    field="descripcion",
                    max_length=2000,
                )
                if raw_description not in (None, "")
                else None
            )
        if "requiere_calidad" in data:
            requires_quality = data.get("requiere_calidad")
            if not isinstance(requires_quality, bool):
                raise ScmServiceError(
                    "INVALID_QUALITY_FLAG",
                    "requiere_calidad debe ser booleano.",
                    status_code=400,
                )
            article.definicion_wip.requiere_calidad = requires_quality
        if "activo" in data:
            active = data.get("activo")
            if not isinstance(active, bool):
                raise ScmServiceError(
                    "INVALID_ACTIVE_FLAG",
                    "activo debe ser booleano.",
                    status_code=400,
                )
            article.activo = active

        article.version += 1
        session.flush()
        after = article.to_dict()
        event_type = (
            "ARTICLE_DEACTIVATED"
            if before["activo"] and not after["activo"]
            else "ARTICLE_REACTIVATED"
            if not before["activo"] and after["activo"]
            else "ARTICLE_UPDATED"
        )
        session.add(ScmEvento(
            aggregate_type="SCM_ARTICULO",
            aggregate_id=article.id,
            tipo=event_type,
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            before_json=before,
            after_json=after,
        ))
        session.commit()
        return after
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ARTICLE_CONFLICT",
            "No se pudo actualizar el articulo WIP.",
            status_code=409,
        ) from error
