import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.scm_articulos import (
    CLASE_PIEZA_COLOR,
    CLASE_PRODUCTO_TERMINADO,
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
)
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_estructuras import (
    ESTADO_ESTRUCTURA_APROBADA,
    ESTADO_ESTRUCTURA_BORRADOR,
    ESTADO_ESTRUCTURA_DESCARTADA,
    ESTADO_ESTRUCTURA_PENDIENTE,
    ESTADO_ESTRUCTURA_RECHAZADA,
    ESTADO_ESTRUCTURA_RETIRADA,
    ScmEstructuraComponente,
    ScmEstructuraRevision,
    utc_now,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    positive_integer,
    reject_unknown_fields,
)


QUANTITY_QUANTUM = Decimal("0.000001")
QUANTITY_MAX = Decimal("999999999.999999")
PERCENT_QUANTUM = Decimal("0.0001")
RESULT_CLASSES = {CLASE_SUBENSAMBLE_WIP, CLASE_PRODUCTO_TERMINADO}
COMPONENT_CLASSES = {CLASE_PIEZA_COLOR, CLASE_SUBENSAMBLE_WIP}


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


def _required_reason(value, *, field):
    reason = _optional_text(value, field=field, max_length=500)
    if reason is None:
        raise ScmServiceError(
            "STRUCTURE_REASON_REQUIRED",
            f"El campo {field} es obligatorio.",
            status_code=422,
            details={"field": field},
        )
    return reason


def _discrete_quantity(value):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = None
    if parsed is None or not parsed.is_finite() or parsed <= 0:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            "La cantidad del componente debe ser positiva.",
            status_code=422,
        )
    try:
        quantized = parsed.quantize(QUANTITY_QUANTUM)
    except InvalidOperation as error:
        raise ScmServiceError(
            "QUANTITY_OUT_OF_RANGE",
            "La cantidad excede Numeric(15,6).",
            status_code=422,
        ) from error
    if parsed != quantized or quantized > QUANTITY_MAX:
        raise ScmServiceError(
            "QUANTITY_OUT_OF_RANGE",
            "La cantidad excede Numeric(15,6).",
            status_code=422,
        )
    if quantized != quantized.to_integral_value():
        raise ScmServiceError(
            "DISCRETE_QUANTITY_REQUIRED",
            "Los articulos en unidad UN requieren cantidades enteras.",
            status_code=422,
        )
    return quantized


def _technical_waste(value):
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
        quantized = parsed.quantize(PERCENT_QUANTUM)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_TECHNICAL_WASTE",
            "La merma tecnica debe estar entre 0 y 100.",
            status_code=422,
        ) from error
    if (
        not parsed.is_finite()
        or parsed != quantized
        or quantized < 0
        or quantized >= 100
    ):
        raise ScmServiceError(
            "INVALID_TECHNICAL_WASTE",
            "La merma tecnica debe estar entre 0 y 100.",
            status_code=422,
        )
    return quantized


def _new_components(session, raw_components, *, result_article_id):
    if not isinstance(raw_components, list) or not raw_components:
        raise ScmServiceError(
            "STRUCTURE_COMPONENTS_REQUIRED",
            "La estructura requiere al menos un componente.",
            status_code=422,
        )

    components = []
    article_ids = []
    sequences = []
    for default_sequence, raw in enumerate(raw_components, start=1):
        if not isinstance(raw, dict):
            raise ScmServiceError(
                "INVALID_STRUCTURE_COMPONENT",
                "Cada componente debe ser un objeto JSON.",
                status_code=400,
            )
        reject_unknown_fields(
            raw,
            allowed={
                "secuencia",
                "articulo_id",
                "cantidad",
                "unidad",
                "merma_tecnica_pct",
            },
        )
        article_id = positive_integer(
            raw.get("articulo_id"),
            field="articulo_id",
        )
        if article_id == result_article_id:
            raise ScmServiceError(
                "STRUCTURE_CYCLE",
                "Un articulo no puede contenerse a si mismo.",
                status_code=422,
            )
        article = session.get(ScmArticulo, article_id)
        if article is None:
            raise ScmServiceError(
                "ARTICLE_NOT_FOUND",
                "Un componente referencia un articulo inexistente.",
                status_code=404,
                details={"article_id": article_id},
            )
        if not article.activo:
            raise ScmServiceError(
                "ARTICLE_INACTIVE",
                "Todos los componentes deben estar activos.",
                status_code=422,
                details={"article_id": article_id},
            )
        if article.clase not in COMPONENT_CLASSES:
            raise ScmServiceError(
                "STRUCTURE_COMPONENT_CLASS_INVALID",
                "Una estructura solo admite PiezaColor o WIP como componente.",
                status_code=422,
                details={
                    "article_id": article_id,
                    "clase": article.clase,
                },
            )
        if raw.get("unidad", "UN") != "UN":
            raise ScmServiceError(
                "ARTICLE_UNIT_INCOMPATIBLE",
                "R2 solo admite componentes discretos en UN.",
                status_code=422,
            )
        sequence = positive_integer(
            raw.get("secuencia", default_sequence),
            field="secuencia",
        )
        article_ids.append(article_id)
        sequences.append(sequence)
        components.append(ScmEstructuraComponente(
            secuencia=sequence,
            articulo_componente_id=article_id,
            cantidad=_discrete_quantity(raw.get("cantidad")),
            unidad="UN",
            merma_tecnica_pct=_technical_waste(
                raw.get("merma_tecnica_pct")
            ),
        ))

    if len(article_ids) != len(set(article_ids)):
        raise ScmServiceError(
            "DUPLICATE_STRUCTURE_COMPONENT",
            "No se puede repetir un articulo componente.",
            status_code=422,
        )
    if len(sequences) != len(set(sequences)):
        raise ScmServiceError(
            "DUPLICATE_STRUCTURE_SEQUENCE",
            "No se puede repetir la secuencia de componente.",
            status_code=422,
        )
    return components


def serialize_structure(revision):
    payload = revision.to_dict()
    payload["articulo_resultado"] = {
        "id": revision.articulo_resultado.id,
        "codigo": revision.articulo_resultado.codigo,
        "nombre": revision.articulo_resultado.nombre,
        "clase": revision.articulo_resultado.clase,
    }
    article_by_id = {
        line.articulo_componente_id: line.articulo_componente
        for line in revision.componentes
    }
    for component in payload["componentes"]:
        article = article_by_id[component["articulo_id"]]
        component["articulo"] = {
            "id": article.id,
            "codigo": article.codigo,
            "nombre": article.nombre,
            "clase": article.clase,
        }
    return payload


def _event(revision, actor, event_type, *, operation=None, before=None):
    return ScmEvento(
        aggregate_type="SCM_ESTRUCTURA",
        aggregate_id=revision.id,
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=serialize_structure(revision),
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


def _locked_structure(session, structure_id):
    revision = session.scalar(
        select(ScmEstructuraRevision)
        .where(ScmEstructuraRevision.id == structure_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if revision is None:
        raise ScmServiceError(
            "STRUCTURE_NOT_FOUND",
            "La revision de estructura no existe.",
            status_code=404,
        )
    return revision


def _check_version(revision, received):
    parsed = expected_version(received)
    if revision.version != parsed:
        raise ScmServiceError(
            "STALE_VERSION",
            "La version de la estructura esta desactualizada.",
            status_code=409,
            details={"expected": revision.version, "received": parsed},
        )


def _content_hash(revision):
    canonical = {
        "articulo_resultado_id": revision.articulo_resultado_id,
        "componentes": [
            {
                "secuencia": line.secuencia,
                "articulo_id": line.articulo_componente_id,
                "cantidad": format(line.cantidad, ".6f"),
                "unidad": line.unidad,
                "merma_tecnica_pct": (
                    format(line.merma_tecnica_pct, ".4f")
                    if line.merma_tecnica_pct is not None
                    else None
                ),
            }
            for line in sorted(
                revision.componentes,
                key=lambda item: item.secuencia,
            )
        ],
    }
    raw = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _assert_acyclic(session, candidate):
    if session.get_bind().dialect.name == "postgresql":
        has_cycle = session.execute(
            text("""
                WITH RECURSIVE edges(parent_id, child_id) AS (
                    SELECT
                        revision.articulo_resultado_id,
                        component.articulo_componente_id
                    FROM scm_estructura_revision AS revision
                    JOIN scm_estructura_componente AS component
                      ON component.revision_id = revision.id
                    WHERE revision.estado = 'APROBADA'
                       OR revision.id = :candidate_id
                ),
                walk(origin_id, node_id, path, cycle) AS (
                    SELECT
                        edge.parent_id,
                        edge.child_id,
                        ARRAY[edge.parent_id, edge.child_id],
                        edge.parent_id = edge.child_id
                    FROM edges AS edge

                    UNION ALL

                    SELECT
                        walk.origin_id,
                        edge.child_id,
                        walk.path || edge.child_id,
                        edge.child_id = ANY(walk.path)
                    FROM walk
                    JOIN edges AS edge
                      ON edge.parent_id = walk.node_id
                    WHERE NOT walk.cycle
                )
                SELECT EXISTS (
                    SELECT 1 FROM walk WHERE cycle
                )
            """),
            {"candidate_id": candidate.id},
        ).scalar_one()
        if has_cycle:
            raise ScmServiceError(
                "STRUCTURE_CYCLE",
                "La estructura introduce un ciclo directo o indirecto.",
                status_code=422,
            )
        return

    approved = session.scalars(
        select(ScmEstructuraRevision).where(
            ScmEstructuraRevision.estado == ESTADO_ESTRUCTURA_APROBADA,
            ScmEstructuraRevision.id != candidate.id,
        )
    ).all()
    adjacency = {}
    for revision in [*approved, candidate]:
        adjacency[revision.articulo_resultado_id] = {
            line.articulo_componente_id for line in revision.componentes
        }

    visiting = set()
    visited = set()

    def visit(article_id):
        if article_id in visiting:
            return True
        if article_id in visited:
            return False
        visiting.add(article_id)
        for component_id in adjacency.get(article_id, ()):
            if visit(component_id):
                return True
        visiting.remove(article_id)
        visited.add(article_id)
        return False

    if any(visit(article_id) for article_id in tuple(adjacency)):
        raise ScmServiceError(
            "STRUCTURE_CYCLE",
            "La estructura introduce un ciclo directo o indirecto.",
            status_code=422,
        )


def list_structures(session, *, actor_id, article_id):
    load_actor(session, actor_id, capability="ESTRUCTURA_VER")
    revisions = session.scalars(
        select(ScmEstructuraRevision)
        .where(ScmEstructuraRevision.articulo_resultado_id == article_id)
        .order_by(ScmEstructuraRevision.numero_revision.desc())
    ).all()
    return {"items": [serialize_structure(item) for item in revisions]}


def get_structure(session, *, actor_id, structure_id):
    load_actor(session, actor_id, capability="ESTRUCTURA_VER")
    revision = session.get(ScmEstructuraRevision, structure_id)
    if revision is None:
        raise ScmServiceError(
            "STRUCTURE_NOT_FOUND",
            "La revision de estructura no existe.",
            status_code=404,
        )
    return serialize_structure(revision)


def create_structure(
    session, *, actor_id, article_id, data, commit=True
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ESTRUCTURA_ADMINISTRAR",
        )
        reject_unknown_fields(data, allowed={"notas", "componentes"})
        result_article = session.scalar(
            select(ScmArticulo)
            .where(ScmArticulo.id == article_id)
            .with_for_update()
        )
        if result_article is None:
            raise ScmServiceError(
                "ARTICLE_NOT_FOUND",
                "El articulo resultado no existe.",
                status_code=404,
            )
        if not result_article.activo:
            raise ScmServiceError(
                "ARTICLE_INACTIVE",
                "El articulo resultado debe estar activo.",
                status_code=422,
            )
        if result_article.clase not in RESULT_CLASSES:
            raise ScmServiceError(
                "STRUCTURE_RESULT_CLASS_INVALID",
                "Solo un WIP o ProductoTerminado puede ser resultado de una estructura.",
                status_code=422,
                details={"clase": result_article.clase},
            )
        open_revision = session.scalar(
            select(ScmEstructuraRevision.id).where(
                ScmEstructuraRevision.articulo_resultado_id == article_id,
                ScmEstructuraRevision.estado.in_(
                    (
                        ESTADO_ESTRUCTURA_BORRADOR,
                        ESTADO_ESTRUCTURA_PENDIENTE,
                    )
                ),
            )
        )
        if open_revision is not None:
            raise ScmServiceError(
                "STRUCTURE_OPEN_REVISION_EXISTS",
                "El articulo ya tiene una revision abierta.",
                status_code=409,
            )
        last_number = session.scalar(
            select(ScmEstructuraRevision.numero_revision)
            .where(
                ScmEstructuraRevision.articulo_resultado_id == article_id
            )
            .order_by(ScmEstructuraRevision.numero_revision.desc())
            .limit(1)
        )
        revision = ScmEstructuraRevision(
            articulo_resultado=result_article,
            numero_revision=(last_number or 0) + 1,
            estado=ESTADO_ESTRUCTURA_BORRADOR,
            notas=_optional_text(
                data.get("notas"),
                field="notas",
                max_length=4000,
            ),
            creada_por_id=actor.id,
            componentes=_new_components(
                session,
                data.get("componentes"),
                result_article_id=article_id,
            ),
        )
        session.add(revision)
        session.flush()
        session.add(_event(revision, actor, "STRUCTURE_CREATED"))
        if commit:
            session.commit()
        return serialize_structure(revision)
    except ScmServiceError:
        if commit:
            session.rollback()
        raise
    except IntegrityError as error:
        if commit:
            session.rollback()
        raise ScmServiceError(
            "STRUCTURE_CONFLICT",
            "La estructura entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_structure(
    session, *, actor_id, structure_id, data, commit=True
):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ESTRUCTURA_ADMINISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={"version", "notas", "componentes"},
        )
        revision = _locked_structure(session, structure_id)
        _check_version(revision, data.get("version"))
        if revision.estado != ESTADO_ESTRUCTURA_BORRADOR:
            raise ScmServiceError(
                "STRUCTURE_NOT_EDITABLE",
                "Solo una estructura BORRADOR puede editarse.",
                status_code=409,
            )
        before = serialize_structure(revision)
        components = _new_components(
            session,
            data.get("componentes"),
            result_article_id=revision.articulo_resultado_id,
        )
        for existing in list(revision.componentes):
            session.delete(existing)
        session.flush()
        revision.componentes = components
        revision.notas = _optional_text(
            data.get("notas"),
            field="notas",
            max_length=4000,
        )
        revision.version += 1
        session.flush()
        session.add(
            _event(
                revision,
                actor,
                "STRUCTURE_UPDATED",
                before=before,
            )
        )
        if commit:
            session.commit()
        return serialize_structure(revision)
    except ScmServiceError:
        if commit:
            session.rollback()
        raise
    except IntegrityError as error:
        if commit:
            session.rollback()
        raise ScmServiceError(
            "STRUCTURE_CONFLICT",
            "No se pudo actualizar la estructura.",
            status_code=409,
        ) from error


def send_structure_for_approval(
    session,
    *,
    actor_id,
    structure_id,
    operation_id,
    data,
    commit=True,
):
    endpoint = f"/estructuras/{structure_id}/enviar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ESTRUCTURA_ADMINISTRAR",
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
            if commit:
                session.rollback()
            return replay
        revision = _locked_structure(session, structure_id)
        _check_version(revision, data.get("version"))
        if revision.estado != ESTADO_ESTRUCTURA_BORRADOR:
            raise ScmServiceError(
                "STRUCTURE_NOT_APPROVABLE",
                "Solo una estructura BORRADOR puede enviarse.",
                status_code=409,
            )
        if not revision.componentes:
            raise ScmServiceError(
                "STRUCTURE_COMPONENTS_REQUIRED",
                "La estructura requiere componentes.",
                status_code=422,
            )
        revision.estado = ESTADO_ESTRUCTURA_PENDIENTE
        revision.enviada_at = utc_now()
        revision.version += 1
        session.flush()
        session.add(
            _event(
                revision,
                actor,
                "STRUCTURE_SUBMITTED",
                operation=operation,
            )
        )
        response = serialize_structure(revision)
        operation.estado_http = 200
        operation.response_json = response
        if commit:
            session.commit()
        return response
    except ScmServiceError:
        if commit:
            session.rollback()
        raise
    except IntegrityError as error:
        if commit:
            session.rollback()
        raise ScmServiceError(
            "STRUCTURE_CONFLICT",
            "No se pudo enviar la estructura.",
            status_code=409,
        ) from error


def _publish_locked_structure(
    session,
    *,
    revision,
    actor,
    operation,
    event_type,
):
    if not revision.componentes:
        raise ScmServiceError(
            "STRUCTURE_COMPONENTS_REQUIRED",
            "La estructura requiere componentes.",
            status_code=422,
        )
    if not revision.articulo_resultado.activo or any(
        not line.articulo_componente.activo
        for line in revision.componentes
    ):
        raise ScmServiceError(
            "ARTICLE_INACTIVE",
            "Resultado y componentes deben permanecer activos.",
            status_code=422,
        )
    _assert_acyclic(session, revision)

    previous_approved = session.scalars(
        select(ScmEstructuraRevision)
        .where(
            ScmEstructuraRevision.articulo_resultado_id
            == revision.articulo_resultado_id,
            ScmEstructuraRevision.estado == ESTADO_ESTRUCTURA_APROBADA,
            ScmEstructuraRevision.id != revision.id,
        )
        .with_for_update()
    ).all()
    for previous in previous_approved:
        previous.estado = ESTADO_ESTRUCTURA_RETIRADA
        previous.retirada_por_id = actor.id
        previous.retirada_at = utc_now()
        previous.version += 1
    if previous_approved:
        session.flush()

    now = utc_now()
    revision.content_hash = _content_hash(revision)
    revision.estado = ESTADO_ESTRUCTURA_APROBADA
    revision.enviada_at = revision.enviada_at or now
    revision.aprobada_por_id = actor.id
    revision.aprobada_at = now
    revision.version += 1
    session.flush()
    session.add(
        _event(
            revision,
            actor,
            event_type,
            operation=operation,
        )
    )


def publish_structure_directly(
    session,
    *,
    actor_id,
    structure_id,
    operation_id,
    data,
    commit=True,
):
    endpoint = f"/estructuras/{structure_id}/publicar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ESTRUCTURA_PUBLICAR_DIRECTO",
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
            if commit:
                session.rollback()
            return replay
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('scm_bom_approval_graph'))"
            ))
        revision = _locked_structure(session, structure_id)
        _check_version(revision, data.get("version"))
        if revision.estado != ESTADO_ESTRUCTURA_BORRADOR:
            raise ScmServiceError(
                "STRUCTURE_NOT_PUBLISHABLE",
                "Solo una estructura BORRADOR puede publicarse directamente.",
                status_code=409,
            )
        _publish_locked_structure(
            session,
            revision=revision,
            actor=actor,
            operation=operation,
            event_type="STRUCTURE_PUBLISHED_DIRECTLY",
        )
        response = serialize_structure(revision)
        operation.estado_http = 200
        operation.response_json = response
        if commit:
            session.commit()
        return response
    except ScmServiceError:
        if commit:
            session.rollback()
        raise
    except IntegrityError as error:
        if commit:
            session.rollback()
        raise ScmServiceError(
            "STRUCTURE_CONFLICT",
            "No se pudo publicar la estructura.",
            status_code=409,
        ) from error


def approve_structure(
    session,
    *,
    actor_id,
    structure_id,
    operation_id,
    data,
):
    endpoint = f"/estructuras/{structure_id}/aprobar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ESTRUCTURA_APROBAR",
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
                "hashtext('scm_bom_approval_graph'))"
            ))
        revision = _locked_structure(session, structure_id)
        _check_version(revision, data.get("version"))
        if revision.estado != ESTADO_ESTRUCTURA_PENDIENTE:
            raise ScmServiceError(
                "STRUCTURE_NOT_APPROVABLE",
                "Solo una estructura pendiente puede aprobarse.",
                status_code=409,
            )
        if revision.creada_por_id == actor.id:
            raise ScmServiceError(
                "CREATOR_CANNOT_APPROVE",
                "El creador de la revision no puede aprobarla.",
                status_code=403,
            )
        _publish_locked_structure(
            session,
            revision=revision,
            actor=actor,
            operation=operation,
            event_type="STRUCTURE_APPROVED",
        )
        response = serialize_structure(revision)
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
            "STRUCTURE_CONFLICT",
            "No se pudo aprobar la estructura.",
            status_code=409,
        ) from error


def reject_structure(
    session,
    *,
    actor_id,
    structure_id,
    operation_id,
    data,
):
    endpoint = f"/estructuras/{structure_id}/rechazar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ESTRUCTURA_APROBAR",
        )
        reject_unknown_fields(data, allowed={"version", "motivo"})
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
        revision = _locked_structure(session, structure_id)
        _check_version(revision, data.get("version"))
        if revision.estado != ESTADO_ESTRUCTURA_PENDIENTE:
            raise ScmServiceError(
                "STRUCTURE_NOT_REJECTABLE",
                "Solo una estructura pendiente puede rechazarse.",
                status_code=409,
            )
        if revision.creada_por_id == actor.id:
            raise ScmServiceError(
                "CREATOR_CANNOT_REVIEW",
                "El creador de la revision no puede rechazarla.",
                status_code=403,
            )
        revision.estado = ESTADO_ESTRUCTURA_RECHAZADA
        revision.rechazada_por_id = actor.id
        revision.rechazada_at = utc_now()
        revision.motivo_rechazo = _required_reason(
            data.get("motivo"),
            field="motivo",
        )
        revision.version += 1
        session.flush()
        session.add(
            _event(
                revision,
                actor,
                "STRUCTURE_REJECTED",
                operation=operation,
            )
        )
        response = serialize_structure(revision)
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
            "STRUCTURE_CONFLICT",
            "No se pudo rechazar la estructura.",
            status_code=409,
        ) from error


def discard_structure(
    session,
    *,
    actor_id,
    structure_id,
    operation_id,
    data,
):
    endpoint = f"/estructuras/{structure_id}/descartar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ESTRUCTURA_ADMINISTRAR",
        )
        reject_unknown_fields(data, allowed={"version", "motivo"})
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
        revision = _locked_structure(session, structure_id)
        _check_version(revision, data.get("version"))
        if revision.estado != ESTADO_ESTRUCTURA_BORRADOR:
            raise ScmServiceError(
                "STRUCTURE_NOT_DISCARDABLE",
                "Solo una estructura en borrador puede descartarse.",
                status_code=409,
            )
        if revision.creada_por_id != actor.id:
            raise ScmServiceError(
                "ONLY_CREATOR_CAN_DISCARD",
                "Solo el creador puede descartar su borrador.",
                status_code=403,
            )
        revision.estado = ESTADO_ESTRUCTURA_DESCARTADA
        revision.descartada_por_id = actor.id
        revision.descartada_at = utc_now()
        revision.motivo_descarte = _required_reason(
            data.get("motivo"),
            field="motivo",
        )
        revision.version += 1
        session.flush()
        session.add(
            _event(
                revision,
                actor,
                "STRUCTURE_DISCARDED",
                operation=operation,
            )
        )
        response = serialize_structure(revision)
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
            "STRUCTURE_CONFLICT",
            "No se pudo descartar la estructura.",
            status_code=409,
        ) from error


def retire_structure(
    session,
    *,
    actor_id,
    structure_id,
    operation_id,
    data,
):
    endpoint = f"/estructuras/{structure_id}/retirar"
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="ESTRUCTURA_APROBAR",
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
        revision = _locked_structure(session, structure_id)
        _check_version(revision, data.get("version"))
        if revision.estado != ESTADO_ESTRUCTURA_APROBADA:
            raise ScmServiceError(
                "STRUCTURE_NOT_RETIRABLE",
                "Solo una estructura aprobada puede retirarse.",
                status_code=409,
            )
        revision.estado = ESTADO_ESTRUCTURA_RETIRADA
        revision.retirada_por_id = actor.id
        revision.retirada_at = utc_now()
        revision.version += 1
        session.flush()
        session.add(
            _event(
                revision,
                actor,
                "STRUCTURE_RETIRED",
                operation=operation,
            )
        )
        response = serialize_structure(revision)
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
            "STRUCTURE_CONFLICT",
            "No se pudo retirar la estructura.",
            status_code=409,
        ) from error
