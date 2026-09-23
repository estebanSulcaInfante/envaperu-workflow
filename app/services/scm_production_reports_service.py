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


KG = Decimal("0.001")
GROUP_OPTIONS = (
    "DIA", "MES", "OF", "CORRIDA", "COLOR", "OT", "RECURSO",
    "RESPONSABLE", "ARTICULO",
)


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


def _filters(raw):
    data = dict(raw or {})
    start = _date(data.get("fecha_desde") or data.get("desde"), "fecha_desde")
    end = _date(data.get("fecha_hasta") or data.get("hasta"), "fecha_hasta")
    if start > end:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_DATE_RANGE",
            "fecha_desde no puede ser posterior a fecha_hasta.",
            status_code=400,
        )
    groups = data.get("agrupaciones") or data.get("agrupar") or data.get("group_by")
    groups = [str(item).strip().upper() for item in str(groups).split(",")] if groups else ["DIA"]
    groups = [item for item in groups if item in GROUP_OPTIONS]
    if not groups:
        groups = ["DIA"]
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
        and str(segment.calidad_evidencia_kg or "").upper() in {"MEDIDA_DIRECTA", "CONCILIADA"}
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
            final = _effective_weight(manga, weighing_rows, correction_by_weight, annulment_ids)
            segments = _valid_kg_segments(manga, {})
            closed_segment = segments[-1] if segments and _d(segments[-1].cantidad_fin_kg) is not None else None
            if final is None and closed_segment is not None and manga.estado not in {"PLANIFICADA", "PREETIQUETADA", "EN_ARMADO", "CONTINUIDAD_PENDIENTE", "EN_LLENADO"}:
                final = _d(closed_segment.cantidad_fin_kg)
            latest_control = controls_by_manga.get(manga.id, [])[-1] if controls_by_manga.get(manga.id) else None
            if final is None and latest_control is not None and manga.estado not in {"PLANIFICADA", "PREETIQUETADA", "EN_ARMADO", "CONTINUIDAD_PENDIENTE", "EN_LLENADO"}:
                # Explicit KG closure can persist the last control without a
                # ScmPesajeManga row. The manga state is the closure marker.
                final = _d(latest_control.peso_neto_kg)
            open_kg = _d(latest_control.peso_neto_kg) if final is None and latest_control is not None else None
            manga._report_final_kg = final
            manga._report_open_kg = open_kg
            manga._report_segments = segments
            manga._report_controls = controls_by_manga.get(manga.id, [])
    result = []
    for run in runs.values():
        ots = run["ots"]
        ot = sorted(ots, key=lambda item: item.fecha)[0] if ots else None
        work = run["works"][0][0] if run["works"] else None
        color_work = run["works"][0][1] if run["works"] else None
        if ot is None:
            continue
        color_name = run["corrida"].color_produccion.nombre if run["corrida"].color_produccion else (color_work.color_nombre_snapshot if color_work else None)
        resource = ot.maquina_nombre_snapshot or ot.maquina_codigo_snapshot if ot.maquina_nombre_snapshot or ot.maquina_codigo_snapshot else None
        responsible = ot.responsable.nombre_completo if ot.responsable else None
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
    values = [str(item or "").lower() for item in (run["color_name"], run["resource"], run["responsible"], corrida.codigo, order.codigo, ot.codigo_ot, run["work"].codigo if run["work"] else None)]
    if not (filters["fecha_desde"] <= ot.fecha <= filters["fecha_hasta"]):
        return False
    if filters["of"] and filters["of"].lower() not in order.codigo.lower():
        return False
    if filters["corrida"] and filters["corrida"].lower() not in corrida.codigo.lower() and filters["corrida"].lower() not in str(corrida.id).lower():
        return False
    checks = (("color", run["color_name"]), ("ot", ot.codigo_ot), ("recurso", run["resource"]), ("responsable", run["responsible"]))
    for key, value in checks:
        if filters[key] and filters[key].lower() not in str(value or "").lower():
            return False
    if filters["estado_of"] and filters["estado_of"] != order.estado:
        return False
    if filters["estado_ot"] and filters["estado_ot"] != ot.estado:
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
    complete = total == known
    objective = _d(run["corrida"].objetivo_neto_kg)
    measured = final + opened if complete else None
    return final, opened, measured, total, known, objective


def list_production_progress(session, *, actor_id, filters=None):
    actor = load_actor(session, actor_id, capability="OT_VER")
    visible = actor.tiene_capacidad("MANGA_PESAJE_VER")
    runs = _load_rows(session)
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
            "ot": run["ot"].codigo_ot,
            "color": run["color_name"],
            "objetivo_neto_kg": _n(objective),
            "kg_finalizados_efectivos": _n(final),
            "kg_medidos_en_abiertas": _n(opened),
            "kg_medidos_efectivos": _n(measured),
            "restante_kg": _n(remaining),
            "porcentaje": _n(percent),
            "mangas": {"total": total, "conocidas": known},
            "coverage": {"estado": coverage, "mangas_total": total, "mangas_conocidas": known},
            "criterio_uniformidad": "Criterio de uniformidad no definido",
        })
    return {"items": items, "as_of": date.today().isoformat(), "visibilidad": {"pesaje": visible}}


def _run_group_value(run, name):
    ot = run["ot"]
    corrida = run["corrida"]
    return {
        "DIA": _iso(ot.fecha),
        "MES": ot.fecha.strftime("%Y-%m"),
        "OF": run["orden"].codigo,
        "CORRIDA": corrida.codigo,
        "COLOR": run["color_name"],
        "OT": ot.codigo_ot,
        "RECURSO": run["resource"],
        "RESPONSABLE": run["responsible"],
        "ARTICULO": next((m.articulo_codigo_snapshot for m in run["mangas"].values()), None),
    }[name]


def _history_rows(runs, groups):
    rows = []
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
                    segment_run = run
                    segment_values.append((segment_run, _d(segment.cantidad_atribuida_kg)))
            else:
                # Without KG-evidence segments, a single unambiguous run can
                # receive the complete NET. Existing but broken segments are
                # deliberately left unattributed; a correction must not be
                # hidden by a fallback to the run total.
                segment_values = [(run, net)] if not segments else []
            if not segment_values:
                values = {group: _run_group_value(run, group) for group in GROUP_OPTIONS}
                rows.append({**values, "PESO_KG": None, "MANGAS": 0, "P_UNITARIO_G": None, "P_TEORICO_KG": None, "_known": False})
            for segment_run, kg in segment_values:
                values = {group: _run_group_value(segment_run, group) for group in GROUP_OPTIONS}
                unit = _d(run["color_work"].peso_neto_snapshot_g) if run["color_work"] else None
                quantity = _d(manga.cantidad_confirmada_un or manga.cantidad_asignada_un)
                theoretical = (unit * quantity / Decimal("1000")) if unit is not None and quantity is not None else None
                rows.append({**values, "PESO_KG": _n(kg), "MANGAS": 1, "P_UNITARIO_G": _n(unit), "P_TEORICO_KG": _n(theoretical), "_known": valid or not segments})
    return rows


def list_production_history(session, *, actor_id, filters=None):
    actor = load_actor(session, actor_id, capability="OT_VER")
    normalized = _filters(filters)
    visible = actor.tiene_capacidad("MANGA_PESAJE_VER")
    runs = _load_rows(session, normalized)
    rows = _history_rows(runs, normalized["groups"]) if visible else []
    grouped = {}
    for row in rows:
        key = tuple(row[group] for group in normalized["groups"])
        item = grouped.setdefault(key, {group: row[group] for group in normalized["groups"]} | {"PESO_KG": Decimal("0"), "MANGAS": 0, "P_UNITARIO_G": [], "P_TEORICO_KG": Decimal("0"), "coverage": "COMPLETA"})
        item["PESO_KG"] += _d(row["PESO_KG"]) or Decimal("0")
        item["MANGAS"] += row["MANGAS"]
        if row["P_UNITARIO_G"] is not None:
            item["P_UNITARIO_G"].append(_d(row["P_UNITARIO_G"]))
        item["P_TEORICO_KG"] += _d(row["P_TEORICO_KG"]) or Decimal("0")
        if not row["_known"]:
            item["coverage"] = "INCOMPLETA"
    items = []
    for item in grouped.values():
        units = item.pop("P_UNITARIO_G")
        item["P_UNITARIO_G"] = _n(sum(units, Decimal("0")) / len(units)) if units else None
        item["PESO_KG"] = _n(item["PESO_KG"])
        item["P_TEORICO_KG"] = _n(item["P_TEORICO_KG"])
        items.append(item)
    return {"items": items, "grouping_options": list(GROUP_OPTIONS), "grouped_by": normalized["groups"], "filters": {key: value.isoformat() if isinstance(value, date) else value for key, value in normalized.items() if key != "groups"}, "visibilidad": {"pesaje": visible}}


def generate_production_history_xlsx(session, *, actor_id, filters=None):
    payload = list_production_history(session, actor_id=actor_id, filters=filters)
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Resumen"
    summary.append(["Agrupado por", ", ".join(payload["grouped_by"])])
    summary.append(["Peso efectivo (kg)", sum((row.get("PESO_KG") or 0 for row in payload["items"]), 0)])
    summary.append(["Filas", len(payload["items"])])
    data = workbook.create_sheet("Datos")
    headers = list(payload["grouped_by"]) + ["PESO_KG", "MANGAS", "P_UNITARIO_G", "P_TEORICO_KG", "coverage"]
    data.append(headers)
    for row in payload["items"]:
        data.append([row.get(header) for header in headers])
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
