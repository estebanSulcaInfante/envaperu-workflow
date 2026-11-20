"""Real Mesa return and many-to-many union commands.

This adapter deliberately has a small contract for the station edge.  The
server resolves the Estación 2 account, validates the edge-attested capture,
and performs the union, stock movement and canonical identity update in one
transaction.  No kg is assigned to an origin during a union.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.scm_articulos import ScmArticulo, ScmArticuloPiezaColor
from app.models.scm_auditoria import ScmOperacion
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmRetiroArmadoKg,
    ScmRetiroArmadoKgItem,
    ScmSaldoInventarioKg,
    ScmEtiquetaUnidadKg,
    ScmUnidadFisicaKg,
    _decimal,
)
from app.models.scm_mesa_returns import (
    ScmMesaRetornoOrigen,
    ScmMesaRetornoRecibo,
    ScmMesaRetornoUnion,
)
from app.models.producto import PiezaColor
from app.models.scm_ot import ScmEtiquetaManga, ScmManga
from app.models.estacion_pesaje import EstacionPesaje
from app.services.scm_service_support import (
    ScmServiceError,
    acquire_kg_productive_write_lock,
    load_actor,
)
from app.services.scm_warehouse_scope_service import assert_location_scope


QUANTUM = Decimal("0.001")
STATION_ID = "ESTACION-2"


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _text(value, field, max_length=160, required=True):
    if value is None and not required:
        return None
    value = str(value or "").strip()
    if required and not value:
        raise ScmServiceError("FIELD_REQUIRED", f"Se requiere {field}.", status_code=422, details={"field": field})
    if len(value) > max_length:
        raise ScmServiceError("FIELD_TOO_LONG", f"{field} excede el tamaño permitido.", status_code=422, details={"field": field})
    return value


def _decimal(value, field="kg", *, positive=False, non_negative=False):
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError("INVALID_DECIMAL", f"{field} debe ser decimal.", status_code=422, details={"field": field}) from error
    if not parsed.is_finite() or (positive and parsed <= 0) or (non_negative and parsed < 0):
        raise ScmServiceError("INVALID_DECIMAL", f"{field} no es válido.", status_code=422, details={"field": field})
    rounded = parsed.quantize(QUANTUM)
    if parsed != rounded:
        raise ScmServiceError("INVALID_DECIMAL_SCALE", f"{field} admite tres decimales.", status_code=422, details={"field": field})
    return rounded


def _fmt(value):
    return format(Decimal(value), ".3f") if value is not None else None


def _uuid(value, field="operation_id"):
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError("INVALID_UUID", f"{field} debe ser UUID.", status_code=422) from error


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _mesa_actor(session, actor_id):
    actor = load_actor(session, actor_id, capability="RETORNO_RECIBIR")
    if actor.codigo != STATION_ID:
        raise ScmServiceError("MESA_ACTOR_REQUIRED", "La operación requiere la cuenta propia de Estación 2.", status_code=403)
    return actor


def _assert_station(session, station_id):
    """Accept the authenticated station UUID (or the legacy symbolic test id)."""
    value = str(station_id or "").strip()
    station = session.get(EstacionPesaje, value) if value else None
    if station is None and value.upper() != STATION_ID:
        raise ScmServiceError("STATION_MISMATCH", "La operación no pertenece a Estación 2.", status_code=403)
    if station is not None and station.estado_admin != "ACTIVA":
        raise ScmServiceError("STATION_DISABLED", "La Estación 2 está deshabilitada.", status_code=403)
    return value


def _operation(session, operation_id, endpoint, actor_id, data):
    operation_id = _uuid(operation_id)
    digest = _hash({"endpoint": endpoint, "actor_id": actor_id, "data": data})
    prior = session.get(ScmOperacion, operation_id)
    if prior is not None:
        if prior.actor_id != actor_id or prior.endpoint != endpoint or prior.request_sha256 != digest:
            raise ScmServiceError("IDEMPOTENCY_CONFLICT", "La operación ya fue usada con otra intención.", status_code=409)
        if prior.response_json is None:
            raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La operación anterior no tiene resultado recuperable.", status_code=409)
        return operation_id, copy.deepcopy(prior.response_json), prior.estado_http or 200
    operation = ScmOperacion(operation_id=operation_id, endpoint=endpoint, actor_id=actor_id, request_sha256=digest)
    session.add(operation)
    session.flush()
    return operation_id, None, operation


def _complete(session, operation_id, payload, status=201):
    operation = session.get(ScmOperacion, operation_id)
    operation.response_json = copy.deepcopy(payload)
    operation.estado_http = status
    session.commit()
    return payload, status


def _article_color(article):
    variant = getattr(article, "pieza_color", None)
    piece_color = getattr(variant, "pieza_color", None) if variant is not None else None
    color_rel = getattr(piece_color, "color_produccion_rel", None) if piece_color is not None else None
    color = getattr(color_rel, "nombre", None) if color_rel is not None else None
    return (
        getattr(variant, "pieza_color_sku", None) if variant is not None else None,
        color,
    )


def _unit_by_code(session, code):
    code = _text(code, "codigo")
    # Use the canonical custody resolver first.  It validates current label
    # version/state and understands both unit labels and PREPESAJE manga QR.
    from app.services.scm_kg_custody_service import _unit_by_code as custody_unit_by_code
    canonical_code = code
    if code.startswith("L1|"):
        canonical_code = json.dumps({"v": 1, "label_id": code[3:]}, separators=(",", ":"))
    resolved = custody_unit_by_code(session, canonical_code)
    if resolved is not None:
        return resolved
    # Human manga codes are retained as a supported legacy alias.  Resolve
    # through the original existence only; never manufacture a new unit.
    manga = session.scalar(select(ScmManga).where(ScmManga.codigo == code))
    if manga is not None:
        existence = session.scalar(select(ScmExistenciaMangaKg).where(
            ScmExistenciaMangaKg.manga_id == manga.id,
            ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
        ).order_by(ScmExistenciaMangaKg.recibida_at.desc()))
        if existence is not None and existence.unidad_fisica_kg_id is not None:
            return session.get(ScmUnidadFisicaKg, existence.unidad_fisica_kg_id)
    label_uuid = None
    if code.startswith("L1|"):
        try:
            label_uuid = uuid.UUID(code[3:])
        except (TypeError, ValueError):
            pass
    if label_uuid is None:
        try:
            parsed_qr = json.loads(code)
            if isinstance(parsed_qr, dict) and parsed_qr.get("label_id"):
                label_uuid = uuid.UUID(str(parsed_qr["label_id"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if label_uuid is not None:
        label = session.scalar(select(ScmEtiquetaUnidadKg).where(
            or_(ScmEtiquetaUnidadKg.public_id == label_uuid, ScmEtiquetaUnidadKg.id == label_uuid)
        ))
        if label is not None:
            return session.get(ScmUnidadFisicaKg, label.unidad_id)
    parsed = None
    try:
        parsed = uuid.UUID(code)
    except (TypeError, ValueError):
        pass
    query = select(ScmUnidadFisicaKg).where(
        or_(ScmUnidadFisicaKg.codigo == code, ScmUnidadFisicaKg.public_id == parsed)
        if parsed else ScmUnidadFisicaKg.codigo == code
    )
    return session.scalar(query)


def _manga_aliases_by_id(session, manga_ids):
    if not manga_ids:
        return {}
    aliases = {}
    mangas = session.scalars(select(ScmManga).where(ScmManga.id.in_(manga_ids))).all()
    for manga in mangas:
        aliases[manga.id] = [manga.codigo]
    labels = session.scalars(select(ScmEtiquetaManga).where(
        ScmEtiquetaManga.manga_id.in_(manga_ids),
        ScmEtiquetaManga.tipo == "PREPESAJE",
    ).order_by(ScmEtiquetaManga.manga_id, ScmEtiquetaManga.version.desc())).all()
    seen = set()
    for label in labels:
        if label.manga_id in seen:
            continue
        seen.add(label.manga_id)
        if label.estado != "IMPRESA":
            continue
        aliases.setdefault(label.manga_id, []).extend([f"L1|{label.public_id}"])
        qr = (label.payload_json or {}).get("qr_value")
        if qr:
            aliases[label.manga_id].append(qr)
        else:
            aliases[label.manga_id].append(json.dumps(label.payload_json or {}, separators=(",", ":"), sort_keys=True))
    return aliases


def _unit_aliases(session, unit):
    aliases = [unit.codigo]
    label = session.scalar(select(ScmEtiquetaUnidadKg).where(
        ScmEtiquetaUnidadKg.unidad_id == unit.id
    ).order_by(ScmEtiquetaUnidadKg.version.desc()))
    if label is not None:
        aliases.extend([f"L1|{label.public_id}"])
        qr = (label.payload_json or {}).get("qr_value")
        if qr:
            aliases.append(qr)
    return list(dict.fromkeys(aliases))


def _aliases_by_unit(session, unit_ids):
    if not unit_ids:
        return {}
    aliases = {unit_id: [] for unit_id in unit_ids}
    labels = session.scalars(select(ScmEtiquetaUnidadKg).where(
        ScmEtiquetaUnidadKg.unidad_id.in_(unit_ids)
    ).order_by(ScmEtiquetaUnidadKg.unidad_id, ScmEtiquetaUnidadKg.version.desc())).all()
    for label in labels:
        if aliases[label.unidad_id]:
            continue
        values = [f"L1|{label.public_id}"]
        qr = (label.payload_json or {}).get("qr_value")
        if qr:
            values.append(qr)
        aliases[label.unidad_id] = values
    return aliases


def _source_code(item):
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        raise ScmServiceError("ORIGIN_INVALID", "El origen debe ser un código o un objeto.", status_code=422)
    return item.get("codigo") or item.get("code") or item.get("unit_id") or item.get("unidad_id") or item.get("public_id")


def _source_label_id(item, data=None):
    """Read the scanned PREPESAJE identity without trusting display fields."""
    value = item.get("label_id") if isinstance(item, dict) else None
    if value is None and isinstance(data, dict):
        labels = data.get("label_ids")
        code = _source_code(item)
        if labels is not None and not isinstance(labels, dict):
            raise ScmServiceError("LABEL_ID_INVALID", "label_ids debe ser un objeto.", status_code=422)
        value = labels.get(code) if isinstance(labels, dict) else None
    return _uuid(value, "label_id") if value is not None else None


def _source_identity(session, unit, *, lock=False):
    if unit is None:
        raise ScmServiceError("ORIGIN_NOT_FOUND", "La identidad de origen no existe.", status_code=404)
    # Serialize competing union/return commands on the physical identity.
    # PostgreSQL honors this row lock; the state check below then observes the
    # first committed reservation and rejects the second command.
    unit_query = select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit.id)
    if lock:
        unit_query = unit_query.with_for_update()
    unit = session.scalar(unit_query)
    article = unit.articulo
    if article is None or article.clase != "PIEZA_COLOR" or article.unidad_inventario != "KG":
        raise ScmServiceError("ORIGIN_NOT_KG_PIECE", "El origen no es una pieza/color KG.", status_code=409)
    item_query = select(ScmRetiroArmadoKgItem).where(ScmRetiroArmadoKgItem.unidad_id == unit.id).join(ScmRetiroArmadoKg).order_by(ScmRetiroArmadoKg.created_at.desc()).limit(1)
    if lock:
        item_query = item_query.with_for_update()
    item = session.scalar(item_query)
    if item is None or unit.estado != "ACTIVA" or unit.estado_logistico != "RETIRADA_ARMADO":
        raise ScmServiceError("ORIGIN_NOT_RETURNABLE", "La identidad no está retirada y disponible para este retorno.", status_code=409)
    piece_sku, color = _article_color(article)
    manga = getattr(item.retiro, "manga", None)
    existence_id = unit.recepcion_vigente_id or unit.existencia_manga_kg_id
    existence = session.get(ScmExistenciaMangaKg, existence_id) if existence_id else None
    manga = existence.manga if existence is not None else None
    lote_id = getattr(manga, "lote_articulo_id", None) if manga is not None else None
    lote_snapshot = getattr(getattr(manga, "lote_articulo", None), "codigo", None) if manga is not None else None
    ot = getattr(manga, "ot", None) if manga is not None else None
    ot_date = getattr(ot, "fecha", None)
    ot_date = ot_date.isoformat() if ot_date is not None else None
    ot_shift = getattr(ot, "turno", None) if ot is not None else None
    return {
        "unit": unit,
        "item": item,
        "article": article,
        "piece_sku": piece_sku,
        "color": color,
        "kg": _decimal(item.neto_entregado_kg, "neto_entregado_kg", positive=True),
        "lote_id": str(lote_id) if lote_id is not None else None,
        "lote_snapshot": lote_snapshot,
        "ot_date": ot_date,
        "ot_shift": ot_shift,
        "piece_name": article.nombre,
    }


def _assert_current_label(session, unit, label_id):
    """Bind a scanned QR to the current manga or KG-unit label resolver."""
    if label_id is None:
        return
    if unit is None:
        raise ScmServiceError("LABEL_NOT_CURRENT", "La etiqueta no corresponde a una identidad vigente.", status_code=409)
    from app.services.scm_kg_custody_service import _unit_by_code as resolve_custody_label
    qr = json.dumps({"v": 1, "label_id": str(label_id)}, separators=(",", ":"))
    try:
        resolved = resolve_custody_label(session, qr)
    except ScmServiceError as error:
        raise ScmServiceError("LABEL_NOT_CURRENT", "La etiqueta ya no está vigente para esta identidad.", status_code=409) from error
    if resolved is None or resolved.id != unit.id:
        raise ScmServiceError("LABEL_NOT_CURRENT", "La etiqueta no corresponde a la identidad escaneada.", status_code=409)


def _origin_items(data):
    for key in ("origenes", "origins", "units", "unidades", "codigos", "codes"):
        value = data.get(key)
        if value is not None:
            if not isinstance(value, list) or not value:
                raise ScmServiceError("ORIGINS_REQUIRED", "La unión requiere uno o más orígenes.", status_code=422)
            return value
    single = data.get("codigo_origen") or data.get("origin_code")
    if single:
        return [single]
    return []


def _union_payload(value):
    return {
        "id": str(value.id), "code": value.codigo, "codigo": value.codigo, "ciclo": value.ciclo, "estado": value.estado,
        "articulo_id": value.articulo_scm_id, "pieza": value.articulo.nombre if value.articulo else None,
        "pieza_codigo": value.pieza_color_sku,
        "color": value.color, "zona": value.zona_hallazgo,
        "bruto": _fmt(value.bruto_kg), "tara": _fmt(value.tara_kg), "neto": _fmt(value.neto_kg),
        "unidad_id": str(value.unidad_fisica_id),
        "origenes": [{"code": row.unidad_fisica.codigo if row.unidad_fisica else None, "unidad_id": str(row.unidad_fisica_id), "retiro_id": str(row.retiro_id) if row.retiro_id else None,
                      "lote_id": row.lote_id, "lote": row.lote_snapshot,
                      "ot_date": row.ot_date, "ot_shift": row.ot_shift,
                      "neto_retirado": _fmt(row.neto_retirado_kg),
                      "estado": row.estado} for row in value.origenes],
        "net_kg": _fmt(value.neto_kg),
        "created_at": _iso(value.created_at),
        "movimiento_id": str(value.movimiento_id) if value.movimiento_id else None,
        "existencia_id": str(value.existencia_id) if value.existencia_id else None,
    }


def _unit_label_payload(value, article=None):
    article = article or value.articulo
    piece_sku, color = _article_color(article) if article else (None, None)
    measured = value.kg_verificados if value.estado_logistico == "RECIBIDA_ALMACEN" else None
    return {"codigo": value.codigo, "pieza": article.nombre if article else None, "pieza_codigo": piece_sku, "color": color,
            "zona": None, "neto": _fmt(measured), "bruto": None, "tara": None}


def vista_state(session, actor_id):
    """Read-only station state: returnable units and received receipts."""
    _mesa_actor(session, actor_id)
    # Keep this endpoint below the station timeout even with a large backlog:
    # resolve live units, latest withdrawals, labels and original production
    # existences in bounded queries instead of querying each QR independently.
    live_units = session.scalars(select(ScmUnidadFisicaKg).where(
        ScmUnidadFisicaKg.estado == "ACTIVA",
        ScmUnidadFisicaKg.estado_logistico == "RETIRADA_ARMADO",
    ).options(
        selectinload(ScmUnidadFisicaKg.articulo)
        .selectinload(ScmArticulo.pieza_color)
        .selectinload(ScmArticuloPiezaColor.pieza_color)
        .selectinload(PiezaColor.color_produccion_rel)
    )).all()
    unit_ids = [unit.id for unit in live_units]
    latest_items = {}
    if unit_ids:
        items = session.scalars(select(ScmRetiroArmadoKgItem).where(
            ScmRetiroArmadoKgItem.unidad_id.in_(unit_ids)
        ).join(ScmRetiroArmadoKg).options(
            selectinload(ScmRetiroArmadoKgItem.retiro)
        ).order_by(ScmRetiroArmadoKgItem.unidad_id, ScmRetiroArmadoKg.created_at.desc(), ScmRetiroArmadoKg.id.desc())).all()
        for item in items:
            latest_items.setdefault(item.unidad_id, item)
    original_existence = {}
    if unit_ids:
        existences = session.scalars(select(ScmExistenciaMangaKg).where(
            ScmExistenciaMangaKg.unidad_fisica_kg_id.in_(unit_ids)
        ).options(
            selectinload(ScmExistenciaMangaKg.manga).selectinload(ScmManga.lote_articulo),
            selectinload(ScmExistenciaMangaKg.manga).selectinload(ScmManga.ot),
        ).order_by(ScmExistenciaMangaKg.unidad_fisica_kg_id,
                   case((ScmExistenciaMangaKg.origen_tipo == "PRODUCCION", 0), else_=1),
                   ScmExistenciaMangaKg.recibida_at.asc())).all()
        for existence in existences:
            original_existence.setdefault(existence.unidad_fisica_kg_id, existence)
    aliases = _aliases_by_unit(session, unit_ids)
    manga_aliases = _manga_aliases_by_id(session, [item.manga_id for item in original_existence.values() if item.manga_id is not None])
    units = []
    for unit in sorted(live_units, key=lambda value: value.codigo):
        item = latest_items.get(unit.id)
        article = unit.articulo
        if item is None or article is None or article.clase != "PIEZA_COLOR" or article.unidad_inventario != "KG":
            continue
        piece_sku, color = _article_color(article)
        existence = original_existence.get(unit.id)
        manga = existence.manga if existence is not None else None
        lote = getattr(getattr(manga, "lote_articulo", None), "codigo", None) if manga is not None else None
        ot = getattr(manga, "ot", None) if manga is not None else None
        ot_date = getattr(ot, "fecha", None)
        ot_date = ot_date.isoformat() if ot_date is not None else None
        current_aliases = manga_aliases.get(manga.id, []) if manga is not None else []
        current_aliases.extend(aliases.get(unit.id, []))
        units.append({"code": unit.codigo, "codigo": unit.codigo, "codigo_humano": unit.codigo,
                      "aliases": list(dict.fromkeys([unit.codigo, *current_aliases])), "cycle_id": str(item.retiro_id),
                      "unidad_id": str(unit.id), "articulo_id": unit.articulo_scm_id,
                      "piece": article.nombre, "pieza": article.nombre, "pieza_codigo": piece_sku, "color": color,
                      "lot": lote, "status": "available", "estado": "DISPONIBLE", "origins": [],
                      "ot_date": ot_date, "ot_shift": getattr(ot, "turno", None),
                      "kg_pendiente": f"{Decimal(item.neto_entregado_kg):.3f}", "retiro_id": str(item.retiro_id)})
    all_unions = session.scalars(select(ScmMesaRetornoUnion).options(
        selectinload(ScmMesaRetornoUnion.articulo),
        selectinload(ScmMesaRetornoUnion.unidad_fisica),
        selectinload(ScmMesaRetornoUnion.origenes).selectinload(ScmMesaRetornoOrigen.unidad_fisica),
    ).order_by(ScmMesaRetornoUnion.created_at.desc(), ScmMesaRetornoUnion.ciclo.desc())).all()
    received_unions = [item for item in all_unions if item.estado == "RECIBIDA"]
    receipts = [{"code": item.codigo, "codigo": item.codigo, "cycle_id": item.ciclo,
                 "net_kg": _fmt(item.neto_kg),
                 "created_at": _iso(item.created_at), "id": str(item.id),
                 "origins": [row.unidad_fisica.codigo if row.unidad_fisica else str(row.unidad_fisica_id) for row in item.origenes]}
                for item in received_unions]
    pending_unions = [item for item in all_unions if item.estado != "RECIBIDA"]
    pending = [_union_payload(item) for item in pending_unions]
    for item in pending_unions:
        first_origin = item.origenes[0] if item.origenes else None
        units.append({"code": item.codigo, "codigo": item.codigo, "codigo_humano": item.codigo,
                      "aliases": _unit_aliases(session, item.unidad_fisica), "cycle_id": str(item.id),
                      "piece": item.articulo.nombre if item.articulo else None,
                      "pieza": item.articulo.nombre if item.articulo else None, "pieza_codigo": item.pieza_color_sku,
                      "color": item.color, "lot": first_origin.lote_snapshot if first_origin else None,
                      "status": "pending", "estado": item.estado,
                      "origins": [row.unidad_fisica.codigo if row.unidad_fisica else str(row.unidad_fisica_id) for row in item.origenes],
                      "ot_date": first_origin.ot_date if first_origin else None,
                      "ot_shift": first_origin.ot_shift if first_origin else None})
    return {"units": units, "receipts": receipts, "pending_unions": pending}


def label(session, actor_id, codigo):
    _mesa_actor(session, actor_id)
    code = _text(codigo, "codigo")
    pending = session.scalar(select(ScmMesaRetornoUnion).where(ScmMesaRetornoUnion.codigo == code).order_by(ScmMesaRetornoUnion.ciclo.desc()))
    if pending is not None:
        return {"codigo": pending.codigo, "pieza": pending.articulo.nombre if pending.articulo else None, "pieza_codigo": pending.pieza_color_sku, "color": pending.color,
                "zona": pending.zona_hallazgo, "neto": _fmt(pending.neto_kg), "bruto": _fmt(pending.bruto_kg), "tara": _fmt(pending.tara_kg)}
    unit = _unit_by_code(session, code)
    if unit is None:
        raise ScmServiceError("QR_NOT_FOUND", "La identidad de retorno no existe.", status_code=404)
    return _unit_label_payload(unit)


def _create_union(session, actor_id, station_id, data, operation_id):
    actor = _mesa_actor(session, actor_id)
    acquire_kg_productive_write_lock(session)
    station_value = _assert_station(session, station_id)
    origins = _origin_items(data)
    if len(origins) < 2:
        raise ScmServiceError("ORIGINS_REQUIRED", "Escanea al menos dos mangas para unirlas.", status_code=422)
    codes = []
    label_ids = {}
    for item in origins:
        code = _text(_source_code(item), "origen")
        if code in codes:
            raise ScmServiceError("DUPLICATE_ORIGIN", "El mismo QR solo puede vincularse una vez.", status_code=409)
        codes.append(code)
        label_ids[code] = _source_label_id(item, data)
    normalized_origins = sorted(codes)
    if any(label_ids.values()):
        normalized_origins = [{"code": code, "label_id": str(label_ids[code]) if label_ids[code] else None} for code in sorted(codes)]
    normalized = {"station_id": station_value, "origenes": normalized_origins, "zona": data.get("zona")}
    op_id, replay, status = _operation(session, operation_id, "POST /unions", actor.id, normalized)
    if replay is not None:
        return replay, status
    resolved = []
    for code in codes:
        unit = _unit_by_code(session, code)
        _assert_current_label(session, unit, label_ids.get(code))
        source = _source_identity(session, unit, lock=True)
        if any(origin["unit"].id == unit.id for origin in resolved):
            raise ScmServiceError("DUPLICATE_ORIGIN", "Los dos códigos identifican la misma manga.", status_code=409)
        resolved.append(source)
    first = resolved[0]
    for source in resolved[1:]:
        if source["article"].id != first["article"].id or source["piece_sku"] != first["piece_sku"] or source["color"] != first["color"]:
            raise ScmServiceError("ORIGIN_IDENTITY_MISMATCH", "No se puede unir: pieza y color deben coincidir", status_code=409)
    code = f"UNION-{op_id.hex[:12].upper()}"
    if session.scalar(select(ScmMesaRetornoUnion.id).where(ScmMesaRetornoUnion.codigo == code)) is not None:
        raise ScmServiceError("IDENTITY_CONFLICT", "El código de retorno ya existe.", status_code=409)
    unit = ScmUnidadFisicaKg(public_id=uuid.uuid4(), codigo=code, articulo_scm_id=first["article"].id,
                             unidad_raiz_id=None, intencion="RETORNO", estado="ACTIVA",
                             estado_logistico="PENDIENTE_VERIFICACION", estado_calidad="SIN_CONTROL",
                             tenedor_fisico_id=actor.id)
    session.add(unit)
    session.flush()
    unit.unidad_raiz_id = unit.id
    from app.models.scm_inventory_kg import ScmEtiquetaUnidadKg
    label_id = uuid.uuid4()
    label = {"v": 1, "label_id": str(label_id), "qr_value": json.dumps({"v": 1, "label_id": str(label_id)}, separators=(",", ":"))}
    session.add(ScmEtiquetaUnidadKg(public_id=label_id, unidad_id=unit.id, payload_json=label, payload_hash=_hash(label), estado="GENERADA"))
    union = ScmMesaRetornoUnion(codigo=code, articulo_scm_id=first["article"].id, pieza_color_sku=first["piece_sku"],
                                color=first["color"], zona_hallazgo=data.get("zona"), unidad_fisica_id=unit.id,
                                station_id=station_value, actor_id=actor.id, operation_id=op_id)
    session.add(union)
    session.flush()
    for source in resolved:
        source["unit"].estado_logistico = "REVERSADA"
        source["unit"].estado = "HISTORICA"
        source["unit"].intencion = "RETORNO"
        source["unit"].version += 1
        session.add(ScmMesaRetornoOrigen(union_id=union.id, unidad_fisica_id=source["unit"].id,
                                         ciclo=union.ciclo,
                                         retiro_id=source["item"].retiro_id, lote_id=source["lote_id"],
                                         lote_snapshot=source["lote_snapshot"], neto_retirado_kg=source["kg"],
                                         ot_date=source["ot_date"], ot_shift=source["ot_shift"],
                                         pieza_color_sku=source["piece_sku"], color=source["color"],
                                         actor_id=actor.id))
        source["item"].retiro.estado = "CONCILIACION_PENDIENTE"
    session.flush()
    return _complete(session, op_id, _union_payload(union), 201)


def _capture_values(data, union):
    capture = data.get("captura")
    if not isinstance(capture, dict):
        raise ScmServiceError("CAPTURE_REQUIRED", "El retorno requiere captura atestada por el edge.", status_code=409)
    token = _text(capture.get("token") or data.get("request_id"), "captura.token")
    if capture.get("codigo") and str(capture["codigo"]) != union.codigo:
        raise ScmServiceError("CAPTURE_IDENTITY_CONFLICT", "La captura pertenece a otra identidad.", status_code=409)
    base = str(capture.get("base") or "").upper()
    if base not in {"NT", "GS"}:
        raise ScmServiceError("CAPTURE_INVALID", "La base física debe ser NT o GS.", status_code=422)
    measured = _decimal(capture.get("kg"), "captura.kg", positive=True)
    tare = _decimal(data.get("tara", capture.get("tara", 0)), "tara", non_negative=True)
    gross = _decimal(data.get("bruto", capture.get("bruto", measured if base == "GS" else measured + tare)), "bruto", positive=True)
    net = measured if base == "NT" else gross - tare
    if net <= 0 or gross <= tare:
        raise ScmServiceError("INVALID_NET", "Se requiere NET positivo y bruto mayor que tara.", status_code=422)
    if base == "GS" and measured != gross:
        raise ScmServiceError("CAPTURE_WEIGHT_CONFLICT", "El bruto no coincide con la lectura atestada.", status_code=409)
    raw_at = capture.get("captured_at")
    try:
        captured_at = datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ScmServiceError("CAPTURE_INVALID", "captured_at debe ser una fecha ISO válida.", status_code=422) from error
    if captured_at.tzinfo is None:
        raise ScmServiceError("CAPTURE_INVALID", "captured_at debe incluir zona horaria.", status_code=422)
    return token, base, measured, gross, tare, net, captured_at


def _receive(session, actor_id, station_id, data, operation_id):
    actor = _mesa_actor(session, actor_id)
    acquire_kg_productive_write_lock(session)
    station_value = _assert_station(session, station_id)
    code = _text(data.get("codigo") or data.get("code") or data.get("codigo_retorno"), "codigo")
    label_id = _source_label_id(data)
    union = session.scalar(select(ScmMesaRetornoUnion).where(ScmMesaRetornoUnion.codigo == code).order_by(ScmMesaRetornoUnion.ciclo.desc()).with_for_update())
    simple_source = None
    previous_cycle = union.ciclo if union is not None else 0
    candidate = None
    if union is not None and union.estado == "RECIBIDA":
        # A received identity can be withdrawn again.  A later return is a
        # new cycle with the same QR, never a second credit on the old receipt.
        candidate = _unit_by_code(session, code)
        if candidate is not None and candidate.estado == "ACTIVA" and candidate.estado_logistico == "RETIRADA_ARMADO":
            union = None
    class _Code:
        pass
    marker = _Code()
    marker.codigo = code
    capture_target = union if union is not None else marker
    token, base, measured, gross, tare, net, captured_at = _capture_values(data, capture_target)
    if data.get("net_kg") is not None and _decimal(data.get("net_kg"), "net_kg", positive=True) != net:
        raise ScmServiceError("CAPTURE_WEIGHT_CONFLICT", "net_kg no coincide con la captura atestada.", status_code=409)
    op_id, replay, status = _operation(session, operation_id, "POST /receive", actor.id,
                                       {"codigo": code, "label_id": str(label_id) if label_id else None,
                                        "captura": {"token": token, "base": base, "kg": str(measured), "captured_at": captured_at.isoformat()}})
    if replay is not None:
        return replay, status
    if union is None:
        # A complete manga may return in its original bag.  It does not need a
        # new printed identity, but its scanned PREPESAJE must still be current.
        source_unit = candidate if candidate is not None and candidate.estado == "ACTIVA" and candidate.estado_logistico == "RETIRADA_ARMADO" else _unit_by_code(session, code)
        _assert_current_label(session, source_unit, label_id)
        simple_source = _source_identity(session, source_unit, lock=True)
    if union is None:
        union = ScmMesaRetornoUnion(
            codigo=code, articulo_scm_id=simple_source["article"].id,
            pieza_color_sku=simple_source["piece_sku"], color=simple_source["color"],
            zona_hallazgo=data.get("zona"), unidad_fisica_id=simple_source["unit"].id,
            ciclo=previous_cycle + 1, station_id=station_value, actor_id=actor.id, operation_id=op_id,
        )
        session.add(union)
        session.flush()
        session.add(ScmMesaRetornoOrigen(
            union_id=union.id, unidad_fisica_id=simple_source["unit"].id,
            ciclo=union.ciclo,
            retiro_id=simple_source["item"].retiro_id,
            lote_id=simple_source["lote_id"], lote_snapshot=simple_source["lote_snapshot"],
            ot_date=simple_source["ot_date"], ot_shift=simple_source["ot_shift"],
            neto_retirado_kg=simple_source["kg"], pieza_color_sku=simple_source["piece_sku"],
            color=simple_source["color"], actor_id=actor.id,
        ))
        session.flush()
    elif union.actor_id != actor.id or union.station_id != station_value:
        raise ScmServiceError("RETURN_NOT_FOUND", "La unión no pertenece a la cuenta o estación autenticada.", status_code=404)
    if union.estado == "RECIBIDA":
        raise ScmServiceError("RETURN_ALREADY_RECEIVED", "El retorno ya fue recibido.", status_code=409)
    if session.scalar(select(ScmMesaRetornoRecibo.id).where(ScmMesaRetornoRecibo.captura_token == token)) is not None:
        raise ScmServiceError("CAPTURE_ALREADY_USED", "La captura ya fue usada por un retorno.", status_code=409)
    max_pending = sum((Decimal(origin.neto_retirado_kg) for origin in union.origenes), Decimal("0"))
    if net > max_pending:
        raise ScmServiceError("RETURN_EXCEEDS_PENDING", "El NET excede el máximo pendiente de los orígenes.", status_code=409,
                              details={"max_kg": f"{max_pending:.3f}", "net_kg": f"{net:.3f}"})
    unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == union.unidad_fisica_id).with_for_update())
    location_code = "PRODUCCION_KG"
    location = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == location_code,
        ScmUbicacionInventario.activo.is_(True),
        ScmUbicacionInventario.permite_saldo_libre.is_(True),
    ).with_for_update())
    if location is None:
        raise ScmServiceError("LOCATION_NOT_FOUND", "La ubicación canónica PRODUCCION_KG no existe.", status_code=404)
    assert_location_scope(session, actor_id=actor.id, location=location, article_class="PIEZA_COLOR")
    unit.almacen_responsable_id = location.almacen_id
    balance = session.scalar(select(ScmSaldoInventarioKg).where(
        ScmSaldoInventarioKg.articulo_scm_id == union.articulo_scm_id,
        ScmSaldoInventarioKg.ubicacion_id == location.id,
    ).with_for_update())
    if balance is None:
        balance = ScmSaldoInventarioKg(articulo_scm_id=union.articulo_scm_id, ubicacion_id=location.id)
        session.add(balance)
        session.flush()
    balance.cantidad_fisica_kg = Decimal(balance.cantidad_fisica_kg or 0) + net
    balance.version += 1
    movement = ScmMovimientoInventarioKg(saldo_id=balance.id, tipo="RETORNO_ENTRADA", cantidad_delta_kg=net,
        saldo_fisico_resultante_kg=balance.cantidad_fisica_kg, motivo="Retorno Mesa Estación 2 medido",
        referencia_tipo="MESA_RETORNO", referencia_id=str(union.id), actor_id=actor.id, operation_id=op_id,
        projection_sha256=_hash({"union": str(union.id), "token": token}), peso_neto_snapshot_kg=net,
        pesada_at_snapshot=captured_at, fuente_tipo="RETORNO_MESA", atributo_proceso="PROCESO")
    session.add(movement)
    session.flush()
    existence = ScmExistenciaMangaKg(manga_id=None, sesion_id=None, etiqueta_resuelta_id=None,
        unidad_fisica_kg_id=unit.id, articulo_scm_id=union.articulo_scm_id, saldo_id=balance.id,
        ubicacion_id=location.id, movimiento_ingreso_id=movement.id, operation_id=op_id,
        resuelta_por="MESA_RETORNO", estado_logistico="RECIBIDA_ALMACEN", estado_calidad="SIN_CONTROL",
        cantidad_fisica_kg=net, peso_neto_snapshot_kg=net, pesaje_public_id=None,
        projection_sha256=None, pesada_at_snapshot=captured_at, recibida_por_id=actor.id,
        origen_tipo="RETORNO", atributo_proceso="PROCESO")
    session.add(existence)
    session.flush()
    unit.saldo_id, unit.ubicacion_id = balance.id, location.id
    unit.recepcion_vigente_id, unit.kg_verificados = existence.id, net
    unit.kg_verificados_at, unit.estado_logistico = captured_at, "RECIBIDA_ALMACEN"
    unit.version += 1
    union.estado, union.bruto_kg, union.tara_kg, union.neto_kg = "RECIBIDA", gross, tare, net
    union.captura_token, union.captura_base, union.captura_at = token, base, captured_at
    union.captura_reading_id = str(data.get("reading_id") or capture_id(token))
    union.captura_payload_hash = _hash(data.get("captura"))
    union.movimiento_id, union.existencia_id = movement.id, existence.id
    for origin in union.origenes:
        origin.estado, origin.terminal_at = "VACIADO_TERMINAL", _now()
    session.add(ScmMesaRetornoRecibo(union_id=union.id, unidad_fisica_id=unit.id, movimiento_id=movement.id,
                                     existencia_id=existence.id, captura_token=token, neto_kg=net,
                                     ciclo=union.ciclo, actor_id=actor.id, operation_id=op_id))
    session.flush()
    payload = {"code": union.codigo, "codigo": union.codigo, "cycle_id": union.ciclo, "estado": union.estado,
               "net_kg": f"{net:.3f}", "neto": f"{net:.3f}",
               "movimiento_id": str(movement.id), "existencia_id": str(existence.id),
               "unidad_id": str(unit.public_id), "operation_id": str(op_id), "tipo_movimiento": "RETORNO_ENTRADA"}
    return _complete(session, op_id, payload, 201)


def capture_id(token):
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def dispatch(session, actor_id, station_id, path, data, operation_id):
    """Dispatch station commands without accepting client actor/location scope."""
    try:
        relative = "/" + str(path or "").strip("/")
        if relative == "/unions":
            return _create_union(session, actor_id, station_id, data if isinstance(data, dict) else {}, operation_id or uuid.uuid4())
        if relative == "/receive":
            return _receive(session, actor_id, station_id, data if isinstance(data, dict) else {}, operation_id or uuid.uuid4())
    except ScmServiceError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    raise ScmServiceError("MESA_ROUTE_NOT_FOUND", "Operación de retornos no reconocida.", status_code=404)


__all__ = ["dispatch", "label", "vista_state", "STATION_ID"]
