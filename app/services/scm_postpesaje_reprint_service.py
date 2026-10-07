"""Post-weighing reprint workflow.

The service only reads the original manga, weighing, correction and label
tables.  Every durable write belongs to the reprint ledger introduced for
this workflow.
"""

import hashlib
import json
import re
import uuid
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from flask import current_app

from app.extensions import db
from app.models.scm_ot import (
    ScmAnulacionPesajeManga,
    ScmCorreccionPesajeManga,
    ScmEtiquetaManga,
    ScmManga,
    ScmPesajeManga,
)
from app.models.scm_postpesaje_reprint import (
    ScmPostpesajeReprintAudit,
    ScmPostpesajeReprintItem,
    ScmPostpesajeReprintJob,
    ScmPostpesajeReprintRequest,
)
from app.models.estacion_pesaje import EstacionPesaje
from app.services.scm_service_support import ScmServiceError, load_actor
from app.services.scm_manga_assignment_projection import effective_work
from app.services.scm_postpesaje_identity import validate_stored_identity


CAPABILITY = "MANGA_ETIQUETA_POST_REIMPRIMIR"
RENDERER_VERSION = "POSTPESAJE_COPY_TSPL_1"
FINAL_JOB_STATES = {"ACK_ACCEPTED", "ACK_NOT_EMITTED", "ACK_UNCERTAIN", "BLOCKED_SOURCE"}
HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _ensure_enabled():
    if not current_app.config.get("POSTPESAJE_REPRINT_ENABLED", False):
        raise ScmServiceError("FEATURE_DISABLED", "La reimpresion POSTPESAJE no esta habilitada.", status_code=404)


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ScmServiceError("INVALID_UUID", f"{field} debe ser UUID valido.", status_code=422) from exc


def _copies(value):
    if isinstance(value, bool):
        raise ScmServiceError("INVALID_COPIES", "copias debe ser entero positivo.", status_code=422)
    if isinstance(value, float) and not value.is_integer():
        raise ScmServiceError("INVALID_COPIES", "copias debe ser entero positivo.", status_code=422)
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value.strip()):
        raise ScmServiceError("INVALID_COPIES", "copias debe ser entero positivo.", status_code=422)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ScmServiceError("INVALID_COPIES", "copias debe ser entero positivo.", status_code=422) from exc
    if result < 1 or result > 50:
        raise ScmServiceError("INVALID_COPIES", "copias debe estar entre 1 y 50.", status_code=422)
    return result


def _decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _effective_projection(session, weighing):
    correction = session.scalar(
        select(ScmCorreccionPesajeManga)
        .where(
            ScmCorreccionPesajeManga.pesaje_id == weighing.id,
            ScmCorreccionPesajeManga.estado == "APLICADA",
        )
        .order_by(ScmCorreccionPesajeManga.id.desc())
    )
    if correction is not None:
        return dict(correction.result_projection_json or {}), correction
    return {
        "peso_fisico_neto_kg": str(weighing.peso_fisico_neto_kg),
        "cantidad_confirmada": str(weighing.cantidad_confirmada),
        "fuente_cantidad": weighing.fuente_cantidad,
        "pesada_at": weighing.pesada_at.isoformat() if weighing.pesada_at else None,
    }, None


def _source(session, source_label_id, source_pesaje_id=None):
    label = session.scalar(
        select(ScmEtiquetaManga)
        .where(ScmEtiquetaManga.public_id == source_label_id)
        .options(selectinload(ScmEtiquetaManga.manga))
    )
    if label is None:
        raise ScmServiceError("SOURCE_LABEL_NOT_FOUND", "La etiqueta fuente no existe.", status_code=404)
    if label.tipo != "POSTPESAJE":
        raise ScmServiceError("SOURCE_LABEL_TYPE_INVALID", "La fuente debe ser una etiqueta POSTPESAJE.", status_code=409)
    manga = label.manga
    if manga is None or manga.estado == "ANULADA":
        raise ScmServiceError("SOURCE_MANGA_INVALID", "La manga fuente fue anulada o no existe.", status_code=409)
    current_label = session.scalar(
        select(ScmEtiquetaManga)
        .where(ScmEtiquetaManga.manga_id == manga.id, ScmEtiquetaManga.tipo == "POSTPESAJE")
        .order_by(ScmEtiquetaManga.version.desc(), ScmEtiquetaManga.id.desc())
        .limit(1)
    )
    if current_label is None or current_label.id != label.id or label.estado == "INVALIDADA":
        raise ScmServiceError(
            "SOURCE_LABEL_STALE",
            "La etiqueta fuente ya no es la POSTPESAJE vigente.",
            status_code=409,
            details={"source_label_id": str(source_label_id), "current_label_id": str(current_label.public_id) if current_label else None},
        )
    if label.estado != "IMPRESA" or label.printed_at is None:
        raise ScmServiceError(
            "SOURCE_LABEL_NOT_PRINTED",
            "La etiqueta POSTPESAJE fuente no tiene una impresion original confirmada.",
            status_code=409,
        )
    weighing = session.scalar(
        select(ScmPesajeManga)
        .where(ScmPesajeManga.manga_id == manga.id, ScmPesajeManga.estado == "VIGENTE")
        .order_by(ScmPesajeManga.id.desc())
        .limit(1)
    )
    if weighing is None:
        raise ScmServiceError("SOURCE_WEIGHING_NOT_CURRENT", "No existe un pesaje final vigente para la manga.", status_code=409)
    if source_pesaje_id is None:
        raise ScmServiceError("SOURCE_PESAJE_REQUIRED", "pesaje_id es obligatorio para identificar la fuente.", status_code=422)
    if weighing.public_id != source_pesaje_id:
        raise ScmServiceError(
            "SOURCE_PESAJE_MISMATCH",
            "pesaje_id no corresponde al pesaje vigente de la etiqueta.",
            status_code=409,
            details={"expected_pesaje_id": str(weighing.public_id), "source_pesaje_id": str(source_pesaje_id)},
        )
    if session.scalar(select(ScmAnulacionPesajeManga.id).where(ScmAnulacionPesajeManga.pesaje_id == weighing.id)) is not None:
        raise ScmServiceError("SOURCE_WEIGHING_CANCELLED", "El pesaje fuente fue anulado.", status_code=409)
    projection, correction = _effective_projection(session, weighing)
    payload = deepcopy(label.payload_json or {})
    template = payload.get("template") if isinstance(payload.get("template"), dict) else {}
    if payload.get("document_type") != "POSTPESAJE":
        raise ScmServiceError("SOURCE_PAYLOAD_INVALID", "La etiqueta fuente no es un documento POSTPESAJE.", status_code=409)
    if template.get("version") != "POSTPESAJE_TSPL_5" or "qr" in payload:
        raise ScmServiceError("SOURCE_PAYLOAD_INVALID", "La etiqueta fuente no cumple el contrato POSTPESAJE vigente.", status_code=409)
    if not label.payload_hash or label.payload_hash != _hash(payload):
        raise ScmServiceError("SOURCE_PAYLOAD_HASH_INVALID", "El hash de la etiqueta fuente no coincide con su payload.", status_code=409)
    expected_manga = str(manga.public_id)
    if payload.get("manga_id") and str(payload["manga_id"]) != expected_manga:
        raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta no pertenece a la manga declarada.", status_code=409)
    for key in ("pesaje_id", "source_pesaje_id"):
        if payload.get(key) is not None and str(payload[key]) != str(weighing.public_id):
            raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta pertenece a otro pesaje.", status_code=409)
    closing_work = effective_work(manga)
    closing_ot = closing_work.orden_trabajo if closing_work is not None else manga.ot
    expected_ot = getattr(closing_ot, "codigo_ot", None) if closing_ot is not None else None
    if payload.get("ot_cierre") and expected_ot and str(payload["ot_cierre"]) != str(expected_ot):
        raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta pertenece a otra OF/OT vigente.", status_code=409, details={"expected_ot": expected_ot, "source_ot": payload.get("ot_cierre")})
    expected_ot_id = getattr(closing_ot, "public_id", None) if closing_ot is not None else None
    for key in ("ot_id", "orden_trabajo_id"):
        if payload.get(key) is not None and expected_ot_id is not None and str(payload[key]) != str(expected_ot_id):
            raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta pertenece a otra OF/OT.", status_code=409)
    if payload.get("codigo_manga") and str(payload["codigo_manga"]) != str(manga.codigo):
        raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta pertenece a otra manga.", status_code=409)
    expected_piece = getattr(manga, "pieza_color_sku_snapshot", None)
    for key in ("pieza_color_sku", "sku"):
        if payload.get(key) is not None and expected_piece is not None and str(payload[key]) != str(expected_piece):
            raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta pertenece a otra pieza.", status_code=409)
    canonical_piece = (
        f"{manga.articulo_nombre_snapshot} ({expected_piece})"
        if expected_piece else manga.articulo_nombre_snapshot
    )
    if payload.get("pieza_color") is not None and str(payload["pieza_color"]) != str(canonical_piece):
        raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta pertenece a otra pieza/color.", status_code=409)
    if not validate_stored_identity(payload.get("identidad_producto"), manga):
        raise ScmServiceError(
            "SOURCE_PAYLOAD_IDENTITY_INVALID",
            "La identidad estructurada de la etiqueta no coincide con el snapshot fuente.",
            status_code=409,
        )
    for key in ("color", "color_nombre"):
        if payload.get(key) is not None and manga.color_snapshot is not None and str(payload[key]) != str(manga.color_snapshot):
            raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta pertenece a otro color.", status_code=409)
    expected_net = _decimal(projection.get("peso_fisico_neto_kg", projection.get("peso_neto_real_kg")))
    source_net = _decimal(payload.get("peso_neto_real_kg", payload.get("kg_fisico")))
    if payload.get("peso_neto_real_kg", payload.get("kg_fisico")) is not None and source_net is None:
        raise ScmServiceError("SOURCE_PAYLOAD_INVALID", "El peso fuente debe ser un numero finito.", status_code=409)
    if expected_net is not None and source_net is not None and expected_net != source_net:
        raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta no refleja el peso vigente.", status_code=409, details={"expected_net_kg": str(expected_net), "source_net_kg": str(source_net)})
    expected_quantity = _decimal(projection.get("cantidad_confirmada"))
    source_quantity = _decimal(payload.get("cantidad_confirmada_un"))
    if payload.get("cantidad_confirmada_un") is not None and source_quantity is None:
        raise ScmServiceError("SOURCE_PAYLOAD_INVALID", "La cantidad fuente debe ser un numero finito.", status_code=409)
    if expected_quantity is not None and source_quantity is not None and expected_quantity != source_quantity:
        raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta no refleja la cantidad vigente.", status_code=409)
    expected_source = projection.get("fuente_cantidad", weighing.fuente_cantidad)
    if payload.get("fuente_cantidad") and str(payload["fuente_cantidad"]) != str(expected_source):
        raise ScmServiceError("SOURCE_MISMATCH", "La etiqueta no refleja la correccion o fuente vigente.", status_code=409)
    source_snapshot = {
        "manga_id": expected_manga,
        "manga_codigo": manga.codigo,
        "weighing_id": str(weighing.public_id),
        "weighing_state": weighing.estado,
        "effective_projection": projection,
        "correction_id": str(correction.public_id) if correction else None,
        "source_label_id": str(label.public_id),
        "source_label_version": label.version,
        "source_payload_hash": label.payload_hash,
        "source_payload_contract": {"document_type": payload.get("document_type"), "template_version": template.get("version"), "qr_present": "qr" in payload},
        "ot_cierre": expected_ot,
        "ot_id": str(expected_ot_id) if expected_ot_id else None,
        "pieza_color_sku": manga.pieza_color_sku_snapshot,
        "color": manga.color_snapshot,
    }
    source_snapshot["snapshot_hash"] = _hash(source_snapshot)
    return label, manga, weighing, correction, payload, source_snapshot


def _station(session, station_id):
    station = session.get(EstacionPesaje, str(station_id))
    if station is None or station.estado_admin == "RETIRADA":
        raise ScmServiceError("STATION_NOT_AVAILABLE", "La estacion no esta disponible.", status_code=409)
    return station


def _normalized_items(session, raw_items, station_id):
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 50:
        raise ScmServiceError("ITEMS_REQUIRED", "items debe contener entre 1 y 50 elementos.", status_code=422)
    seen = set()
    items = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ScmServiceError("ITEM_INVALID", "Cada item debe ser un objeto.", status_code=422)
        label_id = _uuid(raw.get("source_label_id"), "source_label_id")
        pesaje_id = _uuid(raw.get("pesaje_id"), "pesaje_id")
        if label_id in seen:
            raise ScmServiceError("ITEM_DUPLICATE", "No se puede repetir source_label_id.", status_code=422)
        seen.add(label_id)
        copies = _copies(raw.get("copias", raw.get("copies")))
        label, manga, weighing, correction, payload, snapshot = _source(session, label_id, pesaje_id)
        items.append({
            "sequence": index,
            "source_label_id": label_id,
            "manga_id": manga.id,
            "manga_public_id": str(manga.public_id),
            "manga_codigo": manga.codigo,
            "weighing_id": weighing.id,
            "weighing_public_id": str(weighing.public_id),
            "source_pesaje_id": pesaje_id,
            "copies": copies,
            "source_payload_hash": label.payload_hash,
            "source_payload": payload,
            "source_snapshot": snapshot,
            "source_snapshot_hash": snapshot["snapshot_hash"],
        })
    total = sum(item["copies"] for item in items)
    if total > 250:
        raise ScmServiceError("COPIES_LIMIT", "La solicitud no puede superar 250 copias.", status_code=422)
    digest_input = {
        "station_id": str(station_id),
        "renderer_version": RENDERER_VERSION,
        "items": [
            {"source_label_id": str(item["source_label_id"]), "source_pesaje_id": str(item["source_pesaje_id"]), "copies": item["copies"], "source_payload_hash": item["source_payload_hash"], "source_snapshot_hash": item["source_snapshot_hash"]}
            for item in items
        ],
    }
    return items, total, _hash(digest_input)


def _job_dict(job):
    state = "PENDING" if job.estado == "QUEUED" else job.estado
    return {
        "copy_job_id": str(job.job_id),
        "job_kind": "POSTPESAJE_COPY",
        "job_id": str(job.job_id),
        "request_id": str(job.item.request.request_id),
        "item_id": str(job.item.item_id),
        "station_id": job.station_id,
        "source_label_id": str(job.item.source_label_id),
        "source_pesaje_id": str(job.item.source_snapshot_json.get("weighing_id")),
        "station_id": job.station_id,
        "renderer_version": job.renderer_version,
        "copias": job.authorized_copies,
        "authorized_copies": job.authorized_copies,
        "payload_hash": job.source_payload_hash,
        "source_payload_hash": job.source_payload_hash,
        "payload": deepcopy(job.source_payload_json),
        "source_payload": deepcopy(job.source_payload_json),
        "estado": state,
        "attempt_id": str(job.attempt_id) if job.attempt_id else None,
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "acknowledged_at": job.acknowledged_at.isoformat() if job.acknowledged_at else None,
        "ack_result": deepcopy(job.ack_result_json),
    }


def _request_dict(request_row):
    return {
        "request_id": str(request_row.request_id),
        "operation_id": str(request_row.operation_id),
        "station_id": request_row.station_id,
        "motivo": request_row.motivo,
        "preview_digest": request_row.preview_digest,
        "renderer_version": request_row.renderer_version,
        "estado": request_row.estado,
        "created_at": request_row.created_at.isoformat() if request_row.created_at else None,
        "jobs": [_job_dict(item.job) for item in request_row.items],
    }


def _ack_dict(job):
    """Return the immutable flat receipt consumed by the station edge."""
    receipt = deepcopy(job.ack_result_json or {})
    receipt.update({
        "copy_job_id": str(job.job_id),
        "job_kind": "POSTPESAJE_COPY",
        "station_id": job.station_id,
        "attempt_id": str(job.attempt_id) if job.attempt_id else receipt.get("attempt_id"),
        "estado": "PENDING" if job.estado == "QUEUED" else job.estado,
    })
    return receipt


def preview_reprint(session, *, actor_id, data):
    _ensure_enabled()
    load_actor(session, actor_id, capability=CAPABILITY)
    if not isinstance(data, dict):
        raise ScmServiceError("BODY_REQUIRED", "El cuerpo JSON es obligatorio.", status_code=422)
    station_id = _uuid(data.get("station_id"), "station_id")
    _station(session, station_id)
    items, total, digest = _normalized_items(session, data.get("items"), station_id)
    return {
        "preview_digest": digest,
        "renderer_version": RENDERER_VERSION,
        "total_copias": total,
        "items": [
            {
                "source_label_id": str(item["source_label_id"]),
                "manga_id": item["manga_public_id"],
                "manga_codigo": item["manga_codigo"],
                "weighing_id": item["weighing_public_id"],
                "pesaje_id": item["weighing_public_id"],
                "source_pesaje_id": item["source_pesaje_id"],
                "copias": item["copies"],
                "source_payload_hash": item["source_payload_hash"],
                "source_payload": item["source_payload"],
                "source_snapshot": item["source_snapshot"],
                "source_snapshot_hash": item["source_snapshot_hash"],
                "current_source": {
                    "pesaje_id": item["weighing_public_id"],
                    "label_id": str(item["source_label_id"]),
                    "weighing_state": item["source_snapshot"]["weighing_state"],
                    "correction_id": item["source_snapshot"]["correction_id"],
                },
            }
            for item in items
        ],
    }


def confirm_reprint(session, *, actor_id, data):
    _ensure_enabled()
    actor = load_actor(session, actor_id, capability=CAPABILITY)
    if not isinstance(data, dict):
        raise ScmServiceError("BODY_REQUIRED", "El cuerpo JSON es obligatorio.", status_code=422)
    operation_id = _uuid(data.get("operation_id"), "operation_id")
    station_id = _uuid(data.get("station_id"), "station_id")
    _station(session, station_id)
    reason = str(data.get("motivo") or "").strip()
    if not reason or len(reason) > 500:
        raise ScmServiceError("MOTIVO_REQUIRED", "motivo es obligatorio y debe tener hasta 500 caracteres.", status_code=422)
    supplied_digest = str(data.get("preview_digest") or "").strip().lower()
    raw_items = data.get("items")
    fingerprint = _hash({
        "actor_id": actor.id,
        "motivo": reason,
        "station_id": str(station_id),
        "operation_id": str(operation_id),
        "preview_digest": supplied_digest,
        "items": raw_items,
    })
    existing = session.scalar(select(ScmPostpesajeReprintRequest).where(ScmPostpesajeReprintRequest.operation_id == operation_id).options(selectinload(ScmPostpesajeReprintRequest.items).selectinload(ScmPostpesajeReprintItem.job)))
    if existing is not None:
        if existing.request_fingerprint != fingerprint or existing.actor_id != actor.id:
            raise ScmServiceError("IDEMPOTENCY_CONFLICT", "operation_id ya fue usado con otra solicitud.", status_code=409)
        return _request_dict(existing)
    items, total, digest = _normalized_items(session, raw_items, station_id)
    if supplied_digest != digest:
        raise ScmServiceError("PREVIEW_DIGEST_MISMATCH", "La previsualizacion ya no coincide con la fuente vigente.", status_code=409, details={"expected_preview_digest": digest})
    request_row = ScmPostpesajeReprintRequest(
        request_id=uuid.uuid4(), operation_id=operation_id, actor_id=actor.id,
        station_id=str(station_id), motivo=reason, preview_digest=digest,
        renderer_version=RENDERER_VERSION, estado="QUEUED",
        request_fingerprint=fingerprint,
    )
    try:
        session.add(request_row)
        session.flush()
        for item in items:
            item_row = ScmPostpesajeReprintItem(
                request=request_row, sequence=item["sequence"], source_label_id=item["source_label_id"],
                manga_id=item["manga_id"], weighing_id=item["weighing_id"], copies=item["copies"],
                source_payload_hash=item["source_payload_hash"], source_payload_json=item["source_payload"],
                source_snapshot_json=item["source_snapshot"],
                source_snapshot_hash=item["source_snapshot_hash"],
            )
            session.add(item_row)
            session.flush()
            session.add(ScmPostpesajeReprintJob(
                item=item_row, station_id=str(station_id), renderer_version=RENDERER_VERSION,
                authorized_copies=item["copies"], source_payload_hash=item["source_payload_hash"],
                source_payload_json=item["source_payload"], estado="QUEUED",
            ))
        session.add(ScmPostpesajeReprintAudit(
            request=request_row, event="CONFIRMED", actor_id=actor.id, station_id=str(station_id),
            details_json={"preview_digest": digest, "total_copias": total, "item_count": len(items)},
        ))
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(ScmPostpesajeReprintRequest).where(ScmPostpesajeReprintRequest.operation_id == operation_id).options(selectinload(ScmPostpesajeReprintRequest.items).selectinload(ScmPostpesajeReprintItem.job)))
        if existing is not None and existing.request_fingerprint == fingerprint and existing.actor_id == actor.id:
            return _request_dict(existing)
        raise ScmServiceError("IDEMPOTENCY_CONFLICT", "operation_id ya fue usado con otra solicitud.", status_code=409)
    return _request_dict(request_row)


def get_reprint_request(session, *, actor_id, request_id):
    _ensure_enabled()
    load_actor(session, actor_id, capability=CAPABILITY)
    row = session.scalar(select(ScmPostpesajeReprintRequest).where(ScmPostpesajeReprintRequest.request_id == request_id).options(selectinload(ScmPostpesajeReprintRequest.items).selectinload(ScmPostpesajeReprintItem.job)))
    if row is None:
        raise ScmServiceError("REQUEST_NOT_FOUND", "La solicitud no existe.", status_code=404)
    return _request_dict(row)


def list_station_reprint_jobs(session, *, station_id, limit=20):
    _ensure_enabled()
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ScmServiceError("INVALID_LIMIT", "limit debe ser numerico.", status_code=422) from exc
    if not 1 <= limit <= 100:
        raise ScmServiceError("INVALID_LIMIT", "limit debe estar entre 1 y 100.", status_code=422)
    _station(session, station_id)
    jobs = session.scalars(
        select(ScmPostpesajeReprintJob)
        .where(ScmPostpesajeReprintJob.station_id == str(station_id), ScmPostpesajeReprintJob.estado == "QUEUED")
        .options(selectinload(ScmPostpesajeReprintJob.item).selectinload(ScmPostpesajeReprintItem.request))
        .order_by(ScmPostpesajeReprintJob.job_id)
        .limit(limit)
    ).all()
    return {"jobs": [_job_dict(job) for job in jobs], "count": len(jobs)}


def claim_reprint_job(session, *, station_id, job_id, attempt_id):
    _ensure_enabled()
    _station(session, station_id)
    job = session.scalar(select(ScmPostpesajeReprintJob).where(ScmPostpesajeReprintJob.job_id == job_id).options(selectinload(ScmPostpesajeReprintJob.item).selectinload(ScmPostpesajeReprintItem.request)).with_for_update())
    if job is None:
        raise ScmServiceError("JOB_NOT_FOUND", "El trabajo no existe.", status_code=404)
    if job.station_id != str(station_id):
        raise ScmServiceError("JOB_STATION_MISMATCH", "El trabajo pertenece a otra estacion.", status_code=403)
    if job.estado == "CLAIMED" and job.attempt_id == attempt_id:
        return _job_dict(job)
    if job.estado != "QUEUED":
        raise ScmServiceError("JOB_NOT_CLAIMABLE", "El trabajo ya tiene un estado terminal o incierto.", status_code=409, details={"estado": job.estado})
    try:
        source = _source(session, job.item.source_label_id, _uuid(job.item.source_snapshot_json.get("weighing_id"), "source_pesaje_id"))
        if source[5].get("snapshot_hash") != job.item.source_snapshot_hash or source[0].payload_hash != job.item.source_payload_hash:
            raise ScmServiceError("SOURCE_SNAPSHOT_STALE", "La fuente efectiva cambio desde la confirmacion.", status_code=409)
    except ScmServiceError as exc:
        job.estado = "BLOCKED_SOURCE"
        session.add(ScmPostpesajeReprintAudit(request_id=job.item.request_id, job_id=job.job_id, event="CLAIM_BLOCKED_SOURCE", station_id=str(station_id), details_json={"code": exc.code, "message": exc.message}))
        session.commit()
        raise
    job.estado = "CLAIMED"
    job.attempt_id = attempt_id
    from datetime import datetime, timezone
    job.claimed_at = datetime.now(timezone.utc)
    session.add(ScmPostpesajeReprintAudit(request_id=job.item.request_id, job_id=job.job_id, event="CLAIMED", station_id=str(station_id), details_json={"attempt_id": str(attempt_id)}))
    session.commit()
    return _job_dict(job)


def acknowledge_reprint_job(session, *, station_id, job_id, data):
    _ensure_enabled()
    _station(session, station_id)
    if not isinstance(data, dict):
        raise ScmServiceError("BODY_REQUIRED", "El cuerpo JSON es obligatorio.", status_code=422)
    attempt_id = _uuid(data.get("attempt_id"), "attempt_id")
    status = str(data.get("result") or "").strip().upper()
    if status not in {"ACCEPTED", "NOT_EMITTED", "UNCERTAIN"}:
        raise ScmServiceError("ACK_STATUS_INVALID", "result debe ser ACCEPTED, NOT_EMITTED o UNCERTAIN.", status_code=422)
    evidence = {
        key: data.get(key)
        for key in (
            "attempt_id", "result", "expected_bytes", "bytes_written", "document_started",
            "write_attempted", "simulated", "job_id", "error", "rendered_payload_hash",
            "renderer_version", "printer_name",
        )
        if data.get(key) is not None
    }
    spooler_job_id = evidence.get("job_id")
    if spooler_job_id is not None and (isinstance(spooler_job_id, bool) or not isinstance(spooler_job_id, int)):
        raise ScmServiceError("ACK_JOB_ID_INVALID", "job_id debe ser el identificador numerico del spooler.", status_code=422)
    digest = evidence.get("rendered_payload_hash")
    if digest and not HASH_RE.fullmatch(str(digest)):
        raise ScmServiceError("ACK_HASH_INVALID", "rendered_payload_hash debe ser SHA-256.", status_code=422)
    job = session.scalar(select(ScmPostpesajeReprintJob).where(ScmPostpesajeReprintJob.job_id == job_id).options(selectinload(ScmPostpesajeReprintJob.item).selectinload(ScmPostpesajeReprintItem.request)).with_for_update())
    if job is None:
        raise ScmServiceError("JOB_NOT_FOUND", "El trabajo no existe.", status_code=404)
    if job.station_id != str(station_id):
        raise ScmServiceError("JOB_STATION_MISMATCH", "El trabajo pertenece a otra estacion.", status_code=403)
    if job.attempt_id != attempt_id:
        raise ScmServiceError("ATTEMPT_MISMATCH", "attempt_id no coincide con el claim vigente.", status_code=409)
    if job.estado in FINAL_JOB_STATES:
        existing = job.ack_result_json or {}
        if existing == evidence:
            return _ack_dict(job)
        raise ScmServiceError("ACK_ALREADY_RECORDED", "El trabajo ya tiene acuse y no puede cambiarse.", status_code=409)
    from datetime import datetime, timezone
    job.estado = {"ACCEPTED": "ACK_ACCEPTED", "NOT_EMITTED": "ACK_NOT_EMITTED", "UNCERTAIN": "ACK_UNCERTAIN"}[status]
    job.acknowledged_at = datetime.now(timezone.utc)
    job.ack_result_json = evidence
    session.add(ScmPostpesajeReprintAudit(request_id=job.item.request_id, job_id=job.job_id, event="ACKNOWLEDGED", station_id=str(station_id), details_json=job.ack_result_json))
    request_row = job.item.request
    if all(item.job is not None and item.job.estado in FINAL_JOB_STATES for item in request_row.items):
        request_row.estado = "COMPLETED"
    session.commit()
    return _ack_dict(job)
