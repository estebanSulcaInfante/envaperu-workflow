"""Lifecycle services for canonical assembly/operation orders."""

import copy
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from sqlalchemy import select

from app.models.scm_auditoria import ScmEvento
from app.models.scm_ot import ScmLoteArticulo
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    utc_now,
)
from app.models.scm_rutas import ScmOperacionRuta
from app.models.registro import RegistroDiarioProduccion
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
)


def _quantity(value, field, *, allow_zero=False):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} contiene una cantidad invalida.",
            status_code=422,
            details={"field": field},
        ) from error
    if (
        not parsed.is_finite()
        or (parsed < 0 if allow_zero else parsed <= 0)
    ):
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} contiene una cantidad invalida.",
            status_code=422,
            details={"field": field},
        )
    return parsed


def _load(session, order_id, *, lock=False):
    statement = select(ScmOrdenOperacion).where(
        ScmOrdenOperacion.id == order_id,
        ScmOrdenOperacion.tipo == "ENSAMBLE",
    )
    if lock:
        statement = statement.with_for_update()
    order = session.scalar(statement)
    if order is None:
        raise ScmServiceError(
            "OE_NOT_FOUND",
            "La orden de armado no existe.",
            status_code=404,
        )
    return order


def _operation_snapshot(session, order):
    if len(order.salidas) != 1:
        raise ScmServiceError(
            "OE_OUTPUT_INVALID",
            "La OE debe conservar exactamente una salida.",
            status_code=409,
        )
    operation = session.get(
        ScmOperacionRuta,
        order.operacion_ruta_revision_id,
    )
    if (
        operation is None
        or operation.executor_kind != "ORDEN_OPERACION"
        or operation.estructura_revision is None
    ):
        raise ScmServiceError(
            "OE_ROUTE_SNAPSHOT_INVALID",
            "La OE no conserva una operacion y BOM resolubles.",
            status_code=409,
        )
    if (
        operation.ruta.content_hash != order.operacion_ruta_hash
        or operation.articulo_salida_id != order.salidas[0].articulo_scm_id
    ):
        raise ScmServiceError(
            "OE_ROUTE_SNAPSHOT_DRIFT",
            "La ruta congelada de la OE ya no coincide.",
            status_code=409,
        )
    return operation


def _planned_inputs(operation, target):
    items = []
    for component in operation.estructura_revision.componentes:
        required = Decimal(target) * Decimal(component.cantidad)
        waste = Decimal(component.merma_tecnica_pct or 0)
        if waste:
            required /= Decimal("1") - waste / Decimal("100")
        required = required.to_integral_value(rounding=ROUND_CEILING)
        article = component.articulo_componente
        items.append({
            "articulo_scm_id": article.id,
            "articulo": {
                "codigo": article.codigo,
                "nombre": article.nombre,
                "clase": article.clase,
            },
            "cantidad_planificada": format(required, "f"),
            "cantidad_por_salida": format(component.cantidad, "f"),
            "merma_tecnica_pct": (
                format(component.merma_tecnica_pct, "f")
                if component.merma_tecnica_pct is not None else "0"
            ),
        })
    return items


def _serialize(session, order):
    output = order.salidas[0]
    operation = _operation_snapshot(session, order)
    lot = session.scalar(select(ScmLoteArticulo).where(
        ScmLoteArticulo.orden_operacion_salida_id == output.id,
    ))
    return {
        "id": str(order.id),
        "codigo": order.codigo,
        "tipo": order.tipo,
        "estado": order.estado,
        "version": order.version,
        "origen_demanda": order.origen_demanda,
        "plan_produccion_id": (
            str(order.plan_produccion_id)
            if order.plan_produccion_id else None
        ),
        "propuesta_clave": order.propuesta_clave,
        "operacion": {
            "id": operation.id,
            "nombre": operation.nombre,
            "tipo": operation.tipo,
            "permite_concurrente": operation.permite_concurrente,
            "centro_trabajo_id": operation.centro_trabajo_id,
            "centro_trabajo": operation.centro_trabajo.nombre,
            "estructura_revision_id": operation.estructura_revision_id,
            "estructura_revision": (
                operation.estructura_revision.numero_revision
            ),
        },
        "salida": {
            "id": str(output.id),
            "articulo_scm_id": output.articulo_scm_id,
            "codigo": output.articulo.codigo,
            "nombre": output.articulo.nombre,
            "clase": output.articulo.clase,
            "cantidad_objetivo": format(output.cantidad_objetivo, "f"),
            "cantidad_real": (
                format(output.cantidad_real, "f")
                if output.cantidad_real is not None else None
            ),
            "cantidad_rechazada": (
                format(output.cantidad_rechazada, "f")
                if output.cantidad_rechazada is not None else None
            ),
        },
        "entradas_planificadas": _planned_inputs(
            operation,
            output.cantidad_objetivo,
        ),
        "lote_salida": lot.to_dict() if lot else None,
        "released_by_id": order.released_by_id,
        "released_at": _iso(order.released_at),
        "started_by_id": order.started_by_id,
        "started_at": _iso(order.started_at),
        "closed_by_id": order.closed_by_id,
        "closed_at": _iso(order.closed_at),
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
    }


def list_assembly_orders(session, *, actor_id):
    load_actor(session, actor_id, capability="OE_VER")
    orders = session.scalars(
        select(ScmOrdenOperacion)
        .where(ScmOrdenOperacion.tipo == "ENSAMBLE")
        .order_by(ScmOrdenOperacion.created_at.desc())
    ).all()
    return {"items": [_serialize(session, order) for order in orders]}


def get_assembly_order(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="OE_VER")
    return _serialize(session, _load(session, order_id))


def transition_assembly_order(
    session,
    *,
    actor_id,
    operation_id,
    order_id,
    action,
    data,
):
    capability = "OE_LIBERAR" if action == "liberar" else "OE_EJECUTAR"
    actor = load_actor(session, actor_id, capability=capability)
    allowed = {"version"}
    if action == "cerrar":
        allowed |= {"cantidad_real", "cantidad_rechazada", "motivo"}
    reject_unknown_fields(data, allowed=allowed)
    version = expected_version(data.get("version"))
    audit, replay = _reserve_operation(
        session,
        operation_id,
        f"POST /ordenes-armado/{{id}}/{action}",
        actor,
        {"order_id": str(order_id), **data, "version": version},
    )
    if replay is not None:
        return replay
    try:
        order = _load(session, order_id, lock=True)
        if order.version != version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OE fue modificada por otro usuario.",
                status_code=409,
            )
        transitions = {
            "liberar": ("BORRADOR", "LIBERADA"),
            "iniciar": ("LIBERADA", "EN_EJECUCION"),
            "cerrar": ("EN_EJECUCION", "CERRADA"),
        }
        if action not in transitions:
            raise ScmServiceError(
                "INVALID_OE_ACTION",
                "La transicion de OE no existe.",
                status_code=400,
            )
        expected_state, next_state = transitions[action]
        if order.estado != expected_state:
            raise ScmServiceError(
                "INVALID_OE_STATE",
                f"La OE debe estar en {expected_state}.",
                status_code=409,
            )
        _operation_snapshot(session, order)
        output = order.salidas[0]
        if action == "liberar":
            order.released_by_id = actor.id
            order.released_at = utc_now()
        elif action == "iniciar":
            order.started_by_id = actor.id
            order.started_at = utc_now()
        else:
            traceable_ot_id = session.scalar(
                select(RegistroDiarioProduccion.public_id)
                .where(
                    RegistroDiarioProduccion.tipo_ot == "ENSAMBLE",
                    RegistroDiarioProduccion.orden_operacion_id == order.id,
                )
                .limit(1)
            )
            if traceable_ot_id is not None:
                raise ScmServiceError(
                    "OE_TRACEABLE_CLOSE_REQUIRED",
                    "La OE tiene OT de armado y debe cerrarse desde sus mangas "
                    "con consumo trazable de componentes.",
                    status_code=409,
                )
            accepted = _quantity(
                data.get("cantidad_real"),
                "cantidad_real",
                allow_zero=True,
            )
            rejected = _quantity(
                data.get("cantidad_rechazada", 0),
                "cantidad_rechazada",
                allow_zero=True,
            )
            if accepted + rejected <= 0:
                raise ScmServiceError(
                    "OE_EMPTY_RESULT",
                    "El cierre debe registrar produccion o rechazo.",
                    status_code=422,
                )
            output.cantidad_real = accepted
            output.cantidad_rechazada = rejected
            order.closed_by_id = actor.id
            order.closed_at = utc_now()
            lot = ScmLoteArticulo(
                codigo=f"LOT-{order.codigo}",
                articulo_id=output.articulo_scm_id,
                clase="SALIDA_ORDEN_OPERACION",
                orden_operacion_salida_id=output.id,
                cantidad_acreditada=accepted,
                estado_calidad=(
                    "PENDIENTE"
                    if (
                        output.articulo.definicion_wip is not None
                        and output.articulo.definicion_wip.requiere_calidad
                    ) else "LIBERADO"
                ),
                event_time=order.closed_at,
                actor_id=actor.id,
            )
            session.add(lot)
            remaining = accepted
            for allocation in output.asignaciones:
                satisfied = min(
                    Decimal(allocation.cantidad_planificada),
                    remaining,
                )
                remaining -= satisfied
                allocation.cantidad_comprometida = satisfied
                allocation.cantidad_satisfecha = satisfied
                if satisfied >= allocation.cantidad_planificada:
                    allocation.estado = "SATISFECHA"
                elif satisfied > 0:
                    allocation.estado = "COMPROMETIDA"
                else:
                    allocation.estado = "PLANIFICADA"
                allocation.version += 1
        order.estado = next_state
        order.version += 1
        session.flush()
        response = _serialize(session, order)
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="ORDEN_ENSAMBLE",
            aggregate_id=str(order.id),
            tipo=f"OE_{action.upper()}",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            motivo=data.get("motivo"),
            after_json=response,
            operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
