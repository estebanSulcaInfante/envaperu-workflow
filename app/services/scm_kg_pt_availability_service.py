"""Read-only KG availability projections and append-only PT manual Kardex.

The service keeps the two ledgers separate. KG rows are read from the KG
production projection supplied by W1 (with a compatibility fallback to the
existing KG balance table); PT manual movements never call a KG/BOM mutation.
"""

import copy
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from uuid import UUID

from flask import current_app
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.producto import PiezaColor, ProductoTerminado
from app.models.scm_articulos import ScmArticulo, ScmArticuloPiezaColor, ScmArticuloProducto
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_estructuras import ScmEstructuraComponente, ScmEstructuraRevision
from app.models.scm_inventory import (
    ScmMovimientoInventario,
    ScmSaldoInventario,
    ScmUbicacionInventario,
)
from app.models.scm_inventory_kg import ScmSaldoInventarioKg, ScmUnidadFisicaKg
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    required_text,
)
from app.services.scm_warehouse_scope_service import allowed_location_ids


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _qty(value):
    return format(Decimal(value or 0).quantize(Decimal("0.001")), "f")


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _reserve(session, operation_id, endpoint, actor, data):
    digest = _hash({"endpoint": endpoint, "actor_id": actor.id, "data": data})
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        if existing.endpoint != endpoint or existing.request_sha256 != digest:
            raise ScmServiceError(
                "IDEMPOTENCY_CONFLICT",
                "La clave idempotente ya fue usada con otra solicitud.",
                status_code=409,
            )
        if existing.response_json is None:
            raise ScmServiceError(
                "IDEMPOTENCY_OPERATION_INCOMPLETE",
                "La operación previa aún no tiene resultado.",
                status_code=409,
            )
        return None, copy.deepcopy(existing.response_json)
    operation = ScmOperacion(
        operation_id=operation_id,
        endpoint=endpoint,
        actor_id=actor.id,
        request_sha256=digest,
    )
    try:
        with session.begin_nested():
            session.add(operation)
            session.flush()
    except IntegrityError:
        existing = session.get(ScmOperacion, operation_id, populate_existing=True)
        if existing is None:
            raise
        if existing.endpoint != endpoint or existing.request_sha256 != digest:
            raise ScmServiceError("IDEMPOTENCY_CONFLICT", "La clave idempotente ya fue usada con otra solicitud.", status_code=409)
        if existing.response_json is None:
            raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La operación previa aún no tiene resultado.", status_code=409)
        return None, copy.deepcopy(existing.response_json)
    return operation, None


def _complete(session, operation, payload, *, status=201):
    operation.response_json = copy.deepcopy(payload)
    operation.estado_http = status
    session.commit()
    return payload


def _model_rows_for_kg(session):
    """Project KG from W1's authoritative saldo and current physical units.

    ``ScmExistenciaMangaKg`` is a receipt/history projection and is deliberately
    never used as available stock.  A saldo is the authority for physical,
    reserved, blocked and retired quantities.  Units only refine the production
    state; historical/reversed/retired units are excluded so a parent and its
    replacement cannot be counted twice.
    """
    balances = session.scalars(select(ScmSaldoInventarioKg)).all()
    output = []
    excluded_logistics = {"RETIRADA_ARMADO", "REVERSADA"}
    for balance in balances:
        units = session.scalars(select(ScmUnidadFisicaKg).where(
            ScmUnidadFisicaKg.saldo_id == balance.id,
            ScmUnidadFisicaKg.estado == "ACTIVA",
            ScmUnidadFisicaKg.estado_logistico.not_in(excluded_logistics),
        )).all()
        state = balance.atributo_proceso
        if state == "MIXTA":
            state = None
        groups = {}
        for unit in units:
            unit_state = unit.atributo_proceso or "PROCESO"
            amount = Decimal(unit.kg_verificados or unit.kg_entregado or 0)
            if amount > 0:
                groups[unit_state] = groups.get(unit_state, Decimal("0")) + amount
        if not groups:
            groups = {state or "PROCESO": Decimal(balance.cantidad_fisica_kg or 0)}
        total_units = sum(groups.values(), Decimal("0"))
        # Keep the W1 saldo authoritative; use unit states only when their
        # current snapshots reconcile exactly with that saldo.
        physical = Decimal(balance.cantidad_fisica_kg or 0)
        # Never invent a split when unit snapshots do not reconcile with the
        # authoritative saldo.  A single MIXTA row is honest and avoids
        # duplicating reserved/blocked quantities across process states.
        if total_units <= 0 or total_units != physical or (
            len(groups) > 1 and (
                Decimal(balance.cantidad_reservada_kg or 0) > 0
                or Decimal(balance.cantidad_no_disponible_kg or 0) > 0
            )
        ):
            groups = {state or "MIXTA": physical}
        for group_state, quantity in groups.items():
            output.append({
                "articulo_scm_id": balance.articulo_scm_id,
                "ubicacion_id": balance.ubicacion_id,
                "cantidad_kg": quantity,
                "reservada_kg": Decimal(balance.cantidad_reservada_kg or 0),
                "bloqueada_kg": Decimal(balance.cantidad_no_disponible_kg or 0),
                "retirada_kg": Decimal(balance.cantidad_retirada_kg or 0) if group_state == next(iter(groups)) else Decimal("0"),
                "estado": group_state,
                "updated_at": balance.updated_at,
                "fuente": ScmSaldoInventarioKg.__tablename__,
            })
    return output


def _availability_state(article, raw_state):
    normalized = str(raw_state or "").upper()
    if normalized == "MIXTA":
        return "MIXTA"
    if normalized in {"EN_PROCESO", "PROCESO", "WIP", "PENDIENTE", "PRODUCCION"}:
        return "EN_PROCESO"
    if normalized in {"TERMINADA", "TERMINADO", "FINAL", "COMPLETA", "DISPONIBLE"}:
        return "TERMINADA"
    return "EN_PROCESO" if article.clase == "SUBENSAMBLE_WIP" else "TERMINADA"


def _scope_allows_article(scope, location, article_class):
    """Apply the assignment's exact article class to each projected row.

    ``allowed_location_ids`` is intentionally a coarse warehouse prefilter.
    A warehouse can contain more than one class, so the row-level check must
    retain the class restriction that was assigned to this actor.
    """
    if not scope.get("configured") or scope.get("transversal"):
        return True
    return article_class in scope.get("classes", {}).get(location.almacen_id, set())


def list_piece_kg_availability(session, *, actor_id, query=None, location=None):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    location_ids, scope = allowed_location_ids(
        session, actor_id=actor_id, article_class={"PIEZA_COLOR", "SUBENSAMBLE_WIP"},
    )
    pattern = str(query or "").strip().lower()
    location_code = str(location or "").strip().upper() or None
    location_query = select(ScmUbicacionInventario)
    if location_code:
        location_query = location_query.where(ScmUbicacionInventario.codigo == location_code)
    if location_ids is not None:
        location_query = location_query.where(ScmUbicacionInventario.id.in_(location_ids or {-1}))
    locations = {item.id: item for item in session.scalars(location_query).all()}
    rows = _model_rows_for_kg(session)
    article_ids = {row["articulo_scm_id"] for row in rows}
    articles = {
        article.id: article for article in session.scalars(
            select(ScmArticulo).where(ScmArticulo.id.in_(article_ids or {-1}))
        ).all()
    }
    grouped = {}
    for row in rows:
        loc = locations.get(row["ubicacion_id"])
        article = articles.get(row["articulo_scm_id"])
        if loc is None or article is None or article.clase not in {"PIEZA_COLOR", "SUBENSAMBLE_WIP"}:
            continue
        if not _scope_allows_article(scope, loc, article.clase):
            continue
        if pattern and pattern not in f"{article.codigo} {article.nombre}".lower():
            continue
        key = (article.id, _availability_state(article, row["estado"]))
        item = grouped.setdefault(key, {
            "articulo": article.to_dict(),
            "estado_produccion": key[1],
            "kg_medidos": Decimal("0"), "kg_comprometidos": Decimal("0"),
            "kg_retirados": Decimal("0"), "kg_bloqueados": Decimal("0"),
            "ubicaciones": [], "fuentes": set(), "actualizado_at": None,
        })
        item["kg_medidos"] += row["cantidad_kg"]
        item["kg_comprometidos"] += row["reservada_kg"]
        item["kg_bloqueados"] += row["bloqueada_kg"]
        item["kg_retirados"] += row.get("retirada_kg", Decimal("0"))
        item["fuentes"].add(row["fuente"])
        item["actualizado_at"] = max(item["actualizado_at"] or row["updated_at"], row["updated_at"] or item["actualizado_at"]) if row["updated_at"] else item["actualizado_at"]
        item["ubicaciones"].append({
            "id": loc.id, "codigo": loc.codigo, "nombre": loc.nombre,
            "kg_medidos": _qty(row["cantidad_kg"]),
            "kg_comprometidos": _qty(row["reservada_kg"]),
            "kg_retirados": _qty(row.get("retirada_kg", Decimal("0"))),
            "kg_disponibles": _qty(max(
                Decimal("0"), row["cantidad_kg"] - row["reservada_kg"]
                - row["bloqueada_kg"]
            )),
        })
    items = []
    for item in sorted(grouped.values(), key=lambda value: (value["articulo"]["codigo"], value["estado_produccion"])):
        available = max(
            Decimal("0"), item["kg_medidos"] - item["kg_comprometidos"]
            - item["kg_bloqueados"]
        )
        items.append({
            "articulo": item["articulo"],
            "estado_produccion": item["estado_produccion"],
            "kg_medidos": _qty(item["kg_medidos"]),
            "kg_comprometidos": _qty(item["kg_comprometidos"]),
            "kg_retirados": _qty(item["kg_retirados"]),
            "kg_bloqueados": _qty(item["kg_bloqueados"]),
            "kg_disponibles": _qty(available),
            "ubicaciones": item["ubicaciones"],
            "fuentes": sorted(item["fuentes"]),
            "actualizado_at": _iso(item["actualizado_at"]),
        })
    return {
        "items": items,
        "total_items": len(items),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "fuente_vigente": sorted({source for item in items for source in item["fuentes"]}),
        "filtros": {"q": str(query or "").strip(), "ubicacion": location_code},
        "politica_piloto": "SIN_CONTROL_CALIDAD_DESDE_PESAJE",
    }


def _piece_kg_by_article(payload):
    result = {}
    for item in payload["items"]:
        result[item["articulo"]["id"]] = result.get(item["articulo"]["id"], Decimal("0")) + Decimal(item["kg_disponibles"])
    return result


def _weight_kg_for_article(session, article):
    if article.pieza_color is None:
        return None
    piece = article.pieza_color.pieza_color
    if piece is None or piece.peso is None or Decimal(str(piece.peso)) <= 0:
        return None
    return Decimal(str(piece.peso)) / Decimal("1000")


def list_pt_availability(session, *, actor_id, query=None, location=None):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    # The PT query filters finished articles.  It must never be reused to
    # filter component KG rows, otherwise a PT code hides its BOM inputs.
    kg_payload = list_piece_kg_availability(session, actor_id=actor_id, query=None, location=location)
    kg_by_article = _piece_kg_by_article(kg_payload)
    location_ids, scope = allowed_location_ids(
        session, actor_id=actor_id, article_class="PRODUCTO_TERMINADO",
    )
    statement = select(ScmSaldoInventario).join(ScmArticulo).where(ScmArticulo.clase == "PRODUCTO_TERMINADO")
    if location_ids is not None:
        statement = statement.where(ScmSaldoInventario.ubicacion_id.in_(location_ids or {-1}))
    if location:
        statement = statement.join(ScmUbicacionInventario).where(ScmUbicacionInventario.codigo == str(location).strip().upper())
    if query:
        pattern = f"%{str(query).strip()}%"
        statement = statement.where(or_(ScmArticulo.codigo.ilike(pattern), ScmArticulo.nombre.ilike(pattern)))
    balances = session.scalars(statement).all()
    totals = {}
    for balance in balances:
        item = totals.setdefault(balance.articulo_scm_id, {
            "articulo": balance.articulo.to_dict(), "saldo_manual_un": Decimal("0"), "ubicaciones": [],
        })
        item["saldo_manual_un"] += Decimal(balance.cantidad_fisica)
        item["ubicaciones"].append({
            "id": balance.ubicacion.id, "codigo": balance.ubicacion.codigo, "nombre": balance.ubicacion.nombre,
            "saldo_manual_un": _qty(balance.cantidad_fisica), "actualizado_at": _iso(balance.updated_at),
        })
    # Include PT catalog entries even when the manual card is empty, without
    # inventing a saldo. A blank pilot must remain visibly blank.
    products = session.scalars(select(ScmArticulo).where(ScmArticulo.clase == "PRODUCTO_TERMINADO").order_by(ScmArticulo.codigo)).all()
    for product in products:
        if query and str(query).strip().lower() not in f"{product.codigo} {product.nombre}".lower():
            continue
        totals.setdefault(product.id, {"articulo": product.to_dict(), "saldo_manual_un": Decimal("0"), "ubicaciones": []})
    items = []
    for item in totals.values():
        product = session.scalar(select(ScmArticulo).where(ScmArticulo.id == item["articulo"]["id"]))
        revision = session.scalar(select(ScmEstructuraRevision).where(
            ScmEstructuraRevision.articulo_resultado_id == product.id,
            ScmEstructuraRevision.estado == "APROBADA",
        ))
        components = []
        potential = None
        potential_reason = None
        limiting_component = None
        if revision is None:
            potential_reason = "SIN_BOM_APROBADA"
        else:
            estimates = []
            estimate_indexes = []
            for component in revision.componentes:
                component_article = component.articulo_componente
                available_kg = kg_by_article.get(component_article.id, Decimal("0"))
                weight_kg = _weight_kg_for_article(session, component_article)
                required_kg = Decimal(component.cantidad) * weight_kg if weight_kg is not None else None
                estimate = (available_kg / required_kg) if required_kg and required_kg > 0 else None
                if estimate is not None:
                    estimates.append(estimate)
                    estimate_indexes.append(len(components))
                else:
                    potential_reason = "SIN_REFERENCIA_PESO"
                shortage = max(
                    Decimal("0"), (required_kg or Decimal("0")) - available_kg
                ) if required_kg is not None else None
                components.append({
                    "articulo": component_article.to_dict(),
                    "cantidad_bom_un": _qty(component.cantidad),
                    "kg_disponibles": _qty(available_kg),
                    "peso_unitario_kg": _qty(weight_kg) if weight_kg is not None else None,
                    "potencial_un_estimado": _qty(estimate.to_integral_value(rounding=ROUND_FLOOR)) if estimate is not None else None,
                    "cobertura_un": _qty(estimate.to_integral_value(rounding=ROUND_FLOOR)) if estimate is not None else None,
                    "faltante_kg": _qty(shortage) if shortage is not None else None,
                    "grupo_stock_compartido": f"articulo:{component_article.id}",
                    "potencial_sumable": False,
                    "es_limitante": False,
                    "naturaleza": "SUBENSAMBLE_WIP" if component_article.clase == "SUBENSAMBLE_WIP" else "PIEZA_COLOR",
                    "estado": "CALCULABLE" if estimate is not None else "NO_CALCULABLE",
                })
            if not potential_reason and estimates:
                potential = min(estimates).to_integral_value(rounding=ROUND_FLOOR)
                limiting_component = estimate_indexes[min(range(len(estimates)), key=estimates.__getitem__)]
                if components:
                    components[limiting_component]["es_limitante"] = True
            elif not potential_reason and not estimates:
                potential_reason = "BOM_SIN_COMPONENTES"
        items.append({
            "pt": item["articulo"], "saldo_manual_un": _qty(item["saldo_manual_un"]),
            "potencial_un_estimado": _qty(potential) if potential is not None else None,
            "potencial_estado": "CALCULABLE" if potential is not None else "NO_CALCULABLE",
            "potencial_motivo": potential_reason,
            "componentes": components,
            "revision_bom": {
                "id": revision.id, "numero": revision.numero_revision,
                "content_hash": revision.content_hash,
            } if revision is not None else None,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "grupo_stock_compartido": f"pt:{item['articulo']['id']}",
            "potencial_sumable": False,
            "ubicaciones": item["ubicaciones"],
            "alternativa": True,
            "nota": "Las alternativas PT comparten el saldo de componentes; no son sumables.",
        })
    return {"items": items, "fuente_kg": kg_payload["fuente_vigente"], "politica_piloto": "SIN_CONTROL_CALIDAD_DESDE_PESAJE"}


def list_pt_manual_balances(session, *, actor_id, article_id=None, location=None):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    location_ids, _scope = allowed_location_ids(
        session, actor_id=actor_id, article_class="PRODUCTO_TERMINADO",
    )
    statement = select(ScmSaldoInventario).join(ScmArticulo).where(ScmArticulo.clase == "PRODUCTO_TERMINADO")
    if article_id is not None:
        statement = statement.where(ScmSaldoInventario.articulo_scm_id == int(article_id))
    if location_ids is not None:
        statement = statement.where(ScmSaldoInventario.ubicacion_id.in_(location_ids or {-1}))
    if location:
        statement = statement.join(ScmUbicacionInventario).where(ScmUbicacionInventario.codigo == str(location).strip().upper())
    balances = session.scalars(statement.order_by(ScmArticulo.codigo, ScmSaldoInventario.ubicacion_id)).all()
    return {"items": [{
        "id": str(item.id), "articulo": item.articulo.to_dict(),
        "ubicacion": item.ubicacion.to_dict(), "saldo_un": _qty(item.cantidad_fisica),
        "version": item.version, "updated_at": _iso(item.updated_at),
    } for item in balances]}


def list_pt_manual_movements(session, *, actor_id, balance_id):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    balance = session.get(ScmSaldoInventario, balance_id)
    if balance is None:
        raise ScmServiceError("PT_MANUAL_BALANCE_NOT_FOUND", "El saldo PT manual no existe.", status_code=404)
    allowed_ids, _scope = allowed_location_ids(
        session, actor_id=actor_id, article_class="PRODUCTO_TERMINADO",
    )
    if allowed_ids is not None and balance.ubicacion_id not in allowed_ids:
        raise ScmServiceError("PT_MANUAL_BALANCE_NOT_FOUND", "El saldo PT manual no pertenece al alcance del actor.", status_code=404)
    movements = session.scalars(select(ScmMovimientoInventario).where(
        ScmMovimientoInventario.saldo_id == balance.id,
        ScmMovimientoInventario.tipo.in_({
            "ENTRADA_MANUAL_PT", "SALIDA_MANUAL_PT",
            "AJUSTE_POSITIVO_MANUAL_PT", "AJUSTE_NEGATIVO_MANUAL_PT",
        }),
    ).order_by(ScmMovimientoInventario.fecha_operativa, ScmMovimientoInventario.created_at)).all()
    return {"items": [{
        "id": str(item.id), "tipo": item.tipo, "cantidad_delta": _qty(item.cantidad_delta),
        "saldo_resultante": _qty(item.saldo_fisico_resultante), "unidad": "UN",
        "fecha_operativa": (item.fecha_operativa or item.created_at.date()).isoformat(), "motivo": item.motivo,
        "referencia": item.referencia, "actor_id": item.actor_id,
        "operation_id": str(item.operation_id), "created_at": _iso(item.created_at),
    } for item in movements]}


def list_pt_manual_movements_all(session, *, actor_id, article_id=None, location=None):
    """List PT manual history inside the actor's warehouse scope."""
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    allowed_ids, _scope = allowed_location_ids(
        session, actor_id=actor_id, article_class="PRODUCTO_TERMINADO",
    )
    statement = select(ScmMovimientoInventario, ScmSaldoInventario).join(
        ScmSaldoInventario, ScmMovimientoInventario.saldo_id == ScmSaldoInventario.id,
    ).join(ScmArticulo, ScmSaldoInventario.articulo_scm_id == ScmArticulo.id).where(
        ScmArticulo.clase == "PRODUCTO_TERMINADO",
        ScmMovimientoInventario.tipo.in_({
            "ENTRADA_MANUAL_PT", "SALIDA_MANUAL_PT",
            "AJUSTE_POSITIVO_MANUAL_PT", "AJUSTE_NEGATIVO_MANUAL_PT",
        }),
    )
    if article_id is not None:
        statement = statement.where(ScmSaldoInventario.articulo_scm_id == int(article_id))
    if allowed_ids is not None:
        statement = statement.where(ScmSaldoInventario.ubicacion_id.in_(allowed_ids or {-1}))
    if location:
        statement = statement.join(
            ScmUbicacionInventario,
            ScmSaldoInventario.ubicacion_id == ScmUbicacionInventario.id,
        ).where(ScmUbicacionInventario.codigo == str(location).strip().upper())
    rows = session.execute(statement.order_by(
        ScmMovimientoInventario.fecha_operativa,
        ScmMovimientoInventario.created_at,
    )).all()
    return {"items": [{
        "id": str(item.id), "balance_id": str(balance.id), "tipo": item.tipo,
        "cantidad_delta": _qty(item.cantidad_delta),
        "saldo_resultante": _qty(item.saldo_fisico_resultante), "unidad": "UN",
        "fecha_operativa": (item.fecha_operativa or item.created_at.date()).isoformat(),
        "motivo": item.motivo, "referencia": item.referencia, "actor_id": item.actor_id,
        "operation_id": str(item.operation_id), "created_at": _iso(item.created_at),
    } for item, balance in rows]}


def _assert_pt_manual_write_enabled():
    if not current_app.config.get("PT_MANUAL_WRITE_ENABLED", False):
        raise ScmServiceError(
            "PT_MANUAL_WRITE_NOT_ENABLED",
            "La escritura del Kardex PT manual esta deshabilitada.",
            status_code=409,
        )


def register_pt_manual_movement(session, *, actor_id, operation_id, data):
    _assert_pt_manual_write_enabled()
    try:
        article_id = int(data.get("articulo_scm_id"))
        location_id = int(data.get("ubicacion_id"))
    except (TypeError, ValueError) as error:
        raise ScmServiceError("PT_MANUAL_TARGET_REQUIRED", "articulo_scm_id y ubicacion_id son obligatorios.", status_code=422) from error
    article = session.get(ScmArticulo, article_id)
    location = session.get(ScmUbicacionInventario, location_id)
    if article is None or article.clase != "PRODUCTO_TERMINADO" or article.unidad_inventario != "UN":
        raise ScmServiceError("PT_MANUAL_ARTICLE_INVALID", "El artículo debe ser un PT de catálogo en UN.", status_code=422)
    if location is None:
        raise ScmServiceError("LOCATION_NOT_FOUND", "La ubicación no existe.", status_code=404)
    allowed_ids, scope = allowed_location_ids(
        session, actor_id=actor_id, article_class="PRODUCTO_TERMINADO",
    )
    if allowed_ids is not None and location.id not in allowed_ids:
        raise ScmServiceError("LOCATION_NOT_FOUND", "La ubicación no pertenece al alcance del actor.", status_code=404)
    movement_type = str(data.get("tipo") or "").strip().upper()
    if movement_type not in {"ENTRADA", "SALIDA", "AJUSTE_POSITIVO", "AJUSTE_NEGATIVO"}:
        raise ScmServiceError("PT_MANUAL_MOVEMENT_TYPE_INVALID", "No se permite SALDO_INICIAL; use el mecanismo de apertura gobernado.", status_code=422)
    capability = "INVENTARIO_AJUSTAR" if movement_type.startswith("AJUSTE_") else "INVENTARIO_PT_MOVIMIENTO"
    actor = load_actor(session, actor_id, capability=capability)
    raw_version = data.get("version", data.get("expected_version"))
    expected = expected_version(raw_version)
    try:
        quantity = Decimal(str(data.get("cantidad"))).quantize(Decimal("0.001"))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError("PT_MANUAL_QUANTITY_INVALID", "cantidad debe ser positiva.", status_code=422) from error
    if not quantity.is_finite() or quantity <= 0:
        raise ScmServiceError("PT_MANUAL_QUANTITY_INVALID", "cantidad debe ser positiva.", status_code=422)
    try:
        operation_date = date.fromisoformat(str(data.get("fecha_operativa")))
    except (TypeError, ValueError) as error:
        raise ScmServiceError("PT_MANUAL_DATE_INVALID", "fecha_operativa debe ser YYYY-MM-DD.", status_code=422) from error
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    reference = str(data.get("referencia") or "").strip()[:120] or None
    internal_type = {
        "ENTRADA": "ENTRADA_MANUAL_PT",
        "SALIDA": "SALIDA_MANUAL_PT",
        "AJUSTE_POSITIVO": "AJUSTE_POSITIVO_MANUAL_PT",
        "AJUSTE_NEGATIVO": "AJUSTE_NEGATIVO_MANUAL_PT",
    }[movement_type]
    command = {
        "articulo_scm_id": article.id, "ubicacion_id": location.id,
        "tipo": internal_type, "cantidad": _qty(quantity),
        "fecha_operativa": operation_date.isoformat(), "motivo": reason,
        "referencia": reference,
        "version": expected,
    }
    operation, replay = _reserve(session, operation_id, "POST /inventario/pt/movimientos", actor, command)
    if replay is not None:
        return replay
    balance = session.scalar(select(ScmSaldoInventario).where(
        ScmSaldoInventario.articulo_scm_id == article.id,
        ScmSaldoInventario.ubicacion_id == location.id,
    ).with_for_update())
    if balance is None:
        candidate = ScmSaldoInventario(articulo_scm_id=article.id, ubicacion_id=location.id, cantidad_fisica=0)
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            balance = candidate
        except IntegrityError:
            balance = session.scalar(select(ScmSaldoInventario).where(
                ScmSaldoInventario.articulo_scm_id == article.id,
                ScmSaldoInventario.ubicacion_id == location.id,
            ).with_for_update())
            if balance is None:
                raise ScmServiceError("PT_MANUAL_BALANCE_CONCURRENT_CREATE", "No se pudo resolver el saldo concurrente.", status_code=409)
    if balance.version != expected:
        raise ScmServiceError(
            "VERSION_CONFLICT", "El saldo PT manual cambió; recargue y reintente.",
            status_code=409, details={"expected_version": expected, "current_version": balance.version},
        )
    delta = quantity if movement_type in {"ENTRADA", "AJUSTE_POSITIVO"} else -quantity
    result = Decimal(balance.cantidad_fisica) + delta
    if result < Decimal(balance.cantidad_reservada) + Decimal(balance.cantidad_no_disponible):
        raise ScmServiceError("PT_MANUAL_INSUFFICIENT_BALANCE", "La salida excede el saldo PT manual.", status_code=409)
    balance.cantidad_fisica = result
    balance.version += 1
    movement = ScmMovimientoInventario(
        saldo_id=balance.id, tipo=internal_type, cantidad_delta=delta,
        saldo_fisico_resultante=result, fecha_operativa=operation_date,
        motivo=reason, referencia=reference, actor_id=actor.id,
        operation_id=operation_id,
    )
    session.add(movement)
    session.flush()
    payload = {
            "movement": {
            "id": str(movement.id), "tipo": movement.tipo,
            "cantidad_delta": _qty(movement.cantidad_delta),
            "saldo_resultante": _qty(movement.saldo_fisico_resultante), "unidad": "UN",
            "fecha_operativa": operation_date.isoformat(), "motivo": reason,
            "referencia": reference, "operation_id": str(operation_id),
        },
        "saldo": {
            "id": str(balance.id), "articulo_scm_id": article.id,
            "ubicacion_id": location.id, "saldo_un": _qty(balance.cantidad_fisica),
            "version": balance.version,
        },
    }
    session.add(ScmEvento(
        aggregate_type="PT_MANUAL_SALDO", aggregate_id=str(balance.id),
        tipo="PT_MANUAL_MOVIMIENTO_REGISTRADO", actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor), after_json=payload,
        operation_id=operation.operation_id,
    ))
    return _complete(session, operation, payload)
