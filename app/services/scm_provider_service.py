import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import ScmProveedor
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    load_actor_any,
    reject_no_changes,
    reject_unknown_fields,
    require_patch_field,
    required_text,
    stable_code,
)


_RUC_SEPARATORS = re.compile(r"[\s.\-]")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PROVIDER_CONTACT_FIELDS = {"contacto", "telefono", "whatsapp", "correo"}
PROVIDER_MUTABLE_FIELDS = {
    "razon_social",
    "ruc",
    *PROVIDER_CONTACT_FIELDS,
    "activo",
}


def _active_value(data, *, default):
    if "activo" not in data:
        return default
    value = data["activo"]
    if not isinstance(value, bool):
        raise ScmServiceError(
            "INVALID_ACTIVE_FLAG",
            "El campo activo debe ser booleano.",
            status_code=400,
        )
    return value


def _normalized_ruc(value):
    if value is None or str(value).strip() == "":
        return None
    normalized = _RUC_SEPARATORS.sub("", str(value).strip())
    if len(normalized) != 11 or not normalized.isdigit():
        raise ScmServiceError(
            "INVALID_RUC",
            "El RUC debe normalizarse a exactamente 11 digitos.",
            status_code=422,
        )
    return normalized


def _optional_text(value, *, field, max_length):
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip()
    if len(normalized) > max_length:
        raise ScmServiceError(
            "INVALID_FIELD_LENGTH",
            f"El campo {field} excede {max_length} caracteres.",
            status_code=422,
        )
    return normalized


def _normalized_email(value):
    normalized = _optional_text(value, field="correo", max_length=254)
    if normalized is None:
        return None
    normalized = normalized.lower()
    if not _EMAIL_PATTERN.fullmatch(normalized):
        raise ScmServiceError(
            "INVALID_EMAIL",
            "El correo del proveedor no tiene un formato valido.",
            status_code=422,
        )
    return normalized


def _contact_values(data, *, current=None):
    values = {}
    limits = {"contacto": 200, "telefono": 50, "whatsapp": 50}
    for field, max_length in limits.items():
        values[field] = (
            _optional_text(data[field], field=field, max_length=max_length)
            if field in data
            else getattr(current, field, None)
        )
    values["correo"] = (
        _normalized_email(data["correo"])
        if "correo" in data
        else getattr(current, "correo", None)
    )
    return values


def _provider_event(provider, actor, event_type, *, before=None):
    return ScmEvento(
        aggregate_type="SCM_PROVEEDOR",
        aggregate_id=provider.id,
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=provider.to_dict(),
    )


def list_providers(session, *, actor_id, active=None):
    load_actor(session, actor_id)
    statement = select(ScmProveedor)
    if active is not None:
        statement = statement.where(ScmProveedor.activo.is_(active))
    providers = session.scalars(
        statement.order_by(ScmProveedor.codigo)
    ).all()
    return {"items": [item.to_dict() for item in providers]}


def get_provider(session, *, actor_id, provider_id):
    load_actor(session, actor_id)
    provider = session.get(ScmProveedor, provider_id)
    if provider is None:
        raise ScmServiceError(
            "PROVIDER_NOT_FOUND",
            "El proveedor no existe.",
            status_code=404,
        )
    return provider.to_dict()


def create_provider(session, *, actor_id, data):
    try:
        actor = load_actor_any(
            session,
            actor_id,
            capabilities=(
                "PROVEEDOR_ADMINISTRAR",
                "CATALOGO_PROVEEDOR_ADMINISTRAR",
            ),
        )
        reject_unknown_fields(
            data,
            allowed={
                "codigo",
                "razon_social",
                "ruc",
                *PROVIDER_CONTACT_FIELDS,
                "activo",
            },
        )
        code = (
            stable_code(data.get("codigo"))
            if data.get("codigo")
            else generar_codigo_catalogo("PROVEEDOR", session=session)
        )
        business_name = required_text(
            data.get("razon_social"),
            field="razon_social",
            max_length=200,
        )
        ruc = _normalized_ruc(data.get("ruc"))
        contacts = _contact_values(data)

        if session.scalar(
            select(ScmProveedor.id).where(ScmProveedor.codigo == code)
        ) is not None:
            raise ScmServiceError(
                "PROVIDER_CODE_CONFLICT",
                "El codigo del proveedor ya existe.",
                status_code=409,
            )
        if ruc and session.scalar(
            select(ScmProveedor.id).where(ScmProveedor.ruc == ruc)
        ) is not None:
            raise ScmServiceError(
                "PROVIDER_RUC_CONFLICT",
                "El RUC del proveedor ya existe.",
                status_code=409,
            )

        provider = ScmProveedor(
            codigo=code,
            razon_social=business_name,
            ruc=ruc,
            **contacts,
            activo=_active_value(data, default=True),
        )
        session.add(provider)
        session.flush()
        session.add(_provider_event(provider, actor, "PROVEEDOR_CREADO"))
        session.commit()
        return provider.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PROVIDER_CONFLICT",
            "El proveedor entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_provider(session, *, actor_id, provider_id, data):
    try:
        actor = load_actor_any(
            session,
            actor_id,
            capabilities=(
                "PROVEEDOR_ADMINISTRAR",
                "CATALOGO_PROVEEDOR_ADMINISTRAR",
            ),
        )
        reject_unknown_fields(
            data,
            allowed=PROVIDER_MUTABLE_FIELDS | {"codigo", "version"},
        )
        provider = session.scalar(
            select(ScmProveedor)
            .where(ScmProveedor.id == provider_id)
            .with_for_update()
        )
        if provider is None:
            raise ScmServiceError(
                "PROVIDER_NOT_FOUND",
                "El proveedor no existe.",
                status_code=404,
            )

        received_version = expected_version(data.get("version"))
        if provider.version != received_version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version del proveedor esta desactualizada.",
                status_code=409,
                details={
                    "expected": provider.version,
                    "received": received_version,
                },
            )

        if "codigo" in data and stable_code(data["codigo"]) != provider.codigo:
            raise ScmServiceError(
                "IMMUTABLE_PROVIDER_CODE",
                "El codigo estable del proveedor no puede modificarse.",
                status_code=422,
            )
        require_patch_field(
            data,
            mutable=PROVIDER_MUTABLE_FIELDS | {"codigo"},
        )

        before = provider.to_dict()
        business_name = (
            required_text(
                data["razon_social"],
                field="razon_social",
                max_length=200,
            )
            if "razon_social" in data
            else provider.razon_social
        )
        ruc = (
            _normalized_ruc(data["ruc"])
            if "ruc" in data
            else provider.ruc
        )
        contacts = _contact_values(data, current=provider)
        if ruc != provider.ruc:
            conflict = (
                session.scalar(
                    select(ScmProveedor.id).where(
                        ScmProveedor.ruc == ruc,
                        ScmProveedor.id != provider.id,
                    )
                )
                if ruc is not None
                else None
            )
            if conflict is not None:
                raise ScmServiceError(
                    "PROVIDER_RUC_CONFLICT",
                    "El RUC del proveedor ya existe.",
                    status_code=409,
                )
        active = _active_value(data, default=provider.activo)
        if not any((
            business_name != provider.razon_social,
            ruc != provider.ruc,
            *(contacts[field] != getattr(provider, field) for field in PROVIDER_CONTACT_FIELDS),
            active != provider.activo,
        )):
            reject_no_changes()

        provider.razon_social = business_name
        provider.ruc = ruc
        for field, value in contacts.items():
            setattr(provider, field, value)
        provider.activo = active

        provider.version += 1
        session.flush()
        session.add(
            _provider_event(
                provider,
                actor,
                "PROVEEDOR_ACTUALIZADO",
                before=before,
            )
        )
        session.commit()
        return provider.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PROVIDER_CONFLICT",
            "El proveedor entra en conflicto con otro registro.",
            status_code=409,
        ) from error
