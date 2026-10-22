"""Projection of operational output into production-order fulfillment.

This module deliberately does not create inventory movements.  Kardex records
physical custody; fulfillment records which demand an already produced output
satisfies.  Keeping both ledgers separate prevents an OF close from duplicating
stock that was received through the warehouse workflow.
"""

from decimal import Decimal

from sqlalchemy import select

from app.models.scm_auditoria import ScmEvento
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
)
from app.services.scm_service_support import actor_snapshot


ZERO = Decimal("0")


def _active_allocations(output):
    return sorted(
        (
            item for item in output.asignaciones
            if item.estado != "CANCELADA"
        ),
        key=lambda item: (item.created_at, str(item.id)),
    )


def credit_output_allocations(output, accepted_quantity):
    """Set (never add to) demand credits for one canonical output.

    Replaying a close cannot double-count because each allocation is projected
    from the authoritative accepted total instead of incremented.
    """

    remaining = max(Decimal(accepted_quantity), ZERO)
    changes = []
    for allocation in _active_allocations(output):
        planned = Decimal(allocation.cantidad_planificada)
        satisfied = min(planned, remaining)
        remaining -= satisfied
        next_state = (
            "SATISFECHA" if satisfied >= planned
            else "COMPROMETIDA" if satisfied > ZERO
            else "PLANIFICADA"
        )
        before = (
            Decimal(allocation.cantidad_comprometida),
            Decimal(allocation.cantidad_satisfecha),
            allocation.estado,
        )
        after = (satisfied, satisfied, next_state)
        if before != after:
            allocation.cantidad_comprometida = satisfied
            allocation.cantidad_satisfecha = satisfied
            allocation.estado = next_state
            allocation.version += 1
        changes.append({
            "id": str(allocation.id),
            "planificada": format(planned, "f"),
            "satisfecha": format(satisfied, "f"),
            "estado": next_state,
        })
    return {
        "asignaciones": changes,
        "excedente_no_asignado": format(remaining, "f"),
    }


def _line_fulfillment(line):
    # Satisfied history remains authoritative after an execution allocation is
    # cancelled.  Only residual commitment is excluded from coverage.
    satisfied = sum(
        (
            Decimal(item.cantidad_satisfecha)
            for item in line.asignaciones
        ),
        ZERO,
    )
    committed = sum(
        (
            Decimal(item.cantidad_comprometida)
            for item in line.asignaciones
            if item.estado != "CANCELADA"
        ),
        ZERO,
    )
    operational = any(
        item.orden_operacion_salida is not None
        and item.orden_operacion_salida.orden_operacion.estado
        in {"PROGRAMADA", "EN_EJECUCION", "CERRADA"}
        for item in line.asignaciones
        if item.estado != "CANCELADA"
    )
    return satisfied, committed, operational


def reconcile_cancelled_operation_fulfillment(
    session, *, operation_order, actor, operation, reason,
):
    """Release only residual OF commitments, retaining accepted history.

    ``cantidad_real`` is the existing production authority.  If it is not
    available yet, current allocation satisfaction is retained and no unit
    conversion or inferred output is attempted.
    """
    output_ids = [item.id for item in operation_order.salidas]
    if not output_ids:
        return {"salidas": [], "asignaciones": [], "ordenes_produccion": []}
    assignments = list(session.scalars(
        select(ScmAsignacionDemandaSuministro)
        .where(ScmAsignacionDemandaSuministro.orden_operacion_salida_id.in_(output_ids))
        .order_by(ScmAsignacionDemandaSuministro.created_at, ScmAsignacionDemandaSuministro.id)
        .with_for_update(of=ScmAsignacionDemandaSuministro)
    ).all())
    affected_order_ids = sorted({
        item.orden_produccion_linea.orden_produccion_id
        for item in assignments
        if item.orden_produccion_linea is not None
    }, key=str)
    output_results = []
    for output in operation_order.salidas:
        accepted = output.cantidad_real
        output_results.append({
            "salida_id": str(output.id),
            "unidad": getattr(output.articulo, "unidad_inventario", None),
            "cantidad_objetivo": format(Decimal(output.cantidad_objetivo), "f"),
            "cantidad_real": format(Decimal(accepted), "f") if accepted is not None else None,
            "cantidad_rechazada": format(Decimal(output.cantidad_rechazada or 0), "f") if output.cantidad_rechazada is not None else None,
            "fuente": "SALIDA_ORDEN_OPERACION.cantidad_real" if accepted is not None else "satisfaccion_preexistente",
        })
        if accepted is not None:
            credit_output_allocations(output, accepted)
    allocation_results = []
    for item in assignments:
        satisfied = Decimal(item.cantidad_satisfecha or 0)
        committed_before = Decimal(item.cantidad_comprometida or 0)
        residual = max(committed_before - satisfied, ZERO)
        item.cantidad_comprometida = satisfied
        item.estado = "CANCELADA"
        item.version += 1
        allocation_results.append({
            "id": str(item.id),
            "cantidad_satisfecha": format(satisfied, "f"),
            "compromiso_liberado": format(residual, "f"),
            "estado": item.estado,
        })
    projections = project_production_orders_for_outputs(
        session,
        output_ids=output_ids,
        actor=actor,
        operation=operation,
        affected_order_ids=affected_order_ids,
    )
    return {
        "salidas": output_results,
        "asignaciones": allocation_results,
        "ordenes_produccion": projections,
    }


def _affected_order_ids(session, output_ids):
    if not output_ids:
        return []
    return list(session.scalars(
        select(ScmOrdenProduccionLinea.orden_produccion_id)
        .join(
            ScmAsignacionDemandaSuministro,
            ScmAsignacionDemandaSuministro.orden_produccion_linea_id
            == ScmOrdenProduccionLinea.id,
        )
        .where(
            ScmAsignacionDemandaSuministro.orden_operacion_salida_id.in_(
                output_ids
            ),
            ScmAsignacionDemandaSuministro.estado != "CANCELADA",
        )
        .distinct()
    ))


def project_production_orders_for_outputs(
    session,
    *,
    output_ids,
    actor,
    operation,
    affected_order_ids=None,
):
    """Project line and OP states after an operational state/result change."""

    order_ids = (
        affected_order_ids if affected_order_ids is not None
        else _affected_order_ids(session, output_ids)
    )
    if not order_ids:
        return []
    orders = session.scalars(
        select(ScmOrdenProduccion)
        .where(ScmOrdenProduccion.id.in_(order_ids))
        .order_by(ScmOrdenProduccion.codigo)
        .with_for_update(of=ScmOrdenProduccion)
    ).all()
    projections = []
    for order in orders:
        if order.estado not in {
            "PLANIFICADA", "EN_COBERTURA", "COMPLETADA"
        }:
            continue
        active_lines = [
            line for line in order.lineas if line.estado != "CANCELADA"
        ]
        line_results = []
        all_satisfied = bool(active_lines)
        any_coverage = False
        for line in active_lines:
            satisfied, committed, operational = _line_fulfillment(line)
            requested = Decimal(line.cantidad_solicitada)
            line_complete = satisfied >= requested
            next_line_state = "SATISFECHA" if line_complete else "ACTIVA"
            if line.estado != next_line_state:
                line.estado = next_line_state
                line.version += 1
            all_satisfied = all_satisfied and line_complete
            any_coverage = any_coverage or (
                satisfied > ZERO or committed > ZERO or operational
            )
            line_results.append({
                "id": str(line.id),
                "solicitada": format(requested, "f"),
                "satisfecha": format(satisfied, "f"),
                "estado": next_line_state,
            })
        next_state = (
            "COMPLETADA" if all_satisfied
            else "EN_COBERTURA" if any_coverage
            else "PLANIFICADA"
        )
        previous_state = order.estado
        if previous_state != next_state:
            order.estado = next_state
            order.version += 1
            session.add(ScmEvento(
                aggregate_type="ORDEN_PRODUCCION",
                aggregate_id=str(order.id),
                tipo="OP_FULFILLMENT_PROJECTED",
                actor_id=actor.id,
                actor_snapshot=actor_snapshot(actor),
                before_json={"estado": previous_state},
                after_json={"estado": next_state, "lineas": line_results},
                operation_id=operation.operation_id,
            ))
        projections.append({
            "id": str(order.id),
            "codigo": order.codigo,
            "estado_anterior": previous_state,
            "estado": next_state,
            "lineas": line_results,
        })
    return projections


def project_production_orders_for_operation(
    session,
    *,
    operation_order,
    actor,
    operation,
    affected_order_ids=None,
):
    return project_production_orders_for_outputs(
        session,
        output_ids=[item.id for item in operation_order.salidas],
        actor=actor,
        operation=operation,
        affected_order_ids=affected_order_ids,
    )
