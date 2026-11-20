"""Central adapter for the station-2 physical inventory campaign.

The station edge and this service intentionally share the same small JSON
contract. Observations are evidence only; they never write the central KG
saldo. A later, explicit adjustment command creates a local audited movement
and is guarded by ``INVENTARIO_AJUSTAR``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.scm_articulos import ScmArticulo, ScmArticuloPiezaColor
from app.models.producto import PiezaColor
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_inventory_kg import ScmUnidadFisicaKg
from app.models.scm_inventory_kg import (
    ScmEtiquetaUnidadKg,
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmReservaUnidadKg,
    ScmRetiroArmadoKg,
    ScmRetiroArmadoKgItem,
    ScmSaldoInventarioKg,
)
from app.models.scm_mesa_inventory import (
    ScmMesaInventarioCampana,
    ScmMesaInventarioLectura,
    ScmMesaInventarioManga,
    ScmMesaInventarioMovimiento,
    ScmMesaInventarioPesaje,
    ScmMesaInventarioRevision,
    ScmMesaInventarioSesion,
    ScmMesaInventarioSolicitud,
    ScmMesaInventarioNoHabida,
)
from app.models.scm_ot import ScmEtiquetaManga
from app.models.scm_auditoria import ScmOperacion
from app.services.scm_service_support import (
    ScmServiceError, acquire_kg_productive_write_lock, load_actor, load_actor_any, required_text,
)
from app.services.scm_warehouse_scope_service import assert_location_scope


QUANTUM = Decimal("0.001")
STATION_ID = "ESTACION-2"
SALDO_KEY = "PRODUCCION_KG"
ZONES = (
    ("Z-ALMACEN-PIEZAS", "Almacén de piezas general"),
    ("Z-ALMACEN-ARMADO-3P", "Almacén de armado – 3.er piso"),
    ("Z-ALMACEN-LLANTAS-3P", "Almacén de llantas – 3.er piso"),
    ("Z-ALMACEN-ARMADO-4P", "Almacén de armado – 4.º piso"),
)
ZONE_IDS = {item[0] for item in ZONES}


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _after(value, cutoff):
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return value > cutoff


def _at_or_after(value, cutoff):
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return value >= cutoff


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _text(value, field, *, max_length=120, required=True):
    if not required and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    return required_text(value, field=field, max_length=max_length)


def _decimal(value, field, *, positive=False, non_negative=False):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError("INVALID_DECIMAL", f"El campo {field} debe ser decimal.", status_code=422, details={"field": field}) from error
    if not parsed.is_finite() or (positive and parsed <= 0) or (non_negative and parsed < 0):
        raise ScmServiceError("INVALID_DECIMAL", f"El campo {field} no es válido.", status_code=422, details={"field": field})
    rounded = parsed.quantize(QUANTUM)
    if parsed != rounded:
        raise ScmServiceError("INVALID_DECIMAL_SCALE", f"El campo {field} admite como máximo tres decimales.", status_code=422, details={"field": field})
    return rounded


def _uuid(value, field="operation_id"):
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError("INVALID_UUID", f"{field} debe ser UUID.", status_code=400) from error


def _actor(session, actor_id, capability="INVENTARIO_VER"):
    return load_actor(session, actor_id, capability=capability)


def _count_actor(session, actor_id):
    """Named counts are usable by the legacy station or the web C66 role."""
    return load_actor_any(session, actor_id, capabilities=("INVENTARIO_VER", "INVENTARIO_CONTEO_C66"))


def _assert_zone(session, actor_id, zone_id):
    zone_id = _text(zone_id, "zona", max_length=64)
    if zone_id not in ZONE_IDS:
        raise ScmServiceError("ZONE_NOT_FOUND", "La zona de hallazgo no existe.", status_code=404)
    # If the central catalog already maps this code to a location, enforce its
    # warehouse scope. The four pilot zones remain finding labels, not saldo
    # locations, when the catalog has no such row yet.
    location = session.scalar(select(ScmUbicacionInventario).where(ScmUbicacionInventario.codigo == zone_id))
    if location is not None:
        assert_location_scope(session, actor_id=actor_id, location=location)
    return zone_id


def _article(session, article_id):
    try:
        article_id = int(article_id)
    except (TypeError, ValueError) as error:
        raise ScmServiceError("ARTICLE_NOT_FOUND", "El artículo PIEZA_COLOR no existe.", status_code=404) from error
    article = session.get(ScmArticulo, article_id)
    if article is None or not article.activo or article.clase != "PIEZA_COLOR" or article.unidad_inventario != "KG":
        raise ScmServiceError("ARTICLE_NOT_FOUND", "El artículo PIEZA_COLOR no existe.", status_code=404)
    return article


def _piece_names(article):
    """Return the canonical selector name, never a SKU or fuzzy prefix."""
    values = set()
    variant = getattr(getattr(article, "pieza_color", None), "pieza_color", None)
    base = getattr(getattr(variant, "pieza_rel", None), "nombre", None)
    values.add(base or article.nombre)
    return {str(value).strip() for value in values if value}


def _named_articles(session, pieces):
    article_options = selectinload(ScmArticulo.pieza_color).selectinload(
        ScmArticuloPiezaColor.pieza_color
    ).options(
        selectinload(PiezaColor.pieza_rel),
        selectinload(PiezaColor.color_produccion_rel),
    )
    articles = session.scalars(select(ScmArticulo).options(article_options).where(
        ScmArticulo.activo.is_(True),
        ScmArticulo.clase == "PIEZA_COLOR",
        ScmArticulo.unidad_inventario == 'KG',
    ).order_by(ScmArticulo.id)).all()
    wanted = {str(piece).strip() for piece in pieces}
    selected = [article for article in articles if _piece_names(article) & wanted]
    matched = {name for name in wanted if any(name in _piece_names(article) for article in selected)}
    unknown = sorted(wanted - matched)
    if unknown:
        raise ScmServiceError(
            "PIECE_NOT_FOUND", "Una pieza no existe en el catálogo local.",
            status_code=422, details={"piezas": unknown},
        )
    return selected


def _snapshot_named_codes(session, article_ids, cutoff):
    """Freeze only active, positive KG identities at session creation."""
    if not article_ids:
        return []
    codes = set()
    rows = _eligible_named_units(session, article_ids=article_ids, cutoff=cutoff)
    codes.update(unit.codigo for unit in rows)
    return sorted(codes)


def _eligible_named_units(session, *, article_ids=None, codes=None, cutoff=None):
    location = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == SALDO_KEY,
        ScmUbicacionInventario.activo.is_(True),
    ))
    if location is None:
        return []
    query = select(ScmUnidadFisicaKg).join(
        ScmExistenciaMangaKg,
        ScmExistenciaMangaKg.unidad_fisica_kg_id == ScmUnidadFisicaKg.id,
    ).where(
        ScmUnidadFisicaKg.estado == "ACTIVA",
        ScmUnidadFisicaKg.estado_logistico.in_(("RECIBIDA_ALMACEN", "ALMACENADA_CONTROLADA", "DISPONIBLE_PRODUCCION")),
        ScmUnidadFisicaKg.ubicacion_id == location.id,
        ScmExistenciaMangaKg.ubicacion_id == location.id,
        ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
        ScmExistenciaMangaKg.cantidad_fisica_kg > 0,
    )
    if cutoff is not None:
        query = query.where(
            ScmUnidadFisicaKg.updated_at <= cutoff,
            ScmExistenciaMangaKg.recibida_at <= cutoff,
        )
    if article_ids is not None:
        query = query.where(ScmUnidadFisicaKg.articulo_scm_id.in_(article_ids))
    if codes is not None:
        query = query.where(ScmUnidadFisicaKg.codigo.in_(codes))
    # Do not DISTINCT the full ORM row: PostgreSQL cannot compare JSON
    # columns. The positive join already filters eligible rows; group only by
    # scalar identity/existence columns to avoid JSON equality.
    query = query.with_only_columns(
        ScmUnidadFisicaKg.id, ScmUnidadFisicaKg.public_id,
        ScmUnidadFisicaKg.codigo, ScmUnidadFisicaKg.articulo_scm_id,
        ScmUnidadFisicaKg.estado, ScmUnidadFisicaKg.estado_logistico,
        ScmUnidadFisicaKg.updated_at, ScmUnidadFisicaKg.kg_verificados,
        ScmUnidadFisicaKg.ubicacion_id, ScmExistenciaMangaKg.recibida_at,
    ).group_by(
        ScmUnidadFisicaKg.id, ScmUnidadFisicaKg.public_id,
        ScmUnidadFisicaKg.codigo, ScmUnidadFisicaKg.articulo_scm_id,
        ScmUnidadFisicaKg.estado, ScmUnidadFisicaKg.estado_logistico,
        ScmUnidadFisicaKg.updated_at, ScmUnidadFisicaKg.kg_verificados,
        ScmUnidadFisicaKg.ubicacion_id, ScmExistenciaMangaKg.recibida_at,
    ).order_by(ScmUnidadFisicaKg.codigo)
    return session.execute(query).all()


def _snapshot_named_balances(session, article_ids):
    location = session.scalar(select(ScmUbicacionInventario).where(ScmUbicacionInventario.codigo == SALDO_KEY))
    if location is None:
        return {}
    rows = session.scalars(select(ScmSaldoInventarioKg).where(
        ScmSaldoInventarioKg.articulo_scm_id.in_(article_ids),
        ScmSaldoInventarioKg.ubicacion_id == location.id,
    )).all()
    return {
        str(row.articulo_scm_id): {
            "id": str(row.id), "version": row.version,
            "cantidad_fisica_kg": _fmt(row.cantidad_fisica_kg),
        }
        for row in rows
    }


def _session_pieces(session_obj):
    value = session_obj.piezas_json
    if isinstance(value, dict):
        return list(value.get("names", []))
    return list(value or [])


def _is_named_session(session_obj):
    return bool(_session_pieces(session_obj))


def _session_zones(session_obj):
    value = session_obj.zonas_json or []
    return list(value) if isinstance(value, list) and value else [session_obj.zona_id]


def _named_pending(session, session_obj):
    expected = set(session_obj.esperadas_json or [])
    if not expected:
        return []
    found = set()
    rows = session.execute(select(ScmMesaInventarioManga.codigo, ScmMesaInventarioLectura.created_at).join(
        ScmMesaInventarioLectura,
        ScmMesaInventarioLectura.manga_id == ScmMesaInventarioManga.id,
    ).where(
        ScmMesaInventarioManga.codigo.in_(expected),
    )).all()
    cutoff = session_obj.corte_at or session_obj.created_at
    found.update(row.codigo for row in rows if _at_or_after(row.created_at, cutoff))
    eligible = set()
    eligible.update(unit.codigo for unit in _eligible_named_units(session, codes=expected, cutoff=session_obj.corte_at))
    excluded = set(session.scalars(select(ScmMesaInventarioManga.codigo).join(
        ScmMesaInventarioNoHabida,
        ScmMesaInventarioNoHabida.manga_id == ScmMesaInventarioManga.id,
    ).where(
        ScmMesaInventarioNoHabida.sesion_id == session_obj.id,
    )).all())
    return sorted(eligible - found - excluded)


def _lock_count_session(session, actor_id, session_id):
    """Lock campaign before session, the common order for count mutations."""
    unlocked = _session(session, actor_id, session_id, lock=False)
    session.get(ScmMesaInventarioCampana, unlocked.campana_id, with_for_update=True)
    return _session(session, actor_id, session_id, lock=True)


def _lock_named_identities(session, codes):
    if codes:
        session.scalars(select(ScmUnidadFisicaKg).where(
            ScmUnidadFisicaKg.codigo.in_(sorted(set(codes))),
        ).order_by(ScmUnidadFisicaKg.codigo).with_for_update()).all()


def _campaign(session, actor_id):
    campaign = session.scalar(select(ScmMesaInventarioCampana).order_by(ScmMesaInventarioCampana.created_at).limit(1).with_for_update())
    if campaign is None:
        campaign = ScmMesaInventarioCampana(codigo="MESA-INV-001", creado_por_id=actor_id)
        session.add(campaign)
        session.flush()
    if campaign.estado != "ABIERTA":
        raise ScmServiceError("CAMPAIGN_CLOSED", "La campaña de inventario ya está cerrada.", status_code=409)
    return campaign


def _require_complete_walk(session):
    """Require a closed walk for every physical pilot zone.

    Boolean flags from the client are not evidence of coverage.  A missing
    zone or an open session keeps the review/faltante command blocked.
    """
    campaign = session.scalar(
        select(ScmMesaInventarioCampana).order_by(ScmMesaInventarioCampana.created_at).limit(1)
    )
    if campaign is None:
        raise ScmServiceError("WALK_INCOMPLETE", "El recorrido de las cuatro zonas aún no está cerrado.", status_code=409)
    sessions = session.scalars(
        select(ScmMesaInventarioSesion).where(ScmMesaInventarioSesion.campana_id == campaign.id)
    ).all()
    covered = {item.zona_id for item in sessions if item.estado == "CERRADA" and item.cobertura_confirmada}
    if covered != ZONE_IDS or any(item.estado != "CERRADA" for item in sessions):
        raise ScmServiceError("WALK_INCOMPLETE", "Debe cerrar y confirmar cobertura explícita de las cuatro zonas antes de confirmar faltantes.", status_code=409)


def _require_movement_gate():
    """Keep central Kardex writes behind the same pilot activation gate.

    The review queue is read-only and remains available while the gate is
    closed. A missing confirmation is a real stock mutation, so reject it
    before creating an idempotency record or changing any state.
    """
    if os.environ.get("SCM_MESA_MOVIMIENTOS_HABILITADOS") != "1":
        raise ScmServiceError(
            "MOVEMENTS_DISABLED",
            "Los ajustes de inventario están cerrados; no se puede confirmar el faltante todavía.",
            status_code=409,
            details={"habilitado": False},
        )


def _invalidate_active_reviews(session, code):
    """Invalidate review tokens when new evidence changes their basis."""
    rows = session.scalars(select(ScmMesaInventarioRevision).where(
        ScmMesaInventarioRevision.codigo == code,
        ScmMesaInventarioRevision.estado == "ACTIVA",
    )).all()
    for row in rows:
        row.estado = "INVALIDADA"
        row.used_at = _now()


def _require_review_scope(session, manga):
    """Require four-zone evidence for this manga's piece only.

    A count for Tapa cannot certify Asa. A named count with explicit coverage
    each closed count contributes only its own zone. Open counts for the same
    piece remain a hard block. Historical unnamed counts are considered only
    when they explicitly declared coverage and contain a reading for this
    identity.
    """
    names = _piece_names(manga.articulo)
    if not names:
        return
    relevant_sessions = _review_sessions_for_manga(session, manga)
    if any(item.estado != "CERRADA" for item in relevant_sessions):
        raise ScmServiceError(
            "WALK_INCOMPLETE",
            "Hay un conteo abierto para la pieza; ciérrelo antes de revisar faltantes.",
            status_code=409,
        )
    covered = set()
    for item in relevant_sessions:
        if item.estado == "CERRADA" and item.cobertura_confirmada:
            covered.add(item.zona_id)
    missing_zones = sorted(set(ZONE_IDS) - covered)
    if missing_zones:
        raise ScmServiceError(
            "WALK_INCOMPLETE",
            "El conteo de la pieza no cubre las cuatro zonas.",
            status_code=409,
            details={"zonas_faltantes": missing_zones, "piezas": sorted(names)},
        )


def _review_sessions_for_manga(session, manga, *, exclude_id=None):
    names = _piece_names(manga.articulo)
    rows = session.scalars(select(ScmMesaInventarioSesion)).all()
    relevant = []
    for item in rows:
        if item.id == exclude_id:
            continue
        pieces = _session_pieces(item)
        if names.intersection(pieces):
            relevant.append(item)
            continue
        # Named counts without a piece snapshot retain their frozen expected
        # identity list. Historical unnamed sessions are narrower: they can
        # contribute only with explicit coverage and a linked reading.
        if not pieces and manga.codigo in (item.esperadas_json or []):
            if item.nombre is not None or (
                item.cobertura_confirmada and session.scalar(select(ScmMesaInventarioLectura.id).where(
                    ScmMesaInventarioLectura.sesion_id == item.id,
                    ScmMesaInventarioLectura.manga_id == manga.id,
                )) is not None
            ):
                relevant.append(item)
                continue
        if not pieces and item.nombre is None and item.cobertura_confirmada and session.scalar(select(ScmMesaInventarioLectura.id).where(
            ScmMesaInventarioLectura.sesion_id == item.id,
            ScmMesaInventarioLectura.manga_id == manga.id,
        )) is not None:
            relevant.append(item)
    return relevant


def _canonical_inventory_snapshot(session, manga, *, lock=False):
    """Resolve the active canonical existence and balance for a Mesa shadow."""
    location = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == SALDO_KEY,
        ScmUbicacionInventario.activo.is_(True),
    ))
    if location is None or not manga.unidad_fisica_id:
        return None, None
    existence = _active_existence(session, manga.unidad_fisica_id, lock=lock)
    if existence is None or existence.ubicacion_id != location.id or existence.articulo_scm_id != manga.articulo_id:
        return None, None
    balance = session.get(ScmSaldoInventarioKg, existence.saldo_id, with_for_update=lock)
    if balance is None or balance.ubicacion_id != location.id or balance.articulo_scm_id != manga.articulo_id:
        return existence, None
    return existence, balance


def _production_existence(session, unit):
    if unit.estado != "ACTIVA" or unit.estado_logistico not in {
        "RECIBIDA_ALMACEN", "ALMACENADA_CONTROLADA", "DISPONIBLE_PRODUCCION",
    }:
        return None
    location = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == SALDO_KEY,
        ScmUbicacionInventario.activo.is_(True),
    ))
    if location is None or unit.ubicacion_id != location.id:
        return None
    return session.scalar(select(ScmExistenciaMangaKg).where(
        ScmExistenciaMangaKg.unidad_fisica_kg_id == unit.id,
        ScmExistenciaMangaKg.ubicacion_id == location.id,
        ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
    ).order_by(ScmExistenciaMangaKg.recibida_at.desc()))


def _hydrate_manga_from_own_existence(session, manga, unit):
    """Refresh only an observational shadow when its own unit returns to KG."""
    existence = _production_existence(session, unit)
    if manga.neto_kg is None and existence is not None and existence.cantidad_fisica_kg is not None and Decimal(existence.cantidad_fisica_kg) > 0:
        manga.kardex_existence_id = existence.id
        manga.neto_kg = Decimal(existence.cantidad_fisica_kg).quantize(QUANTUM)
        if manga.estado == "HALLAZGO":
            manga.estado = "DISPONIBLE"
    elif manga.neto_kg is None and manga.estado == "DISPONIBLE":
        manga.estado = "HALLAZGO"
    return existence


def _manga_by_code(session, code, *, lock=False, require_inventory=False):
    code = _text(code, "codigo", max_length=120)
    query_manga = select(ScmMesaInventarioManga).where(ScmMesaInventarioManga.codigo == code)
    if lock:
        query_manga = query_manga.with_for_update()
    manga = session.scalar(query_manga)
    if manga is not None:
        # A materialized shadow used to return before the canonical resolver,
        # which let a typed OF/KG-OF code bypass the PREPESAJE validity checks
        # already applied to the printed JSON.  BAJA_COMPROBADA is deliberately
        # exempt: its recovery path uses the persisted identity and must remain
        # available for a documented reappearance/reweighing.
        reappearance = _has_baja_reappearance_evidence(session, manga)
        if _typed_unit_code(code) and not reappearance:
            _resolve_unit(session, code, lock=lock)
        elif reappearance and _typed_unit_code(code):
            _validate_reappearance_shadow_label(session, manga)
        if manga.estado in {"HALLAZGO", "DISPONIBLE"} and manga.neto_kg is None and manga.unidad_fisica_id:
            unit = session.get(ScmUnidadFisicaKg, manga.unidad_fisica_id, with_for_update=lock)
            if unit is not None:
                existence = _hydrate_manga_from_own_existence(session, manga, unit)
                if require_inventory and (existence is None or manga.kardex_existence_id is None or manga.neto_kg is None):
                    raise ScmServiceError("IDENTITY_NOT_ELIGIBLE", "La identidad no tiene existencia vigente en PRODUCCION_KG.", status_code=409)
        return manga
    try:
        unit = _resolve_unit(session, code, lock=lock)
    except ScmServiceError as error:
        # A documented BAJA_COMPROBADA reappearance is the one historical
        # exception: recover its existing shadow using the current PREPESAJE
        # label, for both JSON and typed input. Other historical identities
        # continue to surface the canonical rejection unchanged.
        if error.code in {"KG_UNIT_HISTORICAL", "KG_IDENTITY_MISSING"}:
            recovery = _baja_reappearance_shadow(session, code, lock=lock)
            if recovery is not None:
                return recovery
        raise
    if unit is None:
        raise ScmServiceError("QR_NOT_FOUND", "El QR o código no corresponde a una identidad global.", status_code=404)
    existing_by_unit = session.scalar(select(ScmMesaInventarioManga).where(
        ScmMesaInventarioManga.unidad_fisica_id == unit.id
    ))
    if existing_by_unit is not None:
        # Repair a legacy shadow only from this unit's own active
        # PRODUCCION_KG existence; never borrow another unit's balance.
        existence = _hydrate_manga_from_own_existence(session, existing_by_unit, unit)
        if require_inventory and (existence is None or existing_by_unit.kardex_existence_id is None or existing_by_unit.neto_kg is None):
            raise ScmServiceError("IDENTITY_NOT_ELIGIBLE", "La identidad no tiene existencia vigente en PRODUCCION_KG.", status_code=409)
        return existing_by_unit
    existence = _production_existence(session, unit)
    if require_inventory and (existence is None or existence.cantidad_fisica_kg is None or Decimal(existence.cantidad_fisica_kg) <= 0):
        raise ScmServiceError("IDENTITY_NOT_ELIGIBLE", "La identidad no tiene existencia vigente en PRODUCCION_KG.", status_code=409)
    if existence is None or existence.cantidad_fisica_kg is None or Decimal(existence.cantidad_fisica_kg) <= 0:
        # Read-only scans may record a finding for a unit in custody or a
        # different location, but cannot activate it as inventory.
        manga_state = "HALLAZGO"
        net = None
        existence_id = None
    else:
        manga_state = "DISPONIBLE"
        net = Decimal(existence.cantidad_fisica_kg).quantize(QUANTUM)
        existence_id = existence.id
    manga = ScmMesaInventarioManga(
        codigo=unit.codigo,
        unidad_fisica_id=unit.id,
        articulo_id=unit.articulo_scm_id,
        estado=manga_state,
        kardex_existence_id=existence_id,
        neto_kg=net,
    )
    session.add(manga)
    session.flush()
    return manga


def _typed_unit_code(value):
    """Whether ``value`` is a human-entered OF identity, not a legacy alias."""
    normalized = str(value or "").strip().upper()
    if normalized.startswith("KG-"):
        normalized = normalized[3:]
    return normalized.startswith("OF")


def _has_baja_reappearance_evidence(session, manga):
    """Require the persisted negative FALTANTE movement for recovery."""
    if (
        manga.baja_motivo != "FALTANTE"
        or manga.baja_movimiento_id is None
        or manga.estado not in {
            "BAJA_COMPROBADA",
            "PENDIENTE_PESAJE",
            "PESADA_PENDIENTE_CONFIRMACION",
        }
    ):
        return False
    movement = session.get(ScmMesaInventarioMovimiento, manga.baja_movimiento_id)
    return (
        movement is not None
        and movement.tipo == "AJUSTE_FALTANTE"
        and Decimal(movement.delta_kg) < 0
    )


def _baja_reappearance_shadow(session, value, *, lock=False):
    """Resolve only an existing BAJA_COMPROBADA shadow for reappearance."""
    raw = str(value or "").strip()
    label_id = None
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, dict) and decoded.get("v") == 1:
            label_id = uuid.UUID(str(decoded.get("label_id")))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        if raw.upper().startswith("L1|"):
            try:
                label_id = uuid.UUID(raw[3:].strip())
            except (TypeError, ValueError):
                return None
    from app.services.scm_kg_custody_service import _unit_by_code as canonical_resolver

    # The canonical resolver intentionally rejects historical units. Resolve
    # the underlying identity directly only to inspect an already materialized
    # BAJA_COMPROBADA shadow; no new shadow is created by this escape hatch.
    if label_id is not None:
        scanned = session.scalar(select(ScmEtiquetaManga).where(
            ScmEtiquetaManga.public_id == label_id,
        ))
        if scanned is None or scanned.tipo != "PREPESAJE":
            return None
        existence = session.scalar(select(ScmExistenciaMangaKg).where(
            ScmExistenciaMangaKg.manga_id == scanned.manga_id,
            ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
        ).order_by(ScmExistenciaMangaKg.recibida_at.desc()))
        if existence is None or existence.unidad_fisica_kg_id is None:
            # Reversed historical records still need their original unit for
            # the dedicated reappearance check below.
            existence = session.scalar(select(ScmExistenciaMangaKg).where(
                ScmExistenciaMangaKg.manga_id == scanned.manga_id,
            ).order_by(ScmExistenciaMangaKg.recibida_at.desc()))
        unit = session.get(ScmUnidadFisicaKg, existence.unidad_fisica_kg_id) if existence else None
    else:
        unit = canonical_resolver(session, raw)
        if unit is None and not raw.upper().startswith("KG-"):
            unit = canonical_resolver(session, f"KG-{raw}")
    if unit is None:
        return None
    query = select(ScmMesaInventarioManga).where(
        ScmMesaInventarioManga.unidad_fisica_id == unit.id,
        or_(
            ScmMesaInventarioManga.estado == "BAJA_COMPROBADA",
            and_(
                ScmMesaInventarioManga.estado.in_(
                    ("PENDIENTE_PESAJE", "PESADA_PENDIENTE_CONFIRMACION")
                ),
                ScmMesaInventarioManga.baja_motivo == "FALTANTE",
            ),
        ),
        ScmMesaInventarioManga.baja_motivo == "FALTANTE",
        ScmMesaInventarioManga.baja_movimiento_id.is_not(None),
    )
    if lock:
        query = query.with_for_update()
    shadow = session.scalar(query)
    if shadow is None:
        return None
    if not _has_baja_reappearance_evidence(session, shadow):
        return None
    existences = session.scalars(
        select(ScmExistenciaMangaKg)
        .where(ScmExistenciaMangaKg.unidad_fisica_kg_id == unit.id)
        .order_by(ScmExistenciaMangaKg.recibida_at.desc())
    ).all()
    existence = next((item for item in existences if item.manga_id is not None), None)
    if existence is None:
        return None
    labels = session.scalars(select(ScmEtiquetaManga).where(
        ScmEtiquetaManga.manga_id == existence.manga_id,
        ScmEtiquetaManga.tipo == "PREPESAJE",
    ).order_by(ScmEtiquetaManga.version.desc(), ScmEtiquetaManga.id.desc())).all()
    current = labels[0] if labels else None
    if current is None or current.estado != "IMPRESA":
        return None
    if label_id is not None and (current.public_id != label_id):
        return None
    return shadow


def _validate_reappearance_shadow_label(session, shadow):
    """Keep the BAJA recovery exception within the current label state."""
    if shadow.unidad_fisica_id is None:
        return
    existences = session.scalars(select(ScmExistenciaMangaKg).where(
        ScmExistenciaMangaKg.unidad_fisica_kg_id == shadow.unidad_fisica_id,
    ).order_by(ScmExistenciaMangaKg.recibida_at.desc())).all()
    existence = next((item for item in existences if item.manga_id is not None), None)
    if existence is None:
        return
    label = session.scalar(select(ScmEtiquetaManga).where(
        ScmEtiquetaManga.manga_id == existence.manga_id,
        ScmEtiquetaManga.tipo == "PREPESAJE",
    ).order_by(ScmEtiquetaManga.version.desc(), ScmEtiquetaManga.id.desc()))
    if label is None or label.estado == "IMPRESA":
        return
    if label.estado == "INVALIDADA":
        raise ScmServiceError(
            "ETIQUETA_INVALIDADA",
            "La etiqueta fue reemplazada. Escanea la versión vigente.",
            status_code=409,
        )
    raise ScmServiceError(
        "ETIQUETA_NO_IMPRESA",
        "La etiqueta PREPESAJE todavía no fue confirmada como impresa.",
        status_code=409,
    )


def _validate_typed_unit_label_state(session, unit):
    """Apply the PREPESAJE JSON decision to a typed OF identity.

    Older KG identities can exist without an associated SCM manga/label; those
    rows remain resolvable for legacy flows.  Once a PREPESAJE label exists,
    however, delegating to the canonical JSON resolver guarantees identical
    handling for invalidated/replaced labels and historical identities.
    """
    existences = session.scalars(
        select(ScmExistenciaMangaKg)
        .where(ScmExistenciaMangaKg.unidad_fisica_kg_id == unit.id)
        .order_by(ScmExistenciaMangaKg.recibida_at.desc())
    ).all()
    # A later Mesa reappearance can materialize an existence without manga_id;
    # prefer the current production-linked record so its PREPESAJE remains
    # authoritative. Once a documented reappearance is confirmed, its new
    # active receipt is intentionally unlinked; do not resurrect the old
    # reversed label when resolving the now-active typed identity.
    existence = next(
        (
            item for item in existences
            if item.manga_id is not None and item.estado_logistico != "REVERSADA"
        ),
        None,
    )
    if existence is None and not any(
        item.estado_logistico != "REVERSADA" for item in existences
    ):
        existence = next((item for item in existences if item.manga_id is not None), None)
    if existence is None:
        if unit.estado == "HISTORICA":
            raise ScmServiceError(
                "KG_UNIT_HISTORICAL",
                "La identidad apunta a una unidad KG histórica; usa la identidad vigente.",
                status_code=409,
            )
        return unit
    label = session.scalar(
        select(ScmEtiquetaManga)
        .where(
            ScmEtiquetaManga.manga_id == existence.manga_id,
            ScmEtiquetaManga.tipo == "PREPESAJE",
        )
        .order_by(ScmEtiquetaManga.version.desc(), ScmEtiquetaManga.id.desc())
    )
    if label is None:
        return unit
    from app.services.scm_kg_custody_service import _unit_by_code as canonical_resolver

    resolved = canonical_resolver(
        session,
        json.dumps({"v": 1, "label_id": str(label.public_id)}, separators=(",", ":")),
    )
    if resolved is not None and resolved.id != unit.id:
        raise ScmServiceError(
            "KG_IDENTITY_MISMATCH",
            "La etiqueta PREPESAJE no corresponde a la identidad digitada.",
            status_code=409,
        )
    return resolved or unit


def _resolve_unit(session, code, *, lock=False):
    """Resolve canonical unit/label aliases used by production stickers."""
    value = _text(code, "codigo", max_length=512)
    # Custody owns the authoritative resolver for PREPESAJE labels, including
    # L1|UUID and compact JSON. Keep this import lazy to avoid the service
    # import cycle.
    from app.services.scm_kg_custody_service import _unit_by_code as canonical_resolver
    # The printed PREPESAJE sticker is the short ``L1|<label_uuid>`` form.
    # Custody's canonical parser consumes the compact JSON representation, so
    # normalize only this transport wrapper before delegating.  The raw value
    # is retained for the legacy resolver below and for audit/error semantics.
    canonical_value = value
    if value.upper().startswith("L1|"):
        canonical_value = json.dumps(
            {"v": 1, "label_id": value[3:].strip()},
            separators=(",", ":"),
        )
    unit = canonical_resolver(session, canonical_value)
    if unit is None and not value.upper().startswith("KG-"):
        unit = canonical_resolver(session, f"KG-{value}")
    if unit is not None:
        if _typed_unit_code(value):
            unit = _validate_typed_unit_label_state(session, unit)
        query = select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit.id)
        return session.scalar(query.with_for_update()) if lock else unit

    # Legacy rows without a label record still resolve by UUID or exact code.
    label_public_id = None
    if value.upper().startswith("L1|"):
        try:
            label_public_id = uuid.UUID(value[3:].strip())
        except (TypeError, ValueError):
            raise ScmServiceError("QR_INVALID", "El QR L1 no contiene una identidad válida.", status_code=422)
    try:
        decoded = json.loads(value) if label_public_id is None else None
        if isinstance(decoded, dict) and decoded.get("v") == 1:
            label_public_id = uuid.UUID(str(decoded.get("label_id")))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        pass
    if label_public_id is not None:
        label = session.scalar(select(ScmEtiquetaUnidadKg).where(
            or_(ScmEtiquetaUnidadKg.public_id == label_public_id, ScmEtiquetaUnidadKg.id == label_public_id)
        ))
        if label is None:
            return None
        current = session.scalar(select(ScmEtiquetaUnidadKg).where(
            ScmEtiquetaUnidadKg.unidad_id == label.unidad_id,
        ).order_by(ScmEtiquetaUnidadKg.version.desc()).limit(1))
        if label.estado == "INVALIDADA":
            raise ScmServiceError("KG_LABEL_NOT_PRINTABLE", "La etiqueta KG está invalidada.", status_code=409)
        if current is None or current.id != label.id:
            raise ScmServiceError("KG_LABEL_VERSION_CONFLICT", "La etiqueta KG ya no es la versión vigente.", status_code=409)
        query = select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == label.unidad_id)
    else:
        parsed = None
        try:
            parsed = uuid.UUID(value)
        except (TypeError, ValueError):
            pass
        query = select(ScmUnidadFisicaKg).where(
            (or_(ScmUnidadFisicaKg.codigo == value, ScmUnidadFisicaKg.public_id == parsed) if parsed else ScmUnidadFisicaKg.codigo == value)
        )
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _ledger_location(session, actor_id, article):
    """Resolve the preconfigured station-owned pilot location.

    The adapter must never create a warehouse/location or silently invent a
    second accounting bucket. ``PRODUCCION_KG`` is provisioned by SCM config.
    """
    location = session.scalar(
        select(ScmUbicacionInventario)
        .where(ScmUbicacionInventario.codigo == SALDO_KEY)
        .with_for_update()
    )
    if location is None or not location.activo or not location.permite_saldo_libre:
        raise ScmServiceError("LOCATION_NOT_FOUND", "La ubicación PRODUCCION_KG no está configurada para la estación.", status_code=409)
    assert_location_scope(session, actor_id=actor_id, location=location, article_class=article.clase)
    return location


def _active_existence(session, unit_id, *, lock=True):
    query = select(ScmExistenciaMangaKg).where(
        ScmExistenciaMangaKg.unidad_fisica_kg_id == unit_id,
        ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
    )
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _project_kardex_positive(session, *, actor_id, operation_id, manga, net, reason):
    unit = session.get(ScmUnidadFisicaKg, manga.unidad_fisica_id, with_for_update=True)
    article = session.get(ScmArticulo, manga.articulo_id)
    reappearance = manga.baja_motivo == "FALTANTE"
    if unit is None or (unit.estado != "ACTIVA" and not (reappearance and unit.estado == "HISTORICA")):
        raise ScmServiceError("IDENTITY_NOT_ACTIVE", "La confirmación requiere una identidad Mesa activa.", status_code=409)
    if not reappearance and unit.estado_logistico not in {
        "PENDIENTE_VERIFICACION", "RECIBIDA_ALMACEN", "ALMACENADA_CONTROLADA", "DISPONIBLE_PRODUCCION",
    }:
        raise ScmServiceError("IDENTITY_NOT_ELIGIBLE", "La identidad está en custodia o en otro flujo operativo.", status_code=409)
    location = _ledger_location(session, actor_id, article)
    if _active_existence(session, unit.id) is not None:
        raise ScmServiceError("KARDEX_ALREADY_ACTIVE", "La identidad ya tiene una existencia activa en el Kardex.", status_code=409)
    balance = session.scalar(select(ScmSaldoInventarioKg).where(
        ScmSaldoInventarioKg.articulo_scm_id == article.id,
        ScmSaldoInventarioKg.ubicacion_id == location.id,
    ).with_for_update())
    if balance is None:
        # The location is canonical and already validated above.  A first
        # movement may legitimately materialize its zero balance row; never
        # create a location or another accounting bucket here.
        balance = ScmSaldoInventarioKg(articulo_scm_id=article.id, ubicacion_id=location.id)
        session.add(balance)
        session.flush()
    result = (Decimal(balance.cantidad_fisica_kg) + net).quantize(QUANTUM)
    balance.cantidad_fisica_kg = result
    balance.version += 1
    movement_id = uuid.uuid4()
    movement = ScmMovimientoInventarioKg(
        id=movement_id, saldo_id=balance.id, tipo="AJUSTE_POSITIVO",
        cantidad_delta_kg=net, saldo_fisico_resultante_kg=result,
        motivo=reason, referencia_tipo=SALDO_KEY,
        referencia_id=manga.codigo, actor_id=actor_id,
        operation_id=operation_id, projection_sha256=_hash([manga.codigo, str(operation_id)]),
        peso_neto_snapshot_kg=net, pesada_at_snapshot=_now(),
        fuente_tipo="KG_MESA_INVENTARIO", atributo_proceso="PROCESO",
    )
    session.add(movement)
    session.flush()
    existence = ScmExistenciaMangaKg(
        id=uuid.uuid4(), manga_id=None, sesion_id=None, etiqueta_resuelta_id=None,
        unidad_fisica_kg_id=unit.id, articulo_scm_id=article.id,
        saldo_id=balance.id, ubicacion_id=location.id,
        movimiento_ingreso_id=movement.id, operation_id=operation_id,
        resuelta_por="MESA_INVENTARIO", estado_logistico="RECIBIDA_ALMACEN",
        estado_calidad="SIN_CONTROL", cantidad_fisica_kg=net,
        peso_neto_snapshot_kg=net, pesaje_public_id=None,
        projection_sha256=movement.projection_sha256, pesada_at_snapshot=_now(),
        recibida_por_id=actor_id, origen_tipo="PRODUCCION", atributo_proceso="PROCESO",
    )
    session.add(existence)
    session.flush()
    unit.saldo_id, unit.ubicacion_id, unit.recepcion_vigente_id = balance.id, location.id, existence.id
    # Armado's batch withdrawal validates this ownership against the balance;
    # preserve the configured warehouse instead of leaving a Mesa-only unit
    # orphaned from the custody flow.
    unit.almacen_responsable_id = location.almacen_id
    unit.kg_verificados, unit.kg_verificados_at = net, _now()
    unit.estado, unit.estado_logistico, unit.estado_calidad = "ACTIVA", "RECIBIDA_ALMACEN", "SIN_CONTROL"
    unit.version += 1
    manga.kardex_existence_id = existence.id
    return movement, existence


def _project_kardex_negative(session, *, actor_id, operation_id, manga, net, reason):
    unit = session.get(ScmUnidadFisicaKg, manga.unidad_fisica_id, with_for_update=True)
    if unit is None or unit.estado != "ACTIVA":
        raise ScmServiceError("IDENTITY_NOT_ACTIVE", "La baja requiere una identidad global activa.", status_code=409)
    if session.scalar(select(ScmReservaUnidadKg.id).where(
        ScmReservaUnidadKg.unidad_id == unit.id,
        ScmReservaUnidadKg.estado == "ACTIVA",
    ).with_for_update()) is not None:
        raise ScmServiceError("IDENTITY_RESERVED", "La identidad tiene una reserva o custodia activa.", status_code=409)
    if session.scalar(select(ScmRetiroArmadoKgItem.id).join(ScmRetiroArmadoKg).where(
        ScmRetiroArmadoKgItem.unidad_id == unit.id,
        ScmRetiroArmadoKg.estado.in_(("ABIERTO", "CONCILIACION_PENDIENTE")),
    ).with_for_update()) is not None:
        raise ScmServiceError("IDENTITY_IN_CUSTODY", "La identidad está en custodia de Armado.", status_code=409)
    existence = _active_existence(session, unit.id)
    if existence is None:
        raise ScmServiceError("KARDEX_EXISTENCE_NOT_FOUND", "La identidad no tiene existencia elegible en el Kardex.", status_code=409)
    location = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == SALDO_KEY,
        ScmUbicacionInventario.activo.is_(True),
    ))
    if location is None or existence.ubicacion_id != location.id or existence.articulo_scm_id != manga.articulo_id:
        raise ScmServiceError("KARDEX_EXISTENCE_NOT_FOUND", "La identidad no tiene una existencia canónica vigente en PRODUCCION_KG.", status_code=409)
    balance = session.get(ScmSaldoInventarioKg, existence.saldo_id, with_for_update=True)
    if balance is None or balance.ubicacion_id != location.id or balance.articulo_scm_id != manga.articulo_id:
        raise ScmServiceError("KARDEX_EXISTENCE_NOT_FOUND", "El saldo canónico de la identidad no coincide con PRODUCCION_KG.", status_code=409)
    result = (Decimal(balance.cantidad_fisica_kg) - net).quantize(QUANTUM)
    if result < 0 or Decimal(balance.cantidad_reservada_kg) + Decimal(balance.cantidad_no_disponible_kg) > result:
        raise ScmServiceError("KARDEX_RESERVED_CONFLICT", "La baja dejaría un saldo comprometido o negativo.", status_code=409)
    balance.cantidad_fisica_kg, balance.version = result, balance.version + 1
    movement = ScmMovimientoInventarioKg(
        id=uuid.uuid4(), saldo_id=balance.id, tipo="AJUSTE_NEGATIVO",
        cantidad_delta_kg=-net, saldo_fisico_resultante_kg=result,
        motivo=reason, referencia_tipo=SALDO_KEY, referencia_id=manga.codigo,
        actor_id=actor_id, operation_id=operation_id,
        projection_sha256=_hash([manga.codigo, str(operation_id)]),
        peso_neto_snapshot_kg=net, pesada_at_snapshot=_now(),
        fuente_tipo="KG_MESA_INVENTARIO", atributo_proceso="PROCESO",
    )
    session.add(movement)
    existence.estado_logistico = "REVERSADA"
    unit.estado, unit.estado_logistico, unit.version = "HISTORICA", "REVERSADA", unit.version + 1
    session.flush()
    return movement, existence


def _manga_payload(manga):
    article = manga.articulo
    return {
        "codigo": manga.codigo,
        "unidad_id": str(manga.unidad_fisica.public_id) if manga.unidad_fisica else None,
        "articulo": str(article.id if article else manga.articulo_id),
        "articulo_id": str(manga.articulo_id),
        "pieza": article.nombre if article else None,
        "color": _article_color(article),
        "zona": manga.zona_hallazgo,
        "zona_hallazgo": manga.zona_hallazgo,
        "estado": manga.estado,
        "bruto": _fmt(manga.bruto_kg),
        "tara": _fmt(manga.tara_kg),
        "neto": _fmt(manga.neto_kg),
    }


def _fmt(value):
    return format(Decimal(value), ".3f") if value is not None else None


def _article_color(article):
    """Return the catalog color without exposing unrelated personal data."""
    subtype = getattr(article, "pieza_color", None) if article else None
    variant = getattr(subtype, "pieza_color", None) if subtype else None
    return getattr(getattr(variant, "color_produccion_rel", None), "nombre", None)


def _fingerprint(data):
    return _hash(data)


def _operation(session, operation_id, endpoint, actor_id, data):
    operation_id = _uuid(operation_id)
    digest = _fingerprint({"endpoint": endpoint, "actor_id": actor_id, "data": data})
    prior = session.get(ScmOperacion, operation_id)
    if prior is not None:
        if prior.endpoint != endpoint or prior.request_sha256 != digest:
            raise ScmServiceError("IDEMPOTENCY_CONFLICT", "La operación ya fue usada con otra intención.", status_code=409)
        if prior.response_json is None:
            raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La operación anterior no terminó.", status_code=409)
        return operation_id, copy.deepcopy(prior.response_json), prior.estado_http or 200
    operation = ScmOperacion(operation_id=operation_id, endpoint=endpoint, actor_id=actor_id, request_sha256=digest)
    session.add(operation)
    session.flush()
    return operation_id, None, operation


def _complete(session, operation_id, response, status=201):
    operation = session.get(ScmOperacion, operation_id)
    operation.response_json = copy.deepcopy(response)
    operation.estado_http = status
    session.commit()
    return response, status


def _rollback(session):
    try:
        session.rollback()
    except Exception:
        pass


def _session(session, actor_id, session_id, *, lock=False):
    try:
        parsed = _uuid(session_id, "session_id")
    except ScmServiceError:
        raise ScmServiceError("SESSION_NOT_FOUND", "La sesión no existe.", status_code=404)
    value = session.get(ScmMesaInventarioSesion, parsed, with_for_update=lock)
    if value is None:
        raise ScmServiceError("SESSION_NOT_FOUND", "La sesión no existe.", status_code=404)
    _assert_zone(session, actor_id, value.zona_id)
    return value


def _reading(session, actor_id, session_obj, manga, data):
    requested_zone = data.get("zona") or session_obj.zona_id
    if requested_zone not in _session_zones(session_obj):
        raise ScmServiceError("ZONE_OUT_OF_SCOPE", "La habitación no pertenece al alcance de la sesión.", status_code=409)
    existing = session.scalar(select(ScmMesaInventarioLectura).where(
        ScmMesaInventarioLectura.sesion_id == session_obj.id,
        ScmMesaInventarioLectura.manga_id == manga.id,
    ))
    if existing is not None:
        if _is_named_session(session_obj) and existing.zona_lectura != requested_zone:
            raise ScmServiceError(
                "ZONE_CONFLICT", "La identidad ya fue leída en otra habitación; quite la captura anterior.",
                status_code=409,
            )
        return "Lectura ya registrada"
    previous_zone, previous_state, previous_net = manga.zona_hallazgo, manga.estado, manga.neto_kg
    _invalidate_active_reviews(session, manga.codigo)
    if manga.zona_hallazgo and manga.zona_hallazgo != requested_zone:
        if data.get("confirmar_zona") is not True or data.get("zona_anterior") != manga.zona_hallazgo:
            raise ScmServiceError("ZONE_CONFLICT", "La identidad figura en otra zona; confirme el hallazgo.", status_code=409, details={"zona_anterior": manga.zona_hallazgo, "zona_actual": requested_zone})
    manga.zona_hallazgo = requested_zone
    if manga.estado == "BAJA_COMPROBADA":
        manga.estado = "PENDIENTE_PESAJE"
        manga.bruto_kg = manga.tara_kg = manga.neto_kg = None
    session.add(ScmMesaInventarioLectura(
        sesion_id=session_obj.id, manga_id=manga.id,
        zona_anterior=previous_zone, estado_anterior=previous_state,
        neto_anterior_kg=previous_net, zona_lectura=requested_zone,
        zona_existente=previous_zone, movimiento_corte_at=session_obj.corte_at,
        pesaje_corte_at=session_obj.corte_at, actor_id=actor_id,
    ))
    return "Lectura registrada"


def _state(session):
    zones = [{"id": item[0], "nombre": item[1]} for item in ZONES]
    articles = []
    for article in session.scalars(select(ScmArticulo).where(ScmArticulo.activo.is_(True), ScmArticulo.clase == "PIEZA_COLOR", ScmArticulo.unidad_inventario == "KG").order_by(ScmArticulo.codigo)).all():
        articles.append({
            "id": str(article.id), "codigo": article.codigo,
            "nombre": next(iter(_piece_names(article)), article.nombre),
            "nombre_color": article.nombre,
            "color": _article_color(article),
        })
    mangas = [_manga_payload(item) for item in session.scalars(select(ScmMesaInventarioManga).order_by(ScmMesaInventarioManga.codigo)).all()]
    sessions = []
    for item in session.scalars(select(ScmMesaInventarioSesion).order_by(ScmMesaInventarioSesion.created_at)).all():
        named = _is_named_session(item)
        readings = []
        for row in item.lecturas:
            readings.append({
                "codigo": row.manga.codigo,
                **({"zona": row.zona_lectura or item.zona_id, "zona_existente": row.zona_existente, "estado": "PRESENTE"} if named else {}),
            })
        projected = {"id": str(item.id), "zona": item.zona_id, "estado": item.estado, "lecturas": readings}
        if named:
            projected.update({
                "nombre": item.nombre, "piezas": _session_pieces(item),
                "esperadas": list(item.esperadas_json or []),
                "no_habidas": [row.manga.codigo for row in session.scalars(select(ScmMesaInventarioNoHabida).where(ScmMesaInventarioNoHabida.sesion_id == item.id)).all()],
                "pendientes_conteo": (
                    list((item.cierre_response_json or {}).get("pendientes_conteo", []))
                    if item.estado != "ABIERTA" and item.cierre_response_json
                    else _named_pending(session, item)
                ),
            })
        sessions.append(projected)
    pending = [_manga_payload(item) for item in session.scalars(select(ScmMesaInventarioManga).where(ScmMesaInventarioManga.estado.in_(("PENDIENTE_PESAJE", "PESADA_PENDIENTE_CONFIRMACION"))).order_by(ScmMesaInventarioManga.codigo)).all()]
    movement_rows = session.execute(select(
        ScmMesaInventarioMovimiento,
        ScmMesaInventarioManga.codigo,
    ).join(
        ScmMesaInventarioManga,
        ScmMesaInventarioManga.id == ScmMesaInventarioMovimiento.manga_id,
    ).order_by(ScmMesaInventarioMovimiento.created_at)).all()
    movements = [{
        "id": str(item.id), "codigo": code, "tipo": item.tipo,
        "delta": _fmt(item.delta_kg), "motivo": item.motivo,
        "vinculo_baja": str(item.vinculo_baja_id) if item.vinculo_baja_id else None,
        "kardex_movimiento_id": str(item.kardex_movimiento_id) if item.kardex_movimiento_id else None,
    } for item, code in movement_rows]
    campaign = session.scalar(select(ScmMesaInventarioCampana).order_by(ScmMesaInventarioCampana.created_at).limit(1))
    return {
        "campana": {"id": str(campaign.id), "codigo": campaign.codigo, "estado": campaign.estado} if campaign else None,
        "saldo": {"clave": SALDO_KEY, "clase": "PIEZA_COLOR", "unidad": "KG"},
        "cuenta": STATION_ID,
        "zonas": zones, "articulos": articles, "mangas": mangas,
        "sesiones": sessions, "pendientes": pending, "movimientos": movements,
        "preetiquetas": [],
        "conteo_por_pieza": True,
        "pendientes_conteo": sorted({code for item in session.scalars(select(ScmMesaInventarioSesion).where(ScmMesaInventarioSesion.estado == "ABIERTA")).all() if _is_named_session(item) for code in _named_pending(session, item)}),
    }


def _latest_no_habida(session, code, *, lock=False):
    """Return the latest closed-count missing mark for a manga, if any."""
    statement = select(ScmMesaInventarioNoHabida).join(
        ScmMesaInventarioSesion,
        ScmMesaInventarioSesion.id == ScmMesaInventarioNoHabida.sesion_id,
    ).join(
        ScmMesaInventarioManga,
        ScmMesaInventarioManga.id == ScmMesaInventarioNoHabida.manga_id,
    ).where(
        ScmMesaInventarioManga.codigo == code,
        ScmMesaInventarioSesion.estado == "CERRADA",
    ).order_by(
        ScmMesaInventarioNoHabida.created_at.desc(),
        ScmMesaInventarioNoHabida.id.desc(),
    ).limit(1)
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _no_habida_is_current(session, row):
    """A later physical reading invalidates a missing mark from review."""
    later = session.scalar(select(ScmMesaInventarioLectura.id).where(
        ScmMesaInventarioLectura.manga_id == row.manga_id,
        ScmMesaInventarioLectura.created_at > row.created_at,
    ).limit(1))
    return later is None


def vista_state(session, actor_id):
    """Return the Mesa contract for a server-resolved actor."""
    _count_actor(session, actor_id)
    return _state(session)


def vista_state_revision(session, actor_id):
    """Return the review queue used by the SCM web inventory workspace.

    Observing candidates requires only ``INVENTARIO_VER``. The adjustment
    itself remains guarded by ``INVENTARIO_AJUSTAR`` in ``_missing``. Keeping
    the two capabilities separate lets a reviewer inspect and prepare
    evidence without silently changing Kardex.
    """
    actor = _actor(session, actor_id, "INVENTARIO_VER")
    movements_enabled = os.environ.get("SCM_MESA_MOVIMIENTOS_HABILITADOS") == "1"
    can_adjust = actor.tiene_capacidad("INVENTARIO_AJUSTAR")
    active_reviews = {
        item.codigo: {
            "token": str(item.id),
            "created_at": _iso(item.created_at),
        }
        for item in session.scalars(select(ScmMesaInventarioRevision).where(
            ScmMesaInventarioRevision.estado == "ACTIVA",
        )).all()
    }
    candidates = []
    actionable_candidates = []
    history = []
    manga_load = selectinload(ScmMesaInventarioNoHabida.manga)
    no_habidas = session.scalars(select(ScmMesaInventarioNoHabida).join(
        ScmMesaInventarioSesion,
        ScmMesaInventarioSesion.id == ScmMesaInventarioNoHabida.sesion_id,
    ).options(
        manga_load.selectinload(ScmMesaInventarioManga.unidad_fisica),
        manga_load.selectinload(ScmMesaInventarioManga.articulo).selectinload(ScmArticulo.pieza_color).selectinload(ScmArticuloPiezaColor.pieza_color).selectinload(PiezaColor.color_produccion_rel),
        manga_load.selectinload(ScmMesaInventarioManga.articulo).selectinload(ScmArticulo.pieza_color).selectinload(ScmArticuloPiezaColor.pieza_color).selectinload(PiezaColor.pieza_rel),
        selectinload(ScmMesaInventarioNoHabida.sesion),
    ).where(
        ScmMesaInventarioSesion.estado == "CERRADA",
    ).order_by(
        ScmMesaInventarioNoHabida.created_at.desc(),
    )).all()
    # Keep the latest mark per identity. A manga missing in several counts is
    # still one adjustment candidate, while the linked counts remain visible
    # in its history.
    grouped = {}
    for row in no_habidas:
        grouped.setdefault(row.manga.codigo, []).append(row)
    latest_rows = [rows[0] for rows in grouped.values()]
    coverage_by_piece = {}
    coverage_sessions = session.scalars(select(ScmMesaInventarioSesion).where(
        ScmMesaInventarioSesion.nombre.is_not(None),
    )).all()
    for coverage_session in coverage_sessions:
        zones = {coverage_session.zona_id} if coverage_session.cobertura_confirmada else set()
        for piece in _session_pieces(coverage_session):
            coverage_by_piece.setdefault(piece, set()).update(zones)
    unit_ids = [row.manga.unidad_fisica_id for row in latest_rows if row.manga.unidad_fisica_id]
    manga_code_by_id = {row.manga_id: row.manga.codigo for row in latest_rows}
    coverage_by_code = {}
    unnamed_coverage = [item for item in session.scalars(select(ScmMesaInventarioSesion)).all()
                        if item.nombre is None and item.cobertura_confirmada]
    unnamed_ids = {item.id for item in unnamed_coverage}
    if unnamed_ids:
        for session_id, manga_id in session.execute(select(
            ScmMesaInventarioLectura.sesion_id,
            ScmMesaInventarioLectura.manga_id,
        ).where(ScmMesaInventarioLectura.sesion_id.in_(unnamed_ids))).all():
            code = manga_code_by_id.get(manga_id)
            if code:
                zone = next((item.zona_id for item in unnamed_coverage if item.id == session_id), None)
                if zone:
                    coverage_by_code.setdefault(code, set()).add(zone)
    active_existence = {}
    for existence in session.scalars(select(ScmExistenciaMangaKg).where(
            ScmExistenciaMangaKg.unidad_fisica_kg_id.in_(unit_ids) if unit_ids else False,
            ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
        ).order_by(ScmExistenciaMangaKg.recibida_at.desc())).all():
        active_existence.setdefault(existence.unidad_fisica_kg_id, existence)
    saldo_ids = {item.saldo_id for item in active_existence.values() if item.saldo_id}
    balances = {
        item.id: item
        for item in session.scalars(select(ScmSaldoInventarioKg).where(
            ScmSaldoInventarioKg.id.in_(saldo_ids) if saldo_ids else False,
        )).all()
    }
    canonical_location = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == SALDO_KEY,
        ScmUbicacionInventario.activo.is_(True),
    ))
    manga_ids = [row.manga_id for row in latest_rows]
    later_reads = dict(session.execute(select(
        ScmMesaInventarioLectura.manga_id,
        func.max(ScmMesaInventarioLectura.created_at),
    ).where(
        ScmMesaInventarioLectura.manga_id.in_(manga_ids) if manga_ids else False,
    ).group_by(ScmMesaInventarioLectura.manga_id)).all())
    movement_latest = dict(session.execute(select(
        ScmMovimientoInventarioKg.saldo_id,
        func.max(ScmMovimientoInventarioKg.created_at),
    ).where(
        ScmMovimientoInventarioKg.saldo_id.in_(saldo_ids) if saldo_ids else False,
    ).group_by(ScmMovimientoInventarioKg.saldo_id)).all())
    for row in latest_rows:
        manga_payload = _manga_payload(row.manga)
        piece_names = _piece_names(row.manga.articulo)
        covered_zones = set().union(*(coverage_by_piece.get(piece, set()) for piece in piece_names)) if piece_names else set()
        covered_zones.update(coverage_by_code.get(row.manga.codigo, set()))
        if not _session_pieces(row.sesion) and row.sesion.cobertura_confirmada:
            covered_zones.add(row.sesion.zona_id)
        current = (
            later_reads.get(row.manga.id) is None
            or later_reads[row.manga.id] <= row.created_at
        )
        existence = active_existence.get(row.manga.unidad_fisica_id)
        balance = balances.get(existence.saldo_id) if existence is not None else None
        canonical_available = (
            existence is not None
            and canonical_location is not None
            and existence.ubicacion_id == canonical_location.id
            and existence.ubicacion_id == (balance.ubicacion_id if balance is not None else None)
            and existence.articulo_scm_id == row.manga.articulo_id
            and existence.cantidad_fisica_kg is not None
            and Decimal(existence.cantidad_fisica_kg) > 0
            and balance is not None
            and balance.articulo_scm_id == row.manga.articulo_id
            and balance.cantidad_fisica_kg is not None
            and Decimal(balance.cantidad_fisica_kg) > 0
        )
        movement_changed = (
            existence is not None
            and movement_latest.get(existence.saldo_id) is not None
            and movement_latest[existence.saldo_id] > row.created_at
        )
        current = current and not movement_changed
        scope_complete = covered_zones == set(ZONE_IDS)
        actionable = current and scope_complete and row.manga.estado == "DISPONIBLE" and row.manga.neto_kg is not None and canonical_available
        history_item = {
            **manga_payload,
            "vigente": actionable,
            "motivo_bloqueo": None if actionable else (
                "La manga fue hallada después del conteo." if not current and not movement_changed else
                "El saldo cambió después del conteo." if movement_changed else
                "El conteo de la pieza no cubre las cuatro zonas." if not scope_complete else
                "La manga ya no tiene saldo disponible para ajustar."
            ),
            "conteo": {
                "id": str(row.sesion_id),
                "nombre": row.sesion.nombre,
                "zona": row.sesion.zona_id,
                "piezas": _session_pieces(row.sesion),
                "cobertura_confirmada": bool(row.sesion.cobertura_confirmada),
                "cerrado_at": _iso(row.sesion.closed_at),
            },
            "cobertura_pieza": {
                "zonas_cubiertas": sorted(covered_zones),
                "zonas_faltantes": sorted(set(ZONE_IDS) - covered_zones),
                "completa": covered_zones == set(ZONE_IDS),
            },
            "conteos": [{
                "id": str(item.sesion_id), "nombre": item.sesion.nombre,
                "zona": item.sesion.zona_id, "marcado_at": _iso(item.created_at),
            } for item in grouped[row.manga.codigo]],
            "marcado_no_habida_at": _iso(row.created_at),
        }
        if row.manga.codigo in active_reviews:
            history_item["revision"] = active_reviews[row.manga.codigo]
        history.append(history_item)
        # Keep a current, positively-backed missing mark visible even when
        # the four-zone review scope is still incomplete. The UI can explain
        # the block from ``vigente``/``motivo_bloqueo``; stale marks (later
        # hallazgo or movement) remain history only.
        if current and row.manga.estado == "DISPONIBLE" and canonical_available:
            candidates.append(history_item)
        if actionable:
            actionable_candidates.append(history_item)
    sessions = []
    for item in session.scalars(select(ScmMesaInventarioSesion).order_by(ScmMesaInventarioSesion.created_at.desc())).all():
        sessions.append({
            "id": str(item.id), "nombre": item.nombre, "zona": item.zona_id,
            "estado": item.estado, "piezas": _session_pieces(item),
            "cobertura_confirmada": bool(item.cobertura_confirmada),
            "closed_at": _iso(item.closed_at),
            "no_habidas": [code for code, rows in grouped.items() if any(mark.sesion_id == item.id for mark in rows)],
        })
    movement_rows = session.execute(select(
        ScmMesaInventarioMovimiento,
        ScmMesaInventarioManga.codigo,
    ).join(
        ScmMesaInventarioManga,
        ScmMesaInventarioManga.id == ScmMesaInventarioMovimiento.manga_id,
    ).order_by(ScmMesaInventarioMovimiento.created_at)).all()
    movements = [{
        "id": str(item.id), "codigo": code, "tipo": item.tipo,
        "delta": _fmt(item.delta_kg), "motivo": item.motivo,
        "vinculo_baja": str(item.vinculo_baja_id) if item.vinculo_baja_id else None,
        "kardex_movimiento_id": str(item.kardex_movimiento_id) if item.kardex_movimiento_id else None,
    } for item, code in movement_rows]
    campaign = session.scalar(select(ScmMesaInventarioCampana).order_by(ScmMesaInventarioCampana.created_at).limit(1))
    articles = []
    seen_articles = set()
    for item in history:
        if item["articulo_id"] in seen_articles:
            continue
        seen_articles.add(item["articulo_id"])
        articles.append({
            "id": item["articulo_id"], "nombre": item["pieza"],
            "nombre_color": item["pieza"], "color": item["color"],
        })
    payload = {
        "campana": {"id": str(campaign.id), "codigo": campaign.codigo, "estado": campaign.estado} if campaign else None,
        "saldo": {"clave": SALDO_KEY, "clase": "PIEZA_COLOR", "unidad": "KG"},
        "cuenta": STATION_ID, "zonas": [{"id": key, "nombre": name} for key, name in ZONES],
        "articulos": articles, "mangas": history, "sesiones": sessions,
        "pendientes": [], "movimientos": movements, "preetiquetas": [],
        "conteo_por_pieza": True, "pendientes_conteo": [],
    }
    adjustment_reason = None
    if not can_adjust and not movements_enabled:
        adjustment_reason = "Tu rol no tiene INVENTARIO_AJUSTAR y el gate central de movimientos está cerrado."
    elif not can_adjust:
        adjustment_reason = "Tu rol no tiene INVENTARIO_AJUSTAR."
    elif not movements_enabled:
        adjustment_reason = "Los movimientos centrales están deshabilitados temporalmente."
    payload["revision"] = {
        "candidatas": candidates,
        "vigentes": actionable_candidates,
        "activas": active_reviews,
        "historial": history,
        "movimientos_habilitados": movements_enabled,
        "puede_ajustar": can_adjust,
        "ajuste_habilitado": movements_enabled and can_adjust,
        "motivo_ajuste": adjustment_reason,
    }
    return payload


def _catalogo_conteo(session):
    variant = selectinload(ScmArticulo.pieza_color).selectinload(
        ScmArticuloPiezaColor.pieza_color
    )
    article_rows = session.scalars(select(ScmArticulo).options(
        variant.selectinload(PiezaColor.color_produccion_rel),
        variant.selectinload(PiezaColor.pieza_rel),
    ).where(
        ScmArticulo.activo.is_(True), ScmArticulo.clase == "PIEZA_COLOR",
        ScmArticulo.unidad_inventario == "KG",
    ).order_by(ScmArticulo.codigo)).all()
    return [{
        "id": str(article.id), "codigo": article.codigo,
        "nombre": next(iter(_piece_names(article)), article.nombre),
        "nombre_color": article.nombre, "color": _article_color(article),
    } for article in article_rows]


def require_named_count(session, session_id):
    sid = _uuid(session_id, "sesion_id")
    if session.scalar(select(ScmMesaInventarioSesion.id).where(
        ScmMesaInventarioSesion.id == sid, ScmMesaInventarioSesion.nombre.is_not(None)
    )) is None:
        raise ScmServiceError("SESSION_NOT_FOUND", "El conteo no existe.", status_code=404)


def vista_state_conteo(session, actor_id, session_id=None):
    """Refresh a single count on scan. Load the catalog when entering the view."""
    _count_actor(session, actor_id)
    named = ScmMesaInventarioSesion.nombre.is_not(None)
    if session_id is None:
        rows = session.execute(select(
            ScmMesaInventarioSesion.id, ScmMesaInventarioSesion.nombre,
            ScmMesaInventarioSesion.zona_id, ScmMesaInventarioSesion.piezas_json,
            ScmMesaInventarioSesion.estado,
        ).where(named).order_by(ScmMesaInventarioSesion.created_at.desc()))
        summaries = [{"id": str(row.id), "nombre": row.nombre, "zona": row.zona_id,
                      "piezas": _session_pieces(row), "estado": row.estado, "resumen": True} for row in rows]
        return {"conteo_por_pieza": True, "zonas": [{"id": key, "nombre": name} for key, name in ZONES],
                "articulos": _catalogo_conteo(session), "sesiones": summaries}
    readings = selectinload(ScmMesaInventarioSesion.lecturas).selectinload(
        ScmMesaInventarioLectura.manga
    ).selectinload(ScmMesaInventarioManga.articulo)
    query = select(ScmMesaInventarioSesion).options(readings).where(
        named, ScmMesaInventarioSesion.id == _uuid(session_id, "sesion_id"))
    session_rows = session.scalars(query).all()
    if not session_rows:
        raise ScmServiceError("SESSION_NOT_FOUND", "El conteo no existe.", status_code=404)
    expected_all = {code for item in session_rows if item.estado == "ABIERTA" for code in (item.esperadas_json or [])}
    eligible_rows = _eligible_named_units(session, codes=expected_all) if expected_all else []
    last_seen = {}
    if expected_all:
        last_seen = dict(session.execute(select(
            ScmMesaInventarioManga.codigo, func.max(ScmMesaInventarioLectura.created_at)
        ).join(ScmMesaInventarioLectura, ScmMesaInventarioLectura.manga_id == ScmMesaInventarioManga.id)
          .where(ScmMesaInventarioManga.codigo.in_(expected_all)).group_by(ScmMesaInventarioManga.codigo)).all())
    sessions = []
    for item in session_rows:
        named = _is_named_session(item)
        readings = [{
            "codigo": row.manga.codigo,
            "articulo_id": str(row.manga.articulo_id),
            "articulo": row.manga.articulo.codigo if row.manga.articulo else None,
            **({"zona": row.zona_lectura or item.zona_id, "zona_existente": row.zona_existente, "estado": "PRESENTE"} if named else {}),
        } for row in item.lecturas]
        projected = {"id": str(item.id), "zona": item.zona_id, "estado": item.estado, "lecturas": readings}
        if named:
            expected = set(item.esperadas_json or [])
            cutoff = item.corte_at or item.created_at
            found = {code for code in expected if code in last_seen and _at_or_after(last_seen[code], cutoff)}
            missing = set((item.cierre_response_json or {}).get("no_habidas", []))
            eligible = {
                unit.codigo for unit in eligible_rows
                if not _after(unit.updated_at, item.corte_at or item.created_at)
                and not _after(unit.recibida_at, item.corte_at or item.created_at)
            }
            projected.update({
                "nombre": item.nombre, "piezas": _session_pieces(item),
                "esperadas": sorted(expected), "no_habidas": sorted(missing),
                "pendientes_conteo": (
                    list((item.cierre_response_json or {}).get("pendientes_conteo", []))
                    if item.estado != "ABIERTA" and item.cierre_response_json
                    else sorted((eligible & expected) - found - missing)
                ),
            })
        sessions.append(projected)
    return {"sesiones": sessions}


def label(session, actor_id, codigo):
    _actor(session, actor_id, "INVENTARIO_VER")
    manga = _manga_by_code(session, codigo)
    payload = _manga_payload(manga)
    return {"codigo": payload["codigo"], "pieza": payload["pieza"], "color": payload["color"], "zona": payload["zona"], "neto": payload["neto"], "bruto": payload["bruto"], "tara": payload["tara"]}


def _open_session(session, actor_id, data, operation_id):
    named = "nombre" in data or "piezas" in data
    actor = _count_actor(session, actor_id) if named else _actor(session, actor_id)
    raw_zones = data.get("zona")
    if isinstance(raw_zones, list):
        if not raw_zones:
            raise ScmServiceError("ZONE_REQUIRED", "La sesión requiere al menos una habitación.", status_code=422)
        zones = list(dict.fromkeys(_assert_zone(session, actor.id, item) for item in raw_zones))
    else:
        zones = [_assert_zone(session, actor.id, raw_zones)]
    zone = zones[0]
    key = _text(data.get("clave"), "clave")
    names = []
    article_ids = []
    name = None
    if named:
        if not isinstance(raw_zones, list):
            # Web C66 starts from one room but can continue the same named
            # count from another permitted room.
            zones = [zone, *[item[0] for item in ZONES if item[0] != zone]]
        name = _text(data.get("nombre"), "nombre", max_length=160)
        raw_pieces = data.get("piezas")
        if not isinstance(raw_pieces, list) or not raw_pieces or any(not isinstance(piece, str) or not piece.strip() for piece in raw_pieces):
            raise ScmServiceError("PIECES_REQUIRED", "piezas debe ser una lista no vacía.", status_code=422)
        names = list(dict.fromkeys(piece.strip() for piece in raw_pieces))
        articles = _named_articles(session, names)
        article_ids = [article.id for article in articles]
    operation_payload = {"zona": zone, "zonas": zones, "clave": key, "nombre": name, "piezas": names}
    op_id, replay, status = _operation(session, operation_id, "POST /inventario/sesiones", actor.id, operation_payload)
    if replay is not None:
        return replay, status
    campaign = _campaign(session, actor.id)
    cutoff = _now()
    expected = _snapshot_named_codes(session, article_ids, cutoff) if named else []
    saldo_snapshot = _snapshot_named_balances(session, article_ids) if named else {}
    value = ScmMesaInventarioSesion(
        campana_id=campaign.id, zona_id=zone, clave=key, creado_por_id=actor.id,
        nombre=name, piezas_json={"names": names, "article_ids": article_ids} if named else [],
        esperadas_json=expected, zonas_json=zones, saldo_snapshot_json=saldo_snapshot, corte_at=cutoff,
    )
    session.add(value)
    session.flush()
    response = {"id": str(value.id), "zona": zone, "zonas": zones, "estado": value.estado}
    if named:
        response.update({"nombre": name, "piezas": names, "esperadas": expected})
    return _complete(session, op_id, response, 201)


def _read_session(session, actor_id, session_id, data, operation_id):
    actor = _count_actor(session, actor_id)
    obj = _lock_count_session(session, actor.id, session_id)
    if obj.estado != "ABIERTA":
        raise ScmServiceError("SESSION_CLOSED", "La sesión ya está cerrada.", status_code=409)
    code = _text(data.get("codigo"), "codigo")
    manga = _manga_by_code(session, code, lock=True)
    if _is_named_session(obj):
        article_names = set(_session_pieces(obj))
        if not (_piece_names(manga.articulo) & article_names):
            raise ScmServiceError("PIECE_OUT_OF_SCOPE", "La pieza no pertenece al alcance de esta sesión.", status_code=409)
        if manga.estado == "BAJA_NO_RECUPERABLE":
            raise ScmServiceError("MANGA_BLOCKED", "La manga está bloqueada por una causa no recuperable.", status_code=409)
    operation_data = {"session_id": str(obj.id), "codigo": manga.codigo, "confirmar_zona": data.get("confirmar_zona"), "zona_anterior": data.get("zona_anterior")}
    if _is_named_session(obj):
        operation_data["zona"] = data.get("zona") or obj.zona_id
    op_id, replay, status = _operation(session, operation_id, "POST /inventario/sesiones/lecturas", actor.id, operation_data)
    if replay is not None:
        return replay, status
    message = _reading(session, actor.id, obj, manga, data)
    session.flush()
    response = {"mensaje": message}
    if _is_named_session(obj):
        response.update({
            "codigo": manga.codigo, "zona": manga.zona_hallazgo,
            "zona_existente": next(
                (item.zona_existente for item in obj.lecturas if item.manga_id == manga.id), None
            ),
            "estado": "PRESENTE",
        })
    return _complete(session, op_id, response, 201 if message == "Lectura registrada" else 200)


def _delete_reading(session, actor_id, session_id, code, data, operation_id):
    actor = _count_actor(session, actor_id)
    obj = _lock_count_session(session, actor.id, session_id)
    if obj.estado != "ABIERTA":
        raise ScmServiceError("SESSION_CLOSED", "La sesión ya está cerrada.", status_code=409)
    manga = _manga_by_code(session, code, lock=True)
    op_id, replay, status = _operation(session, operation_id, "DELETE /inventario/sesiones/lecturas", actor.id, {"session_id": str(obj.id), "codigo": manga.codigo})
    if replay is not None:
        return replay, status
    row = session.scalar(select(ScmMesaInventarioLectura).where(ScmMesaInventarioLectura.sesion_id == obj.id, ScmMesaInventarioLectura.manga_id == manga.id))
    if row is None:
        raise ScmServiceError("READING_NOT_FOUND", "La lectura no existe en la sesión.", status_code=404)
    if _is_named_session(obj):
        post_cut_movement = session.scalar(select(ScmMesaInventarioMovimiento.id).where(
            ScmMesaInventarioMovimiento.manga_id == manga.id,
            ScmMesaInventarioMovimiento.created_at > (row.created_at or obj.corte_at),
        ))
        post_cut_weight = session.scalar(select(ScmMesaInventarioPesaje.id).where(
            ScmMesaInventarioPesaje.manga_id == manga.id,
            ScmMesaInventarioPesaje.created_at > (row.created_at or obj.corte_at),
        ))
        if post_cut_movement or post_cut_weight:
            raise ScmServiceError("READING_IMMUTABLE", "No se puede retirar una lectura después de pesar o acreditar.", status_code=409)
    elif manga.estado in {"PESADA_PENDIENTE_CONFIRMACION", "DISPONIBLE"}:
        raise ScmServiceError("READING_IMMUTABLE", "No se puede retirar una lectura con pesaje o saldo confirmado.", status_code=409)
    later = session.scalar(select(ScmMesaInventarioLectura).where(
        ScmMesaInventarioLectura.manga_id == manga.id,
        ScmMesaInventarioLectura.id != row.id,
    ).order_by(ScmMesaInventarioLectura.created_at.desc(), ScmMesaInventarioLectura.id.desc()))
    if later is not None and (
        later.created_at is None
        or row.created_at is None
        or later.created_at > row.created_at
        or (later.created_at == row.created_at and later.id.int > row.id.int)
    ):
        # Another room observed this identity after A. Removing A must not
        # roll the shared shadow back over B's newer location/state.
        manga.zona_hallazgo = later.zona_lectura or manga.zona_hallazgo
    else:
        manga.zona_hallazgo, manga.estado, manga.neto_kg = row.zona_anterior, row.estado_anterior or manga.estado, row.neto_anterior_kg
    session.delete(row)
    return _complete(session, op_id, {"mensaje": "Lectura eliminada"}, 200)


def _close_session(session, actor_id, session_id, data, operation_id):
    actor = _count_actor(session, actor_id)
    obj = _lock_count_session(session, actor.id, session_id)
    if _is_named_session(obj):
        marked = data.get("marcar_no_habidas", False)
        if not isinstance(marked, bool):
            raise ScmServiceError("INVALID_MISSING_FLAG", "marcar_no_habidas debe ser booleano.", status_code=422)
        raw_missing = data.get("no_habidas", [])
        if not isinstance(raw_missing, list) or any(not isinstance(code, str) or not code.strip() for code in raw_missing):
            raise ScmServiceError("INVALID_MISSING", "no_habidas debe ser una lista de códigos.", status_code=422)
        missing = list(dict.fromkeys(code.strip() for code in raw_missing))
        if len(missing) != len(raw_missing):
            raise ScmServiceError("DUPLICATE_MISSING", "no_habidas no debe contener códigos repetidos.", status_code=422)
        coverage_confirmed = data.get("cobertura") is True or data.get("cobertura_completa") is True
        fingerprint = _fingerprint({"marcar_no_habidas": marked, "no_habidas": missing, "cobertura": coverage_confirmed})
        legacy_fingerprint = _fingerprint({"marcar_no_habidas": marked, "no_habidas": missing})
        if obj.estado != "ABIERTA":
            if obj.cierre_fingerprint in {fingerprint, legacy_fingerprint} and obj.cierre_response_json:
                return copy.deepcopy(obj.cierre_response_json), 200
            raise ScmServiceError("SESSION_CLOSED", "La sesión ya está cerrada.", status_code=409)
        _lock_named_identities(session, obj.esperadas_json or [])
        pending = _named_pending(session, obj)
        if marked and set(missing) != set(pending):
            raise ScmServiceError("MISSING_MISMATCH", "La lista de no habidas no coincide con las pendientes del corte.", status_code=409, details={"pendientes_conteo": pending})
        if not marked and missing:
            raise ScmServiceError("MISSING_FLAG_REQUIRED", "no_habidas requiere marcar_no_habidas=true.", status_code=422)
        op_id, replay, status = _operation(session, operation_id, "POST /inventario/sesiones/cerrar", actor.id, {
            "session_id": str(obj.id), "marcar_no_habidas": marked,
            "no_habidas": missing, "cobertura": coverage_confirmed,
        })
        if replay is not None:
            return replay, status
        for code in (missing if marked else []):
            manga = _manga_by_code(session, code, lock=True)
            if code not in (obj.esperadas_json or []):
                raise ScmServiceError("MISSING_OUT_OF_SCOPE", "Una no habida no pertenece al corte de la sesión.", status_code=409)
            _invalidate_active_reviews(session, code)
            session.add(ScmMesaInventarioNoHabida(sesion_id=obj.id, manga_id=manga.id, actor_id=actor.id))
        closed_at = _now()
        obj.cobertura_confirmada = coverage_confirmed
        response = {
            "mensaje": "Sesión cerrada", "closed_at": _iso(closed_at),
            "cantidad": len(obj.lecturas), "no_habidas": missing if marked else [],
            "pendientes_conteo": [] if marked else pending,
            "cobertura_confirmada": coverage_confirmed,
        }
        obj.estado, obj.closed_at = "CERRADA", closed_at
        obj.cierre_fingerprint, obj.cierre_response_json = fingerprint, response
        return _complete(session, op_id, response, 200)
    op_id, replay, status = _operation(session, operation_id, "POST /inventario/sesiones/cerrar", actor.id, {"session_id": str(obj.id)})
    if replay is not None:
        return replay, status
    if obj.estado == "ABIERTA":
        obj.estado = "CERRADA"
        obj.closed_at = _now()
        obj.cobertura_confirmada = data.get("cobertura") is True or data.get("cobertura_completa") is True
    return _complete(session, op_id, {"mensaje": "Sesión cerrada", "closed_at": _iso(obj.closed_at), "cantidad": len(obj.lecturas), "cobertura_confirmada": bool(obj.cobertura_confirmada)}, 200)


def _old_unit(session, actor_id, data, operation_id):
    actor = _actor(session, actor_id)
    code = _text(data.get("codigo"), "codigo", required=False)
    article = _article(session, data.get("articulo"))
    zone = _assert_zone(session, actor.id, data.get("zona"))
    key = _text(data.get("clave"), "clave")
    normalized = {"codigo": code, "articulo": article.id, "zona": zone, "sesion": data.get("sesion")}
    op_id, replay, status = _operation(session, operation_id, "POST /inventario/antiguas", actor.id, normalized | {"clave": key})
    if replay is not None:
        return replay, status
    if code:
        manga = _manga_by_code(session, code, lock=True)
        unit = manga.unidad_fisica
        if unit is None or unit.articulo_scm_id != article.id:
            raise ScmServiceError("IDENTITY_NOT_FOUND", "El código no corresponde a una identidad global de la pieza.", status_code=404)
        if unit.estado != "ACTIVA" or unit.estado_logistico != "PENDIENTE_VERIFICACION":
            raise ScmServiceError("IDENTITY_NOT_ELIGIBLE", "La identidad ya pertenece a otro flujo y no puede registrarse como antigua.", status_code=409)
        if manga.articulo_id != article.id or manga.zona_hallazgo not in (None, zone):
            raise ScmServiceError("IDENTITY_CONFLICT", "La identidad ya está vinculada a otra pieza o zona.", status_code=409)
    else:
        code = f"INV-MESA-{uuid.uuid4().hex[:12].upper()}"
        unit = ScmUnidadFisicaKg(public_id=uuid.uuid4(), codigo=code, articulo_scm_id=article.id, estado="ACTIVA", estado_logistico="PENDIENTE_VERIFICACION", estado_calidad="SIN_CONTROL")
        session.add(unit)
        session.flush()
        manga = ScmMesaInventarioManga(codigo=code, unidad_fisica_id=unit.id, articulo_id=article.id)
        session.add(manga)
    manga.zona_hallazgo = zone
    manga.estado = "PENDIENTE_PESAJE"
    session.flush()
    session_id = data.get("sesion")
    if session_id:
        obj = _session(session, actor.id, session_id, lock=True)
        if obj.zona_id != zone:
            raise ScmServiceError("ZONE_CONFLICT", "La sesión pertenece a otra zona.", status_code=409)
        _reading(session, actor.id, obj, manga, {"codigo": code})
    response = {"codigo": code, "unidad_id": str(unit.public_id), "estado": manga.estado}
    return _complete(session, op_id, response, 201)


def _weigh(session, actor_id, data, operation_id):
    actor = _actor(session, actor_id)
    code = _text(data.get("codigo"), "codigo")
    key = _text(data.get("clave"), "clave")
    capture = data.get("captura")
    if not isinstance(capture, dict) or not capture.get("token"):
        raise ScmServiceError("CAPTURE_REQUIRED", "El pesaje requiere una lectura capturada por la estación.", status_code=409)
    if capture.get("codigo") != code:
        raise ScmServiceError("CAPTURE_IDENTITY_CONFLICT", "La lectura capturada pertenece a otra identidad.", status_code=409)
    if capture.get("base") not in {"NT", "GS"}:
        raise ScmServiceError("CAPTURE_INVALID", "La base de la lectura física no es válida.", status_code=422)
    bruto = _decimal(data.get("bruto"), "bruto", positive=True)
    tara = _decimal(data.get("tara"), "tara", non_negative=True)
    if bruto <= tara:
        raise ScmServiceError("INVALID_NET", "Se requiere bruto > tara >= 0.", status_code=422)
    captured_kg = _decimal(capture.get("kg"), "captura.kg", positive=True)
    if captured_kg != bruto:
        raise ScmServiceError("CAPTURE_WEIGHT_CONFLICT", "El bruto no coincide con la lectura capturada.", status_code=409)
    manga = _manga_by_code(session, code, lock=True)
    op_id, replay, status = _operation(session, operation_id, "POST /inventario/pesajes", actor.id, {"codigo": code, "bruto": str(bruto), "tara": str(tara), "clave": key})
    if replay is not None:
        return replay, status
    if manga.estado not in {"PENDIENTE_PESAJE", "BAJA_COMPROBADA"}:
        raise ScmServiceError("WEIGHING_NOT_PENDING", "La manga no está pendiente de pesaje.", status_code=409)
    net = (bruto - tara).quantize(QUANTUM)
    manga.bruto_kg, manga.tara_kg, manga.neto_kg, manga.estado = bruto, tara, net, "PESADA_PENDIENTE_CONFIRMACION"
    captured_at = capture.get("captured_at")
    try:
        captured_at = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00")) if captured_at else _now()
    except ValueError as error:
        raise ScmServiceError("CAPTURE_INVALID", "captured_at no es una fecha válida.", status_code=422) from error
    session.add(ScmMesaInventarioPesaje(
        manga_id=manga.id, bruto_kg=bruto, tara_kg=tara, neto_kg=net,
        lectura_id=_text(capture.get("reading_id") or uuid.uuid4().hex, "reading_id", max_length=120),
        station_id=STATION_ID, captura_base=capture["base"], captura_kg=captured_kg,
        captura_at=captured_at, clave=key, actor_id=actor.id,
    ))
    return _complete(session, op_id, {"codigo": code, "neto": _fmt(net), "estado": manga.estado}, 201)


def _review(session, actor_id, data, operation_id):
    actor = _actor(session, actor_id)
    code = _text(data.get("codigo"), "codigo")
    normalized = {
        "codigo": code,
        "cobertura": data.get("cobertura") is True,
        "movimientos_revisados": data.get("movimientos_revisados") is True,
        "busqueda": data.get("busqueda") is True,
    }
    # Resolve an idempotent replay before reading mutable manga/saldo state.
    # A lost response must remain recoverable even after the original review
    # token was consumed or the underlying balance changed.
    op_id, replay, status = _operation(session, operation_id, "POST /inventario/revision", actor.id, normalized)
    if replay is not None:
        return replay, status
    if not all(data.get(flag) is True for flag in ("cobertura", "movimientos_revisados", "busqueda")):
        raise ScmServiceError("REVIEW_INCOMPLETE", "La revisión requiere cobertura, movimientos y búsqueda dirigida.", status_code=409)
    manga = _manga_by_code(session, code, lock=True, require_inventory=True)
    missing_mark = _latest_no_habida(session, code, lock=True)
    if missing_mark is None:
        _require_complete_walk(session)
        raise ScmServiceError("REVIEW_NOT_MISSING", "La manga no está marcada como no habida en un conteo cerrado.", status_code=409)
    _require_review_scope(session, manga)
    if not _no_habida_is_current(session, missing_mark):
        _invalidate_active_reviews(session, code)
        raise ScmServiceError("REVIEW_STALE", "La revisión quedó obsoleta porque la manga fue hallada después del conteo.", status_code=409)
    existence, balance = _canonical_inventory_snapshot(session, manga, lock=True)
    if existence is None or balance is None or balance.cantidad_fisica_kg is None or Decimal(balance.cantidad_fisica_kg) <= 0:
        raise ScmServiceError("REVIEW_NOT_ACTIVE", "La manga no tiene saldo canónico disponible para revisión.", status_code=409)
    latest_movement = session.scalar(select(func.max(ScmMovimientoInventarioKg.created_at)).where(
        ScmMovimientoInventarioKg.saldo_id == existence.saldo_id,
    ))
    if latest_movement is not None and latest_movement > missing_mark.created_at:
        _invalidate_active_reviews(session, code)
        raise ScmServiceError("REVIEW_STALE", "El saldo cambió después del conteo.", status_code=409)
    if manga.estado != "DISPONIBLE":
        raise ScmServiceError("REVIEW_NOT_ACTIVE", "La manga no está activa para revisión.", status_code=409)
    existing = session.scalar(select(ScmMesaInventarioRevision).where(ScmMesaInventarioRevision.codigo == code, ScmMesaInventarioRevision.estado == "ACTIVA"))
    if existing is not None:
        return _complete(session, op_id, {"token": str(existing.id), "neto": _fmt(manga.neto_kg)}, 200)
    review = ScmMesaInventarioRevision(id=uuid.uuid4(), codigo=code, actor_id=actor.id)
    session.add(review)
    return _complete(session, op_id, {"token": str(review.id), "neto": _fmt(manga.neto_kg)}, 201)


def _missing(session, actor_id, data, operation_id):
    actor = _actor(session, actor_id, "INVENTARIO_AJUSTAR")
    code = _text(data.get("codigo"), "codigo")
    token = _text(data.get("token"), "token")
    reason = _text(data.get("motivo"), "motivo", max_length=240)
    key = _text(data.get("clave"), "clave")
    op_id, replay, status = _operation(
        session, operation_id, "POST /inventario/faltantes", actor.id,
        {"codigo": code, "token": token, "motivo": reason, "clave": key},
    )
    # Idempotent replay must win before checking the now-consumed review or
    # current unit state.  This is essential after a successful debit.
    if replay is not None:
        return replay, status
    _require_movement_gate()
    manga = _manga_by_code(session, code, lock=True, require_inventory=True)
    missing_mark = _latest_no_habida(session, code, lock=True)
    if missing_mark is None:
        _require_complete_walk(session)
        raise ScmServiceError("REVIEW_NOT_MISSING", "La manga no está marcada como no habida en un conteo cerrado.", status_code=409)
    _require_review_scope(session, manga)
    if not _no_habida_is_current(session, missing_mark):
        _invalidate_active_reviews(session, code)
        raise ScmServiceError("REVIEW_STALE", "La revisión quedó obsoleta porque la manga fue hallada después del conteo.", status_code=409)
    review = session.get(ScmMesaInventarioRevision, _uuid(token, "token"))
    if review is None or review.codigo != code or review.estado != "ACTIVA":
        raise ScmServiceError("REVIEW_INVALID", "La revisión no está activa.", status_code=409)
    if review.created_at < missing_mark.created_at:
        review.estado, review.used_at = "INVALIDADA", _now()
        raise ScmServiceError("REVIEW_STALE", "La revisión pertenece a un conteo anterior.", status_code=409)
    if session.scalar(select(ScmMesaInventarioLectura.id).where(
        ScmMesaInventarioLectura.manga_id == manga.id,
        ScmMesaInventarioLectura.created_at > review.created_at,
    ).limit(1)) is not None:
        review.estado, review.used_at = "INVALIDADA", _now()
        raise ScmServiceError("REVIEW_STALE", "La revisión quedó obsoleta por una lectura posterior.", status_code=409)
    existence, balance = _canonical_inventory_snapshot(session, manga, lock=True)
    if existence is None or balance is None or balance.cantidad_fisica_kg is None or Decimal(balance.cantidad_fisica_kg) <= 0:
        raise ScmServiceError("MANGA_NOT_ACTIVE", "La manga ya no tiene saldo canónico disponible.", status_code=409)
    if session.scalar(select(ScmMovimientoInventarioKg.id).where(
        ScmMovimientoInventarioKg.saldo_id == existence.saldo_id,
        ScmMovimientoInventarioKg.created_at > review.created_at,
    ).limit(1)) is not None:
        review.estado, review.used_at = "INVALIDADA", _now()
        raise ScmServiceError("REVIEW_STALE", "El saldo cambió después de la revisión.", status_code=409)
    if manga.estado != "DISPONIBLE" or manga.neto_kg is None:
        raise ScmServiceError("MANGA_NOT_ACTIVE", "La manga ya no está activa o no tiene neto vigente.", status_code=409)
    canonical, existence = _project_kardex_negative(
        session, actor_id=actor.id, operation_id=op_id,
        manga=manga, net=Decimal(manga.neto_kg), reason=reason,
    )
    movement = ScmMesaInventarioMovimiento(
        id=uuid.uuid4(), manga_id=manga.id, tipo="AJUSTE_FALTANTE",
        delta_kg=-Decimal(manga.neto_kg), motivo=reason, clave=key,
        actor_id=actor.id, kardex_movimiento_id=canonical.id,
    )
    session.add(movement)
    session.flush()
    manga.estado, manga.baja_motivo, manga.baja_movimiento_id = "BAJA_COMPROBADA", "FALTANTE", movement.id
    review.estado, review.used_at = "CONSUMIDA", _now()
    session.flush()
    return _complete(session, op_id, {"codigo": code, "comprobante": f"AJ-{str(movement.id)[:8].upper()}", "movimiento_id": str(movement.id), "kardex_movimiento_id": str(canonical.id)}, 201)


def _confirm(session, actor_id, data, operation_id):
    actor = _actor(session, actor_id, "INVENTARIO_AJUSTAR")
    code = _text(data.get("codigo"), "codigo")
    reason = _text(data.get("motivo"), "motivo", max_length=240)
    key = _text(data.get("clave"), "clave")
    manga = _manga_by_code(session, code, lock=True)
    op_id, replay, status = _operation(session, operation_id, "POST /inventario/confirmaciones", actor.id, {"codigo": code, "motivo": reason, "clave": key})
    if replay is not None:
        return replay, status
    if manga.estado != "PESADA_PENDIENTE_CONFIRMACION" or manga.neto_kg is None:
        raise ScmServiceError("WEIGHT_REQUIRED", "La manga requiere un pesaje físico vigente antes de confirmar.", status_code=409)
    link = manga.baja_movimiento_id
    canonical, existence = _project_kardex_positive(
        session, actor_id=actor.id, operation_id=op_id,
        manga=manga, net=Decimal(manga.neto_kg), reason=reason,
    )
    movement = ScmMesaInventarioMovimiento(
        id=uuid.uuid4(), manga_id=manga.id,
        tipo="REINGRESO" if link else "ALTA_INVENTARIO",
        delta_kg=Decimal(manga.neto_kg), motivo=reason, clave=key,
        vinculo_baja_id=link, actor_id=actor.id,
        kardex_movimiento_id=canonical.id,
    )
    session.add(movement)
    manga.estado, manga.baja_motivo, manga.baja_movimiento_id = "DISPONIBLE", None, None
    session.flush()
    return _complete(session, op_id, {"codigo": code, "movimiento_id": str(movement.id), "kardex_movimiento_id": str(canonical.id), "vinculo_baja": str(link) if link else None}, 201)


def dispatch(session, actor_id, method, path, data=None, operation_id=None):
    """Dispatch a relative ``/inventario`` command using the server actor."""
    data = data if isinstance(data, dict) else {}
    method = str(method or "GET").upper()
    parts = [part for part in str(path or "").strip("/").split("/") if part]
    if parts and parts[0] == "inventario":
        parts = parts[1:]
    try:
        if method != "GET":
            # Serialize the pilot's productive writes with the same KG lock as
            # the canonical ledger.  This covers sessions/readings as well as
            # balance mutations and closes the select-then-insert campaign race.
            acquire_kg_productive_write_lock(session)
        if method == "GET" and parts == ["estado"]:
            return vista_state(session, actor_id)
        if method == "POST" and parts == ["sesiones"]:
            return _open_session(session, actor_id, data, operation_id or uuid.uuid4())
        if len(parts) == 3 and parts[0] == "sesiones" and parts[2] == "lecturas":
            if method == "POST":
                return _read_session(session, actor_id, parts[1], data, operation_id or uuid.uuid4())
        if len(parts) == 4 and parts[0] == "sesiones" and parts[2] == "lecturas" and method == "DELETE":
            return _delete_reading(session, actor_id, parts[1], parts[3], data, operation_id or uuid.uuid4())
        if len(parts) == 3 and parts[0] == "sesiones" and parts[2] == "cerrar" and method == "POST":
            return _close_session(session, actor_id, parts[1], data, operation_id or uuid.uuid4())
        if method == "POST" and parts == ["antiguas"]:
            return _old_unit(session, actor_id, data, operation_id or uuid.uuid4())
        if method == "POST" and parts == ["pesajes"]:
            return _weigh(session, actor_id, data, operation_id or uuid.uuid4())
        if method == "POST" and parts == ["revision"]:
            return _review(session, actor_id, data, operation_id or uuid.uuid4())
        if method == "POST" and parts == ["faltantes"]:
            return _missing(session, actor_id, data, operation_id or uuid.uuid4())
        if method == "POST" and parts == ["confirmaciones"]:
            return _confirm(session, actor_id, data, operation_id or uuid.uuid4())
    except ScmServiceError:
        _rollback(session)
        raise
    except Exception:
        _rollback(session)
        raise
    raise ScmServiceError("MESA_ROUTE_NOT_FOUND", "Operación de inventario no reconocida.", status_code=404)


__all__ = [
    "dispatch", "label", "vista_state", "vista_state_conteo",
    "vista_state_revision", "ZONES", "STATION_ID",
]
