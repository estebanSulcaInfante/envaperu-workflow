"""Reportes de avance e histórico de producción.

Los reportes leen los hechos append-only de SCM. Una manga sólo puede
contribuir una vez: se prefiere su pesaje vigente corregido y, si no existe,
un cierre KG explícito respaldado por el último control. Los controles de una
manga abierta se exponen aparte y nunca se suman al cierre.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import or_, select

from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.scm_ot import (
    ScmAnulacionPesajeManga,
    ScmControlPesoManga,
    ScmCorreccionAsignacionManga,
    ScmCorreccionPesajeManga,
    ScmManga,
    ScmPesajeManga,
    ScmTramoMangaTrabajo,
    ScmTrabajoColor,
    ScmTrabajoOt,
)
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
)
from app.services.scm_production_observability_service import _text
from app.services.scm_service_support import ScmServiceError, load_actor
from app.services.scm_manga_assignment_projection import effective_work, effective_work_for_segment


KG = Decimal("0.001")
GROUP_OPTIONS = (
    "DIA", "MES", "OF", "CORRIDA", "COLOR", "OT", "RECURSO",
    "RESPONSABLE", "ARTICULO",
)
MEASURE_OPTIONS = ("PESO_KG", "MANGAS", "P_UNITARIO_G", "P_TEORICO_KG")


def _d(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        return None


def _n(value):
    return float(value) if value is not None else None


def _iso(value):
    return value.isoformat() if value is not None else None


def _date(value, field):
    if value in (None, ""):
        raise ScmServiceError(
            "OBSERVABILITY_DATE_REQUIRED",
            f"{field} es obligatorio.",
            status_code=400,
        )
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_DATE",
            f"{field} debe usar YYYY-MM-DD.",
            status_code=400,
        ) from error


def _filters(raw, *, require_dates=True):
    data = dict(raw or {})
    raw_start = data.get("fecha_desde") or data.get("desde")
    raw_end = data.get("fecha_hasta") or data.get("hasta")
    if require_dates or raw_start or raw_end:
        start = _date(raw_start, "fecha_desde")
        end = _date(raw_end, "fecha_hasta")
    else:
        start = date.min
        end = date.max
    if start > end:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_DATE_RANGE",
            "fecha_desde no puede ser posterior a fecha_hasta.",
            status_code=400,
        )
    groups = data.get("agrupaciones") or data.get("agrupar") or data.get("group_by")
    groups = [str(item).strip().upper() for item in str(groups).split(",")] if groups else ["DIA"]
    if any(item not in GROUP_OPTIONS for item in groups) or len(set(groups)) != len(groups):
        raise ScmServiceError("INVALID_OBSERVABILITY_GROUP", "Agrupación inválida.", status_code=400)
    measures = data.get("medidas") or data.get("measures") or data.get("measure")
    measures = [str(item).strip().upper() for item in str(measures).split(",")] if measures else list(MEASURE_OPTIONS)
    if any(item not in MEASURE_OPTIONS for item in measures) or len(set(measures)) != len(measures):
        raise ScmServiceError("INVALID_OBSERVABILITY_MEASURE", "Medida inválida.", status_code=400)
    return {
        "fecha_desde": start,
        "fecha_hasta": end,
        "q": _text(data.get("q")),
        "of": _text(data.get("of")),
        "corrida": _text(data.get("corrida")),
        "color": _text(data.get("color")),
        "ot": _text(data.get("ot")),
        "recurso": _text(data.get("recurso")),
        "responsable": _text(data.get("responsable")),
        "articulo": _text(data.get("articulo")),
        "estado_of": _text(data.get("estado_of"), upper=True),
        "estado_ot": _text(data.get("estado_ot"), upper=True),
        "groups": groups,
        "measures": measures,
    }


def _latest_by(values, key):
    result = {}
    for item in values:
        current = result.get(key(item))
        if current is None or item.id > current.id:
            result[key(item)] = item
    return result


def _effective_weight(manga, weighings, corrections, annulments):
    """Return one final physical fact, never a stale/reopened one."""
    candidates = [item for item in weighings if item.manga_id == manga.id]
    current = next((item for item in sorted(candidates, key=lambda value: value.id, reverse=True) if item.estado == "VIGENTE"), None)
    if current is None or current.id in annulments:
        return None
    correction = corrections.get(current.id)
    projection = correction.result_projection_json if correction else None
    return _d((projection or {}).get("peso_fisico_neto_kg", current.peso_fisico_neto_kg))


def _valid_kg_segments(manga, controls_by_tramo):
    segments = [
        segment for segment in manga.tramos_trabajo
        if segment.estado != "ANULADO"
        and _d(segment.cantidad_inicio_kg) is not None
        and _d(segment.cantidad_fin_kg) is not None
        and _d(segment.cantidad_atribuida_kg) is not None
        and str(segment.calidad_evidencia_kg or "").upper() in {
            "MEDIDA_DIRECTA", "MEDIDA_DIRECTA_CIERRE_CONTROL", "CONCILIADA"
        }
    ]
    segments.sort(key=lambda item: item.secuencia)
    return segments


def _segment_conciliates(segments, net):
    if not segments or net is None:
        return False
    previous = Decimal("0")
    total = Decimal("0")
    for segment in segments:
        start = _d(segment.cantidad_inicio_kg)
        end = _d(segment.cantidad_fin_kg)
        attributed = _d(segment.cantidad_atribuida_kg)
        if start != previous or end is None or end <= start or attributed != (end - start).quantize(KG):
            return False
        previous = end
        total += attributed
    return previous == net.quantize(KG) and total == net.quantize(KG)


def _load_rows(session, filters=None):
    """Load runs and their physical mangas in one normalized in-memory graph."""
    run_filters = filters or {}
    rows = session.execute(
        select(ScmCorridaFabricacion, ScmOrdenFabricacion, ScmOrdenOperacion)
        .join(ScmOrdenFabricacion, ScmOrdenFabricacion.orden_operacion_id == ScmCorridaFabricacion.orden_fabricacion_id)
        .join(ScmOrdenOperacion, ScmOrdenOperacion.id == ScmOrdenFabricacion.orden_operacion_id)
    ).all()
    runs = {
        corrida.id: {
            "corrida": corrida,
            "of": orden_fabricacion,
            "orden": orden,
            "works": [],
            "contexts": [],
            "ots": [],
            "mangas": {},
        }
        for corrida, orden_fabricacion, orden in rows
    }
    if not runs:
        return []
    work_rows = session.execute(
        select(ScmTrabajoOt, ScmTrabajoColor, RegistroDiarioProduccion)
        .join(ScmTrabajoColor, ScmTrabajoColor.trabajo_ot_id == ScmTrabajoOt.id)
        .join(RegistroDiarioProduccion, RegistroDiarioProduccion.id == ScmTrabajoOt.orden_trabajo_id)
        .where(ScmTrabajoColor.corrida_fabricacion_id.in_(list(runs)))
    ).all()
    work_ids = set()
    ot_ids = set()
    for work, color_work, ot in work_rows:
        run = runs[color_work.corrida_fabricacion_id]
        run["works"].append((work, color_work, ot))
        run["contexts"].append({"work": work, "color_work": color_work, "ot": ot})
        run["ots"].append(ot)
        work_ids.add(work.id)
        ot_ids.add(ot.id)

    mangas = session.scalars(
        select(ScmManga).where(
            or_(
                ScmManga.trabajo_ot_id.in_(work_ids) if work_ids else False,
                ScmManga.ot_id.in_(ot_ids) if ot_ids else False,
            )
        )
    ).all()
    if work_ids:
        mangas += session.scalars(
            select(ScmManga)
            .join(ScmTramoMangaTrabajo, ScmTramoMangaTrabajo.manga_id == ScmManga.id)
            .where(ScmTramoMangaTrabajo.trabajo_ot_id.in_(work_ids))
        ).all()
    correction_assignments = session.scalars(
        select(ScmCorreccionAsignacionManga).where(
            or_(
                ScmCorreccionAsignacionManga.destino_trabajo_ot_id.in_(work_ids) if work_ids else False,
                ScmCorreccionAsignacionManga.origen_trabajo_ot_id.in_(work_ids) if work_ids else False,
            )
        )
    ).all()
    mangas += [item.manga for item in correction_assignments if item.manga is not None]
    unique_mangas = {item.id: item for item in mangas}
    closure_events = {}
    if unique_mangas:
        closure_events = {
            str(event.aggregate_id): event
            for event in session.scalars(
                select(ScmEvento)
                .where(
                    ScmEvento.aggregate_type == "MANGA",
                    ScmEvento.tipo == "KG_MANGA_CLOSED_FROM_LAST_CONTROL",
                    ScmEvento.aggregate_id.in_([str(item.id) for item in unique_mangas.values()]),
                )
                .order_by(ScmEvento.occurred_at, ScmEvento.id)
            ).all()
        }
    for run in runs.values():
        run_work_ids = {item[0].id for item in run["works"]}
        for manga in unique_mangas.values():
            if manga.trabajo_ot_id in run_work_ids or any(
                segment.trabajo_ot_id in run_work_ids for segment in manga.tramos_trabajo
            ) or any(
                ot.id == manga.ot_id for ot in run["ots"]
            ):
                run["mangas"][manga.id] = manga

    weighing_rows = session.scalars(select(ScmPesajeManga).where(ScmPesajeManga.manga_id.in_(list(unique_mangas)))).all() if unique_mangas else []
    weighing_ids = [item.id for item in weighing_rows]
    corrections = session.scalars(
        select(ScmCorreccionPesajeManga).where(
            ScmCorreccionPesajeManga.pesaje_id.in_(weighing_ids),
            ScmCorreccionPesajeManga.estado == "APLICADA",
        ).order_by(ScmCorreccionPesajeManga.id)
    ).all() if weighing_ids else []
    correction_by_weight = _latest_by(corrections, lambda item: item.pesaje_id)
    annulments = session.scalars(
        select(ScmAnulacionPesajeManga).where(ScmAnulacionPesajeManga.pesaje_id.in_(weighing_ids))
    ).all() if weighing_ids else []
    annulment_ids = {item.pesaje_id for item in annulments}
    controls = session.scalars(
        select(ScmControlPesoManga).where(ScmControlPesoManga.manga_id.in_(list(unique_mangas))).order_by(ScmControlPesoManga.pesado_at, ScmControlPesoManga.id)
    ).all() if unique_mangas else []
    controls_by_manga = defaultdict(list)
    for control in controls:
        controls_by_manga[control.manga_id].append(control)
    for run in runs.values():
        if not run["mangas"]:
            continue
        for manga in run["mangas"].values():
            final = None if manga.estado == "ANULADA" else _effective_weight(manga, weighing_rows, correction_by_weight, annulment_ids)
            segments = _valid_kg_segments(manga, {})
            latest_control = controls_by_manga.get(manga.id, [])[-1] if controls_by_manga.get(manga.id) else None
            closure_event = None
            last_reopen = None
            if manga.estado != "ANULADA":
                closure_event = closure_events.get(str(manga.id))
                last_reopen = max((item.reabierta_at for item in manga.reaperturas if item.reabierta_at), default=None)
                if closure_event is not None and last_reopen is not None and closure_event.occurred_at <= last_reopen:
                    closure_event = None
            direct_close_segment = next(
                (segment for segment in reversed(segments)
                 if str(segment.calidad_evidencia_kg or "").upper() == "MEDIDA_DIRECTA_CIERRE_CONTROL"),
                None,
            )
            if direct_close_segment is not None and last_reopen is not None and direct_close_segment.cerrada_at is not None and direct_close_segment.cerrada_at <= last_reopen:
                direct_close_segment = None
            # A final from the last control is valid only with the append-only
            # closure event (or the explicit segment evidence written by that
            # operation). Manga state alone is never closure evidence.
            if manga.estado != "ANULADA" and final is None and latest_control is not None and (closure_event is not None or direct_close_segment is not None):
                final = _d((direct_close_segment or latest_control).cantidad_fin_kg if direct_close_segment is not None else latest_control.peso_neto_kg)
            open_kg = _d(latest_control.peso_neto_kg) if final is None and latest_control is not None and manga.estado != "ANULADA" else None
            manga._report_final_kg = final
            manga._report_open_kg = open_kg
            manga._report_segments = segments
            manga._report_controls = controls_by_manga.get(manga.id, [])
            manga._report_closure_event = closure_event
    result = []
    for run in runs.values():
        contexts = run["contexts"]
        ot = sorted((item["ot"] for item in contexts), key=lambda item: item.fecha)[0] if contexts else None
        work = contexts[0]["work"] if contexts else None
        color_work = contexts[0]["color_work"] if contexts else None
        color_name = run["corrida"].color_produccion.nombre if run["corrida"].color_produccion else (color_work.color_nombre_snapshot if color_work else None)
        resource = (ot.maquina_nombre_snapshot or ot.maquina_codigo_snapshot) if ot is not None else None
        responsible = ot.responsable.nombre_completo if ot is not None and ot.responsable else None
        record = {
            **run,
            "ot": ot,
            "work": work,
            "color_work": color_work,
            "color_name": color_name,
            "resource": resource,
            "responsible": responsible,
        }
        if not _matches(record, run_filters):
            continue
        result.append(record)
    return result


def _matches(run, filters):
    if not filters:
        return True
    ot = run["ot"]
    corrida = run["corrida"]
    order = run["orden"]
    values = [str(item or "").lower() for item in (run["color_name"], run["resource"], run["responsible"], corrida.codigo, order.codigo, ot.codigo_ot if ot else None, run["work"].codigo if run["work"] else None)]
    contexts = run.get("contexts") or ([{"work": run.get("work"), "color_work": run.get("color_work"), "ot": ot}] if ot else [])
    if contexts and not any(filters["fecha_desde"] <= item["ot"].fecha <= filters["fecha_hasta"] for item in contexts):
        return False
    if filters["of"] and not any(filters["of"].lower() in str(item["ot"].orden_operacion.codigo if item["ot"].orden_operacion else order.codigo).lower() for item in contexts):
        return False
    if filters["corrida"] and filters["corrida"].lower() not in corrida.codigo.lower() and filters["corrida"].lower() not in str(corrida.id).lower():
        return False
    checks = (
        ("color", run["color_name"]),
        ("ot", tuple(item["ot"].codigo_ot for item in contexts)),
        ("recurso", tuple(item["ot"].maquina_nombre_snapshot or item["ot"].maquina_codigo_snapshot for item in contexts)),
        ("responsable", tuple(item["ot"].responsable.nombre_completo if item["ot"].responsable else None for item in contexts)),
    )
    for key, value in checks:
        if filters[key] and filters[key].lower() not in str(value or "").lower():
            return False
    if filters["estado_of"] and filters["estado_of"] != order.estado:
        return False
    if filters["estado_ot"] and (ot is None or filters["estado_ot"] != ot.estado):
        return False
    if filters["articulo"] and not any(filters["articulo"].lower() in str(m.articulo_codigo_snapshot or "").lower() for m in run["mangas"].values()):
        return False
    if filters["q"] and not any(filters["q"].lower() in value for value in values):
        return False
    return True


def _run_manga_values(run):
    final = sum((_d(item._report_final_kg) or Decimal("0") for item in run["mangas"].values()), Decimal("0"))
    opened = sum((_d(item._report_open_kg) or Decimal("0") for item in run["mangas"].values()), Decimal("0"))
    known = sum(item._report_final_kg is not None or item._report_open_kg is not None for item in run["mangas"].values())
    total = len(run["mangas"])
    complete = total > 0 and total == known
    objective = _d(run["corrida"].objetivo_neto_kg)
    measured = final + opened if complete else None
    return final, opened, measured, total, known, objective


def list_production_progress(session, *, actor_id, filters=None):
    actor = load_actor(session, actor_id, capability="OT_VER")
    visible = actor.tiene_capacidad("MANGA_PESAJE_VER")
    normalized = _filters(filters, require_dates=False)
    runs = _load_rows(session, normalized)
    items = []
    for run in runs:
        final, opened, measured, total, known, objective = _run_manga_values(run)
        if not visible:
            final = opened = measured = None
            known = 0
        coverage = "COMPLETA" if total == known and visible else "INCOMPLETA"
        percent = ((final / objective) * 100) if objective and coverage == "COMPLETA" else None
        remaining = (objective - final) if objective is not None and coverage == "COMPLETA" else None
        items.append({
            "corrida_id": str(run["corrida"].id),
            "corrida": run["corrida"].codigo,
            "of": run["orden"].codigo,
            "ot": run["ot"].codigo_ot if run["ot"] is not None else None,
            "color": run["color_name"],
            "objetivo_neto_kg": _n(objective),
            "kg_finalizados_efectivos": _n(final),
            "kg_medidos_en_abiertas": _n(opened),
            "kg_medidos_efectivos": _n(measured),
            "restante_kg": _n(remaining),
            "porcentaje": _n(percent),
            "mangas": {"total": total, "conocidas": known},
            "subtotal_conocido_kg": _n(final + opened) if visible else None,
            "coverage": {"estado": coverage, "mangas_total": total, "mangas_conocidas": known, "motivos": ([] if coverage == "COMPLETA" else ["MANGA_PESAJE_VER_REQUERIDO" if not visible else "PESO_MANGA_SIN_EVIDENCIA"])},
            "criterio_uniformidad": "Criterio de uniformidad no definido",
        })
    return {"items": items, "as_of": date.today().isoformat(), "visibilidad": {"pesaje": visible, "restriccion": None if visible else "MANGA_PESAJE_VER requerido para ver pesos"}}


def _run_group_value(run, name):
    ot = run["ot"]
    corrida = run["corrida"]
    return {
        "DIA": _iso(ot.fecha) if ot else None,
        "MES": ot.fecha.strftime("%Y-%m") if ot else None,
        "OF": run["orden"].codigo,
        "CORRIDA": corrida.codigo,
        "COLOR": run["color_name"],
        "OT": ot.codigo_ot if ot else None,
        "RECURSO": run["resource"],
        "RESPONSABLE": run["responsible"],
        "ARTICULO": next((m.articulo_codigo_snapshot for m in run["mangas"].values()), None),
    }[name]


def _context_for_work(run, work):
    if work is None:
        return None
    return next((item for item in run.get("contexts", ()) if item["work"].id == work.id), None)


def _context_group_value(run, context, name):
    context = context or {"ot": run["ot"], "work": run.get("work"), "color_work": run.get("color_work")}
    ot = context["ot"]
    corrida = run["corrida"]
    return {
        "DIA": _iso(ot.fecha),
        "MES": ot.fecha.strftime("%Y-%m"),
        "OF": run["orden"].codigo,
        "CORRIDA": corrida.codigo,
        "COLOR": run["color_name"],
        "OT": ot.codigo_ot,
        "RECURSO": ot.maquina_nombre_snapshot or ot.maquina_codigo_snapshot,
        "RESPONSABLE": ot.responsable.nombre_completo if ot.responsable else None,
        "ARTICULO": None,
    }[name]


def _context_matches(run, context, manga, filters):
    if not filters:
        return True
    ot = context["ot"]
    work = context["work"]
    values = [
        str(value or "").lower()
        for value in (
            run["color_name"], ot.codigo_ot, ot.maquina_nombre_snapshot,
            ot.maquina_codigo_snapshot, ot.responsable.nombre_completo if ot.responsable else None,
            run["corrida"].codigo, run["orden"].codigo, work.codigo if work else None,
            manga.articulo_codigo_snapshot,
        )
    ]
    if not filters["fecha_desde"] <= ot.fecha <= filters["fecha_hasta"]:
        return False
    checks = (
        ("of", run["orden"].codigo), ("corrida", run["corrida"].codigo),
        ("color", run["color_name"]), ("ot", ot.codigo_ot),
        ("recurso", ot.maquina_nombre_snapshot or ot.maquina_codigo_snapshot),
        ("responsable", ot.responsable.nombre_completo if ot.responsable else None),
        ("articulo", manga.articulo_codigo_snapshot),
    )
    for key, value in checks:
        if filters[key] and filters[key].lower() not in str(value or "").lower():
            return False
    return not filters["q"] or any(filters["q"].lower() in value for value in values)


def _history_rows(runs, groups, filters=None):
    rows = []
    seen_segments = set()
    theoretical_emitted = set()
    for run in runs:
        for manga in run["mangas"].values():
            net = _d(manga._report_final_kg)
            if net is None:
                continue
            segments = manga._report_segments
            valid = _segment_conciliates(segments, net)
            segment_values = []
            if valid:
                for segment in segments:
                    if segment.id in seen_segments:
                        continue
                    owner = effective_work_for_segment(manga, segment)
                    context = _context_for_work(run, owner)
                    if context is not None and _context_matches(run, context, manga, filters):
                        seen_segments.add(segment.id)
                        segment_values.append((run, context, _d(segment.cantidad_atribuida_kg)))
            else:
                # Without KG-evidence segments, a single unambiguous run can
                # receive the complete NET. Existing but broken segments are
                # deliberately left unattributed; a correction must not be
                # hidden by a fallback to the run total.
                owner = effective_work(manga) if not segments else None
                context = _context_for_work(run, owner)
                segment_values = [(run, context, net)] if context is not None and not segments and _context_matches(run, context, manga, filters) else []
            if not segment_values:
                values = {group: _run_group_value(run, group) for group in GROUP_OPTIONS}
                rows.append({**values, "PESO_KG": None, "SUBTOTAL_CONOCIDO_KG": _n(net), "MANGAS": 0, "P_UNITARIO_G": None, "P_TEORICO_KG": None, "_known": False, "_manga_id": manga.id})
            for segment_run, context, kg in segment_values:
                values = {group: _context_group_value(segment_run, context, group) for group in GROUP_OPTIONS}
                values["ARTICULO"] = manga.articulo_codigo_snapshot
                unit = _d(context["color_work"].peso_neto_snapshot_g) if context and context["color_work"] else _d(manga.peso_unitario_snapshot_g)
                quantity = _d(manga.cantidad_confirmada_un or manga.cantidad_asignada_un)
                theoretical = (unit * quantity / Decimal("1000")) if unit is not None and quantity is not None else None
                theoretical_key = (manga.id, tuple(values.get(group) for group in groups))
                emit_theoretical = theoretical_key not in theoretical_emitted
                theoretical_emitted.add(theoretical_key)
                rows.append({**values, "PESO_KG": _n(kg), "SUBTOTAL_CONOCIDO_KG": _n(net), "MANGAS": 1 if emit_theoretical else 0, "P_UNITARIO_G": _n(unit) if emit_theoretical else None, "P_UNITARIO_WEIGHT": _n(unit * quantity) if emit_theoretical and unit is not None and quantity is not None else None, "P_UNITARIO_QTY": _n(quantity) if emit_theoretical and quantity is not None else None, "P_TEORICO_KG": _n(theoretical) if emit_theoretical else None, "_known": True, "_manga_id": manga.id})
    return rows


def list_production_history(session, *, actor_id, filters=None):
    actor = load_actor(session, actor_id, capability="OT_VER")
    normalized = _filters(filters, require_dates=True)
    visible = actor.tiene_capacidad("MANGA_PESAJE_VER")
    runs = _load_rows(session, normalized)
    rows = _history_rows(runs, normalized["groups"], normalized) if visible else []
    grouped = {}
    for row in rows:
        key = tuple(row[group] for group in normalized["groups"])
        item = grouped.setdefault(key, {group: row[group] for group in normalized["groups"]} | {"PESO_KG": Decimal("0"), "SUBTOTAL_CONOCIDO_KG": Decimal("0"), "MANGAS": 0, "P_UNITARIO_WEIGHT": Decimal("0"), "P_UNITARIO_QTY": Decimal("0"), "P_TEORICO_KG": Decimal("0"), "coverage": "COMPLETA", "_has_unknown": False})
        item["SUBTOTAL_CONOCIDO_KG"] += _d(row.get("SUBTOTAL_CONOCIDO_KG")) or Decimal("0")
        if row["PESO_KG"] is not None:
            item["PESO_KG"] += _d(row["PESO_KG"]) or Decimal("0")
        item["MANGAS"] += row["MANGAS"]
        if row.get("P_UNITARIO_WEIGHT") is not None:
            item["P_UNITARIO_WEIGHT"] += _d(row["P_UNITARIO_WEIGHT"])
            item["P_UNITARIO_QTY"] += _d(row["P_UNITARIO_QTY"])
        item["P_TEORICO_KG"] += _d(row["P_TEORICO_KG"]) or Decimal("0")
        if not row["_known"]:
            item["coverage"] = "INCOMPLETA"
            item["_has_unknown"] = True
    items = []
    for item in grouped.values():
        qty = item.pop("P_UNITARIO_QTY")
        weighted = item.pop("P_UNITARIO_WEIGHT")
        item["P_UNITARIO_G"] = _n(weighted / qty) if qty else None
        item["PESO_KG"] = None if item.pop("_has_unknown") else _n(item["PESO_KG"])
        item["SUBTOTAL_CONOCIDO_KG"] = _n(item["SUBTOTAL_CONOCIDO_KG"])
        item["P_TEORICO_KG"] = _n(item["P_TEORICO_KG"])
        items.append(item)
    return {"items": items, "grouping_options": list(GROUP_OPTIONS), "grouped_by": normalized["groups"], "filters": {key: value.isoformat() if isinstance(value, date) else value for key, value in normalized.items() if key != "groups"}, "visibilidad": {"pesaje": visible}}


def generate_production_history_xlsx(session, *, actor_id, filters=None):
    actor = load_actor(session, actor_id, capability="OT_VER")
    if not actor.tiene_capacidad("MANGA_PESAJE_VER"):
        raise ScmServiceError(
            "MANGA_PESAJE_VER_REQUIRED",
            "MANGA_PESAJE_VER es obligatorio para exportar el histórico.",
            status_code=403,
        )
    payload = list_production_history(session, actor_id=actor_id, filters=filters)
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Resumen"
    summary.append(["Agrupado por", ", ".join(payload["grouped_by"])])
    summary.append(["Peso efectivo (kg)", sum((row.get("PESO_KG") or 0 for row in payload["items"]), 0)])
    summary.append(["Filas", len(payload["items"])])
    data = workbook.create_sheet("Datos")
    headers = list(payload["grouped_by"]) + ["PESO_KG", "SUBTOTAL_CONOCIDO_KG", "MANGAS", "P_UNITARIO_G", "P_TEORICO_KG", "coverage"]
    data.append(headers)
    for row in payload["items"]:
        data.append([row.get(header) for header in headers])
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
