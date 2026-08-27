"""TS-010D core: connected weighing of a simple SCM manga."""

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models.scm_auditoria import ScmOperacion
from app.models.scm_inventory import (
    ScmMovimientoInventario,
    ScmSaldoInventario,
)
from app.models.scm_ot import (
    ScmAnulacionPesajeManga,
    ScmAsignacionPersonalTrabajoOt,
    ScmAsignacionPlanMangaOt,
    ScmEtiquetaManga,
    ScmCorreccionPesajeManga,
    ScmControlPesoManga,
    ScmManga,
    ScmPesajeManga,
    ScmTrabajoImpresionManga,
    ScmTramoMangaTrabajo,
    utc_now,
)
from app.models.scm_warehouse import ScmExistenciaManga
from app.services.scm_ot_service import (
    _complete_operation,
    _event,
    _json_hash,
    _latest_manga_segment,
    _close_work_assignment,
    _reserve_operation,
    _recompute_parent_state,
    _serialize_label,
    _serialize_manga,
)
from app.services.scm_alert_service import (
    current_alert_rule,
    upsert_operational_alert,
)
from app.services.scm_service_support import (
    ScmServiceError,
    load_actor,
    reject_unknown_fields,
    required_text,
)


KG_QUANTUM = Decimal("0.001")
UNIT_QUANTUM = Decimal("0.001")


def _elapsed_hours(later, earlier):
    """Return a timezone-safe Decimal duration for SQLite and PostgreSQL."""
    if later.tzinfo is None:
        later = later.replace(tzinfo=ZoneInfo("UTC"))
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=ZoneInfo("UTC"))
    return Decimal(str((later - earlier).total_seconds() / 3600))


def _exceeds_alert_threshold(session, code, value):
    revision = current_alert_rule(session, code)
    return revision is not None and Decimal(str(value)) > Decimal(revision.umbral)


def _kg(value, field, *, allow_zero=False):
    try:
        parsed = Decimal(str(value))
        quantized = parsed.quantize(KG_QUANTUM)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_WEIGHT",
            f"{field} debe expresarse en kg con hasta tres decimales.",
            status_code=422,
            details={"field": field},
        ) from error
    if (
        not parsed.is_finite()
        or parsed != quantized
        or (quantized < 0 if allow_zero else quantized <= 0)
    ):
        raise ScmServiceError(
            "INVALID_WEIGHT",
            f"{field} debe expresarse en kg con hasta tres decimales.",
            status_code=422,
            details={"field": field},
        )
    return quantized


def _units(value, field):
    try:
        parsed = Decimal(str(value))
        quantized = parsed.quantize(UNIT_QUANTUM)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} debe ser una cantidad positiva con hasta tres decimales.",
            status_code=422,
            details={"field": field},
        ) from error
    if not parsed.is_finite() or parsed != quantized or quantized <= 0:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} debe ser una cantidad positiva con hasta tres decimales.",
            status_code=422,
            details={"field": field},
        )
    return quantized


def _assert_final_weight_cumulative(session, manga, net):
    if not manga.tramos_trabajo:
        return
    previous_control = session.scalar(
        select(ScmControlPesoManga)
        .where(ScmControlPesoManga.manga_id == manga.id)
        .order_by(
            ScmControlPesoManga.pesado_at.desc(),
            ScmControlPesoManga.id.desc(),
        )
        .limit(1)
    )
    if (
        previous_control is not None
        and net <= Decimal(previous_control.peso_neto_kg)
    ):
        raise ScmServiceError(
            "FINAL_WEIGHT_NOT_CUMULATIVE",
            "El pesaje final debe contener el peso acumulado total de la misma manga y superar el último corte.",
            status_code=409,
            details={
                "ultimo_control_neto_kg": format(
                    Decimal(previous_control.peso_neto_kg), "f"
                ),
                "peso_final_neto_kg": format(net, "f"),
            },
        )


def _aware_datetime(value, field="pesada_at"):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_DATETIME",
            f"{field} debe ser un timestamp ISO-8601.",
            status_code=422,
        ) from error
    if parsed.tzinfo is None:
        raise ScmServiceError(
            "TIMEZONE_REQUIRED",
            f"{field} debe incluir zona horaria.",
            status_code=422,
        )
    return parsed


def _current_label(manga, label_type):
    return next(
        (
            label
            for label in reversed(manga.etiquetas)
            if label.tipo == label_type and label.estado != "INVALIDADA"
        ),
        None,
    )


def _order_ot_identity(manga):
    if manga.trabajo is not None:
        work = manga.trabajo
        return {
            "of_ot": (
                f"{work.orden_operacion.codigo} - {manga.ot.codigo_ot}"
            ),
            "ot_id": str(manga.ot.public_id),
            "trabajo_color_id": str(work.id),
            "trabajo_color_codigo": work.codigo,
            "corrida_fabricacion_id": str(
                work.trabajo_color.corrida_fabricacion_id
            ),
        }
    if manga.ot.orden_operacion_id is not None:
        return {
            (
                "oa_ot"
                if manga.ot.tipo_ot == "ENSAMBLE"
                else "of_ot"
            ): (
                f"{manga.ot.orden_operacion.codigo} - "
                f"{manga.ot.codigo_ot}"
            )
        }
    return {"op_ot": f"{manga.ot.orden_id} - {manga.ot.codigo_ot}"}


def _piece_color_label(manga):
    if manga.pieza_color_sku_snapshot:
        return (
            f"{manga.articulo_nombre_snapshot} "
            f"({manga.pieza_color_sku_snapshot})"
        )
    return manga.articulo_nombre_snapshot


def _resolve_payload(label):
    manga = label.manga
    weighing = ScmPesajeManga.query.filter_by(manga_id=manga.id).one_or_none()
    is_assembly = manga.ot.tipo_ot == "ENSAMBLE"
    expected_state = (
        "CERRADA_ARMADO_PENDIENTE_PESAJE"
        if is_assembly else "PREETIQUETADA"
    )
    quantity = (
        manga.cantidad_confirmada_un
        if is_assembly else manga.cantidad_asignada_un
    )
    current_segment = _latest_manga_segment(manga)
    current_work = (
        current_segment.trabajo if current_segment is not None else manga.trabajo
    )
    current_ot = current_work.orden_trabajo if current_work else manga.ot
    personal = (
        current_segment.asignacion_personal_trabajo
        if current_segment is not None else manga.asignacion_personal_trabajo
    )
    worker = personal.trabajador if personal is not None else manga.maquinista_previsto
    work_ready = (
        current_work is None
        or current_work.estado in {"EN_EJECUCION", "PAUSADO"}
    )
    continuity_final_ready = (
        not is_assembly
        and manga.estado == "EN_LLENADO"
        and current_segment is not None
        and current_segment.estado == "ACTIVO"
        and work_ready
    )
    initial_final_ready = manga.estado == expected_state and work_ready
    usable_prelabel = label.tipo == "PREPESAJE" and label.estado == "IMPRESA"
    can_finalize = (
        usable_prelabel
        and weighing is None
        and (initial_final_ready or continuity_final_ready)
    )
    can_cut_shift = (
        not is_assembly
        and usable_prelabel
        and weighing is None
        and work_ready
        and (
            (manga.estado == "PREETIQUETADA" and current_segment is None)
            or (
                manga.estado == "EN_LLENADO"
                and current_segment is not None
                and current_segment.estado == "ACTIVO"
            )
        )
    )
    latest_control = (
        manga.controles_peso[-1] if manga.controles_peso else None
    )
    accumulated = (
        Decimal(latest_control.conteo_acumulado_un)
        if latest_control is not None else Decimal("0")
    )
    assigned = Decimal(manga.cantidad_asignada_un)
    return {
        "label": _serialize_label(label),
        "manga": {
            "public_id": str(manga.public_id),
            "codigo": manga.codigo,
            "estado": manga.estado,
            "tipo": manga.tipo,
            "cantidad_asignada_un": format(
                Decimal(manga.cantidad_asignada_un).normalize(), "f"
            ),
            "cantidad_confirmada_un": (
                format(Decimal(quantity).normalize(), "f")
                if quantity is not None else None
            ),
            "cantidad_fuente": (
                "RESPONSABLE_ARMADO"
                if is_assembly else "PLAN_CONFIRMADO_POR_PESAJE"
            ),
            "cantidad_editable": False,
            "tipo_manga": manga.tipo_contenedor_nombre_snapshot,
            "tara_nominal_kg": format(
                (Decimal(manga.tara_nominal_g_snapshot) / 1000).quantize(
                    KG_QUANTUM
                ),
                "f",
            ),
            "peso_bruto_max_kg": format(
                Decimal(manga.peso_bruto_max_kg_snapshot), "f"
            ),
            "fecha_operativa": current_ot.fecha.isoformat(),
            "maquinista": worker.nombre_completo if worker else None,
            "ot": {
                "id": str(current_ot.public_id),
                "codigo": current_ot.codigo_ot,
                "maquina": (
                    current_ot.maquina_nombre_snapshot
                    or (current_ot.maquina.nombre if current_ot.maquina else None)
                ),
                "turno": current_ot.turno,
            },
            "ot_origen": {
                "id": str(manga.ot.public_id),
                "codigo": manga.ot.codigo_ot,
                "fecha_operativa": manga.ot.fecha.isoformat(),
                "turno": manga.ot.turno,
            },
            "trabajo_color": (
                {
                    "id": str(current_work.id),
                    "codigo": current_work.codigo,
                    "estado": current_work.estado,
                    "orden_fabricacion": current_work.orden_operacion.codigo,
                    "corrida": current_work.trabajo_color.corrida.codigo,
                    "color": current_work.trabajo_color.color_nombre_snapshot,
                }
                if current_work is not None else None
            ),
            "asignacion_vigente": (
                {
                    "id": str(personal.id),
                    "estado": personal.estado,
                    "maquinista_id": personal.trabajador_id,
                    "maquinista": (
                        personal.trabajador.nombre_completo
                        if personal.trabajador else None
                    ),
                }
                if personal is not None else None
            ),
            "asignacion_personal_trabajo_id": (
                str(personal.id) if personal else None
            ),
            "pieza_color": _piece_color_label(manga),
            "color": manga.color_snapshot,
            **_order_ot_identity(manga),
        },
        "weighing": weighing.to_dict() if weighing else None,
        "continuidad": {
            "estado": (
                "PENDIENTE_VINCULO"
                if manga.estado == "CONTINUIDAD_PENDIENTE"
                else (
                    current_segment.estado if current_segment else "SIN_CORTE"
                )
            ),
            "tramo_actual": (
                current_segment.to_dict() if current_segment else None
            ),
            "ultimo_control": (
                latest_control.to_dict() if latest_control else None
            ),
            "conteo_acumulado_un": format(accumulated.normalize(), "f"),
            "cantidad_pendiente_un": format(
                max(assigned - accumulated, Decimal("0")).normalize(), "f"
            ),
            "qr_preservado": bool(current_segment),
        },
        "acciones": {
            "registrar_corte_turno": can_cut_shift,
            "completar_final": can_finalize,
        },
        "can_weigh": can_finalize,
        "can_register_shift_cut": can_cut_shift,
    }


def resolve_manga_label(session, *, label_id):
    label = session.scalar(
        select(ScmEtiquetaManga).where(
            ScmEtiquetaManga.public_id == label_id
        )
    )
    if label is None:
        raise ScmServiceError(
            "LABEL_NOT_FOUND", "La etiqueta no existe.", status_code=404
        )
    if label.estado == "INVALIDADA":
        current = _current_label(label.manga, label.tipo)
        raise ScmServiceError(
            "LABEL_INVALIDATED",
            "La etiqueta fue invalidada y no puede utilizarse.",
            status_code=409,
            details={
                "current_label_id": str(current.public_id) if current else None
            },
        )
    return _resolve_payload(label)


def register_manga_weighing_control(
    session,
    *,
    station_id,
    operation_id,
    actor_id,
    data,
):
    """Register an accumulated shift boundary without finalizing the manga."""
    actor = load_actor(
        session, actor_id, capability="MANGA_CONTROL_PESO_REGISTRAR"
    )
    endpoint = "/integration/v1/manga-weighing-controls"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    reject_unknown_fields(
        data,
        allowed={
            "label_id",
            "capture_id",
            "peso_bruto_kg",
            "tara_kg",
            "tara_fuente",
            "pesada_at",
            "pesado_por_id",
            "reading_stable",
            "conteo_acumulado_un",
            "motivo",
        },
    )
    try:
        if data.get("reading_stable") is not True:
            raise ScmServiceError(
                "SCALE_READING_UNSTABLE",
                "El corte requiere una lectura estable de la balanza.",
                status_code=422,
            )
        try:
            label_id = uuid.UUID(str(data.get("label_id")))
            capture_id = uuid.UUID(str(data.get("capture_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "INVALID_UUID",
                "label_id y capture_id deben ser UUID validos.",
                status_code=422,
            ) from error
        label = session.scalar(
            select(ScmEtiquetaManga)
            .where(ScmEtiquetaManga.public_id == label_id)
            .with_for_update()
        )
        if label is None:
            raise ScmServiceError(
                "LABEL_NOT_FOUND", "La etiqueta no existe.", status_code=404
            )
        manga = session.scalar(
            select(ScmManga)
            .where(ScmManga.id == label.manga_id)
            .with_for_update()
        )
        if (
            label.tipo != "PREPESAJE"
            or label.estado != "IMPRESA"
            or manga.ot.tipo_ot != "FABRICACION"
        ):
            raise ScmServiceError(
                "MANGA_NOT_READY",
                "El corte requiere una preetiqueta de fabricacion impresa y vigente.",
                status_code=409,
            )
        if ScmPesajeManga.query.filter_by(manga_id=manga.id).one_or_none():
            raise ScmServiceError(
                "MANGA_ALREADY_WEIGHED",
                "La manga ya posee un pesaje final.",
                status_code=409,
            )

        segments = session.scalars(
            select(ScmTramoMangaTrabajo)
            .where(ScmTramoMangaTrabajo.manga_id == manga.id)
            .order_by(ScmTramoMangaTrabajo.secuencia)
            .with_for_update()
        ).all()
        segment = segments[-1] if segments else None
        if segment is None:
            if manga.estado != "PREETIQUETADA":
                raise ScmServiceError(
                    "MANGA_NOT_READY",
                    "La manga no esta abierta para registrar un corte.",
                    status_code=409,
                )
            work = manga.trabajo
            personal = manga.asignacion_personal_trabajo
            if work is None or personal is None:
                raise ScmServiceError(
                    "ASSIGNMENT_WORK_MISMATCH",
                    "La manga no tiene Trabajo de color y responsable validos.",
                    status_code=409,
                )
            segment = ScmTramoMangaTrabajo(
                manga=manga,
                trabajo=work,
                asignacion_personal_trabajo=personal,
                asignacion_plan_id=manga.asignacion_id,
                secuencia=1,
                estado="ACTIVO",
                cantidad_inicio_un=Decimal("0"),
                cantidad_atribuida_un=Decimal("0"),
                iniciada_at=personal.iniciada_at or work.iniciada_at or utc_now(),
                created_by_id=actor.id,
                operation_id=operation.operation_id,
            )
            session.add(segment)
            session.flush()
        else:
            work = segment.trabajo
            personal = segment.asignacion_personal_trabajo
            if manga.estado != "EN_LLENADO" or segment.estado != "ACTIVO":
                raise ScmServiceError(
                    "MANGA_CONTINUITY_NOT_ACTIVE",
                    "La continuidad debe estar vinculada e iniciada antes de otro corte.",
                    status_code=409,
                )
        if work.estado not in {"EN_EJECUCION", "PAUSADO"}:
            raise ScmServiceError(
                "COLOR_WORK_NOT_WEIGHABLE",
                "El Trabajo de color debe estar en ejecucion o pausado.",
                status_code=409,
            )

        count = _units(data.get("conteo_acumulado_un"), "conteo_acumulado_un")
        if count != count.to_integral_value():
            raise ScmServiceError(
                "INVALID_QUANTITY",
                "El conteo acumulado de piezas debe ser entero.",
                status_code=422,
            )
        assigned = Decimal(manga.cantidad_asignada_un).quantize(UNIT_QUANTUM)
        start = Decimal(segment.cantidad_inicio_un).quantize(UNIT_QUANTUM)
        if count <= start or count >= assigned:
            raise ScmServiceError(
                "INVALID_SHIFT_BOUNDARY_COUNT",
                "El corte debe avanzar sobre el tramo y ser menor al total asignado; el total se cierra con F2.",
                status_code=422,
                details={
                    "cantidad_inicio_un": format(start, "f"),
                    "cantidad_asignada_un": format(assigned, "f"),
                },
            )
        gross = _kg(data.get("peso_bruto_kg"), "peso_bruto_kg")
        tare = _kg(data.get("tara_kg"), "tara_kg", allow_zero=True)
        net = (gross - tare).quantize(KG_QUANTUM)
        if net <= 0:
            raise ScmServiceError(
                "INVALID_TARE", "La tara debe ser menor que el bruto.", status_code=422
            )
        if gross > Decimal(manga.peso_bruto_max_kg_snapshot):
            raise ScmServiceError(
                "WEIGHT_EXCEEDS_CONTAINER_LIMIT",
                "El bruto supera el limite congelado del tipo de manga.",
                status_code=422,
            )
        tare_source = str(data.get("tara_fuente") or "").upper()
        nominal_tare = (
            Decimal(manga.tara_nominal_g_snapshot) / 1000
        ).quantize(KG_QUANTUM)
        if tare_source == "TIPO_MANGA" and tare != nominal_tare:
            raise ScmServiceError(
                "INVALID_TARE",
                "La tara no coincide con el snapshot del tipo de manga.",
                status_code=422,
            )
        if tare_source not in {"TIPO_MANGA", "MEDIDA_AUTORIZADA"}:
            raise ScmServiceError(
                "INVALID_TARE", "La fuente de tara no es valida.", status_code=422
            )
        if tare_source == "MEDIDA_AUTORIZADA":
            load_actor(session, actor_id, capability="PESAJE_TARA_OVERRIDE")
        previous_control = manga.controles_peso[-1] if manga.controles_peso else None
        if previous_control is not None and net <= Decimal(previous_control.peso_neto_kg):
            raise ScmServiceError(
                "CONTROL_WEIGHT_NOT_MONOTONIC",
                "El peso acumulado no puede disminuir; revise la manga antes de transferirla.",
                status_code=409,
            )

        weighed_at = _aware_datetime(data.get("pesada_at"))
        timezone_name = work.orden_trabajo.timezone_snapshot or "America/Lima"
        local_date = weighed_at.astimezone(ZoneInfo(timezone_name)).date()
        if local_date < work.orden_trabajo.fecha:
            raise ScmServiceError(
                "OPERATIONAL_DATE_IN_FUTURE",
                "No se puede registrar el corte antes de la fecha de la OT vigente.",
                status_code=422,
            )
        reason = required_text(data.get("motivo"), field="motivo", max_length=500)
        control = ScmControlPesoManga(
            manga=manga,
            tramo=segment,
            operation_id=operation.operation_id,
            source_system="SCM_STATION",
            station_id=station_id,
            capture_id=capture_id,
            tipo="CORTE_TURNO",
            peso_bruto_kg=gross,
            tara_kg=tare,
            peso_neto_kg=net,
            tara_fuente=tare_source,
            conteo_acumulado_un=count,
            motivo=reason,
            pesado_at=weighed_at,
            timezone_snapshot=timezone_name,
            fecha_local_pesaje=local_date,
            pesado_por_id=actor.id,
        )
        session.add(control)
        segment.estado = "CERRADO"
        segment.cantidad_fin_un = count
        segment.cerrada_at = weighed_at
        segment.motivo_cierre = reason
        manga.estado = "CONTINUIDAD_PENDIENTE"
        manga.version += 1
        if work.estado == "EN_EJECUCION":
            work.estado = "PAUSADO"
            work.pausada_at = weighed_at
            work.motivo_pausa = reason
            work.version += 1
            _close_work_assignment(work, actor=actor, reason=reason)
            _recompute_parent_state(session, work)
        session.flush()
        response = {
            "control": control.to_dict(),
            "manga": _serialize_manga(manga),
            "produccion_confirmada_un": "0",
            "inventario_creado": False,
            "print_job_id": None,
            "qr_preservado": True,
            "continuidad_estado": "PENDIENTE_VINCULO",
            "idempotent_replay": False,
        }
        session.add(_event(
            "MANGA", manga.id, "MANGA_SHIFT_CONTROL_RECORDED",
            actor, operation, response,
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise

def _post_label_payload(manga, weighing, label_id, version):
    canonical = (
        manga.trabajo is not None
        or manga.ot.orden_operacion_id is not None
    )
    personal = manga.asignacion_personal_trabajo
    worker = personal.trabajador if personal is not None else manga.maquinista_previsto
    return {
        "template": {
            "version": (
                "POSTPESAJE_TSPL_3"
                if manga.trabajo is not None and manga.ot.orden_operacion_id is None
                else ("POSTPESAJE_TSPL_2" if canonical else "POSTPESAJE_TSPL_1")
            ),
            "dpi": 203,
            "sheet_width_mm": 109,
            "sheet_height_mm": 50,
            "gap_mm": 3,
            "sticker_width_mm": 50,
            "columns_x_dots": [24, 464],
            "qr_module_dots": 4,
            "qr_reference": "STICKER_PESAJE_LEGACY",
        },
        "generated_at": utc_now().isoformat(),
        "fecha_ot": manga.ot.fecha.isoformat(),
        "maquinista": worker.nombre_completo if worker else None,
        "trabajo_color": (
            {
                "id": str(manga.trabajo.id),
                "codigo": manga.trabajo.codigo,
            }
            if manga.trabajo is not None else None
        ),
        "pieza_color": _piece_color_label(manga),
        "color": manga.color_snapshot,
        "codigo_manga": manga.codigo,
        "tipo_manga": manga.tipo,
        "cantidad_planificada_un": format(
            Decimal(manga.cantidad_planificada_un).normalize(), "f"
        ),
        "kg_fisico": format(weighing.peso_fisico_neto_kg, "f"),
        "kg_produccion_ot": format(weighing.kg_produccion_ot, "f"),
        "qr": {
            "v": 1,
            "type": "SCM_MANGA_LABEL",
            "manga_id": str(manga.public_id),
            "label_id": str(label_id),
            "label_type": "POSTPESAJE",
            "label_version": version,
            "trabajo_color_id": (
                str(manga.trabajo_ot_id) if manga.trabajo_ot_id else None
            ),
        },
        **_order_ot_identity(manga),
    }


def confirm_manga_weighing(
    session,
    *,
    station_id,
    operation_id,
    actor_id,
    data,
):
    actor = load_actor(session, actor_id, capability="MANGA_PESAR")
    endpoint = "/integration/v1/manga-weighings"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        if data.get("reading_stable") is not True:
            raise ScmServiceError(
                "SCALE_READING_UNSTABLE",
                "F2 requiere una lectura estable de la balanza.",
                status_code=422,
            )
        try:
            label_id = uuid.UUID(str(data.get("label_id")))
            capture_id = uuid.UUID(str(data.get("capture_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "INVALID_UUID",
                "label_id y capture_id deben ser UUID validos.",
                status_code=422,
            ) from error
        label = session.scalar(
            select(ScmEtiquetaManga)
            .where(ScmEtiquetaManga.public_id == label_id)
            .with_for_update()
        )
        if label is None:
            raise ScmServiceError(
                "LABEL_NOT_FOUND", "La etiqueta no existe.", status_code=404
            )
        manga = session.scalar(
            select(ScmManga)
            .where(ScmManga.id == label.manga_id)
            .with_for_update()
        )
        if label.estado == "INVALIDADA":
            raise ScmServiceError(
                "LABEL_INVALIDATED",
                "La etiqueta fue invalidada.",
                status_code=409,
            )
        if label.tipo != "PREPESAJE" or label.estado != "IMPRESA":
            raise ScmServiceError(
                "MANGA_NOT_READY",
                "Se requiere una etiqueta PREPESAJE impresa y vigente.",
                status_code=409,
            )
        is_assembly = manga.ot.tipo_ot == "ENSAMBLE"
        segments = session.scalars(
            select(ScmTramoMangaTrabajo)
            .where(ScmTramoMangaTrabajo.manga_id == manga.id)
            .order_by(ScmTramoMangaTrabajo.secuencia)
            .with_for_update()
        ).all()
        current_segment = segments[-1] if segments else None
        current_work = (
            current_segment.trabajo
            if current_segment is not None else manga.trabajo
        )
        current_personal = (
            current_segment.asignacion_personal_trabajo
            if current_segment is not None
            else manga.asignacion_personal_trabajo
        )
        expected_state = (
            "CERRADA_ARMADO_PENDIENTE_PESAJE"
            if is_assembly else "PREETIQUETADA"
        )
        continuity_ready = (
            not is_assembly
            and manga.estado == "EN_LLENADO"
            and current_segment is not None
            and current_segment.estado == "ACTIVO"
        )
        if manga.estado != expected_state and not continuity_ready:
            code = (
                "MANGA_ALREADY_WEIGHED"
                if manga.estado in {
                    "PESADA",
                    "ETIQUETADA_FINAL",
                    "PENDIENTE_RECEPCION_ALMACEN",
                }
                else "MANGA_NOT_READY"
            )
            raise ScmServiceError(
                code,
                "La manga no se encuentra lista para pesaje.",
                status_code=409,
            )
        if current_work is not None:
            if current_work.estado not in {"EN_EJECUCION", "PAUSADO"}:
                raise ScmServiceError(
                    "COLOR_WORK_NOT_WEIGHABLE",
                    "El trabajo debe estar en ejecucion o pausado para pesar.",
                    status_code=409,
                )
            if (
                current_personal is None
                or current_personal.trabajo_ot_id != current_work.id
            ):
                raise ScmServiceError(
                    "ASSIGNMENT_WORK_MISMATCH",
                    "La manga no tiene una asignacion valida de su trabajo.",
                    status_code=409,
                )

        gross = _kg(data.get("peso_bruto_kg"), "peso_bruto_kg")
        tare = _kg(data.get("tara_kg"), "tara_kg", allow_zero=True)
        net = (gross - tare).quantize(KG_QUANTUM)
        if net <= 0:
            raise ScmServiceError(
                "INVALID_TARE",
                "La tara debe ser menor que el peso bruto.",
                status_code=422,
            )
        if gross > Decimal(manga.peso_bruto_max_kg_snapshot):
            raise ScmServiceError(
                "WEIGHT_EXCEEDS_CONTAINER_LIMIT",
                "El bruto supera el limite congelado del tipo de manga.",
                status_code=422,
            )
        tare_source = str(data.get("tara_fuente") or "").upper()
        nominal_tare = (
            Decimal(manga.tara_nominal_g_snapshot) / 1000
        ).quantize(KG_QUANTUM)
        if tare_source == "TIPO_MANGA" and tare != nominal_tare:
            raise ScmServiceError(
                "INVALID_TARE",
                "La tara no coincide con el snapshot del tipo de manga.",
                status_code=422,
            )
        if tare_source not in {"TIPO_MANGA", "MEDIDA_AUTORIZADA"}:
            raise ScmServiceError(
                "INVALID_TARE",
                "La fuente de tara no es valida para el pesaje inicial.",
                status_code=422,
            )
        if tare_source == "MEDIDA_AUTORIZADA":
            load_actor(session, actor_id, capability="PESAJE_TARA_OVERRIDE")
        if current_segment is not None:
            _assert_final_weight_cumulative(session, manga, net)

        weighed_at = _aware_datetime(data.get("pesada_at"))
        current_ot = current_work.orden_trabajo if current_work else manga.ot
        timezone_name = current_ot.timezone_snapshot or "America/Lima"
        local_date = weighed_at.astimezone(ZoneInfo(timezone_name)).date()
        drift = (local_date - current_ot.fecha).days
        if drift < 0:
            raise ScmServiceError(
                "OPERATIONAL_DATE_IN_FUTURE",
                "No se puede pesar antes de la fecha operativa de la OT.",
                status_code=422,
            )
        drift_reason = (data.get("motivo_desfase") or "").strip()
        late_operational_date = _exceeds_alert_threshold(
            session, "PESAJE_FECHA_OPERATIVA_DIFERENTE", drift
        )

        quantity = Decimal(
            manga.cantidad_confirmada_un
            if is_assembly else manga.cantidad_asignada_un
        ).quantize(UNIT_QUANTUM)
        production_kg = (
            quantity * Decimal(manga.peso_unitario_snapshot_g) / 1000
        ).quantize(KG_QUANTUM)
        weighing = ScmPesajeManga(
            manga_id=manga.id,
            operation_id=operation.operation_id,
            source_system="SCM_STATION",
            station_id=station_id,
            capture_id=capture_id,
            peso_bruto_kg=gross,
            tara_kg=tare,
            peso_fisico_neto_kg=net,
            tara_fuente=tare_source,
            cantidad_confirmada=quantity,
            fuente_cantidad=(
                "RESPONSABLE_ARMADO"
                if is_assembly else "PLAN_CONFIRMADO_POR_PESAJE"
            ),
            kg_produccion_ot=production_kg,
            pesada_at=weighed_at,
            timezone_snapshot=timezone_name,
            fecha_local_pesaje=local_date,
            dias_desfase_operativo=drift,
            alerta_fecha=late_operational_date,
            motivo_desfase_texto=drift_reason or None,
            pesado_por_id=actor.id,
            asignacion_personal_trabajo_id=(
                current_personal.id if current_personal else None
            ),
            snapshots_json={
                **_order_ot_identity(manga),
                "trabajo_color_actual_id": (
                    str(current_work.id) if current_work else None
                ),
                "ot_actual_id": str(current_ot.public_id),
                "ot_actual_codigo": current_ot.codigo_ot,
                "maquinista_previsto_id": manga.maquinista_previsto_id,
                "asignacion_personal_trabajo_id": (
                    str(current_personal.id) if current_personal else None
                ),
                "pieza_color_sku": manga.pieza_color_sku_snapshot,
                "color": manga.color_snapshot,
                "tipo_manga": manga.tipo_contenedor_nombre_snapshot,
                "peso_unitario_g": format(
                    manga.peso_unitario_snapshot_g, "f"
                ),
            },
        )
        session.add(weighing)
        if not is_assembly:
            manga.cantidad_confirmada_un = quantity
        manga.cantidad_contenida_un = quantity
        manga.estado = "PESADA"
        manga.version += 1
        if current_segment is not None:
            current_segment.estado = "CERRADO"
            current_segment.cantidad_fin_un = quantity
            current_segment.cerrada_at = weighed_at
            current_segment.motivo_cierre = "PESAJE_FINAL"
            expected_start = Decimal("0.000")
            attributed_total = Decimal("0.000")
            for segment in segments:
                start = Decimal(segment.cantidad_inicio_un).quantize(
                    UNIT_QUANTUM
                )
                end = Decimal(segment.cantidad_fin_un or 0).quantize(
                    UNIT_QUANTUM
                )
                if start != expected_start or end <= start:
                    raise ScmServiceError(
                        "CONTINUITY_SEGMENTS_INCONSISTENT",
                        "Los tramos de la manga no forman una secuencia acumulada valida.",
                        status_code=409,
                    )
                delta = end - start
                segment.cantidad_atribuida_un = delta
                segment.trabajo.cantidad_confirmada_un = (
                    Decimal(segment.trabajo.cantidad_confirmada_un or 0)
                    + delta
                )
                segment.trabajo.version += 1
                attributed_total += delta
                expected_start = end
            if attributed_total != quantity:
                raise ScmServiceError(
                    "CONTINUITY_ATTRIBUTION_MISMATCH",
                    "La atribucion por turnos no coincide con el total final de la manga.",
                    status_code=409,
                )
        elif manga.trabajo is not None:
            manga.trabajo.cantidad_confirmada_un = (
                Decimal(manga.trabajo.cantidad_confirmada_un or 0) + quantity
            )
            manga.trabajo.version += 1
        session.flush()

        label_version = max(
            (
                item.version
                for item in manga.etiquetas
                if item.tipo == "POSTPESAJE"
            ),
            default=0,
        ) + 1
        post_label_id = uuid.uuid4()
        payload = _post_label_payload(
            manga, weighing, post_label_id, label_version
        )
        job = ScmTrabajoImpresionManga(
            plantilla_version=payload["template"]["version"],
            payload_hash=_json_hash([payload]),
            solicitado_por_id=actor.id,
            station_id=station_id,
        )
        session.add(job)
        session.flush()
        post_label = ScmEtiquetaManga(
            public_id=post_label_id,
            manga_id=manga.id,
            trabajo_impresion_id=job.public_id,
            tipo="POSTPESAJE",
            version=label_version,
            plantilla_version=payload["template"]["version"],
            payload_json=payload,
            payload_hash=_json_hash(payload),
        )
        session.add(post_label)
        session.flush()

        generated_alerts = []
        prelabel_reference = label.printed_at or label.generated_at
        prelabel_delay_hours = _elapsed_hours(weighed_at, prelabel_reference)
        if _exceeds_alert_threshold(
            session, "PESAJE_TARDIO_PREETIQUETA", prelabel_delay_hours
        ):
            alert = upsert_operational_alert(
                session,
                rule_code="PESAJE_TARDIO_PREETIQUETA",
                aggregate_type="PESAJE_MANGA",
                aggregate_id=weighing.public_id,
                condition_key=f"preetiqueta:{label.public_id}",
                summary=f"Pesaje tardio de la manga {manga.codigo}",
                detail={
                    "manga": manga.codigo,
                    "preetiqueta_id": str(label.public_id),
                    "referencia_at": prelabel_reference.isoformat(),
                    "pesada_at": weighed_at.isoformat(),
                    "horas_transcurridas": format(prelabel_delay_hours.quantize(Decimal('0.001')), "f"),
                },
                actor_id=actor.id,
            )
            if alert:
                generated_alerts.append(str(alert.id))
        if late_operational_date:
            alert = upsert_operational_alert(
                session,
                rule_code="PESAJE_FECHA_OPERATIVA_DIFERENTE",
                aggregate_type="PESAJE_MANGA",
                aggregate_id=weighing.public_id,
                condition_key=f"fecha:{weighing.fecha_local_pesaje.isoformat()}",
                summary=f"Pesaje fuera de fecha operativa de {manga.codigo}",
                detail={
                    "manga": manga.codigo,
                    "fecha_ot": current_ot.fecha.isoformat(),
                    "fecha_pesaje": weighing.fecha_local_pesaje.isoformat(),
                    "dias_diferencia": drift,
                },
                actor_id=actor.id,
            )
            if alert:
                generated_alerts.append(str(alert.id))

        response = {
            "weighing": weighing.to_dict(),
            "post_label": _serialize_label(post_label),
            "print_job_id": str(job.public_id),
            "alertas_generadas": generated_alerts,
            "atribucion_turnos": [
                segment.to_dict() for segment in segments
            ],
            "idempotent_replay": False,
        }
        session.add(
            _event(
                "MANGA",
                manga.id,
                "MANGA_WEIGHING_CONFIRMED",
                actor,
                operation,
                response,
            )
        )
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def annul_manga_weighing(
    session, *, actor_id, weighing_id, operation_id, data
):
    """Anula el pesaje conservando el hecho y liberando su cupo de plan."""
    reject_unknown_fields(data, allowed={"motivo", "evidencia"})
    actor = load_actor(session, actor_id, capability="ANULAR_PESAJE")
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    evidence = str(data.get("evidencia") or "").strip()[:500] or None
    command = {"motivo": reason, "evidencia": evidence}
    endpoint = f"/pesajes/{weighing_id}/anular"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, command
    )
    if replay is not None:
        return replay
    try:
        weighing = session.scalar(
            select(ScmPesajeManga)
            .where(ScmPesajeManga.public_id == weighing_id)
            .with_for_update()
        )
        if weighing is None:
            raise ScmServiceError(
                "WEIGHING_NOT_FOUND", "El pesaje SCM no existe.", status_code=404
            )
        manga = session.scalar(
            select(ScmManga)
            .where(ScmManga.id == weighing.manga_id)
            .with_for_update()
        )
        if manga.estado == "ANULADA" or weighing.anulacion is not None:
            raise ScmServiceError(
                "WEIGHING_ALREADY_ANNULLED",
                "El pesaje ya fue anulado.",
                status_code=409,
            )
        existence = session.scalar(
            select(ScmExistenciaManga)
            .where(
                ScmExistenciaManga.manga_id == manga.id,
                ScmExistenciaManga.estado_logistico != "REVERSADA",
            )
            .with_for_update()
        )
        if existence is not None:
            raise ScmServiceError(
                "RECEIPT_REVERSAL_REQUIRED",
                "La manga ya ingreso a Almacen; primero debe aprobarse la reversa de recepcion.",
                status_code=409,
                details={"existencia_id": str(existence.id)},
            )
        if manga.tipo != "NORMAL" or manga.asignacion_id is None:
            raise ScmServiceError(
                "NORMAL_REPLACEMENT_NOT_AVAILABLE",
                "Solo un pesaje de manga normal puede devolver cupo al plan.",
                status_code=409,
            )
        quantity = Decimal(manga.cantidad_asignada_un)
        segments = session.scalars(
            select(ScmTramoMangaTrabajo)
            .where(ScmTramoMangaTrabajo.manga_id == manga.id)
            .order_by(ScmTramoMangaTrabajo.secuencia)
            .with_for_update()
        ).all()
        now = utc_now()
        plan_projections = []
        work_reopened = False
        if segments:
            for segment in segments:
                amount = (
                    Decimal(segment.cantidad_fin_un)
                    - Decimal(segment.cantidad_inicio_un)
                ).quantize(UNIT_QUANTUM)
                assignment = session.get(
                    ScmAsignacionPlanMangaOt, segment.asignacion_plan_id
                )
                if (
                    assignment is None
                    or Decimal(assignment.cantidad_asignada_un) < amount
                    or (segment.secuencia == 1 and assignment.mangas_asignadas < 1)
                ):
                    raise ScmServiceError(
                        "PLAN_ASSIGNMENT_INCONSISTENT",
                        "Los cupos por tramo no permiten anular el pesaje final.",
                        status_code=409,
                    )
                assignment.cantidad_asignada_un = (
                    Decimal(assignment.cantidad_asignada_un) - amount
                )
                if segment.secuencia == 1:
                    assignment.mangas_asignadas -= 1
                segment.trabajo.cantidad_objetivo_un = max(
                    Decimal(segment.trabajo.cantidad_objetivo_un) - amount,
                    Decimal("0"),
                )
                segment.trabajo.cantidad_confirmada_un = max(
                    Decimal(segment.trabajo.cantidad_confirmada_un)
                    - Decimal(segment.cantidad_atribuida_un or 0),
                    Decimal("0"),
                )
                segment.trabajo.version += 1
                segment.estado = "ANULADO"
                plan_projections.append({
                    "asignacion_plan_id": assignment.id,
                    "cantidad_devuelta_un": format(amount, "f"),
                    "cantidad_asignada_un": format(
                        assignment.cantidad_asignada_un, "f"
                    ),
                    "mangas_asignadas": assignment.mangas_asignadas,
                })
            assignment = session.get(
                ScmAsignacionPlanMangaOt, manga.asignacion_id
            )
        else:
            assignment = session.scalar(
                select(ScmAsignacionPlanMangaOt)
                .where(ScmAsignacionPlanMangaOt.id == manga.asignacion_id)
                .with_for_update()
            )
            if (
                assignment is None
                or Decimal(assignment.cantidad_asignada_un) < quantity
                or assignment.mangas_asignadas < 1
            ):
                raise ScmServiceError(
                    "PLAN_ASSIGNMENT_INCONSISTENT",
                    "La asignacion del plan no permite devolver el cupo.",
                    status_code=409,
                )
            assignment.cantidad_asignada_un = (
                Decimal(assignment.cantidad_asignada_un) - quantity
            )
            assignment.mangas_asignadas -= 1
            plan_projections.append({
                "asignacion_plan_id": assignment.id,
                "cantidad_devuelta_un": format(quantity, "f"),
                "cantidad_asignada_un": format(
                    assignment.cantidad_asignada_un, "f"
                ),
                "mangas_asignadas": assignment.mangas_asignadas,
            })
        if manga.trabajo is not None and not segments:
            manga.trabajo.cantidad_objetivo_un = max(
                Decimal(manga.trabajo.cantidad_objetivo_un) - quantity,
                Decimal("0"),
            )
            manga.trabajo.cantidad_confirmada_un = max(
                Decimal(manga.trabajo.cantidad_confirmada_un) - quantity,
                Decimal("0"),
            )
            work_reopened = manga.trabajo.estado == "COMPLETADO"
            if work_reopened:
                manga.trabajo.estado = "PAUSADO"
                manga.trabajo.completada_at = None
                manga.trabajo.pausada_at = now
            has_replacement_assignment = any(
                item.estado in {"PREVISTA", "ACTIVA"}
                for item in manga.trabajo.asignaciones_personal
            )
            previous_personal = manga.asignacion_personal_trabajo
            if not has_replacement_assignment and previous_personal is not None:
                session.add(ScmAsignacionPersonalTrabajoOt(
                    trabajo_ot_id=manga.trabajo.id,
                    trabajador_id=previous_personal.trabajador_id,
                    estado="PREVISTA",
                    asignada_por_id=actor.id,
                    motivo="Reposicion requerida por anulacion de pesaje",
                ))
            manga.trabajo.version += 1
        ot_reopened = not segments and manga.ot.estado == "CERRADA"
        if ot_reopened:
            manga.ot.estado = "EN_EJECUCION"
            manga.ot.cerrada_at = None
            manga.ot.version += 1
        elif work_reopened:
            _recompute_parent_state(session, manga.trabajo)
        manga.estado = "ANULADA"
        manga.anulada_at = now
        manga.anulada_por_id = actor.id
        manga.motivo_anulacion = reason
        manga.version += 1
        for label in manga.etiquetas:
            if label.estado != "INVALIDADA":
                label.estado = "INVALIDADA"
                label.invalidada_por_id = actor.id
                label.invalidada_at = now
                label.motivo_invalidacion = f"Pesaje anulado: {reason}"
        pending_corrections = session.scalars(
            select(ScmCorreccionPesajeManga).where(
                ScmCorreccionPesajeManga.pesaje_id == weighing.id,
                ScmCorreccionPesajeManga.estado == "PENDIENTE",
            )
        ).all()
        for correction in pending_corrections:
            correction.estado = "RECHAZADA"
            correction.resolved_by_id = actor.id
            correction.resolved_at = now
            correction.resolution_reason = "Rechazada automaticamente por anulacion del pesaje."
        annulment = ScmAnulacionPesajeManga(
            pesaje_id=weighing.id,
            motivo=reason,
            evidencia=evidence,
            anulada_por_id=actor.id,
            operation_id=operation.operation_id,
            cantidad_devuelta_plan_un=quantity,
            ot_reabierta=ot_reopened,
        )
        session.add(annulment)
        session.flush()
        response = {
            "anulacion": annulment.to_dict(),
            "manga": _serialize_manga(manga),
            "plan": {
                "cantidad_devuelta_un": format(quantity, "f"),
                "cantidad_asignada_un": format(assignment.cantidad_asignada_un, "f"),
                "mangas_asignadas": assignment.mangas_asignadas,
                "asignaciones": plan_projections,
            },
            "ot_reabierta": ot_reopened,
            "trabajo_color_reabierto": work_reopened,
            "trabajo_color_id": (
                str(manga.trabajo_ot_id) if manga.trabajo_ot_id else None
            ),
        }
        session.add(_event(
            "PESAJE_MANGA", weighing.id, "MANGA_WEIGHING_ANNULLED",
            actor, operation, response,
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def get_operation_result(session, *, operation_id):
    operation = session.get(ScmOperacion, operation_id)
    if operation is None or operation.response_json is None:
        raise ScmServiceError(
            "OPERATION_NOT_FOUND",
            "La operacion no existe o aun no tiene resultado.",
            status_code=404,
        )
    return {
        "operation_id": str(operation.operation_id),
        "status": operation.estado_http,
        "response": operation.response_json,
    }


def get_label_print_payload(session, *, label_id):
    label = session.scalar(
        select(ScmEtiquetaManga).where(
            ScmEtiquetaManga.public_id == label_id
        )
    )
    if label is None:
        raise ScmServiceError(
            "LABEL_NOT_FOUND", "La etiqueta no existe.", status_code=404
        )
    if label.estado == "INVALIDADA":
        raise ScmServiceError(
            "LABEL_INVALIDATED",
            "La etiqueta fue invalidada.",
            status_code=409,
        )
    return _serialize_label(label)


def _effective_projection(weighing):
    applied = (
        ScmCorreccionPesajeManga.query.filter_by(
            pesaje_id=weighing.id,
            estado="APLICADA",
        )
        .order_by(ScmCorreccionPesajeManga.id.desc())
        .first()
    )
    if applied is not None:
        return dict(applied.result_projection_json)
    weighed_at = weighing.pesada_at
    if weighed_at.tzinfo is None:
        # SQLite strips the offset from timezone-aware DateTime values.
        # The weighing snapshot keeps the operational timezone needed to
        # recover an unambiguous value without weakening API validation.
        weighed_at = weighed_at.replace(
            tzinfo=ZoneInfo(weighing.timezone_snapshot)
        )
    return {
        "peso_bruto_kg": format(weighing.peso_bruto_kg, "f"),
        "tara_kg": format(weighing.tara_kg, "f"),
        "peso_fisico_neto_kg": format(
            weighing.peso_fisico_neto_kg, "f"
        ),
        "cantidad_confirmada": format(
            weighing.cantidad_confirmada, "f"
        ),
        "kg_produccion_ot": format(weighing.kg_produccion_ot, "f"),
        "pesada_at": weighed_at.isoformat(),
        "fecha_local_pesaje": weighing.fecha_local_pesaje.isoformat(),
        "dias_desfase_operativo": weighing.dias_desfase_operativo,
        "alerta_fecha": weighing.alerta_fecha,
    }


def get_manga_weighing(session, *, actor_id, manga_id):
    load_actor(session, actor_id, capability="MANGA_PESAJE_VER")
    manga = session.scalar(
        select(ScmManga).where(ScmManga.public_id == manga_id)
    )
    if manga is None:
        raise ScmServiceError(
            "MANGA_NOT_FOUND", "La manga no existe.", status_code=404
        )
    weighing = ScmPesajeManga.query.filter_by(
        manga_id=manga.id
    ).one_or_none()
    corrections = []
    if weighing is not None:
        corrections = (
            ScmCorreccionPesajeManga.query.filter_by(
                pesaje_id=weighing.id
            )
            .order_by(ScmCorreccionPesajeManga.id)
            .all()
        )
    return {
        "manga_id": str(manga.public_id),
        "manga_codigo": manga.codigo,
        "estado_manga": manga.estado,
        "estado_inventario": "NO_INGRESADA",
        "ubicacion_id": None,
        "original": weighing.to_dict() if weighing else None,
        "anulacion": (
            weighing.anulacion.to_dict()
            if weighing and weighing.anulacion else None
        ),
        "vigente": _effective_projection(weighing) if weighing and not weighing.anulacion else None,
        "correcciones": [item.to_dict() for item in corrections],
        "etiquetas_postpesaje": [
            _serialize_label(label)
            for label in manga.etiquetas
            if label.tipo == "POSTPESAJE"
        ],
    }


def request_weighing_correction(
    session, *, actor_id, weighing_id, operation_id, data
):
    actor = load_actor(
        session, actor_id, capability="PESAJE_CORRECCION_SOLICITAR"
    )
    endpoint = f"/pesajes/{weighing_id}/correcciones"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        weighing = session.scalar(
            select(ScmPesajeManga).where(
                ScmPesajeManga.public_id == weighing_id
            )
        )
        if weighing is None:
            raise ScmServiceError(
                "WEIGHING_NOT_FOUND",
                "El pesaje SCM no existe.",
                status_code=404,
            )
        proposed = data.get("proposed")
        if weighing.anulacion is not None or weighing.manga.estado == "ANULADA":
            raise ScmServiceError(
                "WEIGHING_ANNULLED",
                "Un pesaje anulado no admite correcciones.",
                status_code=409,
            )
        if not isinstance(proposed, dict) or not proposed:
            raise ScmServiceError(
                "CORRECTION_CHANGES_REQUIRED",
                "La correccion requiere al menos un valor propuesto.",
                status_code=400,
            )
        if (
            "cantidad_confirmada" in proposed
            and weighing.manga.tramos_trabajo
        ):
            raise ScmServiceError(
                "CONTINUITY_QUANTITY_CORRECTION_REQUIRES_BOUNDARY_FLOW",
                "La cantidad de una manga continuada se corrige mediante el flujo de fronteras; este formulario solo puede corregir peso o fecha.",
                status_code=409,
            )
        allowed = {
            "peso_bruto_kg",
            "tara_kg",
            "cantidad_confirmada",
            "pesada_at",
        }
        unknown = sorted(set(proposed) - allowed)
        if unknown:
            raise ScmServiceError(
                "UNKNOWN_FIELDS",
                "La correccion contiene campos no permitidos.",
                status_code=400,
                details={"fields": unknown},
            )
        normalized = {}
        if "peso_bruto_kg" in proposed:
            normalized["peso_bruto_kg"] = format(
                _kg(proposed["peso_bruto_kg"], "peso_bruto_kg"), "f"
            )
        if "tara_kg" in proposed:
            normalized["tara_kg"] = format(
                _kg(proposed["tara_kg"], "tara_kg", allow_zero=True), "f"
            )
        if "cantidad_confirmada" in proposed:
            normalized["cantidad_confirmada"] = format(
                _kg(
                    proposed["cantidad_confirmada"],
                    "cantidad_confirmada",
                ),
                "f",
            )
        if "pesada_at" in proposed:
            normalized["pesada_at"] = _aware_datetime(
                proposed["pesada_at"]
            ).isoformat()
        reason = required_text(
            data.get("motivo"), field="motivo", max_length=500
        )
        evidence = (data.get("evidencia") or "").strip()[:500] or None
        correction = ScmCorreccionPesajeManga(
            pesaje_id=weighing.id,
            proposed_json=normalized,
            reason=reason,
            evidence_reference=evidence,
            requested_by_id=actor.id,
            request_operation_id=operation.operation_id,
        )
        session.add(correction)
        session.flush()
        response = {"correction": correction.to_dict()}
        session.add(_event(
            "PESAJE_MANGA",
            weighing.id,
            "WEIGHING_CORRECTION_REQUESTED",
            actor,
            operation,
            response,
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def approve_weighing_correction(
    session, *, actor_id, correction_id, operation_id, data
):
    actor = load_actor(
        session, actor_id, capability="PESAJE_CORRECCION_APROBAR"
    )
    endpoint = f"/correcciones-pesaje/{correction_id}/aprobar"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        correction = session.scalar(
            select(ScmCorreccionPesajeManga)
            .where(ScmCorreccionPesajeManga.public_id == correction_id)
            .with_for_update()
        )
        if correction is None:
            raise ScmServiceError(
                "CORRECTION_NOT_FOUND",
                "La correccion no existe.",
                status_code=404,
            )
        if correction.estado != "PENDIENTE":
            raise ScmServiceError(
                "CORRECTION_ALREADY_RESOLVED",
                "La correccion ya fue resuelta.",
                status_code=409,
            )
        if correction.requested_by_id == actor.id:
            raise ScmServiceError(
                "FOUR_EYES_REQUIRED",
                "Solicitante y aprobador deben ser distintos.",
                status_code=409,
            )
        weighing = correction.pesaje
        manga = session.scalar(
            select(ScmManga)
            .where(ScmManga.id == weighing.manga_id)
            .with_for_update()
        )
        current = _effective_projection(weighing)
        previous_manga_quantity = Decimal(
            manga.cantidad_confirmada_un or current["cantidad_confirmada"]
        )
        proposed = correction.proposed_json
        if "cantidad_confirmada" in proposed and manga.tramos_trabajo:
            raise ScmServiceError(
                "CONTINUITY_QUANTITY_CORRECTION_REQUIRES_BOUNDARY_FLOW",
                "La cantidad de una manga continuada no puede redistribuirse desde esta aprobacion.",
                status_code=409,
            )
        if weighing.anulacion is not None or manga.estado == "ANULADA":
            raise ScmServiceError(
                "WEIGHING_ANNULLED",
                "Un pesaje anulado no admite correcciones.",
                status_code=409,
            )
        gross = _kg(
            proposed.get("peso_bruto_kg", current["peso_bruto_kg"]),
            "peso_bruto_kg",
        )
        tare = _kg(
            proposed.get("tara_kg", current["tara_kg"]),
            "tara_kg",
            allow_zero=True,
        )
        net = (gross - tare).quantize(KG_QUANTUM)
        if net <= 0:
            raise ScmServiceError(
                "INVALID_TARE",
                "La tara corregida debe ser menor que el bruto.",
                status_code=422,
            )
        _assert_final_weight_cumulative(session, manga, net)
        quantity = _kg(
            proposed.get(
                "cantidad_confirmada", current["cantidad_confirmada"]
            ),
            "cantidad_confirmada",
        )
        weighed_at = _aware_datetime(
            proposed.get("pesada_at", current["pesada_at"])
        )
        local_date = weighed_at.astimezone(
            ZoneInfo(weighing.timezone_snapshot)
        ).date()
        drift = (local_date - manga.ot.fecha).days
        if drift < 0:
            raise ScmServiceError(
                "OPERATIONAL_DATE_IN_FUTURE",
                "La correccion no puede quedar antes de la fecha OT.",
                status_code=422,
            )
        production_kg = (
            quantity * Decimal(manga.peso_unitario_snapshot_g) / 1000
        ).quantize(KG_QUANTUM)
        projection = {
            "peso_bruto_kg": format(gross, "f"),
            "tara_kg": format(tare, "f"),
            "peso_fisico_neto_kg": format(net, "f"),
            "cantidad_confirmada": format(quantity, "f"),
            "kg_produccion_ot": format(production_kg, "f"),
            "pesada_at": weighed_at.isoformat(),
            "fecha_local_pesaje": local_date.isoformat(),
            "dias_desfase_operativo": drift,
            "alerta_fecha": drift > 1,
        }
        existence = session.scalar(
            select(ScmExistenciaManga)
            .where(
                ScmExistenciaManga.manga_id == manga.id,
                ScmExistenciaManga.estado_logistico == "RECIBIDA_ALMACEN",
            )
            .with_for_update()
        )
        inventory_adjustment = None
        if existence is not None:
            balance = session.scalar(
                select(ScmSaldoInventario)
                .where(ScmSaldoInventario.id == existence.saldo_id)
                .with_for_update()
            )
            previous_quantity = Decimal(existence.cantidad_fisica)
            quantity_delta = (quantity - previous_quantity).quantize(
                KG_QUANTUM
            )
            if quantity < Decimal(existence.cantidad_reservada):
                raise ScmServiceError(
                    "RECEIVED_MANGA_QUANTITY_RESERVED",
                    "La correccion deja la manga por debajo de su reserva.",
                    status_code=409,
                )
            resulting_physical = (
                Decimal(balance.cantidad_fisica) + quantity_delta
            ).quantize(KG_QUANTUM)
            resulting_unavailable = Decimal(
                balance.cantidad_no_disponible
            )
            if existence.estado_calidad != "LIBERADA":
                resulting_unavailable = (
                    resulting_unavailable + quantity_delta
                ).quantize(KG_QUANTUM)
            if (
                resulting_physical < 0
                or resulting_unavailable < 0
                or Decimal(balance.cantidad_reservada)
                + resulting_unavailable > resulting_physical
            ):
                raise ScmServiceError(
                    "RECEIVED_MANGA_INVENTORY_CONFLICT",
                    "El Kardex posee reservas o bloqueos incompatibles con la correccion.",
                    status_code=409,
                )
            balance.cantidad_fisica = resulting_physical
            balance.cantidad_no_disponible = resulting_unavailable
            balance.version += 1
            existence.cantidad_fisica = quantity
            existence.peso_neto_snapshot_kg = net
            existence.version += 1
            if quantity_delta != 0:
                movement = ScmMovimientoInventario(
                    saldo=balance,
                    tipo=(
                        "AJUSTE_POSITIVO"
                        if quantity_delta > 0 else "AJUSTE_NEGATIVO"
                    ),
                    cantidad_delta=quantity_delta,
                    saldo_fisico_resultante=resulting_physical,
                    motivo=(
                        "Compensacion por correccion autorizada del pesaje "
                        f"de {manga.codigo}"
                    ),
                    referencia_tipo="CORRECCION_PESAJE_MANGA",
                    referencia_id=str(correction.public_id),
                    actor_id=actor.id,
                    operation_id=operation.operation_id,
                )
                session.add(movement)
                session.flush()
                inventory_adjustment = {
                    "movimiento_id": str(movement.id),
                    "cantidad_delta": format(quantity_delta, "f"),
                    "saldo_fisico_resultante": format(
                        resulting_physical, "f"
                    ),
                }
        correction.estado = "APLICADA"
        correction.resolved_by_id = actor.id
        correction.resolved_at = utc_now()
        correction.approval_operation_id = operation.operation_id
        correction.resolution_reason = (
            (data.get("motivo_aprobacion") or "").strip()[:500] or None
        )
        correction.result_projection_json = projection
        manga.cantidad_confirmada_un = quantity
        manga.cantidad_contenida_un = quantity
        manga.estado = "RECIBIDA" if existence is not None else "PESADA"
        manga.version += 1
        if manga.trabajo is not None:
            delta = quantity - previous_manga_quantity
            manga.trabajo.cantidad_confirmada_un = max(
                Decimal(manga.trabajo.cantidad_confirmada_un or 0) + delta,
                Decimal("0"),
            )
            manga.trabajo.version += 1
        for label in manga.etiquetas:
            if (
                label.tipo == "POSTPESAJE"
                and label.estado != "INVALIDADA"
            ):
                label.estado = "INVALIDADA"
                label.invalidada_por_id = actor.id
                label.invalidada_at = utc_now()
                label.motivo_invalidacion = (
                    f"Correccion de pesaje: {correction.reason}"
                )

        version = max(
            (
                label.version
                for label in manga.etiquetas
                if label.tipo == "POSTPESAJE"
            ),
            default=0,
        ) + 1
        label_id = uuid.uuid4()
        projected_weighing = SimpleNamespace(
            peso_fisico_neto_kg=net,
            kg_produccion_ot=production_kg,
            cantidad_confirmada=quantity,
            fuente_cantidad="CORRECCION_AUTORIZADA",
        )
        payload = _post_label_payload(
            manga, projected_weighing, label_id, version
        )
        job = ScmTrabajoImpresionManga(
            plantilla_version=payload["template"]["version"],
            payload_hash=_json_hash([payload]),
            solicitado_por_id=actor.id,
            station_id=weighing.station_id,
        )
        session.add(job)
        session.flush()
        label = ScmEtiquetaManga(
            public_id=label_id,
            manga_id=manga.id,
            trabajo_impresion_id=job.public_id,
            tipo="POSTPESAJE",
            version=version,
            plantilla_version=payload["template"]["version"],
            payload_json=payload,
            payload_hash=_json_hash(payload),
        )
        session.add(label)
        session.flush()
        generated_alerts = []
        correction_delay_hours = _elapsed_hours(correction.resolved_at, weighing.pesada_at)
        if _exceeds_alert_threshold(
            session, "CORRECCION_PESAJE_TARDIA", correction_delay_hours
        ):
            alert = upsert_operational_alert(
                session,
                rule_code="CORRECCION_PESAJE_TARDIA",
                aggregate_type="CORRECCION_PESAJE_MANGA",
                aggregate_id=correction.public_id,
                condition_key=f"aplicada:{correction.resolved_at.isoformat()}",
                summary=f"Correccion tardia del pesaje de {manga.codigo}",
                detail={
                    "manga": manga.codigo,
                    "pesaje_id": str(weighing.public_id),
                    "pesada_at": weighing.pesada_at.isoformat(),
                    "corregida_at": correction.resolved_at.isoformat(),
                    "horas_transcurridas": format(correction_delay_hours.quantize(Decimal('0.001')), "f"),
                },
                actor_id=actor.id,
            )
            if alert:
                generated_alerts.append(str(alert.id))
        response = {
            "correction": correction.to_dict(),
            "print_job_id": str(job.public_id),
            "post_label": _serialize_label(label),
            "alertas_generadas": generated_alerts,
            "ajuste_inventario": inventory_adjustment,
        }
        session.add(_event(
            "PESAJE_MANGA",
            weighing.id,
            "WEIGHING_CORRECTION_APPLIED",
            actor,
            operation,
            response,
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
