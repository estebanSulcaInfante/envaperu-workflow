"""OPM, lotes y bolsas de material preparado para el piloto SCM."""

import base64
import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import selectinload

from app.models.receta_color import RecetaColorMaestra
from app.models.maquina import Maquina
from app.models.scm_auditoria import ScmEvento
from app.models.scm_inventory import (
    ScmMovimientoMaterialInventario,
    ScmSaldoMaterialInventario,
    ScmUbicacionInventario,
    ScmUnidadLogisticaInventario,
    ScmUsoUnidadLogisticaPreparacion,
)
from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
from app.models.scm_material_execution import (
    ScmEmisionMaterial,
    ScmLotePremezcla,
    ScmRequerimientoMaterial,
    ScmReservaMaterial,
)
from app.models.scm_ot import ScmTrabajoColor, ScmTrabajoOt
from app.models.scm_prepared_material import (
    ScmAportePreparacionMaterial,
    ScmAprobacionLecturaPesoPreparacion,
    ScmBolsaMaterialPreparado,
    ScmAsignacionRequerimientoPreparacion,
    ScmDecisionCalidadMaterialPreparado,
    ScmEmisionMaterialPreparado,
    ScmLoteMaterialPreparado,
    ScmLecturaPesoPreparacion,
    ScmMovimientoMaterialPreparado,
    ScmOrdenPreparacionMaterial,
    ScmRecepcionBolsaMaterialPreparado,
    ScmRequerimientoMaterialPreparado,
    ScmReservaMaterialPreparado,
    ScmSaldoMaterialPreparado,
    utc_now,
)
from app.models.scm_production_orders import ScmCorridaFabricacion, ScmOrdenOperacion
from app.services.scm_inventory_service import _reserve_operation
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    load_actor,
    load_actor_any,
    positive_kg,
    reject_unknown_fields,
    required_text,
)


QTY = Decimal("0.001")
ZERO = Decimal("0.000")
MANUAL_METHOD = "CONTINGENCIA_MANUAL"
STATION_METHOD = "BALANZA_ESTACION"


def _kg(value):
    return format(Decimal(value or 0).quantize(QTY), "f")


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _page_limit(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_PAGE_LIMIT", "limit debe ser un entero.", status_code=400
        ) from error
    if parsed < 1 or parsed > 100:
        raise ScmServiceError(
            "INVALID_PAGE_LIMIT", "limit debe estar entre 1 y 100.", status_code=400
        )
    return parsed


def _encode_cursor(created_at, row_id):
    raw = json.dumps({"created_at": _iso(created_at), "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(value):
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return datetime.fromisoformat(payload["created_at"]), UUID(payload["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ScmServiceError(
            "INVALID_CURSOR", "cursor no es valido.", status_code=400
        ) from error


def _encode_key_cursor(key, row_id):
    raw = json.dumps({"key": key, "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_key_cursor(value):
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        key = str(payload["key"])
        return key, UUID(payload["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ScmServiceError(
            "INVALID_CURSOR", "cursor no es valido.", status_code=400
        ) from error


def _json_hash(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _event(session, *, aggregate_type, aggregate_id, event_type, actor, operation,
           reason=None, after=None, before=None):
    session.add(ScmEvento(
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        motivo=reason,
        before_json=copy.deepcopy(before),
        after_json=copy.deepcopy(after),
        operation_id=operation.operation_id if operation is not None else None,
    ))


def _complete(session, operation, payload, *, status=200):
    operation.response_json = copy.deepcopy(payload)
    operation.estado_http = status
    session.commit()
    return payload


def _load_run(session, run_id, *, lock=False):
    statement = select(ScmCorridaFabricacion).where(
        ScmCorridaFabricacion.id == run_id
    )
    if lock:
        statement = statement.with_for_update(of=ScmCorridaFabricacion)
    run = session.scalar(statement)
    if run is None:
        raise ScmServiceError(
            "FABRICATION_RUN_NOT_FOUND", "La corrida no existe.", status_code=404
        )
    return run


def _calculate_run_composition(run, recipe):
    order = run.orden_fabricacion.orden_operacion
    output_kg = sum(
        (Decimal(value.kg_estandar_objetivo or 0) for value in run.salidas),
        ZERO,
    )
    runner_kg = (
        Decimal(run.ciclos_objetivo or 0)
        * Decimal(run.orden_fabricacion.snapshot_peso_colada_gr or 0)
        / Decimal("1000")
    )
    resin_base_kg = output_kg + runner_kg
    if resin_base_kg <= 0:
        raise ScmServiceError(
            "INVALID_MATERIAL_BASE",
            "La corrida no produce una base calculable.",
            status_code=422,
            details={"corrida_id": str(run.id), "orden_codigo": order.codigo},
        )
    virgin_kg = sum((
        resin_base_kg * Decimal(line.cantidad)
        for line in recipe.lineas
        if line.tipo_componente == "MATERIA_PRIMA"
        and line.material.materia_prima is not None
        and str(line.material.materia_prima.tipo or "").upper() == "VIRGEN"
    ), ZERO)
    components = []
    for line in sorted(recipe.lineas, key=lambda value: (value.orden, value.id or 0)):
        if line.tipo_componente == "MATERIA_PRIMA":
            quantity = resin_base_kg * Decimal(line.cantidad)
            formula = "base_resina_kg × fraccion"
        else:
            if virgin_kg <= 0:
                raise ScmServiceError(
                    "VIRGIN_BASE_REQUIRED",
                    "La receta dosifica componentes por kg virgen, pero no contiene base virgen.",
                    status_code=422,
                    details={"corrida_id": str(run.id)},
                )
            quantity = (
                virgin_kg * Decimal(line.cantidad)
                / Decimal(line.base_kg)
                / Decimal("1000")
            )
            formula = "kg_virgen × gramos_dosis / base_kg / 1000"
        quantity = quantity.quantize(QTY, rounding=ROUND_HALF_UP)
        if quantity <= 0:
            raise ScmServiceError(
                "MATERIAL_REQUIREMENT_BELOW_SCALE",
                "Un componente queda por debajo de 0.001 kg.",
                status_code=422,
                details={"material_id": line.material_id},
            )
        components.append({
            "material_id": line.material_id,
            "material_codigo": line.material.codigo,
            "material_nombre": line.material.nombre,
            "tipo_componente": line.tipo_componente,
            "cantidad_kg": _kg(quantity),
            "formula": formula,
            "cantidad_receta": format(Decimal(line.cantidad), "f"),
            "base_dosis_kg": (
                format(Decimal(line.base_kg), "f") if line.base_kg else None
            ),
        })
    total = sum((Decimal(value["cantidad_kg"]) for value in components), ZERO)
    snapshot = {
        "receta_revision_id": recipe.id,
        "receta_revision": recipe.revision,
        "receta_nombre": recipe.nombre_variante,
        "salidas_kg": _kg(output_kg),
        "runner_kg": _kg(runner_kg),
        "base_resina_kg": _kg(resin_base_kg),
        "base_virgen_kg_calculada": _kg(virgin_kg),
        "componentes": components,
    }
    # La compatibilidad describe la semantica inmutable de la revision, no las
    # cantidades escaladas/redondeadas de una corrida. Dos necesidades de 15 y
    # 25 kg de la misma receta deben poder compartir una OPM aun cuando una
    # dosificacion pequena redondee distinto a 0.001 kg.
    compatibility = {
        "receta_revision_id": recipe.id,
        "revision": recipe.revision,
        "componentes": [
            {
                "material_id": line.material_id,
                "tipo_componente": line.tipo_componente,
                "cantidad_formula": format(Decimal(line.cantidad), "f"),
                "base_kg": (
                    format(Decimal(line.base_kg), "f")
                    if line.base_kg is not None else None
                ),
                "unidad": line.unidad,
                "orden": line.orden,
            }
            for line in sorted(
                recipe.lineas, key=lambda value: (value.orden, value.id or 0)
            )
        ],
    }
    return total.quantize(QTY), snapshot, _json_hash(compatibility)


ACTIVE_ASSIGNMENT_STATES = {"PLANIFICADA", "COMPROMETIDA", "SATISFECHA"}


def _planned_quantity(requirement):
    return sum((
        (
            Decimal(value.cantidad_planificada_kg)
            if value.estado == "PLANIFICADA"
            else Decimal(value.cantidad_comprometida_kg)
        )
        for value in requirement.asignaciones
        if value.estado in ACTIVE_ASSIGNMENT_STATES
    ), ZERO)


def _covered_quantity(requirement):
    return sum((
        Decimal(value.cantidad_comprometida_kg)
        for value in requirement.asignaciones
        if value.estado in ACTIVE_ASSIGNMENT_STATES
    ), ZERO)


def _consumed_quantity(requirement):
    return sum((
        Decimal(value.cantidad_consumida_kg)
        for value in requirement.asignaciones
        if value.estado in ACTIVE_ASSIGNMENT_STATES
    ), ZERO)


def _work_color_payload(run):
    candidates = sorted(
        (
            value for value in (run.trabajos_color or [])
            if value.trabajo is not None and value.trabajo.estado != "ANULADO"
        ),
        key=lambda value: (
            _iso(value.trabajo.created_at) or "",
            str(value.trabajo_ot_id),
        ),
        reverse=True,
    )
    if not candidates:
        return None
    work = candidates[0].trabajo
    return {
        "id": str(work.id),
        "codigo": work.codigo,
        "estado": work.estado,
    }


def _serialize_prepared_requirement(item, *, detail=True, metrics=None):
    if metrics is None:
        covered = _covered_quantity(item)
        planned = _planned_quantity(item)
        consumed = _consumed_quantity(item)
        active = [
            value for value in item.asignaciones
            if value.estado in ACTIVE_ASSIGNMENT_STATES
        ]
        planned_by_source = {
            source: sum((
                (
                    Decimal(value.cantidad_planificada_kg)
                    if value.estado == "PLANIFICADA"
                    else Decimal(value.cantidad_comprometida_kg)
                )
                for value in active if value.tipo_fuente == source
            ), ZERO)
            for source in ("LOTE_PREPARADO_STOCK", "OPM_ESPERADA")
        }
        committed_by_source = {
            source: sum((
                Decimal(value.cantidad_comprometida_kg)
                for value in active if value.tipo_fuente == source
            ), ZERO)
            for source in ("LOTE_PREPARADO_STOCK", "OPM_ESPERADA")
        }
    else:
        planned_by_source = {
            "LOTE_PREPARADO_STOCK": Decimal(
                metrics.get("stock_planificado_kg") or 0
            ),
            "OPM_ESPERADA": Decimal(
                metrics.get("opm_planificada_kg") or 0
            ),
        }
        committed_by_source = {
            "LOTE_PREPARADO_STOCK": Decimal(
                metrics.get("stock_comprometido_kg") or 0
            ),
            "OPM_ESPERADA": Decimal(
                metrics.get("opm_comprometida_kg") or 0
            ),
        }
        planned = sum(planned_by_source.values(), ZERO)
        covered = sum(committed_by_source.values(), ZERO)
        consumed = Decimal(metrics.get("consumida_kg") or 0)
    required = Decimal(item.cantidad_requerida_kg)
    pending = max(required - covered, ZERO)
    payload = {
        "id": str(item.id),
        "corrida_fabricacion_id": str(item.corrida_fabricacion_id),
        "corrida": {
            "id": str(item.corrida.id),
            "codigo": item.corrida.codigo,
            "estado": item.corrida.estado,
            "orden_fabricacion": {
                "id": str(item.corrida.orden_fabricacion_id),
                "codigo": item.corrida.orden_fabricacion.orden_operacion.codigo,
            },
        },
        "trabajo_color": _work_color_payload(item.corrida),
        "receta_revision_id": item.receta_revision_id,
        "receta": {
            "revision_id": item.receta_revision_id,
            "nombre": item.receta_revision.nombre_variante,
            "revision": item.receta_revision.revision,
            "estado": item.receta_revision.estado,
        },
        "cantidad_requerida_kg": _kg(required),
        "planificada_kg": _kg(planned),
        "stock_planificado_kg": _kg(
            planned_by_source["LOTE_PREPARADO_STOCK"]
        ),
        "opm_planificada_kg": _kg(planned_by_source["OPM_ESPERADA"]),
        "cubierta_kg": _kg(covered),
        "stock_comprometido_kg": _kg(
            committed_by_source["LOTE_PREPARADO_STOCK"]
        ),
        "opm_comprometida_kg": _kg(
            committed_by_source["OPM_ESPERADA"]
        ),
        "consumida_kg": _kg(consumed),
        "pendiente_kg": _kg(pending),
        "pendiente_planificacion_kg": _kg(max(required - planned, ZERO)),
        "composicion_hash": item.composicion_hash,
        "estado": item.estado,
        "version": item.version,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }
    if detail:
        payload["composicion"] = copy.deepcopy(item.composicion_snapshot_json)
        payload["asignaciones"] = [
            _serialize_assignment(value) for value in item.asignaciones
        ]
    return payload


def _serialize_raw_requirement(item):
    reserved = sum((Decimal(value.cantidad_kg) for value in item.reservas), ZERO)
    emitted = sum((Decimal(value.emitida_neta_kg) for value in item.reservas), ZERO)
    consumed = sum((Decimal(value.cantidad_consumida_kg) for value in item.reservas), ZERO)
    return {
        "id": str(item.id),
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
        "cantidad_incorporada_kg": _kg(consumed),
        "reservas": [{
            "id": str(reservation.id),
            "cantidad_kg": _kg(reservation.cantidad_kg),
            "emitida_neta_kg": _kg(reservation.emitida_neta_kg),
            "cantidad_consumida_kg": _kg(reservation.cantidad_consumida_kg),
            "estado": reservation.estado,
            "ubicacion": reservation.saldo.ubicacion.to_dict(),
            "emisiones": [{
                "id": str(emission.id),
                "cantidad_kg": _kg(emission.cantidad_kg),
                "cantidad_devuelta_kg": _kg(emission.cantidad_devuelta_kg),
                "cantidad_consumida_kg": _kg(emission.cantidad_consumida_kg),
                "cantidad_disponible_kg": _kg(
                    Decimal(emission.cantidad_kg)
                    - Decimal(emission.cantidad_devuelta_kg)
                    - Decimal(emission.cantidad_consumida_kg)
                ),
                "destino": emission.saldo_destino.ubicacion.to_dict(),
            } for emission in reservation.emisiones],
        } for reservation in item.reservas],
    }


def _serialize_assignment(item):
    bag = item.bolsa
    payload = {
        "id": str(item.id),
        "requerimiento_id": str(item.requerimiento_id),
        "corrida_fabricacion_id": str(item.requerimiento.corrida_fabricacion_id),
        "corrida_codigo": item.requerimiento.corrida.codigo,
        "trabajo_color": _work_color_payload(item.requerimiento.corrida),
        "tipo_fuente": item.tipo_fuente,
        "orden_preparacion_id": (
            str(item.orden_preparacion_id) if item.orden_preparacion_id else None
        ),
        "lote_id": str(item.lote_id) if item.lote_id else None,
        "bolsa_id": str(item.bolsa_id) if item.bolsa_id else None,
        "cantidad_planificada_kg": _kg(item.cantidad_planificada_kg),
        "cantidad_comprometida_kg": _kg(item.cantidad_comprometida_kg),
        "cantidad_consumida_kg": _kg(item.cantidad_consumida_kg),
        "estado": item.estado,
        "motivo": item.motivo,
        "created_by_id": item.created_by_id,
        "released_by_id": item.released_by_id,
        "motivo_liberacion": item.motivo_liberacion,
        "created_at": _iso(item.created_at),
        "released_at": _iso(item.released_at),
        "updated_at": _iso(item.updated_at),
    }
    payload["bolsa"] = (
        {
            "id": str(bag.id),
            "codigo": bag.codigo,
            "lote_id": str(bag.lote_id),
            "lote_codigo": bag.lote.codigo if bag.lote is not None else None,
            "peso_neto_kg": _kg(bag.peso_neto_kg),
            "estado": bag.estado,
            "ubicacion": (
                {
                    "id": bag.ubicacion.id,
                    "codigo": bag.ubicacion.codigo,
                    "nombre": bag.ubicacion.nombre,
                }
                if bag.ubicacion is not None else None
            ),
        }
        if bag is not None else None
    )
    return payload


def _serialize_contribution(item):
    return {
        "id": str(item.id),
        "orden_preparacion_id": str(item.orden_preparacion_id),
        "emision_id": str(item.emision_id),
        "lectura_id": str(item.lectura_id),
        "peso_bruto_kg": _kg(item.peso_bruto_kg),
        "tara_kg": _kg(item.tara_kg),
        "peso_neto_kg": _kg(item.peso_neto_kg),
        "metodo": item.metodo,
        "evidencia_ref": item.evidencia_ref,
        "motivo": item.motivo,
        "estado": item.estado,
        "created_by_id": item.created_by_id,
        "confirmed_by_id": item.confirmed_by_id,
        "created_at": _iso(item.created_at),
        "confirmed_at": _iso(item.confirmed_at),
    }


def _serialize_reading(item):
    approval = item.aprobacion
    return {
        "id": str(item.id),
        "orden_preparacion_id": str(item.orden_preparacion_id),
        "asignacion_requerimiento_id": (
            str(item.asignacion_requerimiento_id)
            if item.asignacion_requerimiento_id else None
        ),
        "tipo_uso": item.tipo_uso,
        "metodo": item.metodo,
        "bruto_kg": _kg(item.peso_bruto_kg),
        "tara_kg": _kg(item.tara_kg),
        "neto_kg": _kg(item.peso_neto_kg),
        "motivo": item.motivo,
        "evidencia_ref": item.evidencia_ref,
        "estado": item.estado,
        "created_by_id": item.created_by_id,
        "invalidated_by_id": item.invalidated_by_id,
        "invalidated_at": _iso(item.invalidated_at),
        "invalidation_reason": item.invalidation_reason,
        "version": item.version,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "aprobacion": ({
            "id": str(approval.id),
            "actor_id": approval.actor_id,
            "lectura_version": approval.lectura_version,
            "bruto_kg": _kg(approval.peso_bruto_kg),
            "tara_kg": _kg(approval.tara_kg),
            "neto_kg": _kg(approval.peso_neto_kg),
            "motivo": approval.motivo,
            "created_at": _iso(approval.created_at),
        } if approval else None),
    }


def _serialize_balance(item):
    return {
        "id": str(item.id),
        "receta_revision_id": item.receta_revision_id,
        "ubicacion": item.ubicacion.to_dict(),
        "cantidad_fisica_kg": _kg(item.cantidad_fisica_kg),
        "cantidad_reservada_kg": _kg(item.cantidad_reservada_kg),
        "cantidad_no_disponible_kg": _kg(item.cantidad_no_disponible_kg),
        "cantidad_libre_kg": _kg(
            Decimal(item.cantidad_fisica_kg)
            - Decimal(item.cantidad_reservada_kg)
            - Decimal(item.cantidad_no_disponible_kg)
        ),
        "version": item.version,
        "updated_at": _iso(item.updated_at),
    }


def _serialize_bag(item):
    receipt = item.recepcion
    return {
        "id": str(item.id),
        "codigo": item.codigo,
        "qr_value": f"SCM:BMP:{item.id}",
        "orden_preparacion_id": str(item.orden_preparacion_id),
        "lote_id": str(item.lote_id) if item.lote_id else None,
        "asignacion_requerimiento_id": (
            str(item.asignacion_requerimiento_id)
            if item.asignacion_requerimiento_id else None
        ),
        "secuencia": item.secuencia,
        "peso_bruto_kg": _kg(item.peso_bruto_kg),
        "tara_kg": _kg(item.tara_kg),
        "peso_neto_kg": _kg(item.peso_neto_kg),
        "metodo": item.metodo,
        "evidencia_ref": item.evidencia_ref,
        "motivo": item.motivo,
        "estado": item.estado,
        "ubicacion": item.ubicacion.to_dict() if item.ubicacion else None,
        "created_by_id": item.created_by_id,
        "confirmed_by_id": item.confirmed_by_id,
        "created_at": _iso(item.created_at),
        "confirmed_at": _iso(item.confirmed_at),
        "recepcion": ({
            "id": str(receipt.id),
            "ubicacion": receipt.ubicacion.to_dict(),
            "actor_id": receipt.actor_id,
            "motivo": receipt.motivo,
            "created_at": _iso(receipt.created_at),
        } if receipt else None),
    }


def _serialize_lot(item, *, include_bags=True, summary_metrics=None):
    decision_counts = (
        {
            value: sum(
                1 for item_value in item.decisiones_calidad
                if item_value.decision == value
            )
            for value in ("LIBERAR", "BLOQUEAR", "RECHAZAR")
        }
        if summary_metrics is None
        else {
            value: int(summary_metrics.get(value, 0))
            for value in ("LIBERAR", "BLOQUEAR", "RECHAZAR")
        }
    )
    payload = {
        "id": str(item.id),
        "codigo": item.codigo,
        "orden_preparacion_id": str(item.orden_preparacion_id),
        "receta_revision_id": item.receta_revision_id,
        "receta": {
            "revision_id": item.receta_revision_id,
            "nombre": item.receta_revision.nombre_variante,
            "revision": item.receta_revision.revision,
            "estado": item.receta_revision.estado,
        },
        "cantidad_kg": _kg(item.cantidad_kg),
        "estado": item.estado,
        "created_by_id": item.created_by_id,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "cantidad_bolsas": (
            len(item.bolsas)
            if summary_metrics is None
            else int(summary_metrics.get("cantidad_bolsas", 0))
        ),
        "resumen_calidad": decision_counts,
    }
    if include_bags:
        payload["bolsas"] = [_serialize_bag(value) for value in item.bolsas]
        payload["decisiones_calidad"] = [{
            "id": str(value.id),
            "bolsa_id": str(value.bolsa_id),
            "decision": value.decision,
            "motivo": value.motivo,
            "actor_id": value.actor_id,
            "created_at": _iso(value.created_at),
        } for value in item.decisiones_calidad]
    return payload


def _serialize_opm(item, *, detail=True, summary_assignments=None):
    assignment_values = (
        item.asignaciones
        if summary_assignments is None else summary_assignments
    )
    requirement_ids = sorted({
        str(
            value.requerimiento_id
            if hasattr(value, "requerimiento_id")
            else value["requerimiento_id"]
        )
        for value in assignment_values
    })
    payload = {
        "id": str(item.id),
        "codigo": item.codigo,
        "receta_revision_id": item.receta_revision_id,
        "receta": {
            "revision_id": item.receta_revision_id,
            "nombre": item.receta_revision.nombre_variante,
            "revision": item.receta_revision.revision,
            "estado": item.receta_revision.estado,
        },
        "cantidad_objetivo_kg": _kg(item.cantidad_objetivo_kg),
        "estado": item.estado,
        "motivo": item.motivo,
        "perdida_kg": _kg(item.perdida_kg) if item.perdida_kg is not None else None,
        "muestra_kg": _kg(item.muestra_kg) if item.muestra_kg is not None else None,
        "remanente_equipo_kg": (
            _kg(item.remanente_equipo_kg)
            if item.remanente_equipo_kg is not None else None
        ),
        "created_by_id": item.created_by_id,
        "released_by_id": item.released_by_id,
        "started_by_id": item.started_by_id,
        "closed_by_id": item.closed_by_id,
        "created_at": _iso(item.created_at),
        "released_at": _iso(item.released_at),
        "started_at": _iso(item.started_at),
        "closed_at": _iso(item.closed_at),
        "updated_at": _iso(item.updated_at),
        "version": item.version,
        "cantidad_asignaciones": len(assignment_values),
        "requerimiento_ids": requirement_ids,
    }
    if detail:
        inputs = sum(
            (Decimal(value.peso_neto_kg) for value in item.aportes), ZERO
        )
        outputs = sum(
            (
                Decimal(value.peso_neto_kg)
                for value in item.lecturas
                if value.tipo_uso == "BOLSA_SALIDA"
                and value.estado in ("APROBADA", "UTILIZADA")
            ),
            ZERO,
        )
        declared = (
            Decimal(item.perdida_kg or 0)
            + Decimal(item.muestra_kg or 0)
            + Decimal(item.remanente_equipo_kg or 0)
        )
        payload.update({
            "asignaciones": [
                _serialize_assignment(value) for value in item.asignaciones
            ],
            "requerimientos_insumo": [
                _serialize_raw_requirement(value)
                for value in item.requerimientos_insumo
            ],
            "aportes": [_serialize_contribution(value) for value in item.aportes],
            "lecturas": [_serialize_reading(value) for value in item.lecturas],
            "bolsas": [_serialize_bag(value) for value in item.bolsas],
            "lote": _serialize_lot(item.lote, include_bags=False) if item.lote else None,
            "balance": {
                "entradas_incorporadas_kg": _kg(inputs),
                "salidas_bolsas_kg": _kg(outputs),
                "perdida_muestra_remanente_kg": _kg(declared),
                "diferencia_kg": _kg(inputs - outputs - declared),
                "conciliado": (inputs - outputs - declared).quantize(QTY) == ZERO,
            },
        })
    elif summary_assignments is not None:
        payload["asignaciones"] = summary_assignments
    return payload


def _refresh_requirement_state(requirement):
    required = Decimal(requirement.cantidad_requerida_kg)
    committed = _covered_quantity(requirement)
    consumed = _consumed_quantity(requirement)
    if consumed >= required:
        requirement.estado = "SATISFECHA"
    elif committed >= required:
        requirement.estado = "CUBIERTA"
    elif committed > 0:
        requirement.estado = "CUBIERTA_PARCIAL"
    else:
        requirement.estado = "PENDIENTE"


def _compatible_prepared_stock_statement(requirement, *, max_quantity=None):
    free_quantity = (
        ScmSaldoMaterialPreparado.cantidad_fisica_kg
        - ScmSaldoMaterialPreparado.cantidad_reservada_kg
        - ScmSaldoMaterialPreparado.cantidad_no_disponible_kg
    )
    statement = (
        select(ScmBolsaMaterialPreparado)
        .join(ScmLoteMaterialPreparado)
        .join(
            ScmOrdenPreparacionMaterial,
            ScmOrdenPreparacionMaterial.id
            == ScmBolsaMaterialPreparado.orden_preparacion_id,
        )
        .join(
            ScmUbicacionInventario,
            ScmUbicacionInventario.id
            == ScmBolsaMaterialPreparado.ubicacion_id,
        )
        .join(
            ScmAlmacen,
            ScmAlmacen.id == ScmUbicacionInventario.almacen_id,
        )
        .join(
            ScmSaldoMaterialPreparado,
            and_(
                ScmSaldoMaterialPreparado.receta_revision_id
                == ScmLoteMaterialPreparado.receta_revision_id,
                ScmSaldoMaterialPreparado.ubicacion_id
                == ScmBolsaMaterialPreparado.ubicacion_id,
            ),
        )
        .where(
            ScmLoteMaterialPreparado.receta_revision_id
            == requirement.receta_revision_id,
            ScmOrdenPreparacionMaterial.composicion_hash
            == requirement.composicion_hash,
            ScmBolsaMaterialPreparado.estado == "DISPONIBLE",
            ScmBolsaMaterialPreparado.ubicacion_id.is_not(None),
            ScmUbicacionInventario.activo.is_(True),
            ScmUbicacionInventario.permite_saldo_libre.is_(True),
            ScmAlmacen.activo.is_(True),
            free_quantity >= ScmBolsaMaterialPreparado.peso_neto_kg,
            ~select(ScmAsignacionRequerimientoPreparacion.id)
            .where(
                or_(
                    ScmAsignacionRequerimientoPreparacion.bolsa_id
                    == ScmBolsaMaterialPreparado.id,
                    ScmAsignacionRequerimientoPreparacion.id
                    == ScmBolsaMaterialPreparado.asignacion_requerimiento_id,
                ),
                ScmAsignacionRequerimientoPreparacion.estado.in_(
                    tuple(ACTIVE_ASSIGNMENT_STATES)
                ),
            )
            .exists(),
        )
    )
    if max_quantity is not None:
        statement = statement.where(
            ScmBolsaMaterialPreparado.peso_neto_kg <= max_quantity
        )
    return statement


def _serialize_compatible_stock_bag(item):
    return {
        "id": str(item.id),
        "codigo": item.codigo,
        "lote": {
            "id": str(item.lote_id),
            "codigo": item.lote.codigo,
        },
        "peso_neto_kg": _kg(item.peso_neto_kg),
        "estado": item.estado,
        "ubicacion": item.ubicacion.to_dict(),
        "created_at": _iso(item.created_at),
        "version": item.version,
    }


def assign_prepared_stock_to_requirement(
    session, *, actor_id, operation_id, requirement_id, data,
):
    """Reserva bolsas completas solo tras una decision explicita autorizada."""
    try:
        reject_unknown_fields(data, allowed={"bolsa_ids", "motivo", "version"})
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_RESERVAR"
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        raw_ids = data.get("bolsa_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ScmServiceError(
                "PREPARED_BAGS_REQUIRED",
                "bolsa_ids debe contener al menos una bolsa completa.",
                status_code=400,
            )
        try:
            bag_ids = [UUID(str(value)) for value in raw_ids]
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "INVALID_PREPARED_BAG_ID",
                "Cada bolsa_id debe ser un UUID valido.",
                status_code=400,
            ) from error
        if len(set(bag_ids)) != len(bag_ids):
            raise ScmServiceError(
                "DUPLICATE_PREPARED_BAG_ID",
                "Una bolsa no puede repetirse en la misma asignacion.",
                status_code=400,
            )
        request_data = {
            "requerimiento_id": str(requirement_id),
            "bolsa_ids": sorted(str(value) for value in bag_ids),
            "motivo": reason,
            "version": data.get("version"),
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /requerimientos-preparacion/{id}/asignaciones-stock",
            actor, request_data,
        )
        if replay is not None:
            return replay
        requirement = session.scalar(
            select(ScmRequerimientoMaterialPreparado)
            .where(ScmRequerimientoMaterialPreparado.id == requirement_id)
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        )
        if requirement is None:
            raise ScmServiceError(
                "PREPARED_REQUIREMENT_NOT_FOUND",
                "El requerimiento de material preparado no existe.",
                status_code=404,
            )
        _require_version(requirement, data.get("version"))
        if requirement.estado in ("SATISFECHA", "CANCELADA"):
            raise ScmServiceError(
                "PREPARED_REQUIREMENT_NOT_COVERABLE",
                "El requerimiento ya no admite asignaciones de stock.",
                status_code=409,
            )

        # Orden global de locks: ubicaciones, saldos y finalmente bolsas.
        candidate_rows = session.execute(
            select(
                ScmBolsaMaterialPreparado.id,
                ScmBolsaMaterialPreparado.ubicacion_id,
            ).where(ScmBolsaMaterialPreparado.id.in_(bag_ids))
        ).all()
        if len(candidate_rows) != len(bag_ids):
            raise ScmServiceError(
                "PREPARED_MATERIAL_BAG_NOT_FOUND",
                "Una o mas bolsas no existen.",
                status_code=404,
            )
        raw_location_ids = {value.ubicacion_id for value in candidate_rows}
        if None in raw_location_ids:
            raise ScmServiceError(
                "PREPARED_BAG_NOT_IN_STOCK",
                "La bolsa debe estar recibida en una ubicacion canonica.",
                status_code=409,
            )
        location_ids = sorted(raw_location_ids)
        session.scalars(
            select(ScmUbicacionInventario)
            .where(ScmUbicacionInventario.id.in_(location_ids))
            .order_by(ScmUbicacionInventario.id)
            .with_for_update(of=ScmUbicacionInventario)
        ).all()
        balances = session.scalars(
            select(ScmSaldoMaterialPreparado)
            .where(
                ScmSaldoMaterialPreparado.receta_revision_id
                == requirement.receta_revision_id,
                ScmSaldoMaterialPreparado.ubicacion_id.in_(location_ids),
            )
            .order_by(ScmSaldoMaterialPreparado.id)
            .with_for_update(of=ScmSaldoMaterialPreparado)
        ).all()
        balance_by_location = {value.ubicacion_id: value for value in balances}
        bags = session.scalars(
            select(ScmBolsaMaterialPreparado)
            .where(ScmBolsaMaterialPreparado.id.in_(bag_ids))
            .order_by(ScmBolsaMaterialPreparado.id)
            .with_for_update(of=ScmBolsaMaterialPreparado)
        ).unique().all()
        active_assignment = session.scalar(
            select(ScmAsignacionRequerimientoPreparacion.id)
            .where(
                ScmAsignacionRequerimientoPreparacion.bolsa_id.in_(bag_ids),
                ScmAsignacionRequerimientoPreparacion.estado.in_(
                    tuple(ACTIVE_ASSIGNMENT_STATES)
                ),
            )
            .limit(1)
        )
        if active_assignment is not None:
            raise ScmServiceError(
                "PREPARED_BAG_ALREADY_ASSIGNED",
                "Una bolsa seleccionada ya cubre otra necesidad activa.",
                status_code=409,
            )
        for bag in bags:
            if (
                bag.estado != "DISPONIBLE"
                or bag.lote.receta_revision_id != requirement.receta_revision_id
                or bag.orden.composicion_hash != requirement.composicion_hash
                or bag.ubicacion is None
                or bag.ubicacion.almacen_id is None
                or not bag.ubicacion.activo
                or not bag.ubicacion.permite_saldo_libre
                or bag.ubicacion.almacen is None
                or not bag.ubicacion.almacen.activo
            ):
                raise ScmServiceError(
                    "PREPARED_BAG_NOT_COMPATIBLE",
                    "La bolsa no esta disponible o no coincide con la revision exacta.",
                    status_code=409,
                    details={"bolsa_id": str(bag.id)},
                )
            balance = balance_by_location.get(bag.ubicacion_id)
            free = (
                Decimal(balance.cantidad_fisica_kg)
                - Decimal(balance.cantidad_reservada_kg)
                - Decimal(balance.cantidad_no_disponible_kg)
                if balance is not None else ZERO
            )
            if free < Decimal(bag.peso_neto_kg):
                raise ScmServiceError(
                    "PREPARED_BAG_BALANCE_NOT_AVAILABLE",
                    "El saldo libre ya no respalda la bolsa seleccionada.",
                    status_code=409,
                    details={"bolsa_id": str(bag.id)},
                )
        total = sum((Decimal(value.peso_neto_kg) for value in bags), ZERO)
        remaining = (
            Decimal(requirement.cantidad_requerida_kg)
            - _planned_quantity(requirement)
        ).quantize(QTY)
        if total > remaining:
            raise ScmServiceError(
                "PREPARED_STOCK_EXCEEDS_PENDING",
                "Las bolsas completas exceden el saldo pendiente de planificacion.",
                status_code=409,
                details={"pendiente_planificacion_kg": _kg(max(remaining, ZERO))},
            )
        assignments = []
        for bag in bags:
            quantity = Decimal(bag.peso_neto_kg)
            assignment = ScmAsignacionRequerimientoPreparacion(
                requerimiento=requirement,
                tipo_fuente="LOTE_PREPARADO_STOCK",
                lote_id=bag.lote_id,
                bolsa_id=bag.id,
                cantidad_planificada_kg=quantity,
                cantidad_comprometida_kg=quantity,
                cantidad_consumida_kg=ZERO,
                estado="COMPROMETIDA",
                motivo=reason,
                created_by_id=actor.id,
            )
            session.add(assignment)
            assignments.append(assignment)
        requirement.version += 1
        _refresh_requirement_state(requirement)
        session.flush()
        payload = {
            "requerimiento": _serialize_prepared_requirement(requirement),
            "asignaciones": [
                _serialize_assignment(value) for value in assignments
            ],
        }
        _event(
            session,
            aggregate_type="REQUERIMIENTO_MATERIAL_PREPARADO",
            aggregate_id=requirement.id,
            event_type="PREPARED_STOCK_ASSIGNED_TO_REQUIREMENT",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload, status=201)
    except Exception:
        session.rollback()
        raise


def release_prepared_stock_assignment(
    session, *, actor_id, operation_id, assignment_id, data,
):
    try:
        reject_unknown_fields(data, allowed={"motivo"})
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_RESERVAR"
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "asignacion_id": str(assignment_id),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /asignaciones-stock-material-preparado/{id}/liberar",
            actor,
            request_data,
        )
        if replay is not None:
            return replay
        preview = session.get(
            ScmAsignacionRequerimientoPreparacion, assignment_id
        )
        if preview is None or preview.tipo_fuente != "LOTE_PREPARADO_STOCK":
            raise ScmServiceError(
                "PREPARED_STOCK_ASSIGNMENT_NOT_FOUND",
                "La asignacion de stock preparado no existe.",
                status_code=404,
            )
        requirement = session.scalar(
            select(ScmRequerimientoMaterialPreparado)
            .where(
                ScmRequerimientoMaterialPreparado.id
                == preview.requerimiento_id
            )
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        )
        bag_preview = session.get(ScmBolsaMaterialPreparado, preview.bolsa_id)
        if bag_preview is None or bag_preview.ubicacion_id is None:
            raise ScmServiceError(
                "PREPARED_STOCK_ASSIGNMENT_INCONSISTENT",
                "La asignacion no conserva una bolsa en stock.",
                status_code=409,
            )
        bag = session.scalar(
            select(ScmBolsaMaterialPreparado)
            .where(ScmBolsaMaterialPreparado.id == preview.bolsa_id)
            .with_for_update(of=ScmBolsaMaterialPreparado)
        )
        assignment = session.scalar(
            select(ScmAsignacionRequerimientoPreparacion)
            .where(ScmAsignacionRequerimientoPreparacion.id == assignment_id)
            .with_for_update(of=ScmAsignacionRequerimientoPreparacion)
        )
        if (
            assignment.estado != "COMPROMETIDA"
            or Decimal(assignment.cantidad_consumida_kg) > 0
            or bag.estado != "DISPONIBLE"
            or any(value.estado == "ACTIVA" for value in bag.reservas)
        ):
            raise ScmServiceError(
                "PREPARED_STOCK_ASSIGNMENT_NOT_RELEASABLE",
                "La bolsa ya fue vinculada, emitida o consumida.",
                status_code=409,
            )
        quantity = Decimal(assignment.cantidad_comprometida_kg)
        assignment.cantidad_comprometida_kg = ZERO
        assignment.estado = "LIBERADA"
        assignment.released_by_id = actor.id
        assignment.motivo_liberacion = reason
        assignment.released_at = utc_now()
        requirement.version += 1
        _refresh_requirement_state(requirement)
        session.flush()
        payload = {
            "requerimiento": _serialize_prepared_requirement(requirement),
            "asignacion": _serialize_assignment(assignment),
            "bolsa": _serialize_bag(bag),
        }
        _event(
            session,
            aggregate_type="REQUERIMIENTO_MATERIAL_PREPARADO",
            aggregate_id=requirement.id,
            event_type="PREPARED_STOCK_ASSIGNMENT_RELEASED",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def generate_prepared_requirement(session, *, actor_id, operation_id, run_id):
    try:
        actor = load_actor(session, actor_id, capability="OPM_CREAR")
        request_data = {"corrida_fabricacion_id": str(run_id)}
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /requerimientos-preparacion/calcular",
            actor,
            request_data,
        )
        if replay is not None:
            return replay
        run = _load_run(session, run_id, lock=True)
        legacy_premix = session.scalar(
            select(ScmLotePremezcla)
            .where(
                ScmLotePremezcla.corrida_fabricacion_id == run.id,
                ScmLotePremezcla.estado != "ANULADO",
            )
            .order_by(ScmLotePremezcla.secuencia)
            .with_for_update(of=ScmLotePremezcla)
        )
        if legacy_premix is not None:
            raise ScmServiceError(
                "PREPARED_MATERIAL_LEGACY_MIGRATION_REQUIRED",
                "La corrida ya posee una premezcla legacy; requiere una decision de migracion.",
                status_code=409,
                details={
                    "lote_premezcla_id": str(legacy_premix.id),
                    "codigo": legacy_premix.codigo,
                    "estado": legacy_premix.estado,
                },
            )
        if run.estado not in ("LIBERADA", "EN_EJECUCION"):
            raise ScmServiceError(
                "FABRICATION_RUN_NOT_RELEASED",
                "La corrida debe estar liberada o en ejecucion.",
                status_code=409,
            )
        recipe = session.get(RecetaColorMaestra, run.receta_revision_id)
        if recipe is None or recipe.estado != "APROBADA":
            raise ScmServiceError(
                "APPROVED_RECIPE_REQUIRED",
                "La corrida requiere una revision de receta aprobada.",
                status_code=422,
            )
        quantity, snapshot, composition_hash = _calculate_run_composition(run, recipe)
        existing = session.scalar(select(ScmRequerimientoMaterialPreparado).where(
            ScmRequerimientoMaterialPreparado.corrida_fabricacion_id == run.id
        ).with_for_update())
        if existing is not None:
            unchanged = (
                existing.receta_revision_id == recipe.id
                and existing.composicion_hash == composition_hash
                and Decimal(existing.cantidad_requerida_kg) == quantity
            )
            if unchanged:
                payload = _serialize_prepared_requirement(existing)
                return _complete(session, operation, payload, status=200)
            if existing.asignaciones:
                raise ScmServiceError(
                    "PREPARED_REQUIREMENT_PLAN_CHANGED",
                    "La corrida cambio despues de ser cubierta; requiere conciliacion explicita.",
                    status_code=409,
                    details={"requerimiento_id": str(existing.id)},
                )
            existing.receta_revision_id = recipe.id
            existing.cantidad_requerida_kg = quantity
            existing.composicion_hash = composition_hash
            existing.composicion_snapshot_json = snapshot
            existing.estado = "PENDIENTE"
            existing.version += 1
            session.flush()
            payload = _serialize_prepared_requirement(existing)
            _event(
                session,
                aggregate_type="REQUERIMIENTO_MATERIAL_PREPARADO",
                aggregate_id=existing.id,
                event_type="PREPARED_MATERIAL_REQUIREMENT_RECALCULATED",
                actor=actor,
                operation=operation,
                after=payload,
            )
            return _complete(session, operation, payload, status=200)
        requirement = ScmRequerimientoMaterialPreparado(
            corrida_fabricacion_id=run.id,
            receta_revision_id=recipe.id,
            cantidad_requerida_kg=quantity,
            composicion_hash=composition_hash,
            composicion_snapshot_json=snapshot,
            estado="PENDIENTE",
            created_by_id=actor.id,
            operation_id=operation_id,
        )
        session.add(requirement)
        session.flush()
        payload = _serialize_prepared_requirement(requirement)
        _event(
            session,
            aggregate_type="REQUERIMIENTO_MATERIAL_PREPARADO",
            aggregate_id=requirement.id,
            event_type="PREPARED_MATERIAL_REQUIREMENT_GENERATED",
            actor=actor,
            operation=operation,
            after=payload,
        )
        return _complete(session, operation, payload, status=200)
    except Exception:
        session.rollback()
        raise


def create_preparation_order(session, *, actor_id, operation_id, data):
    try:
        reject_unknown_fields(data, allowed={"coberturas", "motivo"})
        actor = load_actor(session, actor_id, capability="OPM_CREAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        raw_coverages = data.get("coberturas")
        if not isinstance(raw_coverages, list) or not raw_coverages:
            raise ScmServiceError(
                "PREPARATION_COVERAGES_REQUIRED",
                "coberturas debe contener al menos un requerimiento.",
                status_code=400,
            )
        parsed = []
        seen = set()
        for index, value in enumerate(raw_coverages):
            if not isinstance(value, dict):
                raise ScmServiceError(
                    "INVALID_PREPARATION_COVERAGE",
                    "Cada cobertura debe ser un objeto.",
                    status_code=400,
                    details={"index": index},
                )
            reject_unknown_fields(value, allowed={"requerimiento_id", "cantidad_kg"})
            try:
                requirement_id = UUID(str(value.get("requerimiento_id")))
            except (TypeError, ValueError, AttributeError) as error:
                raise ScmServiceError(
                    "INVALID_PREPARED_REQUIREMENT_ID",
                    "requerimiento_id debe ser un UUID valido.",
                    status_code=400,
                    details={"index": index},
                ) from error
            if requirement_id in seen:
                raise ScmServiceError(
                    "DUPLICATE_PREPARATION_COVERAGE",
                    "Un requerimiento no puede repetirse en la misma OPM.",
                    status_code=400,
                    details={"requerimiento_id": str(requirement_id)},
                )
            seen.add(requirement_id)
            parsed.append((requirement_id, positive_kg(
                value.get("cantidad_kg"), field="cantidad_kg"
            )))
        request_data = {
            "motivo": reason,
            "coberturas": [
                {"requerimiento_id": str(value[0]), "cantidad_kg": _kg(value[1])}
                for value in parsed
            ],
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /ordenes-preparacion-material/proponer",
            actor,
            request_data,
        )
        if replay is not None:
            return replay
        ids = sorted((value[0] for value in parsed), key=str)
        requirements = session.scalars(
            select(ScmRequerimientoMaterialPreparado)
            .where(ScmRequerimientoMaterialPreparado.id.in_(ids))
            .order_by(ScmRequerimientoMaterialPreparado.id)
            .with_for_update()
        ).unique().all()
        if len(requirements) != len(ids):
            found = {value.id for value in requirements}
            raise ScmServiceError(
                "PREPARED_REQUIREMENT_NOT_FOUND",
                "Uno o mas requerimientos no existen.",
                status_code=404,
                details={"ids": [str(value) for value in ids if value not in found]},
            )
        by_id = {value.id: value for value in requirements}
        recipe_ids = {value.receta_revision_id for value in requirements}
        hashes = {value.composicion_hash for value in requirements}
        if len(recipe_ids) != 1 or len(hashes) != 1:
            raise ScmServiceError(
                "OPM_INCOMPATIBLE_REQUIREMENTS",
                "Una OPM solo puede consolidar la misma revision exacta de receta.",
                status_code=409,
                details={"receta_revision_ids": sorted(recipe_ids)},
            )
        recipe = session.get(RecetaColorMaestra, next(iter(recipe_ids)))
        if recipe is None or recipe.estado != "APROBADA":
            raise ScmServiceError(
                "APPROVED_RECIPE_REQUIRED",
                "La OPM requiere una revision de receta aprobada.",
                status_code=422,
            )
        requested_by_id = {value[0]: value[1] for value in parsed}
        for requirement in requirements:
            if requirement.estado in ("SATISFECHA", "CANCELADA"):
                raise ScmServiceError(
                    "PREPARED_REQUIREMENT_NOT_COVERABLE",
                    "El requerimiento ya no admite cobertura.",
                    status_code=409,
                    details={"requerimiento_id": str(requirement.id)},
                )
            remaining = (
                Decimal(requirement.cantidad_requerida_kg)
                - _planned_quantity(requirement)
            ).quantize(QTY)
            if requested_by_id[requirement.id] > remaining:
                code = (
                    "PREPARED_REQUIREMENT_ALREADY_COVERED"
                    if remaining <= 0
                    else "PREPARATION_COVERAGE_EXCEEDS_PENDING"
                )
                raise ScmServiceError(
                    code,
                    "La cobertura excede el saldo pendiente del requerimiento.",
                    status_code=409,
                    details={
                        "requerimiento_id": str(requirement.id),
                        "pendiente_kg": _kg(max(remaining, ZERO)),
                    },
                )
        opm_by_id = requested_by_id
        stock_decisions = []
        for requirement in requirements:
            planning_pending = (
                Decimal(requirement.cantidad_requerida_kg)
                - _planned_quantity(requirement)
            ).quantize(QTY)
            candidates = session.scalars(
                _compatible_prepared_stock_statement(
                    requirement,
                    max_quantity=planning_pending,
                )
                .order_by(
                    ScmBolsaMaterialPreparado.created_at,
                    ScmBolsaMaterialPreparado.id,
                )
                .limit(5)
            ).unique().all()
            if candidates:
                stock_decisions.append({
                    "requerimiento_id": str(requirement.id),
                    "pendiente_planificacion_kg": _kg(planning_pending),
                    "bolsas_compatibles": [
                        _serialize_compatible_stock_bag(value)
                        for value in candidates
                    ],
                })
        if stock_decisions:
            raise ScmServiceError(
                "PREPARED_STOCK_DECISION_REQUIRED",
                "Existe stock preparado compatible; asigna cada bolsa util o "
                "regulariza su calidad/ubicacion antes de proponer nueva preparacion.",
                status_code=409,
                details={"requerimientos": stock_decisions},
            )
        order_id = uuid4()
        order = ScmOrdenPreparacionMaterial(
            id=order_id,
            codigo=f"OPM-{str(order_id)[:8].upper()}",
            receta_revision_id=recipe.id,
            composicion_hash=next(iter(hashes)),
            cantidad_objetivo_kg=sum(opm_by_id.values(), ZERO),
            estado="BORRADOR",
            motivo=reason,
            created_by_id=actor.id,
            operation_id=operation_id,
        )
        session.add(order)
        session.flush()
        for requirement_id, _requested_quantity in parsed:
            quantity = opm_by_id[requirement_id]
            if quantity <= 0:
                continue
            requirement = by_id[requirement_id]
            planned_after = _planned_quantity(requirement) + quantity
            assignment = ScmAsignacionRequerimientoPreparacion(
                orden=order,
                requerimiento=requirement,
                tipo_fuente="OPM_ESPERADA",
                cantidad_planificada_kg=quantity,
                cantidad_comprometida_kg=ZERO,
                cantidad_consumida_kg=ZERO,
                estado="PLANIFICADA",
                motivo=reason,
                created_by_id=actor.id,
            )
            session.add(assignment)
            requirement.estado = (
                "PENDIENTE"
                if planned_after < Decimal(requirement.cantidad_requerida_kg)
                else requirement.estado
            )
            requirement.version += 1
        session.flush()
        payload = _serialize_opm(order)
        _event(
            session,
            aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id,
            event_type="PREPARATION_ORDER_CREATED",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload, status=201)
    except Exception:
        session.rollback()
        raise


def _load_opm(session, order_id, *, lock=False):
    statement = select(ScmOrdenPreparacionMaterial).where(
        ScmOrdenPreparacionMaterial.id == order_id
    )
    if lock:
        statement = statement.with_for_update(of=ScmOrdenPreparacionMaterial)
    order = session.scalar(statement)
    if order is None:
        raise ScmServiceError(
            "PREPARATION_ORDER_NOT_FOUND", "La OPM no existe.", status_code=404
        )
    return order


def resolve_preparation_source_unit(session, *, actor_id, order_id, qr_value):
    """Resuelve una bolsa origen sin reservarla ni consumirla."""
    load_actor(session, actor_id, capability="OPM_EJECUTAR")
    order = _load_opm(session, order_id)
    if order.estado != "EN_PREPARACION":
        raise ScmServiceError(
            "OPM_NOT_IN_PREPARATION",
            "La OPM no está en preparación.", status_code=409,
        )
    qr = required_text(qr_value, field="qr_value", max_length=100)
    unit = session.scalar(select(ScmUnidadLogisticaInventario).where(
        ScmUnidadLogisticaInventario.qr_value == qr
    ))
    if unit is None:
        raise ScmServiceError(
            "OPM_SOURCE_UNIT_NOT_FOUND", "La bolsa origen no existe.", status_code=404,
        )
    if unit.estado != "DISPONIBLE":
        raise ScmServiceError(
            "OPM_SOURCE_UNIT_NOT_AVAILABLE",
            f"La bolsa {unit.codigo} no está disponible.", status_code=409,
        )
    required_material_ids = {item.material_id for item in order.requerimientos_insumo}
    if unit.material_scm_id not in required_material_ids:
        raise ScmServiceError(
            "OPM_SOURCE_MATERIAL_MISMATCH",
            "La bolsa no contiene un material requerido por esta OPM.", status_code=409,
        )
    reserved = session.scalar(select(func.coalesce(func.sum(
        ScmUsoUnidadLogisticaPreparacion.cantidad_kg
    ), 0)).where(
        ScmUsoUnidadLogisticaPreparacion.unidad_logistica_id == unit.id,
        ScmUsoUnidadLogisticaPreparacion.estado == "RESERVADA",
    ))
    usable = Decimal(unit.cantidad_disponible_kg) - Decimal(reserved or 0)
    if usable <= ZERO:
        raise ScmServiceError(
            "OPM_SOURCE_UNIT_NOT_AVAILABLE",
            f"La bolsa {unit.codigo} ya no tiene saldo utilizable.", status_code=409,
        )
    return {
        "id": str(unit.id),
        "codigo": unit.codigo,
        "qr_value": unit.qr_value,
        "material": {
            "id": unit.material.id,
            "codigo": unit.material.codigo,
            "nombre": unit.material.nombre,
        },
        "ubicacion_codigo": unit.ubicacion.codigo,
        "estado_calidad": unit.estado_calidad,
        "cantidad_disponible_kg": _kg(unit.cantidad_disponible_kg),
        "cantidad_reservada_kg": _kg(reserved),
        "cantidad_utilizable_kg": _kg(usable),
    }


def _require_version(resource, value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ScmServiceError(
            "VERSION_REQUIRED",
            "version debe ser un entero positivo.",
            status_code=400,
        )
    if resource.version != value:
        raise ScmServiceError(
            "VERSION_CONFLICT",
            "El recurso fue modificado por otra persona.",
            status_code=409,
            details={"expected": value, "current": resource.version},
        )


def _nonnegative_kg(value, *, field):
    if value in (None, ""):
        raise ScmServiceError(
            "OPM_WEIGHT_FIELDS_REQUIRED",
            "Se requieren bruto, tara y neto.",
            status_code=422,
            details={"field": field},
        )
    try:
        parsed = Decimal(str(value))
    except Exception as error:
        raise ScmServiceError(
            "INVALID_QUANTITY", f"{field} debe ser una cantidad valida.",
            status_code=422, details={"field": field},
        ) from error
    try:
        quantized = parsed.quantize(QTY)
    except InvalidOperation as error:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} excede el rango numerico permitido.",
            status_code=422,
            details={"field": field},
        ) from error
    if (
        not parsed.is_finite()
        or parsed < 0
        or quantized != parsed
        or parsed > Decimal("999999999999.999")
    ):
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"{field} admite hasta tres decimales y 12 enteros no negativos.",
            status_code=422, details={"field": field},
        )
    return parsed


def _manual_weight(data):
    method = str(data.get("metodo") or "").strip().upper()
    if method not in {MANUAL_METHOD, STATION_METHOD}:
        raise ScmServiceError(
            "OPM_MANUAL_METHOD_REQUIRED",
            "El método debe ser CONTINGENCIA_MANUAL o BALANZA_ESTACION con evidencia.",
            status_code=422,
        )
    gross = _nonnegative_kg(data.get("bruto_kg"), field="bruto_kg")
    tare = _nonnegative_kg(data.get("tara_kg"), field="tara_kg")
    net = _nonnegative_kg(data.get("neto_kg"), field="neto_kg")
    if gross <= 0 or net <= 0:
        raise ScmServiceError(
            "INVALID_QUANTITY", "bruto_kg y neto_kg deben ser positivos.",
            status_code=422,
        )
    if (gross - tare).quantize(QTY) != net:
        raise ScmServiceError(
            "OPM_WEIGHT_MISMATCH",
            "neto_kg debe ser exactamente bruto_kg menos tara_kg.",
            status_code=422,
            details={
                "bruto_kg": _kg(gross), "tara_kg": _kg(tare),
                "neto_kg": _kg(net), "neto_calculado_kg": _kg(gross - tare),
            },
        )
    reason = required_text(data.get("motivo"), field="motivo", max_length=240)
    evidence = required_text(
        data.get("evidencia_ref"), field="evidencia_ref", max_length=160
    )
    return method, gross, tare, net, reason, evidence


def release_preparation_order(session, *, actor_id, operation_id, order_id, data):
    try:
        reject_unknown_fields(data, allowed={"version", "motivo"})
        actor = load_actor(session, actor_id, capability="OPM_LIBERAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "orden_preparacion_id": str(order_id),
            "version": data.get("version"), "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id, "POST /ordenes-preparacion-material/{id}/liberar",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado != "BORRADOR":
            raise ScmServiceError(
                "OPM_NOT_DRAFT", "Solo una OPM borrador puede liberarse.", status_code=409
            )
        recipe = session.get(RecetaColorMaestra, order.receta_revision_id)
        if recipe is None or recipe.estado != "APROBADA":
            raise ScmServiceError(
                "OPM_RECIPE_NOT_APPROVED",
                "La revision exacta de receta debe permanecer aprobada.",
                status_code=409,
            )
        assignments = session.scalars(
            select(ScmAsignacionRequerimientoPreparacion)
            .where(
                ScmAsignacionRequerimientoPreparacion.orden_preparacion_id
                == order.id
            )
            .order_by(ScmAsignacionRequerimientoPreparacion.id)
            .with_for_update()
        ).all()
        if not assignments:
            raise ScmServiceError(
                "OPM_ASSIGNMENTS_REQUIRED", "La OPM no posee asignaciones.", status_code=409
            )
        requirement_ids = sorted(
            {value.requerimiento_id for value in assignments}, key=str
        )
        locked_requirements = session.scalars(
            select(ScmRequerimientoMaterialPreparado)
            .where(ScmRequerimientoMaterialPreparado.id.in_(requirement_ids))
            .order_by(ScmRequerimientoMaterialPreparado.id)
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        ).unique().all()
        requirements_by_id = {value.id: value for value in locked_requirements}
        aggregate = {}
        for assignment in assignments:
            requirement = requirements_by_id[assignment.requerimiento_id]
            if (
                requirement.receta_revision_id != order.receta_revision_id
                or requirement.composicion_hash != order.composicion_hash
                or requirement.estado in ("SATISFECHA", "CANCELADA")
            ):
                raise ScmServiceError(
                    "OPM_INCOMPATIBLE_REQUIREMENTS",
                    "Una cobertura dejo de ser compatible o vigente.",
                    status_code=409,
                    details={"requerimiento_id": str(requirement.id)},
                )
            ratio = (
                Decimal(assignment.cantidad_planificada_kg)
                / Decimal(requirement.cantidad_requerida_kg)
            )
            for component in requirement.composicion_snapshot_json["componentes"]:
                key = (component["material_id"], component["tipo_componente"])
                value = aggregate.setdefault(key, {
                    "cantidad": ZERO,
                    "fuentes": [],
                })
                contribution = Decimal(component["cantidad_kg"]) * ratio
                value["cantidad"] += contribution
                value["fuentes"].append({
                    "requerimiento_id": str(requirement.id),
                    "asignacion_kg": _kg(assignment.cantidad_planificada_kg),
                    "aporte_objetivo_kg_sin_redondeo": format(contribution, "f"),
                })
            assignment.cantidad_comprometida_kg = assignment.cantidad_planificada_kg
            assignment.estado = "COMPROMETIDA"
        for requirement in locked_requirements:
            _refresh_requirement_state(requirement)
            requirement.version += 1
        for (material_id, component_type), value in sorted(aggregate.items()):
            quantity = value["cantidad"].quantize(QTY, rounding=ROUND_HALF_UP)
            if quantity <= 0:
                raise ScmServiceError(
                    "MATERIAL_REQUIREMENT_BELOW_SCALE",
                    "Un input objetivo queda debajo de 0.001 kg.",
                    status_code=422,
                    details={"material_id": material_id},
                )
            session.add(ScmRequerimientoMaterial(
                orden_preparacion_material_id=order.id,
                material_id=material_id,
                tipo_componente=component_type,
                cantidad_plan_kg=quantity,
                receta_revision_id=order.receta_revision_id,
                calculo_snapshot_json={
                    "formula": "suma proporcional de coberturas OPM",
                    "cantidad_sin_redondeo": format(value["cantidad"], "f"),
                    "fuentes": value["fuentes"],
                },
                created_by_id=actor.id,
            ))
        order.estado = "LIBERADA"
        order.released_by_id = actor.id
        order.released_at = utc_now()
        order.version += 1
        session.flush()
        payload = _serialize_opm(order)
        _event(
            session, aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id, event_type="PREPARATION_ORDER_RELEASED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def cancel_preparation_order(session, *, actor_id, operation_id, order_id, data):
    """Anula una OPM sin ejecucion fisica y libera sus compromisos."""
    try:
        reject_unknown_fields(data, allowed={"version", "motivo"})
        actor = load_actor(session, actor_id, capability="OPM_LIBERAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "orden_preparacion_id": str(order_id),
            "version": data.get("version"),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /ordenes-preparacion-material/{id}/anular",
            actor,
            request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado == "ANULADA":
            raise ScmServiceError(
                "OPM_ALREADY_CANCELLED",
                "La OPM ya esta anulada; reutiliza la misma clave para replay.",
                status_code=409,
            )
        if order.estado not in ("BORRADOR", "LIBERADA", "EN_PREPARACION"):
            raise ScmServiceError(
                "OPM_NOT_CANCELLABLE",
                "Solo puede anularse antes de incorporar material o crear el lote.",
                status_code=409,
                details={"estado": order.estado},
            )
        contributions = session.scalars(
            select(ScmAportePreparacionMaterial)
            .where(ScmAportePreparacionMaterial.orden_preparacion_id == order.id)
            .order_by(ScmAportePreparacionMaterial.id)
            .with_for_update(of=ScmAportePreparacionMaterial)
        ).all()
        if contributions or order.lote is not None:
            raise ScmServiceError(
                "OPM_EXECUTION_ALREADY_INCORPORATED",
                "La OPM ya incorporo material o genero salida y no puede anularse.",
                status_code=409,
            )

        assignments_preview = session.scalars(
            select(ScmAsignacionRequerimientoPreparacion)
            .where(
                ScmAsignacionRequerimientoPreparacion.orden_preparacion_id
                == order.id
            )
            .order_by(ScmAsignacionRequerimientoPreparacion.id)
        ).unique().all()
        requirement_ids = sorted(
            {value.requerimiento_id for value in assignments_preview}, key=str
        )
        requirements = session.scalars(
            select(ScmRequerimientoMaterialPreparado)
            .where(ScmRequerimientoMaterialPreparado.id.in_(requirement_ids))
            .order_by(ScmRequerimientoMaterialPreparado.id)
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        ).unique().all() if requirement_ids else []
        assignments = session.scalars(
            select(ScmAsignacionRequerimientoPreparacion)
            .where(
                ScmAsignacionRequerimientoPreparacion.orden_preparacion_id
                == order.id
            )
            .order_by(ScmAsignacionRequerimientoPreparacion.id)
            .with_for_update(of=ScmAsignacionRequerimientoPreparacion)
        ).unique().all()

        raw_reservations = session.scalars(
            select(ScmReservaMaterial)
            .join(ScmRequerimientoMaterial)
            .where(
                ScmRequerimientoMaterial.orden_preparacion_material_id
                == order.id
            )
            .order_by(ScmReservaMaterial.id)
            .with_for_update(of=ScmReservaMaterial)
        ).unique().all()
        if any(
            Decimal(value.emitida_neta_kg) > 0
            or Decimal(value.cantidad_consumida_kg) > 0
            for value in raw_reservations
        ):
            raise ScmServiceError(
                "OPM_INPUT_RETURN_REQUIRED",
                "Devuelve primero todo input emitido; un consumo ya confirmado no "
                "admite anulacion.",
                status_code=409,
                details={
                    "reservas": [
                        {
                            "id": str(value.id),
                            "emitida_neta_kg": _kg(value.emitida_neta_kg),
                            "consumida_kg": _kg(value.cantidad_consumida_kg),
                        }
                        for value in raw_reservations
                        if Decimal(value.emitida_neta_kg) > 0
                        or Decimal(value.cantidad_consumida_kg) > 0
                    ]
                },
            )
        raw_balance_ids = sorted(
            {value.saldo_material_id for value in raw_reservations}, key=str
        )
        balances = session.scalars(
            select(ScmSaldoMaterialInventario)
            .where(ScmSaldoMaterialInventario.id.in_(raw_balance_ids))
            .order_by(ScmSaldoMaterialInventario.id)
            .with_for_update(of=ScmSaldoMaterialInventario)
        ).unique().all() if raw_balance_ids else []
        balances_by_id = {value.id: value for value in balances}
        for reservation in raw_reservations:
            if reservation.estado != "ACTIVA":
                continue
            balance = balances_by_id[reservation.saldo_material_id]
            release_quantity = Decimal(reservation.cantidad_kg)
            if Decimal(balance.cantidad_reservada_kg) < release_quantity:
                raise ScmServiceError(
                    "MATERIAL_SOURCE_INCONSISTENT",
                    "El saldo origen no conserva la reserva que debe liberarse.",
                    status_code=409,
                    details={"reserva_id": str(reservation.id)},
                )
            balance.cantidad_reservada_kg = (
                Decimal(balance.cantidad_reservada_kg) - release_quantity
            )
            balance.version += 1
            reservation.estado = "LIBERADA"

        for assignment in assignments:
            if Decimal(assignment.cantidad_consumida_kg) > 0:
                raise ScmServiceError(
                    "OPM_ASSIGNMENT_ALREADY_CONSUMED",
                    "Una asignacion consumida no puede anularse.",
                    status_code=409,
                )
            assignment.cantidad_comprometida_kg = ZERO
            assignment.estado = "CANCELADA"
            assignment.released_by_id = actor.id
            assignment.motivo_liberacion = reason
            assignment.released_at = utc_now()
        for requirement in requirements:
            _refresh_requirement_state(requirement)
            requirement.version += 1

        readings = session.scalars(
            select(ScmLecturaPesoPreparacion)
            .where(ScmLecturaPesoPreparacion.orden_preparacion_id == order.id)
            .order_by(ScmLecturaPesoPreparacion.id)
            .with_for_update(of=ScmLecturaPesoPreparacion)
        ).unique().all()
        for reading in readings:
            if reading.estado in ("PENDIENTE_SEGUNDA_CONFIRMACION", "APROBADA"):
                reading.estado = "INVALIDADA"
                reading.invalidated_by_id = actor.id
                reading.invalidated_at = utc_now()
                reading.invalidation_reason = reason
                reading.version += 1
        order.estado = "ANULADA"
        order.version += 1
        session.flush()
        payload = _serialize_opm(order)
        _event(
            session,
            aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id,
            event_type="PREPARATION_ORDER_CANCELLED",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def _available_raw_balance(balance):
    return (
        Decimal(balance.cantidad_fisica_kg)
        - Decimal(balance.cantidad_reservada_kg)
        - Decimal(balance.cantidad_no_disponible_kg)
    )


def reserve_preparation_inputs(session, *, actor_id, operation_id, order_id, data):
    try:
        reject_unknown_fields(data, allowed={"version", "ubicacion_origen_ids"})
        actor = load_actor(session, actor_id, capability="OPM_EJECUTAR")
        raw_location_ids = data.get("ubicacion_origen_ids")
        if (
            not isinstance(raw_location_ids, list) or not raw_location_ids
            or any(not isinstance(value, int) or isinstance(value, bool) for value in raw_location_ids)
        ):
            raise ScmServiceError(
                "CANONICAL_SOURCE_LOCATIONS_REQUIRED",
                "ubicacion_origen_ids debe contener ubicaciones canonicas.",
                status_code=422,
            )
        location_ids = sorted(set(raw_location_ids))
        request_data = {
            "orden_preparacion_id": str(order_id),
            "version": data.get("version"),
            "ubicacion_origen_ids": location_ids,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /ordenes-preparacion-material/{id}/reservar-insumos",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado != "LIBERADA":
            raise ScmServiceError(
                "OPM_NOT_RELEASED", "La OPM debe estar liberada.", status_code=409
            )
        locations = [
            _canonical_location(
                session, value, actor_id=actor.id, require_assignment=True
            )
            for value in location_ids
        ]
        if any(not value.permite_saldo_libre for value in locations):
            raise ScmServiceError(
                "SOURCE_LOCATION_NOT_AVAILABLE",
                "Una ubicacion origen no permite seleccionar saldo libre.",
                status_code=409,
            )
        requirements = session.scalars(
            select(ScmRequerimientoMaterial)
            .where(ScmRequerimientoMaterial.orden_preparacion_material_id == order.id)
            .order_by(ScmRequerimientoMaterial.id)
            .with_for_update()
        ).all()
        if not requirements:
            raise ScmServiceError(
                "OPM_INPUT_REQUIREMENTS_REQUIRED",
                "La OPM liberada no tiene inputs objetivo.", status_code=409,
            )
        if any(value.reservas for value in requirements):
            raise ScmServiceError(
                "OPM_INPUTS_ALREADY_RESERVED",
                "Los inputs de la OPM ya tienen reservas.", status_code=409,
            )
        allocations = []
        shortages = []
        for requirement in requirements:
            pending = Decimal(requirement.cantidad_plan_kg)
            balances = session.scalars(
                select(ScmSaldoMaterialInventario)
                .where(
                    ScmSaldoMaterialInventario.material_id == requirement.material_id,
                    ScmSaldoMaterialInventario.ubicacion_id.in_(location_ids),
                )
                .order_by(ScmSaldoMaterialInventario.id)
                .with_for_update()
            ).all()
            selected = []
            for balance in balances:
                take = min(pending, max(_available_raw_balance(balance), ZERO))
                if take > 0:
                    selected.append((balance, take))
                    pending -= take
                if pending <= 0:
                    break
            allocations.append((requirement, selected))
            if pending > 0:
                shortages.append({
                    "material_id": requirement.material_id,
                    "codigo": requirement.material.codigo,
                    "faltante_kg": _kg(pending),
                })
        if shortages:
            raise ScmServiceError(
                "INSUFFICIENT_MATERIAL_STOCK",
                "No hay saldo libre suficiente para reservar todos los inputs.",
                status_code=409, details={"faltantes": shortages},
            )
        for requirement, selected in allocations:
            for balance, quantity in selected:
                balance.cantidad_reservada_kg = Decimal(balance.cantidad_reservada_kg) + quantity
                balance.version += 1
                session.add(ScmReservaMaterial(
                    requerimiento=requirement,
                    saldo_material_id=balance.id,
                    cantidad_kg=quantity,
                    created_by_id=actor.id,
                ))
        order.version += 1
        session.flush()
        payload = _serialize_opm(order)
        _event(
            session, aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id, event_type="PREPARATION_INPUTS_RESERVED",
            actor=actor, operation=operation, after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def emit_preparation_input(
    session, *, actor_id, operation_id, order_id, reservation_id, data,
):
    try:
        reject_unknown_fields(
            data,
            allowed={"version", "ubicacion_destino_id", "cantidad_kg", "motivo"},
        )
        actor = load_actor(session, actor_id, capability="MATERIAL_EMITIR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        quantity = positive_kg(data.get("cantidad_kg"), field="cantidad_kg")
        destination_id = data.get("ubicacion_destino_id")
        if not isinstance(destination_id, int) or isinstance(destination_id, bool):
            raise ScmServiceError(
                "CANONICAL_LOCATION_REQUIRED",
                "ubicacion_destino_id debe ser un entero.", status_code=422,
            )
        request_data = {
            "orden_preparacion_id": str(order_id),
            "reserva_id": str(reservation_id),
            "version": data.get("version"),
            "ubicacion_destino_id": destination_id,
            "cantidad_kg": _kg(quantity),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /ordenes-preparacion-material/{id}/reservas-insumo/{reserva_id}/emitir",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado != "LIBERADA":
            raise ScmServiceError(
                "OPM_NOT_RELEASED", "La OPM debe estar liberada.", status_code=409
            )
        reservation = session.scalar(
            select(ScmReservaMaterial)
            .where(ScmReservaMaterial.id == reservation_id)
            .with_for_update(of=ScmReservaMaterial)
        )
        if (
            reservation is None
            or reservation.requerimiento.orden_preparacion_material_id != order.id
        ):
            raise ScmServiceError(
                "OPM_INPUT_RESERVATION_NOT_FOUND",
                "La reserva no pertenece a esta OPM.", status_code=404,
            )
        source = session.scalar(
            select(ScmSaldoMaterialInventario)
            .where(ScmSaldoMaterialInventario.id == reservation.saldo_material_id)
            .with_for_update(of=ScmSaldoMaterialInventario)
        )
        if source is None:
            raise ScmServiceError(
                "MATERIAL_SOURCE_INCONSISTENT", "El saldo origen no existe.",
                status_code=409,
            )
        _canonical_location(
            session, source.ubicacion_id, actor_id=actor.id,
            require_assignment=True,
        )
        destination_location = _canonical_location(
            session, destination_id, actor_id=actor.id,
            require_assignment=True,
        )
        if source.ubicacion_id == destination_location.id:
            raise ScmServiceError(
                "MATERIAL_EMISSION_LOCATION_CONFLICT",
                "Origen y destino deben ser ubicaciones distintas.", status_code=409,
            )
        remaining = (
            Decimal(reservation.cantidad_kg)
            - Decimal(reservation.emitida_neta_kg)
            - Decimal(reservation.cantidad_consumida_kg)
        )
        if reservation.estado != "ACTIVA" or quantity > remaining:
            raise ScmServiceError(
                "MATERIAL_EMISSION_EXCEEDS_RESERVATION",
                "La emision excede el saldo reservado.", status_code=409,
                details={"saldo_reserva_kg": _kg(max(remaining, ZERO))},
            )
        if (
            Decimal(source.cantidad_fisica_kg) < quantity
            or Decimal(source.cantidad_reservada_kg) < quantity
        ):
            raise ScmServiceError(
                "MATERIAL_SOURCE_INCONSISTENT",
                "El saldo origen ya no cubre la emision.", status_code=409,
            )
        destination = session.scalar(
            select(ScmSaldoMaterialInventario)
            .where(
                ScmSaldoMaterialInventario.material_id == source.material_id,
                ScmSaldoMaterialInventario.ubicacion_id == destination_location.id,
            )
            .with_for_update(of=ScmSaldoMaterialInventario)
        )
        if destination is None:
            destination = ScmSaldoMaterialInventario(
                material_id=source.material_id,
                ubicacion_id=destination_location.id,
            )
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
            reserva_id=reservation.id,
            saldo_destino_id=destination.id,
            cantidad_kg=quantity,
            motivo=reason,
            actor_id=actor.id,
            operation_id=operation_id,
        )
        session.add(emission)
        session.flush()
        for balance, delta, suffix in (
            (source, -quantity, "SALIDA"),
            (destination, quantity, "ENTRADA"),
        ):
            session.add(ScmMovimientoMaterialInventario(
                saldo_id=balance.id, tipo="EMISION",
                cantidad_delta_kg=delta,
                saldo_fisico_resultante_kg=balance.cantidad_fisica_kg,
                motivo=reason, referencia_tipo="EMISION_INPUT_OPM",
                referencia_id=str(emission.id), actor_id=actor.id,
                operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:{suffix}"),
            ))
        order.version += 1
        session.flush()
        emission_payload = {
            "id": str(emission.id), "reserva_id": str(emission.reserva_id),
            "cantidad_kg": _kg(emission.cantidad_kg),
            "cantidad_disponible_kg": _kg(quantity),
            "origen": source.ubicacion.to_dict(),
            "destino": destination_location.to_dict(),
            "motivo": reason, "actor_id": actor.id,
            "created_at": _iso(emission.created_at),
        }
        payload = {
            "emision": emission_payload,
            "orden_preparacion": _serialize_opm(order),
        }
        _event(
            session, aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id, event_type="PREPARATION_INPUT_EMITTED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload, status=201)
    except Exception:
        session.rollback()
        raise


def start_preparation_order(session, *, actor_id, operation_id, order_id, data):
    try:
        reject_unknown_fields(data, allowed={"version", "motivo"})
        actor = load_actor(session, actor_id, capability="OPM_EJECUTAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "orden_preparacion_id": str(order_id),
            "version": data.get("version"), "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id, "POST /ordenes-preparacion-material/{id}/iniciar",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado != "LIBERADA":
            raise ScmServiceError(
                "OPM_NOT_RELEASED", "Solo una OPM liberada puede iniciar.", status_code=409
            )
        if not any(
            emission
            for requirement in order.requerimientos_insumo
            for reservation in requirement.reservas
            for emission in reservation.emisiones
            if Decimal(emission.cantidad_kg)
            - Decimal(emission.cantidad_devuelta_kg)
            - Decimal(emission.cantidad_consumida_kg) > 0
        ):
            raise ScmServiceError(
                "OPM_INPUT_EMISSIONS_REQUIRED",
                "Debe existir al menos una emision de input antes de iniciar.",
                status_code=409,
            )
        order.estado = "EN_PREPARACION"
        order.started_by_id = actor.id
        order.started_at = utc_now()
        order.version += 1
        session.flush()
        payload = _serialize_opm(order)
        _event(
            session, aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id, event_type="PREPARATION_ORDER_STARTED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def _output_assignment_for_reading(
    session, *, order_id, assignment_id, lock=True,
):
    statement = (
        select(ScmAsignacionRequerimientoPreparacion)
        .where(
            ScmAsignacionRequerimientoPreparacion.orden_preparacion_id
            == order_id,
            ScmAsignacionRequerimientoPreparacion.tipo_fuente
            == "OPM_ESPERADA",
            ScmAsignacionRequerimientoPreparacion.estado.in_(
                ("COMPROMETIDA", "LIBERADA")
            ),
        )
        .order_by(ScmAsignacionRequerimientoPreparacion.id)
    )
    if lock:
        statement = statement.with_for_update(
            of=ScmAsignacionRequerimientoPreparacion
        )
    assignments = session.scalars(statement).unique().all()
    if not assignments:
        raise ScmServiceError(
            "OPM_OUTPUT_ASSIGNMENT_NOT_AVAILABLE",
            "La OPM no tiene necesidades activas para vincular la bolsa.",
            status_code=409,
        )
    if assignment_id is None:
        if len(assignments) != 1:
            raise ScmServiceError(
                "OPM_OUTPUT_ASSIGNMENT_REQUIRED",
                "Selecciona la necesidad que recibira esta bolsa completa.",
                status_code=422,
                details={
                    "asignaciones": [
                        _serialize_assignment(value) for value in assignments
                    ],
                },
            )
        return assignments[0]
    selected = next(
        (value for value in assignments if value.id == assignment_id), None
    )
    if selected is None:
        raise ScmServiceError(
            "OPM_OUTPUT_ASSIGNMENT_NOT_ELIGIBLE",
            "La asignacion no pertenece a esta OPM o ya no esta activa.",
            status_code=409,
            details={"asignacion_requerimiento_id": str(assignment_id)},
        )
    return selected


def _validate_output_assignment_capacity(
    session, *, assignment, additional_net=ZERO,
):
    assigned = session.scalar(
        select(func.coalesce(func.sum(
            ScmLecturaPesoPreparacion.peso_neto_kg
        ), 0)).where(
            ScmLecturaPesoPreparacion.asignacion_requerimiento_id
            == assignment.id,
            ScmLecturaPesoPreparacion.tipo_uso == "BOLSA_SALIDA",
            ScmLecturaPesoPreparacion.estado.in_((
                "PENDIENTE_SEGUNDA_CONFIRMACION", "APROBADA", "UTILIZADA",
            )),
        )
    )
    resulting = (Decimal(assigned or 0) + Decimal(additional_net)).quantize(QTY)
    planned = Decimal(assignment.cantidad_planificada_kg).quantize(QTY)
    if resulting > planned:
        raise ScmServiceError(
            "OPM_OUTPUT_BAG_EXCEEDS_ASSIGNMENT",
            "La bolsa completa excede la capacidad restante de la necesidad.",
            status_code=409,
            details={
                "asignacion_requerimiento_id": str(assignment.id),
                "requerimiento_id": str(assignment.requerimiento_id),
                "corrida_fabricacion_id": str(
                    assignment.requerimiento.corrida_fabricacion_id
                ),
                "planificada_kg": _kg(planned),
                "ya_registrada_kg": _kg(assigned),
                "bolsa_neta_kg": _kg(additional_net),
                "resultante_kg": _kg(resulting),
            },
        )


def record_preparation_reading(session, *, actor_id, operation_id, order_id, data):
    try:
        reject_unknown_fields(
            data,
            allowed={
                "version", "tipo_uso", "metodo", "bruto_kg", "tara_kg",
                "neto_kg", "motivo", "evidencia_ref",
                "asignacion_requerimiento_id",
                "unidades_origen_qr",
            },
        )
        actor = load_actor(session, actor_id, capability="OPM_EJECUTAR")
        method, gross, tare, net, reason, evidence = _manual_weight(data)
        source_qrs = data.get("unidades_origen_qr") or []
        if not isinstance(source_qrs, list) or any(not isinstance(value, str) for value in source_qrs):
            raise ScmServiceError(
                "OPM_SOURCE_QR_INVALID", "unidades_origen_qr debe ser una lista de QRs.",
                status_code=422,
            )
        use_type = str(data.get("tipo_uso") or "").strip().upper()
        if use_type not in ("APORTE", "BOLSA_SALIDA"):
            raise ScmServiceError(
                "OPM_READING_USE_REQUIRED",
                "tipo_uso debe ser APORTE o BOLSA_SALIDA.", status_code=422,
            )
        raw_assignment_id = data.get("asignacion_requerimiento_id")
        if use_type == "APORTE" and raw_assignment_id is not None:
            raise ScmServiceError(
                "OPM_INPUT_READING_CANNOT_TARGET_REQUIREMENT",
                "Una lectura de aporte no se vincula a una necesidad de salida.",
                status_code=422,
            )
        assignment_id = None
        if raw_assignment_id is not None:
            try:
                assignment_id = UUID(str(raw_assignment_id))
            except (TypeError, ValueError, AttributeError) as error:
                raise ScmServiceError(
                    "OPM_OUTPUT_ASSIGNMENT_INVALID",
                    "asignacion_requerimiento_id debe ser un UUID valido.",
                    status_code=400,
                ) from error
        request_data = {
            "orden_preparacion_id": str(order_id),
            "version": data.get("version"),
            "tipo_uso": use_type, "metodo": method,
            "bruto_kg": _kg(gross), "tara_kg": _kg(tare), "neto_kg": _kg(net),
            "motivo": reason, "evidencia_ref": evidence,
            "asignacion_requerimiento_id": (
                str(assignment_id) if assignment_id else None
            ),
            "unidades_origen_qr": source_qrs,
        }
        operation, replay = _reserve_operation(
            session, operation_id, "POST /ordenes-preparacion-material/{id}/lecturas",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado != "EN_PREPARACION":
            raise ScmServiceError(
                "OPM_NOT_IN_PREPARATION",
                "Los pesos solo se registran durante la preparacion.", status_code=409,
            )
        assignment = None
        if use_type == "BOLSA_SALIDA":
            assignment = _output_assignment_for_reading(
                session,
                order_id=order.id,
                assignment_id=assignment_id,
            )
            _validate_output_assignment_capacity(
                session,
                assignment=assignment,
                additional_net=net,
            )
            # La seleccion automatica de una unica necesidad tambien forma
            # parte de la huella idempotente observable.
            request_data["asignacion_requerimiento_id"] = str(assignment.id)
        if use_type == "APORTE" and method == STATION_METHOD and not source_qrs:
            raise ScmServiceError(
                "OPM_SOURCE_QR_REQUIRED",
                "El pesaje conectado de un aporte exige escanear sus bolsas origen.",
                status_code=422,
            )
        reading = ScmLecturaPesoPreparacion(
            orden_preparacion_id=order.id,
            asignacion_requerimiento_id=(assignment.id if assignment else None),
            tipo_uso=use_type,
            peso_bruto_kg=gross,
            tara_kg=tare,
            peso_neto_kg=net,
            metodo=method,
            evidencia_ref=evidence,
            motivo=reason,
            estado="PENDIENTE_SEGUNDA_CONFIRMACION",
            created_by_id=actor.id,
            operation_id=operation_id,
        )
        session.add(reading)
        session.flush()
        if use_type == "APORTE" and source_qrs:
            remaining = net
            seen_qrs = set()
            for raw_qr in source_qrs:
                qr = raw_qr.strip()
                if not qr or qr in seen_qrs:
                    raise ScmServiceError(
                        "OPM_SOURCE_QR_DUPLICATE", "Cada bolsa origen se escanea una sola vez.",
                        status_code=422,
                    )
                seen_qrs.add(qr)
                unit = session.scalar(
                    select(ScmUnidadLogisticaInventario)
                    .where(ScmUnidadLogisticaInventario.qr_value == qr)
                    .with_for_update(of=ScmUnidadLogisticaInventario)
                )
                if unit is None or unit.estado != "DISPONIBLE":
                    raise ScmServiceError(
                        "OPM_SOURCE_UNIT_NOT_AVAILABLE",
                        "Una bolsa origen no existe o no está disponible.", status_code=409,
                        details={"qr": qr},
                    )
                reserved = session.scalar(select(func.coalesce(func.sum(
                    ScmUsoUnidadLogisticaPreparacion.cantidad_kg
                ), 0)).where(
                    ScmUsoUnidadLogisticaPreparacion.unidad_logistica_id == unit.id,
                    ScmUsoUnidadLogisticaPreparacion.estado == "RESERVADA",
                ))
                available = Decimal(unit.cantidad_disponible_kg) - Decimal(reserved or 0)
                take = min(available, remaining)
                if take > 0:
                    session.add(ScmUsoUnidadLogisticaPreparacion(
                        lectura_id=reading.id, unidad_logistica_id=unit.id,
                        cantidad_kg=take, estado="RESERVADA",
                    ))
                    remaining -= take
                if remaining == ZERO:
                    break
            if remaining != ZERO:
                raise ScmServiceError(
                    "OPM_SOURCE_UNITS_INSUFFICIENT",
                    "Los QRs escaneados no cubren el peso NET del aporte.", status_code=409,
                    details={"faltante_kg": _kg(remaining)},
                )
        order.version += 1
        session.flush()
        payload = _serialize_reading(reading)
        _event(
            session, aggregate_type="LECTURA_PESO_PREPARACION",
            aggregate_id=reading.id, event_type="PREPARATION_WEIGHT_RECORDED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload, status=201)
    except Exception:
        session.rollback()
        raise


def confirm_preparation_reading(session, *, actor_id, operation_id, reading_id, data):
    try:
        reject_unknown_fields(
            data,
            allowed={"version", "bruto_kg", "tara_kg", "neto_kg", "motivo"},
        )
        actor = load_actor(session, actor_id, capability="OPM_PESO_CONFIRMAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        gross = _nonnegative_kg(data.get("bruto_kg"), field="bruto_kg")
        tare = _nonnegative_kg(data.get("tara_kg"), field="tara_kg")
        net = _nonnegative_kg(data.get("neto_kg"), field="neto_kg")
        request_data = {
            "lectura_id": str(reading_id), "version": data.get("version"),
            "bruto_kg": _kg(gross), "tara_kg": _kg(tare), "neto_kg": _kg(net),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /lecturas-preparacion/{id}/confirmar-segundo-actor",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order_id = session.scalar(
            select(ScmLecturaPesoPreparacion.orden_preparacion_id).where(
                ScmLecturaPesoPreparacion.id == reading_id
            )
        )
        if order_id is None:
            raise ScmServiceError(
                "OPM_READING_NOT_FOUND", "La lectura no existe.", status_code=404
            )
        order = _load_opm(session, order_id, lock=True)
        reading = session.scalar(
            select(ScmLecturaPesoPreparacion)
            .where(ScmLecturaPesoPreparacion.id == reading_id)
            .with_for_update(of=ScmLecturaPesoPreparacion)
        )
        if reading is None:
            raise ScmServiceError(
                "OPM_READING_NOT_FOUND", "La lectura no existe.", status_code=404
            )
        _require_version(reading, data.get("version"))
        if order.estado != "EN_PREPARACION":
            raise ScmServiceError(
                "OPM_READING_CONFIRMATION_WINDOW_CLOSED",
                "Los pesos solo se confirman mientras la OPM esta en preparacion.",
                status_code=409,
            )
        if actor.id == reading.created_by_id:
            raise ScmServiceError(
                "OPM_SELF_APPROVAL_FORBIDDEN",
                "El creador de la lectura no puede confirmarla.", status_code=403,
            )
        if reading.estado != "PENDIENTE_SEGUNDA_CONFIRMACION":
            raise ScmServiceError(
                "OPM_READING_NOT_PENDING",
                "La lectura ya no espera segunda confirmacion.", status_code=409,
            )
        if reading.tipo_uso == "BOLSA_SALIDA":
            assignment = _output_assignment_for_reading(
                session,
                order_id=order.id,
                assignment_id=reading.asignacion_requerimiento_id,
            )
            _validate_output_assignment_capacity(
                session, assignment=assignment
            )
        expected = (
            Decimal(reading.peso_bruto_kg), Decimal(reading.tara_kg),
            Decimal(reading.peso_neto_kg),
        )
        if (gross, tare, net) != expected:
            raise ScmServiceError(
                "OPM_SECOND_CONFIRMATION_MISMATCH",
                "La segunda confirmacion debe repetir exactamente los tres pesos.",
                status_code=409,
            )
        approval = ScmAprobacionLecturaPesoPreparacion(
            lectura_id=reading.id,
            lectura_version=reading.version,
            peso_bruto_kg=gross,
            tara_kg=tare,
            peso_neto_kg=net,
            motivo=reason,
            actor_id=actor.id,
            operation_id=operation_id,
        )
        session.add(approval)
        reading.estado = "APROBADA"
        reading.version += 1
        order.version += 1
        session.flush()
        payload = _serialize_reading(reading)
        _event(
            session, aggregate_type="LECTURA_PESO_PREPARACION",
            aggregate_id=reading.id, event_type="PREPARATION_WEIGHT_SECOND_CONFIRMED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def invalidate_preparation_reading(
    session, *, actor_id, operation_id, reading_id, data,
):
    try:
        reject_unknown_fields(data, allowed={"version", "motivo"})
        actor = load_actor_any(
            session, actor_id,
            capabilities=("OPM_EJECUTAR", "OPM_PESO_CONFIRMAR", "OPM_CERRAR"),
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "lectura_id": str(reading_id),
            "version": data.get("version"), "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /lecturas-preparacion/{id}/invalidar",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order_id = session.scalar(
            select(ScmLecturaPesoPreparacion.orden_preparacion_id).where(
                ScmLecturaPesoPreparacion.id == reading_id
            )
        )
        if order_id is None:
            raise ScmServiceError(
                "OPM_READING_NOT_FOUND", "La lectura no existe.", status_code=404
            )
        order = _load_opm(session, order_id, lock=True)
        reading = session.scalar(
            select(ScmLecturaPesoPreparacion)
            .where(ScmLecturaPesoPreparacion.id == reading_id)
            .with_for_update(of=ScmLecturaPesoPreparacion)
        )
        _require_version(reading, data.get("version"))
        if order.estado != "EN_PREPARACION":
            raise ScmServiceError(
                "OPM_READING_INVALIDATION_WINDOW_CLOSED",
                "La lectura solo puede invalidarse durante la preparacion.",
                status_code=409,
            )
        if reading.estado not in ("PENDIENTE_SEGUNDA_CONFIRMACION", "APROBADA"):
            raise ScmServiceError(
                "OPM_READING_NOT_INVALIDATABLE",
                "La lectura ya fue utilizada, invalidada o no admite reversa.",
                status_code=409,
            )
        used = session.scalar(select(ScmAportePreparacionMaterial.id).where(
            ScmAportePreparacionMaterial.lectura_id == reading.id
        )) or session.scalar(select(ScmBolsaMaterialPreparado.id).where(
            ScmBolsaMaterialPreparado.lectura_id == reading.id
        ))
        if used is not None:
            raise ScmServiceError(
                "OPM_READING_ALREADY_USED",
                "Una lectura incorporada o convertida en bolsa no puede invalidarse.",
                status_code=409,
            )
        before = _serialize_reading(reading)
        source_uses = session.scalars(
            select(ScmUsoUnidadLogisticaPreparacion)
            .where(
                ScmUsoUnidadLogisticaPreparacion.lectura_id == reading.id,
                ScmUsoUnidadLogisticaPreparacion.estado == "RESERVADA",
            )
            .with_for_update(of=ScmUsoUnidadLogisticaPreparacion)
        ).all()
        for source_use in source_uses:
            source_use.estado = "LIBERADA"
        reading.estado = "INVALIDADA"
        reading.version += 1
        order.version += 1
        session.flush()
        payload = _serialize_reading(reading)
        _event(
            session, aggregate_type="LECTURA_PESO_PREPARACION",
            aggregate_id=reading.id, event_type="PREPARATION_WEIGHT_INVALIDATED",
            actor=actor, operation=operation, reason=reason,
            before=before, after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def incorporate_preparation_input(session, *, actor_id, operation_id, order_id, data):
    try:
        reject_unknown_fields(data, allowed={"version", "lectura_id", "emision_id", "motivo"})
        actor = load_actor(session, actor_id, capability="OPM_EJECUTAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        try:
            reading_id = UUID(str(data.get("lectura_id")))
            emission_id = UUID(str(data.get("emision_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "OPM_INPUT_REFERENCE_INVALID",
                "lectura_id y emision_id deben ser UUID validos.", status_code=400,
            ) from error
        request_data = {
            "orden_preparacion_id": str(order_id),
            "version": data.get("version"),
            "lectura_id": str(reading_id), "emision_id": str(emission_id),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id, "POST /ordenes-preparacion-material/{id}/aportes",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado != "EN_PREPARACION":
            raise ScmServiceError(
                "OPM_NOT_IN_PREPARATION", "La OPM no esta en preparacion.", status_code=409
            )
        reading = session.scalar(
            select(ScmLecturaPesoPreparacion)
            .where(ScmLecturaPesoPreparacion.id == reading_id)
            .with_for_update(of=ScmLecturaPesoPreparacion)
        )
        emission = session.scalar(
            select(ScmEmisionMaterial)
            .where(ScmEmisionMaterial.id == emission_id)
            .with_for_update(of=ScmEmisionMaterial)
        )
        if (
            reading is None or reading.orden_preparacion_id != order.id
            or reading.tipo_uso != "APORTE"
        ):
            raise ScmServiceError(
                "OPM_APPROVED_INPUT_READING_REQUIRED",
                "La lectura de aporte no pertenece a esta OPM.", status_code=409,
            )
        if reading.estado != "APROBADA" or reading.aprobacion is None:
            raise ScmServiceError(
                "OPM_SECOND_ACTOR_REQUIRED",
                "La lectura manual debe tener segunda confirmacion.", status_code=409,
            )
        if emission is None or (
            emission.reserva.requerimiento.orden_preparacion_material_id != order.id
        ):
            raise ScmServiceError(
                "OPM_INPUT_EMISSION_NOT_FOUND",
                "La emision no pertenece a los inputs de esta OPM.", status_code=404,
            )
        quantity = Decimal(reading.peso_neto_kg)
        available = (
            Decimal(emission.cantidad_kg)
            - Decimal(emission.cantidad_devuelta_kg)
            - Decimal(emission.cantidad_consumida_kg)
        )
        if quantity > available:
            raise ScmServiceError(
                "OPM_INPUT_EXCEEDS_EMISSION",
                "El aporte excede la emision separable disponible.", status_code=409,
                details={"disponible_kg": _kg(available), "aporte_kg": _kg(quantity)},
            )
        destination = session.scalar(
            select(ScmSaldoMaterialInventario)
            .where(ScmSaldoMaterialInventario.id == emission.saldo_destino_id)
            .with_for_update(of=ScmSaldoMaterialInventario)
        )
        if destination is None or (
            Decimal(destination.cantidad_fisica_kg) < quantity
            or Decimal(destination.cantidad_reservada_kg) < quantity
        ):
            raise ScmServiceError(
                "OPM_INPUT_BALANCE_INCONSISTENT",
                "El input emitido ya no esta disponible para incorporar.", status_code=409,
            )
        source_uses = session.scalars(
            select(ScmUsoUnidadLogisticaPreparacion)
            .where(
                ScmUsoUnidadLogisticaPreparacion.lectura_id == reading.id,
                ScmUsoUnidadLogisticaPreparacion.estado == "RESERVADA",
            )
            .order_by(ScmUsoUnidadLogisticaPreparacion.unidad_logistica_id)
            .with_for_update(of=ScmUsoUnidadLogisticaPreparacion)
        ).all()
        if reading.metodo == STATION_METHOD and sum(
            (Decimal(value.cantidad_kg) for value in source_uses), ZERO
        ) != quantity:
            raise ScmServiceError(
                "OPM_SOURCE_UNIT_EVIDENCE_MISMATCH",
                "Las bolsas origen no coinciden con el aporte confirmado.", status_code=409,
            )
        for source_use in source_uses:
            unit = session.scalar(
                select(ScmUnidadLogisticaInventario)
                .where(ScmUnidadLogisticaInventario.id == source_use.unidad_logistica_id)
                .with_for_update(of=ScmUnidadLogisticaInventario)
            )
            used_quantity = Decimal(source_use.cantidad_kg)
            if unit is None or Decimal(unit.cantidad_disponible_kg) < used_quantity:
                raise ScmServiceError(
                    "OPM_SOURCE_UNIT_BALANCE_CHANGED",
                    "Una bolsa origen ya no contiene los kg confirmados.", status_code=409,
                )
            unit.ubicacion_id = destination.ubicacion_id
            unit.cantidad_disponible_kg = Decimal(unit.cantidad_disponible_kg) - used_quantity
            if Decimal(unit.cantidad_disponible_kg) == ZERO:
                unit.estado = "CONSUMIDA"
            source_use.estado = "CONSUMIDA"
        reservation = emission.reserva
        destination.cantidad_fisica_kg = Decimal(destination.cantidad_fisica_kg) - quantity
        destination.cantidad_reservada_kg = Decimal(destination.cantidad_reservada_kg) - quantity
        destination.version += 1
        emission.cantidad_consumida_kg = Decimal(emission.cantidad_consumida_kg) + quantity
        reservation.emitida_neta_kg = Decimal(reservation.emitida_neta_kg) - quantity
        reservation.cantidad_consumida_kg = Decimal(reservation.cantidad_consumida_kg) + quantity
        contribution = ScmAportePreparacionMaterial(
            orden_preparacion_id=order.id,
            emision_id=emission.id,
            lectura_id=reading.id,
            peso_bruto_kg=reading.peso_bruto_kg,
            tara_kg=reading.tara_kg,
            peso_neto_kg=reading.peso_neto_kg,
            metodo=reading.metodo,
            evidencia_ref=reading.evidencia_ref,
            motivo=reason,
            estado="INCORPORADO",
            created_by_id=actor.id,
            confirmed_by_id=reading.aprobacion.actor_id,
            confirmed_at=utc_now(),
            operation_id=operation_id,
        )
        session.add(contribution)
        reading.estado = "UTILIZADA"
        reading.version += 1
        order.version += 1
        session.add(ScmMovimientoMaterialInventario(
            saldo_id=destination.id,
            tipo="CONSUMO",
            cantidad_delta_kg=-quantity,
            saldo_fisico_resultante_kg=destination.cantidad_fisica_kg,
            motivo=reason,
            referencia_tipo="APORTE_OPM",
            referencia_id=str(contribution.id),
            actor_id=actor.id,
            operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:APORTE"),
        ))
        session.flush()
        payload = _serialize_contribution(contribution)
        _event(
            session, aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id, event_type="PREPARATION_INPUT_INCORPORATED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload, status=201)
    except Exception:
        session.rollback()
        raise


def reconcile_preparation_order(session, *, actor_id, operation_id, order_id, data):
    try:
        reject_unknown_fields(
            data,
            allowed={"version", "perdida_kg", "muestra_kg", "remanente_equipo_kg", "motivo"},
        )
        actor = load_actor(session, actor_id, capability="OPM_CERRAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        loss = _nonnegative_kg(data.get("perdida_kg"), field="perdida_kg")
        sample = _nonnegative_kg(data.get("muestra_kg"), field="muestra_kg")
        remnant = _nonnegative_kg(data.get("remanente_equipo_kg"), field="remanente_equipo_kg")
        request_data = {
            "orden_preparacion_id": str(order_id),
            "version": data.get("version"),
            "perdida_kg": _kg(loss), "muestra_kg": _kg(sample),
            "remanente_equipo_kg": _kg(remnant), "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id, "POST /ordenes-preparacion-material/{id}/conciliar",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado not in ("EN_PREPARACION", "PENDIENTE_CONCILIACION"):
            raise ScmServiceError(
                "OPM_NOT_RECONCILABLE", "La OPM no admite conciliacion.", status_code=409
            )
        locked_readings = session.scalars(
            select(ScmLecturaPesoPreparacion)
            .where(ScmLecturaPesoPreparacion.orden_preparacion_id == order.id)
            .order_by(ScmLecturaPesoPreparacion.id)
            .with_for_update(of=ScmLecturaPesoPreparacion)
        ).all()
        if any(
            value.estado == "PENDIENTE_SEGUNDA_CONFIRMACION"
            for value in locked_readings
        ):
            raise ScmServiceError(
                "OPM_PENDING_WEIGHT_CONFIRMATIONS",
                "Confirma todas las lecturas antes de conciliar.",
                status_code=409,
            )
        inputs = sum((Decimal(value.peso_neto_kg) for value in order.aportes), ZERO)
        output_readings = [
            value for value in locked_readings
            if value.tipo_uso == "BOLSA_SALIDA" and value.estado == "APROBADA"
        ]
        if not output_readings:
            raise ScmServiceError(
                "OPM_OUTPUT_BAG_READINGS_REQUIRED",
                "Debe existir al menos una lectura aprobada para bolsa de salida.",
                status_code=409,
            )
        outputs = sum((Decimal(value.peso_neto_kg) for value in output_readings), ZERO)
        difference = (inputs - outputs - loss - sample - remnant).quantize(QTY)
        if difference != ZERO:
            raise ScmServiceError(
                "OPM_BALANCE_OUT_OF_TOLERANCE",
                "Entradas, bolsas y disposiciones no cierran a 0.001 kg.",
                status_code=409,
                details={
                    "entradas_kg": _kg(inputs), "bolsas_kg": _kg(outputs),
                    "perdida_kg": _kg(loss), "muestra_kg": _kg(sample),
                    "remanente_equipo_kg": _kg(remnant),
                    "diferencia_kg": _kg(difference),
                },
            )
        component_mismatches = []
        for requirement in order.requerimientos_insumo:
            actual = sum((
                Decimal(value.peso_neto_kg)
                for value in order.aportes
                if value.emision.reserva.requerimiento_id == requirement.id
            ), ZERO).quantize(QTY)
            planned = Decimal(requirement.cantidad_plan_kg).quantize(QTY)
            if actual != planned:
                component_mismatches.append({
                    "material_id": requirement.material_id,
                    "codigo": requirement.material.codigo,
                    "plan_kg": _kg(planned), "real_kg": _kg(actual),
                })
        if component_mismatches:
            raise ScmServiceError(
                "OPM_INPUT_COMPONENT_BALANCE_MISMATCH",
                "Los aportes no coinciden con cada input objetivo.", status_code=409,
                details={"componentes": component_mismatches},
            )
        order.perdida_kg = loss
        order.muestra_kg = sample
        order.remanente_equipo_kg = remnant
        order.estado = "PENDIENTE_CONCILIACION"
        order.version += 1
        session.flush()
        payload = _serialize_opm(order)
        _event(
            session, aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id, event_type="PREPARATION_ORDER_RECONCILED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def close_preparation_order(session, *, actor_id, operation_id, order_id, data):
    try:
        reject_unknown_fields(data, allowed={
            "version", "motivo", "flujo_salida", "ubicacion_preparacion_id",
        })
        actor = load_actor(session, actor_id, capability="OPM_CERRAR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        output_flow = str(data.get("flujo_salida") or "ALMACEN").strip().upper()
        if output_flow not in {"ALMACEN", "DIRECTO_MAQUINA"}:
            raise ScmServiceError(
                "INVALID_PREPARED_OUTPUT_FLOW",
                "flujo_salida debe ser ALMACEN o DIRECTO_MAQUINA.", status_code=422,
            )
        preparation_location_id = data.get("ubicacion_preparacion_id")
        if output_flow == "DIRECTO_MAQUINA" and not isinstance(preparation_location_id, int):
            raise ScmServiceError(
                "PREPARATION_LOCATION_REQUIRED",
                "El flujo directo requiere la ubicación física de Preparación.", status_code=422,
            )
        request_data = {
            "orden_preparacion_id": str(order_id),
            "version": data.get("version"), "motivo": reason,
            "flujo_salida": output_flow,
            "ubicacion_preparacion_id": preparation_location_id,
        }
        operation, replay = _reserve_operation(
            session, operation_id, "POST /ordenes-preparacion-material/{id}/cerrar",
            actor, request_data,
        )
        if replay is not None:
            return replay
        order = _load_opm(session, order_id, lock=True)
        _require_version(order, data.get("version"))
        if order.estado != "PENDIENTE_CONCILIACION" or order.lote is not None:
            raise ScmServiceError(
                "OPM_NOT_READY_TO_CLOSE",
                "La OPM debe estar conciliada y sin LMP previo.", status_code=409,
            )
        preparation_location = None
        preparation_balance = None
        if output_flow == "DIRECTO_MAQUINA":
            preparation_location = _canonical_location(
                session, preparation_location_id, actor_id=actor.id,
                require_assignment=True, article_class="MATERIAL_PREPARADO",
            )
            if preparation_location.tipo != "STAGING":
                raise ScmServiceError(
                    "PREPARATION_STAGING_REQUIRED",
                    "El nacimiento directo exige una ubicación STAGING de Preparación.",
                    status_code=422,
                )
            preparation_balance = _prepared_balance(
                session, recipe_id=order.receta_revision_id,
                location_id=preparation_location.id,
            )
        all_readings = session.scalars(
            select(ScmLecturaPesoPreparacion)
            .where(
                ScmLecturaPesoPreparacion.orden_preparacion_id == order.id,
            )
            .order_by(ScmLecturaPesoPreparacion.id)
            .with_for_update(of=ScmLecturaPesoPreparacion)
        ).all()
        if any(
            value.tipo_uso == "BOLSA_SALIDA"
            and value.estado == "PENDIENTE_SEGUNDA_CONFIRMACION"
            for value in all_readings
        ):
            raise ScmServiceError(
                "OPM_PENDING_OUTPUT_CONFIRMATIONS",
                "Todas las bolsas registradas deben confirmarse o invalidarse antes de cerrar.",
                status_code=409,
            )
        readings = [
            value for value in all_readings
            if value.tipo_uso == "BOLSA_SALIDA" and value.estado == "APROBADA"
        ]
        if not readings:
            raise ScmServiceError(
                "OPM_OUTPUT_BAG_READINGS_REQUIRED",
                "No hay lecturas aprobadas para crear bolsas.", status_code=409,
            )
        output_assignments = {
            value.id: value
            for value in _lock_opm_assignments_and_requirements(session, order.id)
            if value.tipo_fuente == "OPM_ESPERADA"
            and value.estado in ("COMPROMETIDA", "LIBERADA")
        }
        if any(
            reading.asignacion_requerimiento_id not in output_assignments
            for reading in readings
        ):
            raise ScmServiceError(
                "OPM_OUTPUT_ASSIGNMENT_REQUIRED",
                "Cada lectura de bolsa debe conservar una necesidad activa.",
                status_code=409,
            )
        for assignment_id in {
            reading.asignacion_requerimiento_id for reading in readings
        }:
            _validate_output_assignment_capacity(
                session, assignment=output_assignments[assignment_id]
            )
        contributions = session.scalars(
            select(ScmAportePreparacionMaterial)
            .where(ScmAportePreparacionMaterial.orden_preparacion_id == order.id)
            .order_by(ScmAportePreparacionMaterial.id)
            .with_for_update(of=ScmAportePreparacionMaterial)
        ).all()
        inputs = sum((Decimal(value.peso_neto_kg) for value in contributions), ZERO)
        outputs = sum((Decimal(value.peso_neto_kg) for value in readings), ZERO)
        declared = (
            Decimal(order.perdida_kg or 0)
            + Decimal(order.muestra_kg or 0)
            + Decimal(order.remanente_equipo_kg or 0)
        )
        difference = (inputs - outputs - declared).quantize(QTY)
        if difference != ZERO:
            raise ScmServiceError(
                "OPM_BALANCE_CHANGED_AFTER_RECONCILIATION",
                "El balance cambio despues de conciliar; debe conciliarse nuevamente.",
                status_code=409,
                details={
                    "entradas_kg": _kg(inputs), "salidas_kg": _kg(outputs),
                    "declarado_kg": _kg(declared),
                    "diferencia_kg": _kg(difference),
                },
            )
        lot_id = uuid4()
        lot = ScmLoteMaterialPreparado(
            id=lot_id,
            codigo=f"LMP-{str(lot_id)[:8].upper()}",
            orden_preparacion_id=order.id,
            receta_revision_id=order.receta_revision_id,
            cantidad_kg=sum((Decimal(value.peso_neto_kg) for value in readings), ZERO),
            estado="PENDIENTE_RECEPCION",
            created_by_id=actor.id,
        )
        session.add(lot)
        session.flush()
        bags = []
        for sequence, reading in enumerate(readings, start=1):
            bag = ScmBolsaMaterialPreparado(
                codigo=f"BMP-{str(lot.id)[:8].upper()}-{sequence:03d}",
                orden_preparacion_id=order.id,
                lote_id=lot.id,
                lectura_id=reading.id,
                asignacion_requerimiento_id=(
                    reading.asignacion_requerimiento_id
                ),
                secuencia=sequence,
                peso_bruto_kg=reading.peso_bruto_kg,
                tara_kg=reading.tara_kg,
                peso_neto_kg=reading.peso_neto_kg,
                metodo=reading.metodo,
                evidencia_ref=reading.evidencia_ref,
                motivo=reading.motivo,
                estado="PENDIENTE_RECEPCION",
                created_by_id=reading.created_by_id,
                confirmed_by_id=reading.aprobacion.actor_id,
                operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:BAG:{reading.id}"),
                confirmed_at=reading.aprobacion.created_at,
            )
            session.add(bag)
            if output_flow == "DIRECTO_MAQUINA":
                session.flush()
                quantity = Decimal(reading.peso_neto_kg)
                bag.ubicacion_id = preparation_location.id
                bag.estado = "PENDIENTE_CALIDAD"
                _move_prepared_balance(
                    session, balance=preparation_balance, bag=bag,
                    movement_type="RECEPCION",
                    physical_delta=quantity, unavailable_delta=quantity,
                    reason="Nacimiento físico en Preparación: " + reason,
                    actor_id=actor.id,
                    operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:NACIMIENTO:{reading.id}"),
                )
            bags.append(bag)
            reading.estado = "UTILIZADA"
            reading.version += 1
        session.flush()
        _refresh_opm_commitments_from_bag_links(
            session=session,
            order=order,
            actor_id=actor.id,
            reason="Ajuste de cobertura por bolsas completas cerradas: " + reason,
        )
        order.estado = "CERRADA"
        order.closed_by_id = actor.id
        order.closed_at = utc_now()
        order.version += 1
        if output_flow == "DIRECTO_MAQUINA":
            lot.estado = "PENDIENTE_CALIDAD"
            lot.version += 1
        session.flush()
        payload = {
            "orden_preparacion": _serialize_opm(order),
            "lote": _serialize_lot(lot),
            "kardex_acreditado": False,
            "flujo_salida": output_flow,
            "requiere_recepcion_almacen": output_flow == "ALMACEN",
        }
        _event(
            session, aggregate_type="ORDEN_PREPARACION_MATERIAL",
            aggregate_id=order.id, event_type="PREPARATION_ORDER_CLOSED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload, status=201)
    except Exception:
        session.rollback()
        raise


def _canonical_location(
    session, location_id, *, actor_id=None, require_assignment=False,
    article_class=None,
):
    location = session.scalar(
        select(ScmUbicacionInventario)
        .where(ScmUbicacionInventario.id == location_id)
        .with_for_update(of=ScmUbicacionInventario)
    )
    if (
        location is None or not location.activo or location.almacen_id is None
        or location.almacen is None or not location.almacen.activo
    ):
        raise ScmServiceError(
            "CANONICAL_LOCATION_REQUIRED",
            "La ubicacion debe pertenecer a un almacen activo configurado.",
            status_code=422,
        )
    if require_assignment:
        worker_assignment = session.scalar(select(ScmAlmacenTrabajador).where(
            ScmAlmacenTrabajador.almacen_id == location.almacen_id,
            ScmAlmacenTrabajador.trabajador_id == actor_id,
            ScmAlmacenTrabajador.activo.is_(True),
        ))
        if worker_assignment is None:
            raise ScmServiceError(
                "WAREHOUSE_OPERATION_SCOPE_REQUIRED",
                "El actor no esta asignado al almacen de la ubicacion.",
                status_code=403,
                details={"ubicacion_id": location.id},
            )
        worker_classes = set(worker_assignment.clases_articulo_json or [])
        if article_class and article_class not in worker_classes:
            raise ScmServiceError(
                "WAREHOUSE_ARTICLE_CLASS_SCOPE_REQUIRED",
                "La asignacion del actor no incluye esta clase de articulo.",
                status_code=403,
                details={
                    "ubicacion_id": location.id,
                    "clase": article_class,
                    "clases_asignadas": sorted(worker_classes),
                },
            )
    allowed_classes = set(location.clases_articulo_json or [])
    if article_class and allowed_classes and article_class not in allowed_classes:
        raise ScmServiceError(
            "LOCATION_ARTICLE_CLASS_NOT_ALLOWED",
            "La ubicacion no admite la clase de articulo solicitada.",
            status_code=422,
            details={
                "ubicacion_id": location.id,
                "clase": article_class,
            },
        )
    return location


def _prepared_balance(session, *, recipe_id, location_id):
    balance = session.scalar(
        select(ScmSaldoMaterialPreparado)
        .where(
            ScmSaldoMaterialPreparado.receta_revision_id == recipe_id,
            ScmSaldoMaterialPreparado.ubicacion_id == location_id,
        )
        .with_for_update(of=ScmSaldoMaterialPreparado)
    )
    if balance is None:
        balance = ScmSaldoMaterialPreparado(
            receta_revision_id=recipe_id,
            ubicacion_id=location_id,
        )
        session.add(balance)
        session.flush()
    return balance


def _move_prepared_balance(
    session, *, balance, bag, movement_type, physical_delta=ZERO,
    reserved_delta=ZERO, unavailable_delta=ZERO, reason, actor_id, operation_id,
):
    physical = Decimal(balance.cantidad_fisica_kg) + physical_delta
    reserved = Decimal(balance.cantidad_reservada_kg) + reserved_delta
    unavailable = Decimal(balance.cantidad_no_disponible_kg) + unavailable_delta
    if (
        physical < 0 or reserved < 0 or unavailable < 0
        or reserved + unavailable > physical
    ):
        raise ScmServiceError(
            "PREPARED_MATERIAL_BALANCE_INCONSISTENT",
            "El movimiento produciria un saldo preparado inconsistente.",
            status_code=409,
            details={
                "saldo_id": str(balance.id), "bolsa_id": str(bag.id),
                "fisico_resultante_kg": _kg(physical),
                "reservado_resultante_kg": _kg(reserved),
                "no_disponible_resultante_kg": _kg(unavailable),
            },
        )
    balance.cantidad_fisica_kg = physical
    balance.cantidad_reservada_kg = reserved
    balance.cantidad_no_disponible_kg = unavailable
    balance.version += 1
    movement = ScmMovimientoMaterialPreparado(
        saldo_id=balance.id,
        bolsa_id=bag.id,
        tipo=movement_type,
        delta_fisico_kg=physical_delta,
        delta_reservado_kg=reserved_delta,
        delta_no_disponible_kg=unavailable_delta,
        saldo_fisico_resultante_kg=physical,
        saldo_reservado_resultante_kg=reserved,
        saldo_no_disponible_resultante_kg=unavailable,
        motivo=reason,
        actor_id=actor_id,
        operation_id=operation_id,
    )
    session.add(movement)
    return movement


def receive_prepared_bag(
    session, *, actor_id, operation_id, lot_id, bag_id, data,
):
    try:
        reject_unknown_fields(data, allowed={"ubicacion_id", "motivo"})
        actor = load_actor(session, actor_id, capability="MATERIAL_PREPARADO_RECIBIR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        location_id = data.get("ubicacion_id")
        if not isinstance(location_id, int) or isinstance(location_id, bool):
            raise ScmServiceError(
                "CANONICAL_LOCATION_REQUIRED", "ubicacion_id debe ser un entero.",
                status_code=422,
            )
        request_data = {
            "lote_id": str(lot_id), "bolsa_id": str(bag_id),
            "ubicacion_id": location_id, "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /lotes-material-preparado/{id}/bolsas/{unidad_id}/recibir",
            actor, request_data,
        )
        if replay is not None:
            return replay
        lot_preview = session.get(ScmLoteMaterialPreparado, lot_id)
        bag_preview = session.get(ScmBolsaMaterialPreparado, bag_id)
        if (
            lot_preview is None or bag_preview is None
            or bag_preview.lote_id != lot_preview.id
        ):
            raise ScmServiceError(
                "PREPARED_MATERIAL_BAG_NOT_FOUND", "La bolsa no existe en el lote.",
                status_code=404,
            )
        # Global physical lock order: requirements/assignments, locations,
        # balances, lots/bags, then receipts/reservations/deliveries.
        _lock_opm_assignments_and_requirements(
            session, lot_preview.orden_preparacion_id
        )
        location = _canonical_location(
            session, location_id, actor_id=actor.id, require_assignment=True,
            article_class="MATERIAL_PREPARADO",
        )
        balance = _prepared_balance(
            session,
            recipe_id=lot_preview.receta_revision_id,
            location_id=location.id,
        )
        lot = session.scalar(
            select(ScmLoteMaterialPreparado)
            .where(ScmLoteMaterialPreparado.id == lot_id)
            .with_for_update(of=ScmLoteMaterialPreparado)
        )
        bag = session.scalar(
            select(ScmBolsaMaterialPreparado)
            .where(ScmBolsaMaterialPreparado.id == bag_id)
            .with_for_update(of=ScmBolsaMaterialPreparado)
        )
        if bag.recepcion is not None:
            if bag.recepcion.ubicacion_id != location.id:
                raise ScmServiceError(
                    "PREPARED_BAG_ALREADY_RECEIVED",
                    "La bolsa ya fue recibida en otra ubicacion.", status_code=409,
                )
            payload = {
                "bolsa": _serialize_bag(bag),
                "lote": _serialize_lot(lot, include_bags=False),
                "saldo": _serialize_balance(balance),
            }
            return _complete(session, operation, payload)
        if bag.estado != "PENDIENTE_RECEPCION":
            raise ScmServiceError(
                "PREPARED_BAG_NOT_PENDING_RECEIPT",
                "La bolsa no espera recepcion.", status_code=409,
            )
        quantity = Decimal(bag.peso_neto_kg)
        receipt = ScmRecepcionBolsaMaterialPreparado(
            bolsa=bag, ubicacion=location,
            motivo=reason, actor_id=actor.id, operation_id=operation_id,
        )
        session.add(receipt)
        bag.ubicacion_id = location.id
        bag.estado = "PENDIENTE_CALIDAD"
        bag.version += 1
        _move_prepared_balance(
            session, balance=balance, bag=bag, movement_type="RECEPCION",
            physical_delta=quantity, unavailable_delta=quantity,
            reason=reason, actor_id=actor.id,
            operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:RECEPCION"),
        )
        session.flush()
        if all(value.estado != "PENDIENTE_RECEPCION" for value in lot.bolsas):
            lot.estado = "PENDIENTE_CALIDAD"
            lot.version += 1
        session.flush()
        payload = {
            "bolsa": _serialize_bag(bag),
            "lote": _serialize_lot(lot, include_bags=False),
            "saldo": _serialize_balance(balance),
        }
        _event(
            session, aggregate_type="LOTE_MATERIAL_PREPARADO",
            aggregate_id=lot.id, event_type="PREPARED_BAG_RECEIVED",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def _lock_opm_assignments_and_requirements(session, order_id):
    requirement_ids = sorted(
        set(session.scalars(
            select(ScmAsignacionRequerimientoPreparacion.requerimiento_id)
            .where(
                ScmAsignacionRequerimientoPreparacion.orden_preparacion_id
                == order_id
            )
        ).all()),
        key=str,
    )
    if requirement_ids:
        session.scalars(
            select(ScmRequerimientoMaterialPreparado)
            .where(
                ScmRequerimientoMaterialPreparado.id.in_(requirement_ids)
            )
            .order_by(ScmRequerimientoMaterialPreparado.id)
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        ).unique().all()
    return session.scalars(
        select(ScmAsignacionRequerimientoPreparacion)
        .where(
            ScmAsignacionRequerimientoPreparacion.orden_preparacion_id
            == order_id
        )
        .order_by(ScmAsignacionRequerimientoPreparacion.id)
        .with_for_update(of=ScmAsignacionRequerimientoPreparacion)
    ).unique().all()


def _refresh_opm_commitments_from_bag_links(
    *, session, order, actor_id, reason,
):
    assignments = [
        value
        for value in _lock_opm_assignments_and_requirements(session, order.id)
        if value.tipo_fuente == "OPM_ESPERADA"
        and value.estado in ("COMPROMETIDA", "LIBERADA")
    ]
    if not assignments:
        return
    assignment_ids = [value.id for value in assignments]
    linked_bags = session.scalars(
        select(ScmBolsaMaterialPreparado)
        .where(
            ScmBolsaMaterialPreparado.orden_preparacion_id == order.id,
            ScmBolsaMaterialPreparado.asignacion_requerimiento_id.in_(
                assignment_ids
            ),
        )
        .order_by(ScmBolsaMaterialPreparado.id)
        .with_for_update(of=ScmBolsaMaterialPreparado)
    ).all()
    quantities = {value.id: ZERO for value in assignments}
    for bag in linked_bags:
        if bag.estado != "RECHAZADA":
            quantities[bag.asignacion_requerimiento_id] += Decimal(
                bag.peso_neto_kg
            )
    changed_requirements = set()
    for assignment in assignments:
        target = quantities[assignment.id].quantize(QTY)
        planned = Decimal(assignment.cantidad_planificada_kg)
        consumed = Decimal(assignment.cantidad_consumida_kg)
        if target > planned or consumed > target:
            raise ScmServiceError(
                "OPM_WHOLE_BAG_ASSIGNMENT_INCONSISTENT",
                "Las bolsas vinculadas exceden el compromiso de su requerimiento.",
                status_code=409,
                details={
                    "asignacion_id": str(assignment.id),
                    "planificada_kg": _kg(planned),
                    "bolsas_vinculadas_kg": _kg(target),
                    "consumida_kg": _kg(consumed),
                },
            )
        prior = Decimal(assignment.cantidad_comprometida_kg)
        if prior == target:
            continue
        assignment.cantidad_comprometida_kg = target
        assignment.estado = (
            "LIBERADA"
            if target == ZERO
            else "SATISFECHA" if consumed == target else "COMPROMETIDA"
        )
        if target < prior:
            assignment.released_by_id = actor_id
            assignment.motivo_liberacion = reason
            assignment.released_at = utc_now()
        changed_requirements.add(assignment.requerimiento)
    for requirement in changed_requirements:
        _refresh_requirement_state(requirement)
        requirement.version += 1


def decide_prepared_bag_quality(
    session, *, actor_id, operation_id, lot_id, bag_id, data,
):
    try:
        reject_unknown_fields(data, allowed={"decision", "motivo"})
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_CALIDAD_RESOLVER"
        )
        decision = str(data.get("decision") or "").strip().upper()
        if decision not in ("LIBERAR", "BLOQUEAR", "RECHAZAR"):
            raise ScmServiceError(
                "INVALID_PREPARED_MATERIAL_QUALITY_DECISION",
                "decision debe ser LIBERAR, BLOQUEAR o RECHAZAR.", status_code=422,
            )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "lote_id": str(lot_id), "bolsa_id": str(bag_id),
            "decision": decision, "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /lotes-material-preparado/{id}/bolsas/{unidad_id}/calidad",
            actor, request_data,
        )
        if replay is not None:
            return replay
        lot_preview = session.get(ScmLoteMaterialPreparado, lot_id)
        if lot_preview is None:
            raise ScmServiceError(
                "PREPARED_MATERIAL_BAG_NOT_FOUND",
                "El lote de material preparado no existe.",
                status_code=404,
            )
        bag_preview = session.get(ScmBolsaMaterialPreparado, bag_id)
        if bag_preview is None or bag_preview.lote_id != lot_preview.id:
            raise ScmServiceError(
                "PREPARED_MATERIAL_BAG_NOT_FOUND",
                "La bolsa no existe en el lote.",
                status_code=404,
            )
        # Mismo orden global que reserva de stock: requerimientos/asignaciones
        # antes de ubicacion, saldo, lote y bolsa.
        _lock_opm_assignments_and_requirements(
            session, lot_preview.orden_preparacion_id
        )
        if bag_preview.ubicacion_id is None:
            raise ScmServiceError(
                "PREPARED_BAG_RECEIPT_REQUIRED",
                "Calidad solo puede resolver una bolsa ya recibida.",
                status_code=409,
            )
        location = _canonical_location(
            session,
            bag_preview.ubicacion_id,
            article_class="MATERIAL_PREPARADO",
        )
        balance = _prepared_balance(
            session,
            recipe_id=lot_preview.receta_revision_id,
            location_id=location.id,
        )
        lot = session.scalar(
            select(ScmLoteMaterialPreparado)
            .where(ScmLoteMaterialPreparado.id == lot_id)
            .with_for_update(of=ScmLoteMaterialPreparado)
        )
        bag = session.scalar(
            select(ScmBolsaMaterialPreparado)
            .where(ScmBolsaMaterialPreparado.id == bag_id)
            .with_for_update(of=ScmBolsaMaterialPreparado)
        )
        if lot is None or bag is None or bag.lote_id != lot.id:
            raise ScmServiceError(
                "PREPARED_MATERIAL_BAG_NOT_FOUND", "La bolsa no existe en el lote.",
                status_code=404,
            )
        execution_participants = {
            value for value in (
                lot.orden.created_by_id,
                lot.orden.released_by_id,
                lot.orden.started_by_id,
                lot.orden.closed_by_id,
                *(reading.created_by_id for reading in lot.orden.lecturas),
                *(contribution.created_by_id for contribution in lot.orden.aportes),
                *(
                    contribution.confirmed_by_id
                    for contribution in lot.orden.aportes
                ),
                *(value.confirmed_by_id for value in lot.orden.bolsas),
                *(
                    value.recepcion.actor_id
                    for value in lot.orden.bolsas
                    if value.recepcion is not None
                ),
            )
            if value is not None
        }
        if actor.id in execution_participants:
            raise ScmServiceError(
                "QUALITY_SEGREGATION_REQUIRED",
                "Quien planifico, preparo, confirmo peso o cerro la OPM no puede resolver Calidad.",
                status_code=403,
                details={"participantes_ejecucion": sorted(execution_participants)},
            )
        existing = bag.decision_calidad
        if existing is not None:
            if existing.decision != decision:
                raise ScmServiceError(
                    "PREPARED_BAG_QUALITY_ALREADY_DECIDED",
                    "La bolsa ya posee otra decision de Calidad.", status_code=409,
                )
            payload = {
                "bolsa": _serialize_bag(bag),
                "lote": _serialize_lot(lot, include_bags=False),
                "saldo": _serialize_balance(balance),
            }
            return _complete(session, operation, payload)
        if bag.estado != "PENDIENTE_CALIDAD" or bag.ubicacion_id is None:
            raise ScmServiceError(
                "PREPARED_BAG_RECEIPT_REQUIRED",
                "Calidad solo puede resolver una bolsa ya recibida.", status_code=409,
            )
        quantity = Decimal(bag.peso_neto_kg)
        if decision == "LIBERAR":
            if not bag.ubicacion.permite_saldo_libre:
                raise ScmServiceError(
                    "LOCATION_DOES_NOT_ALLOW_FREE_STOCK",
                    "La ubicacion no permite saldo libre; traslade o reciba en una zona valida.",
                    status_code=409,
                )
            _move_prepared_balance(
                session, balance=balance, bag=bag,
                movement_type="LIBERACION_CALIDAD",
                unavailable_delta=-quantity, reason=reason, actor_id=actor.id,
                operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:CALIDAD"),
            )
            bag.estado = "DISPONIBLE"
        elif decision == "BLOQUEAR":
            bag.estado = "BLOQUEADA"
        else:
            bag.estado = "RECHAZADA"
        bag.version += 1
        quality = ScmDecisionCalidadMaterialPreparado(
            lote=lot, bolsa=bag, decision=decision,
            motivo=reason, actor_id=actor.id, operation_id=operation_id,
        )
        session.add(quality)
        session.flush()
        states = {value.estado for value in lot.bolsas}
        _refresh_opm_commitments_from_bag_links(
            session=session,
            order=lot.orden,
            actor_id=actor.id,
            reason=reason,
        )
        if states and states <= {"DISPONIBLE", "RESERVADA", "EMITIDA", "CONSUMIDA", "DEVUELTA"}:
            lot.estado = "DISPONIBLE"
        elif "RECHAZADA" in states:
            lot.estado = "RECHAZADO"
        elif "BLOQUEADA" in states:
            lot.estado = "BLOQUEADO"
        else:
            lot.estado = "PENDIENTE_CALIDAD"
        lot.version += 1
        session.flush()
        payload = {
            "bolsa": _serialize_bag(bag),
            "lote": _serialize_lot(lot, include_bags=False),
            "saldo": _serialize_balance(balance),
        }
        _event(
            session, aggregate_type="LOTE_MATERIAL_PREPARADO",
            aggregate_id=lot.id, event_type=f"PREPARED_BAG_QUALITY_{decision}",
            actor=actor, operation=operation, reason=reason, after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def get_preparation_order(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="OPM_VER")
    order = session.get(ScmOrdenPreparacionMaterial, order_id)
    if order is None:
        raise ScmServiceError(
            "PREPARATION_ORDER_NOT_FOUND", "La OPM no existe.", status_code=404
        )
    return _serialize_opm(order)


def get_prepared_material_lot(session, *, actor_id, lot_id):
    load_actor(session, actor_id, capability="MATERIAL_PREPARADO_GENEALOGIA_VER")
    lot = session.get(ScmLoteMaterialPreparado, lot_id)
    if lot is None:
        raise ScmServiceError(
            "PREPARED_MATERIAL_LOT_NOT_FOUND", "El lote no existe.", status_code=404
        )
    payload = _serialize_lot(lot)
    bag_ids = [value.id for value in lot.bolsas]
    linked_assignment_ids = [
        value.asignacion_requerimiento_id
        for value in lot.bolsas
        if value.asignacion_requerimiento_id is not None
    ]
    assignment_filters = []
    if bag_ids:
        assignment_filters.append(
            ScmAsignacionRequerimientoPreparacion.bolsa_id.in_(bag_ids)
        )
    if linked_assignment_ids:
        assignment_filters.append(
            ScmAsignacionRequerimientoPreparacion.id.in_(
                linked_assignment_ids
            )
        )
    assignments = (
        session.scalars(
            select(ScmAsignacionRequerimientoPreparacion)
            .where(or_(*assignment_filters))
            .order_by(ScmAsignacionRequerimientoPreparacion.created_at)
        ).unique().all()
        if assignment_filters else []
    )
    assignment_by_id = {value.id: value for value in assignments}
    assignments_by_stock_bag = {
        value.bolsa_id: value
        for value in assignments
        if value.bolsa_id is not None
    }
    movements = (
        session.scalars(
            select(ScmMovimientoMaterialPreparado)
            .where(ScmMovimientoMaterialPreparado.bolsa_id.in_(bag_ids))
            .order_by(
                ScmMovimientoMaterialPreparado.created_at,
                ScmMovimientoMaterialPreparado.id,
            )
        ).unique().all()
        if bag_ids else []
    )
    movements_by_bag = {value: [] for value in bag_ids}
    for movement in movements:
        movements_by_bag.setdefault(movement.bolsa_id, []).append(movement)
    bag_genealogy = []
    for bag in lot.bolsas:
        assignment = (
            assignment_by_id.get(bag.asignacion_requerimiento_id)
            or assignments_by_stock_bag.get(bag.id)
        )
        reservation_rows = sorted(
            bag.reservas, key=lambda value: (value.created_at, str(value.id))
        )
        bag_genealogy.append({
            "bolsa": _serialize_bag(bag),
            "asignacion": (
                _serialize_assignment(assignment)
                if assignment is not None else None
            ),
            "reservas_y_entregas": [
                {
                    "reserva": _serialize_prepared_reservation(reservation),
                    "trabajo_color": {
                        "id": str(reservation.trabajo_ot_id),
                        "codigo": reservation.trabajo.codigo,
                        "estado": reservation.trabajo.estado,
                        "corrida_fabricacion_id": (
                            str(
                                reservation.trabajo.trabajo_color
                                .corrida_fabricacion_id
                            )
                            if reservation.trabajo.trabajo_color is not None
                            else None
                        ),
                        "corrida_codigo": (
                            reservation.trabajo.trabajo_color.corrida.codigo
                            if reservation.trabajo.trabajo_color is not None
                            else None
                        ),
                    },
                    "recepcion_maquina": (
                        {
                            "actor_id": reservation.emision.received_by_id,
                            "at": _iso(reservation.emision.received_at),
                        }
                        if reservation.emision is not None
                        and reservation.emision.received_at is not None
                        else None
                    ),
                    "resultado": (
                        "CONSUMO"
                        if reservation.estado == "CONSUMIDA"
                        else "RETORNO"
                        if reservation.estado == "DEVUELTA"
                        or (
                            reservation.emision is not None
                            and reservation.emision.estado
                            == "RETORNADA_TOTAL"
                        )
                        else "LIBERACION"
                        if reservation.estado in ("LIBERADA", "CANCELADA")
                        else "EN_CUSTODIA"
                    ),
                }
                for reservation in reservation_rows
            ],
            "movimientos": [
                _serialize_prepared_movement(value)
                for value in movements_by_bag.get(bag.id, [])
            ],
        })
    payload["genealogia"] = {
        "orden_preparacion": _serialize_opm(lot.orden, detail=False),
        "entradas": {
            "requerimientos_insumo": [
                _serialize_raw_requirement(value)
                for value in lot.orden.requerimientos_insumo
            ],
            "aportes": [
                _serialize_contribution(value) for value in lot.orden.aportes
            ],
        },
        "bolsas": bag_genealogy,
        "movimientos": [
            _serialize_prepared_movement(value) for value in movements
        ],
    }
    return payload


def _created_page(statement, model, *, limit, cursor):
    parsed_limit = _page_limit(limit)
    parsed_cursor = _decode_cursor(cursor)
    if parsed_cursor is not None:
        cursor_at, cursor_id = parsed_cursor
        statement = statement.where(or_(
            model.created_at < cursor_at,
            and_(model.created_at == cursor_at, model.id < cursor_id),
        ))
    return statement.order_by(model.created_at.desc(), model.id.desc()).limit(
        parsed_limit + 1
    ), parsed_limit


def _page_payload(rows, *, limit, serializer):
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = (
        _encode_cursor(visible[-1].created_at, visible[-1].id)
        if has_more and visible else None
    )
    return {
        "items": [serializer(value) for value in visible],
        "limit": limit,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def _prepared_requirement_metrics_subquery():
    assignment = ScmAsignacionRequerimientoPreparacion
    active = assignment.estado.in_(tuple(ACTIVE_ASSIGNMENT_STATES))

    def planned_for(source):
        return func.coalesce(func.sum(case(
            (
                and_(
                    active,
                    assignment.tipo_fuente == source,
                    assignment.estado == "PLANIFICADA",
                ),
                assignment.cantidad_planificada_kg,
            ),
            (
                and_(active, assignment.tipo_fuente == source),
                assignment.cantidad_comprometida_kg,
            ),
            else_=ZERO,
        )), ZERO)

    def committed_for(source):
        return func.coalesce(func.sum(case(
            (
                and_(active, assignment.tipo_fuente == source),
                assignment.cantidad_comprometida_kg,
            ),
            else_=ZERO,
        )), ZERO)

    return (
        select(
            assignment.requerimiento_id.label("requerimiento_id"),
            planned_for("LOTE_PREPARADO_STOCK").label(
                "stock_planificado_kg"
            ),
            planned_for("OPM_ESPERADA").label("opm_planificada_kg"),
            committed_for("LOTE_PREPARADO_STOCK").label(
                "stock_comprometido_kg"
            ),
            committed_for("OPM_ESPERADA").label(
                "opm_comprometida_kg"
            ),
            func.coalesce(func.sum(case(
                (active, assignment.cantidad_consumida_kg),
                else_=ZERO,
            )), ZERO).label("consumida_kg"),
        )
        .group_by(assignment.requerimiento_id)
        .subquery()
    )


def list_prepared_requirements(
    session, *, actor_id, limit=25, cursor=None, state=None,
):
    load_actor(session, actor_id, capability="OPM_VER")
    allowed_states = {
        "PENDIENTE", "CUBIERTA_PARCIAL", "CUBIERTA", "SATISFECHA",
        "CANCELADA",
    }
    normalized_state = str(state or "").strip().upper() or None
    if normalized_state is not None and normalized_state not in allowed_states:
        raise ScmServiceError(
            "INVALID_PREPARED_REQUIREMENT_STATE",
            "estado no es valido para requerimientos de preparacion.",
            status_code=422,
        )
    metrics = _prepared_requirement_metrics_subquery()
    statement = (
        select(ScmRequerimientoMaterialPreparado, metrics)
        .outerjoin(
            metrics,
            metrics.c.requerimiento_id
            == ScmRequerimientoMaterialPreparado.id,
        )
        .options(
            selectinload(ScmRequerimientoMaterialPreparado.receta_revision),
            selectinload(ScmRequerimientoMaterialPreparado.corrida)
            .selectinload(ScmCorridaFabricacion.trabajos_color)
            .selectinload(ScmTrabajoColor.trabajo),
            selectinload(ScmRequerimientoMaterialPreparado.corrida)
            .selectinload(ScmCorridaFabricacion.orden_fabricacion),
        )
    )
    if normalized_state is not None:
        statement = statement.where(
            ScmRequerimientoMaterialPreparado.estado == normalized_state
        )
    statement, parsed_limit = _created_page(
        statement, ScmRequerimientoMaterialPreparado,
        limit=limit, cursor=cursor,
    )
    rows = session.execute(statement).unique().all()
    has_more = len(rows) > parsed_limit
    visible = rows[:parsed_limit]
    next_cursor = (
        _encode_cursor(visible[-1][0].created_at, visible[-1][0].id)
        if has_more and visible else None
    )
    return {
        "items": [
            _serialize_prepared_requirement(
                row[0],
                detail=False,
                metrics={
                    "stock_planificado_kg": row.stock_planificado_kg,
                    "opm_planificada_kg": row.opm_planificada_kg,
                    "stock_comprometido_kg": row.stock_comprometido_kg,
                    "opm_comprometida_kg": row.opm_comprometida_kg,
                    "consumida_kg": row.consumida_kg,
                },
            )
            for row in visible
        ],
        "limit": parsed_limit,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def list_compatible_prepared_stock(
    session, *, actor_id, requirement_id, limit=25, cursor=None,
):
    load_actor(session, actor_id, capability="MATERIAL_PREPARADO_RESERVAR")
    requirement = session.get(ScmRequerimientoMaterialPreparado, requirement_id)
    if requirement is None:
        raise ScmServiceError(
            "PREPARED_REQUIREMENT_NOT_FOUND",
            "El requerimiento de material preparado no existe.",
            status_code=404,
        )
    remaining = max(
        Decimal(requirement.cantidad_requerida_kg)
        - _planned_quantity(requirement),
        ZERO,
    ).quantize(QTY)
    statement = _compatible_prepared_stock_statement(
        requirement, max_quantity=remaining,
    ).options(
        selectinload(ScmBolsaMaterialPreparado.lote),
        selectinload(ScmBolsaMaterialPreparado.ubicacion),
    )
    statement, parsed_limit = _created_page(
        statement, ScmBolsaMaterialPreparado,
        limit=limit, cursor=cursor,
    )
    rows = session.scalars(statement).unique().all()
    payload = _page_payload(
        rows,
        limit=parsed_limit,
        serializer=_serialize_compatible_stock_bag,
    )
    payload["requerimiento"] = _serialize_prepared_requirement(
        requirement, detail=False,
    )
    return payload


def list_preparation_orders(
    session, *, actor_id, limit=25, cursor=None, state=None,
):
    load_actor(session, actor_id, capability="OPM_VER")
    allowed_states = {
        "BORRADOR", "LIBERADA", "EN_PREPARACION", "PENDIENTE_CONCILIACION",
        "CERRADA", "ANULADA",
    }
    normalized_state = str(state or "").strip().upper() or None
    if normalized_state is not None and normalized_state not in allowed_states:
        raise ScmServiceError(
            "INVALID_PREPARATION_ORDER_STATE",
            "estado no es valido para ordenes de preparacion.",
            status_code=422,
        )
    statement = select(ScmOrdenPreparacionMaterial).options(
        selectinload(ScmOrdenPreparacionMaterial.receta_revision)
    )
    if normalized_state is not None:
        statement = statement.where(
            ScmOrdenPreparacionMaterial.estado == normalized_state
        )
    statement, parsed_limit = _created_page(
        statement, ScmOrdenPreparacionMaterial,
        limit=limit, cursor=cursor,
    )
    rows = session.scalars(statement).unique().all()
    has_more = len(rows) > parsed_limit
    visible = rows[:parsed_limit]
    order_ids = [value.id for value in visible]
    assignment_rows = (
        session.execute(
            select(
                ScmAsignacionRequerimientoPreparacion.id,
                ScmAsignacionRequerimientoPreparacion.orden_preparacion_id,
                ScmAsignacionRequerimientoPreparacion.requerimiento_id,
                ScmAsignacionRequerimientoPreparacion.tipo_fuente,
                ScmAsignacionRequerimientoPreparacion.cantidad_planificada_kg,
                ScmAsignacionRequerimientoPreparacion.cantidad_comprometida_kg,
                ScmAsignacionRequerimientoPreparacion.cantidad_consumida_kg,
                ScmAsignacionRequerimientoPreparacion.estado,
            )
            .where(
                ScmAsignacionRequerimientoPreparacion.orden_preparacion_id
                .in_(order_ids)
            )
            .order_by(
                ScmAsignacionRequerimientoPreparacion.orden_preparacion_id,
                ScmAsignacionRequerimientoPreparacion.created_at,
                ScmAsignacionRequerimientoPreparacion.id,
            )
        ).all()
        if order_ids else []
    )
    assignments_by_order = {value: [] for value in order_ids}
    for row in assignment_rows:
        assignments_by_order[row.orden_preparacion_id].append({
            "id": str(row.id),
            "requerimiento_id": str(row.requerimiento_id),
            "tipo_fuente": row.tipo_fuente,
            "cantidad_planificada_kg": _kg(row.cantidad_planificada_kg),
            "cantidad_comprometida_kg": _kg(row.cantidad_comprometida_kg),
            "cantidad_consumida_kg": _kg(row.cantidad_consumida_kg),
            "estado": row.estado,
        })
    return {
        "items": [
            _serialize_opm(
                value,
                detail=False,
                summary_assignments=assignments_by_order[value.id],
            )
            for value in visible
        ],
        "limit": parsed_limit,
        "next_cursor": (
            _encode_cursor(visible[-1].created_at, visible[-1].id)
            if has_more and visible else None
        ),
        "has_more": has_more,
    }


def list_prepared_material_lots(
    session, *, actor_id, limit=25, cursor=None, state=None,
):
    load_actor(session, actor_id, capability="MATERIAL_PREPARADO_GENEALOGIA_VER")
    allowed_states = {
        "PENDIENTE_RECEPCION", "PENDIENTE_CALIDAD", "DISPONIBLE",
        "BLOQUEADO", "RECHAZADO", "AGOTADO",
    }
    normalized_state = str(state or "").strip().upper() or None
    if normalized_state is not None and normalized_state not in allowed_states:
        raise ScmServiceError(
            "INVALID_PREPARED_MATERIAL_LOT_STATE",
            "estado no es valido para lotes de material preparado.",
            status_code=422,
        )
    statement = select(ScmLoteMaterialPreparado).options(
        selectinload(ScmLoteMaterialPreparado.receta_revision)
    )
    if normalized_state is not None:
        statement = statement.where(ScmLoteMaterialPreparado.estado == normalized_state)
    statement, parsed_limit = _created_page(
        statement, ScmLoteMaterialPreparado, limit=limit, cursor=cursor,
    )
    rows = session.scalars(statement).unique().all()
    has_more = len(rows) > parsed_limit
    visible = rows[:parsed_limit]
    lot_ids = [value.id for value in visible]
    bag_counts = dict(
        session.execute(
            select(
                ScmBolsaMaterialPreparado.lote_id,
                func.count(ScmBolsaMaterialPreparado.id),
            )
            .where(ScmBolsaMaterialPreparado.lote_id.in_(lot_ids))
            .group_by(ScmBolsaMaterialPreparado.lote_id)
        ).all()
    ) if lot_ids else {}
    quality_rows = (
        session.execute(
            select(
                ScmDecisionCalidadMaterialPreparado.lote_id,
                ScmDecisionCalidadMaterialPreparado.decision,
                func.count(ScmDecisionCalidadMaterialPreparado.id),
            )
            .where(ScmDecisionCalidadMaterialPreparado.lote_id.in_(lot_ids))
            .group_by(
                ScmDecisionCalidadMaterialPreparado.lote_id,
                ScmDecisionCalidadMaterialPreparado.decision,
            )
        ).all()
        if lot_ids else []
    )
    metrics_by_lot = {
        value: {"cantidad_bolsas": bag_counts.get(value, 0)}
        for value in lot_ids
    }
    for lot_key, decision, count in quality_rows:
        metrics_by_lot[lot_key][decision] = count
    return {
        "items": [
            _serialize_lot(
                value,
                include_bags=False,
                summary_metrics=metrics_by_lot[value.id],
            )
            for value in visible
        ],
        "limit": parsed_limit,
        "next_cursor": (
            _encode_cursor(visible[-1].created_at, visible[-1].id)
            if has_more and visible else None
        ),
        "has_more": has_more,
    }


def list_eligible_preparation_runs(
    session, *, actor_id, limit=25, cursor=None,
):
    load_actor(session, actor_id, capability="OPM_CREAR")
    parsed_limit = _page_limit(limit)
    statement = (
        select(ScmCorridaFabricacion)
        .where(
            ScmCorridaFabricacion.estado.in_(("LIBERADA", "EN_EJECUCION")),
            ~select(ScmRequerimientoMaterialPreparado.id).where(
                ScmRequerimientoMaterialPreparado.corrida_fabricacion_id
                == ScmCorridaFabricacion.id
            ).exists(),
        )
        .options(
            selectinload(ScmCorridaFabricacion.receta_revision),
            selectinload(ScmCorridaFabricacion.orden_fabricacion),
            selectinload(ScmCorridaFabricacion.trabajos_color)
            .selectinload(ScmTrabajoColor.trabajo),
        )
    )
    parsed_cursor = _decode_key_cursor(cursor)
    if parsed_cursor is not None:
        cursor_code, cursor_id = parsed_cursor
        statement = statement.where(or_(
            ScmCorridaFabricacion.codigo > cursor_code,
            and_(
                ScmCorridaFabricacion.codigo == cursor_code,
                ScmCorridaFabricacion.id > cursor_id,
            ),
        ))
    candidates = session.scalars(
        statement.order_by(
            ScmCorridaFabricacion.codigo.asc(), ScmCorridaFabricacion.id.asc()
        ).limit(parsed_limit + 1)
    ).unique().all()
    has_more = len(candidates) > parsed_limit
    page_rows = candidates[:parsed_limit]
    eligible = []
    for run in page_rows:
        recipe = run.receta_revision
        if recipe is None or recipe.estado != "APROBADA":
            continue
        try:
            quantity, _, _ = _calculate_run_composition(run, recipe)
        except ScmServiceError:
            continue
        eligible.append({
            "tipo": "CORRIDA_ELEGIBLE",
            "id": str(run.id),
            "codigo": run.codigo,
            "estado": run.estado,
            "orden_fabricacion": {
                "id": str(run.orden_fabricacion_id),
                "codigo": run.orden_fabricacion.orden_operacion.codigo,
            },
            "receta": {
                "revision_id": recipe.id,
                "nombre": recipe.nombre_variante,
                "revision": recipe.revision,
                "estado": recipe.estado,
            },
            "cantidad_requerida_kg": _kg(quantity),
            "trabajo_color": _work_color_payload(run),
        })
    cursor_row = page_rows[-1] if has_more and page_rows else None
    return {
        "items": eligible,
        "limit": parsed_limit,
        "next_cursor": (
            _encode_key_cursor(cursor_row.codigo, cursor_row.id)
            if cursor_row is not None else None
        ),
        "has_more": has_more,
    }


def list_prepared_material_destinations(session, *, actor_id):
    actor = load_actor(session, actor_id, capability="OPM_VER")
    assignments = session.scalars(select(ScmAlmacenTrabajador).where(
        ScmAlmacenTrabajador.trabajador_id == actor.id,
        ScmAlmacenTrabajador.activo.is_(True),
    )).all()
    scoped_warehouses = {
        value.almacen_id for value in assignments
        if "MATERIAL_PREPARADO" in set(value.clases_articulo_json or [])
    }
    transversal = actor.tiene_capacidad("INVENTARIO_CONTROL_TRANSVERSAL")
    rows = session.scalars(
        select(ScmUbicacionInventario)
        .outerjoin(ScmAlmacen)
        .where(
            ScmUbicacionInventario.activo.is_(True),
            or_(ScmAlmacen.id.is_(None), ScmAlmacen.activo.is_(True)),
        )
        .order_by(ScmUbicacionInventario.codigo.asc())
    ).unique().all()
    items = []
    for location in rows:
        allowed_classes = set(location.clases_articulo_json or [])
        if allowed_classes and "MATERIAL_PREPARADO" not in allowed_classes:
            continue
        global_operational = (
            location.almacen_id is None
            and location.tipo == "PUNTO_PRODUCCION"
        )
        scoped_stock = (
            location.almacen_id is not None
            and (transversal or location.almacen_id in scoped_warehouses)
        )
        if not global_operational and not scoped_stock:
            continue
        uses = []
        if global_operational:
            uses.append("ENTREGA_PRODUCCION")
        if scoped_stock:
            uses.extend(("RECEPCION_ALMACEN", "RETORNO_ALMACEN"))
        items.append({
            "id": location.id,
            "codigo": location.codigo,
            "nombre": location.nombre,
            "tipo": location.tipo,
            "almacen": (
                {
                    "id": str(location.almacen.id),
                    "codigo": location.almacen.codigo,
                    "nombre": location.almacen.nombre,
                }
                if location.almacen is not None else None
            ),
            "permite_saldo_libre": bool(location.permite_saldo_libre),
            "seleccionable_como_stock": bool(
                scoped_stock and location.permite_saldo_libre
            ),
            "usos": uses,
        })
    return {"items": items}


def _serialize_prepared_delivery(item):
    reservation = item.reserva
    return {
        "id": str(item.id),
        "reserva_id": str(item.reserva_id),
        "asignacion_id": str(reservation.asignacion_id),
        "bolsa_id": str(reservation.bolsa_id),
        "trabajo_color": {
            "id": str(reservation.trabajo_ot_id),
            "codigo": reservation.trabajo.codigo,
            "estado": reservation.trabajo.estado,
        },
        "estado": item.estado,
        "origen": reservation.ubicacion_origen.to_dict(),
        "destino": item.ubicacion_destino.to_dict(),
        "retorno": (
            item.ubicacion_retorno.to_dict()
            if item.ubicacion_retorno is not None else None
        ),
        "cantidad_kg": _kg(reservation.cantidad_kg),
        "motivo": item.motivo,
        "created_by_id": item.actor_id,
        "dispatched_by_id": item.dispatched_by_id,
        "received_by_id": item.received_by_id,
        "returned_by_id": item.returned_by_id,
        "closed_by_id": item.closed_by_id,
        "created_at": _iso(item.created_at),
        "dispatched_at": _iso(item.dispatched_at),
        "received_at": _iso(item.received_at),
        "consumed_at": _iso(item.consumed_at),
        "returned_at": _iso(item.returned_at),
        "cancelled_at": _iso(item.cancelled_at),
        "updated_at": _iso(item.updated_at),
        "version": item.version,
        "recepcion_qr": (
            {
                "metodo": item.recepcion_metodo,
                "maquina": (
                    {
                        "id": item.maquina_recepcion.id,
                        "codigo": item.maquina_recepcion.codigo,
                        "nombre": item.maquina_recepcion.nombre,
                    }
                    if item.maquina_recepcion is not None else None
                ),
                "punto": (
                    item.punto_recepcion.to_dict()
                    if item.punto_recepcion is not None else None
                ),
                "maquina_qr": item.maquina_qr_snapshot,
                "bolsa_qr": item.bolsa_qr_snapshot,
            }
            if item.recepcion_metodo is not None else None
        ),
    }


def _parse_machine_qr(value):
    payload = str(value or "").strip()
    parts = payload.split(":")
    if len(parts) != 4 or parts[0] != "SCM" or parts[1] != "MAQUINA" or parts[3] != "V1":
        raise ScmServiceError(
            "MACHINE_QR_INVALID",
            "El QR de maquina no tiene un formato o version reconocidos.",
            status_code=422,
        )
    code = parts[2].strip()
    if not code or len(code) > 20:
        raise ScmServiceError(
            "MACHINE_QR_INVALID", "El QR de maquina no contiene un codigo valido.",
            status_code=422,
        )
    return payload, code


def _prepared_bag_qr_lookup(value):
    """Return the exact scanned payload and its authoritative bag lookup.

    ``SCM:BMP:{uuid}`` is the canonical payload published by ``_serialize_bag``.
    The visible bag code remains accepted for the audited manual contingency,
    but it is not rewritten into a QR payload: snapshots must preserve exactly
    what the operator presented.
    """
    payload = str(value or "").strip()
    if not payload or len(payload) > 80:
        raise ScmServiceError(
            "PREPARED_BAG_QR_NOT_FOUND", "El QR de bolsa no es valido.",
            status_code=404,
        )
    if payload.startswith("SCM:BMP:"):
        raw_id = payload.removeprefix("SCM:BMP:")
        try:
            bag_id = UUID(raw_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "PREPARED_BAG_QR_NOT_FOUND", "El QR de bolsa no es valido.",
                status_code=404,
            ) from error
        return payload, ScmBolsaMaterialPreparado.id == bag_id
    return payload, ScmBolsaMaterialPreparado.codigo == payload


def _machine_bag_qr_context(session, *, machine_qr, bag_qr, lock=False):
    normalized_machine_qr, machine_code = _parse_machine_qr(machine_qr)
    normalized_bag_qr, bag_lookup = _prepared_bag_qr_lookup(bag_qr)
    machine_statement = select(Maquina).where(Maquina.codigo == machine_code)
    if lock:
        machine_statement = machine_statement.with_for_update(of=Maquina)
    machine = session.scalar(machine_statement)
    point_statement = select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.maquina_id == (machine.id if machine else -1)
    )
    if lock:
        point_statement = point_statement.with_for_update(of=ScmUbicacionInventario)
    point = session.scalar(point_statement)
    if (
        machine is None or not machine.activo or machine.estado != "OPERATIVA"
        or point is None or not point.activo or point.tipo != "PUNTO_PRODUCCION"
        or "MATERIAL_PREPARADO" not in (point.clases_articulo_json or [])
    ):
        raise ScmServiceError(
            "MACHINE_QR_INACTIVE",
            "La maquina o su punto de produccion no estan operativos.",
            status_code=409,
        )
    bag_statement = select(ScmBolsaMaterialPreparado).where(bag_lookup)
    if lock:
        bag_statement = bag_statement.with_for_update(of=ScmBolsaMaterialPreparado)
    bag = session.scalar(bag_statement)
    if bag is None:
        raise ScmServiceError(
            "PREPARED_BAG_QR_NOT_FOUND", "La bolsa escaneada no existe.",
            status_code=404,
        )
    delivery_statement = (
        select(ScmEmisionMaterialPreparado)
        .join(ScmReservaMaterialPreparado)
        .where(ScmReservaMaterialPreparado.bolsa_id == bag.id)
        .order_by(ScmEmisionMaterialPreparado.created_at.desc())
    )
    delivery = session.scalars(delivery_statement).first()
    if delivery is None or delivery.estado != "EN_TRANSITO":
        raise ScmServiceError(
            "PREPARED_DELIVERY_NOT_IN_TRANSIT",
            "La bolsa no tiene una entrega en transito para recibir.",
            status_code=409,
        )
    reservation = delivery.reserva
    work = reservation.trabajo
    work_machine_id = work.orden_trabajo.maquina_id
    if delivery.ubicacion_destino_id != point.id or work_machine_id != machine.id:
        raise ScmServiceError(
            "PREPARED_DELIVERY_MACHINE_MISMATCH",
            "La bolsa, el destino y la maquina del Trabajo de color no coinciden.",
            status_code=409,
        )
    return {
        "machine_qr": normalized_machine_qr,
        "bag_qr": normalized_bag_qr,
        "machine": machine,
        "point": point,
        "bag": bag,
        "delivery": delivery,
        "reservation": reservation,
        "work": work,
    }


def resolve_prepared_material_machine_qr(session, *, actor_id, data):
    reject_unknown_fields(data, allowed={"maquina_qr", "bolsa_qr"})
    load_actor(session, actor_id, capability="MATERIAL_PREPARADO_RECIBIR_MAQUINA")
    context = _machine_bag_qr_context(
        session, machine_qr=data.get("maquina_qr"), bag_qr=data.get("bolsa_qr")
    )
    work = context["work"]
    delivery = context["delivery"]
    return {
        "maquina": context["machine"].to_dict(),
        "punto": context["point"].to_dict(),
        "bolsa": _serialize_bag(context["bag"]),
        "entrega": _serialize_prepared_delivery(delivery),
        "trabajo_color": {
            "id": str(work.id), "codigo": work.codigo, "estado": work.estado,
        },
        "acciones_permitidas": {
            "recibir": True,
            "recibir_y_consumir": work.estado in ("EN_EJECUCION", "PAUSADO"),
        },
    }


def confirm_prepared_material_machine_qr(
    session, *, actor_id, operation_id, data,
):
    try:
        reject_unknown_fields(data, allowed={
            "maquina_qr", "bolsa_qr", "entrega_id", "expected_version",
            "accion", "motivo",
        })
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_RECIBIR_MAQUINA"
        )
        action = str(data.get("accion") or "").strip().upper()
        if action not in ("RECIBIR", "RECIBIR_Y_CONSUMIR"):
            raise ScmServiceError(
                "PREPARED_MACHINE_RECEIPT_ACTION_INVALID",
                "accion debe ser RECIBIR o RECIBIR_Y_CONSUMIR.", status_code=422,
            )
        if action == "RECIBIR_Y_CONSUMIR":
            load_actor(session, actor_id, capability="MATERIAL_PREPARADO_CONSUMIR")
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        try:
            expected_delivery_id = UUID(str(data.get("entrega_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "PREPARED_MATERIAL_DELIVERY_REQUIRED",
                "entrega_id debe ser un UUID valido.", status_code=400,
            ) from error
        request_data = {
            "maquina_qr": str(data.get("maquina_qr") or "").strip(),
            "bolsa_qr": str(data.get("bolsa_qr") or "").strip(),
            "entrega_id": str(expected_delivery_id),
            "expected_version": data.get("expected_version"),
            "accion": action,
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /recepciones-material-preparado/confirmar-qr",
            actor, request_data,
        )
        if replay is not None:
            return replay
        context = _machine_bag_qr_context(
            session, machine_qr=data.get("maquina_qr"),
            bag_qr=data.get("bolsa_qr"), lock=True,
        )
        delivery = context["delivery"]
        if delivery.id != expected_delivery_id:
            raise ScmServiceError(
                "PREPARED_DELIVERY_MACHINE_MISMATCH",
                "La entrega resuelta ya no coincide con la confirmacion.",
                status_code=409,
            )
        _require_version(delivery, data.get("expected_version"))
        bag = context["bag"]
        reservation = context["reservation"]
        work = context["work"]
        point = context["point"]
        if reservation.estado != "ACTIVA" or bag.estado != "EMITIDA":
            raise ScmServiceError(
                "PREPARED_DELIVERY_NOT_IN_TRANSIT",
                "La bolsa perdio su reserva activa.", status_code=409,
            )
        delivery.estado = "RECIBIDA_MAQUINA"
        delivery.received_by_id = actor.id
        delivery.received_at = utc_now()
        delivery.maquina_recepcion_id = context["machine"].id
        delivery.punto_recepcion_id = point.id
        delivery.maquina_qr_snapshot = context["machine_qr"]
        delivery.bolsa_qr_snapshot = context["bag_qr"]
        delivery.recepcion_metodo = "QR_COMPARTIDO"
        delivery.version += 1
        reservation.version += 1
        received_payload = {"entrega": _serialize_prepared_delivery(delivery)}
        _event(
            session, aggregate_type="ENTREGA_MATERIAL_PREPARADO",
            aggregate_id=delivery.id,
            event_type="PREPARED_MATERIAL_RECEIVED_AT_MACHINE_QR",
            actor=actor, operation=operation, reason=reason,
            after=received_payload,
        )
        if action == "RECIBIR_Y_CONSUMIR":
            if work.estado not in ("EN_EJECUCION", "PAUSADO"):
                raise ScmServiceError(
                    "WORK_COLOR_NOT_EXECUTABLE",
                    "El TrabajoColor debe estar en ejecucion o pausado para consumir.",
                    status_code=409,
                )
            requirement = reservation.requerimiento
            assignment = reservation.asignacion
            balance = session.scalar(
                select(ScmSaldoMaterialPreparado).where(
                    ScmSaldoMaterialPreparado.receta_revision_id
                    == requirement.receta_revision_id,
                    ScmSaldoMaterialPreparado.ubicacion_id == point.id,
                ).with_for_update(of=ScmSaldoMaterialPreparado)
            )
            quantity = Decimal(reservation.cantidad_kg)
            if balance is None or bag.ubicacion_id != point.id:
                raise ScmServiceError(
                    "PREPARED_MATERIAL_BALANCE_INCONSISTENT",
                    "La bolsa no conserva saldo en el punto de produccion.",
                    status_code=409,
                )
            consumed = Decimal(assignment.cantidad_consumida_kg) + quantity
            if consumed > Decimal(assignment.cantidad_comprometida_kg):
                raise ScmServiceError(
                    "PREPARED_ASSIGNMENT_CONSUMPTION_EXCEEDED",
                    "El consumo excede el compromiso de la asignacion.",
                    status_code=409,
                )
            _move_prepared_balance(
                session, balance=balance, bag=bag, movement_type="CONSUMO",
                physical_delta=-quantity, reserved_delta=-quantity,
                reason=reason, actor_id=actor.id,
                operation_id=uuid5(NAMESPACE_URL, f"{operation_id}:CONSUME:{bag.id}"),
            )
            assignment.cantidad_consumida_kg = consumed
            assignment.estado = (
                "SATISFECHA"
                if consumed == Decimal(assignment.cantidad_comprometida_kg)
                else "COMPROMETIDA"
            )
            reservation.estado = "CONSUMIDA"
            bag.estado = "CONSUMIDA"
            bag.ubicacion_id = None
            bag.version += 1
            delivery.estado = "CERRADA"
            delivery.closed_by_id = actor.id
            delivery.consumed_at = utc_now()
            delivery.version += 1
            requirement.version += 1
            _refresh_requirement_state(requirement)
            _event(
                session, aggregate_type="ENTREGA_MATERIAL_PREPARADO",
                aggregate_id=delivery.id,
                event_type="PREPARED_MATERIAL_CONSUMED_AFTER_QR_RECEIPT",
                actor=actor, operation=operation, reason=reason,
                after={"entrega_id": str(delivery.id), "cantidad_kg": _kg(quantity)},
            )
        session.flush()
        payload = {
            "accion": action,
            "reserva": _serialize_prepared_reservation(reservation),
            "entrega": _serialize_prepared_delivery(delivery),
        }
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def _serialize_prepared_reservation(item):
    return {
        "id": str(item.id),
        "asignacion_id": str(item.asignacion_id),
        "requerimiento_id": str(item.requerimiento_id),
        "bolsa": _serialize_bag(item.bolsa),
        "trabajo_color": {
            "id": str(item.trabajo_ot_id),
            "codigo": item.trabajo.codigo,
            "estado": item.trabajo.estado,
        },
        "ubicacion_origen": item.ubicacion_origen.to_dict(),
        "cantidad_kg": _kg(item.cantidad_kg),
        "estado": item.estado,
        "motivo": item.motivo,
        "created_by_id": item.created_by_id,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "version": item.version,
        "entrega": (
            _serialize_prepared_delivery(item.emision)
            if item.emision is not None else None
        ),
    }


def _serialize_prepared_reservation_summary(item):
    bag = item.bolsa
    delivery = item.emision
    return {
        "id": str(item.id),
        "asignacion_id": str(item.asignacion_id),
        "requerimiento_id": str(item.requerimiento_id),
        "bolsa": {
            "id": str(bag.id),
            "codigo": bag.codigo,
            "peso_neto_kg": _kg(bag.peso_neto_kg),
            "estado": bag.estado,
        },
        "trabajo_color": {
            "id": str(item.trabajo_ot_id),
            "codigo": item.trabajo.codigo,
            "estado": item.trabajo.estado,
        },
        "ubicacion_origen": {
            "id": item.ubicacion_origen.id,
            "codigo": item.ubicacion_origen.codigo,
            "nombre": item.ubicacion_origen.nombre,
        },
        "cantidad_kg": _kg(item.cantidad_kg),
        "estado": item.estado,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "version": item.version,
        "entrega": (
            {
                "id": str(delivery.id),
                "estado": delivery.estado,
                "destino": {
                    "id": delivery.ubicacion_destino.id,
                    "codigo": delivery.ubicacion_destino.codigo,
                    "nombre": delivery.ubicacion_destino.nombre,
                },
                "dispatched_at": _iso(delivery.dispatched_at),
                "received_at": _iso(delivery.received_at),
                "consumed_at": _iso(delivery.consumed_at),
                "returned_at": _iso(delivery.returned_at),
                "version": delivery.version,
            }
            if delivery is not None else None
        ),
    }


def _serialize_prepared_movement(item):
    return {
        "id": str(item.id),
        "bolsa_id": str(item.bolsa_id),
        "tipo": item.tipo,
        "ubicacion": {
            "id": item.saldo.ubicacion.id,
            "codigo": item.saldo.ubicacion.codigo,
            "nombre": item.saldo.ubicacion.nombre,
        },
        "delta_fisico_kg": _kg(item.delta_fisico_kg),
        "delta_reservado_kg": _kg(item.delta_reservado_kg),
        "delta_no_disponible_kg": _kg(item.delta_no_disponible_kg),
        "saldo_fisico_resultante_kg": _kg(
            item.saldo_fisico_resultante_kg
        ),
        "saldo_reservado_resultante_kg": _kg(
            item.saldo_reservado_resultante_kg
        ),
        "saldo_no_disponible_resultante_kg": _kg(
            item.saldo_no_disponible_resultante_kg
        ),
        "motivo": item.motivo,
        "actor_id": item.actor_id,
        "operation_id": str(item.operation_id),
        "created_at": _iso(item.created_at),
    }


def _load_work_color(session, work_id):
    work_color = session.get(ScmTrabajoColor, work_id)
    if work_color is None or work_color.trabajo is None:
        raise ScmServiceError(
            "WORK_COLOR_NOT_FOUND",
            "El TrabajoColor no existe.",
            status_code=404,
        )
    if work_color.trabajo.estado == "ANULADO":
        raise ScmServiceError(
            "WORK_COLOR_NOT_ACTIVE",
            "Un TrabajoColor anulado no admite material preparado.",
            status_code=409,
        )
    return work_color


def _require_prepared_delivery_work_open(reservation):
    if reservation.trabajo.estado not in (
        "PLANIFICADO", "EN_EJECUCION", "PAUSADO"
    ):
        raise ScmServiceError(
            "WORK_COLOR_NOT_DELIVERABLE",
            "El TrabajoColor completado o anulado no admite despachos.",
            status_code=409,
        )


def _operational_prepared_destination(session, location_id):
    location = session.scalar(
        select(ScmUbicacionInventario)
        .where(ScmUbicacionInventario.id == location_id)
        .with_for_update(of=ScmUbicacionInventario)
    )
    allowed_classes = set(location.clases_articulo_json or []) if location else set()
    if (
        location is None
        or not location.activo
        or location.tipo != "PUNTO_PRODUCCION"
        or (allowed_classes and "MATERIAL_PREPARADO" not in allowed_classes)
    ):
        raise ScmServiceError(
            "PREPARED_MATERIAL_DESTINATION_REQUIRED",
            "Selecciona un punto operativo canonico para la entrega.",
            status_code=422,
        )
    return location


def reserve_prepared_material_for_work(
    session, *, actor_id, operation_id, work_id, data,
):
    try:
        reject_unknown_fields(
            data, allowed={"asignacion_id", "bolsa_id", "motivo"}
        )
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_RESERVAR"
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        try:
            assignment_id = UUID(str(data.get("asignacion_id")))
            bag_id = UUID(str(data.get("bolsa_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "PREPARED_ASSIGNMENT_AND_BAG_REQUIRED",
                "asignacion_id y bolsa_id deben ser UUID validos.",
                status_code=400,
            ) from error
        request_data = {
            "trabajo_color_id": str(work_id),
            "asignacion_id": str(assignment_id),
            "bolsa_id": str(bag_id),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /trabajos-color/{id}/reservas-material-preparado",
            actor,
            request_data,
        )
        if replay is not None:
            return replay
        work_color = session.get(ScmTrabajoColor, work_id)
        if work_color is None:
            raise ScmServiceError(
                "WORK_COLOR_NOT_FOUND",
                "El TrabajoColor no existe.",
                status_code=404,
            )
        locked_work = session.scalar(
            select(ScmTrabajoOt)
            .where(ScmTrabajoOt.id == work_color.trabajo_ot_id)
            .execution_options(populate_existing=True)
            .with_for_update(of=ScmTrabajoOt)
        )
        if locked_work is None:
            raise ScmServiceError(
                "WORK_COLOR_NOT_FOUND",
                "El TrabajoColor no existe.",
                status_code=404,
            )
        if locked_work.estado not in (
            "PLANIFICADO", "EN_EJECUCION", "PAUSADO"
        ):
            raise ScmServiceError(
                "WORK_COLOR_NOT_RESERVABLE",
                "El TrabajoColor ya no admite nuevas reservas de material.",
                status_code=409,
            )
        assignment_preview = session.get(
            ScmAsignacionRequerimientoPreparacion, assignment_id
        )
        if assignment_preview is None:
            raise ScmServiceError(
                "PREPARED_ASSIGNMENT_NOT_FOUND",
                "La asignacion de material preparado no existe.",
                status_code=404,
            )
        requirement = session.scalar(
            select(ScmRequerimientoMaterialPreparado)
            .where(
                ScmRequerimientoMaterialPreparado.id
                == assignment_preview.requerimiento_id
            )
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        )
        assignment = session.scalar(
            select(ScmAsignacionRequerimientoPreparacion)
            .where(ScmAsignacionRequerimientoPreparacion.id == assignment_id)
            .with_for_update(of=ScmAsignacionRequerimientoPreparacion)
        )
        if (
            assignment.estado != "COMPROMETIDA"
            or Decimal(assignment.cantidad_comprometida_kg) <= 0
            or requirement.corrida_fabricacion_id
            != work_color.corrida_fabricacion_id
        ):
            raise ScmServiceError(
                "PREPARED_ASSIGNMENT_NOT_ELIGIBLE",
                "La asignacion no corresponde a la corrida del TrabajoColor.",
                status_code=409,
            )
        run = work_color.corrida
        if (
            run.receta_revision_id != requirement.receta_revision_id
            or (
                work_color.receta_revision_id_snapshot is not None
                and work_color.receta_revision_id_snapshot
                != requirement.receta_revision_id
            )
            or (
                work_color.receta_hash_snapshot
                and run.receta_hash
                and work_color.receta_hash_snapshot != run.receta_hash
            )
        ):
            raise ScmServiceError(
                "PREPARED_RECIPE_MISMATCH",
                "El TrabajoColor no conserva la misma revision y huella de receta.",
                status_code=409,
            )
        bag_preview = session.get(ScmBolsaMaterialPreparado, bag_id)
        if bag_preview is None or bag_preview.ubicacion_id is None:
            raise ScmServiceError(
                "PREPARED_MATERIAL_BAG_NOT_FOUND",
                "La bolsa no existe en stock.",
                status_code=404,
            )
        location = session.scalar(
            select(ScmUbicacionInventario)
            .where(ScmUbicacionInventario.id == bag_preview.ubicacion_id)
            .with_for_update(of=ScmUbicacionInventario)
        )
        balance = session.scalar(
            select(ScmSaldoMaterialPreparado)
            .where(
                ScmSaldoMaterialPreparado.receta_revision_id
                == requirement.receta_revision_id,
                ScmSaldoMaterialPreparado.ubicacion_id == location.id,
            )
            .with_for_update(of=ScmSaldoMaterialPreparado)
        )
        bag = session.scalar(
            select(ScmBolsaMaterialPreparado)
            .where(ScmBolsaMaterialPreparado.id == bag_id)
            .with_for_update(of=ScmBolsaMaterialPreparado)
        )
        quantity = Decimal(bag.peso_neto_kg)
        active_or_consumed = sum((
            Decimal(value.cantidad_kg)
            for value in assignment.reservas_material_preparado
            if value.estado in ("ACTIVA", "CONSUMIDA")
        ), ZERO)
        if active_or_consumed + quantity > Decimal(
            assignment.cantidad_comprometida_kg
        ):
            raise ScmServiceError(
                "PREPARED_ASSIGNMENT_EXCEEDS_COMMITMENT",
                "La bolsa completa excede el compromiso restante de la asignacion.",
                status_code=409,
                details={
                    "comprometida_kg": _kg(assignment.cantidad_comprometida_kg),
                    "ya_vinculada_kg": _kg(active_or_consumed),
                },
            )
        if assignment.tipo_fuente == "LOTE_PREPARADO_STOCK":
            eligible = (
                assignment.bolsa_id == bag.id
                and assignment.lote_id == bag.lote_id
                and bag.estado == "DISPONIBLE"
            )
        else:
            eligible = (
                assignment.orden_preparacion_id == bag.orden_preparacion_id
                and bag.asignacion_requerimiento_id == assignment.id
                and bag.lote is not None
                and bag.lote.receta_revision_id == requirement.receta_revision_id
                and bag.orden.composicion_hash == requirement.composicion_hash
                and bag.estado == "DISPONIBLE"
            )
        if not eligible or any(
            value.estado == "ACTIVA" for value in bag.reservas
        ):
            raise ScmServiceError(
                "PREPARED_BAG_NOT_ELIGIBLE_FOR_ASSIGNMENT",
                "La bolsa no pertenece a la asignacion o ya esta reservada.",
                status_code=409,
            )
        if balance is None:
            raise ScmServiceError(
                "PREPARED_MATERIAL_BALANCE_INCONSISTENT",
                "La bolsa no posee saldo fisico acreditado.",
                status_code=409,
            )
        free = (
            Decimal(balance.cantidad_fisica_kg)
            - Decimal(balance.cantidad_reservada_kg)
            - Decimal(balance.cantidad_no_disponible_kg)
        )
        if free < quantity:
            raise ScmServiceError(
                "PREPARED_BAG_BALANCE_NOT_AVAILABLE",
                "El saldo libre no respalda la bolsa.",
                status_code=409,
            )
        _move_prepared_balance(
            session,
            balance=balance,
            bag=bag,
            movement_type="RESERVA",
            reserved_delta=quantity,
            reason=reason,
            actor_id=actor.id,
            operation_id=uuid5(
                NAMESPACE_URL, f"{operation_id}:RESERVA:{bag.id}"
            ),
        )
        bag.estado = "RESERVADA"
        bag.version += 1
        reservation = ScmReservaMaterialPreparado(
            asignacion_id=assignment.id,
            bolsa_id=bag.id,
            trabajo_ot_id=work_color.trabajo_ot_id,
            requerimiento_id=requirement.id,
            ubicacion_origen_id=bag.ubicacion_id,
            cantidad_kg=quantity,
            estado="ACTIVA",
            motivo=reason,
            created_by_id=actor.id,
            operation_id=operation_id,
        )
        session.add(reservation)
        session.flush()
        payload = _serialize_prepared_reservation(reservation)
        _event(
            session,
            aggregate_type="RESERVA_MATERIAL_PREPARADO",
            aggregate_id=reservation.id,
            event_type="PREPARED_MATERIAL_RESERVED_FOR_WORK",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload, status=201)
    except Exception:
        session.rollback()
        raise


def list_work_prepared_material_reservations(
    session, *, actor_id, work_id, limit=25, cursor=None,
):
    load_actor(session, actor_id, capability="OPM_VER")
    work_color = session.get(ScmTrabajoColor, work_id)
    if work_color is None:
        raise ScmServiceError(
            "WORK_COLOR_NOT_FOUND",
            "El TrabajoColor no existe.",
            status_code=404,
        )
    statement = (
        select(ScmReservaMaterialPreparado)
        .where(ScmReservaMaterialPreparado.trabajo_ot_id == work_id)
    )
    statement, parsed_limit = _created_page(
        statement,
        ScmReservaMaterialPreparado,
        limit=limit,
        cursor=cursor,
    )
    rows = session.scalars(statement).unique().all()
    return _page_payload(
        rows,
        limit=parsed_limit,
        serializer=_serialize_prepared_reservation_summary,
    )


def prepare_prepared_material_delivery(
    session, *, actor_id, operation_id, reservation_id, data,
):
    try:
        reject_unknown_fields(
            data, allowed={"version", "ubicacion_destino_id", "motivo"}
        )
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_EMITIR"
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        destination_id = data.get("ubicacion_destino_id")
        if not isinstance(destination_id, int) or isinstance(destination_id, bool):
            raise ScmServiceError(
                "PREPARED_MATERIAL_DESTINATION_REQUIRED",
                "ubicacion_destino_id debe ser un entero.",
                status_code=422,
            )
        request_data = {
            "reserva_id": str(reservation_id),
            "version": data.get("version"),
            "ubicacion_destino_id": destination_id,
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /reservas-material-preparado/{id}/preparar-entrega",
            actor,
            request_data,
        )
        if replay is not None:
            return replay
        preview = session.get(ScmReservaMaterialPreparado, reservation_id)
        if preview is None:
            raise ScmServiceError(
                "PREPARED_MATERIAL_RESERVATION_NOT_FOUND",
                "La reserva no existe.",
                status_code=404,
            )
        session.scalar(
            select(ScmRequerimientoMaterialPreparado)
            .where(
                ScmRequerimientoMaterialPreparado.id
                == preview.requerimiento_id
            )
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        )
        session.scalar(
            select(ScmAsignacionRequerimientoPreparacion)
            .where(
                ScmAsignacionRequerimientoPreparacion.id
                == preview.asignacion_id
            )
            .with_for_update(of=ScmAsignacionRequerimientoPreparacion)
        )
        session.scalars(
            select(ScmUbicacionInventario)
            .where(
                ScmUbicacionInventario.id.in_(sorted({
                    preview.ubicacion_origen_id, destination_id,
                }))
            )
            .order_by(ScmUbicacionInventario.id)
            .with_for_update(of=ScmUbicacionInventario)
        ).all()
        source = _canonical_location(
            session,
            preview.ubicacion_origen_id,
            actor_id=actor.id,
            require_assignment=True,
            article_class="MATERIAL_PREPARADO",
        )
        destination = _operational_prepared_destination(session, destination_id)
        reservation = session.scalar(
            select(ScmReservaMaterialPreparado)
            .where(ScmReservaMaterialPreparado.id == reservation_id)
            .with_for_update(of=ScmReservaMaterialPreparado)
        )
        _require_version(reservation, data.get("version"))
        _require_prepared_delivery_work_open(reservation)
        if reservation.estado != "ACTIVA" or reservation.emision is not None:
            raise ScmServiceError(
                "PREPARED_DELIVERY_NOT_PREPARABLE",
                "La reserva ya posee una entrega o no esta activa.",
                status_code=409,
            )
        delivery = ScmEmisionMaterialPreparado(
            reserva=reservation,
            ubicacion_destino_id=destination.id,
            estado="PREPARADA",
            motivo=reason,
            actor_id=actor.id,
            operation_id=operation_id,
        )
        session.add(delivery)
        reservation.version += 1
        session.flush()
        payload = {
            "reserva": _serialize_prepared_reservation(reservation),
            "entrega": _serialize_prepared_delivery(delivery),
        }
        _event(
            session,
            aggregate_type="ENTREGA_MATERIAL_PREPARADO",
            aggregate_id=delivery.id,
            event_type="PREPARED_MATERIAL_DELIVERY_PREPARED",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload, status=201)
    except Exception:
        session.rollback()
        raise


def _delivery_lock_head(session, delivery_id):
    preview = session.get(ScmEmisionMaterialPreparado, delivery_id)
    if preview is None:
        raise ScmServiceError(
            "PREPARED_MATERIAL_DELIVERY_NOT_FOUND",
            "La entrega no existe.",
            status_code=404,
        )
    reservation_preview = preview.reserva
    requirement = session.scalar(
        select(ScmRequerimientoMaterialPreparado)
        .where(
            ScmRequerimientoMaterialPreparado.id
            == reservation_preview.requerimiento_id
        )
        .with_for_update(of=ScmRequerimientoMaterialPreparado)
    )
    assignment = session.scalar(
        select(ScmAsignacionRequerimientoPreparacion)
        .where(
            ScmAsignacionRequerimientoPreparacion.id
            == reservation_preview.asignacion_id
        )
        .with_for_update(of=ScmAsignacionRequerimientoPreparacion)
    )
    return preview, requirement, assignment


def _delivery_lock_tail(session, preview):
    reservation_preview = preview.reserva
    bag = session.scalar(
        select(ScmBolsaMaterialPreparado)
        .where(ScmBolsaMaterialPreparado.id == reservation_preview.bolsa_id)
        .with_for_update(of=ScmBolsaMaterialPreparado)
    )
    reservation = session.scalar(
        select(ScmReservaMaterialPreparado)
        .where(ScmReservaMaterialPreparado.id == reservation_preview.id)
        .with_for_update(of=ScmReservaMaterialPreparado)
    )
    delivery = session.scalar(
        select(ScmEmisionMaterialPreparado)
        .where(ScmEmisionMaterialPreparado.id == preview.id)
        .with_for_update(of=ScmEmisionMaterialPreparado)
    )
    return bag, reservation, delivery


def dispatch_prepared_material_delivery(
    session, *, actor_id, operation_id, delivery_id, data,
):
    try:
        reject_unknown_fields(data, allowed={"version", "motivo"})
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_EMITIR"
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "entrega_id": str(delivery_id),
            "version": data.get("version"),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /entregas-material-preparado/{id}/despachar",
            actor, request_data,
        )
        if replay is not None:
            return replay
        preview, requirement, _assignment = _delivery_lock_head(
            session, delivery_id
        )
        source_location_id = preview.reserva.ubicacion_origen_id
        destination_location_id = preview.ubicacion_destino_id
        session.scalars(
            select(ScmUbicacionInventario)
            .where(ScmUbicacionInventario.id.in_(sorted({
                source_location_id, destination_location_id,
            })))
            .order_by(ScmUbicacionInventario.id)
            .with_for_update(of=ScmUbicacionInventario)
        ).all()
        source_location = _canonical_location(
            session, source_location_id, actor_id=actor.id,
            require_assignment=True, article_class="MATERIAL_PREPARADO",
        )
        destination_location = _operational_prepared_destination(
            session, destination_location_id,
        )
        source_balance = session.scalar(
            select(ScmSaldoMaterialPreparado)
            .where(
                ScmSaldoMaterialPreparado.receta_revision_id
                == requirement.receta_revision_id,
                ScmSaldoMaterialPreparado.ubicacion_id == source_location.id,
            )
            .with_for_update(of=ScmSaldoMaterialPreparado)
        )
        destination_balance = _prepared_balance(
            session,
            recipe_id=requirement.receta_revision_id,
            location_id=destination_location.id,
        )
        bag, reservation, delivery = _delivery_lock_tail(session, preview)
        _require_version(delivery, data.get("version"))
        _require_prepared_delivery_work_open(reservation)
        quantity = Decimal(reservation.cantidad_kg)
        if source_balance is None:
            raise ScmServiceError(
                "PREPARED_MATERIAL_BALANCE_INCONSISTENT",
                "La ubicacion origen no conserva saldo preparado.",
                status_code=409,
            )
        if (
            delivery.estado != "PREPARADA"
            or reservation.estado != "ACTIVA"
            or bag.estado != "RESERVADA"
            or bag.ubicacion_id != source_location.id
        ):
            raise ScmServiceError(
                "PREPARED_DELIVERY_NOT_DISPATCHABLE",
                "La entrega no esta preparada o la bolsa perdio su reserva.",
                status_code=409,
            )
        _move_prepared_balance(
            session,
            balance=source_balance,
            bag=bag,
            movement_type="EMISION_SALIDA",
            physical_delta=-quantity,
            reserved_delta=-quantity,
            reason=reason,
            actor_id=actor.id,
            operation_id=uuid5(
                NAMESPACE_URL, f"{operation_id}:DISPATCH:{bag.id}"
            ),
        )
        _move_prepared_balance(
            session,
            balance=destination_balance,
            bag=bag,
            movement_type="EMISION_ENTRADA",
            physical_delta=quantity,
            reserved_delta=quantity,
            reason=reason,
            actor_id=actor.id,
            operation_id=uuid5(
                NAMESPACE_URL, f"{operation_id}:DISPATCH-IN:{bag.id}"
            ),
        )
        bag.estado = "EMITIDA"
        bag.ubicacion_id = destination_location.id
        bag.version += 1
        delivery.estado = "EN_TRANSITO"
        delivery.dispatched_by_id = actor.id
        delivery.dispatched_at = utc_now()
        delivery.version += 1
        reservation.version += 1
        session.flush()
        payload = {
            "reserva": _serialize_prepared_reservation(reservation),
            "entrega": _serialize_prepared_delivery(delivery),
            "saldo_origen": _serialize_balance(source_balance),
            "saldo_destino": _serialize_balance(destination_balance),
        }
        _event(
            session,
            aggregate_type="ENTREGA_MATERIAL_PREPARADO",
            aggregate_id=delivery.id,
            event_type="PREPARED_MATERIAL_DISPATCHED",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def receive_prepared_material_at_machine(
    session, *, actor_id, operation_id, delivery_id, data,
):
    try:
        reject_unknown_fields(data, allowed={"version", "motivo"})
        actor = load_actor(
            session, actor_id,
            capability="MATERIAL_PREPARADO_RECIBIR_MAQUINA",
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "entrega_id": str(delivery_id),
            "version": data.get("version"),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /entregas-material-preparado/{id}/recibir-maquina",
            actor, request_data,
        )
        if replay is not None:
            return replay
        preview, _requirement, _assignment = _delivery_lock_head(
            session, delivery_id
        )
        destination = _operational_prepared_destination(
            session, preview.ubicacion_destino_id
        )
        bag, reservation, delivery = _delivery_lock_tail(session, preview)
        _require_version(delivery, data.get("version"))
        if (
            delivery.estado != "EN_TRANSITO"
            or reservation.estado != "ACTIVA"
            or bag.estado != "EMITIDA"
            or bag.ubicacion_id != delivery.ubicacion_destino_id
        ):
            raise ScmServiceError(
                "PREPARED_DELIVERY_NOT_IN_TRANSIT",
                "Solo una entrega en transito puede recibirse en maquina.",
                status_code=409,
            )
        delivery.estado = "RECIBIDA_MAQUINA"
        delivery.received_by_id = actor.id
        delivery.received_at = utc_now()
        delivery.version += 1
        reservation.version += 1
        session.flush()
        payload = {
            "reserva": _serialize_prepared_reservation(reservation),
            "entrega": _serialize_prepared_delivery(delivery),
        }
        _event(
            session,
            aggregate_type="ENTREGA_MATERIAL_PREPARADO",
            aggregate_id=delivery.id,
            event_type="PREPARED_MATERIAL_RECEIVED_AT_MACHINE",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def consume_prepared_material_delivery(
    session, *, actor_id, operation_id, work_id, data,
):
    try:
        reject_unknown_fields(data, allowed={"entrega_id", "version", "motivo"})
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_CONSUMIR"
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        try:
            delivery_id = UUID(str(data.get("entrega_id")))
        except (TypeError, ValueError, AttributeError) as error:
            raise ScmServiceError(
                "PREPARED_MATERIAL_DELIVERY_REQUIRED",
                "entrega_id debe ser un UUID valido.",
                status_code=400,
            ) from error
        request_data = {
            "trabajo_color_id": str(work_id),
            "entrega_id": str(delivery_id),
            "version": data.get("version"),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /trabajos-color/{id}/consumos-material-preparado",
            actor, request_data,
        )
        if replay is not None:
            return replay
        work_color = _load_work_color(session, work_id)
        preview, requirement, assignment = _delivery_lock_head(
            session, delivery_id
        )
        if preview.reserva.trabajo_ot_id != work_id:
            raise ScmServiceError(
                "PREPARED_DELIVERY_WORK_MISMATCH",
                "La entrega no pertenece al TrabajoColor indicado.",
                status_code=409,
            )
        if work_color.trabajo.estado not in ("EN_EJECUCION", "PAUSADO"):
            raise ScmServiceError(
                "WORK_COLOR_NOT_EXECUTABLE",
                "El TrabajoColor debe estar en ejecucion o pausado para consumir.",
                status_code=409,
            )
        destination = _operational_prepared_destination(
            session, preview.ubicacion_destino_id,
        )
        destination_balance = session.scalar(
            select(ScmSaldoMaterialPreparado)
            .where(
                ScmSaldoMaterialPreparado.receta_revision_id
                == requirement.receta_revision_id,
                ScmSaldoMaterialPreparado.ubicacion_id == destination.id,
            )
            .with_for_update(of=ScmSaldoMaterialPreparado)
        )
        bag, reservation, delivery = _delivery_lock_tail(session, preview)
        _require_version(delivery, data.get("version"))
        if delivery.estado != "RECIBIDA_MAQUINA" or reservation.estado != "ACTIVA":
            raise ScmServiceError(
                "LMP_DELIVERY_NOT_RECEIVED",
                "Confirma primero la recepcion fisica en maquina.",
                status_code=409,
            )
        quantity = Decimal(reservation.cantidad_kg)
        if (
            destination_balance is None
            or bag.estado != "EMITIDA"
            or bag.ubicacion_id != destination.id
        ):
            raise ScmServiceError(
                "PREPARED_MATERIAL_BALANCE_INCONSISTENT",
                "La bolsa recibida no conserva saldo en el punto de produccion.",
                status_code=409,
            )
        consumed = Decimal(assignment.cantidad_consumida_kg) + quantity
        if consumed > Decimal(assignment.cantidad_comprometida_kg):
            raise ScmServiceError(
                "PREPARED_ASSIGNMENT_CONSUMPTION_EXCEEDED",
                "El consumo excede el compromiso de la asignacion.",
                status_code=409,
            )
        _move_prepared_balance(
            session,
            balance=destination_balance,
            bag=bag,
            movement_type="CONSUMO",
            physical_delta=-quantity,
            reserved_delta=-quantity,
            reason=reason,
            actor_id=actor.id,
            operation_id=uuid5(
                NAMESPACE_URL, f"{operation_id}:CONSUME:{bag.id}"
            ),
        )
        assignment.cantidad_consumida_kg = consumed
        assignment.estado = (
            "SATISFECHA"
            if consumed == Decimal(assignment.cantidad_comprometida_kg)
            else "COMPROMETIDA"
        )
        reservation.estado = "CONSUMIDA"
        reservation.version += 1
        bag.estado = "CONSUMIDA"
        bag.ubicacion_id = None
        bag.version += 1
        delivery.estado = "CERRADA"
        delivery.closed_by_id = actor.id
        delivery.consumed_at = utc_now()
        delivery.version += 1
        requirement.version += 1
        _refresh_requirement_state(requirement)
        session.flush()
        payload = {
            "requerimiento": _serialize_prepared_requirement(requirement),
            "asignacion": _serialize_assignment(assignment),
            "reserva": _serialize_prepared_reservation(reservation),
            "entrega": _serialize_prepared_delivery(delivery),
            "saldo_destino": _serialize_balance(destination_balance),
        }
        _event(
            session,
            aggregate_type="ENTREGA_MATERIAL_PREPARADO",
            aggregate_id=delivery.id,
            event_type="PREPARED_MATERIAL_CONSUMED",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def return_prepared_material_delivery(
    session, *, actor_id, operation_id, delivery_id, data,
):
    try:
        reject_unknown_fields(
            data, allowed={"version", "ubicacion_retorno_id", "motivo"}
        )
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_DEVOLVER"
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        return_location_id = data.get("ubicacion_retorno_id")
        if not isinstance(return_location_id, int) or isinstance(return_location_id, bool):
            raise ScmServiceError(
                "CANONICAL_LOCATION_REQUIRED",
                "ubicacion_retorno_id debe ser un entero.",
                status_code=422,
            )
        request_data = {
            "entrega_id": str(delivery_id),
            "version": data.get("version"),
            "ubicacion_retorno_id": return_location_id,
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session, operation_id,
            "POST /entregas-material-preparado/{id}/retornar",
            actor, request_data,
        )
        if replay is not None:
            return replay
        preview, requirement, assignment = _delivery_lock_head(
            session, delivery_id
        )
        session.scalars(
            select(ScmUbicacionInventario)
            .where(ScmUbicacionInventario.id.in_(sorted({
                preview.ubicacion_destino_id, return_location_id,
            })))
            .order_by(ScmUbicacionInventario.id)
            .with_for_update(of=ScmUbicacionInventario)
        ).all()
        destination_location = _operational_prepared_destination(
            session, preview.ubicacion_destino_id,
        )
        return_location = _canonical_location(
            session, return_location_id, actor_id=actor.id,
            require_assignment=True, article_class="MATERIAL_PREPARADO",
        )
        destination_balance = session.scalar(
            select(ScmSaldoMaterialPreparado)
            .where(
                ScmSaldoMaterialPreparado.receta_revision_id
                == requirement.receta_revision_id,
                ScmSaldoMaterialPreparado.ubicacion_id
                == destination_location.id,
            )
            .with_for_update(of=ScmSaldoMaterialPreparado)
        )
        return_balance = _prepared_balance(
            session,
            recipe_id=requirement.receta_revision_id,
            location_id=return_location.id,
        )
        bag, reservation, delivery = _delivery_lock_tail(session, preview)
        _require_version(delivery, data.get("version"))
        if (
            delivery.estado not in ("EN_TRANSITO", "RECIBIDA_MAQUINA")
            or reservation.estado != "ACTIVA"
            or bag.estado != "EMITIDA"
            or bag.ubicacion_id != destination_location.id
        ):
            raise ScmServiceError(
                "PREPARED_DELIVERY_NOT_RETURNABLE",
                "Solo una bolsa en transito o recibida sin consumo puede retornar.",
                status_code=409,
            )
        quantity = Decimal(reservation.cantidad_kg)
        if destination_balance is None:
            raise ScmServiceError(
                "PREPARED_MATERIAL_BALANCE_INCONSISTENT",
                "El punto de produccion no conserva la bolsa en custodia.",
                status_code=409,
            )
        _move_prepared_balance(
            session,
            balance=destination_balance,
            bag=bag,
            movement_type="RETORNO_SALIDA",
            physical_delta=-quantity,
            reserved_delta=-quantity,
            reason=reason,
            actor_id=actor.id,
            operation_id=uuid5(
                NAMESPACE_URL, f"{operation_id}:RETURN-OUT:{bag.id}"
            ),
        )
        _move_prepared_balance(
            session,
            balance=return_balance,
            bag=bag,
            movement_type="RETORNO_ENTRADA",
            physical_delta=quantity,
            reason=reason,
            actor_id=actor.id,
            operation_id=uuid5(
                NAMESPACE_URL, f"{operation_id}:RETURN:{bag.id}"
            ),
        )
        bag.ubicacion_id = return_location.id
        bag.estado = "DISPONIBLE"
        bag.version += 1
        reservation.estado = "DEVUELTA"
        reservation.version += 1
        delivery.estado = "RETORNADA_TOTAL"
        delivery.returned_by_id = actor.id
        delivery.returned_at = utc_now()
        delivery.ubicacion_retorno_id = return_location.id
        delivery.version += 1
        session.flush()
        payload = {
            "asignacion": _serialize_assignment(assignment),
            "reserva": _serialize_prepared_reservation(reservation),
            "entrega": _serialize_prepared_delivery(delivery),
            "saldo_destino": _serialize_balance(destination_balance),
            "saldo_retorno": _serialize_balance(return_balance),
        }
        _event(
            session,
            aggregate_type="ENTREGA_MATERIAL_PREPARADO",
            aggregate_id=delivery.id,
            event_type="PREPARED_MATERIAL_RETURNED",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise


def release_prepared_material_reservation(
    session, *, actor_id, operation_id, reservation_id, data,
):
    try:
        reject_unknown_fields(data, allowed={"version", "motivo"})
        actor = load_actor(
            session, actor_id, capability="MATERIAL_PREPARADO_RESERVAR"
        )
        reason = required_text(data.get("motivo"), field="motivo", max_length=240)
        request_data = {
            "reserva_id": str(reservation_id),
            "version": data.get("version"),
            "motivo": reason,
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /reservas-material-preparado/{id}/liberar",
            actor,
            request_data,
        )
        if replay is not None:
            return replay
        preview = session.get(ScmReservaMaterialPreparado, reservation_id)
        if preview is None:
            raise ScmServiceError(
                "PREPARED_MATERIAL_RESERVATION_NOT_FOUND",
                "La reserva no existe.", status_code=404,
            )
        requirement = session.scalar(
            select(ScmRequerimientoMaterialPreparado)
            .where(
                ScmRequerimientoMaterialPreparado.id
                == preview.requerimiento_id
            )
            .with_for_update(of=ScmRequerimientoMaterialPreparado)
        )
        assignment = session.scalar(
            select(ScmAsignacionRequerimientoPreparacion)
            .where(
                ScmAsignacionRequerimientoPreparacion.id
                == preview.asignacion_id
            )
            .with_for_update(of=ScmAsignacionRequerimientoPreparacion)
        )
        bag_preview = session.get(ScmBolsaMaterialPreparado, preview.bolsa_id)
        if bag_preview.ubicacion_id is None:
            raise ScmServiceError(
                "PREPARED_RESERVATION_RETURN_REQUIRED",
                "La bolsa salio del almacen; retornala antes de liberar la reserva.",
                status_code=409,
            )
        location = _canonical_location(
            session, bag_preview.ubicacion_id,
            article_class="MATERIAL_PREPARADO",
        )
        balance = session.scalar(
            select(ScmSaldoMaterialPreparado)
            .where(
                ScmSaldoMaterialPreparado.receta_revision_id
                == requirement.receta_revision_id,
                ScmSaldoMaterialPreparado.ubicacion_id == location.id,
            )
            .with_for_update(of=ScmSaldoMaterialPreparado)
        )
        bag = session.scalar(
            select(ScmBolsaMaterialPreparado)
            .where(ScmBolsaMaterialPreparado.id == preview.bolsa_id)
            .with_for_update(of=ScmBolsaMaterialPreparado)
        )
        reservation = session.scalar(
            select(ScmReservaMaterialPreparado)
            .where(ScmReservaMaterialPreparado.id == reservation_id)
            .with_for_update(of=ScmReservaMaterialPreparado)
        )
        _require_version(reservation, data.get("version"))
        delivery = reservation.emision
        active_before_dispatch = (
            reservation.estado == "ACTIVA"
            and bag.estado == "RESERVADA"
            and (delivery is None or delivery.estado == "PREPARADA")
        )
        returned_to_stock = (
            reservation.estado == "DEVUELTA"
            and bag.estado == "DISPONIBLE"
            and delivery is not None
            and delivery.estado == "RETORNADA_TOTAL"
        )
        if not active_before_dispatch and not returned_to_stock:
            raise ScmServiceError(
                "PREPARED_RESERVATION_NOT_RELEASABLE",
                "La reserva esta emitida, consumida o ya fue liberada.",
                status_code=409,
            )
        quantity = Decimal(reservation.cantidad_kg)
        if balance is None or (
            active_before_dispatch
            and Decimal(balance.cantidad_reservada_kg) < quantity
        ):
            raise ScmServiceError(
                "PREPARED_MATERIAL_BALANCE_INCONSISTENT",
                "El saldo no conserva la cantidad reservada.",
                status_code=409,
            )
        if active_before_dispatch:
            _move_prepared_balance(
                session,
                balance=balance,
                bag=bag,
                movement_type="LIBERACION_RESERVA",
                reserved_delta=-quantity,
                reason=reason,
                actor_id=actor.id,
                operation_id=uuid5(
                    NAMESPACE_URL, f"{operation_id}:RELEASE:{bag.id}"
                ),
            )
            if delivery is not None:
                delivery.estado = "CANCELADA"
                delivery.closed_by_id = actor.id
                delivery.cancelled_at = utc_now()
                delivery.version += 1
        bag.estado = "DISPONIBLE"
        bag.version += 1
        reservation.estado = "LIBERADA"
        reservation.version += 1
        session.flush()
        payload = {
            "asignacion": _serialize_assignment(assignment),
            "reserva": _serialize_prepared_reservation(reservation),
            "saldo": _serialize_balance(balance),
        }
        _event(
            session,
            aggregate_type="RESERVA_MATERIAL_PREPARADO",
            aggregate_id=reservation.id,
            event_type="PREPARED_MATERIAL_RESERVATION_RELEASED",
            actor=actor,
            operation=operation,
            reason=reason,
            after=payload,
        )
        return _complete(session, operation, payload)
    except Exception:
        session.rollback()
        raise
