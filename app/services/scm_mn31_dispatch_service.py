"""Atomic direct-client dispatch for MN31 custody KG identities.

This service deliberately does not call the Armado withdrawal flow.  A MN31
dispatch has its own causal movement, terminal state and audit event.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal
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
from app.models.scm_ot import ScmControlPesoManga, ScmManga, ScmPesajeManga, ScmTramoMangaTrabajo
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


def _lot_for_manga(session, manga):
    assigned = session.scalar(select(ScmMn31LoteAsignacion).where(ScmMn31LoteAsignacion.manga_id == manga.id))
    if assigned is not None:
        return assigned.to_dict()
    ot = getattr(manga, "ot", None)
    ot_date = getattr(ot, "fecha", None)
    shift = getattr(ot, "turno", None)
    if not isinstance(ot_date, date) or shift not in {"DIA", "NOCHE", "DIURNO", "NOCTURNO"}:
        raise ScmServiceError(
            "MN31_LOT_AMBIGUOUS",
            "No se puede determinar un lote independiente por turno de la OT.",
            status_code=409,
        )
    shift_code = "D" if shift in {"DIA", "DIURNO"} else "N"
    code = f"MN31-{ot_date:%Y%m%d}-{shift_code}-01"
    return {
        "id": None,
        "codigo": code,
        "fecha": ot_date.isoformat(),
        "turno": shift,
        "criterio": "OT_FECHA_TURNO",
    }


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
            candidates.append((final.pesada_at, final_net, "PESAJE_FINAL_CORREGIDO" if projection.get("correccion_aplicada_public_id") else "PESAJE_FINAL", final.public_id))
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


def _source_snapshot(session, source_type, source_id, weight, source_at):
    row = None
    if source_type.startswith("CONTROL"):
        row = session.scalar(select(ScmControlPesoManga).where(ScmControlPesoManga.public_id == source_id))
    elif source_type.startswith("PESAJE"):
        row = session.scalar(select(ScmPesajeManga).where(ScmPesajeManga.public_id == source_id))
    payload = {"tipo": source_type, "id": str(source_id) if source_id else None, "neto_kg": _decimal(weight), "pesado_at": source_at.isoformat() if source_at else None}
    if row is not None:
        payload.update({"bruto_kg": _decimal(row.peso_bruto_kg), "tara_kg": _decimal(row.tara_kg), "pesado_por_id": row.pesado_por_id})
    return payload


def _persist_lot_assignment(session, manga, lot):
    assigned = session.scalar(select(ScmMn31LoteAsignacion).where(ScmMn31LoteAsignacion.manga_id == manga.id).with_for_update())
    if assigned is not None:
        return assigned.to_dict()
    assigned = ScmMn31LoteAsignacion(
        manga_id=manga.id, manga_public_id=manga.public_id, manga_codigo=manga.codigo,
        lote_public_id=uuid5(MN31_LOT_NAMESPACE, str(manga.public_id)), lote_codigo=lot["codigo"],
        fuente="OT_FECHA_TURNO_CONFIRMACION",
    )
    session.add(assigned)
    session.flush()
    return assigned.to_dict()


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


def resolve_mn31_dispatch(session, *, actor_id, code, client_id=None):
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
    if unit is None:
        raise ScmServiceError("KG_IDENTITY_MISSING", "La manga no tiene una identidad KG vigente; no se reconstruye automáticamente.", status_code=409)
    if unit is not None and unit.codigo != code and not manga_scan:
        raise ScmServiceError("MN31_IDENTITY_MISMATCH", "El código escaneado no coincide exactamente.", status_code=409)
    if unit is not None:
        _scope_unit(session, actor, unit)
    existence = session.get(ScmExistenciaMangaKg, unit.recepcion_vigente_id) if unit is not None and unit.recepcion_vigente_id else None
    if existence is None and manga_scan is not None:
        existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga_scan.id))
    if unit is None and existence is not None and existence.ubicacion is not None:
        _assert_kg_location_scope(session, actor_id=actor.id, location=existence.ubicacion, article_class=existence.articulo.clase)
    if unit is not None and (unit.articulo is None or unit.articulo.codigo != MN31_SKU):
        raise ScmServiceError("MN31_SKU_REQUIRED", "La identidad no corresponde a PC-000468.", status_code=409)
    if unit is None and (existence is None or existence.articulo is None or existence.articulo.codigo != MN31_SKU):
        raise ScmServiceError("MN31_SKU_REQUIRED", "La existencia no corresponde a PC-000468.", status_code=409)
    if unit is not None and unit.estado != "ACTIVA":
        raise ScmServiceError("MN31_ALREADY_DISPATCHED", "La identidad ya no está vigente para despacho.", status_code=409)
    manga = existence.manga if existence is not None else None
    if manga is None or manga.pieza_color_sku_snapshot != MN31_SKU:
        raise ScmServiceError("MN31_MANGA_NOT_FOUND", "La identidad no tiene una manga MN31 asociada.", status_code=409)
    if manga.estado == "ANULADA":
        raise ScmServiceError("MN31_MANGA_ANULADA", "La manga anulada no puede despacharse.", status_code=409)
    if unit.estado_calidad not in {"SIN_CONTROL", "LIBERADA"} or unit.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION", "RECIBIDA_ALMACEN"} or existence is None or existence.estado_calidad not in {"SIN_CONTROL", "LIBERADA"} or existence.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION", "RECIBIDA_ALMACEN"}:
        raise ScmServiceError("MN31_STOCK_NOT_AVAILABLE", "La identidad no tiene custodia KG acreditada disponible.", status_code=409)
    weight, source_type, source_id, _source_at = _weight_source(session, manga, unit, existence)
    lot = _lot_for_manga(session, manga)
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
        "existencia": {
            "id": str(existence.id), "estado_logistico": existence.estado_logistico,
            "estado_calidad": existence.estado_calidad, "cantidad_fisica_kg": _decimal(existence.cantidad_fisica_kg),
        },
        "cliente_seleccionado": client.to_dict() if client else None,
        "confirmable": True,
    }


def _child_operation(session, root_id, suffix, actor, command):
    child_id = uuid5(root_id, suffix)
    child, replay = _reserve_operation(session, child_id, f"{MN31_ENDPOINT}/{suffix}", actor, command)
    if replay is not None:
        raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La operación hija ya existe.", status_code=409)
    return child


def confirm_mn31_dispatch(session, *, actor_id, operation_id, data):
    assert_custody_enabled()
    actor = load_actor(session, actor_id, capability="DESPACHO_MN31_CONFIRMAR")
    reject_unknown_fields(data, allowed={"codigo", "unit_id", "version", "cliente_id", "documento", "motivo"})
    code = str(data.get("codigo") or "").strip()
    if not code:
        raise ScmServiceError("MN31_CODE_REQUIRED", "codigo es obligatorio.", status_code=400)
    if not isinstance(data.get("cliente_id"), int) or isinstance(data.get("cliente_id"), bool):
        raise ScmServiceError("MN31_CLIENT_REQUIRED", "cliente_id es obligatorio.", status_code=422)
    if not data.get("unit_id"):
        raise ScmServiceError("MN31_UNIT_REQUIRED", "unit_id es obligatorio y debe coincidir con la resolución.", status_code=422)
    if data.get("version") is None:
        raise ScmServiceError("MN31_VERSION_REQUIRED", "version es obligatoria y debe coincidir con la resolución.", status_code=422)
    client = session.get(ScmClienteMn31, data["cliente_id"])
    if client is None or not client.activo:
        raise ScmServiceError("MN31_CLIENT_NOT_FOUND", "El cliente no existe o no está activo.", status_code=404)
    reason = str(data.get("motivo") or "").strip()
    if not reason:
        raise ScmServiceError("MN31_MOTIVO_REQUIRED", "motivo es obligatorio para generar el comprobante causal SCM.", status_code=422)
    command = {"codigo": code, "unit_id": str(data.get("unit_id") or ""), "version": data.get("version"), "cliente_id": client.id, "documento": str(data.get("documento") or "").strip() or None, "motivo": reason}
    acquire_kg_productive_write_lock(session)
    operation, replay = _reserve_operation(session, operation_id, MN31_ENDPOINT, actor, command)
    if replay is not None:
        return deepcopy(replay)
    try:
        unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.codigo == code).with_for_update())
        if unit is None:
            manga_by_code = session.scalar(select(ScmManga).where(ScmManga.codigo == code).with_for_update())
            if manga_by_code is not None:
                existence_by_manga = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga_by_code.id, ScmExistenciaMangaKg.unidad_fisica_kg_id.is_not(None)).with_for_update())
                if existence_by_manga is not None:
                    unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == existence_by_manga.unidad_fisica_kg_id).with_for_update())
        if unit is not None:
            unit = _lock_unit_custody(session, unit)
        if unit is not None and data.get("unit_id") and str(unit.id) != str(data["unit_id"]):
            raise ScmServiceError("MN31_IDENTITY_MISMATCH", "unit_id no corresponde al código escaneado.", status_code=409)
        if unit is not None and unit.version != expected_version(data["version"]):
            raise ScmServiceError("VERSION_CONFLICT", "La identidad cambió desde la lectura.", status_code=409)
        if unit is not None:
            _scope_unit(session, actor, unit)
        if unit is not None and (unit.articulo is None or unit.articulo.codigo != MN31_SKU):
            raise ScmServiceError("MN31_SKU_REQUIRED", "La identidad no corresponde a PC-000468.", status_code=409)
        if unit is not None and unit.estado != "ACTIVA":
            raise ScmServiceError("MN31_ALREADY_DISPATCHED", "La identidad ya no está vigente para despacho.", status_code=409)
        if unit is not None and (unit.estado_calidad not in {"SIN_CONTROL", "LIBERADA"} or unit.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION", "RECIBIDA_ALMACEN"}):
            raise ScmServiceError("MN31_STOCK_NOT_AVAILABLE", "La identidad no tiene custodia KG acreditada disponible.", status_code=409)
        existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.unidad_fisica_kg_id == unit.id).with_for_update()) if unit is not None else None
        if existence is None and unit is not None and unit.recepcion_vigente_id:
            existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.id == unit.recepcion_vigente_id).with_for_update())
        if existence is None and unit is None:
            manga_by_code = session.scalar(select(ScmManga).where(ScmManga.codigo == code).with_for_update())
            if manga_by_code is not None:
                existence = session.scalar(select(ScmExistenciaMangaKg).where(ScmExistenciaMangaKg.manga_id == manga_by_code.id).with_for_update())
        if unit is None:
            raise ScmServiceError("KG_IDENTITY_MISSING", "La manga no tiene una identidad KG vigente; no se reconstruye automáticamente.", status_code=409)
        manga = existence.manga if existence is not None else None
        if manga is None or manga.pieza_color_sku_snapshot != MN31_SKU:
            raise ScmServiceError("MN31_MANGA_NOT_FOUND", "La identidad no tiene una manga MN31 asociada.", status_code=409)
        manga = session.scalar(select(ScmManga).where(ScmManga.id == manga.id).with_for_update())
        if manga.estado == "ANULADA":
            raise ScmServiceError("MN31_MANGA_ANULADA", "La manga anulada no puede despacharse.", status_code=409)
        if manga.estado == "DESPACHADA_CLIENTE_MN31":
            raise ScmServiceError("MN31_ALREADY_DISPATCHED", "La manga ya fue despachada.", status_code=409)
        if existence is None or existence.estado_calidad not in {"SIN_CONTROL", "LIBERADA"} or existence.estado_logistico not in {"EN_PRODUCCION", "DISPONIBLE_PRODUCCION", "RECIBIDA_ALMACEN"}:
            raise ScmServiceError("MN31_STOCK_NOT_AVAILABLE", "La existencia KG no tiene custodia disponible.", status_code=409)
        weight, source_type, source_id, source_at = _weight_source(session, manga, unit, existence)
        lot = _lot_for_manga(session, manga)
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
                operation_id=ingress_op.operation_id, pesaje_public_id=source_id,
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
            operation_id=output_op.operation_id, pesaje_public_id=source_id,
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
            "despacho": {"codigo": code, "unidad_id": str(unit.id), "manga_id": str(manga.public_id), "manga_codigo": manga.codigo, "peso_neto_kg": _decimal(weight), "documento": command["documento"], "causa": "DESPACHO_CLIENTE_MN31", "lote": lot},
            "peso_fuente": _source_snapshot(session, source_type, source_id, weight, source_at),
            "conciliacion": {"delta_kg": _decimal(delta), "movimiento_ingreso": delta > 0},
            "movimiento_salida_id": str(movement.id), "terminal": True,
        }
        _complete(output_op, {"movement_id": str(movement.id), "parent_operation_id": str(operation_id)}, 201)
        _complete(operation, response, 201)
        session.add(ScmEvento(
            aggregate_type="DESPACHO_MN31", aggregate_id=str(manga.public_id), tipo="MN31_DESPACHO_CLIENTE_CONFIRMADO",
            actor_id=actor.id, actor_snapshot=actor_snapshot(actor), motivo=command["motivo"],
            before_json=before, after_json=response, operation_id=operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def get_mn31_dispatch(session, *, actor_id, operation_id):
    actor = load_actor(session, actor_id, capability="DESPACHO_MN31_CONFIRMAR")
    operation = session.get(ScmOperacion, operation_id)
    if operation is None or operation.endpoint != MN31_ENDPOINT or operation.actor_id != actor.id or operation.response_json is None:
        raise ScmServiceError("MN31_OPERATION_NOT_FOUND", "La operación MN31 no existe.", status_code=404)
    return deepcopy(operation.response_json)
