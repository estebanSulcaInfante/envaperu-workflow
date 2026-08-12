"""Configuracion y autorizacion de almacenes SCM."""

import copy
import hashlib
import json

from sqlalchemy import select

from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
from app.models.trabajador import Trabajador
from app.services.scm_service_support import (
    ScmServiceError, actor_snapshot, load_actor, required_text, stable_code,
)


WAREHOUSE_TYPES = {
    "MATERIAS_PRIMAS", "PIEZAS_WIP", "PRODUCTO_TERMINADO",
    "GENERAL_CONTINGENCIA",
}
LOCATION_TYPES = {
    "RECEPCION", "CUARENTENA", "ZONA", "POSICION", "STAGING",
    "PUNTO_PRODUCCION", "TRANSITO",
}


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _reserve(session, operation_id, endpoint, actor, data):
    digest = _hash({"endpoint": endpoint, "actor_id": actor.id, "data": data})
    prior = session.get(ScmOperacion, operation_id)
    if prior:
        if prior.endpoint != endpoint or prior.request_sha256 != digest:
            raise ScmServiceError("IDEMPOTENCY_CONFLICT", "La clave ya fue usada.", status_code=409)
        if prior.response_json is None:
            raise ScmServiceError("IDEMPOTENCY_OPERATION_INCOMPLETE", "La operacion no termino.", status_code=409)
        return None, copy.deepcopy(prior.response_json)
    operation = ScmOperacion(
        operation_id=operation_id, endpoint=endpoint, actor_id=actor.id,
        request_sha256=digest,
    )
    session.add(operation)
    session.flush()
    return operation, None


def _complete(session, operation, payload, *, status=201):
    operation.response_json = copy.deepcopy(payload)
    operation.estado_http = status
    session.commit()
    return payload


def _capability(actor, code):
    if not actor.tiene_capacidad(code):
        raise ScmServiceError(
            "CAPABILITY_REQUIRED", f"El actor requiere la capacidad {code}.",
            status_code=403, details={"capability": code},
        )


def _classes(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ScmServiceError("INVALID_ARTICLE_CLASSES", "clases_articulo debe ser una lista.", status_code=400)
    return sorted({item.strip().upper() for item in value if item.strip()})


def warehouse_scope(session, *, actor_id):
    actor = load_actor(session, actor_id)
    configured = session.scalar(select(ScmAlmacen.id).where(ScmAlmacen.activo.is_(True)).limit(1)) is not None
    transversal = actor.tiene_capacidad("INVENTARIO_CONTROL_TRANSVERSAL")
    assignments = session.scalars(
        select(ScmAlmacenTrabajador)
        .join(ScmAlmacen)
        .where(
            ScmAlmacenTrabajador.trabajador_id == actor.id,
            ScmAlmacenTrabajador.activo.is_(True),
            ScmAlmacen.activo.is_(True),
        )
    ).all()
    return {
        "actor": actor, "configured": configured, "transversal": transversal,
        "warehouse_ids": {item.almacen_id for item in assignments},
        "classes": {
            item.almacen_id: set(item.clases_articulo_json or [])
            for item in assignments
        },
        "assignments": assignments,
    }


def allowed_location_ids(session, *, actor_id):
    scope = warehouse_scope(session, actor_id=actor_id)
    if not scope["configured"] or scope["transversal"]:
        return None, scope
    if not scope["warehouse_ids"]:
        return set(), scope
    values = session.scalars(
        select(ScmUbicacionInventario.id).where(
            ScmUbicacionInventario.almacen_id.in_(scope["warehouse_ids"])
        )
    ).all()
    return set(values), scope


def assert_location_scope(session, *, actor_id, location, article_class=None):
    scope = warehouse_scope(session, actor_id=actor_id)
    if not scope["configured"] or scope["transversal"]:
        return scope
    classes = scope["classes"].get(location.almacen_id, set())
    if location.almacen_id not in scope["warehouse_ids"] or (
        article_class is not None and article_class not in classes
    ):
        raise ScmServiceError("LOCATION_NOT_FOUND", "La ubicacion no existe.", status_code=404)
    return scope


def assert_article_class_scope(session, *, actor_id, article_class):
    scope = warehouse_scope(session, actor_id=actor_id)
    if not scope["configured"] or scope["transversal"]:
        return scope
    if not any(article_class in values for values in scope["classes"].values()):
        raise ScmServiceError("LOGISTIC_UNIT_NOT_FOUND", "La unidad no existe.", status_code=404)
    return scope


def list_warehouses(session, *, actor_id):
    scope = warehouse_scope(session, actor_id=actor_id)
    _capability(scope["actor"], "INVENTARIO_VER")
    query = select(ScmAlmacen).where(ScmAlmacen.activo.is_(True)).order_by(ScmAlmacen.codigo)
    if scope["configured"] and not scope["transversal"]:
        if not scope["warehouse_ids"]:
            return {"items": []}
        query = query.where(ScmAlmacen.id.in_(scope["warehouse_ids"]))
    return {"items": [item.to_dict(include_locations=True) for item in session.scalars(query).all()]}


def get_warehouse(session, *, actor_id, warehouse_id):
    scope = warehouse_scope(session, actor_id=actor_id)
    _capability(scope["actor"], "INVENTARIO_VER")
    item = session.get(ScmAlmacen, warehouse_id)
    if item is None or not item.activo or (
        scope["configured"] and not scope["transversal"] and item.id not in scope["warehouse_ids"]
    ):
        raise ScmServiceError("WAREHOUSE_NOT_FOUND", "El almacen no existe.", status_code=404)
    return item.to_dict(include_locations=True)


def create_warehouse(session, *, actor_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="ALMACEN_CONFIG_ADMINISTRAR")
    code = stable_code(data.get("codigo"), max_length=40)
    name = required_text(data.get("nombre"), field="nombre", max_length=120)
    warehouse_type = str(data.get("tipo") or "").strip().upper()
    if warehouse_type not in WAREHOUSE_TYPES:
        raise ScmServiceError("INVALID_WAREHOUSE_TYPE", "tipo de almacen invalido.", status_code=422)
    command = {"codigo": code, "nombre": name, "tipo": warehouse_type}
    operation, replay = _reserve(session, operation_id, "POST /almacenes", actor, command)
    if replay is not None:
        return replay
    if session.scalar(select(ScmAlmacen).where(ScmAlmacen.codigo == code)):
        raise ScmServiceError("WAREHOUSE_CODE_DUPLICATE", "El codigo ya existe.", status_code=409)
    item = ScmAlmacen(codigo=code, nombre=name, tipo=warehouse_type)
    session.add(item)
    session.flush()
    payload = item.to_dict()
    session.add(ScmEvento(
        aggregate_type="ALMACEN", aggregate_id=str(item.id), tipo="ALMACEN_CREADO",
        actor_id=actor.id, actor_snapshot=actor_snapshot(actor), after_json=payload,
        operation_id=operation.operation_id,
    ))
    return _complete(session, operation, payload)


def create_location(session, *, actor_id, warehouse_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="ALMACEN_CONFIG_ADMINISTRAR")
    warehouse = session.get(ScmAlmacen, warehouse_id)
    if warehouse is None or not warehouse.activo:
        raise ScmServiceError("WAREHOUSE_NOT_FOUND", "El almacen no existe.", status_code=404)
    code = stable_code(data.get("codigo"), max_length=40)
    name = required_text(data.get("nombre"), field="nombre", max_length=120)
    location_type = str(data.get("tipo") or "POSICION").strip().upper()
    if location_type not in LOCATION_TYPES or location_type == "TRANSITO":
        raise ScmServiceError("INVALID_LOCATION_TYPE", "Tipo de ubicacion invalido para un almacen.", status_code=422)
    command = {"warehouse_id": str(warehouse.id), "codigo": code, "nombre": name, "tipo": location_type, "clases_articulo": _classes(data.get("clases_articulo"))}
    operation, replay = _reserve(session, operation_id, "POST /almacenes/{id}/ubicaciones", actor, command)
    if replay is not None:
        return replay
    if session.scalar(select(ScmUbicacionInventario).where(ScmUbicacionInventario.codigo == code)):
        raise ScmServiceError("LOCATION_CODE_DUPLICATE", "El codigo ya existe.", status_code=409)
    parent_id = data.get("parent_id")
    if parent_id is not None:
        parent = session.get(ScmUbicacionInventario, parent_id)
        if parent is None or parent.almacen_id != warehouse.id:
            raise ScmServiceError("LOCATION_PARENT_INVALID", "La ubicacion padre no pertenece al almacen.", status_code=422)
    item = ScmUbicacionInventario(
        almacen_id=warehouse.id, parent_id=parent_id, codigo=code, nombre=name,
        tipo=location_type, clases_articulo_json=command["clases_articulo"],
        permite_saldo_libre=bool(data.get("permite_saldo_libre", True)),
    )
    session.add(item)
    session.flush()
    payload = item.to_dict()
    session.add(ScmEvento(
        aggregate_type="UBICACION_INVENTARIO", aggregate_id=str(item.id),
        tipo="UBICACION_CREADA", actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor), after_json=payload,
        operation_id=operation.operation_id,
    ))
    return _complete(session, operation, payload)


def assign_worker(session, *, actor_id, warehouse_id, operation_id, data):
    actor = load_actor(session, actor_id, capability="ALMACEN_SCOPE_ADMINISTRAR")
    warehouse = session.get(ScmAlmacen, warehouse_id)
    worker = session.get(Trabajador, data.get("trabajador_id"))
    if warehouse is None or not warehouse.activo:
        raise ScmServiceError("WAREHOUSE_NOT_FOUND", "El almacen no existe.", status_code=404)
    if worker is None or not worker.activo:
        raise ScmServiceError("WORKER_NOT_FOUND", "El trabajador no existe.", status_code=404)
    classes = _classes(data.get("clases_articulo"))
    command = {"warehouse_id": str(warehouse.id), "trabajador_id": worker.id, "clases_articulo": classes}
    operation, replay = _reserve(session, operation_id, "POST /almacenes/{id}/trabajadores", actor, command)
    if replay is not None:
        return replay
    assignment = session.scalar(select(ScmAlmacenTrabajador).where(
        ScmAlmacenTrabajador.almacen_id == warehouse.id,
        ScmAlmacenTrabajador.trabajador_id == worker.id,
    ))
    before = assignment.to_dict() if assignment else None
    if assignment:
        assignment.clases_articulo_json = classes
        assignment.activo = True
        assignment.version += 1
        assignment.asignado_por_id = actor.id
    else:
        assignment = ScmAlmacenTrabajador(
            almacen_id=warehouse.id, trabajador_id=worker.id,
            clases_articulo_json=classes, asignado_por_id=actor.id,
        )
        session.add(assignment)
    session.flush()
    payload = assignment.to_dict()
    session.add(ScmEvento(
        aggregate_type="ALMACEN_SCOPE", aggregate_id=str(assignment.id),
        tipo="ALMACEN_SCOPE_ASIGNADO", actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor), before_json=before, after_json=payload,
        operation_id=operation.operation_id,
    ))
    return _complete(session, operation, payload)


def my_warehouse_scope(session, *, actor_id):
    scope = warehouse_scope(session, actor_id=actor_id)
    return {
        "configurado": scope["configured"],
        "control_transversal": scope["transversal"],
        "almacenes": [
            {**item.almacen.to_dict(), "clases_articulo": list(item.clases_articulo_json or [])}
            for item in scope["assignments"]
        ],
    }
