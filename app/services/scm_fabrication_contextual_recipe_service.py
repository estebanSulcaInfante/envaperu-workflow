"""Atomic contextual color/formulation command for draft fabrication orders."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select
from app.models.producto import ColorBase, ColorProduccion, FamiliaColor
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import ScmMaterial
from app.models.scm_production_orders import ScmCorridaFabricacion, ScmOrdenOperacion
from app.services.catalog_code_generator import generar_numero_catalogo
from app.services.color_recipe_service import ColorRecipeError, create_recipe, normalize_hex
from app.services.scm_fabrication_order_service import (
    _output_piece_color,
    _serialize,
    _validated_approved_recipe,
)
from app.services.scm_material_catalog_service import create_material
from app.services.scm_ot_service import _reserve_operation
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    load_actor_any,
    positive_integer,
    reject_unknown_fields,
    required_text,
)


ACTION_DRAFT = "GUARDAR_BORRADOR"
ACTION_APPROVE = "APROBAR_SELECCIONAR"
ALLOWED_ACTIONS = {ACTION_DRAFT, ACTION_APPROVE}


def _int_value(value, *, field):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "POSITIVE_INTEGER_REQUIRED",
            f"El campo {field} debe ser un entero positivo.",
            status_code=400,
            details={"field": field},
        ) from error
    return positive_integer(parsed, field=field)


def _serialize_color(color):
    return {
        "id": color.id,
        "nombre": color.color_base_rel.nombre if color.color_base_rel else None,
        "familia_color_id": color.familia_color_id,
        "familia_color_nombre": (
            color.familia_color_rel.nombre if color.familia_color_rel else None
        ),
        "hex_referencia": color.hex_referencia,
        "activo": color.activo,
        "version": color.version,
    }


def _resolve_family(session, raw, *, created):
    family_id = raw.get("familia_color_id")
    family_name = raw.get("familia_nueva_nombre")
    if family_id is not None and family_name not in (None, ""):
        raise ScmServiceError(
            "COLOR_FAMILY_AMBIGUOUS",
            "familia_color_id y familia_nueva_nombre son mutuamente excluyentes.",
            status_code=400,
        )
    if family_id is not None:
        family = session.get(FamiliaColor, _int_value(
            family_id,
            field="color.familia_color_id",
        ))
        if family is None:
            raise ScmServiceError(
                "COLOR_FAMILY_NOT_FOUND",
                "La familia de color no existe.",
                status_code=404,
            )
        if not family.activo:
            raise ScmServiceError(
                "COLOR_FAMILY_INACTIVE",
                "La familia de color esta inactiva.",
                status_code=422,
            )
        return family
    if family_name in (None, ""):
        raise ScmServiceError(
            "COLOR_FAMILY_REQUIRED",
            "Un color nuevo requiere familia_color_id o familia_nueva_nombre.",
            status_code=400,
        )
    normalized = required_text(
        str(family_name).upper(),
        field="color.familia_nueva_nombre",
        max_length=50,
    )
    family = session.scalar(
        select(FamiliaColor)
        .where(func.upper(FamiliaColor.nombre) == normalized.upper())
        .with_for_update()
    )
    if family is not None:
        if not family.activo:
            raise ScmServiceError(
                "COLOR_FAMILY_INACTIVE",
                "La familia de color esta inactiva.",
                status_code=422,
            )
        return family
    family = FamiliaColor(
        nombre=normalized.upper(),
        codigo=generar_numero_catalogo("FAMILIA_COLOR", session=session),
        activo=True,
    )
    session.add(family)
    session.flush()
    created.append({
        "id": family.id,
        "nombre": family.nombre,
        "codigo": family.codigo,
    })
    return family


def _resolve_color(session, raw, *, created):
    if not isinstance(raw, dict):
        raise ScmServiceError(
            "COLOR_REQUIRED",
            "color debe ser un objeto JSON.",
            status_code=400,
        )
    if raw.get("id") is not None:
        reject_unknown_fields(raw, allowed={"id"})
        color = session.get(ColorProduccion, _int_value(raw["id"], field="color.id"))
        if color is None:
            raise ScmServiceError(
                "COLOR_NOT_FOUND",
                "El color de produccion no existe.",
                status_code=404,
            )
        if not color.activo:
            raise ScmServiceError(
                "COLOR_INACTIVE",
                "El color de produccion esta inactivo.",
                status_code=422,
            )
        return color

    reject_unknown_fields(
        raw,
        allowed={"nombre", "familia_color_id", "familia_nueva_nombre", "hex_referencia"},
    )
    name = required_text(
        str(raw.get("nombre") or "").upper(),
        field="color.nombre",
        max_length=50,
    )
    family = _resolve_family(session, raw, created=created)
    try:
        hex_value = normalize_hex(raw.get("hex_referencia"))
    except ColorRecipeError as error:
        raise ScmServiceError(
            error.code,
            error.message,
            status_code=422,
            details=error.details,
        ) from error
    base = session.scalar(
        select(ColorBase)
        .where(func.upper(ColorBase.nombre) == name.upper())
        .with_for_update()
    )
    if base is None:
        base = ColorBase(nombre=name.upper())
        session.add(base)
        session.flush()
    color = session.scalar(
        select(ColorProduccion)
        .where(
            ColorProduccion.color_base_id == base.id,
            ColorProduccion.familia_color_id == family.id,
        )
        .with_for_update()
    )
    if color is None:
        color = ColorProduccion(
            color_base_id=base.id,
            familia_color_id=family.id,
            hex_referencia=hex_value,
            activo=True,
        )
        session.add(color)
        session.flush()
        created.append(_serialize_color(color))
    elif not color.activo:
        raise ScmServiceError(
            "COLOR_INACTIVE",
            "El color de produccion esta inactivo.",
            status_code=422,
        )
    return color


def _material_payload(raw):
    if not isinstance(raw, dict):
        raise ScmServiceError(
            "INVALID_MATERIAL",
            "Cada material nuevo debe ser un objeto JSON.",
            status_code=400,
        )
    reject_unknown_fields(
        raw,
        allowed={
            "client_id",
            "nombre",
            "clase",
            "tipo_colorante",
            "categoria_recepcion_id",
        },
    )
    client_id = required_text(raw.get("client_id"), field="client_id", max_length=80)
    payload = {
        "nombre": raw.get("nombre"),
        "clase": raw.get("clase"),
        "categoria_recepcion_id": raw.get("categoria_recepcion_id"),
    }
    if raw.get("tipo_colorante") is not None:
        payload["tipo_colorante"] = raw["tipo_colorante"]
    return client_id, payload


def _recipe_scope(raw):
    value = (
        raw.get("producto_sku")
        if raw.get("producto_sku") not in (None, "")
        else raw.get("alcance")
    )
    if value in (None, "", "*"):
        return None
    if isinstance(value, dict):
        value = value.get("producto_sku") or value.get("sku")
    return required_text(value, field="receta.alcance", max_length=50)


def _normalize_recipe_lines(
    session,
    raw_lines,
    material_by_client,
    *,
    require_complete,
):
    if raw_lines is None and not require_complete:
        return []
    if not isinstance(raw_lines, list) or (require_complete and not raw_lines):
        raise ScmServiceError(
            "RECIPE_LINES_REQUIRED",
            "La formulacion requiere al menos una linea.",
            status_code=422,
        )
    normalized = []
    material_fraction = Decimal("0")
    for index, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            raise ScmServiceError(
                "INVALID_RECIPE_LINE",
                "Cada linea de formulacion debe ser un objeto JSON.",
                status_code=400,
                details={"index": index},
            )
        reject_unknown_fields(
            raw,
            allowed={"material_id", "material_client_id", "cantidad", "tipo_componente", "base_kg"},
        )
        if raw.get("material_id") is not None and raw.get("material_client_id") is not None:
            raise ScmServiceError(
                "MATERIAL_REFERENCE_AMBIGUOUS",
                "Una linea no puede usar material_id y material_client_id a la vez.",
                status_code=400,
            )
        if raw.get("material_client_id") is not None:
            client_id = required_text(
                raw["material_client_id"],
                field=f"receta.lineas[{index}].material_client_id",
                max_length=80,
            )
            material = material_by_client.get(client_id)
            if material is None:
                raise ScmServiceError(
                    "MATERIAL_CLIENT_NOT_FOUND",
                    "La linea referencia un material_client_id inexistente.",
                    status_code=422,
                    details={"material_client_id": client_id},
                )
        elif raw.get("material_id") is not None:
            material = session.get(ScmMaterial, _int_value(
                raw["material_id"],
                field=f"receta.lineas[{index}].material_id",
            ))
        else:
            raise ScmServiceError(
                "MATERIAL_REFERENCE_REQUIRED",
                "Cada linea requiere material_id o material_client_id.",
                status_code=400,
            )
        if material is None or not material.activo:
            raise ScmServiceError(
                "MATERIAL_NOT_FOUND",
                "El material de la linea no existe o esta inactivo.",
                status_code=422,
            )
        component_type = str(
            raw.get("tipo_componente")
            or ("MATERIA_PRIMA" if material.clase == "MATERIA_PRIMA" else "COLORANTE")
        ).strip().upper()
        line = {
            "material_id": material.id,
            "tipo_componente": component_type,
            "cantidad": raw.get("cantidad"),
            **({"base_kg": raw["base_kg"]} if "base_kg" in raw else {}),
        }
        if component_type == "MATERIA_PRIMA":
            try:
                fraction = Decimal(str(raw.get("cantidad")))
            except (InvalidOperation, TypeError, ValueError) as error:
                raise ScmServiceError(
                    "INVALID_RECIPE_FRACTION",
                    "La cantidad de materia prima debe ser una fraccion positiva.",
                    status_code=422,
                ) from error
            if not fraction.is_finite() or fraction <= 0 or fraction > 1:
                raise ScmServiceError(
                    "INVALID_RECIPE_FRACTION",
                    "La cantidad de materia prima debe estar entre 0 y 1.",
                    status_code=422,
                )
            material_fraction += fraction
        normalized.append(line)
    if require_complete and material_fraction != Decimal("1"):
        raise ScmServiceError(
            "FRACCIONES_RECETA_INVALIDAS",
            "Las fracciones de materia prima deben sumar 1.",
            status_code=422,
            details={"suma": str(material_fraction)},
        )
    return normalized


def _validate_run_color_compatibility(session, run, color):
    if (
        run.color_produccion_id not in (None, color.id)
        and run.estado != "BORRADOR"
    ):
        raise ScmServiceError(
            "COLOR_RUN_MISMATCH",
            "El color no coincide con el color de la corrida.",
            status_code=422,
            details={
                "corrida_color_id": run.color_produccion_id,
                "color_id": color.id,
            },
        )
    for output in run.salidas:
        piece_color, _ = _output_piece_color(session, output)
        if piece_color is not None and piece_color.color_produccion_id not in (None, color.id):
            raise ScmServiceError(
                "COLOR_OUTPUT_MISMATCH",
                "El color no es compatible con las salidas de la corrida.",
                status_code=422,
                details={
                    "salida_id": str(output.id),
                    "salida_color_id": piece_color.color_produccion_id,
                    "color_id": color.id,
                },
            )


def create_contextual_fabrication_recipe(
    session,
    *,
    actor_id,
    operation_id,
    operation_order_id,
    run_id,
    data,
):
    """Create a contextual recipe and optionally select it on a draft OF run."""
    actor = load_actor(session, actor_id, capability="OF_EDITAR_BORRADOR")
    if not isinstance(data, dict):
        raise ScmServiceError(
            "JSON_OBJECT_REQUIRED",
            "Se requiere un objeto JSON.",
            status_code=400,
        )
    reject_unknown_fields(
        data,
        allowed={"version", "color", "materiales_nuevos", "receta", "accion"},
    )
    version = expected_version(data.get("version"))
    action = str(data.get("accion") or "").strip().upper()
    if action not in ALLOWED_ACTIONS:
        raise ScmServiceError(
            "INVALID_FORMULATION_ACTION",
            "accion debe ser GUARDAR_BORRADOR o APROBAR_SELECCIONAR.",
            status_code=400,
        )
    actor = load_actor(session, actor_id, capability="OF_EDITAR_BORRADOR")
    load_actor(session, actor_id, capability="ARTICULO_ADMINISTRAR")
    if action == ACTION_APPROVE:
        load_actor(session, actor_id, capability="FORMULACION_PUBLICAR_DIRECTO")
    raw_new_materials = data.get("materiales_nuevos") or []
    if not isinstance(raw_new_materials, list):
        raise ScmServiceError(
            "INVALID_MATERIALS",
            "materiales_nuevos debe ser una lista.",
            status_code=400,
        )
    if raw_new_materials:
        load_actor_any(
            session,
            actor_id,
            capabilities=("CATALOGO_MATERIAL_ADMINISTRAR", "CONFIG_RECEPCION_ADMINISTRAR"),
        )

    endpoint = (
        f"POST /ordenes-fabricacion/{operation_order_id}/corridas/{run_id}/"
        "formulacion-contextual"
    )
    operation, replay = _reserve_operation(
        session,
        operation_id,
        endpoint,
        actor,
        data,
    )
    if replay is not None:
        return replay
    try:
        order = session.scalar(
            select(ScmOrdenOperacion)
            .where(ScmOrdenOperacion.id == operation_order_id)
            .with_for_update()
        )
        if order is None or order.tipo != "FABRICACION" or order.fabricacion is None:
            raise ScmServiceError(
                "OF_NOT_FOUND",
                "La orden de fabricacion no existe.",
                status_code=404,
            )
        if order.estado != "BORRADOR":
            raise ScmServiceError(
                "INVALID_OF_STATE",
                "Solo una OF en borrador admite formulacion contextual.",
                status_code=409,
            )
        if order.version != version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OF fue modificada por otro usuario.",
                status_code=409,
                details={"expected": order.version, "received": version},
            )
        try:
            parsed_run_id = UUID(str(run_id))
        except (TypeError, ValueError) as error:
            raise ScmServiceError(
                "RUN_NOT_FOUND",
                "La corrida de fabricacion no existe.",
                status_code=404,
            ) from error
        run = session.scalar(
            select(ScmCorridaFabricacion)
            .where(
                ScmCorridaFabricacion.id == parsed_run_id,
                ScmCorridaFabricacion.orden_fabricacion_id == order.id,
            )
            .with_for_update(of=ScmCorridaFabricacion)
        )
        if run is None:
            raise ScmServiceError(
                "RUN_NOT_FOUND",
                "La corrida de fabricacion no pertenece a la OF.",
                status_code=404,
            )

        created_materials = []
        created_colors = []
        material_by_client = {}
        for raw_material in raw_new_materials:
            client_id, material_data = _material_payload(raw_material)
            if client_id in material_by_client:
                raise ScmServiceError(
                    "DUPLICATE_MATERIAL_CLIENT_ID",
                    "client_id debe ser unico dentro de materiales_nuevos.",
                    status_code=422,
                )
            material = create_material(
                session,
                actor_id=actor_id,
                data=material_data,
                commit=False,
            )
            material_id = material["id"]
            material_entity = session.get(ScmMaterial, material_id)
            material_by_client[client_id] = material_entity
            created_materials.append(material)

        raw_recipe = data.get("receta")
        if not isinstance(raw_recipe, dict):
            raise ScmServiceError(
                "RECIPE_REQUIRED",
                "receta debe ser un objeto JSON.",
                status_code=400,
            )
        reject_unknown_fields(
            raw_recipe,
            allowed={
                "variante",
                "nombre_variante",
                "alcance",
                "producto_sku",
                "base_virgen_kg",
                "notas",
                "es_default",
                "lineas",
            },
        )
        color = _resolve_color(
            session,
            data.get("color"),
            created=created_colors,
        )
        _validate_run_color_compatibility(session, run, color)
        lines = _normalize_recipe_lines(
            session,
            raw_recipe.get("lineas"),
            material_by_client,
            require_complete=action == ACTION_APPROVE,
        )
        recipe_data = {
            "color_produccion_id": color.id,
            "producto_sku": _recipe_scope(raw_recipe),
            "nombre_variante": raw_recipe.get("variante", raw_recipe.get("nombre_variante")),
            "base_virgen_kg": raw_recipe.get("base_virgen_kg", 25),
            "notas": raw_recipe.get("notas"),
            "es_default": raw_recipe.get("es_default", False),
            "estado": "APROBADA" if action == ACTION_APPROVE else "BORRADOR",
            "lineas": lines,
            "origen": "OF_CONTEXTUAL",
        }
        try:
            recipe = create_recipe(session, recipe_data, commit=False)
        except ColorRecipeError as error:
            raise ScmServiceError(
                error.code,
                error.message,
                status_code=error.status,
                details=error.details,
            ) from error
        if action == ACTION_APPROVE:
            _validated_approved_recipe(
                session,
                recipe["id"],
                color.id,
                outputs=run.salidas,
            )

        run.color_produccion_id = color.id
        run.color_produccion = color
        selected = action == ACTION_APPROVE
        if selected:
            run.receta_revision_id = recipe["id"]
            run.receta_revision = session.get(RecetaColorMaestra, recipe["id"])
        order.version += 1
        session.flush()
        response = {
            "receta": recipe,
            "orden_fabricacion": _serialize(session, order),
            "materiales_creados": created_materials,
            "color": _serialize_color(color),
            "seleccionada": selected,
        }
        operation.response_json = deepcopy(response)
        operation.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="ORDEN_FABRICACION",
            aggregate_id=str(order.id),
            tipo="OF_FORMULACION_CONTEXTUAL_GUARDADA",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
