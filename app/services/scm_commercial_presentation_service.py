"""CRUD y reglas de presentaciones comerciales de ProductoTerminado."""

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.models.producto import ProductoTerminado
from app.models.scm_auditoria import ScmEvento
from app.models.scm_commercial import ScmPresentacionComercial
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor_any,
    positive_integer,
    reject_no_changes,
    reject_unknown_fields,
    require_patch_field,
    required_text,
)


MUTABLE_FIELDS = {
    "nombre",
    "unidades_base",
    "codigo_barra",
    "predeterminada",
    "activo",
}


def _load_reader(session, actor_id):
    return load_actor_any(
        session,
        actor_id,
        capabilities=("ARTICULO_VER", "OP_VER"),
    )


def _load_admin(session, actor_id):
    return load_actor_any(
        session,
        actor_id,
        capabilities=("ARTICULO_ADMINISTRAR",),
    )


def _optional_barcode(value):
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip()
    if len(normalized) > 50:
        raise ScmServiceError(
            "BARCODE_TOO_LONG",
            "El codigo de barras supera 50 caracteres.",
            status_code=422,
        )
    return normalized


def _boolean(value, *, field):
    if not isinstance(value, bool):
        raise ScmServiceError(
            "BOOLEAN_REQUIRED",
            f"El campo {field} debe ser booleano.",
            status_code=400,
            details={"field": field},
        )
    return value


def _presentation_event(item, actor, event_type, *, before=None):
    return ScmEvento(
        aggregate_type="SCM_PRESENTACION_COMERCIAL",
        aggregate_id=str(item.id),
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=item.to_dict(),
    )


def _unset_other_defaults(session, product_id, *, exclude_id=None):
    statement = update(ScmPresentacionComercial).where(
        ScmPresentacionComercial.producto_terminado_id == product_id,
        ScmPresentacionComercial.predeterminada.is_(True),
    )
    if exclude_id is not None:
        statement = statement.where(ScmPresentacionComercial.id != exclude_id)
    session.execute(
        statement
        .values(
            predeterminada=False,
            version=ScmPresentacionComercial.version + 1,
            updated_at=func.now(),
        )
    )
    session.flush()


def list_commercial_presentations(
    session,
    *,
    actor_id,
    product_id=None,
    active=None,
):
    _load_reader(session, actor_id)
    statement = select(ScmPresentacionComercial)
    if product_id:
        statement = statement.where(
            ScmPresentacionComercial.producto_terminado_id == product_id
        )
    if active is not None:
        statement = statement.where(ScmPresentacionComercial.activo.is_(active))
    items = session.scalars(
        statement.order_by(
            ScmPresentacionComercial.producto_terminado_id,
            ScmPresentacionComercial.predeterminada.desc(),
            ScmPresentacionComercial.codigo,
        )
    ).all()
    return {"items": [item.to_dict() for item in items]}


def create_commercial_presentation(session, *, actor_id, data):
    try:
        actor = _load_admin(session, actor_id)
        reject_unknown_fields(
            data,
            allowed={
                "producto_terminado_id",
                "nombre",
                "unidades_base",
                "codigo_barra",
                "predeterminada",
            },
        )
        product_id = required_text(
            data.get("producto_terminado_id"),
            field="producto_terminado_id",
            max_length=50,
        )
        product = session.get(ProductoTerminado, product_id)
        if product is None:
            raise ScmServiceError(
                "PRODUCT_NOT_FOUND",
                "El ProductoTerminado no existe.",
                status_code=404,
            )
        name = required_text(data.get("nombre"), field="nombre", max_length=100)
        units = positive_integer(data.get("unidades_base"), field="unidades_base")
        barcode = _optional_barcode(data.get("codigo_barra"))
        requested_default = data.get("predeterminada", False)
        if not isinstance(requested_default, bool):
            _boolean(requested_default, field="predeterminada")
        has_default = session.scalar(
            select(ScmPresentacionComercial.id).where(
                ScmPresentacionComercial.producto_terminado_id == product_id,
                ScmPresentacionComercial.activo.is_(True),
                ScmPresentacionComercial.predeterminada.is_(True),
            )
        ) is not None
        item = ScmPresentacionComercial(
            codigo=generar_codigo_catalogo(
                "PRESENTACION_COMERCIAL",
                session=session,
            ),
            producto_terminado_id=product_id,
            nombre=name,
            unidades_base=units,
            codigo_barra=barcode,
            predeterminada=requested_default or not has_default,
        )
        if item.predeterminada:
            _unset_other_defaults(session, product_id)
        session.add(item)
        session.flush()
        session.add(_presentation_event(item, actor, "PRESENTACION_COMERCIAL_CREADA"))
        session.commit()
        return item.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "COMMERCIAL_PRESENTATION_CONFLICT",
            "La presentacion, su nombre o codigo de barras ya existe.",
            status_code=409,
        ) from error


def update_commercial_presentation(
    session,
    *,
    actor_id,
    presentation_id,
    data,
):
    try:
        actor = _load_admin(session, actor_id)
        reject_unknown_fields(data, allowed=MUTABLE_FIELDS | {"codigo", "version"})
        require_patch_field(data, mutable=MUTABLE_FIELDS | {"codigo"})
        item = session.scalar(
            select(ScmPresentacionComercial)
            .where(ScmPresentacionComercial.id == presentation_id)
            .with_for_update()
        )
        if item is None:
            raise ScmServiceError(
                "COMMERCIAL_PRESENTATION_NOT_FOUND",
                "La presentacion comercial no existe.",
                status_code=404,
            )
        received_version = expected_version(data.get("version"))
        if received_version != item.version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version de la presentacion esta desactualizada.",
                status_code=409,
                details={"expected": item.version, "received": received_version},
            )
        if "codigo" in data and str(data["codigo"]).strip().upper() != item.codigo:
            raise ScmServiceError(
                "IMMUTABLE_PRESENTATION_CODE",
                "El codigo de la presentacion no puede modificarse.",
                status_code=422,
            )
        before = item.to_dict()
        name = (
            required_text(data["nombre"], field="nombre", max_length=100)
            if "nombre" in data else item.nombre
        )
        units = (
            positive_integer(data["unidades_base"], field="unidades_base")
            if "unidades_base" in data else item.unidades_base
        )
        barcode = (
            _optional_barcode(data["codigo_barra"])
            if "codigo_barra" in data else item.codigo_barra
        )
        active = (
            _boolean(data["activo"], field="activo")
            if "activo" in data else item.activo
        )
        default = (
            _boolean(data["predeterminada"], field="predeterminada")
            if "predeterminada" in data else item.predeterminada
        )
        if item.predeterminada and (not active or not default):
            raise ScmServiceError(
                "DEFAULT_PRESENTATION_REQUIRED",
                "Asigna primero otra presentacion predeterminada.",
                status_code=409,
            )
        if default and not active:
            raise ScmServiceError(
                "DEFAULT_PRESENTATION_MUST_BE_ACTIVE",
                "La presentacion predeterminada debe estar activa.",
                status_code=422,
            )
        if all((
            name == item.nombre,
            units == item.unidades_base,
            barcode == item.codigo_barra,
            active == item.activo,
            default == item.predeterminada,
        )):
            reject_no_changes()
        if default:
            _unset_other_defaults(
                session,
                item.producto_terminado_id,
                exclude_id=item.id,
            )
        item.nombre = name
        item.unidades_base = units
        item.codigo_barra = barcode
        item.activo = active
        item.predeterminada = default
        item.version += 1
        session.flush()
        session.add(_presentation_event(
            item,
            actor,
            "PRESENTACION_COMERCIAL_ACTUALIZADA",
            before=before,
        ))
        session.commit()
        return item.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "COMMERCIAL_PRESENTATION_CONFLICT",
            "La presentacion, su nombre o codigo de barras ya existe.",
            status_code=409,
        ) from error
