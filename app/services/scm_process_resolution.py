"""Canonical process and route snapshot resolution for OF/OT."""

from sqlalchemy import select

from app.models.scm_rutas import (
    ScmOperacionRuta,
    ScmRutaRevision,
    ESTADO_RUTA_APROBADA,
    ESTADO_RUTA_RETIRADA,
)
from app.services.scm_service_support import ScmServiceError

PROCESS_TYPES = {"INYECCION", "SOPLADO"}


def normalize_process(value):
    return str(value or "").strip().upper() or None


def resolve_route_operation(
    session,
    operation_id,
    *,
    lock=False,
    allow_retired=False,
):
    """Load and validate an approved OP_OT route operation."""
    if operation_id is None:
        return None
    route_id = session.scalar(
        select(ScmOperacionRuta.ruta_id).where(ScmOperacionRuta.id == operation_id)
    )
    if lock and route_id is not None:
        session.scalar(
            select(ScmRutaRevision)
            .where(ScmRutaRevision.id == route_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    statement = select(ScmOperacionRuta).where(ScmOperacionRuta.id == operation_id)
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    route_operation = session.scalar(statement)
    if lock and route_operation is not None:
        session.expire(route_operation, ["ruta"])
    if route_operation is None or route_operation.ruta is None:
        raise ScmServiceError(
            "ROUTE_OPERATION_NOT_FOUND",
            "La operacion de ruta indicada no existe.",
            status_code=422,
        )
    valid_states = {ESTADO_RUTA_APROBADA}
    if allow_retired:
        valid_states.add(ESTADO_RUTA_RETIRADA)
    if route_operation.ruta.estado not in valid_states:
        raise ScmServiceError(
            "ROUTE_OPERATION_NOT_APPROVED",
            "La operacion de ruta debe pertenecer a una revision aprobada.",
            status_code=422,
        )
    if route_operation.executor_kind != "OP_OT" or route_operation.tipo not in PROCESS_TYPES:
        raise ScmServiceError(
            "ROUTE_OPERATION_PROCESS_INVALID",
            "La operacion de ruta debe ser OP_OT de INYECCION o SOPLADO.",
            status_code=422,
        )
    content_hash = route_operation.ruta.content_hash
    if not content_hash:
        raise ScmServiceError(
            "ROUTE_REVISION_HASH_REQUIRED",
            "La revision aprobada no tiene hash de contenido.",
            status_code=422,
        )
    return route_operation


def route_snapshot(route_operation):
    if route_operation is None:
        return None, None
    return route_operation.tipo, route_operation.ruta.content_hash


def resolve_order_process(order, *, strict=False):
    """Resolve persisted OF process while keeping legacy reads diagnosable."""
    fabrication = order.fabricacion
    if fabrication is not None and normalize_process(fabrication.snapshot_proceso):
        return normalize_process(fabrication.snapshot_proceso), fabrication.fuente_proceso, None
    operation = getattr(order, "operacion_ruta_revision", None)
    if getattr(order, "estado", None) == "BORRADOR":
        if getattr(order, "origen_demanda", None) in {
            "ORDEN_PRODUCCION",
            "REEMPLAZO_OF",
        } and operation is not None and operation.ruta is not None:
            process = normalize_process(operation.tipo)
            if process in PROCESS_TYPES:
                return process, "RUTA_CABECERA", None
        return None, None, "LEGACY_PENDIENTE"
    if operation is not None and operation.ruta is not None:
        process = normalize_process(operation.tipo)
        if process in PROCESS_TYPES:
            return process, "RUTA_CABECERA", None
    if strict:
        raise ScmServiceError(
            "PROCESS_REQUIRED",
            "La OF requiere proceso explicito o una operacion de ruta aprobada.",
            status_code=422,
        )
    if (
        getattr(order, "origen_demanda", None) == "EXCEPCIONAL"
        and fabrication is not None
        and fabrication.molde_id
    ):
        return "INYECCION", None, "LEGACY_ASUMIDO_INYECCION"
    return None, None, "LEGACY_PENDIENTE"


def route_operation_dto(route_operation):
    if route_operation is None:
        return None
    route = route_operation.ruta
    return {
        "id": route_operation.id,
        "clave": route_operation.clave,
        "nombre": route_operation.nombre,
        "tipo": route_operation.tipo,
        "executor_kind": route_operation.executor_kind,
        "ruta_revision_id": route.id,
        "ruta_numero_revision": route.numero_revision,
        "ruta_content_hash": route.content_hash,
        "articulo_salida_id": route_operation.articulo_salida_id,
        "articulo_salida": (
            {
                "id": route_operation.articulo_salida.id,
                "codigo": route_operation.articulo_salida.codigo,
                "nombre": route_operation.articulo_salida.nombre,
                "clase": route_operation.articulo_salida.clase,
            }
            if route_operation.articulo_salida is not None else None
        ),
    }
