"""Services for canonical fabrication orders introduced by TS-010P."""

import copy
from app.services.scm_draft_order_annulment import (
    annulment_summary,
    replacement_summary,
)
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.maquina import Maquina
from app.models.molde import Molde, MoldePieza
from app.models.producto import ColorProduccion
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_articulos import (
    CLASE_PRODUCTO_TERMINADO,
    ScmArticulo,
)
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_estructuras import ScmEstructuraComponente
from app.models.scm_inline_wip import (
    ScmReservaWipSalida,
    ScmSaldoWipSalida,
)
from app.models.scm_ot import ScmCierreProductivoKg, ScmLoteArticulo, ScmManga, ScmTrabajoOt
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    utc_now,
)
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_color_identity import serialize_color_identity
from app.services.color_recipe_service import serialize_recipe
from app.services.scm_operation_schedule_projection import (
    operation_schedule_projection,
    operation_schedule_projections,
)
from app.services.scm_production_order_service import (
    _iso,
    _reserve_operation,
)
from app.services.scm_fulfillment_service import (
    credit_output_allocations,
    project_production_orders_for_operation,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)
from app.services.scm_process_resolution import (
    normalize_process,
    resolve_order_process,
    resolve_route_operation,
    route_operation_dto,
    route_snapshot,
)
from app.services.scm_route_service import _content_hash as _route_content_hash


def _positive_decimal(value, field, *, integral=False, allow_zero=False):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} contiene una cantidad invalida.",
            status_code=422,
            details={"field": field},
        ) from error
    minimum_ok = parsed >= 0 if allow_zero else parsed > 0
    if (
        not parsed.is_finite()
        or not minimum_ok
        or (integral and parsed != parsed.to_integral_value())
    ):
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} contiene una cantidad invalida.",
            status_code=422,
            details={"field": field},
        )
    return parsed


def _run_cycles(
    *,
    minimum_cycles,
    explicit_cycles,
    objective_neto_kg,
    kg_neto_por_ciclo,
):
    candidates = [int(minimum_cycles)]
    if explicit_cycles is not None:
        candidates.append(int(_positive_decimal(
            explicit_cycles,
            "ciclos_objetivo",
            integral=True,
        )))
    if objective_neto_kg is not None:
        if kg_neto_por_ciclo is None or kg_neto_por_ciclo <= 0:
            raise ScmServiceError(
                "OF_NET_TARGET_DATA_REQUIRED",
                "El objetivo neto requiere cantidad por ciclo y peso unitario.",
                status_code=422,
            )
        candidates.append(int((
            objective_neto_kg / kg_neto_por_ciclo
        ).to_integral_value(rounding=ROUND_CEILING)))
    return max(candidates)


def _run_net_projection(run):
    snapshots = [
        output for output in run.salidas
        if output.cantidad_por_ciclo_snapshot is not None
        and output.peso_unitario_snapshot_g is not None
    ]
    if not snapshots:
        return None, None, None
    kg_neto_por_ciclo = sum(
        (
            Decimal(output.cantidad_por_ciclo_snapshot)
            * Decimal(output.peso_unitario_snapshot_g)
            / Decimal("1000")
            for output in snapshots
        ),
        Decimal("0"),
    )
    alcanzable = (
        kg_neto_por_ciclo * Decimal(run.ciclos_objetivo)
        if run.ciclos_objetivo is not None and kg_neto_por_ciclo > 0
        else None
    )
    redondeo = (
        alcanzable - Decimal(run.objetivo_neto_kg)
        if alcanzable is not None and run.objetivo_neto_kg is not None
        else None
    )
    return kg_neto_por_ciclo, alcanzable, redondeo


def _decimal_text(value, scale):
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-scale)
    return format(Decimal(value).quantize(quantum), "f")


def _output_piece_color(session, output):
    """Resolve the physical mold piece behind an operational output.

    A PieceColor is direct. A terminal PT can only inherit mold data when its
    frozen demand structure proves it is exactly one unit of one PieceColor.
    Multi-piece PTs must keep separate physical outputs and an assembly step.
    """
    article = output.articulo
    if article is None:
        return None, None
    if article.pieza_color is not None:
        return article.pieza_color.pieza_color, "PIEZA_COLOR"
    if article.clase != CLASE_PRODUCTO_TERMINADO:
        return None, None

    structure_ids = {
        allocation.orden_produccion_linea.estructura_revision_id
        for allocation in output.asignaciones
        if (allocation.estado != "CANCELADA" or output.orden_operacion.estado == "ANULADA")
        and allocation.orden_produccion_linea is not None
        and allocation.orden_produccion_linea.estructura_revision_id is not None
    }
    if len(structure_ids) != 1:
        return None, None
    components = session.scalars(
        select(ScmEstructuraComponente).where(
            ScmEstructuraComponente.revision_id == next(iter(structure_ids))
        )
    ).all()
    if len(components) != 1 or Decimal(components[0].cantidad) != Decimal("1"):
        return None, None
    component_article = components[0].articulo_componente
    if component_article is None or component_article.pieza_color is None:
        return None, None
    return component_article.pieza_color.pieza_color, "PT_MONOPIEZA"


def _mold_output_spec(session, output, mold):
    piece_color, derivation = _output_piece_color(session, output)
    if piece_color is None:
        return None
    composition = session.scalar(
        select(MoldePieza).where(
            MoldePieza.molde_id == mold.codigo,
            MoldePieza.pieza_id == piece_color.pieza_id,
            MoldePieza.activo.is_(True),
        )
    )
    if composition is None:
        raise ScmServiceError(
            "MOLD_OUTPUT_INCOMPATIBLE",
            "El molde no fabrica la salida fisica requerida por la OF.",
            status_code=422,
            details={"salida_id": str(output.id)},
        )
    return {
        "piece_color": piece_color,
        "derivation": derivation,
        "per_cycle": Decimal(composition.cavidades),
        "unit_weight": Decimal(str(composition.peso_unitario_gr)),
    }


def _validate_physical_output_set(
    session,
    mold,
    color_id,
    entries,
    *,
    require_complete=True,
):
    """Validate the immutable physical set whenever a run targets PC data.

    Legacy PT/WIP runs retain their existing path. Once one PieceColor output
    is present, every active mold piece must appear exactly once with the
    canonical cavity and unit-weight snapshots.
    """
    mold_pieces = {
        item.pieza_id: item
        for item in session.scalars(
            select(MoldePieza).where(
                MoldePieza.molde_id == mold.codigo,
                MoldePieza.activo.is_(True),
            )
        ).all()
    }
    pc_entries = []
    non_pc_entries = []
    for entry in entries:
        article = entry[0]
        if article.pieza_color is None or article.pieza_color.pieza_color is None:
            non_pc_entries.append(entry)
            continue
        pc_entries.append(entry)
    if not pc_entries:
        return
    if non_pc_entries:
        raise ScmServiceError(
            "MOLD_OUTPUT_SET_MISMATCH",
            "Una corrida PiezaColor no puede mezclar salidas WIP/PT.",
            status_code=422,
        )
    by_piece = {}
    for article, per_cycle, unit_weight in pc_entries:
        piece_color = article.pieza_color.pieza_color
        if piece_color.color_produccion_id != color_id:
            raise ScmServiceError(
                "OF_RUN_COLOR_MISMATCH",
                "La salida PiezaColor no corresponde al color de la corrida.",
                status_code=422,
            )
        if piece_color.pieza_id in by_piece:
            raise ScmServiceError(
                "DUPLICATE_OF_PIECE",
                "Una corrida no puede repetir una pieza fisica del molde.",
                status_code=422,
            )
        by_piece[piece_color.pieza_id] = (per_cycle, unit_weight)
    if require_complete and (set(by_piece) != set(mold_pieces) or any(
        Decimal(str(by_piece[piece_id][0]))
        != Decimal(str(mold_pieces[piece_id].cavidades))
        or Decimal(str(by_piece[piece_id][1]))
        != Decimal(str(mold_pieces[piece_id].peso_unitario_gr))
        for piece_id in mold_pieces
    )):
        raise ScmServiceError(
            "MOLD_OUTPUT_SET_MISMATCH",
            "Las salidas deben cubrir exactamente las piezas activas, cavidades y pesos del molde.",
            status_code=422,
            details={
                "piezas_requeridas": sorted(mold_pieces),
                "piezas_recibidas": sorted(by_piece),
            },
        )


def _default_recipe_for_run(session, run, outputs):
    candidates = session.scalars(
        select(RecetaColorMaestra).where(
            RecetaColorMaestra.color_produccion_id == run.color_produccion_id,
            RecetaColorMaestra.estado == "APROBADA",
            RecetaColorMaestra.es_default.is_(True),
        )
    ).all()
    product_scopes = {
        output.articulo.producto.producto_terminado_id
        for output in outputs
        if output.articulo is not None
        and output.articulo.producto is not None
    }
    exact = [
        recipe for recipe in candidates
        if recipe.producto_scope in product_scopes
    ]
    if len(exact) == 1:
        return exact[0]
    generic = [recipe for recipe in candidates if recipe.producto_scope == "*"]
    if not exact and len(generic) == 1:
        return generic[0]
    if not product_scopes and len(candidates) == 1:
        return candidates[0]
    return None


def _recipe_product_scopes(outputs):
    scopes = set()
    for output in outputs:
        article = getattr(output, "articulo", output)
        if article is not None and article.producto is not None:
            scopes.add(article.producto.producto_terminado_id)
    return scopes


def _validated_approved_recipe(
    session,
    recipe_id,
    color_id,
    *,
    outputs=None,
):
    recipe = session.get(RecetaColorMaestra, recipe_id)
    if recipe is None or recipe.color_produccion_id != color_id:
        raise ScmServiceError(
            "RECIPE_COLOR_MISMATCH",
            "La receta no corresponde al color de la corrida.",
            status_code=422,
        )
    if recipe.estado != "APROBADA":
        raise ScmServiceError(
            "APPROVED_RECIPE_REQUIRED",
            "La OF solo puede usar una formulación de material aprobada.",
            status_code=422,
            details={"receta_revision_id": recipe.id, "estado": recipe.estado},
        )
    if (
        outputs is not None
        and recipe.producto_scope != "*"
        and recipe.producto_scope not in _recipe_product_scopes(outputs)
    ):
        raise ScmServiceError(
            "RECIPE_SCOPE_MISMATCH",
            "La formulación no corresponde al producto de la corrida.",
            status_code=422,
            details={
                "receta_revision_id": recipe.id,
                "producto_scope": recipe.producto_scope,
            },
        )
    return recipe


def _recipe_content_hash(recipe):
    content = {
        "id": recipe.id,
        "color_produccion_id": recipe.color_produccion_id,
        "producto_scope": recipe.producto_scope,
        "nombre_variante": recipe.nombre_variante,
        "revision": recipe.revision,
        "base_virgen_kg": format(Decimal(recipe.base_virgen_kg), "f"),
        "lineas": [
            {
                "material_id": line.material_id,
                "tipo_componente": line.tipo_componente,
                "cantidad": format(Decimal(line.cantidad), "f"),
                "unidad": line.unidad,
                "base_kg": (
                    format(Decimal(line.base_kg), "f")
                    if line.base_kg is not None else None
                ),
                "orden": line.orden,
            }
            for line in recipe.lineas
        ],
    }
    raw = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _serialize_output(session, output):
    piece_color, derivation = _output_piece_color(session, output)
    piece_id = piece_color.pieza_id if piece_color is not None else None
    return {
        "id": str(output.id),
        "articulo_scm_id": output.articulo_scm_id,
        "articulo": {
            "codigo": output.articulo.codigo,
            "nombre": output.articulo.nombre,
            "clase": output.articulo.clase,
            "unidad_inventario": output.articulo.unidad_inventario,
            "pieza_id": piece_id,
            "producto_sku": (
                output.articulo.producto.producto_terminado_id
                if output.articulo.producto is not None else None
            ),
            "derivacion_molde": derivation,
        } if output.articulo else None,
        "cantidad_por_ciclo_snapshot": (
            _decimal_text(output.cantidad_por_ciclo_snapshot, 4)
            if output.cantidad_por_ciclo_snapshot is not None
            else None
        ),
        "peso_unitario_snapshot_g": (
            _decimal_text(output.peso_unitario_snapshot_g, 4)
            if output.peso_unitario_snapshot_g is not None
            else None
        ),
        "cantidad_objetivo": _decimal_text(output.cantidad_objetivo, 3),
        "cantidad_real": (
            _decimal_text(output.cantidad_real, 3)
            if output.cantidad_real is not None else None
        ),
        "cantidad_rechazada": (
            _decimal_text(output.cantidad_rechazada, 3)
            if output.cantidad_rechazada is not None else None
        ),
        "kg_estandar_objetivo": (
            _decimal_text(output.kg_estandar_objetivo, 6)
            if output.kg_estandar_objetivo is not None
            else None
        ),
        "excedente_objetivo": _decimal_text(output.excedente_objetivo, 3),
        "lote_salida_legacy_id": output.lote_salida_legacy_id,
    }


def _serialize_run(session, run):
    color_identity = serialize_color_identity(
        run.color_produccion,
        color_id=run.color_produccion_id,
    )
    color_name = color_identity["nombre"] if color_identity else None
    kg_neto_por_ciclo, kg_neto_alcanzable, redondeo_kg = _run_net_projection(run)
    return {
        "id": str(run.id),
        "codigo": run.codigo,
        "secuencia": run.secuencia,
        "color_produccion_id": run.color_produccion_id,
        # ``color`` and the flat aliases keep existing UI clients simple;
        # ``color_identidad`` is the canonical human-readable projection.
        "color": color_name,
        "color_nombre": color_name,
        "color_hex": color_identity["hex"] if color_identity else None,
        "color_identidad": color_identity,
        "receta_revision_id": run.receta_revision_id,
        "receta_hash": run.receta_hash,
        "receta": (
            {
                **serialize_recipe(run.receta_revision),
                "nombre": run.receta_revision.nombre_variante,
            }
            if run.receta_revision is not None else None
        ),
        "operacion_ruta_revision_id": run.operacion_ruta_revision_id,
        "operacion_ruta_hash": run.operacion_ruta_hash,
        "operacion_ruta": route_operation_dto(run.operacion_ruta),
        "ciclos_objetivo": run.ciclos_objetivo,
        "objetivo_neto_kg": _decimal_text(run.objetivo_neto_kg, 6),
        "kg_neto_por_ciclo": _decimal_text(kg_neto_por_ciclo, 6),
        "kg_neto_alcanzable": _decimal_text(kg_neto_alcanzable, 6),
        "redondeo_kg": _decimal_text(redondeo_kg, 6),
        "estado": run.estado,
        "lote_color_legacy_id": run.lote_color_legacy_id,
        "meta_kg_legacy": (
            format(run.meta_kg_legacy, "f")
            if run.meta_kg_legacy is not None
            else None
        ),
        "salidas": [_serialize_output(session, output) for output in run.salidas],
    }


def _serialize(session, operation, *, schedule_projection=None):
    fabrication = operation.fabricacion
    resolved_process, _, compatibility = resolve_order_process(operation)
    route_operation = operation.operacion_ruta_revision
    if not fabrication.snapshot_proceso and route_operation is not None:
        header_output_ids = {item.articulo_scm_id for item in operation.salidas}
        header_valid = (
            route_operation.ruta is not None
            and route_operation.ruta.estado in {"APROBADA", "RETIRADA"}
            and route_operation.executor_kind == "OP_OT"
            and route_operation.tipo in {"INYECCION", "SOPLADO"}
            and bool(operation.operacion_ruta_hash)
            and operation.operacion_ruta_hash == _route_content_hash(
                route_operation.ruta
            )
            and route_operation.articulo_salida_id in header_output_ids
        )
        if not header_valid:
            resolved_process = None
            compatibility = "RUTA_CABECERA_NO_VERIFICABLE"
    replacement = replacement_summary(session, operation)
    origin_op = operation.plan_produccion.orden_produccion if operation.plan_produccion else None
    kg_closure = session.scalar(
        select(ScmCierreProductivoKg)
        .where(
            ScmCierreProductivoKg.documento_tipo == "OF",
            ScmCierreProductivoKg.documento_id == str(operation.id),
        )
        .order_by(ScmCierreProductivoKg.created_at.desc(), ScmCierreProductivoKg.id.desc())
    )
    return {
        **(
            schedule_projection
            if schedule_projection is not None
            else operation_schedule_projection(session, operation)
        ),
        "id": str(operation.id),
        "codigo": operation.codigo,
        "tipo": operation.tipo,
        "origen_demanda": operation.origen_demanda,
        "motivo": operation.motivo,
        "estado": operation.estado,
        "anulacion": annulment_summary(session, operation),
        "reemplazo": replacement,
        "procedencia": {
            "op_id": str(origin_op.id) if origin_op else None,
            "op_codigo": origin_op.codigo if origin_op else None,
            "plan_id": (
                str(operation.plan_produccion_id)
                if operation.plan_produccion_id else None
            ),
            "tipo": operation.origen_demanda,
            "orden_anterior_id": (
                replacement or {}
            ).get("anterior", {}).get("id")
            if operation.origen_demanda == "REEMPLAZO_OF" else None,
        },
        "version": operation.version,
        "plan_produccion_id": (
            str(operation.plan_produccion_id)
            if operation.plan_produccion_id else None
        ),
        "propuesta_clave": operation.propuesta_clave,
        "proceso_requerido": resolved_process,
        "snapshot_proceso": fabrication.snapshot_proceso,
        "fuente_proceso": fabrication.fuente_proceso,
        "compatibilidad_proceso": compatibility,
        "created_by_id": operation.created_by_id,
        "released_by_id": operation.released_by_id,
        "released_at": _iso(operation.released_at),
        "started_by_id": operation.started_by_id,
        "started_at": _iso(operation.started_at),
        "closed_by_id": operation.closed_by_id,
        "closed_at": _iso(operation.closed_at),
        "created_at": _iso(operation.created_at),
        "updated_at": _iso(operation.updated_at),
        "cierre_kg": kg_closure.to_dict() if kg_closure is not None else None,
        "molde_id": fabrication.molde_id,
        "maquina_prevista_id": fabrication.maquina_prevista_id,
        "snapshot_tiempo_ciclo_seg": (
            format(fabrication.snapshot_tiempo_ciclo_seg, "f")
            if fabrication.snapshot_tiempo_ciclo_seg is not None
            else None
        ),
        "snapshot_horas_turno": (
            format(fabrication.snapshot_horas_turno, "f")
            if fabrication.snapshot_horas_turno is not None
            else None
        ),
        "snapshot_peso_colada_gr": (
            format(fabrication.snapshot_peso_colada_gr, "f")
            if fabrication.snapshot_peso_colada_gr is not None
            else None
        ),
        "codigo_legacy_op": fabrication.codigo_legacy_op,
        "corridas": [
            _serialize_run(session, run) for run in fabrication.corridas
        ],
    }


def _normalized_process(value):
    return str(value or "").strip().upper()


def _machine_processes(machine):
    machine_type = machine.tipo_maquina
    canonical = _normalized_process(
        machine_type.proceso if machine_type else None
    )
    if canonical:
        return {canonical}
    return {
        _normalized_process(value)
        for value in (
            machine_type.codigo if machine_type else None,
            machine_type.nombre if machine_type else None,
            machine.tipo,
        )
        if _normalized_process(value)
    }


def _resolve_machine_process(machine):
    processes = _machine_processes(machine)
    if len(processes) > 1:
        raise ScmServiceError(
            "MACHINE_PROCESS_AMBIGUOUS",
            "La maquina tiene aliases legacy de proceso incompatibles; "
            "corrija TipoMaquina.proceso.",
            status_code=422,
            details={
                "maquina_id": machine.id,
                "procesos_maquina": sorted(processes),
            },
        )
    return next(iter(processes), None)


def _required_process_for_operation(operation):
    process, _, _ = resolve_order_process(operation)
    return process


def _validate_machine_process(machine, required_process):
    actual_process = _resolve_machine_process(machine)
    if actual_process is None:
        raise ScmServiceError(
            "MACHINE_PROCESS_REQUIRED",
            "La maquina no tiene un proceso canonico o legacy univoco.",
            status_code=422,
            details={"maquina_id": machine.id},
        )
    if actual_process != required_process:
        raise ScmServiceError(
            "MACHINE_PROCESS_INCOMPATIBLE",
            "La maquina prevista no corresponde al proceso de la OF.",
            status_code=422,
            details={
                "maquina_id": machine.id,
                "proceso_requerido": required_process,
                "proceso_maquina": actual_process,
                "procesos_maquina": sorted(_machine_processes(machine)),
            },
        )


def _validate_machine_for_operation(machine, operation):
    if machine.estado != "OPERATIVA":
        raise ScmServiceError(
            "MACHINE_NOT_AVAILABLE",
            "La maquina prevista no se encuentra OPERATIVA.",
            status_code=422,
            details={
                "maquina_id": machine.id,
                "estado": machine.estado,
            },
        )
    required_process = _required_process_for_operation(operation)
    if required_process:
        _validate_machine_process(machine, required_process)


def _load_fabrication(session, operation_id, *, lock=False):
    statement = select(ScmOrdenOperacion).where(
        ScmOrdenOperacion.id == operation_id,
        ScmOrdenOperacion.tipo == "FABRICACION",
    )
    if lock:
        statement = statement.with_for_update(of=ScmOrdenOperacion).execution_options(populate_existing=True)
    operation = session.scalar(statement)
    if (
        operation is None
        or operation.tipo != "FABRICACION"
        or operation.fabricacion is None
    ):
        raise ScmServiceError(
            "OF_NOT_FOUND",
            "La orden de fabricacion no existe.",
            status_code=404,
        )
    return operation


def list_fabrication_orders(session, *, actor_id):
    load_actor(session, actor_id, capability="OF_VER")
    operations = session.scalars(
        select(ScmOrdenOperacion)
        .where(ScmOrdenOperacion.tipo == "FABRICACION")
        .order_by(ScmOrdenOperacion.created_at.desc())
    ).all()
    projections = operation_schedule_projections(session, operations)
    return {
        "items": [
            _serialize(
                session,
                operation,
                schedule_projection=projections[operation.id],
            )
            for operation in operations
        ]
    }


def get_fabrication_order(session, *, actor_id, operation_id):
    load_actor(session, actor_id, capability="OF_VER")
    order = _load_fabrication(session, operation_id)
    payload = _serialize(session, order)
    if order.estado == "LIBERADA":
        try:
            _ensure_replacement_eligible(session, order)
            payload["reemplazo_disponibilidad"] = {"permitido": True}
        except ScmServiceError as error:
            payload["reemplazo_disponibilidad"] = {"permitido": False, "motivo": str(error)}
    return payload


def _ensure_replacement_eligible(session, order):
    """Fail closed before changing a released intermediate OF."""
    if order.estado != "LIBERADA":
        raise ScmServiceError(
            "INVALID_REPLACEMENT_STATE",
            "Solo una OF LIBERADA puede reemplazarse por este flujo.",
            status_code=409,
        )
    if order.started_at is not None or order.closed_at is not None:
        raise ScmServiceError(
            "ORDER_HAS_ACTIVITY",
            "La OF conserva actividad operativa y no puede reemplazarse.",
            status_code=409,
        )
    if order.plan_produccion_id is None or order.plan_produccion is None:
        raise ScmServiceError(
            "REPLACEMENT_ORIGIN_REQUIRED",
            "El reemplazo solo procede para una OF proveniente de un plan.",
            status_code=409,
        )
    if order.plan_produccion.estado != "CONFIRMADO":
        raise ScmServiceError(
            "REPLACEMENT_PLAN_NOT_CONFIRMED",
            "El plan de origen debe permanecer CONFIRMADO.",
            status_code=409,
        )
    if not order.propuesta_clave:
        raise ScmServiceError(
            "REPLACEMENT_ORIGIN_AMBIGUOUS",
            "La OF no conserva una propuesta de origen verificable.",
            status_code=409,
        )
    proposal = order.plan_produccion.propuesta_json or {}
    reservations = proposal.get("reservas_stock", [])
    if not isinstance(reservations, list) or any(
        not isinstance(item, dict)
        or item.get("propuesta_consumidora_clave") in (None, order.propuesta_clave)
        for item in reservations
    ):
        raise ScmServiceError(
            "REPLACEMENT_STOCK_REQUIRES_RECONCILIATION",
            "El plan conserva reservas para esta propuesta o de origen ambiguo; concilie antes de reemplazar.",
            status_code=409,
        )
    documents = [
        item for item in proposal.get("documentos", [])
        if isinstance(item, dict) and item.get("clave") == order.propuesta_clave
    ]
    if len(documents) != 1 or documents[0].get("tipo") != "FABRICACION":
        raise ScmServiceError(
            "REPLACEMENT_ORIGIN_AMBIGUOUS",
            "La propuesta de origen no identifica una única OF de fabricación.",
            status_code=409,
        )
    document = documents[0]
    if (
        document.get("operacion_ruta_id") != order.operacion_ruta_revision_id
        or document.get("ruta_hash") != order.operacion_ruta_hash
        or not document.get("articulo_scm_id")
    ):
        raise ScmServiceError(
            "REPLACEMENT_ORIGIN_AMBIGUOUS",
            "La ruta o salida de la propuesta de origen no coincide con la OF.",
            status_code=409,
        )
    output_article_ids = {
        output.articulo_scm_id for output in order.salidas
    }
    if output_article_ids != {document.get("articulo_scm_id")}:
        raise ScmServiceError(
            "REPLACEMENT_ORIGIN_AMBIGUOUS",
            "La salida de la OF no coincide con la propuesta confirmada.",
            status_code=409,
        )
    runs = order.fabricacion.corridas
    if not runs or any(run.estado != "LIBERADA" for run in runs):
        raise ScmServiceError(
            "ORDER_HAS_ACTIVITY",
            "La OF conserva una corrida iniciada o en un estado no reemplazable.",
            status_code=409,
        )
    if any(run.trabajos_color for run in runs):
        raise ScmServiceError(
            "ORDER_HAS_DEPENDENCIES",
            "La OF tiene trabajos de color vinculados; no se anula parcialmente.",
            status_code=409,
        )
    outputs = list(order.salidas)
    if any(
        output.cantidad_real is not None
        or output.cantidad_rechazada is not None
        for output in outputs
    ):
        raise ScmServiceError(
            "ORDER_HAS_ACTIVITY",
            "La OF conserva resultados y no puede reemplazarse.",
            status_code=409,
        )
    assignments = session.scalars(select(ScmAsignacionDemandaSuministro).where(
        ScmAsignacionDemandaSuministro.orden_operacion_salida_id.in_(
            [output.id for output in outputs]
        ),
    )).all()
    if assignments:
        raise ScmServiceError(
            "DIRECT_DEMAND_ASSIGNMENT",
            "La OF tiene asignaciones directas de demanda; primero concilia su cobertura.",
            status_code=409,
            details={"asignaciones": [str(item.id) for item in assignments]},
        )

    # Operational references are not part of the OF's structural children.
    # Inspect every FK in the current metadata so new OT/manga dependencies
    # fail closed until this command explicitly supports them.
    structural = {
        "scm_orden_operacion", "scm_orden_fabricacion",
        "scm_corrida_fabricacion", "scm_orden_operacion_salida",
        "scm_asignacion_demanda_suministro",
    }
    targets = {
        "scm_orden_operacion": [order.id],
        "scm_orden_fabricacion": [order.id],
        "scm_corrida_fabricacion": [run.id for run in runs],
        "scm_orden_operacion_salida": [output.id for output in outputs],
    }
    for table in ScmOrdenOperacion.metadata.tables.values():
        if table.name in structural:
            continue
        for foreign_key in table.foreign_keys:
            ids = targets.get(foreign_key.column.table.name)
            if ids and session.scalar(
                select(foreign_key.parent).where(
                    foreign_key.parent.in_(ids)
                ).limit(1)
            ) is not None:
                raise ScmServiceError(
                    "ORDER_HAS_DEPENDENCIES",
                    "La OF tiene actividad o dependencia operativa vinculada.",
                    status_code=409,
                    details={"table": table.name},
                )


def replace_fabrication_order(
    session,
    *,
    actor_id,
    operation_id,
    operation_order_id,
    data,
):
    """Atomically annul an unused released OF and create its draft successor."""
    try:
        actor = load_actor(session, actor_id, capability="OF_ANULAR")
        if not actor.tiene_capacidad("OF_EDITAR_BORRADOR"):
            raise ScmServiceError(
                "CAPABILITY_REQUIRED",
                "El actor requiere la capacidad OF_EDITAR_BORRADOR.",
                status_code=403,
                details={"capability": "OF_EDITAR_BORRADOR"},
            )
        reject_unknown_fields(data, allowed={"version", "motivo"})
        version = expected_version(data.get("version"))
        reason = required_text(data.get("motivo"), field="motivo", max_length=500)
        request = {"order_id": str(operation_order_id), "version": version, "motivo": reason}
        endpoint = "POST /ordenes-fabricacion/{id}/reemplazar"
        try:
            with session.begin_nested():
                operation, replay = _reserve_operation(session, operation_id, endpoint, actor, request)
        except IntegrityError:
            if session.get(ScmOperacion, operation_id) is None:
                raise
            operation, replay = _reserve_operation(session, operation_id, endpoint, actor, request)
        if replay is not None:
            return replay
        order = _load_fabrication(session, operation_order_id, lock=True)
        if order.version != version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OF fue modificada por otro usuario.",
                status_code=409,
            )
        _ensure_replacement_eligible(session, order)
        successor_key = f"REEMPLAZO:{order.id}"
        existing_successor = session.scalar(select(ScmOrdenOperacion).where(
            ScmOrdenOperacion.plan_produccion_id == order.plan_produccion_id,
            ScmOrdenOperacion.propuesta_clave == successor_key,
        ))
        if existing_successor is not None:
            raise ScmServiceError(
                "REPLACEMENT_ALREADY_EXISTS",
                "La OF ya tiene una sucesora preparada.",
                status_code=409,
                details={"sucesora_id": str(existing_successor.id)},
            )

        successor_code = generar_codigo_catalogo("ORDEN_FABRICACION", session=session)
        successor = ScmOrdenOperacion(
            codigo=successor_code,
            tipo="FABRICACION",
            origen_demanda="REEMPLAZO_OF",
            motivo=reason,
            plan_produccion_id=order.plan_produccion_id,
            propuesta_clave=successor_key,
            estado="BORRADOR",
            operacion_ruta_revision_id=order.operacion_ruta_revision_id,
            operacion_ruta_hash=order.operacion_ruta_hash,
            created_by_id=actor.id,
        )
        successor_fabrication = ScmOrdenFabricacion(
            orden_operacion=successor,
            molde_id=order.fabricacion.molde_id,
            maquina_prevista_id=order.fabricacion.maquina_prevista_id,
            snapshot_tiempo_ciclo_seg=order.fabricacion.snapshot_tiempo_ciclo_seg,
            snapshot_horas_turno=order.fabricacion.snapshot_horas_turno,
            snapshot_peso_colada_gr=order.fabricacion.snapshot_peso_colada_gr,
            snapshot_proceso=order.fabricacion.snapshot_proceso,
            fuente_proceso=order.fabricacion.fuente_proceso,
            # codigo_legacy_op is intentionally not cloned: it is unique legacy identity.
        )
        for run in order.fabricacion.corridas:
            successor_run = ScmCorridaFabricacion(
                codigo=f"{successor_code}-C{run.secuencia:02d}",
                secuencia=run.secuencia,
                color_produccion_id=run.color_produccion_id,
                # Recipe selection is an explicit decision on the successor.
                receta_revision_id=None,
                receta_hash=None,
                ciclos_objetivo=run.ciclos_objetivo,
                objetivo_neto_kg=run.objetivo_neto_kg,
                operacion_ruta_revision_id=run.operacion_ruta_revision_id,
                operacion_ruta_hash=run.operacion_ruta_hash,
                estado="BORRADOR",
                meta_kg_legacy=run.meta_kg_legacy,
            )
            successor_fabrication.corridas.append(successor_run)
            for output in run.salidas:
                successor_run.salidas.append(ScmOrdenOperacionSalida(
                    orden_operacion=successor,
                    articulo_scm_id=output.articulo_scm_id,
                    cantidad_por_ciclo_snapshot=output.cantidad_por_ciclo_snapshot,
                    peso_unitario_snapshot_g=output.peso_unitario_snapshot_g,
                    cantidad_objetivo=output.cantidad_objetivo,
                    kg_estandar_objetivo=output.kg_estandar_objetivo,
                    excedente_objetivo=output.excedente_objetivo,
                    # legacy output identity is never copied.
                ))
        session.add(successor)
        order.estado = "ANULADA"
        order.version += 1
        for run in order.fabricacion.corridas:
            run.estado = "ANULADA"
        session.flush()
        link = {
            "anterior": {
                "id": str(order.id),
                "codigo": order.codigo,
                "estado": order.estado,
                "version": order.version,
            },
            "sucesora": {
                "id": str(successor.id),
                "codigo": successor.codigo,
                "estado": successor.estado,
                "version": successor.version,
            },
            "plan_id": str(order.plan_produccion_id),
            "propuesta_clave": successor_key,
            "motivo": reason,
            "receta_pendiente": True,
        }
        event = ScmEvento(
            aggregate_type="ORDEN_FABRICACION",
            aggregate_id=str(order.id),
            tipo="OF_REPLACED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            motivo=reason,
            before_json={"estado": "LIBERADA", "version": version},
            after_json={"reemplazo": link},
            operation_id=operation.operation_id,
        )
        session.add(event)
        session.flush()
        response = {
            "anterior": _serialize(session, order),
            "sucesora": _serialize(session, successor),
            "vinculo": link,
        }
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 201
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def create_exceptional_fabrication_order(
    session,
    *,
    actor_id,
    operation_id,
    data,
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="OF_EXCEPCIONAL_CREAR",
        )
        reject_unknown_fields(
            data,
            allowed={
                "motivo",
                "molde_id",
                "maquina_prevista_id",
                "snapshot_tiempo_ciclo_seg",
                "snapshot_horas_turno",
                "snapshot_peso_colada_gr",
                "proceso",
                "corridas",
            },
        )
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /ordenes-fabricacion/excepcionales",
            actor,
            data,
        )
        if replay is not None:
            return replay
        reason = required_text(
            data.get("motivo"),
            field="motivo",
            max_length=2000,
        )
        mold_id = required_text(
            data.get("molde_id"),
            field="molde_id",
            max_length=50,
        )
        mold = session.get(Molde, mold_id)
        if mold is None:
            raise ScmServiceError(
                "MOLD_NOT_FOUND",
                "El molde indicado no existe.",
                status_code=422,
            )
        raw_runs = data.get("corridas")
        if not isinstance(raw_runs, list) or not raw_runs:
            raise ScmServiceError(
                "OF_CORRIDA_REQUIRED",
                "La OF requiere al menos una corrida.",
                status_code=422,
            )
        explicit_process = normalize_process(data.get("proceso"))
        if explicit_process is not None and explicit_process not in {"INYECCION", "SOPLADO"}:
            raise ScmServiceError(
                "PROCESS_INVALID",
                "El proceso debe ser INYECCION o SOPLADO.",
                status_code=422,
            )
        linked_operations = []
        seen_route_ids = set()
        all_runs_linked = True
        for raw_run in raw_runs:
            if not isinstance(raw_run, dict):
                all_runs_linked = False
                continue
            route_id = raw_run.get("operacion_ruta_revision_id")
            if route_id is None:
                all_runs_linked = False
                continue
            if route_id in seen_route_ids:
                raise ScmServiceError(
                    "DUPLICATE_ROUTE_OPERATION",
                    "Una OF no puede repetir la misma operacion de ruta.",
                    status_code=422,
                )
            seen_route_ids.add(route_id)
            linked_operations.append(resolve_route_operation(session, route_id, lock=True))
        linked_processes = {item.tipo for item in linked_operations}
        machine_id = data.get("maquina_prevista_id")
        machine = session.get(Maquina, machine_id) if machine_id is not None else None
        if machine_id is not None and machine is None:
            raise ScmServiceError(
                "MACHINE_NOT_FOUND",
                "La maquina prevista no existe.",
                status_code=422,
            )
        if len(linked_processes) > 1:
            raise ScmServiceError(
                "PROCESS_MISMATCH",
                "Las operaciones de ruta de las corridas requieren procesos distintos.",
                status_code=422,
            )
        if explicit_process is not None and linked_processes and explicit_process not in linked_processes:
            raise ScmServiceError(
                "PROCESS_MISMATCH",
                "El proceso explicito no coincide con las operaciones de ruta.",
                status_code=422,
            )
        if explicit_process is None and linked_processes and not all_runs_linked:
            raise ScmServiceError(
                "PROCESS_REQUIRED",
                "Las corridas parcialmente enlazadas requieren proceso explicito.",
                status_code=422,
            )
        resolved_process = explicit_process or (
            next(iter(linked_processes)) if linked_processes else None
        )
        process_source = (
            "EXPLICITO" if explicit_process is not None
            else "RUTA_OBJETIVOS" if linked_processes else None
        )
        if machine_id is not None and resolved_process is not None:
            _validate_machine_process(machine, resolved_process)
        code = generar_codigo_catalogo(
            "ORDEN_FABRICACION",
            session=session,
        )
        order = ScmOrdenOperacion(
            codigo=code,
            tipo="FABRICACION",
            origen_demanda="EXCEPCIONAL",
            motivo=reason,
            created_by_id=actor.id,
        )
        fabrication = ScmOrdenFabricacion(
            orden_operacion=order,
            molde_id=mold_id,
            maquina_prevista_id=machine_id,
            snapshot_tiempo_ciclo_seg=_positive_decimal(
                data.get("snapshot_tiempo_ciclo_seg"),
                "snapshot_tiempo_ciclo_seg",
            ),
            snapshot_horas_turno=_positive_decimal(
                data.get("snapshot_horas_turno"),
                "snapshot_horas_turno",
            ),
            snapshot_peso_colada_gr=_positive_decimal(
                data.get("snapshot_peso_colada_gr", 0),
                "snapshot_peso_colada_gr",
                allow_zero=True,
            ),
            snapshot_proceso=resolved_process,
            fuente_proceso=process_source,
        )
        seen_colors = set()
        for sequence, raw_run in enumerate(raw_runs, start=1):
            if not isinstance(raw_run, dict):
                raise ScmServiceError(
                    "INVALID_OF_RUN",
                    "Cada corrida debe ser un objeto JSON.",
                    status_code=400,
                )
            reject_unknown_fields(
                raw_run,
                allowed={
                    "color_produccion_id",
                    "receta_revision_id",
                    "ciclos_objetivo",
                    "objetivo_neto_kg",
                    "operacion_ruta_revision_id",
                    "salidas",
                },
            )
            color_id = raw_run.get("color_produccion_id")
            if session.get(ColorProduccion, color_id) is None:
                raise ScmServiceError(
                    "COLOR_NOT_FOUND",
                    "El color de una corrida no existe.",
                    status_code=422,
                )
            if color_id in seen_colors:
                raise ScmServiceError(
                    "DUPLICATE_OF_COLOR",
                    "Una OF no puede repetir el mismo color en varias corridas.",
                    status_code=422,
                )
            seen_colors.add(color_id)
            recipe_id = raw_run.get("receta_revision_id")
            recipe = None
            if recipe_id is not None:
                recipe = _validated_approved_recipe(session, recipe_id, color_id)
            objective = (
                _positive_decimal(raw_run["objetivo_neto_kg"], "objetivo_neto_kg")
                if raw_run.get("objetivo_neto_kg") is not None
                else None
            )
            explicit_cycles = (
                _positive_decimal(
                    raw_run["ciclos_objetivo"],
                    "ciclos_objetivo",
                    integral=True,
                )
                if raw_run.get("ciclos_objetivo") is not None
                else None
            )
            raw_outputs = raw_run.get("salidas")
            if not isinstance(raw_outputs, list) or not raw_outputs:
                raise ScmServiceError(
                    "OF_OUTPUT_REQUIRED",
                    "Cada corrida requiere al menos una salida.",
                    status_code=422,
                )
            seen_articles = set()
            output_articles = []
            output_specs = []
            minimum_cycles = 1
            for raw_output in raw_outputs:
                if not isinstance(raw_output, dict):
                    raise ScmServiceError(
                        "INVALID_OF_OUTPUT",
                        "Cada salida debe ser un objeto JSON.",
                        status_code=400,
                    )
                reject_unknown_fields(
                    raw_output,
                    allowed={
                        "articulo_scm_id",
                        "cantidad_por_ciclo",
                        "peso_unitario_g",
                        "cantidad_objetivo",
                    },
                )
                article_id = raw_output.get("articulo_scm_id")
                article = session.get(ScmArticulo, article_id)
                if article is None:
                    raise ScmServiceError(
                        "ARTICLE_NOT_FOUND",
                        "Una salida referencia un articulo inexistente.",
                        status_code=422,
                    )
                if article_id in seen_articles:
                    raise ScmServiceError(
                        "DUPLICATE_OF_OUTPUT",
                        "Una corrida no puede repetir el mismo articulo.",
                        status_code=422,
                    )
                if objective is not None and (
                    raw_output.get("cantidad_por_ciclo") is None
                    or raw_output.get("peso_unitario_g") is None
                ):
                    raise ScmServiceError(
                        "OF_NET_TARGET_DATA_REQUIRED",
                        "El objetivo neto requiere cantidad por ciclo y peso unitario.",
                        status_code=422,
                    )
                per_cycle = _positive_decimal(
                    raw_output.get("cantidad_por_ciclo"),
                    "cantidad_por_ciclo",
                )
                unit_weight = _positive_decimal(
                    raw_output.get("peso_unitario_g"),
                    "peso_unitario_g",
                )
                requested_quantity = (
                    _positive_decimal(
                        raw_output["cantidad_objetivo"],
                        "cantidad_objetivo",
                    )
                    if raw_output.get("cantidad_objetivo") is not None
                    else None
                )
                if requested_quantity is not None:
                    minimum_cycles = max(
                        minimum_cycles,
                        int((requested_quantity / per_cycle).to_integral_value(
                            rounding=ROUND_CEILING
                        )),
                    )
                output_specs.append((
                    article_id,
                    article,
                    per_cycle,
                    unit_weight,
                    requested_quantity,
                ))
                seen_articles.add(article_id)
                output_articles.append(article)
            route_id = raw_run.get("operacion_ruta_revision_id")
            if route_id is not None:
                route_operation = resolve_route_operation(session, route_id, lock=True)
                route_article = route_operation.articulo_salida
                route_piece_color = (
                    route_article is not None
                    and route_article.pieza_color is not None
                    and route_article.pieza_color.pieza_color is not None
                )
                if not route_piece_color:
                    raise ScmServiceError(
                        "ROUTE_TARGET_OUT_OF_SCOPE",
                        "La referencia de ruta por corrida solo aplica a articulos PiezaColor.",
                        status_code=422,
                    )
                if route_operation.articulo_salida_id not in seen_articles:
                    raise ScmServiceError(
                        "ROUTE_OUTPUT_MISMATCH",
                        "La salida objetivo de la operacion de ruta no pertenece a la corrida.",
                        status_code=422,
                    )
                route_article = route_operation.articulo_salida
                route_color = (
                    route_article.pieza_color.pieza_color.color_produccion_id
                    if route_article is not None
                    and route_article.pieza_color is not None
                    and route_article.pieza_color.pieza_color is not None
                    else None
                )
                if route_color is not None and route_color != color_id:
                    raise ScmServiceError(
                        "ROUTE_COLOR_MISMATCH",
                        "La operacion de ruta no corresponde al color de la corrida.",
                        status_code=422,
                    )
            _validate_physical_output_set(
                session,
                mold,
                color_id,
                [
                    (article, per_cycle, unit_weight)
                    for _, article, per_cycle, unit_weight, _ in output_specs
                ],
                require_complete=True,
            )
            kg_neto_por_ciclo = sum(
                per_cycle * unit_weight / Decimal("1000")
                for _, _, per_cycle, unit_weight, _ in output_specs
            )
            cycles = _run_cycles(
                minimum_cycles=minimum_cycles,
                explicit_cycles=explicit_cycles,
                objective_neto_kg=objective,
                kg_neto_por_ciclo=kg_neto_por_ciclo,
            )
            run = ScmCorridaFabricacion(
                codigo=f"{code}-C{sequence:02d}",
                secuencia=sequence,
                color_produccion_id=color_id,
                receta_revision_id=recipe_id,
                # The immutable content fingerprint is calculated at release.
                receta_hash=None,
                ciclos_objetivo=cycles,
                objetivo_neto_kg=objective,
                operacion_ruta_revision_id=(
                    raw_run.get("operacion_ruta_revision_id")
                ),
                operacion_ruta_hash=(
                    route_snapshot(
                        resolve_route_operation(
                            session,
                            raw_run.get("operacion_ruta_revision_id"),
                            lock=True,
                        )
                    )[1]
                    if raw_run.get("operacion_ruta_revision_id") is not None
                    else None
                ),
            )
            fabrication.corridas.append(run)
            for article_id, _, per_cycle, unit_weight, requested_quantity in output_specs:
                projected_quantity = Decimal(cycles) * per_cycle
                objective_excess = (
                    projected_quantity - requested_quantity
                    if objective is not None and requested_quantity is not None
                    else Decimal("0")
                )
                quantity = (
                    projected_quantity
                    if objective is not None and requested_quantity is not None
                    else requested_quantity or projected_quantity
                )
                ScmOrdenOperacionSalida(
                    orden_operacion=order,
                    corrida_fabricacion=run,
                    articulo_scm_id=article_id,
                    cantidad_por_ciclo_snapshot=per_cycle,
                    peso_unitario_snapshot_g=unit_weight,
                    cantidad_objetivo=quantity,
                    kg_estandar_objetivo=quantity * unit_weight / Decimal("1000"),
                    excedente_objetivo=objective_excess,
                )
            if recipe is not None:
                _validated_approved_recipe(
                    session,
                    recipe.id,
                    color_id,
                    outputs=output_articles,
                )
        if resolved_process is None:
            raise ScmServiceError(
                "PROCESS_REQUIRED",
                "La OF excepcional requiere proceso explicito o rutas por corrida.",
                status_code=422,
            )
        session.add(order)
        session.flush()
        response = _serialize(session, order)
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="ORDEN_FABRICACION",
            aggregate_id=str(order.id),
            tipo="OF_EXCEPTIONAL_CREATED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            motivo=reason,
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def update_fabrication_order(
    session,
    *,
    actor_id,
    operation_id,
    operation_order_id,
    data,
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="OF_EDITAR_BORRADOR",
        )
        reject_unknown_fields(
            data,
            allowed={
                "version",
                "molde_id",
                "maquina_prevista_id",
                "snapshot_tiempo_ciclo_seg",
                "snapshot_horas_turno",
                "snapshot_peso_colada_gr",
                "proceso",
                "corridas",
            },
        )
        audit, replay = _reserve_operation(
            session,
            operation_id,
            "PATCH /ordenes-fabricacion/{id}",
            actor,
            data,
        )
        if replay is not None:
            return replay
        order = session.scalar(
            select(ScmOrdenOperacion)
            .where(ScmOrdenOperacion.id == operation_order_id)
            .with_for_update()
        )
        if (
            order is None
            or order.tipo != "FABRICACION"
            or order.fabricacion is None
        ):
            raise ScmServiceError(
                "OF_NOT_FOUND",
                "La orden de fabricacion no existe.",
                status_code=404,
            )
        version = expected_version(data.get("version"))
        if order.version != version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OF fue modificada por otro usuario.",
                status_code=409,
            )
        if order.estado != "BORRADOR":
            raise ScmServiceError(
                "INVALID_OF_STATE",
                "Solo una OF en borrador admite configuración.",
                status_code=409,
            )
        before_response = _serialize(session, order)
        # A replacement carries immutable technical snapshots. Its only
        # editable decision in this increment is the approved recipe; using
        # the normal configuration path here would recalculate from current
        # mold masters and silently change the successor's target.
        if order.origen_demanda == "REEMPLAZO_OF":
            reject_unknown_fields(data, allowed={"version", "corridas"})
            raw_runs = data.get("corridas")
            runs_by_id = {str(item.id): item for item in order.fabricacion.corridas}
            if not isinstance(raw_runs, list) or len(raw_runs) != len(runs_by_id) or {
                str(item.get("id")) for item in raw_runs
            } != set(runs_by_id):
                raise ScmServiceError(
                    "OF_CORRIDA_MISMATCH",
                    "Deben configurarse exactamente las corridas existentes.",
                    status_code=422,
                )
            for raw_run in raw_runs:
                reject_unknown_fields(
                    raw_run,
                    allowed={"id", "receta_revision_id"},
                )
                run = runs_by_id[str(raw_run["id"])]
                recipe_id = raw_run.get("receta_revision_id")
                if recipe_id is None:
                    raise ScmServiceError(
                        "APPROVED_RECIPE_REQUIRED",
                        "La sucesora requiere una formulación aprobada explícita.",
                        status_code=422,
                    )
                _validated_approved_recipe(
                    session,
                    recipe_id,
                    run.color_produccion_id,
                    outputs=run.salidas,
                )
                run.receta_revision_id = recipe_id
                run.receta_hash = None
            order.version += 1
            session.flush()
            response = _serialize(session, order)
            audit.response_json = copy.deepcopy(response)
            audit.estado_http = 200
            session.add(ScmEvento(
                aggregate_type="ORDEN_FABRICACION",
                aggregate_id=str(order.id),
                tipo="OF_REPLACEMENT_RECIPE_SELECTED",
                actor_id=actor.id,
                actor_snapshot=actor_snapshot(actor),
                before_json=before_response,
                after_json=response,
                operation_id=audit.operation_id,
            ))
            session.commit()
            return response
        mold_id = required_text(
            data.get("molde_id"),
            field="molde_id",
            max_length=50,
        )
        mold = session.get(Molde, mold_id)
        if mold is None or not mold.activo:
            raise ScmServiceError(
                "MOLD_NOT_FOUND",
                "El molde no existe o está inactivo.",
                status_code=422,
            )
        machine_id = data.get("maquina_prevista_id")
        machine = session.get(Maquina, machine_id) if machine_id is not None else None
        if machine_id is not None and (machine is None or not machine.activo):
            raise ScmServiceError(
                "MACHINE_NOT_FOUND",
                "La máquina prevista no existe o está inactiva.",
                status_code=422,
            )
        fabrication = order.fabricacion
        process_input_present = "proceso" in data
        if order.origen_demanda != "EXCEPCIONAL" and process_input_present:
            raise ScmServiceError(
                "PROCESS_NOT_ALLOWED",
                "El proceso solo puede editarse en OF excepcionales.",
                status_code=422,
            )
        explicit_process = normalize_process(data.get("proceso")) if process_input_present else None
        if process_input_present and explicit_process is not None and explicit_process not in {"INYECCION", "SOPLADO"}:
            raise ScmServiceError(
                "PROCESS_INVALID",
                "El proceso debe ser INYECCION o SOPLADO.",
                status_code=422,
            )
        raw_runs = data.get("corridas")
        if not isinstance(raw_runs, list) or not raw_runs:
            raise ScmServiceError(
                "OF_CORRIDA_REQUIRED",
                "La configuración requiere las corridas de la OF.",
                status_code=422,
            )
        runs_by_id = {str(item.id): item for item in fabrication.corridas}
        if (
            len(raw_runs) != len(runs_by_id)
            or {str(item.get("id")) for item in raw_runs} != set(runs_by_id)
        ):
            raise ScmServiceError(
                "OF_CORRIDA_MISMATCH",
                "Deben configurarse exactamente las corridas existentes.",
                status_code=422,
            )
        requested_routes = []
        seen_route_ids = set()
        route_fields_changed = False
        for raw_run in raw_runs:
            if not isinstance(raw_run, dict):
                raise ScmServiceError(
                    "INVALID_OF_RUN",
                    "Cada corrida debe ser un objeto JSON.",
                    status_code=400,
                )
            reject_unknown_fields(
                raw_run,
                allowed={
                    "id", "color_produccion_id", "receta_revision_id",
                    "ciclos_objetivo", "objetivo_neto_kg",
                    "operacion_ruta_revision_id", "salidas",
                },
            )
            run = runs_by_id[str(raw_run["id"])]
            if "operacion_ruta_revision_id" in raw_run:
                if order.origen_demanda != "EXCEPCIONAL":
                    raise ScmServiceError(
                        "ROUTE_TARGET_OUT_OF_SCOPE",
                        "Las referencias de ruta por corrida solo aplican a OF excepcionales.",
                        status_code=422,
                    )
                route_fields_changed = True
                route_id = raw_run.get("operacion_ruta_revision_id")
                route_operation = (
                    resolve_route_operation(
                        session,
                        route_id,
                        lock=True,
                        allow_retired=True,
                    )
                    if route_id is not None else None
                )
            else:
                route_operation = None
                if run.operacion_ruta_revision_id is not None:
                    route_operation = resolve_route_operation(
                        session,
                        run.operacion_ruta_revision_id,
                        lock=True,
                        allow_retired=True,
                    )
                    if run.operacion_ruta_hash != _route_content_hash(
                        route_operation.ruta
                    ):
                        raise ScmServiceError(
                            "ROUTE_SNAPSHOT_MISMATCH",
                            "La referencia de ruta de la corrida no coincide con su hash congelado.",
                            status_code=409,
                        )
            if route_operation is not None:
                route_article = route_operation.articulo_salida
                route_piece_color = (
                    route_article is not None
                    and route_article.pieza_color is not None
                    and route_article.pieza_color.pieza_color is not None
                )
                if not route_piece_color:
                    raise ScmServiceError(
                        "ROUTE_TARGET_OUT_OF_SCOPE",
                        "La referencia de ruta por corrida solo aplica a articulos PiezaColor.",
                        status_code=422,
                    )
                if route_operation.id in seen_route_ids:
                    raise ScmServiceError(
                        "DUPLICATE_ROUTE_OPERATION",
                        "Una OF no puede repetir la misma operacion de ruta.",
                        status_code=422,
                    )
                seen_route_ids.add(route_operation.id)
            requested_routes.append(route_operation)
        linked_processes = {
            item.tipo for item in requested_routes if item is not None
        }
        if len(linked_processes) > 1:
            raise ScmServiceError(
                "PROCESS_MISMATCH",
                "Las operaciones de ruta de las corridas requieren procesos distintos.",
                status_code=422,
            )
        all_runs_linked = bool(requested_routes) and all(
            item is not None for item in requested_routes
        )
        current_process = normalize_process(fabrication.snapshot_proceso)
        current_source = fabrication.fuente_proceso
        if process_input_present:
            if explicit_process is not None:
                desired_process = explicit_process
                desired_source = "EXPLICITO"
            elif all_runs_linked:
                desired_process = next(iter(linked_processes))
                desired_source = "RUTA_OBJETIVOS"
            else:
                raise ScmServiceError(
                    "PROCESS_REQUIRED",
                    "proceso:null sólo puede derivarse con todas las corridas enlazadas.",
                    status_code=422,
                )
        elif current_source == "EXPLICITO" and current_process:
            desired_process = current_process
            desired_source = current_source
        elif all_runs_linked:
            desired_process = next(iter(linked_processes))
            desired_source = "RUTA_OBJETIVOS"
        elif current_process and not route_fields_changed:
            desired_process = current_process
            desired_source = current_source
        else:
            raise ScmServiceError(
                "PROCESS_REQUIRED",
                "Las corridas parcialmente enlazadas requieren proceso explicito.",
                status_code=422,
            )
        if linked_processes and desired_process not in linked_processes:
            raise ScmServiceError(
                "PROCESS_MISMATCH",
                "El proceso de la OF no coincide con una operacion de ruta.",
                status_code=422,
            )
        if machine is not None:
            _validate_machine_process(machine, desired_process)
        fabrication.molde_id = mold.codigo
        if machine is not None:
            _validate_machine_process(machine, desired_process)
        fabrication.maquina_prevista_id = machine.id if machine is not None else None
        fabrication.snapshot_tiempo_ciclo_seg = _positive_decimal(
            data.get(
                "snapshot_tiempo_ciclo_seg",
                mold.tiempo_ciclo_std,
            ),
            "snapshot_tiempo_ciclo_seg",
        )
        fabrication.snapshot_horas_turno = _positive_decimal(
            data.get("snapshot_horas_turno"),
            "snapshot_horas_turno",
        )
        fabrication.snapshot_peso_colada_gr = _positive_decimal(
            data.get(
                "snapshot_peso_colada_gr",
                max(mold.peso_colada_gr, 0),
            ),
            "snapshot_peso_colada_gr",
            allow_zero=True,
        )
        for raw_run in raw_runs:
            reject_unknown_fields(
                raw_run,
                allowed={
                    "id",
                    "color_produccion_id",
                    "receta_revision_id",
                    "ciclos_objetivo",
                    "objetivo_neto_kg",
                    "operacion_ruta_revision_id",
                    "salidas",
                },
            )
            run = runs_by_id[str(raw_run["id"])]
            if "operacion_ruta_revision_id" in raw_run:
                route_id = raw_run.get("operacion_ruta_revision_id")
                if route_id is None:
                    run.operacion_ruta_revision_id = None
                    run.operacion_ruta_hash = None
                else:
                    route_operation = resolve_route_operation(session, route_id, lock=True)
                    run.operacion_ruta_revision_id = route_id
                    run.operacion_ruta_hash = route_operation.ruta.content_hash
            raw_outputs = raw_run.get("salidas")
            outputs_by_id = {str(item.id): item for item in run.salidas}
            if (
                not isinstance(raw_outputs, list)
                or {str(item.get("id")) for item in raw_outputs}
                != set(outputs_by_id)
            ):
                raise ScmServiceError(
                    "OF_OUTPUT_MISMATCH",
                    "Deben configurarse exactamente las salidas existentes.",
                    status_code=422,
                )
            minimum_cycles = 1
            prepared = []
            derived_colors = set()
            for raw_output in raw_outputs:
                reject_unknown_fields(
                    raw_output,
                    allowed={"id", "cantidad_por_ciclo", "peso_unitario_g"},
                )
                output = outputs_by_id[str(raw_output["id"])]
                article = output.articulo
                per_cycle = None
                unit_weight = None
                mold_spec = _mold_output_spec(session, output, mold)
                if mold_spec is not None:
                    piece_color = mold_spec["piece_color"]
                    per_cycle = mold_spec["per_cycle"]
                    unit_weight = mold_spec["unit_weight"]
                    if piece_color.color_produccion_id is not None:
                        derived_colors.add(piece_color.color_produccion_id)
                else:
                    if raw_run.get("objetivo_neto_kg") is not None and (
                        raw_output.get("cantidad_por_ciclo") is None
                        or raw_output.get("peso_unitario_g") is None
                    ):
                        raise ScmServiceError(
                            "OF_NET_TARGET_DATA_REQUIRED",
                            "El objetivo neto requiere cantidad por ciclo y peso unitario.",
                            status_code=422,
                        )
                    per_cycle = _positive_decimal(
                        raw_output.get("cantidad_por_ciclo"),
                        "cantidad_por_ciclo",
                    )
                    unit_weight = _positive_decimal(
                        raw_output.get("peso_unitario_g"),
                        "peso_unitario_g",
                    )
                required = (
                    Decimal(output.cantidad_objetivo)
                    - Decimal(output.excedente_objetivo or 0)
                )
                cycles_for_output = int(
                    (required / per_cycle).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                )
                minimum_cycles = max(minimum_cycles, cycles_for_output)
                prepared.append((output, per_cycle, unit_weight, required))
            _validate_physical_output_set(
                session,
                mold,
                raw_run.get("color_produccion_id") or run.color_produccion_id,
                [(item[0].articulo, item[1], item[2]) for item in prepared],
                require_complete=order.origen_demanda == "EXCEPCIONAL",
            )
            if run.operacion_ruta is not None:
                if run.operacion_ruta.articulo_salida_id not in {
                    output.articulo_scm_id for output in outputs_by_id.values()
                }:
                    raise ScmServiceError(
                        "ROUTE_OUTPUT_MISMATCH",
                        "La salida objetivo de la operacion de ruta no pertenece a la corrida.",
                        status_code=422,
                    )
                route_article = run.operacion_ruta.articulo_salida
                route_color = (
                    route_article.pieza_color.pieza_color.color_produccion_id
                    if route_article is not None
                    and route_article.pieza_color is not None
                    and route_article.pieza_color.pieza_color is not None
                    else None
                )
                if route_color is not None and route_color != run.color_produccion_id:
                    raise ScmServiceError(
                        "ROUTE_COLOR_MISMATCH",
                        "La operacion de ruta no corresponde al color de la corrida.",
                        status_code=422,
                    )
            objective = (
                _positive_decimal(raw_run["objetivo_neto_kg"], "objetivo_neto_kg")
                if raw_run.get("objetivo_neto_kg") is not None
                else None
            )
            explicit_cycles = (
                _positive_decimal(
                    raw_run["ciclos_objetivo"],
                    "ciclos_objetivo",
                    integral=True,
                )
                if raw_run.get("ciclos_objetivo") is not None
                else None
            )
            kg_neto_por_ciclo = sum(
                per_cycle * unit_weight / Decimal("1000")
                for _, per_cycle, unit_weight, _ in prepared
            )
            cycles = _run_cycles(
                minimum_cycles=minimum_cycles,
                explicit_cycles=explicit_cycles,
                objective_neto_kg=objective,
                kg_neto_por_ciclo=kg_neto_por_ciclo,
            )
            requested_color = raw_run.get("color_produccion_id")
            if derived_colors:
                if len(derived_colors) != 1:
                    raise ScmServiceError(
                        "OF_RUN_COLOR_MISMATCH",
                        "Una corrida no puede mezclar colores.",
                        status_code=422,
                    )
                derived_color = next(iter(derived_colors))
                if requested_color not in (None, derived_color):
                    raise ScmServiceError(
                        "OF_RUN_COLOR_MISMATCH",
                        "El color no coincide con las salidas PiezaColor.",
                        status_code=422,
                    )
                run.color_produccion_id = derived_color
            else:
                if session.get(ColorProduccion, requested_color) is None:
                    raise ScmServiceError(
                        "COLOR_NOT_FOUND",
                        "La corrida requiere un color válido.",
                        status_code=422,
                    )
                run.color_produccion_id = requested_color
            recipe_id = raw_run.get("receta_revision_id")
            if recipe_id is None:
                default_recipe = _default_recipe_for_run(
                    session,
                    run,
                    [item[0] for item in prepared],
                )
                if default_recipe is not None:
                    recipe_id = default_recipe.id
            if recipe_id is not None:
                _validated_approved_recipe(
                    session,
                    recipe_id,
                    run.color_produccion_id,
                    outputs=[item[0] for item in prepared],
                )
            run.receta_revision_id = recipe_id
            run.ciclos_objetivo = cycles
            run.objetivo_neto_kg = objective
            for output, per_cycle, unit_weight, required in prepared:
                actual = Decimal(cycles) * per_cycle
                output.cantidad_por_ciclo_snapshot = per_cycle
                output.peso_unitario_snapshot_g = unit_weight
                output.cantidad_objetivo = actual
                output.excedente_objetivo = actual - required
                output.kg_estandar_objetivo = (
                    actual * unit_weight / Decimal("1000")
                )
        fabrication.snapshot_proceso = desired_process
        fabrication.fuente_proceso = desired_source
        order.version += 1
        session.flush()
        response = _serialize(session, order)
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="ORDEN_FABRICACION",
            aggregate_id=str(order.id),
            tipo="OF_DRAFT_CONFIGURED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            before_json=before_response,
            after_json=response,
            operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def release_fabrication_order(
    session,
    *,
    actor_id,
    operation_id,
    operation_order_id,
    expected_resource_version,
):
    try:
        actor = load_actor(session, actor_id, capability="OF_LIBERAR")
        data = {
            "order_id": str(operation_order_id),
            "version": expected_version(expected_resource_version),
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /ordenes-fabricacion/{id}/liberar",
            actor,
            data,
        )
        if replay is not None:
            return replay
        order = _load_fabrication(session, operation_order_id, lock=True)
        if order.version != data["version"]:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OF fue modificada por otro usuario.",
                status_code=409,
            )
        if order.estado != "BORRADOR":
            raise ScmServiceError(
                "INVALID_OF_STATE",
                "Solo una OF en BORRADOR puede liberarse.",
                status_code=409,
            )
        before_response = _serialize(session, order)
        fabrication = order.fabricacion
        if bool(fabrication.snapshot_proceso) != bool(fabrication.fuente_proceso):
            raise ScmServiceError(
                "PROCESS_SNAPSHOT_INCOMPLETE",
                "La OF conserva un snapshot de proceso incompleto.",
                status_code=409,
            )
        if not fabrication.snapshot_proceso:
            if order.operacion_ruta_revision_id is None:
                raise ScmServiceError(
                    "PROCESS_REQUIRED",
                    "La OF planificada requiere una operación de ruta de cabecera verificable.",
                    status_code=422,
                )
            header_route = resolve_route_operation(
                session,
                order.operacion_ruta_revision_id,
                lock=True,
                allow_retired=True,
            )
            if (
                not order.operacion_ruta_hash
                or order.operacion_ruta_hash != _route_content_hash(header_route.ruta)
            ):
                raise ScmServiceError(
                    "ROUTE_SNAPSHOT_MISMATCH",
                    "La ruta de cabecera no coincide con su hash congelado.",
                    status_code=409,
                )
            output_ids = {item.articulo_scm_id for item in order.salidas}
            if header_route.articulo_salida_id not in output_ids:
                raise ScmServiceError(
                    "ROUTE_OUTPUT_MISMATCH",
                    "La salida de la operación de cabecera no pertenece a la OF.",
                    status_code=409,
                )
            fabrication.snapshot_proceso = header_route.tipo
            fabrication.fuente_proceso = "RUTA_CABECERA"
        for run in fabrication.corridas:
            if run.operacion_ruta_revision_id is None:
                continue
            route_operation = resolve_route_operation(
                session,
                run.operacion_ruta_revision_id,
                lock=True,
                allow_retired=True,
            )
            if run.operacion_ruta_hash != _route_content_hash(route_operation.ruta):
                raise ScmServiceError(
                    "ROUTE_SNAPSHOT_MISMATCH",
                    "La corrida conserva un hash de ruta que no coincide con la revision.",
                    status_code=409,
                )
            if route_operation.tipo != fabrication.snapshot_proceso:
                raise ScmServiceError(
                    "PROCESS_MISMATCH",
                    "La operación de ruta congelada no coincide con el proceso de la OF.",
                    status_code=409,
                )
        if fabrication.maquina_prevista_id is not None:
            release_machine = session.get(Maquina, fabrication.maquina_prevista_id)
            if release_machine is None or not release_machine.activo:
                raise ScmServiceError(
                    "MACHINE_NOT_FOUND",
                    "La máquina prevista no existe o está inactiva.",
                    status_code=422,
                )
            _validate_machine_process(
                release_machine,
                normalize_process(fabrication.snapshot_proceso),
            )
        release_mold = session.get(Molde, fabrication.molde_id) if fabrication.molde_id else None
        if release_mold is not None:
            for run in fabrication.corridas:
                _validate_physical_output_set(
                    session,
                    release_mold,
                    run.color_produccion_id,
                    [
                        (
                            output.articulo,
                            output.cantidad_por_ciclo_snapshot,
                            output.peso_unitario_snapshot_g,
                        )
                        for output in run.salidas
                    ],
                    require_complete=order.origen_demanda == "EXCEPCIONAL",
                )
        incomplete = (
            not fabrication.molde_id
            or not fabrication.snapshot_tiempo_ciclo_seg
            or not fabrication.snapshot_horas_turno
            or not fabrication.corridas
            or any(
                not run.color_produccion_id
                or not run.ciclos_objetivo
                or not run.salidas
                or any(
                    output.cantidad_por_ciclo_snapshot is None
                    or output.cantidad_por_ciclo_snapshot <= 0
                    or output.peso_unitario_snapshot_g is None
                    or output.peso_unitario_snapshot_g <= 0
                    or output.cantidad_objetivo is None
                    or output.cantidad_objetivo <= 0
                    or output.kg_estandar_objetivo is None
                    or output.kg_estandar_objetivo <= 0
                    for output in run.salidas
                )
                for run in fabrication.corridas
            )
        )
        if incomplete:
            raise ScmServiceError(
                "OF_NOT_RELEASABLE",
                "La OF no tiene configuracion tecnica completa.",
                status_code=422,
            )
        for run in fabrication.corridas:
            if run.receta_revision_id is None and order.origen_demanda == "REEMPLAZO_OF":
                raise ScmServiceError(
                    "APPROVED_RECIPE_REQUIRED",
                    "La OF requiere una formulación aprobada antes de liberarse.",
                    status_code=422,
                )
            if run.receta_revision_id is not None:
                recipe = _validated_approved_recipe(
                    session,
                    run.receta_revision_id,
                    run.color_produccion_id,
                    outputs=run.salidas,
                )
                run.receta_hash = _recipe_content_hash(recipe)
            run.estado = "LIBERADA"
        order.estado = "LIBERADA"
        order.released_by_id = actor.id
        order.released_at = utc_now()
        order.version += 1
        session.flush()
        response = _serialize(session, order)
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="ORDEN_FABRICACION",
            aggregate_id=str(order.id),
            tipo="OF_RELEASED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            before_json=before_response,
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def close_fabrication_order(
    session,
    *,
    actor_id,
    operation_id,
    operation_order_id,
    data,
):
    """Close an OF from effective manga facts and credit its demand.

    This command never creates inventory movements.  Warehouse receipt remains
    the sole authority for Kardex; the close only reconciles operational output
    and the OP allocations that output satisfies.
    """

    # KG production closes are evidence-only.  Route the existing OF action
    # before the UN reconciliation below so a KG output cannot be coerced into
    # ``cantidad_real`` units or credited to an OP.
    actor_probe = load_actor(session, actor_id, capability="OF_CERRAR")
    probe = _load_fabrication(session, operation_order_id, lock=False)
    has_kg_output = any(
        getattr(output.articulo, "unidad_inventario", None) == "KG"
        for output in probe.salidas
    )
    inline_works = session.scalars(
        select(ScmTrabajoOt).where(
            ScmTrabajoOt.orden_operacion_id == operation_order_id
        )
    ).all()
    has_kg_inline_context = any(
        reservation.manga is not None
        and reservation.manga.lote_articulo is not None
        and reservation.manga.lote_articulo.articulo is not None
        and reservation.manga.lote_articulo.articulo.unidad_inventario == "KG"
        for work in inline_works
        for saldo in getattr(work, "saldos_wip_salida", ())
        for reservation in getattr(saldo, "reservas", ())
    )
    if has_kg_output or has_kg_inline_context:
        from app.services.scm_kg_production_service import close_productive_document_kg

        return close_productive_document_kg(
            session,
            actor_id=actor_probe.id,
            documento_tipo="OF",
            documento_id=operation_order_id,
            operation_id=operation_id,
            data=data,
        )

    try:
        actor = load_actor(session, actor_id, capability="OF_CERRAR")
        reject_unknown_fields(data, allowed={"version", "motivo"})
        command = {
            "order_id": str(operation_order_id),
            "version": expected_version(data.get("version")),
            "motivo": (
                str(data.get("motivo") or "").strip() or None
            ),
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /ordenes-fabricacion/{id}/cerrar",
            actor,
            command,
        )
        if replay is not None:
            return replay
        # El TrabajoColor es la raíz de bloqueo para producción en línea.
        # Tomarlo antes de la OF mantiene el mismo orden que cierre/corrección
        # de Armado y evita el ciclo OF -> trabajo frente a trabajo -> reserva.
        works = session.scalars(
            select(ScmTrabajoOt)
            .where(
                ScmTrabajoOt.orden_operacion_id == operation_order_id
            )
            .order_by(ScmTrabajoOt.created_at, ScmTrabajoOt.id)
            .with_for_update(of=ScmTrabajoOt)
        ).all()
        order = _load_fabrication(
            session, operation_order_id, lock=True
        )
        if order.version != command["version"]:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OF fue modificada por otro usuario.",
                status_code=409,
            )
        if order.estado != "EN_EJECUCION":
            raise ScmServiceError(
                "INVALID_OF_STATE",
                "Solo una OF en ejecución puede cerrarse.",
                status_code=409,
            )
        active_works = [item for item in works if item.estado != "ANULADO"]
        if not active_works:
            raise ScmServiceError(
                "OF_WORK_REQUIRED",
                "La OF no tiene trabajos ejecutados que puedan acreditarse.",
                status_code=409,
            )
        pending_works = [
            item for item in active_works if item.estado != "COMPLETADO"
        ]
        if pending_works:
            raise ScmServiceError(
                "OF_HAS_PENDING_WORKS",
                "La OF conserva trabajos de color sin completar.",
                status_code=409,
                details={
                    "trabajos": [item.codigo for item in pending_works],
                },
            )
        work_ids = [item.id for item in active_works]
        pending_inline_reservations = session.scalars(
            select(ScmReservaWipSalida)
            .join(
                ScmSaldoWipSalida,
                ScmSaldoWipSalida.id == ScmReservaWipSalida.saldo_id,
            )
            .where(
                ScmSaldoWipSalida.trabajo_color_id.in_(work_ids),
                ScmReservaWipSalida.estado
                == "CREDITO_EN_LINEA_PENDIENTE",
            )
            .order_by(ScmReservaWipSalida.id)
            .with_for_update(of=ScmReservaWipSalida)
        ).all()
        if pending_inline_reservations:
            raise ScmServiceError(
                "OF_HAS_PENDING_INLINE_RESERVATIONS",
                "La OF conserva reservas de producción en línea sin conciliar.",
                status_code=409,
                details={
                    "reservas": [
                        str(item.id) for item in pending_inline_reservations
                    ],
                },
            )
        mangas = session.scalars(
            select(ScmManga)
            .join(ScmTrabajoOt, ScmTrabajoOt.id == ScmManga.trabajo_ot_id)
            .where(ScmTrabajoOt.orden_operacion_id == order.id)
            .order_by(ScmManga.id)
            .with_for_update(of=ScmManga)
        ).all()
        terminal_states = {
            "PESADA",
            "ETIQUETADA_FINAL",
            "PENDIENTE_RECEPCION_ALMACEN",
            "RECIBIDA",
            "ANULADA",
        }
        pending_mangas = [
            item for item in mangas
            if item.estado not in terminal_states
            or (
                item.estado != "ANULADA"
                and item.cantidad_confirmada_un is None
            )
        ]
        if pending_mangas:
            raise ScmServiceError(
                "OF_HAS_PENDING_MANGAS",
                "La OF conserva mangas sin pesar o anular.",
                status_code=409,
                details={
                    "mangas": [item.codigo for item in pending_mangas],
                },
            )
        output_by_id = {item.id: item for item in order.salidas}
        actual_by_output = {item.id: Decimal("0") for item in order.salidas}
        for manga in mangas:
            if manga.estado == "ANULADA":
                continue
            output_id = manga.plan_linea.orden_operacion_salida_id
            if output_id not in output_by_id:
                raise ScmServiceError(
                    "OF_MANGA_OUTPUT_MISMATCH",
                    "Una manga no corresponde a una salida de la OF.",
                    status_code=409,
                    details={"manga": manga.codigo},
                )
            actual_by_output[output_id] += Decimal(
                manga.cantidad_confirmada_un
            )
        inline_balances = session.scalars(
            select(ScmSaldoWipSalida)
            .where(ScmSaldoWipSalida.trabajo_color_id.in_(work_ids))
            .order_by(ScmSaldoWipSalida.id)
            .with_for_update(of=ScmSaldoWipSalida)
        ).all()
        for balance in inline_balances:
            output_id = balance.orden_operacion_salida_id
            if output_id not in output_by_id:
                raise ScmServiceError(
                    "OF_INLINE_OUTPUT_MISMATCH",
                    "Un crédito en línea no corresponde a una salida de la OF.",
                    status_code=409,
                    details={"saldo_wip_salida_id": str(balance.id)},
                )
            actual_by_output[output_id] += Decimal(
                balance.cantidad_acreditada
            )
        differences = []
        for output in order.salidas:
            actual = actual_by_output[output.id]
            target = Decimal(output.cantidad_objetivo)
            if actual != target:
                differences.append({
                    "salida_id": str(output.id),
                    "articulo": output.articulo.codigo,
                    "objetivo": format(target, "f"),
                    "real": format(actual, "f"),
                })
        if differences and command["motivo"] is None:
            raise ScmServiceError(
                "OF_CLOSE_REASON_REQUIRED",
                "Explique la diferencia entre la producción objetivo y real.",
                status_code=422,
                details={"diferencias": differences},
            )
        fulfillment = []
        closed_at = utc_now()
        for output in order.salidas:
            actual = actual_by_output[output.id]
            output.cantidad_real = actual
            output.cantidad_rechazada = Decimal("0")
            lot = session.scalar(
                select(ScmLoteArticulo)
                .where(
                    ScmLoteArticulo.orden_operacion_salida_id == output.id
                )
                .with_for_update(of=ScmLoteArticulo)
            )
            if lot is None:
                lot = ScmLoteArticulo(
                    codigo=(
                        f"LOT-{order.codigo}-{str(output.id)[:8]}"
                    ).upper()[:64],
                    articulo_id=output.articulo_scm_id,
                    clase="SALIDA_ORDEN_OPERACION",
                    orden_operacion_salida_id=output.id,
                )
                session.add(lot)
            lot.cantidad_acreditada = actual
            lot.event_time = closed_at
            lot.actor_id = actor.id
            fulfillment.append({
                "salida_id": str(output.id),
                "articulo": output.articulo.codigo,
                "cantidad_real": format(actual, "f"),
                **credit_output_allocations(output, actual),
            })
        for run in order.fabricacion.corridas:
            if run.estado != "ANULADA":
                run.estado = "COMPLETADA"
        order.estado = "CERRADA"
        order.closed_by_id = actor.id
        order.closed_at = closed_at
        order.version += 1
        op_projections = project_production_orders_for_operation(
            session,
            operation_order=order,
            actor=actor,
            operation=operation,
        )
        session.flush()
        response = {
            **_serialize(session, order),
            "cierre": {
                "motivo": command["motivo"],
                "diferencias": differences,
                "salidas": fulfillment,
                "ordenes_produccion": op_projections,
            },
        }
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="ORDEN_FABRICACION",
            aggregate_id=str(order.id),
            tipo="OF_CLOSED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            motivo=command["motivo"],
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
