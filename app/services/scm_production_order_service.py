"""Application service for ProductoTerminado demand orders (TS-010P)."""

import copy
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from sqlalchemy import select

from app.models.producto import ProductoTerminado
from app.models.scm_commercial import ScmPresentacionComercial
from app.models.scm_articulos import ScmArticulo, ScmArticuloProducto
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    ScmPlanProduccion,
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
    utc_now,
)
from app.models.scm_rutas import (
    EXECUTOR_OP_OT,
    ScmRutaRevision,
)
from app.models.scm_estructuras import ScmEstructuraRevision
from app.models.scm_inventory import (
    ScmReservaInventario,
    ScmSaldoInventario,
)
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)


def _json_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _reserve_operation(session, operation_id, endpoint, actor, data):
    request_hash = _json_hash({
        "endpoint": endpoint,
        "actor_id": actor.id,
        "data": data,
    })
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        if (
            existing.endpoint != endpoint
            or existing.request_sha256 != request_hash
        ):
            raise ScmServiceError(
                "IDEMPOTENCY_CONFLICT",
                "La clave idempotente ya fue usada con otra solicitud.",
                status_code=409,
            )
        if existing.response_json is None:
            raise ScmServiceError(
                "IDEMPOTENCY_OPERATION_INCOMPLETE",
                "La operacion previa aun no tiene resultado.",
                status_code=409,
            )
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


def _parse_date(value, field):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_DATE",
            f"{field} debe usar YYYY-MM-DD.",
            status_code=422,
            details={"field": field},
        ) from error


def _quantity(value):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            "cantidad_solicitada debe ser un entero positivo.",
            status_code=422,
        ) from error
    if (
        not parsed.is_finite()
        or parsed <= 0
        or parsed != parsed.to_integral_value()
    ):
        raise ScmServiceError(
            "INVALID_QUANTITY",
            "cantidad_solicitada debe ser un entero positivo.",
            status_code=422,
        )
    return parsed.quantize(Decimal("0.001"))


def _nonnegative_quantity(value, *, field="cantidad_objetivo"):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_TARGET_QUANTITY",
            f"{field} debe ser un entero mayor o igual a cero.",
            status_code=422,
            details={"field": field},
        ) from error
    if (
        not parsed.is_finite()
        or parsed < 0
        or parsed != parsed.to_integral_value()
    ):
        raise ScmServiceError(
            "INVALID_TARGET_QUANTITY",
            f"{field} debe ser un entero mayor o igual a cero.",
            status_code=422,
            details={"field": field},
        )
    return parsed.quantize(Decimal("0.001"))


def _iso(value):
    return value.isoformat() if value is not None else None


def _serialize_line(line):
    planned = sum(
        (
            Decimal(item.cantidad_planificada)
            for item in line.asignaciones
            if item.estado != "CANCELADA"
        ),
        Decimal("0"),
    )
    planned += sum(
        (
            Decimal(item.cantidad)
            for item in line.reservas_inventario
            if item.estado == "RESERVADA" and item.uso == "DEMANDA_DIRECTA"
        ),
        Decimal("0"),
    )
    committed = sum(
        (
            Decimal(item.cantidad_comprometida)
            for item in line.asignaciones
            if item.estado != "CANCELADA"
        ),
        Decimal("0"),
    )
    satisfied = sum(
        (
            Decimal(item.cantidad_satisfecha)
            for item in line.asignaciones
            if item.estado != "CANCELADA"
        ),
        Decimal("0"),
    )
    return {
        "id": str(line.id),
        "producto_terminado_id": line.producto_terminado_id,
        "producto": (
            line.producto_terminado.producto
            if line.producto_terminado
            else None
        ),
        "cantidad_solicitada": format(line.cantidad_solicitada, "f"),
        "presentacion_comercial": (
            {
                "id": line.presentacion_comercial_id,
                "codigo": line.snapshot_presentacion_codigo,
                "nombre": line.snapshot_presentacion_nombre,
                "unidades_base": line.snapshot_unidades_por_presentacion,
                "cantidad": line.cantidad_presentaciones,
            }
            if line.presentacion_comercial_id is not None
            else None
        ),
        "fecha_necesidad": _iso(line.fecha_necesidad),
        "estructura_revision_id": line.estructura_revision_id,
        "estructura_hash": line.estructura_hash,
        "ruta_revision_id": line.ruta_revision_id,
        "ruta_hash": line.ruta_hash,
        "estado": line.estado,
        "version": line.version,
        "cobertura": {
            "planificada": format(planned, "f"),
            "comprometida": format(committed, "f"),
            "satisfecha": format(satisfied, "f"),
            "pendiente": format(
                max(Decimal(line.cantidad_solicitada) - satisfied, Decimal(0)),
                "f",
            ),
        },
    }


def _serialize(order):
    active_plan = next(
        (
            plan for plan in reversed(order.planes)
            if plan.estado in ("CALCULADO", "CONFIRMADO")
        ),
        None,
    )
    return {
        "id": str(order.id),
        "codigo": order.codigo,
        "origen": order.origen,
        "referencia_origen": order.referencia_origen,
        "fecha_necesidad": _iso(order.fecha_necesidad),
        "prioridad": order.prioridad,
        "estado": order.estado,
        "version": order.version,
        "created_by_id": order.created_by_id,
        "approved_by_id": order.approved_by_id,
        "approved_at": _iso(order.approved_at),
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
        "lineas": [_serialize_line(line) for line in order.lineas],
        "plan_activo": (
            {
                "id": str(active_plan.id),
                "revision": active_plan.revision,
                "estado": active_plan.estado,
                "content_hash": active_plan.content_hash,
            }
            if active_plan else None
        ),
    }


def _load_order(session, order_id):
    order = session.get(ScmOrdenProduccion, order_id)
    if order is None:
        raise ScmServiceError(
            "OP_NOT_FOUND",
            "La orden de produccion no existe.",
            status_code=404,
        )
    return order


def _approved_snapshots(session, product_id):
    link = session.scalar(
        select(ScmArticuloProducto).where(
            ScmArticuloProducto.producto_terminado_id == product_id
        )
    )
    if link is None:
        return None, None
    structure = session.scalar(
        select(ScmEstructuraRevision)
        .where(
            ScmEstructuraRevision.articulo_resultado_id == link.articulo_id,
            ScmEstructuraRevision.estado == "APROBADA",
        )
        .order_by(ScmEstructuraRevision.numero_revision.desc())
    )
    route = session.scalar(
        select(ScmRutaRevision)
        .where(
            ScmRutaRevision.articulo_objetivo_id == link.articulo_id,
            ScmRutaRevision.estado == "APROBADA",
        )
        .order_by(ScmRutaRevision.numero_revision.desc())
    )
    return structure, route


def create_production_order(
    session,
    *,
    actor_id,
    operation_id,
    data,
):
    try:
        actor = load_actor(session, actor_id, capability="OP_CREAR")
        reject_unknown_fields(
            data,
            allowed={
                "origen",
                "referencia_origen",
                "fecha_necesidad",
                "prioridad",
                "lineas",
            },
        )
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /ordenes-produccion",
            actor,
            data,
        )
        if replay is not None:
            return replay
        raw_lines = data.get("lineas")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ScmServiceError(
                "OP_LINE_REQUIRED",
                "La OP debe contener al menos una linea de PT.",
                status_code=422,
            )
        need_date = _parse_date(data.get("fecha_necesidad"), "fecha_necesidad")
        order = ScmOrdenProduccion(
            codigo=generar_codigo_catalogo(
                "ORDEN_PRODUCCION",
                session=session,
            ),
            origen=required_text(
                data.get("origen"),
                field="origen",
                max_length=32,
            ).upper(),
            referencia_origen=(
                required_text(
                    data["referencia_origen"],
                    field="referencia_origen",
                    max_length=100,
                )
                if data.get("referencia_origen")
                else None
            ),
            fecha_necesidad=need_date,
            prioridad=str(data.get("prioridad") or "NORMAL").strip().upper(),
            created_by_id=actor.id,
        )
        seen_products = set()
        for raw_line in raw_lines:
            if not isinstance(raw_line, dict):
                raise ScmServiceError(
                    "INVALID_OP_LINE",
                    "Cada linea debe ser un objeto JSON.",
                    status_code=400,
                )
            reject_unknown_fields(
                raw_line,
                allowed={
                    "producto_terminado_id",
                    "cantidad_solicitada",
                    "presentacion_comercial_id",
                    "cantidad_presentaciones",
                    "fecha_necesidad",
                },
            )
            product_id = required_text(
                raw_line.get("producto_terminado_id"),
                field="producto_terminado_id",
                max_length=50,
            )
            if product_id in seen_products:
                raise ScmServiceError(
                    "DUPLICATE_OP_PRODUCT",
                    "Un PT solo puede aparecer una vez en la OP.",
                    status_code=422,
                    details={"producto_terminado_id": product_id},
                )
            product = session.get(ProductoTerminado, product_id)
            if product is None:
                raise ScmServiceError(
                    "PRODUCT_NOT_FOUND",
                    "El ProductoTerminado no existe.",
                    status_code=422,
                    details={"producto_terminado_id": product_id},
                )
            presentation = None
            presentation_count = None
            has_presentation = raw_line.get("presentacion_comercial_id") is not None
            has_presentation_count = raw_line.get("cantidad_presentaciones") is not None
            if has_presentation != has_presentation_count:
                raise ScmServiceError(
                    "COMMERCIAL_PRESENTATION_QUANTITY_REQUIRED",
                    "Selecciona la presentacion y su cantidad.",
                    status_code=422,
                )
            if has_presentation:
                presentation_id = raw_line.get("presentacion_comercial_id")
                if not isinstance(presentation_id, int) or isinstance(presentation_id, bool):
                    raise ScmServiceError(
                        "INVALID_COMMERCIAL_PRESENTATION",
                        "presentacion_comercial_id debe ser un entero.",
                        status_code=422,
                    )
                presentation = session.get(
                    ScmPresentacionComercial,
                    presentation_id,
                )
                if (
                    presentation is None
                    or not presentation.activo
                    or presentation.producto_terminado_id != product_id
                ):
                    raise ScmServiceError(
                        "COMMERCIAL_PRESENTATION_NOT_AVAILABLE",
                        "La presentacion no esta activa para el producto seleccionado.",
                        status_code=422,
                    )
                presentation_count = _quantity(
                    raw_line.get("cantidad_presentaciones")
                )
                requested_quantity = _quantity(
                    presentation_count * presentation.unidades_base
                )
            else:
                requested_quantity = _quantity(
                    raw_line.get("cantidad_solicitada")
                )
            structure, route = _approved_snapshots(session, product_id)
            order.lineas.append(ScmOrdenProduccionLinea(
                producto_terminado_id=product_id,
                cantidad_solicitada=requested_quantity,
                presentacion_comercial_id=(presentation.id if presentation else None),
                cantidad_presentaciones=(
                    int(presentation_count) if presentation_count is not None else None
                ),
                snapshot_presentacion_codigo=(
                    presentation.codigo if presentation else None
                ),
                snapshot_presentacion_nombre=(
                    presentation.nombre if presentation else None
                ),
                snapshot_unidades_por_presentacion=(
                    presentation.unidades_base if presentation else None
                ),
                fecha_necesidad=(
                    _parse_date(
                        raw_line["fecha_necesidad"],
                        "lineas.fecha_necesidad",
                    )
                    if raw_line.get("fecha_necesidad")
                    else None
                ),
                estructura_revision_id=structure.id if structure else None,
                estructura_hash=structure.content_hash if structure else None,
                ruta_revision_id=route.id if route else None,
                ruta_hash=route.content_hash if route else None,
            ))
            seen_products.add(product_id)
        session.add(order)
        session.flush()
        response = _serialize(order)
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="ORDEN_PRODUCCION",
            aggregate_id=str(order.id),
            tipo="OP_CREATED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def list_production_orders(session, *, actor_id):
    load_actor(session, actor_id, capability="OP_VER")
    orders = session.scalars(
        select(ScmOrdenProduccion).order_by(
            ScmOrdenProduccion.created_at.desc()
        )
    ).all()
    return {"items": [_serialize(order) for order in orders]}


def get_production_order(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="OP_VER")
    return _serialize(_load_order(session, order_id))


def get_production_plan(session, *, actor_id, order_id):
    load_actor(session, actor_id, capability="OP_VER")
    _load_order(session, order_id)
    plan = session.scalar(
        select(ScmPlanProduccion)
        .where(
            ScmPlanProduccion.orden_produccion_id == order_id,
            ScmPlanProduccion.estado.in_(("CALCULADO", "CONFIRMADO")),
        )
        .order_by(ScmPlanProduccion.revision.desc())
    )
    return {"plan": _plan_payload(plan) if plan else None}


def approve_production_order(
    session,
    *,
    actor_id,
    operation_id,
    order_id,
    expected_resource_version,
):
    try:
        actor = load_actor(session, actor_id, capability="OP_APROBAR")
        data = {
            "order_id": str(order_id),
            "version": expected_version(expected_resource_version),
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "POST /ordenes-produccion/{id}/aprobar",
            actor,
            data,
        )
        if replay is not None:
            return replay
        order = _load_order(session, order_id)
        if order.version != data["version"]:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OP fue modificada por otro usuario.",
                status_code=409,
            )
        if order.estado != "BORRADOR":
            raise ScmServiceError(
                "INVALID_OP_STATE",
                "Solo una OP en BORRADOR puede aprobarse.",
                status_code=409,
            )
        if not order.lineas:
            raise ScmServiceError(
                "OP_LINE_REQUIRED",
                "La OP debe contener al menos una linea.",
                status_code=422,
            )
        missing = [
            line.producto_terminado_id
            for line in order.lineas
            if (
                line.estructura_revision_id is None
                or not line.estructura_hash
                or line.ruta_revision_id is None
                or not line.ruta_hash
            )
        ]
        if missing:
            raise ScmServiceError(
                "STRUCTURE_REVISION_INVALID",
                "Todos los PT requieren BOM y ruta aprobadas.",
                status_code=422,
                details={"productos": missing},
            )
        order.estado = "APROBADA"
        order.approved_by_id = actor.id
        order.approved_at = utc_now()
        order.version += 1
        session.flush()
        response = _serialize(order)
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 200
        session.add(ScmEvento(
            aggregate_type="ORDEN_PRODUCCION",
            aggregate_id=str(order.id),
            tipo="OP_APPROVED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=response,
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _ceil_units(value):
    return Decimal(value).to_integral_value(rounding=ROUND_CEILING)


def _plan_payload(plan):
    return {
        "id": str(plan.id),
        "orden_produccion_id": str(plan.orden_produccion_id),
        "revision": plan.revision,
        "estado": plan.estado,
        "input_hash": plan.input_hash,
        "content_hash": plan.content_hash,
        "propuesta": copy.deepcopy(plan.propuesta_json),
        "calculado_por_id": plan.calculado_por_id,
        "confirmado_por_id": plan.confirmado_por_id,
        "created_at": _iso(plan.created_at),
        "confirmado_at": _iso(plan.confirmado_at),
    }


def _expand_requirements(
    session,
    *,
    article_id,
    quantity,
    structure_id,
    structure_by_output,
    requirements,
    visiting,
    line_id,
    inventory,
    stock_uses,
    proposal_key_by_article,
):
    if article_id in visiting:
        raise ScmServiceError(
            "STRUCTURE_CYCLE",
            "La estructura congelada contiene un ciclo.",
            status_code=422,
        )
    structure = session.get(ScmEstructuraRevision, structure_id)
    if (
        structure is None
        or structure.articulo_resultado_id != article_id
        or not structure.content_hash
    ):
        raise ScmServiceError(
            "STRUCTURE_REVISION_INVALID",
            "La estructura congelada ya no es resoluble.",
            status_code=409,
            details={"article_id": article_id, "structure_id": structure_id},
        )
    visiting.add(article_id)
    for component in structure.componentes:
        factor = Decimal(component.cantidad)
        waste = Decimal(component.merma_tecnica_pct or 0)
        required = Decimal(quantity) * factor
        if waste:
            required = required / (Decimal("1") - waste / Decimal("100"))
        required = _ceil_units(required)
        child_id = component.articulo_componente_id
        uncovered = _cover_from_inventory(
            inventory,
            stock_uses,
            line_id=line_id,
            article_id=child_id,
            quantity=required,
            use="INPUT_OPERACION",
            consumer_key=proposal_key_by_article.get(article_id),
        )
        requirements[child_id] += uncovered
        child_structure = structure_by_output.get(child_id)
        if child_structure is not None and uncovered > 0:
            _expand_requirements(
                session,
                article_id=child_id,
                quantity=uncovered,
                structure_id=child_structure,
                structure_by_output=structure_by_output,
                requirements=requirements,
                visiting=visiting,
                line_id=line_id,
                inventory=inventory,
                stock_uses=stock_uses,
                proposal_key_by_article=proposal_key_by_article,
            )
    visiting.remove(article_id)


def _inventory_pool(session):
    rows = session.scalars(
        select(ScmSaldoInventario).order_by(ScmSaldoInventario.id)
    ).all()
    pool = defaultdict(list)
    snapshot = []
    for row in rows:
        free = max(
            Decimal(row.cantidad_fisica)
            - Decimal(row.cantidad_reservada)
            - Decimal(row.cantidad_no_disponible),
            Decimal("0"),
        )
        if free <= 0:
            continue
        pool[row.articulo_scm_id].append({
            "saldo_id": str(row.id),
            "available": free,
        })
        snapshot.append({
            "saldo_id": str(row.id),
            "articulo_scm_id": row.articulo_scm_id,
            "version": row.version,
            "cantidad_libre": format(free, "f"),
        })
    return pool, snapshot


def _cover_from_inventory(
    inventory,
    stock_uses,
    *,
    line_id,
    article_id,
    quantity,
    use,
    consumer_key=None,
):
    remaining = Decimal(quantity)
    for source in inventory.get(article_id, []):
        if remaining <= 0:
            break
        take = min(source["available"], remaining)
        if take <= 0:
            continue
        source["available"] -= take
        remaining -= take
        stock_uses.append({
            "saldo_id": source["saldo_id"],
            "linea_id": str(line_id),
            "articulo_scm_id": article_id,
            "uso": use,
            "propuesta_consumidora_clave": consumer_key,
            "cantidad": format(take, "f"),
            "cantidad_calculada": format(take, "f"),
        })
    return remaining


def _build_plan_proposal(session, order):
    proposals = {}
    allocations = []
    blockers = []
    input_lines = []
    stock_uses = []
    inventory, inventory_snapshot = _inventory_pool(session)
    for line in order.lineas:
        route = session.get(ScmRutaRevision, line.ruta_revision_id)
        structure = session.get(
            ScmEstructuraRevision,
            line.estructura_revision_id,
        )
        if (
            route is None
            or structure is None
            or route.content_hash != line.ruta_hash
            or structure.content_hash != line.estructura_hash
        ):
            raise ScmServiceError(
                "PLANNING_SNAPSHOT_DRIFT",
                "Los snapshots BOM/ruta de la OP no coinciden.",
                status_code=409,
                details={"linea_id": str(line.id)},
            )
        operations = list(route.operaciones)
        output_ids = {item.articulo_salida_id for item in operations}
        outgoing = {
            edge.operacion_anterior_id for edge in route.precedencias
        }
        terminals = [item for item in operations if item.id not in outgoing]
        if len(terminals) != 1:
            raise ScmServiceError(
                "ROUTE_TERMINAL_INVALID",
                "La ruta congelada no tiene un terminal único.",
                status_code=409,
            )
        structure_by_output = {
            item.articulo_salida_id: item.estructura_revision_id
            for item in operations
            if item.estructura_revision_id is not None
        }
        requirements = defaultdict(Decimal)
        root_id = route.articulo_objetivo_id
        requested = Decimal(line.cantidad_solicitada)
        proposal_key_by_article = {
            item.articulo_salida_id: f"R{route.id}-O{item.id}"
            for item in operations
        }
        root_uncovered = _cover_from_inventory(
            inventory,
            stock_uses,
            line_id=line.id,
            article_id=root_id,
            quantity=requested,
            use="DEMANDA_DIRECTA",
        )
        requirements[root_id] += root_uncovered
        if root_uncovered > 0:
            _expand_requirements(
                session,
                article_id=root_id,
                quantity=root_uncovered,
                structure_id=line.estructura_revision_id,
                structure_by_output=structure_by_output,
                requirements=requirements,
                visiting=set(),
                line_id=line.id,
                inventory=inventory,
                stock_uses=stock_uses,
                proposal_key_by_article=proposal_key_by_article,
            )
        missing = sorted(
            article_id
            for article_id in requirements
            if article_id not in output_ids
        )
        for article_id in missing:
            article = session.get(ScmArticulo, article_id)
            blockers.append({
                "codigo": "ROUTE_OUTPUT_MISSING",
                "linea_id": str(line.id),
                "articulo_id": article_id,
                "articulo_codigo": article.codigo if article else None,
                "cantidad": format(requirements[article_id], "f"),
            })
        for route_operation in operations:
            quantity = requirements.get(
                route_operation.articulo_salida_id,
                Decimal("0"),
            )
            if quantity <= 0:
                continue
            key = f"R{route.id}-O{route_operation.id}"
            proposal = proposals.setdefault(key, {
                "clave": key,
                "tipo": (
                    "FABRICACION"
                    if route_operation.executor_kind == EXECUTOR_OP_OT
                    else "ENSAMBLE"
                ),
                "ruta_revision_id": route.id,
                "ruta_hash": route.content_hash,
                "operacion_ruta_id": route_operation.id,
                "operacion": route_operation.nombre,
                "tipo_operacion": route_operation.tipo,
                "articulo_scm_id": route_operation.articulo_salida_id,
                "articulo": {
                    "codigo": route_operation.articulo_salida.codigo,
                    "nombre": route_operation.articulo_salida.nombre,
                    "clase": route_operation.articulo_salida.clase,
                },
                "cantidad_objetivo": "0",
                "cantidad_calculada": "0",
                "aportes_demanda": [],
                "requiere_configuracion_tecnica": (
                    route_operation.executor_kind == EXECUTOR_OP_OT
                ),
            })
            total = Decimal(proposal["cantidad_objetivo"]) + quantity
            proposal["cantidad_objetivo"] = format(total, "f")
            proposal["cantidad_calculada"] = format(total, "f")
            proposal["aportes_demanda"].append({
                "linea_id": str(line.id),
                "cantidad": format(quantity, "f"),
            })
        terminal = terminals[0]
        terminal_key = f"R{route.id}-O{terminal.id}"
        if root_uncovered > 0:
            allocations.append({
                "linea_id": str(line.id),
                "propuesta_clave": terminal_key,
                "cantidad": format(root_uncovered, "f"),
            })
        input_lines.append({
            "linea_id": str(line.id),
            "cantidad": format(requested, "f"),
            "estructura_revision_id": line.estructura_revision_id,
            "estructura_hash": line.estructura_hash,
            "ruta_revision_id": line.ruta_revision_id,
            "ruta_hash": line.ruta_hash,
        })
    return {
        "version": 2,
        "politica_stock": "KARDEX_NORMALIZADO",
        "documentos": sorted(
            proposals.values(),
            key=lambda item: (
                item["ruta_revision_id"],
                item["operacion_ruta_id"],
            ),
        ),
        "asignaciones_demanda": allocations,
        "reservas_stock": stock_uses,
        "bloqueos": blockers,
        "inputs": {
            "lineas": input_lines,
            "inventario": inventory_snapshot,
        },
    }


def _redistribute_terminal_allocations(proposal):
    """Cap demand allocations by the selected terminal output target."""
    remaining = {
        item["clave"]: Decimal(item["cantidad_objetivo"])
        for item in proposal.get("documentos", [])
    }
    adjusted = []
    for item in proposal.get("asignaciones_demanda", []):
        key = item["propuesta_clave"]
        available = max(remaining.get(key, Decimal("0")), Decimal("0"))
        quantity = min(Decimal(item["cantidad"]), available)
        remaining[key] = available - quantity
        if quantity > 0:
            adjusted.append({**item, "cantidad": format(quantity, "f")})
    proposal["asignaciones_demanda"] = adjusted
    documents = {
        item["clave"]: item for item in proposal.get("documentos", [])
    }
    for reservation in proposal.get("reservas_stock", []):
        consumer_key = reservation.get("propuesta_consumidora_clave")
        if not consumer_key:
            continue
        consumer = documents.get(consumer_key)
        if consumer is None:
            continue
        calculated_target = Decimal(
            consumer.get("cantidad_calculada", consumer["cantidad_objetivo"])
        )
        selected_target = Decimal(consumer["cantidad_objetivo"])
        ratio = (
            min(selected_target / calculated_target, Decimal("1"))
            if calculated_target > 0 else Decimal("0")
        )
        calculated_stock = Decimal(
            reservation.get("cantidad_calculada", reservation["cantidad"])
        )
        reservation["cantidad"] = format(
            (calculated_stock * ratio).quantize(Decimal("0.001")),
            "f",
        )


def adjust_production_plan_targets(
    session,
    *,
    actor_id,
    operation_id,
    order_id,
    data,
):
    actor = load_actor(
        session,
        actor_id,
        capability="PLANIFICACION_CALCULAR",
    )
    reject_unknown_fields(
        data,
        allowed={
            "version",
            "plan_id",
            "content_hash",
            "ajustes",
            "motivo",
        },
    )
    reason = required_text(
        data.get("motivo"),
        field="motivo",
        max_length=240,
    )
    raw_adjustments = data.get("ajustes")
    if not isinstance(raw_adjustments, list) or not raw_adjustments:
        raise ScmServiceError(
            "PLAN_TARGETS_REQUIRED",
            "Incluye al menos una meta de fabricacion o armado.",
            status_code=422,
        )
    command = {
        "order_id": str(order_id),
        "version": expected_version(data.get("version")),
        "plan_id": required_text(
            data.get("plan_id"),
            field="plan_id",
            max_length=36,
        ),
        "content_hash": required_text(
            data.get("content_hash"),
            field="content_hash",
            max_length=64,
        ),
        "ajustes": raw_adjustments,
        "motivo": reason,
    }
    audit, replay = _reserve_operation(
        session,
        operation_id,
        "POST /ordenes-produccion/{id}/ajustar-metas",
        actor,
        command,
    )
    if replay is not None:
        return replay
    try:
        try:
            plan_id = uuid.UUID(command["plan_id"])
        except ValueError as error:
            raise ScmServiceError(
                "INVALID_UUID",
                "plan_id debe ser un UUID valido.",
                status_code=400,
            ) from error
        order = session.scalar(
            select(ScmOrdenProduccion)
            .where(ScmOrdenProduccion.id == order_id)
            .with_for_update()
        )
        plan = session.scalar(
            select(ScmPlanProduccion)
            .where(
                ScmPlanProduccion.id == plan_id,
                ScmPlanProduccion.orden_produccion_id == order_id,
            )
            .with_for_update()
        )
        if order is None or plan is None:
            raise ScmServiceError(
                "PLAN_NOT_FOUND",
                "El plan calculado no existe para la OP.",
                status_code=404,
            )
        if order.version != command["version"] or order.estado != "APROBADA":
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OP cambio desde el calculo del plan.",
                status_code=409,
            )
        if (
            plan.estado != "CALCULADO"
            or plan.content_hash != command["content_hash"]
        ):
            raise ScmServiceError(
                "PLAN_STALE",
                "El plan fue superado o su hash no coincide.",
                status_code=409,
            )
        proposal = copy.deepcopy(plan.propuesta_json)
        documents = {
            item["clave"]: item for item in proposal.get("documentos", [])
        }
        seen = set()
        change_log = []
        now = _iso(utc_now())
        for raw_item in raw_adjustments:
            if not isinstance(raw_item, dict):
                raise ScmServiceError(
                    "INVALID_PLAN_TARGET",
                    "Cada ajuste debe ser un objeto JSON.",
                    status_code=400,
                )
            reject_unknown_fields(
                raw_item,
                allowed={"clave", "cantidad_objetivo"},
            )
            key = required_text(
                raw_item.get("clave"),
                field="ajustes.clave",
                max_length=100,
            )
            if key in seen or key not in documents:
                raise ScmServiceError(
                    "INVALID_PLAN_TARGET",
                    "La meta no pertenece una sola vez al plan activo.",
                    status_code=422,
                    details={"clave": key},
                )
            quantity = _nonnegative_quantity(
                raw_item.get("cantidad_objetivo"),
                field="ajustes.cantidad_objetivo",
            )
            document = documents[key]
            previous = Decimal(document["cantidad_objetivo"])
            calculated = Decimal(
                document.get("cantidad_calculada", document["cantidad_objetivo"])
            )
            if quantity > calculated:
                raise ScmServiceError(
                    "PLAN_TARGET_EXCEEDS_CALCULATED",
                    "La meta manual no puede superar la sugerencia. "
                    "Aumenta la demanda o recalcula el plan.",
                    status_code=422,
                    details={
                        "clave": key,
                        "cantidad_calculada": format(calculated, "f"),
                    },
                )
            document["cantidad_calculada"] = format(calculated, "f")
            document["cantidad_objetivo"] = format(quantity, "f")
            document["ajuste_manual"] = {
                "cantidad_anterior": format(previous, "f"),
                "cantidad_calculada": format(calculated, "f"),
                "motivo": reason,
                "actor_id": actor.id,
                "fecha": now,
            }
            change_log.append({
                "clave": key,
                "cantidad_anterior": format(previous, "f"),
                "cantidad_objetivo": format(quantity, "f"),
            })
            seen.add(key)
        _redistribute_terminal_allocations(proposal)
        proposal["ajustes"] = [
            *proposal.get("ajustes", []),
            {
                "motivo": reason,
                "actor_id": actor.id,
                "fecha": now,
                "metas": change_log,
            },
        ]
        proposal["version"] = max(int(proposal.get("version", 1)), 2)
        plan.estado = "SUPERADO"
        revised = ScmPlanProduccion(
            orden_produccion=order,
            revision=plan.revision + 1,
            input_hash=plan.input_hash,
            content_hash=_json_hash(proposal),
            propuesta_json=proposal,
            calculado_por_id=actor.id,
            operation_id=audit.operation_id,
        )
        session.add(revised)
        session.flush()
        response = {
            "plan": _plan_payload(revised),
            "orden": _serialize(order),
        }
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="PLAN_PRODUCCION",
            aggregate_id=str(revised.id),
            tipo="PRODUCTION_PLAN_TARGETS_ADJUSTED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            before_json=_plan_payload(plan),
            after_json=response["plan"],
            operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def calculate_production_plan(
    session,
    *,
    actor_id,
    operation_id,
    order_id,
    expected_resource_version,
):
    actor = load_actor(
        session,
        actor_id,
        capability="PLANIFICACION_CALCULAR",
    )
    data = {
        "order_id": str(order_id),
        "version": expected_version(expected_resource_version),
    }
    operation, replay = _reserve_operation(
        session,
        operation_id,
        "POST /ordenes-produccion/{id}/calcular-plan",
        actor,
        data,
    )
    if replay is not None:
        return replay
    try:
        order = session.scalar(
            select(ScmOrdenProduccion)
            .where(ScmOrdenProduccion.id == order_id)
            .with_for_update()
        )
        if order is None:
            raise ScmServiceError(
                "OP_NOT_FOUND",
                "La orden de produccion no existe.",
                status_code=404,
            )
        if order.version != data["version"]:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OP fue modificada por otro usuario.",
                status_code=409,
            )
        if order.estado != "APROBADA":
            raise ScmServiceError(
                "INVALID_OP_STATE",
                "Solo una OP aprobada puede calcular planificación.",
                status_code=409,
            )
        proposal = _build_plan_proposal(session, order)
        input_hash = _json_hash(proposal["inputs"])
        content_hash = _json_hash(proposal)
        previous = session.scalar(
            select(ScmPlanProduccion)
            .where(
                ScmPlanProduccion.orden_produccion_id == order.id,
                ScmPlanProduccion.estado == "CALCULADO",
            )
            .with_for_update()
        )
        revision = previous.revision + 1 if previous else 1
        if previous is not None:
            previous.estado = "SUPERADO"
        plan = ScmPlanProduccion(
            orden_produccion=order,
            revision=revision,
            input_hash=input_hash,
            content_hash=content_hash,
            propuesta_json=proposal,
            calculado_por_id=actor.id,
            operation_id=operation.operation_id,
        )
        session.add(plan)
        session.flush()
        response = {"plan": _plan_payload(plan), "orden": _serialize(order)}
        operation.response_json = copy.deepcopy(response)
        operation.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="PLAN_PRODUCCION",
            aggregate_id=str(plan.id),
            tipo="PRODUCTION_PLAN_CALCULATED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=response["plan"],
            operation_id=operation.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def _proposal_color_and_weight(article):
    if article.pieza_color is None:
        return None, None
    piece_color = article.pieza_color.pieza_color
    return piece_color.color_produccion_id, piece_color.peso


def confirm_production_plan(
    session,
    *,
    actor_id,
    operation_id,
    order_id,
    data,
):
    actor = load_actor(
        session,
        actor_id,
        capability="PLANIFICACION_CONFIRMAR",
    )
    reject_unknown_fields(
        data,
        allowed={"version", "plan_id", "content_hash"},
    )
    command = {
        "order_id": str(order_id),
        "version": expected_version(data.get("version")),
        "plan_id": required_text(
            data.get("plan_id"),
            field="plan_id",
            max_length=36,
        ),
        "content_hash": required_text(
            data.get("content_hash"),
            field="content_hash",
            max_length=64,
        ),
    }
    audit, replay = _reserve_operation(
        session,
        operation_id,
        "POST /ordenes-produccion/{id}/confirmar-plan",
        actor,
        command,
    )
    if replay is not None:
        return replay
    try:
        try:
            plan_id = uuid.UUID(command["plan_id"])
        except ValueError as error:
            raise ScmServiceError(
                "INVALID_UUID",
                "plan_id debe ser un UUID válido.",
                status_code=400,
            ) from error
        order = session.scalar(
            select(ScmOrdenProduccion)
            .where(ScmOrdenProduccion.id == order_id)
            .with_for_update()
        )
        plan = session.scalar(
            select(ScmPlanProduccion)
            .where(
                ScmPlanProduccion.id == plan_id,
                ScmPlanProduccion.orden_produccion_id == order_id,
            )
            .with_for_update()
        )
        if order is None or plan is None:
            raise ScmServiceError(
                "PLAN_NOT_FOUND",
                "El plan calculado no existe para la OP.",
                status_code=404,
            )
        if order.version != command["version"] or order.estado != "APROBADA":
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OP cambió desde el cálculo del plan.",
                status_code=409,
            )
        if (
            plan.estado != "CALCULADO"
            or plan.content_hash != command["content_hash"]
        ):
            raise ScmServiceError(
                "PLAN_STALE",
                "El plan fue superado o su hash no coincide.",
                status_code=409,
            )
        proposal = plan.propuesta_json
        if proposal.get("bloqueos"):
            raise ScmServiceError(
                "PLAN_HAS_BLOCKERS",
                "La propuesta conserva artículos sin operación de ruta.",
                status_code=422,
                details={"bloqueos": proposal["bloqueos"]},
            )
        reserved_stock = []
        stock_totals = defaultdict(Decimal)
        stock_meta = {}
        for item in proposal.get("reservas_stock", []):
            quantity = Decimal(item["cantidad"])
            if quantity <= 0:
                continue
            key = (
                item["saldo_id"],
                item["linea_id"],
                item["uso"],
            )
            stock_totals[key] += quantity
            stock_meta[key] = item
        order_lines = {str(item.id): item for item in order.lineas}
        for key, quantity in stock_totals.items():
            raw = stock_meta[key]
            balance = session.scalar(
                select(ScmSaldoInventario)
                .where(ScmSaldoInventario.id == uuid.UUID(raw["saldo_id"]))
                .with_for_update()
            )
            line = order_lines.get(raw["linea_id"])
            if balance is None or line is None:
                raise ScmServiceError(
                    "INVENTORY_SNAPSHOT_STALE",
                    "Una fuente de Kardex del plan ya no existe.",
                    status_code=409,
                )
            free = (
                Decimal(balance.cantidad_fisica)
                - Decimal(balance.cantidad_reservada)
                - Decimal(balance.cantidad_no_disponible)
            )
            if free < quantity:
                raise ScmServiceError(
                    "INVENTORY_SNAPSHOT_STALE",
                    "El saldo libre cambio. Recalcula el plan.",
                    status_code=409,
                    details={
                        "articulo_scm_id": balance.articulo_scm_id,
                        "requerido": format(quantity, "f"),
                        "disponible": format(max(free, Decimal("0")), "f"),
                    },
                )
            balance.cantidad_reservada = (
                Decimal(balance.cantidad_reservada) + quantity
            )
            balance.version += 1
            reservation = ScmReservaInventario(
                plan_produccion_id=plan.id,
                orden_produccion_linea=line,
                saldo=balance,
                articulo_scm_id=balance.articulo_scm_id,
                uso=raw["uso"],
                cantidad=quantity,
                actor_id=actor.id,
            )
            session.add(reservation)
            reserved_stock.append({
                "articulo_scm_id": balance.articulo_scm_id,
                "uso": raw["uso"],
                "cantidad": format(quantity, "f"),
            })
        outputs_by_key = {}
        documents = []
        for item in proposal["documentos"]:
            quantity = Decimal(item["cantidad_objetivo"])
            if quantity <= 0:
                continue
            article = session.get(ScmArticulo, item["articulo_scm_id"])
            code_kind = (
                "ORDEN_FABRICACION"
                if item["tipo"] == "FABRICACION"
                else "ORDEN_ENSAMBLE"
            )
            operation_order = ScmOrdenOperacion(
                codigo=generar_codigo_catalogo(code_kind, session=session),
                tipo=item["tipo"],
                origen_demanda="ORDEN_PRODUCCION",
                estado="BORRADOR",
                operacion_ruta_revision_id=item["operacion_ruta_id"],
                operacion_ruta_hash=item["ruta_hash"],
                plan_produccion_id=plan.id,
                propuesta_clave=item["clave"],
                created_by_id=actor.id,
            )
            if item["tipo"] == "FABRICACION":
                fabrication = ScmOrdenFabricacion(
                    orden_operacion=operation_order,
                )
                color_id, unit_weight = _proposal_color_and_weight(article)
                run = ScmCorridaFabricacion(
                    codigo=f"{operation_order.codigo}-C01",
                    secuencia=1,
                    color_produccion_id=color_id,
                    estado="BORRADOR",
                )
                fabrication.corridas.append(run)
                output = ScmOrdenOperacionSalida(
                    orden_operacion=operation_order,
                    corrida_fabricacion=run,
                    articulo_scm_id=article.id,
                    peso_unitario_snapshot_g=unit_weight,
                    cantidad_objetivo=quantity,
                    kg_estandar_objetivo=(
                        quantity * Decimal(str(unit_weight)) / Decimal("1000")
                        if unit_weight is not None else None
                    ),
                )
            else:
                output = ScmOrdenOperacionSalida(
                    orden_operacion=operation_order,
                    articulo_scm_id=article.id,
                    cantidad_objetivo=quantity,
                )
            session.add(operation_order)
            session.flush()
            outputs_by_key[item["clave"]] = output
            documents.append({
                "id": str(operation_order.id),
                "codigo": operation_order.codigo,
                "tipo": operation_order.tipo,
                "estado": operation_order.estado,
                "propuesta_clave": item["clave"],
            })
        for item in proposal["asignaciones_demanda"]:
            line_id = uuid.UUID(item["linea_id"])
            session.add(ScmAsignacionDemandaSuministro(
                orden_produccion_linea_id=line_id,
                fuente_tipo="SALIDA_ORDEN",
                orden_operacion_salida=outputs_by_key[
                    item["propuesta_clave"]
                ],
                cantidad_planificada=Decimal(item["cantidad"]),
                operation_id=uuid.uuid5(
                    operation_id,
                    f"{item['linea_id']}:{item['propuesta_clave']}",
                ),
            ))
        plan.estado = "CONFIRMADO"
        plan.confirmado_por_id = actor.id
        plan.confirmado_at = utc_now()
        order.estado = "PLANIFICADA"
        order.version += 1
        session.flush()
        response = {
            "plan": _plan_payload(plan),
            "orden": _serialize(order),
            "documentos": documents,
            "reservas_stock": reserved_stock,
        }
        audit.response_json = copy.deepcopy(response)
        audit.estado_http = 201
        session.add(ScmEvento(
            aggregate_type="PLAN_PRODUCCION",
            aggregate_id=str(plan.id),
            tipo="PRODUCTION_PLAN_CONFIRMED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=response,
            operation_id=audit.operation_id,
        ))
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
