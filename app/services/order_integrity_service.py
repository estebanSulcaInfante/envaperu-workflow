"""Validacion autoritativa del catalogo usado al crear una OP excepcional.

La orden congela la geometria de ``MoldePieza``. ``PiezaColor`` solo puede
usarse como referencia fisica cuando pertenece a esa pieza y al color del
lote; nunca se elige una variante arbitraria para completar el snapshot.
"""

from dataclasses import dataclass, field
import math

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.maquina import Maquina
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.orden import OrdenProduccion
from app.models.producto import (
    ColorProduccion,
    PiezaColor,
    ProductoPieza,
    ProductoTerminado,
)
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.catalog_classification_service import (
    ClassificationError,
    validate_linea_familia,
)


@dataclass(frozen=True)
class OrderIntegrityError(ValueError):
    """Error de dominio que la API puede convertir en una respuesta estable."""

    message: str
    code: str
    status: int = 400
    details: dict = field(default_factory=dict)

    def __str__(self):
        return self.message


@dataclass(frozen=True)
class OrderCreationContext:
    """Referencias ya validadas y valores canonicos para crear la orden."""

    maquina: Maquina
    molde: Molde | None
    producto: ProductoTerminado | None
    snapshot_rows: tuple[dict, ...]
    lot_color_ids: tuple[int | None, ...]
    pending_variants: tuple[dict, ...] = ()
    snapshot_tiempo_ciclo: float = 0.0
    snapshot_horas_turno: float = 24.0
    snapshot_peso_colada_gr: float = 0.0


@dataclass(frozen=True)
class OrderPrerequisiteContext:
    """Resultado no mutante para la prevalidacion interactiva del formulario."""

    molde: Molde
    producto: ProductoTerminado | None
    snapshot_rows: tuple[dict, ...]
    color_ids: tuple[int | None, ...]
    pending_variants: tuple[dict, ...]


def _error(message, code, status=400, **details):
    raise OrderIntegrityError(message, code, status, details)


def _positive_int(value, field_name, *, allow_zero=False):
    if isinstance(value, bool):
        _error(f"{field_name} debe ser un entero", "VALOR_INVALIDO", field=field_name)
    if isinstance(value, float) and not value.is_integer():
        _error(f"{field_name} debe ser un entero", "VALOR_INVALIDO", field=field_name)
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        _error(f"{field_name} debe ser un entero", "VALOR_INVALIDO", field=field_name)
    minimum = 0 if allow_zero else 1
    if normalized < minimum:
        qualifier = "mayor o igual a cero" if allow_zero else "mayor que cero"
        _error(
            f"{field_name} debe ser {qualifier}",
            "VALOR_INVALIDO",
            field=field_name,
        )
    return normalized


def _positive_float(value, field_name):
    if isinstance(value, bool):
        _error(f"{field_name} debe ser numerico", "VALOR_INVALIDO", field=field_name)
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        _error(f"{field_name} debe ser numerico", "VALOR_INVALIDO", field=field_name)
    if not math.isfinite(normalized) or normalized <= 0:
        _error(
            f"{field_name} debe ser mayor que cero",
            "VALOR_INVALIDO",
            field=field_name,
        )
    return normalized


def _nonnegative_float(value, field_name):
    if isinstance(value, bool):
        _error(f"{field_name} debe ser numerico", "VALOR_INVALIDO", field=field_name)
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        _error(f"{field_name} debe ser numerico", "VALOR_INVALIDO", field=field_name)
    if not math.isfinite(normalized) or normalized < 0:
        _error(
            f"{field_name} debe ser mayor o igual a cero",
            "VALOR_INVALIDO",
            field=field_name,
        )
    return normalized


def _validate_classification(*, pieza, pieza_color=None, session=None):
    active_session = session if session is not None else db.session
    if pieza.linea_id is None or pieza.familia_id is None:
        _error(
            f"La pieza {pieza.codigo} no tiene Linea y Familia completas",
            "PIEZA_SIN_CLASIFICACION",
            409,
            pieza_id=pieza.id,
            pieza_codigo=pieza.codigo,
        )
    try:
        validate_linea_familia(
            linea_id=pieza.linea_id,
            familia_id=pieza.familia_id,
            session=active_session,
        )
    except ClassificationError as exc:
        raise OrderIntegrityError(
            exc.message,
            exc.code,
            exc.status,
            {"pieza_id": pieza.id, "pieza_codigo": pieza.codigo},
        ) from exc

    if pieza_color is not None and (
        pieza_color.linea_id != pieza.linea_id
        or pieza_color.familia_id != pieza.familia_id
    ):
        _error(
            (
                f"El SKU {pieza_color.sku} contradice la clasificacion "
                f"de su pieza maestra {pieza.codigo}"
            ),
            "CLASIFICACION_PIEZA_DIVERGENTE",
            409,
            pieza_sku=pieza_color.sku,
            pieza_id=pieza.id,
            clasificacion_pieza={
                "linea_id": pieza.linea_id,
                "familia_id": pieza.familia_id,
            },
            clasificacion_sku={
                "linea_id": pieza_color.linea_id,
                "familia_id": pieza_color.familia_id,
            },
        )


def _validate_piece(pieza, *, session=None, pieza_color=None):
    if pieza is None:
        _error(
            "El SKU no esta vinculado a una pieza maestra",
            "PIEZA_COLOR_SIN_PIEZA",
            409,
            pieza_sku=pieza_color.sku if pieza_color else None,
        )
    if not pieza.activo:
        _error(
            f"La pieza {pieza.codigo} esta inactiva",
            "PIEZA_INACTIVA",
            409,
            pieza_id=pieza.id,
            pieza_codigo=pieza.codigo,
        )
    _validate_classification(
        pieza=pieza,
        pieza_color=pieza_color,
        session=session,
    )


def _resolve_colors(lotes, *, session=None):
    active_session = session if session is not None else db.session
    if lotes is None:
        lotes = []
    if not isinstance(lotes, list):
        _error("lotes debe ser una lista", "LOTES_INVALIDOS")

    resolved_ids = []
    supplied_count = 0
    for index, lote in enumerate(lotes):
        if not isinstance(lote, dict):
            _error(
                f"El lote {index + 1} debe ser un objeto",
                "LOTE_INVALIDO",
                lote_index=index,
            )
        _positive_float(lote.get("meta_kg"), f"lotes[{index}].meta_kg")
        _positive_int(lote.get("personas", 1), f"lotes[{index}].personas")
        raw_id = lote.get("color_id", lote.get("color_produccion_id"))
        if raw_id in (None, ""):
            resolved_ids.append(None)
            continue
        color_id = _positive_int(raw_id, f"lotes[{index}].color_id")
        color = active_session.get(ColorProduccion, color_id)
        if color is None:
            _error(
                f"El color de produccion {color_id} no existe",
                "COLOR_NO_ENCONTRADO",
                color_id=color_id,
                lote_index=index,
            )
        if color.familia_color_id is None or color.familia_color_rel is None:
            _error(
                f"El color de produccion {color_id} no tiene FamiliaColor",
                "COLOR_SIN_FAMILIA",
                409,
                color_id=color_id,
                lote_index=index,
            )
        supplied_count += 1
        resolved_ids.append(color_id)

    # Se toleran OPs legacy completamente sin color, pero no mezclar lotes
    # identificados con otros de texto libre: esa combinacion no es trazable.
    if supplied_count not in (0, len(resolved_ids)):
        missing = [index for index, value in enumerate(resolved_ids) if value is None]
        _error(
            "Todos los lotes deben seleccionar ColorProduccion cuando uno de ellos lo usa",
            "LOTE_COLOR_INCOMPLETO",
            missing_lote_indexes=missing,
        )
    return tuple(resolved_ids)


def _resolve_variant(
    *,
    pieza,
    color_id,
    session=None,
    create_missing=False,
    allow_missing=False,
    pending=None,
):
    active_session = session if session is not None else db.session
    variants = active_session.query(PiezaColor).filter_by(
        pieza_id=pieza.id,
        color_produccion_id=color_id,
    ).all()
    if not variants:
        missing = {
            "pieza_id": pieza.id,
            "pieza_codigo": pieza.codigo,
            "pieza_nombre": pieza.nombre,
            "color_id": color_id,
        }
        if pending is not None and missing not in pending:
            pending.append(missing)
        if create_missing:
            color = active_session.get(ColorProduccion, color_id)
            try:
                # El savepoint permite que dos OP concurrentes compitan por la
                # misma pareja sin invalidar toda la transaccion de la ganadora.
                with active_session.begin_nested():
                    variant = PiezaColor(
                        sku=generar_codigo_catalogo(
                            "PIEZA_COLOR",
                            session=active_session,
                        ),
                        piezas=f"{pieza.nombre} {color.nombre}",
                        tipo="SIMPLE",
                        pieza_id=pieza.id,
                        linea_id=pieza.linea_id,
                        familia_id=pieza.familia_id,
                        color_produccion_id=color_id,
                        peso=pieza.peso_nominal_gr,
                        estado_revision="EN_REVISION",
                    )
                    active_session.add(variant)
                    active_session.flush()
            except IntegrityError:
                # En READ COMMITTED, tras esperar la unicidad ya es visible la
                # variante confirmada por la transaccion competidora.
                variant = active_session.query(PiezaColor).filter_by(
                    pieza_id=pieza.id,
                    color_produccion_id=color_id,
                ).one_or_none()
                if variant is None:
                    raise
                _validate_piece(
                    pieza,
                    pieza_color=variant,
                    session=active_session,
                )
            missing["pieza_sku"] = variant.sku
            return variant
        if allow_missing:
            return None
        _error(
            (
                f"No existe una PiezaColor para {pieza.codigo} con el color "
                f"de produccion {color_id}"
            ),
            "PIEZA_COLOR_NO_CONFIGURADA",
            409,
            pieza_id=pieza.id,
            pieza_codigo=pieza.codigo,
            color_id=color_id,
        )
    if len(variants) > 1:
        _error(
            (
                f"La combinacion de {pieza.codigo} y color {color_id} tiene "
                "mas de un SKU"
            ),
            "PIEZA_COLOR_AMBIGUA",
            409,
            pieza_id=pieza.id,
            color_id=color_id,
            pieza_skus=sorted(item.sku for item in variants),
        )
    variant = variants[0]
    _validate_piece(
        pieza,
        pieza_color=variant,
        session=active_session,
    )
    return variant


def _active_mold_composition(molde, *, session=None):
    active_session = session if session is not None else db.session
    rows = active_session.query(MoldePieza).filter_by(
        molde_id=molde.codigo,
        activo=True,
    ).order_by(MoldePieza.id).all()
    if not rows:
        _error(
            f"El molde {molde.codigo} no tiene una composicion activa",
            "MOLDE_SIN_COMPOSICION",
            409,
            molde_id=molde.codigo,
        )
    for row in rows:
        _validate_piece(row.pieza, session=active_session)
        if row.cavidades is None or row.cavidades <= 0:
            _error(
                f"La composicion {row.id} tiene cavidades invalidas",
                "COMPOSICION_MOLDE_INVALIDA",
                409,
                molde_pieza_id=row.id,
            )
        if row.peso_unitario_gr is None or row.peso_unitario_gr <= 0:
            _error(
                f"La composicion {row.id} tiene peso unitario invalido",
                "COMPOSICION_MOLDE_INVALIDA",
                409,
                molde_pieza_id=row.id,
            )
    return rows


def _auto_snapshot(
    *,
    molde,
    color_ids,
    session=None,
    create_missing=False,
    allow_missing=False,
    pending=None,
):
    rows = _active_mold_composition(molde, session=session)
    return rows, tuple(
        {
            "pieza_id": row.pieza_id,
            "pieza_codigo_snapshot": row.pieza.codigo,
            "pieza_nombre_snapshot": row.pieza.nombre,
            "pieza_sku_legacy": None,
            "cavidades": row.cavidades,
            "peso_unit_gr": row.peso_unitario_gr,
            "molde_pieza_id": row.id,
        }
        for row in rows
    )


def _manual_snapshot(
    *,
    items,
    molde,
    color_ids,
    session=None,
    create_missing=False,
    allow_missing=False,
    pending=None,
):
    active_session = session if session is not None else db.session
    if not isinstance(items, list) or not items:
        _error("snapshot_composicion no puede estar vacio", "COMPOSICION_REQUERIDA")

    active_rows = _active_mold_composition(molde, session=active_session) if molde else []
    active_by_id = {row.id: row for row in active_rows}
    active_by_piece = {row.pieza_id: row for row in active_rows}
    used_composition_ids = set()
    snapshot_rows = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            _error(
                f"La fila {index + 1} de composicion debe ser un objeto",
                "COMPOSICION_INVALIDA",
                row_index=index,
            )
        cavidades = _positive_int(
            item.get("cavidades", 1),
            f"snapshot_composicion[{index}].cavidades",
        )
        peso = _positive_float(
            item.get("peso_unit_gr"),
            f"snapshot_composicion[{index}].peso_unit_gr",
        )

        relation = None
        pieza = None
        pieza_color = None
        raw_relation_id = item.get("molde_pieza_id", item.get("composicion_id"))
        raw_piece_id = item.get("pieza_id")
        raw_sku_new = item.get("pieza_sku_legacy")
        raw_sku_old = item.get("pieza_sku")
        if (
            raw_sku_new not in (None, "")
            and raw_sku_old not in (None, "")
            and str(raw_sku_new).strip() != str(raw_sku_old).strip()
        ):
            _error(
                "pieza_sku y pieza_sku_legacy no pueden identificar valores distintos",
                "PIEZA_SKU_LEGACY_DIVERGENTE",
                409,
                row_index=index,
            )
        raw_sku = (
            raw_sku_new
            if raw_sku_new not in (None, "")
            else raw_sku_old
        )
        raw_sku = str(raw_sku).strip() if raw_sku not in (None, "") else None

        if raw_relation_id not in (None, ""):
            relation_id = _positive_int(
                raw_relation_id,
                f"snapshot_composicion[{index}].molde_pieza_id",
            )
            relation = active_session.get(MoldePieza, relation_id)
            if relation is None:
                _error(
                    f"La composicion MoldePieza {relation_id} no existe",
                    "MOLDE_PIEZA_NO_ENCONTRADA",
                    row_index=index,
                    molde_pieza_id=relation_id,
                )
            if not relation.activo:
                _error(
                    f"La composicion MoldePieza {relation_id} esta inactiva",
                    "MOLDE_PIEZA_INACTIVA",
                    409,
                    row_index=index,
                    molde_pieza_id=relation_id,
                )
            pieza = relation.pieza

        if raw_piece_id not in (None, ""):
            piece_id = _positive_int(
                raw_piece_id,
                f"snapshot_composicion[{index}].pieza_id",
            )
            supplied_piece = active_session.get(Pieza, piece_id)
            if supplied_piece is None:
                _error(
                    f"La pieza {piece_id} no existe",
                    "PIEZA_NO_ENCONTRADA",
                    row_index=index,
                    pieza_id=piece_id,
                )
            if pieza is not None and pieza.id != supplied_piece.id:
                _error(
                    "molde_pieza_id y pieza_id identifican piezas distintas",
                    "COMPOSICION_DIVERGENTE",
                    409,
                    row_index=index,
                )
            pieza = supplied_piece

        if raw_sku is not None:
            pieza_color = active_session.get(PiezaColor, raw_sku)
            if pieza_color is None:
                _error(
                    f"El SKU PiezaColor {raw_sku} no existe",
                    "PIEZA_COLOR_NO_ENCONTRADA",
                    row_index=index,
                    pieza_sku=raw_sku,
                )
            if pieza is not None and pieza_color.pieza_id != pieza.id:
                _error(
                    "pieza_sku y la pieza/composicion seleccionada no corresponden",
                    "PIEZA_COLOR_DIVERGENTE",
                    409,
                    row_index=index,
                    pieza_sku=raw_sku,
                    pieza_id=pieza.id,
                )
            pieza = pieza_color.pieza_rel

        if molde is not None:
            if pieza is None:
                _error(
                    (
                        "Cada fila manual de un molde catalogado debe indicar "
                        "molde_pieza_id, pieza_id o un pieza_sku valido"
                    ),
                    "COMPOSICION_PIEZA_REQUERIDA",
                    row_index=index,
                    molde_id=molde.codigo,
                )
            canonical_relation = active_by_piece.get(pieza.id)
            if canonical_relation is None:
                _error(
                    f"La pieza {pieza.codigo} no integra activamente el molde {molde.codigo}",
                    "PIEZA_NO_PERTENECE_MOLDE",
                    409,
                    row_index=index,
                    molde_id=molde.codigo,
                    pieza_id=pieza.id,
                    pieza_sku=raw_sku,
                )
            if relation is not None and relation.id != canonical_relation.id:
                _error(
                    f"La composicion {relation.id} no pertenece al molde {molde.codigo}",
                    "MOLDE_PIEZA_DIVERGENTE",
                    409,
                    row_index=index,
                    molde_id=molde.codigo,
                    molde_pieza_id=relation.id,
                )
            relation = canonical_relation
            if relation.id in used_composition_ids:
                _error(
                    f"La pieza {pieza.codigo} esta repetida en el snapshot",
                    "COMPOSICION_DUPLICADA",
                    409,
                    row_index=index,
                    pieza_id=pieza.id,
                )
            used_composition_ids.add(relation.id)

        if relation is not None and (
            cavidades != relation.cavidades
            or not math.isclose(
                peso,
                float(relation.peso_unitario_gr),
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            _error(
                (
                    f"La fila {index + 1} no coincide con los valores vigentes "
                    f"de MoldePieza {relation.id}"
                ),
                "COMPOSICION_MOLDE_VALORES_DIVERGENTES",
                409,
                row_index=index,
                molde_pieza_id=relation.id,
                esperado={
                    "cavidades": relation.cavidades,
                    "peso_unit_gr": relation.peso_unitario_gr,
                },
                recibido={
                    "cavidades": cavidades,
                    "peso_unit_gr": peso,
                },
            )

        if pieza is not None:
            _validate_piece(
                pieza,
                pieza_color=pieza_color,
                session=active_session,
            )

        supplied_code_snapshot = str(
            item.get("pieza_codigo_snapshot") or ""
        ).strip()
        supplied_name_snapshot = str(
            item.get("pieza_nombre_snapshot") or item.get("pieza_nombre") or ""
        ).strip()
        piece_code_snapshot = (
            pieza.codigo
            if pieza is not None
            else supplied_code_snapshot or raw_sku or f"LEGACY-SIN-PIEZA-{index + 1}"
        )
        piece_name_snapshot = (
            pieza.nombre
            if pieza is not None
            else supplied_name_snapshot or piece_code_snapshot
        )

        snapshot_rows.append({
            "pieza_id": pieza.id if pieza is not None else None,
            "pieza_codigo_snapshot": piece_code_snapshot,
            "pieza_nombre_snapshot": piece_name_snapshot,
            "pieza_sku_legacy": raw_sku,
            "cavidades": cavidades,
            "peso_unit_gr": peso,
            "molde_pieza_id": relation.id if relation is not None else None,
        })

    if molde is not None:
        missing = [
            row for row in active_rows if row.id not in used_composition_ids
        ]
        if missing:
            _error(
                "El snapshot manual debe incluir todas las piezas activas del molde",
                "COMPOSICION_MOLDE_INCOMPLETA",
                409,
                molde_id=molde.codigo,
                piezas_faltantes=[
                    {
                        "molde_pieza_id": row.id,
                        "pieza_id": row.pieza_id,
                        "pieza_codigo": row.pieza.codigo,
                    }
                    for row in missing
                ],
            )

    return active_rows, tuple(snapshot_rows)


def _validate_product(
    *,
    producto,
    molde,
    snapshot_rows,
    composition_rows,
    color_ids,
    session=None,
    create_missing=False,
    allow_missing=False,
    pending=None,
):
    if producto is None:
        return
    active_session = session if session is not None else db.session
    try:
        validate_linea_familia(
            linea_id=producto.linea_id,
            familia_id=producto.familia_id,
            session=active_session,
        )
    except ClassificationError as exc:
        raise OrderIntegrityError(
            exc.message,
            exc.code,
            exc.status,
            {"producto_sku": producto.cod_sku_pt},
        ) from exc

    bom_rows = active_session.query(ProductoPieza).filter_by(
        producto_terminado_id=producto.cod_sku_pt,
    ).all()
    if not bom_rows:
        _error(
            f"El producto {producto.cod_sku_pt} no tiene una BOM de PiezaColor",
            "PRODUCTO_SIN_BOM",
            409,
            producto_sku=producto.cod_sku_pt,
        )
    bom_piece_ids = set()
    for row in bom_rows:
        if row.cantidad is None or row.cantidad <= 0:
            _error(
                f"La BOM del producto {producto.cod_sku_pt} tiene una cantidad invalida",
                "PRODUCTO_BOM_INVALIDA",
                409,
                producto_sku=producto.cod_sku_pt,
                producto_pieza_id=row.id,
                cantidad=row.cantidad,
            )
        variant = row.pieza
        _validate_piece(
            variant.pieza_rel if variant else None,
            pieza_color=variant,
            session=active_session,
        )
        bom_piece_ids.add(variant.pieza_id)

    selected_piece_ids = {
        row.pieza_id for row in composition_rows
    } or {
        row["pieza_id"] for row in snapshot_rows if row.get("pieza_id") is not None
    }
    if not selected_piece_ids:
        _error(
            "No se puede validar un ProductoTerminado contra una composicion sin piezas catalogadas",
            "PRODUCTO_COMPOSICION_AMBIGUA",
            409,
            producto_sku=producto.cod_sku_pt,
        )
    missing_in_mold = sorted(bom_piece_ids - selected_piece_ids)
    extra_in_mold = sorted(selected_piece_ids - bom_piece_ids)
    if missing_in_mold or extra_in_mold:
        composition_label = (
            f"del molde {molde.codigo}"
            if molde is not None
            else "del snapshot manual"
        )
        _error(
            (
                f"La BOM del producto {producto.cod_sku_pt} y la composicion "
                f"{composition_label} identifican piezas distintas"
            ),
            "PRODUCTO_MOLDE_INCOMPATIBLE",
            409,
            producto_sku=producto.cod_sku_pt,
            molde_id=molde.codigo if molde is not None else None,
            piezas_bom=sorted(bom_piece_ids),
            piezas_molde=sorted(selected_piece_ids),
            piezas_bom_faltantes_en_molde=missing_in_mold,
            piezas_molde_fuera_bom=extra_in_mold,
        )


def validate_order_header_prerequisites(
    *,
    numero_op=None,
    maquina_id=None,
    session=None,
    require_values=False,
):
    """Valida identidad y maquina; admite campos aun vacios en prevalidacion."""

    active_session = session if session is not None else db.session
    normalized_order = str(numero_op or "").strip()
    if require_values and not normalized_order:
        _error("Numero de OP requerido", "NUMERO_OP_REQUERIDO")
    if normalized_order and active_session.get(OrdenProduccion, normalized_order) is not None:
        _error(
            f"La orden {normalized_order} ya existe",
            "ORDEN_YA_EXISTE",
            409,
            numero_op=normalized_order,
        )

    if maquina_id in (None, ""):
        if require_values:
            _error("maquina_id requerido", "MAQUINA_REQUERIDA")
        return None
    normalized_machine_id = _positive_int(maquina_id, "maquina_id")
    maquina = active_session.get(Maquina, normalized_machine_id)
    if maquina is None:
        _error(
            f"La maquina {normalized_machine_id} no existe",
            "MAQUINA_NO_ENCONTRADA",
            maquina_id=normalized_machine_id,
        )
    if not maquina.activo:
        _error(
            f"La maquina {maquina.codigo} esta inactiva",
            "MAQUINA_INACTIVA",
            409,
            maquina_id=maquina.id,
        )
    if str(maquina.estado or "").strip().upper() != "OPERATIVA":
        _error(
            f"La maquina {maquina.codigo} no esta OPERATIVA",
            "MAQUINA_NO_OPERATIVA",
            409,
            maquina_id=maquina.id,
            estado=maquina.estado,
        )
    return maquina


def validate_order_creation(
    data,
    *,
    session=None,
    create_missing_variants=True,
    allow_missing_variants=False,
):
    """Valida un payload de ``POST /ordenes`` antes de abrir la transaccion."""

    active_session = session if session is not None else db.session
    maquina = validate_order_header_prerequisites(
        numero_op=data.get("numero_op"),
        maquina_id=data.get("maquina_id"),
        session=active_session,
        require_values=True,
    )

    molde = None
    molde_id = str(data.get("molde_id") or "").strip()
    if molde_id:
        molde = active_session.get(Molde, molde_id)
        if molde is None:
            _error(
                f"El molde {molde_id} no existe",
                "MOLDE_NO_ENCONTRADO",
                molde_id=molde_id,
            )
        if not molde.activo:
            _error(
                f"El molde {molde.codigo} esta inactivo",
                "MOLDE_INACTIVO",
                409,
                molde_id=molde.codigo,
            )

    producto = None
    producto_sku = str(data.get("producto_sku") or "").strip()
    if producto_sku:
        producto = active_session.get(ProductoTerminado, producto_sku)
        if producto is None:
            _error(
                f"El producto {producto_sku} no existe",
                "PRODUCTO_NO_ENCONTRADO",
                producto_sku=producto_sku,
            )

    default_cycle = molde.tiempo_ciclo_std if molde is not None else None
    snapshot_tiempo_ciclo = _positive_float(
        data.get("snapshot_tiempo_ciclo", default_cycle),
        "snapshot_tiempo_ciclo",
    )
    snapshot_horas_turno = _positive_float(
        data.get("snapshot_horas_turno", 24.0),
        "snapshot_horas_turno",
    )
    snapshot_peso_colada_gr = _nonnegative_float(
        data.get("snapshot_peso_colada_gr", 0.0),
        "snapshot_peso_colada_gr",
    )

    color_ids = _resolve_colors(data.get("lotes", []), session=active_session)
    pending_variants = []
    auto_snapshot = data.get("auto_snapshot_molde", False)
    if not isinstance(auto_snapshot, bool):
        _error("auto_snapshot_molde debe ser booleano", "MODO_SNAPSHOT_INVALIDO")
    manual_items = data.get("snapshot_composicion", [])
    if auto_snapshot and manual_items:
        _error(
            "No se puede combinar auto_snapshot_molde con snapshot_composicion manual",
            "MODO_SNAPSHOT_AMBIGUO",
        )
    if auto_snapshot:
        if molde is None:
            _error(
                "molde_id requerido para auto_snapshot_molde",
                "MOLDE_REQUERIDO",
            )
        composition_rows, snapshot_rows = _auto_snapshot(
            molde=molde,
            color_ids=color_ids,
            session=active_session,
            create_missing=create_missing_variants,
            allow_missing=allow_missing_variants,
            pending=pending_variants,
        )
    else:
        composition_rows, snapshot_rows = _manual_snapshot(
            items=manual_items,
            molde=molde,
            color_ids=color_ids,
            session=active_session,
            create_missing=create_missing_variants,
            allow_missing=allow_missing_variants,
            pending=pending_variants,
        )

    # Independientemente de si el SKU cabe en el snapshot legacy, cada color
    # identificado debe tener una variante no ambigua para cada pieza catalogada.
    distinct_colors = sorted({value for value in color_ids if value is not None})
    selected_pieces = {
        row.pieza_id: row.pieza for row in composition_rows
    }
    for row in snapshot_rows:
        if row.get("pieza_id") is not None and row["pieza_id"] not in selected_pieces:
            selected_pieces[row["pieza_id"]] = active_session.get(Pieza, row["pieza_id"])
    for pieza in selected_pieces.values():
        for color_id in distinct_colors:
            _resolve_variant(
                pieza=pieza,
                color_id=color_id,
                session=active_session,
                create_missing=create_missing_variants,
                allow_missing=allow_missing_variants,
                pending=pending_variants,
            )

    _validate_product(
        producto=producto,
        molde=molde,
        snapshot_rows=snapshot_rows,
        composition_rows=composition_rows,
        color_ids=color_ids,
        session=active_session,
        create_missing=create_missing_variants,
        allow_missing=allow_missing_variants,
        pending=pending_variants,
    )
    return OrderCreationContext(
        maquina=maquina,
        molde=molde,
        producto=producto,
        snapshot_rows=snapshot_rows,
        lot_color_ids=color_ids,
        pending_variants=tuple(pending_variants),
        snapshot_tiempo_ciclo=snapshot_tiempo_ciclo,
        snapshot_horas_turno=snapshot_horas_turno,
        snapshot_peso_colada_gr=snapshot_peso_colada_gr,
    )


def validate_order_prerequisites(
    *,
    molde_id,
    color_ids=(),
    producto_sku=None,
    session=None,
):
    """Prevalida el subconjunto de catalogo sin reservar codigos ni escribir.

    Las combinaciones Pieza x Color que aun no existen son validas para una OP
    directa: ``POST /ordenes`` las crea en su propia transaccion. Se reportan en
    ``pending_variants`` para que el formulario explique ese efecto. Si se
    selecciona un ProductoTerminado, su BOM si debe contener las salidas exactas
    y una variante ausente vuelve incompatible esa seleccion.
    """

    active_session = session if session is not None else db.session
    normalized_mold_id = str(molde_id or "").strip()
    if not normalized_mold_id:
        _error("molde_id requerido", "MOLDE_REQUERIDO")
    molde = active_session.get(Molde, normalized_mold_id)
    if molde is None:
        _error(
            f"El molde {normalized_mold_id} no existe",
            "MOLDE_NO_ENCONTRADO",
            molde_id=normalized_mold_id,
        )
    if not molde.activo:
        _error(
            f"El molde {molde.codigo} esta inactivo",
            "MOLDE_INACTIVO",
            409,
            molde_id=molde.codigo,
        )

    normalized_product_sku = str(producto_sku or "").strip()
    producto = None
    if normalized_product_sku:
        producto = active_session.get(ProductoTerminado, normalized_product_sku)
        if producto is None:
            _error(
                f"El producto {normalized_product_sku} no existe",
                "PRODUCTO_NO_ENCONTRADO",
                producto_sku=normalized_product_sku,
            )

    lot_payloads = [
        {"color_id": value, "meta_kg": 1, "personas": 1}
        for value in (color_ids or ())
    ]
    normalized_colors = _resolve_colors(lot_payloads, session=active_session)
    pending_variants = []
    composition_rows, snapshot_rows = _auto_snapshot(
        molde=molde,
        color_ids=normalized_colors,
        session=active_session,
        create_missing=False,
        allow_missing=True,
        pending=pending_variants,
    )
    for row in composition_rows:
        for color_id in sorted({item for item in normalized_colors if item is not None}):
            _resolve_variant(
                pieza=row.pieza,
                color_id=color_id,
                session=active_session,
                create_missing=False,
                allow_missing=True,
                pending=pending_variants,
            )

    _validate_product(
        producto=producto,
        molde=molde,
        snapshot_rows=snapshot_rows,
        composition_rows=composition_rows,
        color_ids=normalized_colors,
        session=active_session,
        create_missing=False,
        allow_missing=True,
        pending=pending_variants,
    )
    return OrderPrerequisiteContext(
        molde=molde,
        producto=producto,
        snapshot_rows=snapshot_rows,
        color_ids=normalized_colors,
        pending_variants=tuple(pending_variants),
    )
