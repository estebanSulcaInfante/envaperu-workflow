import copy
import hashlib
import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_catalogos import (
    ScmCategoriaRecepcion,
    ScmMaterial,
    ScmProveedor,
)
from app.models.scm_compras import (
    ESTADO_OC_ACTIVA,
    ESTADO_REVISION_APROBADA,
    ESTADO_REVISION_BORRADOR,
    ESTADO_REVISION_PENDIENTE_APROBACION,
    ESTADO_REVISION_SUPERADA,
    ScmOrdenCompra,
    ScmOrdenCompraLinea,
    ScmOrdenCompraRevision,
    utc_now,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    positive_integer,
    positive_kg,
    reject_unknown_fields,
    required_text,
)


ZERO_KG = Decimal("0.000")


def _kg_text(value):
    return format(value or ZERO_KG, ".3f")


def _parse_date(value, *, field):
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ScmServiceError(
            "INVALID_DATE",
            f"El campo {field} debe usar YYYY-MM-DD.",
            status_code=400,
            details={"field": field},
        ) from error


def _serialize_line(line):
    payload = line.to_dict()
    authorized = line.cantidad_autorizada_kg or ZERO_KG
    payload.update({
        "material_codigo": line.material.codigo,
        "material_nombre": line.material.nombre,
        "cantidad_autorizada_kg": _kg_text(authorized),
        # Se deriva de imputaciones confirmadas. Todavia no existen en este
        # corte, por lo que nunca se persiste un saldo editable.
        "cantidad_recibida_kg": _kg_text(ZERO_KG),
        "saldo_kg": _kg_text(authorized),
    })
    return payload


def _serialize_revision(revision):
    payload = revision.to_dict()
    payload["lineas"] = [
        _serialize_line(line)
        for line in sorted(revision.lineas, key=lambda item: item.numero_linea)
    ]
    return payload


def serialize_order(order):
    revisions = [
        _serialize_revision(revision)
        for revision in sorted(order.revisiones, key=lambda item: item.numero)
    ]
    payload = order.to_dict()
    payload.update({
        "proveedor_codigo": order.proveedor.codigo,
        "proveedor_razon_social": order.proveedor.razon_social,
        "revisiones": revisions,
        "revision_actual": revisions[-1] if revisions else None,
    })
    return payload


def _event(
    order,
    actor,
    event_type,
    *,
    operation=None,
    before=None,
    after=None,
):
    return ScmEvento(
        aggregate_type="SCM_ORDEN_COMPRA",
        aggregate_id=order.id,
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=after,
        operation_id=operation.operation_id if operation else None,
    )


def _request_hash(*, endpoint, actor_id, payload):
    canonical = json.dumps(
        {
            "endpoint": endpoint,
            "actor_id": actor_id,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _operation_replay(operation, *, endpoint, request_sha256):
    if (
        operation.endpoint != endpoint
        or operation.request_sha256 != request_sha256
    ):
        raise ScmServiceError(
            "IDEMPOTENCY_CONFLICT",
            "La clave idempotente ya fue usada con otra solicitud.",
            status_code=409,
        )
    if operation.estado_http is None or operation.response_json is None:
        raise ScmServiceError(
            "IDEMPOTENCY_OPERATION_INCOMPLETE",
            "La operacion idempotente no tiene un resultado reutilizable.",
            status_code=409,
        )
    return copy.deepcopy(operation.response_json)


def _reserve_operation(
    session,
    *,
    operation_id,
    endpoint,
    actor,
    payload,
):
    request_sha256 = _request_hash(
        endpoint=endpoint,
        actor_id=actor.id,
        payload=payload,
    )
    existing = session.get(ScmOperacion, operation_id)
    if existing is not None:
        return None, _operation_replay(
            existing,
            endpoint=endpoint,
            request_sha256=request_sha256,
        )

    operation = ScmOperacion(
        operation_id=operation_id,
        endpoint=endpoint,
        actor_id=actor.id,
        request_sha256=request_sha256,
    )
    session.add(operation)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.get(ScmOperacion, operation_id)
        if existing is None:
            raise
        return None, _operation_replay(
            existing,
            endpoint=endpoint,
            request_sha256=request_sha256,
        )
    return operation, None


def _locked_order(session, order_id):
    order = session.scalar(
        select(ScmOrdenCompra)
        .where(ScmOrdenCompra.id == order_id)
        .with_for_update()
    )
    if order is None:
        raise ScmServiceError(
            "PURCHASE_ORDER_NOT_FOUND",
            "La orden interna de compra no existe.",
            status_code=404,
        )
    return order


def _requested_revision(order, revision_number):
    number = positive_integer(revision_number, field="revision_numero")
    revision = next(
        (item for item in order.revisiones if item.numero == number),
        None,
    )
    if revision is None:
        raise ScmServiceError(
            "PURCHASE_ORDER_REVISION_NOT_FOUND",
            "La revision solicitada no existe.",
            status_code=404,
        )
    return revision


def _check_order_version(order, received):
    parsed = expected_version(received)
    if order.version != parsed:
        raise ScmServiceError(
            "STALE_VERSION",
            "La version de la orden de compra esta desactualizada.",
            status_code=409,
            details={"expected": order.version, "received": parsed},
        )


def _validate_line_material_for_approval(line):
    material = line.material
    category = material.categoria_recepcion
    if (
        not material.activo
        or not category.activo
        or not category.recepcion_habilitada
    ):
        raise ScmServiceError(
            "MATERIAL_NOT_RECEIVABLE",
            "Una linea usa un material inactivo o pendiente de configurar.",
            status_code=422,
            details={"material_id": material.id},
        )


def _lock_and_validate_approval_resources(session, order, revision):
    provider = session.scalar(
        select(ScmProveedor)
        .where(ScmProveedor.id == order.proveedor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if provider is None or not provider.activo:
        raise ScmServiceError(
            "PROVIDER_INACTIVE",
            "El proveedor debe estar activo al aprobar la OC.",
            status_code=422,
        )

    # Las categorías se bloquean antes que los materiales, igual que en el
    # CRUD de catálogo. Se toma el catálogo completo (pequeño en v1) para que
    # una reasignación concurrente no invierta el orden de locks.
    categories = {
        item.id: item
        for item in session.scalars(
            select(ScmCategoriaRecepcion)
            .order_by(ScmCategoriaRecepcion.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    material_ids = sorted({line.material_id for line in revision.lineas})
    materials = {
        item.id: item
        for item in session.scalars(
            select(ScmMaterial)
            .where(ScmMaterial.id.in_(material_ids))
            .order_by(ScmMaterial.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    for material_id in material_ids:
        material = materials.get(material_id)
        category = (
            categories.get(material.categoria_recepcion_id)
            if material is not None
            else None
        )
        if (
            material is None
            or category is None
            or not material.activo
            or not category.activo
            or not category.recepcion_habilitada
        ):
            raise ScmServiceError(
                "MATERIAL_NOT_RECEIVABLE",
                "Una linea usa un material inactivo o pendiente de configurar.",
                status_code=422,
                details={"material_id": material_id},
            )


def _new_line(session, raw_line, *, default_number):
    if not isinstance(raw_line, dict):
        raise ScmServiceError(
            "INVALID_PURCHASE_ORDER_LINE",
            "Cada linea debe ser un objeto JSON.",
            status_code=400,
        )
    reject_unknown_fields(
        raw_line,
        allowed={
            "numero_linea",
            "material_id",
            "cantidad_autorizada_kg",
            "fecha_requerida",
            "observacion",
        },
    )
    number = positive_integer(
        raw_line.get("numero_linea", default_number),
        field="numero_linea",
    )
    material_id = positive_integer(
        raw_line.get("material_id"),
        field="material_id",
    )
    material = session.get(ScmMaterial, material_id)
    if material is None:
        raise ScmServiceError(
            "MATERIAL_NOT_FOUND",
            "El material comun no existe.",
            status_code=404,
            details={"material_id": material_id},
        )
    if not material.activo:
        raise ScmServiceError(
            "MATERIAL_INACTIVE",
            "El material esta inactivo.",
            status_code=422,
            details={"material_id": material_id},
        )
    observation = raw_line.get("observacion")
    if observation is not None:
        observation = str(observation).strip() or None
    return ScmOrdenCompraLinea(
        numero_linea=number,
        material=material,
        cantidad_autorizada_kg=positive_kg(
            raw_line.get("cantidad_autorizada_kg")
        ),
        fecha_requerida=_parse_date(
            raw_line.get("fecha_requerida"),
            field="fecha_requerida",
        ),
        observacion=observation,
    )


def list_purchase_orders(session, *, actor_id):
    load_actor(session, actor_id)
    orders = session.scalars(
        select(ScmOrdenCompra).order_by(ScmOrdenCompra.id.desc())
    ).all()
    return {"items": [serialize_order(item) for item in orders]}


def get_purchase_order(session, *, actor_id, order_id):
    load_actor(session, actor_id)
    order = session.get(ScmOrdenCompra, order_id)
    if order is None:
        raise ScmServiceError(
            "PURCHASE_ORDER_NOT_FOUND",
            "La orden interna de compra no existe.",
            status_code=404,
        )
    return serialize_order(order)


def create_purchase_order(session, *, actor_id, data):
    try:
        actor = load_actor(session, actor_id, capability="OC_CREAR")
        reject_unknown_fields(
            data,
            allowed={"codigo", "proveedor_id", "lineas"},
        )
        provider_id = positive_integer(
            data.get("proveedor_id"),
            field="proveedor_id",
        )
        provider = session.get(ScmProveedor, provider_id)
        if provider is None:
            raise ScmServiceError(
                "PROVIDER_NOT_FOUND",
                "El proveedor no existe.",
                status_code=404,
            )
        if not provider.activo:
            raise ScmServiceError(
                "PROVIDER_INACTIVE",
                "No se puede crear una OC para un proveedor inactivo.",
                status_code=422,
            )

        raw_lines = data.get("lineas", [])
        if not isinstance(raw_lines, list):
            raise ScmServiceError(
                "INVALID_PURCHASE_ORDER_LINES",
                "El campo lineas debe ser una lista.",
                status_code=400,
            )
        lines = [
            _new_line(session, raw, default_number=index)
            for index, raw in enumerate(raw_lines, start=1)
        ]
        numbers = [item.numero_linea for item in lines]
        if len(numbers) != len(set(numbers)):
            raise ScmServiceError(
                "DUPLICATE_LINE_NUMBER",
                "No se puede repetir numero_linea en una revision.",
                status_code=422,
            )

        requested_code = data.get("codigo")
        code = (
            required_text(requested_code, field="codigo", max_length=64)
            .upper()
            if requested_code
            else f"OCM-AUTO-{uuid4().hex.upper()}"
        )
        if session.scalar(
            select(ScmOrdenCompra.id).where(ScmOrdenCompra.codigo == code)
        ) is not None:
            raise ScmServiceError(
                "PURCHASE_ORDER_CODE_CONFLICT",
                "El codigo de la orden de compra ya existe.",
                status_code=409,
            )

        revision = ScmOrdenCompraRevision(
            numero=1,
            estado=ESTADO_REVISION_BORRADOR,
            creada_por_id=actor.id,
            lineas=lines,
        )
        order = ScmOrdenCompra(
            codigo=code,
            proveedor=provider,
            estado=ESTADO_OC_ACTIVA,
            revisiones=[revision],
        )
        session.add(order)
        session.flush()
        session.add(
            _event(
                order,
                actor,
                "ORDEN_COMPRA_CREADA",
                after={"revision_numero": 1, "estado": "BORRADOR"},
            )
        )
        session.commit()
        return serialize_order(order)
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PURCHASE_ORDER_CONFLICT",
            "La orden de compra entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_purchase_order_revision(
    session,
    *,
    actor_id,
    order_id,
    revision_number,
    data,
):
    try:
        actor = load_actor(session, actor_id, capability="OC_CREAR")
        reject_unknown_fields(
            data,
            allowed={"version", "revision_version", "lineas"},
        )
        order = _locked_order(session, order_id)
        _check_order_version(order, data.get("version"))
        revision = _requested_revision(order, revision_number)
        received_revision_version = expected_version(
            data.get("revision_version")
        )
        if revision.version != received_revision_version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version de la revision esta desactualizada.",
                status_code=409,
                details={
                    "expected": revision.version,
                    "received": received_revision_version,
                    "resource": "revision",
                },
            )
        if revision.estado != ESTADO_REVISION_BORRADOR:
            raise ScmServiceError(
                "INVALID_PURCHASE_ORDER_TRANSITION",
                "Solo una revision BORRADOR puede editarse.",
                status_code=409,
            )
        if "lineas" not in data or not isinstance(data["lineas"], list):
            raise ScmServiceError(
                "INVALID_PURCHASE_ORDER_LINES",
                "El campo lineas debe ser una lista.",
                status_code=400,
            )

        lines = [
            _new_line(session, raw, default_number=index)
            for index, raw in enumerate(data["lineas"], start=1)
        ]
        numbers = [item.numero_linea for item in lines]
        if len(numbers) != len(set(numbers)):
            raise ScmServiceError(
                "DUPLICATE_LINE_NUMBER",
                "No se puede repetir numero_linea en una revision.",
                status_code=422,
            )

        before = {
            "version": order.version,
            "revision_numero": revision.numero,
            "revision_version": revision.version,
            "lineas": [_serialize_line(item) for item in revision.lineas],
        }
        for existing_line in list(revision.lineas):
            session.delete(existing_line)
        # Libera las claves (revision_id, numero_linea) antes de insertar el
        # reemplazo completo del borrador.
        session.flush()
        session.expire(revision, ["lineas"])
        revision.lineas.extend(lines)
        revision.version += 1
        order.version += 1
        session.flush()
        session.add(
            _event(
                order,
                actor,
                "ORDEN_COMPRA_REVISION_ACTUALIZADA",
                before=before,
                after={
                    "version": order.version,
                    "revision_numero": revision.numero,
                    "revision_version": revision.version,
                    "lineas": [
                        _serialize_line(item) for item in revision.lineas
                    ],
                },
            )
        )
        session.commit()
        return serialize_order(order)
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PURCHASE_ORDER_CONFLICT",
            "No se pudo actualizar la revision de la orden.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise


def send_purchase_order_for_approval(
    session,
    *,
    actor_id,
    order_id,
    operation_id,
    data,
):
    endpoint = f"/ordenes-compra-material/{order_id}/enviar-aprobacion"
    try:
        actor = load_actor(session, actor_id, capability="OC_CREAR")
        reject_unknown_fields(
            data,
            allowed={"version", "revision_numero"},
        )
        operation, replay = _reserve_operation(
            session,
            operation_id=operation_id,
            endpoint=endpoint,
            actor=actor,
            payload=data,
        )
        if replay is not None:
            session.rollback()
            return replay

        order = _locked_order(session, order_id)
        _check_order_version(order, data.get("version"))
        revision = _requested_revision(order, data.get("revision_numero"))
        if revision.estado != ESTADO_REVISION_BORRADOR:
            raise ScmServiceError(
                "INVALID_PURCHASE_ORDER_TRANSITION",
                "Solo una revision BORRADOR puede enviarse a aprobacion.",
                status_code=409,
            )
        if not revision.lineas:
            raise ScmServiceError(
                "PURCHASE_ORDER_LINES_REQUIRED",
                "La revision requiere al menos una linea.",
                status_code=422,
            )
        if not order.proveedor.activo:
            raise ScmServiceError(
                "PROVIDER_INACTIVE",
                "El proveedor debe estar activo al enviar la OC.",
                status_code=422,
            )
        for line in revision.lineas:
            _validate_line_material_for_approval(line)

        before = {
            "version": order.version,
            "revision_numero": revision.numero,
            "revision_estado": revision.estado,
        }
        revision.estado = ESTADO_REVISION_PENDIENTE_APROBACION
        revision.enviada_at = utc_now()
        revision.version += 1
        order.version += 1
        session.flush()
        session.add(
            _event(
                order,
                actor,
                "ORDEN_COMPRA_ENVIADA_APROBACION",
                operation=operation,
                before=before,
                after={
                    "version": order.version,
                    "revision_numero": revision.numero,
                    "revision_estado": revision.estado,
                },
            )
        )
        response = serialize_order(order)
        operation.estado_http = 200
        operation.response_json = response
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PURCHASE_ORDER_CONFLICT",
            "No se pudo completar la transicion de la orden.",
            status_code=409,
        ) from error


def approve_purchase_order(
    session,
    *,
    actor_id,
    order_id,
    operation_id,
    data,
):
    endpoint = f"/ordenes-compra-material/{order_id}/aprobar"
    try:
        actor = load_actor(session, actor_id, capability="OC_APROBAR")
        reject_unknown_fields(
            data,
            allowed={"version", "revision_numero"},
        )
        operation, replay = _reserve_operation(
            session,
            operation_id=operation_id,
            endpoint=endpoint,
            actor=actor,
            payload=data,
        )
        if replay is not None:
            session.rollback()
            return replay

        order = _locked_order(session, order_id)
        _check_order_version(order, data.get("version"))
        revision = _requested_revision(order, data.get("revision_numero"))
        if revision.estado != ESTADO_REVISION_PENDIENTE_APROBACION:
            raise ScmServiceError(
                "INVALID_PURCHASE_ORDER_TRANSITION",
                "Solo una revision pendiente puede aprobarse.",
                status_code=409,
            )
        if revision.creada_por_id == actor.id:
            raise ScmServiceError(
                "PURCHASE_ORDER_SELF_APPROVAL_FORBIDDEN",
                "El creador de la revision no puede aprobarla.",
                status_code=403,
            )
        _lock_and_validate_approval_resources(session, order, revision)

        before = {
            "version": order.version,
            "revision_numero": revision.numero,
            "revision_estado": revision.estado,
        }
        previous_approved = [
            item
            for item in order.revisiones
            if item.id != revision.id
            and item.estado == ESTADO_REVISION_APROBADA
        ]
        for previous in previous_approved:
            previous.estado = ESTADO_REVISION_SUPERADA
            previous.version += 1
        if previous_approved:
            # Libera el indice parcial antes de promover la nueva revision.
            session.flush()

        revision.estado = ESTADO_REVISION_APROBADA
        revision.aprobada_por_id = actor.id
        revision.aprobada_at = utc_now()
        revision.version += 1
        order.version += 1
        session.flush()
        session.add(
            _event(
                order,
                actor,
                "ORDEN_COMPRA_APROBADA",
                operation=operation,
                before=before,
                after={
                    "version": order.version,
                    "revision_numero": revision.numero,
                    "revision_estado": revision.estado,
                    "aprobada_por_id": actor.id,
                },
            )
        )
        response = serialize_order(order)
        operation.estado_http = 200
        operation.response_json = response
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PURCHASE_ORDER_CONFLICT",
            "No se pudo aprobar la orden de compra.",
            status_code=409,
        ) from error


def create_purchase_order_revision(session, *, actor_id, order_id, data):
    try:
        actor = load_actor(session, actor_id, capability="OC_CREAR")
        reject_unknown_fields(data, allowed={"version"})
        order = _locked_order(session, order_id)
        _check_order_version(order, data.get("version"))
        if order.estado != ESTADO_OC_ACTIVA:
            raise ScmServiceError(
                "PURCHASE_ORDER_NOT_ACTIVE",
                "Solo una orden ACTIVA admite nuevas revisiones.",
                status_code=409,
            )
        if any(
            item.estado in {
                ESTADO_REVISION_BORRADOR,
                ESTADO_REVISION_PENDIENTE_APROBACION,
            }
            for item in order.revisiones
        ):
            raise ScmServiceError(
                "PURCHASE_ORDER_OPEN_REVISION_EXISTS",
                "La orden ya tiene una revision abierta.",
                status_code=409,
            )
        approved = [
            item
            for item in order.revisiones
            if item.estado == ESTADO_REVISION_APROBADA
        ]
        if len(approved) != 1:
            raise ScmServiceError(
                "PURCHASE_ORDER_APPROVED_REVISION_REQUIRED",
                "Se requiere una revision aprobada para crear la siguiente.",
                status_code=409,
            )
        source = approved[0]
        next_number = max(item.numero for item in order.revisiones) + 1
        revision = ScmOrdenCompraRevision(
            numero=next_number,
            estado=ESTADO_REVISION_BORRADOR,
            creada_por_id=actor.id,
            lineas=[
                ScmOrdenCompraLinea(
                    numero_linea=line.numero_linea,
                    material_id=line.material_id,
                    cantidad_autorizada_kg=line.cantidad_autorizada_kg,
                    fecha_requerida=line.fecha_requerida,
                    observacion=line.observacion,
                )
                for line in source.lineas
            ],
        )
        order.revisiones.append(revision)
        order.version += 1
        session.flush()
        session.add(
            _event(
                order,
                actor,
                "ORDEN_COMPRA_REVISION_CREADA",
                after={
                    "revision_numero": next_number,
                    "revision_estado": ESTADO_REVISION_BORRADOR,
                    "version": order.version,
                },
            )
        )
        session.commit()
        return serialize_order(order)
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PURCHASE_ORDER_CONFLICT",
            "No se pudo crear una nueva revision.",
            status_code=409,
        ) from error
