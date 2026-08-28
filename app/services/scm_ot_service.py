"""Application services for the TS-010C OP -> OT -> manga slice."""

import copy
import hashlib
import json
import math
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import String, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models.lote import LoteSalidaPiezaColor
from app.models.maquina import Maquina
from app.models.orden import OrdenProduccion
from app.models.registro import RegistroDiarioProduccion
from app.models.producto import ColorProduccion
from app.models.scm_articulos import ScmArticulo, ScmArticuloPiezaColor
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_empaque import (
    ScmArticuloPerfil,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_ot import (
    ScmAsignacionPersonalTrabajoOt,
    ScmAsignacionPlanMangaOt,
    ScmEtiquetaManga,
    ScmControlPesoManga,
    ScmLoteArticulo,
    ScmManga,
    ScmPlanMangaOp,
    ScmPlanMangaOpLinea,
    ScmSolicitudMangaExtra,
    ScmTrabajoImpresionManga,
    ScmTrabajoColor,
    ScmTrabajoOt,
    ScmTramoMangaTrabajo,
    utc_now,
)
from app.models.scm_internal_supply import ScmSolicitudAbastecimiento
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.models.scm_rutas import ScmCentroTrabajo
from app.models.trabajador import Trabajador
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_color_identity import serialize_color_identity
from app.services.scm_packaging_service import calculate_packaging_capacity
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)


def _json_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _operation_hash(endpoint, actor_id, data):
    return _json_hash({
        "endpoint": endpoint,
        "actor_id": actor_id,
        "data": data,
    })


def _operation_replay(existing, *, endpoint, request_hash):
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


def _reserve_operation(session, operation_id, endpoint, actor, data):
    request_hash = _operation_hash(endpoint, actor.id, data)
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        return _operation_replay(
            existing, endpoint=endpoint, request_hash=request_hash
        )
    operation = ScmOperacion(
        operation_id=operation_id,
        endpoint=endpoint,
        actor_id=actor.id,
        request_sha256=request_hash,
    )
    try:
        # The savepoint keeps the surrounding command usable when two
        # stations race with the same idempotency key. PostgreSQL waits for
        # the winner and the loser can then read the committed response.
        with session.begin_nested():
            session.add(operation)
            session.flush()
    except IntegrityError:
        session.expire_all()
        existing = session.get(ScmOperacion, operation_id)
        if existing is None:
            raise
        return _operation_replay(
            existing, endpoint=endpoint, request_hash=request_hash
        )
    return operation, None


def _complete_operation(operation, response, status=201):
    operation.response_json = copy.deepcopy(response)
    operation.estado_http = status


def _event(aggregate_type, aggregate_id, event_type, actor, operation, after):
    return ScmEvento(
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        after_json=after,
        operation_id=operation.operation_id if operation else None,
    )


def _decimal(value, field, *, integral=False):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} debe ser una cantidad positiva.",
            status_code=422,
            details={"field": field},
        ) from error
    if not parsed.is_finite() or parsed <= 0:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} debe ser una cantidad positiva.",
            status_code=422,
            details={"field": field},
        )
    if integral and parsed != parsed.to_integral_value():
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} debe expresarse en unidades enteras.",
            status_code=422,
            details={"field": field},
        )
    return parsed.quantize(Decimal("0.001"))


def _parse_date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_DATE",
            "fecha_operativa debe usar YYYY-MM-DD.",
            status_code=422,
        ) from error


def _compact_number(value):
    number = Decimal(value)
    return format(number.normalize(), "f")


_SERIALIZATION_CONTEXT_UNSET = object()


def _manga_code(op_number, ot_code, sequence):
    op_part = re.sub(r"[^A-Z0-9]", "", str(op_number).upper())
    ot_digits = re.sub(r"\D", "", str(ot_code))[-3:].zfill(3)
    return f"{op_part}-OT{ot_digits}-M{sequence:03d}"


def _serialize_plan_line(line):
    assigned = sum(
        (Decimal(item.cantidad_asignada_un) for item in line.asignaciones),
        Decimal("0"),
    )
    objective = Decimal(line.cantidad_objetivo_un)
    return {
        "id": line.id,
        "lote_salida_pieza_color_id": line.lote_salida_pieza_color_id,
        "orden_operacion_salida_id": (
            str(line.orden_operacion_salida_id)
            if line.orden_operacion_salida_id else None
        ),
        "corrida_fabricacion_id": (
            str(line.salida_canonica.corrida_fabricacion_id)
            if line.salida_canonica is not None
            and line.salida_canonica.corrida_fabricacion_id
            else None
        ),
        "lote_articulo_id": line.lote_articulo_id,
        "articulo": {
            "codigo": line.articulo_codigo_snapshot,
            "nombre": line.articulo_nombre_snapshot,
        },
        "pieza_color_sku": line.pieza_color_sku_snapshot,
        "color": line.color_snapshot,
        "cantidad_objetivo_un": _compact_number(objective),
        "capacidad_efectiva_un": line.capacidad_efectiva_un,
        "mangas_propuestas": line.mangas_propuestas,
        "cantidad_asignada_un": _compact_number(assigned),
        "saldo_un": _compact_number(max(objective - assigned, Decimal("0"))),
        "tipo_manga": {
            "id": line.tipo_contenedor_id,
            "codigo": line.tipo_contenedor.codigo,
            "nombre": line.tipo_contenedor.nombre,
        },
        "regla_revision_id": line.regla_revision_id,
    }


def _serialize_plan(plan):
    return {
        "id": plan.id,
        "orden_id": plan.orden_id,
        "orden_operacion_id": (
            str(plan.orden_operacion_id)
            if plan.orden_operacion_id else None
        ),
        "orden_fabricacion_codigo": (
            plan.orden_operacion.codigo
            if plan.orden_operacion else None
        ),
        "revision": plan.revision,
        "estado": plan.estado,
        "content_hash": plan.content_hash,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "lineas": [_serialize_plan_line(line) for line in plan.lineas],
    }


def _latest_manga_segment(manga):
    segments = list(getattr(manga, "tramos_trabajo", ()) or ())
    return max(segments, key=lambda item: item.secuencia) if segments else None


def _serialize_manga_segment(segment):
    if segment is None:
        return None
    payload = segment.to_dict()
    payload["control_peso"] = (
        segment.control_peso.to_dict() if segment.control_peso else None
    )
    return payload


def _serialize_manga(manga):
    current_label = next(
        (
            label for label in reversed(manga.etiquetas)
            if label.estado != "INVALIDADA"
        ),
        None,
    )
    current_segment = _latest_manga_segment(manga)
    current_work = current_segment.trabajo if current_segment else manga.trabajo
    current_assignment = (
        current_segment.asignacion_personal_trabajo
        if current_segment else manga.asignacion_personal_trabajo
    )
    latest_control = next(
        iter(reversed(list(getattr(manga, "controles_peso", ()) or ()))),
        None,
    )
    assigned = Decimal(manga.cantidad_asignada_un)
    accumulated = (
        Decimal(latest_control.conteo_acumulado_un)
        if latest_control is not None else Decimal("0")
    )
    return {
        "id": manga.id,
        "public_id": str(manga.public_id),
        "codigo": manga.codigo,
        "trabajo_color_id": (
            str(manga.trabajo_ot_id) if manga.trabajo_ot_id else None
        ),
        "trabajo_color_codigo": (
            manga.trabajo.codigo if manga.trabajo else None
        ),
        "trabajo_color_origen_id": (
            str(manga.trabajo_ot_id) if manga.trabajo_ot_id else None
        ),
        "trabajo_color_origen_codigo": (
            manga.trabajo.codigo if manga.trabajo else None
        ),
        "trabajo_color_actual_id": (
            str(current_work.id) if current_work else None
        ),
        "trabajo_color_actual_codigo": (
            current_work.codigo if current_work else None
        ),
        "asignacion_personal_trabajo_id": (
            str(manga.asignacion_personal_trabajo_id)
            if manga.asignacion_personal_trabajo_id else None
        ),
        "tipo": manga.tipo,
        "estado": manga.estado,
        "secuencia_ot": manga.secuencia_ot,
        "cantidad_planificada_un": _compact_number(
            manga.cantidad_planificada_un
        ),
        "cantidad_asignada_un": _compact_number(
            manga.cantidad_asignada_un
        ),
        "cantidad_confirmada_un": (
            _compact_number(manga.cantidad_confirmada_un)
            if manga.cantidad_confirmada_un is not None else None
        ),
        "pieza_color_sku": manga.pieza_color_sku_snapshot,
        "articulo_nombre": manga.articulo_nombre_snapshot,
        "color": manga.color_snapshot,
        "tipo_manga": manga.tipo_contenedor_nombre_snapshot,
        "maquinista_previsto_id": manga.maquinista_previsto_id,
        "maquinista": (
            manga.maquinista_previsto.nombre_completo
            if manga.maquinista_previsto else None
        ),
        "maquinista_actual": (
            current_assignment.trabajador.nombre_completo
            if current_assignment and current_assignment.trabajador else None
        ),
        "motivo_extra": manga.motivo_extra,
        "version": manga.version,
        "etiqueta_vigente": _serialize_label(current_label)
        if current_label else None,
        "continuidad": {
            "estado": (
                "PENDIENTE_VINCULO"
                if manga.estado == "CONTINUIDAD_PENDIENTE"
                and (
                    current_segment is None
                    or current_segment.estado == "CERRADO"
                )
                else (
                    current_segment.estado if current_segment else "SIN_CORTE"
                )
            ),
            "conteo_acumulado_un": _compact_number(accumulated),
            "cantidad_pendiente_un": _compact_number(
                max(assigned - accumulated, Decimal("0"))
            ),
            "ultimo_control": (
                latest_control.to_dict() if latest_control else None
            ),
            "tramo_actual": _serialize_manga_segment(current_segment),
            "tramos": [
                _serialize_manga_segment(item)
                for item in (getattr(manga, "tramos_trabajo", ()) or ())
            ],
            "qr_preservado": bool(current_segment),
        },
    }


def _work_mangas(work):
    """Return origin and inherited mangas once for this work context."""
    values = list(work.mangas)
    seen = {item.id for item in values}
    for segment in getattr(work, "tramos_manga", ()) or ():
        if segment.manga_id not in seen:
            values.append(segment.manga)
            seen.add(segment.manga_id)
    return values


def _manga_resolved_for_work(manga, work):
    if manga.estado in _terminal_manga_states():
        return True
    segments = list(getattr(manga, "tramos_trabajo", ()) or ())
    own = [item for item in segments if item.trabajo_ot_id == work.id]
    if not own:
        return False
    latest_own = max(own, key=lambda item: item.secuencia)
    later = [item for item in segments if item.secuencia > latest_own.secuencia]
    return latest_own.estado == "CERRADO" and bool(later)


def _work_confirmed_quantity(work):
    segmented_ids = {
        item.manga_id for item in (getattr(work, "tramos_manga", ()) or ())
    }
    segmented = sum(
        (
            Decimal(item.cantidad_atribuida_un or 0)
            for item in (getattr(work, "tramos_manga", ()) or ())
            if item.estado != "ANULADO"
        ),
        Decimal("0"),
    )
    direct = sum(
        (
            Decimal(item.cantidad_confirmada_un or 0)
            for item in work.mangas
            if item.id not in segmented_ids and item.estado != "ANULADA"
        ),
        Decimal("0"),
    )
    return segmented + direct


def _serialize_manga_for_work(manga, work):
    payload = _serialize_manga(manga)
    payload["heredada_de_ot_anterior"] = (
        manga.trabajo_ot_id is not None and manga.trabajo_ot_id != work.id
    )
    payload["resuelta_para_trabajo"] = _manga_resolved_for_work(manga, work)
    return payload


def _active_work_assignment(work):
    if work is None:
        return None
    return next(
        (
            item for item in reversed(work.asignaciones_personal)
            if item.estado == "ACTIVA"
        ),
        None,
    )


def _current_or_planned_work_assignment(work):
    if work is None:
        return None
    active = _active_work_assignment(work)
    if active is not None:
        return active
    return next(
        (
            item for item in reversed(work.asignaciones_personal)
            if item.estado == "PREVISTA"
        ),
        None,
    )


def _serialize_person_assignment(item):
    return {
        "id": str(item.id),
        "trabajo_color_id": str(item.trabajo_ot_id),
        "trabajador_id": item.trabajador_id,
        "trabajador": (
            item.trabajador.nombre_completo if item.trabajador else None
        ),
        "estado": item.estado,
        "asignada_at": item.asignada_at.isoformat()
        if item.asignada_at else None,
        "iniciada_at": item.iniciada_at.isoformat()
        if item.iniciada_at else None,
        "finalizada_at": item.finalizada_at.isoformat()
        if item.finalizada_at else None,
        "motivo": item.motivo,
        "version": item.version,
    }


def _serialize_article_summary(article):
    if article is None:
        return None
    return {
        "id": article.id,
        "codigo": article.codigo,
        "nombre": article.nombre,
        "clase": article.clase,
        "unidad": article.unidad_base,
    }


def _deduplicated_output_articles(outputs):
    articles = []
    seen = set()
    for output in outputs:
        article = output.articulo
        if article is None or article.id in seen:
            continue
        seen.add(article.id)
        articles.append(_serialize_article_summary(article))
    return articles


def _work_output_articles(work):
    color = work.trabajo_color
    run_outputs = (
        list(color.corrida.salidas)
        if color is not None and color.corrida is not None
        else []
    )
    if run_outputs:
        return _deduplicated_output_articles(run_outputs)
    operation_outputs = (
        list(work.orden_operacion.salidas)
        if work.orden_operacion is not None
        else []
    )
    return _deduplicated_output_articles(operation_outputs)


def _serialize_color_work(work, *, output_articles=None):
    color = work.trabajo_color
    catalog_color = (
        color.corrida.color_produccion
        if color and color.corrida else None
    )
    color_identity = serialize_color_identity(
        catalog_color,
        color_id=color.color_id_snapshot if color else None,
        name_snapshot=color.color_nombre_snapshot if color else None,
    )
    color_name = color_identity["nombre"] if color_identity else None
    active_assignment = _active_work_assignment(work)
    current_assignment = _current_or_planned_work_assignment(work)
    return {
        "id": str(work.id),
        "codigo": work.codigo,
        "tipo": work.tipo,
        "secuencia": work.secuencia,
        "estado": work.estado,
        "version": work.version,
        "continua_de_id": (
            str(work.continua_de_id) if work.continua_de_id else None
        ),
        "ot_id": str(work.orden_trabajo.public_id),
        "orden_fabricacion_id": str(work.orden_operacion_id),
        "orden_fabricacion_codigo": (
            work.orden_operacion.codigo if work.orden_operacion else None
        ),
        "corrida_fabricacion_id": (
            str(color.corrida_fabricacion_id) if color else None
        ),
        "corrida_codigo": (
            color.corrida.codigo if color and color.corrida else None
        ),
        "color": color_name,
        "color_nombre": color_name,
        "color_hex": color_identity["hex"] if color_identity else None,
        "color_identidad": color_identity,
        "articulos_salida": (
            output_articles
            if output_articles is not None
            else _work_output_articles(work)
        ),
        "cantidad_objetivo_un": _compact_number(
            work.cantidad_objetivo_un
        ),
        "cantidad_confirmada_un": _compact_number(
            work.cantidad_confirmada_un
        ),
        "iniciada_at": work.iniciada_at.isoformat()
        if work.iniciada_at else None,
        "pausada_at": work.pausada_at.isoformat()
        if work.pausada_at else None,
        "completada_at": work.completada_at.isoformat()
        if work.completada_at else None,
        "asignaciones_personal": [
            _serialize_person_assignment(item)
            for item in work.asignaciones_personal
        ],
        "asignacion_activa": (
            _serialize_person_assignment(active_assignment)
            if active_assignment else None
        ),
        "asignacion_vigente": (
            _serialize_person_assignment(current_assignment)
            if current_assignment else None
        ),
        "mangas": [
            _serialize_manga_for_work(item, work) for item in _work_mangas(work)
        ],
        "continuidades_heredadas": [
            _serialize_manga_segment(item)
            for item in (getattr(work, "tramos_manga", ()) or ())
            if item.secuencia > 1
        ],
    }


def _serialize_assembly_order_context(order):
    if order is None:
        return None
    output = order.salidas[0] if order.salidas else None
    return {
        "id": str(order.id),
        "codigo": order.codigo,
        "salida": (
            {
                "articulo": _serialize_article_summary(output.articulo),
                "cantidad_objetivo": _compact_number(
                    output.cantidad_objetivo
                ),
            }
            if output is not None
            else None
        ),
    }


def _serialize_supply_summary(request):
    if request is None:
        return None
    return {
        "codigo": request.codigo,
        "estado": request.estado,
    }


def _batch_work_output_articles(session, works):
    works_by_id = {work.id: work for work in works if work is not None}
    if not works_by_id:
        return {}
    metadata = session.execute(
        select(
            ScmTrabajoOt.id,
            ScmTrabajoOt.orden_operacion_id,
            ScmTrabajoColor.corrida_fabricacion_id,
        )
        .outerjoin(
            ScmTrabajoColor,
            ScmTrabajoColor.trabajo_ot_id == ScmTrabajoOt.id,
        )
        .where(ScmTrabajoOt.id.in_(works_by_id))
    ).all()
    run_ids = {
        row.corrida_fabricacion_id
        for row in metadata
        if row.corrida_fabricacion_id is not None
    }
    operation_ids = {row.orden_operacion_id for row in metadata}
    filters = []
    if run_ids:
        filters.append(
            ScmOrdenOperacionSalida.corrida_fabricacion_id.in_(run_ids)
        )
    if operation_ids:
        filters.append(
            ScmOrdenOperacionSalida.orden_operacion_id.in_(operation_ids)
        )
    outputs = session.scalars(
        select(ScmOrdenOperacionSalida)
        .options(selectinload(ScmOrdenOperacionSalida.articulo))
        .where(or_(*filters))
        .order_by(ScmOrdenOperacionSalida.id)
    ).all() if filters else []
    by_run = {}
    by_operation = {}
    for output in outputs:
        by_operation.setdefault(output.orden_operacion_id, []).append(output)
        if output.corrida_fabricacion_id is not None:
            by_run.setdefault(output.corrida_fabricacion_id, []).append(output)
    return {
        row.id: _deduplicated_output_articles(
            by_run.get(row.corrida_fabricacion_id)
            or by_operation.get(row.orden_operacion_id, [])
        )
        for row in metadata
    }


def _batch_ot_serialization_context(session, items):
    assembly_items = [item for item in items if item.tipo_ot == "ENSAMBLE"]
    order_ids = {
        item.orden_operacion_id
        for item in assembly_items
        if item.orden_operacion_id is not None
    }
    orders_by_id = {}
    if order_ids:
        orders = session.scalars(
            select(ScmOrdenOperacion)
            .options(
                selectinload(ScmOrdenOperacion.salidas).selectinload(
                    ScmOrdenOperacionSalida.articulo
                )
            )
            .where(ScmOrdenOperacion.id.in_(order_ids))
        ).all()
        orders_by_id = {order.id: order for order in orders}
    requests_by_ot = {}
    assembly_ot_ids = {item.id for item in assembly_items}
    if assembly_ot_ids:
        requests = session.scalars(
            select(ScmSolicitudAbastecimiento).where(
                ScmSolicitudAbastecimiento.orden_trabajo_id.in_(
                    assembly_ot_ids
                )
            )
        ).all()
        requests_by_ot = {
            request.orden_trabajo_id: request for request in requests
        }
    assembly_by_ot = {
        item.id: (
            orders_by_id.get(item.orden_operacion_id),
            requests_by_ot.get(item.id),
        )
        for item in assembly_items
    }

    context_ids = {
        item.trabajo_color_contexto_id
        for item in items
        if item.trabajo_color_contexto_id is not None
    }
    context_works = session.scalars(
        select(ScmTrabajoOt).where(ScmTrabajoOt.id.in_(context_ids))
    ).all() if context_ids else []
    context_by_id = {work.id: work for work in context_works}
    color_context_by_ot = {
        item.id: context_by_id.get(item.trabajo_color_contexto_id)
        for item in items
    }
    all_works = {
        work.id: work
        for item in items
        for work in item.trabajos_ot
        if work.tipo == "COLOR"
    }
    all_works.update(context_by_id)
    return {
        "assembly_by_ot": assembly_by_ot,
        "color_context_by_ot": color_context_by_ot,
        "output_articles_by_work": _batch_work_output_articles(
            session, all_works.values()
        ),
    }


def _serialize_ot(
    ot,
    *,
    mangas=None,
    assembly_context=_SERIALIZATION_CONTEXT_UNSET,
    color_context=_SERIALIZATION_CONTEXT_UNSET,
    output_articles_by_work=None,
):
    if mangas is None:
        mangas = ScmManga.query.filter_by(ot_id=ot.id).order_by(
            ScmManga.secuencia_ot
        ).all()
    payload = ot.to_dict()
    payload["orden_id"] = ot.orden_id
    contextual_work = (
        ot.trabajo_color_contexto
        if color_context is _SERIALIZATION_CONTEXT_UNSET
        else color_context
    )
    payload["trabajo_color_contexto"] = (
        _serialize_color_work(
            contextual_work,
            output_articles=(
                output_articles_by_work.get(contextual_work.id, [])
                if output_articles_by_work is not None
                else None
            ),
        )
        if contextual_work is not None
        else None
    )
    payload["mangas"] = [_serialize_manga(manga) for manga in mangas]
    color_works = [
        item for item in ot.trabajos_ot if item.tipo == "COLOR"
    ]
    payload["trabajos_color"] = [
        _serialize_color_work(
            item,
            output_articles=(
                output_articles_by_work.get(item.id, [])
                if output_articles_by_work is not None
                else None
            ),
        )
        for item in color_works
    ]
    if ot.tipo_ot == "ENSAMBLE":
        if assembly_context is _SERIALIZATION_CONTEXT_UNSET:
            order = ot.orden_operacion
            request = ScmSolicitudAbastecimiento.query.filter_by(
                orden_trabajo_id=ot.id
            ).one_or_none()
        else:
            order, request = assembly_context
        payload["orden_armado"] = _serialize_assembly_order_context(order)
        payload["abastecimiento"] = _serialize_supply_summary(request)
    if color_works:
        objective = sum(
            (Decimal(item.cantidad_objetivo_un or 0) for item in color_works),
            Decimal("0"),
        )
        confirmed = sum(
            (Decimal(item.cantidad_confirmada_un or 0) for item in color_works),
            Decimal("0"),
        )
        payload["cantidad_objetivo"] = _compact_number(objective)
        payload["cantidad_confirmada"] = _compact_number(confirmed)
        payload["cantidad_objetivo_un"] = _compact_number(objective)
        payload["cantidad_confirmada_un"] = _compact_number(confirmed)
    return payload


def _serialize_label(label):
    if label is None:
        return None
    return {
        "id": label.id,
        "public_id": str(label.public_id),
        "manga_id": str(label.manga.public_id),
        "manga_codigo": label.manga.codigo,
        "tipo": label.tipo,
        "version": label.version,
        "estado": label.estado,
        "plantilla_version": label.plantilla_version,
        "payload": label.payload_json,
        "payload_hash": label.payload_hash,
        "printed_at": label.printed_at.isoformat()
        if label.printed_at else None,
    }


def _packaging_rule_diagnostic(session, article_id):
    article = session.get(ScmArticulo, article_id)
    profile_links = session.scalars(
        select(ScmArticuloPerfil)
        .options(selectinload(ScmArticuloPerfil.perfil))
        .where(ScmArticuloPerfil.articulo_id == article_id)
        .order_by(ScmArticuloPerfil.id)
    ).all()
    active_links = [
        item
        for item in profile_links
        if item.activo and item.perfil is not None and item.perfil.activo
    ]
    default_links = [
        item for item in active_links if item.es_predeterminado
    ]
    active_profile_ids = {
        item.perfil_empacable_id for item in active_links
    }
    approved_rules = (
        session.scalars(
            select(ScmReglaEmpaqueRevision)
            .join(ScmReglaEmpaque)
            .join(ScmTipoContenedor)
            .where(
                ScmReglaEmpaque.perfil_empacable_id.in_(
                    active_profile_ids
                ),
                ScmTipoContenedor.clase == "MANGA",
                ScmTipoContenedor.activo.is_(True),
                ScmReglaEmpaqueRevision.estado == "APROBADA",
                ScmReglaEmpaqueRevision.medicion_fisica_probada.is_(True),
            )
            .order_by(ScmReglaEmpaqueRevision.id)
        ).all()
        if active_profile_ids
        else []
    )
    default_profile_ids = {
        item.perfil_empacable_id for item in default_links
    }
    default_rules = [
        item
        for item in approved_rules
        if item.regla.perfil_empacable_id in default_profile_ids
    ]
    article_payload = {
        "id": article_id,
        "codigo": article.codigo if article is not None else None,
        "nombre": article.nombre if article is not None else None,
        "clase": article.clase if article is not None else None,
    }
    article_label = (
        article.codigo if article is not None else f"Articulo #{article_id}"
    )
    return {
        "article": article,
        "article_label": article_label,
        "profile_links": profile_links,
        "default_links": default_links,
        "default_rules": default_rules,
        "details": {
            # ``articulo_id`` y ``approved_rules`` se conservan para clientes
            # anteriores; los bloques estructurados permiten explicar el
            # bloqueo sin pedir al usuario que inspeccione la base de datos.
            "articulo_id": article_id,
            "approved_rules": len(default_rules),
            "articulo": article_payload,
            "perfiles": {
                "asignados": len(profile_links),
                "activos": len(active_links),
                "predeterminados_activos": len(default_links),
                "items": [
                    {
                        "perfil_empacable_id": item.perfil_empacable_id,
                        "codigo": (
                            item.perfil.codigo
                            if item.perfil is not None else None
                        ),
                        "nombre": (
                            item.perfil.nombre
                            if item.perfil is not None else None
                        ),
                        "asignacion_activa": item.activo,
                        "perfil_activo": bool(
                            item.perfil is not None and item.perfil.activo
                        ),
                        "es_predeterminado": item.es_predeterminado,
                    }
                    for item in profile_links
                ],
            },
            "reglas": {
                "manga_aprobadas_para_perfiles_activos": len(
                    approved_rules
                ),
                "manga_aprobadas_para_predeterminado": len(
                    default_rules
                ),
                "medicion_fisica_requerida": True,
            },
            "accion": {
                "etiqueta": f"Revisar empaque de {article_label}",
                "ruta": (
                    "/datos-maestros/ingenieria-scm"
                    f"?tab=empaque&articulo={article_id}"
                ),
                "requiere_validacion_fisica": True,
            },
        },
    }


def _approved_manga_rule(session, article_id):
    diagnostic = _packaging_rule_diagnostic(session, article_id)
    profiles = diagnostic["default_links"]
    if len(profiles) != 1:
        raise ScmServiceError(
            "PACKAGING_RULE_MISSING",
            (
                f"{diagnostic['article_label']} requiere exactamente un "
                "perfil de empaque activo y predeterminado."
            ),
            status_code=422,
            details=diagnostic["details"],
        )
    rules = diagnostic["default_rules"]
    if len(rules) != 1:
        raise ScmServiceError(
            "PACKAGING_RULE_MISSING",
            (
                f"{diagnostic['article_label']} requiere exactamente una "
                "regla MANGA aprobada y validada fisicamente."
            ),
            status_code=422,
            details=diagnostic["details"],
        )
    return profiles[0], rules[0]


def _load_executable_order(session, op_number, *, lock=False):
    statement = select(OrdenProduccion).where(
        OrdenProduccion.numero_op == op_number
    )
    if lock:
        statement = statement.with_for_update()
    order = session.scalar(statement)
    if (
        order is None
        or not order.activa
        or order.molde_id is None
        or order.maquina_id is None
        or not order.snapshot_composicion
        or any(item.pieza_id is None for item in order.snapshot_composicion)
        or not order.lotes
        or not any(lot.salidas for lot in order.lotes)
    ):
        raise ScmServiceError(
            "OP_NOT_EXECUTABLE",
            "La OP no cumple el modelo normalizado requerido por el piloto.",
            status_code=422,
            details={"orden_id": op_number},
        )
    return order


def get_manga_plan(session, *, actor_id, op_number):
    load_actor(session, actor_id, capability="PLAN_MANGA_VER")
    plan = session.scalar(
        select(ScmPlanMangaOp).where(
            ScmPlanMangaOp.orden_id == op_number,
            ScmPlanMangaOp.estado == "ACTIVO",
        )
    )
    return {"plan": _serialize_plan(plan) if plan else None}


def recalculate_manga_plan(
    session, *, actor_id, op_number, operation_id, data
):
    actor = load_actor(
        session, actor_id, capability="PLAN_MANGA_ADMINISTRAR"
    )
    endpoint = f"/ordenes-produccion/{op_number}/plan-mangas/recalcular"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        order = _load_executable_order(session, op_number, lock=True)
        previous = session.scalar(
            select(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOp.orden_id == op_number,
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        revision = (previous.revision + 1) if previous else 1
        drafts = []
        for lot in order.lotes:
            for output in lot.salidas:
                article_link = session.scalar(
                    select(ScmArticuloPiezaColor).where(
                        ScmArticuloPiezaColor.pieza_color_sku
                        == output.pieza_color_sku
                    )
                )
                if article_link is None or not article_link.articulo.activo:
                    raise ScmServiceError(
                        "OP_NOT_EXECUTABLE",
                        "Una salida no posee articulo SCM activo.",
                        status_code=422,
                        details={"pieza_color_sku": output.pieza_color_sku},
                    )
                profile_link, revision_rule = _approved_manga_rule(
                    session, article_link.articulo_id
                )
                container = revision_rule.regla.tipo_contenedor
                result = calculate_packaging_capacity(
                    tara_nominal_g=Decimal(
                        revision_rule.tara_nominal_g_snapshot
                        if revision_rule.tara_nominal_g_snapshot is not None
                        else container.tara_nominal_g
                    ),
                    tolerancia_tara_g=Decimal(
                        revision_rule.tolerancia_tara_g_snapshot
                        if revision_rule.tolerancia_tara_g_snapshot is not None
                        else container.tolerancia_tara_g
                    ),
                    peso_bruto_max_kg=Decimal(
                        revision_rule.peso_bruto_max_kg_snapshot
                        if revision_rule.peso_bruto_max_kg_snapshot is not None
                        else container.peso_bruto_max_kg
                    ),
                    peso_neto_operativo_max_kg=Decimal(
                        revision_rule.peso_neto_operativo_max_kg
                    ),
                    margen_seguridad_kg=Decimal(
                        revision_rule.margen_seguridad_kg
                    ),
                    cantidad_objetivo_un=revision_rule.cantidad_objetivo_un,
                    cantidad_maxima_probada_un=(
                        revision_rule.cantidad_maxima_probada_un
                    ),
                    peso_unitario_snapshot_g=Decimal(
                        output.peso_unitario_snapshot_gr
                    ),
                )
                objective = _decimal(
                    output.cantidad_objetivo,
                    "cantidad_objetivo",
                    integral=True,
                )
                capacity = result["capacidad_efectiva_un"]
                lot_article = session.scalar(
                    select(ScmLoteArticulo).where(
                        ScmLoteArticulo.lote_salida_pieza_color_id
                        == output.id
                    )
                )
                if lot_article is None:
                    lot_article = ScmLoteArticulo(
                        codigo=f"LOT-{op_number}-{output.id}".upper()[:64],
                        articulo_id=article_link.articulo_id,
                        lote_salida_pieza_color_id=output.id,
                    )
                    session.add(lot_article)
                    session.flush()
                color_name = (
                    output.pieza_color.color_produccion_rel.nombre
                    if output.pieza_color
                    and output.pieza_color.color_produccion_rel
                    else None
                )
                drafts.append({
                    "output": output,
                    "lot_article": lot_article,
                    "profile": profile_link.perfil,
                    "rule": revision_rule,
                    "container": container,
                    "objective": objective,
                    "capacity": capacity,
                    "mangas": int(math.ceil(objective / capacity)),
                    "article": article_link.articulo,
                    "color": color_name,
                })
        content = [{
            "salida_id": item["output"].id,
            "articulo_id": item["article"].id,
            "cantidad_objetivo_un": str(item["objective"]),
            "capacidad_efectiva_un": item["capacity"],
            "regla_revision_id": item["rule"].id,
            "regla_hash": item["rule"].content_hash,
        } for item in drafts]
        plan = ScmPlanMangaOp(
            orden_id=op_number,
            revision=revision,
            calculado_por_id=actor.id,
            operation_id=operation.operation_id,
            content_hash=_json_hash(content),
        )
        if previous:
            previous.estado = "SUPERADO"
        session.add(plan)
        session.flush()
        for item in drafts:
            rule = item["rule"]
            container = item["container"]
            session.add(ScmPlanMangaOpLinea(
                plan_id=plan.id,
                lote_salida_pieza_color_id=item["output"].id,
                lote_articulo_id=item["lot_article"].id,
                perfil_empacable_id=item["profile"].id,
                regla_revision_id=rule.id,
                tipo_contenedor_id=container.id,
                cantidad_objetivo_un=item["objective"],
                capacidad_efectiva_un=item["capacity"],
                mangas_propuestas=item["mangas"],
                peso_unitario_snapshot_g=(
                    item["output"].peso_unitario_snapshot_gr
                ),
                articulo_codigo_snapshot=item["article"].codigo,
                articulo_nombre_snapshot=item["article"].nombre,
                pieza_color_sku_snapshot=(
                    item["output"].pieza_color_sku
                ),
                color_snapshot=item["color"],
                regla_hash_snapshot=rule.content_hash,
                tara_nominal_g_snapshot=(
                    rule.tara_nominal_g_snapshot
                    if rule.tara_nominal_g_snapshot is not None
                    else container.tara_nominal_g
                ),
                tolerancia_tara_g_snapshot=(
                    rule.tolerancia_tara_g_snapshot
                    if rule.tolerancia_tara_g_snapshot is not None
                    else container.tolerancia_tara_g
                ),
                peso_bruto_max_kg_snapshot=(
                    rule.peso_bruto_max_kg_snapshot
                    if rule.peso_bruto_max_kg_snapshot is not None
                    else container.peso_bruto_max_kg
                ),
            ))
        session.flush()
        response = {"plan": _serialize_plan(plan)}
        session.add(_event(
            "PLAN_MANGA_OP", plan.id, "OP_MANGA_PLAN_CALCULATED",
            actor, operation, response["plan"],
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _load_fabrication_order(session, order_id, *, lock=False):
    statement = select(ScmOrdenOperacion).where(
        ScmOrdenOperacion.id == order_id,
        ScmOrdenOperacion.tipo == "FABRICACION",
    )
    if lock:
        statement = statement.with_for_update()
    order = session.scalar(statement)
    if order is None or order.fabricacion is None:
        raise ScmServiceError(
            "OF_NOT_FOUND",
            "La orden de fabricacion no existe.",
            status_code=404,
        )
    return order


def get_fabrication_manga_plan(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="PLAN_MANGA_VER")
    plan = session.scalar(
        select(ScmPlanMangaOp).where(
            ScmPlanMangaOp.orden_operacion_id == order_id,
            ScmPlanMangaOp.estado == "ACTIVO",
        )
    )
    return {"plan": _serialize_plan(plan) if plan else None}


def recalculate_fabrication_manga_plan(
    session,
    *,
    actor_id,
    order_id,
    operation_id,
    data,
):
    actor = load_actor(
        session,
        actor_id,
        capability="PLAN_MANGA_ADMINISTRAR",
    )
    endpoint = (
        f"/ordenes-fabricacion/{order_id}/plan-mangas/recalcular"
    )
    operation, replay = _reserve_operation(
        session,
        operation_id,
        endpoint,
        actor,
        data,
    )
    if replay is not None:
        return replay
    try:
        order = _load_fabrication_order(session, order_id, lock=True)
        if order.estado not in (
            "LIBERADA",
            "PROGRAMADA",
            "EN_EJECUCION",
        ):
            raise ScmServiceError(
                "OF_NOT_RELEASABLE",
                "La OF debe estar liberada antes de planificar mangas.",
                status_code=409,
            )
        previous = session.scalar(
            select(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOp.orden_operacion_id == order.id,
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        revision = (previous.revision + 1) if previous else 1
        drafts = []
        for run in order.fabricacion.corridas:
            if run.estado not in ("LIBERADA", "EN_EJECUCION"):
                continue
            color = (
                session.get(ColorProduccion, run.color_produccion_id)
                if run.color_produccion_id is not None
                else None
            )
            for output in run.salidas:
                article = output.articulo
                if article is None or not article.activo:
                    raise ScmServiceError(
                        "OF_NOT_RELEASABLE",
                        "Una salida no posee articulo SCM activo.",
                        status_code=422,
                        details={"salida_id": str(output.id)},
                    )
                if output.peso_unitario_snapshot_g is None:
                    raise ScmServiceError(
                        "OF_NOT_RELEASABLE",
                        "Una salida no posee peso unitario congelado.",
                        status_code=422,
                        details={"salida_id": str(output.id)},
                    )
                profile_link, revision_rule = _approved_manga_rule(
                    session,
                    article.id,
                )
                container = revision_rule.regla.tipo_contenedor
                result = calculate_packaging_capacity(
                    tara_nominal_g=Decimal(
                        revision_rule.tara_nominal_g_snapshot
                        if revision_rule.tara_nominal_g_snapshot is not None
                        else container.tara_nominal_g
                    ),
                    tolerancia_tara_g=Decimal(
                        revision_rule.tolerancia_tara_g_snapshot
                        if revision_rule.tolerancia_tara_g_snapshot
                        is not None
                        else container.tolerancia_tara_g
                    ),
                    peso_bruto_max_kg=Decimal(
                        revision_rule.peso_bruto_max_kg_snapshot
                        if revision_rule.peso_bruto_max_kg_snapshot
                        is not None
                        else container.peso_bruto_max_kg
                    ),
                    peso_neto_operativo_max_kg=Decimal(
                        revision_rule.peso_neto_operativo_max_kg
                    ),
                    margen_seguridad_kg=Decimal(
                        revision_rule.margen_seguridad_kg
                    ),
                    cantidad_objetivo_un=(
                        revision_rule.cantidad_objetivo_un
                    ),
                    cantidad_maxima_probada_un=(
                        revision_rule.cantidad_maxima_probada_un
                    ),
                    peso_unitario_snapshot_g=Decimal(
                        output.peso_unitario_snapshot_g
                    ),
                )
                objective = _decimal(
                    output.cantidad_objetivo,
                    "cantidad_objetivo",
                    integral=True,
                )
                capacity = result["capacidad_efectiva_un"]
                lot_article = session.scalar(
                    select(ScmLoteArticulo).where(
                        ScmLoteArticulo.orden_operacion_salida_id
                        == output.id
                    )
                )
                if lot_article is None:
                    lot_article = ScmLoteArticulo(
                        codigo=(
                            f"LOT-{order.codigo}-{run.secuencia}-"
                            f"{str(output.id)[:8]}"
                        ).upper()[:64],
                        articulo_id=article.id,
                        clase="SALIDA_ORDEN_OPERACION",
                        orden_operacion_salida_id=output.id,
                    )
                    session.add(lot_article)
                    session.flush()
                piece_sku = (
                    article.pieza_color.pieza_color_sku
                    if article.pieza_color is not None
                    else None
                )
                drafts.append({
                    "run": run,
                    "output": output,
                    "lot_article": lot_article,
                    "profile": profile_link.perfil,
                    "rule": revision_rule,
                    "container": container,
                    "objective": objective,
                    "capacity": capacity,
                    "mangas": int(math.ceil(objective / capacity)),
                    "article": article,
                    "piece_sku": piece_sku,
                    "color": color.nombre if color else None,
                })
        if not drafts:
            raise ScmServiceError(
                "OF_OUTPUT_REQUIRED",
                "La OF no tiene salidas liberadas para planificar.",
                status_code=422,
            )
        content = [{
            "corrida_id": str(item["run"].id),
            "salida_id": str(item["output"].id),
            "articulo_id": item["article"].id,
            "cantidad_objetivo_un": str(item["objective"]),
            "capacidad_efectiva_un": item["capacity"],
            "regla_revision_id": item["rule"].id,
            "regla_hash": item["rule"].content_hash,
        } for item in drafts]
        plan = ScmPlanMangaOp(
            orden_id=None,
            orden_operacion_id=order.id,
            revision=revision,
            calculado_por_id=actor.id,
            operation_id=operation.operation_id,
            content_hash=_json_hash(content),
        )
        if previous is not None:
            previous.estado = "SUPERADO"
        session.add(plan)
        session.flush()
        for item in drafts:
            rule = item["rule"]
            container = item["container"]
            session.add(ScmPlanMangaOpLinea(
                plan_id=plan.id,
                lote_salida_pieza_color_id=None,
                orden_operacion_salida_id=item["output"].id,
                lote_articulo_id=item["lot_article"].id,
                perfil_empacable_id=item["profile"].id,
                regla_revision_id=rule.id,
                tipo_contenedor_id=container.id,
                cantidad_objetivo_un=item["objective"],
                capacidad_efectiva_un=item["capacity"],
                mangas_propuestas=item["mangas"],
                peso_unitario_snapshot_g=(
                    item["output"].peso_unitario_snapshot_g
                ),
                articulo_codigo_snapshot=item["article"].codigo,
                articulo_nombre_snapshot=item["article"].nombre,
                pieza_color_sku_snapshot=item["piece_sku"],
                color_snapshot=item["color"],
                regla_hash_snapshot=rule.content_hash,
                tara_nominal_g_snapshot=(
                    rule.tara_nominal_g_snapshot
                    if rule.tara_nominal_g_snapshot is not None
                    else container.tara_nominal_g
                ),
                tolerancia_tara_g_snapshot=(
                    rule.tolerancia_tara_g_snapshot
                    if rule.tolerancia_tara_g_snapshot is not None
                    else container.tolerancia_tara_g
                ),
                peso_bruto_max_kg_snapshot=(
                    rule.peso_bruto_max_kg_snapshot
                    if rule.peso_bruto_max_kg_snapshot is not None
                    else container.peso_bruto_max_kg
                ),
            ))
        session.flush()
        response = {"plan": _serialize_plan(plan)}
        session.add(_event(
            "PLAN_MANGA_OF",
            plan.id,
            "OF_MANGA_PLAN_CALCULATED",
            actor,
            operation,
            response["plan"],
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _create_mangas(
    session,
    *,
    ot,
    line,
    assignment,
    quantity,
    actor,
    kind,
    work=None,
    personal_assignment=None,
    reason=None,
    requester_id=None,
    approver_id=None,
):
    capacity = Decimal(line.capacidad_efectiva_un)
    remaining = Decimal(quantity)
    created = []
    personal_assignment = (
        personal_assignment
        or _current_or_planned_work_assignment(work)
    )
    worker_id = (
        personal_assignment.trabajador_id
        if personal_assignment is not None
        else (ot.maquinista_previsto_id or ot.responsable_id)
    )
    if worker_id is None:
        raise ScmServiceError(
            "INVALID_WORKER",
            "La manga requiere un responsable previsto.",
            status_code=422,
        )
    while remaining > 0:
        amount = min(remaining, capacity)
        sequence = ot.secuencia_siguiente_manga
        ot.secuencia_siguiente_manga += 1
        order_code = (
            work.orden_operacion.codigo
            if work is not None
            else (
                ot.orden_operacion.codigo
                if ot.orden_operacion is not None
                else ot.orden_id
            )
        )
        manga = ScmManga(
            codigo=_manga_code(order_code, ot.codigo_ot, sequence),
            ot_id=ot.id,
            trabajo_ot_id=work.id if work is not None else None,
            asignacion_personal_trabajo_id=(
                personal_assignment.id
                if personal_assignment is not None else None
            ),
            plan_linea_id=line.id,
            asignacion_id=assignment.id if assignment else None,
            lote_articulo_id=line.lote_articulo_id,
            secuencia_ot=sequence,
            tipo=kind,
            cantidad_planificada_un=amount,
            cantidad_asignada_un=amount,
            maquinista_previsto_id=worker_id,
            articulo_codigo_snapshot=line.articulo_codigo_snapshot,
            articulo_nombre_snapshot=line.articulo_nombre_snapshot,
            pieza_color_sku_snapshot=line.pieza_color_sku_snapshot,
            color_snapshot=line.color_snapshot,
            regla_revision_id_snapshot=line.regla_revision_id,
            regla_hash_snapshot=line.regla_hash_snapshot,
            tipo_contenedor_codigo_snapshot=line.tipo_contenedor.codigo,
            tipo_contenedor_nombre_snapshot=line.tipo_contenedor.nombre,
            peso_unitario_snapshot_g=line.peso_unitario_snapshot_g,
            tara_nominal_g_snapshot=line.tara_nominal_g_snapshot,
            tolerancia_tara_g_snapshot=line.tolerancia_tara_g_snapshot,
            peso_bruto_max_kg_snapshot=line.peso_bruto_max_kg_snapshot,
            motivo_extra=reason,
            extra_solicitada_por_id=requester_id,
            extra_aprobada_por_id=approver_id,
            extra_aprobada_at=utc_now() if approver_id else None,
            created_by_id=actor.id,
        )
        session.add(manga)
        created.append(manga)
        remaining -= amount
    session.flush()
    return created


def _plan_line_belongs_to_ot(line, ot):
    if ot.orden_operacion_id is not None:
        return (
            line.plan.orden_operacion_id == ot.orden_operacion_id
            and line.salida_canonica is not None
            and line.salida_canonica.corrida_fabricacion_id
            == ot.corrida_fabricacion_id
        )
    return (
        ot.orden_id is not None
        and line.plan.orden_id == ot.orden_id
    )


def _uuid_value(value, *, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "INVALID_UUID",
            f"{field} debe ser un UUID valido.",
            status_code=400,
            details={"field": field},
        ) from error


def _load_color_work(session, work_id, *, lock=False):
    statement = select(ScmTrabajoOt).where(
        ScmTrabajoOt.id == work_id,
        ScmTrabajoOt.tipo == "COLOR",
    )
    if lock:
        statement = statement.with_for_update()
    work = session.scalar(statement)
    if work is None or work.trabajo_color is None:
        raise ScmServiceError(
            "TRABAJO_COLOR_NOT_FOUND",
            "El trabajo de color no existe.",
            status_code=404,
        )
    return work


def _plan_line_belongs_to_work(line, work):
    color = work.trabajo_color
    return (
        color is not None
        and line.plan.orden_operacion_id == work.orden_operacion_id
        and line.salida_canonica is not None
        and line.salida_canonica.corrida_fabricacion_id
        == color.corrida_fabricacion_id
    )


def _validate_work_parent(work):
    if work.orden_trabajo_id != work.orden_trabajo.id:
        raise ScmServiceError(
            "TRABAJO_NO_PERTENECE_A_OT",
            "El trabajo no pertenece a la OT indicada.",
            status_code=409,
        )


def _validate_color_worker(worker):
    if worker is None or not worker.activo:
        raise ScmServiceError(
            "INVALID_WORKER", "El maquinista no esta activo.", status_code=422
        )
    if not worker.tiene_capacidad("MANGA_PESAR"):
        raise ScmServiceError(
            "WORKER_NOT_ELIGIBLE",
            "El trabajador no esta habilitado para operar y pesar mangas.",
            status_code=422,
        )
    return worker


def _resolve_color_work_for_ot(ot, raw_work_id=None, *, required=False):
    works = [item for item in ot.trabajos_ot if item.tipo == "COLOR"]
    if raw_work_id is not None:
        work_id = _uuid_value(raw_work_id, field="trabajo_color_id")
        work = next((item for item in works if item.id == work_id), None)
        if work is None:
            raise ScmServiceError(
                "TRABAJO_NO_PERTENECE_A_OT",
                "El trabajo no pertenece a la OT.",
                status_code=409,
            )
        return work
    if len(works) == 1:
        return works[0]
    if len(works) > 1 or required:
        raise ScmServiceError(
            "TRABAJO_COLOR_REQUIRED",
            "La OT contiene varios colores; indique el trabajo.",
            status_code=422,
        )
    return None


def create_fabrication_ot_header(
    session,
    *,
    actor_id,
    operation_id,
    data,
):
    """Create the machine/date/shift header without binding one color."""
    actor = load_actor(session, actor_id, capability="OT_CREAR")
    endpoint = "/ots/fabricacion"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    reject_unknown_fields(
        data,
        allowed={
            "maquina_id",
            "fecha_operativa",
            "turno",
            "maquinista_predeterminado_id",
        },
    )
    try:
        machine = session.scalar(
            select(Maquina)
            .where(Maquina.id == data.get("maquina_id"))
            .with_for_update()
        )
        if machine is None or not machine.activo:
            raise ScmServiceError(
                "INVALID_MACHINE",
                "La maquina no esta activa.",
                status_code=422,
            )
        operational_date = _parse_date(data.get("fecha_operativa"))
        shift = required_text(
            data.get("turno"), field="turno", max_length=20
        ).upper()
        worker_id = data.get("maquinista_predeterminado_id")
        worker = session.get(Trabajador, worker_id) if worker_id else None
        if worker_id is not None:
            _validate_color_worker(worker)
        existing = session.scalar(
            select(RegistroDiarioProduccion)
            .where(
                RegistroDiarioProduccion.tipo_ot == "FABRICACION",
                RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
                RegistroDiarioProduccion.maquina_id == machine.id,
                RegistroDiarioProduccion.fecha == operational_date,
                RegistroDiarioProduccion.turno == shift,
                RegistroDiarioProduccion.estado != "ANULADA",
                RegistroDiarioProduccion.orden_id.is_(None),
                RegistroDiarioProduccion.orden_operacion_id.is_(None),
                RegistroDiarioProduccion.corrida_fabricacion_id.is_(None),
            )
            .with_for_update()
        )
        if existing is not None:
            raise ScmServiceError(
                "OT_MACHINE_SHIFT_ALREADY_EXISTS",
                "Ya existe una OT para la maquina, fecha y turno.",
                status_code=409,
                details={"ot_id": str(existing.public_id)},
            )
        ot = RegistroDiarioProduccion(
            public_id=uuid.uuid4(),
            codigo_ot=generar_codigo_catalogo(
                "ORDEN_TRABAJO", session=session
            ),
            codigo_ot_sintetico=False,
            estado="PLANIFICADA",
            tipo_ot="FABRICACION",
            orden_id=None,
            orden_operacion_id=None,
            corrida_fabricacion_id=None,
            maquina_id=machine.id,
            fecha=operational_date,
            turno=shift,
            created_by_id=actor.id,
            maquinista_previsto_id=worker.id if worker else None,
            maquina_codigo_snapshot=machine.codigo,
            maquina_nombre_snapshot=machine.nombre,
        )
        session.add(ot)
        session.flush()
        response = {"ot": _serialize_ot(ot)}
        session.add(_event(
            "ORDEN_TRABAJO",
            ot.id,
            "FABRICATION_OT_HEADER_CREATED",
            actor,
            operation,
            response["ot"],
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "OT_MACHINE_SHIFT_ALREADY_EXISTS",
            "Ya existe una OT para la maquina, fecha y turno.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise


def _create_color_work_model(
    session,
    *,
    ot,
    order,
    run,
    worker,
    actor,
    continues_from=None,
):
    _validate_color_worker(worker)
    sequence = ot.secuencia_siguiente_trabajo
    ot.secuencia_siguiente_trabajo += 1
    color = (
        session.get(ColorProduccion, run.color_produccion_id)
        if run.color_produccion_id else None
    )
    cavities = sum(
        (Decimal(item.cantidad_por_ciclo_snapshot or 0) for item in run.salidas),
        Decimal("0"),
    )
    net_weight = sum(
        (
            Decimal(item.cantidad_por_ciclo_snapshot or 0)
            * Decimal(item.peso_unitario_snapshot_g or 0)
            for item in run.salidas
        ),
        Decimal("0"),
    )
    work = ScmTrabajoOt(
        orden_trabajo_id=ot.id,
        codigo=f"{ot.codigo_ot}-TC{sequence:02d}",
        tipo="COLOR",
        secuencia=sequence,
        estado="PLANIFICADO",
        orden_operacion_id=order.id,
        continua_de_id=(continues_from.id if continues_from is not None else None),
        cantidad_objetivo_un=0,
        cantidad_confirmada_un=0,
        created_by_id=actor.id,
    )
    work.trabajo_color = ScmTrabajoColor(
        corrida_fabricacion_id=run.id,
        molde_codigo_snapshot=order.fabricacion.molde_id,
        color_id_snapshot=run.color_produccion_id,
        color_nombre_snapshot=color.nombre if color else None,
        receta_revision_id_snapshot=run.receta_revision_id,
        receta_hash_snapshot=run.receta_hash,
        cavidades_snapshot=int(cavities),
        peso_neto_snapshot_g=net_weight,
        peso_colada_snapshot_g=(
            order.fabricacion.snapshot_peso_colada_gr or 0
        ),
    )
    session.add(work)
    session.flush()
    personal = ScmAsignacionPersonalTrabajoOt(
        trabajo_ot_id=work.id,
        trabajador_id=worker.id,
        estado="PREVISTA",
        asignada_por_id=actor.id,
    )
    session.add(personal)
    session.flush()
    return work, personal


def _allocate_work_lines(
    session,
    *,
    work,
    plan,
    allocations,
    actor,
    personal_assignment,
):
    if not isinstance(allocations, list):
        raise ScmServiceError(
            "REQUIRED_FIELD",
            "asignaciones debe ser una lista.",
            status_code=400,
        )
    if not allocations:
        return []
    seen = set()
    created = []
    total = Decimal("0")
    for index, raw in enumerate(allocations):
        if not isinstance(raw, dict):
            raise ScmServiceError(
                "JSON_OBJECT_REQUIRED",
                "Cada asignacion debe ser un objeto.",
                status_code=400,
            )
        reject_unknown_fields(raw, allowed={"plan_linea_id", "cantidad_un"})
        line_id = raw.get("plan_linea_id")
        if line_id in seen:
            raise ScmServiceError(
                "DUPLICATE_PLAN_LINE",
                "Una linea no puede repetirse en el trabajo.",
                status_code=422,
            )
        seen.add(line_id)
        line = session.scalar(
            select(ScmPlanMangaOpLinea)
            .where(
                ScmPlanMangaOpLinea.id == line_id,
                ScmPlanMangaOpLinea.plan_id == plan.id,
            )
            .with_for_update()
        )
        if line is None or not _plan_line_belongs_to_work(line, work):
            raise ScmServiceError(
                "PLAN_LINE_NOT_FOUND",
                "La linea no pertenece a la corrida del trabajo.",
                status_code=404,
                details={"index": index},
            )
        quantity = _decimal(raw.get("cantidad_un"), "cantidad_un", integral=True)
        already = session.scalar(
            select(func.coalesce(
                func.sum(ScmAsignacionPlanMangaOt.cantidad_asignada_un), 0
            )).where(ScmAsignacionPlanMangaOt.plan_linea_id == line.id)
        )
        if Decimal(already) + quantity > Decimal(line.cantidad_objetivo_un):
            raise ScmServiceError(
                "PLAN_BALANCE_EXCEEDED",
                "La asignacion excede el saldo del plan.",
                status_code=409,
                details={"plan_linea_id": line.id},
            )
        count = int(math.ceil(quantity / Decimal(line.capacidad_efectiva_un)))
        assignment = session.scalar(
            select(ScmAsignacionPlanMangaOt)
            .where(
                ScmAsignacionPlanMangaOt.plan_linea_id == line.id,
                ScmAsignacionPlanMangaOt.trabajo_ot_id == work.id,
            )
            .with_for_update()
        )
        if assignment is None:
            assignment = ScmAsignacionPlanMangaOt(
                plan_linea_id=line.id,
                ot_id=work.orden_trabajo_id,
                trabajo_ot_id=work.id,
                cantidad_asignada_un=quantity,
                mangas_asignadas=count,
                asignada_por_id=actor.id,
            )
            session.add(assignment)
        else:
            assignment.cantidad_asignada_un = (
                Decimal(assignment.cantidad_asignada_un) + quantity
            )
            assignment.mangas_asignadas += count
        session.flush()
        created.extend(_create_mangas(
            session,
            ot=work.orden_trabajo,
            work=work,
            line=line,
            assignment=assignment,
            personal_assignment=personal_assignment,
            quantity=quantity,
            actor=actor,
            kind="NORMAL",
        ))
        total += quantity
    work.cantidad_objetivo_un = Decimal(work.cantidad_objetivo_un or 0) + total
    return created


_CONTINUITY_SHIFT_ORDER = {
    "DIA": 10,
    "DIURNO": 10,
    "NOCHE": 20,
    "NOCTURNO": 20,
    "EXTRA": 30,
}

_CONTINUITY_DAY_START_HOUR = 6
_CONTINUITY_NIGHT_START_HOUR = 18


def _continuity_ot_slot(ot):
    rank = _CONTINUITY_SHIFT_ORDER.get(
        str(ot.turno or "").strip().upper()
    )
    if rank is None or ot.fecha is None:
        return None
    return ot.fecha, rank


def _continuity_control_slot(control):
    """Map the physical cut to the operational shift containing it.

    The pilot uses 06:00/18:00 boundaries. A cut before 06:00 belongs to
    the previous operational night's slot.
    """
    weighed_at = control.pesado_at
    timezone_name = control.timezone_snapshot or "America/Lima"
    if weighed_at.tzinfo is not None:
        local_cut = weighed_at.astimezone(ZoneInfo(timezone_name))
        local_date = local_cut.date()
        local_hour = local_cut.hour
    else:
        # SQLite drops timezone offsets. fecha_local_pesaje is the canonical
        # local date captured at ingestion, while the stored wall time remains
        # suitable for directed tests and local UAT.
        local_date = control.fecha_local_pesaje
        local_hour = weighed_at.hour
    if local_hour < _CONTINUITY_DAY_START_HOUR:
        return local_date - timedelta(days=1), _CONTINUITY_SHIFT_ORDER["NOCHE"]
    if local_hour < _CONTINUITY_NIGHT_START_HOUR:
        return local_date, _CONTINUITY_SHIFT_ORDER["DIA"]
    return local_date, _CONTINUITY_SHIFT_ORDER["NOCHE"]


def _continuity_target_is_later(source_ot, target_ot):
    source_slot = _continuity_ot_slot(source_ot)
    target_slot = _continuity_ot_slot(target_ot)
    return (
        source_slot is not None
        and target_slot is not None
        and target_slot > source_slot
    )


def _continuity_target_is_after_cut(*, segment, target_ot):
    target_slot = _continuity_ot_slot(target_ot)
    control = segment.control_peso
    return (
        target_slot is not None
        and control is not None
        and target_slot >= _continuity_control_slot(control)
    )


def _work_matches_manga_continuity(work, *, manga, segment):
    source_work = segment.trabajo
    source_color = source_work.trabajo_color
    return (
        work.estado in {"PLANIFICADO", "EN_EJECUCION", "PAUSADO"}
        and work.orden_operacion_id == source_work.orden_operacion_id
        and work.trabajo_color is not None
        and source_color is not None
        and work.trabajo_color.corrida_fabricacion_id
        == source_color.corrida_fabricacion_id
        and work.trabajo_color.color_id_snapshot
        == source_color.color_id_snapshot
        and work.trabajo_color.receta_revision_id_snapshot
        == source_color.receta_revision_id_snapshot
        and work.trabajo_color.receta_hash_snapshot
        == source_color.receta_hash_snapshot
        and _plan_line_belongs_to_work(manga.plan_linea, work)
    )


def _ot_can_host_continuity(ot, *, manga, segment):
    works = list(getattr(ot, "trabajos_ot", ()) or ())
    return not works or any(
        _work_matches_manga_continuity(
            work, manga=manga, segment=segment
        )
        for work in works
    )


def _next_linkable_continuity_ot(
    session, *, manga, segment, target_ot, target_work=None
):
    """Return the earliest compatible OT that can still accept the manga.

    Closed or annulled historical OTs do not block continuity because they can
    no longer be linked. The destination work is evaluated explicitly: its OT
    relationship collection may already be loaded without that just-created
    work in a multi-color request.
    """
    source_ot = segment.trabajo.orden_trabajo
    source_slot = _continuity_ot_slot(source_ot)
    target_slot = _continuity_ot_slot(target_ot)
    if source_slot is None or target_slot is None:
        return None
    cut_slot = _continuity_control_slot(segment.control_peso)
    lower_date = min(source_slot[0], cut_slot[0])
    candidates = session.scalars(
        select(RegistroDiarioProduccion)
        .options(
            selectinload(RegistroDiarioProduccion.trabajos_ot)
            .selectinload(ScmTrabajoOt.trabajo_color)
        )
        .where(
            RegistroDiarioProduccion.tipo_ot == "FABRICACION",
            RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
            RegistroDiarioProduccion.maquina_id == target_ot.maquina_id,
            RegistroDiarioProduccion.estado.in_(
                {"PLANIFICADA", "EN_EJECUCION"}
            ),
            RegistroDiarioProduccion.fecha >= lower_date,
            RegistroDiarioProduccion.fecha <= target_ot.fecha,
            RegistroDiarioProduccion.id != source_ot.id,
        )
    ).unique().all()
    eligible = [
        candidate
        for candidate in candidates
        if (
            (slot := _continuity_ot_slot(candidate)) is not None
            and candidate.estado in {"PLANIFICADA", "EN_EJECUCION"}
            and slot > source_slot
            and slot >= cut_slot
            and slot <= target_slot
            and (
                (
                    candidate.id == target_ot.id
                    and target_work is not None
                    and _work_matches_manga_continuity(
                        target_work, manga=manga, segment=segment
                    )
                )
                or _ot_can_host_continuity(
                    candidate, manga=manga, segment=segment
                )
            )
        )
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: (
            *_continuity_ot_slot(candidate),
            candidate.id,
        ),
    )


def _validate_continuity_target(
    *, manga, segment, target_work, session=None
):
    source_work = segment.trabajo
    source_ot = source_work.orden_trabajo
    target_ot = target_work.orden_trabajo
    if source_work.id == target_work.id or source_ot.id == target_ot.id:
        raise ScmServiceError(
            "CONTINUITY_TARGET_SAME_OT",
            "La continuidad requiere una OT de turno distinta.",
            status_code=409,
        )
    if not _continuity_target_is_later(source_ot, target_ot):
        raise ScmServiceError(
            "CONTINUITY_TARGET_PRECEDES_SOURCE",
            "La OT destino debe pertenecer a una fecha/turno posterior al corte.",
            status_code=409,
        )
    if not _continuity_target_is_after_cut(
        segment=segment, target_ot=target_ot
    ):
        raise ScmServiceError(
            "CONTINUITY_TARGET_PRECEDES_CUT",
            "La OT destino ocurre antes del corte fisico registrado.",
            status_code=409,
            details={
                "corte_pesado_at": segment.control_peso.pesado_at.isoformat(),
                "fecha_local_corte": (
                    segment.control_peso.fecha_local_pesaje.isoformat()
                ),
            },
        )
    if source_ot.maquina_id != target_ot.maquina_id:
        raise ScmServiceError(
            "CONTINUITY_MACHINE_MISMATCH",
            "La manga debe continuar en la misma maquina durante el piloto.",
            status_code=409,
        )
    source_color = source_work.trabajo_color
    target_color = target_work.trabajo_color
    if (
        source_color is None
        or target_color is None
        or source_work.orden_operacion_id != target_work.orden_operacion_id
        or source_color.corrida_fabricacion_id
        != target_color.corrida_fabricacion_id
        or source_color.color_id_snapshot != target_color.color_id_snapshot
        or source_color.receta_revision_id_snapshot
        != target_color.receta_revision_id_snapshot
        or source_color.receta_hash_snapshot != target_color.receta_hash_snapshot
        or not _plan_line_belongs_to_work(manga.plan_linea, target_work)
    ):
        raise ScmServiceError(
            "CONTINUITY_CONTEXT_MISMATCH",
            "La OT destino no conserva la misma OF, corrida, color, receta y salida.",
            status_code=409,
        )
    if session is not None:
        next_ot = _next_linkable_continuity_ot(
            session,
            manga=manga,
            segment=segment,
            target_ot=target_ot,
            target_work=target_work,
        )
        if next_ot is None or next_ot.id != target_ot.id:
            raise ScmServiceError(
                "CONTINUITY_NEXT_OT_REQUIRED",
                "La manga debe vincularse a la primera OT compatible posterior al corte.",
                status_code=409,
                details={
                    "ot_siguiente_id": (
                        str(next_ot.public_id) if next_ot is not None else None
                    ),
                    "ot_siguiente_codigo": (
                        next_ot.codigo_ot if next_ot is not None else None
                    ),
                },
            )


def _continuity_candidate_payload(manga):
    segment = _latest_manga_segment(manga)
    source_work = segment.trabajo if segment else manga.trabajo
    source_assignment = (
        segment.asignacion_personal_trabajo
        if segment else manga.asignacion_personal_trabajo
    )
    control = segment.control_peso if segment else None
    assigned = Decimal(manga.cantidad_asignada_un)
    boundary = Decimal(segment.cantidad_fin_un) if segment else Decimal("0")
    return {
        "manga": _serialize_manga(manga),
        "origen": {
            "ot_id": str(source_work.orden_trabajo.public_id),
            "ot_codigo": source_work.orden_trabajo.codigo_ot,
            "fecha_operativa": source_work.orden_trabajo.fecha.isoformat(),
            "turno": source_work.orden_trabajo.turno,
            "trabajo_color_id": str(source_work.id),
            "trabajo_color_codigo": source_work.codigo,
            "maquinista": (
                source_assignment.trabajador.nombre_completo
                if source_assignment and source_assignment.trabajador else None
            ),
        },
        "control_frontera": control.to_dict() if control else None,
        "conteo_acumulado_un": _compact_number(boundary),
        "cantidad_pendiente_un": _compact_number(assigned - boundary),
        "qr_preservado": True,
    }


def list_pending_manga_continuities(
    session, *, actor_id, ot_id, corrida_fabricacion_id
):
    load_actor(session, actor_id, capability="OT_VER")
    target_ot = session.scalar(
        select(RegistroDiarioProduccion).where(
            RegistroDiarioProduccion.public_id == ot_id,
            RegistroDiarioProduccion.tipo_ot == "FABRICACION",
        )
    )
    if target_ot is None:
        raise ScmServiceError(
            "OT_NOT_FOUND", "La OT de fabricacion no existe.", status_code=404
        )
    if target_ot.estado not in {"PLANIFICADA", "EN_EJECUCION"}:
        return {"items": []}
    run_id = _uuid_value(
        corrida_fabricacion_id, field="corrida_fabricacion_id"
    )
    candidates = session.scalars(
        select(ScmManga)
        .join(ScmTrabajoOt, ScmTrabajoOt.id == ScmManga.trabajo_ot_id)
        .join(ScmTrabajoColor, ScmTrabajoColor.trabajo_ot_id == ScmTrabajoOt.id)
        .join(
            RegistroDiarioProduccion,
            RegistroDiarioProduccion.id == ScmTrabajoOt.orden_trabajo_id,
        )
        .where(
            ScmManga.estado == "CONTINUIDAD_PENDIENTE",
            ScmTrabajoColor.corrida_fabricacion_id == run_id,
            RegistroDiarioProduccion.maquina_id == target_ot.maquina_id,
            RegistroDiarioProduccion.id != target_ot.id,
            RegistroDiarioProduccion.fecha <= target_ot.fecha,
        )
        .order_by(ScmManga.created_at, ScmManga.id)
    ).unique().all()
    items = []
    for manga in candidates:
        segment = _latest_manga_segment(manga)
        if (
            segment is None
            or segment.estado != "CERRADO"
            or segment.control_peso is None
            or Decimal(segment.cantidad_fin_un or 0)
            >= Decimal(manga.cantidad_asignada_un)
            or not _continuity_target_is_later(
                segment.trabajo.orden_trabajo, target_ot
            )
            or not _continuity_target_is_after_cut(
                segment=segment, target_ot=target_ot
            )
        ):
            continue
        next_ot = _next_linkable_continuity_ot(
            session, manga=manga, segment=segment, target_ot=target_ot
        )
        if next_ot is None or next_ot.id != target_ot.id:
            continue
        items.append(_continuity_candidate_payload(manga))
    return {"items": items}


def _attach_continuity_mangas(
    session, *, work, personal_assignment, manga_ids, actor, operation
):
    if not isinstance(manga_ids, list) or not manga_ids:
        return []
    load_actor(session, actor.id, capability="MANGA_TRANSFERIR_OT")
    canonical_ids = [
        _uuid_value(value, field="continuidad_manga_ids")
        for value in manga_ids
    ]
    if len(set(canonical_ids)) != len(canonical_ids):
        raise ScmServiceError(
            "DUPLICATE_CONTINUITY_MANGA",
            "Una manga abierta no puede repetirse en la continuidad.",
            status_code=422,
        )
    attached = []
    for public_id in canonical_ids:
        manga = session.scalar(
            select(ScmManga)
            .where(ScmManga.public_id == public_id)
            .with_for_update()
        )
        if manga is None:
            raise ScmServiceError(
                "MANGA_NOT_FOUND", "La manga abierta no existe.", status_code=404
            )
        segments = session.scalars(
            select(ScmTramoMangaTrabajo)
            .where(ScmTramoMangaTrabajo.manga_id == manga.id)
            .order_by(ScmTramoMangaTrabajo.secuencia)
            .with_for_update()
        ).all()
        segment = segments[-1] if segments else None
        if (
            manga.estado != "CONTINUIDAD_PENDIENTE"
            or segment is None
            or segment.estado != "CERRADO"
            or segment.control_peso is None
        ):
            raise ScmServiceError(
                "MANGA_CONTINUITY_NOT_PENDING",
                "La manga no posee un corte vigente pendiente de continuidad.",
                status_code=409,
            )
        _validate_continuity_target(
            manga=manga,
            segment=segment,
            target_work=work,
            session=session,
        )
        boundary = Decimal(segment.cantidad_fin_un)
        remaining = Decimal(manga.cantidad_asignada_un) - boundary
        if remaining <= 0:
            raise ScmServiceError(
                "MANGA_ALREADY_COMPLETE",
                "El conteo de frontera ya completo la manga.",
                status_code=409,
            )
        source_plan_id = segment.asignacion_plan_id or manga.asignacion_id
        source_plan = session.get(ScmAsignacionPlanMangaOt, source_plan_id)
        if (
            source_plan is None
            or Decimal(source_plan.cantidad_asignada_un) < remaining
            or Decimal(segment.trabajo.cantidad_objetivo_un) < remaining
        ):
            raise ScmServiceError(
                "PLAN_ASSIGNMENT_INCONSISTENT",
                "El cupo del trabajo origen no permite transferir el remanente.",
                status_code=409,
            )
        target_plan = session.scalar(
            select(ScmAsignacionPlanMangaOt)
            .where(
                ScmAsignacionPlanMangaOt.plan_linea_id == manga.plan_linea_id,
                ScmAsignacionPlanMangaOt.trabajo_ot_id == work.id,
            )
            .with_for_update()
        )
        if target_plan is None:
            target_plan = ScmAsignacionPlanMangaOt(
                plan_linea_id=manga.plan_linea_id,
                ot_id=work.orden_trabajo_id,
                trabajo_ot_id=work.id,
                cantidad_asignada_un=Decimal("0"),
                mangas_asignadas=0,
                asignada_por_id=actor.id,
            )
            session.add(target_plan)
            session.flush()
        source_plan.cantidad_asignada_un = (
            Decimal(source_plan.cantidad_asignada_un) - remaining
        )
        target_plan.cantidad_asignada_un = (
            Decimal(target_plan.cantidad_asignada_un) + remaining
        )
        segment.trabajo.cantidad_objetivo_un = (
            Decimal(segment.trabajo.cantidad_objetivo_un) - remaining
        )
        segment.trabajo.version += 1
        work.cantidad_objetivo_un = (
            Decimal(work.cantidad_objetivo_un or 0) + remaining
        )
        next_segment = ScmTramoMangaTrabajo(
            manga=manga,
            trabajo=work,
            asignacion_personal_trabajo=personal_assignment,
            asignacion_plan_id=target_plan.id,
            secuencia=segment.secuencia + 1,
            estado="PROGRAMADO",
            cantidad_inicio_un=boundary,
            cantidad_atribuida_un=0,
            created_by_id=actor.id,
            operation_id=operation.operation_id,
        )
        session.add(next_segment)
        manga.estado = "EN_LLENADO"
        manga.version += 1
        attached.append(manga)
    session.flush()
    return attached


def add_color_work(
    session,
    *,
    actor_id,
    ot_id,
    operation_id,
    data,
):
    actor = load_actor(session, actor_id, capability="OT_CREAR")
    endpoint = f"/ots/{ot_id}/trabajos-color"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    reject_unknown_fields(
        data,
        allowed={
            "corrida_fabricacion_id",
            "maquinista_id",
            "asignaciones",
            "continua_de_id",
            "continuidad_manga_ids",
        },
    )
    try:
        ot = session.scalar(
            select(RegistroDiarioProduccion)
            .where(RegistroDiarioProduccion.public_id == ot_id)
            .with_for_update()
        )
        if (
            ot is None
            or ot.codigo_ot_sintetico
            or ot.tipo_ot != "FABRICACION"
        ):
            raise ScmServiceError(
                "OT_NOT_FOUND", "La OT de fabricacion no existe.", status_code=404
            )
        if ot.estado not in ("PLANIFICADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "La OT no admite nuevos trabajos de color.",
                status_code=409,
            )
        run_id = _uuid_value(
            data.get("corrida_fabricacion_id"),
            field="corrida_fabricacion_id",
        )
        run = session.scalar(
            select(ScmCorridaFabricacion)
            .where(ScmCorridaFabricacion.id == run_id)
            # ColorProduccion usa eager LEFT JOIN. PostgreSQL no permite
            # FOR UPDATE sobre el lado nullable de ese join; el agregado que
            # se modifica y debe bloquearse es exclusivamente la corrida.
            .with_for_update(of=ScmCorridaFabricacion)
        )
        if run is None or run.estado not in ("LIBERADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "OF_CORRIDA_REQUIRED",
                "La corrida no existe o no esta liberada.",
                status_code=422,
            )
        continues_from = None
        if data.get("continua_de_id") is not None:
            continues_from = _load_color_work(
                session,
                _uuid_value(data.get("continua_de_id"), field="continua_de_id"),
                lock=True,
            )
            if continues_from.orden_trabajo_id != ot.id:
                raise ScmServiceError(
                    "TRABAJO_NO_PERTENECE_A_OT",
                    "La continuacion debe permanecer en la misma OT.",
                    status_code=409,
                )
            if continues_from.estado not in {"PAUSADO", "COMPLETADO", "ANULADO"}:
                raise ScmServiceError(
                    "CONTINUATION_SOURCE_NOT_CLOSED",
                    "El trabajo anterior debe estar pausado o terminal.",
                    status_code=409,
                )
            previous_color = continues_from.trabajo_color
            if previous_color.color_id_snapshot != run.color_produccion_id:
                raise ScmServiceError(
                    "CONTINUATION_COLOR_MISMATCH",
                    "Una continuacion debe conservar el mismo color productivo.",
                    status_code=409,
                )
        duplicate = session.scalar(
            select(ScmTrabajoOt)
            .join(ScmTrabajoColor)
            .where(
                ScmTrabajoOt.orden_trabajo_id == ot.id,
                ScmTrabajoOt.estado != "ANULADO",
                ScmTrabajoColor.corrida_fabricacion_id == run.id,
            )
            .with_for_update()
        )
        if duplicate is not None and continues_from is None:
            raise ScmServiceError(
                "COLOR_WORK_ALREADY_EXISTS",
                "La corrida ya posee un trabajo en esta OT; reanudelo.",
                status_code=409,
                details={"trabajo_color_id": str(duplicate.id)},
            )
        order = _load_fabrication_order(
            session, run.orden_fabricacion_id, lock=True
        )
        if order.fabricacion.maquina_prevista_id != ot.maquina_id:
            raise ScmServiceError(
                "COLOR_WORK_MACHINE_MISMATCH",
                "La maquina de la OT no coincide con la maquina prevista por la OF.",
                status_code=409,
            )
        if continues_from is not None:
            previous_color = continues_from.trabajo_color
            unchanged = (
                previous_color.corrida_fabricacion_id == run.id
                and previous_color.molde_codigo_snapshot
                == order.fabricacion.molde_id
                and previous_color.receta_revision_id_snapshot
                == run.receta_revision_id
                and previous_color.receta_hash_snapshot == run.receta_hash
            )
            if unchanged:
                raise ScmServiceError(
                    "COLOR_WORK_CONTEXT_UNCHANGED_USE_RESUME",
                    "El contexto no cambio; reanude el trabajo anterior.",
                    status_code=409,
                    details={"trabajo_color_id": str(continues_from.id)},
                )
        if order.estado not in ("LIBERADA", "PROGRAMADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "OF_NOT_RELEASABLE",
                "La OF debe estar liberada para programar el trabajo.",
                status_code=409,
            )
        plan = session.scalar(
            select(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOp.orden_operacion_id == order.id,
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        if plan is None:
            raise ScmServiceError(
                "PACKAGING_RULE_MISSING",
                "La OF aun no tiene un plan de mangas activo.",
                status_code=422,
            )
        worker_id = data.get("maquinista_id") or ot.maquinista_previsto_id
        worker = session.get(Trabajador, worker_id)
        _validate_color_worker(worker)
        work, personal = _create_color_work_model(
            session,
            ot=ot,
            order=order,
            run=run,
            worker=worker,
            actor=actor,
            continues_from=continues_from,
        )
        allocations = data.get("asignaciones")
        continuity_ids = data.get("continuidad_manga_ids")
        if allocations is None:
            allocations = []
        if continuity_ids is None:
            continuity_ids = []
        if not isinstance(continuity_ids, list):
            raise ScmServiceError(
                "JSON_ARRAY_REQUIRED",
                "continuidad_manga_ids debe ser una lista.",
                status_code=400,
            )
        if not allocations and not continuity_ids:
            raise ScmServiceError(
                "REQUIRED_FIELD",
                "Asigne saldo del plan o seleccione una manga abierta compatible.",
                status_code=400,
            )
        mangas = _allocate_work_lines(
            session,
            work=work,
            plan=plan,
            allocations=allocations,
            actor=actor,
            personal_assignment=personal,
        )
        inherited = _attach_continuity_mangas(
            session,
            work=work,
            personal_assignment=personal,
            manga_ids=continuity_ids,
            actor=actor,
            operation=operation,
        )
        if order.estado == "LIBERADA":
            order.estado = "PROGRAMADA"
            order.version += 1
        session.flush()
        session.expire(ot, ["trabajos_ot"])
        response = {
            "ot": _serialize_ot(ot),
            "trabajo_color": _serialize_color_work(work),
            "mangas": [
                _serialize_manga(item) for item in [*mangas, *inherited]
            ],
            "continuidades_vinculadas": [
                _serialize_manga(item) for item in inherited
            ],
        }
        session.add(_event(
            "TRABAJO_COLOR",
            work.id,
            "COLOR_WORK_ADDED",
            actor,
            operation,
            response["trabajo_color"],
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _terminal_manga_states():
    return {
        "PESADA",
        "ETIQUETADA_FINAL",
        "PENDIENTE_RECEPCION_ALMACEN",
        "RECIBIDA",
        "ANULADA",
    }


def _annul_unweighed_manga_model(manga, *, actor, reason):
    returned = Decimal("0")
    if manga.tipo == "NORMAL" and manga.asignacion is not None:
        returned = Decimal(manga.cantidad_asignada_un)
        manga.asignacion.cantidad_asignada_un = max(
            Decimal(manga.asignacion.cantidad_asignada_un) - returned,
            Decimal("0"),
        )
        manga.asignacion.mangas_asignadas = max(
            int(manga.asignacion.mangas_asignadas) - 1,
            0,
        )
        if manga.trabajo is not None:
            manga.trabajo.cantidad_objetivo_un = max(
                Decimal(manga.trabajo.cantidad_objetivo_un) - returned,
                Decimal("0"),
            )
            manga.trabajo.version += 1
    manga.estado = "ANULADA"
    manga.anulada_at = utc_now()
    manga.anulada_por_id = actor.id
    manga.motivo_anulacion = reason
    manga.version += 1
    for label in manga.etiquetas:
        if label.estado != "INVALIDADA":
            label.estado = "INVALIDADA"
            label.invalidada_por_id = actor.id
            label.invalidada_at = utc_now()
            label.motivo_invalidacion = f"Manga anulada: {reason}"
    return returned


def _activate_work_assignment(session, *, work, actor):
    current = _active_work_assignment(work)
    if current is not None:
        return current
    planned = next(
        (
            item for item in reversed(work.asignaciones_personal)
            if item.estado == "PREVISTA"
        ),
        None,
    )
    if planned is not None:
        planned.estado = "ACTIVA"
        planned.iniciada_at = utc_now()
        planned.version += 1
        return planned
    previous = next(
        (
            item for item in reversed(work.asignaciones_personal)
            if item.estado == "CERRADA"
        ),
        None,
    )
    worker_id = (
        previous.trabajador_id
        if previous is not None
        else work.orden_trabajo.maquinista_previsto_id
    )
    if worker_id is None:
        raise ScmServiceError(
            "INVALID_WORKER",
            "El trabajo requiere una asignacion personal antes de iniciar.",
            status_code=422,
        )
    current = ScmAsignacionPersonalTrabajoOt(
        trabajo_ot_id=work.id,
        trabajador_id=worker_id,
        estado="ACTIVA",
        asignada_por_id=actor.id,
        iniciada_at=utc_now(),
        motivo="Reanudacion del trabajo de color",
    )
    session.add(current)
    session.flush()
    return current


def _close_work_assignment(
    work, *, actor, cancelled=False, terminal=False, reason=None
):
    active = _active_work_assignment(work)
    if terminal:
        targets = [
            item for item in work.asignaciones_personal
            if item.estado in {"ACTIVA", "PREVISTA"}
        ]
    else:
        targets = [active] if active is not None else []
    for current in targets:
        current.estado = (
            "CANCELADA"
            if cancelled or current.estado == "PREVISTA"
            else "CERRADA"
        )
        current.finalizada_at = utc_now()
        current.finalizada_por_id = actor.id
        current.motivo = reason or current.motivo
        current.version += 1


def _recompute_parent_state(session, work):
    parent = work.orden_trabajo
    states = session.scalars(
        select(ScmTrabajoOt.estado).where(
            ScmTrabajoOt.orden_trabajo_id == parent.id
        )
    ).all()
    if any(item == "EN_EJECUCION" for item in states):
        parent.estado = "EN_EJECUCION"
        parent.cerrada_at = None
    else:
        # A terminal/paused queue remains open so the supervisor may add a
        # later color in the same shift. Closing the physical OT is explicit.
        parent.estado = "EN_EJECUCION" if parent.iniciada_at else "PLANIFICADA"
        parent.cerrada_at = None
    parent.version += 1


def transition_color_work(
    session,
    *,
    actor_id,
    work_id,
    operation_id,
    data,
    action,
):
    allowed_actions = {"iniciar", "pausar", "reanudar", "completar", "anular"}
    if action not in allowed_actions:
        raise ScmServiceError(
            "INVALID_ACTION", "La accion del trabajo no es valida.", status_code=404
        )
    capability = (
        "OT_INICIAR" if action in {"iniciar", "pausar", "reanudar"}
        else "OT_CERRAR"
    )
    actor = load_actor(session, actor_id, capability=capability)
    endpoint = f"/trabajos-color/{work_id}/{action}"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    reject_unknown_fields(data, allowed={"version", "motivo"})
    try:
        work = _load_color_work(session, work_id, lock=True)
        _validate_work_parent(work)
        version = expected_version(data.get("version"))
        if work.version != version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "El trabajo fue modificado por otra operacion.",
                status_code=409,
            )
        before = _serialize_color_work(work)
        now = utc_now()
        transition = {
            "iniciar": ({"PLANIFICADO"}, "EN_EJECUCION"),
            "pausar": ({"EN_EJECUCION"}, "PAUSADO"),
            "reanudar": ({"PAUSADO"}, "EN_EJECUCION"),
            # A paused color can be completed after its last deferred manga
            # is weighed while a different color keeps the machine running.
            "completar": ({"EN_EJECUCION", "PAUSADO"}, "COMPLETADO"),
        }
        if action == "anular":
            if work.estado not in {"PLANIFICADO", "PAUSADO"}:
                raise ScmServiceError(
                    "INVALID_STATE_TRANSITION",
                    "Solo un trabajo planificado o pausado puede anularse.",
                    status_code=409,
                )
            inherited_open = [
                item for item in (getattr(work, "tramos_manga", ()) or ())
                if item.secuencia > 1
                and item.estado in {"PROGRAMADO", "ACTIVO"}
            ]
            if inherited_open:
                raise ScmServiceError(
                    "WORK_HAS_OPEN_CONTINUITIES",
                    "El trabajo posee mangas heredadas; reprograme su continuidad antes de anularlo.",
                    status_code=409,
                    details={"pending": len(inherited_open)},
                )
            non_reversible = [
                manga for manga in work.mangas
                if manga.estado in _terminal_manga_states() - {"ANULADA"}
            ]
            if non_reversible:
                raise ScmServiceError(
                    "WORK_HAS_PRODUCTION_FACTS",
                    "El trabajo posee mangas pesadas o recibidas y no puede anularse.",
                    status_code=409,
                )
            reason = required_text(
                data.get("motivo"), field="motivo", max_length=500
            )
            for manga in work.mangas:
                if manga.estado != "ANULADA":
                    _annul_unweighed_manga_model(
                        manga, actor=actor, reason=reason
                    )
            work.estado = "ANULADO"
            work.anulada_at = now
            work.anulada_por_id = actor.id
            work.motivo_anulacion = reason
            _close_work_assignment(
                work,
                actor=actor,
                cancelled=True,
                terminal=True,
                reason=reason,
            )
            event_type = "COLOR_WORK_CANCELLED"
        else:
            expected_states, target_state = transition[action]
            if work.estado not in expected_states:
                raise ScmServiceError(
                    "INVALID_STATE_TRANSITION",
                    "El trabajo no se encuentra en un estado valido para la accion.",
                    status_code=409,
                )
            if target_state == "EN_EJECUCION":
                # Serialize every start/resume by physical machine and parent
                # OT. This closes the race between two different headers that
                # both observe the machine as idle before either commits.
                session.scalar(
                    select(Maquina)
                    .where(Maquina.id == work.orden_trabajo.maquina_id)
                    .with_for_update()
                )
                session.scalar(
                    select(RegistroDiarioProduccion)
                    .where(
                        RegistroDiarioProduccion.id
                        == work.orden_trabajo_id
                    )
                    .with_for_update()
                )
                running_same_ot = session.scalar(
                    select(ScmTrabajoOt).where(
                        ScmTrabajoOt.orden_trabajo_id == work.orden_trabajo_id,
                        ScmTrabajoOt.id != work.id,
                        ScmTrabajoOt.estado == "EN_EJECUCION",
                    )
                )
                if running_same_ot is not None:
                    raise ScmServiceError(
                        "OT_WORK_ALREADY_RUNNING",
                        "La OT ya tiene otro trabajo en ejecucion.",
                        status_code=409,
                        details={"trabajo_color_id": str(running_same_ot.id)},
                    )
                running_machine = session.scalar(
                    select(ScmTrabajoOt)
                    .join(
                        RegistroDiarioProduccion,
                        RegistroDiarioProduccion.id
                        == ScmTrabajoOt.orden_trabajo_id,
                    )
                    .where(
                        RegistroDiarioProduccion.maquina_id
                        == work.orden_trabajo.maquina_id,
                        ScmTrabajoOt.id != work.id,
                        ScmTrabajoOt.estado == "EN_EJECUCION",
                    )
                )
                if running_machine is not None:
                    raise ScmServiceError(
                        "MACHINE_ALREADY_RUNNING",
                        "La maquina ya ejecuta otro trabajo.",
                        status_code=409,
                        details={"trabajo_color_id": str(running_machine.id)},
                    )
                current_personal = _activate_work_assignment(
                    session, work=work, actor=actor
                )
                for segment in getattr(work, "tramos_manga", ()) or ():
                    if segment.estado != "PROGRAMADO":
                        continue
                    segment.estado = "ACTIVO"
                    segment.iniciada_at = now
                    segment.asignacion_personal_trabajo_id = current_personal.id
                    segment.manga.estado = "EN_LLENADO"
                    segment.manga.version += 1
                work.iniciada_at = work.iniciada_at or now
                work.pausada_at = None
                work.orden_trabajo.iniciada_at = (
                    work.orden_trabajo.iniciada_at or now
                )
                work.orden_trabajo.estado = "EN_EJECUCION"
                work.trabajo_color.corrida.estado = "EN_EJECUCION"
                if work.orden_operacion.estado in {"LIBERADA", "PROGRAMADA"}:
                    work.orden_operacion.estado = "EN_EJECUCION"
                    work.orden_operacion.started_by_id = actor.id
                    work.orden_operacion.started_at = now
                    work.orden_operacion.version += 1
                event_type = (
                    "COLOR_WORK_STARTED"
                    if action == "iniciar" else "COLOR_WORK_RESUMED"
                )
            elif action == "pausar":
                work.pausada_at = now
                work.motivo_pausa = (
                    str(data.get("motivo") or "").strip()[:500] or None
                )
                # Close the operating interval. Mangas retain this assignment
                # as their immutable attribution and remain weighable.
                _close_work_assignment(
                    work,
                    actor=actor,
                    reason=work.motivo_pausa or "Pausa del trabajo de color",
                )
                event_type = "COLOR_WORK_PAUSED"
            else:
                pending = [
                    manga for manga in _work_mangas(work)
                    if not _manga_resolved_for_work(manga, work)
                ]
                if pending:
                    raise ScmServiceError(
                        "WORK_HAS_PENDING_MANGAS",
                        "El trabajo conserva mangas sin pesar o anular.",
                        status_code=409,
                        details={"pending": len(pending)},
                    )
                work.completada_at = now
                work.cantidad_confirmada_un = _work_confirmed_quantity(work)
                _close_work_assignment(work, actor=actor, terminal=True)
                event_type = "COLOR_WORK_COMPLETED"
            work.estado = target_state
        work.version += 1
        _recompute_parent_state(session, work)
        session.flush()
        after = _serialize_color_work(work)
        response = {
            "trabajo_color": after,
            "ot": _serialize_ot(work.orden_trabajo),
        }
        change_event = _event(
            "TRABAJO_COLOR", work.id, event_type,
            actor, operation, after,
        )
        change_event.before_json = before
        change_event.motivo = str(data.get("motivo") or "").strip() or None
        session.add(change_event)
        _complete_operation(operation, response)
        session.commit()
        return response
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "OT_WORK_ALREADY_RUNNING",
            "La OT ya tiene otro trabajo en ejecucion.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise


def add_work_mangas(
    session,
    *,
    actor_id,
    work_id,
    operation_id,
    data,
):
    actor = load_actor(session, actor_id, capability="MANGA_PLANIFICAR")
    endpoint = f"/trabajos-color/{work_id}/mangas"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    reject_unknown_fields(data, allowed={"plan_linea_id", "cantidad_un"})
    try:
        work = _load_color_work(session, work_id, lock=True)
        if work.estado not in {"PLANIFICADO", "EN_EJECUCION", "PAUSADO"}:
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "El trabajo no admite nuevas mangas.",
                status_code=409,
            )
        line = session.scalar(
            select(ScmPlanMangaOpLinea)
            .join(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOpLinea.id == data.get("plan_linea_id"),
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        if line is None or not _plan_line_belongs_to_work(line, work):
            raise ScmServiceError(
                "PLAN_LINE_NOT_FOUND",
                "La linea no pertenece al trabajo de color.",
                status_code=404,
            )
        quantity = _decimal(data.get("cantidad_un"), "cantidad_un", integral=True)
        already = session.scalar(
            select(func.coalesce(
                func.sum(ScmAsignacionPlanMangaOt.cantidad_asignada_un), 0
            )).where(ScmAsignacionPlanMangaOt.plan_linea_id == line.id)
        )
        if Decimal(already) + quantity > Decimal(line.cantidad_objetivo_un):
            raise ScmServiceError(
                "PLAN_BALANCE_EXCEEDED",
                "La asignacion excede el saldo del plan.",
                status_code=409,
            )
        assignment = session.scalar(
            select(ScmAsignacionPlanMangaOt)
            .where(
                ScmAsignacionPlanMangaOt.plan_linea_id == line.id,
                ScmAsignacionPlanMangaOt.trabajo_ot_id == work.id,
            )
            .with_for_update()
        )
        count = int(math.ceil(quantity / Decimal(line.capacidad_efectiva_un)))
        if assignment is None:
            assignment = ScmAsignacionPlanMangaOt(
                plan_linea_id=line.id,
                ot_id=work.orden_trabajo_id,
                trabajo_ot_id=work.id,
                cantidad_asignada_un=quantity,
                mangas_asignadas=count,
                asignada_por_id=actor.id,
            )
            session.add(assignment)
            session.flush()
        else:
            assignment.cantidad_asignada_un += quantity
            assignment.mangas_asignadas += count
        mangas = _create_mangas(
            session,
            ot=work.orden_trabajo,
            work=work,
            line=line,
            assignment=assignment,
            personal_assignment=_current_or_planned_work_assignment(work),
            quantity=quantity,
            actor=actor,
            kind="NORMAL",
        )
        work.cantidad_objetivo_un += quantity
        work.version += 1
        response = {
            "trabajo_color": _serialize_color_work(work),
            "mangas": [_serialize_manga(item) for item in mangas],
        }
        for manga in mangas:
            session.add(_event(
                "MANGA", manga.id, "MANGA_PLANNED_FOR_COLOR_WORK",
                actor, operation, _serialize_manga(manga),
            ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _replace_prelabels_for_reassignment(session, mangas, *, actor, reason):
    targets = []
    for manga in mangas:
        current = next(
            (
                label for label in reversed(manga.etiquetas)
                if label.tipo == "PREPESAJE" and label.estado != "INVALIDADA"
            ),
            None,
        )
        if current is not None:
            targets.append((manga, current))
    jobs = []
    for offset in range(0, len(targets), 2):
        batch = targets[offset:offset + 2]
        replacements = []
        for manga, current in batch:
            version = max(
                (
                    label.version for label in manga.etiquetas
                    if label.tipo == "PREPESAJE"
                ),
                default=0,
            ) + 1
            label_id = uuid.uuid4()
            payload = _label_payload(manga, label_id, version)
            replacements.append((manga, current, label_id, version, payload))
        job = ScmTrabajoImpresionManga(
            plantilla_version=replacements[0][4]["template"]["version"],
            payload_hash=_json_hash([item[4] for item in replacements]),
            solicitado_por_id=actor.id,
        )
        session.add(job)
        session.flush()
        labels = []
        for manga, current, label_id, version, payload in replacements:
            replacement = ScmEtiquetaManga(
                public_id=label_id,
                manga_id=manga.id,
                trabajo_impresion_id=job.public_id,
                tipo="PREPESAJE",
                version=version,
                plantilla_version=payload["template"]["version"],
                payload_json=payload,
                payload_hash=_json_hash(payload),
            )
            session.add(replacement)
            session.flush()
            current.estado = "INVALIDADA"
            current.invalidada_por_id = actor.id
            current.invalidada_at = utc_now()
            current.motivo_invalidacion = (
                f"Relevo de maquinista: {reason}"
            )
            current.reemplazada_por_id = replacement.id
            labels.append(replacement)
        jobs.append({
            "print_job_id": str(job.public_id),
            "labels": [_serialize_label(label) for label in labels],
        })
    return jobs


def assign_color_work_worker(
    session,
    *,
    actor_id,
    work_id,
    operation_id,
    data,
):
    actor = load_actor(session, actor_id, capability="OT_CREAR")
    endpoint = f"/trabajos-color/{work_id}/asignaciones"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    reject_unknown_fields(
        data,
        allowed={
            "trabajador_id",
            "motivo",
            "version",
            "manga_ids",
            "manga_abierta",
            "conteo_frontera",
            "confirmacion_stickers_vacios",
        },
    )
    try:
        work = _load_color_work(session, work_id, lock=True)
        if work.estado in {"COMPLETADO", "ANULADO"}:
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "El trabajo cerrado no admite relevos.",
                status_code=409,
            )
        if work.version != expected_version(data.get("version")):
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "El trabajo fue modificado por otra operacion.",
                status_code=409,
            )
        worker = session.get(Trabajador, data.get("trabajador_id"))
        _validate_color_worker(worker)
        reason = required_text(
            data.get("motivo"), field="motivo", max_length=500
        )
        raw_ids = data.get("manga_ids")
        eligible_states = {"PLANIFICADA", "PREETIQUETADA"}
        eligible = [
            manga for manga in work.mangas if manga.estado in eligible_states
        ]
        if raw_ids is not None:
            if not isinstance(raw_ids, list):
                raise ScmServiceError(
                    "INVALID_MANGA_SELECTION",
                    "manga_ids debe ser una lista.",
                    status_code=400,
                )
            requested = {_uuid_value(value, field="manga_ids") for value in raw_ids}
            selected = [manga for manga in eligible if manga.public_id in requested]
            if len(selected) != len(requested):
                foreign_manga = session.scalar(
                    select(ScmManga.id).where(
                        ScmManga.public_id.in_(requested),
                        ScmManga.trabajo_ot_id != work.id,
                    )
                )
                if foreign_manga is not None:
                    raise ScmServiceError(
                        "MULTI_SHIFT_BAG_NOT_ENABLED",
                        "Una manga no puede transferirse a otro trabajo u OT en este piloto.",
                        status_code=409,
                        details={"follow_up": "US-010K"},
                    )
                raise ScmServiceError(
                    "MANGA_NOT_ELIGIBLE_FOR_REASSIGNMENT",
                    "Una manga no pertenece al trabajo o ya fue pesada/anulada.",
                    status_code=409,
                )
        else:
            # Compatibilidad con clientes anteriores: omitir manga_ids
            # conserva la transferencia de todas las mangas elegibles. Los
            # clientes nuevos envían [] para registrar solo el intervalo de
            # responsabilidad, sin transferir stickers.
            selected = eligible
        active_assignment = _active_work_assignment(work)
        same_worker_mangas = [
            manga.codigo
            for manga in selected
            if (
                manga.asignacion_personal_trabajo is not None
                and manga.asignacion_personal_trabajo.trabajador_id == worker.id
            )
            or manga.maquinista_previsto_id == worker.id
        ]
        if (
            active_assignment is not None
            and active_assignment.trabajador_id == worker.id
        ) or same_worker_mangas:
            raise ScmServiceError(
                "WORKER_ALREADY_ASSIGNED",
                "El nuevo maquinista debe ser distinto del responsable actual.",
                status_code=422,
                details={"mangas_sin_cambio": same_worker_mangas},
            )
        if not selected and not (
            raw_ids == []
            and work.estado == "EN_EJECUCION"
            and active_assignment is not None
        ):
            raise ScmServiceError(
                "MANGA_NOT_ELIGIBLE_FOR_REASSIGNMENT",
                "El relevo sin stickers requiere un trabajo en ejecución y una asignación activa.",
                status_code=409,
            )
        if data.get("manga_abierta") is True or data.get("conteo_frontera") is not None:
            raise ScmServiceError(
                "OPEN_MANGA_RELIEF_NOT_ALLOWED",
                "Una manga con contenido no se transfiere. Pésela con el responsable saliente y registre el relevo después.",
                status_code=422,
            )
        selected_prelabels = [
            manga for manga in selected if manga.estado == "PREETIQUETADA"
        ]
        if (
            selected_prelabels
            and data.get("confirmacion_stickers_vacios") is not True
        ):
            raise ScmServiceError(
                "EMPTY_STICKER_CONFIRMATION_REQUIRED",
                "Confirme que las mangas preetiquetadas seleccionadas están vacías y sus stickers no fueron utilizados.",
                status_code=422,
            )
        before = {
            "trabajo_color": _serialize_color_work(work),
            "mangas": [_serialize_manga(item) for item in selected],
        }
        if active_assignment is not None:
            # A running machine has one principal operating interval. A
            # relief always closes that interval, even when only a subset of
            # its stickers is handed over.
            active_assignment.estado = "CERRADA"
            active_assignment.finalizada_at = utc_now()
            active_assignment.finalizada_por_id = actor.id
            active_assignment.motivo = reason
            active_assignment.version += 1
        else:
            # Before execution, different subsets may be planned for
            # different workers. Preserve a PREVISTA assignment while other
            # eligible mangas still reference it; cancel it only when its
            # complete pending subset moved away.
            selected_ids = {manga.id for manga in selected}
            planned_by_id = {
                manga.asignacion_personal_trabajo.id:
                    manga.asignacion_personal_trabajo
                for manga in selected
                if manga.asignacion_personal_trabajo is not None
                and manga.asignacion_personal_trabajo.estado == "PREVISTA"
            }
            for planned in planned_by_id.values():
                has_remaining = any(
                    manga.id not in selected_ids
                    and manga.estado in eligible_states
                    and manga.asignacion_personal_trabajo_id == planned.id
                    for manga in work.mangas
                )
                if not has_remaining:
                    planned.estado = "CANCELADA"
                    planned.finalizada_at = utc_now()
                    planned.finalizada_por_id = actor.id
                    planned.motivo = reason
                    planned.version += 1
        assignment = ScmAsignacionPersonalTrabajoOt(
            trabajo_ot_id=work.id,
            trabajador_id=worker.id,
            estado="ACTIVA" if work.estado == "EN_EJECUCION" else "PREVISTA",
            iniciada_at=(
                utc_now() if work.estado == "EN_EJECUCION"
                else None
            ),
            asignada_por_id=actor.id,
            motivo=reason,
        )
        session.add(assignment)
        session.flush()
        for manga in selected:
            manga.asignacion_personal_trabajo_id = assignment.id
            manga.maquinista_previsto_id = worker.id
            manga.version += 1
        session.flush()
        replacement_jobs = _replace_prelabels_for_reassignment(
            session,
            selected,
            actor=actor,
            reason=reason,
        )
        work.version += 1
        session.flush()
        response = {
            "trabajo_color": _serialize_color_work(work),
            "asignacion": _serialize_person_assignment(assignment),
            "mangas": [_serialize_manga(item) for item in selected],
            "trabajos_impresion_reemplazo": replacement_jobs,
            "transferencia_manga_abierta": None,
            "relevo_sin_stickers": not selected,
            "stickers_transferidos": len(selected),
        }
        event = _event(
            "TRABAJO_COLOR",
            work.id,
            "COLOR_WORK_WORKER_REASSIGNED",
            actor,
            operation,
            response,
        )
        event.before_json = before
        event.motivo = reason
        session.add(event)
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def create_ot(
    session, *, actor_id, op_number, operation_id, data
):
    actor = load_actor(session, actor_id, capability="OT_CREAR")
    endpoint = f"/ordenes-produccion/{op_number}/ots"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        order = _load_executable_order(session, op_number, lock=True)
        plan = session.scalar(
            select(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOp.orden_id == op_number,
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        if plan is None:
            raise ScmServiceError(
                "PACKAGING_RULE_MISSING",
                "La OP aun no tiene un plan de mangas activo.",
                status_code=422,
            )
        machine_id = data.get("maquina_id", order.maquina_id)
        machine = session.scalar(
            select(Maquina)
            .where(Maquina.id == machine_id)
            .with_for_update()
        )
        worker = session.get(Trabajador, data.get("maquinista_id"))
        if machine is None or not machine.activo:
            raise ScmServiceError(
                "INVALID_MACHINE", "La maquina no esta activa.", status_code=422
            )
        if worker is None or not worker.activo:
            raise ScmServiceError(
                "INVALID_WORKER", "El maquinista no esta activo.", status_code=422
            )
        allocations = data.get("asignaciones")
        if not isinstance(allocations, list) or not allocations:
            raise ScmServiceError(
                "REQUIRED_FIELD",
                "Se requiere al menos una asignacion del plan.",
                status_code=400,
            )
        ot = RegistroDiarioProduccion(
            public_id=uuid.uuid4(),
            codigo_ot=generar_codigo_catalogo(
                "ORDEN_TRABAJO", session=session
            ),
            codigo_ot_sintetico=False,
            estado="PLANIFICADA",
            orden_id=order.numero_op,
            maquina_id=machine.id,
            fecha=_parse_date(data.get("fecha_operativa")),
            turno=required_text(
                data.get("turno"), field="turno", max_length=20
            ).upper(),
            created_by_id=actor.id,
            maquinista_previsto_id=worker.id,
            maquina_codigo_snapshot=machine.codigo,
            maquina_nombre_snapshot=machine.nombre,
            snapshot_peso_colada_gr=order.snapshot_peso_colada_gr,
        )
        session.add(ot)
        session.flush()
        seen = set()
        for index, raw in enumerate(allocations):
            if not isinstance(raw, dict):
                raise ScmServiceError(
                    "JSON_OBJECT_REQUIRED",
                    "Cada asignacion debe ser un objeto.",
                    status_code=400,
                )
            line_id = raw.get("plan_linea_id")
            if line_id in seen:
                raise ScmServiceError(
                    "DUPLICATE_PLAN_LINE",
                    "Una linea no puede repetirse en la misma OT.",
                    status_code=422,
                )
            seen.add(line_id)
            line = session.scalar(
                select(ScmPlanMangaOpLinea)
                .where(
                    ScmPlanMangaOpLinea.id == line_id,
                    ScmPlanMangaOpLinea.plan_id == plan.id,
                )
                .with_for_update()
            )
            if line is None:
                raise ScmServiceError(
                    "PLAN_LINE_NOT_FOUND",
                    "La linea no pertenece al plan activo.",
                    status_code=404,
                    details={"index": index},
                )
            quantity = _decimal(
                raw.get("cantidad_un"), "cantidad_un", integral=True
            )
            already = session.scalar(
                select(func.coalesce(
                    func.sum(ScmAsignacionPlanMangaOt.cantidad_asignada_un),
                    0,
                )).where(
                    ScmAsignacionPlanMangaOt.plan_linea_id == line.id
                )
            )
            if Decimal(already) + quantity > Decimal(
                line.cantidad_objetivo_un
            ):
                raise ScmServiceError(
                    "PLAN_BALANCE_EXCEEDED",
                    "La asignacion excede el saldo del plan.",
                    status_code=409,
                    details={"plan_linea_id": line.id},
                )
            assignment = ScmAsignacionPlanMangaOt(
                plan_linea_id=line.id,
                ot_id=ot.id,
                cantidad_asignada_un=quantity,
                mangas_asignadas=int(math.ceil(
                    quantity / Decimal(line.capacidad_efectiva_un)
                )),
                asignada_por_id=actor.id,
            )
            session.add(assignment)
            session.flush()
            _create_mangas(
                session,
                ot=ot,
                line=line,
                assignment=assignment,
                quantity=quantity,
                actor=actor,
                kind="NORMAL",
            )
        session.flush()
        response = {"ot": _serialize_ot(ot)}
        session.add(_event(
            "ORDEN_TRABAJO", ot.id, "OT_CREATED",
            actor, operation, response["ot"],
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def create_fabrication_ot(
    session,
    *,
    actor_id,
    order_id,
    operation_id,
    data,
):
    actor = load_actor(session, actor_id, capability="OT_CREAR")
    endpoint = f"/ordenes-fabricacion/{order_id}/ots"
    operation, replay = _reserve_operation(
        session,
        operation_id,
        endpoint,
        actor,
        data,
    )
    if replay is not None:
        return replay
    try:
        order = _load_fabrication_order(session, order_id, lock=True)
        if order.estado not in (
            "LIBERADA",
            "PROGRAMADA",
            "EN_EJECUCION",
        ):
            raise ScmServiceError(
                "OF_NOT_RELEASABLE",
                "La OF debe estar liberada para crear una OT.",
                status_code=409,
            )
        try:
            run_id = uuid.UUID(str(data.get("corrida_fabricacion_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "OF_CORRIDA_REQUIRED",
                "La OT debe indicar una corrida valida.",
                status_code=422,
            ) from error
        run = session.scalar(
            select(ScmCorridaFabricacion)
            .where(
                ScmCorridaFabricacion.id == run_id,
                ScmCorridaFabricacion.orden_fabricacion_id == order.id,
            )
            .with_for_update()
        )
        if run is None or run.estado not in ("LIBERADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "OF_CORRIDA_REQUIRED",
                "La corrida no pertenece a la OF o no esta liberada.",
                status_code=422,
            )
        plan = session.scalar(
            select(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOp.orden_operacion_id == order.id,
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        if plan is None:
            raise ScmServiceError(
                "PACKAGING_RULE_MISSING",
                "La OF aun no tiene un plan de mangas activo.",
                status_code=422,
            )
        machine_id = data.get(
            "maquina_id",
            order.fabricacion.maquina_prevista_id,
        )
        machine = session.scalar(
            select(Maquina)
            .where(Maquina.id == machine_id)
            .with_for_update()
        )
        worker = session.get(Trabajador, data.get("maquinista_id"))
        if machine is None or not machine.activo:
            raise ScmServiceError(
                "INVALID_MACHINE",
                "La maquina no esta activa.",
                status_code=422,
            )
        if order.fabricacion.maquina_prevista_id != machine.id:
            raise ScmServiceError(
                "COLOR_WORK_MACHINE_MISMATCH",
                "La maquina indicada no coincide con la maquina prevista por la OF.",
                status_code=409,
            )
        _validate_color_worker(worker)
        allocations = data.get("asignaciones")
        if not isinstance(allocations, list) or not allocations:
            raise ScmServiceError(
                "REQUIRED_FIELD",
                "Se requiere al menos una asignacion del plan.",
                status_code=400,
            )
        operational_date = _parse_date(data.get("fecha_operativa"))
        shift = required_text(
            data.get("turno"), field="turno", max_length=20
        ).upper()
        ot = session.scalar(
            select(RegistroDiarioProduccion)
            .where(
                RegistroDiarioProduccion.tipo_ot == "FABRICACION",
                RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
                RegistroDiarioProduccion.maquina_id == machine.id,
                RegistroDiarioProduccion.fecha == operational_date,
                RegistroDiarioProduccion.turno == shift,
                RegistroDiarioProduccion.estado != "ANULADA",
                RegistroDiarioProduccion.orden_id.is_(None),
                RegistroDiarioProduccion.orden_operacion_id.is_(None),
                RegistroDiarioProduccion.corrida_fabricacion_id.is_(None),
            )
            .with_for_update()
        )
        header_created = ot is None
        if ot is None:
            ot = RegistroDiarioProduccion(
                public_id=uuid.uuid4(),
                codigo_ot=generar_codigo_catalogo(
                    "ORDEN_TRABAJO", session=session
                ),
                codigo_ot_sintetico=False,
                estado="PLANIFICADA",
                tipo_ot="FABRICACION",
                orden_id=None,
                orden_operacion_id=None,
                corrida_fabricacion_id=None,
                maquina_id=machine.id,
                fecha=operational_date,
                turno=shift,
                created_by_id=actor.id,
                maquinista_previsto_id=worker.id,
                maquina_codigo_snapshot=machine.codigo,
                maquina_nombre_snapshot=machine.nombre,
            )
            session.add(ot)
            session.flush()
        elif ot.estado not in {"PLANIFICADA", "EN_EJECUCION"}:
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "La cabecera del turno ya esta cerrada.",
                status_code=409,
            )
        duplicate = session.scalar(
            select(ScmTrabajoOt)
            .join(ScmTrabajoColor)
            .where(
                ScmTrabajoOt.orden_trabajo_id == ot.id,
                ScmTrabajoOt.estado != "ANULADO",
                ScmTrabajoColor.corrida_fabricacion_id == run.id,
            )
        )
        if duplicate is not None:
            raise ScmServiceError(
                "COLOR_WORK_ALREADY_EXISTS",
                "La corrida ya posee un trabajo en esta OT; reanudelo.",
                status_code=409,
                details={"trabajo_color_id": str(duplicate.id)},
            )
        work, personal = _create_color_work_model(
            session,
            ot=ot,
            order=order,
            run=run,
            worker=worker,
            actor=actor,
        )
        _allocate_work_lines(
            session,
            work=work,
            plan=plan,
            allocations=allocations,
            actor=actor,
            personal_assignment=personal,
        )
        if order.estado == "LIBERADA":
            order.estado = "PROGRAMADA"
            order.version += 1
        session.flush()
        session.expire(ot, ["trabajos_ot"])
        response = {
            "ot": _serialize_ot(ot),
            "trabajo_color": _serialize_color_work(work),
        }
        session.add(_event(
            "ORDEN_TRABAJO",
            ot.id,
            (
                "FABRICATION_OT_HEADER_CREATED_BY_LEGACY_FACADE"
                if header_created else "COLOR_WORK_ADDED_BY_LEGACY_FACADE"
            ),
            actor,
            operation,
            response["ot"],
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def get_ot(session, *, actor_id, public_id):
    load_actor(session, actor_id, capability="OT_VER")
    ot = session.scalar(select(RegistroDiarioProduccion).where(
        RegistroDiarioProduccion.public_id == public_id
    ))
    if ot is None or ot.codigo_ot_sintetico:
        raise ScmServiceError(
            "OT_NOT_FOUND", "La OT SCM no existe.", status_code=404
        )
    return {"ot": _serialize_ot(ot)}


def list_ots(
    session,
    *,
    actor_id,
    op_number=None,
    operation_order_id=None,
    tipo_ot=None,
    operational_date=None,
    machine_id=None,
    machine=None,
    shift=None,
):
    load_actor(session, actor_id, capability="OT_VER")
    statement = (
        select(RegistroDiarioProduccion)
        .options(
            selectinload(RegistroDiarioProduccion.detalles),
            selectinload(
                RegistroDiarioProduccion.ot_fabricacion_contexto
            ),
        )
        .where(RegistroDiarioProduccion.codigo_ot_sintetico.is_(False))
    )
    normalized_type = str(tipo_ot or "").strip().upper()
    if normalized_type and normalized_type not in ("FABRICACION", "ENSAMBLE"):
        raise ScmServiceError(
            "OT_TYPE_INVALID",
            "tipo_ot debe ser FABRICACION o ENSAMBLE.",
            status_code=400,
        )
    if normalized_type:
        statement = statement.where(
            RegistroDiarioProduccion.tipo_ot == normalized_type
        )
    if operational_date:
        statement = statement.where(
            RegistroDiarioProduccion.fecha == _parse_date(operational_date)
        )
    if machine_id is not None and str(machine_id).strip():
        try:
            parsed_machine_id = int(machine_id)
        except (TypeError, ValueError) as error:
            raise ScmServiceError(
                "INVALID_MACHINE", "maquina_id debe ser entero.", status_code=400
            ) from error
        if parsed_machine_id <= 0:
            raise ScmServiceError(
                "INVALID_MACHINE", "maquina_id debe ser positivo.", status_code=400
            )
        statement = statement.where(
            RegistroDiarioProduccion.maquina_id == parsed_machine_id
        )
    if machine:
        normalized_machine = str(machine).strip().upper()
        statement = statement.where(or_(
            func.upper(RegistroDiarioProduccion.maquina_codigo_snapshot)
            == normalized_machine,
            func.upper(RegistroDiarioProduccion.maquina_nombre_snapshot)
            == normalized_machine,
        ))
    if shift:
        normalized_shift = required_text(
            shift, field="turno", max_length=20
        ).upper()
        statement = statement.where(
            RegistroDiarioProduccion.turno == normalized_shift
        )
    if op_number and operation_order_id:
        raise ScmServiceError(
            "AMBIGUOUS_ORDER_FILTER",
            "Filtre las OT por OP legacy o por OF, no por ambas.",
            status_code=400,
        )
    if op_number:
        statement = statement.where(
            RegistroDiarioProduccion.orden_id == op_number
        )
    if operation_order_id:
        try:
            canonical_id = uuid.UUID(str(operation_order_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "INVALID_UUID",
                "orden_operacion_id debe ser un UUID valido.",
                status_code=400,
            ) from error
        statement = statement.where(
            or_(
                RegistroDiarioProduccion.orden_operacion_id == canonical_id,
                RegistroDiarioProduccion.trabajos_ot.any(
                    ScmTrabajoOt.orden_operacion_id == canonical_id
                ),
            )
        )
    items = session.scalars(
        statement.order_by(
            RegistroDiarioProduccion.fecha.desc(),
            RegistroDiarioProduccion.id.desc(),
        )
    ).all()
    mangas_by_ot = {item.id: [] for item in items}
    if mangas_by_ot:
        for manga in session.scalars(
            select(ScmManga)
            .where(ScmManga.ot_id.in_(mangas_by_ot))
            .order_by(ScmManga.ot_id, ScmManga.secuencia_ot)
        ).all():
            mangas_by_ot[manga.ot_id].append(manga)
    serialization_context = _batch_ot_serialization_context(session, items)
    return {
        "items": [
            _serialize_ot(
                item,
                mangas=mangas_by_ot[item.id],
                assembly_context=serialization_context[
                    "assembly_by_ot"
                ].get(item.id, (None, None)),
                color_context=serialization_context[
                    "color_context_by_ot"
                ].get(item.id),
                output_articles_by_work=serialization_context[
                    "output_articles_by_work"
                ],
            )
            for item in items
        ]
    }


def list_plant_journeys(
    session,
    *,
    actor_id,
    operational_date,
    shift,
):
    """Aggregate the daily board in one authenticated HTTP request."""
    load_actor(session, actor_id, capability="OT_VER")
    parsed_date = _parse_date(operational_date)
    normalized_shift = required_text(
        shift,
        field="turno",
        max_length=20,
    ).upper()
    machines = session.scalars(
        select(Maquina)
        .options(selectinload(Maquina.tipo_maquina))
        .order_by(Maquina.codigo)
    ).all()
    work_centers = session.scalars(
        select(ScmCentroTrabajo).order_by(ScmCentroTrabajo.codigo)
    ).all()
    journeys = list_ots(
        session,
        actor_id=actor_id,
        operational_date=parsed_date.isoformat(),
        shift=normalized_shift,
    )
    fabrication = []
    assembly = []
    for item in journeys["items"]:
        target = assembly if item["tipo_ot"] == "ENSAMBLE" else fabrication
        target.append(item)
    return {
        "fecha_operativa": parsed_date.isoformat(),
        "turno": normalized_shift,
        "maquinas": [item.to_dict() for item in machines],
        "centros_trabajo": [item.to_dict() for item in work_centers],
        "ots_fabricacion": fabrication,
        "ots_armado": assembly,
    }


def transition_ot(
    session, *, actor_id, public_id, operation_id, data, action
):
    capability = "OT_INICIAR" if action == "iniciar" else "OT_CERRAR"
    actor = load_actor(session, actor_id, capability=capability)
    endpoint = f"/ots/{public_id}/{action}"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        ot = session.scalar(
            select(RegistroDiarioProduccion)
            .where(RegistroDiarioProduccion.public_id == public_id)
            .with_for_update()
        )
        if ot is None or ot.codigo_ot_sintetico:
            raise ScmServiceError(
                "OT_NOT_FOUND", "La OT SCM no existe.", status_code=404
            )
        if ot.trabajos_ot and action != "cerrar":
            raise ScmServiceError(
                "USE_COLOR_WORK_ACTION",
                "Inicie, pause o complete el trabajo de color; la cabecera se proyecta automaticamente.",
                status_code=409,
            )
        version = expected_version(data.get("version"))
        if ot.version != version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OT fue modificada por otra operacion.",
                status_code=409,
            )
        if ot.trabajos_ot and action == "cerrar":
            pending_works = [
                item for item in ot.trabajos_ot
                if item.estado not in {"COMPLETADO", "ANULADO"}
            ]
            if pending_works:
                raise ScmServiceError(
                    "OT_HAS_PENDING_COLOR_WORKS",
                    "La OT conserva trabajos de color sin completar o anular.",
                    status_code=409,
                    details={"pending": len(pending_works)},
                )
            ot.estado = "CERRADA"
            ot.cerrada_at = utc_now()
            ot.version += 1
            response = {"ot": _serialize_ot(ot)}
            session.add(_event(
                "ORDEN_TRABAJO", ot.id, "FABRICATION_OT_HEADER_CLOSED",
                actor, operation, response["ot"],
            ))
            _complete_operation(operation, response)
            session.commit()
            return response
        expected_state = "PLANIFICADA" if action == "iniciar" else "EN_EJECUCION"
        if ot.estado != expected_state:
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                f"La OT debe estar {expected_state}.",
                status_code=409,
            )
        if action == "cerrar":
            pending = session.scalar(select(func.count(ScmManga.id)).where(
                ScmManga.ot_id == ot.id,
                ScmManga.estado.notin_(("PESADA", "ANULADA")),
            ))
            if pending:
                raise ScmServiceError(
                    "OT_HAS_PENDING_MANGAS",
                    "La OT conserva mangas sin pesar o anular.",
                    status_code=409,
                    details={"pending": pending},
                )
            ot.estado = "CERRADA"
            ot.cerrada_at = utc_now()
            event_type = "OT_CLOSED"
        else:
            ot.estado = "EN_EJECUCION"
            ot.iniciada_at = utc_now()
            event_type = "OT_STARTED"
        ot.version += 1
        response = {"ot": _serialize_ot(ot)}
        session.add(_event(
            "ORDEN_TRABAJO", ot.id, event_type,
            actor, operation, response["ot"],
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def request_extra_manga(
    session, *, actor_id, public_id, operation_id, data
):
    actor = load_actor(
        session, actor_id, capability="MANGA_EXTRA_SOLICITAR"
    )
    endpoint = f"/ots/{public_id}/mangas-extra/solicitudes"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        ot = session.scalar(select(RegistroDiarioProduccion).where(
            RegistroDiarioProduccion.public_id == public_id
        ))
        if ot is None or ot.codigo_ot_sintetico:
            raise ScmServiceError(
                "OT_NOT_FOUND", "La OT SCM no existe.", status_code=404
            )
        work = _resolve_color_work_for_ot(
            ot, data.get("trabajo_color_id"), required=False
        )
        line = session.get(ScmPlanMangaOpLinea, data.get("plan_linea_id"))
        if line is None or (
            not _plan_line_belongs_to_work(line, work)
            if work is not None else not _plan_line_belongs_to_ot(line, ot)
        ):
            raise ScmServiceError(
                "PLAN_LINE_NOT_FOUND",
                "La linea no corresponde a la orden y corrida de la OT.",
                status_code=404,
            )
        request_item = ScmSolicitudMangaExtra(
            ot_id=ot.id,
            trabajo_ot_id=work.id if work is not None else None,
            plan_linea_id=line.id,
            cantidad_solicitada_un=_decimal(
                data.get("cantidad_un"), "cantidad_un", integral=True
            ),
            motivo=required_text(
                data.get("motivo"), field="motivo", max_length=250
            ),
            solicitada_por_id=actor.id,
        )
        session.add(request_item)
        session.flush()
        response = {"solicitud": _serialize_extra_request(request_item)}
        session.add(_event(
            "SOLICITUD_MANGA_EXTRA", request_item.id,
            "MANGA_EXTRA_REQUESTED", actor, operation,
            response["solicitud"],
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def add_normal_mangas(
    session, *, actor_id, public_id, operation_id, data
):
    actor = load_actor(session, actor_id, capability="MANGA_PLANIFICAR")
    endpoint = f"/ots/{public_id}/mangas"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        ot = session.scalar(
            select(RegistroDiarioProduccion)
            .where(RegistroDiarioProduccion.public_id == public_id)
            .with_for_update()
        )
        if ot is None or ot.codigo_ot_sintetico:
            raise ScmServiceError(
                "OT_NOT_FOUND", "La OT SCM no existe.", status_code=404
            )
        if ot.estado not in ("PLANIFICADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "La OT no admite nuevas mangas normales.",
                status_code=409,
            )
        work = None
        color_works = [item for item in ot.trabajos_ot if item.tipo == "COLOR"]
        if color_works:
            requested_work_id = data.get("trabajo_color_id")
            if requested_work_id is not None:
                parsed_work_id = _uuid_value(
                    requested_work_id, field="trabajo_color_id"
                )
                work = next(
                    (item for item in color_works if item.id == parsed_work_id),
                    None,
                )
                if work is None:
                    raise ScmServiceError(
                        "TRABAJO_NO_PERTENECE_A_OT",
                        "El trabajo no pertenece a la OT.",
                        status_code=409,
                    )
            elif len(color_works) == 1:
                work = color_works[0]
            else:
                raise ScmServiceError(
                    "TRABAJO_COLOR_REQUIRED",
                    "La OT contiene varios colores; indique el trabajo.",
                    status_code=422,
                )
        line = session.scalar(
            select(ScmPlanMangaOpLinea)
            .join(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOpLinea.id == data.get("plan_linea_id"),
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        if line is None or (
            not _plan_line_belongs_to_work(line, work)
            if work is not None else not _plan_line_belongs_to_ot(line, ot)
        ):
            raise ScmServiceError(
                "PLAN_LINE_NOT_FOUND",
                "La linea no pertenece al plan activo de la orden y corrida.",
                status_code=404,
            )
        quantity = _decimal(
            data.get("cantidad_un"), "cantidad_un", integral=True
        )
        already = session.scalar(
            select(func.coalesce(
                func.sum(ScmAsignacionPlanMangaOt.cantidad_asignada_un), 0
            )).where(ScmAsignacionPlanMangaOt.plan_linea_id == line.id)
        )
        if Decimal(already) + quantity > Decimal(line.cantidad_objetivo_un):
            raise ScmServiceError(
                "PLAN_BALANCE_EXCEEDED",
                "La asignacion excede el saldo del plan.",
                status_code=409,
            )
        assignment = session.scalar(select(ScmAsignacionPlanMangaOt).where(
            ScmAsignacionPlanMangaOt.plan_linea_id == line.id,
            ScmAsignacionPlanMangaOt.ot_id == ot.id,
            ScmAsignacionPlanMangaOt.trabajo_ot_id
            == (work.id if work is not None else None),
        ))
        new_count = int(math.ceil(
            quantity / Decimal(line.capacidad_efectiva_un)
        ))
        if assignment is None:
            assignment = ScmAsignacionPlanMangaOt(
                plan_linea_id=line.id,
                ot_id=ot.id,
                trabajo_ot_id=work.id if work is not None else None,
                cantidad_asignada_un=quantity,
                mangas_asignadas=new_count,
                asignada_por_id=actor.id,
            )
            session.add(assignment)
            session.flush()
        else:
            assignment.cantidad_asignada_un += quantity
            assignment.mangas_asignadas += new_count
        mangas = _create_mangas(
            session, ot=ot, work=work, line=line, assignment=assignment,
            quantity=quantity, actor=actor, kind="NORMAL",
        )
        if work is not None:
            work.cantidad_objetivo_un += quantity
            work.version += 1
        response = {"mangas": [_serialize_manga(item) for item in mangas]}
        for manga in mangas:
            session.add(_event(
                "MANGA", manga.id, "MANGA_PLANNED",
                actor, operation, _serialize_manga(manga),
            ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _serialize_extra_request(item):
    return {
        "id": str(item.public_id),
        "ot_id": str(item.ot.public_id),
        "trabajo_color_id": (
            str(item.trabajo_ot_id) if item.trabajo_ot_id else None
        ),
        "plan_linea_id": item.plan_linea_id,
        "cantidad_solicitada_un": _compact_number(
            item.cantidad_solicitada_un
        ),
        "motivo": item.motivo,
        "estado": item.estado,
        "solicitada_por_id": item.solicitada_por_id,
        "resuelta_por_id": item.resuelta_por_id,
        "version": item.version,
    }


def list_extra_manga_requests(
    session,
    *,
    actor_id,
    op_number=None,
    operation_order_id=None,
    state=None,
):
    load_actor(session, actor_id, capability="OT_VER")
    statement = select(ScmSolicitudMangaExtra).join(
        RegistroDiarioProduccion,
        RegistroDiarioProduccion.id == ScmSolicitudMangaExtra.ot_id,
    )
    if op_number:
        statement = statement.where(
            RegistroDiarioProduccion.orden_id == op_number
        )
    if op_number and operation_order_id:
        raise ScmServiceError(
            "AMBIGUOUS_ORDER_FILTER",
            "Filtre las solicitudes por OP legacy o por OF, no por ambas.",
            status_code=400,
        )
    if operation_order_id:
        try:
            canonical_id = uuid.UUID(str(operation_order_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "INVALID_UUID",
                "orden_operacion_id debe ser un UUID valido.",
                status_code=400,
            ) from error
        statement = statement.where(
            or_(
                RegistroDiarioProduccion.orden_operacion_id == canonical_id,
                ScmSolicitudMangaExtra.trabajo.has(
                    ScmTrabajoOt.orden_operacion_id == canonical_id
                ),
            )
        )
    if state:
        normalized = str(state).strip().upper()
        if normalized not in {"PENDIENTE", "APROBADA", "RECHAZADA"}:
            raise ScmServiceError(
                "INVALID_EXTRA_REQUEST_STATE",
                "El estado de solicitud extra no es valido.",
                status_code=400,
            )
        statement = statement.where(
            ScmSolicitudMangaExtra.estado == normalized
        )
    items = session.scalars(
        statement.order_by(ScmSolicitudMangaExtra.id.desc())
    ).all()
    return {"items": [_serialize_extra_request(item) for item in items]}


def approve_extra_manga(
    session, *, actor_id, request_id, operation_id, data
):
    actor = load_actor(
        session, actor_id, capability="MANGA_EXTRA_APROBAR"
    )
    endpoint = f"/mangas-extra/solicitudes/{request_id}/aprobar"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        item = session.scalar(
            select(ScmSolicitudMangaExtra)
            .where(ScmSolicitudMangaExtra.public_id == request_id)
            .with_for_update()
        )
        if item is None:
            raise ScmServiceError(
                "EXTRA_REQUEST_NOT_FOUND",
                "La solicitud no existe.",
                status_code=404,
            )
        if item.estado != "PENDIENTE":
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "La solicitud ya fue resuelta.",
                status_code=409,
            )
        if item.solicitada_por_id == actor.id:
            raise ScmServiceError(
                "FOUR_EYES_REQUIRED",
                "Solicitante y aprobador deben ser personas distintas.",
                status_code=409,
            )
        item.estado = "APROBADA"
        item.resuelta_por_id = actor.id
        item.resuelta_at = utc_now()
        item.version += 1
        mangas = _create_mangas(
            session,
            ot=item.ot,
            work=item.trabajo,
            line=item.plan_linea,
            assignment=None,
            quantity=item.cantidad_solicitada_un,
            actor=actor,
            kind="EXTRA",
            reason=item.motivo,
            requester_id=item.solicitada_por_id,
            approver_id=actor.id,
        )
        response = {
            "solicitud": _serialize_extra_request(item),
            "mangas": [_serialize_manga(manga) for manga in mangas],
        }
        session.add(_event(
            "SOLICITUD_MANGA_EXTRA", item.id,
            "MANGA_EXTRA_APPROVED", actor, operation, response,
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _label_payload(manga, label_id, version):
    work = manga.trabajo
    aggregate_header = work is not None and manga.ot.orden_operacion_id is None
    canonical = work is not None or manga.ot.orden_operacion_id is not None
    operation_order = (
        work.orden_operacion if work is not None else manga.ot.orden_operacion
    )
    order_code = (
        operation_order.codigo
        if canonical
        else manga.ot.orden_id
    )
    template_version = (
        "PREPESAJE_TSPL_3"
        if aggregate_header
        else ("PREPESAJE_TSPL_2" if canonical else "PREPESAJE_TSPL_1")
    )
    personal = manga.asignacion_personal_trabajo
    worker = personal.trabajador if personal is not None else manga.maquinista_previsto
    qr = {
        "v": 1,
        "type": "SCM_MANGA_LABEL",
        "manga_id": str(manga.public_id),
        "label_id": str(label_id),
        "label_type": "PREPESAJE",
        "label_version": version,
        "trabajo_color_id": str(work.id) if work is not None else None,
    }
    return {
        "template": {
            "version": template_version,
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
        "ot": {
            "id": str(manga.ot.public_id),
            "codigo": manga.ot.codigo_ot,
        },
        "trabajo_color": (
            {
                "id": str(work.id),
                "codigo": work.codigo,
                "corrida_fabricacion_id": str(
                    work.trabajo_color.corrida_fabricacion_id
                ),
            }
            if work is not None else None
        ),
        "responsable_tipo": (
            "RESPONSABLE_ARMADO"
            if manga.ot.tipo_ot == "ENSAMBLE" else "MAQUINISTA"
        ),
        "pieza_color": (
            (
                f"{manga.articulo_nombre_snapshot} "
                f"({manga.pieza_color_sku_snapshot})"
            )
            if manga.pieza_color_sku_snapshot
            else manga.articulo_nombre_snapshot
        ),
        "color": manga.color_snapshot,
        "codigo_manga": manga.codigo,
        "tipo_manga": manga.tipo,
        "cantidad_planificada_un": _compact_number(
            manga.cantidad_planificada_un
        ),
        "qr": qr,
        **(
            {
                (
                    "oa_ot"
                    if manga.ot.tipo_ot == "ENSAMBLE"
                    else "of_ot"
                ): (
                    f"{order_code} - {manga.ot.codigo_ot}"
                )
            }
            if canonical
            else {"op_ot": f"{order_code} - {manga.ot.codigo_ot}"}
        ),
    }


def generate_prelabels(
    session, *, actor_id, manga_id, operation_id, data
):
    actor = load_actor(
        session, actor_id, capability="MANGA_ETIQUETA_PRE_GENERAR"
    )
    endpoint = f"/mangas/{manga_id}/etiquetas-prepesaje"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        raw_ids = data.get("manga_ids", [str(manga_id)])
        if not isinstance(raw_ids, list) or not (1 <= len(raw_ids) <= 2):
            raise ScmServiceError(
                "LABEL_BATCH_SIZE",
                "Un trabajo de impresion contiene una o dos mangas.",
                status_code=422,
            )
        ids = [uuid.UUID(str(value)) for value in raw_ids]
        if ids[0] != manga_id or len(set(ids)) != len(ids):
            raise ScmServiceError(
                "INVALID_LABEL_BATCH",
                "La primera manga debe coincidir con la ruta y no repetirse.",
                status_code=422,
            )
        mangas = session.scalars(
            select(ScmManga).where(ScmManga.public_id.in_(ids))
        ).all()
        by_id = {manga.public_id: manga for manga in mangas}
        if len(by_id) != len(ids):
            raise ScmServiceError(
                "MANGA_NOT_FOUND", "Una manga no existe.", status_code=404
            )
        ordered = [by_id[item] for item in ids]
        if len({item.ot_id for item in ordered}) != 1:
            raise ScmServiceError(
                "INVALID_LABEL_BATCH",
                "Las dos mangas deben pertenecer a la misma OT.",
                status_code=422,
            )
        if len({item.trabajo_ot_id for item in ordered}) != 1:
            raise ScmServiceError(
                "MIXED_COLOR_WORK_LABEL_BATCH",
                "Las dos mangas deben pertenecer al mismo trabajo de color.",
                status_code=422,
            )
        labels = []
        for manga in ordered:
            active = next((
                item for item in reversed(manga.etiquetas)
                if item.estado in ("GENERADA", "IMPRESA", "EMISION_INCIERTA")
            ), None)
            if active is not None:
                raise ScmServiceError(
                    "LABEL_ALREADY_EMITTED",
                    "La manga ya posee una etiqueta vigente.",
                    status_code=409,
                    details={"manga_id": str(manga.public_id)},
                )
            version = max(
                (item.version for item in manga.etiquetas), default=0
            ) + 1
            label_id = uuid.uuid4()
            payload = _label_payload(manga, label_id, version)
            labels.append((manga, label_id, version, payload))
        job_hash = _json_hash([item[3] for item in labels])
        job = ScmTrabajoImpresionManga(
            plantilla_version=labels[0][3]["template"]["version"],
            payload_hash=job_hash,
            solicitado_por_id=actor.id,
        )
        session.add(job)
        session.flush()
        models = []
        for manga, label_id, version, payload in labels:
            label = ScmEtiquetaManga(
                public_id=label_id,
                manga_id=manga.id,
                trabajo_impresion_id=job.public_id,
                version=version,
                plantilla_version=payload["template"]["version"],
                payload_json=payload,
                payload_hash=_json_hash(payload),
            )
            manga.estado = "PREETIQUETADA"
            manga.version += 1
            session.add(label)
            models.append(label)
        session.flush()
        response = {
            "print_job_id": str(job.public_id),
            "template": labels[0][3]["template"],
            "labels": [_serialize_label(label) for label in models],
        }
        for label in models:
            session.add(_event(
                "ETIQUETA_MANGA", label.id, "PRE_LABEL_GENERATED",
                actor, operation, _serialize_label(label),
            ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except (ValueError, AttributeError) as error:
        session.rollback()
        raise ScmServiceError(
            "INVALID_UUID", "Una identidad UUID no es valida.", status_code=400
        ) from error
    except Exception:
        session.rollback()
        raise


def annul_manga(
    session, *, actor_id, manga_id, operation_id, data
):
    actor = load_actor(session, actor_id, capability="MANGA_ANULAR")
    endpoint = f"/mangas/{manga_id}/anular"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        manga = session.scalar(
            select(ScmManga)
            .where(ScmManga.public_id == manga_id)
            .with_for_update()
        )
        if manga is None:
            raise ScmServiceError(
                "MANGA_NOT_FOUND", "La manga no existe.", status_code=404
            )
        if manga.estado not in {"PLANIFICADA", "PREETIQUETADA"}:
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "Solo una manga aun no pesada puede anularse por esta accion.",
                status_code=409,
            )
        reason = required_text(
            data.get("motivo"), field="motivo", max_length=500
        )
        before = _serialize_manga(manga)
        returned = _annul_unweighed_manga_model(
            manga, actor=actor, reason=reason
        )
        if manga.trabajo is not None and manga.trabajo.estado == "COMPLETADO":
            manga.trabajo.estado = "PAUSADO"
            manga.trabajo.completada_at = None
            manga.trabajo.pausada_at = utc_now()
            _recompute_parent_state(session, manga.trabajo)
        response = {
            "manga": _serialize_manga(manga),
            "plan": {
                "plan_linea_id": manga.plan_linea_id,
                "trabajo_color_id": (
                    str(manga.trabajo_ot_id) if manga.trabajo_ot_id else None
                ),
                "cantidad_devuelta_un": _compact_number(returned),
            },
        }
        event = _event(
            "MANGA", manga.id, "MANGA_CANCELLED",
            actor, operation, response["manga"],
        )
        event.before_json = before
        event.motivo = reason
        session.add(event)
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def replace_prelabel(
    session, *, actor_id, label_id, operation_id, data
):
    actor = load_actor(
        session,
        actor_id,
        capability="MANGA_ETIQUETA_REEMPLAZAR_APROBAR",
    )
    endpoint = f"/etiquetas/{label_id}/reemplazos"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, data
    )
    if replay is not None:
        return replay
    try:
        old = session.scalar(
            select(ScmEtiquetaManga)
            .where(ScmEtiquetaManga.public_id == label_id)
            .with_for_update()
        )
        if old is None:
            raise ScmServiceError(
                "LABEL_NOT_FOUND", "La etiqueta no existe.", status_code=404
            )
        if old.estado == "INVALIDADA":
            raise ScmServiceError(
                "LABEL_INVALIDATED",
                "La etiqueta ya fue invalidada.",
                status_code=409,
            )
        if old.manga.estado == "ANULADA":
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "Una manga anulada no admite otra etiqueta.",
                status_code=409,
            )
        if old.tipo == "PREPESAJE" and old.manga.tramos_trabajo:
            raise ScmServiceError(
                "OPEN_MANGA_QR_MUST_BE_PRESERVED",
                "Una manga con contenido o continuidad conserva su preetiqueta y QR originales.",
                status_code=409,
            )
        reason = required_text(
            data.get("motivo"), field="motivo", max_length=500
        )
        new_id = uuid.uuid4()
        version = max(
            (
                item.version
                for item in old.manga.etiquetas
                if item.tipo == old.tipo
            ),
            default=0,
        ) + 1
        if old.tipo == "PREPESAJE":
            new_payload = _label_payload(old.manga, new_id, version)
        else:
            new_payload = copy.deepcopy(old.payload_json)
            new_payload["generated_at"] = utc_now().isoformat()
            new_payload["qr"] = {
                **new_payload["qr"],
                "label_id": str(new_id),
                "label_type": old.tipo,
                "label_version": version,
            }
        job = ScmTrabajoImpresionManga(
            payload_hash=_json_hash([new_payload]),
            solicitado_por_id=actor.id,
            plantilla_version=old.plantilla_version,
        )
        session.add(job)
        session.flush()
        replacement = ScmEtiquetaManga(
            public_id=new_id,
            manga_id=old.manga_id,
            trabajo_impresion_id=job.public_id,
            tipo=old.tipo,
            version=version,
            plantilla_version=old.plantilla_version,
            payload_json=new_payload,
            payload_hash=_json_hash(new_payload),
        )
        session.add(replacement)
        session.flush()
        old.estado = "INVALIDADA"
        old.invalidada_por_id = actor.id
        old.invalidada_at = utc_now()
        old.motivo_invalidacion = reason
        old.reemplazada_por_id = replacement.id
        if old.tipo == "POSTPESAJE":
            old.manga.estado = "PESADA"
            old.manga.version += 1
        response = {
            "print_job_id": str(job.public_id),
            "label": _serialize_label(replacement),
            "invalidated_label_id": str(old.public_id),
        }
        session.add(_event(
            "ETIQUETA_MANGA", replacement.id, "LABEL_REPLACED",
            actor, operation, response,
        ))
        _complete_operation(operation, response)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _print_job_status(job):
    return {
        "GENERADO": "PENDING",
        "PARCIAL": "PARTIAL",
        "PROCESADO": "PRINTED",
        "FALLIDO": "FAILED",
    }.get(job.estado, "FAILED")


def _serialize_print_job(job):
    return {
        "print_job_id": str(job.public_id),
        "status": _print_job_status(job),
        "estado": job.estado,
        "station_id": job.station_id,
        "plantilla_version": job.plantilla_version,
        "payload_hash": job.payload_hash,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "processed_at": (
            job.processed_at.isoformat() if job.processed_at else None
        ),
        "labels": [_serialize_label(label) for label in job.etiquetas],
    }


def list_control_print_jobs(session, *, actor_id, filters=None):
    """Read-only central outbox for production supervisors.

    Unlike the station inbox this view never claims a job and does not hide
    jobs already assigned to another station.
    """
    load_actor(session, actor_id, capability="OT_VER")
    filters = filters or {}
    normalized_status = str(filters.get("status") or "PENDING").strip().upper()
    normalized_type = str(filters.get("tipo") or "ALL").strip().upper()
    allowed_statuses = {"PENDING", "PARTIAL", "PRINTED", "FAILED", "ALL"}
    if normalized_status not in allowed_statuses:
        raise ScmServiceError(
            "INVALID_PRINT_JOB_STATUS",
            "El estado de impresion no es valido.",
            status_code=400,
        )
    if normalized_type not in {"PREPESAJE", "POSTPESAJE", "ALL"}:
        raise ScmServiceError(
            "INVALID_PRINT_JOB_TYPE",
            "El tipo de etiqueta no es valido.",
            status_code=400,
        )
    try:
        limit = int(filters.get("limit") or 50)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_LIMIT", "El limite debe ser numerico.", status_code=400,
        ) from error
    if not 1 <= limit <= 100:
        raise ScmServiceError(
            "INVALID_LIMIT", "El limite debe estar entre 1 y 100.", status_code=400,
        )

    query = (
        select(ScmTrabajoImpresionManga)
        .options(
            selectinload(ScmTrabajoImpresionManga.etiquetas)
            .selectinload(ScmEtiquetaManga.manga)
        )
        .order_by(
            ScmTrabajoImpresionManga.created_at.desc(),
            ScmTrabajoImpresionManga.public_id.desc(),
        )
        .limit(limit)
    )
    if normalized_status == "PENDING":
        query = query.where(
            ScmTrabajoImpresionManga.estado.in_(("GENERADO", "PARCIAL", "FALLIDO"))
        )
    elif normalized_status != "ALL":
        query = query.where(
            ScmTrabajoImpresionManga.estado
            == {"PARTIAL": "PARCIAL", "PRINTED": "PROCESADO", "FAILED": "FALLIDO"}[
                normalized_status
            ]
        )
    if normalized_type != "ALL":
        query = query.where(
            ScmTrabajoImpresionManga.etiquetas.any(
                ScmEtiquetaManga.tipo == normalized_type
            )
        )
    search = str(filters.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.where(
            or_(
                ScmTrabajoImpresionManga.etiquetas.any(
                    ScmEtiquetaManga.manga.has(ScmManga.codigo.ilike(like))
                ),
                ScmTrabajoImpresionManga.etiquetas.any(
                    ScmEtiquetaManga.public_id.cast(String).ilike(like)
                ),
            )
        )
    jobs = session.scalars(query).all()
    items = [_serialize_print_job(job) for job in jobs]
    return {
        "items": items,
        "count": len(items),
        "as_of": utc_now().isoformat(),
    }

def list_station_print_jobs(
    session, *, station_id, status="PENDING", limit=20
):
    """List PREPESAJE jobs visible to one station without claiming them.

    ``PENDING`` is an actionable inbox: it includes never-printed, partial and
    failed jobs while excluding fully printed jobs. Exact status filters remain
    available for diagnostics and history.
    """
    normalized_status = str(status or "PENDING").strip().upper()
    allowed_statuses = {"PENDING", "PARTIAL", "PRINTED", "FAILED", "ALL"}
    if normalized_status not in allowed_statuses:
        raise ScmServiceError(
            "INVALID_PRINT_JOB_STATUS",
            "El estado de bandeja de impresion no es valido.",
            status_code=400,
        )
    try:
        effective_limit = int(limit)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_LIMIT",


            "El limite de la bandeja debe ser numerico.",
            status_code=400,
        ) from error
    if not 1 <= effective_limit <= 100:
        raise ScmServiceError(
            "INVALID_LIMIT",
            "El limite de la bandeja debe estar entre 1 y 100.",
            status_code=400,
        )

    query = (
        select(ScmTrabajoImpresionManga)
        .where(
            ScmTrabajoImpresionManga.etiquetas.any(
                ScmEtiquetaManga.tipo == "PREPESAJE"
            ),
            or_(
                ScmTrabajoImpresionManga.station_id.is_(None),
                ScmTrabajoImpresionManga.station_id == station_id,
            ),
        )
        .options(
            selectinload(
                ScmTrabajoImpresionManga.etiquetas
            ).selectinload(ScmEtiquetaManga.manga)
        )
        .order_by(
            ScmTrabajoImpresionManga.created_at.desc(),
            ScmTrabajoImpresionManga.public_id.desc(),
        )
        .limit(effective_limit)
    )
    if normalized_status == "PENDING":
        query = query.where(
            ScmTrabajoImpresionManga.estado.in_(
                ("GENERADO", "PARCIAL", "FALLIDO")
            )
        )
    elif normalized_status != "ALL":
        query = query.where(
            ScmTrabajoImpresionManga.estado
            == {
                "PARTIAL": "PARCIAL",
                "PRINTED": "PROCESADO",
                "FAILED": "FALLIDO",
            }[normalized_status]
        )
    jobs = session.scalars(query).all()
    serialized = [_serialize_print_job(job) for job in jobs]
    return {"print_jobs": serialized, "count": len(serialized)}


def get_station_print_job(session, *, station_id, print_job_id):
    """Return preview data. This read must never reserve the print job."""
    job = session.scalar(
        select(ScmTrabajoImpresionManga)
        .where(ScmTrabajoImpresionManga.public_id == print_job_id)
        .options(
            selectinload(
                ScmTrabajoImpresionManga.etiquetas
            ).selectinload(ScmEtiquetaManga.manga)
        )
    )
    if job is None:
        raise ScmServiceError(
            "PRINT_JOB_NOT_FOUND",
            "El trabajo de impresion no existe.",
            status_code=404,
        )
    if job.station_id is not None and job.station_id != station_id:
        raise ScmServiceError(
            "PRINT_JOB_STATION_MISMATCH",
            "El trabajo pertenece a otra estacion.",
            status_code=403,
        )
    return _serialize_print_job(job)


def claim_station_print_job(session, *, station_id, print_job_id):
    """Atomically reserve a job immediately before physical printing.

    A replay from the owning station returns the same immutable job until its
    result is acknowledged. That replay is transport idempotency, not reprint
    authorization: the station must persist its per-label emission attempt
    before talking to the spooler and retry only the acknowledgement after an
    emitted or uncertain result. A physical retry is valid solely after the
    station has durably classified the previous attempt FALLIDA_SIN_EMISION.
    """
    job = session.scalar(
        select(ScmTrabajoImpresionManga)
        .where(ScmTrabajoImpresionManga.public_id == print_job_id)
        .with_for_update()
    )
    if job is None:
        raise ScmServiceError(
            "PRINT_JOB_NOT_FOUND",
            "El trabajo de impresion no existe.",
            status_code=404,
        )
    if job.station_id not in (None, station_id):
        raise ScmServiceError(
            "PRINT_JOB_ALREADY_CLAIMED",
            "El trabajo ya fue reservado por otra estacion.",
            status_code=409,
        )
    if job.estado == "PROCESADO":
        raise ScmServiceError(
            "PRINT_JOB_ALREADY_PROCESSED",
            "El trabajo ya fue impreso completamente.",
            status_code=409,
        )
    if job.station_id is None:
        job.station_id = station_id
        session.commit()
    return _serialize_print_job(job)


def acknowledge_station_print_job(
    session, *, station_id, print_job_id, data
):
    job = session.scalar(
        select(ScmTrabajoImpresionManga)
        .where(ScmTrabajoImpresionManga.public_id == print_job_id)
        .with_for_update()
    )
    if job is None:
        raise ScmServiceError(
            "PRINT_JOB_NOT_FOUND",
            "El trabajo de impresion no existe.",
            status_code=404,
        )
    if job.station_id not in (None, station_id):
        raise ScmServiceError(
            "PRINT_JOB_STATION_MISMATCH",
            "El trabajo pertenece a otra estacion.",
            status_code=403,
        )
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise ScmServiceError(
            "REQUIRED_FIELD", "Se requieren resultados por etiqueta.",
            status_code=400,
        )
    labels = {str(label.public_id): label for label in job.etiquetas}
    allowed = {
        "IMPRESA", "FALLIDA_SIN_EMISION", "EMISION_INCIERTA"
    }
    for result in results:
        label = labels.get(str(result.get("label_id")))
        state = str(result.get("estado", "")).upper()
        if label is None or state not in allowed:
            raise ScmServiceError(
                "INVALID_PRINT_RESULT",
                "El resultado no pertenece al trabajo o es invalido.",
                status_code=422,
            )
        if label.estado in {"IMPRESA", "EMISION_INCIERTA"}:
            if label.estado != state:
                raise ScmServiceError(
                    "IDEMPOTENCY_CONFLICT",
                    "La etiqueta ya tiene otro resultado.",
                    status_code=409,
                )
            continue
        # FALLIDA_SIN_EMISION confirms that no physical label was emitted.
        # Therefore the same immutable job may be attempted again. Its local
        # station log remains append-only while the central state advances.
        label.estado = state
        label.station_id = station_id
        label.printer_name = str(result.get("printer_name") or "")[:160] or None
        label.error_tecnico = str(result.get("error") or "")[:500] or None
        if state == "IMPRESA":
            label.printed_at = utc_now()
            if label.tipo == "POSTPESAJE":
                label.manga.estado = "PENDIENTE_RECEPCION_ALMACEN"
                label.manga.version += 1
        else:
            label.printed_at = None
    states = {label.estado for label in job.etiquetas}
    job.station_id = station_id
    job.processed_at = utc_now()
    if states == {"IMPRESA"}:
        job.estado = "PROCESADO"
    elif "IMPRESA" in states:
        job.estado = "PARCIAL"
    else:
        job.estado = "FALLIDO"
    session.commit()
    return {
        "print_job_id": str(job.public_id),
        "estado": job.estado,
        "labels": [_serialize_label(label) for label in job.etiquetas],
    }
