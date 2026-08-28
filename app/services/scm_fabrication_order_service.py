"""Services for canonical fabrication orders introduced by TS-010P."""

import copy
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from sqlalchemy import select
from sqlalchemy.orm import joinedload, load_only, selectinload

from app.models.maquina import Maquina
from app.models.molde import Molde, MoldePieza
from app.models.producto import ColorProduccion, PiezaColor
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_articulos import ScmArticulo, ScmArticuloPiezaColor
from app.models.scm_auditoria import ScmEvento
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    utc_now,
)
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_color_identity import serialize_color_identity
from app.services.scm_operation_schedule_projection import (
    operation_schedule_projection,
    operation_schedule_projections,
)
from app.services.scm_production_order_service import (
    _iso,
    _reserve_operation,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)


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


def _decimal_text(value, scale):
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-scale)
    return format(Decimal(value).quantize(quantum), "f")


def _serialize_output(output):
    piece_id = None
    if output.articulo and output.articulo.pieza_color is not None:
        piece_id = output.articulo.pieza_color.pieza_color.pieza_id
    return {
        "id": str(output.id),
        "articulo_scm_id": output.articulo_scm_id,
        "articulo": {
            "codigo": output.articulo.codigo,
            "nombre": output.articulo.nombre,
            "clase": output.articulo.clase,
            "pieza_id": piece_id,
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
        "kg_estandar_objetivo": (
            _decimal_text(output.kg_estandar_objetivo, 6)
            if output.kg_estandar_objetivo is not None
            else None
        ),
        "excedente_objetivo": _decimal_text(output.excedente_objetivo, 3),
        "lote_salida_legacy_id": output.lote_salida_legacy_id,
    }


def _serialize_run(run):
    color_identity = serialize_color_identity(
        run.color_produccion,
        color_id=run.color_produccion_id,
    )
    color_name = color_identity["nombre"] if color_identity else None
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
        "ciclos_objetivo": run.ciclos_objetivo,
        "estado": run.estado,
        "lote_color_legacy_id": run.lote_color_legacy_id,
        "meta_kg_legacy": (
            format(run.meta_kg_legacy, "f")
            if run.meta_kg_legacy is not None
            else None
        ),
        "salidas": [_serialize_output(output) for output in run.salidas],
    }


def _serialize(session, operation, *, schedule_projection=None):
    fabrication = operation.fabricacion
    route_operation = operation.operacion_ruta_revision
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
        "version": operation.version,
        "plan_produccion_id": (
            str(operation.plan_produccion_id)
            if operation.plan_produccion_id else None
        ),
        "propuesta_clave": operation.propuesta_clave,
        "proceso_requerido": (
            route_operation.tipo if route_operation is not None else None
        ),
        "created_by_id": operation.created_by_id,
        "released_by_id": operation.released_by_id,
        "released_at": _iso(operation.released_at),
        "started_by_id": operation.started_by_id,
        "started_at": _iso(operation.started_at),
        "closed_by_id": operation.closed_by_id,
        "closed_at": _iso(operation.closed_at),
        "created_at": _iso(operation.created_at),
        "updated_at": _iso(operation.updated_at),
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
        "corridas": [_serialize_run(run) for run in fabrication.corridas],
    }


def _normalized_process(value):
    return str(value or "").strip().upper()


def _machine_processes(machine):
    machine_type = machine.tipo_maquina
    return {
        _normalized_process(value)
        for value in (
            machine_type.proceso if machine_type else None,
            machine_type.codigo if machine_type else None,
            machine_type.nombre if machine_type else None,
            machine.tipo,
        )
        if _normalized_process(value)
    }


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
    route_operation = operation.operacion_ruta_revision
    required_process = _normalized_process(
        route_operation.tipo if route_operation is not None else None
    )
    if required_process and required_process not in _machine_processes(machine):
        raise ScmServiceError(
            "MACHINE_PROCESS_INCOMPATIBLE",
            "La maquina prevista no corresponde al proceso de la OF.",
            status_code=422,
            details={
                "maquina_id": machine.id,
                "proceso_requerido": required_process,
                "procesos_maquina": sorted(_machine_processes(machine)),
            },
        )


def _load_fabrication(session, operation_id):
    operation = session.get(ScmOrdenOperacion, operation_id)
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
    fabrication_runs = (
        selectinload(ScmOrdenOperacion.fabricacion)
        .selectinload(ScmOrdenFabricacion.corridas)
    )
    run_outputs = fabrication_runs.selectinload(
        ScmCorridaFabricacion.salidas
    )
    output_articles = run_outputs.selectinload(
        ScmOrdenOperacionSalida.articulo
    )
    operations = session.scalars(
        select(ScmOrdenOperacion)
        .where(ScmOrdenOperacion.tipo == "FABRICACION")
        .options(
            selectinload(ScmOrdenOperacion.fabricacion),
            selectinload(ScmOrdenOperacion.operacion_ruta_revision),
            fabrication_runs
            .joinedload(ScmCorridaFabricacion.color_produccion)
            .joinedload(ColorProduccion.color_base_rel),
            fabrication_runs
            .joinedload(ScmCorridaFabricacion.color_produccion)
            .joinedload(ColorProduccion.familia_color_rel),
            output_articles,
            output_articles
            .selectinload(ScmArticulo.pieza_color)
            .selectinload(ScmArticuloPiezaColor.pieza_color)
            .load_only(PiezaColor.pieza_id),
        )
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
    return _serialize(session, _load_fabrication(session, operation_id))


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
        if session.get(Molde, mold_id) is None:
            raise ScmServiceError(
                "MOLD_NOT_FOUND",
                "El molde indicado no existe.",
                status_code=422,
            )
        machine_id = data.get("maquina_prevista_id")
        if machine_id is not None and session.get(Maquina, machine_id) is None:
            raise ScmServiceError(
                "MACHINE_NOT_FOUND",
                "La maquina prevista no existe.",
                status_code=422,
            )
        raw_runs = data.get("corridas")
        if not isinstance(raw_runs, list) or not raw_runs:
            raise ScmServiceError(
                "OF_CORRIDA_REQUIRED",
                "La OF requiere al menos una corrida.",
                status_code=422,
            )
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
        )
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
            recipe_id = raw_run.get("receta_revision_id")
            recipe = (
                session.get(RecetaColorMaestra, recipe_id)
                if recipe_id is not None
                else None
            )
            if recipe_id is not None and (
                recipe is None or recipe.color_produccion_id != color_id
            ):
                raise ScmServiceError(
                    "RECIPE_COLOR_MISMATCH",
                    "La receta no corresponde al color de la corrida.",
                    status_code=422,
                )
            cycles = int(_positive_decimal(
                raw_run.get("ciclos_objetivo"),
                "ciclos_objetivo",
                integral=True,
            ))
            raw_outputs = raw_run.get("salidas")
            if not isinstance(raw_outputs, list) or not raw_outputs:
                raise ScmServiceError(
                    "OF_OUTPUT_REQUIRED",
                    "Cada corrida requiere al menos una salida.",
                    status_code=422,
                )
            run = ScmCorridaFabricacion(
                codigo=f"{code}-C{sequence:02d}",
                secuencia=sequence,
                color_produccion_id=color_id,
                receta_revision_id=recipe_id,
                # RecetaColorMaestra aun no persiste content_hash; la FK y
                # revision son el snapshot disponible en esta fase expand.
                receta_hash=None,
                ciclos_objetivo=cycles,
            )
            fabrication.corridas.append(run)
            seen_articles = set()
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
                per_cycle = _positive_decimal(
                    raw_output.get("cantidad_por_ciclo"),
                    "cantidad_por_ciclo",
                )
                unit_weight = _positive_decimal(
                    raw_output.get("peso_unitario_g"),
                    "peso_unitario_g",
                )
                calculated_quantity = Decimal(cycles) * per_cycle
                quantity = (
                    _positive_decimal(
                        raw_output["cantidad_objetivo"],
                        "cantidad_objetivo",
                    )
                    if raw_output.get("cantidad_objetivo") is not None
                    else calculated_quantity
                )
                output = ScmOrdenOperacionSalida(
                    orden_operacion=order,
                    corrida_fabricacion=run,
                    articulo_scm_id=article_id,
                    cantidad_por_ciclo_snapshot=per_cycle,
                    peso_unitario_snapshot_g=unit_weight,
                    cantidad_objetivo=quantity,
                    kg_estandar_objetivo=(
                        quantity * unit_weight / Decimal("1000")
                    ),
                )
                seen_articles.add(article_id)
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
        machine = session.get(Maquina, machine_id)
        if machine is None or not machine.activo:
            raise ScmServiceError(
                "MACHINE_NOT_FOUND",
                "La máquina prevista no existe o está inactiva.",
                status_code=422,
            )
        _validate_machine_for_operation(machine, order)
        fabrication = order.fabricacion
        fabrication.molde_id = mold.codigo
        fabrication.maquina_prevista_id = machine.id
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
        raw_runs = data.get("corridas")
        if not isinstance(raw_runs, list) or not raw_runs:
            raise ScmServiceError(
                "OF_CORRIDA_REQUIRED",
                "La configuración requiere las corridas de la OF.",
                status_code=422,
            )
        runs_by_id = {str(item.id): item for item in fabrication.corridas}
        if {str(item.get("id")) for item in raw_runs} != set(runs_by_id):
            raise ScmServiceError(
                "OF_CORRIDA_MISMATCH",
                "Deben configurarse exactamente las corridas existentes.",
                status_code=422,
            )
        for raw_run in raw_runs:
            reject_unknown_fields(
                raw_run,
                allowed={
                    "id",
                    "color_produccion_id",
                    "receta_revision_id",
                    "ciclos_objetivo",
                    "salidas",
                },
            )
            run = runs_by_id[str(raw_run["id"])]
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
                if article.pieza_color is not None:
                    piece_color = article.pieza_color.pieza_color
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
                            "El molde no fabrica una salida PiezaColor de la OF.",
                            status_code=422,
                            details={"salida_id": str(output.id)},
                        )
                    per_cycle = Decimal(composition.cavidades)
                    unit_weight = Decimal(str(composition.peso_unitario_gr))
                    if piece_color.color_produccion_id is not None:
                        derived_colors.add(piece_color.color_produccion_id)
                else:
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
            cycles = int(_positive_decimal(
                raw_run.get("ciclos_objetivo", minimum_cycles),
                "ciclos_objetivo",
                integral=True,
            ))
            if cycles < minimum_cycles:
                raise ScmServiceError(
                    "OF_CYCLES_INSUFFICIENT",
                    "Los ciclos no cubren la cantidad requerida.",
                    status_code=422,
                    details={"minimum_cycles": minimum_cycles},
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
            if recipe_id is not None:
                recipe = session.get(RecetaColorMaestra, recipe_id)
                if (
                    recipe is None
                    or recipe.color_produccion_id != run.color_produccion_id
                ):
                    raise ScmServiceError(
                        "RECIPE_COLOR_MISMATCH",
                        "La receta no corresponde al color de la corrida.",
                        status_code=422,
                    )
            run.receta_revision_id = recipe_id
            run.ciclos_objetivo = cycles
            for output, per_cycle, unit_weight, required in prepared:
                actual = Decimal(cycles) * per_cycle
                output.cantidad_por_ciclo_snapshot = per_cycle
                output.peso_unitario_snapshot_g = unit_weight
                output.cantidad_objetivo = actual
                output.excedente_objetivo = actual - required
                output.kg_estandar_objetivo = (
                    actual * unit_weight / Decimal("1000")
                )
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
        order = _load_fabrication(session, operation_order_id)
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
        fabrication = order.fabricacion
        incomplete = (
            not fabrication.molde_id
            or not fabrication.maquina_prevista_id
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
            if run.receta_revision_id is not None:
                recipe = session.get(
                    RecetaColorMaestra,
                    run.receta_revision_id,
                )
                if recipe is None or recipe.estado != "APROBADA":
                    raise ScmServiceError(
                        "OF_NOT_RELEASABLE",
                        "Todas las recetas informadas deben estar aprobadas.",
                        status_code=422,
                    )
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
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
