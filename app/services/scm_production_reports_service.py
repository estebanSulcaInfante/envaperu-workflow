"""Reportes de avance e histórico de producción.

Los reportes leen los hechos append-only de SCM. Una manga sólo puede
contribuir una vez: se prefiere su pesaje vigente corregido y, si no existe,
un cierre KG explícito respaldado por el último control. Los controles de una
manga abierta se exponen aparte y nunca se suman al cierre.
"""

from __future__ import annotations

from collections import defaultdict
from copy import copy
from datetime import date
from decimal import Decimal
from io import BytesIO
import json

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
from app.models.molde import Molde
from app.services.scm_production_observability_service import _text
from app.services.scm_service_support import ScmServiceError, load_actor
from app.services.scm_manga_assignment_projection import effective_work, effective_work_for_segment


KG = Decimal("0.001")
GROUP_OPTIONS = (
    "DIA", "MES", "OF", "CORRIDA", "COLOR", "OT", "RECURSO",
    "RESPONSABLE", "MOLDE", "PIEZA", "ARTICULO",
)
MEASURE_OPTIONS = ("PESO_KG", "MANGAS", "P_UNITARIO_G", "P_TEORICO_KG")


def _history_weight_summary(items, measures):
    if "PESO_KG" not in measures:
        return "No solicitado", None
    known_weights = [row.get("PESO_KG") for row in items if row.get("PESO_KG") is not None]
    unknown_count = len(items) - len(known_weights)
    coverage = "Completa" if unknown_count == 0 else f"Parcial: {unknown_count} fila(s) sin peso"
    return (sum(known_weights, 0) if known_weights else None), coverage


def _d(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        return None


def _n(value):
    return float(value) if value is not None else None


def _history_color_identity(row):
    """Return the canonical color identity carried by one history row."""
    return (
        row.get("_COLOR_ID"),
        row.get("COLOR_CODIGO"),
        row.get("COLOR_HEX"),
        row.get("COLOR"),
    )


def _history_color_metadata(rows):
    """Expose a swatch only when every row has one unambiguous color identity."""
    identities = {_history_color_identity(row) for row in rows}
    if len(identities) != 1:
        return None, None
    identity = next(iter(identities))
    return identity[1], identity[2]


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
    group_key = next((key for key in ("agrupaciones", "agrupar", "group_by") if key in data), None)
    groups = data.get(group_key) if group_key else None
    groups = [str(item).strip().upper() for item in str(groups).split(",")] if groups else (["DIA"] if group_key is None else [])
    if any(item not in GROUP_OPTIONS for item in groups) or len(set(groups)) != len(groups):
        raise ScmServiceError("INVALID_OBSERVABILITY_GROUP", "Agrupación inválida.", status_code=400)
    measure_key = next((key for key in ("medidas", "measures", "measure") if key in data), None)
    measures = data.get(measure_key) if measure_key else None
    if measure_key and not measures:
        raise ScmServiceError("INVALID_OBSERVABILITY_MEASURE", "Debe seleccionar al menos una medida.", status_code=400)
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


def _project_corrected_kg_segments(segments, net):
    """Project a corrected final NET onto the final closed segment only."""
    if not segments or net is None:
        return segments
    ordered = sorted(segments, key=lambda item: item.secuencia)
    previous = Decimal("0")
    for segment in ordered:
        start = _d(segment.cantidad_inicio_kg)
        end = _d(segment.cantidad_fin_kg)
        attributed = _d(segment.cantidad_atribuida_kg)
        if (
            start != previous
            or end is None
            or end <= start
            or attributed != (end - start).quantize(KG)
        ):
            return segments
        previous = end
    final_net = net.quantize(KG)
    last = ordered[-1]
    start = _d(last.cantidad_inicio_kg)
    if final_net == _d(last.cantidad_fin_kg):
        return segments
    if start is None or final_net <= start:
        return segments
    projected = copy(last)
    projected.cantidad_fin_kg = final_net
    projected.cantidad_atribuida_kg = (final_net - start).quantize(KG)
    projected.calidad_evidencia_kg = "CONCILIADA"
    return [*ordered[:-1], projected]


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
            "molde": None,
            "moldes": {},
        }
        for corrida, orden_fabricacion, orden in rows
    }
    if not runs:
        return []
    mold_ids = {run["of"].molde_id for run in runs.values() if run["of"].molde_id}
    molds = session.scalars(select(Molde).where(Molde.codigo.in_(mold_ids))).all() if mold_ids else []
    mold_by_code = {mold.codigo: mold for mold in molds}
    for run in runs.values():
        run["molde"] = mold_by_code.get(run["of"].molde_id)
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
    snapshot_mold_ids = {
        getattr(color_work, "molde_codigo_snapshot", None)
        for _work, color_work, _ot in work_rows
        if getattr(color_work, "molde_codigo_snapshot", None)
    }
    all_mold_ids = mold_ids | snapshot_mold_ids
    all_molds = session.scalars(select(Molde).where(Molde.codigo.in_(all_mold_ids))).all() if all_mold_ids else []
    mold_by_code.update({mold.codigo: mold for mold in all_molds})
    for run in runs.values():
        run["moldes"] = mold_by_code

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
            current_weighing = next(
                (
                    item for item in sorted(
                        (row for row in weighing_rows if row.manga_id == manga.id),
                        key=lambda value: value.id,
                        reverse=True,
                    )
                    if item.estado == "VIGENTE"
                ),
                None,
            )
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
            # The direct segment marker corroborates the append-only event;
            # it can never create a final by itself.  The event payload keeps
            # the source control identity when the closure operation supplied
            # it, so reject a mismatched control rather than guessing.
            event_control = ((closure_event.after_json or {}).get("control_fuente") if closure_event else None) or {}
            event_control_id = str(event_control.get("id") or event_control.get("public_id") or "")
            latest_control_id = str(getattr(latest_control, "public_id", "") or getattr(latest_control, "id", ""))
            control_matches_event = not event_control_id or event_control_id in {latest_control_id, str(getattr(latest_control, "id", ""))}
            if manga.estado != "ANULADA" and final is None and closure_event is not None and latest_control is not None and control_matches_event:
                final = _d(direct_close_segment.cantidad_fin_kg if direct_close_segment is not None else latest_control.peso_neto_kg)
            open_kg = _d(latest_control.peso_neto_kg) if final is None and latest_control is not None and manga.estado != "ANULADA" else None
            manga._report_final_kg = final
            manga._report_open_kg = open_kg
            manga._report_segments = segments
            manga._report_weight_corrected = (
                current_weighing is not None
                and current_weighing.id in correction_by_weight
            )
            manga._report_controls = controls_by_manga.get(manga.id, [])
            manga._report_closure_event = closure_event
            article = getattr(getattr(manga, "lote_articulo", None), "articulo", None)
            manga._report_identity = _canonical_article_identity(article)
    result = []
    for run in runs.values():
        contexts = run["contexts"]
        ot = sorted((item["ot"] for item in contexts), key=lambda item: item.fecha)[0] if contexts else None
        work = contexts[0]["work"] if contexts else None
        color_work = contexts[0]["color_work"] if contexts else None
        canonical_color = run["corrida"].color_produccion
        color_name = canonical_color.nombre if canonical_color else (color_work.color_nombre_snapshot if color_work else None)
        resource = (ot.maquina_nombre_snapshot or ot.maquina_codigo_snapshot) if ot is not None else None
        responsible = ot.responsable.nombre_completo if ot is not None and ot.responsable else None
        record = {
            **run,
            "ot": ot,
            "work": work,
            "color_work": color_work,
            "color_name": color_name,
            "color_id": getattr(canonical_color, "id", None),
            "color_code": getattr(canonical_color, "codigo", None),
            "color_hex": getattr(canonical_color, "hex_referencia", None),
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
    article_values = [
        str(value or "").lower()
        for manga in run["mangas"].values()
        for value in (getattr(manga, "articulo_nombre_snapshot", None), manga.articulo_codigo_snapshot)
    ]
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
    if filters["estado_ot"] and not any(filters["estado_ot"] == item["ot"].estado for item in contexts):
        return False
    if filters["articulo"] and not any(filters["articulo"].lower() in value for value in article_values):
        return False
    context_values = [
        str(value or "").lower()
        for item in contexts
        for value in (
            item["ot"].codigo_ot,
            item["ot"].estado,
            item["ot"].maquina_nombre_snapshot,
            item["ot"].maquina_codigo_snapshot,
            item["ot"].responsable.nombre_completo if item["ot"].responsable else None,
            item["work"].codigo if item.get("work") else None,
        )
    ]
    if filters["q"] and not any(filters["q"].lower() in value for value in (*values, *context_values, *article_values)):
        return False
    return True


def _run_manga_values(run):
    work_ids = {str(item["work"].id) for item in run.get("contexts", ()) if item.get("work") is not None}
    final = Decimal("0")
    opened = Decimal("0")
    known = 0
    total = 0
    for manga in run["mangas"].values():
        segments = list(getattr(manga, "_report_segments", ()) or ())
        net = _d(getattr(manga, "_report_final_kg", None))
        assigned = False
        open_net = _d(getattr(manga, "_report_open_kg", None))
        observed = net if net is not None else open_net
        attributed_evidence = False
        if segments and observed is not None and _segment_conciliates(segments, observed):
            if getattr(manga, "_report_weight_corrected", False):
                segments = _project_corrected_kg_segments(segments, observed)
            attributed = sum(
                (
                    _d(segment.cantidad_atribuida_kg) or Decimal("0")
                    for segment in segments
                    if (owner := effective_work_for_segment(manga, segment)) is not None
                    and str(owner.id) in work_ids
                ),
                Decimal("0"),
            )
            assigned = attributed > 0
            if assigned:
                attributed_evidence = True
                if net is not None:
                    final += attributed
                else:
                    opened += attributed
        if not assigned and segments:
            # Keep ambiguous/open ledgers visible as incomplete under their
            # effective owner without inventing or duplicating a kg amount.
            owner = effective_work(manga)
            assigned = owner is not None and str(owner.id) in work_ids
        if not assigned and not segments:
            owner = effective_work(manga)
            assigned = owner is not None and str(owner.id) in work_ids
            if assigned:
                final += net or Decimal("0")
                opened += _d(getattr(manga, "_report_open_kg", None)) or Decimal("0")
                attributed_evidence = net is not None or open_net is not None
        if assigned:
            total += 1
            if attributed_evidence:
                known += 1
    complete = total > 0 and total == known
    objective = _d(run["corrida"].objetivo_neto_kg)
    measured = final + opened if complete else None
    return final, opened, measured, total, known, objective


def _canonical_article_identity(article):
    """Resolve only persisted SCM -> PiezaColor -> Pieza relationships."""
    if article is None:
        return {"pieza_nombre": None, "pieza_codigo": None}
    variant_link = getattr(article, "pieza_color", None)
    variant = getattr(variant_link, "pieza_color", None)
    piece = getattr(variant, "pieza_rel", None)
    return {
        "pieza_nombre": getattr(piece, "nombre", None),
        "pieza_codigo": getattr(piece, "codigo", None),
    }


def _manga_group_value(run, manga, name):
    identity = getattr(manga, "_report_identity", None) or {"pieza_nombre": None, "pieza_codigo": None}
    owner = effective_work(manga)
    context = _context_for_work(run, owner)
    mold_code, mold_name = _context_mold_identity(run, context)
    return {
        "MOLDE": mold_code,
        "PIEZA": identity.get("pieza_codigo"),
    }.get(name, _run_group_value(run, name))


def _context_mold_identity(run, context):
    snapshot_code = getattr(getattr(context, "color_work", None), "molde_codigo_snapshot", None) if context else None
    if context and isinstance(context, dict):
        snapshot_code = getattr(context.get("color_work"), "molde_codigo_snapshot", None)
    code = snapshot_code or getattr(run.get("of"), "molde_id", None)
    mold = (run.get("moldes") or {}).get(code)
    return code, getattr(mold, "nombre", None)


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
        coverage = "COMPLETA" if total > 0 and total == known and visible else "INCOMPLETA"
        percent = ((final / objective) * 100) if objective and coverage == "COMPLETA" else None
        remaining = (objective - final) if objective is not None and coverage == "COMPLETA" else None
        outputs = []
        for output in getattr(run["corrida"], "salidas", ()) or ():
            article = getattr(output, "articulo", None)
            variant_link = getattr(article, "pieza_color", None)
            variant = getattr(variant_link, "pieza_color", None)
            piece = getattr(variant, "pieza_rel", None)
            outputs.append({
                "nombre": getattr(article, "nombre", None),
                "codigo": getattr(article, "codigo", None),
                "clase": getattr(article, "clase", None),
                "cantidad_objetivo": _n(getattr(output, "cantidad_objetivo", None)),
                "kg_estandar_objetivo": _n(getattr(output, "kg_estandar_objetivo", None)),
                "pieza": {
                    "nombre": getattr(piece, "nombre", None),
                    "codigo": getattr(piece, "codigo", None),
                } if piece is not None else None,
            })
        items.append({
            "corrida_id": str(run["corrida"].id),
            "corrida": run["corrida"].codigo,
            "of": run["orden"].codigo,
            "ot": run["ot"].codigo_ot if run["ot"] is not None else None,
            "color": run["color_name"],
            "color_hex": getattr(getattr(run["corrida"], "color_produccion", None), "hex_referencia", None),
            "molde": {
                "nombre": getattr(run.get("molde"), "nombre", None),
                "codigo": getattr(run.get("molde"), "codigo", None),
            } if run.get("molde") is not None else None,
            "salidas": outputs,
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
        "MOLDE": getattr(run.get("molde"), "codigo", None),
        "PIEZA": None,
        "ARTICULO": next((m.articulo_codigo_snapshot for m in run["mangas"].values()), None),
    }[name]


def _context_for_work(run, work):
    if work is None:
        return None
    return next((item for item in run.get("contexts", ()) if item["work"].id == work.id), None)


def _context_group_value(run, context, name, manga=None):
    context = context or {"ot": run["ot"], "work": run.get("work"), "color_work": run.get("color_work")}
    ot = context["ot"]
    corrida = run["corrida"]
    mold_code, _mold_name = _context_mold_identity(run, context)
    return {
        "DIA": _iso(ot.fecha),
        "MES": ot.fecha.strftime("%Y-%m"),
        "OF": run["orden"].codigo,
        "CORRIDA": corrida.codigo,
        "COLOR": run["color_name"],
        "OT": ot.codigo_ot,
        "RECURSO": ot.maquina_nombre_snapshot or ot.maquina_codigo_snapshot,
        "RESPONSABLE": ot.responsable.nombre_completo if ot.responsable else None,
        "MOLDE": mold_code,
        "PIEZA": (
            (getattr(manga, "_report_identity", None) or {}).get("pieza_codigo")
            if manga is not None else None
        ),
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
            getattr(manga, "articulo_nombre_snapshot", None), manga.articulo_codigo_snapshot,
        )
    ]
    if not filters["fecha_desde"] <= ot.fecha <= filters["fecha_hasta"]:
        return False
    if filters["estado_ot"] and filters["estado_ot"] != ot.estado:
        return False
    checks = (
        ("of", run["orden"].codigo), ("corrida", run["corrida"].codigo),
        ("color", run["color_name"]), ("ot", ot.codigo_ot),
        ("recurso", ot.maquina_nombre_snapshot or ot.maquina_codigo_snapshot),
        ("responsable", ot.responsable.nombre_completo if ot.responsable else None),
    )
    for key, value in checks:
        if filters[key] and filters[key].lower() not in str(value or "").lower():
            return False
    if filters["articulo"] and not any(
        filters["articulo"].lower() in str(value or "").lower()
        for value in (getattr(manga, "articulo_nombre_snapshot", None), manga.articulo_codigo_snapshot)
    ):
        return False
    return not filters["q"] or any(filters["q"].lower() in value for value in values)


def _history_rows(runs, groups, filters=None):
    rows = []
    seen_segments = set()
    theoretical_emitted = set()
    theoretical_subtotal_emitted = set()
    for run in runs:
        for manga in run["mangas"].values():
            net = _d(manga._report_final_kg)
            if net is None:
                continue
            segments = manga._report_segments
            if getattr(manga, "_report_weight_corrected", False):
                segments = _project_corrected_kg_segments(segments, net)
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
                        segment_values.append((run, context, _d(segment.cantidad_atribuida_kg), segment))
            else:
                # Without KG-evidence segments, a single unambiguous run can
                # receive the complete NET. Existing but broken segments are
                # deliberately left unattributed; a correction must not be
                # hidden by a fallback to the run total.
                owner = effective_work(manga) if not segments else None
                context = _context_for_work(run, owner)
                segment_values = [(run, context, net, None)] if context is not None and not segments and _context_matches(run, context, manga, filters) else []
            if not segment_values and segments:
                # A filtered-out ledger is still an attribution structure; it
                # must not be turned into an unattributed NET fallback.
                if valid:
                    continue
                owner = effective_work(manga)
                context = _context_for_work(run, owner)
                if context is None or not _context_matches(run, context, manga, filters):
                    continue
                values = {group: _manga_group_value(run, manga, group) for group in GROUP_OPTIONS}
                values["COLOR_CODIGO"] = run.get("color_code")
                values["COLOR_HEX"] = run.get("color_hex")
                values["_COLOR_ID"] = run.get("color_id")
                values["PIEZA_CODIGO"] = (getattr(manga, "_report_identity", None) or {}).get("pieza_codigo")
                values["PIEZA_NOMBRE"] = (getattr(manga, "_report_identity", None) or {}).get("pieza_nombre")
                mold_code, mold_name = _context_mold_identity(run, context)
                values["MOLDE_CODIGO"] = mold_code
                values["MOLDE_NOMBRE"] = mold_name
                unit = _d(manga.peso_unitario_snapshot_g)
                quantity = _d(manga.cantidad_confirmada_un or manga.cantidad_asignada_un)
                rows.append({**values, "ARTICULO_NOMBRE": getattr(manga, "articulo_nombre_snapshot", None), "PESO_KG": None, "SUBTOTAL_CONOCIDO_KG": _n(net), "MANGAS": 0, "P_UNITARIO_G": None, "P_TEORICO_KG": None, "SUBTOTAL_TEORICO_KG": _n(unit * quantity / Decimal("1000")) if unit is not None and quantity is not None else None, "_known": False, "_manga_id": manga.id})
            for segment_run, context, kg, segment in segment_values:
                values = {group: _context_group_value(segment_run, context, group, manga) for group in GROUP_OPTIONS}
                values["COLOR_CODIGO"] = segment_run.get("color_code")
                values["COLOR_HEX"] = segment_run.get("color_hex")
                values["_COLOR_ID"] = segment_run.get("color_id")
                values["PIEZA_CODIGO"] = (getattr(manga, "_report_identity", None) or {}).get("pieza_codigo")
                values["PIEZA_NOMBRE"] = (getattr(manga, "_report_identity", None) or {}).get("pieza_nombre")
                mold_code, mold_name = _context_mold_identity(segment_run, context)
                values["MOLDE_CODIGO"] = mold_code
                values["MOLDE_NOMBRE"] = mold_name
                values["ARTICULO"] = manga.articulo_codigo_snapshot
                values["ARTICULO_NOMBRE"] = getattr(manga, "articulo_nombre_snapshot", None)
                unit = _d(context["color_work"].peso_neto_snapshot_g) if context and context["color_work"] else _d(manga.peso_unitario_snapshot_g)
                quantity = _d(getattr(segment, "cantidad_atribuida_un", None)) if valid and segment is not None else None
                theoretical = (unit * quantity / Decimal("1000")) if unit is not None and quantity is not None and quantity > 0 else None
                theoretical_key = (manga.id, tuple(values.get(group) for group in groups))
                emit_theoretical = theoretical_key not in theoretical_emitted
                theoretical_emitted.add(theoretical_key)
                theoretical_total = _n(unit * _d(manga.cantidad_confirmada_un or manga.cantidad_asignada_un) / Decimal("1000")) if theoretical is None and manga.id not in theoretical_subtotal_emitted and unit is not None and _d(manga.cantidad_confirmada_un or manga.cantidad_asignada_un) is not None else None
                if theoretical_total is not None:
                    theoretical_subtotal_emitted.add(manga.id)
                rows.append({**values, "PESO_KG": _n(kg), "SUBTOTAL_CONOCIDO_KG": _n(kg), "MANGAS": 1 if emit_theoretical else 0, "P_UNITARIO_G": _n(unit) if emit_theoretical else None, "P_UNITARIO_WEIGHT": _n(unit * quantity) if unit is not None and quantity is not None and quantity > 0 else None, "P_UNITARIO_QTY": _n(quantity) if quantity is not None and quantity > 0 else None, "P_TEORICO_KG": _n(theoretical), "SUBTOTAL_TEORICO_KG": theoretical_total, "_known": True, "_manga_id": manga.id})
    return rows


def _group_history_rows(rows, normalized):
    grouped = {}
    for row in rows:
        key = tuple(row[group] for group in normalized["groups"])
        article_descriptor = ({
            "ARTICULO_NOMBRE": row.get("ARTICULO_NOMBRE"),
            "ARTICULO_CODIGO": row.get("ARTICULO"),
        } if "ARTICULO" in normalized["groups"] else {})
        identity_descriptor = {}
        if "MOLDE" in normalized["groups"]:
            identity_descriptor = {
                "MOLDE_NOMBRE": row.get("MOLDE_NOMBRE"),
                "MOLDE_CODIGO": row.get("MOLDE_CODIGO"),
            }
        if "PIEZA" in normalized["groups"]:
            identity_descriptor = {
                **identity_descriptor,
                "PIEZA_NOMBRE": row.get("PIEZA_NOMBRE"),
                "PIEZA_CODIGO": row.get("PIEZA_CODIGO"),
            }
        item = grouped.setdefault(key, {group: row[group] for group in normalized["groups"]} | article_descriptor | identity_descriptor | {"PESO_KG": Decimal("0"), "SUBTOTAL_CONOCIDO_KG": Decimal("0"), "SUBTOTAL_TEORICO_KG": Decimal("0"), "MANGAS": 0, "P_UNITARIO_WEIGHT": Decimal("0"), "P_UNITARIO_QTY": Decimal("0"), "P_TEORICO_KG": Decimal("0"), "coverage": "COMPLETA", "_has_unknown": False, "_has_theoretical_evidence": False, "_has_theoretical_subtotal": False, "_color_identities": set()})
        item["_color_identities"].add(_history_color_identity(row))
        if "ARTICULO" in normalized["groups"] and not item.get("ARTICULO_NOMBRE"):
            item["ARTICULO_NOMBRE"] = row.get("ARTICULO_NOMBRE")
        if "MOLDE" in normalized["groups"] and not item.get("MOLDE_NOMBRE"):
            item["MOLDE_NOMBRE"] = row.get("MOLDE_NOMBRE")
        if "PIEZA" in normalized["groups"] and not item.get("PIEZA_NOMBRE"):
            item["PIEZA_NOMBRE"] = row.get("PIEZA_NOMBRE")
        manga_id = row.get("_manga_id")
        item["SUBTOTAL_CONOCIDO_KG"] += _d(row.get("SUBTOTAL_CONOCIDO_KG")) or Decimal("0")
        if row.get("P_TEORICO_KG") is not None:
            item["_has_theoretical_evidence"] = True
        if row.get("SUBTOTAL_TEORICO_KG") is not None:
            item["_has_theoretical_subtotal"] = True
            item["SUBTOTAL_TEORICO_KG"] += _d(row.get("SUBTOTAL_TEORICO_KG")) or Decimal("0")
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
        item["SUBTOTAL_TEORICO_KG"] = _n(item["SUBTOTAL_TEORICO_KG"]) if item.pop("_has_theoretical_subtotal") else None
        item["P_TEORICO_KG"] = _n(item["P_TEORICO_KG"]) if item.pop("_has_theoretical_evidence") else None
        color_identities = item.pop("_color_identities")
        if len(color_identities) == 1:
            identity = next(iter(color_identities))
            item["COLOR_CODIGO"] = identity[1]
            item["COLOR_HEX"] = identity[2]
        else:
            item["COLOR_CODIGO"] = None
            item["COLOR_HEX"] = None
        items.append(item)
    return items


def _history_aggregate(rows, groups, measures):
    """Aggregate the authorized atomic rows for one hierarchy prefix.

    ``rows`` are the same evidence rows used by the flat response.  A node is
    rebuilt from those rows instead of summing already grouped children.  The
    manga count is therefore distinct at every prefix, while unit averages
    retain their evidence weights across a manga split between contexts.
    """
    if not rows:
        return None

    item = {group: rows[0].get(group) for group in groups}
    if "ARTICULO" in groups:
        item["ARTICULO_NOMBRE"] = next(
            (row.get("ARTICULO_NOMBRE") for row in rows if row.get("ARTICULO_NOMBRE") is not None),
            None,
        )
        item["ARTICULO_CODIGO"] = next(
            (row.get("ARTICULO") for row in rows if row.get("ARTICULO") is not None),
            None,
        )
    if "MOLDE" in groups:
        item["MOLDE_NOMBRE"] = next((row.get("MOLDE_NOMBRE") for row in rows if row.get("MOLDE_NOMBRE") is not None), None)
        item["MOLDE_CODIGO"] = next((row.get("MOLDE_CODIGO") for row in rows if row.get("MOLDE_CODIGO") is not None), None)
    if "PIEZA" in groups:
        item["PIEZA_NOMBRE"] = next((row.get("PIEZA_NOMBRE") for row in rows if row.get("PIEZA_NOMBRE") is not None), None)
        item["PIEZA_CODIGO"] = next((row.get("PIEZA_CODIGO") for row in rows if row.get("PIEZA_CODIGO") is not None), None)

    peso = Decimal("0")
    subtotal_conocido = Decimal("0")
    subtotal_teorico = Decimal("0")
    unit_weight = Decimal("0")
    unit_qty = Decimal("0")
    theoretical = Decimal("0")
    has_theoretical_subtotal = False
    has_theoretical = False
    complete = True
    manga_keys = set()
    fallback_manga_key = 0
    for row in rows:
        known = bool(row.get("_known", True)) and row.get("PESO_KG") is not None
        complete = complete and known
        if known:
            manga_id = row.get("_manga_id")
            if manga_id is None:
                fallback_manga_key += 1
                manga_key = ("row", fallback_manga_key)
            else:
                manga_key = ("manga", manga_id)
            manga_keys.add(manga_key)
        if row.get("PESO_KG") is not None:
            peso += _d(row["PESO_KG"]) or Decimal("0")
        if row.get("SUBTOTAL_CONOCIDO_KG") is not None:
            subtotal_conocido += _d(row["SUBTOTAL_CONOCIDO_KG"]) or Decimal("0")
        if row.get("SUBTOTAL_TEORICO_KG") is not None:
            has_theoretical_subtotal = True
            subtotal_teorico += _d(row["SUBTOTAL_TEORICO_KG"]) or Decimal("0")
        if row.get("P_TEORICO_KG") is not None:
            has_theoretical = True
            theoretical += _d(row["P_TEORICO_KG"]) or Decimal("0")
        if row.get("P_UNITARIO_WEIGHT") is not None and row.get("P_UNITARIO_QTY") is not None:
            unit_weight += _d(row["P_UNITARIO_WEIGHT"]) or Decimal("0")
            unit_qty += _d(row["P_UNITARIO_QTY"]) or Decimal("0")

    item["MANGAS"] = len(manga_keys)
    item["coverage"] = "COMPLETA" if complete else "INCOMPLETA"
    item["PESO_KG"] = _n(peso) if complete else None
    item["SUBTOTAL_CONOCIDO_KG"] = _n(subtotal_conocido)
    item["SUBTOTAL_TEORICO_KG"] = _n(subtotal_teorico) if has_theoretical_subtotal else None
    item["P_TEORICO_KG"] = _n(theoretical) if has_theoretical else None
    item["P_UNITARIO_G"] = _n(unit_weight / unit_qty) if unit_qty else None
    color_code, color_hex = _history_color_metadata(rows)
    item["COLOR_CODIGO"] = color_code
    item["COLOR_HEX"] = color_hex

    _select_history_measures(item, measures)
    return item


def _select_history_measures(item, measures):
    selected = set(measures)
    for measure in MEASURE_OPTIONS:
        if measure not in selected:
            item.pop(measure, None)
    if "PESO_KG" not in selected:
        item.pop("SUBTOTAL_CONOCIDO_KG", None)
    if "P_TEORICO_KG" not in selected:
        item.pop("SUBTOTAL_TEORICO_KG", None)


def _history_path_id(path):
    """Return a stable, position-independent id for a group/value path."""
    return json.dumps(
        [{"dimension": dimension, "value": value} for dimension, value in path],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _history_hierarchy(rows, groups, measures):
    """Build the opt-in hierarchy and its independent all-row aggregate."""
    if not rows:
        return [], None

    summary = _history_aggregate(rows, (), measures)
    if not groups:
        return [], summary

    roots = []
    nodes = {}
    for row in rows:
        parent = None
        path = []
        for depth, dimension in enumerate(groups):
            path.append((dimension, row.get(dimension)))
            path_key = tuple(path)
            node = nodes.get(path_key)
            if node is None:
                node = {
                    "id": _history_path_id(path),
                    "dimension": dimension,
                    "value": row.get(dimension),
                    "_rows": [],
                    "children": [],
                }
                nodes[path_key] = node
                if parent is None:
                    roots.append(node)
                else:
                    parent["children"].append(node)
            node["_rows"].append(row)
            parent = node

    def finalize(node, depth):
        children = [finalize(child, depth + 1) for child in node["children"]]
        return {
            "id": node["id"],
            "dimension": node["dimension"],
            "value": node["value"],
            "item": _history_aggregate(node["_rows"], groups[: depth + 1], measures),
            "children": children,
        }

    return [finalize(node, 0) for node in roots], summary


def history_rows_for_manga_detail(session, filters):
    """Return the exact atomic report rows used by the contextual manga list.

    Keeping this projection beside the report builder prevents the detail list
    from reconstructing identity or contribution from already aggregated rows.
    """
    normalized = _filters(filters, require_dates=True)
    runs = _load_rows(session, normalized)
    return normalized, _history_rows(runs, normalized["groups"], normalized), runs


def list_production_history(session, *, actor_id, filters=None):
    actor = load_actor(session, actor_id, capability="OT_VER")
    normalized = _filters(filters, require_dates=True)
    visible = actor.tiene_capacidad("MANGA_PESAJE_VER")
    runs = _load_rows(session, normalized)
    rows = _history_rows(runs, normalized["groups"], normalized) if visible else []
    items = _group_history_rows(rows, normalized)
    selected = set(normalized["measures"])
    for item in items:
        _select_history_measures(item, selected)
    dedup_known = defaultdict(Decimal)
    for row in rows:
        dedup_known[row.get("_manga_id")] += _d(row.get("SUBTOTAL_CONOCIDO_KG")) or Decimal("0")
    subtotal_known = sum(dedup_known.values(), Decimal("0"))
    payload = {"items": items, "grouping_options": list(GROUP_OPTIONS), "measure_options": list(MEASURE_OPTIONS), "measures": normalized["measures"], "grouped_by": normalized["groups"], "subtotal_conocido_kg": _n(subtotal_known), "filters": {key: value.isoformat() if isinstance(value, date) else value for key, value in normalized.items() if key not in {"groups", "measures"}}, "visibilidad": {"pesaje": visible}}
    if "PESO_KG" not in selected:
        payload.pop("subtotal_conocido_kg", None)
    include_hierarchy = str((filters or {}).get("incluir_jerarquia", "")).strip().lower() in {"1", "true", "yes", "si"}
    if include_hierarchy:
        hierarchy, summary = _history_hierarchy(rows, normalized["groups"], normalized["measures"])
        payload["jerarquia"] = hierarchy
        payload["resumen"] = summary
    return payload


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
    weight_total, weight_coverage = _history_weight_summary(payload["items"], payload["measures"])
    summary.append(["Peso efectivo (kg)", weight_total])
    if weight_coverage is not None and "PESO_KG" in payload["measures"]:
        summary.append(["Cobertura peso", weight_coverage])
    summary.append(["Filas", len(payload["items"])])
    data = workbook.create_sheet("Datos")
    headers = []
    for group in payload["grouped_by"]:
        if group == "ARTICULO":
            headers.extend(["ARTICULO_NOMBRE", "ARTICULO_CODIGO"])
        elif group == "MOLDE":
            headers.extend(["MOLDE_NOMBRE", "MOLDE_CODIGO"])
        elif group == "PIEZA":
            headers.extend(["PIEZA_NOMBRE", "PIEZA_CODIGO"])
        else:
            headers.append(group)
    headers += list(payload["measures"])
    if "PESO_KG" in payload["measures"]:
        headers.append("SUBTOTAL_CONOCIDO_KG")
    if "P_TEORICO_KG" in payload["measures"]:
        headers.append("SUBTOTAL_TEORICO_KG")
    headers.append("coverage")
    data.append(headers)
    for row in payload["items"]:
        data.append([row.get(header) for header in headers])
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
