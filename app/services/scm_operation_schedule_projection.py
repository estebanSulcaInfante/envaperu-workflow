"""Read-only temporal projections shared by fabrication and assembly orders."""

from collections import defaultdict

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from app.models.registro import RegistroDiarioProduccion
from app.models.scm_ot import ScmTrabajoOt
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmOrdenOperacionSalida,
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
    ScmPlanProduccion,
)


def _effective_need_date(line, production_order):
    return line.fecha_necesidad or production_order.fecha_necesidad


def _programming_state(records):
    if not records:
        return "SIN_JORNADA"
    states = {item[1] for item in records.values()}
    if "EN_EJECUCION" in states:
        return "EN_EJECUCION"
    if states == {"CERRADA"}:
        return "CERRADA"
    return "PROGRAMADA"


def _operation_ot_records(session, operations):
    """Return unique, non-annulled OT records keyed by operation and OT id."""

    result = defaultdict(dict)
    fabrication_ids = {
        item.id for item in operations if item.tipo == "FABRICACION"
    }
    assembly_ids = {
        item.id for item in operations if item.tipo == "ENSAMBLE"
    }

    if fabrication_ids:
        rows = session.execute(
            select(
                ScmTrabajoOt.orden_operacion_id,
                RegistroDiarioProduccion.id,
                RegistroDiarioProduccion.fecha,
                RegistroDiarioProduccion.estado,
            )
            .join(
                RegistroDiarioProduccion,
                RegistroDiarioProduccion.id
                == ScmTrabajoOt.orden_trabajo_id,
            )
            .where(
                ScmTrabajoOt.orden_operacion_id.in_(fabrication_ids),
                RegistroDiarioProduccion.tipo_ot == "FABRICACION",
                RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
                RegistroDiarioProduccion.estado != "ANULADA",
            )
            .distinct()
        ).all()
        for operation_id, ot_id, operational_date, state in rows:
            result[operation_id][ot_id] = (operational_date, state)

    direct_conditions = []
    if assembly_ids:
        direct_conditions.append(and_(
            RegistroDiarioProduccion.tipo_ot == "ENSAMBLE",
            RegistroDiarioProduccion.orden_operacion_id.in_(assembly_ids),
        ))
    if fabrication_ids:
        # Explicit adapter for migrated/direct legacy OT. Normalized machine OT
        # derive their OF ownership exclusively from ScmTrabajoOt.
        direct_conditions.append(and_(
            RegistroDiarioProduccion.tipo_ot == "FABRICACION",
            RegistroDiarioProduccion.orden_operacion_id.in_(fabrication_ids),
            ~RegistroDiarioProduccion.trabajos_ot.any(),
        ))
    if direct_conditions:
        rows = session.execute(
            select(
                RegistroDiarioProduccion.orden_operacion_id,
                RegistroDiarioProduccion.id,
                RegistroDiarioProduccion.fecha,
                RegistroDiarioProduccion.estado,
            ).where(
                RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
                RegistroDiarioProduccion.estado != "ANULADA",
                or_(*direct_conditions),
            )
        ).all()
        for operation_id, ot_id, operational_date, state in rows:
            result[operation_id][ot_id] = (operational_date, state)

    return result


def _direct_demand_dates(session, operation_ids):
    """Resolve terminal demand dates through persisted N:M assignments."""

    dates = defaultdict(list)
    if not operation_ids:
        return dates
    rows = session.execute(
        select(
            ScmOrdenOperacionSalida.orden_operacion_id,
            ScmOrdenProduccionLinea.fecha_necesidad,
            ScmOrdenProduccion.fecha_necesidad,
        )
        .select_from(ScmAsignacionDemandaSuministro)
        .join(
            ScmOrdenOperacionSalida,
            ScmOrdenOperacionSalida.id
            == ScmAsignacionDemandaSuministro.orden_operacion_salida_id,
        )
        .join(
            ScmOrdenProduccionLinea,
            ScmOrdenProduccionLinea.id
            == ScmAsignacionDemandaSuministro.orden_produccion_linea_id,
        )
        .join(
            ScmOrdenProduccion,
            ScmOrdenProduccion.id
            == ScmOrdenProduccionLinea.orden_produccion_id,
        )
        .where(
            ScmOrdenOperacionSalida.orden_operacion_id.in_(operation_ids),
            ScmAsignacionDemandaSuministro.estado != "CANCELADA",
            ScmAsignacionDemandaSuministro.cantidad_planificada > 0,
            ScmOrdenProduccionLinea.estado != "CANCELADA",
        )
    ).all()
    for operation_id, line_date, header_date in rows:
        effective = line_date or header_date
        if effective is not None:
            dates[operation_id].append(effective)
    return dates


def _plans_by_id(session, operations):
    plan_ids = {
        item.plan_produccion_id
        for item in operations
        if item.plan_produccion_id is not None
    }
    if not plan_ids:
        return {}
    plans = session.scalars(
        select(ScmPlanProduccion)
        .where(ScmPlanProduccion.id.in_(plan_ids))
        .options(
            selectinload(ScmPlanProduccion.orden_produccion)
            .selectinload(ScmOrdenProduccion.lineas)
        )
    ).all()
    return {item.id: item for item in plans}


def _proposal_demand_dates(operation, plan):
    """Resolve upstream document demand from the confirmed plan snapshot.

    Persisted demand assignments point to the terminal output. Intermediate OF
    retain their exact demand contributions in the immutable proposal snapshot,
    so this adapter preserves the same line-level dates for the whole route.
    """

    if plan is None or not operation.propuesta_clave:
        return []
    proposal = plan.propuesta_json or {}
    document = next(
        (
            item for item in proposal.get("documentos", [])
            if item.get("clave") == operation.propuesta_clave
        ),
        None,
    )
    if document is None:
        return []
    production_order = plan.orden_produccion
    lines = {str(item.id): item for item in production_order.lineas}
    dates = []
    for contribution in document.get("aportes_demanda", []):
        line = lines.get(str(contribution.get("linea_id")))
        if line is None or line.estado == "CANCELADA":
            continue
        effective = _effective_need_date(line, production_order)
        if effective is not None:
            dates.append(effective)
    return dates


def operation_schedule_projections(session, operations):
    """Project temporal context for many OF/OA with a bounded query count."""

    operations = list(operations)
    if not operations:
        return {}
    operation_ids = {item.id for item in operations}
    ot_records = _operation_ot_records(session, operations)
    demand_dates = _direct_demand_dates(session, operation_ids)
    plans = _plans_by_id(session, operations)
    projections = {}

    for operation in operations:
        plan = plans.get(operation.plan_produccion_id)
        production_order = plan.orden_produccion if plan is not None else None
        need_dates = list(demand_dates.get(operation.id, ()))
        if not need_dates:
            need_dates = _proposal_demand_dates(operation, plan)
        first_need = min(need_dates) if need_dates else None
        last_need = max(need_dates) if need_dates else None

        records = ot_records.get(operation.id, {})
        operational_dates = [item[0] for item in records.values()]
        first_ot = min(operational_dates) if operational_dates else None
        last_ot = max(operational_dates) if operational_dates else None
        context_state = _programming_state(records)
        context = {
            "fecha_necesidad_min": (
                first_need.isoformat() if first_need is not None else None
            ),
            "fecha_necesidad_max": (
                last_need.isoformat() if last_need is not None else None
            ),
            "fecha_necesidad_motivo": (
                None if first_need is not None else "SIN_DEMANDA_FECHADA"
            ),
            "fecha_ot_primera": (
                first_ot.isoformat() if first_ot is not None else None
            ),
            "fecha_ot_ultima": (
                last_ot.isoformat() if last_ot is not None else None
            ),
            "programacion_estado": context_state,
            "cantidad_ot": len(records),
        }
        projections[operation.id] = {
            "contexto_temporal": context,
            # Flat aliases remain during the N3 client migration.
            "fecha_necesidad": context["fecha_necesidad_min"],
            "fecha_necesidad_fuente": (
                {
                    "tipo": "OP",
                    "id": str(production_order.id),
                    "codigo": production_order.codigo,
                }
                if production_order is not None
                else None
            ),
            "rango_fechas_ot": {
                "desde": context["fecha_ot_primera"],
                "hasta": context["fecha_ot_ultima"],
                "cantidad": context["cantidad_ot"],
            },
            "programacion_estado": (
                "SIN_PROGRAMAR"
                if context_state == "SIN_JORNADA"
                else "PROGRAMADA"
            ),
        }

    return projections


def operation_schedule_projection(session, operation):
    """Project one OF/OA while sharing the exact list-contract semantics."""

    return operation_schedule_projections(session, [operation])[operation.id]
