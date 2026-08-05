"""Application services for the TS-010C OP -> OT -> manga slice."""

import copy
import hashlib
import json
import math
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from app.models.lote import LoteSalidaPiezaColor
from app.models.maquina import Maquina
from app.models.orden import OrdenProduccion
from app.models.registro import RegistroDiarioProduccion
from app.models.producto import ColorProduccion
from app.models.scm_articulos import ScmArticuloPiezaColor
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_empaque import (
    ScmArticuloPerfil,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_ot import (
    ScmAsignacionPlanMangaOt,
    ScmEtiquetaManga,
    ScmLoteArticulo,
    ScmManga,
    ScmPlanMangaOp,
    ScmPlanMangaOpLinea,
    ScmSolicitudMangaExtra,
    ScmTrabajoImpresionManga,
    utc_now,
)
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenOperacion,
)
from app.models.trabajador import Trabajador
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_packaging_service import calculate_packaging_capacity
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
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


def _reserve_operation(session, operation_id, endpoint, actor, data):
    request_hash = _operation_hash(endpoint, actor.id, data)
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
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
    operation = ScmOperacion(
        operation_id=operation_id,
        endpoint=endpoint,
        actor_id=actor.id,
        request_sha256=request_hash,
    )
    session.add(operation)
    session.flush()
    return operation, None


def _complete_operation(operation, response, status=201):
    operation.response_json = copy.deepcopy(response)
    operation.estado_http = status


def _event(aggregate_type, aggregate_id, event_type, actor, operation, after):
    return ScmEvento(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
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


def _serialize_manga(manga):
    current_label = next(
        (
            label for label in reversed(manga.etiquetas)
            if label.estado != "INVALIDADA"
        ),
        None,
    )
    return {
        "id": manga.id,
        "public_id": str(manga.public_id),
        "codigo": manga.codigo,
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
        "motivo_extra": manga.motivo_extra,
        "version": manga.version,
        "etiqueta_vigente": _serialize_label(current_label)
        if current_label else None,
    }


def _serialize_ot(ot):
    mangas = ScmManga.query.filter_by(ot_id=ot.id).order_by(
        ScmManga.secuencia_ot
    ).all()
    payload = ot.to_dict()
    payload["orden_id"] = ot.orden_id
    payload["mangas"] = [_serialize_manga(manga) for manga in mangas]
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


def _approved_manga_rule(session, article_id):
    profiles = session.scalars(
        select(ScmArticuloPerfil).where(
            ScmArticuloPerfil.articulo_id == article_id,
            ScmArticuloPerfil.activo.is_(True),
            ScmArticuloPerfil.es_predeterminado.is_(True),
        )
    ).all()
    if len(profiles) != 1:
        raise ScmServiceError(
            "PACKAGING_RULE_MISSING",
            "La salida requiere exactamente un perfil de empaque predeterminado.",
            status_code=422,
            details={"articulo_id": article_id},
        )
    rules = session.scalars(
        select(ScmReglaEmpaqueRevision)
        .join(ScmReglaEmpaque)
        .join(ScmTipoContenedor)
        .where(
            ScmReglaEmpaque.perfil_empacable_id
            == profiles[0].perfil_empacable_id,
            ScmTipoContenedor.clase == "MANGA",
            ScmTipoContenedor.activo.is_(True),
            ScmReglaEmpaqueRevision.estado == "APROBADA",
        )
    ).all()
    if len(rules) != 1:
        raise ScmServiceError(
            "PACKAGING_RULE_MISSING",
            "La salida requiere exactamente una regla MANGA aprobada.",
            status_code=422,
            details={
                "articulo_id": article_id,
                "approved_rules": len(rules),
            },
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


def _create_mangas(session, *, ot, line, assignment, quantity, actor, kind,
                   reason=None, requester_id=None, approver_id=None):
    capacity = Decimal(line.capacidad_efectiva_un)
    remaining = Decimal(quantity)
    created = []
    while remaining > 0:
        amount = min(remaining, capacity)
        sequence = ot.secuencia_siguiente_manga
        ot.secuencia_siguiente_manga += 1
        order_code = (
            ot.orden_operacion.codigo
            if ot.orden_operacion is not None
            else ot.orden_id
        )
        manga = ScmManga(
            codigo=_manga_code(order_code, ot.codigo_ot, sequence),
            ot_id=ot.id,
            plan_linea_id=line.id,
            asignacion_id=assignment.id if assignment else None,
            lote_articulo_id=line.lote_articulo_id,
            secuencia_ot=sequence,
            tipo=kind,
            cantidad_planificada_un=amount,
            cantidad_asignada_un=amount,
            maquinista_previsto_id=(
                ot.maquinista_previsto_id or ot.responsable_id
            ),
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
        machine = session.get(Maquina, machine_id)
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
        machine = session.get(Maquina, machine_id)
        worker = session.get(Trabajador, data.get("maquinista_id"))
        if machine is None or not machine.activo:
            raise ScmServiceError(
                "INVALID_MACHINE",
                "La maquina no esta activa.",
                status_code=422,
            )
        if worker is None or not worker.activo:
            raise ScmServiceError(
                "INVALID_WORKER",
                "El maquinista no esta activo.",
                status_code=422,
            )
        allocations = data.get("asignaciones")
        if not isinstance(allocations, list) or not allocations:
            raise ScmServiceError(
                "REQUIRED_FIELD",
                "Se requiere al menos una asignacion del plan.",
                status_code=400,
            )
        net_weight = sum(
            (
                Decimal(output.cantidad_por_ciclo_snapshot or 0)
                * Decimal(output.peso_unitario_snapshot_g or 0)
                for output in run.salidas
            ),
            Decimal("0"),
        )
        cavities = sum(
            (
                Decimal(output.cantidad_por_ciclo_snapshot or 0)
                for output in run.salidas
            ),
            Decimal("0"),
        )
        ot = RegistroDiarioProduccion(
            public_id=uuid.uuid4(),
            codigo_ot=generar_codigo_catalogo(
                "ORDEN_TRABAJO",
                session=session,
            ),
            codigo_ot_sintetico=False,
            estado="PLANIFICADA",
            orden_id=None,
            orden_operacion_id=order.id,
            corrida_fabricacion_id=run.id,
            maquina_id=machine.id,
            fecha=_parse_date(data.get("fecha_operativa")),
            turno=required_text(
                data.get("turno"),
                field="turno",
                max_length=20,
            ).upper(),
            created_by_id=actor.id,
            maquinista_previsto_id=worker.id,
            maquina_codigo_snapshot=machine.codigo,
            maquina_nombre_snapshot=machine.nombre,
            snapshot_cavidades=int(cavities),
            snapshot_peso_neto_gr=float(net_weight),
            snapshot_peso_colada_gr=float(
                order.fabricacion.snapshot_peso_colada_gr or 0
            ),
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
            if (
                line is None
                or line.salida_canonica is None
                or line.salida_canonica.corrida_fabricacion_id != run.id
            ):
                raise ScmServiceError(
                    "PLAN_LINE_NOT_FOUND",
                    "La linea no pertenece a la corrida seleccionada.",
                    status_code=404,
                    details={"index": index},
                )
            quantity = _decimal(
                raw.get("cantidad_un"),
                "cantidad_un",
                integral=True,
            )
            already = session.scalar(
                select(func.coalesce(
                    func.sum(
                        ScmAsignacionPlanMangaOt.cantidad_asignada_un
                    ),
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
        if order.estado == "LIBERADA":
            order.estado = "PROGRAMADA"
            order.version += 1
        session.flush()
        response = {"ot": _serialize_ot(ot)}
        session.add(_event(
            "ORDEN_TRABAJO",
            ot.id,
            "OT_CREATED_FROM_OF",
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
):
    load_actor(session, actor_id, capability="OT_VER")
    statement = select(RegistroDiarioProduccion).where(
        RegistroDiarioProduccion.codigo_ot_sintetico.is_(False)
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
            RegistroDiarioProduccion.orden_operacion_id == canonical_id
        )
    items = session.scalars(
        statement.order_by(
            RegistroDiarioProduccion.fecha.desc(),
            RegistroDiarioProduccion.id.desc(),
        )
    ).all()
    return {"items": [_serialize_ot(item) for item in items]}


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
        version = expected_version(data.get("version"))
        if ot.version != version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OT fue modificada por otra operacion.",
                status_code=409,
            )
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
        line = session.get(ScmPlanMangaOpLinea, data.get("plan_linea_id"))
        if line is None or not _plan_line_belongs_to_ot(line, ot):
            raise ScmServiceError(
                "PLAN_LINE_NOT_FOUND",
                "La linea no corresponde a la orden y corrida de la OT.",
                status_code=404,
            )
        request_item = ScmSolicitudMangaExtra(
            ot_id=ot.id,
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
        line = session.scalar(
            select(ScmPlanMangaOpLinea)
            .join(ScmPlanMangaOp)
            .where(
                ScmPlanMangaOpLinea.id == data.get("plan_linea_id"),
                ScmPlanMangaOp.estado == "ACTIVO",
            )
            .with_for_update()
        )
        if line is None or not _plan_line_belongs_to_ot(line, ot):
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
        ))
        new_count = int(math.ceil(
            quantity / Decimal(line.capacidad_efectiva_un)
        ))
        if assignment is None:
            assignment = ScmAsignacionPlanMangaOt(
                plan_linea_id=line.id,
                ot_id=ot.id,
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
            session, ot=ot, line=line, assignment=assignment,
            quantity=quantity, actor=actor, kind="NORMAL",
        )
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
            RegistroDiarioProduccion.orden_operacion_id == canonical_id
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
    canonical = manga.ot.orden_operacion_id is not None
    order_code = (
        manga.ot.orden_operacion.codigo
        if canonical
        else manga.ot.orden_id
    )
    template_version = (
        "PREPESAJE_TSPL_2" if canonical else "PREPESAJE_TSPL_1"
    )
    qr = {
        "v": 1,
        "type": "SCM_MANGA_LABEL",
        "manga_id": str(manga.public_id),
        "label_id": str(label_id),
        "label_type": "PREPESAJE",
        "label_version": version,
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
        "maquinista": manga.maquinista_previsto.nombre_completo,
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
                    "oe_ot"
                    if manga.ot.tipo_ot == "ENSAMBLE"
                    else "of_ot"
                ): f"{order_code} - {manga.ot.codigo_ot}"
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
        if manga.estado in ("PESADA", "ETIQUETADA_FINAL", "ANULADA"):
            raise ScmServiceError(
                "INVALID_STATE_TRANSITION",
                "La manga ya no puede anularse.",
                status_code=409,
            )
        reason = required_text(
            data.get("motivo"), field="motivo", max_length=500
        )
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
                label.motivo_invalidacion = (
                    f"Manga anulada: {reason}"
                )
        response = {"manga": _serialize_manga(manga)}
        session.add(_event(
            "MANGA", manga.id, "MANGA_CANCELLED",
            actor, operation, response["manga"],
        ))
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


def get_station_print_job(session, *, station_id, print_job_id):
    job = session.get(ScmTrabajoImpresionManga, print_job_id)
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
    if job.station_id is None:
        job.station_id = station_id
        session.commit()
    labels = [_serialize_label(label) for label in job.etiquetas]
    return {
        "print_job_id": str(job.public_id),
        "estado": job.estado,
        "plantilla_version": job.plantilla_version,
        "payload_hash": job.payload_hash,
        "labels": labels,
    }


def acknowledge_station_print_job(
    session, *, station_id, print_job_id, data
):
    job = session.get(ScmTrabajoImpresionManga, print_job_id)
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
