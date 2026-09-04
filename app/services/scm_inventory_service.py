"""Servicios del Kardex normalizado por articulo SCM."""

import base64
import copy
import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.models.scm_articulos import ScmArticulo
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_catalogos import ScmMaterial
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
from app.services.scm_warehouse_scope_service import allowed_location_ids


INVENTORY_LEDGERS = {
    "MATERIALES": {"kind": "material", "classes": ("MATERIA_PRIMA", "COLORANTE")},
    "PIEZAS_WIP": {"kind": "article", "classes": ("PIEZA_COLOR", "SUBENSAMBLE_WIP")},
    "PRODUCTO_TERMINADO": {"kind": "article", "classes": ("PRODUCTO_TERMINADO",)},
}
INVENTORY_SORTS = {
    "CODIGO", "NOMBRE", "FISICO_DESC", "LIBRE_DESC", "ACTUALIZADO",
}
INVENTORY_STOCK_FILTERS = {
    "TODOS", "CON_EXISTENCIA", "LIBRE", "RESERVADO", "NO_DISPONIBLE",
}


def _page_limit(value):
    try:
        parsed = int(value or 25)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_INVENTORY_LIMIT",
            "limite debe ser un entero entre 1 y 100.",
            status_code=400,
        ) from error
    if parsed < 1 or parsed > 100:
        raise ScmServiceError(
            "INVALID_INVENTORY_LIMIT",
            "limite debe estar entre 1 y 100.",
            status_code=400,
        )
    return parsed


def _encode_inventory_cursor(*, ledger, sort, value, row_id):
    raw = json.dumps({
        "v": 1, "ledger": ledger, "sort": sort,
        "value": str(value), "id": str(row_id),
    }, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_inventory_cursor(value, *, ledger, sort):
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if (
            payload.get("v") != 1
            or payload.get("ledger") != ledger
            or payload.get("sort") != sort
        ):
            raise ValueError("cursor scope mismatch")
        if sort in {"FISICO_DESC", "LIBRE_DESC"}:
            Decimal(payload["value"])
        elif sort == "ACTUALIZADO":
            datetime.fromisoformat(payload["value"])
        return {"value": payload["value"], "id": uuid.UUID(payload["id"])}
    except (
        InvalidOperation, KeyError, TypeError, ValueError, json.JSONDecodeError,
    ) as error:
        raise ScmServiceError(
            "INVALID_INVENTORY_CURSOR",
            "El cursor no corresponde a este Kardex y orden.",
            status_code=400,
        ) from error


def _search_pattern(value):
    normalized = str(value or "").strip()
    if not normalized:
        return None
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _scope_condition(scope, class_column):
    if not scope["configured"] or scope["transversal"]:
        return None
    conditions = []
    for warehouse_id, classes in scope["classes"].items():
        if classes:
            conditions.append(and_(
                ScmUbicacionInventario.almacen_id == warehouse_id,
                class_column.in_(classes),
            ))
    return or_(*conditions) if conditions else false()


def _availability_condition(stock_filter, physical, reserved, unavailable):
    if stock_filter == "TODOS":
        return None
    if stock_filter == "CON_EXISTENCIA":
        return physical > 0
    if stock_filter == "LIBRE":
        return physical - reserved - unavailable > 0
    if stock_filter == "RESERVADO":
        return reserved > 0
    return unavailable > 0


def _cursor_condition(cursor, *, sort, primary, row_id):
    if cursor is None:
        return None
    raw = cursor["value"]
    if sort in {"FISICO_DESC", "LIBRE_DESC"}:
        value = Decimal(raw)
        return or_(primary < value, and_(primary == value, row_id > cursor["id"]))
    if sort == "ACTUALIZADO":
        value = datetime.fromisoformat(raw)
        return or_(primary < value, and_(primary == value, row_id > cursor["id"]))
    return or_(primary > raw, and_(primary == raw, row_id > cursor["id"]))


def _inventory_order(sort, *, code, name, physical, free, updated, row_id):
    if sort == "NOMBRE":
        return name, (name.asc(), row_id.asc())
    if sort == "FISICO_DESC":
        return physical, (physical.desc(), row_id.asc())
    if sort == "LIBRE_DESC":
        return free, (free.desc(), row_id.asc())
    if sort == "ACTUALIZADO":
        return updated, (updated.desc(), row_id.asc())
    return code, (code.asc(), row_id.asc())


def _row_quantity(value):
    return format(Decimal(value).quantize(Decimal("0.001")), "f")


def _article_explorer(session, *, ledger, classes, scope, query, location, stock_filter, sort, limit, cursor):
    free = (
        ScmSaldoInventario.cantidad_fisica
        - ScmSaldoInventario.cantidad_reservada
        - ScmSaldoInventario.cantidad_no_disponible
    )
    primary, ordering = _inventory_order(
        sort,
        code=ScmArticulo.codigo,
        name=ScmArticulo.nombre,
        physical=ScmSaldoInventario.cantidad_fisica,
        free=free,
        updated=ScmSaldoInventario.updated_at,
        row_id=ScmSaldoInventario.id,
    )
    conditions = [ScmArticulo.clase.in_(classes)]
    scoped = _scope_condition(scope, ScmArticulo.clase)
    if scoped is not None:
        conditions.append(scoped)
    pattern = _search_pattern(query)
    if pattern:
        conditions.append(or_(
            ScmArticulo.codigo.ilike(pattern, escape="\\"),
            ScmArticulo.nombre.ilike(pattern, escape="\\"),
            ScmUbicacionInventario.codigo.ilike(pattern, escape="\\"),
            ScmUbicacionInventario.nombre.ilike(pattern, escape="\\"),
        ))
    if location:
        conditions.append(ScmUbicacionInventario.codigo == location)
    available = _availability_condition(
        stock_filter,
        ScmSaldoInventario.cantidad_fisica,
        ScmSaldoInventario.cantidad_reservada,
        ScmSaldoInventario.cantidad_no_disponible,
    )
    if available is not None:
        conditions.append(available)
    page_after = _cursor_condition(
        cursor, sort=sort, primary=primary, row_id=ScmSaldoInventario.id,
    )
    page_conditions = conditions + ([page_after] if page_after is not None else [])
    columns = (
        ScmSaldoInventario.id.label("id"),
        ScmSaldoInventario.articulo_scm_id.label("article_id"),
        ScmArticulo.codigo.label("code"),
        ScmArticulo.nombre.label("name"),
        ScmArticulo.clase.label("class_name"),
        ScmArticulo.unidad_base.label("unit"),
        ScmUbicacionInventario.id.label("location_id"),
        ScmUbicacionInventario.codigo.label("location_code"),
        ScmUbicacionInventario.nombre.label("location_name"),
        ScmSaldoInventario.cantidad_fisica.label("physical"),
        ScmSaldoInventario.cantidad_reservada.label("reserved"),
        ScmSaldoInventario.cantidad_no_disponible.label("unavailable"),
        free.label("free"),
        ScmSaldoInventario.version.label("version"),
        ScmSaldoInventario.updated_at.label("updated_at"),
    )
    base_from = (
        ScmSaldoInventario.__table__
        .join(ScmArticulo, ScmArticulo.id == ScmSaldoInventario.articulo_scm_id)
        .join(ScmUbicacionInventario, ScmUbicacionInventario.id == ScmSaldoInventario.ubicacion_id)
    )
    rows = session.execute(
        select(*columns).select_from(base_from)
        .where(*page_conditions).order_by(*ordering).limit(limit + 1)
    ).mappings().all()
    total = session.scalar(
        select(func.count()).select_from(base_from).where(*conditions)
    ) or 0
    visible = rows[:limit]
    items = [{
        "id": str(row["id"]),
        "articulo_scm_id": row["article_id"],
        "articulo": {
            "codigo": row["code"], "nombre": row["name"],
            "clase": row["class_name"], "unidad": row["unit"],
        },
        "ubicacion": {
            "id": row["location_id"], "codigo": row["location_code"],
            "nombre": row["location_name"],
        },
        "cantidad_fisica": _row_quantity(row["physical"]),
        "cantidad_reservada": _row_quantity(row["reserved"]),
        "cantidad_no_disponible": _row_quantity(row["unavailable"]),
        "cantidad_libre": _row_quantity(row["free"]),
        "version": row["version"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    } for row in visible]
    next_cursor = None
    if len(rows) > limit and visible:
        last = visible[-1]
        cursor_value = {
            "CODIGO": last["code"], "NOMBRE": last["name"],
            "FISICO_DESC": last["physical"], "LIBRE_DESC": last["free"],
            "ACTUALIZADO": last["updated_at"].isoformat(),
        }[sort]
        next_cursor = _encode_inventory_cursor(
            ledger=ledger, sort=sort, value=cursor_value, row_id=last["id"],
        )
    return items, int(total), next_cursor


def _material_explorer(session, *, ledger, classes, scope, query, location, stock_filter, sort, limit, cursor):
    free = (
        ScmSaldoMaterialInventario.cantidad_fisica_kg
        - ScmSaldoMaterialInventario.cantidad_reservada_kg
        - ScmSaldoMaterialInventario.cantidad_no_disponible_kg
    )
    primary, ordering = _inventory_order(
        sort,
        code=ScmMaterial.codigo,
        name=ScmMaterial.nombre,
        physical=ScmSaldoMaterialInventario.cantidad_fisica_kg,
        free=free,
        updated=ScmSaldoMaterialInventario.updated_at,
        row_id=ScmSaldoMaterialInventario.id,
    )
    conditions = [ScmMaterial.clase.in_(classes)]
    scoped = _scope_condition(scope, ScmMaterial.clase)
    if scoped is not None:
        conditions.append(scoped)
    pattern = _search_pattern(query)
    if pattern:
        conditions.append(or_(
            ScmMaterial.codigo.ilike(pattern, escape="\\"),
            ScmMaterial.nombre.ilike(pattern, escape="\\"),
            ScmUbicacionInventario.codigo.ilike(pattern, escape="\\"),
            ScmUbicacionInventario.nombre.ilike(pattern, escape="\\"),
        ))
    if location:
        conditions.append(ScmUbicacionInventario.codigo == location)
    available = _availability_condition(
        stock_filter,
        ScmSaldoMaterialInventario.cantidad_fisica_kg,
        ScmSaldoMaterialInventario.cantidad_reservada_kg,
        ScmSaldoMaterialInventario.cantidad_no_disponible_kg,
    )
    if available is not None:
        conditions.append(available)
    page_after = _cursor_condition(
        cursor, sort=sort, primary=primary,
        row_id=ScmSaldoMaterialInventario.id,
    )
    page_conditions = conditions + ([page_after] if page_after is not None else [])
    columns = (
        ScmSaldoMaterialInventario.id.label("id"),
        ScmSaldoMaterialInventario.material_id.label("article_id"),
        ScmMaterial.codigo.label("code"), ScmMaterial.nombre.label("name"),
        ScmMaterial.clase.label("class_name"),
        ScmUbicacionInventario.id.label("location_id"),
        ScmUbicacionInventario.codigo.label("location_code"),
        ScmUbicacionInventario.nombre.label("location_name"),
        ScmSaldoMaterialInventario.cantidad_fisica_kg.label("physical"),
        ScmSaldoMaterialInventario.cantidad_reservada_kg.label("reserved"),
        ScmSaldoMaterialInventario.cantidad_no_disponible_kg.label("unavailable"),
        free.label("free"), ScmSaldoMaterialInventario.version.label("version"),
        ScmSaldoMaterialInventario.updated_at.label("updated_at"),
    )
    base_from = (
        ScmSaldoMaterialInventario.__table__
        .join(ScmMaterial, ScmMaterial.id == ScmSaldoMaterialInventario.material_id)
        .join(ScmUbicacionInventario, ScmUbicacionInventario.id == ScmSaldoMaterialInventario.ubicacion_id)
    )
    rows = session.execute(
        select(*columns).select_from(base_from)
        .where(*page_conditions).order_by(*ordering).limit(limit + 1)
    ).mappings().all()
    total = session.scalar(
        select(func.count()).select_from(base_from).where(*conditions)
    ) or 0
    visible = rows[:limit]
    items = [{
        "id": str(row["id"]), "material_scm_id": row["article_id"],
        "articulo": {
            "codigo": row["code"], "nombre": row["name"],
            "clase": row["class_name"], "unidad": "KG",
        },
        "ubicacion": {
            "id": row["location_id"], "codigo": row["location_code"],
            "nombre": row["location_name"],
        },
        "cantidad_fisica": _row_quantity(row["physical"]),
        "cantidad_reservada": _row_quantity(row["reserved"]),
        "cantidad_no_disponible": _row_quantity(row["unavailable"]),
        "cantidad_libre": _row_quantity(row["free"]),
        "version": row["version"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    } for row in visible]
    next_cursor = None
    if len(rows) > limit and visible:
        last = visible[-1]
        cursor_value = {
            "CODIGO": last["code"], "NOMBRE": last["name"],
            "FISICO_DESC": last["physical"], "LIBRE_DESC": last["free"],
            "ACTUALIZADO": last["updated_at"].isoformat(),
        }[sort]
        next_cursor = _encode_inventory_cursor(
            ledger=ledger, sort=sort, value=cursor_value, row_id=last["id"],
        )
    return items, int(total), next_cursor


def explore_inventory_balances(
    session, *, actor_id, ledger, query=None, location=None,
    stock_filter="TODOS", sort="CODIGO", limit=25, cursor=None,
):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    ledger = str(ledger or "").strip().upper()
    if ledger not in INVENTORY_LEDGERS:
        raise ScmServiceError(
            "INVALID_INVENTORY_LEDGER",
            "kardex debe ser MATERIALES, PIEZAS_WIP o PRODUCTO_TERMINADO.",
            status_code=400,
        )
    sort = str(sort or "CODIGO").strip().upper()
    if sort not in INVENTORY_SORTS:
        raise ScmServiceError(
            "INVALID_INVENTORY_SORT", "ordenar no es valido.", status_code=400,
        )
    stock_filter = str(stock_filter or "TODOS").strip().upper()
    if stock_filter not in INVENTORY_STOCK_FILTERS:
        raise ScmServiceError(
            "INVALID_INVENTORY_FILTER",
            "disponibilidad no es valida.", status_code=400,
        )
    limit = _page_limit(limit)
    decoded_cursor = _decode_inventory_cursor(cursor, ledger=ledger, sort=sort)
    _, scope = allowed_location_ids(session, actor_id=actor_id)
    definition = INVENTORY_LEDGERS[ledger]
    explorer = _material_explorer if definition["kind"] == "material" else _article_explorer
    items, total, next_cursor = explorer(
        session, ledger=ledger, classes=definition["classes"], scope=scope,
        query=query, location=str(location or "").strip().upper() or None,
        stock_filter=stock_filter, sort=sort, limit=limit,
        cursor=decoded_cursor,
    )
    return {
        "items": items,
        "page": {
            "next_cursor": next_cursor,
            "limit": limit,
            "has_more": next_cursor is not None,
            "total": total,
        },
        "filters": {
            "kardex": ledger, "q": str(query or "").strip(),
            "ubicacion": str(location or "").strip().upper() or None,
            "disponibilidad": stock_filter, "ordenar": sort,
        },
    }


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

    def replay_or_conflict(existing):
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

    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        return replay_or_conflict(existing)
    operation = ScmOperacion(
        operation_id=operation_id,
        endpoint=endpoint,
        actor_id=actor.id,
        request_sha256=request_hash,
    )
    try:
        # El savepoint mantiene utilizable la transaccion del perdedor cuando
        # dos solicitudes intentan reservar la misma clave a la vez. En
        # PostgreSQL el INSERT espera al ganador y el conflicto se resuelve
        # aqui como replay, no como un IntegrityError expuesto por la API.
        with session.begin_nested():
            session.add(operation)
            session.flush()
    except IntegrityError as error:
        existing = session.get(
            ScmOperacion,
            operation_id,
            populate_existing=True,
        )
        if existing is None:
            raise error
        return replay_or_conflict(existing)
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
    location_ids, scope = allowed_location_ids(session, actor_id=actor_id)
    article_query = (
        select(ScmSaldoInventario)
        .join(ScmSaldoInventario.articulo)
        .join(ScmSaldoInventario.ubicacion)
        .order_by(ScmArticulo.codigo, ScmUbicacionInventario.codigo)
    )
    material_query = (
        select(ScmSaldoMaterialInventario)
        .join(ScmSaldoMaterialInventario.material)
        .join(ScmSaldoMaterialInventario.ubicacion)
        .order_by(ScmSaldoMaterialInventario.material_id, ScmUbicacionInventario.codigo)
    )
    if location_ids is not None:
        article_query = article_query.where(ScmSaldoInventario.ubicacion_id.in_(location_ids))
        material_query = material_query.where(ScmSaldoMaterialInventario.ubicacion_id.in_(location_ids))
    items = session.scalars(article_query).all()
    material_items = session.scalars(material_query).all()
    if scope["configured"] and not scope["transversal"]:
        allowed_by_warehouse = scope["classes"]
        items = [
            item for item in items
            if item.articulo.clase in allowed_by_warehouse.get(item.ubicacion.almacen_id, set())
        ]
        material_items = [
            item for item in material_items
            if item.material.clase in allowed_by_warehouse.get(item.ubicacion.almacen_id, set())
        ]
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
    location_ids, scope = allowed_location_ids(session, actor_id=actor_id)
    safe_limit = min(max(int(limit or 100), 1), 500)
    article_query = (
        select(ScmMovimientoInventario)
        .join(ScmMovimientoInventario.saldo)
        .order_by(ScmMovimientoInventario.created_at.desc())
        .limit(safe_limit)
    )
    material_query = (
        select(ScmMovimientoMaterialInventario)
        .join(ScmMovimientoMaterialInventario.saldo)
        .order_by(ScmMovimientoMaterialInventario.created_at.desc())
        .limit(safe_limit)
    )
    if location_ids is not None:
        article_query = article_query.where(ScmSaldoInventario.ubicacion_id.in_(location_ids))
        material_query = material_query.where(ScmSaldoMaterialInventario.ubicacion_id.in_(location_ids))
    items = session.scalars(article_query).all()
    material_items = session.scalars(material_query).all()
    if scope["configured"] and not scope["transversal"]:
        items = [item for item in items if item.saldo.articulo.clase in scope["classes"].get(item.saldo.ubicacion.almacen_id, set())]
        material_items = [item for item in material_items if item.saldo.material.clase in scope["classes"].get(item.saldo.ubicacion.almacen_id, set())]
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
