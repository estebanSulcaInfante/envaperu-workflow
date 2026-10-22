"""Aggregate pause/resume/cancel commands for a fabrication order.

The OF is the authority for the aggregate lifecycle.  Individual work that
was already paused is intentionally distinguishable from work paused by the
aggregate command, so resume never wakes an unrelated operator decision.
"""

import copy
import hashlib
import json
from decimal import Decimal
from sqlalchemy import func, select

from app.models.scm_auditoria import ScmEvento
from app.models.scm_material_execution import ScmReservaMaterial, ScmRequerimientoMaterial
from app.models.scm_inline_wip import ScmReservaWipSalida, ScmSaldoWipSalida
from app.models.scm_ot import ScmManga, ScmTrabajoOt, ScmTramoMangaTrabajo
from app.models.scm_prepared_material import ScmReservaMaterialPreparado
from app.models.scm_production_orders import ScmAsignacionDemandaSuministro
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.services.scm_fabrication_order_service import _load_fabrication, _serialize
from app.services.scm_ot_service import (
    _activate_work_assignment,
    _close_work_assignment,
    _recompute_parent_state,
    _reserve_operation,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    required_text,
)
from app.services.scm_fulfillment_service import reconcile_cancelled_operation_fulfillment


ACTION_CAPABILITY = {
    "PAUSAR": "OF_PAUSAR_EJECUCION",
    "REANUDAR": "OF_PAUSAR_EJECUCION",
    "CANCELAR": "OF_CANCELAR_EJECUCION",
}
OPEN_MANGA_STATES = {
    "PLANIFICADA", "PREETIQUETADA", "EN_ARMADO", "CONTINUIDAD_PENDIENTE",
    "EN_LLENADO", "CERRADA_ARMADO_PENDIENTE_PESAJE",
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _ordered_by_id(items):
    """Return relationship/query collections in a stable fingerprint order."""
    return sorted(items, key=lambda item: str(item.id))


def _load_works(session, order_id, *, lock=False):
    statement = select(ScmTrabajoOt).where(
        ScmTrabajoOt.orden_operacion_id == order_id,
    ).order_by(ScmTrabajoOt.created_at, ScmTrabajoOt.id)
    if lock:
        statement = statement.with_for_update()
    return session.scalars(statement).all()


def _validate_resume_exclusivity(session, works):
    """Fail closed before restoring a work that would steal a machine/OT."""
    to_resume = [
        work for work in works
        if work.estado == "PAUSADO" and work.estado_previo_pausa == "EN_EJECUCION"
    ]
    if not to_resume:
        return
    by_ot = {}
    by_machine = {}
    for work in to_resume:
        ot_id = work.orden_trabajo_id
        machine_id = work.orden_trabajo.maquina_id
        by_ot.setdefault(ot_id, []).append(work)
        by_machine.setdefault(machine_id, []).append(work)
        session.scalar(
            select(Maquina).where(Maquina.id == machine_id).with_for_update()
        )
        session.scalar(
            select(RegistroDiarioProduccion)
            .where(RegistroDiarioProduccion.id == ot_id)
            .with_for_update()
        )
    if any(len(items) > 1 for items in by_ot.values()):
        raise ScmServiceError(
            "OT_WORK_ALREADY_RUNNING",
            "La OT tiene mas de un trabajo listo para reanudar; reconcilia la exclusividad.",
            status_code=409,
        )
    if any(len(items) > 1 for items in by_machine.values()):
        raise ScmServiceError(
            "MACHINE_ALREADY_RUNNING",
            "La OF tiene varios trabajos listos para reanudar en la misma maquina.",
            status_code=409,
        )
    for machine_id, items in by_machine.items():
        existing = session.scalar(
            select(ScmTrabajoOt)
            .join(RegistroDiarioProduccion, RegistroDiarioProduccion.id == ScmTrabajoOt.orden_trabajo_id)
            .where(
                RegistroDiarioProduccion.maquina_id == machine_id,
                ScmTrabajoOt.estado == "EN_EJECUCION",
                ScmTrabajoOt.id.not_in([work.id for work in items]),
            )
        )
        if existing is not None:
            raise ScmServiceError(
                "MACHINE_ALREADY_RUNNING",
                "La maquina ya ejecuta otro trabajo; no se reactiva la OF.",
                status_code=409,
                details={"trabajo_color_id": str(existing.id)},
            )


def _impact(session, order, action, works):
    mangas = session.scalars(
        select(ScmManga).join(
            ScmTrabajoOt, ScmTrabajoOt.id == ScmManga.trabajo_ot_id,
        ).where(ScmTrabajoOt.orden_operacion_id == order.id).order_by(ScmManga.id)
    ).all()
    tramos = session.scalars(
        select(ScmTramoMangaTrabajo).join(
            ScmTrabajoOt, ScmTrabajoOt.id == ScmTramoMangaTrabajo.trabajo_ot_id,
        ).where(ScmTrabajoOt.orden_operacion_id == order.id).order_by(ScmTramoMangaTrabajo.id)
    ).all()
    corrida_ids = [run.id for run in (order.fabricacion.corridas if order.fabricacion else ())]
    reservations = []
    if corrida_ids:
        reservations = session.scalars(
            select(ScmReservaMaterial).join(ScmRequerimientoMaterial).where(
                ScmReservaMaterial.estado == "ACTIVA",
                ScmRequerimientoMaterial.corrida_fabricacion_id.in_(corrida_ids),
            ).order_by(ScmReservaMaterial.id)
        ).all()
    works = _ordered_by_id(works)
    mangas = _ordered_by_id(mangas)
    tramos = _ordered_by_id(tramos)
    reservations = _ordered_by_id(reservations)
    outputs = _ordered_by_id(order.salidas)
    produced = [{
        "salida_id": str(output.id),
        # The canonical output article carries the inventory unit.  Do not
        # infer a KG/UN conversion from snapshots when it is absent.
        "unidad": getattr(output.articulo, "unidad_inventario", None),
        "cantidad_objetivo": format(output.cantidad_objetivo, "f"),
        "cantidad_real": format(output.cantidad_real, "f") if output.cantidad_real is not None else None,
        "cantidad_rechazada": format(output.cantidad_rechazada, "f") if output.cantidad_rechazada is not None else None,
    } for output in outputs]
    residuals = []
    assignment_fingerprint = []
    for output in outputs:
        for assignment in _ordered_by_id(output.asignaciones):
            assignment_fingerprint.append({
                "id": str(assignment.id),
                "version": assignment.version,
                "estado": assignment.estado,
                "cantidad_comprometida": assignment.cantidad_comprometida,
                "cantidad_satisfecha": assignment.cantidad_satisfecha,
                "cantidad_planificada": assignment.cantidad_planificada,
            })
        residual = session.scalar(
            select(func.coalesce(func.sum(
                ScmAsignacionDemandaSuministro.cantidad_comprometida
                - ScmAsignacionDemandaSuministro.cantidad_satisfecha
            ), 0)).where(
                ScmAsignacionDemandaSuministro.orden_operacion_salida_id == output.id,
                ScmAsignacionDemandaSuministro.estado != "CANCELADA",
            )
        ) or 0
        residuals.append({
            "salida_id": str(output.id),
            "unidad": getattr(output.articulo, "unidad_inventario", None),
            "cantidad": format(max(Decimal(residual), 0), "f"),
        })
    work_ids = [work.id for work in works]
    inline_wip = []
    prepared_reservations = []
    if work_ids:
        inline_wip = session.scalars(
            select(ScmReservaWipSalida)
            .join(ScmSaldoWipSalida, ScmSaldoWipSalida.id == ScmReservaWipSalida.saldo_id)
            .where(
                ScmSaldoWipSalida.trabajo_color_id.in_(work_ids),
                ScmReservaWipSalida.estado == "CREDITO_EN_LINEA_PENDIENTE",
            ).order_by(ScmReservaWipSalida.id)
        ).all()
        prepared_reservations = session.scalars(
            select(ScmReservaMaterialPreparado).where(
                ScmReservaMaterialPreparado.trabajo_ot_id.in_(work_ids),
                ScmReservaMaterialPreparado.estado == "ACTIVA",
            ).order_by(ScmReservaMaterialPreparado.id)
        ).all()
    inline_wip = _ordered_by_id(inline_wip)
    prepared_reservations = _ordered_by_id(prepared_reservations)
    blockers = []
    executable = [work for work in works if work.estado in {"PLANIFICADO", "EN_EJECUCION"}]
    if action == "PAUSAR":
        if order.estado != "EN_EJECUCION":
            blockers.append({"code": "OF_NOT_EXECUTING", "message": "Solo una OF en ejecucion puede pausarse."})
        if not executable:
            blockers.append({"code": "NO_EXECUTABLE_WORKS", "message": "La OF no tiene trabajos propios ejecutables pendientes."})
    elif action == "REANUDAR":
        if order.estado != "PAUSADA":
            blockers.append({"code": "OF_NOT_PAUSED", "message": "Solo una OF pausada puede reanudarse."})
        if not any(work.estado == "PAUSADO" and work.estado_previo_pausa for work in works):
            blockers.append({"code": "NO_WORKS_PAUSED_BY_OF", "message": "No hay trabajos pausados por esta OF para reanudar."})
    elif action == "CANCELAR":
        if order.estado not in {"EN_EJECUCION", "PAUSADA"}:
            blockers.append({"code": "OF_NOT_CANCELLABLE", "message": "Solo una OF en ejecucion o pausada puede cancelarse."})
        if not any(work.estado in {"PLANIFICADO", "EN_EJECUCION", "PAUSADO"} for work in works):
            blockers.append({"code": "NO_PENDING_WORKS", "message": "La OF no conserva trabajos propios pendientes."})
    open_mangas = [m for m in mangas if m.estado in OPEN_MANGA_STATES]
    open_tramos = [t for t in tramos if t.estado in {"PROGRAMADO", "ACTIVO"}]
    if open_mangas and action in {"PAUSAR", "CANCELAR"}:
        blockers.append({"code": "OPEN_MANGAS", "message": "Hay mangas abiertas; ciérralas o reconcilia su estado antes de continuar.", "count": len(open_mangas)})
    if open_tramos and action in {"PAUSAR", "CANCELAR"}:
        blockers.append({"code": "OPEN_MANGA_SEGMENTS", "message": "Hay tramos de manga abiertos; completa la continuidad antes de continuar.", "count": len(open_tramos)})
    # Pausing preserves an unissued reservation as pending work.  Cancellation
    # remains fail-closed until the residual, unissued quantity has a safe
    # release/reconciliation path.
    if reservations and action == "CANCELAR":
        blockers.append({"code": "ACTIVE_MATERIAL_RESERVATIONS", "message": "Hay reservas de material activas; concilia el saldo no emitido antes de cancelar.", "count": len(reservations)})
    if inline_wip and action in {"PAUSAR", "CANCELAR"}:
        blockers.append({"code": "PENDING_INLINE_WIP", "message": "Hay salidas WIP en linea pendientes de conciliacion.", "count": len(inline_wip)})
    if prepared_reservations and action in {"PAUSAR", "CANCELAR"}:
        blockers.append({"code": "PENDING_PREPARED_MATERIAL", "message": "Hay material preparado pendiente de conciliacion o custodia.", "count": len(prepared_reservations)})
    fingerprint = {
        "of": str(order.id),
        "action": action,
        "version": order.version,
        "estado": order.estado,
        "works": [{"id": str(w.id), "estado": w.estado, "version": w.version, "previo": w.estado_previo_pausa} for w in works],
        "mangas": [{"id": str(m.id), "estado": m.estado, "version": m.version} for m in mangas],
        "tramos": [{"id": str(t.id), "estado": t.estado} for t in tramos],
        "reservas": [{"id": str(r.id), "estado": r.estado, "emitida": r.emitida_neta_kg, "consumida": r.cantidad_consumida_kg} for r in reservations],
        # Produced facts and demand fulfillment are mutable authorities during
        # the cancel command.  They must be in the read/confirm fingerprint so
        # a GET followed by a concurrent production reconciliation fails closed
        # with STALE_IMPACT instead of silently cancelling a different balance.
        "producido": produced,
        "compromisos_residuales": residuals,
        "asignaciones": assignment_fingerprint,
        "wip": [str(item.id) for item in inline_wip],
        "material_preparado": [str(item.id) for item in prepared_reservations],
    }
    impact_hash = _hash(fingerprint)
    return {
        "action": action,
        "can_execute": not blockers,
        "blockers": blockers,
        "impact_hash": impact_hash,
        "version": order.version,
        "of": {"id": str(order.id), "codigo": order.codigo, "estado": order.estado},
        "trabajos": {"total": len(works), "ejecutables": len(executable), "afectados": len(executable) if action == "PAUSAR" else sum(w.estado in {"PLANIFICADO", "EN_EJECUCION", "PAUSADO"} for w in works)},
        "mangas_abiertas": len(open_mangas),
        "reservas_activas": len(reservations),
        "wip_pendiente": len(inline_wip),
        "material_preparado_pendiente": len(prepared_reservations),
        "producido_conservado": True,
        "producido": produced,
        "compromisos_residuales": residuals,
    }


def get_execution_impact(session, *, actor_id, operation_id, action):
    action = str(action or "").strip().upper()
    if action not in ACTION_CAPABILITY:
        raise ScmServiceError("INVALID_EXECUTION_ACTION", "La accion debe ser PAUSAR, REANUDAR o CANCELAR.", status_code=400)
    load_actor(session, actor_id, capability=ACTION_CAPABILITY[action])
    order = _load_fabrication(session, operation_id)
    return _impact(session, order, action, _load_works(session, operation_id))


def execute_execution_command(session, *, actor_id, operation_id, action, command, idempotency_key):
    action = str(action or "").strip().upper()
    if action not in ACTION_CAPABILITY:
        raise ScmServiceError("INVALID_EXECUTION_ACTION", "Accion de ejecucion invalida.", status_code=400)
    actor = load_actor(session, actor_id, capability=ACTION_CAPABILITY[action])
    if not isinstance(command, dict):
        raise ScmServiceError("JSON_OBJECT_REQUIRED", "Se requiere un objeto JSON.", status_code=400)
    motivo = required_text(command.get("motivo"), field="motivo", max_length=500)
    version = expected_version(command.get("version"))
    impact_hash = required_text(command.get("impact_hash"), field="impact_hash", max_length=64)
    payload = {"operation_id": str(operation_id), "action": action, "version": version, "impact_hash": impact_hash, "motivo": motivo}
    endpoint = f"POST /ordenes-fabricacion/{{id}}/{action.lower()}-ejecucion"
    operation, replay = _reserve_operation(session, idempotency_key, endpoint, actor, payload)
    if replay is not None:
        return replay
    try:
        order = _load_fabrication(session, operation_id, lock=True)
        # Lock order is the aggregate serialization point.  Child rows are
        # locked afterwards, matching create/transition/material guard paths.
        works = _load_works(session, operation_id, lock=True)
        if order.version != version:
            raise ScmServiceError("STALE_VERSION", "La OF cambio; vuelve a consultar el impacto.", status_code=409)
        impact = _impact(session, order, action, works)
        if impact["impact_hash"] != impact_hash:
            raise ScmServiceError("STALE_IMPACT", "El impacto ya no es vigente; revisa la informacion antes de confirmar.", status_code=409, details={"impact_hash": impact["impact_hash"]})
        if not impact["can_execute"]:
            first = impact["blockers"][0]
            raise ScmServiceError(first["code"], first["message"], status_code=409, details={"blockers": impact["blockers"], "impact": impact})
        before = _serialize(session, order)
        from app.models.scm_production_orders import utc_now
        now = utc_now()
        if action == "PAUSAR":
            order.estado = "PAUSADA"
            for work in works:
                if work.estado in {"PLANIFICADO", "EN_EJECUCION"}:
                    work.estado_previo_pausa = work.estado
                    work.estado = "PAUSADO"
                    work.motivo_pausa = motivo
                    work.pausada_at = now
                    work.version += 1
                    # The aggregate pause closes only the current personal
                    # interval.  Manga attribution remains immutable and
                    # already-paused individual work is left untouched.
                    _close_work_assignment(work, actor=actor, reason=motivo)
                    _recompute_parent_state(session, work)
            for run in order.fabricacion.corridas:
                if run.estado == "EN_EJECUCION":
                    run.estado = "PAUSADA"
        elif action == "REANUDAR":
            _validate_resume_exclusivity(session, works)
            order.estado = "EN_EJECUCION"
            for work in works:
                if work.estado == "PAUSADO" and work.estado_previo_pausa:
                    previous_state = work.estado_previo_pausa
                    if previous_state == "EN_EJECUCION":
                        _activate_work_assignment(session, work=work, actor=actor)
                    work.estado = previous_state
                    work.estado_previo_pausa = None
                    work.motivo_pausa = None
                    work.version += 1
                    _recompute_parent_state(session, work)
            for run in order.fabricacion.corridas:
                if run.estado == "PAUSADA":
                    run.estado = "EN_EJECUCION"
        else:
            fulfillment = reconcile_cancelled_operation_fulfillment(
                session,
                operation_order=order,
                actor=actor,
                operation=operation,
                reason=motivo,
            )
            order.estado = "CANCELADA"
            for work in works:
                if work.estado in {"PLANIFICADO", "EN_EJECUCION", "PAUSADO"}:
                    work.estado_previo_pausa = None
                    work.estado = "CANCELADO"
                    work.motivo_anulacion = motivo
                    work.anulada_at = now
                    work.version += 1
                    _close_work_assignment(
                        work,
                        actor=actor,
                        cancelled=True,
                        terminal=True,
                        reason=motivo,
                    )
                    _recompute_parent_state(session, work)
            for run in order.fabricacion.corridas:
                if run.estado in {"LIBERADA", "EN_EJECUCION", "PAUSADA"}:
                    run.estado = "CANCELADA"
        order.version += 1
        session.flush()
        response = _serialize(session, order)
        response.update({"action": action, "impact": impact, "idempotent_replay": False})
        if action == "CANCELAR":
            response["cancelacion"] = fulfillment
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="ORDEN_FABRICACION", aggregate_id=str(order.id),
            tipo=f"OF_{action}_EJECUCION", actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor), motivo=motivo,
            before_json=before, after_json=response, operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def pause_execution(session, **kwargs):
    return execute_execution_command(session, action="PAUSAR", **kwargs)


def resume_execution(session, **kwargs):
    return execute_execution_command(session, action="REANUDAR", **kwargs)


def cancel_execution(session, **kwargs):
    return execute_execution_command(session, action="CANCELAR", **kwargs)
