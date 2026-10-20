"""Read-only Fabricación manga explorer.

This projection deliberately keeps the physical manga as the row identity.  The
origin OT supplies the operational date (continuity is explained by the detail
view), while the effective work/assignment projection supplies the current OF,
OT and work context.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font
from sqlalchemy import String, and_, cast, exists, or_, select
from sqlalchemy.orm import aliased, selectinload

from app.models.registro import RegistroDiarioProduccion
from app.models.scm_ot import (
    ScmAsignacionPersonalTrabajoOt,
    ScmCorreccionAsignacionManga,
    ScmCorreccionPesajeManga,
    ScmLoteArticulo,
    ScmManga,
    ScmPesajeManga,
    ScmTramoMangaTrabajo,
    ScmTrabajoOt,
    ScmTrabajoColor,
)
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenOperacion,
    ScmPlanProduccion,
)
from app.services.scm_manga_assignment_projection import (
    effective_assignment,
    effective_work,
)
from app.services.scm_manga_detail_service import _number
from app.services.scm_production_observability_service import _iso
from app.services.scm_service_support import ScmServiceError, load_actor


DEFAULT_LIMIT = 25
MAX_LIMIT = 100
COLOR_HEX_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")
OPEN_MANGA_STATES = {
    "PLANIFICADA", "PREETIQUETADA", "EN_ARMADO", "CONTINUIDAD_PENDIENTE", "EN_LLENADO",
}


def _parse_date(value, field):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_MANGA_EXPLORER_DATE",
            f"El filtro {field} debe usar YYYY-MM-DD.",
            status_code=400,
        ) from error


def _filters(raw=None):
    raw = dict(raw or {})
    start = _parse_date(raw.get("fecha_desde") or raw.get("desde"), "fecha_desde")
    end = _parse_date(raw.get("fecha_hasta") or raw.get("hasta"), "fecha_hasta")
    if start and end and start > end:
        raise ScmServiceError(
            "INVALID_MANGA_EXPLORER_DATE_RANGE",
            "fecha_desde no puede ser posterior a fecha_hasta.",
            status_code=400,
        )
    try:
        limit = int(raw.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_MANGA_EXPLORER_LIMIT",
            "limit debe ser un entero entre 1 y 100.",
            status_code=400,
        ) from error
    if not 1 <= limit <= MAX_LIMIT:
        raise ScmServiceError(
            "INVALID_MANGA_EXPLORER_LIMIT",
            "limit debe estar entre 1 y 100.",
            status_code=400,
        )
    tipo = str(raw.get("tipo_ot") or raw.get("tipo") or "FABRICACION").strip().upper()
    return {
        "fecha_desde": start,
        "fecha_hasta": end,
        "tipo_ot": tipo,
        "estado_manga": str(raw.get("estado_manga") or "").strip().upper() or None,
        "q": str(raw.get("q") or raw.get("busqueda") or "").strip().casefold() or None,
        "cursor": str(raw.get("cursor") or "").strip() or None,
        "limit": limit,
        "sort": str(raw.get("sort") or "FECHA_DESC").strip().upper(),
    }


def _scope(filters):
    return hashlib.sha256(json.dumps({
        key: value.isoformat() if isinstance(value, date) else value
        for key, value in filters.items() if key not in {"cursor", "limit"}
    }, sort_keys=True).encode()).hexdigest()[:16]


def _encode_cursor(*, operational_date, manga_id, as_of, scope):
    payload = {
        "d": operational_date.isoformat(),
        "id": int(manga_id),
        "as_of": _iso(as_of),
        "scope": scope,
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(value, *, scope):
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload.get("scope") != scope:
            raise ValueError("scope")
        return date.fromisoformat(payload["d"]), int(payload["id"]), datetime.fromisoformat(payload["as_of"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise ScmServiceError(
            "INVALID_MANGA_EXPLORER_CURSOR",
            "El cursor de mangas no es válido para estos filtros.",
            status_code=400,
        ) from error


def _canonical_color_hex(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value.upper() if COLOR_HEX_PATTERN.fullmatch(value) else None


def _worker_payload(assignment):
    worker = getattr(assignment, "trabajador", None) if assignment else None
    if worker is None:
        return None
    return {
        "id": worker.id,
        "codigo": worker.codigo,
        "nombre": worker.nombre_corto or worker.nombre_completo,
    }


def _resource(ot):
    machine = getattr(ot, "maquina", None)
    if machine is None:
        return None
    return {
        "tipo": "MAQUINA",
        "id": machine.id,
        "codigo": machine.codigo,
        "nombre": machine.nombre,
    }


def _upstream(work):
    operation = getattr(work, "orden_operacion", None) if work else None
    if operation is None:
        return {"op": None, "orden": None, "ordenes": [], "plan": None, "warnings": [
            {"codigo": "UPSTREAM_ORDEN_AUSENTE", "mensaje": "La manga no tiene OF resoluble."}
        ]}
    plan = getattr(operation, "plan_produccion", None)
    op = getattr(plan, "orden_produccion", None) if plan else None
    order = {
        "id": str(operation.id),
        "codigo": operation.codigo,
        "tipo": operation.tipo,
    }
    return {
        "op": {"id": str(op.id), "codigo": op.codigo} if op else None,
        "orden": order,
        "ordenes": [order],
        "plan": {"revision": plan.revision, "estado": plan.estado} if plan else None,
        "warnings": [] if plan is not None else [{
            "codigo": "UPSTREAM_PLAN_AUSENTE",
            "mensaje": "La OF no tiene un plan resoluble.",
        }],
    }


def _row(manga, origin_ot, *, vigente, visible_weights):
    work = effective_work(manga)
    effective_ot = getattr(work, "orden_trabajo", None) or origin_ot
    trabajo_color = getattr(work, "trabajo_color", None)
    color = getattr(trabajo_color, "color_nombre_snapshot", None)
    if color is None:
        color = manga.color_snapshot
    color_produccion = getattr(getattr(trabajo_color, "corrida", None), "color_produccion", None)
    color_hex = getattr(color_produccion, "hex_referencia", None)
    color_hex = _canonical_color_hex(color_hex)
    mold_code = getattr(trabajo_color, "molde_codigo_snapshot", None)
    article = getattr(getattr(manga, "lote_articulo", None), "articulo", None)
    labels = getattr(manga, "etiquetas", ()) or ()
    label = (
        {
            "id": str(labels[-1].public_id),
            "tipo": labels[-1].tipo,
            "version": labels[-1].version,
            "estado": labels[-1].estado,
            "plantilla_version": labels[-1].plantilla_version,
            "generated_at": _iso(labels[-1].generated_at),
            "printed_at": _iso(labels[-1].printed_at),
        }
        if labels and visible_weights else None
    )
    assignment = effective_assignment(manga)
    return {
        "manga": {
            "public_id": str(manga.public_id),
            "codigo": manga.codigo,
            "tipo": manga.tipo,
            "articulo": {
                "codigo": manga.articulo_codigo_snapshot,
                "nombre": manga.articulo_nombre_snapshot,
                "sku_pieza_color": manga.pieza_color_sku_snapshot,
                "unidad_inventario": getattr(article, "unidad_inventario", None),
            },
            "color": color,
            "color_hex": color_hex,
            "contenedor": {
                "codigo": manga.tipo_contenedor_codigo_snapshot,
                "nombre": manga.tipo_contenedor_nombre_snapshot,
            },
            "created_at": _iso(manga.created_at),
            "estado_operativo": manga.estado,
                "estado_logistico": "RECIBIDA_ALMACEN" if manga.estado == "RECIBIDA" else "NO_INGRESADA",
            "cantidad_objetivo_un": float(manga.cantidad_asignada_un) if manga.cantidad_asignada_un is not None else None,
            "cantidad_confirmada_un": float(manga.cantidad_confirmada_un) if manga.cantidad_confirmada_un is not None else None,
            "responsable": _worker_payload(assignment),
            "etiqueta": label,
            "pesaje": vigente,
            "almacen": None,
        },
        "ot": {
            "public_id": str(effective_ot.public_id),
            "codigo": effective_ot.codigo_ot,
            "tipo": "FABRICACION",
            "estado_documental": effective_ot.estado,
            "estado_operativo": getattr(work, "estado", None) or effective_ot.estado,
            # The explorer's date is explicitly the physical/origin OT date.
            "fecha_operativa": origin_ot.fecha.isoformat(),
            "turno": origin_ot.turno,
            "version": effective_ot.version,
        },
        "recurso": _resource(effective_ot),
        "upstream": _upstream(work),
        "trabajo": {
            "id": str(work.id) if work else None,
            "codigo": getattr(work, "codigo", None),
            "tipo": "COLOR" if work else None,
            "color": color,
            "molde_codigo": mold_code,
            "estado": getattr(work, "estado", None),
            "responsable": _worker_payload(assignment),
        },
        "visibilidad": {"pesaje": visible_weights, "inventario": False, "genealogia": False},
        "fecha_operativa_origen": origin_ot.fecha,
        "_manga_id": manga.id,
    }


def _collect(session, *, actor_id, raw_filters=None, paginate=True):
    filters = _filters(raw_filters)
    if filters["sort"] != "FECHA_DESC":
        raise ScmServiceError(
            "INVALID_MANGA_EXPLORER_SORT",
            "sort debe ser FECHA_DESC.",
            status_code=400,
        )
    if filters["tipo_ot"] not in {"FABRICACION", "OF"}:
        return {"items": [], "page": {"next_cursor": None, "limit": filters["limit"], "has_more": False}, "as_of": _iso(datetime.now(timezone.utc))}
    actor = load_actor(session, actor_id, capability="OT_VER")
    visible_weights = actor.tiene_capacidad("MANGA_PESAJE_VER")
    as_of = datetime.now(timezone.utc)
    scope = _scope(filters)
    cursor = _decode_cursor(filters["cursor"], scope=scope) if filters["cursor"] else None
    if cursor:
        as_of = cursor[2]
    stmt = select(ScmManga, RegistroDiarioProduccion).join(
        RegistroDiarioProduccion,
        RegistroDiarioProduccion.id == ScmManga.ot_id,
    ).outerjoin(
        ScmTrabajoOt,
        ScmTrabajoOt.id == ScmManga.trabajo_ot_id,
    ).outerjoin(
        ScmOrdenOperacion,
        ScmOrdenOperacion.id == ScmTrabajoOt.orden_operacion_id,
    ).where(
        RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
        RegistroDiarioProduccion.tipo_ot == "FABRICACION",
        ScmManga.created_at <= as_of,
    ).options(
        selectinload(ScmManga.etiquetas),
        selectinload(ScmManga.lote_articulo).selectinload(ScmLoteArticulo.articulo),
        selectinload(ScmManga.trabajo)
        .selectinload(ScmTrabajoOt.orden_trabajo),
        selectinload(ScmManga.trabajo)
        .selectinload(ScmTrabajoOt.orden_operacion)
        .selectinload(ScmOrdenOperacion.plan_produccion)
        .selectinload(ScmPlanProduccion.orden_produccion),
        selectinload(ScmManga.trabajo)
        .selectinload(ScmTrabajoOt.trabajo_color)
        .selectinload(ScmTrabajoColor.corrida)
        .selectinload(ScmCorridaFabricacion.color_produccion),
        selectinload(ScmManga.trabajo)
        .selectinload(ScmTrabajoOt.asignaciones_personal)
        .selectinload(ScmAsignacionPersonalTrabajoOt.trabajador),
        selectinload(ScmManga.asignacion_personal_trabajo)
        .selectinload(ScmAsignacionPersonalTrabajoOt.trabajador),
        selectinload(ScmManga.tramos_trabajo)
        .selectinload(ScmTramoMangaTrabajo.trabajo)
        .selectinload(ScmTrabajoOt.orden_trabajo),
        selectinload(ScmManga.tramos_trabajo)
        .selectinload(ScmTramoMangaTrabajo.trabajo)
        .selectinload(ScmTrabajoOt.orden_trabajo),
        selectinload(ScmManga.tramos_trabajo)
        .selectinload(ScmTramoMangaTrabajo.trabajo)
        .selectinload(ScmTrabajoOt.orden_operacion)
        .selectinload(ScmOrdenOperacion.plan_produccion)
        .selectinload(ScmPlanProduccion.orden_produccion),
        selectinload(ScmManga.tramos_trabajo)
        .selectinload(ScmTramoMangaTrabajo.trabajo)
        .selectinload(ScmTrabajoOt.trabajo_color)
        .selectinload(ScmTrabajoColor.corrida)
        .selectinload(ScmCorridaFabricacion.color_produccion),
        selectinload(ScmManga.tramos_trabajo)
        .selectinload(ScmTramoMangaTrabajo.asignacion_personal_trabajo)
        .selectinload(ScmAsignacionPersonalTrabajoOt.trabajador),
        selectinload(ScmManga.correccion_asignacion)
        .selectinload(ScmCorreccionAsignacionManga.destino_trabajo)
        .selectinload(ScmTrabajoOt.orden_trabajo),
        selectinload(ScmManga.correccion_asignacion)
        .selectinload(ScmCorreccionAsignacionManga.destino_trabajo)
        .selectinload(ScmTrabajoOt.orden_trabajo)
        .selectinload(RegistroDiarioProduccion.maquina),
        selectinload(ScmManga.correccion_asignacion)
        .selectinload(ScmCorreccionAsignacionManga.destino_trabajo)
        .selectinload(ScmTrabajoOt.orden_operacion)
        .selectinload(ScmOrdenOperacion.plan_produccion)
        .selectinload(ScmPlanProduccion.orden_produccion),
        selectinload(ScmManga.correccion_asignacion)
        .selectinload(ScmCorreccionAsignacionManga.destino_trabajo)
        .selectinload(ScmTrabajoOt.trabajo_color)
        .selectinload(ScmTrabajoColor.corrida)
        .selectinload(ScmCorridaFabricacion.color_produccion),
        selectinload(ScmManga.correccion_asignacion)
        .selectinload(ScmCorreccionAsignacionManga.destino_asignacion)
        .selectinload(ScmAsignacionPersonalTrabajoOt.trabajador),
        selectinload(ScmManga.correccion_asignacion)
        .selectinload(ScmCorreccionAsignacionManga.tramo_objetivo)
        .selectinload(ScmTramoMangaTrabajo.trabajo)
        .selectinload(ScmTrabajoOt.orden_trabajo),
        selectinload(ScmManga.correccion_asignacion)
        .selectinload(ScmCorreccionAsignacionManga.tramo_objetivo)
        .selectinload(ScmTramoMangaTrabajo.asignacion_personal_trabajo)
        .selectinload(ScmAsignacionPersonalTrabajoOt.trabajador),
    )
    if filters["fecha_desde"]:
        stmt = stmt.where(RegistroDiarioProduccion.fecha >= filters["fecha_desde"])
    if filters["fecha_hasta"]:
        stmt = stmt.where(RegistroDiarioProduccion.fecha <= filters["fecha_hasta"])
    if filters["estado_manga"]:
        stmt = stmt.where(ScmManga.estado == filters["estado_manga"])
    if filters["q"]:
        term = f"%{filters['q']}%"
        def work_match(work_model, operation_model, ot_model, color_model):
            return or_(
                work_model.codigo.ilike(term),
                operation_model.codigo.ilike(term),
                ot_model.codigo_ot.ilike(term),
                ot_model.maquina_codigo_snapshot.ilike(term),
                ot_model.maquina_nombre_snapshot.ilike(term),
                color_model.color_nombre_snapshot.ilike(term),
                color_model.molde_codigo_snapshot.ilike(term),
            )

        direct_work = aliased(ScmTrabajoOt)
        direct_operation = aliased(ScmOrdenOperacion)
        direct_ot = aliased(RegistroDiarioProduccion)
        direct_color = aliased(ScmTrabajoColor)
        direct_segment = aliased(ScmTramoMangaTrabajo)
        direct_correction = aliased(ScmCorreccionAsignacionManga)
        direct_context = exists(
            select(direct_work.id)
            .join(direct_operation, direct_operation.id == direct_work.orden_operacion_id)
            .join(direct_ot, direct_ot.id == direct_work.orden_trabajo_id)
            .outerjoin(direct_color, direct_color.trabajo_ot_id == direct_work.id)
            .where(
                direct_work.id == ScmManga.trabajo_ot_id,
                ~exists(select(direct_segment.id).where(
                    direct_segment.manga_id == ScmManga.id,
                )),
                ~exists(select(direct_correction.id).where(
                    direct_correction.manga_id == ScmManga.id,
                )),
                work_match(direct_work, direct_operation, direct_ot, direct_color),
            )
        )

        correction = aliased(ScmCorreccionAsignacionManga)
        correction_work = aliased(ScmTrabajoOt)
        correction_operation = aliased(ScmOrdenOperacion)
        correction_ot = aliased(RegistroDiarioProduccion)
        correction_color = aliased(ScmTrabajoColor)
        correction_target = aliased(ScmTramoMangaTrabajo)
        newer_target = aliased(ScmTramoMangaTrabajo)
        correction_active = or_(
            and_(
                correction.tramo_objetivo_id.is_(None),
                ~exists(select(newer_target.id).where(
                    newer_target.manga_id == correction.manga_id,
                    newer_target.created_at > correction.created_at,
                )),
            ),
            and_(
                correction.tramo_objetivo_id.is_not(None),
                ~exists(select(newer_target.id).where(
                    newer_target.manga_id == correction.manga_id,
                    newer_target.secuencia > correction_target.secuencia,
                )),
            ),
        )
        correction_context = exists(
            select(correction.id)
            .join(correction_work, correction_work.id == correction.destino_trabajo_ot_id)
            .join(correction_operation, correction_operation.id == correction_work.orden_operacion_id)
            .join(correction_ot, correction_ot.id == correction_work.orden_trabajo_id)
            .outerjoin(correction_color, correction_color.trabajo_ot_id == correction_work.id)
            .outerjoin(
                correction_target,
                correction_target.id == correction.tramo_objetivo_id,
            )
            .where(
                correction.manga_id == ScmManga.id,
                correction_active,
                work_match(correction_work, correction_operation, correction_ot, correction_color),
            )
        )

        latest_segment = aliased(ScmTramoMangaTrabajo)
        newer_segment = aliased(ScmTramoMangaTrabajo)
        latest_work = aliased(ScmTrabajoOt)
        latest_operation = aliased(ScmOrdenOperacion)
        latest_ot = aliased(RegistroDiarioProduccion)
        latest_color = aliased(ScmTrabajoColor)
        latest_correction = aliased(ScmCorreccionAsignacionManga)
        latest_correction_target = aliased(ScmTramoMangaTrabajo)
        latest_context = exists(
            select(latest_segment.id)
            .join(latest_work, latest_work.id == latest_segment.trabajo_ot_id)
            .join(latest_operation, latest_operation.id == latest_work.orden_operacion_id)
            .join(latest_ot, latest_ot.id == latest_work.orden_trabajo_id)
            .outerjoin(latest_color, latest_color.trabajo_ot_id == latest_work.id)
            .where(
                latest_segment.manga_id == ScmManga.id,
                ~exists(select(newer_segment.id).where(
                    newer_segment.manga_id == latest_segment.manga_id,
                    newer_segment.secuencia > latest_segment.secuencia,
                )),
                # A later real segment supersedes the correction overlay only
                # when its sequence is strictly newer than the anchored
                # segment.  Keep this correlation local to the manga so search
                # follows the same effective projection as the serializer.
                ~exists(
                    select(latest_correction.id)
                    .outerjoin(
                        latest_correction_target,
                        latest_correction_target.id == latest_correction.tramo_objetivo_id,
                    )
                    .where(
                        latest_correction.manga_id == ScmManga.id,
                        or_(
                            and_(
                                latest_correction.tramo_objetivo_id.is_not(None),
                                latest_correction_target.secuencia >= latest_segment.secuencia,
                            ),
                            and_(
                                latest_correction.tramo_objetivo_id.is_(None),
                                latest_correction.created_at >= latest_segment.created_at,
                            ),
                        ),
                    )
                ),
                work_match(latest_work, latest_operation, latest_ot, latest_color),
            )
        )
        origin_context = and_(
            ~exists(select(direct_segment.id).where(
                direct_segment.manga_id == ScmManga.id,
            )),
            ~exists(select(direct_correction.id).where(
                direct_correction.manga_id == ScmManga.id,
            )),
            or_(
                RegistroDiarioProduccion.codigo_ot.ilike(term),
                RegistroDiarioProduccion.maquina_codigo_snapshot.ilike(term),
                RegistroDiarioProduccion.maquina_nombre_snapshot.ilike(term),
            ),
        )
        stmt = stmt.where(or_(
            cast(ScmManga.public_id, String).ilike(term),
            ScmManga.codigo.ilike(term),
            ScmManga.articulo_codigo_snapshot.ilike(term),
            ScmManga.articulo_nombre_snapshot.ilike(term),
            ScmManga.pieza_color_sku_snapshot.ilike(term),
            ScmManga.color_snapshot.ilike(term),
            origin_context,
            direct_context,
            correction_context,
            latest_context,
        ))
    if cursor:
        stmt = stmt.where(or_(
            RegistroDiarioProduccion.fecha < cursor[0],
            and_(RegistroDiarioProduccion.fecha == cursor[0], ScmManga.id < cursor[1]),
        ))
    stmt = stmt.order_by(RegistroDiarioProduccion.fecha.desc(), ScmManga.id.desc())
    if paginate:
        stmt = stmt.limit(filters["limit"] + 1)
    rows = session.execute(stmt).all()
    manga_ids = [manga.id for manga, _origin_ot in rows]
    vigente_by_manga = {}
    if visible_weights and manga_ids:
        vigente_by_manga = {
            item.manga_id: item
            for item in session.scalars(
                select(ScmPesajeManga).where(
                    ScmPesajeManga.manga_id.in_(manga_ids),
                    ScmPesajeManga.estado == "VIGENTE",
                )
            ).all()
        }

    correction_by_pesaje = {}
    if vigente_by_manga:
        corrections = session.scalars(
            select(ScmCorreccionPesajeManga)
            .where(
                ScmCorreccionPesajeManga.pesaje_id.in_(
                    [item.id for item in vigente_by_manga.values()]
                ),
                ScmCorreccionPesajeManga.estado == "APLICADA",
            )
            .order_by(ScmCorreccionPesajeManga.id.desc())
        ).all()
        correction_by_pesaje = {
            item.pesaje_id: item
            for item in corrections
            if item.pesaje_id not in correction_by_pesaje
        }

    def serialize_vigente(manga, pesaje):
        if pesaje is None or manga.estado == "ANULADA":
            return None
        correction = correction_by_pesaje.get(pesaje.id)
        projection = dict(correction.result_projection_json) if correction else {
            "peso_fisico_neto_kg": format(pesaje.peso_fisico_neto_kg, "f"),
            "cantidad_confirmada": format(pesaje.cantidad_confirmada, "f"),
            "kg_produccion_ot": format(pesaje.kg_produccion_ot, "f"),
            "pesada_at": pesaje.pesada_at.isoformat(),
            "fecha_local_pesaje": pesaje.fecha_local_pesaje.isoformat(),
        }
        return {
            "pesaje_id": str(pesaje.public_id),
            "estado": "VIGENTE",
            "peso_fisico_neto_kg": _number(projection.get("peso_fisico_neto_kg")),
            "cantidad_confirmada": _number(projection.get("cantidad_confirmada")),
            "kg_produccion_ot": _number(projection.get("kg_produccion_ot")),
            "pesada_at": projection.get("pesada_at"),
            "fecha_local_pesaje": projection.get("fecha_local_pesaje"),
        }

    items = [
        _row(
            manga,
            origin_ot,
            vigente=serialize_vigente(manga, vigente_by_manga.get(manga.id)) if visible_weights else None,
            visible_weights=visible_weights,
        )
        for manga, origin_ot in rows
    ]
    has_more = paginate and len(items) > filters["limit"]
    visible = items[: filters["limit"]] if paginate else items
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        next_cursor = _encode_cursor(
            operational_date=last["fecha_operativa_origen"],
            manga_id=last["_manga_id"],
            as_of=as_of,
            scope=scope,
        )
    for item in visible:
        item.pop("fecha_operativa_origen", None)
        item.pop("_manga_id", None)
    return {
        "items": visible,
        "page": {"next_cursor": next_cursor, "limit": filters["limit"], "has_more": bool(has_more)},
        "as_of": _iso(as_of),
    }


def list_manga_explorer(session, *, actor_id, filters=None):
    return _collect(session, actor_id=actor_id, raw_filters=filters, paginate=True)


def _safe_cell(value):
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _export_rows(payload):
    detail_rows = []
    groups = {}
    for item in payload["items"]:
        manga = item["manga"]
        ot = item["ot"]
        article = manga["articulo"]
        resource = item.get("recurso") or {}
        weighing = manga.get("pesaje") or {}
        weights_visible = bool(item.get("visibilidad", {}).get("pesaje"))
        final_kg = weighing.get("peso_fisico_neto_kg") if weights_visible and weighing.get("estado") == "VIGENTE" else None
        weighing_state = (
            "RESTRINGIDO" if not weights_visible
            else ("FINAL_VIGENTE" if final_kg is not None else "SIN_PESAJE_VIGENTE")
        )
        row = {
            "Fecha operativa de origen": date.fromisoformat(ot["fecha_operativa"]),
            "Turno": ot.get("turno"), "Máquina": resource.get("nombre"), "Código máquina": resource.get("codigo"),
            "N°OF": (item.get("upstream", {}).get("orden") or {}).get("codigo"), "N°OT": ot.get("codigo"),
            "Pieza": article.get("nombre"), "Código pieza": article.get("codigo"), "Color": manga.get("color"),
            "Manga": manga.get("codigo"), "ID manga": manga.get("public_id"), "Estado": manga.get("estado_operativo"),
            "KG final vigente": final_kg, "Estado pesaje": weighing_state,
        }
        detail_rows.append(row)
        key = tuple(row[field] for field in ("Fecha operativa de origen", "Turno", "Máquina", "Código máquina", "N°OF", "Pieza", "Código pieza", "Color"))
        summary = groups.setdefault(key, {
            field: row[field]
            for field in ("Fecha operativa de origen", "Turno", "Máquina", "Código máquina", "N°OF", "Pieza", "Código pieza", "Color")
        })
        summary.setdefault("Mangas", 0)
        summary.setdefault("Mangas abiertas", 0)
        summary.setdefault("Mangas con pesaje restringido", 0)
        summary.setdefault("KG final vigente", 0.0)
        summary.setdefault("_has_final_kg", False)
        summary["Mangas"] += 1
        if not weights_visible:
            summary["Mangas con pesaje restringido"] += 1
        elif final_kg is None and manga.get("estado_operativo") in OPEN_MANGA_STATES:
            summary["Mangas abiertas"] += 1
        elif final_kg is not None:
            summary["KG final vigente"] += float(final_kg)
            summary["_has_final_kg"] = True
    summary_rows = list(groups.values())
    for row in summary_rows:
        if row["Mangas con pesaje restringido"]:
            row["Mangas abiertas"] = None
        if not row.pop("_has_final_kg", False):
            row["KG final vigente"] = None
    return summary_rows, detail_rows


def key_fields():
    return ["Fecha operativa de origen", "Turno", "Máquina", "Código máquina", "N°OF", "Pieza", "Código pieza", "Color", "Mangas", "Mangas abiertas", "Mangas con pesaje restringido", "KG final vigente"]


def generate_manga_explorer_xlsx(session, *, actor_id, filters=None):
    raw = dict(filters or {})
    mode = str(raw.pop("vista", raw.pop("modo", "AMBOS"))).strip().upper()
    if mode not in {"RESUMIDO", "DETALLADO", "AMBOS"}:
        raise ScmServiceError("INVALID_MANGA_EXPORT_MODE", "vista debe ser RESUMIDO, DETALLADO o AMBOS.", status_code=400)
    # Exports always represent the complete filtered physical set, never the
    # cursor page currently visible in the table.
    raw.pop("cursor", None)
    raw.pop("page", None)
    payload = _collect(session, actor_id=actor_id, raw_filters=raw, paginate=False)
    summary_rows, detail_rows = _export_rows(payload)
    workbook = Workbook()
    workbook.remove(workbook.active)
    if mode in {"RESUMIDO", "AMBOS"}:
        sheet = workbook.create_sheet("Resumen")
        headers = key_fields()
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet["A1"].comment = Comment("Cohorte por OT origen; no reparto diario de masa.", "EnvaPeru")
        for row in summary_rows:
            sheet.append([_safe_cell(row.get(header)) for header in headers])
    if mode in {"DETALLADO", "AMBOS"}:
        sheet = workbook.create_sheet("Detalle")
        headers = list(detail_rows[0]) if detail_rows else ["Fecha operativa de origen", "N°OF", "N°OT", "Manga", "KG final vigente"]
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet["A1"].comment = Comment("Cohorte por OT origen; no reparto diario de masa.", "EnvaPeru")
        for row in detail_rows:
            sheet.append([_safe_cell(row.get(header)) for header in headers])
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
