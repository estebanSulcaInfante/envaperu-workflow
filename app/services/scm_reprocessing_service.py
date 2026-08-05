"""Aplicación de US-010E: maestros, subledger kg, molienda y liberación."""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select

from app.models.producto import ColorBase, FamiliaColor
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_catalogos import ScmMaterial
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_reproceso import (
    ScmCondicionMerma,
    ScmFamiliaMaterialReproceso,
    ScmLoteMaterialRecuperado,
    ScmLoteMermaRecuperable,
    ScmMovimientoMerma,
    ScmOrdenMolienda,
    ScmOrdenMoliendaAporte,
    ScmProcesoMaterialReproceso,
    ScmReglaCompatibilidadReproceso,
)
from app.services.scm_alert_service import upsert_operational_alert
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    positive_kg,
    required_text,
    stable_code,
)


def utc_now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _reserve_operation(session, operation_id, endpoint, actor, data):
    request_hash = _hash({"endpoint": endpoint, "actor_id": actor.id, "data": data})
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        if existing.endpoint != endpoint or existing.request_sha256 != request_hash:
            raise ScmServiceError("IDEMPOTENCY_CONFLICT", "La clave idempotente ya fue usada con otra solicitud.", status_code=409)
        if existing.response_json is None:
            raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La operacion previa aun no tiene resultado.", status_code=409)
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


def _nonnegative_kg(value, *, field):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError("INVALID_QUANTITY", f"{field} debe ser una cantidad no negativa.", status_code=422) from error
    if not parsed.is_finite() or parsed < 0:
        raise ScmServiceError("INVALID_QUANTITY", f"{field} debe ser una cantidad no negativa.", status_code=422)
    quantized = parsed.quantize(Decimal("0.001"))
    if parsed != quantized:
        raise ScmServiceError("INVALID_QUANTITY_SCALE", f"{field} admite hasta tres decimales.", status_code=422)
    return quantized


MASTER_MODELS = {
    "familias-material": ScmFamiliaMaterialReproceso,
    "procesos": ScmProcesoMaterialReproceso,
    "condiciones": ScmCondicionMerma,
}


def list_reprocessing_masters(session, *, actor_id, master_type):
    load_actor(session, actor_id, capability="MOLIENDA_VER")
    model = MASTER_MODELS.get(master_type)
    if model is None:
        raise ScmServiceError("MASTER_NOT_FOUND", "El maestro solicitado no existe.", status_code=404)
    items = session.scalars(select(model).order_by(model.codigo)).all()
    return {"items": [item.to_dict() for item in items]}


def create_reprocessing_master(session, *, actor_id, master_type, data):
    load_actor(session, actor_id, capability="MOLIENDA_REGLA_ADMINISTRAR")
    model = MASTER_MODELS.get(master_type)
    if model is None:
        raise ScmServiceError("MASTER_NOT_FOUND", "El maestro solicitado no existe.", status_code=404)
    code = stable_code(data.get("codigo"), max_length=40)
    if session.scalar(select(model).where(model.codigo == code)):
        raise ScmServiceError("DUPLICATE_MASTER_CODE", "Ya existe un registro con ese codigo.", status_code=409)
    kwargs = {
        "codigo": code,
        "nombre": required_text(data.get("nombre"), field="nombre", max_length=120),
        "descripcion": (data.get("descripcion") or "").strip() or None,
    }
    if model is ScmCondicionMerma:
        kwargs["recuperable"] = bool(data.get("recuperable", True))
    item = model(**kwargs)
    session.add(item)
    session.commit()
    return item.to_dict()


def update_reprocessing_master(session, *, actor_id, master_type, item_id, data):
    load_actor(session, actor_id, capability="MOLIENDA_REGLA_ADMINISTRAR")
    model = MASTER_MODELS.get(master_type)
    item = session.get(model, item_id) if model else None
    if item is None:
        raise ScmServiceError("MASTER_NOT_FOUND", "El registro no existe.", status_code=404)
    version = expected_version(data.get("version"))
    if version != item.version:
        raise ScmServiceError("VERSION_CONFLICT", "El maestro fue modificado por otro actor.", status_code=409, details={"version_actual": item.version})
    if "nombre" in data:
        item.nombre = required_text(data.get("nombre"), field="nombre", max_length=120)
    if "descripcion" in data:
        item.descripcion = (data.get("descripcion") or "").strip() or None
    if "activo" in data:
        item.activo = bool(data.get("activo"))
    if isinstance(item, ScmCondicionMerma) and "recuperable" in data:
        item.recuperable = bool(data.get("recuperable"))
    item.version += 1
    session.commit()
    return item.to_dict()


def list_reprocessing_references(session, *, actor_id):
    load_actor(session, actor_id, capability="MOLIENDA_VER")
    return {
        "familias_material": [item.to_dict() for item in session.scalars(select(ScmFamiliaMaterialReproceso).where(ScmFamiliaMaterialReproceso.activo.is_(True)).order_by(ScmFamiliaMaterialReproceso.codigo))],
        "procesos": [item.to_dict() for item in session.scalars(select(ScmProcesoMaterialReproceso).where(ScmProcesoMaterialReproceso.activo.is_(True)).order_by(ScmProcesoMaterialReproceso.codigo))],
        "condiciones": [item.to_dict() for item in session.scalars(select(ScmCondicionMerma).where(ScmCondicionMerma.activo.is_(True)).order_by(ScmCondicionMerma.codigo))],
        "colores": [{"id": item.id, "nombre": item.nombre} for item in session.scalars(select(ColorBase).order_by(ColorBase.nombre))],
        "familias_color": [{"id": item.id, "nombre": item.nombre} for item in session.scalars(select(FamiliaColor).where(FamiliaColor.activo.is_(True)).order_by(FamiliaColor.nombre))],
        "materiales_salida": [item.to_dict() for item in session.scalars(select(ScmMaterial).where(ScmMaterial.activo.is_(True), ScmMaterial.clase == "MATERIA_PRIMA").order_by(ScmMaterial.codigo))],
        "ubicaciones": [item.to_dict() for item in session.scalars(select(ScmUbicacionInventario).where(ScmUbicacionInventario.activo.is_(True)).order_by(ScmUbicacionInventario.codigo))],
    }


def list_compatibility_rules(session, *, actor_id):
    load_actor(session, actor_id, capability="MOLIENDA_VER")
    items = session.scalars(select(ScmReglaCompatibilidadReproceso).order_by(
        ScmReglaCompatibilidadReproceso.codigo,
        ScmReglaCompatibilidadReproceso.revision.desc(),
    )).all()
    return {"items": [item.to_dict() for item in items]}


def create_compatibility_rule(session, *, actor_id, data):
    actor = load_actor(session, actor_id, capability="MOLIENDA_REGLA_ADMINISTRAR")
    code = stable_code(data.get("codigo"), max_length=64)
    revisions = session.scalars(select(ScmReglaCompatibilidadReproceso).where(ScmReglaCompatibilidadReproceso.codigo == code)).all()
    result = stable_code(data.get("resultado"), field="resultado", max_length=20)
    if result not in {"COMPATIBLE", "CONDICIONADA", "INCOMPATIBLE"}:
        raise ScmServiceError("INVALID_COMPATIBILITY_RESULT", "El resultado de compatibilidad no es valido.", status_code=422)
    max_pct = None
    if result == "CONDICIONADA":
        try:
            max_pct = Decimal(str(data.get("porcentaje_maximo")))
        except (InvalidOperation, TypeError, ValueError):
            raise ScmServiceError("INVALID_COMPATIBILITY_PERCENTAGE", "El porcentaje maximo debe estar entre 0 y 100.", status_code=422)
        if not max_pct.is_finite() or max_pct <= 0 or max_pct > 100:
            raise ScmServiceError("INVALID_COMPATIBILITY_PERCENTAGE", "El porcentaje maximo debe estar entre 0 y 100.", status_code=422)
        max_pct = max_pct.quantize(Decimal("0.01"))
    item = ScmReglaCompatibilidadReproceso(
        codigo=code,
        revision=max((revision.revision for revision in revisions), default=0) + 1,
        nombre=required_text(data.get("nombre"), field="nombre", max_length=160),
        familia_objetivo_id=data.get("familia_objetivo_id"),
        proceso_objetivo_id=data.get("proceso_objetivo_id"),
        familia_aporte_id=data.get("familia_aporte_id"),
        proceso_aporte_id=data.get("proceso_aporte_id"),
        color_objetivo_id=data.get("color_objetivo_id"),
        familia_color_objetivo_id=data.get("familia_color_objetivo_id"),
        color_aporte_id=data.get("color_aporte_id"),
        familia_color_aporte_id=data.get("familia_color_aporte_id"),
        resultado=result,
        porcentaje_maximo=max_pct,
        simetrica=bool(data.get("simetrica", False)),
        notas=(data.get("notas") or "").strip() or None,
        creado_por_id=actor.id,
    )
    session.add(item)
    session.commit()
    return item.to_dict()


def approve_compatibility_rule(session, *, actor_id, rule_id):
    actor = load_actor(session, actor_id, capability="MOLIENDA_REGLA_APROBAR")
    item = session.get(ScmReglaCompatibilidadReproceso, rule_id)
    if item is None:
        raise ScmServiceError("COMPATIBILITY_RULE_NOT_FOUND", "La regla no existe.", status_code=404)
    if item.estado != "BORRADOR":
        raise ScmServiceError("INVALID_RULE_STATE", "Solo una regla borrador puede aprobarse.", status_code=409)
    if item.creado_por_id == actor.id:
        raise ScmServiceError("SELF_APPROVAL_NOT_ALLOWED", "El creador no puede aprobar su propia regla.", status_code=409)
    approved = session.scalars(select(ScmReglaCompatibilidadReproceso).where(
        ScmReglaCompatibilidadReproceso.codigo == item.codigo,
        ScmReglaCompatibilidadReproceso.estado == "APROBADA",
    )).all()
    for old in approved:
        old.estado = "RETIRADA"
    item.estado = "APROBADA"
    item.aprobado_por_id = actor.id
    item.approved_at = utc_now()
    session.commit()
    return item.to_dict()


def _location(session, code, name):
    normalized = stable_code(code or "ALMACEN_MERMA", field="ubicacion_codigo", max_length=40)
    item = session.scalar(select(ScmUbicacionInventario).where(ScmUbicacionInventario.codigo == normalized))
    if item is None:
        item = ScmUbicacionInventario(codigo=normalized, nombre=(name or normalized).strip())
        session.add(item)
        session.flush()
    return item


def list_scrap_lots(session, *, actor_id, state=None):
    load_actor(session, actor_id, capability="MOLIENDA_VER")
    statement = select(ScmLoteMermaRecuperable)
    if state:
        statement = statement.where(ScmLoteMermaRecuperable.estado == state.upper())
    items = session.scalars(statement.order_by(ScmLoteMermaRecuperable.created_at.desc())).unique().all()
    return {"items": [item.to_dict() for item in items]}


def register_scrap_lot(session, *, actor_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="MERMA_RECUPERABLE_REGISTRAR")
    gross = positive_kg(data.get("peso_bruto_kg"), field="peso_bruto_kg")
    tare = _nonnegative_kg(data.get("tara_kg", 0), field="tara_kg")
    net = gross - tare
    if net <= 0:
        raise ScmServiceError("INVALID_NET_WEIGHT", "El peso neto debe ser mayor que cero.", status_code=422)
    condition = session.get(ScmCondicionMerma, data.get("condicion_id"))
    if condition is None or not condition.activo:
        raise ScmServiceError("SCRAP_CONDITION_NOT_FOUND", "La condicion no existe o esta inactiva.", status_code=422)
    if not condition.recuperable:
        raise ScmServiceError("SCRAP_NOT_RECOVERABLE", "La condicion clasifica el material como no recuperable.", status_code=409)
    command = {
        "familia_material_id": data.get("familia_material_id"),
        "proceso_origen_id": data.get("proceso_origen_id"),
        "condicion_id": condition.id, "color_id": data.get("color_id"),
        "familia_color_id": data.get("familia_color_id"), "material_id": data.get("material_id"),
        "origen_tipo": stable_code(data.get("origen_tipo"), field="origen_tipo", max_length=40),
        "origen_id": required_text(data.get("origen_id"), field="origen_id", max_length=64),
        "peso_bruto_kg": format(gross, "f"), "tara_kg": format(tare, "f"),
        "ubicacion_codigo": stable_code(data.get("ubicacion_codigo") or "ALMACEN_MERMA", field="ubicacion_codigo", max_length=40),
        "observaciones": (data.get("observaciones") or "").strip() or None,
    }
    operation, replay = _reserve_operation(session, operation_id, "POST /reproceso/mermas", actor, command)
    if replay is not None:
        return replay
    try:
        for model, value, code in (
            (ScmFamiliaMaterialReproceso, command["familia_material_id"], "SCRAP_FAMILY_NOT_FOUND"),
            (ScmProcesoMaterialReproceso, command["proceso_origen_id"], "SCRAP_PROCESS_NOT_FOUND"),
            (ColorBase, command["color_id"], "SCRAP_COLOR_NOT_FOUND"),
        ):
            if session.get(model, value) is None:
                raise ScmServiceError(code, "Falta un maestro requerido para clasificar la merma.", status_code=422)
        location = _location(session, command["ubicacion_codigo"], data.get("ubicacion_nombre"))
        lot = ScmLoteMermaRecuperable(
            codigo=f"MER-{uuid.uuid4().hex[:10].upper()}",
            familia_material_id=command["familia_material_id"],
            proceso_origen_id=command["proceso_origen_id"], condicion_id=condition.id,
            color_id=command["color_id"], familia_color_id=command["familia_color_id"],
            material_id=command["material_id"], ubicacion_id=location.id,
            origen_tipo=command["origen_tipo"], origen_id=command["origen_id"],
            peso_bruto_almacen_kg=gross, tara_kg=tare,
            peso_neto_almacen_kg=net, saldo_disponible_kg=net,
            observaciones=command["observaciones"], pesado_por_id=actor.id,
        )
        session.add(lot)
        session.flush()
        movement = ScmMovimientoMerma(
            lote=lot, tipo="INGRESO_ALMACEN", cantidad_delta_kg=net,
            saldo_resultante_kg=net, referencia_tipo=command["origen_tipo"],
            referencia_id=command["origen_id"], motivo="Pesaje de ingreso a almacen de merma",
            actor_id=actor.id, operation_id=operation.operation_id,
        )
        session.add(movement)
        response = {"lote": lot.to_dict(), "movimiento": movement.to_dict()}
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="LOTE_MERMA_RECUPERABLE", aggregate_id=str(lot.id),
            tipo="SCRAP_STORED", actor_id=actor.id, actor_snapshot=actor_snapshot(actor),
            after_json=response, operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def list_scrap_movements(session, *, actor_id, lot_id):
    load_actor(session, actor_id, capability="MOLIENDA_VER")
    lot = session.get(ScmLoteMermaRecuperable, lot_id)
    if lot is None:
        raise ScmServiceError("SCRAP_LOT_NOT_FOUND", "La bolsa de merma no existe.", status_code=404)
    return {"lote": lot.to_dict(), "items": [item.to_dict() for item in sorted(lot.movimientos, key=lambda x: x.created_at)]}


def list_grinding_orders(session, *, actor_id):
    load_actor(session, actor_id, capability="MOLIENDA_VER")
    items = session.scalars(select(ScmOrdenMolienda).order_by(ScmOrdenMolienda.created_at.desc())).unique().all()
    return {"items": [item.to_dict() for item in items]}


def create_grinding_order(session, *, actor_id, data):
    actor = load_actor(session, actor_id, capability="MOLIENDA_ORDEN_CREAR")
    balance_tolerance = data.get("tolerancia_balance_kg")
    item = ScmOrdenMolienda(
        codigo=f"OM-{utc_now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
        familia_objetivo_id=data.get("familia_objetivo_id"),
        proceso_objetivo_id=data.get("proceso_objetivo_id"),
        color_objetivo_id=data.get("color_objetivo_id"),
        familia_color_objetivo_id=data.get("familia_color_objetivo_id"),
        material_salida_id=data.get("material_salida_id"),
        tolerancia_custodia_kg=positive_kg(data.get("tolerancia_custodia_kg", 1), field="tolerancia_custodia_kg"),
        tolerancia_balance_kg=positive_kg(balance_tolerance, field="tolerancia_balance_kg") if balance_tolerance not in (None, "") else None,
        notas=(data.get("notas") or "").strip() or None,
        creado_por_id=actor.id,
    )
    for model, value in (
        (ScmFamiliaMaterialReproceso, item.familia_objetivo_id),
        (ScmProcesoMaterialReproceso, item.proceso_objetivo_id),
        (ColorBase, item.color_objetivo_id), (ScmMaterial, item.material_salida_id),
    ):
        if session.get(model, value) is None:
            raise ScmServiceError("GRINDING_TARGET_INVALID", "La especificacion objetivo esta incompleta.", status_code=422)
    session.add(item)
    session.commit()
    return item.to_dict()


def add_grinding_input(session, *, actor_id, order_id, data):
    load_actor(session, actor_id, capability="MOLIENDA_ORDEN_CREAR")
    order = session.get(ScmOrdenMolienda, order_id)
    if order is None:
        raise ScmServiceError("GRINDING_ORDER_NOT_FOUND", "La orden no existe.", status_code=404)
    if order.estado not in {"BORRADOR", "BLOQUEADA_COMPATIBILIDAD"}:
        raise ScmServiceError("INVALID_GRINDING_ORDER_STATE", "La orden ya no admite aportes.", status_code=409)
    quantity = positive_kg(data.get("cantidad_planificada_kg"), field="cantidad_planificada_kg")
    try:
        lot_key = uuid.UUID(str(data.get("lote_merma_id")))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError("SCRAP_LOT_NOT_FOUND", "La bolsa de merma no existe.", status_code=404) from error
    lot = session.get(ScmLoteMermaRecuperable, lot_key)
    if lot is None or lot.estado in {"CONSUMIDA", "BLOQUEADA", "ANULADA"}:
        raise ScmServiceError("SCRAP_LOT_NOT_AVAILABLE", "La bolsa de merma no esta disponible.", status_code=409)
    if any(item.lote_merma_id == lot.id for item in order.aportes):
        raise ScmServiceError("DUPLICATE_GRINDING_INPUT", "La bolsa ya pertenece a esta orden.", status_code=409)
    free = Decimal(lot.saldo_disponible_kg) - Decimal(lot.saldo_reservado_kg)
    if quantity > free:
        raise ScmServiceError("INSUFFICIENT_SCRAP_STOCK", "La cantidad supera el saldo libre de la bolsa.", status_code=409, details={"saldo_libre_kg": format(free, "f")})
    contribution = ScmOrdenMoliendaAporte(
        orden=order, lote_merma=lot, cantidad_planificada_kg=quantity,
    )
    lot.saldo_reservado_kg = Decimal(lot.saldo_reservado_kg) + quantity
    lot.estado = "RESERVADA"
    lot.version += 1
    order.estado = "BORRADOR"
    order.version += 1
    session.add(contribution)
    session.commit()
    return order.to_dict()


def _matching_rule(session, order, lot):
    candidates = session.scalars(select(ScmReglaCompatibilidadReproceso).where(
        ScmReglaCompatibilidadReproceso.estado == "APROBADA",
    ).order_by(ScmReglaCompatibilidadReproceso.revision.desc())).all()
    for rule in candidates:
        direct = (
            rule.familia_objetivo_id == order.familia_objetivo_id
            and rule.proceso_objetivo_id == order.proceso_objetivo_id
            and rule.familia_aporte_id == lot.familia_material_id
            and rule.proceso_aporte_id == lot.proceso_origen_id
        )
        reverse = rule.simetrica and (
            rule.familia_aporte_id == order.familia_objetivo_id
            and rule.proceso_aporte_id == order.proceso_objetivo_id
            and rule.familia_objetivo_id == lot.familia_material_id
            and rule.proceso_objetivo_id == lot.proceso_origen_id
        )
        if not (direct or reverse):
            continue
        target_color = rule.color_objetivo_id if direct else rule.color_aporte_id
        source_color = rule.color_aporte_id if direct else rule.color_objetivo_id
        target_family_color = rule.familia_color_objetivo_id if direct else rule.familia_color_aporte_id
        source_family_color = rule.familia_color_aporte_id if direct else rule.familia_color_objetivo_id
        if target_color is not None and target_color != order.color_objetivo_id:
            continue
        if source_color is not None and source_color != lot.color_id:
            continue
        if target_family_color is not None and target_family_color != order.familia_color_objetivo_id:
            continue
        if source_family_color is not None and source_family_color != lot.familia_color_id:
            continue
        # Una regla sin colores es comodin de proceso, pero exige mismo color nominal.
        if target_color is None and source_color is None and lot.color_id != order.color_objetivo_id:
            continue
        return rule
    return None


def _evaluate_order(session, order, *, real=False):
    if not order.aportes:
        raise ScmServiceError("GRINDING_INPUT_REQUIRED", "Agregue al menos una bolsa de merma.", status_code=422)
    quantities = []
    for item in order.aportes:
        value = item.peso_pre_molino_kg if real else item.cantidad_planificada_kg
        if value is None:
            raise ScmServiceError("PRE_MILL_WEIGHT_REQUIRED", "Todos los aportes requieren peso previo al molino.", status_code=422)
        quantities.append(Decimal(value))
    total = sum(quantities, Decimal("0"))
    blocked = []
    for contribution, quantity in zip(order.aportes, quantities):
        percentage = (quantity / total * Decimal("100")).quantize(Decimal("0.0001"))
        contribution.porcentaje_real = percentage if real else contribution.porcentaje_real
        rule = _matching_rule(session, order, contribution.lote_merma)
        if rule is None:
            result = "SIN_REGLA"
            blocked.append({"aporte_id": contribution.id, "motivo": "SIN_REGLA"})
            contribution.regla_revision_id = None
            contribution.regla_snapshot = None
        else:
            result = rule.resultado
            contribution.regla_revision_id = rule.id
            contribution.regla_snapshot = rule.to_dict()
            if result == "INCOMPATIBLE":
                blocked.append({"aporte_id": contribution.id, "motivo": "INCOMPATIBLE"})
            elif result == "CONDICIONADA" and percentage > Decimal(rule.porcentaje_maximo):
                blocked.append({
                    "aporte_id": contribution.id, "motivo": "PORCENTAJE_EXCEDIDO",
                    "porcentaje": format(percentage, "f"),
                    "maximo": format(rule.porcentaje_maximo, "f"),
                })
        contribution.resultado_compatibilidad = result
    return total, blocked


def validate_grinding_order(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="MOLIENDA_ORDEN_CREAR")
    order = session.get(ScmOrdenMolienda, order_id)
    if order is None:
        raise ScmServiceError("GRINDING_ORDER_NOT_FOUND", "La orden no existe.", status_code=404)
    _, blocked = _evaluate_order(session, order, real=False)
    if blocked and not order.excepcion_aprobada_por_id:
        order.estado = "BLOQUEADA_COMPATIBILIDAD"
    else:
        order.estado = "VALIDADA"
    order.validated_at = utc_now()
    order.version += 1
    session.commit()
    payload = order.to_dict()
    payload["bloqueos"] = blocked
    return payload


def approve_grinding_exception(session, *, actor_id, order_id, data):
    actor = load_actor(session, actor_id, capability="MOLIENDA_EXCEPCION_APROBAR")
    order = session.get(ScmOrdenMolienda, order_id)
    if order is None:
        raise ScmServiceError("GRINDING_ORDER_NOT_FOUND", "La orden no existe.", status_code=404)
    if order.creado_por_id == actor.id:
        raise ScmServiceError("SELF_APPROVAL_NOT_ALLOWED", "El creador no puede autorizar su propia excepcion.", status_code=409)
    order.excepcion_motivo = required_text(data.get("motivo"), field="motivo", max_length=500)
    order.excepcion_aprobada_por_id = actor.id
    order.mezcla_excepcional = True
    order.estado = "VALIDADA"
    order.version += 1
    session.commit()
    return order.to_dict()


def record_pre_mill_weights(session, *, actor_id, order_id, data):
    actor = load_actor(session, actor_id, capability="MOLIENDA_EJECUTAR")
    order = session.get(ScmOrdenMolienda, order_id)
    if order is None:
        raise ScmServiceError("GRINDING_ORDER_NOT_FOUND", "La orden no existe.", status_code=404)
    if order.estado not in {"VALIDADA", "BLOQUEADA_COMPATIBILIDAD"}:
        raise ScmServiceError("INVALID_GRINDING_ORDER_STATE", "Primero valide la orden.", status_code=409)
    values = {int(item["aporte_id"]): item for item in data.get("aportes", [])}
    alerts = []
    for contribution in order.aportes:
        payload = values.get(contribution.id)
        if payload is None:
            raise ScmServiceError("PRE_MILL_WEIGHT_REQUIRED", "Debe pesar todos los aportes.", status_code=422)
        weight = positive_kg(payload.get("peso_pre_molino_kg"), field="peso_pre_molino_kg")
        lot = contribution.lote_merma
        reserved_by_others = max(
            Decimal("0"),
            Decimal(lot.saldo_reservado_kg) - Decimal(contribution.cantidad_planificada_kg),
        )
        available_for_order = Decimal(lot.saldo_disponible_kg) - reserved_by_others
        if weight > available_for_order:
            raise ScmServiceError(
                "INSUFFICIENT_SCRAP_STOCK",
                "El peso previo invade saldo reservado para otra orden.",
                status_code=409,
                details={"lote": lot.codigo, "disponible_para_orden_kg": format(available_for_order, "f")},
            )
        difference = weight - Decimal(contribution.cantidad_planificada_kg)
        contribution.peso_pre_molino_kg = weight
        contribution.diferencia_custodia_kg = difference
        contribution.excede_tolerancia = abs(difference) > Decimal(order.tolerancia_custodia_kg)
        contribution.motivo_diferencia = (payload.get("motivo_diferencia") or "").strip() or None
        contribution.pesado_por_id = actor.id
        contribution.pesado_at = utc_now()
        if contribution.excede_tolerancia:
            alert = upsert_operational_alert(
                session,
                rule_code="DIFERENCIA_CUSTODIA_MERMA",
                aggregate_type="APORTE_MOLIENDA",
                aggregate_id=contribution.id,
                condition_key=f"peso:{format(weight, 'f')}",
                summary=f"Diferencia de custodia en {contribution.lote_merma.codigo}",
                detail={
                    "lote": contribution.lote_merma.codigo,
                    "planificado_kg": format(contribution.cantidad_planificada_kg, "f"),
                    "pre_molino_kg": format(weight, "f"),
                    "diferencia_kg": format(difference, "f"),
                    "tolerancia_kg": format(order.tolerancia_custodia_kg, "f"),
                },
                actor_id=actor.id,
            )
            if alert:
                alerts.append(str(alert.id))
    _, blocked = _evaluate_order(session, order, real=True)
    if blocked and not order.excepcion_aprobada_por_id:
        order.estado = "BLOQUEADA_COMPATIBILIDAD"
    order.version += 1
    session.commit()
    response = order.to_dict()
    response["alertas_generadas"] = alerts
    response["bloqueos"] = blocked
    return response


def authorize_custody_difference(session, *, actor_id, contribution_id, data):
    actor = load_actor(session, actor_id, capability="MOLIENDA_EXCEPCION_APROBAR")
    item = session.get(ScmOrdenMoliendaAporte, contribution_id)
    if item is None:
        raise ScmServiceError("GRINDING_INPUT_NOT_FOUND", "El aporte no existe.", status_code=404)
    if not item.excede_tolerancia:
        raise ScmServiceError("CUSTODY_AUTH_NOT_REQUIRED", "El aporte esta dentro de tolerancia.", status_code=409)
    if item.pesado_por_id == actor.id:
        raise ScmServiceError("SELF_APPROVAL_NOT_ALLOWED", "Quien registro el peso no puede autorizar la diferencia.", status_code=409)
    item.motivo_diferencia = required_text(data.get("motivo"), field="motivo", max_length=500)
    item.autorizado_por_id = actor.id
    session.commit()
    return item.to_dict()


def start_grinding_order(session, *, actor_id, order_id):
    actor = load_actor(session, actor_id, capability="MOLIENDA_EJECUTAR")
    order = session.get(ScmOrdenMolienda, order_id)
    if order is None:
        raise ScmServiceError("GRINDING_ORDER_NOT_FOUND", "La orden no existe.", status_code=404)
    if order.estado != "VALIDADA":
        raise ScmServiceError("INVALID_GRINDING_ORDER_STATE", "La orden debe estar validada.", status_code=409)
    _, blocked = _evaluate_order(session, order, real=True)
    if blocked and not order.excepcion_aprobada_por_id:
        raise ScmServiceError("GRINDING_COMPATIBILITY_BLOCK", "La mezcla real no es compatible.", status_code=409, details={"bloqueos": blocked})
    pending_auth = [item.id for item in order.aportes if item.excede_tolerancia and item.autorizado_por_id is None]
    if pending_auth:
        raise ScmServiceError("CUSTODY_AUTH_REQUIRED", "Hay diferencias de custodia sin autorizar.", status_code=409, details={"aportes": pending_auth})
    order.estado = "EN_EJECUCION"
    order.ejecutado_por_id = actor.id
    order.started_at = utc_now()
    order.version += 1
    session.commit()
    return order.to_dict()


def close_grinding_order(session, *, actor_id, order_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="MOLIENDA_EJECUTAR")
    order = session.get(ScmOrdenMolienda, order_id)
    if order is None:
        raise ScmServiceError("GRINDING_ORDER_NOT_FOUND", "La orden no existe.", status_code=404)
    outputs = data.get("salidas") or []
    if not isinstance(outputs, list) or not outputs:
        raise ScmServiceError("GRINDING_OUTPUT_REQUIRED", "Registre al menos una bolsa de salida.", status_code=422)
    normalized_outputs = []
    for output in outputs:
        normalized_outputs.append({
            "peso_neto_kg": format(positive_kg(output.get("peso_neto_kg"), field="peso_neto_kg"), "f"),
            "ubicacion_codigo": stable_code(output.get("ubicacion_codigo") or "ALMACEN_RECUPERADO", field="ubicacion_codigo", max_length=40),
            "ubicacion_nombre": (output.get("ubicacion_nombre") or "Almacen de material recuperado").strip(),
        })
    loss = _nonnegative_kg(data.get("perdida_kg", 0), field="perdida_kg")
    command = {"orden_id": str(order.id), "salidas": normalized_outputs, "perdida_kg": format(loss, "f")}
    operation, replay = _reserve_operation(session, operation_id, f"POST /reproceso/ordenes-molienda/{order.id}/cerrar", actor, command)
    if replay is not None:
        return replay
    try:
        if order.estado != "EN_EJECUCION":
            raise ScmServiceError("INVALID_GRINDING_ORDER_STATE", "La orden debe estar en ejecucion.", status_code=409)
        if order.tolerancia_balance_kg is None:
            raise ScmServiceError("BALANCE_TOLERANCE_REQUIRED", "Configure la tolerancia de balance antes de cerrar.", status_code=409)
        input_total, blocked = _evaluate_order(session, order, real=True)
        if blocked and not order.excepcion_aprobada_por_id:
            raise ScmServiceError("GRINDING_COMPATIBILITY_BLOCK", "La mezcla real no es compatible.", status_code=409, details={"bloqueos": blocked})
        output_total = sum((Decimal(item["peso_neto_kg"]) for item in normalized_outputs), Decimal("0"))
        difference = input_total - output_total - loss
        if abs(difference) > Decimal(order.tolerancia_balance_kg) and not order.excepcion_aprobada_por_id:
            raise ScmServiceError("GRINDING_BALANCE_OUT_OF_TOLERANCE", "El balance de masa esta fuera de tolerancia.", status_code=409, details={"diferencia_kg": format(difference, "f"), "tolerancia_kg": format(order.tolerancia_balance_kg, "f")})
        composition = []
        for contribution in order.aportes:
            lot = session.scalar(select(ScmLoteMermaRecuperable).where(ScmLoteMermaRecuperable.id == contribution.lote_merma_id).with_for_update())
            consumed = Decimal(contribution.peso_pre_molino_kg)
            if consumed > Decimal(lot.saldo_disponible_kg):
                raise ScmServiceError("INSUFFICIENT_SCRAP_STOCK", "Una bolsa ya no tiene saldo suficiente.", status_code=409, details={"lote": lot.codigo})
            lot.saldo_disponible_kg = Decimal(lot.saldo_disponible_kg) - consumed
            lot.saldo_reservado_kg = max(Decimal("0"), Decimal(lot.saldo_reservado_kg) - Decimal(contribution.cantidad_planificada_kg))
            lot.estado = "CONSUMIDA" if lot.saldo_disponible_kg == 0 else ("RESERVADA" if lot.saldo_reservado_kg > 0 else "ALMACENADA")
            lot.version += 1
            movement = ScmMovimientoMerma(
                lote=lot, tipo="CONSUMO_MOLIENDA", cantidad_delta_kg=-consumed,
                saldo_resultante_kg=lot.saldo_disponible_kg,
                referencia_tipo="ORDEN_MOLIENDA", referencia_id=str(order.id),
                motivo=f"Consumo confirmado por {order.codigo}", actor_id=actor.id,
                operation_id=operation.operation_id,
            )
            session.add(movement)
            composition.append({
                "lote_merma_id": str(lot.id), "lote_codigo": lot.codigo,
                "kg": format(consumed, "f"),
                "porcentaje": format((consumed / input_total * Decimal("100")).quantize(Decimal("0.0001")), "f"),
                "familia_material_id": lot.familia_material_id,
                "proceso_origen_id": lot.proceso_origen_id,
                "color_id": lot.color_id, "familia_color_id": lot.familia_color_id,
                "regla_revision_id": contribution.regla_revision_id,
            })
        recovered = []
        for output in normalized_outputs:
            weight = Decimal(output["peso_neto_kg"])
            location = _location(session, output["ubicacion_codigo"], output["ubicacion_nombre"])
            recovered_lot = ScmLoteMaterialRecuperado(
                codigo=f"REC-{uuid.uuid4().hex[:10].upper()}", orden=order,
                material_id=order.material_salida_id, ubicacion_id=location.id,
                peso_neto_kg=weight, saldo_disponible_kg=weight,
                composicion_snapshot=composition,
                mezcla_excepcional=order.mezcla_excepcional,
                producido_por_id=actor.id,
            )
            session.add(recovered_lot)
            session.flush()
            recovered.append(recovered_lot.to_dict())
        order.estado = "CERRADA"
        order.entrada_real_kg = input_total
        order.salida_real_kg = output_total
        order.perdida_real_kg = loss
        order.diferencia_balance_kg = difference
        order.cerrado_por_id = actor.id
        order.closed_at = utc_now()
        order.version += 1
        response = {"orden": order.to_dict(include_lines=False), "lotes_recuperados": recovered, "composicion": composition}
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="ORDEN_MOLIENDA", aggregate_id=str(order.id),
            tipo="GRINDING_CLOSED", actor_id=actor.id, actor_snapshot=actor_snapshot(actor),
            after_json=response, operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def list_recovered_lots(session, *, actor_id, state=None):
    load_actor(session, actor_id, capability="MOLIENDA_VER")
    statement = select(ScmLoteMaterialRecuperado)
    if state:
        statement = statement.where(ScmLoteMaterialRecuperado.estado == state.upper())
    items = session.scalars(statement.order_by(ScmLoteMaterialRecuperado.producido_at.desc())).unique().all()
    return {"items": [item.to_dict() for item in items]}


def release_recovered_lot(session, *, actor_id, lot_id, data):
    actor = load_actor(session, actor_id, capability="MOLIENDA_LOTE_LIBERAR")
    lot = session.get(ScmLoteMaterialRecuperado, lot_id)
    if lot is None:
        raise ScmServiceError("RECOVERED_LOT_NOT_FOUND", "El lote recuperado no existe.", status_code=404)
    if lot.estado != "PENDIENTE_LIBERACION":
        raise ScmServiceError("INVALID_RECOVERED_LOT_STATE", "El lote no esta pendiente de liberacion.", status_code=409)
    required_text(data.get("motivo"), field="motivo", max_length=500)
    lot.estado = "DISPONIBLE"
    lot.liberado_por_id = actor.id
    lot.liberado_at = utc_now()
    session.add(ScmEvento(
        aggregate_type="LOTE_MATERIAL_RECUPERADO", aggregate_id=str(lot.id),
        tipo="RECOVERED_LOT_RELEASED", actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor), motivo=data.get("motivo"),
        after_json=lot.to_dict(),
    ))
    session.commit()
    return lot.to_dict()
