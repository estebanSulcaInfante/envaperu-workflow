"""Audited annulment, never deletion, of unused OF/OA drafts."""

import copy

from sqlalchemy import select

from app.models.scm_auditoria import ScmEvento
from app.models.scm_production_orders import (
    ScmOrdenOperacion, ScmAsignacionDemandaSuministro, utc_now,
)
from app.services.scm_production_order_service import _reserve_operation
from app.services.scm_fulfillment_service import project_production_orders_for_outputs, _affected_order_ids
from app.services.scm_service_support import (
    ScmServiceError, actor_snapshot, expected_version, load_actor,
    reject_unknown_fields, required_text,
)


def annulment_summary(session, order):
    if order.estado != "ANULADA":
        return None
    event = session.scalar(select(ScmEvento).where(
        ScmEvento.aggregate_id == str(order.id),
        ScmEvento.tipo.in_(("OF_ANNULLED", "OA_ANNULLED")),
    ))
    return event.after_json.get("anulacion") if event else None


def _ensure_unused(session, order):
    if any((order.released_at, order.started_at, order.closed_at)):
        raise ScmServiceError("ORDER_HAS_ACTIVITY", "La orden conserva actividad previa.", status_code=409)
    outputs = order.salidas
    runs = order.fabricacion.corridas if order.fabricacion else []
    if any(run.estado != "BORRADOR" for run in runs) or any(
        output.cantidad_real is not None or output.cantidad_rechazada is not None
        for output in outputs
    ):
        raise ScmServiceError("ORDER_HAS_ACTIVITY", "La orden conserva resultados o corridas activadas.", status_code=409)
    # Fail closed on any operational reference, including future model additions.
    # These children are the draft itself, not downstream activity.
    structural = {"scm_orden_fabricacion", "scm_corrida_fabricacion", "scm_orden_operacion_salida", "scm_asignacion_demanda_suministro"}
    targets = {
        "scm_orden_operacion": [order.id],
        "scm_orden_fabricacion": [order.id],
        "scm_corrida_fabricacion": [run.id for run in runs],
        "scm_orden_operacion_salida": [output.id for output in outputs],
    }
    for table in ScmOrdenOperacion.metadata.tables.values():
        if table.name in structural:
            continue
        for fk in table.foreign_keys:
            ids = targets.get(fk.column.table.name)
            if ids and session.scalar(select(fk.parent).where(fk.parent.in_(ids)).limit(1)) is not None:
                raise ScmServiceError(
                    "ORDER_HAS_DEPENDENCIES",
                    "La orden tiene documentos o actividad vinculada; no puede anularse como borrador.",
                    status_code=409, details={"table": table.name},
                )


def annul_draft_order(session, *, actor_id, operation_id, order_id, kind, data):
    prefix, resource = (
        ("OF", "ordenes-fabricacion") if kind == "FABRICACION"
        else ("OA", "ordenes-armado")
    )
    try:
        actor = load_actor(session, actor_id, capability=f"{prefix}_ANULAR")
        reject_unknown_fields(data, allowed={"version", "motivo"})
        version = expected_version(data.get("version"))
        reason = required_text(data.get("motivo"), field="motivo", max_length=500)
        operation, replay = _reserve_operation(session, operation_id,
            f"POST /{resource}/{{id}}/anular", actor,
            {"order_id": str(order_id), "version": version, "motivo": reason})
        if replay is not None:
            return replay
        order = session.scalar(select(ScmOrdenOperacion).where(
            ScmOrdenOperacion.id == order_id, ScmOrdenOperacion.tipo == kind,
        ).with_for_update().execution_options(populate_existing=True))
        if order is None:
            raise ScmServiceError(f"{prefix}_NOT_FOUND", "La orden no existe.", status_code=404)
        if order.version != version:
            raise ScmServiceError("VERSION_CONFLICT", "La orden cambió. Actualice antes de anular.", status_code=409)
        if order.estado != "BORRADOR":
            raise ScmServiceError("INVALID_ORDER_STATE", "Solo se puede anular una orden en BORRADOR.", status_code=409)
        _ensure_unused(session, order)
        output_ids = [output.id for output in order.salidas]
        affected_ids = _affected_order_ids(session, output_ids)
        allocations = session.scalars(select(ScmAsignacionDemandaSuministro).where(
            ScmAsignacionDemandaSuministro.orden_operacion_salida_id.in_(output_ids),
            ScmAsignacionDemandaSuministro.estado != "CANCELADA",
        ).with_for_update().execution_options(populate_existing=True)).all()
        if any(a.cantidad_comprometida or a.cantidad_satisfecha for a in allocations):
            raise ScmServiceError("ORDER_HAS_FULFILLMENT", "La orden ya tiene cantidades comprometidas o satisfechas.", status_code=409)
        changes = []
        for allocation in allocations:
            changes.append({"id": str(allocation.id), "estado_anterior": allocation.estado,
                            "cantidad_planificada": format(allocation.cantidad_planificada, "f")})
            allocation.estado = "CANCELADA"
            allocation.version += 1
        order.estado = "ANULADA"
        order.version += 1
        if order.fabricacion:
            for run in order.fabricacion.corridas:
                run.estado = "ANULADA"
        session.flush()
        projections = project_production_orders_for_outputs(
            session, output_ids=output_ids, actor=actor, operation=operation,
            affected_order_ids=affected_ids,
        )
        response = {"id": str(order.id), "codigo": order.codigo,
                    "estado": order.estado, "version": order.version,
                    "anulacion": {"motivo": reason, "actor_id": actor.id,
                                  "actor": actor_snapshot(actor), "fecha": utc_now().isoformat()},
                    "asignaciones_canceladas": changes, "ordenes_produccion": projections}
        session.add(ScmEvento(
            aggregate_type="ORDEN_FABRICACION" if prefix == "OF" else "ORDEN_ARMADO",
            aggregate_id=str(order.id), tipo=f"{prefix}_ANNULLED", actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor), before_json={"estado": "BORRADOR", "version": version},
            after_json=copy.deepcopy(response), operation_id=operation.operation_id,
        ))
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
