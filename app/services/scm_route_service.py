import copy
import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.scm_articulos import (
    CLASE_PRODUCTO_TERMINADO,
    ScmArticulo,
    ScmArticuloProducto,
)
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_estructuras import (
    ESTADO_ESTRUCTURA_APROBADA,
    ScmEstructuraRevision,
)
from app.models.scm_rutas import (
    ESTADO_RUTA_APROBADA,
    ESTADO_RUTA_BORRADOR,
    ESTADO_RUTA_RETIRADA,
    EXECUTOR_KINDS,
    EXECUTOR_OP_OT,
    EXECUTOR_ORDEN_OPERACION,
    TIPOS_OPERACION,
    ScmCentroTrabajo,
    ScmOperacionPrecedencia,
    ScmOperacionRuta,
    ScmRutaRevision,
    utc_now,
)
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    positive_integer,
    reject_unknown_fields,
    required_text,
    stable_code,
)


def _optional_text(value, *, field, max_length):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScmServiceError(
            "TEXT_FIELD_REQUIRED",
            f"El campo {field} debe ser texto.",
            status_code=400,
            details={"field": field},
        )
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ScmServiceError(
            "FIELD_TOO_LONG",
            f"El campo {field} supera la longitud permitida.",
            status_code=400,
            details={"field": field, "max_length": max_length},
        )
    return normalized or None


def _choice(value, *, field, allowed, error_code="INVALID_CHOICE"):
    if not isinstance(value, str):
        normalized = None
    else:
        normalized = value.strip().upper()
    if normalized not in allowed:
        raise ScmServiceError(
            error_code,
            f"El campo {field} contiene un valor incompatible.",
            status_code=422,
            details={"field": field, "allowed": list(allowed)},
        )
    return normalized


def _boolean(value, *, field, default=None):
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise ScmServiceError(
            "BOOLEAN_FIELD_REQUIRED",
            f"El campo {field} debe ser booleano.",
            status_code=400,
            details={"field": field},
        )
    return value


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


def _reserve_operation(
    session,
    *,
    operation_id,
    endpoint,
    actor,
    payload,
):
    request_hash = _request_hash(
        endpoint=endpoint,
        actor_id=actor.id,
        payload=payload,
    )
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
        if existing.estado_http is None or existing.response_json is None:
            raise ScmServiceError(
                "IDEMPOTENCY_OPERATION_INCOMPLETE",
                "La operacion no tiene un resultado reutilizable.",
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


def serialize_route(route):
    payload = route.to_dict()
    target = route.articulo_objetivo
    payload["producto_id"] = (
        target.producto.producto_terminado_id
        if target.producto is not None
        else None
    )
    payload["articulo_objetivo"] = {
        "id": target.id,
        "codigo": target.codigo,
        "nombre": target.nombre,
        "clase": target.clase,
    }
    by_id = {item.id: item for item in route.operaciones}
    for operation in payload["operaciones"]:
        model = by_id[operation["id"]]
        operation["centro_trabajo"] = {
            "id": model.centro_trabajo.id,
            "codigo": model.centro_trabajo.codigo,
            "nombre": model.centro_trabajo.nombre,
            "tipo": model.centro_trabajo.tipo,
        }
        operation["articulo_salida"] = {
            "id": model.articulo_salida.id,
            "codigo": model.articulo_salida.codigo,
            "nombre": model.articulo_salida.nombre,
            "clase": model.articulo_salida.clase,
        }
    return payload


def _event(route, actor, event_type, *, operation=None, before=None):
    return ScmEvento(
        aggregate_type="SCM_RUTA",
        aggregate_id=route.id,
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=serialize_route(route),
        operation_id=operation.operation_id if operation else None,
    )


def _route_target(session, product_id, *, lock=False):
    statement = (
        select(ScmArticulo)
        .join(
            ScmArticuloProducto,
            ScmArticuloProducto.articulo_id == ScmArticulo.id,
        )
        .where(
            ScmArticuloProducto.producto_terminado_id == str(product_id)
        )
    )
    if lock:
        statement = statement.with_for_update()
    target = session.scalar(statement)
    if target is None:
        raise ScmServiceError(
            "PRODUCT_ARTICLE_NOT_FOUND",
            "El producto no tiene una identidad de articulo SCM.",
            status_code=404,
            details={"product_id": str(product_id)},
        )
    if target.clase != CLASE_PRODUCTO_TERMINADO:
        raise ScmServiceError(
            "ARTICLE_SUBTYPE_MISMATCH",
            "La ruta requiere un articulo PRODUCTO_TERMINADO.",
            status_code=422,
        )
    if not target.activo:
        raise ScmServiceError(
            "ARTICLE_INACTIVE",
            "El producto objetivo debe estar activo.",
            status_code=422,
        )
    return target


def _new_operations(session, raw_operations):
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ScmServiceError(
            "ROUTE_OPERATIONS_REQUIRED",
            "La ruta requiere al menos una operacion.",
            status_code=422,
        )

    operations = []
    keys = []
    sequences = []
    for raw in raw_operations:
        if not isinstance(raw, dict):
            raise ScmServiceError(
                "INVALID_ROUTE_OPERATION",
                "Cada operacion debe ser un objeto JSON.",
                status_code=400,
            )
        reject_unknown_fields(
            raw,
            allowed={
                "clave",
                "secuencia_visible",
                "nombre",
                "tipo",
                "executor_kind",
                "centro_trabajo_id",
                "articulo_salida_id",
                "estructura_revision_id",
                "permite_concurrente",
            },
        )
        executor = _choice(
            raw.get("executor_kind"),
            field="executor_kind",
            allowed=EXECUTOR_KINDS,
            error_code="EXECUTOR_KIND_INCOMPATIBLE",
        )
        structure_id = raw.get("estructura_revision_id")
        if executor == EXECUTOR_OP_OT and structure_id is not None:
            raise ScmServiceError(
                "EXECUTOR_KIND_INCOMPATIBLE",
                "OP_OT no puede declarar una estructura de operacion.",
                status_code=422,
            )
        if executor == EXECUTOR_ORDEN_OPERACION and structure_id is None:
            raise ScmServiceError(
                "EXECUTOR_KIND_INCOMPATIBLE",
                "ORDEN_OPERACION requiere una estructura revisionada.",
                status_code=422,
            )
        if structure_id is not None:
            structure_id = positive_integer(
                structure_id,
                field="estructura_revision_id",
            )
            if session.get(ScmEstructuraRevision, structure_id) is None:
                raise ScmServiceError(
                    "STRUCTURE_NOT_FOUND",
                    "La estructura declarada no existe.",
                    status_code=404,
                )

        center_id = positive_integer(
            raw.get("centro_trabajo_id"),
            field="centro_trabajo_id",
        )
        if session.get(ScmCentroTrabajo, center_id) is None:
            raise ScmServiceError(
                "WORK_CENTER_NOT_FOUND",
                "El centro de trabajo no existe.",
                status_code=404,
            )
        output_id = positive_integer(
            raw.get("articulo_salida_id"),
            field="articulo_salida_id",
        )
        if session.get(ScmArticulo, output_id) is None:
            raise ScmServiceError(
                "ARTICLE_NOT_FOUND",
                "El articulo de salida no existe.",
                status_code=404,
            )

        key = stable_code(raw.get("clave"), field="clave")
        sequence = positive_integer(
            raw.get("secuencia_visible"),
            field="secuencia_visible",
        )
        keys.append(key)
        sequences.append(sequence)
        operations.append(ScmOperacionRuta(
            clave=key,
            secuencia_visible=sequence,
            nombre=required_text(
                raw.get("nombre"),
                field="nombre",
                max_length=160,
            ),
            tipo=_choice(
                raw.get("tipo"),
                field="tipo",
                allowed=TIPOS_OPERACION,
            ),
            executor_kind=executor,
            centro_trabajo_id=center_id,
            articulo_salida_id=output_id,
            estructura_revision_id=structure_id,
            permite_concurrente=_boolean(
                raw.get("permite_concurrente"),
                field="permite_concurrente",
                default=False,
            ),
        ))

    if len(keys) != len(set(keys)):
        raise ScmServiceError(
            "DUPLICATE_ROUTE_OPERATION_KEY",
            "Las claves de operacion no pueden repetirse.",
            status_code=422,
        )
    if len(sequences) != len(set(sequences)):
        raise ScmServiceError(
            "DUPLICATE_ROUTE_SEQUENCE",
            "La secuencia visible no puede repetirse.",
            status_code=422,
        )
    return operations


def _new_precedences(raw_edges, operations_by_key):
    if not isinstance(raw_edges, list):
        raise ScmServiceError(
            "ROUTE_PRECEDENCES_REQUIRED",
            "Las precedencias deben ser una lista.",
            status_code=400,
        )
    edges = []
    pairs = []
    for raw in raw_edges:
        if not isinstance(raw, dict):
            raise ScmServiceError(
                "INVALID_ROUTE_PRECEDENCE",
                "Cada precedencia debe ser un objeto JSON.",
                status_code=400,
            )
        reject_unknown_fields(
            raw,
            allowed={"anterior_clave", "siguiente_clave"},
        )
        previous_key = stable_code(
            raw.get("anterior_clave"),
            field="anterior_clave",
        )
        next_key = stable_code(
            raw.get("siguiente_clave"),
            field="siguiente_clave",
        )
        if previous_key == next_key:
            raise ScmServiceError(
                "ROUTE_CYCLE",
                "Una operacion no puede precederse a si misma.",
                status_code=422,
            )
        if (
            previous_key not in operations_by_key
            or next_key not in operations_by_key
        ):
            raise ScmServiceError(
                "ROUTE_OPERATION_NOT_FOUND",
                "La precedencia referencia una clave inexistente.",
                status_code=422,
            )
        pair = (previous_key, next_key)
        pairs.append(pair)
        edges.append(ScmOperacionPrecedencia(
            operacion_anterior_id=operations_by_key[previous_key].id,
            operacion_siguiente_id=operations_by_key[next_key].id,
        ))
    if len(pairs) != len(set(pairs)):
        raise ScmServiceError(
            "DUPLICATE_ROUTE_PRECEDENCE",
            "No se puede repetir una precedencia.",
            status_code=422,
        )
    return edges


def _replace_content(session, route, data):
    operations = _new_operations(session, data.get("operaciones"))
    route.notas = _optional_text(
        data.get("notas"),
        field="notas",
        max_length=4000,
    )
    route.operaciones = operations
    session.flush()
    by_key = {item.clave: item for item in operations}
    route.precedencias = _new_precedences(
        data.get("precedencias"),
        by_key,
    )
    session.flush()


def _locked_route(session, route_id):
    route = session.scalar(
        select(ScmRutaRevision)
        .where(ScmRutaRevision.id == route_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if route is None:
        raise ScmServiceError(
            "ROUTE_NOT_FOUND",
            "La revision de ruta no existe.",
            status_code=404,
        )
    return route


def _check_version(route, received):
    parsed = expected_version(received)
    if route.version != parsed:
        raise ScmServiceError(
            "STALE_VERSION",
            "La version de la ruta esta desactualizada.",
            status_code=409,
            details={"expected": route.version, "received": parsed},
        )


def _assert_dag(session, route):
    has_cycle = session.execute(
        text("""
            WITH RECURSIVE reach(anterior_id, siguiente_id) AS (
                SELECT operacion_anterior_id, operacion_siguiente_id
                FROM scm_operacion_precedencia
                WHERE ruta_id = :route_id
                UNION
                SELECT reach.anterior_id, edge.operacion_siguiente_id
                FROM reach
                JOIN scm_operacion_precedencia AS edge
                  ON edge.ruta_id = :route_id
                 AND edge.operacion_anterior_id = reach.siguiente_id
            )
            SELECT EXISTS (
                SELECT 1
                FROM reach
                WHERE anterior_id = siguiente_id
            )
        """),
        {"route_id": route.id},
    ).scalar_one()
    if has_cycle:
        raise ScmServiceError(
            "ROUTE_CYCLE",
            "Las precedencias de la ruta forman un ciclo.",
            status_code=422,
        )


def _assert_route_approvable(session, route):
    if not route.operaciones:
        raise ScmServiceError(
            "ROUTE_OPERATIONS_REQUIRED",
            "La ruta requiere al menos una operacion.",
            status_code=422,
        )
    _assert_dag(session, route)
    outgoing_ids = {
        edge.operacion_anterior_id for edge in route.precedencias
    }
    terminal = [
        operation
        for operation in route.operaciones
        if operation.id not in outgoing_ids
    ]
    if (
        len(terminal) != 1
        or terminal[0].articulo_salida_id
        != route.articulo_objetivo_id
    ):
        raise ScmServiceError(
            "OUTPUT_ARTICLE_INCOMPATIBLE",
            "La ruta requiere un unico terminal con el producto objetivo.",
            status_code=422,
            details={
                "target_article_id": route.articulo_objetivo_id,
                "terminal_operation_ids": [item.id for item in terminal],
            },
        )

    terminal_id = terminal[0].id
    for operation in route.operaciones:
        if not operation.centro_trabajo.activo:
            raise ScmServiceError(
                "WORK_CENTER_INACTIVE",
                "Todos los centros de trabajo deben estar activos.",
                status_code=422,
                details={"work_center_id": operation.centro_trabajo_id},
            )
        if not operation.articulo_salida.activo:
            raise ScmServiceError(
                "ARTICLE_INACTIVE",
                "Todas las salidas de ruta deben estar activas.",
                status_code=422,
                details={"article_id": operation.articulo_salida_id},
            )
        if (
            operation.id != terminal_id
            and operation.articulo_salida.clase
            == CLASE_PRODUCTO_TERMINADO
        ):
            raise ScmServiceError(
                "OUTPUT_ARTICLE_INCOMPATIBLE",
                "Una operacion intermedia no puede acreditar producto terminado.",
                status_code=422,
                details={"operation_id": operation.id},
            )
        if operation.executor_kind == EXECUTOR_OP_OT:
            if operation.estructura_revision_id is not None:
                raise ScmServiceError(
                    "EXECUTOR_KIND_INCOMPATIBLE",
                    "OP_OT no puede ejecutar una estructura paralela.",
                    status_code=422,
                    details={"operation_id": operation.id},
                )
            continue
        if operation.executor_kind != EXECUTOR_ORDEN_OPERACION:
            raise ScmServiceError(
                "EXECUTOR_KIND_INCOMPATIBLE",
                "La operacion no declara una autoridad valida.",
                status_code=422,
                details={"operation_id": operation.id},
            )
        structure = operation.estructura_revision
        if (
            structure is None
            or structure.estado != ESTADO_ESTRUCTURA_APROBADA
            or structure.articulo_resultado_id
            != operation.articulo_salida_id
        ):
            raise ScmServiceError(
                "EXECUTOR_KIND_INCOMPATIBLE",
                "ORDEN_OPERACION requiere una estructura aprobada compatible.",
                status_code=422,
                details={"operation_id": operation.id},
            )


def _content_hash(route):
    operations = [
        {
            "clave": item.clave,
            "secuencia_visible": item.secuencia_visible,
            "nombre": item.nombre,
            "tipo": item.tipo,
            "executor_kind": item.executor_kind,
            "centro_trabajo_id": item.centro_trabajo_id,
            "articulo_salida_id": item.articulo_salida_id,
            "estructura_revision_id": item.estructura_revision_id,
            "permite_concurrente": item.permite_concurrente,
        }
        for item in sorted(route.operaciones, key=lambda value: value.clave)
    ]
    by_id = {item.id: item.clave for item in route.operaciones}
    precedences = sorted(
        (
            by_id[item.operacion_anterior_id],
            by_id[item.operacion_siguiente_id],
        )
        for item in route.precedencias
    )
    canonical = {
        "articulo_objetivo_id": route.articulo_objetivo_id,
        "operaciones": operations,
        "precedencias": precedences,
    }
    raw = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def list_work_centers(session, *, actor_id, active=None):
    load_actor(session, actor_id, capability="RUTA_VER")
    statement = select(ScmCentroTrabajo)
    if active is not None:
        statement = statement.where(ScmCentroTrabajo.activo == active)
    centers = session.scalars(
        statement.order_by(ScmCentroTrabajo.codigo)
    ).all()
    return {"items": [center.to_dict() for center in centers]}


def create_work_center(session, *, actor_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="RUTA_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={"nombre", "tipo"},
        )
        center = ScmCentroTrabajo(
            codigo=generar_codigo_catalogo(
                "CENTRO_TRABAJO",
                session=session,
            ),
            nombre=required_text(
                data.get("nombre"),
                field="nombre",
                max_length=160,
            ),
            tipo=_choice(
                data.get("tipo"),
                field="tipo",
                allowed=TIPOS_OPERACION,
            ),
        )
        session.add(center)
        session.flush()
        session.add(ScmEvento(
            aggregate_type="SCM_CENTRO_TRABAJO",
            aggregate_id=center.id,
            tipo="WORK_CENTER_CREATED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            after_json=center.to_dict(),
        ))
        session.commit()
        return center.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "WORK_CENTER_CONFLICT",
            "El centro de trabajo entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_work_center(session, *, actor_id, center_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="RUTA_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={"version", "nombre", "tipo", "activo"},
        )
        center = session.scalar(
            select(ScmCentroTrabajo)
            .where(ScmCentroTrabajo.id == center_id)
            .with_for_update()
        )
        if center is None:
            raise ScmServiceError(
                "WORK_CENTER_NOT_FOUND",
                "El centro de trabajo no existe.",
                status_code=404,
            )
        received = expected_version(data.get("version"))
        if center.version != received:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version del centro esta desactualizada.",
                status_code=409,
                details={
                    "expected": center.version,
                    "received": received,
                },
            )
        before = center.to_dict()
        if "nombre" in data:
            center.nombre = required_text(
                data.get("nombre"),
                field="nombre",
                max_length=160,
            )
        if "tipo" in data:
            center.tipo = _choice(
                data.get("tipo"),
                field="tipo",
                allowed=TIPOS_OPERACION,
            )
        if "activo" in data:
            center.activo = _boolean(data.get("activo"), field="activo")
        center.version += 1
        session.flush()
        session.add(ScmEvento(
            aggregate_type="SCM_CENTRO_TRABAJO",
            aggregate_id=center.id,
            tipo="WORK_CENTER_UPDATED",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            before_json=before,
            after_json=center.to_dict(),
        ))
        session.commit()
        return center.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "WORK_CENTER_CONFLICT",
            "No se pudo actualizar el centro de trabajo.",
            status_code=409,
        ) from error


def list_routes(session, *, actor_id, product_id):
    load_actor(session, actor_id, capability="RUTA_VER")
    target = _route_target(session, product_id)
    routes = session.scalars(
        select(ScmRutaRevision)
        .where(ScmRutaRevision.articulo_objetivo_id == target.id)
        .order_by(ScmRutaRevision.numero_revision.desc())
    ).all()
    return {"items": [serialize_route(route) for route in routes]}


def get_route(session, *, actor_id, route_id):
    load_actor(session, actor_id, capability="RUTA_VER")
    route = session.get(ScmRutaRevision, route_id)
    if route is None:
        raise ScmServiceError(
            "ROUTE_NOT_FOUND",
            "La revision de ruta no existe.",
            status_code=404,
        )
    return serialize_route(route)


def create_route(session, *, actor_id, product_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="RUTA_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={"notas", "operaciones", "precedencias"},
        )
        target = _route_target(session, product_id, lock=True)
        open_revision = session.scalar(
            select(ScmRutaRevision.id).where(
                ScmRutaRevision.articulo_objetivo_id == target.id,
                ScmRutaRevision.estado == ESTADO_RUTA_BORRADOR,
            )
        )
        if open_revision is not None:
            raise ScmServiceError(
                "ROUTE_OPEN_REVISION_EXISTS",
                "El producto ya tiene una revision de ruta abierta.",
                status_code=409,
            )
        last_number = session.scalar(
            select(ScmRutaRevision.numero_revision)
            .where(ScmRutaRevision.articulo_objetivo_id == target.id)
            .order_by(ScmRutaRevision.numero_revision.desc())
            .limit(1)
        )
        route = ScmRutaRevision(
            articulo_objetivo=target,
            numero_revision=(last_number or 0) + 1,
            estado=ESTADO_RUTA_BORRADOR,
            creada_por_id=actor.id,
        )
        session.add(route)
        session.flush()
        _replace_content(session, route, data)
        session.add(_event(route, actor, "ROUTE_CREATED"))
        session.commit()
        return serialize_route(route)
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ROUTE_CONFLICT",
            "La ruta entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_route(session, *, actor_id, route_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="RUTA_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={"version", "notas", "operaciones", "precedencias"},
        )
        route = _locked_route(session, route_id)
        _check_version(route, data.get("version"))
        if route.estado != ESTADO_RUTA_BORRADOR:
            raise ScmServiceError(
                "ROUTE_NOT_EDITABLE",
                "Solo una ruta BORRADOR puede editarse.",
                status_code=409,
            )
        before = serialize_route(route)
        route.precedencias = []
        session.flush()
        route.operaciones = []
        session.flush()
        _replace_content(session, route, data)
        route.version += 1
        session.flush()
        session.add(
            _event(route, actor, "ROUTE_UPDATED", before=before)
        )
        session.commit()
        return serialize_route(route)
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ROUTE_CONFLICT",
            "No se pudo actualizar la ruta.",
            status_code=409,
        ) from error


def approve_route(
    session,
    *,
    actor_id,
    route_id,
    operation_id,
    data,
):
    endpoint = f"/rutas/{route_id}/aprobar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="RUTA_APROBAR",
        )
        reject_unknown_fields(data, allowed={"version"})
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
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('scm_route_approval'))"
            ))
        route = _locked_route(session, route_id)
        _check_version(route, data.get("version"))
        if route.estado != ESTADO_RUTA_BORRADOR:
            raise ScmServiceError(
                "ROUTE_NOT_APPROVABLE",
                "Solo una ruta BORRADOR puede aprobarse.",
                status_code=409,
            )
        if route.creada_por_id == actor.id:
            raise ScmServiceError(
                "CREATOR_CANNOT_APPROVE",
                "El creador de la revision no puede aprobarla.",
                status_code=403,
            )
        if not route.articulo_objetivo.activo:
            raise ScmServiceError(
                "ARTICLE_INACTIVE",
                "El producto objetivo debe permanecer activo.",
                status_code=422,
            )
        _assert_route_approvable(session, route)

        previous_approved = session.scalars(
            select(ScmRutaRevision)
            .where(
                ScmRutaRevision.articulo_objetivo_id
                == route.articulo_objetivo_id,
                ScmRutaRevision.estado == ESTADO_RUTA_APROBADA,
                ScmRutaRevision.id != route.id,
            )
            .with_for_update()
        ).all()
        for previous in previous_approved:
            previous.estado = ESTADO_RUTA_RETIRADA
            previous.retirada_por_id = actor.id
            previous.retirada_at = utc_now()
            previous.version += 1
        if previous_approved:
            session.flush()

        route.content_hash = _content_hash(route)
        route.estado = ESTADO_RUTA_APROBADA
        route.aprobada_por_id = actor.id
        route.aprobada_at = utc_now()
        route.version += 1
        session.flush()
        session.add(
            _event(
                route,
                actor,
                "ROUTE_APPROVED",
                operation=operation,
            )
        )
        response = serialize_route(route)
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
            "ROUTE_CONFLICT",
            "No se pudo aprobar la ruta.",
            status_code=409,
        ) from error


def publish_route_directly(
    session,
    *,
    actor_id,
    route_id,
    operation_id,
    data,
):
    endpoint = f"/rutas/{route_id}/publicar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="RUTA_PUBLICAR_DIRECTO",
        )
        reject_unknown_fields(data, allowed={"version"})
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
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('scm_route_approval'))"
            ))
        route = _locked_route(session, route_id)
        _check_version(route, data.get("version"))
        if route.estado != ESTADO_RUTA_BORRADOR:
            raise ScmServiceError(
                "ROUTE_NOT_PUBLISHABLE",
                "Solo una ruta BORRADOR puede publicarse directamente.",
                status_code=409,
            )
        if not route.articulo_objetivo.activo:
            raise ScmServiceError(
                "ARTICLE_INACTIVE",
                "El producto objetivo debe permanecer activo.",
                status_code=422,
            )
        _assert_route_approvable(session, route)

        previous_approved = session.scalars(
            select(ScmRutaRevision)
            .where(
                ScmRutaRevision.articulo_objetivo_id
                == route.articulo_objetivo_id,
                ScmRutaRevision.estado == ESTADO_RUTA_APROBADA,
                ScmRutaRevision.id != route.id,
            )
            .with_for_update()
        ).all()
        for previous in previous_approved:
            previous.estado = ESTADO_RUTA_RETIRADA
            previous.retirada_por_id = actor.id
            previous.retirada_at = utc_now()
            previous.version += 1
        if previous_approved:
            session.flush()

        route.content_hash = _content_hash(route)
        route.estado = ESTADO_RUTA_APROBADA
        route.aprobada_por_id = actor.id
        route.aprobada_at = utc_now()
        route.version += 1
        session.flush()
        session.add(
            _event(
                route,
                actor,
                "ROUTE_PUBLISHED_DIRECTLY",
                operation=operation,
            )
        )
        response = serialize_route(route)
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
            "ROUTE_CONFLICT",
            "No se pudo publicar la ruta.",
            status_code=409,
        ) from error


def retire_route(
    session,
    *,
    actor_id,
    route_id,
    operation_id,
    data,
):
    endpoint = f"/rutas/{route_id}/retirar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="RUTA_APROBAR",
        )
        reject_unknown_fields(data, allowed={"version"})
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
        route = _locked_route(session, route_id)
        _check_version(route, data.get("version"))
        if route.estado != ESTADO_RUTA_APROBADA:
            raise ScmServiceError(
                "ROUTE_NOT_RETIRABLE",
                "Solo una ruta aprobada puede retirarse.",
                status_code=409,
            )
        route.estado = ESTADO_RUTA_RETIRADA
        route.retirada_por_id = actor.id
        route.retirada_at = utc_now()
        route.version += 1
        session.flush()
        session.add(
            _event(
                route,
                actor,
                "ROUTE_RETIRED",
                operation=operation,
            )
        )
        response = serialize_route(route)
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
            "ROUTE_CONFLICT",
            "No se pudo retirar la ruta.",
            status_code=409,
        ) from error
