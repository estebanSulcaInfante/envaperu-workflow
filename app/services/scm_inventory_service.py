"""Servicios del Kardex normalizado por articulo SCM."""

import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.models.scm_articulos import ScmArticulo
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_inventory import (
    ScmMovimientoInventario,
    ScmMovimientoMaterialInventario,
    ScmSaldoInventario,
    ScmSaldoMaterialInventario,
    ScmUbicacionInventario,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    load_actor,
    reject_unknown_fields,
    required_text,
)


def _hash(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _reserve_operation(session, operation_id, endpoint, actor, data):
    request_hash = _hash({
        "endpoint": endpoint,
        "actor_id": actor.id,
        "data": data,
    })
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        if (
            existing.endpoint != endpoint
            or existing.request_sha256 != request_hash
        ):
            raise ScmServiceError(
                "IDEMPOTENCY_CONFLICT",
                "La clave idempotente ya fue usada con otra solicitud.",
                status_code=409,
            )
        if existing.response_json is None:
            raise ScmServiceError(
                "IDEMPOTENCY_OPERATION_INCOMPLETE",
                "La operacion previa aun no tiene resultado.",
                status_code=409,
            )
        return None, copy.deepcopy(existing.response_json)
    operation = ScmOperacion(
        operation_id=operation_id,
        endpoint=endpoint,
        actor_id=actor.id,
        request_sha256=request_hash,
    )
    session.add(operation)
    session.flush()
    return operation, None


def _positive_quantity(value):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_INVENTORY_QUANTITY",
            "cantidad debe ser mayor que cero.",
            status_code=422,
        ) from error
    if not parsed.is_finite() or parsed <= 0:
        raise ScmServiceError(
            "INVALID_INVENTORY_QUANTITY",
            "cantidad debe ser mayor que cero.",
            status_code=422,
        )
    return parsed.quantize(Decimal("0.001"))


def _balance_payload(item):
    physical = Decimal(item.cantidad_fisica)
    reserved = Decimal(item.cantidad_reservada)
    unavailable = Decimal(item.cantidad_no_disponible)
    quantity_text = lambda value: format(
        Decimal(value).quantize(Decimal("0.001")),
        "f",
    )
    return {
        "id": str(item.id),
        "articulo_scm_id": item.articulo_scm_id,
        "articulo": {
            "codigo": item.articulo.codigo,
            "nombre": item.articulo.nombre,
            "clase": item.articulo.clase,
            "unidad": item.articulo.unidad_base,
        },
        "ubicacion": {
            "id": item.ubicacion.id,
            "codigo": item.ubicacion.codigo,
            "nombre": item.ubicacion.nombre,
        },
        "cantidad_fisica": quantity_text(physical),
        "cantidad_reservada": quantity_text(reserved),
        "cantidad_no_disponible": quantity_text(unavailable),
        "cantidad_libre": quantity_text(physical - reserved - unavailable),
        "version": item.version,
        "updated_at": (
            item.updated_at.isoformat() if item.updated_at else None
        ),
    }


def list_inventory_balances(session, *, actor_id):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    items = session.scalars(
        select(ScmSaldoInventario)
        .join(ScmSaldoInventario.articulo)
        .join(ScmSaldoInventario.ubicacion)
        .order_by(ScmArticulo.codigo, ScmUbicacionInventario.codigo)
    ).all()
    material_items = session.scalars(
        select(ScmSaldoMaterialInventario)
        .join(ScmSaldoMaterialInventario.material)
        .join(ScmSaldoMaterialInventario.ubicacion)
        .order_by(ScmSaldoMaterialInventario.material_id, ScmUbicacionInventario.codigo)
    ).all()
    return {
        "items": [_balance_payload(item) for item in items],
        "materiales": [_material_balance_payload(item) for item in material_items],
    }


def _material_balance_payload(item):
    physical = Decimal(item.cantidad_fisica_kg)
    reserved = Decimal(item.cantidad_reservada_kg)
    unavailable = Decimal(item.cantidad_no_disponible_kg)
    quantity = lambda value: format(Decimal(value).quantize(Decimal("0.001")), "f")
    return {
        "id": str(item.id),
        "material_scm_id": item.material_id,
        "articulo": {
            "codigo": item.material.codigo,
            "nombre": item.material.nombre,
            "clase": item.material.clase,
            "unidad": "KG",
        },
        "ubicacion": {
            "id": item.ubicacion.id, "codigo": item.ubicacion.codigo,
            "nombre": item.ubicacion.nombre,
        },
        "cantidad_fisica": quantity(physical),
        "cantidad_reservada": quantity(reserved),
        "cantidad_no_disponible": quantity(unavailable),
        "cantidad_libre": quantity(physical - reserved - unavailable),
        "version": item.version,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def list_inventory_movements(session, *, actor_id, limit=100):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    safe_limit = min(max(int(limit or 100), 1), 500)
    items = session.scalars(
        select(ScmMovimientoInventario)
        .order_by(ScmMovimientoInventario.created_at.desc())
        .limit(safe_limit)
    ).all()
    material_items = session.scalars(
        select(ScmMovimientoMaterialInventario)
        .order_by(ScmMovimientoMaterialInventario.created_at.desc())
        .limit(safe_limit)
    ).all()
    return {
        "items": [{
            "id": str(item.id),
            "tipo": item.tipo,
            "articulo_scm_id": item.saldo.articulo_scm_id,
            "articulo_codigo": item.saldo.articulo.codigo,
            "articulo_nombre": item.saldo.articulo.nombre,
            "ubicacion_codigo": item.saldo.ubicacion.codigo,
            "cantidad_delta": format(item.cantidad_delta, "f"),
            "saldo_fisico_resultante": format(
                item.saldo_fisico_resultante,
                "f",
            ),
            "motivo": item.motivo,
            "actor_id": item.actor_id,
            "created_at": (
                item.created_at.isoformat() if item.created_at else None
            ),
        } for item in items] + [{
            "id": str(item.id), "tipo": item.tipo,
            "articulo_scm_id": None,
            "material_scm_id": item.saldo.material_id,
            "articulo_codigo": item.saldo.material.codigo,
            "articulo_nombre": item.saldo.material.nombre,
            "ubicacion_codigo": item.saldo.ubicacion.codigo,
            "cantidad_delta": format(item.cantidad_delta_kg, "f"),
            "saldo_fisico_resultante": format(item.saldo_fisico_resultante_kg, "f"),
            "unidad": "KG", "motivo": item.motivo,
            "actor_id": item.actor_id,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        } for item in material_items],
    }


def register_inventory_movement(
    session,
    *,
    actor_id,
    operation_id,
    data,
):
    reject_unknown_fields(
        data,
        allowed={
            "articulo_scm_id",
            "cantidad",
            "tipo",
            "motivo",
            "ubicacion_codigo",
            "ubicacion_nombre",
        },
    )
    movement_type = str(data.get("tipo") or "SALDO_INICIAL").strip().upper()
    if movement_type == "SALDO_INICIAL":
        raise ScmServiceError(
            "INITIAL_BALANCE_BATCH_REQUIRED",
            "El saldo inicial solo puede aplicarse mediante un lote de apertura aprobado.",
            status_code=409,
        )
    actor = load_actor(session, actor_id, capability="INVENTARIO_AJUSTAR")
    if movement_type not in (
        "AJUSTE_POSITIVO",
        "AJUSTE_NEGATIVO",
    ):
        raise ScmServiceError(
            "INVALID_INVENTORY_MOVEMENT_TYPE",
            "El movimiento manual no esta permitido.",
            status_code=422,
        )
    quantity = _positive_quantity(data.get("cantidad"))
    location_code = str(
        data.get("ubicacion_codigo") or "ALMACEN_GENERAL"
    ).strip().upper()
    reason = required_text(
        data.get("motivo"),
        field="motivo",
        max_length=240,
    )
    command = {
        "articulo_scm_id": data.get("articulo_scm_id"),
        "cantidad": format(quantity, "f"),
        "tipo": movement_type,
        "motivo": reason,
        "ubicacion_codigo": location_code,
        "ubicacion_nombre": str(
            data.get("ubicacion_nombre") or "Almacen general"
        ).strip(),
    }
    operation, replay = _reserve_operation(
        session,
        operation_id,
        "POST /inventario/movimientos",
        actor,
        command,
    )
    if replay is not None:
        return replay
    try:
        article = session.get(ScmArticulo, command["articulo_scm_id"])
        if article is None or not article.activo:
            raise ScmServiceError(
                "ARTICLE_NOT_FOUND",
                "El articulo SCM no existe o esta inactivo.",
                status_code=422,
            )
        location = session.scalar(
            select(ScmUbicacionInventario)
            .where(ScmUbicacionInventario.codigo == location_code)
            .with_for_update()
        )
        if location is None:
            location = ScmUbicacionInventario(
                codigo=location_code,
                nombre=command["ubicacion_nombre"],
            )
            session.add(location)
            session.flush()
        balance = session.scalar(
            select(ScmSaldoInventario)
            .where(
                ScmSaldoInventario.articulo_scm_id == article.id,
                ScmSaldoInventario.ubicacion_id == location.id,
            )
            .with_for_update()
        )
        if balance is None:
            balance = ScmSaldoInventario(
                articulo_scm_id=article.id,
                ubicacion_id=location.id,
            )
            session.add(balance)
            session.flush()
        delta = (
            -quantity if movement_type == "AJUSTE_NEGATIVO" else quantity
        )
        resulting = Decimal(balance.cantidad_fisica) + delta
        if resulting < (
            Decimal(balance.cantidad_reservada)
            + Decimal(balance.cantidad_no_disponible)
        ):
            raise ScmServiceError(
                "INVENTORY_BELOW_RESERVED",
                "El ajuste dejaria menos existencia que la ya reservada.",
                status_code=409,
            )
        balance.cantidad_fisica = resulting
        balance.version += 1
        movement = ScmMovimientoInventario(
            saldo=balance,
            tipo=movement_type,
            cantidad_delta=delta,
            saldo_fisico_resultante=resulting,
            motivo=reason,
            actor_id=actor.id,
            operation_id=operation.operation_id,
        )
        session.add(movement)
        session.flush()
        response = {
            "saldo": _balance_payload(balance),
            "movimiento_id": str(movement.id),
        }
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="INVENTARIO_SCM",
            aggregate_id=str(balance.id),
            tipo=f"INVENTORY_{movement_type}",
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
