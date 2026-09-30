"""Read-only history and detail projections for one SCM manga identity."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.models.scm_inventory import ScmMovimientoInventario
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmEtiquetaUnidadKg,
    ScmMovimientoInventarioKg,
    ScmRetiroArmadoKgItem,
    ScmUnidadFisicaKg,
)
from app.models.scm_inventory_operations import ScmTransferenciaItem
from app.models.scm_auditoria import ScmEvento
from app.models.scm_ot import (
    ScmAnulacionPesajeManga,
    ScmCorreccionPesajeManga,
    ScmEtiquetaManga,
    ScmManga,
    ScmPesajeManga,
    ScmReaperturaManga,
    ScmTramoMangaTrabajo,
)
from app.models.scm_warehouse import ScmExistenciaManga, ScmReversionRecepcionManga
from app.models.molde import Molde
from app.services.scm_production_reports_service import (
    GROUP_OPTIONS,
    _context_group_value,
    _filters,
    history_rows_for_manga_detail,
)
from app.services.scm_service_support import ScmServiceError, load_actor
from app.services.scm_warehouse_scope_service import allowed_location_ids
from app.services.scm_manga_assignment_projection import (
    effective_assignment,
    effective_assignment_for_segment,
    effective_work,
    effective_work_for_segment,
)
from app.services.scm_weighing_service import _effective_projection, _weighing_color_identity


def _iso(value):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _pesaje_timestamp(value, timezone_name=None):
    """Normalize a weighing timestamp to the UTC API contract.

    SQLite may return the stored DateTime without its offset. In that case the
    weighing's timezone snapshot is the only authoritative interpretation;
    correction projections already carry their explicit offset.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime) and value.tzinfo is None:
        try:
            value = value.replace(tzinfo=ZoneInfo(timezone_name or "UTC"))
        except (TypeError, ValueError):
            value = value.replace(tzinfo=timezone.utc)
    return _iso(value)


def _number(value):
    return float(value) if value is not None else None


def _text(value):
    return value if value not in (None, "") else None


def _actor_name(value):
    return getattr(value, "nombre_completo", None) if value is not None else None


def _location_payload(value):
    """Expose the operational location identity without internal flags/links."""
    if value is None:
        return None
    return {
        "codigo": getattr(value, "codigo", None),
        "nombre": getattr(value, "nombre", None),
        "tipo": getattr(value, "tipo", None),
    }


def _section(state, *, items=None, reason=None, **extra):
    payload = {"estado": state}
    if items is not None:
        payload["items"] = items
    if reason:
        payload["motivo"] = reason
    payload.update(extra)
    return payload


def _parse_group(raw):
    if raw in (None, ""):
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_GROUP_PATH",
            "grupo debe ser un JSON array de dimensiones y valores.",
            status_code=400,
        ) from error
    if not isinstance(value, list):
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_GROUP_PATH",
            "grupo debe ser un JSON array de dimensiones y valores.",
            status_code=400,
        )
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"dimension", "value"}:
            raise ScmServiceError(
                "INVALID_OBSERVABILITY_GROUP_PATH",
                "Cada elemento de grupo requiere dimension y value.",
                status_code=400,
            )
        dimension = str(item["dimension"]).strip().upper()
        if dimension not in GROUP_OPTIONS:
            raise ScmServiceError(
                "INVALID_OBSERVABILITY_GROUP_PATH",
                "La dimensión de grupo no es válida.",
                status_code=400,
            )
        result.append({"dimension": dimension, "value": item["value"]})
    return result


def _group_matches(row, group):
    return all(row.get(item["dimension"]) == item["value"] for item in group)


def list_manga_history(session, *, actor_id, filters=None, group=None):
    """List the public manga identities counted by the applied history query."""
    actor = load_actor(session, actor_id, capability="OT_VER")
    normalized = _filters(filters, require_dates=True)
    visible_weights = actor.tiene_capacidad("MANGA_PESAJE_VER")
    if not visible_weights:
        raise ScmServiceError(
            "MANGA_PESAJE_VER_REQUIRED",
            "MANGA_PESAJE_VER es obligatorio para consultar mangas y pesos.",
            status_code=403,
        )
    runs = []
    rows = []
    if visible_weights:
        normalized, rows, runs = history_rows_for_manga_detail(session, filters)
    group_path = _parse_group(group)
    if len(group_path) > len(normalized["groups"]):
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_GROUP_PATH",
            "grupo no puede superar el camino de agrupación aplicado.",
            status_code=400,
        )
    expected = normalized["groups"][:len(group_path)]
    if [item["dimension"] for item in group_path] != expected:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_GROUP_PATH",
            "grupo debe ser un prefijo exacto de las agrupaciones aplicadas.",
            status_code=400,
        )
    # The report may carry an incomplete ledger row for coverage diagnostics;
    # it is deliberately excluded because the manga list is identity-counted
    # evidence, never a placeholder with a fabricated zero weight.
    selected_rows = [
        row for row in rows
        if row.get("_known") and row.get("PESO_KG") is not None
        and _group_matches(row, group_path)
    ]
    grouped = {}
    for row in selected_rows:
        manga_id = row.get("_manga_id")
        if manga_id in grouped:
            item = grouped[manga_id]
            item["aporte_consulta_kg"] += Decimal(str(row.get("PESO_KG") or 0))
            item["tramos_consulta"] += 1
            continue
        item = {
            "id": str(getattr(next((m for run in runs for m in run["mangas"].values() if m.id == manga_id), None), "public_id", manga_id)),
            "codigo": row.get("MANGA") or None,
            "articulo": {"codigo": row.get("ARTICULO"), "nombre": row.get("ARTICULO_NOMBRE")},
            "molde": {"codigo": row.get("MOLDE_CODIGO") or row.get("MOLDE"), "nombre": row.get("MOLDE_NOMBRE")},
            "pieza": {"codigo": row.get("PIEZA_CODIGO") or row.get("PIEZA"), "nombre": row.get("PIEZA_NOMBRE")},
            "color": row.get("COLOR"),
            "estado": None,
            "aporte_consulta_kg": Decimal(str(row.get("PESO_KG") or 0)),
            "tramos_consulta": 1,
            "grupo": [{"dimension": dimension, "value": row.get(dimension)} for dimension in normalized["groups"]],
        }
        manga = next((m for run in runs for m in run["mangas"].values() if m.id == manga_id), None)
        if manga is not None:
            item["codigo"] = manga.codigo
            item["estado"] = manga.estado
        grouped[manga_id] = item
    items = []
    for item in grouped.values():
        item["aporte_consulta_kg"] = _number(item["aporte_consulta_kg"])
        items.append(item)
    items.sort(key=lambda item: (item.get("codigo") or "", item["id"]))
    return {
        "items": items,
        "total": len(items),
        "grupo": group_path,
        "filters": {key: value.isoformat() if isinstance(value, date) else value for key, value in normalized.items() if key not in {"groups", "measures"}},
        "visibilidad": {"pesaje": visible_weights, "restriccion": None if visible_weights else "MANGA_PESAJE_VER requerido para ver pesos"},
        "as_of": _iso(datetime.now(timezone.utc)),
    }


def _identity(session, manga):
    work = effective_work(manga)
    operation = getattr(work, "orden_operacion", None)
    fabrication = getattr(operation, "fabricacion", None)
    work_color = getattr(work, "trabajo_color", None)
    mold_code = getattr(work_color, "molde_codigo_snapshot", None) or (
        fabrication.molde_id if fabrication is not None else None
    )
    mold = session.get(Molde, mold_code) if mold_code else None
    article = getattr(getattr(manga, "lote_articulo", None), "articulo", None)
    variant = getattr(getattr(article, "pieza_color", None), "pieza_color", None)
    piece = getattr(variant, "pieza_rel", None)
    return {
        "id": str(manga.public_id),
        "codigo": manga.codigo,
        "articulo": {"codigo": manga.articulo_codigo_snapshot, "nombre": manga.articulo_nombre_snapshot},
        "pieza_color": manga.pieza_color_sku_snapshot,
        "color": manga.color_snapshot,
        "molde": {"codigo": mold_code, "nombre": getattr(mold, "nombre", None)} if mold_code else None,
        "pieza": {"codigo": getattr(piece, "codigo", None), "nombre": getattr(piece, "nombre", None)} if piece else None,
        "color_identidad": _weighing_color_identity(manga, effective_work(manga)),
        "tipo_manga": {"codigo": manga.tipo_contenedor_codigo_snapshot, "nombre": manga.tipo_contenedor_nombre_snapshot},
        "tipo": manga.tipo,
        "estado": manga.estado,
        "version": manga.version,
    }


def _documents(manga):
    trabajo = effective_work(manga)
    ot = getattr(trabajo, "orden_trabajo", None) or getattr(manga, "ot", None)
    trabajo_color = getattr(trabajo, "trabajo_color", None)
    assignment = effective_assignment(manga)
    responsable = getattr(assignment, "trabajador", None)
    effective = {
        "of": getattr(getattr(trabajo, "orden_operacion", None), "codigo", None),
        "ot": getattr(ot, "codigo_ot", None),
        "trabajo": getattr(trabajo, "codigo", None),
        "fecha_productiva": _iso(getattr(ot, "fecha", None)),
        "objetivo_color": getattr(trabajo_color, "color_nombre_snapshot", None),
        "maquina": (
            getattr(ot, "maquina_nombre_snapshot", None)
            or getattr(getattr(ot, "maquina", None), "codigo", None)
            or getattr(ot, "maquina_codigo_snapshot", None)
        ),
        "responsable": _actor_name(responsable),
    }
    historical = []
    for segment in getattr(manga, "tramos_trabajo", ()) or ():
        segment_work = effective_work_for_segment(manga, segment)
        segment_ot = getattr(segment_work, "orden_trabajo", None)
        segment_assignment = effective_assignment_for_segment(manga, segment)
        segment_responsable = getattr(segment_assignment, "trabajador", None)
        historical.append({
            "tramo_id": str(segment.id),
            "secuencia": segment.secuencia,
            "of": getattr(getattr(segment_work, "orden_operacion", None), "codigo", None),
            "ot": getattr(segment_ot, "codigo_ot", None),
            "trabajo": getattr(segment_work, "codigo", None),
            "objetivo_color": getattr(getattr(segment_work, "trabajo_color", None), "color_nombre_snapshot", None),
            "maquina": (
                getattr(segment_ot, "maquina_nombre_snapshot", None)
                or getattr(getattr(segment_ot, "maquina", None), "codigo", None)
                or getattr(segment_ot, "maquina_codigo_snapshot", None)
            ),
            "responsable": _actor_name(segment_responsable),
            "estado": segment.estado,
        })
    return {"efectivos": effective, "historicos": historical}


def _tramos(manga, visible_weights):
    items = []
    for segment in getattr(manga, "tramos_trabajo", ()) or ():
        item = {
            "id": str(segment.id), "secuencia": segment.secuencia, "estado": segment.estado,
            "cantidad_inicio_un": _number(segment.cantidad_inicio_un),
            "cantidad_fin_un": _number(segment.cantidad_fin_un),
            "cantidad_atribuida_un": _number(segment.cantidad_atribuida_un),
            "iniciada_at": _iso(segment.iniciada_at), "cerrada_at": _iso(segment.cerrada_at),
            "motivo_cierre": segment.motivo_cierre,
        }
        if visible_weights:
            item.update({
                "cantidad_inicio_kg": _number(segment.cantidad_inicio_kg),
                "cantidad_fin_kg": _number(segment.cantidad_fin_kg),
                "cantidad_atribuida_kg": _number(segment.cantidad_atribuida_kg),
                "calidad_evidencia_kg": segment.calidad_evidencia_kg,
            })
        items.append(item)
    return _section("disponible" if items else "sin_datos", items=items)


def _pesajes(session, manga, visible_weights):
    if not visible_weights:
        return _section("restringido", reason="MANGA_PESAJE_VER requerido para ver pesos")
    pesaje_rows = ScmPesajeManga.query.filter_by(manga_id=manga.id).order_by(ScmPesajeManga.pesada_at, ScmPesajeManga.id).all()
    pesajes = []
    for item in pesaje_rows:
        correction = ScmCorreccionPesajeManga.query.filter_by(pesaje_id=item.id).order_by(ScmCorreccionPesajeManga.id).all()
        annulment = ScmAnulacionPesajeManga.query.filter_by(pesaje_id=item.id).one_or_none()
        def projection(value):
            allowed = {"peso_bruto_kg", "tara_kg", "peso_fisico_neto_kg", "cantidad_confirmada", "kg_produccion_ot", "pesada_at", "fecha_local_pesaje", "dias_desfase_operativo", "alerta_fecha"}
            if not isinstance(value, dict):
                return None
            result = {key: value.get(key) for key in allowed if key in value}
            if "pesada_at" in result:
                result["pesada_at"] = _pesaje_timestamp(result["pesada_at"], item.timezone_snapshot)
            return result
        pesajes.append({
            "id": str(item.public_id), "estado": item.estado, "pesada_at": _pesaje_timestamp(item.pesada_at, item.timezone_snapshot),
            "peso_fisico_neto_kg": _number(item.peso_fisico_neto_kg), "peso_bruto_kg": _number(item.peso_bruto_kg),
            "tara_kg": _number(item.tara_kg), "fuente_cantidad": item.fuente_cantidad,
            "kg_produccion_ot": _number(item.kg_produccion_ot), "kg_fabricacion_estimado": _number(item.kg_fabricacion_estimado),
            "pesado_por": _actor_name(item.pesado_por),
            "correcciones": [{"id": str(c.public_id), "estado": c.estado, "motivo": c.reason, "solicitada_at": _iso(c.requested_at), "solicitada_por": _actor_name(c.requested_by), "resuelta_at": _iso(c.resolved_at), "resuelta_por": _actor_name(c.resolved_by), "resultado": projection(c.result_projection_json)} for c in correction],
            "anulacion": {"id": str(annulment.public_id), "motivo": annulment.motivo, "anulada_at": _iso(annulment.anulada_at), "anulada_por": _actor_name(annulment.anulada_por)} if annulment else None,
        })
    controls = []
    for item in getattr(manga, "controles_peso", ()) or ():
        controls.append({
            "id": str(item.public_id), "tipo": item.tipo, "unidad": item.unidad_evidencia,
            "peso_neto_kg": _number(item.peso_neto_kg), "aporte_desde_control_anterior_kg": _number(item.aporte_desde_control_anterior_kg),
            "conteo_acumulado_un": _number(item.conteo_acumulado_un), "pesado_at": _pesaje_timestamp(item.pesado_at, item.timezone_snapshot),
            "pesado_por": _actor_name(item.pesado_por), "tramo_id": str(item.tramo_id),
        })
    active_weighing = next(
        (item for item in reversed(pesaje_rows) if item.estado == "VIGENTE"),
        None,
    )
    vigente = None
    if active_weighing is not None and manga.estado != "ANULADA":
        applied = next(
            (
                correction for correction in reversed(
                    ScmCorreccionPesajeManga.query.filter_by(
                        pesaje_id=active_weighing.id, estado="APLICADA"
                    ).order_by(ScmCorreccionPesajeManga.id).all()
                )
            ),
            None,
        )
        projection = _effective_projection(active_weighing)
        vigente = {
            "pesaje_id": str(active_weighing.public_id),
            "estado": "VIGENTE",
            "peso_bruto_kg": _number(projection.get("peso_bruto_kg")),
            "tara_kg": _number(projection.get("tara_kg")),
            "peso_fisico_neto_kg": _number(projection.get("peso_fisico_neto_kg")),
            "cantidad_confirmada": _number(projection.get("cantidad_confirmada")),
            "kg_produccion_ot": _number(projection.get("kg_produccion_ot")),
            "pesada_at": _pesaje_timestamp(projection.get("pesada_at"), active_weighing.timezone_snapshot),
            "fecha_local_pesaje": projection.get("fecha_local_pesaje"),
            "corregida": applied is not None,
            "corregida_at": _iso(applied.resolved_at) if applied else None,
        }
    cierre_control = []
    # A closure-from-control is an historical event.  Keep it visible after
    # receiving, reopening, or a later weighing; current state and `vigente`
    # are projected independently below/above and must not erase the event.
    closure_event = session.scalar(
        select(ScmEvento)
        .where(
            ScmEvento.aggregate_type == "MANGA",
            ScmEvento.aggregate_id.in_((str(manga.id), str(manga.public_id))),
            ScmEvento.tipo == "KG_MANGA_CLOSED_FROM_LAST_CONTROL",
        )
        .order_by(ScmEvento.occurred_at.desc(), ScmEvento.id.desc())
    )
    event_control_id = (
        ((closure_event.after_json or {}).get("control_fuente") or {}).get("id")
        if closure_event else None
    )
    final_control = next(
        (item for item in reversed(controls)
         if item.get("unidad") == "KG" and (
             event_control_id is None or item.get("id") == event_control_id
         )),
        None,
    )
    if final_control is not None and closure_event is not None:
        cierre_control.append({
            "tipo": "CIERRE_DESDE_CONTROL",
            "control_id": final_control["id"],
            "peso_neto_kg": final_control["peso_neto_kg"],
            "cerrado_at": _iso(closure_event.occurred_at),
            "simula_pesaje": False,
        })
    reopenings = [{"id": str(item.public_id), "tipo": item.tipo_reapertura, "motivo": item.motivo, "reabierta_at": _iso(item.reabierta_at), "reabierta_por": _actor_name(item.reabierta_por), "peso_base_neto_kg": _number(item.peso_base_neto_kg)} for item in getattr(manga, "reaperturas", ()) or ()]
    return _section("disponible" if pesajes or controls or reopenings else "sin_datos", vigente=vigente, pesajes=pesajes, controles_acumulados=controls, cierres_control=cierre_control, reaperturas=reopenings)


def _stock(session, manga, actor_id, visible_weights):
    actor = load_actor(session, actor_id)
    if not actor.tiene_capacidad("INVENTARIO_VER"):
        return _section("restringido", reason="INVENTARIO_VER requerido para consultar custodia y movimientos")
    items = []
    existence = session.scalar(select(ScmExistenciaManga).where(ScmExistenciaManga.manga_id == manga.id))
    kg_existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga.id))
    kg_units = []
    if kg_existence is not None and kg_existence.unidad_fisica_kg_id:
        root_unit = session.get(ScmUnidadFisicaKg, kg_existence.unidad_fisica_kg_id)
        if root_unit is not None:
            kg_units = session.scalars(select(ScmUnidadFisicaKg).where(or_(
                ScmUnidadFisicaKg.id == root_unit.id,
                ScmUnidadFisicaKg.unidad_raiz_id == root_unit.id,
                ScmUnidadFisicaKg.unidad_padre_id == root_unit.id,
            )).order_by(ScmUnidadFisicaKg.created_at, ScmUnidadFisicaKg.id)).all()
    historical_receipts = []
    current_custody = []

    def scope_allows(*, location_id, article_class):
        allowed, _scope = allowed_location_ids(
            session, actor_id=actor_id, article_class=article_class
        )
        return allowed is None or location_id in allowed

    def kg_receipt_payload(receipt, unit, *, historical=False):
        location = getattr(receipt, "ubicacion", None)
        return {
            "estado": "historica" if historical else "disponible",
            "estado_custodia": "historica" if historical else "vigente",
            "unidad": "KG",
            "unidad_id": str(unit.public_id) if unit is not None else None,
            "unidad_codigo": unit.codigo if unit is not None else None,
            "cantidad_fisica_kg": _number(receipt.cantidad_fisica_kg),
            "cantidad_reservada_kg": _number(receipt.cantidad_reservada_kg),
            "estado_logistico": receipt.estado_logistico,
            "estado_calidad": receipt.estado_calidad,
            "origen_tipo": receipt.origen_tipo,
            "ubicacion": _location_payload(location),
        }

    # The manga-linked receipt is an auditable snapshot of the original
    # reception. It is never used as current custody after the unit is
    # withdrawn, divided, or returned under a descendant identity.
    if kg_existence is not None:
        root_for_receipt = next(
            (unit for unit in kg_units if unit.id == kg_existence.unidad_fisica_kg_id),
            None,
        )
        article_class = getattr(getattr(kg_existence, "articulo", None), "clase", None)
        if scope_allows(
            location_id=kg_existence.ubicacion_id, article_class=article_class
        ):
            if visible_weights:
                historical_receipts.append(
                    kg_receipt_payload(kg_existence, root_for_receipt, historical=True)
                )
            else:
                historical_receipts.append({
                    "estado": "restringido", "unidad": "KG",
                    "motivo": "MANGA_PESAJE_VER requerido para ver pesos",
                })
        else:
            historical_receipts.append({
                "estado": "restringido", "unidad": "KG", "motivo": "warehouse_scope"
            })

    # Current custody is projected from each physical unit's current receipt.
    # An active unit without a receipt remains visible as an unknown quantity;
    # no difference against the original receipt is calculated here.
    for unit in kg_units:
        if unit.estado != "ACTIVA":
            continue
        article_class = getattr(getattr(unit, "articulo", None), "clase", None)
        receipt = (
            session.get(ScmExistenciaMangaKg, unit.recepcion_vigente_id)
            if unit.recepcion_vigente_id else None
        )
        location_id = (
            receipt.ubicacion_id if receipt is not None else unit.ubicacion_id
        )
        if not scope_allows(location_id=location_id, article_class=article_class):
            current_custody.append({
                "estado": "restringido", "unidad": "KG", "motivo": "warehouse_scope"
            })
            continue
        if receipt is not None and receipt.estado_logistico != "REVERSADA" and receipt.unidad_fisica_kg_id == unit.id:
            if visible_weights:
                current_custody.append(kg_receipt_payload(receipt, unit))
            else:
                current_custody.append({
                    "estado": "restringido", "unidad": "KG",
                    "motivo": "MANGA_PESAJE_VER requerido para ver pesos",
                })
        elif unit.estado == "ACTIVA":
            current_custody.append({
                "estado": "sin_datos", "estado_custodia": "vigente",
                "unidad": "KG", "unidad_id": str(unit.public_id),
                "unidad_codigo": unit.codigo,
                "estado_logistico": unit.estado_logistico,
                "estado_calidad": unit.estado_calidad,
                "ubicacion": _location_payload(getattr(unit, "ubicacion", None)),
                "cantidad_fisica_kg": None,
                "cantidad_reservada_kg": None,
                "motivo": "remanente_sin_medicion",
            })
    for item, unit in ((existence, "UN"), (kg_existence, "KG")):
        if item is None:
            continue
        location = getattr(item, "ubicacion", None)
        article = getattr(getattr(item, "articulo", None), "clase", None)
        allowed, _scope = allowed_location_ids(session, actor_id=actor_id, article_class=article)
        if allowed is not None and getattr(item, "ubicacion_id", None) not in allowed:
            items.append({"estado": "restringido", "unidad": unit, "motivo": "warehouse_scope"})
            continue
        if unit == "UN":
            items.append({"estado": "disponible", "unidad": "UN", "cantidad_fisica_un": _number(item.cantidad_fisica), "cantidad_reservada_un": _number(item.cantidad_reservada), "estado_logistico": item.estado_logistico, "estado_calidad": item.estado_calidad, "ubicacion": _location_payload(location)})
    items.extend(current_custody)
    movements = []

    # Movement history is linked through typed references and the canonical
    # foreign keys.  A bare ``referencia_id`` is unsafe here: integer manga
    # ids and UUID/public ids can collide across aggregates.
    pesajes = ScmPesajeManga.query.filter_by(manga_id=manga.id).all()
    pesaje_ids = {str(item.public_id) for item in pesajes}
    control_ids = {str(item.public_id) for item in getattr(manga, "controles_peso", ()) or ()}
    corrections = [correction for pesaje in pesajes for correction in ScmCorreccionPesajeManga.query.filter_by(pesaje_id=pesaje.id).all()]
    correction_ids = {str(item.public_id) for item in corrections}
    un_transfer_items = []
    if existence is not None:
        un_transfer_items = session.scalars(select(ScmTransferenciaItem).where(ScmTransferenciaItem.existencia_manga_id == existence.id)).all()
    transfer_ids = {str(item.transferencia_id) for item in un_transfer_items}
    un_movement_ids = {
        item_id for item_id in (
            [existence.movimiento_ingreso_id] if existence is not None else []
        ) if item_id is not None
    }
    un_movement_ids.update(
        item_id for transfer in un_transfer_items
        for item_id in (transfer.movimiento_salida_id, transfer.movimiento_transito_id, transfer.movimiento_entrada_id)
        if item_id is not None
    )
    reversals = []
    if existence is not None:
        reversals = session.scalars(select(ScmReversionRecepcionManga).where(ScmReversionRecepcionManga.existencia_id == existence.id)).all()
    reversal_ids = {str(item.id) for item in reversals}
    un_filters = [ScmMovimientoInventario.id.in_(un_movement_ids)] if un_movement_ids else []
    un_filters.extend([
        (ScmMovimientoInventario.referencia_tipo == "MANGA") & (ScmMovimientoInventario.referencia_id == str(manga.public_id)),
        (ScmMovimientoInventario.referencia_tipo == "TRANSFERENCIA_INVENTARIO") & ScmMovimientoInventario.referencia_id.in_(transfer_ids),
        (ScmMovimientoInventario.referencia_tipo == "REVERSION_RECEPCION") & ScmMovimientoInventario.referencia_id.in_(reversal_ids),
        (ScmMovimientoInventario.referencia_tipo == "CORRECCION_PESAJE_MANGA") & ScmMovimientoInventario.referencia_id.in_(correction_ids),
        (ScmMovimientoInventario.referencia_tipo == "ANULACION_PESAJE_MANGA") & ScmMovimientoInventario.referencia_id.in_(pesaje_ids),
    ])

    kg_unit_ids = {str(unit.id) for unit in kg_units}
    kg_unit_public_ids = {str(unit.public_id) for unit in kg_units}
    kg_transfer_items = []
    if kg_unit_ids:
        kg_transfer_items = session.scalars(select(ScmTransferenciaItem).where(ScmTransferenciaItem.unidad_fisica_kg_id.in_([unit.id for unit in kg_units]))).all()
    kg_movement_ids = {
        item_id for item_id in (
            [kg_existence.movimiento_ingreso_id] if kg_existence is not None else []
        ) if item_id is not None
    }
    kg_movement_ids.update(
        item_id for transfer in kg_transfer_items
        for item_id in (transfer.movimiento_salida_id, transfer.movimiento_transito_id, transfer.movimiento_entrada_id)
        if item_id is not None
    )
    retire_items = session.scalars(select(ScmRetiroArmadoKgItem).where(ScmRetiroArmadoKgItem.unidad_id.in_([unit.id for unit in kg_units]))).all() if kg_units else []
    kg_movement_ids.update(item.movimiento_id for item in retire_items if item.movimiento_id is not None)
    kg_reference_ids = {str(item.retiro_id) for item in retire_items}
    kg_filters = [ScmMovimientoInventarioKg.id.in_(kg_movement_ids)] if kg_movement_ids else []
    kg_filters.extend([
        (ScmMovimientoInventarioKg.referencia_tipo == "MANGA") & (ScmMovimientoInventarioKg.referencia_id == str(manga.public_id)),
        (ScmMovimientoInventarioKg.referencia_tipo.in_(("PESAJE_MANGA", "CONTROL_PESO_MANGA"))) & ScmMovimientoInventarioKg.referencia_id.in_(pesaje_ids | control_ids),
        (ScmMovimientoInventarioKg.referencia_tipo == "CORRECCION_PESAJE_MANGA") & ScmMovimientoInventarioKg.referencia_id.in_(correction_ids),
        (ScmMovimientoInventarioKg.referencia_tipo == "ANULACION_PESAJE_MANGA") & ScmMovimientoInventarioKg.referencia_id.in_(pesaje_ids),
        (ScmMovimientoInventarioKg.referencia_tipo == "RETIRO_ARMADO_KG") & ScmMovimientoInventarioKg.referencia_id.in_(kg_reference_ids | kg_unit_ids | kg_unit_public_ids),
        (ScmMovimientoInventarioKg.referencia_tipo.in_(("DIVISION_UNIDAD_KG", "CORRECCION_ARMADO"))) & ScmMovimientoInventarioKg.referencia_id.in_(kg_unit_ids | kg_unit_public_ids),
    ])

    def movement_visible(item, *, expected_unit, expected_class):
        saldo = getattr(item, "saldo", None)
        if saldo is None or getattr(saldo, "ubicacion_id", None) is None:
            return False
        movement_class = getattr(getattr(saldo, "articulo", None), "clase", None)
        if expected_class is not None and movement_class != expected_class:
            return False
        allowed, _scope = allowed_location_ids(session, actor_id=actor_id, article_class=movement_class or expected_class)
        return allowed is None or saldo.ubicacion_id in allowed

    def un_payload(item):
        return {
            "unidad": "UN", "tipo": item.tipo, "cantidad_delta": _number(item.cantidad_delta),
            "saldo_fisico_resultante": _number(item.saldo_fisico_resultante), "timestamp": _iso(item.created_at),
            "motivo": item.motivo, "referencia_tipo": item.referencia_tipo, "referencia_id": item.referencia_id,
            "actor": {"id": item.actor_id, "nombre": _actor_name(item.actor)},
        }

    def kg_payload(item):
        return {
            "unidad": "KG", "tipo": item.tipo, "cantidad_delta": _number(item.cantidad_delta_kg),
            "saldo_fisico_resultante": _number(item.saldo_fisico_resultante_kg), "timestamp": _iso(item.created_at),
            "motivo": item.motivo, "referencia_tipo": item.referencia_tipo, "referencia_id": item.referencia_id,
            "actor": {"id": item.actor_id, "nombre": _actor_name(item.actor)},
        }

    expected_un_class = getattr(getattr(existence, "articulo", None), "clase", None) if existence else None
    if un_filters:
        for item in session.scalars(select(ScmMovimientoInventario).where(or_(*un_filters)).order_by(ScmMovimientoInventario.created_at, ScmMovimientoInventario.id)).all():
            if movement_visible(item, expected_unit="UN", expected_class=expected_un_class):
                movements.append(un_payload(item))
    expected_kg_class = getattr(getattr(kg_existence, "articulo", None), "clase", None) if kg_existence else None
    if visible_weights and kg_filters:
        for item in session.scalars(select(ScmMovimientoInventarioKg).where(or_(*kg_filters)).order_by(ScmMovimientoInventarioKg.created_at, ScmMovimientoInventarioKg.id)).all():
            if movement_visible(item, expected_unit="KG", expected_class=expected_kg_class):
                movements.append(kg_payload(item))
    return _section(
        "disponible" if items or movements or historical_receipts else "sin_datos",
        items=items,
        custodia_vigente=current_custody,
        recepciones_historicas=historical_receipts,
        movimientos=movements,
    )


def _labels(session, manga, actor_id, visible_weights):
    if not visible_weights:
        return _section("restringido", reason="MANGA_PESAJE_VER requerido para proteger datos vinculados a pesaje")
    items = [{"id": str(item.public_id), "tipo": item.tipo, "version": item.version, "estado": item.estado, "plantilla_version": item.plantilla_version, "generated_at": _iso(item.generated_at), "printed_at": _iso(item.printed_at)} for item in getattr(manga, "etiquetas", ()) or ()]
    actor = load_actor(session, actor_id)
    if not actor.tiene_capacidad("INVENTARIO_VER"):
        return _section("disponible" if items else "sin_datos", items=items)
    kg_existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga.id))
    if kg_existence is not None and kg_existence.unidad_fisica_kg_id:
        root = session.get(ScmUnidadFisicaKg, kg_existence.unidad_fisica_kg_id)
        units = session.scalars(select(ScmUnidadFisicaKg).where(or_(
            ScmUnidadFisicaKg.id == root.id,
            ScmUnidadFisicaKg.unidad_raiz_id == root.id,
            ScmUnidadFisicaKg.unidad_padre_id == root.id,
        ))).all() if root else []
        for unit in units:
            article = getattr(getattr(unit, "articulo", None), "clase", None)
            allowed, _scope = allowed_location_ids(session, actor_id=actor_id, article_class=article)
            if allowed is not None and unit.ubicacion_id not in allowed:
                continue
            items.extend({"id": str(item.public_id), "tipo": "KG", "version": item.version, "estado": item.estado, "generated_at": _iso(item.created_at)} for item in session.scalars(select(ScmEtiquetaUnidadKg).where(ScmEtiquetaUnidadKg.unidad_id == unit.id).order_by(ScmEtiquetaUnidadKg.version)).all())
    return _section("disponible" if items else "sin_datos", items=items)


def get_manga_detail(session, *, actor_id, public_id):
    actor = load_actor(session, actor_id, capability="OT_VER")
    manga = session.scalar(select(ScmManga).where(ScmManga.public_id == public_id))
    if manga is None:
        raise ScmServiceError("MANGA_NOT_FOUND", "La manga no existe.", status_code=404)
    visible_weights = actor.tiene_capacidad("MANGA_PESAJE_VER")
    return {
        "id": str(manga.public_id),
        "as_of": _iso(datetime.now(timezone.utc)),
        "secciones": {
            "identidad": _section("disponible", item=_identity(session, manga)),
            "documentos": _section("disponible", item=_documents(manga)),
            "tramos": _tramos(manga, visible_weights),
            "pesajes_correcciones_reaperturas": _pesajes(session, manga, visible_weights),
            "stock_movimientos": _stock(session, manga, actor_id, visible_weights),
            "etiquetas": _labels(session, manga, actor_id, visible_weights),
            "genealogia": _genealogy(session, manga, actor),
        },
        "visibilidad": {"pesaje": visible_weights, "inventario": actor.tiene_capacidad("INVENTARIO_VER"), "genealogia": actor.tiene_capacidad("GENEALOGIA_VER")},
    }


def _genealogy(session, manga, actor):
    if not actor.tiene_capacidad("GENEALOGIA_VER"):
        return _section("restringido", reason="GENEALOGIA_VER requerido")
    if not actor.tiene_capacidad("INVENTARIO_VER"):
        return _section("restringido", reason="INVENTARIO_VER requerido para el alcance de custodia")
    confirmation = getattr(manga, "confirmacion_armado", None)
    armado = None
    if confirmation is not None:
        origen = []
        for consumption in getattr(confirmation, "consumos", ()) or ():
            source = getattr(consumption, "asignacion_abastecimiento", None)
            source_existence = getattr(source, "existencia", None) if source else None
            source_article = getattr(source_existence, "articulo", None) if source_existence else None
            source_class = getattr(source_article, "clase", None)
            source_location = getattr(source_existence, "ubicacion_id", None)
            allowed, _scope = allowed_location_ids(
                session, actor_id=actor.id, article_class=source_class
            ) if source_existence is not None else (set(), None)
            if source_existence is None or (
                allowed is not None and source_location not in allowed
            ):
                origen.append({
                    "tipo": consumption.procedencia,
                    "nivel": consumption.nivel_genealogia,
                    "estado": "restringido" if source_existence is not None else "sin_datos",
                    "motivo": "warehouse_scope" if source_existence is not None else "origen_no_resoluble",
                })
                continue
            origen.append({
                "tipo": consumption.procedencia,
                "nivel": consumption.nivel_genealogia,
                "estado": "disponible",
                "manga_codigo": getattr(source_existence.manga, "codigo", None),
                "articulo": getattr(source_article, "codigo", None),
                "cantidad_incorporada_un": _number(consumption.cantidad_incorporada),
            })
        armado = {
            "orden_ensamble": getattr(confirmation.orden_ensamble, "codigo", None),
            "orden_trabajo": getattr(confirmation.orden_trabajo, "codigo_ot", None),
            "articulo_salida": {
                "codigo": getattr(confirmation.articulo_salida, "codigo", None),
                "nombre": getattr(confirmation.articulo_salida, "nombre", None),
            },
            "cantidad_planificada": _number(confirmation.cantidad_planificada),
            "cantidad_real": _number(confirmation.cantidad_real),
            "confirmado_at": _iso(confirmation.confirmado_at),
            "confirmado_por": _actor_name(confirmation.confirmado_por),
            "origen": origen,
        }
    existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga.id))
    if existence is None or existence.unidad_fisica_kg_id is None:
        return _section("disponible" if armado else "sin_datos", items=[], armado=armado) if armado else _section("sin_datos")
    root = session.get(ScmUnidadFisicaKg, existence.unidad_fisica_kg_id)
    if root is None:
        return _section("disponible" if armado else "sin_datos", items=[], armado=armado) if armado else _section("sin_datos")
    units = session.scalars(select(ScmUnidadFisicaKg).where(or_(
        ScmUnidadFisicaKg.id == root.id,
        ScmUnidadFisicaKg.unidad_raiz_id == root.id,
        ScmUnidadFisicaKg.unidad_padre_id == root.id,
    )).order_by(ScmUnidadFisicaKg.created_at, ScmUnidadFisicaKg.id)).all()
    weights = actor.tiene_capacidad("MANGA_PESAJE_VER")
    items = []
    for unit in units:
        article_class = getattr(getattr(unit, "articulo", None), "clase", None)
        allowed, _scope = allowed_location_ids(session, actor_id=actor.id, article_class=article_class)
        if allowed is not None and unit.ubicacion_id not in allowed:
            continue
        root_ref = unit.raiz or unit
        parent_ref = unit.padre
        item = {
            "unidad_id": str(unit.public_id), "codigo": unit.codigo,
            "raiz": {"id": str(root_ref.public_id), "codigo": root_ref.codigo},
            "padre": {"id": str(parent_ref.public_id), "codigo": parent_ref.codigo} if parent_ref else None,
            "estado": unit.estado, "estado_logistico": unit.estado_logistico,
        }
        if weights:
            item.update({"kg_entregado": _number(unit.kg_entregado), "kg_verificados": _number(unit.kg_verificados)})
        items.append(item)
    return _section("disponible" if items or armado else "sin_datos", items=items, armado=armado)
