"""Recepción QR de mangas, custodia, Calidad y nacimiento de Kardex."""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_inventory import (
    ScmMovimientoInventario,
    ScmSaldoInventario,
    ScmUbicacionInventario,
)
from app.models.scm_ot import ScmEtiquetaManga, ScmManga, ScmPesajeManga
from app.models.scm_warehouse import (
    ScmExistenciaManga,
    ScmRechazoRecepcionManga,
    ScmSesionRecepcionManga,
    ScmReversionRecepcionManga,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    load_actor,
    reject_unknown_fields,
    required_text,
)
from app.services.scm_weighing_service import _effective_projection


QUANTUM = Decimal("0.001")


def utc_now():
    return datetime.now(timezone.utc)


def _uuid_value(value, *, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "UUID_INVALID",
            f"El campo {field} debe contener un UUID valido.",
            status_code=400,
            details={"field": field},
        ) from error


def _hash(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _reserve_operation(session, operation_id, endpoint, actor, data):
    request_hash = _hash({
        "endpoint": endpoint,
        "actor_id": actor.id,
        "data": data,
    })
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        if existing.endpoint != endpoint or existing.request_sha256 != request_hash:
            raise ScmServiceError(
                "IDEMPOTENCY_CONFLICT",
                "La clave idempotente ya fue usada con otra solicitud.",
                status_code=409,
            )
        if existing.response_json is None:
            raise ScmServiceError(
                "IDEMPOTENCY_OPERATION_INCOMPLETE",
                "La operación anterior aún no tiene resultado.",
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


def _complete(operation, response, status=200):
    operation.response_json = copy.deepcopy(response)
    operation.estado_http = status


def _active_final_label(session, manga_id):
    return session.scalar(
        select(ScmEtiquetaManga)
        .where(
            ScmEtiquetaManga.manga_id == manga_id,
            ScmEtiquetaManga.tipo == "POSTPESAJE",
            ScmEtiquetaManga.estado == "IMPRESA",
        )
        .order_by(ScmEtiquetaManga.version.desc())
        .limit(1)
    )


def _resolve_label(session, label_id):
    label = session.scalar(
        select(ScmEtiquetaManga).where(ScmEtiquetaManga.public_id == label_id)
    )
    if label is None:
        raise ScmServiceError(
            "MANGA_NO_ENCONTRADA", "El QR no corresponde a una etiqueta SCM.", status_code=404
        )
    if label.estado == "INVALIDADA":
        raise ScmServiceError(
            "ETIQUETA_INVALIDADA",
            "La etiqueta fue reemplazada. Escanea la versión vigente.",
            status_code=409,
        )
    if label.estado != "IMPRESA":
        raise ScmServiceError(
            "ETIQUETA_NO_IMPRESA",
            "La etiqueta todavía no fue confirmada como impresa.",
            status_code=409,
        )
    if label.tipo == "POSTPESAJE":
        return label.manga, label, "QR_FINAL"
    final_label = _active_final_label(session, label.manga_id)
    if final_label is None:
        raise ScmServiceError(
            "PESAJE_FINAL_REQUERIDO",
            "La preetiqueta identifica la manga, pero falta su etiqueta final impresa.",
            status_code=409,
        )
    return label.manga, label, "QR_PREETIQUETA"


def _resolve_code(session, code):
    manga = session.scalar(select(ScmManga).where(ScmManga.codigo == code))
    if manga is None:
        raise ScmServiceError(
            "MANGA_NO_ENCONTRADA", "No existe una manga con ese código.", status_code=404
        )
    final_label = _active_final_label(session, manga.id)
    if final_label is None:
        raise ScmServiceError(
            "PESAJE_FINAL_REQUERIDO",
            "La manga no posee una etiqueta final vigente e impresa.",
            status_code=409,
        )
    return manga, final_label, "CODIGO_MANUAL"


def _candidate_payload(session, manga, label, resolution):
    weighing = session.scalar(
        select(ScmPesajeManga).where(ScmPesajeManga.manga_id == manga.id)
    )
    if weighing is None:
        raise ScmServiceError(
            "PESAJE_FINAL_REQUERIDO",
            "La manga todavía no posee un pesaje final confirmado.",
            status_code=409,
        )
    if manga.trabajo is not None and (
        manga.asignacion_personal_trabajo_id is None
        or weighing.asignacion_personal_trabajo_id
        != manga.asignacion_personal_trabajo_id
    ):
        raise ScmServiceError(
            "ASSIGNMENT_WORK_MISMATCH",
            "El pesaje no conserva la asignacion del trabajo de la manga.",
            status_code=409,
        )
    if manga.estado == "ANULADA":
        raise ScmServiceError(
            "MANGA_ANULADA", "La manga fue anulada y no puede recibirse.", status_code=409
        )
    existing = session.scalar(
        select(ScmExistenciaManga).where(ScmExistenciaManga.manga_id == manga.id)
    )
    if existing is not None:
        raise ScmServiceError(
            "MANGA_YA_RECIBIDA",
            "La manga ya fue aceptada por Almacén.",
            status_code=409,
            details={"existencia": existing.to_dict()},
        )
    if manga.estado != "PENDIENTE_RECEPCION_ALMACEN":
        raise ScmServiceError(
            "MANGA_NO_RECIBIBLE",
            "La manga todavía no está lista para recepción de Almacén.",
            status_code=409,
            details={"estado": manga.estado},
        )
    projection = _effective_projection(weighing)
    article = manga.lote_articulo.articulo
    return {
        "manga_id": str(manga.public_id),
        "manga_codigo": manga.codigo,
        "estado": manga.estado,
        "resuelta_por": resolution,
        "etiqueta_id": str(label.public_id),
        "etiqueta_tipo": label.tipo,
        "articulo": article.to_dict(),
        "cantidad_confirmada": projection["cantidad_confirmada"],
        "peso_bruto_kg": projection["peso_bruto_kg"],
        "tara_kg": projection["tara_kg"],
        "peso_neto_kg": projection["peso_fisico_neto_kg"],
        "pesada_at": projection["pesada_at"],
        "ot": {
            "id": str(manga.ot.public_id),
            "codigo": manga.ot.codigo_ot,
            "fecha_operativa": manga.ot.fecha.isoformat(),
            "turno": manga.ot.turno,
        },
        "trabajo_color": (
            {
                "id": str(manga.trabajo.id),
                "codigo": manga.trabajo.codigo,
                "estado": manga.trabajo.estado,
                "orden_fabricacion_id": str(
                    manga.trabajo.orden_operacion_id
                ),
                "orden_fabricacion_codigo": (
                    manga.trabajo.orden_operacion.codigo
                ),
                "corrida_fabricacion_id": str(
                    manga.trabajo.trabajo_color.corrida_fabricacion_id
                ),
            }
            if manga.trabajo is not None else None
        ),
        "asignacion_personal_trabajo_id": (
            str(manga.asignacion_personal_trabajo_id)
            if manga.asignacion_personal_trabajo_id else None
        ),
        "color": manga.color_snapshot,
    }


def list_warehouse_receiving(session, *, actor_id):
    load_actor(session, actor_id, capability="RECEPCION_MANGA_VER")
    pending = session.scalars(
        select(ScmManga)
        .where(ScmManga.estado == "PENDIENTE_RECEPCION_ALMACEN")
        .order_by(ScmManga.id)
    ).all()
    pending_items = []
    for manga in pending:
        label = _active_final_label(session, manga.id)
        if label is not None:
            pending_items.append(_candidate_payload(session, manga, label, "QR_FINAL"))
    existences = session.scalars(
        select(ScmExistenciaManga).order_by(ScmExistenciaManga.recibida_at.desc())
    ).all()
    rejections = session.scalars(
        select(ScmRechazoRecepcionManga)
        .order_by(ScmRechazoRecepcionManga.created_at.desc())
        .limit(100)
    ).all()
    reversals = session.scalars(
        select(ScmReversionRecepcionManga)
        .order_by(ScmReversionRecepcionManga.solicitada_at.desc())
        .limit(100)
    ).all()
    locations = session.scalars(
        select(ScmUbicacionInventario)
        .where(ScmUbicacionInventario.activo.is_(True))
        .order_by(ScmUbicacionInventario.codigo)
    ).all()
    return {
        "pendientes": pending_items,
        "existencias": [item.to_dict() for item in existences],
        "rechazos": [item.to_dict() for item in rejections],
        "reversiones": [item.to_dict() for item in reversals],
        "ubicaciones": [item.to_dict() for item in locations],
    }


def resolve_receiving_label(session, *, actor_id, label_id):
    load_actor(session, actor_id, capability="RECEPCION_MANGA_VER")
    manga, label, resolution = _resolve_label(session, label_id)
    return _candidate_payload(session, manga, label, resolution)


def resolve_receiving_code(session, *, actor_id, code):
    load_actor(session, actor_id, capability="RECEPCION_MANGA_BUSCAR_MANUAL")
    normalized = required_text(code, field="codigo", max_length=80).upper()
    manga, label, resolution = _resolve_code(session, normalized)
    return _candidate_payload(session, manga, label, resolution)


def create_receiving_session(session, *, actor_id, operation_id, data):
    reject_unknown_fields(data, allowed={"punto_ingreso"})
    actor = load_actor(session, actor_id, capability="RECEPCION_MANGA_CONFIRMAR")
    point = required_text(data.get("punto_ingreso"), field="punto_ingreso", max_length=80).upper()
    command = {"punto_ingreso": point}
    operation, replay = _reserve_operation(
        session, operation_id, "POST /recepcion-mangas/sesiones", actor, command
    )
    if replay is not None:
        return replay
    try:
        identifier = uuid.uuid4()
        receipt_session = ScmSesionRecepcionManga(
            id=identifier,
            codigo=f"RCP-{utc_now():%Y%m%d}-{str(identifier)[:8].upper()}",
            punto_ingreso=point,
            actor_id=actor.id,
        )
        session.add(receipt_session)
        session.flush()
        response = {"sesion": receipt_session.to_dict()}
        _complete(operation, response, 201)
        session.add(ScmEvento(
            aggregate_type="SESION_RECEPCION_MANGA",
            aggregate_id=str(receipt_session.id),
            tipo="WAREHOUSE_RECEIVING_SESSION_OPENED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def close_receiving_session(session, *, actor_id, session_id, operation_id):
    actor = load_actor(session, actor_id, capability="RECEPCION_MANGA_CONFIRMAR")
    command = {"sesion_id": str(session_id)}
    endpoint = f"POST /recepcion-mangas/sesiones/{session_id}/cerrar"
    operation, replay = _reserve_operation(
        session, operation_id, endpoint, actor, command
    )
    if replay is not None:
        return replay
    try:
        receipt_session = session.scalar(
            select(ScmSesionRecepcionManga)
            .where(ScmSesionRecepcionManga.id == session_id)
            .with_for_update()
        )
        if receipt_session is None:
            raise ScmServiceError(
                "SESION_RECEPCION_NO_ENCONTRADA",
                "La sesion de recepcion no existe.",
                status_code=404,
            )
        if receipt_session.actor_id != actor.id:
            raise ScmServiceError(
                "SESION_RECEPCION_OTRO_ACTOR",
                "La sesion pertenece a otro almacenero.",
                status_code=409,
            )
        if receipt_session.estado != "ABIERTA":
            raise ScmServiceError(
                "SESION_RECEPCION_CERRADA",
                "La sesion ya fue cerrada.",
                status_code=409,
            )
        receipt_session.estado = "CERRADA"
        receipt_session.cerrada_at = utc_now()
        session.flush()
        response = {"sesion": receipt_session.to_dict()}
        _complete(operation, response)
        session.add(ScmEvento(
            aggregate_type="SESION_RECEPCION_MANGA",
            aggregate_id=str(receipt_session.id),
            tipo="WAREHOUSE_RECEIVING_SESSION_CLOSED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _validate_physical_checks(data):
    checks = {
        "presencia_confirmada": data.get("presencia_confirmada"),
        "bolsa_cerrada": data.get("bolsa_cerrada"),
        "coincidencia_etiquetas": data.get("coincidencia_etiquetas"),
    }
    if any(value is not True for value in checks.values()):
        raise ScmServiceError(
            "VERIFICACION_FISICA_INCOMPLETA",
            "Confirma presencia, bolsa cerrada y coincidencia de etiquetas.",
            status_code=422,
            details={"checks": checks},
        )


def receive_manga(session, *, actor_id, operation_id, data):
    reject_unknown_fields(data, allowed={
        "label_id", "manga_codigo", "sesion_id", "ubicacion_codigo",
        "presencia_confirmada", "bolsa_cerrada", "coincidencia_etiquetas",
    })
    actor = load_actor(session, actor_id, capability="RECEPCION_MANGA_CONFIRMAR")
    _validate_physical_checks(data)
    if data.get("label_id"):
        manga, label, resolution = _resolve_label(
            session, _uuid_value(data["label_id"], field="label_id")
        )
    elif data.get("manga_codigo"):
        load_actor(session, actor_id, capability="RECEPCION_MANGA_BUSCAR_MANUAL")
        manga, label, resolution = _resolve_code(
            session, required_text(data["manga_codigo"], field="manga_codigo", max_length=80).upper()
        )
    else:
        raise ScmServiceError(
            "MANGA_IDENTITY_REQUIRED", "Escanea una etiqueta o indica un código autorizado.", status_code=422
        )
    location_code = required_text(
        data.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40
    ).upper()
    command = {
        "label_id": str(label.public_id),
        "manga_id": str(manga.public_id),
        "sesion_id": str(data.get("sesion_id")) if data.get("sesion_id") else None,
        "ubicacion_codigo": location_code,
        "resuelta_por": resolution,
        "presencia_confirmada": True,
        "bolsa_cerrada": True,
        "coincidencia_etiquetas": True,
    }
    endpoint = f"POST /recepcion-mangas/{manga.public_id}/confirmar"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        manga = session.scalar(
            select(ScmManga).where(ScmManga.id == manga.id).with_for_update()
        )
        candidate = _candidate_payload(session, manga, label, resolution)
        article = manga.lote_articulo.articulo
        location = session.scalar(
            select(ScmUbicacionInventario)
            .where(ScmUbicacionInventario.codigo == location_code)
            .with_for_update()
        )
        if location is None or not location.activo:
            raise ScmServiceError(
                "UBICACION_INCOMPATIBLE", "La ubicación no existe o está inactiva.", status_code=422
            )
        allowed_classes = set(location.clases_articulo_json or [])
        if allowed_classes and article.clase not in allowed_classes:
            raise ScmServiceError(
                "UBICACION_INCOMPATIBLE",
                "La ubicación no admite esta clase de artículo.",
                status_code=422,
                details={"clase": article.clase, "permitidas": sorted(allowed_classes)},
            )
        receipt_session = None
        if data.get("sesion_id"):
            receipt_session = session.get(
                ScmSesionRecepcionManga,
                _uuid_value(data["sesion_id"], field="sesion_id"),
            )
            if receipt_session is None or receipt_session.estado != "ABIERTA":
                raise ScmServiceError(
                    "SESION_RECEPCION_INVALIDA", "La sesión no existe o ya fue cerrada.", status_code=409
                )
            if receipt_session.actor_id != actor.id:
                raise ScmServiceError(
                    "SESION_RECEPCION_OTRO_ACTOR", "La sesión pertenece a otro almacenero.", status_code=409
                )
        quantity = Decimal(candidate["cantidad_confirmada"]).quantize(QUANTUM)
        net = Decimal(candidate["peso_neto_kg"]).quantize(QUANTUM)
        balance = session.scalar(
            select(ScmSaldoInventario)
            .where(
                ScmSaldoInventario.articulo_scm_id == article.id,
                ScmSaldoInventario.ubicacion_id == location.id,
            )
            .with_for_update()
        )
        if balance is None:
            balance = ScmSaldoInventario(
                articulo_scm_id=article.id,
                ubicacion_id=location.id,
            )
            session.add(balance)
            session.flush()
        resulting = Decimal(balance.cantidad_fisica) + quantity
        balance.cantidad_fisica = resulting
        balance.cantidad_no_disponible = Decimal(balance.cantidad_no_disponible) + quantity
        balance.version += 1
        movement = ScmMovimientoInventario(
            saldo=balance,
            tipo="INGRESO_PRODUCCION",
            cantidad_delta=quantity,
            saldo_fisico_resultante=resulting,
            motivo=f"Recepción física de manga {manga.codigo}",
            referencia_tipo="MANGA",
            referencia_id=str(manga.public_id),
            actor_id=actor.id,
            operation_id=operation.operation_id,
        )
        session.add(movement)
        session.flush()
        existence = ScmExistenciaManga(
            manga_id=manga.id,
            sesion_id=receipt_session.id if receipt_session else None,
            etiqueta_resuelta_id=label.id,
            articulo_scm_id=article.id,
            saldo_id=balance.id,
            ubicacion_id=location.id,
            movimiento_ingreso_id=movement.id,
            operation_id=operation.operation_id,
            resuelta_por=resolution,
            cantidad_fisica=quantity,
            peso_neto_snapshot_kg=net,
            recibida_por_id=actor.id,
        )
        session.add(existence)
        manga.estado = "RECIBIDA"
        manga.version += 1
        session.flush()
        response = {
            "existencia": existence.to_dict(),
            "movimiento_id": str(movement.id),
            "idempotent_replay": False,
        }
        _complete(operation, response, 201)
        session.add(ScmEvento(
            aggregate_type="MANGA",
            aggregate_id=str(manga.public_id),
            tipo="MANGA_RECEIVED_IN_WAREHOUSE",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            before_json=candidate,
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def reject_manga_receiving(session, *, actor_id, operation_id, data):
    reject_unknown_fields(data, allowed={"label_id", "manga_codigo", "motivo", "evidencia"})
    actor = load_actor(session, actor_id, capability="RECEPCION_MANGA_RECHAZAR")
    if data.get("label_id"):
        manga, label, _ = _resolve_label(
            session, _uuid_value(data["label_id"], field="label_id")
        )
    elif data.get("manga_codigo"):
        manga, label, _ = _resolve_code(
            session, required_text(data["manga_codigo"], field="manga_codigo", max_length=80).upper()
        )
    else:
        raise ScmServiceError(
            "MANGA_IDENTITY_REQUIRED", "Escanea una etiqueta o indica el código.", status_code=422
        )
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    evidence = str(data.get("evidencia") or "").strip()[:500] or None
    command = {
        "manga_id": str(manga.public_id),
        "label_id": str(label.public_id),
        "motivo": reason,
        "evidencia": evidence,
    }
    endpoint = f"POST /recepcion-mangas/{manga.public_id}/rechazar"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        _candidate_payload(session, manga, label, "QR_FINAL")
        rejection = ScmRechazoRecepcionManga(
            manga_id=manga.id,
            etiqueta_resuelta_id=label.id,
            motivo=reason,
            evidencia=evidence,
            actor_id=actor.id,
            operation_id=operation.operation_id,
        )
        session.add(rejection)
        session.flush()
        response = {"rechazo": rejection.to_dict()}
        _complete(operation, response, 201)
        session.add(ScmEvento(
            aggregate_type="MANGA",
            aggregate_id=str(manga.public_id),
            tipo="MANGA_WAREHOUSE_RECEIPT_REJECTED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            motivo=reason,
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def decide_manga_quality(session, *, actor_id, existence_id, operation_id, data):
    reject_unknown_fields(data, allowed={"decision", "motivo", "evidencia", "version"})
    decision = str(data.get("decision") or "").strip().upper()
    capability = {
        "LIBERADA": "CALIDAD_MANGA_LIBERAR",
        "BLOQUEADA": "CALIDAD_MANGA_BLOQUEAR",
        "RECHAZADA": "CALIDAD_MANGA_RECHAZAR",
    }.get(decision)
    if capability is None:
        raise ScmServiceError(
            "DECISION_CALIDAD_INVALIDA",
            "La decisión debe ser LIBERADA, BLOQUEADA o RECHAZADA.",
            status_code=422,
        )
    actor = load_actor(session, actor_id, capability=capability)
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    evidence = str(data.get("evidencia") or "").strip()[:500] or None
    command = {
        "decision": decision,
        "motivo": reason,
        "evidencia": evidence,
        "version": data.get("version"),
    }
    endpoint = f"POST /recepcion-mangas/{existence_id}/calidad"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    try:
        existence = session.scalar(
            select(ScmExistenciaManga)
            .where(ScmExistenciaManga.id == existence_id)
            .with_for_update()
        )
        if existence is None:
            raise ScmServiceError(
                "EXISTENCIA_MANGA_NO_ENCONTRADA", "La manga recibida no existe.", status_code=404
            )
        expected_version = data.get("version")
        if expected_version is not None and int(expected_version) != existence.version:
            raise ScmServiceError(
                "CONFLICTO_CONCURRENCIA",
                "La manga cambió desde la última lectura.",
                status_code=409,
                details={"version_actual": existence.version},
            )
        previous = existence.estado_calidad
        if previous == decision:
            raise ScmServiceError(
                "CALIDAD_SIN_CAMBIO", "La manga ya posee esa decisión de Calidad.", status_code=409
            )
        balance = session.scalar(
            select(ScmSaldoInventario)
            .where(ScmSaldoInventario.id == existence.saldo_id)
            .with_for_update()
        )
        quantity = Decimal(existence.cantidad_fisica)
        unavailable = Decimal(balance.cantidad_no_disponible)
        if previous == "LIBERADA" and decision != "LIBERADA":
            if Decimal(existence.cantidad_reservada) > 0:
                raise ScmServiceError(
                    "MANGA_CON_RESERVA",
                    "Libera o concilia la reserva antes de bloquear la manga.",
                    status_code=409,
                )
            balance.cantidad_no_disponible = unavailable + quantity
        elif previous != "LIBERADA" and decision == "LIBERADA":
            resulting = unavailable - quantity
            if resulting < 0:
                raise ScmServiceError(
                    "INVENTORY_QUALITY_INCONSISTENT",
                    "El saldo de Calidad no permite liberar esta manga.",
                    status_code=409,
                )
            balance.cantidad_no_disponible = resulting
        balance.version += 1
        existence.estado_calidad = decision
        existence.calidad_por_id = actor.id
        existence.calidad_at = utc_now()
        existence.calidad_motivo = reason
        existence.calidad_evidencia = evidence
        existence.version += 1
        session.flush()
        response = {
            "existencia": existence.to_dict(),
            "estado_anterior": previous,
        }
        _complete(operation, response)
        session.add(ScmEvento(
            aggregate_type="EXISTENCIA_MANGA",
            aggregate_id=str(existence.id),
            tipo=f"MANGA_QUALITY_{decision}",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            motivo=reason,
            before_json={"estado_calidad": previous},
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def request_receipt_reversal(session, *, actor_id, existence_id, operation_id, data):
    reject_unknown_fields(data, allowed={"motivo", "evidencia"})
    actor = load_actor(session, actor_id, capability="RECEPCION_MANGA_REVERSION_SOLICITAR")
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    evidence = str(data.get("evidencia") or "").strip()[:500] or None
    command = {"existencia_id": str(existence_id), "motivo": reason, "evidencia": evidence}
    endpoint = f"POST /recepcion-mangas/{existence_id}/reversiones"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    existence = session.get(ScmExistenciaManga, existence_id)
    if existence is None or existence.estado_logistico == "REVERSADA":
        raise ScmServiceError("EXISTENCIA_NO_REVERSABLE", "La recepcion no existe o ya fue reversada.", status_code=409)
    pending = session.scalar(select(ScmReversionRecepcionManga).where(
        ScmReversionRecepcionManga.existencia_id == existence_id,
        ScmReversionRecepcionManga.estado == "PENDIENTE",
    ))
    if pending:
        raise ScmServiceError("REVERSION_YA_SOLICITADA", "Ya existe una reversa pendiente.", status_code=409)
    reversal = ScmReversionRecepcionManga(
        existencia_id=existence_id, motivo=reason, evidencia=evidence,
        solicitada_por_id=actor.id, request_operation_id=operation.operation_id,
    )
    session.add(reversal)
    session.flush()
    response = {"reversion": reversal.to_dict()}
    _complete(operation, response, 201)
    session.add(ScmEvento(
        aggregate_type="EXISTENCIA_MANGA", aggregate_id=str(existence_id),
        tipo="MANGA_WAREHOUSE_REVERSAL_REQUESTED", actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor), motivo=reason, after_json=response,
        operation_id=operation.operation_id,
    ))
    session.commit()
    return response


def resolve_receipt_reversal(session, *, actor_id, reversal_id, operation_id, data):
    reject_unknown_fields(data, allowed={"aprobar", "motivo"})
    actor = load_actor(session, actor_id, capability="RECEPCION_MANGA_REVERSION_APROBAR")
    approve = bool(data.get("aprobar"))
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    command = {"reversion_id": str(reversal_id), "aprobar": approve, "motivo": reason}
    endpoint = f"POST /recepcion-mangas/reversiones/{reversal_id}/resolver"
    operation, replay = _reserve_operation(session, operation_id, endpoint, actor, command)
    if replay is not None:
        return replay
    reversal = session.scalar(select(ScmReversionRecepcionManga).where(
        ScmReversionRecepcionManga.id == reversal_id).with_for_update())
    if reversal is None or reversal.estado != "PENDIENTE":
        raise ScmServiceError("REVERSION_NO_PENDIENTE", "La solicitud no existe o ya fue resuelta.", status_code=409)
    if reversal.solicitada_por_id == actor.id:
        raise ScmServiceError("SEGREGACION_REQUERIDA", "Quien solicita no puede aprobar la reversa.", status_code=403)
    existence = session.scalar(select(ScmExistenciaManga).where(
        ScmExistenciaManga.id == reversal.existencia_id).with_for_update())
    reversal.estado = "APROBADA" if approve else "RECHAZADA"
    reversal.resuelta_por_id = actor.id
    reversal.resuelta_at = utc_now()
    reversal.resolucion_motivo = reason
    reversal.resolution_operation_id = operation.operation_id
    if approve:
        if existence.estado_logistico != "RECIBIDA_ALMACEN" or Decimal(existence.cantidad_reservada) > 0:
            raise ScmServiceError("EXISTENCIA_COMPROMETIDA", "La manga debe estar recibida y sin reservas.", status_code=409)
        balance = session.scalar(select(ScmSaldoInventario).where(
            ScmSaldoInventario.id == existence.saldo_id).with_for_update())
        quantity = Decimal(existence.cantidad_fisica)
        resulting = Decimal(balance.cantidad_fisica) - quantity
        unavailable = Decimal(balance.cantidad_no_disponible)
        if resulting < 0 or (existence.estado_calidad != "LIBERADA" and unavailable < quantity):
            raise ScmServiceError("INVENTARIO_INCONSISTENTE", "El Kardex no permite aplicar la reversa.", status_code=409)
        balance.cantidad_fisica = resulting
        if existence.estado_calidad != "LIBERADA":
            balance.cantidad_no_disponible = unavailable - quantity
        balance.version += 1
        session.add(ScmMovimientoInventario(
            saldo=balance, tipo="AJUSTE_NEGATIVO", cantidad_delta=-quantity,
            saldo_fisico_resultante=resulting,
            motivo=f"Reversa aprobada de recepcion {existence.manga.codigo}: {reason}",
            referencia_tipo="REVERSION_RECEPCION", referencia_id=str(reversal.id),
            actor_id=actor.id, operation_id=operation.operation_id,
        ))
        existence.estado_logistico = "REVERSADA"
        existence.version += 1
        existence.manga.estado = "PENDIENTE_RECEPCION_ALMACEN"
        existence.manga.version += 1
    session.flush()
    response = {"reversion": reversal.to_dict(), "existencia": existence.to_dict()}
    _complete(operation, response)
    session.add(ScmEvento(
        aggregate_type="EXISTENCIA_MANGA", aggregate_id=str(existence.id),
        tipo="MANGA_WAREHOUSE_REVERSAL_APPROVED" if approve else "MANGA_WAREHOUSE_REVERSAL_REJECTED",
        actor_id=actor.id, actor_snapshot=actor_snapshot(actor), motivo=reason,
        before_json={"estado_logistico": "RECIBIDA_ALMACEN"}, after_json=response,
        operation_id=operation.operation_id,
    ))
    session.commit()
    return response
