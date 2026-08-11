from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.models.producto import ColorProduccion, ProductoTerminado
from app.models.receta_color import RecetaColorLinea, RecetaColorMaestra
from app.models.scm_catalogos import ScmMaterial


HEX_PATTERN = re.compile(r"^#[0-9A-F]{6}$")
RECIPE_STATES = {"BORRADOR", "APROBADA", "INACTIVA"}
COMPONENT_TYPES = {"MATERIA_PRIMA", "COLORANTE", "ADITIVO"}


class ColorRecipeError(ValueError):
    def __init__(self, message, *, code="RECETA_INVALIDA", status=400, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}


def normalize_hex(value):
    if value in (None, ""):
        return None
    normalized = str(value).strip().upper()
    if not HEX_PATTERN.fullmatch(normalized):
        raise ColorRecipeError(
            "El HEX de referencia debe usar el formato #RRGGBB.",
            code="HEX_REFERENCIA_INVALIDO",
            details={"field": "hex_referencia"},
        )
    return normalized


def _positive_decimal(value, field):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ColorRecipeError(
            f"{field} debe ser un número positivo.",
            details={"field": field},
        )
    if parsed <= 0:
        raise ColorRecipeError(
            f"{field} debe ser mayor que cero.",
            details={"field": field},
        )
    return parsed


def _required_text(value, field, max_length):
    normalized = " ".join(str(value or "").strip().split())
    if not normalized:
        raise ColorRecipeError(
            f"{field} es requerido.",
            details={"field": field},
        )
    if len(normalized) > max_length:
        raise ColorRecipeError(
            f"{field} excede {max_length} caracteres.",
            details={"field": field},
        )
    return normalized


def _scope_product(session, producto_sku):
    normalized = str(producto_sku or "").strip() or None
    if normalized and session.get(ProductoTerminado, normalized) is None:
        raise ColorRecipeError(
            f"ProductoTerminado {normalized} no encontrado.",
            code="PRODUCTO_NO_ENCONTRADO",
            status=404,
        )
    return normalized, normalized or "*"


def serialize_recipe(recipe):
    return {
        "id": recipe.id,
        "color_produccion_id": recipe.color_produccion_id,
        "color_nombre": recipe.color_produccion.nombre if recipe.color_produccion else None,
        "color_hex": recipe.color_produccion.hex_referencia if recipe.color_produccion else None,
        "producto_sku": recipe.producto_sku,
        "nombre_variante": recipe.nombre_variante,
        "revision": recipe.revision,
        "estado": recipe.estado,
        "es_default": recipe.es_default,
        "base_virgen_kg": float(recipe.base_virgen_kg),
        "notas": recipe.notas,
        "origen": recipe.origen,
        "version": recipe.version,
        "creado_en": recipe.creado_en.isoformat() if recipe.creado_en else None,
        "actualizado_en": recipe.actualizado_en.isoformat() if recipe.actualizado_en else None,
        "lineas": [
            {
                "id": line.id,
                "material_id": line.material_id,
                "material_codigo": line.material.codigo if line.material else None,
                "material_nombre": line.material.nombre if line.material else None,
                "material_clase": line.material.clase if line.material else None,
                "tipo_componente": line.tipo_componente,
                "cantidad": float(line.cantidad),
                "unidad": line.unidad,
                "base_kg": float(line.base_kg) if line.base_kg is not None else None,
                "orden": line.orden,
            }
            for line in recipe.lineas
        ],
    }


def list_recipe_ingredients(session, *, include_inactive=False):
    query = ScmMaterial.query.order_by(ScmMaterial.clase, ScmMaterial.nombre)
    if not include_inactive:
        query = query.filter(ScmMaterial.activo.is_(True))
    return [
        {
            **material.to_dict(),
            "tipo_colorante": (
                material.colorante.tipo
                if material.clase == "COLORANTE" and material.colorante
                else None
            ),
        }
        for material in query.all()
    ]


def list_recipes(session, *, color_produccion_id=None, include_inactive=False):
    query = RecetaColorMaestra.query
    if color_produccion_id is not None:
        query = query.filter_by(color_produccion_id=color_produccion_id)
    if not include_inactive:
        query = query.filter(RecetaColorMaestra.estado != "INACTIVA")
    return [
        serialize_recipe(item)
        for item in query.order_by(
            RecetaColorMaestra.color_produccion_id,
            RecetaColorMaestra.nombre_variante,
            RecetaColorMaestra.revision.desc(),
        ).all()
    ]


def _normalize_lines(session, raw_lines, *, default_base_kg, approved):
    if raw_lines is None:
        raw_lines = []
    if not isinstance(raw_lines, list):
        raise ColorRecipeError("lineas debe ser una lista.", details={"field": "lineas"})

    normalized = []
    seen = set()
    material_fraction = Decimal("0")
    for index, raw in enumerate(raw_lines):
        if not isinstance(raw, dict):
            raise ColorRecipeError(
                "Cada línea de receta debe ser un objeto.",
                details={"field": f"lineas[{index}]"},
            )
        try:
            material_id = int(raw.get("material_id"))
        except (TypeError, ValueError):
            raise ColorRecipeError(
                "material_id debe ser un entero positivo.",
                details={"field": f"lineas[{index}].material_id"},
            )
        material = session.get(ScmMaterial, material_id)
        if material is None:
            raise ColorRecipeError(
                f"Material {material_id} no encontrado.",
                code="MATERIAL_NO_ENCONTRADO",
                status=404,
                details={"field": f"lineas[{index}].material_id"},
            )

        component_type = str(raw.get("tipo_componente") or "").strip().upper()
        if component_type not in COMPONENT_TYPES:
            raise ColorRecipeError(
                "tipo_componente no es válido.",
                details={"field": f"lineas[{index}].tipo_componente"},
            )
        expected_class = "MATERIA_PRIMA" if component_type == "MATERIA_PRIMA" else "COLORANTE"
        if material.clase != expected_class:
            raise ColorRecipeError(
                f"{material.nombre} no puede usarse como {component_type}.",
                code="CLASE_MATERIAL_INCOMPATIBLE",
                details={"material_id": material_id, "tipo_componente": component_type},
            )
        key = (material_id, component_type)
        if key in seen:
            raise ColorRecipeError(
                f"El material {material.nombre} está duplicado en la receta.",
                code="MATERIAL_RECETA_DUPLICADO",
            )
        seen.add(key)

        quantity = _positive_decimal(raw.get("cantidad"), f"lineas[{index}].cantidad")
        if component_type == "MATERIA_PRIMA":
            if quantity > 1:
                raise ColorRecipeError(
                    "La fracción de materia prima no puede ser mayor que 1.",
                    details={"field": f"lineas[{index}].cantidad"},
                )
            unit = "FRACCION"
            base_kg = None
            material_fraction += quantity
        else:
            unit = "GRAMOS"
            base_kg = _positive_decimal(
                raw.get("base_kg", default_base_kg),
                f"lineas[{index}].base_kg",
            )

        normalized.append({
            "material_id": material_id,
            "tipo_componente": component_type,
            "cantidad": quantity,
            "unidad": unit,
            "base_kg": base_kg,
            "orden": index,
        })

    if approved:
        if not normalized:
            raise ColorRecipeError(
                "Una receta aprobada debe tener al menos una línea.",
                code="RECETA_SIN_COMPONENTES",
            )
        if material_fraction != Decimal("1"):
            raise ColorRecipeError(
                "Las fracciones de materia prima de una receta aprobada deben sumar 1.",
                code="FRACCIONES_RECETA_INVALIDAS",
                details={"suma": float(material_fraction)},
            )
    return normalized


def _clear_scope_default(session, *, color_id, product_scope, except_id=None):
    query = RecetaColorMaestra.query.filter_by(
        color_produccion_id=color_id,
        producto_scope=product_scope,
        estado="APROBADA",
        es_default=True,
    )
    if except_id is not None:
        query = query.filter(RecetaColorMaestra.id != except_id)
    for item in query.all():
        item.es_default = False
        item.version += 1


def _next_revision(session, *, color_id, product_scope, variant_name):
    current = session.query(func.max(RecetaColorMaestra.revision)).filter_by(
        color_produccion_id=color_id,
        producto_scope=product_scope,
        nombre_variante=variant_name,
    ).scalar()
    return (current or 0) + 1


def _apply_lines(recipe, normalized_lines):
    recipe.lineas = [RecetaColorLinea(**line) for line in normalized_lines]


def create_recipe(session, data, *, forced_revision=None, commit=True):
    data = data or {}
    try:
        color_id = int(data.get("color_produccion_id"))
    except (TypeError, ValueError):
        raise ColorRecipeError("color_produccion_id es requerido.")
    color = session.get(ColorProduccion, color_id)
    if color is None:
        raise ColorRecipeError(
            f"ColorProduccion {color_id} no encontrado.",
            code="COLOR_NO_ENCONTRADO",
            status=404,
        )

    product_sku, product_scope = _scope_product(session, data.get("producto_sku"))
    variant_name = _required_text(data.get("nombre_variante"), "nombre_variante", 120)
    state = str(data.get("estado") or "BORRADOR").strip().upper()
    if state not in RECIPE_STATES:
        raise ColorRecipeError("estado de receta no válido.", details={"field": "estado"})
    is_default = bool(data.get("es_default", False))
    if is_default and state != "APROBADA":
        raise ColorRecipeError(
            "Solo una receta aprobada puede ser predeterminada.",
            code="RECETA_DEFAULT_NO_APROBADA",
        )
    base_kg = _positive_decimal(data.get("base_virgen_kg", 25), "base_virgen_kg")
    lines = _normalize_lines(
        session,
        data.get("lineas"),
        default_base_kg=base_kg,
        approved=state == "APROBADA",
    )
    revision = forced_revision or _next_revision(
        session,
        color_id=color_id,
        product_scope=product_scope,
        variant_name=variant_name,
    )
    recipe = RecetaColorMaestra(
        color_produccion_id=color_id,
        producto_sku=product_sku,
        producto_scope=product_scope,
        nombre_variante=variant_name,
        revision=revision,
        estado=state,
        es_default=is_default,
        base_virgen_kg=base_kg,
        notas=(str(data.get("notas") or "").strip() or None),
        origen=str(data.get("origen") or "MANUAL").strip().upper(),
    )
    _apply_lines(recipe, lines)
    if is_default:
        _clear_scope_default(session, color_id=color_id, product_scope=product_scope)
    try:
        if commit:
            session.add(recipe)
            session.flush()
            session.commit()
        else:
            # Savepoint: a conflict must not erase units already applied by
            # the resumable product-onboarding command.
            with session.begin_nested():
                session.add(recipe)
                session.flush()
    except IntegrityError as exc:
        if commit:
            session.rollback()
        raise ColorRecipeError(
            "La revisión o receta predeterminada entra en conflicto con otra existente.",
            code="RECETA_CONFLICTO",
            status=409,
        ) from exc
    return serialize_recipe(recipe)


def _recipe_or_404(session, recipe_id):
    recipe = session.get(RecetaColorMaestra, recipe_id)
    if recipe is None:
        raise ColorRecipeError(
            f"Receta {recipe_id} no encontrada.",
            code="RECETA_NO_ENCONTRADA",
            status=404,
        )
    return recipe


def update_recipe(session, recipe_id, data):
    recipe = _recipe_or_404(session, recipe_id)
    data = data or {}
    try:
        expected_version = int(data.get("version"))
    except (TypeError, ValueError):
        raise ColorRecipeError("version es requerida para editar la receta.")
    if expected_version != recipe.version:
        raise ColorRecipeError(
            "La receta cambió; recargue antes de guardar.",
            code="RECETA_VERSION_CONFLICTO",
            status=409,
        )

    current_payload = serialize_recipe(recipe)
    merged = {
        "color_produccion_id": data.get("color_produccion_id", recipe.color_produccion_id),
        "producto_sku": data.get("producto_sku", recipe.producto_sku),
        "nombre_variante": data.get("nombre_variante", recipe.nombre_variante),
        "estado": data.get("estado", recipe.estado),
        "es_default": data.get("es_default", recipe.es_default),
        "base_virgen_kg": data.get("base_virgen_kg", float(recipe.base_virgen_kg)),
        "notas": data.get("notas", recipe.notas),
        "origen": recipe.origen,
        "lineas": data.get("lineas", current_payload["lineas"]),
    }

    if recipe.estado in {"APROBADA", "INACTIVA"}:
        recipe.estado = "INACTIVA"
        recipe.es_default = False
        recipe.version += 1
        result = create_recipe(session, merged, commit=False)
        session.commit()
        return {**result, "reemplaza_receta_id": recipe.id}

    product_sku, product_scope = _scope_product(session, merged["producto_sku"])
    variant_name = _required_text(merged["nombre_variante"], "nombre_variante", 120)
    state = str(merged["estado"] or "BORRADOR").strip().upper()
    if state not in RECIPE_STATES:
        raise ColorRecipeError("estado de receta no válido.")
    is_default = bool(merged["es_default"])
    if is_default and state != "APROBADA":
        raise ColorRecipeError("Solo una receta aprobada puede ser predeterminada.")
    base_kg = _positive_decimal(merged["base_virgen_kg"], "base_virgen_kg")
    lines = _normalize_lines(
        session,
        merged["lineas"],
        default_base_kg=base_kg,
        approved=state == "APROBADA",
    )
    if is_default:
        _clear_scope_default(
            session,
            color_id=int(merged["color_produccion_id"]),
            product_scope=product_scope,
            except_id=recipe.id,
        )
    recipe.color_produccion_id = int(merged["color_produccion_id"])
    recipe.producto_sku = product_sku
    recipe.producto_scope = product_scope
    recipe.nombre_variante = variant_name
    recipe.estado = state
    recipe.es_default = is_default
    recipe.base_virgen_kg = base_kg
    recipe.notas = str(merged["notas"] or "").strip() or None
    recipe.version += 1
    try:
        # A draft keeps the same recipe identity. Delete and flush its previous
        # children before inserting replacements so the unique material key is
        # never occupied by both generations in the same SQLAlchemy flush.
        recipe.lineas.clear()
        session.flush()
        _apply_lines(recipe, lines)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ColorRecipeError(
            "La receta entra en conflicto con otra revisión o componente existente.",
            code="RECETA_CONFLICTO",
            status=409,
        ) from exc
    return serialize_recipe(recipe)


def deactivate_recipe(session, recipe_id, *, version):
    recipe = _recipe_or_404(session, recipe_id)
    try:
        expected_version = int(version)
    except (TypeError, ValueError):
        raise ColorRecipeError("version es requerida para inactivar la receta.")
    if expected_version != recipe.version:
        raise ColorRecipeError(
            "La receta cambió; recargue antes de inactivarla.",
            code="RECETA_VERSION_CONFLICTO",
            status=409,
        )
    recipe.estado = "INACTIVA"
    recipe.es_default = False
    recipe.version += 1
    session.commit()
    return serialize_recipe(recipe)


def find_default_recipe(session, *, color_produccion_id, producto_sku=None):
    scopes = [producto_sku, "*"] if producto_sku else ["*"]
    for scope in scopes:
        recipe = RecetaColorMaestra.query.filter_by(
            color_produccion_id=color_produccion_id,
            producto_scope=scope,
            estado="APROBADA",
            es_default=True,
        ).first()
        if recipe is not None:
            return recipe
    return None
