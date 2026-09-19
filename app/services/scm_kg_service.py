"""Boundaries and read helpers for the protected piece/WIP KG subledger."""

import hashlib
import json
import os
from decimal import Decimal

from sqlalchemy import and_, false, func, or_, select

from app.models.scm_articulos import (
    CLASE_PIEZA_COLOR,
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
)
from app.models.scm_inventory import ScmSaldoInventario, ScmUnidadLogisticaInventario, ScmUbicacionInventario
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmSaldoInventarioKg,
)
from app.models.scm_ot import ScmCorreccionPesajeManga
from app.services.scm_service_support import ScmServiceError
from app.services.scm_inventory_service import (
    INVENTORY_SORTS,
    INVENTORY_STOCK_FILTERS,
    _availability_condition,
    _cursor_condition,
    _decode_inventory_cursor,
    _encode_inventory_cursor,
    _inventory_order,
    _page_limit,
    _search_pattern,
)


KG_CLASSES = {CLASE_PIEZA_COLOR, CLASE_SUBENSAMBLE_WIP}


def kg_write_enabled():
    try:
        from flask import current_app
        configured = current_app.config.get("KG_RECEIPT_WRITE_ENABLED")
        if configured is not None:
            return bool(configured)
    except RuntimeError:
        pass
    return os.getenv("KG_RECEIPT_WRITE_ENABLED", "false").strip().lower() == "true"


def is_kg_article(article):
    return article is not None and article.unidad_inventario == "KG"


def assert_kg_article(article):
    if article is None or article.unidad_inventario != "KG":
        raise ScmServiceError("ARTICLE_NOT_KG_ENABLED", "El articulo no esta habilitado para inventario KG.", status_code=409)
    if article.clase not in KG_CLASSES:
        raise ScmServiceError("KG_CLASS_NOT_ALLOWED", "Solo piezas y subensambles WIP pueden usar el sublibro KG.", status_code=409)


def assert_kg_write_enabled():
    if not kg_write_enabled():
        raise ScmServiceError("KG_OPERATION_NOT_ENABLED", "La escritura del sublibro KG esta deshabilitada.", status_code=409)


def _canonical_projection(weighing, projection, correction_id=None):
    return {
        "pesaje_public_id": str(weighing.public_id),
        "correccion_aplicada_public_id": str(correction_id) if correction_id else None,
        "projection": projection,
    }


def source_token(session, weighing, projection):
    correction = session.scalar(
        select(ScmCorreccionPesajeManga)
        .where(ScmCorreccionPesajeManga.pesaje_id == weighing.id, ScmCorreccionPesajeManga.estado == "APLICADA")
        .order_by(ScmCorreccionPesajeManga.id.desc())
    )
    correction_id = correction.public_id if correction else None
    canonical = _canonical_projection(weighing, projection, correction_id)
    digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return {
        "pesaje_public_id": str(weighing.public_id),
        "correccion_aplicada_public_id": str(correction_id) if correction_id else None,
        "projection_sha256": digest,
        "peso_neto_snapshot_kg": format(Decimal(str(projection["peso_fisico_neto_kg"])).quantize(Decimal("0.001")), "f"),
        "pesada_at_snapshot": projection["pesada_at"],
    }


def expected_source_payload(session, weighing, projection):
    token = source_token(session, weighing, projection)
    return {key: token[key] for key in ("pesaje_public_id", "correccion_aplicada_public_id", "projection_sha256")}


def validate_expected_source(session, weighing, projection, expected):
    if not isinstance(expected, dict):
        raise ScmServiceError("EXPECTED_WEIGHING_SOURCE_REQUIRED", "Se requiere expected_weighing_source para recibir un articulo KG.", status_code=409)
    required = {"pesaje_public_id", "correccion_aplicada_public_id", "projection_sha256"}
    if set(expected) != required:
        raise ScmServiceError("EXPECTED_WEIGHING_SOURCE_INVALID", "El token de fuente de pesaje es incompleto.", status_code=422)
    current = expected_source_payload(session, weighing, projection)
    if any(str(expected.get(key)) != str(current.get(key)) for key in required):
        raise ScmServiceError("PESAJE_VERSION_CONFLICT", "La fuente de pesaje cambio; revisa el NET vigente.", status_code=409, details={"expected_weighing_source": current})
    return source_token(session, weighing, projection)


def activate_article_for_kg(session, *, article_id):
    """Internal fixture-only opt-in. No API/admin route calls this helper."""
    article = session.scalar(select(ScmArticulo).where(ScmArticulo.id == article_id).with_for_update())
    if article is None:
        raise ScmServiceError("ARTICLE_NOT_FOUND", "El articulo no existe.", status_code=404)
    if article.unidad_inventario == "KG":
        return article
    if article.clase not in KG_CLASSES:
        raise ScmServiceError("KG_CLASS_NOT_ALLOWED", "Solo piezas y subensambles WIP pueden usar el sublibro KG.", status_code=409)
    legacy_rows = session.scalars(select(ScmSaldoInventario).where(ScmSaldoInventario.articulo_scm_id == article.id).with_for_update()).all()
    if any(Decimal(value or 0) != 0 for legacy in legacy_rows for value in (legacy.cantidad_fisica, legacy.cantidad_reservada, legacy.cantidad_no_disponible)):
        raise ScmServiceError("KG_OPT_IN_LEGACY_BALANCE", "El articulo conserva saldo UN y no puede activarse como KG.", status_code=409)
    from app.models.scm_warehouse import ScmExistenciaManga
    if session.scalar(select(ScmExistenciaManga.id).where(ScmExistenciaManga.articulo_scm_id == article.id, ScmExistenciaManga.estado_logistico != "REVERSADA")) is not None:
        raise ScmServiceError("KG_OPT_IN_LEGACY_EXISTENCE", "El articulo conserva una existencia UN.", status_code=409)
    if session.scalar(select(ScmUnidadLogisticaInventario.id).where(ScmUnidadLogisticaInventario.articulo_scm_id == article.id)) is not None:
        raise ScmServiceError("KG_OPT_IN_LOGISTIC_UNIT", "El articulo conserva una unidad logistica generica.", status_code=409)
    article.unidad_inventario = "KG"
    article.version += 1
    session.flush()
    return article


def deactivate_article_from_kg(session, *, article_id):
    article = session.scalar(select(ScmArticulo).where(ScmArticulo.id == article_id).with_for_update())
    if article is None:
        raise ScmServiceError("ARTICLE_NOT_FOUND", "El articulo no existe.", status_code=404)
    if article.unidad_inventario != "KG":
        return article
    balance_rows = session.scalars(select(ScmSaldoInventarioKg).where(ScmSaldoInventarioKg.articulo_scm_id == article.id).with_for_update()).all()
    if any(Decimal(value or 0) != 0 for balance in balance_rows for value in (balance.cantidad_fisica_kg, balance.cantidad_reservada_kg, balance.cantidad_no_disponible_kg)):
        raise ScmServiceError("KG_DOWNGRADE_NONZERO", "El sublibro KG conserva saldo.", status_code=409)
    if session.scalar(select(ScmExistenciaMangaKg.id).where(ScmExistenciaMangaKg.articulo_scm_id == article.id, ScmExistenciaMangaKg.estado_logistico != "REVERSADA")) is not None:
        raise ScmServiceError("KG_DOWNGRADE_ACTIVE_EXISTENCE", "El sublibro KG conserva una existencia activa.", status_code=409)
    article.unidad_inventario = "UN"
    article.version += 1
    session.flush()
    return article


def kg_balance_payload(balance):
    return balance.to_dict()


def kg_movement_payload(movement):
    payload = movement.to_dict()
    payload.update({
        "articulo_scm_id": movement.saldo.articulo_scm_id,
        "articulo_codigo": movement.saldo.articulo.codigo,
        "articulo_nombre": movement.saldo.articulo.nombre,
        "ubicacion_codigo": movement.saldo.ubicacion.codigo,
    })
    return payload


def _scope_filter(scope, article_column):
    if not scope["configured"] or scope["transversal"]:
        return None
    clauses = [and_(ScmUbicacionInventario.almacen_id == wid, article_column.in_(classes)) for wid, classes in scope["classes"].items() if classes]
    return or_(*clauses) if clauses else false()


def list_kg_balances(session, *, actor_id):
    from app.services.scm_service_support import load_actor
    from app.services.scm_warehouse_scope_service import warehouse_scope
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    scope = warehouse_scope(session, actor_id=actor_id)
    query = select(ScmSaldoInventarioKg).join(ScmSaldoInventarioKg.articulo).join(ScmSaldoInventarioKg.ubicacion).order_by(ScmArticulo.codigo, ScmUbicacionInventario.codigo)
    scoped = _scope_filter(scope, ScmArticulo.clase)
    if scoped is not None:
        query = query.where(scoped)
    return {"items": [item.to_dict() for item in session.scalars(query).all()], "unidad": "KG", "unidad_inventario": "KG"}


def list_kg_movements(session, *, actor_id, limit=100):
    from app.services.scm_service_support import load_actor
    from app.services.scm_warehouse_scope_service import warehouse_scope
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    scope = warehouse_scope(session, actor_id=actor_id)
    query = select(ScmMovimientoInventarioKg).join(ScmMovimientoInventarioKg.saldo).join(ScmSaldoInventarioKg.articulo).join(ScmSaldoInventarioKg.ubicacion).order_by(ScmMovimientoInventarioKg.created_at.desc()).limit(min(max(int(limit or 100), 1), 500))
    scoped = _scope_filter(scope, ScmArticulo.clase)
    if scoped is not None:
        query = query.where(scoped)
    return {"items": [kg_movement_payload(item) for item in session.scalars(query).all()], "unidad": "KG", "unidad_inventario": "KG"}


def explore_kg_balances(session, *, actor_id, query=None, location=None, stock_filter="TODOS", sort="CODIGO", limit=25, cursor=None):
    from app.services.scm_service_support import load_actor
    from app.services.scm_warehouse_scope_service import warehouse_scope
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    sort = str(sort or "CODIGO").strip().upper()
    if sort not in INVENTORY_SORTS:
        raise ScmServiceError("INVALID_INVENTORY_SORT", "ordenar no es valido.", status_code=400)
    stock_filter = str(stock_filter or "TODOS").strip().upper()
    if stock_filter not in INVENTORY_STOCK_FILTERS:
        raise ScmServiceError("INVALID_INVENTORY_FILTER", "disponibilidad no es valida.", status_code=400)
    safe_limit = _page_limit(limit)
    decoded_cursor = _decode_inventory_cursor(cursor, ledger="PIEZAS_WIP_KG", sort=sort)
    scope = warehouse_scope(session, actor_id=actor_id)
    free = ScmSaldoInventarioKg.cantidad_fisica_kg - ScmSaldoInventarioKg.cantidad_reservada_kg - ScmSaldoInventarioKg.cantidad_no_disponible_kg
    conditions = [ScmArticulo.clase.in_(sorted(KG_CLASSES)), ScmArticulo.unidad_inventario == "KG"]
    scoped = _scope_filter(scope, ScmArticulo.clase)
    if scoped is not None: conditions.append(scoped)
    pattern = _search_pattern(query)
    if pattern:
        conditions.append(or_(
            ScmArticulo.codigo.ilike(pattern, escape="\\"),
            ScmArticulo.nombre.ilike(pattern, escape="\\"),
            ScmUbicacionInventario.codigo.ilike(pattern, escape="\\"),
            ScmUbicacionInventario.nombre.ilike(pattern, escape="\\"),
        ))
    if location: conditions.append(ScmUbicacionInventario.codigo == str(location).strip().upper())
    available = _availability_condition(
        stock_filter,
        ScmSaldoInventarioKg.cantidad_fisica_kg,
        ScmSaldoInventarioKg.cantidad_reservada_kg,
        ScmSaldoInventarioKg.cantidad_no_disponible_kg,
    )
    if available is not None:
        conditions.append(available)
    primary, ordering = _inventory_order(
        sort,
        code=ScmArticulo.codigo,
        name=ScmArticulo.nombre,
        physical=ScmSaldoInventarioKg.cantidad_fisica_kg,
        free=free,
        updated=ScmSaldoInventarioKg.updated_at,
        row_id=ScmSaldoInventarioKg.id,
    )
    page_after = _cursor_condition(
        decoded_cursor,
        sort=sort,
        primary=primary,
        row_id=ScmSaldoInventarioKg.id,
    )
    page_conditions = conditions + ([page_after] if page_after is not None else [])
    base_from = (
        ScmSaldoInventarioKg.__table__
        .join(ScmArticulo, ScmArticulo.id == ScmSaldoInventarioKg.articulo_scm_id)
        .join(ScmUbicacionInventario, ScmUbicacionInventario.id == ScmSaldoInventarioKg.ubicacion_id)
    )
    columns = (
        ScmSaldoInventarioKg.id.label("id"),
        ScmSaldoInventarioKg.articulo_scm_id.label("article_id"),
        ScmArticulo.codigo.label("code"), ScmArticulo.nombre.label("name"),
        ScmArticulo.clase.label("class_name"),
        ScmSaldoInventarioKg.ubicacion_id.label("location_id"),
        ScmUbicacionInventario.codigo.label("location_code"),
        ScmUbicacionInventario.nombre.label("location_name"),
        ScmSaldoInventarioKg.cantidad_fisica_kg.label("physical"),
        ScmSaldoInventarioKg.cantidad_reservada_kg.label("reserved"),
        ScmSaldoInventarioKg.cantidad_no_disponible_kg.label("unavailable"),
        ScmSaldoInventarioKg.cantidad_retirada_kg.label("withdrawn"),
        ScmSaldoInventarioKg.atributo_proceso.label("process_attribute"),
        free.label("free"), ScmSaldoInventarioKg.version.label("version"),
        ScmSaldoInventarioKg.updated_at.label("updated_at"),
    )
    rows = session.execute(
        select(*columns).select_from(base_from)
        .where(*page_conditions).order_by(*ordering).limit(safe_limit + 1)
    ).mappings().all()
    total = session.scalar(
        select(func.count()).select_from(base_from).where(*conditions)
    ) or 0
    visible = rows[:safe_limit]
    items = [{
        "id": str(row["id"]), "articulo_scm_id": row["article_id"],
        "articulo": {"codigo": row["code"], "nombre": row["name"], "clase": row["class_name"], "unidad": "KG", "unidad_inventario": "KG"},
        "ubicacion": {"id": row["location_id"], "codigo": row["location_code"], "nombre": row["location_name"]},
        "cantidad_fisica": format(Decimal(row["physical"]).quantize(Decimal("0.001")), "f"),
        "cantidad_reservada": format(Decimal(row["reserved"]).quantize(Decimal("0.001")), "f"),
        "cantidad_no_disponible": format(Decimal(row["unavailable"]).quantize(Decimal("0.001")), "f"),
        "cantidad_retirada": format(Decimal(row["withdrawn"] or 0).quantize(Decimal("0.001")), "f"),
        "cantidad_libre": format(Decimal(row["free"]).quantize(Decimal("0.001")), "f"),
        "cantidad_medida_kg": format(Decimal(row["physical"]).quantize(Decimal("0.001")), "f"),
        "cantidad_disponible_kg": format(Decimal(row["free"]).quantize(Decimal("0.001")), "f"),
        "cantidad_comprometida_kg": format(Decimal(row["reserved"]).quantize(Decimal("0.001")), "f"),
        "atributo_proceso": row["process_attribute"],
        "unidad": "KG", "unidad_inventario": "KG", "version": row["version"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    } for row in visible]
    next_cursor = None
    if len(rows) > safe_limit and visible:
        last = visible[-1]
        value = {
            "CODIGO": last["code"], "NOMBRE": last["name"],
            "FISICO_DESC": last["physical"], "LIBRE_DESC": last["free"],
            "ACTUALIZADO": last["updated_at"].isoformat(),
        }[sort]
        next_cursor = _encode_inventory_cursor(
            ledger="PIEZAS_WIP_KG", sort=sort, value=value, row_id=last["id"],
        )
    return {"items": items, "page": {"next_cursor": next_cursor, "limit": safe_limit, "has_more": next_cursor is not None, "total": int(total)}, "filters": {"kardex": "PIEZAS_WIP", "unidad": "KG", "q": str(query or "").strip(), "ubicacion": str(location or "").strip().upper() or None, "disponibilidad": stock_filter, "ordenar": sort}, "unidad": "KG", "unidad_inventario": "KG"}


def kg_summary(session, *, actor_id):
    from app.services.scm_service_support import load_actor
    from app.services.scm_warehouse_scope_service import warehouse_scope
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    scope = warehouse_scope(session, actor_id=actor_id)
    free = ScmSaldoInventarioKg.cantidad_fisica_kg - ScmSaldoInventarioKg.cantidad_reservada_kg - ScmSaldoInventarioKg.cantidad_no_disponible_kg
    statement = select(ScmUbicacionInventario.almacen_id, func.count(ScmSaldoInventarioKg.id), func.sum(ScmSaldoInventarioKg.cantidad_fisica_kg), func.sum(ScmSaldoInventarioKg.cantidad_reservada_kg), func.sum(ScmSaldoInventarioKg.cantidad_no_disponible_kg), func.sum(free)).join(ScmSaldoInventarioKg, ScmSaldoInventarioKg.ubicacion_id == ScmUbicacionInventario.id).join(ScmArticulo, ScmArticulo.id == ScmSaldoInventarioKg.articulo_scm_id).where(ScmArticulo.unidad_inventario == "KG", ScmArticulo.clase.in_(sorted(KG_CLASSES))).group_by(ScmUbicacionInventario.almacen_id)
    scoped = _scope_filter(scope, ScmArticulo.clase)
    if scoped is not None: statement = statement.where(scoped)
    return [{"almacen_id": str(row[0]) if row[0] else None, "posiciones": row[1], "unidad": "KG", "fisico": format(row[2] or 0, "f"), "reservado": format(row[3] or 0, "f"), "no_disponible": format(row[4] or 0, "f"), "libre": format(row[5] or 0, "f")} for row in session.execute(statement).all()]
