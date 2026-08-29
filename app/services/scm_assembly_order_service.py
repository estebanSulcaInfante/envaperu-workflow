"""Lifecycle services for canonical assembly/operation orders."""

import copy
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from sqlalchemy import select

from app.models.scm_articulos import (
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
)
from app.models.scm_auditoria import ScmEvento
from app.models.scm_estructuras import (
    ESTADO_ESTRUCTURA_APROBADA,
    ScmEstructuraRevision,
)
from app.models.scm_ot import ScmLoteArticulo
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    utc_now,
)
from app.models.scm_rutas import (
    ESTADO_RUTA_APROBADA,
    EXECUTOR_ORDEN_OPERACION,
    ScmOperacionPrecedencia,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.registro import RegistroDiarioProduccion
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_production_order_service import (
    _iso,
    _reserve_operation,
)
from app.services.scm_operation_schedule_projection import (
    operation_schedule_projection,
    operation_schedule_projections,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    positive_integer,
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
            "OA_NOT_FOUND",
            "La orden de armado no existe.",
            status_code=404,
        )
    return order


def _operation_snapshot(session, order):
    if len(order.salidas) != 1:
        raise ScmServiceError(
            "OA_OUTPUT_INVALID",
            "La OA debe conservar exactamente una salida.",
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
            "OA_ROUTE_SNAPSHOT_INVALID",
            "La OA no conserva una operacion y BOM resolubles.",
            status_code=409,
        )
    if (
        operation.ruta.content_hash != order.operacion_ruta_hash
        or operation.articulo_salida_id != order.salidas[0].articulo_scm_id
    ):
        raise ScmServiceError(
            "OA_ROUTE_SNAPSHOT_DRIFT",
            "La ruta congelada de la OA ya no coincide.",
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


def _serialize(session, order, *, schedule_projection=None):
    output = order.salidas[0]
    operation = _operation_snapshot(session, order)
    lot = session.scalar(select(ScmLoteArticulo).where(
        ScmLoteArticulo.orden_operacion_salida_id == output.id,
    ))
    return {
        **(
            schedule_projection
            if schedule_projection is not None
            else operation_schedule_projection(session, order)
        ),
        "id": str(order.id),
        "codigo": order.codigo,
        "tipo": order.tipo,
        "estado": order.estado,
        "version": order.version,
        "origen_demanda": order.origen_demanda,
        "motivo": order.motivo,
        "created_by_id": order.created_by_id,
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
    load_actor(session, actor_id, capability="OA_VER")
    orders = session.scalars(
        select(ScmOrdenOperacion)
        .where(ScmOrdenOperacion.tipo == "ENSAMBLE")
        .order_by(ScmOrdenOperacion.created_at.desc())
    ).all()
    projections = operation_schedule_projections(session, orders)
    return {
        "items": [
            _serialize(
                session,
                order,
                schedule_projection=projections[order.id],
            )
            for order in orders
        ]
    }


def get_assembly_order(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="OA_VER")
    return _serialize(session, _load(session, order_id))


def _exceptional_reason(value):
    if not isinstance(value, str) or not value.strip():
        raise ScmServiceError(
            "EXCEPTIONAL_ASSEMBLY_REASON_REQUIRED",
            "Explica el motivo de la reposicion WIP excepcional.",
            status_code=422,
            details={"field": "motivo"},
        )
    reason = value.strip()
    if len(reason) > 2000:
        raise ScmServiceError(
            "FIELD_TOO_LONG",
            "El campo motivo supera la longitud permitida.",
            status_code=400,
            details={"field": "motivo", "max_length": 2000},
        )
    return reason


def _exceptional_quantity(value):
    quantity = _quantity(value, "cantidad_objetivo")
    if quantity != quantity.to_integral_value():
        raise ScmServiceError(
            "DISCRETE_QUANTITY_REQUIRED",
            "Los articulos en unidad UN requieren cantidades enteras.",
            status_code=422,
            details={"field": "cantidad_objetivo"},
        )
    return quantity


def _engineering_not_ready(message, *, details=None):
    raise ScmServiceError(
        "ASSEMBLY_ENGINEERING_NOT_READY",
        message,
        status_code=422,
        details=details or {},
    )


def create_exceptional_assembly_order(
    session,
    *,
    actor_id,
    operation_id,
    data,
):
    """Create a governed WIP replenishment OA without a parent OP."""

    try:
        actor = load_actor(session, actor_id)
        if not actor.tiene_capacidad("OA_EXCEPCIONAL_CREAR"):
            raise ScmServiceError(
                "EXCEPTIONAL_ASSEMBLY_AUTHORIZATION_REQUIRED",
                "El actor no puede crear ordenes de armado excepcionales.",
                status_code=403,
                details={"capability": "OA_EXCEPCIONAL_CREAR"},
            )
        reject_unknown_fields(
            data,
            allowed={
                "origen_demanda",
                "motivo",
                "articulo_salida_id",
                "operacion_ruta_revision_id",
                "estructura_revision_id",
                "cantidad_objetivo",
                "versiones",
            },
        )
        origin = str(data.get("origen_demanda") or "").strip().upper()
        if origin != "REPOSICION_WIP":
            raise ScmServiceError(
                "INVALID_DEMAND_ORIGIN",
                "La OA excepcional solo admite REPOSICION_WIP.",
                status_code=422,
                details={"field": "origen_demanda"},
            )
        reason = _exceptional_reason(data.get("motivo"))
        target_id = positive_integer(
            data.get("articulo_salida_id"),
            field="articulo_salida_id",
        )
        route_operation_id = positive_integer(
            data.get("operacion_ruta_revision_id"),
            field="operacion_ruta_revision_id",
        )
        structure_id = positive_integer(
            data.get("estructura_revision_id"),
            field="estructura_revision_id",
        )
        quantity = _exceptional_quantity(data.get("cantidad_objetivo"))
        versions = data.get("versiones")
        if not isinstance(versions, dict):
            raise ScmServiceError(
                "VERSION_REQUIRED",
                "Se requieren las versiones de ruta y estructura.",
                status_code=400,
                details={"field": "versiones"},
            )
        reject_unknown_fields(
            versions,
            allowed={"ruta", "estructura"},
        )
        route_version = expected_version(versions.get("ruta"))
        structure_version = expected_version(versions.get("estructura"))
        command = {
            "origen_demanda": origin,
            "motivo": reason,
            "articulo_salida_id": target_id,
            "operacion_ruta_revision_id": route_operation_id,
            "estructura_revision_id": structure_id,
            "cantidad_objetivo": format(quantity, "f"),
            "versiones": {
                "ruta": route_version,
                "estructura": structure_version,
            },
        }
        audit, replay = _reserve_operation(
            session,
            operation_id,
            "POST /ordenes-armado/excepcionales",
            actor,
            command,
        )
        if replay is not None:
            return replay, False

        route_operation = session.scalar(
            select(ScmOperacionRuta)
            .where(ScmOperacionRuta.id == route_operation_id)
            .with_for_update()
        )
        if route_operation is None:
            _engineering_not_ready(
                "La operacion de ruta seleccionada no existe.",
                details={"field": "operacion_ruta_revision_id"},
            )
        route = session.scalar(
            select(ScmRutaRevision)
            .where(ScmRutaRevision.id == route_operation.ruta_id)
            .with_for_update()
        )
        structure = session.scalar(
            select(ScmEstructuraRevision)
            .where(
                ScmEstructuraRevision.id
                == route_operation.estructura_revision_id
            )
            .with_for_update()
        )
        target = session.scalar(
            select(ScmArticulo)
            .where(ScmArticulo.id == target_id)
            .with_for_update()
        )
        if (
            route is None
            or route.estado != ESTADO_RUTA_APROBADA
            or not route.content_hash
        ):
            _engineering_not_ready(
                "La ruta debe estar aprobada y congelada.",
                details={"resource": "ruta"},
            )
        if route.version != route_version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La ruta fue modificada; actualiza la ingenieria.",
                status_code=409,
                details={"resource": "ruta", "current": route.version},
            )
        if (
            target is None
            or not target.activo
            or target.clase != CLASE_SUBENSAMBLE_WIP
        ):
            _engineering_not_ready(
                "La salida debe ser un WIP activo.",
                details={"field": "articulo_salida_id"},
            )
        has_successor = session.scalar(
            select(ScmOperacionPrecedencia.id)
            .where(
                ScmOperacionPrecedencia.ruta_id == route.id,
                ScmOperacionPrecedencia.operacion_anterior_id
                == route_operation.id,
            )
            .limit(1)
        )
        if (
            route_operation.executor_kind != EXECUTOR_ORDEN_OPERACION
            or route_operation.articulo_salida_id != target.id
            or route.articulo_objetivo_id != target.id
            or has_successor is not None
        ):
            _engineering_not_ready(
                "Selecciona la operacion terminal compatible con el WIP.",
                details={"resource": "operacion_ruta"},
            )
        if (
            structure is None
            or structure.id != structure_id
            or structure.estado != ESTADO_ESTRUCTURA_APROBADA
            or not structure.content_hash
            or structure.articulo_resultado_id != target.id
        ):
            _engineering_not_ready(
                "La BOM aprobada no corresponde al WIP de salida.",
                details={"resource": "estructura"},
            )
        if structure.version != structure_version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La estructura fue modificada; actualiza la ingenieria.",
                status_code=409,
                details={
                    "resource": "estructura",
                    "current": structure.version,
                },
            )
        if not route_operation.centro_trabajo.activo:
            _engineering_not_ready(
                "El centro de trabajo de la operacion esta inactivo.",
                details={"resource": "centro_trabajo"},
            )

        order = ScmOrdenOperacion(
            codigo=generar_codigo_catalogo(
                "ORDEN_ARMADO",
                session=session,
            ),
            tipo="ENSAMBLE",
            origen_demanda=origin,
            motivo=reason,
            estado="BORRADOR",
            operacion_ruta_revision_id=route_operation.id,
            operacion_ruta_hash=route.content_hash,
            created_by_id=actor.id,
        )
        order.salidas.append(ScmOrdenOperacionSalida(
            articulo_scm_id=target.id,
            cantidad_objetivo=quantity,
        ))
        session.add(order)
        session.flush()
        response = _serialize(session, order)
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="ORDEN_ARMADO",
            aggregate_id=str(order.id),
            tipo="EXCEPTIONAL_ASSEMBLY_ORDER_CREATED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            motivo=reason,
            after_json=response,
            operation_id=audit.operation_id,
        ))
        session.commit()
        return response, True
    except Exception:
        session.rollback()
        raise


def transition_assembly_order(
    session,
    *,
    actor_id,
    operation_id,
    order_id,
    action,
    data,
):
    capability = "OA_LIBERAR" if action == "liberar" else "OA_EJECUTAR"
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
                "La OA fue modificada por otro usuario.",
                status_code=409,
            )
        transitions = {
            "liberar": ("BORRADOR", "LIBERADA"),
            "iniciar": ("LIBERADA", "EN_EJECUCION"),
            "cerrar": ("EN_EJECUCION", "CERRADA"),
        }
        if action not in transitions:
            raise ScmServiceError(
                "INVALID_OA_ACTION",
                "La transicion de OA no existe.",
                status_code=400,
            )
        expected_state, next_state = transitions[action]
        if order.estado != expected_state:
            raise ScmServiceError(
                "INVALID_OA_STATE",
                f"La OA debe estar en {expected_state}.",
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
            traceable_ots = session.scalars(
                select(RegistroDiarioProduccion)
                .where(
                    RegistroDiarioProduccion.tipo_ot == "ENSAMBLE",
                    RegistroDiarioProduccion.orden_operacion_id == order.id,
                )
                .with_for_update(of=RegistroDiarioProduccion)
            ).all()
            if traceable_ots:
                pending_ots = [
                    item for item in traceable_ots
                    if item.estado not in {"CERRADA", "ANULADA"}
                ]
                if pending_ots:
                    raise ScmServiceError(
                        "OA_HAS_PENDING_OTS",
                        "La OA conserva órdenes de trabajo sin cerrar.",
                        status_code=409,
                        details={
                            "ots": [item.codigo_ot for item in pending_ots],
                        },
                    )
                accepted = Decimal(output.cantidad_real or 0)
                rejected = Decimal(output.cantidad_rechazada or 0)
            else:
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
                    "OA_EMPTY_RESULT",
                    "El cierre debe registrar produccion o rechazo.",
                    status_code=422,
                )
            if (
                accepted != Decimal(output.cantidad_objetivo)
                and not str(data.get("motivo") or "").strip()
            ):
                raise ScmServiceError(
                    "OA_CLOSE_REASON_REQUIRED",
                    "Explique la diferencia entre la producción objetivo y real.",
                    status_code=422,
                    details={
                        "objetivo": format(output.cantidad_objetivo, "f"),
                        "real": format(accepted, "f"),
                    },
                )
            output.cantidad_real = accepted
            output.cantidad_rechazada = rejected
            order.closed_by_id = actor.id
            order.closed_at = utc_now()
            lot = session.scalar(
                select(ScmLoteArticulo)
                .where(
                    ScmLoteArticulo.orden_operacion_salida_id == output.id
                )
                .with_for_update(of=ScmLoteArticulo)
            )
            if lot is None:
                lot = ScmLoteArticulo(
                    codigo=f"LOT-{order.codigo}",
                    articulo_id=output.articulo_scm_id,
                    clase="SALIDA_ORDEN_OPERACION",
                    orden_operacion_salida_id=output.id,
                )
                session.add(lot)
            lot.cantidad_acreditada = accepted
            lot.estado_calidad = (
                "PENDIENTE"
                if (
                    output.articulo.definicion_wip is not None
                    and output.articulo.definicion_wip.requiere_calidad
                ) else "LIBERADO"
            )
            lot.event_time = order.closed_at
            lot.actor_id = actor.id
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
            aggregate_type="ORDEN_ARMADO",
            aggregate_id=str(order.id),
            tipo=f"OA_{action.upper()}",
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
