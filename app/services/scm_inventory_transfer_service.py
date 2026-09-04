"""Sesiones multi-QR y transferencias append-only del Kardex SCM."""

import copy
import hashlib
import json
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, false, func, or_, select

from app.models.scm_articulos import ScmArticulo
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_catalogos import ScmMaterial
from app.models.scm_inventory import (
    ScmMovimientoInventario, ScmSaldoInventario, ScmSaldoMaterialInventario,
    ScmUbicacionInventario,
)
from app.models.scm_inventory_operations import (
    ScmSesionOperacionAlmacen, ScmSesionOperacionItem,
    ScmTransferenciaInventario, ScmTransferenciaItem,
)
from app.models.scm_ot import ScmManga
from app.models.scm_warehouse import ScmExistenciaManga
from app.services.scm_service_support import (
    ScmServiceError, actor_snapshot, expected_version, load_actor,
)
from app.services.scm_alert_service import upsert_operational_alert
from app.services.scm_warehouse_scope_service import warehouse_scope


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _reserve(session, operation_id, endpoint, actor, data):
    digest = _hash({"endpoint": endpoint, "actor_id": actor.id, "data": data})
    prior = session.get(ScmOperacion, operation_id)
    if prior:
        if prior.endpoint != endpoint or prior.request_sha256 != digest:
            raise ScmServiceError("IDEMPOTENCY_CONFLICT", "La clave ya fue usada con otra solicitud.", status_code=409)
        if prior.response_json is None:
            raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La operacion previa no termino.", status_code=409)
        return None, copy.deepcopy(prior.response_json)
    op = ScmOperacion(operation_id=operation_id, endpoint=endpoint, actor_id=actor.id, request_sha256=digest)
    session.add(op)
    session.flush()
    return op, None


def _complete(session, operation, payload, status=200):
    operation.response_json = copy.deepcopy(payload)
    operation.estado_http = status
    session.commit()
    return payload


def _scope_location(session, actor, location_id, *, destination=False):
    location = session.get(ScmUbicacionInventario, location_id)
    if location is None or not location.activo:
        raise ScmServiceError("LOCATION_NOT_FOUND", "La ubicacion no existe.", status_code=404)
    scope = warehouse_scope(session, actor_id=actor.id)
    if scope["configured"] and not scope["transversal"]:
        allowed = location.almacen_id in scope["warehouse_ids"]
        if destination and location.tipo in {"PUNTO_PRODUCCION", "STAGING"}:
            allowed = True
        if not allowed:
            raise ScmServiceError("LOCATION_NOT_FOUND", "La ubicacion no existe.", status_code=404)
    return location


def _session_payload(item):
    return {
        "id": str(item.id), "tipo": item.tipo, "modalidad": item.modalidad,
        "estado": item.estado, "version": item.version,
        "origen": item.origen.to_dict(), "destino": item.destino.to_dict(),
        "contexto": dict(item.contexto_json or {}),
        "items": [{
            "id": str(child.id), "codigo": child.codigo_escaneado,
            "estado": child.estado, "motivo": child.motivo,
            "cantidad": format(child.cantidad_snapshot, "f"),
            "existencia_id": str(child.existencia_manga_id),
            "manga_codigo": child.existencia.manga.codigo,
        } for child in item.items],
    }


def _transfer_payload(item):
    return {
        "id": str(item.id), "codigo": item.codigo, "estado": item.estado,
        "modalidad": item.modalidad, "version": item.version,
        "custodio_id": item.custodio_id,
        "origen": item.origen.to_dict(), "destino": item.destino.to_dict(),
        "incidencia": dict(item.incidencia_json or {}),
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "items": [{
            "id": str(child.id), "existencia_id": str(child.existencia_manga_id),
            "manga_codigo": child.existencia.manga.codigo,
            "cantidad": format(child.cantidad, "f"),
            "movimiento_salida_id": str(child.movimiento_salida_id) if child.movimiento_salida_id else None,
            "movimiento_transito_id": str(child.movimiento_transito_id) if child.movimiento_transito_id else None,
            "movimiento_entrada_id": str(child.movimiento_entrada_id) if child.movimiento_entrada_id else None,
        } for child in item.items],
    }


def create_operation_session(session, *, actor_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_MOVILIZAR")
    origin = _scope_location(session, actor, data.get("origen_ubicacion_id"))
    destination = _scope_location(session, actor, data.get("destino_ubicacion_id"), destination=True)
    kind = str(data.get("tipo") or "TRANSFERENCIA").upper()
    mode = str(data.get("modalidad") or "PICKUP").upper()
    if kind not in {"ENTRADA", "SALIDA", "TRANSFERENCIA", "RETORNO"} or mode not in {"PICKUP", "ENTREGA"}:
        raise ScmServiceError("INVALID_WAREHOUSE_OPERATION", "Tipo o modalidad invalida.", status_code=422)
    command = {"tipo": kind, "modalidad": mode, "origen": origin.id, "destino": destination.id, "contexto": data.get("contexto") or {}}
    operation, replay = _reserve(session, operation_id, "POST /operaciones-almacen/sesiones", actor, command)
    if replay is not None:
        return replay
    item = ScmSesionOperacionAlmacen(
        tipo=kind, modalidad=mode, origen_ubicacion_id=origin.id,
        destino_ubicacion_id=destination.id, contexto_json=command["contexto"],
        actor_id=actor.id,
    )
    session.add(item)
    session.flush()
    payload = _session_payload(item)
    session.add(ScmEvento(
        aggregate_type="SESION_OPERACION_ALMACEN", aggregate_id=str(item.id),
        tipo="SESION_ALMACEN_ABIERTA", actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor), after_json=payload,
        operation_id=operation.operation_id,
    ))
    return _complete(session, operation, payload, 201)


def get_operation_session(session, *, actor_id, session_id):
    actor = load_actor(session, actor_id, capability="INVENTARIO_MOVILIZAR")
    item = session.get(ScmSesionOperacionAlmacen, session_id)
    if item is None or (item.actor_id != actor.id and not actor.tiene_capacidad("INVENTARIO_CONTROL_TRANSVERSAL")):
        raise ScmServiceError("WAREHOUSE_SESSION_NOT_FOUND", "La sesion no existe.", status_code=404)
    return _session_payload(item)


def scan_operation_item(session, *, actor_id, session_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_MOVILIZAR")
    item = session.get(ScmSesionOperacionAlmacen, session_id)
    if item is None or item.actor_id != actor.id:
        raise ScmServiceError("WAREHOUSE_SESSION_NOT_FOUND", "La sesion no existe.", status_code=404)
    if item.estado not in {"ABIERTA", "LISTA"}:
        raise ScmServiceError("WAREHOUSE_SESSION_CLOSED", "La sesion ya no admite escaneos.", status_code=409)
    code = str(data.get("codigo") or "").strip()
    if not code:
        raise ScmServiceError("QR_CODE_REQUIRED", "codigo es obligatorio.", status_code=400)
    operation, replay = _reserve(session, operation_id, "POST /operaciones-almacen/sesiones/{id}/escanear", actor, {"session_id": str(item.id), "codigo": code})
    if replay is not None:
        return replay
    if len(item.items) >= 100:
        raise ScmServiceError("WAREHOUSE_SESSION_LIMIT", "La sesion admite hasta 100 unidades.", status_code=422)
    try:
        public_id = UUID(code)
    except ValueError:
        public_id = None
    identity_filter = ScmManga.codigo == code
    if public_id is not None:
        identity_filter = identity_filter | (ScmManga.public_id == public_id)
    existence = session.scalar(
        select(ScmExistenciaManga).join(ScmManga).where(identity_filter)
    )
    if existence is None or existence.ubicacion_id != item.origen_ubicacion_id:
        raise ScmServiceError("LOGISTIC_UNIT_NOT_FOUND", "La unidad no esta disponible en el origen.", status_code=404)
    if existence.estado_calidad != "LIBERADA" or existence.estado_logistico not in {"RECIBIDA_ALMACEN", "RESERVADA", "PENDIENTE_RETORNO"}:
        raise ScmServiceError("LOGISTIC_UNIT_NOT_AVAILABLE", "La unidad no esta habilitada para mover.", status_code=409)
    if any(child.existencia_manga_id == existence.id for child in item.items):
        raise ScmServiceError("LOGISTIC_UNIT_DUPLICATE", "La unidad ya fue escaneada.", status_code=409)
    child = ScmSesionOperacionItem(
        sesion_id=item.id, existencia_manga_id=existence.id,
        codigo_escaneado=code, cantidad_snapshot=existence.cantidad_fisica,
        orden=len(item.items) + 1,
    )
    item.items.append(child)
    item.estado = "LISTA"
    item.version += 1
    session.flush()
    payload = _session_payload(item)
    return _complete(session, operation, payload)


def remove_operation_item(session, *, actor_id, session_id, item_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_MOVILIZAR")
    item = session.scalar(select(ScmSesionOperacionAlmacen).where(
        ScmSesionOperacionAlmacen.id == session_id
    ).with_for_update())
    if item is None or item.actor_id != actor.id:
        raise ScmServiceError("WAREHOUSE_SESSION_NOT_FOUND", "La sesion no existe.", status_code=404)
    if item.estado not in {"ABIERTA", "LISTA"}:
        raise ScmServiceError("WAREHOUSE_SESSION_CLOSED", "La sesion ya no admite cambios.", status_code=409)
    command = {"session_id": str(item.id), "item_id": str(item_id), "version": data.get("version")}
    operation, replay = _reserve(
        session, operation_id,
        "DELETE /operaciones-almacen/sesiones/{id}/items/{item_id}", actor, command,
    )
    if replay is not None:
        return replay
    if expected_version(data.get("version")) != item.version:
        raise ScmServiceError("VERSION_CONFLICT", "La sesion cambio.", status_code=409)
    child = next((candidate for candidate in item.items if candidate.id == item_id), None)
    if child is None:
        raise ScmServiceError("WAREHOUSE_SESSION_ITEM_NOT_FOUND", "La unidad escaneada no existe.", status_code=404)
    item.items.remove(child)
    session.delete(child)
    session.flush()
    item.estado = "LISTA" if item.items else "ABIERTA"
    item.version += 1
    session.flush()
    return _complete(session, operation, _session_payload(item))


def trace_logistic_unit(session, *, actor_id, code):
    actor = load_actor(session, actor_id, capability="INVENTARIO_VER")
    normalized = str(code or "").strip()
    try:
        public_id = UUID(normalized)
    except ValueError:
        public_id = None
    identity_filter = ScmManga.codigo == normalized
    if public_id is not None:
        identity_filter = identity_filter | (ScmManga.public_id == public_id)
    existence = session.scalar(select(ScmExistenciaManga).join(ScmManga).where(identity_filter))
    if existence is None:
        raise ScmServiceError("LOGISTIC_UNIT_NOT_FOUND", "La unidad logistica no existe.", status_code=404)
    scope = warehouse_scope(session, actor_id=actor.id)
    location = session.get(ScmUbicacionInventario, existence.ubicacion_id)
    visible = not scope["configured"] or scope["transversal"] or (
        location is not None and location.almacen_id in scope["warehouse_ids"]
    )
    if not visible:
        raise ScmServiceError("LOGISTIC_UNIT_NOT_FOUND", "La unidad logistica no existe.", status_code=404)
    transfers = session.scalars(select(ScmTransferenciaInventario).join(
        ScmTransferenciaItem
    ).where(ScmTransferenciaItem.existencia_manga_id == existence.id).order_by(
        ScmTransferenciaInventario.created_at.asc()
    )).unique().all()
    transfer_ids = {str(item.id) for item in transfers}
    movements = session.scalars(select(ScmMovimientoInventario).where(
        (ScmMovimientoInventario.referencia_id.in_(transfer_ids)) |
        (ScmMovimientoInventario.referencia_id == str(existence.id))
    ).order_by(ScmMovimientoInventario.created_at.asc())).all() if transfer_ids else []
    return {
        "codigo": existence.manga.codigo,
        "existencia_id": str(existence.id),
        "estado_logistico": existence.estado_logistico,
        "cantidad": format(existence.cantidad_fisica, "f"),
        "ubicacion": location.to_dict() if location else None,
        "transferencias": [_transfer_payload(item) for item in transfers],
        "movimientos": [{
            "id": str(movement.id), "tipo": movement.tipo,
            "cantidad_delta": format(movement.cantidad_delta, "f"),
            "saldo_fisico_resultante": format(movement.saldo_fisico_resultante, "f"),
            "motivo": movement.motivo, "referencia_tipo": movement.referencia_tipo,
            "referencia_id": movement.referencia_id,
            "created_at": movement.created_at.isoformat() if movement.created_at else None,
        } for movement in movements],
    }


def _balance(session, article_id, location_id):
    balance = session.scalar(select(ScmSaldoInventario).where(
        ScmSaldoInventario.articulo_scm_id == article_id,
        ScmSaldoInventario.ubicacion_id == location_id,
    ).with_for_update())
    if balance is None:
        balance = ScmSaldoInventario(articulo_scm_id=article_id, ubicacion_id=location_id)
        session.add(balance)
        session.flush()
    return balance


def _transit_location(session):
    item = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == "TRANSITO_ALMACEN"
    ))
    if item is None:
        item = ScmUbicacionInventario(
            codigo="TRANSITO_ALMACEN", nombre="Transito entre almacenes",
            tipo="TRANSITO", permite_saldo_libre=False,
            clases_articulo_json=["PIEZA_COLOR", "SUBENSAMBLE_WIP", "PRODUCTO_TERMINADO"],
        )
        session.add(item)
        session.flush()
    return item


def _move_existence(
    session, *, existence, destination, root_operation_id, actor, reference,
    logistic_state,
):
    quantity = Decimal(existence.cantidad_fisica)
    origin_balance = session.get(ScmSaldoInventario, existence.saldo_id)
    destination_balance = _balance(session, existence.articulo_scm_id, destination.id)
    if origin_balance is None or Decimal(origin_balance.cantidad_fisica) < quantity:
        raise ScmServiceError("INVENTORY_CONFLICT", "El saldo origen no cubre la unidad.", status_code=409)
    origin_balance.cantidad_fisica -= quantity
    origin_balance.cantidad_reservada = max(Decimal("0"), Decimal(origin_balance.cantidad_reservada) - min(Decimal(origin_balance.cantidad_reservada), quantity))
    origin_balance.version += 1
    destination_balance.cantidad_fisica += quantity
    destination_balance.version += 1
    exit_id = uuid5(NAMESPACE_URL, f"{root_operation_id}:{existence.id}:salida")
    entry_id = uuid5(NAMESPACE_URL, f"{root_operation_id}:{existence.id}:entrada")
    outgoing = ScmMovimientoInventario(
        saldo_id=origin_balance.id, tipo="TRASLADO_SALIDA", cantidad_delta=-quantity,
        saldo_fisico_resultante=origin_balance.cantidad_fisica,
        motivo="Transferencia de custodia", referencia_tipo="TRANSFERENCIA_INVENTARIO",
        referencia_id=reference, actor_id=actor.id, operation_id=exit_id,
    )
    incoming = ScmMovimientoInventario(
        saldo_id=destination_balance.id, tipo="TRASLADO_ENTRADA", cantidad_delta=quantity,
        saldo_fisico_resultante=destination_balance.cantidad_fisica,
        motivo="Transferencia de custodia", referencia_tipo="TRANSFERENCIA_INVENTARIO",
        referencia_id=reference, actor_id=actor.id, operation_id=entry_id,
    )
    session.add_all([outgoing, incoming])
    session.flush()
    existence.saldo_id = destination_balance.id
    existence.ubicacion_id = destination.id
    existence.estado_logistico = logistic_state
    existence.version += 1
    return outgoing, incoming


def confirm_operation_session(session, *, actor_id, session_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_MOVILIZAR")
    item = session.scalar(select(ScmSesionOperacionAlmacen).where(ScmSesionOperacionAlmacen.id == session_id).with_for_update())
    if item is None or item.actor_id != actor.id:
        raise ScmServiceError("WAREHOUSE_SESSION_NOT_FOUND", "La sesion no existe.", status_code=404)
    command = {"session_id": str(item.id), "version": data.get("version"), "custodio_id": data.get("custodio_id") or actor.id}
    operation, replay = _reserve(session, operation_id, "POST /operaciones-almacen/sesiones/{id}/confirmar", actor, command)
    if replay is not None:
        return replay
    if expected_version(data.get("version")) != item.version:
        raise ScmServiceError("VERSION_CONFLICT", "La sesion cambio.", status_code=409)
    if item.estado != "LISTA" or not item.items:
        raise ScmServiceError("WAREHOUSE_SESSION_NOT_READY", "Escanea al menos una unidad valida.", status_code=409)
    transfer = ScmTransferenciaInventario(
        codigo=f"TRF-{str(item.id).split('-')[0].upper()}", sesion_id=item.id,
        origen_ubicacion_id=item.origen_ubicacion_id,
        destino_ubicacion_id=item.destino_ubicacion_id,
        modalidad=item.modalidad, estado="CERRADA" if item.modalidad == "PICKUP" else "EN_TRANSITO",
        custodio_id=command["custodio_id"], actor_id=actor.id,
        operation_id=operation.operation_id,
    )
    session.add(transfer)
    session.flush()
    destination = item.destino if item.modalidad == "PICKUP" else _transit_location(session)
    for candidate in item.items:
        existence = session.scalar(select(ScmExistenciaManga).where(ScmExistenciaManga.id == candidate.existencia_manga_id).with_for_update())
        if existence.ubicacion_id != item.origen_ubicacion_id:
            raise ScmServiceError("LOGISTIC_UNIT_COMPETING_TRANSFER", "La unidad ya fue movida.", status_code=409)
        outgoing, incoming = _move_existence(
            session, existence=existence, destination=destination,
            root_operation_id=operation.operation_id, actor=actor,
            reference=str(transfer.id),
            logistic_state=(
                "EN_STAGING_ARMADO" if item.modalidad == "PICKUP"
                else "EN_TRANSITO_RETORNO" if item.tipo == "RETORNO"
                else "EN_TRANSITO_PRODUCCION"
            ),
        )
        transfer.items.append(ScmTransferenciaItem(
            existencia_manga_id=existence.id, cantidad=candidate.cantidad_snapshot,
            movimiento_salida_id=outgoing.id,
            movimiento_transito_id=incoming.id if item.modalidad == "ENTREGA" else None,
            movimiento_entrada_id=incoming.id if item.modalidad == "PICKUP" else None,
        ))
    item.estado = "CONFIRMADA"
    item.version += 1
    session.flush()
    payload = _transfer_payload(transfer)
    session.add(ScmEvento(
        aggregate_type="TRANSFERENCIA_INVENTARIO", aggregate_id=str(transfer.id),
        tipo="PICKUP_CONFIRMADO" if item.modalidad == "PICKUP" else "TRANSFERENCIA_DESPACHADA",
        actor_id=actor.id, actor_snapshot=actor_snapshot(actor), after_json=payload,
        operation_id=operation.operation_id,
    ))
    return _complete(session, operation, payload, 201)


def receive_transfer(session, *, actor_id, transfer_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_MOVILIZAR")
    transfer = session.scalar(select(ScmTransferenciaInventario).where(
        ScmTransferenciaInventario.id == transfer_id
    ).with_for_update())
    if transfer is None:
        raise ScmServiceError("TRANSFER_NOT_FOUND", "La transferencia no existe.", status_code=404)
    _scope_location(session, actor, transfer.destino_ubicacion_id)
    command = {
        "transfer_id": str(transfer.id), "version": data.get("version"),
        "existencia_ids": sorted(str(value) for value in (data.get("existencia_ids") or [])),
    }
    operation, replay = _reserve(session, operation_id, "POST /transferencias/{id}/recibir", actor, command)
    if replay is not None:
        return replay
    if expected_version(data.get("version")) != transfer.version:
        raise ScmServiceError("VERSION_CONFLICT", "La transferencia cambio.", status_code=409)
    if transfer.estado != "EN_TRANSITO":
        raise ScmServiceError("TRANSFER_NOT_IN_TRANSIT", "La transferencia no esta en transito.", status_code=409)
    selected = set(command["existencia_ids"])
    if not selected:
        selected = {str(item.existencia_manga_id) for item in transfer.items}
    unknown = selected - {str(item.existencia_manga_id) for item in transfer.items}
    if unknown:
        raise ScmServiceError("TRANSFER_ITEM_OUT_OF_SCOPE", "La unidad no pertenece a la transferencia.", status_code=404)
    received = 0
    for child in transfer.items:
        if str(child.existencia_manga_id) not in selected:
            continue
        existence = session.scalar(select(ScmExistenciaManga).where(
            ScmExistenciaManga.id == child.existencia_manga_id
        ).with_for_update())
        outgoing, incoming = _move_existence(
            session, existence=existence, destination=transfer.destino,
            root_operation_id=operation.operation_id, actor=actor,
            reference=str(transfer.id), logistic_state="RECIBIDA_ALMACEN",
        )
        child.movimiento_entrada_id = incoming.id
        received += 1
    missing = len(transfer.items) - received
    transfer.estado = "INCIDENCIA" if missing else "CERRADA"
    transfer.version += 1
    if missing:
        transfer.incidencia_json = {
            "tipo": "DIFERENCIA_RECEPCION", "esperadas": len(transfer.items),
            "recibidas": received, "faltantes": missing,
        }
        upsert_operational_alert(
            session, rule_code="TRANSFERENCIA_DIFERENCIA",
            aggregate_type="TRANSFERENCIA_INVENTARIO", aggregate_id=transfer.id,
            condition_key=f"faltantes:{missing}",
            summary=f"{transfer.codigo} tiene {missing} unidad(es) sin recibir",
            detail=transfer.incidencia_json, actor_id=actor.id,
        )
    session.flush()
    payload = _transfer_payload(transfer)
    session.add(ScmEvento(
        aggregate_type="TRANSFERENCIA_INVENTARIO", aggregate_id=str(transfer.id),
        tipo="TRANSFERENCIA_RECIBIDA" if not missing else "TRANSFERENCIA_CON_DIFERENCIA",
        actor_id=actor.id, actor_snapshot=actor_snapshot(actor),
        after_json=payload, operation_id=operation.operation_id,
    ))
    return _complete(session, operation, payload)


def start_transfer_return(session, *, actor_id, transfer_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="INVENTARIO_MOVILIZAR")
    transfer = session.scalar(select(ScmTransferenciaInventario).where(
        ScmTransferenciaInventario.id == transfer_id
    ).with_for_update())
    if transfer is None or transfer.estado not in {"CERRADA", "INCIDENCIA"}:
        raise ScmServiceError("TRANSFER_NOT_RETURNABLE", "La transferencia no admite retorno.", status_code=409)
    scope = warehouse_scope(session, actor_id=actor.id)
    allowed = scope["transversal"] or transfer.custodio_id == actor.id
    if scope["configured"] and transfer.origen.almacen_id in scope["warehouse_ids"]:
        allowed = True
    if not allowed:
        raise ScmServiceError("TRANSFER_NOT_FOUND", "La transferencia no existe.", status_code=404)
    selected = {str(value) for value in (data.get("existencia_ids") or [])}
    if not selected:
        selected = {str(item.existencia_manga_id) for item in transfer.items}
    available = {str(item.existencia_manga_id): item for item in transfer.items}
    if selected - set(available):
        raise ScmServiceError("TRANSFER_ITEM_OUT_OF_SCOPE", "La unidad no pertenece a la transferencia.", status_code=404)
    command = {"transfer_id": str(transfer.id), "existencia_ids": sorted(selected)}
    operation, replay = _reserve(
        session, operation_id, "POST /transferencias/{id}/retorno", actor, command,
    )
    if replay is not None:
        return replay
    return_session = ScmSesionOperacionAlmacen(
        tipo="RETORNO", modalidad="ENTREGA",
        origen_ubicacion_id=transfer.destino_ubicacion_id,
        destino_ubicacion_id=transfer.origen_ubicacion_id,
        contexto_json={"transferencia_origen_id": str(transfer.id)},
        actor_id=actor.id, estado="LISTA",
    )
    session.add(return_session)
    session.flush()
    for order, existence_id in enumerate(sorted(selected), start=1):
        transfer_item = available[existence_id]
        existence = session.get(ScmExistenciaManga, transfer_item.existencia_manga_id)
        if existence is None or existence.ubicacion_id != transfer.destino_ubicacion_id:
            raise ScmServiceError(
                "LOGISTIC_UNIT_NOT_RETURNABLE",
                "La unidad ya no esta bajo la custodia de destino.", status_code=409,
            )
        existence.estado_logistico = "PENDIENTE_RETORNO"
        existence.version += 1
        return_session.items.append(ScmSesionOperacionItem(
            existencia_manga_id=existence.id, codigo_escaneado=existence.manga.codigo,
            cantidad_snapshot=existence.cantidad_fisica, orden=order,
        ))
    session.flush()
    payload = _session_payload(return_session)
    session.add(ScmEvento(
        aggregate_type="SESION_OPERACION_ALMACEN", aggregate_id=str(return_session.id),
        tipo="RETORNO_PREPARADO", actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor), after_json=payload,
        operation_id=operation.operation_id,
    ))
    return _complete(session, operation, payload, 201)


def list_transfers(session, *, actor_id, limit=100):
    actor = load_actor(session, actor_id, capability="INVENTARIO_VER")
    scope = warehouse_scope(session, actor_id=actor.id)
    query = select(ScmTransferenciaInventario).order_by(ScmTransferenciaInventario.created_at.desc()).limit(min(max(int(limit), 1), 100))
    if scope["configured"] and not scope["transversal"]:
        allowed_locations = select(ScmUbicacionInventario.id).where(ScmUbicacionInventario.almacen_id.in_(scope["warehouse_ids"]))
        query = query.where(
            (ScmTransferenciaInventario.origen_ubicacion_id.in_(allowed_locations)) |
            (ScmTransferenciaInventario.destino_ubicacion_id.in_(allowed_locations)) |
            (ScmTransferenciaInventario.custodio_id == actor.id)
        )
    return {"items": [_transfer_payload(item) for item in session.scalars(query).all()]}


def inventory_summary(session, *, actor_id):
    actor = load_actor(session, actor_id, capability="INVENTARIO_VER")
    scope = warehouse_scope(session, actor_id=actor.id)
    query = select(
        ScmUbicacionInventario.almacen_id,
        func.count(ScmSaldoInventario.id),
        func.sum(ScmSaldoInventario.cantidad_fisica),
        func.sum(ScmSaldoInventario.cantidad_reservada),
        func.sum(ScmSaldoInventario.cantidad_no_disponible),
    ).join(
        ScmSaldoInventario,
        ScmSaldoInventario.ubicacion_id == ScmUbicacionInventario.id,
    ).join(
        ScmArticulo,
        ScmArticulo.id == ScmSaldoInventario.articulo_scm_id,
    ).group_by(ScmUbicacionInventario.almacen_id)
    if scope["configured"] and not scope["transversal"]:
        article_scope = [and_(
            ScmUbicacionInventario.almacen_id == warehouse_id,
            ScmArticulo.clase.in_(classes),
        ) for warehouse_id, classes in scope["classes"].items() if classes]
        query = query.where(or_(*article_scope) if article_scope else false())
    rows = session.execute(query).all()
    material_query = select(
        ScmUbicacionInventario.almacen_id,
        func.count(ScmSaldoMaterialInventario.id),
        func.sum(ScmSaldoMaterialInventario.cantidad_fisica_kg),
        func.sum(ScmSaldoMaterialInventario.cantidad_reservada_kg),
        func.sum(ScmSaldoMaterialInventario.cantidad_no_disponible_kg),
    ).join(
        ScmSaldoMaterialInventario,
        ScmSaldoMaterialInventario.ubicacion_id == ScmUbicacionInventario.id,
    ).join(
        ScmMaterial,
        ScmMaterial.id == ScmSaldoMaterialInventario.material_id,
    ).group_by(ScmUbicacionInventario.almacen_id)
    if scope["configured"] and not scope["transversal"]:
        material_scope = [and_(
            ScmUbicacionInventario.almacen_id == warehouse_id,
            ScmMaterial.clase.in_(classes),
        ) for warehouse_id, classes in scope["classes"].items() if classes]
        material_query = material_query.where(
            or_(*material_scope) if material_scope else false()
        )
    material_rows = session.execute(material_query).all()
    return {
        "as_of": func.now(),
        "items": [{
            "almacen_id": str(row[0]) if row[0] else None,
            "posiciones": row[1], "unidad": "UN",
            "fisico": format(row[2] or 0, "f"),
            "reservado": format(row[3] or 0, "f"),
            "no_disponible": format(row[4] or 0, "f"),
        } for row in rows],
        "materiales": [{
            "almacen_id": str(row[0]) if row[0] else None,
            "posiciones": row[1], "unidad": "KG",
            "fisico": format(row[2] or 0, "f"),
            "reservado": format(row[3] or 0, "f"),
            "no_disponible": format(row[4] or 0, "f"),
        } for row in material_rows],
    }
