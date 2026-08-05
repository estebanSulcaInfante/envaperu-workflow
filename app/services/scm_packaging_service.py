import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.scm_articulos import ScmArticulo
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_empaque import (
    CLASES_CONTENEDOR,
    ESTADO_REGLA_APROBADA,
    ESTADO_REGLA_BORRADOR,
    ESTADO_REGLA_RETIRADA,
    ScmArticuloPerfil,
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
    utc_now,
)
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    positive_integer,
    reject_unknown_fields,
    required_text,
)


PHYSICAL_QUANTUM = Decimal("0.001")
PERCENT_QUANTUM = Decimal("0.0001")
PHYSICAL_MAX = Decimal("999999999.999")


def _optional_text(value, *, field, max_length):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScmServiceError(
            "TEXT_FIELD_REQUIRED",
            f"El campo {field} debe ser texto.",
            status_code=400,
            details={"field": field},
        )
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ScmServiceError(
            "FIELD_TOO_LONG",
            f"El campo {field} supera la longitud permitida.",
            status_code=400,
            details={"field": field, "max_length": max_length},
        )
    return normalized or None


def _choice(value, *, field, allowed):
    normalized = (
        value.strip().upper()
        if isinstance(value, str)
        else None
    )
    if normalized not in allowed:
        raise ScmServiceError(
            "INVALID_CHOICE",
            f"El campo {field} contiene un valor invalido.",
            status_code=422,
            details={"field": field, "allowed": list(allowed)},
        )
    return normalized


def _boolean(value, *, field):
    if not isinstance(value, bool):
        raise ScmServiceError(
            "BOOLEAN_FIELD_REQUIRED",
            f"El campo {field} debe ser booleano.",
            status_code=400,
            details={"field": field},
        )
    return value


def _decimal(
    value,
    *,
    field,
    quantum=PHYSICAL_QUANTUM,
    positive=False,
):
    try:
        parsed = Decimal(str(value))
        quantized = parsed.quantize(quantum)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_DECIMAL",
            f"El campo {field} no es un decimal valido.",
            status_code=422,
            details={"field": field},
        ) from error
    invalid_sign = quantized <= 0 if positive else quantized < 0
    if (
        not parsed.is_finite()
        or parsed != quantized
        or invalid_sign
        or quantized > PHYSICAL_MAX
    ):
        raise ScmServiceError(
            "INVALID_DECIMAL",
            f"El campo {field} esta fuera del rango permitido.",
            status_code=422,
            details={"field": field},
        )
    return quantized


def _percentage(value, *, field):
    parsed = _decimal(
        value,
        field=field,
        quantum=PERCENT_QUANTUM,
    )
    if parsed >= 100:
        raise ScmServiceError(
            "INVALID_DECIMAL",
            f"El campo {field} debe ser menor que 100.",
            status_code=422,
            details={"field": field},
        )
    return parsed


def _request_hash(*, endpoint, actor_id, payload):
    raw = json.dumps(
        {
            "endpoint": endpoint,
            "actor_id": actor_id,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _reserve_operation(
    session,
    *,
    operation_id,
    endpoint,
    actor,
    payload,
):
    request_hash = _request_hash(
        endpoint=endpoint,
        actor_id=actor.id,
        payload=payload,
    )
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        if (
            existing.endpoint != endpoint
            or existing.request_sha256 != request_hash
        ):
            raise ScmServiceError(
                "IDEMPOTENCY_CONFLICT",
                "La clave idempotente ya fue usada con otra solicitud.",
                status_code=409,
            )
        if existing.estado_http is None or existing.response_json is None:
            raise ScmServiceError(
                "IDEMPOTENCY_OPERATION_INCOMPLETE",
                "La operacion no tiene un resultado reutilizable.",
                status_code=409,
            )
        return None, copy.deepcopy(existing.response_json)
    operation = ScmOperacion(
        operation_id=operation_id,
        endpoint=endpoint,
        actor_id=actor.id,
        request_sha256=request_hash,
    )
    session.add(operation)
    session.flush()
    return operation, None


def _event(
    aggregate_type,
    aggregate_id,
    actor,
    event_type,
    *,
    before=None,
    after=None,
    operation=None,
):
    return ScmEvento(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=after,
        operation_id=operation.operation_id if operation else None,
    )


def _serialize_rule(revision):
    payload = revision.to_dict()
    rule = revision.regla
    payload["perfil_empacable_id"] = rule.perfil_empacable_id
    payload["tipo_contenedor_id"] = rule.tipo_contenedor_id
    payload["perfil_empacable"] = rule.perfil.to_dict()
    payload["tipo_contenedor"] = rule.tipo_contenedor.to_dict()
    return payload


def calculate_packaging_capacity(
    *,
    tara_nominal_g,
    tolerancia_tara_g,
    peso_bruto_max_kg,
    peso_neto_operativo_max_kg,
    margen_seguridad_kg,
    cantidad_objetivo_un,
    cantidad_maxima_probada_un,
    peso_unitario_snapshot_g,
):
    values = (
        tara_nominal_g,
        tolerancia_tara_g,
        peso_bruto_max_kg,
        peso_neto_operativo_max_kg,
        margen_seguridad_kg,
        peso_unitario_snapshot_g,
    )
    if any(not isinstance(value, Decimal) for value in values):
        raise TypeError("La calculadora pura requiere Decimal.")
    if (
        tara_nominal_g < 0
        or tolerancia_tara_g < 0
        or peso_bruto_max_kg <= 0
        or peso_neto_operativo_max_kg <= 0
        or margen_seguridad_kg < 0
        or peso_unitario_snapshot_g <= 0
        or cantidad_objetivo_un <= 0
        or cantidad_maxima_probada_un <= 0
        or cantidad_objetivo_un > cantidad_maxima_probada_un
    ):
        raise ScmServiceError(
            "PACKAGING_RULE_NOT_VIABLE",
            "La regla no conserva limites fisicos positivos.",
            status_code=422,
        )
    tara_superior_kg = (
        tara_nominal_g + tolerancia_tara_g
    ) / Decimal("1000")
    limite_neto_por_bruto_kg = (
        peso_bruto_max_kg
        - tara_superior_kg
        - margen_seguridad_kg
    )
    limite_neto_efectivo_kg = min(
        peso_neto_operativo_max_kg,
        limite_neto_por_bruto_kg,
    )
    if limite_neto_efectivo_kg <= 0:
        raise ScmServiceError(
            "PACKAGING_RULE_NOT_VIABLE",
            "La tara y el margen consumen el limite del contenedor.",
            status_code=422,
        )
    capacidad_por_peso = int((
        limite_neto_efectivo_kg
        * Decimal("1000")
        / peso_unitario_snapshot_g
    ).to_integral_value(rounding=ROUND_FLOOR))
    capacidad_efectiva = min(
        cantidad_objetivo_un,
        cantidad_maxima_probada_un,
        capacidad_por_peso,
    )
    if capacidad_por_peso <= 0 or capacidad_efectiva <= 0:
        raise ScmServiceError(
            "PACKAGING_RULE_NOT_VIABLE",
            "El peso unitario no permite una unidad por contenedor.",
            status_code=422,
        )
    return {
        "tara_superior_kg": tara_superior_kg,
        "limite_neto_por_bruto_kg": limite_neto_por_bruto_kg,
        "limite_neto_efectivo_kg": limite_neto_efectivo_kg,
        "capacidad_por_peso_un": capacidad_por_peso,
        "capacidad_efectiva_un": capacidad_efectiva,
    }


def list_container_types(session, *, actor_id, active=None):
    load_actor(session, actor_id, capability="EMPAQUE_VER")
    statement = select(ScmTipoContenedor)
    if active is not None:
        statement = statement.where(ScmTipoContenedor.activo == active)
    items = session.scalars(
        statement.order_by(ScmTipoContenedor.codigo)
    ).all()
    return {"items": [item.to_dict() for item in items]}


def create_container_type(
    session, *, actor_id, data, capability="EMPAQUE_ADMINISTRAR"
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability=capability,
        )
        reject_unknown_fields(
            data,
            allowed={
                "clase",
                "nombre",
                "material",
                "dimensiones",
                "tara_nominal_g",
                "tolerancia_tara_g",
                "peso_bruto_max_kg",
            },
        )
        container_class = _choice(
            data.get("clase"),
            field="clase",
            allowed=CLASES_CONTENEDOR,
        )
        dimensions = data.get("dimensiones")
        if dimensions is not None and not isinstance(dimensions, dict):
            raise ScmServiceError(
                "JSON_OBJECT_REQUIRED",
                "dimensiones debe ser un objeto JSON.",
                status_code=400,
            )
        code_key = (
            "TIPO_MANGA"
            if container_class == "MANGA"
            else "TIPO_CONTENEDOR"
        )
        container = ScmTipoContenedor(
            codigo=generar_codigo_catalogo(code_key, session=session),
            clase=container_class,
            nombre=required_text(
                data.get("nombre"),
                field="nombre",
                max_length=160,
            ),
            material=_optional_text(
                data.get("material"),
                field="material",
                max_length=120,
            ),
            dimensiones_json=copy.deepcopy(dimensions),
            tara_nominal_g=_decimal(
                data.get("tara_nominal_g", 0),
                field="tara_nominal_g",
            ),
            tolerancia_tara_g=_decimal(
                data.get("tolerancia_tara_g", 0),
                field="tolerancia_tara_g",
            ),
            peso_bruto_max_kg=_decimal(
                data.get("peso_bruto_max_kg", 0),
                field="peso_bruto_max_kg",
            ),
        )
        session.add(container)
        session.flush()
        session.add(_event(
            "SCM_TIPO_CONTENEDOR",
            container.id,
            actor,
            "CONTAINER_TYPE_CREATED",
            after=container.to_dict(),
        ))
        session.commit()
        return container.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "CONTAINER_TYPE_CONFLICT",
            "El tipo de contenedor entra en conflicto.",
            status_code=409,
        ) from error


def update_container_type(
    session, *, actor_id, container_id, data,
    capability="EMPAQUE_ADMINISTRAR",
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability=capability,
        )
        reject_unknown_fields(
            data,
            allowed={
                "version",
                "nombre",
                "material",
                "dimensiones",
                "tara_nominal_g",
                "tolerancia_tara_g",
                "peso_bruto_max_kg",
                "activo",
            },
        )
        container = session.scalar(
            select(ScmTipoContenedor)
            .where(ScmTipoContenedor.id == container_id)
            .with_for_update()
        )
        if container is None:
            raise ScmServiceError(
                "CONTAINER_TYPE_NOT_FOUND",
                "El tipo de contenedor no existe.",
                status_code=404,
            )
        received = expected_version(data.get("version"))
        if received != container.version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version del contenedor esta desactualizada.",
                status_code=409,
            )
        before = container.to_dict()
        if "nombre" in data:
            container.nombre = required_text(
                data.get("nombre"),
                field="nombre",
                max_length=160,
            )
        if "material" in data:
            container.material = _optional_text(
                data.get("material"),
                field="material",
                max_length=120,
            )
        if "dimensiones" in data:
            if (
                data["dimensiones"] is not None
                and not isinstance(data["dimensiones"], dict)
            ):
                raise ScmServiceError(
                    "JSON_OBJECT_REQUIRED",
                    "dimensiones debe ser un objeto JSON.",
                    status_code=400,
                )
            container.dimensiones_json = copy.deepcopy(
                data["dimensiones"]
            )
        for field in (
            "tara_nominal_g",
            "tolerancia_tara_g",
            "peso_bruto_max_kg",
        ):
            if field in data:
                setattr(
                    container,
                    field,
                    _decimal(data[field], field=field),
                )
        if "activo" in data:
            container.activo = _boolean(
                data.get("activo"),
                field="activo",
            )
        container.version += 1
        session.flush()
        session.add(_event(
            "SCM_TIPO_CONTENEDOR",
            container.id,
            actor,
            "CONTAINER_TYPE_UPDATED",
            before=before,
            after=container.to_dict(),
        ))
        session.commit()
        return container.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "CONTAINER_TYPE_CONFLICT",
            "No se pudo actualizar el tipo de contenedor.",
            status_code=409,
        ) from error


def deactivate_container_type(
    session, *, actor_id, container_id, data,
    capability="EMPAQUE_ADMINISTRAR",
):
    reject_unknown_fields(data, allowed={"version"})
    return update_container_type(
        session,
        actor_id=actor_id,
        container_id=container_id,
        data={"version": data.get("version"), "activo": False},
        capability=capability,
    )


def list_packable_profiles(session, *, actor_id, active=None):
    load_actor(session, actor_id, capability="EMPAQUE_VER")
    statement = select(ScmPerfilEmpacable)
    if active is not None:
        statement = statement.where(ScmPerfilEmpacable.activo == active)
    items = session.scalars(
        statement.order_by(ScmPerfilEmpacable.codigo)
    ).all()
    return {"items": [item.to_dict() for item in items]}


def create_packable_profile(session, *, actor_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="EMPAQUE_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={"nombre", "descripcion_fisica"},
        )
        profile = ScmPerfilEmpacable(
            codigo=generar_codigo_catalogo(
                "PERFIL_EMPAQUE",
                session=session,
            ),
            nombre=required_text(
                data.get("nombre"),
                field="nombre",
                max_length=160,
            ),
            descripcion_fisica=_optional_text(
                data.get("descripcion_fisica"),
                field="descripcion_fisica",
                max_length=4000,
            ),
        )
        session.add(profile)
        session.flush()
        session.add(_event(
            "SCM_PERFIL_EMPAQUE",
            profile.id,
            actor,
            "PACKABLE_PROFILE_CREATED",
            after=profile.to_dict(),
        ))
        session.commit()
        return profile.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PACKABLE_PROFILE_CONFLICT",
            "El perfil empacable entra en conflicto.",
            status_code=409,
        ) from error


def update_packable_profile(session, *, actor_id, profile_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="EMPAQUE_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={
                "version",
                "nombre",
                "descripcion_fisica",
                "activo",
            },
        )
        profile = session.scalar(
            select(ScmPerfilEmpacable)
            .where(ScmPerfilEmpacable.id == profile_id)
            .with_for_update()
        )
        if profile is None:
            raise ScmServiceError(
                "PACKABLE_PROFILE_NOT_FOUND",
                "El perfil empacable no existe.",
                status_code=404,
            )
        received = expected_version(data.get("version"))
        if received != profile.version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version del perfil esta desactualizada.",
                status_code=409,
            )
        before = profile.to_dict()
        if "nombre" in data:
            profile.nombre = required_text(
                data.get("nombre"),
                field="nombre",
                max_length=160,
            )
        if "descripcion_fisica" in data:
            profile.descripcion_fisica = _optional_text(
                data.get("descripcion_fisica"),
                field="descripcion_fisica",
                max_length=4000,
            )
        if "activo" in data:
            profile.activo = _boolean(
                data.get("activo"),
                field="activo",
            )
        profile.version += 1
        session.flush()
        session.add(_event(
            "SCM_PERFIL_EMPAQUE",
            profile.id,
            actor,
            "PACKABLE_PROFILE_UPDATED",
            before=before,
            after=profile.to_dict(),
        ))
        session.commit()
        return profile.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PACKABLE_PROFILE_CONFLICT",
            "No se pudo actualizar el perfil.",
            status_code=409,
        ) from error


def deactivate_packable_profile(session, *, actor_id, profile_id, data):
    reject_unknown_fields(data, allowed={"version"})
    return update_packable_profile(
        session,
        actor_id=actor_id,
        profile_id=profile_id,
        data={"version": data.get("version"), "activo": False},
    )


def assign_article_profiles(
    session,
    *,
    actor_id,
    article_id,
    data,
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="EMPAQUE_ADMINISTRAR",
        )
        reject_unknown_fields(data, allowed={"version", "perfiles"})
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
        received = expected_version(data.get("version"))
        if received != article.version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version del articulo esta desactualizada.",
                status_code=409,
            )
        raw_profiles = data.get("perfiles")
        if not isinstance(raw_profiles, list):
            raise ScmServiceError(
                "PROFILES_LIST_REQUIRED",
                "perfiles debe ser una lista.",
                status_code=400,
            )
        rows = []
        ids = []
        defaults = 0
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                raise ScmServiceError(
                    "INVALID_ARTICLE_PROFILE",
                    "Cada perfil debe ser un objeto JSON.",
                    status_code=400,
                )
            reject_unknown_fields(
                raw,
                allowed={
                    "perfil_empacable_id",
                    "es_predeterminado",
                    "activo",
                },
            )
            profile_id = positive_integer(
                raw.get("perfil_empacable_id"),
                field="perfil_empacable_id",
            )
            profile = session.get(ScmPerfilEmpacable, profile_id)
            if profile is None or not profile.activo:
                raise ScmServiceError(
                    "PACKABLE_PROFILE_NOT_FOUND",
                    "El perfil no existe o esta inactivo.",
                    status_code=422,
                )
            active = raw.get("activo", True)
            active = _boolean(active, field="activo")
            is_default = _boolean(
                raw.get("es_predeterminado", False),
                field="es_predeterminado",
            )
            if active and is_default:
                defaults += 1
            ids.append(profile_id)
            rows.append(ScmArticuloPerfil(
                articulo_id=article.id,
                perfil_empacable_id=profile_id,
                activo=active,
                es_predeterminado=is_default,
            ))
        if len(ids) != len(set(ids)):
            raise ScmServiceError(
                "DUPLICATE_ARTICLE_PROFILE",
                "No se puede repetir un perfil.",
                status_code=422,
            )
        if defaults > 1:
            raise ScmServiceError(
                "MULTIPLE_DEFAULT_PACKAGING_PROFILES",
                "Solo un perfil activo puede ser predeterminado.",
                status_code=422,
            )
        existing = session.scalars(
            select(ScmArticuloPerfil).where(
                ScmArticuloPerfil.articulo_id == article.id
            )
        ).all()
        for row in existing:
            session.delete(row)
        session.flush()
        session.add_all(rows)
        article.version += 1
        session.flush()
        response = {
            "articulo_id": article.id,
            "version": article.version,
            "perfiles": [row.to_dict() for row in rows],
        }
        session.add(_event(
            "SCM_ARTICULO",
            article.id,
            actor,
            "ARTICLE_PACKAGING_PROFILES_ASSIGNED",
            after=response,
        ))
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ARTICLE_PROFILE_CONFLICT",
            "No se pudieron asignar los perfiles.",
            status_code=409,
        ) from error


def get_article_profiles(session, *, actor_id, article_id):
    load_actor(session, actor_id, capability="EMPAQUE_VER")
    if session.get(ScmArticulo, article_id) is None:
        raise ScmServiceError(
            "ARTICLE_NOT_FOUND",
            "El articulo no existe.",
            status_code=404,
        )
    rows = session.scalars(
        select(ScmArticuloPerfil)
        .where(ScmArticuloPerfil.articulo_id == article_id)
        .order_by(ScmArticuloPerfil.id)
    ).all()
    return {
        "articulo_id": article_id,
        "perfiles": [row.to_dict() for row in rows],
    }


def _rule_values(data):
    target = positive_integer(
        data.get("cantidad_objetivo_un"),
        field="cantidad_objetivo_un",
    )
    tested = positive_integer(
        data.get("cantidad_maxima_probada_un"),
        field="cantidad_maxima_probada_un",
    )
    if target > tested:
        raise ScmServiceError(
            "PACKAGING_RULE_NOT_VIABLE",
            "El objetivo no puede superar el maximo probado.",
            status_code=422,
        )
    return {
        "medicion_fisica_probada": _boolean(
            data.get("medicion_fisica_probada"),
            field="medicion_fisica_probada",
        ),
        "cantidad_objetivo_un": target,
        "cantidad_maxima_probada_un": tested,
        "peso_neto_operativo_max_kg": _decimal(
            data.get("peso_neto_operativo_max_kg"),
            field="peso_neto_operativo_max_kg",
            positive=True,
        ),
        "margen_seguridad_kg": _decimal(
            data.get("margen_seguridad_kg", 0),
            field="margen_seguridad_kg",
        ),
        "tolerancia_peso_abs_g": _decimal(
            data.get("tolerancia_peso_abs_g", 0),
            field="tolerancia_peso_abs_g",
        ),
        "tolerancia_peso_pct": _percentage(
            data.get("tolerancia_peso_pct", 0),
            field="tolerancia_peso_pct",
        ),
        "notas": _optional_text(
            data.get("notas"),
            field="notas",
            max_length=4000,
        ),
    }


def list_packaging_rules(
    session,
    *,
    actor_id,
    profile_id=None,
    container_id=None,
):
    load_actor(session, actor_id, capability="EMPAQUE_VER")
    statement = select(ScmReglaEmpaqueRevision).join(
        ScmReglaEmpaque
    )
    if profile_id is not None:
        statement = statement.where(
            ScmReglaEmpaque.perfil_empacable_id
            == positive_integer(profile_id, field="perfil_empacable_id")
        )
    if container_id is not None:
        statement = statement.where(
            ScmReglaEmpaque.tipo_contenedor_id
            == positive_integer(container_id, field="tipo_contenedor_id")
        )
    items = session.scalars(
        statement.order_by(
            ScmReglaEmpaqueRevision.regla_id,
            ScmReglaEmpaqueRevision.numero_revision.desc(),
        )
    ).all()
    return {"items": [_serialize_rule(item) for item in items]}


def get_packaging_rule(session, *, actor_id, revision_id):
    load_actor(session, actor_id, capability="EMPAQUE_VER")
    revision = session.get(ScmReglaEmpaqueRevision, revision_id)
    if revision is None:
        raise ScmServiceError(
            "PACKAGING_RULE_NOT_FOUND",
            "La revision de regla no existe.",
            status_code=404,
        )
    return _serialize_rule(revision)


def create_packaging_rule(session, *, actor_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="EMPAQUE_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={
                "perfil_empacable_id",
                "tipo_contenedor_id",
                "medicion_fisica_probada",
                "cantidad_objetivo_un",
                "cantidad_maxima_probada_un",
                "peso_neto_operativo_max_kg",
                "margen_seguridad_kg",
                "tolerancia_peso_abs_g",
                "tolerancia_peso_pct",
                "notas",
            },
        )
        profile_id = positive_integer(
            data.get("perfil_empacable_id"),
            field="perfil_empacable_id",
        )
        container_id = positive_integer(
            data.get("tipo_contenedor_id"),
            field="tipo_contenedor_id",
        )
        if session.get(ScmPerfilEmpacable, profile_id) is None:
            raise ScmServiceError(
                "PACKABLE_PROFILE_NOT_FOUND",
                "El perfil empacable no existe.",
                status_code=404,
            )
        if session.get(ScmTipoContenedor, container_id) is None:
            raise ScmServiceError(
                "CONTAINER_TYPE_NOT_FOUND",
                "El tipo de contenedor no existe.",
                status_code=404,
            )
        rule = session.scalar(
            select(ScmReglaEmpaque)
            .where(
                ScmReglaEmpaque.perfil_empacable_id == profile_id,
                ScmReglaEmpaque.tipo_contenedor_id == container_id,
            )
            .with_for_update()
        )
        if rule is None:
            rule = ScmReglaEmpaque(
                perfil_empacable_id=profile_id,
                tipo_contenedor_id=container_id,
            )
            session.add(rule)
            session.flush()
        if any(
            item.estado == ESTADO_REGLA_BORRADOR
            for item in rule.revisiones
        ):
            raise ScmServiceError(
                "PACKAGING_RULE_OPEN_REVISION_EXISTS",
                "La combinacion ya tiene un borrador.",
                status_code=409,
            )
        last_number = max(
            (item.numero_revision for item in rule.revisiones),
            default=0,
        )
        revision = ScmReglaEmpaqueRevision(
            regla=rule,
            numero_revision=last_number + 1,
            estado=ESTADO_REGLA_BORRADOR,
            creada_por_id=actor.id,
            **_rule_values(data),
        )
        session.add(revision)
        session.flush()
        response = _serialize_rule(revision)
        session.add(_event(
            "SCM_REGLA_EMPAQUE",
            revision.id,
            actor,
            "PACKAGING_RULE_CREATED",
            after=response,
        ))
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PACKAGING_RULE_CONFLICT",
            "La regla entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_packaging_rule(
    session,
    *,
    actor_id,
    revision_id,
    data,
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="EMPAQUE_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={
                "version",
                "medicion_fisica_probada",
                "cantidad_objetivo_un",
                "cantidad_maxima_probada_un",
                "peso_neto_operativo_max_kg",
                "margen_seguridad_kg",
                "tolerancia_peso_abs_g",
                "tolerancia_peso_pct",
                "notas",
            },
        )
        revision = session.scalar(
            select(ScmReglaEmpaqueRevision)
            .where(ScmReglaEmpaqueRevision.id == revision_id)
            .with_for_update()
        )
        if revision is None:
            raise ScmServiceError(
                "PACKAGING_RULE_NOT_FOUND",
                "La revision de regla no existe.",
                status_code=404,
            )
        received = expected_version(data.get("version"))
        if received != revision.version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version de la regla esta desactualizada.",
                status_code=409,
            )
        if revision.estado != ESTADO_REGLA_BORRADOR:
            raise ScmServiceError(
                "PACKAGING_RULE_NOT_EDITABLE",
                "Solo una regla BORRADOR puede editarse.",
                status_code=409,
            )
        before = _serialize_rule(revision)
        values = _rule_values(data)
        for field, value in values.items():
            setattr(revision, field, value)
        revision.version += 1
        session.flush()
        response = _serialize_rule(revision)
        session.add(_event(
            "SCM_REGLA_EMPAQUE",
            revision.id,
            actor,
            "PACKAGING_RULE_UPDATED",
            before=before,
            after=response,
        ))
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PACKAGING_RULE_CONFLICT",
            "No se pudo actualizar la regla.",
            status_code=409,
        ) from error


def _approval_viability(revision):
    container = revision.regla.tipo_contenedor
    if (
        not revision.medicion_fisica_probada
        or not revision.regla.perfil.activo
        or not container.activo
    ):
        raise ScmServiceError(
            "PACKAGING_RULE_NOT_VIABLE",
            "La regla requiere medicion probada y maestros activos.",
            status_code=422,
        )
    tara_upper_kg = (
        container.tara_nominal_g + container.tolerancia_tara_g
    ) / Decimal("1000")
    effective = min(
        revision.peso_neto_operativo_max_kg,
        container.peso_bruto_max_kg
        - tara_upper_kg
        - revision.margen_seguridad_kg,
    )
    if (
        container.peso_bruto_max_kg <= 0
        or effective <= 0
        or revision.cantidad_objetivo_un
        > revision.cantidad_maxima_probada_un
    ):
        raise ScmServiceError(
            "PACKAGING_RULE_NOT_VIABLE",
            "Los limites fisicos no dejan capacidad neta positiva.",
            status_code=422,
        )


def _rule_content_hash(revision):
    canonical = {
        "perfil_empacable_id": revision.regla.perfil_empacable_id,
        "tipo_contenedor_id": revision.regla.tipo_contenedor_id,
        "medicion_fisica_probada": revision.medicion_fisica_probada,
        "cantidad_objetivo_un": revision.cantidad_objetivo_un,
        "cantidad_maxima_probada_un": (
            revision.cantidad_maxima_probada_un
        ),
        "peso_neto_operativo_max_kg": format(
            revision.peso_neto_operativo_max_kg,
            ".3f",
        ),
        "margen_seguridad_kg": format(
            revision.margen_seguridad_kg,
            ".3f",
        ),
        "tolerancia_peso_abs_g": format(
            revision.tolerancia_peso_abs_g,
            ".3f",
        ),
        "tolerancia_peso_pct": format(
            revision.tolerancia_peso_pct,
            ".4f",
        ),
        "tara_nominal_g_snapshot": format(
            revision.tara_nominal_g_snapshot,
            ".3f",
        ),
        "tolerancia_tara_g_snapshot": format(
            revision.tolerancia_tara_g_snapshot,
            ".3f",
        ),
        "peso_bruto_max_kg_snapshot": format(
            revision.peso_bruto_max_kg_snapshot,
            ".3f",
        ),
    }
    raw = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def approve_packaging_rule(
    session,
    *,
    actor_id,
    revision_id,
    operation_id,
    data,
):
    endpoint = f"/reglas-empaque/{revision_id}/aprobar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="EMPAQUE_APROBAR",
        )
        reject_unknown_fields(data, allowed={"version"})
        operation, replay = _reserve_operation(
            session,
            operation_id=operation_id,
            endpoint=endpoint,
            actor=actor,
            payload=data,
        )
        if replay is not None:
            session.rollback()
            return replay
        revision = session.scalar(
            select(ScmReglaEmpaqueRevision)
            .where(ScmReglaEmpaqueRevision.id == revision_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if revision is None:
            raise ScmServiceError(
                "PACKAGING_RULE_NOT_FOUND",
                "La revision de regla no existe.",
                status_code=404,
            )
        received = expected_version(data.get("version"))
        if received != revision.version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version de la regla esta desactualizada.",
                status_code=409,
            )
        if revision.estado != ESTADO_REGLA_BORRADOR:
            raise ScmServiceError(
                "PACKAGING_RULE_NOT_APPROVABLE",
                "Solo una regla BORRADOR puede aprobarse.",
                status_code=409,
            )
        if revision.creada_por_id == actor.id:
            raise ScmServiceError(
                "CREATOR_CANNOT_APPROVE",
                "El creador de la revision no puede aprobarla.",
                status_code=403,
            )
        _approval_viability(revision)
        container = revision.regla.tipo_contenedor
        revision.tara_nominal_g_snapshot = container.tara_nominal_g
        revision.tolerancia_tara_g_snapshot = (
            container.tolerancia_tara_g
        )
        revision.peso_bruto_max_kg_snapshot = (
            container.peso_bruto_max_kg
        )

        previous = session.scalars(
            select(ScmReglaEmpaqueRevision)
            .where(
                ScmReglaEmpaqueRevision.regla_id == revision.regla_id,
                ScmReglaEmpaqueRevision.estado
                == ESTADO_REGLA_APROBADA,
                ScmReglaEmpaqueRevision.id != revision.id,
            )
            .with_for_update()
        ).all()
        for approved in previous:
            approved.estado = ESTADO_REGLA_RETIRADA
            approved.retirada_por_id = actor.id
            approved.retirada_at = utc_now()
            approved.version += 1
        if previous:
            session.flush()

        revision.content_hash = _rule_content_hash(revision)
        revision.estado = ESTADO_REGLA_APROBADA
        revision.aprobada_por_id = actor.id
        revision.aprobada_at = utc_now()
        revision.version += 1
        session.flush()
        response = _serialize_rule(revision)
        session.add(_event(
            "SCM_REGLA_EMPAQUE",
            revision.id,
            actor,
            "PACKAGING_RULE_APPROVED",
            after=response,
            operation=operation,
        ))
        operation.estado_http = 200
        operation.response_json = response
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PACKAGING_RULE_CONFLICT",
            "No se pudo aprobar la regla.",
            status_code=409,
        ) from error


def publish_packaging_rule_directly(
    session,
    *,
    actor_id,
    revision_id,
    operation_id,
    data,
):
    endpoint = f"/reglas-empaque/{revision_id}/publicar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="EMPAQUE_PUBLICAR_DIRECTO",
        )
        reject_unknown_fields(data, allowed={"version"})
        operation, replay = _reserve_operation(
            session,
            operation_id=operation_id,
            endpoint=endpoint,
            actor=actor,
            payload=data,
        )
        if replay is not None:
            session.rollback()
            return replay
        revision = session.scalar(
            select(ScmReglaEmpaqueRevision)
            .where(ScmReglaEmpaqueRevision.id == revision_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if revision is None:
            raise ScmServiceError(
                "PACKAGING_RULE_NOT_FOUND",
                "La revision de regla no existe.",
                status_code=404,
            )
        received = expected_version(data.get("version"))
        if received != revision.version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version de la regla esta desactualizada.",
                status_code=409,
            )
        if revision.estado != ESTADO_REGLA_BORRADOR:
            raise ScmServiceError(
                "PACKAGING_RULE_NOT_PUBLISHABLE",
                "Solo una regla BORRADOR puede publicarse directamente.",
                status_code=409,
            )
        _approval_viability(revision)
        container = revision.regla.tipo_contenedor
        revision.tara_nominal_g_snapshot = container.tara_nominal_g
        revision.tolerancia_tara_g_snapshot = container.tolerancia_tara_g
        revision.peso_bruto_max_kg_snapshot = container.peso_bruto_max_kg

        previous = session.scalars(
            select(ScmReglaEmpaqueRevision)
            .where(
                ScmReglaEmpaqueRevision.regla_id == revision.regla_id,
                ScmReglaEmpaqueRevision.estado == ESTADO_REGLA_APROBADA,
                ScmReglaEmpaqueRevision.id != revision.id,
            )
            .with_for_update()
        ).all()
        for approved in previous:
            approved.estado = ESTADO_REGLA_RETIRADA
            approved.retirada_por_id = actor.id
            approved.retirada_at = utc_now()
            approved.version += 1
        if previous:
            session.flush()

        revision.content_hash = _rule_content_hash(revision)
        revision.estado = ESTADO_REGLA_APROBADA
        revision.aprobada_por_id = actor.id
        revision.aprobada_at = utc_now()
        revision.version += 1
        session.flush()
        response = _serialize_rule(revision)
        session.add(_event(
            "SCM_REGLA_EMPAQUE",
            revision.id,
            actor,
            "PACKAGING_RULE_PUBLISHED_DIRECTLY",
            after=response,
            operation=operation,
        ))
        operation.estado_http = 200
        operation.response_json = response
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PACKAGING_RULE_CONFLICT",
            "No se pudo publicar la regla.",
            status_code=409,
        ) from error


def calculate_packaging_plan(session, *, actor_id, data):
    actor = load_actor(session, actor_id, capability="EMPAQUE_VER")
    reject_unknown_fields(
        data,
        allowed={
            "regla_revision_id",
            "cantidad_planificada_un",
            "peso_unitario_snapshot_g",
            "override_cantidad_un",
            "tara_real_g",
            "motivo_override",
        },
    )
    revision_id = positive_integer(
        data.get("regla_revision_id"),
        field="regla_revision_id",
    )
    planned = positive_integer(
        data.get("cantidad_planificada_un"),
        field="cantidad_planificada_un",
    )
    unit_weight = _decimal(
        data.get("peso_unitario_snapshot_g"),
        field="peso_unitario_snapshot_g",
        positive=True,
    )
    revision = session.get(ScmReglaEmpaqueRevision, revision_id)
    if revision is None or revision.estado != ESTADO_REGLA_APROBADA:
        raise ScmServiceError(
            "PACKAGING_RULE_NOT_FOUND",
            "Se requiere una regla aprobada.",
            status_code=404,
        )
    base = calculate_packaging_capacity(
        tara_nominal_g=revision.tara_nominal_g_snapshot,
        tolerancia_tara_g=revision.tolerancia_tara_g_snapshot,
        peso_bruto_max_kg=revision.peso_bruto_max_kg_snapshot,
        peso_neto_operativo_max_kg=(
            revision.peso_neto_operativo_max_kg
        ),
        margen_seguridad_kg=revision.margen_seguridad_kg,
        cantidad_objetivo_un=revision.cantidad_objetivo_un,
        cantidad_maxima_probada_un=(
            revision.cantidad_maxima_probada_un
        ),
        peso_unitario_snapshot_g=unit_weight,
    )
    override_quantity = data.get("override_cantidad_un")
    real_tare = data.get("tara_real_g")
    has_override = override_quantity is not None or real_tare is not None
    reason = _optional_text(
        data.get("motivo_override"),
        field="motivo_override",
        max_length=500,
    )
    if has_override:
        if not actor.tiene_capacidad("EMPAQUE_ADMINISTRAR"):
            raise ScmServiceError(
                "CAPABILITY_REQUIRED",
                "El override requiere EMPAQUE_ADMINISTRAR.",
                status_code=403,
            )
        if not reason:
            raise ScmServiceError(
                "PACKAGING_OVERRIDE_REASON_REQUIRED",
                "El override requiere un motivo.",
                status_code=422,
            )

    capacity = base["capacidad_efectiva_un"]
    capacity_by_weight = base["capacidad_por_peso_un"]
    tare_used = revision.tara_nominal_g_snapshot
    if override_quantity is not None:
        override_quantity = positive_integer(
            override_quantity,
            field="override_cantidad_un",
        )
        if override_quantity > capacity:
            raise ScmServiceError(
                "PACKAGING_OVERRIDE_EXCEEDS_LIMIT",
                "El override no puede ampliar la capacidad aprobada.",
                status_code=422,
                details={"approved_capacity_un": capacity},
            )
        capacity = min(capacity, override_quantity)
    if real_tare is not None:
        tare_used = _decimal(real_tare, field="tara_real_g")
        with_real_tare = calculate_packaging_capacity(
            tara_nominal_g=tare_used,
            tolerancia_tara_g=Decimal("0"),
            peso_bruto_max_kg=revision.peso_bruto_max_kg_snapshot,
            peso_neto_operativo_max_kg=(
                revision.peso_neto_operativo_max_kg
            ),
            margen_seguridad_kg=revision.margen_seguridad_kg,
            cantidad_objetivo_un=revision.cantidad_objetivo_un,
            cantidad_maxima_probada_un=(
                revision.cantidad_maxima_probada_un
            ),
            peso_unitario_snapshot_g=unit_weight,
        )
        capacity_by_weight = with_real_tare["capacidad_por_peso_un"]
        capacity = min(
            capacity,
            base["capacidad_efectiva_un"],
            with_real_tare["capacidad_efectiva_un"],
        )

    number_of_containers = (planned + capacity - 1) // capacity
    remaining = planned
    containers = []
    for sequence in range(1, number_of_containers + 1):
        quantity = min(capacity, remaining)
        net_kg = (
            Decimal(quantity) * unit_weight / Decimal("1000")
        ).quantize(PHYSICAL_QUANTUM, rounding=ROUND_HALF_UP)
        containers.append({
            "secuencia": sequence,
            "cantidad_planificada_un": quantity,
            "peso_neto_teorico_kg": format(net_kg, ".3f"),
        })
        remaining -= quantity

    return {
        "regla_revision_id": revision.id,
        "content_hash": revision.content_hash,
        "cantidad_planificada_un": planned,
        "peso_unitario_snapshot_g": format(unit_weight, ".3f"),
        "tara_usada_g": format(tare_used, ".3f"),
        "capacidad_por_peso_un": capacity_by_weight,
        "capacidad_efectiva_un": capacity,
        "numero_contenedores": number_of_containers,
        "override": (
            {
                "actor_id": actor.id,
                "motivo": reason,
                "cantidad_un": override_quantity,
                "tara_real_g": (
                    format(tare_used, ".3f")
                    if real_tare is not None
                    else None
                ),
            }
            if has_override
            else None
        ),
        "contenedores": containers,
    }
