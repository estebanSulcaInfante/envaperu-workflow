"""Custody commands for measured piece/WIP kg; UN flows remain separate."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from flask import current_app
from sqlalchemy import and_, func, or_, select

from app.models.scm_articulos import ScmArticulo
from app.models.scm_auditoria import ScmEvento
from app.models.scm_inventory_kg import (
    ScmDivisionUnidadKg, ScmEtiquetaUnidadKg, ScmExistenciaMangaKg,
    ScmMedicionUnidadKg, ScmMovimientoInventarioKg, ScmReservaUnidadKg,
    ScmRetiroArmadoKg, ScmRetiroArmadoKgItem, ScmSaldoInventarioKg,
    ScmUnidadFisicaKg,
)
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_auditoria import ScmOperacion
from app.services.scm_kg_receipt_service import _assert_kg_location_scope
from app.services.scm_service_support import (
    ScmServiceError, actor_snapshot, expected_version, load_actor,
    reject_unknown_fields, required_text,
)


def assert_custody_enabled():
    if not current_app.config.get("KG_CUSTODY_WRITE_ENABLED", False):
        raise ScmServiceError("KG_OPERATION_NOT_ENABLED", "Las operaciones de custodia KG están deshabilitadas.", status_code=409)


def decide_kg_quality(session, *, actor_id, existence_id, operation_id, data):
    from app.services.scm_warehouse_service import _reserve_operation, _complete
    reject_unknown_fields(data, allowed={"decision", "motivo", "evidencia", "version"})
    decision = str(data.get("decision") or "").strip().upper()
    capability = {"LIBERADA": "CALIDAD_MANGA_LIBERAR", "BLOQUEADA": "CALIDAD_MANGA_BLOQUEAR",
                  "RECHAZADA": "CALIDAD_MANGA_RECHAZAR"}.get(decision)
    if capability is None:
        raise ScmServiceError("DECISION_CALIDAD_INVALIDA", "Decisión de Calidad inválida.", status_code=422)
    actor = load_actor(session, actor_id, capability=capability)
    reason = required_text(data.get("motivo"), field="motivo", max_length=500)
    evidence = str(data.get("evidencia") or "").strip()[:500] or None
    version = expected_version(data.get("version"))
    try:
        reference = session.get(ScmExistenciaMangaKg, existence_id)
        if reference is None:
            raise ScmServiceError("EXISTENCIA_MANGA_NO_ENCONTRADA", "La manga recibida no existe.", status_code=404)
        session.scalar(select(ScmArticulo).where(ScmArticulo.id == reference.articulo_scm_id).with_for_update())
        existence = session.scalar(select(ScmExistenciaMangaKg).where(
            ScmExistenciaMangaKg.id == existence_id).with_for_update().execution_options(populate_existing=True))
        if existence.unidad_fisica_kg_id:
            current_unit = session.scalar(select(ScmUnidadFisicaKg).where(
                ScmUnidadFisicaKg.id == existence.unidad_fisica_kg_id).with_for_update())
            if current_unit is None or current_unit.recepcion_vigente_id != existence.id:
                raise ScmServiceError("KG_UNIT_COMPETING_OPERATION", "La recepción ya no es la vigente para la identidad.", status_code=409)
        _assert_kg_location_scope(session, actor_id=actor.id, location=existence.ubicacion,
                                  article_class=existence.articulo.clase)
        command = {"decision": decision, "motivo": reason, "evidencia": evidence, "version": version}
        operation, replay = _reserve_operation(session, operation_id,
            f"POST /recepcion-mangas/{existence_id}/calidad", actor, command)
        if replay is not None:
            return replay
        assert_custody_enabled()
        if existence.version != version:
            raise ScmServiceError("CONFLICTO_CONCURRENCIA", "La manga cambió desde la última lectura.", status_code=409)
        if existence.estado_logistico != "RECIBIDA_ALMACEN":
            raise ScmServiceError("KG_UNIT_COMPETING_OPERATION", "La unidad no está en Almacén.", status_code=409)
        previous = existence.estado_calidad
        if previous == "SIN_CONTROL":
            raise ScmServiceError(
                "KG_QUALITY_NOT_REQUIRED",
                "La manga KG del piloto opera en SIN_CONTROL y no requiere una decisión de Calidad.",
                status_code=409,
            )
        if previous == decision:
            raise ScmServiceError("CALIDAD_SIN_CAMBIO", "La manga ya posee esa decisión.", status_code=409)
        if Decimal(existence.cantidad_reservada_kg) > 0:
            raise ScmServiceError("MANGA_CON_RESERVA", "Libera la reserva antes de cambiar Calidad.", status_code=409)
        balance = session.scalar(select(ScmSaldoInventarioKg).where(
            ScmSaldoInventarioKg.id == existence.saldo_id).with_for_update().execution_options(populate_existing=True))
        quantity = Decimal(existence.cantidad_fisica_kg)
        unavailable = Decimal(balance.cantidad_no_disponible_kg)
        delta = -quantity if decision == "LIBERADA" else (quantity if previous == "LIBERADA" else Decimal(0))
        if unavailable + delta < 0 or unavailable + delta > Decimal(balance.cantidad_fisica_kg) - Decimal(balance.cantidad_reservada_kg):
            raise ScmServiceError("INVENTORY_QUALITY_INCONSISTENT", "El saldo no permite esta decisión de Calidad.", status_code=409)
        balance.cantidad_no_disponible_kg = unavailable + delta
        balance.version += 1
        existence.estado_calidad = decision
        existence.calidad_actor_id = actor.id
        existence.calidad_at = datetime.now(timezone.utc)
        existence.calidad_motivo = reason
        existence.calidad_evidencia = evidence
        if existence.unidad_fisica_kg_id:
            unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == existence.unidad_fisica_kg_id).with_for_update())
            if unit is not None:
                unit.estado_calidad = decision
                unit.estado_logistico = "RECIBIDA_ALMACEN" if decision == "LIBERADA" else "PENDIENTE_CALIDAD"
                unit.version += 1
        existence.version += 1
        session.flush()
        response = {"existencia": existence.to_dict(), "estado_anterior": previous}
        _complete(operation, response)
        session.add(ScmEvento(aggregate_type="EXISTENCIA_MANGA_KG", aggregate_id=str(existence.id),
            tipo=f"KG_QUALITY_{decision}", actor_id=actor.id, actor_snapshot=actor_snapshot(actor),
            motivo=reason, before_json={"estado_calidad": previous},
            after_json={**copy.deepcopy(response), "evidencia": evidence}, operation_id=operation.operation_id))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _now():
    return datetime.now(timezone.utc)


def _uuid_value(value, *, code="INVALID_UUID"):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        raise ScmServiceError(code, "Se requiere un UUID válido.", status_code=422)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _reserve_operation(session, operation_id, endpoint, actor, data):
    # Lazy import avoids the existing warehouse-service -> KG-custody routing cycle.
    from app.services.scm_warehouse_service import _reserve_operation as reserve
    return reserve(session, operation_id, endpoint, actor, data)


def _complete(operation, payload, status=200):
    from app.services.scm_warehouse_service import _complete as complete
    return complete(operation, payload, status)


def _scope_unit(session, actor, unit):
    if unit is None:
        raise ScmServiceError("KG_UNIT_NOT_FOUND", "La identidad KG no existe.", status_code=404)
    if unit.ubicacion is not None:
        _assert_kg_location_scope(session, actor_id=actor.id, location=unit.ubicacion, article_class=unit.articulo.clase)
    else:
        from app.services.scm_warehouse_scope_service import warehouse_scope
        scope = warehouse_scope(session, actor_id=actor.id)
        if scope["configured"] and not scope["transversal"] and unit.almacen_responsable_id not in scope["warehouse_ids"]:
            raise ScmServiceError("KG_UNIT_NOT_FOUND", "La identidad KG no pertenece al alcance del actor.", status_code=404)
    return unit


def _unit_by_code(session, code):
    value = str(code or "").strip()
    # The station may scan either the unit code/public id or the compact
    # label QR.  QR parsing is deliberately strict and never trusts a mass
    # or a client supplied unit mapping.
    label_public_id = None
    try:
        decoded = json.loads(value)
        if isinstance(decoded, dict) and decoded.get("v") == 1:
            label_public_id = UUID(str(decoded.get("label_id")))
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    if label_public_id is not None:
        label = session.scalar(select(ScmEtiquetaUnidadKg).where(ScmEtiquetaUnidadKg.public_id == label_public_id))
        return session.get(ScmUnidadFisicaKg, label.unidad_id) if label else None
    try:
        public_id = UUID(value)
    except (ValueError, TypeError):
        public_id = None
    query = select(ScmUnidadFisicaKg).where(
        or_(ScmUnidadFisicaKg.codigo == value, ScmUnidadFisicaKg.public_id == public_id) if public_id else ScmUnidadFisicaKg.codigo == value
    )
    return session.scalar(query)


def _active_reservation(session, unit_id):
    return session.scalar(select(ScmReservaUnidadKg).where(
        ScmReservaUnidadKg.unidad_id == unit_id, ScmReservaUnidadKg.estado == "ACTIVA"
    ).with_for_update())


def _current_retiro_item(session, unit):
    """Return the latest causal withdrawal, also for a division child."""
    source = unit
    seen = set()
    while source is not None and source.id not in seen:
        seen.add(source.id)
        item = session.scalar(select(ScmRetiroArmadoKgItem).where(
            ScmRetiroArmadoKgItem.unidad_id == source.id
        ).join(ScmRetiroArmadoKg).order_by(
            ScmRetiroArmadoKg.created_at.desc(),
            ScmRetiroArmadoKg.id.desc(),
        ).limit(1))
        if item is not None:
            return item
        if not source.unidad_padre_id:
            break
        source = session.scalar(select(ScmUnidadFisicaKg).where(
            ScmUnidadFisicaKg.id == source.unidad_padre_id
        ))
    return None


def prepare_kg_return(session, *, actor_id, unit_id, operation_id, data):
    """Explicitly authorize a retained split child to re-enter returns."""
    assert_custody_enabled()
    actor = load_actor(session, actor_id, capability="RETORNO_RECIBIR")
    reject_unknown_fields(data, allowed={"version", "motivo", "evidencia"})
    unit = session.scalar(select(ScmUnidadFisicaKg).where(
        ScmUnidadFisicaKg.id == unit_id
    ))
    _scope_unit(session, actor, unit)
    unit = _lock_unit_custody(session, unit)
    operation, replay = _reserve_operation(
        session,
        operation_id,
        f"POST /unidades-kg/{unit_id}/retorno/preparar",
        actor,
        {"unit_id": str(unit_id), **data},
    )
    if replay is not None:
        _scope_unit(session, actor, unit)
        return replay
    if expected_version(data.get("version")) != unit.version:
        raise ScmServiceError(
            "VERSION_CONFLICT",
            "La identidad cambió desde la lectura.",
            status_code=409,
        )
    if unit.estado != "ACTIVA":
        raise ScmServiceError(
            "KG_UNIT_COMPETING_OPERATION",
            "La identidad no está vigente para retorno.",
            status_code=409,
        )
    if unit.intencion != "PERMANECE":
        raise ScmServiceError(
            "KG_RETURN_ALREADY_PREPARED",
            "La identidad ya está preparada para retorno.",
            status_code=409,
        )
    reason = str(data.get("motivo") or "").strip() or "Retorno posterior de parte retenida"
    evidence = str(data.get("evidencia") or "").strip()[:500] or None
    unit.intencion = "RETORNO"
    unit.version += 1
    session.add(ScmEvento(
        aggregate_type="UNIDAD_FISICA_KG",
        aggregate_id=str(unit.id),
        tipo="KG_INTENCION_PERMANECE_A_RETORNO",
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        motivo=reason,
        before_json={"intencion": "PERMANECE"},
        after_json={"intencion": "RETORNO", "evidencia": evidence},
        operation_id=operation.operation_id,
    ))
    session.flush()
    payload = {"unit": unit.to_dict(), "operation_id": str(operation.operation_id)}
    _complete(operation, payload)
    session.commit()
    return payload


def _lock_unit_custody(session, unit):
    """Lock article, root and all descendants in one deterministic order."""
    root_id = unit.unidad_raiz_id or unit.id
    session.scalar(select(ScmArticulo).where(
        ScmArticulo.id == unit.articulo_scm_id
    ).with_for_update().execution_options(populate_existing=True))
    locked = session.scalars(select(ScmUnidadFisicaKg).where(
        or_(ScmUnidadFisicaKg.id == root_id, ScmUnidadFisicaKg.unidad_raiz_id == root_id)
    ).order_by(ScmUnidadFisicaKg.id).with_for_update().execution_options(populate_existing=True)).all()
    return next((candidate for candidate in locked if candidate.id == unit.id), unit)


def _labels_payload(session, unit):
    label = session.scalar(select(ScmEtiquetaUnidadKg).where(ScmEtiquetaUnidadKg.unidad_id == unit.id).order_by(ScmEtiquetaUnidadKg.version.desc()))
    return {
        "id": str(label.id) if label else None, "public_id": str(label.public_id) if label else None,
        "job_id": label.print_job_id if label else None, "payload_hash": label.payload_hash if label else None,
        "qr_value": (label.payload_json or {}).get("qr_value") if label else None,
        "estado": label.estado if label else None,
    }


def _delivery_payload(session, retiro):
    delivered = sum((Decimal(item.neto_entregado_kg) for item in retiro.items), Decimal("0"))
    unit_ids = []
    pending_units = [item.unidad for item in retiro.items]
    seen_units = set()
    while pending_units:
        current = pending_units.pop()
        if current is None or current.id in seen_units:
            continue
        seen_units.add(current.id)
        unit_ids.append(current.id)
        pending_units.extend(current.hijas)
    return_measurements = select(ScmMedicionUnidadKg.id).where(
        ScmMedicionUnidadKg.retiro_id == retiro.id,
        ScmMedicionUnidadKg.unidad_id.in_(unit_ids),
    ) if unit_ids else select(ScmMedicionUnidadKg.id).where(False)
    returned_movements = select(ScmMovimientoInventarioKg.id).where(
        ScmMovimientoInventarioKg.medicion_unidad_kg_id.in_(return_measurements)
    )
    returned = session.scalar(select(func.coalesce(func.sum(ScmExistenciaMangaKg.cantidad_fisica_kg), 0)).where(
        ScmExistenciaMangaKg.unidad_fisica_kg_id.in_(unit_ids),
        ScmExistenciaMangaKg.origen_tipo == "RETORNO",
        ScmExistenciaMangaKg.movimiento_ingreso_id.in_(returned_movements),
    )) if unit_ids else Decimal("0")
    measured = session.scalar(select(func.coalesce(func.sum(ScmMedicionUnidadKg.neto_kg), 0)).where(
        ScmMedicionUnidadKg.unidad_id.in_(unit_ids),
        ScmMedicionUnidadKg.intencion == "RETORNO",
        ScmMedicionUnidadKg.retiro_id == retiro.id,
    )) if unit_ids else Decimal("0")
    retained = session.scalar(select(func.coalesce(func.sum(ScmMedicionUnidadKg.neto_kg), 0)).where(
        ScmMedicionUnidadKg.unidad_id.in_(unit_ids),
        ScmMedicionUnidadKg.intencion == "DIVISION",
        ScmMedicionUnidadKg.retiro_id == retiro.id,
    )) if unit_ids else Decimal("0")
    returned = Decimal(returned or 0); measured = Decimal(measured or 0); retained = Decimal(retained or 0)
    pending_return = max(Decimal("0"), measured - returned)
    unverified = max(Decimal("0"), delivered - returned - pending_return - retained)
    created_at = retiro.created_at
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return {
        "id": str(retiro.id), "codigo": retiro.codigo, "estado": retiro.estado,
        "created_at": created_at.astimezone(timezone.utc).isoformat() if created_at else None,
        "kg_entregado": f"{delivered:.3f}", "kg_retornado_recibido": f"{returned:.3f}",
        "kg_retorno_verificado_pendiente": f"{pending_return:.3f}", "kg_retenido_verificado": f"{retained:.3f}",
        "kg_sin_verificar_clasificar": f"{unverified:.3f}",
        "documento_destino_tipo": retiro.documento_destino_tipo, "documento_destino_id": retiro.documento_destino_id,
        "almacen_responsable_id": str(retiro.almacen_responsable_id) if retiro.almacen_responsable_id else None,
        "tenedor_fisico_id": retiro.tenedor_fisico_id, "motivo_operativo": retiro.motivo_operativo,
        "items": [{"id": str(item.id), "unidad": item.unidad.to_dict(), "neto_entregado_kg": f"{Decimal(item.neto_entregado_kg):.3f}"} for item in retiro.items],
    }


def resolve_kg_return(session, *, actor_id, code, operation_id=None):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_VER")
    unit = _unit_by_code(session, code)
    if unit is None:
        raise ScmServiceError("KG_UNIT_NOT_FOUND", "La identidad KG no existe.", status_code=404)
    _scope_unit(session, actor, unit)
    if unit.estado == "HISTORICA":
        children = [child.to_dict() for child in unit.hijas if child.estado == "ACTIVA"]
        raise ScmServiceError("KG_UNIT_HISTORICAL", "La identidad es histórica; usa una hija vigente.", status_code=409, details={"unit": unit.to_dict(), "hijas": children})
    retiro_item = _current_retiro_item(session, unit)
    retiro = retiro_item.retiro if retiro_item else None
    measurement_context = {"modo_lectura": getattr(unit, "modo_lectura", None), "tara_contexto": getattr(unit, "tara_contexto_json", None)}
    reservation = session.scalar(select(ScmReservaUnidadKg).where(
        ScmReservaUnidadKg.unidad_id == unit.id, ScmReservaUnidadKg.estado == "ACTIVA"
    ))
    measurement = session.get(ScmMedicionUnidadKg, unit.medicion_vigente_id) if unit.medicion_vigente_id else None
    return_locations = []
    for location in session.scalars(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.activo.is_(True),
        ScmUbicacionInventario.permite_saldo_libre.is_(True),
    ).order_by(ScmUbicacionInventario.codigo)).all():
        try:
            _assert_kg_location_scope(session, actor_id=actor.id, location=location, article_class=unit.articulo.clase)
        except ScmServiceError:
            continue
        return_locations.append(location.to_dict())
    return {"unit": unit.to_dict(), "retiro": _delivery_payload(session, retiro) if retiro else None,
            "expected_unit_source": {"unit_id": str(unit.id), "version": unit.version, "root_id": str(unit.unidad_raiz_id or unit.id), "source_hash": _hash([str(unit.id), unit.version])},
            "expected_unit_source_nonce": _hash([str(unit.id), unit.version, str(unit.public_id)])[:32],
            "measurement_context": measurement_context, "reservation": {"id": str(reservation.id), "estado": reservation.estado, "cantidad_snapshot_kg": f"{Decimal(reservation.cantidad_snapshot_kg):.3f}"} if reservation else None,
            "measurement": {"id": str(measurement.id), "neto_kg": f"{Decimal(measurement.neto_kg):.3f}", "intencion": measurement.intencion} if measurement else None,
            "can_capture": bool(measurement_context["modo_lectura"]) and measurement is None,
            "return_locations": return_locations, "label": _labels_payload(session, unit)}


def reserve_kg_unit(session, *, actor_id, unit_id, operation_id, data):
    assert_custody_enabled()
    actor = load_actor(session, actor_id, capability="PICKING_PREPARAR")
    reject_unknown_fields(data, allowed={"version", "documento_destino_tipo", "documento_destino_id", "motivo_operativo"})
    unit = _scope_unit(session, actor, session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit_id)))
    unit = _lock_unit_custody(session, unit)
    version = expected_version(data.get("version")); destination_type = str(data.get("documento_destino_tipo") or "").strip() or None; destination_id = str(data.get("documento_destino_id") or "").strip() or None
    if bool(destination_type) != bool(destination_id):
        raise ScmServiceError("KG_CAUSAL_MISMATCH", "documento_destino_tipo e id deben venir juntos.", status_code=422)
    reason = str(data.get("motivo_operativo") or "").strip() or None
    if not destination_id and not reason:
        raise ScmServiceError("KG_OPERATION_REASON_REQUIRED", "Se requiere motivo operativo si no hay documento destino.", status_code=422)
    operation, replay = _reserve_operation(session, operation_id, f"POST /unidades-kg/{unit_id}/reservas", actor, {"unit_id": str(unit_id), **data})
    if replay is not None:
        _scope_unit(session, actor, unit); return replay
    if unit.version != version: raise ScmServiceError("VERSION_CONFLICT", "La identidad cambió.", status_code=409)
    if unit.estado != "ACTIVA" or unit.estado_calidad not in {"LIBERADA", "SIN_CONTROL"} or unit.estado_logistico not in {"RECIBIDA_ALMACEN", "ALMACENADA_CONTROLADA", "DISPONIBLE_PRODUCCION"}:
        raise ScmServiceError("KG_QUALITY_BLOCKED", "La unidad no está liberada y disponible.", status_code=409)
    if _active_reservation(session, unit.id): raise ScmServiceError("KG_RESERVATION_ALREADY_EXISTS", "La unidad ya tiene una reserva activa.", status_code=409)
    balance = session.scalar(select(ScmSaldoInventarioKg).where(ScmSaldoInventarioKg.id == unit.saldo_id).with_for_update())
    if balance is None:
        raise ScmServiceError("INVENTORY_CONFLICT", "La identidad no tiene saldo disponible.", status_code=409)
    quantity = Decimal(unit.kg_verificados or unit.kg_entregado or 0)
    if Decimal(balance.cantidad_fisica_kg) - Decimal(balance.cantidad_reservada_kg) < quantity:
        raise ScmServiceError("INVENTORY_CONFLICT", "El saldo libre no cubre la reserva KG.", status_code=409)
    balance.cantidad_reservada_kg = Decimal(balance.cantidad_reservada_kg) + quantity; balance.version += 1
    reservation = ScmReservaUnidadKg(unidad_id=unit.id, cantidad_snapshot_kg=quantity, documento_destino_tipo=destination_type, documento_destino_id=destination_id, motivo_operativo=reason, actor_id=actor.id, operation_id=operation.operation_id)
    session.add(reservation); unit.estado_logistico = "RESERVADA"; unit.version += 1; session.flush()
    payload = {"reserva": {"id": str(reservation.id), "unidad_id": str(unit.id), "cantidad_snapshot_kg": f"{quantity:.3f}", "estado": reservation.estado}, "unit": unit.to_dict(), "operation_id": str(operation.operation_id)}
    _complete(operation, payload); session.commit(); return payload


def release_kg_reservation(session, *, actor_id, unit_id, operation_id, data):
    assert_custody_enabled(); actor = load_actor(session, actor_id, capability="PICKING_PREPARAR")
    unit = _scope_unit(session, actor, session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit_id)))
    unit = _lock_unit_custody(session, unit)
    operation, replay = _reserve_operation(session, operation_id, f"POST /unidades-kg/{unit_id}/reservas/liberar", actor, {"unit_id": str(unit_id), "version": data.get("version")})
    if replay is not None: _scope_unit(session, actor, unit); return replay
    reservation = _active_reservation(session, unit.id)
    if reservation is None: raise ScmServiceError("KG_RESERVATION_NOT_FOUND", "No hay reserva activa.", status_code=404)
    if expected_version(data.get("version")) != unit.version: raise ScmServiceError("VERSION_CONFLICT", "La identidad cambió.", status_code=409)
    balance = session.scalar(select(ScmSaldoInventarioKg).where(ScmSaldoInventarioKg.id == unit.saldo_id).with_for_update())
    if balance is None or Decimal(balance.cantidad_reservada_kg) < Decimal(reservation.cantidad_snapshot_kg):
        raise ScmServiceError("INVENTORY_CONFLICT", "El saldo reservado no es consistente.", status_code=409)
    balance.cantidad_reservada_kg = Decimal(balance.cantidad_reservada_kg) - Decimal(reservation.cantidad_snapshot_kg); balance.version += 1; reservation.estado = "LIBERADA"; reservation.released_at = _now(); unit.estado_logistico = "RECIBIDA_ALMACEN"; unit.version += 1
    payload = {"unit": unit.to_dict(), "reserva": {"id": str(reservation.id), "estado": reservation.estado}, "operation_id": str(operation.operation_id)}; _complete(operation, payload); session.commit(); return payload


def withdraw_kg_unit(session, *, actor_id, unit_id, operation_id, data):
    assert_custody_enabled(); actor = load_actor(session, actor_id, capability="PICKING_DESPACHAR")
    unit = _scope_unit(session, actor, session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit_id)))
    unit = _lock_unit_custody(session, unit)
    command = {"unit_id": str(unit_id), "version": data.get("version"), "tenedor_fisico_id": data.get("tenedor_fisico_id") or actor.id, "motivo_operativo": data.get("motivo_operativo"), "documento_destino_tipo": data.get("documento_destino_tipo"), "documento_destino_id": data.get("documento_destino_id")}
    operation, replay = _reserve_operation(session, operation_id, f"POST /unidades-kg/{unit_id}/retiro", actor, command)
    if replay is not None: _scope_unit(session, actor, unit); return replay
    reservation = _active_reservation(session, unit.id)
    if reservation is None: raise ScmServiceError("KG_RESERVATION_REQUIRED", "El retiro requiere una reserva activa.", status_code=409)
    command["documento_destino_tipo"] = command["documento_destino_tipo"] or reservation.documento_destino_tipo
    command["documento_destino_id"] = command["documento_destino_id"] or reservation.documento_destino_id
    if not command["documento_destino_id"] and not str(command["motivo_operativo"] or reservation.motivo_operativo or "").strip(): raise ScmServiceError("KG_OPERATION_REASON_REQUIRED", "Se requiere motivo operativo.", status_code=422)
    if expected_version(data.get("version")) != unit.version: raise ScmServiceError("VERSION_CONFLICT", "La identidad cambió.", status_code=409)
    quantity = Decimal(unit.kg_verificados or reservation.cantidad_snapshot_kg); balance = session.scalar(select(ScmSaldoInventarioKg).where(ScmSaldoInventarioKg.id == unit.saldo_id).with_for_update())
    if balance is None or Decimal(balance.cantidad_fisica_kg) < quantity or Decimal(balance.cantidad_reservada_kg) < quantity: raise ScmServiceError("INVENTORY_CONFLICT", "El saldo KG no cubre el retiro.", status_code=409)
    balance.cantidad_fisica_kg = Decimal(balance.cantidad_fisica_kg) - quantity; balance.cantidad_reservada_kg = Decimal(balance.cantidad_reservada_kg) - quantity; balance.cantidad_retirada_kg = Decimal(balance.cantidad_retirada_kg or 0) + quantity; balance.version += 1
    retiro = ScmRetiroArmadoKg(codigo=f"RET-KG-{str(operation.operation_id)[:8].upper()}", documento_destino_tipo=command["documento_destino_tipo"], documento_destino_id=command["documento_destino_id"], almacen_responsable_id=unit.almacen_responsable_id, actor_id=actor.id, tenedor_fisico_id=command["tenedor_fisico_id"], motivo_operativo=command["motivo_operativo"] or reservation.motivo_operativo, operation_id=operation.operation_id)
    session.add(retiro); session.flush()
    movement = ScmMovimientoInventarioKg(saldo_id=balance.id, tipo="RETIRO_ARMADO", cantidad_delta_kg=-quantity, saldo_fisico_resultante_kg=balance.cantidad_fisica_kg, motivo="Retiro completo para Armado", referencia_tipo="RETIRO_ARMADO_KG", referencia_id=str(retiro.id), actor_id=actor.id, operation_id=operation.operation_id, projection_sha256=_hash([str(unit.id), str(operation.operation_id)]), peso_neto_snapshot_kg=quantity, pesada_at_snapshot=_now(), fuente_tipo="CUSTODIA")
    session.add(movement); session.flush()
    retiro.items.append(ScmRetiroArmadoKgItem(unidad_id=unit.id, reserva_id=reservation.id, neto_entregado_kg=quantity, movimiento_id=movement.id)); reservation.estado = "RETIRADA"; unit.kg_entregado = quantity; unit.kg_verificados = None; unit.kg_verificados_at = None; unit.medicion_vigente_id = None; unit.recepcion_vigente_id = None; unit.estado_logistico = "RETIRADA_ARMADO"; unit.saldo_id = None; unit.ubicacion_id = None; unit.version += 1; session.flush()
    payload = {"retiro": _delivery_payload(session, retiro), "operation_id": str(operation.operation_id)}; _complete(operation, payload, 201); session.commit(); return payload


def capture_kg_measurement(session, *, actor_id, station_id, unit_id, operation_id, data, snapshot=None):
    assert_custody_enabled()
    from app.services.scm_service_support import load_actor_any
    actor = load_actor_any(session, actor_id, capabilities=("RETORNO_RECIBIR", "ABASTECIMIENTO_DEVOLVER"))
    unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit_id))
    _scope_unit(session, actor, unit)
    unit = _lock_unit_custody(session, unit)
    reject_unknown_fields(data, allowed={"version", "reading_id", "captured_at_utc", "reading_stable", "modo_lectura", "label_id", "expected_unit_source_nonce", "station_version", "payload_hash", "intencion", "bruto_kg", "tara_kg"})
    if not isinstance(snapshot, dict) or not snapshot.get("reading_id") or not snapshot.get("stable"):
        raise ScmServiceError("SCALE_READING_UNSTABLE", "La estación no entregó una lectura estable.", status_code=409)
    if str(snapshot.get("source") or "").upper() != "SERIAL":
        raise ScmServiceError("SCALE_READING_SOURCE_INVALID", "La lectura debe proceder del lector serial autenticado.", status_code=409)
    if str(snapshot.get("reading_id")) != str(data.get("reading_id")):
        raise ScmServiceError("KG_UNIT_SOURCE_CONFLICT", "La lectura no coincide con la solicitud.", status_code=409)
    operation, replay = _reserve_operation(session, operation_id, "POST /stations/kg-return-readings", actor, {"station_id": station_id, "unit_id": str(unit_id), **data, "snapshot": snapshot})
    if replay is not None:
        _scope_unit(session, actor, unit)
        return replay
    if unit.estado != "ACTIVA" or unit.estado_logistico not in {"RETIRADA_ARMADO", "PENDIENTE_VERIFICACION"}:
        raise ScmServiceError("KG_UNIT_COMPETING_OPERATION", "La identidad no está disponible para medición.", status_code=409)
    if expected_version(data.get("version")) != unit.version:
        raise ScmServiceError("KG_MEASUREMENT_VERSION_CONFLICT", "La identidad cambió.", status_code=409)
    expected_nonce = _hash([str(unit.id), unit.version, str(unit.public_id)])[:32]
    if data.get("expected_unit_source_nonce") != expected_nonce:
        raise ScmServiceError("KG_UNIT_SOURCE_CONFLICT", "El contexto de identidad expiró.", status_code=409)
    label_value = data.get("label_id")
    if not label_value:
        raise ScmServiceError("KG_LABEL_REQUIRED", "La captura requiere la etiqueta resuelta.", status_code=422)
    try:
        label_uuid = UUID(str(label_value))
    except (TypeError, ValueError):
        raise ScmServiceError("KG_LABEL_NOT_FOUND", "La etiqueta no es válida.", status_code=404)
    label = session.scalar(select(ScmEtiquetaUnidadKg).where(
        ScmEtiquetaUnidadKg.unidad_id == unit.id,
        or_(ScmEtiquetaUnidadKg.id == label_uuid, ScmEtiquetaUnidadKg.public_id == label_uuid),
    ).with_for_update().execution_options(populate_existing=True))
    if label is None or label.estado not in {"GENERADA", "IMPRESA"}:
        raise ScmServiceError("KG_LABEL_NOT_FOUND", "La etiqueta no pertenece a la identidad vigente.", status_code=404)
    latest_label = session.scalar(select(ScmEtiquetaUnidadKg).where(
        ScmEtiquetaUnidadKg.unidad_id == unit.id
    ).order_by(ScmEtiquetaUnidadKg.version.desc()).limit(1).with_for_update().execution_options(populate_existing=True))
    if latest_label is None or latest_label.id != label.id:
        raise ScmServiceError("KG_LABEL_VERSION_CONFLICT", "La etiqueta ya no es la versión vigente.", status_code=409)
    if label.estado != "IMPRESA":
        raise ScmServiceError("KG_LABEL_NOT_PRINTED", "La etiqueta debe tener impresión confirmada antes de medir.", status_code=409)
    configured_intention = str(unit.intencion or "RETORNO").upper()
    requested_intention = data.get("intencion")
    if configured_intention == "PERMANECE":
        raise ScmServiceError(
            "KG_RETURN_PREPARATION_REQUIRED",
            "La parte retenida requiere preparación explícita para retorno.",
            status_code=409,
        )
    if requested_intention and str(requested_intention).upper() != configured_intention:
        raise ScmServiceError(
            "KG_INTENTION_CONTEXT_REQUIRED",
            "La intención de captura no coincide con el contexto gobernado.",
            status_code=409,
        )
    intention = configured_intention
    if intention != "RETORNO":
        raise ScmServiceError("KG_WEIGHT_INVALID", "La intención de medición no es válida.", status_code=422)
    mode = str(unit.modo_lectura or "").upper()
    if mode not in {"NET_DIRECTO", "BRUTO_MENOS_TARA_CONFIGURADA"}:
        raise ScmServiceError("TARA_CONFIG_REQUIRED", "Falta modo/tara gobernado para la identidad.", status_code=409)
    try:
        gross = Decimal(str(snapshot.get("peso_kg")))
        if not gross.is_finite() or gross <= 0:
            raise ValueError
    except (ArithmeticError, ValueError, TypeError):
        raise ScmServiceError("KG_WEIGHT_INVALID", "La lectura estable no es un peso válido.", status_code=422)
    tare_context = unit.tara_contexto_json or {}
    tare_value = tare_context.get("tara_kg", tare_context.get("tara_nominal_kg"))
    try:
        tare = Decimal(str(tare_value)) if tare_value is not None else None
    except (ArithmeticError, ValueError, TypeError):
        raise ScmServiceError("TARA_CONFIG_REQUIRED", "La tara gobernada no es válida.", status_code=409)
    if tare is not None and (not tare.is_finite() or tare < 0):
        raise ScmServiceError("TARA_CONFIG_REQUIRED", "La tara gobernada no es válida.", status_code=409)
    if mode == "BRUTO_MENOS_TARA_CONFIGURADA" and tare is None:
        raise ScmServiceError("TARA_CONFIG_REQUIRED", "Falta tara gobernada.", status_code=409)
    net = gross if mode == "NET_DIRECTO" else gross - tare
    if not net.is_finite() or net <= 0:
        raise ScmServiceError("KG_WEIGHT_INVALID", "El NET medido debe ser positivo.", status_code=422)
    if session.scalar(select(ScmMedicionUnidadKg).where(ScmMedicionUnidadKg.station_id == station_id, ScmMedicionUnidadKg.reading_id == str(snapshot["reading_id"]))):
        raise ScmServiceError("SCALE_READING_ALREADY_USED", "La lectura ya fue usada.", status_code=409)
    current_item = _current_retiro_item(session, unit)
    delivered = Decimal(current_item.neto_entregado_kg or 0) if current_item else Decimal(unit.kg_entregado or 0)
    current_retiro_id = current_item.retiro_id if current_item else None
    causal_root_id = unit.unidad_raiz_id or unit.id
    root_ids = session.scalars(select(ScmUnidadFisicaKg.id).where(
        or_(ScmUnidadFisicaKg.id == causal_root_id,
            ScmUnidadFisicaKg.unidad_raiz_id == causal_root_id)
    ).execution_options(populate_existing=True)).all()
    measured_query = select(func.coalesce(func.sum(ScmMedicionUnidadKg.neto_kg), 0)).where(ScmMedicionUnidadKg.unidad_id.in_(root_ids))
    if current_retiro_id is not None:
        measured_query = measured_query.where(ScmMedicionUnidadKg.retiro_id == current_retiro_id)
    measured_total = session.scalar(measured_query) or 0
    if delivered and Decimal(measured_total) + net > delivered:
        raise ScmServiceError("KG_RETURN_EXCEEDS_DELIVERY", "La medición excede el NET entregado.", status_code=409)
    captured = snapshot.get("received_at_utc") or data.get("captured_at_utc")
    try:
        captured_at = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ScmServiceError("KG_WEIGHT_INVALID", "La fecha de lectura no es válida.", status_code=422)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ScmServiceError("KG_WEIGHT_INVALID", "La fecha de lectura debe incluir zona horaria.", status_code=422)
    measurement = ScmMedicionUnidadKg(unidad_id=unit.id, retiro_id=current_retiro_id, intencion=intention, station_id=station_id, reading_id=str(snapshot["reading_id"]), captured_at_utc=captured_at, reading_stable=True, modo_lectura=mode, bruto_kg=gross if mode != "NET_DIRECTO" else None, tara_kg=tare, neto_kg=net, station_version=data.get("station_version"), payload_hash=data.get("payload_hash") or _hash(snapshot), actor_id=actor.id, operation_id=operation.operation_id)
    session.add(measurement); session.flush()
    unit.medicion_vigente_id = measurement.id; unit.kg_verificados = net; unit.kg_verificados_at = captured_at; unit.estado_logistico = "REPESADA_PENDIENTE_RECEPCION" if intention == "RETORNO" else "PENDIENTE_VERIFICACION"; unit.version += 1; session.flush()
    payload = {"measurement": {"id": str(measurement.id), "unit_id": str(unit.id), "neto_kg": f"{net:.3f}", "modo_lectura": mode, "reading_id": measurement.reading_id}, "unit": unit.to_dict(), "operation_id": str(operation.operation_id)}
    _complete(operation, payload); session.commit(); return payload


def divide_kg_unit(session, *, actor_id, retiro_id, operation_id, data):
    assert_custody_enabled(); actor = load_actor(session, actor_id, capability="UNIDAD_LOGISTICA_FRACCIONAR")
    reject_unknown_fields(data, allowed={"version", "partes"})
    retiro = session.scalar(select(ScmRetiroArmadoKg).where(ScmRetiroArmadoKg.id == retiro_id).with_for_update())
    if retiro is None: raise ScmServiceError("KG_UNIT_NOT_FOUND", "El retiro no existe.", status_code=404)
    parts = data.get("partes") if isinstance(data, dict) else None
    if not isinstance(parts, list) or len(parts) < 2: raise ScmServiceError("KG_DIVISION_PARTS_REQUIRED", "La división requiere dos o más partes.", status_code=422)
    parent_id = retiro.items[0].unidad_id if len(retiro.items) == 1 else None
    if parent_id is None: raise ScmServiceError("KG_MIXED_ORIGIN_NOT_ALLOWED", "La división exige una sola identidad causal.", status_code=409)
    parent = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == parent_id).with_for_update()); _scope_unit(session, actor, parent)
    operation, replay = _reserve_operation(session, operation_id, f"POST /retiros-armado-kg/{retiro_id}/divisiones", actor, {"retiro_id": str(retiro_id), **data})
    if replay is not None: _scope_unit(session, actor, parent); return replay
    if expected_version(data.get("version")) != parent.version:
        raise ScmServiceError("VERSION_CONFLICT", "La identidad cambió desde la lectura.", status_code=409)
    if parent.estado != "ACTIVA" or parent.estado_logistico != "RETIRADA_ARMADO": raise ScmServiceError("KG_UNIT_COMPETING_OPERATION", "La identidad ya fue dividida o no está retirada.", status_code=409)
    division = ScmDivisionUnidadKg(padre_id=parent.id, actor_id=actor.id, operation_id=operation.operation_id); session.add(division); session.flush()
    parent.estado = "HISTORICA"; parent.saldo_id = None; parent.ubicacion_id = None; parent.division_id = division.id; parent.version += 1
    children = []
    for index, raw in enumerate(parts, 1):
        intention = str(raw.get("intencion") or "").upper()
        if intention not in {"RETORNO", "PERMANECE"}: raise ScmServiceError("KG_DIVISION_INTENTION_INVALID", "La intención de parte no es válida.", status_code=422)
        child = ScmUnidadFisicaKg(public_id=uuid4(), codigo=f"{parent.codigo}-P{index}", articulo_scm_id=parent.articulo_scm_id, unidad_padre_id=parent.id, unidad_raiz_id=parent.unidad_raiz_id or parent.id, division_id=division.id, intencion=intention, estado="ACTIVA", estado_logistico="PENDIENTE_VERIFICACION", estado_calidad=parent.estado_calidad, kg_entregado=None, ubicacion_id=None, almacen_responsable_id=parent.almacen_responsable_id, tenedor_fisico_id=retiro.tenedor_fisico_id)
        session.add(child); session.flush(); label_public_id = uuid4(); payload_json = {"v": 1, "label_id": str(label_public_id), "qr_value": json.dumps({"v": 1, "label_id": str(label_public_id)}, separators=(",", ":"))}; session.add(ScmEtiquetaUnidadKg(public_id=label_public_id, unidad_id=child.id, payload_json=payload_json, payload_hash=_hash(payload_json), estado="GENERADA")); children.append(child)
    session.flush(); payload = {"division": {"id": str(division.id), "padre": parent.to_dict(), "partes": [{"unidad": child.to_dict(), "label": _labels_payload(session, child)} for child in children]}, "operation_id": str(operation.operation_id)}; _complete(operation, payload, 201); session.commit(); return payload


def receive_kg_return(session, *, actor_id, unit_id, operation_id, data):
    assert_custody_enabled(); actor = load_actor(session, actor_id, capability="RETORNO_RECIBIR")
    unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit_id)); _scope_unit(session, actor, unit); unit = _lock_unit_custody(session, unit)
    operation, replay = _reserve_operation(session, operation_id, f"POST /unidades-kg/{unit_id}/retorno/recibir", actor, {"unit_id": str(unit_id), **data})
    if replay is not None: _scope_unit(session, actor, unit); return replay
    try:
        measurement_id = UUID(str(data.get("measurement_id")))
    except (TypeError, ValueError):
        raise ScmServiceError("KG_WEIGHT_REQUIRED", "La identidad requiere una medición automática.", status_code=409)
    measurement = session.scalar(select(ScmMedicionUnidadKg).where(ScmMedicionUnidadKg.id == measurement_id, ScmMedicionUnidadKg.unidad_id == unit.id).with_for_update())
    if measurement is None: raise ScmServiceError("KG_WEIGHT_REQUIRED", "La identidad requiere una medición automática.", status_code=409)
    if unit.estado != "ACTIVA" or unit.estado_logistico != "REPESADA_PENDIENTE_RECEPCION" or unit.medicion_vigente_id != measurement.id or measurement.intencion != "RETORNO":
        raise ScmServiceError("KG_WEIGHT_NOT_CURRENT", "La recepción requiere la medición vigente de retorno.", status_code=409)
    if expected_version(data.get("version")) != unit.version:
        raise ScmServiceError("VERSION_CONFLICT", "La identidad cambió desde la medición.", status_code=409)
    if session.scalar(select(ScmExistenciaMangaKg.id).where(
        ScmExistenciaMangaKg.unidad_fisica_kg_id == unit.id,
        ScmExistenciaMangaKg.origen_tipo == "RETORNO",
        ScmExistenciaMangaKg.movimiento_ingreso_id.in_(select(ScmMovimientoInventarioKg.id).where(
            ScmMovimientoInventarioKg.medicion_unidad_kg_id == measurement.id
        )),
    )) is not None: raise ScmServiceError("KG_RETURN_ALREADY_RECEIVED", "La medición ya fue recibida.", status_code=409)
    location_code = required_text(data.get("ubicacion_codigo"), field="ubicacion_codigo", max_length=40).upper(); location = session.scalar(select(ScmUbicacionInventario).where(ScmUbicacionInventario.codigo == location_code).with_for_update())
    if location is None: raise ScmServiceError("LOCATION_NOT_FOUND", "La ubicación no existe.", status_code=404)
    _assert_kg_location_scope(session, actor_id=actor.id, location=location, article_class=unit.articulo.clase)
    quantity = Decimal(measurement.neto_kg); balance = session.scalar(select(ScmSaldoInventarioKg).where(ScmSaldoInventarioKg.articulo_scm_id == unit.articulo_scm_id, ScmSaldoInventarioKg.ubicacion_id == location.id).with_for_update())
    if balance is None: balance = ScmSaldoInventarioKg(articulo_scm_id=unit.articulo_scm_id, ubicacion_id=location.id); session.add(balance); session.flush()
    # Returns are measured KG facts and remain immediately available under
    # the no-QC pilot state.  Do not route them through the historical
    # PENDIENTE quality bucket or create a false quality release.
    balance.cantidad_fisica_kg = Decimal(balance.cantidad_fisica_kg) + quantity; balance.version += 1
    movement = ScmMovimientoInventarioKg(saldo_id=balance.id, tipo="RETORNO_ENTRADA", cantidad_delta_kg=quantity, saldo_fisico_resultante_kg=balance.cantidad_fisica_kg, motivo="Recepción de retorno KG medido", referencia_tipo="RETIRO_ARMADO_KG", referencia_id=str(unit.id), actor_id=actor.id, operation_id=operation.operation_id, projection_sha256=measurement.payload_hash, peso_neto_snapshot_kg=quantity, pesada_at_snapshot=measurement.captured_at_utc, fuente_tipo="RETORNO", medicion_unidad_kg_id=measurement.id)
    session.add(movement); session.flush(); existence = ScmExistenciaMangaKg(manga_id=None, sesion_id=None, etiqueta_resuelta_id=None, unidad_fisica_kg_id=unit.id, articulo_scm_id=unit.articulo_scm_id, saldo_id=balance.id, ubicacion_id=location.id, movimiento_ingreso_id=movement.id, operation_id=operation.operation_id, resuelta_por="KG_RETURN", estado_logistico="RECIBIDA_ALMACEN", estado_calidad="SIN_CONTROL", origen_tipo="RETORNO", atributo_proceso=unit.atributo_proceso or "PROCESO", cantidad_fisica_kg=quantity, peso_neto_snapshot_kg=quantity, pesaje_public_id=None, projection_sha256=None, pesada_at_snapshot=measurement.captured_at_utc, recibida_por_id=actor.id)
    session.add(existence); session.flush(); unit.saldo_id = balance.id; unit.ubicacion_id = location.id; unit.recepcion_vigente_id = existence.id; unit.estado_logistico = "RECIBIDA_ALMACEN"; unit.estado_calidad = "SIN_CONTROL"; unit.version += 1
    payload = {"existencia": existence.to_dict(), "unit": unit.to_dict(), "movement_id": str(movement.id), "operation_id": str(operation.operation_id)}; _complete(operation, payload, 201); session.commit(); return payload


def list_kg_retiros(session, *, actor_id):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_VER")
    items = []
    for retiro in session.scalars(select(ScmRetiroArmadoKg).order_by(ScmRetiroArmadoKg.created_at.desc())).all():
        try:
            for child in retiro.items:
                _scope_unit(session, actor, child.unidad)
            items.append(_delivery_payload(session, retiro))
        except ScmServiceError as error:
            if error.code in {"KG_UNIT_NOT_FOUND", "INVENTORY_SCOPE_FORBIDDEN"}:
                continue
            raise
    return {"items": items}


def get_kg_retiro(session, *, actor_id, retiro_id):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_VER"); retiro = session.get(ScmRetiroArmadoKg, retiro_id)
    if retiro is None: raise ScmServiceError("KG_UNIT_NOT_FOUND", "El retiro no existe.", status_code=404)
    for item in retiro.items: _scope_unit(session, actor, item.unidad)
    return _delivery_payload(session, retiro)


def get_kg_label(session, *, actor_id, unit_id=None, label_id=None):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_VER")
    unit_id = _uuid_value(unit_id) if unit_id else None
    label_id = _uuid_value(label_id) if label_id else None
    label = session.scalar(select(ScmEtiquetaUnidadKg).where(
        or_(ScmEtiquetaUnidadKg.id == label_id, ScmEtiquetaUnidadKg.public_id == label_id)
    )) if label_id else None
    unit = session.get(ScmUnidadFisicaKg, unit_id) if unit_id else (session.get(ScmUnidadFisicaKg, label.unidad_id) if label else None)
    _scope_unit(session, actor, unit)
    if label is not None and label.unidad_id != unit.id:
        raise ScmServiceError("KG_UNIT_SOURCE_CONFLICT", "La etiqueta no pertenece a la identidad.", status_code=409)
    return {"label": _labels_payload(session, unit), "unit": unit.to_dict()}


def acknowledge_kg_label(session, *, actor_id, station_id, unit_id, label_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="ABASTECIMIENTO_VER")
    unit_id = _uuid_value(unit_id)
    label_id = _uuid_value(label_id)
    unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit_id)); _scope_unit(session, actor, unit); unit = _lock_unit_custody(session, unit)
    label = session.scalar(select(ScmEtiquetaUnidadKg).where(
        ScmEtiquetaUnidadKg.unidad_id == unit.id,
        or_(ScmEtiquetaUnidadKg.id == label_id, ScmEtiquetaUnidadKg.public_id == label_id),
    ).with_for_update())
    if label is None: raise ScmServiceError("KG_LABEL_NOT_FOUND", "La etiqueta no existe.", status_code=404)
    operation, replay = _reserve_operation(session, operation_id, f"POST /stations/{station_id}/kg-label-ack", actor, {"station_id": station_id, "unit_id": str(unit_id), "label_id": str(label_id), **data})
    if replay is not None: _scope_unit(session, actor, unit); return replay
    latest_label = session.scalar(select(ScmEtiquetaUnidadKg).where(
        ScmEtiquetaUnidadKg.unidad_id == unit.id
    ).order_by(ScmEtiquetaUnidadKg.version.desc()).limit(1).with_for_update())
    if latest_label is None or latest_label.id != label.id:
        raise ScmServiceError("KG_LABEL_VERSION_CONFLICT", "La etiqueta ya no es la versión vigente.", status_code=409)
    if unit.estado != "ACTIVA":
        raise ScmServiceError("KG_LABEL_NOT_PRINTABLE", "La identidad histórica no admite impresión.", status_code=409)
    if label.estado == "INVALIDADA":
        raise ScmServiceError("KG_LABEL_NOT_PRINTABLE", "La etiqueta está invalidada.", status_code=409)
    status = str(data.get("estado") or "").upper()
    if status not in {"IMPRESA", "EMISION_INCIERTA"}: raise ScmServiceError("KG_LABEL_EMISSION_UNCERTAIN", "El ACK de impresión no tiene estado válido.", status_code=422)
    if not str(data.get("payload_hash") or "").strip():
        raise ScmServiceError("KG_LABEL_PAYLOAD_REQUIRED", "El ACK debe confirmar el hash del payload.", status_code=422)
    if data["payload_hash"] != label.payload_hash: raise ScmServiceError("KG_UNIT_SOURCE_CONFLICT", "El payload de etiqueta no coincide.", status_code=409)
    if label.estado == "IMPRESA" and status == "EMISION_INCIERTA":
        raise ScmServiceError("KG_LABEL_STATE_CONFLICT", "Una etiqueta impresa no puede volver a emisión incierta.", status_code=409)
    if data.get("job_id") and label.print_job_id and data["job_id"] != label.print_job_id:
        raise ScmServiceError("KG_LABEL_JOB_CONFLICT", "El job de impresión no coincide.", status_code=409)
    if status == "IMPRESA" and not str(data.get("job_id") or label.print_job_id or "").strip():
        raise ScmServiceError("KG_LABEL_JOB_REQUIRED", "Una impresión confirmada requiere job_id.", status_code=422)
    label.estado = status; label.print_job_id = data.get("job_id") or label.print_job_id
    payload = {"label": _labels_payload(session, unit), "unit": unit.to_dict(), "station_id": station_id, "operation_id": str(operation.operation_id)}; _complete(operation, payload); session.commit(); return payload


def configure_kg_measurement_context(session, *, actor_id, unit_id, operation_id, data):
    """Bind a governed reading mode/tare snapshot before station capture."""
    assert_custody_enabled(); actor = load_actor(session, actor_id, capability="ALMACEN_CONFIG_ADMINISTRAR")
    reject_unknown_fields(data, allowed={"version", "modo_lectura", "tara_contexto", "motivo", "evidencia"})
    unit = session.scalar(select(ScmUnidadFisicaKg).where(ScmUnidadFisicaKg.id == unit_id)); _scope_unit(session, actor, unit); unit = _lock_unit_custody(session, unit)
    mode = str(data.get("modo_lectura") or "").upper(); context = data.get("tara_contexto")
    if mode not in {"NET_DIRECTO", "BRUTO_MENOS_TARA_CONFIGURADA"}: raise ScmServiceError("KG_WEIGHT_INVALID", "Modo de lectura inválido.", status_code=422)
    if mode == "BRUTO_MENOS_TARA_CONFIGURADA" and (not isinstance(context, dict) or (context.get("tara_kg") is None and context.get("tara_nominal_kg") is None)): raise ScmServiceError("TARA_CONFIG_REQUIRED", "Falta tara gobernada.", status_code=422)
    if mode == "BRUTO_MENOS_TARA_CONFIGURADA":
        if not isinstance(context, dict) or not str(context.get("origen") or "").strip():
            raise ScmServiceError("TARA_CONTEXT_REQUIRED", "La tara debe provenir de un contexto gobernado.", status_code=422)
        tare_value = context.get("tara_kg", context.get("tara_nominal_kg"))
        try:
            tare = Decimal(str(tare_value))
        except (ArithmeticError, ValueError, TypeError):
            raise ScmServiceError("TARA_CONFIG_REQUIRED", "La tara gobernada no es válida.", status_code=422)
        if not tare.is_finite() or tare < 0:
            raise ScmServiceError("TARA_CONFIG_REQUIRED", "La tara gobernada no es válida.", status_code=422)
    if isinstance(context, dict) and context.get("origen") == "OVERRIDE_MANUAL":
        load_actor(session, actor_id, capability="PESAJE_TARA_OVERRIDE")
        if not str(data.get("motivo") or "").strip():
            raise ScmServiceError("KG_OPERATION_REASON_REQUIRED", "La tara manual requiere motivo.", status_code=422)
        context = {**context, "motivo": str(data["motivo"]).strip(), "evidencia": str(data.get("evidencia") or "").strip() or None}
    operation, replay = _reserve_operation(session, operation_id, f"POST /unidades-kg/{unit_id}/measurement-context", actor, {"unit_id": str(unit_id), **data})
    if replay is not None: _scope_unit(session, actor, unit); return replay
    if expected_version(data.get("version")) != unit.version: raise ScmServiceError("VERSION_CONFLICT", "La identidad cambió.", status_code=409)
    unit.modo_lectura = mode; unit.tara_contexto_json = copy.deepcopy(context) if context else None; unit.version += 1; session.flush()
    payload = {"unit": unit.to_dict(), "measurement_context": {"modo_lectura": mode, "tara_contexto": unit.tara_contexto_json}, "operation_id": str(operation.operation_id)}; _complete(operation, payload); session.commit(); return payload
