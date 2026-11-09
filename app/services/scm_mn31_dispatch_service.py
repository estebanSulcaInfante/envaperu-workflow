"""Atomic direct-client dispatch for MN31 custody KG identities.

This service deliberately does not call the Armado withdrawal flow.  A MN31
dispatch has its own causal movement, terminal state and audit event.
"""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import re
from uuid import UUID, uuid5

from sqlalchemy import select

from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmSaldoInventarioKg,
    ScmUnidadFisicaKg,
    ScmReservaUnidadKg,
)
from app.models.scm_mn31 import ScmClienteMn31, ScmMn31LoteAsignacion
from app.models.scm_ot import ScmControlPesoManga, ScmCorreccionPesajeManga, ScmManga, ScmPesajeManga, ScmTramoMangaTrabajo
from app.models.registro import RegistroDiarioProduccion
from app.services.scm_weighing_service import _effective_projection
from app.services.scm_kg_custody_service import (
    assert_custody_enabled,
    _active_reservation,
    _assert_kg_location_scope,
    _complete,
    _hash,
    _lock_unit_custody,
    _reserve_operation,
    _scope_unit,
    _unit_by_code,
)
from app.services.scm_service_support import (
    ScmServiceError,
    acquire_kg_productive_write_lock,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
)


MN31_SKU = "PC-000468"
MN31_LOT_NAMESPACE = UUID("62d8b48f-f0ea-44d1-9e6b-31d9c1bcf531")
MN31_ENDPOINT = "POST /despachos-mn31/confirmar"


def _decimal(value):
    return format(Decimal(value).quantize(Decimal("0.001")), "f") if value is not None else None


def _suggested_lot_code(manga, candidate_codes):
    ot = getattr(manga, "ot", None)
    ot_date = getattr(ot, "fecha", None)
    shift = getattr(ot, "turno", None)
    if not isinstance(ot_date, date) or shift not in {"DIA", "NOCHE", "DIURNO", "NOCTURNO"}:
        return None
    shift_code = "D" if shift in {"DIA", "DIURNO"} else "N"
    prefix = f"MN31-{ot_date:%Y%m%d}-{shift_code}-"
    numbers = [
        int(match.group(1))
        for code in candidate_codes
        if (match := re.fullmatch(re.escape(prefix) + r"(\d{2,})", code))
    ]
    return f"{prefix}{(max(numbers, default=0) + 1):02d}"


def _production_signature(manga):
    ot = getattr(manga, "ot", None)
    work = getattr(manga, "trabajo", None)
    work_color = getattr(work, "trabajo_color", None)
    return (
        getattr(ot, "maquina_id", None),
        getattr(ot, "maquina_codigo_snapshot", None),
        getattr(work_color, "molde_codigo_snapshot", None),
        getattr(work_color, "receta_revision_id_snapshot", None),
        getattr(work_color, "receta_hash_snapshot", None),
    )


def _observable_production_change(current, candidate):
    current_signature = _production_signature(current)
    candidate_signature = _production_signature(candidate)
    comparable = [(left, right) for left, right in zip(current_signature, candidate_signature) if left is not None and right is not None]
    return bool(comparable) and any(left != right for left, right in comparable)


def _lot_response(lot, *, assigned):
    return {
        "id": str(lot["id"]),
        "codigo": lot["codigo"],
        "criterio": lot.get("criterio") or lot.get("fuente") or "MN31_CONFIRMACION",
        "asignado": assigned,
    }


def _lot_for_manga(session, manga, *, selected_code=None, productive_change=False, forced_lot=None):
    assigned = session.scalar(select(ScmMn31LoteAsignacion).where(ScmMn31LoteAsignacion.manga_id == manga.id))
    if forced_lot is not None:
        if assigned is not None and (assigned.lote_codigo != forced_lot["codigo"] or str(assigned.lote_public_id) != str(forced_lot["id"])):
            raise ScmServiceError("MN31_LOT_BLOCK_CONFLICT", "La manga ya tiene un lote distinto al bloque reservado.", status_code=409)
        return forced_lot
    if assigned is not None:
        return _lot_response(assigned.to_dict(), assigned=True)
    current_ot = getattr(manga, "ot", None)
    current_date = getattr(current_ot, "fecha", None)
    current_shift = getattr(current_ot, "turno", None)
    candidates = []
    if current_date is not None and current_shift is not None:
        candidates = list(session.scalars(
            select(ScmMn31LoteAsignacion)
            .join(ScmManga, ScmManga.id == ScmMn31LoteAsignacion.manga_id)
            .join(RegistroDiarioProduccion, RegistroDiarioProduccion.id == ScmManga.ot_id)
            .where(
                RegistroDiarioProduccion.fecha == current_date,
                RegistroDiarioProduccion.turno == current_shift,
                ScmManga.pieza_color_sku_snapshot == manga.pieza_color_sku_snapshot,
            )
        ).all())
    candidate_codes = {candidate.lote_codigo for candidate in candidates}
    change_detected = any(_observable_production_change(manga, candidate.manga) for candidate in candidates)
    if selected_code:
        if not selected_code.startswith("MN31-") or len(selected_code) > 80:
            raise ScmServiceError("MN31_LOT_SELECTION_INVALID", "El código de lote no cumple el formato MN31.", status_code=422)
        selected = next((candidate for candidate in candidates if candidate.lote_codigo == selected_code), None)
        suggested = _suggested_lot_code(manga, candidate_codes)
        selected_change = selected is not None and _observable_production_change(manga, selected.manga)
        if (productive_change or selected_change) and selected_code != suggested:
            raise ScmServiceError(
                "MN31_LOT_SELECTION_REQUIRED",
                "Un cambio productivo requiere un lote nuevo autorizado; no se puede reutilizar un lote existente.",
                status_code=409,
                details={"candidatos": sorted(candidate_codes), "candidato_sugerido": suggested, "motivo": "CAMBIO_PRODUCTIVO_REQUIERE_NUEVO_LOTE"},
            )
        if (productive_change or selected_change) and selected_code == suggested:
            return {"id": str(uuid5(MN31_LOT_NAMESPACE, selected_code)), "codigo": selected_code, "criterio": "SELECCION_NUEVO_LOTE_CAMBIO_PRODUCTIVO", "asignado": False}
        if selected is not None:
            return _lot_response(selected.to_dict(), assigned=False)
        if not candidates and not productive_change:
            ot = getattr(manga, "ot", None)
            ot_date = getattr(ot, "fecha", None)
            shift = getattr(ot, "turno", None)
            if isinstance(ot_date, date) and shift in {"DIA", "NOCHE", "DIURNO", "NOCTURNO"}:
                shift_code = "D" if shift in {"DIA", "DIURNO"} else "N"
                suggested = f"MN31-{ot_date:%Y%m%d}-{shift_code}-01"
                if selected_code == suggested:
                    return {"id": str(uuid5(MN31_LOT_NAMESPACE, selected_code)), "codigo": selected_code, "criterio": "SELECCION_PRIMER_LOTE_TURNO", "asignado": False}
        raise ScmServiceError("MN31_LOT_SELECTION_INVALID", "El lote elegido no pertenece al conjunto documental disponible.", status_code=409, details={"candidatos": sorted(candidate_codes)})
    if productive_change or change_detected or len(candidate_codes) > 1:
        details = {"candidatos": sorted(candidate_codes)}
        if productive_change or change_detected:
            details["candidato_sugerido"] = _suggested_lot_code(manga, candidate_codes)
        raise ScmServiceError("MN31_LOT_SELECTION_REQUIRED", "Existe cambio productivo o más de un lote candidato; seleccione lote.", status_code=409, details=details)
    ot = getattr(manga, "ot", None)
    ot_date = getattr(ot, "fecha", None)
    shift = getattr(ot, "turno", None)
    if len(candidate_codes) == 1:
        code = next(iter(candidate_codes))
        candidate = next(candidate for candidate in candidates if candidate.lote_codigo == code)
        return _lot_response(candidate.to_dict(), assigned=False)
    if not isinstance(ot_date, date) or shift not in {"DIA", "NOCHE", "DIURNO", "NOCTURNO"}:
        raise ScmServiceError("MN31_LOT_AMBIGUOUS", "La OT no permite determinar fecha/turno de lote.", status_code=409)
    code = _suggested_lot_code(manga, candidate_codes)
    raise ScmServiceError("MN31_LOT_SELECTION_REQUIRED", "Primera apertura de turno: confirme explícitamente el candidato de lote.", status_code=409, details={"candidatos": [], "candidato_sugerido": code})


def _weight_source(session, manga, unit, existence=None):
    final = session.scalar(
        select(ScmPesajeManga)
        .where(ScmPesajeManga.manga_id == manga.id, ScmPesajeManga.estado == "VIGENTE")
        .order_by(ScmPesajeManga.pesada_at.desc(), ScmPesajeManga.id.desc())
    )
    control = session.scalar(
        select(ScmControlPesoManga)
        .where(
            ScmControlPesoManga.manga_id == manga.id,
            ScmControlPesoManga.unidad_evidencia == "KG",
        )
        .order_by(ScmControlPesoManga.pesado_at.desc(), ScmControlPesoManga.id.desc())
    )
    candidates = []
    if final is not None:
        projection = _effective_projection(final)
        final_net = Decimal(str(projection.get("peso_fisico_neto_kg") or 0))
        if final_net > 0:
            applied = session.scalar(select(ScmCorreccionPesajeManga).where(ScmCorreccionPesajeManga.pesaje_id == final.id, ScmCorreccionPesajeManga.estado == "APLICADA").order_by(ScmCorreccionPesajeManga.id.desc()))
            effective_at = final.pesada_at
            if projection.get("pesada_at"):
                try:
                    effective_at = datetime.fromisoformat(str(projection["pesada_at"]))
                except ValueError:
                    effective_at = final.pesada_at
            candidates.append((effective_at, final_net, "PESAJE_FINAL_CORREGIDO" if applied else "PESAJE_FINAL", final.public_id))
    if control is not None and Decimal(control.peso_neto_kg) > 0:
        candidates.append((control.pesado_at, Decimal(control.peso_neto_kg), "CONTROL_AVANCE", control.public_id))
    if candidates:
        source_at, weight, source_type, source_id = max(candidates, key=lambda item: (item[0], str(item[3])))
        return weight.quantize(Decimal("0.001")), source_type, source_id, source_at
    raise ScmServiceError(
        "MN31_WEIGHT_REQUIRED",
        "La manga no tiene un peso neto positivo autorizado.",
        status_code=409,
    )


def _manga_payload(manga, weight, source_type, source_id, lot, client=None):
    ot = getattr(manga, "ot", None)
    return {
        "id": str(manga.public_id),
        "codigo": manga.codigo,
        "id_humano": manga.codigo,
        "estado": manga.estado,
        "version": manga.version,
        "ot": {
            "id": getattr(ot, "id", None),
            "codigo": getattr(ot, "codigo_ot", None),
            "fecha": getattr(ot, "fecha", None).isoformat() if getattr(ot, "fecha", None) else None,
            "turno": getattr(ot, "turno", None),
        },
        "fecha_ot": getattr(ot, "fecha", None).isoformat() if getattr(ot, "fecha", None) else None,
        "turno_origen": getattr(ot, "turno", None),
        "peso_autoritativo_kg": _decimal(weight),
        "fuente_peso": {"tipo": source_type, "id": str(source_id) if source_id else None},
        "lote": lot,
        "cliente": client.to_dict() if client else None,
    }


def _resolution_token(manga, unit, weight, source_type, source_id):
    return {
        "manga": manga.version,
        "manga_id": str(manga.public_id),
        "unidad": unit.version if unit is not None else None,
        "unidad_id": str(unit.id) if unit is not None else None,
        "peso_fuente": {
            "id": str(source_id) if source_id else None,
            "tipo": source_type,
            "kg": _decimal(weight),
        },
    }


def _require_resolution_token(data):
    token = data.get("version_token") or data.get("evidencia")
    if not isinstance(token, dict):
        raise ScmServiceError("MN31_RESOLUTION_TOKEN_REQUIRED", "La confirmación requiere el token de resolución vigente.", status_code=422)
    return token


def _validate_resolution_token(token, manga, unit, weight, source_type, source_id):
    if token.get("manga_id") != str(manga.public_id) or token.get("manga") != manga.version:
        raise ScmServiceError("VERSION_CONFLICT", "La manga cambió desde la resolución.", status_code=409)
    expected_unit_version = unit.version if unit is not None else None
    expected_unit_id = str(unit.id) if unit is not None else None
    if token.get("unidad_id") != expected_unit_id or token.get("unidad") != expected_unit_version:
        raise ScmServiceError("VERSION_CONFLICT", "La identidad KG cambió desde la resolución.", status_code=409)
    source = token.get("peso_fuente")
    if not isinstance(source, dict) or str(source.get("id") or "") != str(source_id) or source.get("tipo") != source_type:
        raise ScmServiceError("MN31_WEIGHT_CONFLICT", "La fuente de peso cambió desde la resolución.", status_code=409)
    try:
        token_weight = Decimal(str(source.get("kg"))).quantize(Decimal("0.001"))
    except (ArithmeticError, TypeError, ValueError):
        raise ScmServiceError("MN31_WEIGHT_CONFLICT", "El token de peso no es válido.", status_code=409)
    if token_weight != Decimal(weight).quantize(Decimal("0.001")):
        raise ScmServiceError("MN31_WEIGHT_CONFLICT", "El peso autorizado cambió desde la resolución.", status_code=409)


def _source_snapshot(session, source_type, source_id, weight, source_at):
    row = None
    if source_type.startswith("CONTROL"):
        row = session.scalar(select(ScmControlPesoManga).where(ScmControlPesoManga.public_id == source_id))
    elif source_type.startswith("PESAJE"):
        row = session.scalar(select(ScmPesajeManga).where(ScmPesajeManga.public_id == source_id))
    payload = {"tipo": source_type, "id": str(source_id) if source_id else None, "neto_kg": _decimal(weight), "pesado_at": source_at.isoformat() if source_at else None}
    if row is not None:
        effective = {"peso_bruto_kg": row.peso_bruto_kg, "tara_kg": row.tara_kg, "pesada_at": source_at}
        correction = session.scalar(select(ScmCorreccionPesajeManga).where(ScmCorreccionPesajeManga.pesaje_id == row.id, ScmCorreccionPesajeManga.estado == "APLICADA").order_by(ScmCorreccionPesajeManga.id.desc()))
        if correction is not None:
            projection = correction.result_projection_json or {}
            effective.update({key: projection[key] for key in ("peso_bruto_kg", "tara_kg", "pesada_at") if key in projection})
            payload["correccion_aplicada_id"] = str(correction.public_id)
        effective_at = effective["pesada_at"]
        if isinstance(effective_at, str):
            try:
                effective_at = datetime.fromisoformat(effective_at)
            except ValueError:
                effective_at = source_at
        payload.update({"bruto_kg": _decimal(effective["peso_bruto_kg"]), "tara_kg": _decimal(effective["tara_kg"]), "pesado_at": effective_at.isoformat() if effective_at else None, "pesado_por_id": row.pesado_por_id})
    return payload


def _applied_correction_id(session, source_type, source_id):
    if not source_id or not source_type.startswith("PESAJE"):
        return None
    weighing = session.scalar(select(ScmPesajeManga).where(ScmPesajeManga.public_id == source_id))
    if weighing is None:
        return None
    correction = session.scalar(
        select(ScmCorreccionPesajeManga)
        .where(
            ScmCorreccionPesajeManga.pesaje_id == weighing.id,
            ScmCorreccionPesajeManga.estado == "APLICADA",
        )
        .order_by(ScmCorreccionPesajeManga.id.desc())
    )
    return correction.public_id if correction is not None else None


def _materialize_open_manga_identity(session, *, actor, manga, operation_id, source_type, source_id, source_at, weight):
    """Create the canonical KG004 identity only for an unreceived manga.

    This is deliberately limited to the no-existence case.  A historical
    existence without its unit is a damaged custody projection and must not
    be silently rebuilt by dispatch.
    """
    from app.services.scm_kg_production_service import _production_location, sync_kg_production_inventory

    article = getattr(getattr(manga, "lote_articulo", None), "articulo", None)
    if article is None or article.codigo != MN31_SKU:
        raise ScmServiceError("MN31_SKU_REQUIRED", "La manga no corresponde a PC-000468.", status_code=409)
    location = _production_location(session, article_class=article.clase)
    _assert_kg_location_scope(session, actor_id=actor.id, location=location, article_class=article.clase)
    sync_source_type = "PESAJE_FINAL" if source_type.startswith("PESAJE") else source_type
    sync_kg_production_inventory(
        session,
        actor_id=actor.id,
        manga=manga,
        net_kg=weight,
        operation_id=operation_id,
        source_type=sync_source_type,
        source_id=source_id,
        source_at=source_at,
        correction_id=_applied_correction_id(session, source_type, source_id),
        final=True,
        force=True,
        article=article,
        location=location,
    )
    session.flush()
    existence = session.scalar(
        select(ScmExistenciaMangaKg)
        .where(ScmExistenciaMangaKg.manga_id == manga.id)
        .with_for_update()
    )
    unit = session.scalar(
        select(ScmUnidadFisicaKg)
        .where(ScmUnidadFisicaKg.recepcion_vigente_id == existence.id)
        .with_for_update()
    ) if existence is not None else None
    if existence is None or unit is None:
        raise ScmServiceError(
            "KG_IDENTITY_MISSING",
            "La entrada KG004 no produjo una identidad física vigente.",
            status_code=409,
        )
    return unit, existence


def _persist_lot_assignment(session, manga, lot):
    assigned = session.scalar(select(ScmMn31LoteAsignacion).where(ScmMn31LoteAsignacion.manga_id == manga.id).with_for_update())
    if assigned is not None:
        return _lot_response(assigned.to_dict(), assigned=True)
    assigned = ScmMn31LoteAsignacion(
        manga_id=manga.id, manga_public_id=manga.public_id, manga_codigo=manga.codigo,
        lote_public_id=UUID(str(lot["id"])), lote_codigo=lot["codigo"],
        fuente=lot.get("criterio", "MN31_CONFIRMACION"),
    )
    session.add(assigned)
    session.flush()
    return _lot_response(assigned.to_dict(), assigned=True)


def list_mn31_clients(session, *, actor_id, query=None):
    load_actor(session, actor_id, capability="DESPACHO_MN31_CONFIRMAR")
    statement = select(ScmClienteMn31).where(ScmClienteMn31.activo.is_(True)).order_by(ScmClienteMn31.nombre)
    needle = str(query or "").strip()
    if needle:
        statement = statement.where(
            (ScmClienteMn31.ruc.ilike(f"%{needle}%"))
            | (ScmClienteMn31.nombre.ilike(f"%{needle}%"))
        )
    return {"items": [item.to_dict() for item in session.scalars(statement.limit(100)).all()]}


def resolve_mn31_dispatch(session, *, actor_id, code, client_id=None, lote_codigo=None, hubo_cambio_productivo=False):
    load_actor(session, actor_id, capability="DESPACHO_MN31_CONFIRMAR")
    actor = load_actor(session, actor_id)
    unit = _unit_by_code(session, code, actor=actor)
    manga_scan = None
    if unit is None:
        manga_scan = session.scalar(select(ScmManga).where(ScmManga.codigo == code))
        if manga_scan is not None:
            unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.recepcion_vigente_id.in_(select(ScmExistenciaMangaKg.id).where(ScmExistenciaMangaKg.manga_id == manga_scan.id))))
    if unit is None and manga_scan is None:
        raise ScmServiceError("KG_UNIT_NOT_FOUND", "La identidad KG no existe.", status_code=404)
    existence = session.get(ScmExistenciaMangaKg, unit.recepcion_vigente_id) if unit is not None and unit.recepcion_vigente_id else None
    if unit is None and manga_scan is not None:
        existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga_scan.id))
        if existence is not None:
            raise ScmServiceError("KG_IDENTITY_MISSING", "La existencia KG histórica no tiene identidad vigente; no se reconstruye automáticamente.", status_code=409)
    if unit is not None and unit.codigo != code and not manga_scan:
        raise ScmServiceError("MN31_IDENTITY_MISMATCH", "El código escaneado no coincide exactamente.", status_code=409)
    if unit is not None:
        _scope_unit(session, actor, unit)
    if unit is None and existence is not None and existence.ubicacion is not None:
        _assert_kg_location_scope(session, actor_id=actor.id, location=existence.ubicacion, article_class=existence.articulo.clase)
    if unit is not None and (unit.articulo is None or unit.articulo.codigo != MN31_SKU):
        raise ScmServiceError("MN31_SKU_REQUIRED", "La identidad no corresponde a PC-000468.", status_code=409)
    if unit is None and manga_scan is not None and manga_scan.pieza_color_sku_snapshot != MN31_SKU:
        raise ScmServiceError("MN31_SKU_REQUIRED", "La manga no corresponde a PC-000468.", status_code=409)
    if unit is not None and unit.estado != "ACTIVA":
        raise ScmServiceError("MN31_ALREADY_DISPATCHED", "La identidad ya no está vigente para despacho.", status_code=409)
    manga = existence.manga if existence is not None else manga_scan
    if manga is None or manga.pieza_color_sku_snapshot != MN31_SKU:
        raise ScmServiceError("MN31_MANGA_NOT_FOUND", "La identidad no tiene una manga MN31 asociada.", status_code=409)
    if manga.estado == "ANULADA":
        raise ScmServiceError("MN31_MANGA_ANULADA", "La manga anulada no puede despacharse.", status_code=409)
    if unit is not None and (unit.estado_calidad != "SIN_CONTROL" or unit.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION"} or existence is None or existence.estado_calidad != "SIN_CONTROL" or existence.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION"}):
        raise ScmServiceError("MN31_STOCK_NOT_AVAILABLE", "La identidad no tiene custodia KG acreditada disponible.", status_code=409)
    if unit is None:
        from app.services.scm_kg_production_service import _production_location
        article = getattr(getattr(manga, "lote_articulo", None), "articulo", None)
        if article is None or article.codigo != MN31_SKU:
            raise ScmServiceError("MN31_SKU_REQUIRED", "La manga no corresponde a PC-000468.", status_code=409)
        location = _production_location(session, article_class=article.clase)
        _assert_kg_location_scope(session, actor_id=actor.id, location=location, article_class=article.clase)
    weight, source_type, source_id, _source_at = _weight_source(session, manga, unit, existence)
    lot = _lot_for_manga(session, manga, selected_code=lote_codigo, productive_change=hubo_cambio_productivo)
    client = session.get(ScmClienteMn31, int(client_id)) if client_id is not None else None
    if client_id is not None and (client is None or not client.activo):
        raise ScmServiceError("MN31_CLIENT_NOT_FOUND", "El cliente no existe o no está activo.", status_code=404)
    return {
        "sku": MN31_SKU,
        "unidad": ({
            "id": str(unit.id), "public_id": str(unit.public_id), "codigo": unit.codigo,
            "version": unit.version, "estado": unit.estado,
            "estado_logistico": unit.estado_logistico, "estado_calidad": unit.estado_calidad,
        } if unit is not None else None),
        "manga": _manga_payload(manga, weight, source_type, source_id, lot, client),
        "existencia": ({
            "id": str(existence.id), "estado_logistico": existence.estado_logistico,
            "estado_calidad": existence.estado_calidad, "cantidad_fisica_kg": _decimal(existence.cantidad_fisica_kg),
        } if existence is not None else {"id": None, "estado_logistico": "SIN_INGRESO_KG004", "estado_calidad": "SIN_CONTROL", "cantidad_fisica_kg": "0.000"}),
        "cliente_seleccionado": client.to_dict() if client else None,
        "confirmable": True,
        "requiere_ingreso_kg004": unit is None,
        "version_token": _resolution_token(manga, unit, weight, source_type, source_id),
    }


def _child_operation(session, root_id, suffix, actor, command):
    child_id = uuid5(root_id, suffix)
    child, replay = _reserve_operation(session, child_id, f"{MN31_ENDPOINT}/{suffix}", actor, command)
    if replay is not None:
        raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La operación hija ya existe.", status_code=409)
    return child


def confirm_mn31_dispatch(session, *, actor_id, operation_id, data, manage_transaction=True, batch_parent_operation_id=None, lot_selection=None):
    assert_custody_enabled()
    actor = load_actor(session, actor_id, capability="DESPACHO_MN31_CONFIRMAR")
    reject_unknown_fields(data, allowed={"codigo", "unit_id", "version", "version_token", "evidencia", "cliente_id", "documento", "motivo", "lote_codigo", "hubo_cambio_productivo"})
    code = str(data.get("codigo") or "").strip()
    if not code:
        raise ScmServiceError("MN31_CODE_REQUIRED", "codigo es obligatorio.", status_code=400)
    if not isinstance(data.get("cliente_id"), int) or isinstance(data.get("cliente_id"), bool):
        raise ScmServiceError("MN31_CLIENT_REQUIRED", "cliente_id es obligatorio.", status_code=422)
    resolution_token = _require_resolution_token(data)
    if data.get("version") is None:
        fallback_version = resolution_token.get("unidad") if resolution_token.get("unidad") is not None else resolution_token.get("manga")
        if fallback_version is None:
            raise ScmServiceError("MN31_VERSION_REQUIRED", "version es obligatoria y debe coincidir con la resolución.", status_code=422)
        data = {**data, "version": fallback_version}
    client = session.get(ScmClienteMn31, data["cliente_id"])
    if client is None or not client.activo:
        raise ScmServiceError("MN31_CLIENT_NOT_FOUND", "El cliente no existe o no está activo.", status_code=404)
    reason = str(data.get("motivo") or "Despacho directo MN31").strip() or "Despacho directo MN31"
    document = str(data.get("documento") or "").strip()
    productive_change = data.get("hubo_cambio_productivo") is True or str(data.get("hubo_cambio_productivo") or "").strip().lower() == "true"
    command = {"codigo": code, "unit_id": str(data.get("unit_id") or ""), "version": data.get("version"), "version_token": resolution_token, "cliente_id": client.id, "documento": document or None, "motivo": reason, "lote_codigo": str(data.get("lote_codigo") or "").strip() or None, "hubo_cambio_productivo": productive_change}
    acquire_kg_productive_write_lock(session)
    operation, replay = _reserve_operation(session, operation_id, MN31_ENDPOINT, actor, command)
    if replay is not None:
        return deepcopy(replay)
    try:
        materialized_from_evidence = False
        unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.codigo == code).with_for_update())
        manga_by_code = None
        if unit is None:
            manga_by_code = session.scalar(select(ScmManga).where(ScmManga.codigo == code).with_for_update())
            if manga_by_code is not None:
                existence_by_manga = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga_by_code.id, ScmExistenciaMangaKg.unidad_fisica_kg_id.is_not(None)).with_for_update())
                if existence_by_manga is not None:
                    unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == existence_by_manga.unidad_fisica_kg_id).with_for_update())
        existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.unidad_fisica_kg_id == unit.id).with_for_update()) if unit is not None else None
        if existence is None and unit is not None and unit.recepcion_vigente_id:
            existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.id == unit.recepcion_vigente_id).with_for_update())
        if existence is None and unit is None:
            if manga_by_code is not None:
                existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga_by_code.id).with_for_update())
        manga = existence.manga if existence is not None else manga_by_code
        if manga is None or manga.pieza_color_sku_snapshot != MN31_SKU:
            raise ScmServiceError("MN31_MANGA_NOT_FOUND", "La identidad no tiene una manga MN31 asociada.", status_code=409)
        manga = session.scalar(select(ScmManga).where(ScmManga.id == manga.id).with_for_update())
        if manga.estado == "ANULADA":
            raise ScmServiceError("MN31_MANGA_ANULADA", "La manga anulada no puede despacharse.", status_code=409)
        if manga.estado == "DESPACHADA_CLIENTE_MN31":
            raise ScmServiceError("MN31_ALREADY_DISPATCHED", "La manga ya fue despachada.", status_code=409)
        if unit is None:
            if existence is not None:
                raise ScmServiceError("KG_IDENTITY_MISSING", "La existencia KG histórica no tiene identidad vigente; no se reconstruye automáticamente.", status_code=409)
            if data.get("unit_id"):
                raise ScmServiceError("MN31_IDENTITY_MISMATCH", "unit_id no corresponde a una manga sin identidad KG.", status_code=409)
            weight, source_type, source_id, source_at = _weight_source(session, manga, None, None)
            _validate_resolution_token(resolution_token, manga, None, weight, source_type, source_id)
            unit, existence = _materialize_open_manga_identity(
                session, actor=actor, manga=manga, operation_id=operation_id,
                source_type=source_type, source_id=source_id, source_at=source_at,
                weight=weight,
            )
            materialized_from_evidence = True
        unit = _lock_unit_custody(session, unit)
        if data.get("unit_id") and str(unit.id) != str(data["unit_id"]):
            raise ScmServiceError("MN31_IDENTITY_MISMATCH", "unit_id no corresponde al código escaneado.", status_code=409)
        if unit.version != expected_version(data["version"]):
            # Existing identities are versioned by custody; a just-materialized
            # identity is protected by the manga version check above.
            if not materialized_from_evidence:
                raise ScmServiceError("VERSION_CONFLICT", "La identidad cambió desde la lectura.", status_code=409)
        _scope_unit(session, actor, unit)
        if unit.articulo is None or unit.articulo.codigo != MN31_SKU:
            raise ScmServiceError("MN31_SKU_REQUIRED", "La identidad no corresponde a PC-000468.", status_code=409)
        if unit.estado != "ACTIVA":
            raise ScmServiceError("MN31_ALREADY_DISPATCHED", "La identidad ya no está vigente para despacho.", status_code=409)
        if unit.estado_calidad != "SIN_CONTROL" or unit.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION"}:
            raise ScmServiceError("MN31_STOCK_NOT_AVAILABLE", "La identidad no tiene custodia KG acreditada disponible.", status_code=409)
        if existence is None or existence.estado_calidad != "SIN_CONTROL" or existence.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION"}:
            raise ScmServiceError("MN31_STOCK_NOT_AVAILABLE", "La existencia KG no tiene custodia disponible.", status_code=409)
        weight, source_type, source_id, source_at = _weight_source(session, manga, unit, existence)
        _validate_resolution_token(resolution_token, manga, None if materialized_from_evidence else unit, weight, source_type, source_id)
        correction_id = _applied_correction_id(session, source_type, source_id)
        lot = _lot_for_manga(session, manga, selected_code=command["lote_codigo"], productive_change=command["hubo_cambio_productivo"], forced_lot=lot_selection)
        lot = _persist_lot_assignment(session, manga, lot)
        balance = session.scalar(select(ScmSaldoInventarioKg).where(ScmSaldoInventarioKg.id == unit.saldo_id).with_for_update())
        if balance is None or balance.articulo_scm_id != unit.articulo_scm_id or balance.ubicacion_id != unit.ubicacion_id:
            raise ScmServiceError("INVENTORY_CONFLICT", "La identidad no tiene saldo KG acreditado vigente.", status_code=409)
        if _active_reservation(session, unit.id) is not None:
            raise ScmServiceError("MN31_STOCK_RESERVED", "La identidad tiene una reserva activa.", status_code=409)
        _assert_kg_location_scope(session, actor_id=actor.id, location=balance.ubicacion, article_class=unit.articulo.clase)
        credited = Decimal(existence.cantidad_fisica_kg if existence is not None else 0).quantize(Decimal("0.001"))
        available = Decimal(balance.cantidad_fisica_kg) - Decimal(balance.cantidad_reservada_kg) - Decimal(balance.cantidad_no_disponible_kg)
        if available < credited:
            raise ScmServiceError("INVENTORY_CONFLICT", "El saldo KG libre no cubre la existencia acreditada.", status_code=409)
        delta = (weight - credited).quantize(Decimal("0.001"))
        if available + delta < weight:
            raise ScmServiceError("INVENTORY_CONFLICT", "El saldo KG libre no cubre el peso autorizado tras conciliar el delta.", status_code=409)
        before = {"manga_estado": manga.estado, "unidad_estado": unit.estado, "balance_kg": _decimal(balance.cantidad_fisica_kg), "peso_acreditado_kg": _decimal(credited)}
        if delta != 0:
            ingress_op = _child_operation(session, operation_id, "conciliacion", actor, command)
            balance.cantidad_fisica_kg = Decimal(balance.cantidad_fisica_kg) + delta
            balance.version += 1
            movement = ScmMovimientoInventarioKg(
                saldo_id=balance.id, tipo="INGRESO_PRODUCCION" if delta > 0 else "AJUSTE_NEGATIVO",
                cantidad_delta_kg=delta, saldo_fisico_resultante_kg=balance.cantidad_fisica_kg,
                motivo="Conciliación de peso autorizado MN31 antes de salida a cliente",
                referencia_tipo="DESPACHO_MN31", referencia_id=str(manga.public_id), actor_id=actor.id,
                operation_id=ingress_op.operation_id, pesaje_public_id=source_id if source_type.startswith("PESAJE") else None,
                correccion_aplicada_public_id=correction_id,
                projection_sha256=_hash([str(manga.public_id), str(source_id), _decimal(weight)]),
                peso_neto_snapshot_kg=weight, pesada_at_snapshot=source_at, fuente_tipo="MN31_CONCILIACION",
            )
            session.add(movement)
            session.flush()
            _complete(ingress_op, {"movement_id": str(movement.id), "delta_kg": _decimal(delta)}, 201)
        output_op = _child_operation(session, operation_id, "salida-cliente", actor, command)
        balance.cantidad_fisica_kg = Decimal(balance.cantidad_fisica_kg) - weight
        balance.cantidad_retirada_kg = Decimal(balance.cantidad_retirada_kg or 0) + weight
        balance.version += 1
        movement = ScmMovimientoInventarioKg(
            saldo_id=balance.id, tipo="SALIDA_CLIENTE_MN31", cantidad_delta_kg=-weight,
            saldo_fisico_resultante_kg=balance.cantidad_fisica_kg,
            motivo="Salida directa MN31 a cliente; no Armado",
            referencia_tipo="DESPACHO_MN31", referencia_id=str(manga.public_id), actor_id=actor.id,
            operation_id=output_op.operation_id, pesaje_public_id=source_id if source_type.startswith("PESAJE") else None,
            correccion_aplicada_public_id=correction_id,
            projection_sha256=_hash([str(unit.id), str(operation_id), client.ruc]),
            peso_neto_snapshot_kg=weight, pesada_at_snapshot=source_at, fuente_tipo="MN31_CLIENTE",
        )
        session.add(movement)
        session.flush()
        segments = session.scalars(
            select(ScmTramoMangaTrabajo)
            .where(ScmTramoMangaTrabajo.manga_id == manga.id)
            .order_by(ScmTramoMangaTrabajo.secuencia)
            .with_for_update()
        ).all()
        active_segment = next((item for item in reversed(segments) if item.estado == "ACTIVO"), None)
        if active_segment is not None:
            start = Decimal(active_segment.cantidad_inicio_kg or 0).quantize(Decimal("0.001"))
            if weight <= start:
                raise ScmServiceError("MN31_WEIGHT_CONFLICT", "El peso de despacho no supera el inicio del tramo.", status_code=409)
            active_segment.cantidad_fin_kg = weight
            active_segment.cantidad_atribuida_kg = (weight - start).quantize(Decimal("0.001"))
            active_segment.calidad_evidencia_kg = "MEDIDA_DIRECTA_DESPACHO_MN31"
            active_segment.estado = "CERRADO"
            active_segment.cerrada_at = source_at
            active_segment.motivo_cierre = "Cierre atómico por despacho directo MN31"
        manga.estado = "DESPACHADA_CLIENTE_MN31"
        manga.version += 1
        if existence is not None:
            existence.cantidad_fisica_kg = weight
            existence.peso_neto_snapshot_kg = weight
            existence.estado_logistico = "DESPACHADA_CLIENTE_MN31"
            existence.cantidad_reservada_kg = 0
            existence.version += 1
        unit.estado = "HISTORICA"
        unit.estado_logistico = "RETIRADA_CLIENTE_MN31"
        unit.saldo_id = None
        unit.ubicacion_id = None
        unit.version += 1
        session.flush()
        response = {
            "operation_id": str(operation_id), "estado": "CONFIRMADO", "cliente": client.to_dict(),
            "comprobante_scm": {"tipo": "DESPACHO_CLIENTE_MN31", "id": str(operation_id)},
            "despacho": {"codigo": code, "unidad_id": str(unit.id), "manga_id": str(manga.public_id), "manga_codigo": manga.codigo, "peso_neto_kg": _decimal(weight), "documento": command["documento"], "causa": "DESPACHO_CLIENTE_MN31", "lote": lot},
            "peso_fuente": _source_snapshot(session, source_type, source_id, weight, source_at),
            "conciliacion": {"delta_kg": _decimal(delta), "cantidad_acreditada_previa_kg": _decimal(credited), "movimiento_ingreso": delta > 0},
            "movimiento_salida_id": str(movement.id), "terminal": True,
        }
        if batch_parent_operation_id is not None:
            response["batch_parent_operation_id"] = str(batch_parent_operation_id)
        _complete(output_op, {"movement_id": str(movement.id), "parent_operation_id": str(operation_id)}, 201)
        _complete(operation, response, 201)
        session.add(ScmEvento(
            aggregate_type="DESPACHO_MN31", aggregate_id=str(manga.public_id), tipo="MN31_DESPACHO_CLIENTE_CONFIRMADO",
            actor_id=actor.id, actor_snapshot=actor_snapshot(actor), motivo=command["motivo"],
            before_json=before, after_json=response, operation_id=operation_id,
        ))
        if manage_transaction:
            session.commit()
        return response
    except Exception:
        if manage_transaction:
            session.rollback()
        raise


def _manga_for_batch_lot(session, code):
    unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.codigo == code))
    if unit is not None and unit.recepcion_vigente_id:
        existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.id == unit.recepcion_vigente_id))
        if existence is not None:
            return existence.manga
    manga = session.scalar(select(ScmManga).where(ScmManga.codigo == code))
    if manga is not None:
        return manga
    return None


def _preflight_batch_lot_selection(session, normalized):
    """Resolve each line's lot against one pre-write catalog snapshot."""
    prepared = []
    lot_by_code = {}
    for item in normalized:
        manga = _manga_for_batch_lot(session, item["codigo"])
        if manga is None:
            raise ScmServiceError("MN31_MANGA_NOT_FOUND", "Una manga del bloque no existe.", status_code=404, details={"codigo": item["codigo"]})
        lot = _lot_for_manga(
            session,
            manga,
            selected_code=item.get("lote_codigo"),
            productive_change=item.get("hubo_cambio_productivo", False),
        )
        prepared.append(item)
        lot_by_code[item["codigo"]] = lot
    return prepared, lot_by_code


def confirm_mn31_dispatch_batch(session, *, actor_id, operation_id, data):
    """Confirm several mangas under one idempotent, all-or-nothing operation."""
    assert_custody_enabled()
    actor = load_actor(session, actor_id, capability="DESPACHO_MN31_CONFIRMAR")
    reject_unknown_fields(data, allowed={"cliente_id", "items", "documento", "motivo"})
    if not isinstance(data.get("cliente_id"), int) or isinstance(data.get("cliente_id"), bool):
        raise ScmServiceError("MN31_CLIENT_REQUIRED", "cliente_id es obligatorio.", status_code=422)
    client = session.get(ScmClienteMn31, data["cliente_id"])
    if client is None or not client.activo:
        raise ScmServiceError("MN31_CLIENT_NOT_FOUND", "El cliente no existe o no está activo.", status_code=404)
    reason = str(data.get("motivo") or "Despacho directo MN31").strip() or "Despacho directo MN31"
    document = str(data.get("documento") or "").strip()
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ScmServiceError("MN31_ITEMS_REQUIRED", "items requiere al menos una manga.", status_code=422)
    if len(raw_items) > 200:
        raise ScmServiceError("MN31_ITEMS_TOO_MANY", "El lote MN31 supera 200 mangas.", status_code=422)
    normalized = []
    seen = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ScmServiceError("MN31_ITEM_INVALID", "Cada item debe ser un objeto.", status_code=422, details={"index": index})
        reject_unknown_fields(item, allowed={"codigo", "unit_id", "version", "version_token", "evidencia", "lote_codigo", "hubo_cambio_productivo"})
        code = str(item.get("codigo") or "").strip()
        if not code:
            raise ScmServiceError("MN31_CODE_REQUIRED", "Cada item requiere codigo.", status_code=422, details={"index": index})
        resolution_token = _require_resolution_token(item)
        if item.get("version") is None:
            fallback_version = resolution_token.get("unidad") if resolution_token.get("unidad") is not None else resolution_token.get("manga")
            if fallback_version is None:
                raise ScmServiceError("MN31_VERSION_REQUIRED", "Cada item requiere version de la resolución.", status_code=422, details={"index": index})
            item = {**item, "version": fallback_version}
        version = expected_version(item["version"])
        key = code
        if key in seen:
            raise ScmServiceError("MN31_DUPLICATE_ITEM", "Una identidad aparece más de una vez.", status_code=422, details={"codigo": code})
        seen.add(key)
        productive_change = item.get("hubo_cambio_productivo") is True or str(item.get("hubo_cambio_productivo") or "").strip().lower() == "true"
        normalized.append({"codigo": code, "unit_id": item.get("unit_id"), "version": version, "version_token": resolution_token, "lote_codigo": item.get("lote_codigo"), "hubo_cambio_productivo": productive_change})
    normalized.sort(key=lambda item: (str(item.get("unit_id") or ""), item["codigo"]))
    command = {"cliente_id": client.id, "items": normalized, "documento": document, "motivo": reason}
    acquire_kg_productive_write_lock(session)
    operation, replay = _reserve_operation(session, operation_id, MN31_ENDPOINT, actor, command)
    if replay is not None:
        return deepcopy(replay)
    try:
        normalized, lot_by_code = _preflight_batch_lot_selection(session, normalized)
        results = []
        total = Decimal("0")
        for index, item in enumerate(normalized):
            child_id = uuid5(operation_id, f"item:{index}:{item['codigo']}:{item.get('unit_id') or ''}")
            item_data = {
                "codigo": item["codigo"], "unit_id": item.get("unit_id"), "version": item["version"],
                "version_token": item["version_token"],
                "cliente_id": client.id, "documento": command["documento"], "motivo": reason,
                "lote_codigo": item.get("lote_codigo"), "hubo_cambio_productivo": item.get("hubo_cambio_productivo", False),
            }
            result = confirm_mn31_dispatch(
                session, actor_id=actor.id, operation_id=child_id,
                data=item_data, manage_transaction=False,
                batch_parent_operation_id=operation_id, lot_selection=lot_by_code[item["codigo"]],
            )
            results.append(result)
            total += Decimal(result["despacho"]["peso_neto_kg"])
        response = {
            "operation_id": str(operation_id), "estado": "CONFIRMADO", "cliente": client.to_dict(),
            "comprobante_scm": {"tipo": "DESPACHO_CLIENTE_MN31_LOTE", "id": str(operation_id)},
            "documento": command["documento"], "motivo": reason, "items": results,
            "cantidad_mangas": len(results), "total_kg": _decimal(total), "terminal": True,
        }
        _complete(operation, response, 201)
        session.add(ScmEvento(
            aggregate_type="DESPACHO_MN31_LOTE", aggregate_id=str(operation_id),
            tipo="MN31_DESPACHO_CLIENTE_LOTE_CONFIRMADO", actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor), motivo=reason,
            after_json=response, operation_id=operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def reverse_mn31_dispatch(session, *, actor_id, dispatch_operation_id, operation_id, data):
    """Compensate a client dispatch without reopening production weighing."""
    assert_custody_enabled()
    actor = load_actor(session, actor_id, capability="DESPACHO_MN31_CONFIRMAR")
    reject_unknown_fields(data, allowed={"motivo"})
    reason = str(data.get("motivo") or "").strip()
    if not reason:
        raise ScmServiceError("MN31_MOTIVO_REQUIRED", "motivo es obligatorio para revertir.", status_code=422)
    original = session.get(ScmOperacion, dispatch_operation_id)
    if original is None or original.endpoint != MN31_ENDPOINT or original.response_json is None:
        raise ScmServiceError("MN31_OPERATION_NOT_FOUND", "La operación MN31 no existe.", status_code=404)
    if original.response_json.get("batch_parent_operation_id"):
        raise ScmServiceError("MN31_BATCH_CHILD_NOT_REVERSIBLE", "Reversa el comprobante raíz del lote.", status_code=409)
    command = {"dispatch_operation_id": str(dispatch_operation_id), "motivo": reason}
    acquire_kg_productive_write_lock(session)
    operation, replay = _reserve_operation(session, operation_id, "POST /despachos-mn31/revertir", actor, command)
    if replay is not None:
        return deepcopy(replay)
    try:
        items = original.response_json.get("items") if isinstance(original.response_json, dict) else None
        if not items:
            items = [original.response_json]
        restored = []
        for index, item in enumerate(items):
            dispatch = item.get("despacho") or {}
            manga_id = dispatch.get("manga_id")
            unit_id = dispatch.get("unidad_id")
            if not manga_id or not unit_id:
                raise ScmServiceError("MN31_REVERSAL_UNRECOVERABLE", "El comprobante no conserva identidad suficiente.", status_code=409)
            unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == UUID(str(unit_id))).with_for_update())
            if unit is None:
                raise ScmServiceError("MN31_REVERSAL_UNRECOVERABLE", "La identidad KG no existe.", status_code=409)
            _scope_unit(session, actor, unit)
            manga = session.scalar(select(ScmManga).where(ScmManga.public_id == UUID(str(manga_id))).with_for_update())
            if manga is None or manga.estado != "DESPACHADA_CLIENTE_MN31":
                raise ScmServiceError("MN31_NOT_REVERSIBLE", "El despacho ya fue revertido o cambió de estado.", status_code=409)
            movement_id = item.get("movimiento_salida_id") or dispatch.get("movimiento_salida_id")
            movement = session.get(ScmMovimientoInventarioKg, UUID(str(movement_id))) if movement_id else None
            if movement is None:
                raise ScmServiceError("MN31_REVERSAL_UNRECOVERABLE", "No existe salida compensable.", status_code=409)
            current_movement = session.scalar(select(ScmMovimientoInventarioKg).where(ScmMovimientoInventarioKg.tipo == "SALIDA_CLIENTE_MN31", ScmMovimientoInventarioKg.referencia_id == str(manga.public_id)).order_by(ScmMovimientoInventarioKg.created_at.desc(), ScmMovimientoInventarioKg.id.desc()))
            if movement.tipo != "SALIDA_CLIENTE_MN31" or movement.referencia_id != str(manga.public_id) or current_movement is None or current_movement.id != movement.id:
                raise ScmServiceError("MN31_NOT_REVERSIBLE", "El comprobante ya no corresponde a la salida vigente de la manga.", status_code=409)
            balance = session.scalar(select(ScmSaldoInventarioKg).where(ScmSaldoInventarioKg.id == movement.saldo_id).with_for_update())
            if balance is None:
                raise ScmServiceError("MN31_REVERSAL_UNRECOVERABLE", "No existe saldo compensable.", status_code=409)
            qty = Decimal(movement.peso_neto_snapshot_kg).quantize(Decimal("0.001"))
            delta = Decimal(str((item.get("conciliacion") or {}).get("delta_kg") or "0")).quantize(Decimal("0.001"))
            child_id = uuid5(operation_id, f"reversa:{index}:{unit.id}")
            child, child_replay = _reserve_operation(session, child_id, "POST /despachos-mn31/revertir/movimiento", actor, command)
            if child_replay is not None:
                raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La reversa hija ya existe.", status_code=409)
            balance.cantidad_fisica_kg = Decimal(balance.cantidad_fisica_kg) + qty
            balance.cantidad_retirada_kg = max(Decimal("0"), Decimal(balance.cantidad_retirada_kg or 0) - qty)
            balance.version += 1
            compensating = ScmMovimientoInventarioKg(
                saldo_id=balance.id, tipo="AJUSTE_POSITIVO", cantidad_delta_kg=qty,
                saldo_fisico_resultante_kg=balance.cantidad_fisica_kg,
                motivo="Reversa compensatoria de despacho MN31 a cliente",
                referencia_tipo="REVERSA_DESPACHO_MN31", referencia_id=str(manga.public_id), actor_id=actor.id,
                operation_id=child_id, projection_sha256=_hash([str(movement.id), str(operation_id), reason]),
                peso_neto_snapshot_kg=qty, pesada_at_snapshot=movement.pesada_at_snapshot, fuente_tipo="MN31_REVERSA",
            )
            session.add(compensating)
            unit.estado = "ACTIVA"
            unit.estado_logistico = "DISPONIBLE_PRODUCCION"
            unit.saldo_id = balance.id
            unit.ubicacion_id = balance.ubicacion_id
            unit.version += 1
            existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.unidad_fisica_kg_id == unit.id).with_for_update())
            if existence is not None:
                existence.cantidad_fisica_kg = (qty - delta).quantize(Decimal("0.001"))
                existence.peso_neto_snapshot_kg = existence.cantidad_fisica_kg
                existence.estado_logistico = "DISPONIBLE_PRODUCCION"
                existence.version += 1
            if delta != 0:
                correction_id = uuid5(operation_id, f"reversa-conciliacion:{index}:{unit.id}")
                correction_op, correction_replay = _reserve_operation(session, correction_id, "POST /despachos-mn31/revertir/conciliacion", actor, command)
                if correction_replay is not None:
                    raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La compensación de conciliación ya existe.", status_code=409)
                correction_delta = -delta
                balance.cantidad_fisica_kg = Decimal(balance.cantidad_fisica_kg) + correction_delta
                balance.version += 1
                correction = ScmMovimientoInventarioKg(
                    saldo_id=balance.id, tipo="AJUSTE_POSITIVO" if correction_delta > 0 else "AJUSTE_NEGATIVO",
                    cantidad_delta_kg=correction_delta, saldo_fisico_resultante_kg=balance.cantidad_fisica_kg,
                    motivo="Reversa de conciliación de peso MN31",
                    referencia_tipo="REVERSA_DESPACHO_MN31", referencia_id=str(manga.public_id), actor_id=actor.id,
                    operation_id=correction_id, projection_sha256=_hash([str(movement.id), str(correction_id)]),
                    peso_neto_snapshot_kg=abs(correction_delta), pesada_at_snapshot=movement.pesada_at_snapshot, fuente_tipo="MN31_REVERSA",
                )
                session.add(correction)
                session.flush()
                _complete(correction_op, {"movement_id": str(correction.id), "delta_kg": _decimal(correction_delta)}, 201)
            manga.estado = "DESPACHO_MN31_REVERTIDO"
            manga.version += 1
            session.flush()
            _complete(child, {"movement_id": str(compensating.id), "manga_id": str(manga.public_id)}, 201)
            restored.append({"manga_id": str(manga.public_id), "unidad_id": str(unit.id), "kg": _decimal(qty), "movimiento_compensatorio_id": str(compensating.id)})
        response = {"operation_id": str(operation_id), "estado": "REVERTIDO", "comprobante_origen": str(dispatch_operation_id), "motivo": reason, "items": restored, "terminal_produccion_preservada": True}
        _complete(operation, response, 201)
        session.add(ScmEvento(aggregate_type="DESPACHO_MN31", aggregate_id=str(dispatch_operation_id), tipo="MN31_DESPACHO_CLIENTE_REVERTIDO", actor_id=actor.id, actor_snapshot=actor_snapshot(actor), motivo=reason, before_json=original.response_json, after_json=response, operation_id=operation_id))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def get_mn31_dispatch(session, *, actor_id, operation_id):
    actor = load_actor(session, actor_id, capability="DESPACHO_MN31_CONFIRMAR")
    operation = session.get(ScmOperacion, operation_id)
    if operation is None or operation.endpoint != MN31_ENDPOINT or operation.response_json is None:
        raise ScmServiceError("MN31_OPERATION_NOT_FOUND", "La operación MN31 no existe.", status_code=404)
    items = operation.response_json.get("items") if isinstance(operation.response_json, dict) else None
    if not items:
        items = [operation.response_json]
    for item in items:
        dispatch = item.get("despacho") if isinstance(item, dict) else None
        unit_id = dispatch.get("unidad_id") if isinstance(dispatch, dict) else None
        if not unit_id:
            raise ScmServiceError("MN31_OPERATION_NOT_FOUND", "El comprobante no conserva una identidad consultable.", status_code=404)
        unit = session.get(ScmUnidadFisicaKg, UUID(str(unit_id)))
        if unit is None:
            raise ScmServiceError("MN31_OPERATION_NOT_FOUND", "La identidad del comprobante no existe.", status_code=404)
        _scope_unit(session, actor, unit)
    return deepcopy(operation.response_json)
