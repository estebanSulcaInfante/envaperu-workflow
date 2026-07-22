from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import (
    CLASE_COLORANTE,
    CLASE_MATERIA_PRIMA,
    CLASES_MATERIAL,
    MODALIDAD_POR_CONFIGURAR,
    MODALIDAD_SEGUNDA,
    MODALIDAD_VIRGEN,
    MODALIDADES_RECEPCION,
    ScmCategoriaRecepcion,
    ScmMaterial,
)
from app.services.scm_material_service import (
    ScmMaterialConfigurationError,
    create_colorante_with_scm,
    create_materia_prima_with_scm,
)
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    positive_integer,
    reject_no_changes,
    reject_unknown_fields,
    require_patch_field,
    required_text,
    stable_code,
)


CONFIG_CAPABILITY = "CONFIG_RECEPCION_ADMINISTRAR"
CATEGORY_CREATE_FIELDS = {
    "codigo",
    "nombre",
    "modalidad_default",
    "lote_externo_obligatorio",
    "recepcion_habilitada",
    "activo",
}
CATEGORY_MUTABLE_FIELDS = {
    "nombre",
    "modalidad_default",
    "lote_externo_obligatorio",
    "recepcion_habilitada",
    "activo",
}
MATERIAL_CREATE_FIELDS = {
    "codigo",
    "nombre",
    "clase",
    "categoria_recepcion_id",
    "unidad_base",
    "activo",
    "tipo_colorante",
}
MATERIAL_MUTABLE_FIELDS = {
    "nombre",
    "categoria_recepcion_id",
    "activo",
    "tipo_colorante",
}


def _colorant_type(value):
    normalized = str(value or "COLORANTE").strip().upper()
    if normalized not in {"COLORANTE", "ADITIVO"}:
        raise ScmServiceError(
            "INVALID_COLORANT_TYPE",
            "El tipo debe ser COLORANTE o ADITIVO.",
            status_code=400,
        )
    return normalized


def _boolean_value(data, field, *, default):
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, bool):
        raise ScmServiceError(
            "INVALID_BOOLEAN_FIELD",
            f"El campo {field} debe ser booleano.",
            status_code=400,
            details={"field": field},
        )
    return value


def _reception_modality(value):
    modality = stable_code(
        value,
        field="modalidad_default",
        max_length=40,
    )
    if modality not in MODALIDADES_RECEPCION:
        raise ScmServiceError(
            "INVALID_RECEPTION_MODALITY",
            "La modalidad de recepcion no es valida.",
            status_code=422,
            details={
                "field": "modalidad_default",
                "allowed": list(MODALIDADES_RECEPCION),
            },
        )
    return modality


def _material_class(value):
    material_class = stable_code(
        value,
        field="clase",
        max_length=30,
    )
    if material_class not in CLASES_MATERIAL:
        raise ScmServiceError(
            "INVALID_MATERIAL_CLASS",
            "La clase del material comun no es valida.",
            status_code=422,
            details={"allowed": list(CLASES_MATERIAL)},
        )
    return material_class


def _material_unit(value):
    unit = stable_code(
        "KG" if value is None else value,
        field="unidad_base",
        max_length=10,
    )
    if unit != "KG":
        raise ScmServiceError(
            "INVALID_MATERIAL_UNIT",
            "La unidad base soportada para materiales es KG.",
            status_code=422,
        )
    return unit


def _validate_category_state(*, modality, reception_enabled):
    if modality == MODALIDAD_POR_CONFIGURAR and reception_enabled:
        raise ScmServiceError(
            "CATEGORY_NOT_CONFIGURED",
            "Una categoria POR_CONFIGURAR no puede habilitar recepciones.",
            status_code=422,
        )


def _category_or_404(
    session,
    category_id,
    *,
    lock=False,
    require_active=False,
):
    parsed_id = positive_integer(
        category_id,
        field="categoria_recepcion_id",
    )
    statement = select(ScmCategoriaRecepcion).where(
        ScmCategoriaRecepcion.id == parsed_id
    )
    if lock:
        statement = statement.with_for_update()
    category = session.scalar(statement)
    if category is None:
        raise ScmServiceError(
            "CATEGORY_NOT_FOUND",
            "La categoria de recepcion no existe.",
            status_code=404,
        )
    if require_active and not category.activo:
        raise ScmServiceError(
            "CATEGORY_INACTIVE",
            "No se puede asignar una categoria de recepcion inactiva.",
            status_code=422,
            details={"categoria_recepcion_id": category.id},
        )
    return category


def _category_event(category, actor, event_type, *, before=None):
    return ScmEvento(
        aggregate_type="SCM_CATEGORIA_RECEPCION",
        aggregate_id=category.id,
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=category.to_dict(),
    )


def _legacy_type_for_category(category):
    if category.modalidad_default == MODALIDAD_VIRGEN:
        return "VIRGEN"
    if category.modalidad_default == MODALIDAD_SEGUNDA:
        return "SEGUNDA"
    return None


def serialize_material(material):
    payload = material.to_dict()
    category = material.categoria_recepcion
    payload.update({
        "categoria_recepcion_codigo": category.codigo,
        "categoria_recepcion": category.to_dict(),
        "materia_prima_id": (
            material.materia_prima.id if material.materia_prima else None
        ),
        "colorante_id": (
            material.colorante.id if material.colorante else None
        ),
        "tipo_colorante": (
            material.colorante.tipo if material.colorante else None
        ),
    })
    return payload


def _material_event(material, actor, event_type, *, before=None):
    return ScmEvento(
        aggregate_type="SCM_MATERIAL",
        aggregate_id=material.id,
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=serialize_material(material),
    )


def list_reception_categories(session, *, actor_id, active=None):
    load_actor(session, actor_id)
    statement = select(ScmCategoriaRecepcion)
    if active is not None:
        statement = statement.where(
            ScmCategoriaRecepcion.activo.is_(active)
        )
    categories = session.scalars(
        statement.order_by(ScmCategoriaRecepcion.codigo)
    ).all()
    return {"items": [category.to_dict() for category in categories]}


def get_reception_category(session, *, actor_id, category_id):
    load_actor(session, actor_id)
    return _category_or_404(session, category_id).to_dict()


def create_reception_category(session, *, actor_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability=CONFIG_CAPABILITY,
        )
        reject_unknown_fields(data, allowed=CATEGORY_CREATE_FIELDS)
        code = (
            stable_code(data.get("codigo"))
            if data.get("codigo")
            else generar_codigo_catalogo("CATEGORIA_RECEPCION", session=session)
        )
        if session.scalar(
            select(ScmCategoriaRecepcion.id).where(
                ScmCategoriaRecepcion.codigo == code
            )
        ) is not None:
            raise ScmServiceError(
                "CATEGORY_CODE_CONFLICT",
                "El codigo de la categoria ya existe.",
                status_code=409,
            )

        modality = _reception_modality(data.get("modalidad_default"))
        reception_enabled = _boolean_value(
            data,
            "recepcion_habilitada",
            default=False,
        )
        _validate_category_state(
            modality=modality,
            reception_enabled=reception_enabled,
        )
        category = ScmCategoriaRecepcion(
            codigo=code,
            nombre=required_text(
                data.get("nombre"),
                field="nombre",
                max_length=120,
            ),
            modalidad_default=modality,
            lote_externo_obligatorio=_boolean_value(
                data,
                "lote_externo_obligatorio",
                default=False,
            ),
            recepcion_habilitada=reception_enabled,
            activo=_boolean_value(data, "activo", default=True),
        )
        session.add(category)
        session.flush()
        session.add(
            _category_event(category, actor, "CATEGORIA_RECEPCION_CREADA")
        )
        session.commit()
        return category.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "CATEGORY_CONFLICT",
            "La categoria entra en conflicto con otro registro.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise


def update_reception_category(
    session,
    *,
    actor_id,
    category_id,
    data,
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability=CONFIG_CAPABILITY,
        )
        reject_unknown_fields(
            data,
            allowed=CATEGORY_MUTABLE_FIELDS | {"codigo", "version"},
        )
        category = _category_or_404(session, category_id, lock=True)
        received_version = expected_version(data.get("version"))
        if category.version != received_version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version de la categoria esta desactualizada.",
                status_code=409,
                details={
                    "expected": category.version,
                    "received": received_version,
                },
            )
        if (
            "codigo" in data
            and stable_code(data["codigo"]) != category.codigo
        ):
            raise ScmServiceError(
                "IMMUTABLE_CATEGORY_CODE",
                "El codigo estable de la categoria no puede modificarse.",
                status_code=422,
            )
        require_patch_field(
            data,
            mutable=CATEGORY_MUTABLE_FIELDS | {"codigo"},
        )

        before = category.to_dict()
        name = (
            required_text(
                data["nombre"],
                field="nombre",
                max_length=120,
            )
            if "nombre" in data
            else category.nombre
        )
        modality = (
            _reception_modality(data["modalidad_default"])
            if "modalidad_default" in data
            else category.modalidad_default
        )
        reception_enabled = _boolean_value(
            data,
            "recepcion_habilitada",
            default=category.recepcion_habilitada,
        )
        _validate_category_state(
            modality=modality,
            reception_enabled=reception_enabled,
        )
        external_lot_required = _boolean_value(
            data,
            "lote_externo_obligatorio",
            default=category.lote_externo_obligatorio,
        )
        active = _boolean_value(
            data,
            "activo",
            default=category.activo,
        )
        changed = any((
            name != category.nombre,
            modality != category.modalidad_default,
            reception_enabled != category.recepcion_habilitada,
            external_lot_required != category.lote_externo_obligatorio,
            active != category.activo,
        ))
        if not changed:
            reject_no_changes()

        modality_changed = modality != category.modalidad_default
        category.nombre = name
        category.modalidad_default = modality
        category.recepcion_habilitada = reception_enabled
        category.lote_externo_obligatorio = external_lot_required
        category.activo = active
        if modality_changed:
            legacy_type = _legacy_type_for_category(category)
            for material in category.materiales:
                if material.clase != CLASE_MATERIA_PRIMA:
                    continue
                if material.materia_prima is None:
                    raise ScmServiceError(
                        "MATERIAL_IDENTITY_BROKEN",
                        "Un material de la categoria no tiene identidad legacy.",
                        status_code=409,
                        details={"material_id": material.id},
                    )
                material.materia_prima.tipo = legacy_type
        category.version += 1
        session.flush()
        session.add(
            _category_event(
                category,
                actor,
                "CATEGORIA_RECEPCION_ACTUALIZADA",
                before=before,
            )
        )
        session.commit()
        return category.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "CATEGORY_CONFLICT",
            "La categoria entra en conflicto con otro registro.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise


def list_materials(session, *, actor_id, active=None):
    load_actor(session, actor_id)
    statement = select(ScmMaterial)
    if active is not None:
        statement = statement.where(ScmMaterial.activo.is_(active))
    materials = session.scalars(
        statement.order_by(ScmMaterial.codigo)
    ).all()
    return {"items": [serialize_material(item) for item in materials]}


def get_material(session, *, actor_id, material_id):
    load_actor(session, actor_id)
    material = session.get(ScmMaterial, material_id)
    if material is None:
        raise ScmServiceError(
            "MATERIAL_NOT_FOUND",
            "El material comun no existe.",
            status_code=404,
        )
    return serialize_material(material)


def create_material(session, *, actor_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability=CONFIG_CAPABILITY,
        )
        reject_unknown_fields(data, allowed=MATERIAL_CREATE_FIELDS)
        material_class = _material_class(data.get("clase"))
        if data.get("codigo"):
            code = stable_code(data.get("codigo"))
        elif material_class == CLASE_MATERIA_PRIMA:
            code = generar_codigo_catalogo("MATERIA_PRIMA", session=session)
        else:
            tipo = _colorant_type(data.get("tipo_colorante"))
            key = "ADITIVO" if tipo == "ADITIVO" else "COLORANTE"
            code = generar_codigo_catalogo(key, session=session)
        if session.scalar(
            select(ScmMaterial.id).where(ScmMaterial.codigo == code)
        ) is not None:
            raise ScmServiceError(
                "MATERIAL_CODE_CONFLICT",
                "El codigo del material ya existe.",
                status_code=409,
            )
        name = required_text(
            data.get("nombre"),
            field="nombre",
            max_length=100,
        )
        _material_unit(data.get("unidad_base"))
        category = _category_or_404(
            session,
            data.get("categoria_recepcion_id"),
            lock=True,
            require_active=True,
        )
        active = _boolean_value(data, "activo", default=True)

        if material_class == CLASE_MATERIA_PRIMA:
            if "tipo_colorante" in data:
                raise ScmServiceError(
                    "COLORANT_TYPE_NOT_APPLICABLE",
                    "tipo_colorante solo aplica a materiales de clase COLORANTE.",
                    status_code=422,
                )
            legacy = create_materia_prima_with_scm(
                session=session,
                nombre=name,
                tipo=_legacy_type_for_category(category),
                categoria_codigo=category.codigo,
                codigo_scm=code,
            )
        else:
            legacy = create_colorante_with_scm(
                session=session,
                nombre=name,
                categoria_codigo=category.codigo,
                codigo_scm=code,
            )
            legacy.tipo = _colorant_type(data.get("tipo_colorante"))
        material = legacy.scm_material
        material.activo = active
        session.flush()
        session.add(_material_event(material, actor, "MATERIAL_CREADO"))
        session.commit()
        return serialize_material(material)
    except ScmServiceError:
        session.rollback()
        raise
    except ScmMaterialConfigurationError as error:
        session.rollback()
        raise ScmServiceError(
            error.code,
            str(error),
            status_code=422,
            details=error.details,
        ) from error
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "MATERIAL_CODE_CONFLICT",
            "El codigo del material ya existe o su identidad es invalida.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise


def update_material(session, *, actor_id, material_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability=CONFIG_CAPABILITY,
        )
        reject_unknown_fields(
            data,
            allowed=(
                MATERIAL_MUTABLE_FIELDS
                | {"codigo", "clase", "unidad_base", "version"}
            ),
        )
        parsed_id = positive_integer(material_id, field="material_id")
        material_probe = session.get(ScmMaterial, parsed_id)
        if material_probe is None:
            raise ScmServiceError(
                "MATERIAL_NOT_FOUND",
                "El material comun no existe.",
                status_code=404,
            )
        category_ids = {material_probe.categoria_recepcion_id}
        target_category_id = None
        if "categoria_recepcion_id" in data:
            target_category_id = positive_integer(
                data["categoria_recepcion_id"],
                field="categoria_recepcion_id",
            )
            category_ids.add(target_category_id)
        locked_categories = {
            item.id: item
            for item in session.scalars(
                select(ScmCategoriaRecepcion)
                .where(ScmCategoriaRecepcion.id.in_(category_ids))
                .order_by(ScmCategoriaRecepcion.id)
                .with_for_update()
            )
        }
        if (
            target_category_id is not None
            and target_category_id not in locked_categories
        ):
            raise ScmServiceError(
                "CATEGORY_NOT_FOUND",
                "La categoria de recepcion no existe.",
                status_code=404,
            )
        if (
            target_category_id is not None
            and not locked_categories[target_category_id].activo
        ):
            raise ScmServiceError(
                "CATEGORY_INACTIVE",
                "No se puede asignar una categoria de recepcion inactiva.",
                status_code=422,
                details={
                    "categoria_recepcion_id": target_category_id,
                },
            )
        material = session.scalar(
            select(ScmMaterial)
            .where(ScmMaterial.id == parsed_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if material is None:
            raise ScmServiceError(
                "MATERIAL_NOT_FOUND",
                "El material comun no existe.",
                status_code=404,
            )
        received_version = expected_version(data.get("version"))
        if material.version != received_version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version del material esta desactualizada.",
                status_code=409,
                details={
                    "expected": material.version,
                    "received": received_version,
                },
            )
        if (
            "codigo" in data
            and stable_code(data["codigo"]) != material.codigo
        ):
            raise ScmServiceError(
                "IMMUTABLE_MATERIAL_CODE",
                "El codigo estable del material no puede modificarse.",
                status_code=422,
            )
        if (
            "clase" in data
            and _material_class(data["clase"]) != material.clase
        ):
            raise ScmServiceError(
                "IMMUTABLE_MATERIAL_CLASS",
                "La clase del material no puede modificarse.",
                status_code=422,
            )
        if "unidad_base" in data:
            unit = _material_unit(data["unidad_base"])
            if unit != material.unidad_base:
                raise ScmServiceError(
                    "IMMUTABLE_MATERIAL_UNIT",
                    "La unidad base del material no puede modificarse.",
                    status_code=422,
                )
        require_patch_field(
            data,
            mutable=(
                MATERIAL_MUTABLE_FIELDS
                | {"codigo", "clase", "unidad_base"}
            ),
        )

        legacy = (
            material.materia_prima
            if material.clase == CLASE_MATERIA_PRIMA
            else material.colorante
        )
        if legacy is None:
            raise ScmServiceError(
                "MATERIAL_IDENTITY_BROKEN",
                "El material comun no tiene su identidad legacy 1:1.",
                status_code=409,
            )
        before = serialize_material(material)
        name = (
            required_text(
                data["nombre"],
                field="nombre",
                max_length=100,
            )
            if "nombre" in data
            else material.nombre
        )
        target_category = (
            locked_categories[target_category_id]
            if target_category_id is not None
            else material.categoria_recepcion
        )
        active = _boolean_value(
            data,
            "activo",
            default=material.activo,
        )
        colorant_type = (
            _colorant_type(data.get("tipo_colorante", legacy.tipo))
            if material.clase == CLASE_COLORANTE
            else None
        )
        if material.clase == CLASE_MATERIA_PRIMA and "tipo_colorante" in data:
            raise ScmServiceError(
                "COLORANT_TYPE_NOT_APPLICABLE",
                "tipo_colorante solo aplica a materiales de clase COLORANTE.",
                status_code=422,
            )
        changed = any((
            name != material.nombre,
            target_category.id != material.categoria_recepcion_id,
            active != material.activo,
            material.clase == CLASE_COLORANTE and colorant_type != legacy.tipo,
        ))
        if not changed:
            reject_no_changes()

        material.nombre = name
        legacy.nombre = name
        if target_category.id != material.categoria_recepcion_id:
            material.categoria_recepcion = target_category
            if material.clase == CLASE_MATERIA_PRIMA:
                legacy.tipo = _legacy_type_for_category(target_category)
        material.activo = active
        if material.clase == CLASE_COLORANTE:
            legacy.tipo = colorant_type
        material.version += 1
        session.flush()
        session.add(
            _material_event(
                material,
                actor,
                "MATERIAL_ACTUALIZADO",
                before=before,
            )
        )
        session.commit()
        return serialize_material(material)
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "MATERIAL_CONFLICT",
            "El material entra en conflicto con otro registro.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise
