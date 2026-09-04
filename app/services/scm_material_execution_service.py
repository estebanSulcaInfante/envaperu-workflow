"""Ejecucion minima de materiales para corridas de ordenes de fabricacion."""

import copy
from decimal import Decimal, ROUND_HALF_UP
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.models.receta_color import RecetaColorMaestra
from app.models.scm_auditoria import ScmEvento
from app.models.scm_inventory import (
    ScmMovimientoMaterialInventario,
    ScmSaldoMaterialInventario,
    ScmUbicacionInventario,
)
from app.models.scm_material_execution import (
    ScmDevolucionMaterial,
    ScmEmisionMaterial,
    ScmLotePremezcla,
    ScmLotePremezclaInput,
    ScmRequerimientoMaterial,
    ScmReservaMaterial,
)
from app.models.scm_production_orders import ScmCorridaFabricacion, ScmOrdenOperacion
from app.models.scm_prepared_material import (
    ScmAsignacionRequerimientoPreparacion,
    ScmOrdenPreparacionMaterial,
    ScmRequerimientoMaterialPreparado,
)
from app.services.scm_inventory_service import _positive_quantity, _reserve_operation
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    load_actor,
    reject_unknown_fields,
    required_text,
)


QTY = Decimal("0.001")


def _kg(value):
    return format(Decimal(value or 0).quantize(QTY), "f")


def _available(balance):
    return (
        Decimal(balance.cantidad_fisica_kg)
        - Decimal(balance.cantidad_reservada_kg)
        - Decimal(balance.cantidad_no_disponible_kg)
    )


def _serialize_reservation(item):
    return {
        "id": str(item.id),
        "saldo_material_id": str(item.saldo_material_id),
        "ubicacion": {
            "codigo": item.saldo.ubicacion.codigo,
            "nombre": item.saldo.ubicacion.nombre,
        },
        "cantidad_kg": _kg(item.cantidad_kg),
        "emitida_neta_kg": _kg(item.emitida_neta_kg),
        "cantidad_consumida_kg": _kg(item.cantidad_consumida_kg),
        "estado": item.estado,
        "emisiones": [_serialize_emission(value) for value in item.emisiones],
    }


def _serialize_emission(item):
    return {
        "id": str(item.id),
        "reserva_id": str(item.reserva_id),
        "cantidad_kg": _kg(item.cantidad_kg),
        "cantidad_devuelta_kg": _kg(item.cantidad_devuelta_kg),
        "cantidad_consumida_kg": _kg(item.cantidad_consumida_kg),
        "cantidad_neta_kg": _kg(
            Decimal(item.cantidad_kg)
            - Decimal(item.cantidad_devuelta_kg)
            - Decimal(item.cantidad_consumida_kg)
        ),
        "destino": {
            "codigo": item.saldo_destino.ubicacion.codigo,
            "nombre": item.saldo_destino.ubicacion.nombre,
        },
        "motivo": item.motivo,
        "actor_id": item.actor_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "devoluciones": [{
            "id": str(value.id),
            "cantidad_kg": _kg(value.cantidad_kg),
            "motivo": value.motivo,
            "actor_id": value.actor_id,
            "created_at": value.created_at.isoformat() if value.created_at else None,
        } for value in item.devoluciones],
    }


def _serialize_requirement(item):
    reserved = sum((Decimal(value.cantidad_kg) for value in item.reservas), Decimal("0"))
    emitted = sum((Decimal(value.emitida_neta_kg) for value in item.reservas), Decimal("0"))
    consumed = sum((Decimal(value.cantidad_consumida_kg) for value in item.reservas), Decimal("0"))
    return {
        "id": str(item.id),
        "corrida_fabricacion_id": str(item.corrida_fabricacion_id),
        "material": {
            "id": item.material.id,
            "codigo": item.material.codigo,
            "nombre": item.material.nombre,
            "clase": item.material.clase,
        },
        "tipo_componente": item.tipo_componente,
        "cantidad_plan_kg": _kg(item.cantidad_plan_kg),
        "cantidad_reservada_kg": _kg(reserved),
        "cantidad_emitida_neta_kg": _kg(emitted),
        "cantidad_consumida_preparacion_kg": _kg(consumed),
        "receta_revision_id": item.receta_revision_id,
        "calculo_snapshot": copy.deepcopy(item.calculo_snapshot_json),
        "reservas": [_serialize_reservation(value) for value in item.reservas],
    }


def _load_run(session, run_id, *, lock=False):
    statement = select(ScmCorridaFabricacion).where(
        ScmCorridaFabricacion.id == run_id
    )
    if lock:
        statement = statement.with_for_update(of=ScmCorridaFabricacion)
    run = session.scalar(statement)
    if run is None:
        raise ScmServiceError("FABRICATION_RUN_NOT_FOUND", "La corrida no existe.", status_code=404)
    return run


def _run_payload(run, requirements):
    order = run.orden_fabricacion.orden_operacion
    outputs = [{
        "articulo_scm_id": value.articulo_scm_id,
        "codigo": value.articulo.codigo,
        "nombre": value.articulo.nombre,
        "cantidad_objetivo": format(Decimal(value.cantidad_objetivo), "f"),
        "kg_estandar_objetivo": _kg(value.kg_estandar_objetivo),
    } for value in run.salidas]
    premixes = run.corrida_premezclas
    return {
        "orden_fabricacion": {
            "id": str(order.id), "codigo": order.codigo, "estado": order.estado,
            "maquina_prevista_id": run.orden_fabricacion.maquina_prevista_id,
        },
        "corrida": {
            "id": str(run.id), "codigo": run.codigo, "estado": run.estado,
            "receta_revision_id": run.receta_revision_id,
            "ciclos_objetivo": run.ciclos_objetivo,
            "color": (
                run.receta_revision.color_produccion.nombre
                if run.receta_revision else None
            ),
            "salidas": outputs,
        },
        "requerimientos": [_serialize_requirement(value) for value in requirements],
        "premezclas": [_serialize_premix(value) for value in premixes],
    }


def _serialize_premix(item):
    return {
        "id": str(item.id), "codigo": item.codigo,
        "corrida_fabricacion_id": str(item.corrida_fabricacion_id),
        "secuencia": item.secuencia, "cantidad_kg": _kg(item.cantidad_kg),
        "genealogia_tipo": item.genealogia_tipo, "estado": item.estado,
        "ubicacion_codigo": item.ubicacion_codigo, "motivo": item.motivo,
        "actor_id": item.actor_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "inputs": [{
            "emision_id": str(value.emision_id),
            "material": {
                "id": value.emision.reserva.requerimiento.material.id,
                "codigo": value.emision.reserva.requerimiento.material.codigo,
                "nombre": value.emision.reserva.requerimiento.material.nombre,
            },
            "cantidad_kg": _kg(value.cantidad_kg),
        } for value in item.inputs],
    }


def list_material_execution(session, *, actor_id, fabrication_order_id=None):
    load_actor(session, actor_id, capability="INVENTARIO_VER")
    statement = select(ScmCorridaFabricacion).join(
        ScmCorridaFabricacion.orden_fabricacion
    ).join(ScmOrdenOperacion)
    if fabrication_order_id is not None:
        statement = statement.where(ScmOrdenOperacion.id == fabrication_order_id)
    runs = session.scalars(
        statement.order_by(ScmOrdenOperacion.created_at.desc(), ScmCorridaFabricacion.secuencia)
    ).unique().all()
    return {"items": [
        _run_payload(run, session.scalars(
            select(ScmRequerimientoMaterial)
            .where(ScmRequerimientoMaterial.corrida_fabricacion_id == run.id)
            .order_by(ScmRequerimientoMaterial.id)
        ).all()) for run in runs
    ]}


def generate_material_requirements(session, *, actor_id, operation_id, fabrication_order_id):
    try:
        actor = load_actor(session, actor_id, capability="MATERIAL_REQUERIMIENTO_GENERAR")
        data = {"orden_fabricacion_id": str(fabrication_order_id)}
        audit, replay = _reserve_operation(
            session, operation_id,
            "POST /ordenes-fabricacion/{id}/requerimientos-material/generar",
            actor, data,
        )
        if replay is not None:
            return replay
        order = session.get(ScmOrdenOperacion, fabrication_order_id)
        if order is None or order.tipo != "FABRICACION" or order.fabricacion is None:
            raise ScmServiceError("OF_NOT_FOUND", "La orden de fabricacion no existe.", status_code=404)
        if order.estado not in ("LIBERADA", "PROGRAMADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "OF_NOT_RELEASED", "La OF debe estar liberada antes de requerir materiales.",
                status_code=409,
            )
        generated = []
        for run in order.fabricacion.corridas:
            existing = session.scalar(select(ScmRequerimientoMaterial.id).where(
                ScmRequerimientoMaterial.corrida_fabricacion_id == run.id
            ))
            if existing is not None:
                raise ScmServiceError(
                    "MATERIAL_REQUIREMENTS_ALREADY_EXIST",
                    "La corrida ya tiene requerimientos congelados.", status_code=409,
                    details={"corrida_id": str(run.id)},
                )
            recipe = session.get(RecetaColorMaestra, run.receta_revision_id)
            if recipe is None or recipe.estado != "APROBADA":
                raise ScmServiceError(
                    "APPROVED_RECIPE_REQUIRED",
                    "Cada corrida requiere una receta aprobada para calcular materiales.",
                    status_code=422, details={"corrida_id": str(run.id)},
                )
            output_kg = sum(
                (Decimal(value.kg_estandar_objetivo or 0) for value in run.salidas),
                Decimal("0"),
            )
            runner_kg = (
                Decimal(run.ciclos_objetivo or 0)
                * Decimal(order.fabricacion.snapshot_peso_colada_gr or 0)
                / Decimal("1000")
            )
            resin_base_kg = output_kg + runner_kg
            if resin_base_kg <= 0:
                raise ScmServiceError(
                    "INVALID_MATERIAL_BASE", "La corrida no produce una base calculable.",
                    status_code=422, details={"corrida_id": str(run.id)},
                )
            virgin_kg = sum((
                resin_base_kg * Decimal(line.cantidad)
                for line in recipe.lineas
                if line.tipo_componente == "MATERIA_PRIMA"
                and line.material.materia_prima is not None
                and str(line.material.materia_prima.tipo or "").upper() == "VIRGEN"
            ), Decimal("0"))
            for line in recipe.lineas:
                if line.tipo_componente == "MATERIA_PRIMA":
                    quantity = resin_base_kg * Decimal(line.cantidad)
                    formula = "base_resina_kg × fraccion"
                else:
                    if virgin_kg <= 0:
                        raise ScmServiceError(
                            "VIRGIN_BASE_REQUIRED",
                            "La receta dosifica colorantes/aditivos por kg virgen, pero no contiene base virgen.",
                            status_code=422, details={"corrida_id": str(run.id)},
                        )
                    quantity = virgin_kg * Decimal(line.cantidad) / Decimal(line.base_kg) / Decimal("1000")
                    formula = "kg_virgen × gramos_dosis / base_kg / 1000"
                quantity = quantity.quantize(QTY, rounding=ROUND_HALF_UP)
                if quantity <= 0:
                    raise ScmServiceError(
                        "MATERIAL_REQUIREMENT_BELOW_SCALE",
                        "Un componente queda por debajo de 0.001 kg.", status_code=422,
                        details={"material_id": line.material_id},
                    )
                requirement = ScmRequerimientoMaterial(
                    corrida_fabricacion_id=run.id,
                    material_id=line.material_id,
                    tipo_componente=line.tipo_componente,
                    cantidad_plan_kg=quantity,
                    receta_revision_id=recipe.id,
                    calculo_snapshot_json={
                        "formula": formula,
                        "salidas_kg": _kg(output_kg),
                        "runner_kg": _kg(runner_kg),
                        "base_resina_kg": _kg(resin_base_kg),
                        "base_virgen_kg_calculada": _kg(virgin_kg),
                        "cantidad_receta": format(Decimal(line.cantidad), "f"),
                        "base_dosis_kg": format(Decimal(line.base_kg), "f") if line.base_kg else None,
                    },
                    created_by_id=actor.id,
                )
                session.add(requirement)
                generated.append(requirement)
        session.flush()
        response = {"items": [_serialize_requirement(value) for value in generated]}
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="ORDEN_FABRICACION", aggregate_id=str(order.id),
            tipo="MATERIAL_REQUIREMENTS_GENERATED", actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor), after_json=response,
            operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def reserve_run_materials(session, *, actor_id, operation_id, run_id):
    try:
        actor = load_actor(session, actor_id, capability="MATERIAL_RESERVAR")
        data = {"corrida_fabricacion_id": str(run_id)}
        audit, replay = _reserve_operation(
            session, operation_id, "POST /corridas-fabricacion/{id}/materiales/reservar",
            actor, data,
        )
        if replay is not None:
            return replay
        run = _load_run(session, run_id)
        requirements = session.scalars(
            select(ScmRequerimientoMaterial)
            .where(ScmRequerimientoMaterial.corrida_fabricacion_id == run.id)
            .with_for_update()
        ).all()
        if not requirements:
            raise ScmServiceError(
                "MATERIAL_REQUIREMENTS_REQUIRED",
                "Primero genere los requerimientos de la corrida.", status_code=409,
            )
        if any(value.reservas for value in requirements):
            raise ScmServiceError(
                "MATERIALS_ALREADY_RESERVED", "La corrida ya tiene reservas.", status_code=409,
            )
        shortages = []
        allocations = []
        for requirement in requirements:
            pending = Decimal(requirement.cantidad_plan_kg)
            balances = session.scalars(
                select(ScmSaldoMaterialInventario)
                .where(ScmSaldoMaterialInventario.material_id == requirement.material_id)
                .order_by(ScmSaldoMaterialInventario.updated_at, ScmSaldoMaterialInventario.id)
                .with_for_update()
            ).all()
            candidates = []
            for balance in balances:
                take = min(pending, max(_available(balance), Decimal("0")))
                if take > 0:
                    candidates.append((balance, take))
                    pending -= take
                if pending <= 0:
                    break
            if pending > 0:
                shortages.append({
                    "material_id": requirement.material_id,
                    "codigo": requirement.material.codigo,
                    "faltante_kg": _kg(pending),
                })
            allocations.append((requirement, candidates))
        if shortages:
            raise ScmServiceError(
                "INSUFFICIENT_MATERIAL_STOCK",
                "No hay saldo libre suficiente para reservar la corrida.",
                status_code=409, details={"faltantes": shortages},
            )
        for requirement, candidates in allocations:
            for balance, quantity in candidates:
                balance.cantidad_reservada_kg = Decimal(balance.cantidad_reservada_kg) + quantity
                balance.version += 1
                reservation = ScmReservaMaterial(
                    saldo_material_id=balance.id,
                    cantidad_kg=quantity, created_by_id=actor.id,
                )
                requirement.reservas.append(reservation)
                session.add(reservation)
        session.flush()
        response = _run_payload(run, requirements)
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="CORRIDA_FABRICACION", aggregate_id=str(run.id),
            tipo="MATERIALS_RESERVED", actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor), after_json=response,
            operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _location(session, code, name):
    item = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == code
    ).with_for_update())
    if item is None:
        item = ScmUbicacionInventario(
            codigo=code, nombre=name,
            clases_articulo_json=["MATERIA_PRIMA", "COLORANTE"],
        )
        session.add(item)
        session.flush()
    return item


def emit_reserved_material(session, *, actor_id, operation_id, reservation_id, data):
    try:
        reject_unknown_fields(data, allowed={"cantidad_kg", "motivo", "ubicacion_destino_codigo", "ubicacion_destino_nombre"})
        actor = load_actor(session, actor_id, capability="MATERIAL_EMITIR")
        quantity = _positive_quantity(data.get("cantidad_kg"))
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        destination_code = str(data.get("ubicacion_destino_codigo") or "PREPARACION_PRODUCCION").strip().upper()
        destination_name = str(data.get("ubicacion_destino_nombre") or "Preparacion de produccion").strip()
        request_data = {**data, "reserva_id": str(reservation_id), "cantidad_kg": _kg(quantity), "ubicacion_destino_codigo": destination_code}
        audit, replay = _reserve_operation(
            session, operation_id, "POST /reservas-material/{id}/emitir", actor, request_data,
        )
        if replay is not None:
            return replay
        reservation = session.scalar(select(ScmReservaMaterial).where(
            ScmReservaMaterial.id == reservation_id
        ).with_for_update())
        if reservation is None:
            raise ScmServiceError("MATERIAL_RESERVATION_NOT_FOUND", "La reserva no existe.", status_code=404)
        if reservation.estado != "ACTIVA" or Decimal(reservation.emitida_neta_kg) + quantity > Decimal(reservation.cantidad_kg):
            raise ScmServiceError("MATERIAL_EMISSION_EXCEEDS_RESERVATION", "La emision excede el saldo reservado.", status_code=409)
        source = session.scalar(select(ScmSaldoMaterialInventario).where(
            ScmSaldoMaterialInventario.id == reservation.saldo_material_id
        ).with_for_update())
        if Decimal(source.cantidad_fisica_kg) < quantity or Decimal(source.cantidad_reservada_kg) < quantity:
            raise ScmServiceError("MATERIAL_SOURCE_INCONSISTENT", "El saldo origen ya no cubre la emision.", status_code=409)
        location = _location(session, destination_code, destination_name)
        destination = session.scalar(select(ScmSaldoMaterialInventario).where(
            ScmSaldoMaterialInventario.material_id == source.material_id,
            ScmSaldoMaterialInventario.ubicacion_id == location.id,
        ).with_for_update())
        if destination is None:
            destination = ScmSaldoMaterialInventario(material_id=source.material_id, ubicacion_id=location.id)
            session.add(destination)
            session.flush()
        source.cantidad_fisica_kg = Decimal(source.cantidad_fisica_kg) - quantity
        source.cantidad_reservada_kg = Decimal(source.cantidad_reservada_kg) - quantity
        source.version += 1
        destination.cantidad_fisica_kg = Decimal(destination.cantidad_fisica_kg) + quantity
        destination.cantidad_reservada_kg = Decimal(destination.cantidad_reservada_kg) + quantity
        destination.version += 1
        reservation.emitida_neta_kg = Decimal(reservation.emitida_neta_kg) + quantity
        emission = ScmEmisionMaterial(
            reserva_id=reservation.id, saldo_destino_id=destination.id,
            cantidad_kg=quantity, motivo=reason, actor_id=actor.id,
            operation_id=operation_id,
        )
        session.add(emission)
        session.flush()
        for balance, delta, suffix in ((source, -quantity, "SALIDA"), (destination, quantity, "ENTRADA")):
            session.add(ScmMovimientoMaterialInventario(
                saldo_id=balance.id, tipo="EMISION", cantidad_delta_kg=delta,
                saldo_fisico_resultante_kg=balance.cantidad_fisica_kg,
                motivo=reason, referencia_tipo="EMISION_MATERIAL",
                referencia_id=str(emission.id), actor_id=actor.id,
                operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:EMISION:{suffix}"),
            ))
        session.flush()
        response = _serialize_emission(emission)
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="RESERVA_MATERIAL", aggregate_id=str(reservation.id),
            tipo="MATERIAL_EMITTED", actor_id=actor.id, actor_snapshot=actor_snapshot(actor),
            motivo=reason, after_json=response, operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def return_emitted_material(session, *, actor_id, operation_id, emission_id, data):
    try:
        reject_unknown_fields(data, allowed={"cantidad_kg", "motivo"})
        actor = load_actor(session, actor_id, capability="MATERIAL_DEVOLVER")
        quantity = _positive_quantity(data.get("cantidad_kg"))
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {**data, "emision_id": str(emission_id), "cantidad_kg": _kg(quantity)}
        audit, replay = _reserve_operation(
            session, operation_id, "POST /emisiones-material/{id}/devolver", actor, request_data,
        )
        if replay is not None:
            return replay
        emission = session.scalar(select(ScmEmisionMaterial).where(
            ScmEmisionMaterial.id == emission_id
        ).with_for_update())
        if emission is None:
            raise ScmServiceError("MATERIAL_EMISSION_NOT_FOUND", "La emision no existe.", status_code=404)
        if (
            Decimal(emission.cantidad_devuelta_kg)
            + Decimal(emission.cantidad_consumida_kg)
            + quantity
            > Decimal(emission.cantidad_kg)
        ):
            raise ScmServiceError("MATERIAL_RETURN_EXCEEDS_EMISSION", "La devolucion excede la emision.", status_code=409)
        reservation = session.scalar(select(ScmReservaMaterial).where(
            ScmReservaMaterial.id == emission.reserva_id
        ).with_for_update())
        source = session.scalar(select(ScmSaldoMaterialInventario).where(
            ScmSaldoMaterialInventario.id == reservation.saldo_material_id
        ).with_for_update())
        preparation = session.scalar(select(ScmSaldoMaterialInventario).where(
            ScmSaldoMaterialInventario.id == emission.saldo_destino_id
        ).with_for_update())
        if Decimal(preparation.cantidad_fisica_kg) < quantity or Decimal(preparation.cantidad_reservada_kg) < quantity:
            raise ScmServiceError("MATERIAL_RETURN_SOURCE_INCONSISTENT", "Preparacion ya no conserva el saldo por devolver.", status_code=409)
        preparation.cantidad_fisica_kg = Decimal(preparation.cantidad_fisica_kg) - quantity
        preparation.cantidad_reservada_kg = Decimal(preparation.cantidad_reservada_kg) - quantity
        preparation.version += 1
        source.cantidad_fisica_kg = Decimal(source.cantidad_fisica_kg) + quantity
        source.cantidad_reservada_kg = Decimal(source.cantidad_reservada_kg) + quantity
        source.version += 1
        reservation.emitida_neta_kg = Decimal(reservation.emitida_neta_kg) - quantity
        emission.cantidad_devuelta_kg = Decimal(emission.cantidad_devuelta_kg) + quantity
        returned = ScmDevolucionMaterial(
            emision_id=emission.id, cantidad_kg=quantity, motivo=reason,
            actor_id=actor.id, operation_id=operation_id,
        )
        session.add(returned)
        session.flush()
        for balance, delta, suffix in ((preparation, -quantity, "SALIDA"), (source, quantity, "ENTRADA")):
            session.add(ScmMovimientoMaterialInventario(
                saldo_id=balance.id, tipo="DEVOLUCION", cantidad_delta_kg=delta,
                saldo_fisico_resultante_kg=balance.cantidad_fisica_kg,
                motivo=reason, referencia_tipo="DEVOLUCION_MATERIAL",
                referencia_id=str(returned.id), actor_id=actor.id,
                operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:DEVOLUCION:{suffix}"),
            ))
        session.flush()
        response = _serialize_emission(emission)
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="EMISION_MATERIAL", aggregate_id=str(emission.id),
            tipo="MATERIAL_RETURNED", actor_id=actor.id, actor_snapshot=actor_snapshot(actor),
            motivo=reason, after_json=response, operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def confirm_premix(session, *, actor_id, operation_id, run_id, data):
    """Consume todos los saldos emitidos separables y crea el WIP de tolva."""
    try:
        reject_unknown_fields(data, allowed={"motivo", "genealogia_tipo", "ubicacion_codigo"})
        actor = load_actor(session, actor_id, capability="MATERIAL_PREMEZCLA_CONFIRMAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        genealogy = str(data.get("genealogia_tipo") or "").strip().upper()
        if genealogy not in ("EXACTA", "CONJUNTO_CANDIDATOS"):
            raise ScmServiceError(
                "INVALID_PREMIX_GENEALOGY",
                "Debe declarar si la genealogia es exacta o un conjunto de candidatos.",
                status_code=422,
            )
        location_code = str(data.get("ubicacion_codigo") or "PREPARACION_PRODUCCION").strip().upper()
        request_data = {
            "corrida_fabricacion_id": str(run_id), "motivo": reason,
            "genealogia_tipo": genealogy, "ubicacion_codigo": location_code,
        }
        audit, replay = _reserve_operation(
            session, operation_id, "POST /corridas-fabricacion/{id}/premezclas",
            actor, request_data,
        )
        if replay is not None:
            return replay
        run = _load_run(session, run_id, lock=True)
        canonical_requirement = session.scalar(
            select(ScmRequerimientoMaterialPreparado)
            .where(
                ScmRequerimientoMaterialPreparado.corrida_fabricacion_id
                == run.id,
                ScmRequerimientoMaterialPreparado.estado != "CANCELADA",
            )
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        )
        canonical_assignment = session.scalar(
            select(ScmAsignacionRequerimientoPreparacion.id)
            .join(ScmRequerimientoMaterialPreparado)
            .where(
                ScmRequerimientoMaterialPreparado.corrida_fabricacion_id
                == run.id,
                ScmAsignacionRequerimientoPreparacion.estado.in_((
                    "PLANIFICADA", "COMPROMETIDA", "SATISFECHA",
                )),
            )
            .limit(1)
        )
        canonical_order = session.scalar(
            select(ScmOrdenPreparacionMaterial.id)
            .join(ScmAsignacionRequerimientoPreparacion)
            .join(ScmRequerimientoMaterialPreparado)
            .where(
                ScmRequerimientoMaterialPreparado.corrida_fabricacion_id
                == run.id,
                ScmOrdenPreparacionMaterial.estado.notin_((
                    "ANULADA", "CERRADA",
                )),
            )
            .limit(1)
        )
        if (
            canonical_requirement is not None
            or canonical_assignment is not None
            or canonical_order is not None
        ):
            raise ScmServiceError(
                "CANONICAL_PREPARED_MATERIAL_ALREADY_ACTIVE",
                "La corrida ya usa el flujo canonico OPM; no admite premezcla legacy.",
                status_code=409,
                details={
                    "requerimiento_id": (
                        str(canonical_requirement.id)
                        if canonical_requirement is not None else None
                    ),
                },
            )
        requirements = session.scalars(
            select(ScmRequerimientoMaterial)
            .where(ScmRequerimientoMaterial.corrida_fabricacion_id == run.id)
            .with_for_update()
        ).all()
        emissions = []
        available_by_requirement = {}
        for requirement in requirements:
            requirement_available = Decimal("0")
            for reservation in requirement.reservas:
                for emission in reservation.emisiones:
                    available = (
                        Decimal(emission.cantidad_kg)
                        - Decimal(emission.cantidad_devuelta_kg)
                        - Decimal(emission.cantidad_consumida_kg)
                    )
                    if available > 0:
                        emissions.append((reservation, emission, available))
                        requirement_available += available
            available_by_requirement[requirement.id] = requirement_available
        if not emissions:
            raise ScmServiceError(
                "PREMIX_INPUTS_REQUIRED",
                "No existen emisiones separables disponibles para formar la premezcla.",
                status_code=409,
            )
        incomplete = [
            requirement for requirement in requirements
            if available_by_requirement[requirement.id] <= 0
        ]
        if incomplete:
            raise ScmServiceError(
                "PREMIX_COMPONENTS_INCOMPLETE",
                "Todos los componentes de la receta deben estar emitidos antes de confirmar la premezcla.",
                status_code=409,
                details={"materiales_faltantes": [value.material.codigo for value in incomplete]},
            )
        ratios = [
            available_by_requirement[requirement.id] / Decimal(requirement.cantidad_plan_kg)
            for requirement in requirements
        ]
        if max(ratios) - min(ratios) > Decimal("0.010"):
            raise ScmServiceError(
                "PREMIX_COMPONENT_PROPORTION_MISMATCH",
                "Las cantidades emitidas no guardan la proporcion de la receta aprobada.",
                status_code=409,
            )
        sequence = len(run.corrida_premezclas) + 1
        premix = ScmLotePremezcla(
            codigo=f"LMP-{run.codigo}-{sequence:03d}",
            corrida_fabricacion_id=run.id, secuencia=sequence,
            cantidad_kg=sum((value[2] for value in emissions), Decimal("0")),
            genealogia_tipo=genealogy, ubicacion_codigo=location_code,
            motivo=reason, actor_id=actor.id, operation_id=operation_id,
        )
        session.add(premix)
        session.flush()
        for reservation, emission, quantity in emissions:
            preparation = session.scalar(select(ScmSaldoMaterialInventario).where(
                ScmSaldoMaterialInventario.id == emission.saldo_destino_id
            ).with_for_update())
            if (
                Decimal(preparation.cantidad_fisica_kg) < quantity
                or Decimal(preparation.cantidad_reservada_kg) < quantity
            ):
                raise ScmServiceError(
                    "PREMIX_INPUT_BALANCE_INCONSISTENT",
                    "Un input emitido ya no esta disponible en Preparacion.",
                    status_code=409, details={"emision_id": str(emission.id)},
                )
            preparation.cantidad_fisica_kg = Decimal(preparation.cantidad_fisica_kg) - quantity
            preparation.cantidad_reservada_kg = Decimal(preparation.cantidad_reservada_kg) - quantity
            preparation.version += 1
            emission.cantidad_consumida_kg = Decimal(emission.cantidad_consumida_kg) + quantity
            reservation.emitida_neta_kg = Decimal(reservation.emitida_neta_kg) - quantity
            reservation.cantidad_consumida_kg = Decimal(reservation.cantidad_consumida_kg) + quantity
            premix.inputs.append(ScmLotePremezclaInput(
                emision_id=emission.id, cantidad_kg=quantity,
            ))
            session.add(ScmMovimientoMaterialInventario(
                saldo_id=preparation.id, tipo="CONSUMO", cantidad_delta_kg=-quantity,
                saldo_fisico_resultante_kg=preparation.cantidad_fisica_kg,
                motivo=reason, referencia_tipo="LOTE_PREMEZCLA",
                referencia_id=str(premix.id), actor_id=actor.id,
                operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:PREMEZCLA:{emission.id}"),
            ))
        session.flush()
        response = _serialize_premix(premix)
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="CORRIDA_FABRICACION", aggregate_id=str(run.id),
            tipo="PREMIX_CONFIRMED", actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor), motivo=reason,
            after_json=response, operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
