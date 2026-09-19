"""Supervised OT/Trabajo correction for a weighed manga.

This service changes the current attribution pointer only after validating that
the physical article and all KG facts remain the same.  The correction itself
is an immutable audit row; the original weighing snapshots are never edited.
"""

import uuid
from decimal import Decimal

from sqlalchemy import or_, select

from app.extensions import db
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.scm_inventory_kg import (
    ScmDivisionUnidadKg,
    ScmExistenciaMangaKg,
    ScmMedicionUnidadKg,
    ScmMovimientoInventarioKg,
    ScmReservaUnidadKg,
    ScmRetiroArmadoKgItem,
    ScmUnidadFisicaKg,
)
from app.models.scm_ot import (
    ScmAsignacionPlanMangaOt,
    ScmAsignacionPersonalTrabajoOt,
    ScmAtribucionProduccionKg,
    ScmCierreProductivoKg,
    ScmCorreccionAsignacionManga,
    ScmCorreccionPesajeManga,
    ScmManga,
    ScmPesajeManga,
    ScmTrabajoOt,
    ScmTramoMangaTrabajo,
    utc_now,
)
from app.services.scm_ot_service import (
    _complete_operation,
    _reserve_operation,
    _event,
)
from app.services.scm_service_support import (
    ScmServiceError,
    acquire_kg_productive_write_lock,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)


CAPABILITY = "MANGA_REATRIBUIR_TRABAJO"
_SAFE_MANGA_STATES = {
    "EN_LLENADO",
    "CERRADA_ARMADO_PENDIENTE_PESAJE",
    "PESADA",
    "ETIQUETADA_FINAL",
    "PENDIENTE_RECEPCION_ALMACEN",
}
_SAFE_KG_LOGISTICS = {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION"}
_OPEN_SEGMENT_STATES = {"PROGRAMADO", "ACTIVO"}
_NON_TERMINAL_WORK = {"PLANIFICADO", "EN_EJECUCION", "PAUSADO"}
_NON_TERMINAL_OT = {"BORRADOR", "PLANIFICADA", "EN_EJECUCION"}
_FINAL_MANGA_STATES = {
    "PESADA", "ETIQUETADA_FINAL", "PENDIENTE_RECEPCION_ALMACEN", "RECIBIDA",
}


def _uuid(value, *, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "INVALID_UUID", f"El campo {field} debe ser un UUID valido.",
            status_code=400, details={"field": field},
        ) from error


def _load_manga(session, manga_id, *, lock=False):
    query = select(ScmManga).where(ScmManga.public_id == manga_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    manga = session.scalars(query).first()
    if manga is None:
        raise ScmServiceError("MANGA_NOT_FOUND", "La manga no existe.", status_code=404)
    return manga


def _work_identity(work, manga):
    color = getattr(work, "trabajo_color", None)
    return {
        "maquina_id": getattr(work.orden_trabajo, "maquina_id", None),
        "orden_operacion_id": str(work.orden_operacion_id),
        "corrida_fabricacion_id": str(color.corrida_fabricacion_id) if color else None,
        "color_id": color.color_id_snapshot if color else None,
        "receta_revision_id": color.receta_revision_id_snapshot if color else None,
        "receta_hash": color.receta_hash_snapshot if color else None,
        "articulo_id": getattr(manga.lote_articulo, "articulo_id", None),
        "salida_id": str(manga.plan_linea.orden_operacion_salida_id)
        if manga.plan_linea and manga.plan_linea.orden_operacion_salida_id else None,
    }


def _compatibility(source, target, manga):
    left = _work_identity(source, manga)
    right = _work_identity(target, manga)
    checks = (
        ("MAQUINA_DISTINTA", "maquina_id", "La manga debe permanecer en la misma maquina."),
        ("OPERACION_DISTINTA", "orden_operacion_id", "La OF/operacion no coincide."),
        ("CORRIDA_DISTINTA", "corrida_fabricacion_id", "La corrida de fabricacion no coincide."),
        ("COLOR_DISTINTO", "color_id", "El color de produccion no coincide."),
        ("RECETA_DISTINTA", "receta_revision_id", "La revision de receta no coincide."),
        ("RECETA_HASH_DISTINTO", "receta_hash", "La receta congelada no coincide."),
    )
    blockers = []
    for code, field, message in checks:
        if left[field] != right[field]:
            blockers.append({"code": code, "message": message, "field": field})
    outputs = {
        str(output.id): output.articulo_scm_id
        for output in getattr(target.orden_operacion, "salidas", ()) or ()
    }
    if left["salida_id"] and outputs.get(left["salida_id"]) != left["articulo_id"]:
        blockers.append({
            "code": "SALIDA_ARTICULO_INCOMPATIBLE",
            "message": "La salida/articulo de la manga no existe en la operacion destino.",
            "field": "salida_id",
        })
    return left, right, blockers


def _downstream_blockers(session, manga):
    blockers = []
    if manga.estado not in _SAFE_MANGA_STATES:
        blockers.append({"code": "MANGA_ESTADO_NO_CORREGIBLE", "message": f"La manga esta en estado {manga.estado}."})
    if manga.estado == "ANULADA":
        blockers.append({"code": "MANGA_ANULADA", "message": "Una manga anulada no puede reatribuirse."})
    if session.scalar(
        select(ScmCorreccionPesajeManga.id)
        .join(ScmPesajeManga, ScmPesajeManga.id == ScmCorreccionPesajeManga.pesaje_id)
        .where(
            ScmPesajeManga.manga_id == manga.id,
            ScmCorreccionPesajeManga.estado == "PENDIENTE",
        )
        .limit(1)
    ):
        blockers.append({
            "code": "PESAJE_CORRECCION_PENDIENTE",
            "message": "La manga tiene una correccion de pesaje pendiente de resolver.",
        })

    existence = session.scalars(
        select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga.id)
    ).all()
    for item in existence:
        if item.estado_logistico not in _SAFE_KG_LOGISTICS:
            blockers.append({"code": "KG_LOGISTICA_POSTERIOR", "message": f"La existencia KG ya esta en {item.estado_logistico}.", "id": str(item.id)})
    existence_ids = [item.id for item in existence]
    units = []
    if existence_ids:
        units = session.scalars(
            select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.existencia_manga_kg_id.in_(existence_ids))
        ).all()
    unit_ids = [item.id for item in units]
    for item in units:
        if item.estado_logistico not in _SAFE_KG_LOGISTICS:
            blockers.append({"code": "KG_UNIDAD_POSTERIOR", "message": f"La unidad KG ya esta en {item.estado_logistico}.", "id": str(item.id)})
    if unit_ids:
        if session.scalar(select(ScmReservaUnidadKg.id).where(ScmReservaUnidadKg.unidad_id.in_(unit_ids)).limit(1)):
            blockers.append({"code": "KG_RESERVA_EXISTENTE", "message": "La manga ya tiene historial de reserva KG."})
        if session.scalar(select(ScmRetiroArmadoKgItem.id).where(ScmRetiroArmadoKgItem.unidad_id.in_(unit_ids)).limit(1)):
            blockers.append({"code": "KG_RETIRO_ARMADO", "message": "La manga ya fue retirada hacia Armado."})
        if session.scalar(select(ScmMedicionUnidadKg.id).where(ScmMedicionUnidadKg.unidad_id.in_(unit_ids)).limit(1)):
            blockers.append({"code": "KG_REPESA_O_RETorno", "message": "La manga ya tiene repesaje o retorno registrado."})
        if session.scalar(select(ScmDivisionUnidadKg.id).where(ScmDivisionUnidadKg.padre_id.in_(unit_ids)).limit(1)):
            blockers.append({"code": "KG_DIVISION", "message": "La manga ya fue dividida."})
    saldo_ids = [item.saldo_id for item in existence if item.saldo_id is not None]
    if saldo_ids and session.scalar(select(ScmMovimientoInventarioKg.id).where(
        ScmMovimientoInventarioKg.saldo_id.in_(saldo_ids),
        ScmMovimientoInventarioKg.tipo != "INGRESO_PRODUCCION",
    ).limit(1)):
        blockers.append({"code": "KG_MOVIMIENTO_POSTERIOR", "message": "La existencia tiene movimientos posteriores al ingreso."})
    if session.scalar(select(ScmCierreProductivoKg.id).where(ScmCierreProductivoKg.ot_id == manga.ot_id).limit(1)):
        blockers.append({"code": "OT_CIERRE_PRODUCTIVO", "message": "La OT origen ya tiene cierre productivo."})
    return blockers


def _document_closure_blockers(session, source, target):
    conditions = []
    seen = set()
    for work in (source, target):
        ot_key = ("OT", str(work.orden_trabajo.public_id))
        operation_type = "OF" if work.orden_operacion.tipo == "FABRICACION" else "OA"
        operation_key = (operation_type, str(work.orden_operacion_id))
        for document_type, document_id in (ot_key, operation_key):
            if (document_type, document_id) in seen:
                continue
            seen.add((document_type, document_id))
            conditions.append(
                (ScmCierreProductivoKg.documento_tipo == document_type)
                & (ScmCierreProductivoKg.documento_id == document_id)
            )
    if conditions and session.scalar(
        select(ScmCierreProductivoKg.id).where(or_(*conditions)).limit(1)
    ):
        return [{
            "code": "DOCUMENTO_CIERRE_PRODUCTIVO",
            "message": "La OT, OF u OA relacionada ya tiene cierre productivo KG.",
        }]
    return []


def _validate(
    session,
    manga,
    target_work_id,
    target_assignment_id=None,
    target_plan_assignment_id=None,
    target_segment_id=None,
    *,
    lock=False,
):
    article = (
        manga.lote_articulo.articulo
        if manga.lote_articulo is not None else None
    )
    if article is None or str(article.unidad_inventario or "").upper() != "KG":
        raise ScmServiceError(
            "KG_MANGA_REQUIRED",
            "La correccion auditada del piloto solo aplica a mangas con inventario KG.",
            status_code=409,
        )
    source = manga.trabajo
    if source is None:
        raise ScmServiceError("MANGA_WITHOUT_WORK", "La manga no tiene Trabajo de origen.", status_code=409)
    target = session.get(ScmTrabajoOt, target_work_id)
    if target is None:
        raise ScmServiceError("TARGET_WORK_NOT_FOUND", "El Trabajo destino no existe.", status_code=404)
    if source.id == target.id:
        raise ScmServiceError("SAME_WORK", "El Trabajo destino debe ser distinto al origen.", status_code=409)
    if source.tipo != "COLOR" or target.tipo != "COLOR" or source.trabajo_color is None or target.trabajo_color is None:
        raise ScmServiceError(
            "COLOR_WORK_REQUIRED",
            "Origen y destino deben ser Trabajos de color de fabricacion.",
            status_code=409,
        )
    if source.estado not in _NON_TERMINAL_WORK or target.estado not in _NON_TERMINAL_WORK:
        raise ScmServiceError("WORK_TERMINAL", "Origen y destino deben estar abiertos.", status_code=409, details={"origen": source.estado, "destino": target.estado})
    if source.orden_trabajo.estado not in _NON_TERMINAL_OT or target.orden_trabajo.estado not in _NON_TERMINAL_OT:
        raise ScmServiceError("OT_TERMINAL", "Origen y destino deben pertenecer a OT no terminales.", status_code=409)
    if source.orden_operacion.estado in {"CERRADA", "ANULADA"} or target.orden_operacion.estado in {"CERRADA", "ANULADA"}:
        raise ScmServiceError("OPERATION_TERMINAL", "La OF/OA de origen y destino debe permanecer abierta.", status_code=409)
    if manga.estado not in _FINAL_MANGA_STATES and target.estado == "PLANIFICADO":
        blockers = [{
            "code": "DESTINATION_WORK_NOT_ACTIVE",
            "message": "El Trabajo destino debe estar EN_EJECUCION o PAUSADO antes de continuar una manga abierta.",
            "recovery": "Inicie o pause el Trabajo destino y vuelva a previsualizar la correccion.",
        }]
    else:
        blockers = []
    existing_correction = getattr(manga, "correccion_asignacion", None)
    if existing_correction is not None:
        blockers.append({
            "code": "MANGA_ALREADY_CORRECTED",
            "message": "La manga ya tiene una correccion de asignacion aplicada.",
            "correccion_id": str(existing_correction.public_id),
        })
    left, right, compatibility = _compatibility(source, target, manga)
    blockers.extend(_downstream_blockers(session, manga))
    blockers.extend(_document_closure_blockers(session, source, target))
    segments = list(getattr(manga, "tramos_trabajo", ()) or ())
    allowed_segment_states = set(_OPEN_SEGMENT_STATES)
    if manga.estado in {"PESADA", "ETIQUETADA_FINAL", "PENDIENTE_RECEPCION_ALMACEN"}:
        allowed_segment_states.add("CERRADO")
    if len(segments) > 1 or any(item.estado not in allowed_segment_states for item in segments):
        blockers.append({
            "code": "MANGA_CONTINUIDAD_O_TRAMO_NO_CORREGIBLE",
            "message": "La manga tiene continuidad o un tramo que ya no admite correccion.",
        })
    assignment = None
    if target_assignment_id is not None:
        assignment = session.get(ScmAsignacionPersonalTrabajoOt, target_assignment_id)
        if assignment is None or assignment.trabajo_ot_id != target.id:
            blockers.append({"code": "DESTINATION_ASSIGNMENT_INVALID", "message": "La asignacion destino no pertenece al Trabajo destino."})
        elif assignment.estado not in {"PREVISTA", "ACTIVA"}:
            blockers.append({"code": "DESTINATION_ASSIGNMENT_CLOSED", "message": "La asignacion destino esta cerrada."})
    candidates = [item for item in target.asignaciones_personal if item.estado in {"PREVISTA", "ACTIVA"}]
    plan_query = (
        select(ScmAsignacionPlanMangaOt)
        .where(
            ScmAsignacionPlanMangaOt.plan_linea_id == manga.plan_linea_id,
            ScmAsignacionPlanMangaOt.trabajo_ot_id.in_((source.id, target.id)),
        )
        .order_by(ScmAsignacionPlanMangaOt.id)
    )
    if lock:
        plan_query = plan_query.with_for_update()
    plan_assignments = session.scalars(plan_query).all()
    source_plan_assignment = next(
        (item for item in plan_assignments if item.trabajo_ot_id == source.id),
        None,
    )
    target_plan_assignment = next(
        (
            item for item in plan_assignments
            if item.trabajo_ot_id == target.id and item.ot_id == target.orden_trabajo_id
        ),
        None,
    )
    active_weighing = session.scalar(
        select(ScmPesajeManga)
        .where(ScmPesajeManga.manga_id == manga.id, ScmPesajeManga.estado == "VIGENTE")
        .order_by(ScmPesajeManga.id.desc())
        .limit(1)
    )
    if (
        source_plan_assignment is None
        or source_plan_assignment.trabajo_ot_id != source.id
        or source_plan_assignment.ot_id != source.orden_trabajo_id
    ):
        blockers.append({
            "code": "SOURCE_PLAN_ASSIGNMENT_INVALID",
            "message": "La manga no conserva una asignacion de plan coherente con el Trabajo origen.",
        })
    if target_plan_assignment is None:
        blockers.append({
            "code": "DESTINATION_PLAN_ASSIGNMENT_REQUIRED",
            "message": "El Trabajo destino no tiene asignacion para la misma linea del plan.",
        })
    if target_plan_assignment_id is not None and (
        target_plan_assignment is None
        or target_plan_assignment.id != target_plan_assignment_id
    ):
        blockers.append({
            "code": "DESTINATION_PLAN_ASSIGNMENT_INVALID",
            "message": "La asignacion de plan destino no pertenece al Trabajo destino.",
        })
    # The correction is anchored to the physical source segment that existed
    # when it was applied.  It never creates or rewrites a destination segment.
    # A later real segment can therefore supersede the overlay unambiguously.
    correction_segment = max(segments, key=lambda item: item.secuencia) if segments else None
    if correction_segment is None and active_weighing is None:
        blockers.append({
            "code": "CORRECTION_SEGMENT_REQUIRED",
            "message": "La manga no conserva el tramo fisico que se desea corregir.",
        })
    if target_segment_id is not None and (
        correction_segment is None or correction_segment.id != target_segment_id
    ):
            blockers.append({
                "code": "TARGET_SEGMENT_INVALID",
                "message": "El tramo indicado no es el tramo fisico vigente de la manga.",
            })
    return {
        "manga": manga,
        "source": source,
        "target": target,
        "assignment": assignment,
        "candidates": candidates,
        "source_plan_assignment": source_plan_assignment,
        "target_plan_assignment": target_plan_assignment,
        "correction_segment": correction_segment,
        "active_weighing": active_weighing,
        "checks": {"origen": left, "destino": right},
        "compatibility": compatibility,
        "blockers": blockers,
    }


def _preview_payload(result):
    manga = result["manga"]
    source = result["source"]
    target = result["target"]
    active_weighing = result.get("active_weighing")
    controls = list(getattr(manga, "controles_peso", ()) or ())
    last_control = controls[-1] if controls else None
    kg_reference = (
        active_weighing.peso_fisico_neto_kg
        if active_weighing is not None
        else getattr(last_control, "peso_neto_kg", None)
    )
    labels = {
        "maquina_id": "Maquina",
        "orden_operacion_id": "OF / operacion",
        "corrida_fabricacion_id": "Corrida",
        "color_id": "Color",
        "receta_revision_id": "Revision de receta",
        "receta_hash": "Hash de receta",
        "articulo_id": "Articulo",
        "salida_id": "Salida",
    }
    target_plan = result.get("target_plan_assignment")
    correction_segment = result.get("correction_segment")
    return {
        "manga": {"id": str(manga.public_id), "codigo": manga.codigo, "version": manga.version, "estado": manga.estado},
        "origen": {"trabajo_id": str(source.id), "trabajo_codigo": source.codigo, "ot_id": source.orden_trabajo_id, "ot_codigo": source.orden_trabajo.codigo_ot},
        "destino": {
            "trabajo_id": str(target.id),
            "trabajo_codigo": target.codigo,
            "ot_id": target.orden_trabajo_id,
            "ot_codigo": target.orden_trabajo.codigo_ot,
            "asignacion_plan_id": target_plan.id if target_plan is not None else None,
        },
        "tramo_fisico_corregido_id": (
            str(correction_segment.id) if correction_segment is not None else None
        ),
        "compatibilidad": result["compatibility"],
        "verificaciones": [
            {
                "campo": field,
                "etiqueta": labels[field],
                "origen": result["checks"]["origen"].get(field),
                "destino": result["checks"]["destino"].get(field),
                "coincide": result["checks"]["origen"].get(field)
                == result["checks"]["destino"].get(field),
            }
            for field in labels
        ],
        "bloqueos": result["blockers"],
        "puede_aplicar": not result["compatibility"] and not result["blockers"],
        "asignaciones_destino": [{"id": str(item.id), "trabajador_id": item.trabajador_id, "estado": item.estado} for item in result["candidates"]],
        "sin_cambio_stock_kg": True,
        "kg_referencia": format(kg_reference, ".3f") if kg_reference is not None else None,
        "kg_fuente": (
            "PESAJE_FINAL" if active_weighing is not None
            else ("CONTROL" if last_control is not None else None)
        ),
    }


def preview_assignment_correction(session, *, actor_id, manga_id, data):
    load_actor(session, actor_id, capability=CAPABILITY)
    reject_unknown_fields(data, allowed={
        "destino_trabajo_ot_id", "destino_asignacion_id",
        "destino_asignacion_plan_id", "tramo_objetivo_id",
    })
    target_work_id = _uuid(data.get("destino_trabajo_ot_id"), field="destino_trabajo_ot_id")
    target_assignment_id = data.get("destino_asignacion_id")
    if target_assignment_id:
        target_assignment_id = _uuid(target_assignment_id, field="destino_asignacion_id")
    target_plan_id = data.get("destino_asignacion_plan_id")
    if target_plan_id is not None:
        try:
            target_plan_id = int(target_plan_id)
        except (TypeError, ValueError) as error:
            raise ScmServiceError(
                "INVALID_PLAN_ASSIGNMENT",
                "destino_asignacion_plan_id debe ser entero.",
                status_code=400,
            ) from error
    target_segment_id = data.get("tramo_objetivo_id")
    if target_segment_id:
        target_segment_id = _uuid(target_segment_id, field="tramo_objetivo_id")
    result = _validate(
        session, _load_manga(session, manga_id), target_work_id, target_assignment_id,
        target_plan_id, target_segment_id,
    )
    return _preview_payload(result)


def apply_assignment_correction(session, *, actor_id, manga_id, operation_id, data):
    actor = load_actor(session, actor_id, capability=CAPABILITY)
    endpoint = f"/mangas/{manga_id}/correcciones-asignacion"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, data)
    if replay is not None:
        return replay
    acquire_kg_productive_write_lock(session)
    reject_unknown_fields(data, allowed={
        "destino_trabajo_ot_id", "destino_asignacion_id", "destino_asignacion_plan_id",
        "tramo_objetivo_id", "version", "motivo",
    })
    target_work_id = _uuid(data.get("destino_trabajo_ot_id"), field="destino_trabajo_ot_id")
    assignment_id = _uuid(data.get("destino_asignacion_id"), field="destino_asignacion_id")
    plan_assignment_id = data.get("destino_asignacion_plan_id")
    if plan_assignment_id is not None:
        try:
            plan_assignment_id = int(plan_assignment_id)
        except (TypeError, ValueError) as error:
            raise ScmServiceError("INVALID_PLAN_ASSIGNMENT", "destino_asignacion_plan_id debe ser entero.", status_code=400) from error
    target_segment_id = data.get("tramo_objetivo_id")
    if target_segment_id:
        target_segment_id = _uuid(target_segment_id, field="tramo_objetivo_id")
    version = expected_version(data.get("version"))
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    # Match KG closure lock order: trabajos/documentos, physical tramos,
    # manga, then plan rows and personnel assignment.  This prevents a
    # correction racing a closure from taking the inverse lock path.
    manga_probe = _load_manga(session, manga_id)
    session.scalars(
        select(ScmTrabajoOt)
        .where(ScmTrabajoOt.id.in_((manga_probe.trabajo_ot_id, target_work_id)))
        .order_by(ScmTrabajoOt.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    session.scalars(
        select(ScmTramoMangaTrabajo)
        .where(ScmTramoMangaTrabajo.manga_id == manga_probe.id)
        .order_by(ScmTramoMangaTrabajo.secuencia)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    manga = _load_manga(session, manga_id, lock=True)
    # ``tramos_trabajo`` and ``correccion_asignacion`` use select-in loading.
    # Expire both collections after the row locks so the blocker revalidation
    # observes any transaction that committed while this request was waiting.
    session.expire(manga, ["tramos_trabajo", "correccion_asignacion"])
    if manga.version != version:
        raise ScmServiceError("VERSION_CONFLICT", "La manga fue modificada por otra operacion.", status_code=409)
    existing = session.scalar(select(ScmCorreccionAsignacionManga).where(ScmCorreccionAsignacionManga.manga_id == manga.id))
    if existing is not None:
        raise ScmServiceError("MANGA_ALREADY_CORRECTED", "La manga ya tiene una correccion aplicada.", status_code=409, details={"correccion": existing.to_dict()})
    result = _validate(
        session, manga, target_work_id, assignment_id, plan_assignment_id,
        target_segment_id, lock=True,
    )
    if result["compatibility"] or result["blockers"]:
        raise ScmServiceError("ASSIGNMENT_CORRECTION_BLOCKED", "La correccion no puede aplicarse.", status_code=409, details=_preview_payload(result))
    session.scalars(
        select(ScmAsignacionPersonalTrabajoOt)
        .where(ScmAsignacionPersonalTrabajoOt.id == assignment_id)
        .with_for_update()
    ).all()
    # Re-read all domain blockers after the final lock so a concurrent
    # assignment/status transition yields a deterministic domain conflict.
    result = _validate(
        session, manga, target_work_id, assignment_id, plan_assignment_id,
        target_segment_id, lock=True,
    )
    if result["compatibility"] or result["blockers"]:
        raise ScmServiceError("ASSIGNMENT_CORRECTION_BLOCKED", "La correccion no puede aplicarse.", status_code=409, details=_preview_payload(result))
    source, target, assignment = result["source"], result["target"], result["assignment"]
    source_plan = result["source_plan_assignment"]
    target_plan = result["target_plan_assignment"]
    if assignment is None:
        raise ScmServiceError("DESTINATION_ASSIGNMENT_REQUIRED", "Selecciona la asignacion destino.", status_code=400)
    before_version = manga.version
    source_plan_before = {
        "id": source_plan.id,
        "mangas_asignadas": source_plan.mangas_asignadas,
        "cantidad_asignada_un": str(source_plan.cantidad_asignada_un),
    }
    target_plan_before = {
        "id": target_plan.id,
        "mangas_asignadas": target_plan.mangas_asignadas,
        "cantidad_asignada_un": str(target_plan.cantidad_asignada_un),
    }
    source_work_before = {
        "id": str(source.id),
        "cantidad_objetivo_un": str(source.cantidad_objetivo_un),
    }
    target_work_before = {
        "id": str(target.id),
        "cantidad_objetivo_un": str(target.cantidad_objetivo_un),
    }
    attribution_rows = session.scalars(select(ScmAtribucionProduccionKg).where(ScmAtribucionProduccionKg.manga_id == manga.id)).all()
    original = {
        "manga_trabajo_ot_id": str(manga.trabajo_ot_id),
        "manga_asignacion_id": str(manga.asignacion_personal_trabajo_id) if manga.asignacion_personal_trabajo_id else None,
        "manga_asignacion_plan_id": manga.asignacion_id,
        "pesajes_asignacion_ids": [
            str(item.asignacion_personal_trabajo_id)
            for item in session.scalars(
                select(ScmPesajeManga).where(ScmPesajeManga.manga_id == manga.id)
            ).all()
            if item.asignacion_personal_trabajo_id
        ],
        "tramos": [{"id": str(item.id), "trabajo_ot_id": str(item.trabajo_ot_id), "asignacion_id": str(item.asignacion_personal_trabajo_id)} for item in manga.tramos_trabajo],
        "atribuciones": [{"id": str(item.public_id), "trabajo_ot_id": str(item.trabajo_ot_id) if item.trabajo_ot_id else None} for item in attribution_rows],
    }
    correction = ScmCorreccionAsignacionManga(
        manga=manga,
        origen_ot_id=source.orden_trabajo_id,
        origen_trabajo_ot_id=source.id,
        destino_ot_id=target.orden_trabajo_id,
        destino_trabajo_ot_id=target.id,
        origen_asignacion_id=manga.asignacion_personal_trabajo_id,
        destino_asignacion_id=assignment.id,
        destino_asignacion_plan_id=target_plan.id,
        tramo_objetivo_id=(
            result["correction_segment"].id
            if result["correction_segment"] is not None else None
        ),
        manga_version_antes=before_version,
        manga_version_despues=before_version + 1,
        motivo=reason,
        evidencia_json={"original": original, "compatibilidad": result["checks"], "stock_kg_sin_cambio": True},
        actor_id=actor.id,
        operation_id=operation.operation_id,
    )
    session.add(correction)
    moved_quantity = Decimal(manga.cantidad_asignada_un)
    if (
        source_plan.mangas_asignadas < 1
        or Decimal(source_plan.cantidad_asignada_un) < moved_quantity
        or Decimal(source.cantidad_objetivo_un) < moved_quantity
    ):
        raise ScmServiceError(
            "SOURCE_PLAN_BALANCE_INVALID",
            "La asignacion origen no cubre la manga que se desea corregir.",
            status_code=409,
        )
    source_plan.mangas_asignadas -= 1
    source_plan.cantidad_asignada_un = Decimal(source_plan.cantidad_asignada_un) - moved_quantity
    target_plan.mangas_asignadas += 1
    target_plan.cantidad_asignada_un = Decimal(target_plan.cantidad_asignada_un) + moved_quantity
    source.cantidad_objetivo_un = Decimal(source.cantidad_objetivo_un) - moved_quantity
    target.cantidad_objetivo_un = Decimal(target.cantidad_objetivo_un) + moved_quantity
    source.version += 1
    target.version += 1
    manga.version += 1
    correction.evidencia_json["transferencia"] = {
        "manga_id": str(manga.public_id),
        "cantidad_un": str(moved_quantity),
        "origen": {
            "plan": {
                "id": source_plan.id,
                "antes": source_plan_before,
                "despues": {
                    "mangas_asignadas": source_plan.mangas_asignadas,
                    "cantidad_asignada_un": str(source_plan.cantidad_asignada_un),
                },
            },
            "trabajo": {
                "id": str(source.id),
                "antes": source_work_before,
                "despues": {
                    "cantidad_objetivo_un": str(source.cantidad_objetivo_un),
                },
            },
        },
        "destino": {
            "plan": {
                "id": target_plan.id,
                "antes": target_plan_before,
                "despues": {
                    "mangas_asignadas": target_plan.mangas_asignadas,
                    "cantidad_asignada_un": str(target_plan.cantidad_asignada_un),
                },
            },
            "trabajo": {
                "id": str(target.id),
                "antes": target_work_before,
                "despues": {
                    "cantidad_objetivo_un": str(target.cantidad_objetivo_un),
                },
            },
        },
        "total_objetivo_un_antes": str(
            Decimal(source_work_before["cantidad_objetivo_un"])
            + Decimal(target_work_before["cantidad_objetivo_un"])
        ),
        "total_objetivo_un_despues": str(
            Decimal(source.cantidad_objetivo_un)
            + Decimal(target.cantidad_objetivo_un)
        ),
    }
    session.flush()
    response = {
        "correccion": correction.to_dict(),
        "manga": {"id": str(manga.public_id), "codigo": manga.codigo, "version": manga.version, "trabajo_color_id": str(target.id), "trabajo_color_codigo": target.codigo},
        "stock_kg": {"movimientos_creados": 0, "saldo_modificado": False},
    }
    session.add(_event("SCM_MANGA", manga.public_id, "MANGA_ASSIGNMENT_CORRECTED", actor, operation, response))
    _complete_operation(operation, response)
    session.commit()
    return response
