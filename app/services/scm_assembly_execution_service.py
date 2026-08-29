"""TS-010F: plan de mangas, cierre de Armado y genealogia exacta."""

import copy
import math
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from app.models.registro import RegistroDiarioProduccion
from app.models.scm_assembly_execution import (
    ScmConfirmacionMangaArmado,
    ScmConsumoComponenteArmado,
    ScmCorreccionMangaArmado,
)
from app.models.scm_auditoria import ScmEvento
from app.models.scm_estructuras import ScmEstructuraRevision
from app.models.scm_internal_supply import (
    ScmAsignacionAbastecimiento,
    ScmAsignacionPoolArmado,
    ScmSolicitudAbastecimiento,
)
from app.models.scm_inventory import ScmMovimientoInventario, ScmSaldoInventario
from app.models.scm_inline_wip import (
    ScmMovimientoWipSalida,
    ScmReservaWipSalida,
    ScmSaldoWipSalida,
)
from app.models.scm_ot import (
    ScmAsignacionPlanMangaOt,
    ScmLoteArticulo,
    ScmManga,
    ScmPlanMangaOp,
    ScmPlanMangaOpLinea,
    ScmTrabajoOt,
    utc_now,
)
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.models.scm_rutas import ScmOperacionRuta
from app.models.scm_warehouse import ScmExistenciaManga
from app.services.scm_ot_service import (
    _approved_manga_rule,
    _complete_operation,
    _create_mangas,
    _json_hash,
    _reserve_operation,
    _serialize_manga,
    _serialize_ot,
    _serialize_plan,
)
from app.services.scm_packaging_service import calculate_packaging_capacity
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)


QUANTUM = Decimal("0.001")


def _inline_source_for_ot(session, *, ot, structure, lock=False):
    if ot.modo_ejecucion_ensamble != "CONCURRENTE":
        return None
    if ot.trabajo_color_contexto_id is None:
        raise ScmServiceError(
            "INLINE_COLOR_WORK_CONTEXT_REQUIRED",
            "El armado concurrente requiere un TrabajoColor exacto.",
            status_code=409,
        )
    statement = select(ScmTrabajoOt).where(
        ScmTrabajoOt.id == ot.trabajo_color_contexto_id,
        ScmTrabajoOt.tipo == "COLOR",
    )
    if lock:
        statement = statement.with_for_update()
    work = session.scalar(statement)
    if (
        work is None
        or work.orden_operacion is None
        or work.orden_operacion.tipo != "FABRICACION"
        or work.trabajo_color is None
        or work.trabajo_color.corrida is None
    ):
        raise ScmServiceError(
            "INLINE_SOURCE_OUTPUT_UNRESOLVED",
            "El TrabajoColor no conserva una corrida de fabricación resoluble.",
            status_code=409,
        )
    component_by_article = {
        item.articulo_componente_id: item for item in structure.componentes
    }
    matches = [
        (component_by_article[output.articulo_scm_id], output)
        for output in work.trabajo_color.corrida.salidas
        if output.orden_operacion_id == work.orden_operacion_id
        and output.articulo_scm_id in component_by_article
    ]
    if len(matches) != 1:
        raise ScmServiceError(
            "INLINE_SOURCE_OUTPUT_AMBIGUOUS",
            "La BOM debe coincidir con exactamente una salida del TrabajoColor.",
            status_code=422,
            details={
                "trabajo_color_id": str(work.id),
                "coincidencias": len(matches),
            },
        )
    component, output = matches[0]
    if output.articulo is None or not output.articulo.activo:
        raise ScmServiceError(
            "INLINE_SOURCE_ARTICLE_INACTIVE",
            "La salida fresca seleccionada debe permanecer activa.",
            status_code=422,
        )
    return work, component, output


def _inline_plan_allocation(
    session, *, work, output, actor=None, create=False
):
    line_statement = (
        select(ScmPlanMangaOpLinea)
        .join(ScmPlanMangaOp)
        .where(
            ScmPlanMangaOp.orden_operacion_id == work.orden_operacion_id,
            ScmPlanMangaOp.estado == "ACTIVO",
            ScmPlanMangaOpLinea.orden_operacion_salida_id == output.id,
        )
        .with_for_update(of=ScmPlanMangaOpLinea)
    )
    line = session.scalar(line_statement)
    if line is None:
        raise ScmServiceError(
            "INLINE_SOURCE_PLAN_MISSING",
            "La salida fresca no conserva una cuota activa del plan de mangas.",
            status_code=409,
            details={"orden_operacion_salida_id": str(output.id)},
        )
    assignment = session.scalar(
        select(ScmAsignacionPlanMangaOt)
        .where(
            ScmAsignacionPlanMangaOt.plan_linea_id == line.id,
            ScmAsignacionPlanMangaOt.trabajo_ot_id == work.id,
        )
        .with_for_update()
    )
    if assignment is None and create:
        assignment = ScmAsignacionPlanMangaOt(
            plan_linea_id=line.id,
            ot_id=work.orden_trabajo_id,
            trabajo_ot_id=work.id,
            cantidad_asignada_un=Decimal("0"),
            mangas_asignadas=0,
            asignada_por_id=actor.id,
        )
        session.add(assignment)
        session.flush()
    if assignment is None:
        raise ScmServiceError(
            "INLINE_SOURCE_PLAN_ALLOCATION_MISSING",
            "La reserva perdió su asignación de cuota del TrabajoColor.",
            status_code=409,
            details={"trabajo_color_id": str(work.id)},
        )
    return line, assignment


def _release_inline_plan_quota(
    *, work, assignment, quantity, error_code="INLINE_RESERVATION_CONTEXT_MISMATCH"
):
    """Return unused inline output to the exact source work allocation.

    Callers must already hold row locks for ``work`` and ``assignment``.  The
    invariant deliberately includes the confirmed quantity so a cancellation
    or partial close can never shrink the source target below facts that have
    already been credited.
    """
    amount = Decimal(quantity).quantize(QUANTUM)
    assigned = Decimal(assignment.cantidad_asignada_un)
    target = Decimal(work.cantidad_objetivo_un or 0)
    confirmed = Decimal(work.cantidad_confirmada_un or 0)
    if (
        amount < 0
        or assigned < amount
        or target - confirmed < amount
    ):
        raise ScmServiceError(
            error_code,
            "La cuota reservada ya no coincide con el TrabajoColor de origen.",
            status_code=409,
            details={
                "trabajo_color_id": str(work.id),
                "cantidad_liberar": format(amount, "f"),
                "cantidad_asignada": format(assigned, "f"),
                "cantidad_objetivo": format(target, "f"),
                "cantidad_confirmada": format(confirmed, "f"),
            },
        )
    if amount == 0:
        return
    assignment.cantidad_asignada_un = assigned - amount
    work.cantidad_objetivo_un = target - amount


def _reserve_inline_output_for_mangas(
    session, *, ot, structure, mangas, actor, operation
):
    source = _inline_source_for_ot(
        session, ot=ot, structure=structure, lock=True
    )
    if source is None:
        return []
    work, component, output = source
    line, assignment = _inline_plan_allocation(
        session, work=work, output=output, actor=actor, create=True
    )
    assigned_total = session.scalar(
        select(func.coalesce(
            func.sum(ScmAsignacionPlanMangaOt.cantidad_asignada_un), 0
        )).where(ScmAsignacionPlanMangaOt.plan_linea_id == line.id)
    )
    available = Decimal(line.cantidad_objetivo_un) - Decimal(assigned_total)
    required_by_manga = []
    for manga in sorted(mangas, key=lambda item: item.id):
        required = (
            Decimal(manga.cantidad_asignada_un)
            * Decimal(component.cantidad)
        ).quantize(QUANTUM)
        if required <= 0 or required > available:
            raise ScmServiceError(
                "INLINE_OUTPUT_QUOTA_EXCEEDED",
                "La reserva en línea excede la salida autorizada del TrabajoColor.",
                status_code=409,
                details={
                    "trabajo_color_id": str(work.id),
                    "articulo_id": output.articulo_scm_id,
                    "requerido": format(required, "f"),
                    "disponible": format(available, "f"),
                },
            )
        required_by_manga.append((manga, required))
        available -= required

    reserved_total = sum(
        (required for _manga, required in required_by_manga),
        Decimal("0"),
    )
    assignment.cantidad_asignada_un = (
        Decimal(assignment.cantidad_asignada_un) + reserved_total
    )
    work.cantidad_objetivo_un = (
        Decimal(work.cantidad_objetivo_un or 0) + reserved_total
    )
    work.version += 1
    saldo = session.scalar(
        select(ScmSaldoWipSalida)
        .where(
            ScmSaldoWipSalida.trabajo_color_id == work.id,
            ScmSaldoWipSalida.orden_operacion_salida_id == output.id,
        )
        .with_for_update()
    )
    if saldo is None:
        saldo = ScmSaldoWipSalida(
            trabajo_color_id=work.id,
            orden_operacion_salida_id=output.id,
            articulo_id=output.articulo_scm_id,
        )
        session.add(saldo)
        session.flush()
    reservations = []
    for manga, required in required_by_manga:
        reservation = ScmReservaWipSalida(
            saldo=saldo,
            manga_id=manga.id,
            asignacion_plan_id=assignment.id,
            articulo_componente_id=component.articulo_componente_id,
            cantidad_reservada=required,
            creada_por_id=actor.id,
            operation_id=operation.operation_id,
        )
        session.add(reservation)
        reservations.append(reservation)
    session.flush()
    return reservations


def _quantity(value, field="cantidad_real"):
    try:
        parsed = Decimal(str(value))
        quantized = parsed.quantize(QUANTUM)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "ASSEMBLY_QUANTITY_REQUIRED",
            f"{field} debe ser una cantidad positiva con hasta tres decimales.",
            status_code=422,
            details={"field": field},
        ) from error
    if not parsed.is_finite() or parsed != quantized or quantized <= 0:
        raise ScmServiceError(
            "ASSEMBLY_QUANTITY_REQUIRED",
            f"{field} debe ser una cantidad positiva con hasta tres decimales.",
            status_code=422,
            details={"field": field},
        )
    return quantized


def _event(aggregate_type, aggregate_id, event_type, actor, operation, after):
    return ScmEvento(
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        after_json=copy.deepcopy(after),
        operation_id=operation.operation_id,
    )


def _load_order(session, order_id, *, lock=False):
    statement = select(ScmOrdenOperacion).where(
        ScmOrdenOperacion.id == order_id,
        ScmOrdenOperacion.tipo == "ENSAMBLE",
    )
    if lock:
        statement = statement.with_for_update()
    order = session.scalar(statement)
    if order is None:
        raise ScmServiceError("OA_NOT_FOUND", "La OA no existe.", status_code=404)
    return order


def _load_assembly_ot(session, public_id, *, lock=False):
    statement = select(RegistroDiarioProduccion).where(
        RegistroDiarioProduccion.public_id == public_id,
        RegistroDiarioProduccion.tipo_ot == "ENSAMBLE",
    )
    if lock:
        statement = statement.with_for_update()
    ot = session.scalar(statement)
    if ot is None:
        raise ScmServiceError(
            "OT_ENSAMBLE_NOT_FOUND",
            "La OT diaria de Armado no existe.",
            status_code=404,
        )
    return ot


def _master_weight_g(article):
    if article.pieza_color is not None:
        value = article.pieza_color.pieza_color.peso
    elif article.producto is not None:
        value = article.producto.producto_terminado.peso_g
    else:
        value = None
    if value is None or Decimal(str(value)) <= 0:
        return None
    return Decimal(str(value)).quantize(Decimal("0.0001"))


def _article_weight_g(session, article, *, preferred_structure=None, visited=None):
    direct = _master_weight_g(article)
    if direct is not None:
        return direct
    visited = set(visited or ())
    if article.id in visited:
        return None
    visited.add(article.id)
    structure = preferred_structure
    if structure is None or structure.articulo_resultado_id != article.id:
        structure = session.scalar(select(ScmEstructuraRevision).where(
            ScmEstructuraRevision.articulo_resultado_id == article.id,
            ScmEstructuraRevision.estado == "APROBADA",
        ))
    if structure is None:
        return None
    total = Decimal("0")
    for component in structure.componentes:
        weight = _article_weight_g(
            session,
            component.articulo_componente,
            visited=visited,
        )
        if weight is None:
            return None
        total += weight * Decimal(component.cantidad)
    return total.quantize(Decimal("0.0001")) if total > 0 else None


def get_assembly_manga_plan(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="PLAN_MANGA_VER")
    _load_order(session, order_id)
    plan = session.scalar(select(ScmPlanMangaOp).where(
        ScmPlanMangaOp.orden_operacion_id == order_id,
        ScmPlanMangaOp.estado == "ACTIVO",
    ))
    return {"plan": _serialize_plan(plan) if plan else None}


def recalculate_assembly_manga_plan(
    session, *, actor_id, order_id, operation_id, data
):
    reject_unknown_fields(data, allowed=set())
    actor = load_actor(session, actor_id, capability="ENSAMBLE_PLANIFICAR")
    endpoint = f"/ordenes-armado/{order_id}/plan-mangas/recalcular"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        order = _load_order(session, order_id, lock=True)
        if order.estado not in ("LIBERADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "OA_NOT_RELEASED",
                "La OA debe estar liberada para planificar sus mangas de salida.",
                status_code=409,
            )
        if len(order.salidas) != 1:
            raise ScmServiceError(
                "OA_OUTPUT_INVALID", "La OA debe poseer una sola salida.", status_code=409
            )
        previous = session.scalar(
            select(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOp.orden_operacion_id == order.id,
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        if previous is not None:
            materialized = session.scalar(
                select(func.count(ScmManga.id))
                .join(ScmPlanMangaOpLinea)
                .where(ScmPlanMangaOpLinea.plan_id == previous.id)
            )
            if materialized:
                raise ScmServiceError(
                    "ASSEMBLY_PLAN_ALREADY_MATERIALIZED",
                    "El plan ya tiene mangas. Ajusta la OA mediante una nueva revision autorizada.",
                    status_code=409,
                )

        output = order.salidas[0]
        article = output.articulo
        route_operation = session.get(ScmOperacionRuta, order.operacion_ruta_revision_id)
        structure = route_operation.estructura_revision if route_operation else None
        weight = (
            Decimal(output.peso_unitario_snapshot_g)
            if output.peso_unitario_snapshot_g is not None
            else _article_weight_g(
                session, article, preferred_structure=structure
            )
        )
        if weight is None or weight <= 0:
            raise ScmServiceError(
                "ASSEMBLY_OUTPUT_WEIGHT_MISSING",
                "La salida necesita un peso unitario para calcular la capacidad de manga.",
                status_code=422,
                details={"articulo_id": article.id},
            )
        profile_link, rule_revision = _approved_manga_rule(session, article.id)
        container = rule_revision.regla.tipo_contenedor
        capacity_result = calculate_packaging_capacity(
            tara_nominal_g=Decimal(
                rule_revision.tara_nominal_g_snapshot
                if rule_revision.tara_nominal_g_snapshot is not None
                else container.tara_nominal_g
            ),
            tolerancia_tara_g=Decimal(
                rule_revision.tolerancia_tara_g_snapshot
                if rule_revision.tolerancia_tara_g_snapshot is not None
                else container.tolerancia_tara_g
            ),
            peso_bruto_max_kg=Decimal(
                rule_revision.peso_bruto_max_kg_snapshot
                if rule_revision.peso_bruto_max_kg_snapshot is not None
                else container.peso_bruto_max_kg
            ),
            peso_neto_operativo_max_kg=Decimal(
                rule_revision.peso_neto_operativo_max_kg
            ),
            margen_seguridad_kg=Decimal(rule_revision.margen_seguridad_kg),
            cantidad_objetivo_un=rule_revision.cantidad_objetivo_un,
            cantidad_maxima_probada_un=rule_revision.cantidad_maxima_probada_un,
            peso_unitario_snapshot_g=weight,
        )
        objective = _quantity(output.cantidad_objetivo, "cantidad_objetivo")
        capacity = capacity_result["capacidad_efectiva_un"]
        lot = session.scalar(select(ScmLoteArticulo).where(
            ScmLoteArticulo.orden_operacion_salida_id == output.id
        ))
        if lot is None:
            lot = ScmLoteArticulo(
                codigo=f"LOT-{order.codigo}-{str(output.id)[:8]}".upper()[:64],
                articulo_id=article.id,
                clase="SALIDA_ORDEN_OPERACION",
                orden_operacion_salida_id=output.id,
            )
            session.add(lot)
            session.flush()
        revision = previous.revision + 1 if previous else 1
        content = [{
            "salida_id": str(output.id),
            "articulo_id": article.id,
            "cantidad_objetivo_un": format(objective, "f"),
            "capacidad_efectiva_un": capacity,
            "regla_revision_id": rule_revision.id,
            "regla_hash": rule_revision.content_hash,
        }]
        plan = ScmPlanMangaOp(
            orden_operacion_id=order.id,
            revision=revision,
            calculado_por_id=actor.id,
            operation_id=operation.operation_id,
            content_hash=_json_hash(content),
        )
        if previous is not None:
            previous.estado = "SUPERADO"
        session.add(plan)
        session.flush()
        piece_sku = (
            article.pieza_color.pieza_color_sku
            if article.pieza_color is not None else None
        )
        session.add(ScmPlanMangaOpLinea(
            plan_id=plan.id,
            orden_operacion_salida_id=output.id,
            lote_articulo_id=lot.id,
            perfil_empacable_id=profile_link.perfil_empacable_id,
            regla_revision_id=rule_revision.id,
            tipo_contenedor_id=container.id,
            cantidad_objetivo_un=objective,
            capacidad_efectiva_un=capacity,
            mangas_propuestas=int(math.ceil(objective / Decimal(capacity))),
            peso_unitario_snapshot_g=weight,
            articulo_codigo_snapshot=article.codigo,
            articulo_nombre_snapshot=article.nombre,
            pieza_color_sku_snapshot=piece_sku,
            color_snapshot=None,
            regla_hash_snapshot=rule_revision.content_hash,
            tara_nominal_g_snapshot=(
                rule_revision.tara_nominal_g_snapshot
                if rule_revision.tara_nominal_g_snapshot is not None
                else container.tara_nominal_g
            ),
            tolerancia_tara_g_snapshot=(
                rule_revision.tolerancia_tara_g_snapshot
                if rule_revision.tolerancia_tara_g_snapshot is not None
                else container.tolerancia_tara_g
            ),
            peso_bruto_max_kg_snapshot=(
                rule_revision.peso_bruto_max_kg_snapshot
                if rule_revision.peso_bruto_max_kg_snapshot is not None
                else container.peso_bruto_max_kg
            ),
        ))
        session.flush()
        response = {"plan": _serialize_plan(plan)}
        _complete_operation(operation, response)
        session.add(_event(
            "PLAN_MANGA_OA", plan.id, "ASSEMBLY_BAG_PLAN_CALCULATED",
            actor, operation, response,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def assign_assembly_output_mangas(
    session, *, actor_id, ot_id, operation_id, data
):
    reject_unknown_fields(data, allowed={"version"})
    actor = load_actor(session, actor_id, capability="ENSAMBLE_PLANIFICAR")
    endpoint = f"/ots/{ot_id}/mangas-salida"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        ot = _load_assembly_ot(session, ot_id, lock=True)
        if ot.version != expected_version(data.get("version")):
            raise ScmServiceError(
                "VERSION_CONFLICT", "La OT fue modificada por otro usuario.", status_code=409
            )
        if ot.estado not in ("PLANIFICADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "WORK_ORDER_NOT_READY", "La OT no admite nuevas mangas.", status_code=409
            )
        plan = session.scalar(
            select(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOp.orden_operacion_id == ot.orden_operacion_id,
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        if plan is None or len(plan.lineas) != 1:
            raise ScmServiceError(
                "PACKAGING_RULE_MISSING",
                "La OA aun no tiene un plan de mangas de salida activo.",
                status_code=422,
            )
        line = plan.lineas[0]
        existing = session.scalar(select(ScmAsignacionPlanMangaOt).where(
            ScmAsignacionPlanMangaOt.plan_linea_id == line.id,
            ScmAsignacionPlanMangaOt.ot_id == ot.id,
        ))
        if existing is not None:
            raise ScmServiceError(
                "ASSEMBLY_OUTPUT_ALREADY_ASSIGNED",
                "La OT ya tiene sus mangas de salida asignadas.",
                status_code=409,
            )
        quantity = Decimal(ot.cantidad_objetivo)
        assigned = session.scalar(select(func.coalesce(
            func.sum(ScmAsignacionPlanMangaOt.cantidad_asignada_un), 0
        )).where(ScmAsignacionPlanMangaOt.plan_linea_id == line.id))
        if Decimal(assigned) + quantity > Decimal(line.cantidad_objetivo_un):
            raise ScmServiceError(
                "PLAN_BALANCE_EXCEEDED",
                "La cuota de la OT excede el saldo del plan de mangas.",
                status_code=409,
            )
        assignment = ScmAsignacionPlanMangaOt(
            plan_linea_id=line.id,
            ot_id=ot.id,
            cantidad_asignada_un=quantity,
            mangas_asignadas=int(math.ceil(
                quantity / Decimal(line.capacidad_efectiva_un)
            )),
            asignada_por_id=actor.id,
        )
        session.add(assignment)
        session.flush()
        mangas = _create_mangas(
            session,
            ot=ot,
            line=line,
            assignment=assignment,
            quantity=quantity,
            actor=actor,
            kind="NORMAL",
        )
        order = _load_order(session, ot.orden_operacion_id, lock=True)
        route_operation = session.get(
            ScmOperacionRuta, order.operacion_ruta_revision_id
        )
        structure = (
            route_operation.estructura_revision if route_operation else None
        )
        if structure is None:
            raise ScmServiceError(
                "OA_BOM_SNAPSHOT_MISSING",
                "La OA no conserva una estructura congelada resoluble.",
                status_code=409,
            )
        inline_reservations = _reserve_inline_output_for_mangas(
            session,
            ot=ot,
            structure=structure,
            mangas=mangas,
            actor=actor,
            operation=operation,
        )
        response = {
            "ot": _serialize_ot(ot),
            "mangas": [_serialize_manga(item) for item in mangas],
            "reservas_produccion_linea": [
                item.to_dict() for item in inline_reservations
            ],
        }
        _complete_operation(operation, response)
        session.add(_event(
            "ORDEN_TRABAJO", ot.public_id, "ASSEMBLY_OUTPUT_BAGS_ASSIGNED",
            actor, operation, response,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _load_manga(session, manga_id, *, lock=False):
    statement = select(ScmManga).where(ScmManga.public_id == manga_id)
    if lock:
        statement = statement.with_for_update()
    manga = session.scalar(statement)
    if manga is None or manga.ot.tipo_ot != "ENSAMBLE":
        raise ScmServiceError(
            "ASSEMBLY_MANGA_NOT_FOUND", "La manga de Armado no existe.", status_code=404
        )
    return manga


def get_assembly_manga_genealogy(session, *, actor_id, manga_id):
    load_actor(session, actor_id, capability="GENEALOGIA_VER")
    manga = _load_manga(session, manga_id)
    confirmation = manga.confirmacion_armado
    return {
        "manga": _serialize_manga(manga),
        "confirmacion": confirmation.to_dict() if confirmation else None,
        "correcciones": [
            item.to_dict() for item in session.scalars(
                select(ScmCorreccionMangaArmado)
                .where(ScmCorreccionMangaArmado.confirmacion_id == confirmation.id)
                .order_by(ScmCorreccionMangaArmado.solicitada_at)
            ).all()
        ] if confirmation else [],
    }


def request_assembly_quantity_correction(
    session, *, actor_id, manga_id, operation_id, data
):
    reject_unknown_fields(data, allowed={"cantidad_propuesta", "motivo"})
    actor = load_actor(session, actor_id, capability="ENSAMBLE_CORREGIR_SOLICITAR")
    proposed = _quantity(data.get("cantidad_propuesta"))
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    command = {
        "manga_id": str(manga_id), "cantidad_propuesta": format(proposed, "f"),
        "motivo": reason,
    }
    endpoint = f"/mangas/{manga_id}/correcciones-cantidad"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        manga = _load_manga(session, manga_id, lock=True)
        confirmation = manga.confirmacion_armado
        if confirmation is None or manga.estado != "CERRADA_ARMADO_PENDIENTE_PESAJE":
            raise ScmServiceError(
                "ASSEMBLY_CORRECTION_REQUIRES_PREWEIGH_CLOSED_BAG",
                "La correccion simple solo aplica despues del cierre y antes del pesaje.",
                status_code=409,
            )
        if session.scalar(select(func.count()).select_from(ScmCorreccionMangaArmado).where(
            ScmCorreccionMangaArmado.confirmacion_id == confirmation.id,
            ScmCorreccionMangaArmado.estado.in_(("PENDIENTE", "APLICADA")),
        )):
            raise ScmServiceError(
                "ASSEMBLY_CORRECTION_ALREADY_EXISTS",
                "La manga ya posee una correccion pendiente o aplicada.", status_code=409,
            )
        current = Decimal(manga.cantidad_confirmada_un)
        if proposed == current:
            raise ScmServiceError(
                "ASSEMBLY_CORRECTION_NO_CHANGE", "La cantidad propuesta no cambia la vigente.",
                status_code=422,
            )
        if proposed > Decimal(manga.cantidad_asignada_un):
            raise ScmServiceError(
                "ASSEMBLY_QUANTITY_EXCEEDS_AUTHORIZATION",
                "La cantidad propuesta excede la capacidad asignada.", status_code=409,
            )
        correction = ScmCorreccionMangaArmado(
            confirmacion_id=confirmation.id, cantidad_anterior=current,
            cantidad_propuesta=proposed, motivo=reason, solicitada_por_id=actor.id,
            request_operation_id=operation.operation_id,
        )
        session.add(correction)
        session.flush()
        response = {"correccion": correction.to_dict()}
        _complete_operation(operation, response, 201)
        session.add(_event("MANGA", manga.public_id, "ASSEMBLY_CORRECTION_REQUESTED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def approve_assembly_quantity_correction(
    session, *, actor_id, correction_id, operation_id, data
):
    reject_unknown_fields(data, allowed={"motivo_aprobacion"})
    actor = load_actor(session, actor_id, capability="ENSAMBLE_CORREGIR_APROBAR")
    resolution_reason = required_text(
        data.get("motivo_aprobacion"), field="motivo_aprobacion", max_length=500
    )
    command = {"correction_id": str(correction_id), "motivo_aprobacion": resolution_reason}
    endpoint = f"/correcciones-armado/{correction_id}/aprobar"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        correction = session.scalar(
            select(ScmCorreccionMangaArmado)
            .where(ScmCorreccionMangaArmado.id == correction_id).with_for_update()
        )
        if correction is None:
            raise ScmServiceError("ASSEMBLY_CORRECTION_NOT_FOUND", "La correccion no existe.", status_code=404)
        if correction.estado != "PENDIENTE":
            raise ScmServiceError("ASSEMBLY_CORRECTION_RESOLVED", "La correccion ya fue resuelta.", status_code=409)
        if correction.solicitada_por_id == actor.id:
            raise ScmServiceError("FOUR_EYES_REQUIRED", "Quien solicita no puede aprobar.", status_code=403)
        confirmation = correction.confirmacion
        manga = _load_manga(session, confirmation.manga.public_id, lock=True)
        if manga.estado != "CERRADA_ARMADO_PENDIENTE_PESAJE":
            raise ScmServiceError(
                "ASSEMBLY_CORRECTION_REQUIRES_CUSTODY_WORKFLOW",
                "La manga ya avanzo a pesaje o Almacen; requiere reapertura fisica coordinada.",
                status_code=409,
            )
        current = Decimal(manga.cantidad_confirmada_un)
        if current != Decimal(correction.cantidad_anterior):
            raise ScmServiceError("ASSEMBLY_CORRECTION_STALE", "La cantidad vigente cambio.", status_code=409)
        delta = Decimal(correction.cantidad_propuesta) - current
        structure = confirmation.estructura_revision
        request = session.scalar(
            select(ScmSolicitudAbastecimiento)
            .where(ScmSolicitudAbastecimiento.orden_trabajo_id == confirmation.orden_trabajo_id)
            .with_for_update()
        )
        lines = {item.articulo_scm_id: item for item in request.lineas}
        inline_consumptions = {
            item.articulo_componente_id: item
            for item in confirmation.consumos
            if item.reserva_wip_salida_id is not None
        }
        effects = []
        for component in structure.componentes:
            amount = (abs(delta) * Decimal(component.cantidad)).quantize(QUANTUM)
            if amount <= 0:
                continue
            inline_consumption = inline_consumptions.get(
                component.articulo_componente_id
            )
            if inline_consumption is not None:
                reservation_context = session.get(
                    ScmReservaWipSalida,
                    inline_consumption.reserva_wip_salida_id,
                )
                saldo_context = (
                    session.get(
                        ScmSaldoWipSalida, reservation_context.saldo_id
                    )
                    if reservation_context is not None
                    else None
                )
                if saldo_context is None:
                    raise ScmServiceError(
                        "INLINE_RESERVATION_CONTEXT_MISMATCH",
                        "La corrección perdió el saldo de producción en línea.",
                        status_code=409,
                    )
                work = session.scalar(
                    select(ScmTrabajoOt)
                    .where(
                        ScmTrabajoOt.id
                        == saldo_context.trabajo_color_id
                    )
                    .with_for_update()
                )
                if work is None or work.orden_operacion.estado == "CERRADA":
                    raise ScmServiceError(
                        "INLINE_SOURCE_OF_REOPEN_REQUIRED",
                        "La OF de origen ya cerró; requiere una reapertura controlada antes de corregir el crédito en línea.",
                        status_code=409,
                        details={
                            "trabajo_color_id": (
                                str(work.id) if work is not None else None
                            )
                        },
                    )
                assignment = session.scalar(
                    select(ScmAsignacionPlanMangaOt)
                    .where(
                        ScmAsignacionPlanMangaOt.id
                        == reservation_context.asignacion_plan_id
                    )
                    .with_for_update()
                )
                reservation = session.scalar(
                    select(ScmReservaWipSalida)
                    .where(
                        ScmReservaWipSalida.id
                        == inline_consumption.reserva_wip_salida_id
                    )
                    .with_for_update()
                )
                saldo = session.scalar(
                    select(ScmSaldoWipSalida)
                    .where(ScmSaldoWipSalida.id == reservation.saldo_id)
                    .with_for_update()
                )
                source_output = session.scalar(
                    select(ScmOrdenOperacionSalida)
                    .where(
                        ScmOrdenOperacionSalida.id
                        == saldo.orden_operacion_salida_id
                    )
                    .with_for_update()
                )
                if (
                    assignment is None
                    or assignment.trabajo_ot_id != work.id
                    or reservation.asignacion_plan_id != assignment.id
                    or saldo.trabajo_color_id != work.id
                    or source_output is None
                ):
                    raise ScmServiceError(
                        "INLINE_RESERVATION_CONTEXT_MISMATCH",
                        "La corrección ya no coincide con la cuota del TrabajoColor.",
                        status_code=409,
                    )
                signed = amount if delta > 0 else -amount
                if delta > 0:
                    available = (
                        Decimal(reservation.cantidad_reservada)
                        - Decimal(reservation.cantidad_aplicada)
                    )
                    if available < amount or (
                        Decimal(work.cantidad_confirmada_un) + amount
                        > Decimal(work.cantidad_objetivo_un)
                    ):
                        raise ScmServiceError(
                            "ASSEMBLY_CORRECTION_INLINE_COVERAGE_MISSING",
                            "La reserva en línea no cubre la corrección.",
                            status_code=409,
                            details={
                                "articulo_id": (
                                    component.articulo_componente_id
                                ),
                                "faltante": format(
                                    max(amount - available, Decimal("0")),
                                    "f",
                                ),
                            },
                        )
                    movement_types = (
                        "SALIDA_BUENA_CONFIRMADA",
                        "CONSUMO_EN_LINEA_ARMADO",
                    )
                else:
                    if (
                        Decimal(reservation.cantidad_aplicada) < amount
                        or Decimal(inline_consumption.cantidad_incorporada)
                        < amount
                        or Decimal(work.cantidad_confirmada_un) < amount
                        or Decimal(assignment.cantidad_asignada_un) < amount
                        or Decimal(work.cantidad_objetivo_un) < amount
                    ):
                        raise ScmServiceError(
                            "ASSEMBLY_CORRECTION_INLINE_COVERAGE_MISSING",
                            "El crédito en línea no permite esa compensación.",
                            status_code=409,
                            details={
                                "articulo_id": (
                                    component.articulo_componente_id
                                )
                            },
                        )
                    movement_types = (
                        "REVERSO_SALIDA_BUENA",
                        "REVERSO_CONSUMO_EN_LINEA_ARMADO",
                    )
                    assignment.cantidad_asignada_un = (
                        Decimal(assignment.cantidad_asignada_un) - amount
                    )
                    work.cantidad_objetivo_un = (
                        Decimal(work.cantidad_objetivo_un) - amount
                    )
                reservation.cantidad_aplicada = (
                    Decimal(reservation.cantidad_aplicada) + signed
                )
                saldo.cantidad_acreditada = (
                    Decimal(saldo.cantidad_acreditada) + signed
                )
                saldo.cantidad_consumida = (
                    Decimal(saldo.cantidad_consumida) + signed
                )
                saldo.version += 1
                work.cantidad_confirmada_un = (
                    Decimal(work.cantidad_confirmada_un) + signed
                )
                work.version += 1
                source_output.cantidad_real = (
                    Decimal(source_output.cantidad_real or 0) + signed
                )
                inline_consumption.cantidad_incorporada = (
                    Decimal(inline_consumption.cantidad_incorporada) + signed
                )
                for index, movement_type in enumerate(movement_types):
                    session.add(ScmMovimientoWipSalida(
                        saldo_id=saldo.id,
                        reserva_id=reservation.id,
                        confirmacion_id=confirmation.id,
                        tipo=movement_type,
                        cantidad=amount,
                        effect_key=(
                            f"{correction.id}:{reservation.id}:"
                            f"{movement_type}:{index}"
                        ),
                        actor_id=actor.id,
                        operation_id=operation.operation_id,
                    ))
                effects.append({
                    "articulo_id": component.articulo_componente_id,
                    "nivel": "EXACTA",
                    "procedencia": "PRODUCIDO_OT_ACTUAL",
                    "fuente_id": str(reservation.id),
                    "delta": format(signed, "f"),
                })
                continue
            line = lines.get(component.articulo_componente_id)
            if line is None:
                raise ScmServiceError(
                    "ASSEMBLY_CORRECTION_COMPONENT_COVERAGE_MISSING",
                    "Falta una fuente para compensar la corrección.",
                    status_code=409,
                    details={
                        "articulo_id": component.articulo_componente_id,
                        "faltante": format(amount, "f"),
                    },
                )
            remaining = amount
            if delta > 0:
                sources = [
                    ("EXACTA", item) for item in line.asignaciones
                    if item.estado in ("EN_STAGING_ARMADO", "ABIERTA_EN_CONSUMO")
                ] + [
                    (item.pool.modo, item) for item in line.asignaciones_pool
                    if item.estado in ("EN_STAGING_ARMADO", "ABIERTA_EN_CONSUMO")
                ]
                for level, assignment in sources:
                    if remaining <= 0:
                        break
                    source_balance = (
                        assignment.existencia.saldo if level == "EXACTA" else assignment.saldo
                    )
                    source_available = Decimal(
                        assignment.saldo if level == "EXACTA" else assignment.saldo_cantidad
                    )
                    take = min(remaining, source_available).quantize(QUANTUM)
                    if take <= 0 or Decimal(source_balance.cantidad_reservada) < take:
                        continue
                    assignment.cantidad_consumida = Decimal(assignment.cantidad_consumida) + take
                    source_balance.cantidad_fisica = Decimal(source_balance.cantidad_fisica) - take
                    source_balance.cantidad_reservada = Decimal(source_balance.cantidad_reservada) - take
                    source_balance.version += 1
                    if level == "EXACTA":
                        existence = assignment.existencia
                        existence.cantidad_fisica = Decimal(existence.cantidad_fisica) - take
                        existence.cantidad_reservada = Decimal(existence.cantidad_reservada) - take
                        existence.estado_logistico = "CONSUMIDA" if Decimal(assignment.saldo) == 0 else "ABIERTA_EN_CONSUMO"
                        existence.version += 1
                        assignment.estado = existence.estado_logistico
                    else:
                        assignment.estado = "CONSUMIDA" if Decimal(assignment.saldo_cantidad) == 0 else "ABIERTA_EN_CONSUMO"
                    session.add(ScmMovimientoInventario(
                        saldo=source_balance, tipo="CONSUMO", cantidad_delta=-take,
                        saldo_fisico_resultante=source_balance.cantidad_fisica,
                        motivo=f"Correccion autorizada de {manga.codigo}",
                        referencia_tipo="CORRECCION_ARMADO", referencia_id=str(correction.id),
                        actor_id=actor.id,
                        operation_id=uuid.uuid5(operation.operation_id, f"{assignment.id}:PLUS"),
                    ))
                    effects.append({"articulo_id": component.articulo_componente_id, "nivel": level,
                                    "fuente_id": str(assignment.id), "delta": format(take, "f")})
                    remaining -= take
            else:
                consumptions = [
                    value for value in reversed(confirmation.consumos)
                    if value.articulo_componente_id == component.articulo_componente_id
                ]
                for consumption in consumptions:
                    if remaining <= 0:
                        break
                    take = min(remaining, Decimal(consumption.cantidad_incorporada)).quantize(QUANTUM)
                    assignment = consumption.asignacion_abastecimiento or consumption.asignacion_pool
                    level = consumption.nivel_genealogia
                    source_balance = assignment.existencia.saldo if level == "EXACTA" else assignment.saldo
                    assignment.cantidad_consumida = Decimal(assignment.cantidad_consumida) - take
                    source_balance.cantidad_fisica = Decimal(source_balance.cantidad_fisica) + take
                    source_balance.cantidad_reservada = Decimal(source_balance.cantidad_reservada) + take
                    source_balance.version += 1
                    if level == "EXACTA":
                        existence = assignment.existencia
                        existence.cantidad_fisica = Decimal(existence.cantidad_fisica) + take
                        existence.cantidad_reservada = Decimal(existence.cantidad_reservada) + take
                        existence.estado_logistico = "ABIERTA_EN_CONSUMO"
                        existence.version += 1
                    assignment.estado = "ABIERTA_EN_CONSUMO"
                    session.add(ScmMovimientoInventario(
                        saldo=source_balance, tipo="AJUSTE_POSITIVO", cantidad_delta=take,
                        saldo_fisico_resultante=source_balance.cantidad_fisica,
                        motivo=f"Compensacion autorizada de {manga.codigo}",
                        referencia_tipo="CORRECCION_ARMADO", referencia_id=str(correction.id),
                        actor_id=actor.id,
                        operation_id=uuid.uuid5(operation.operation_id, f"{assignment.id}:MINUS"),
                    ))
                    effects.append({"articulo_id": component.articulo_componente_id, "nivel": level,
                                    "fuente_id": str(assignment.id), "delta": format(-take, "f")})
                    remaining -= take
            if remaining > 0:
                raise ScmServiceError(
                    "ASSEMBLY_CORRECTION_COMPONENT_COVERAGE_MISSING",
                    "Las fuentes disponibles no permiten compensar la correccion.", status_code=409,
                    details={"articulo_id": component.articulo_componente_id,
                             "faltante": format(remaining, "f")},
                )
        proposed = Decimal(correction.cantidad_propuesta)
        manga.cantidad_confirmada_un = proposed
        manga.cantidad_contenida_un = proposed
        manga.version += 1
        manga.lote_articulo.cantidad_acreditada = Decimal(manga.lote_articulo.cantidad_acreditada) + delta
        manga.lote_articulo.event_time = utc_now()
        manga.lote_articulo.actor_id = actor.id
        manga.plan_linea.salida_canonica.cantidad_real = Decimal(
            manga.plan_linea.salida_canonica.cantidad_real or 0
        ) + delta
        ot = confirmation.orden_trabajo
        ot.cantidad_confirmada = Decimal(ot.cantidad_confirmada or 0) + delta
        ot.version += 1
        correction.estado = "APLICADA"
        correction.resuelta_por_id = actor.id
        correction.resuelta_at = utc_now()
        correction.approval_operation_id = operation.operation_id
        correction.motivo_resolucion = resolution_reason
        correction.efectos_json = effects
        session.flush()
        response = {"manga": _serialize_manga(manga), "correccion": correction.to_dict()}
        _complete_operation(operation, response)
        session.add(_event("MANGA", manga.public_id, "ASSEMBLY_CORRECTION_APPLIED", actor, operation, response))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def close_assembly_manga(
    session, *, actor_id, manga_id, operation_id, data
):
    reject_unknown_fields(
        data, allowed={"version", "cantidad_real", "motivo_diferencia"}
    )
    actor = load_actor(session, actor_id, capability="ENSAMBLE_MANGA_CERRAR")
    command = {
        "manga_id": str(manga_id),
        "version": data.get("version"),
        "cantidad_real": format(_quantity(data.get("cantidad_real")), "f"),
        "motivo_diferencia": str(data.get("motivo_diferencia") or "").strip(),
    }
    endpoint = f"/mangas/{manga_id}/cerrar-armado"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, command
    )
    if replay is not None:
        return replay
    try:
        manga = _load_manga(session, manga_id, lock=True)
        if manga.version != expected_version(command["version"]):
            raise ScmServiceError(
                "VERSION_CONFLICT", "La manga fue modificada por otro usuario.", status_code=409
            )
        ot = _load_assembly_ot(session, manga.ot.public_id, lock=True)
        if ot.estado != "EN_EJECUCION":
            raise ScmServiceError(
                "WORK_ORDER_NOT_READY", "La OT de Armado debe estar iniciada.", status_code=409
            )
        if ot.responsable_id != actor.id:
            raise ScmServiceError(
                "ASSEMBLY_RESPONSIBLE_REQUIRED",
                "Solo el responsable asignado a la OT puede confirmar la cantidad.",
                status_code=403,
            )
        if manga.estado not in ("PREETIQUETADA", "EN_ARMADO"):
            raise ScmServiceError(
                "MANGA_NOT_READY_FOR_ASSEMBLY_CLOSE",
                "La manga no se encuentra lista para cerrar Armado.",
                status_code=409,
            )
        real = Decimal(command["cantidad_real"])
        planned = Decimal(manga.cantidad_planificada_un)
        if real > Decimal(manga.cantidad_asignada_un):
            raise ScmServiceError(
                "ASSEMBLY_QUANTITY_EXCEEDS_AUTHORIZATION",
                "La cantidad real excede la capacidad asignada a la manga.",
                status_code=409,
            )
        if real != planned and not command["motivo_diferencia"]:
            raise ScmServiceError(
                "ASSEMBLY_DIFFERENCE_REASON_REQUIRED",
                "Explica la diferencia entre la cantidad planificada y la real.",
                status_code=422,
            )
        confirmed_before = Decimal(ot.cantidad_confirmada or 0)
        if confirmed_before + real > Decimal(ot.cantidad_objetivo):
            raise ScmServiceError(
                "WORK_ORDER_QUOTA_EXCEEDED",
                "El cierre excede la cuota diaria autorizada.",
                status_code=409,
            )

        order = _load_order(session, ot.orden_operacion_id, lock=True)
        route_operation = session.get(ScmOperacionRuta, order.operacion_ruta_revision_id)
        structure = route_operation.estructura_revision if route_operation else None
        if structure is None or not structure.content_hash:
            raise ScmServiceError(
                "OA_BOM_SNAPSHOT_MISSING",
                "La OA no conserva una estructura congelada resoluble.",
                status_code=409,
            )
        inline_source = _inline_source_for_ot(
            session, ot=ot, structure=structure, lock=True
        )
        inline_assignment = None
        if inline_source is not None:
            source_work, _source_component, source_output = inline_source
            if source_work.estado not in {"EN_EJECUCION", "PAUSADO"}:
                raise ScmServiceError(
                    "INLINE_SOURCE_WORK_NOT_ACTIVE",
                    "El TrabajoColor de origen debe estar activo para cerrar el armado en línea.",
                    status_code=409,
                    details={
                        "trabajo_color_id": str(source_work.id),
                        "estado": source_work.estado,
                    },
                )
            _source_line, inline_assignment = _inline_plan_allocation(
                session,
                work=source_work,
                output=source_output,
                create=False,
            )
        request = session.scalar(
            select(ScmSolicitudAbastecimiento)
            .where(ScmSolicitudAbastecimiento.orden_trabajo_id == ot.id)
            .with_for_update()
        )
        if request is None or request.estado != "RECIBIDA":
            raise ScmServiceError(
                "COMPONENT_RESERVATION_MISSING",
                "El abastecimiento exacto debe estar recibido en Mesa de Armado.",
                status_code=409,
            )
        request_lines = {item.articulo_scm_id: item for item in request.lineas}
        inline_reservations = session.scalars(
            select(ScmReservaWipSalida)
            .where(
                ScmReservaWipSalida.manga_id == manga.id,
                ScmReservaWipSalida.estado.in_((
                    "CREDITO_EN_LINEA_PENDIENTE",
                    "APLICADA",
                )),
            )
            .order_by(ScmReservaWipSalida.id)
            .with_for_update()
        ).all()
        inline_by_article = {
            item.articulo_componente_id: item
            for item in inline_reservations
        }
        if len(inline_by_article) != len(inline_reservations):
            raise ScmServiceError(
                "INLINE_RESERVATION_AMBIGUOUS",
                "La manga posee reservas en línea ambiguas.",
                status_code=409,
            )
        requirements = []
        for component in structure.componentes:
            required = (real * Decimal(component.cantidad)).quantize(QUANTUM)
            inline_reservation = inline_by_article.get(
                component.articulo_componente_id
            )
            if inline_reservation is not None:
                reservation_remaining = (
                    Decimal(inline_reservation.cantidad_reservada)
                    - Decimal(inline_reservation.cantidad_aplicada)
                )
                if reservation_remaining < required:
                    raise ScmServiceError(
                        "INLINE_OUTPUT_RESERVATION_INSUFFICIENT",
                        "La salida fresca reservada no cubre el cierre de Armado.",
                        status_code=409,
                        details={
                            "articulo_id": component.articulo_componente_id,
                            "requerido": format(required, "f"),
                            "disponible": format(
                                reservation_remaining, "f"
                            ),
                        },
                    )
                requirements.append(
                    (component, None, required, inline_reservation)
                )
                continue
            line = request_lines.get(component.articulo_componente_id)
            if line is None:
                raise ScmServiceError(
                    "COMPONENT_RESERVATION_MISSING",
                    "Falta una linea de abastecimiento para la BOM congelada.",
                    status_code=409,
                    details={"articulo_id": component.articulo_componente_id},
                )
            available = sum(
                (
                    Decimal(item.saldo)
                    for item in line.asignaciones
                    if item.estado in ("EN_STAGING_ARMADO", "ABIERTA_EN_CONSUMO")
                ),
                Decimal("0"),
            )
            available += sum(
                (
                    Decimal(item.saldo_cantidad)
                    for item in line.asignaciones_pool
                    if item.estado in ("EN_STAGING_ARMADO", "ABIERTA_EN_CONSUMO")
                ),
                Decimal("0"),
            )
            if available < required:
                raise ScmServiceError(
                    "COMPONENT_STOCK_INSUFFICIENT",
                    "Las mangas asignadas no cubren el cierre de Armado.",
                    status_code=409,
                    details={
                        "articulo_id": component.articulo_componente_id,
                        "requerido": format(required, "f"),
                        "disponible": format(available, "f"),
                    },
                )
            requirements.append((component, line, required, None))

        confirmation = ScmConfirmacionMangaArmado(
            manga_id=manga.id,
            orden_ensamble_id=order.id,
            orden_trabajo_id=ot.id,
            articulo_salida_id=manga.lote_articulo.articulo_id,
            estructura_revision_id=structure.id,
            estructura_hash=structure.content_hash,
            cantidad_planificada=planned,
            cantidad_real=real,
            diferencia_cantidad=real - planned,
            motivo_diferencia=command["motivo_diferencia"] or None,
            confirmado_por_id=actor.id,
            operation_id=operation.operation_id,
            payload_hash=_json_hash(command),
        )
        session.add(confirmation)
        session.flush()
        for component, line, required, inline_reservation in requirements:
            if inline_reservation is not None:
                if inline_source is None or inline_assignment is None:
                    raise ScmServiceError(
                        "INLINE_RESERVATION_CONTEXT_MISMATCH",
                        "La reserva en línea no conserva un origen resoluble.",
                        status_code=409,
                    )
                source_work, _source_component, source_output = inline_source
                if inline_reservation.asignacion_plan_id != inline_assignment.id:
                    raise ScmServiceError(
                        "INLINE_RESERVATION_CONTEXT_MISMATCH",
                        "La reserva en línea ya no coincide con la cuota del TrabajoColor.",
                        status_code=409,
                    )
                saldo = session.scalar(
                    select(ScmSaldoWipSalida)
                    .where(
                        ScmSaldoWipSalida.id
                        == inline_reservation.saldo_id
                    )
                    .with_for_update()
                )
                source_output = session.scalar(
                    select(ScmOrdenOperacionSalida)
                    .where(
                        ScmOrdenOperacionSalida.id
                        == saldo.orden_operacion_salida_id
                    )
                    .with_for_update()
                )
                if (
                    source_output is None
                    or source_work.id != ot.trabajo_color_contexto_id
                    or saldo.trabajo_color_id != source_work.id
                    or source_output.articulo_scm_id
                    != component.articulo_componente_id
                    or source_output.orden_operacion_id
                    != source_work.orden_operacion_id
                ):
                    raise ScmServiceError(
                        "INLINE_RESERVATION_CONTEXT_MISMATCH",
                        "La reserva en línea ya no coincide con el TrabajoColor.",
                        status_code=409,
                    )
                confirmed = Decimal(
                    source_work.cantidad_confirmada_un or 0
                )
                if confirmed + required > Decimal(
                    source_work.cantidad_objetivo_un
                ):
                    raise ScmServiceError(
                        "INLINE_OUTPUT_QUOTA_EXCEEDED",
                        "El crédito en línea excede la cuota del TrabajoColor.",
                        status_code=409,
                    )
                unused = (
                    Decimal(inline_reservation.cantidad_reservada)
                    - Decimal(inline_reservation.cantidad_aplicada)
                    - required
                )
                _release_inline_plan_quota(
                    work=source_work,
                    assignment=inline_assignment,
                    quantity=unused,
                )
                inline_reservation.cantidad_aplicada = (
                    Decimal(inline_reservation.cantidad_aplicada) + required
                )
                inline_reservation.estado = "APLICADA"
                inline_reservation.aplicada_at = utc_now()
                saldo.cantidad_acreditada = (
                    Decimal(saldo.cantidad_acreditada) + required
                )
                saldo.cantidad_consumida = (
                    Decimal(saldo.cantidad_consumida) + required
                )
                saldo.version += 1
                source_work.cantidad_confirmada_un = confirmed + required
                source_work.version += 1
                source_output.cantidad_real = (
                    Decimal(source_output.cantidad_real or 0) + required
                )
                session.add_all([
                    ScmMovimientoWipSalida(
                        saldo_id=saldo.id,
                        reserva_id=inline_reservation.id,
                        confirmacion_id=confirmation.id,
                        tipo="SALIDA_BUENA_CONFIRMADA",
                        cantidad=required,
                        effect_key=(
                            f"{confirmation.id}:"
                            f"{inline_reservation.id}:CREDITO"
                        ),
                        actor_id=actor.id,
                        operation_id=operation.operation_id,
                    ),
                    ScmMovimientoWipSalida(
                        saldo_id=saldo.id,
                        reserva_id=inline_reservation.id,
                        confirmacion_id=confirmation.id,
                        tipo="CONSUMO_EN_LINEA_ARMADO",
                        cantidad=required,
                        effect_key=(
                            f"{confirmation.id}:"
                            f"{inline_reservation.id}:CONSUMO"
                        ),
                        actor_id=actor.id,
                        operation_id=operation.operation_id,
                    ),
                    ScmConsumoComponenteArmado(
                        confirmacion_id=confirmation.id,
                        reserva_wip_salida_id=inline_reservation.id,
                        articulo_componente_id=(
                            component.articulo_componente_id
                        ),
                        cantidad_incorporada=required,
                        cantidad_merma=0,
                        nivel_genealogia="EXACTA",
                        procedencia="PRODUCIDO_OT_ACTUAL",
                    ),
                ])
                continue
            remaining = required
            assignments = session.scalars(
                select(ScmAsignacionAbastecimiento)
                .where(
                    ScmAsignacionAbastecimiento.linea_id == line.id,
                    ScmAsignacionAbastecimiento.estado.in_((
                        "EN_STAGING_ARMADO", "ABIERTA_EN_CONSUMO"
                    )),
                )
                .order_by(ScmAsignacionAbastecimiento.id)
                .with_for_update()
            ).all()
            for assignment in assignments:
                if remaining <= 0:
                    break
                take = min(remaining, Decimal(assignment.saldo)).quantize(QUANTUM)
                if take <= 0:
                    continue
                existence = session.scalar(
                    select(ScmExistenciaManga)
                    .where(ScmExistenciaManga.id == assignment.existencia_manga_id)
                    .with_for_update()
                )
                balance = session.scalar(
                    select(ScmSaldoInventario)
                    .where(ScmSaldoInventario.id == existence.saldo_id)
                    .with_for_update()
                )
                if (
                    Decimal(existence.cantidad_fisica) < take
                    or Decimal(existence.cantidad_reservada) < take
                    or Decimal(balance.cantidad_fisica) < take
                    or Decimal(balance.cantidad_reservada) < take
                ):
                    raise ScmServiceError(
                        "COMPONENT_STOCK_INSUFFICIENT",
                        "El saldo fisico cambió durante el cierre de Armado.",
                        status_code=409,
                    )
                assignment.cantidad_consumida = (
                    Decimal(assignment.cantidad_consumida) + take
                )
                existence.cantidad_fisica = Decimal(existence.cantidad_fisica) - take
                existence.cantidad_reservada = (
                    Decimal(existence.cantidad_reservada) - take
                )
                balance.cantidad_fisica = Decimal(balance.cantidad_fisica) - take
                balance.cantidad_reservada = Decimal(balance.cantidad_reservada) - take
                existence.version += 1
                balance.version += 1
                if Decimal(assignment.saldo) == 0:
                    assignment.estado = "CONSUMIDA"
                    existence.estado_logistico = "CONSUMIDA"
                else:
                    assignment.estado = "ABIERTA_EN_CONSUMO"
                    existence.estado_logistico = "ABIERTA_EN_CONSUMO"
                session.add(ScmMovimientoInventario(
                    saldo_id=balance.id,
                    tipo="CONSUMO",
                    cantidad_delta=-take,
                    saldo_fisico_resultante=balance.cantidad_fisica,
                    motivo=f"Consumo exacto en {manga.codigo}",
                    referencia_tipo="CONFIRMACION_ARMADO",
                    referencia_id=str(confirmation.id),
                    actor_id=actor.id,
                    operation_id=uuid.uuid5(
                        operation.operation_id, f"{assignment.id}:CONSUMO"
                    ),
                ))
                session.add(ScmConsumoComponenteArmado(
                    confirmacion_id=confirmation.id,
                    asignacion_abastecimiento_id=assignment.id,
                    articulo_componente_id=component.articulo_componente_id,
                    cantidad_incorporada=take,
                    cantidad_merma=0,
                    nivel_genealogia="EXACTA",
                    procedencia="CONSUMIDO_STOCK_PREVIO",
                ))
                remaining -= take
            if remaining > 0:
                pool_assignments = session.scalars(
                    select(ScmAsignacionPoolArmado)
                    .where(
                        ScmAsignacionPoolArmado.linea_id == line.id,
                        ScmAsignacionPoolArmado.estado.in_((
                            "EN_STAGING_ARMADO", "ABIERTA_EN_CONSUMO"
                        )),
                    )
                    .order_by(ScmAsignacionPoolArmado.id)
                    .with_for_update()
                ).all()
                for assignment in pool_assignments:
                    if remaining <= 0:
                        break
                    take = min(remaining, Decimal(assignment.saldo_cantidad)).quantize(QUANTUM)
                    if take <= 0:
                        continue
                    balance = session.scalar(
                        select(ScmSaldoInventario)
                        .where(ScmSaldoInventario.id == assignment.saldo_id)
                        .with_for_update()
                    )
                    if (
                        Decimal(balance.cantidad_fisica) < take
                        or Decimal(balance.cantidad_reservada) < take
                    ):
                        raise ScmServiceError(
                            "COMPONENT_STOCK_INSUFFICIENT",
                            "El saldo agrupado cambio durante el cierre de Armado.",
                            status_code=409,
                        )
                    assignment.cantidad_consumida = Decimal(assignment.cantidad_consumida) + take
                    balance.cantidad_fisica = Decimal(balance.cantidad_fisica) - take
                    balance.cantidad_reservada = Decimal(balance.cantidad_reservada) - take
                    balance.version += 1
                    assignment.estado = (
                        "CONSUMIDA" if Decimal(assignment.saldo_cantidad) == 0
                        else "ABIERTA_EN_CONSUMO"
                    )
                    session.add(ScmMovimientoInventario(
                        saldo_id=balance.id, tipo="CONSUMO", cantidad_delta=-take,
                        saldo_fisico_resultante=balance.cantidad_fisica,
                        motivo=f"Consumo {assignment.pool.modo} en {manga.codigo}",
                        referencia_tipo="CONFIRMACION_ARMADO",
                        referencia_id=str(confirmation.id), actor_id=actor.id,
                        operation_id=uuid.uuid5(
                            operation.operation_id, f"{assignment.id}:POOL:CONSUMO"
                        ),
                    ))
                    session.add(ScmConsumoComponenteArmado(
                        confirmacion_id=confirmation.id,
                        asignacion_pool_id=assignment.id,
                        articulo_componente_id=component.articulo_componente_id,
                        cantidad_incorporada=take, cantidad_merma=0,
                        nivel_genealogia=assignment.pool.modo,
                        procedencia="CONSUMIDO_STOCK_PREVIO",
                    ))
                    remaining -= take

            if remaining > 0:
                raise ScmServiceError(
                    "COMPONENT_STOCK_INSUFFICIENT",
                    "El saldo cambió durante el cierre de Armado y ya no cubre la BOM.",
                    status_code=409,
                    details={
                        "articulo_id": component.articulo_componente_id,
                        "faltante": format(remaining, "f"),
                    },
                )

        manga.cantidad_confirmada_un = real
        manga.cantidad_contenida_un = real
        manga.estado = "CERRADA_ARMADO_PENDIENTE_PESAJE"
        manga.version += 1
        manga.lote_articulo.cantidad_acreditada = (
            Decimal(manga.lote_articulo.cantidad_acreditada) + real
        )
        manga.lote_articulo.event_time = utc_now()
        manga.lote_articulo.actor_id = actor.id
        output = manga.plan_linea.salida_canonica
        output.cantidad_real = Decimal(output.cantidad_real or 0) + real
        ot.cantidad_confirmada = confirmed_before + real
        ot.version += 1
        if order.estado == "LIBERADA":
            order.estado = "EN_EJECUCION"
            order.started_by_id = actor.id
            order.started_at = utc_now()
            order.version += 1
        session.flush()
        response = {
            "manga": _serialize_manga(manga),
            "confirmacion": confirmation.to_dict(),
            "pesaje_creado": False,
            "kardex_salida_creado": False,
        }
        _complete_operation(operation, response)
        session.add(_event(
            "MANGA", manga.public_id, "ASSEMBLY_BAG_CLOSED",
            actor, operation, response,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
