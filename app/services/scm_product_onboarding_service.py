"""Durable, resumable workflow state for guided PT onboarding.

This increment intentionally does not create BOMs, routes, recipes or
packaging rules. It records opaque step drafts, their provenance and canonical
references, and evaluates structural readiness only.
"""

import copy
import hashlib
import json
import uuid
from datetime import timezone
import math

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session

from app.models.molde import Molde, MoldePieza, Pieza
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    FamiliaColor,
    PiezaColor,
    ProductoTerminado,
)
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_catalogos import ScmMaterial
from app.models.scm_articulos import (
    CLASE_PIEZA_COLOR,
    CLASE_PRODUCTO_TERMINADO,
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
    ScmArticuloPiezaColor,
    ScmArticuloProducto,
)
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_empaque import (
    ESTADO_REGLA_APROBADA,
    ESTADO_REGLA_BORRADOR,
    ScmArticuloPerfil,
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_estructuras import (
    ESTADO_ESTRUCTURA_APROBADA,
    ESTADO_ESTRUCTURA_BORRADOR,
    ESTADO_ESTRUCTURA_PENDIENTE,
    ScmEstructuraRevision,
)
from app.models.scm_product_onboarding import (
    ONBOARDING_STEPS,
    SESSION_STATES,
    ScmAltaProductoSesion,
    utc_now,
)
from app.models.scm_rutas import (
    ESTADO_RUTA_APROBADA,
    ESTADO_RUTA_BORRADOR,
    ScmRutaRevision,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    reject_unknown_fields,
    required_text,
)
from app.services.catalog_product_service import (
    CatalogProductError,
    create_finished_product,
)
from app.services.catalog_classification_service import (
    ClassificationError,
    validate_linea_familia,
)
from app.services.catalog_code_generator import generar_codigo_catalogo
from app.services.scm_article_service import create_wip_article
from app.services.catalog_image_storage import (
    CatalogImageStorageError,
    CatalogImageValidationError,
    get_catalog_image_storage,
    validate_catalog_image_content,
)
from app.services.color_recipe_service import (
    ColorRecipeError,
    create_recipe,
    find_default_recipe,
    normalize_hex,
)
from app.services.scm_packaging_service import (
    _approval_viability,
    assign_article_profiles,
    create_packable_profile,
    create_packaging_rule,
    publish_packaging_rule_directly,
    update_packable_profile,
    update_packaging_rule,
)
from app.services.scm_route_service import (
    _assert_route_approvable,
    create_route,
    publish_route_directly,
    update_route,
)
from app.services.scm_structure_service import (
    _assert_acyclic,
    create_structure,
    publish_structure_directly,
    send_structure_for_approval,
    update_structure,
)


CREATE_FIELDS = {"titulo", "producto_terminado_id", "data"}
STEP_UPDATE_FIELDS = {"expected_version", "data", "estado_paso"}
VERSION_COMMAND_FIELDS = {"expected_version"}
APPLICATION_FIELDS = {
    "expected_version",
    "application_key",
    "supersedes_application_key",
    "data",
}
APPLICATION_STEPS = {
    "IDENTIDAD",
    "COMPONENTES",
    "COLORES",
    "ESTRUCTURA",
    "RUTA_EMPAQUE",
}
STEP_STATES = {"PENDIENTE", "EN_PROGRESO", "COMPLETADO", "INVALIDADO"}
CLIENT_STEP_STATES = STEP_STATES - {"INVALIDADO"}
IMMUTABLE_SESSION_STATES = {"FINALIZADA", "ABANDONADA"}
PLACEHOLDER_TITLES = {"nuevo producto terminado"}
STEP_ALIASES = {
    "COMPLETO": "COMPLETADO",
    "COMPLETA": "COMPLETADO",
    "COMPLETADA": "COMPLETADO",
}
DIRECT_PUBLICATION_HANDOFFS = (
    (
        "ESTRUCTURA",
        "ESTRUCTURA_PUBLICAR_DIRECTO",
        "ESTRUCTURA_APROBAR",
    ),
    ("RUTA", "RUTA_PUBLICAR_DIRECTO", "RUTA_APROBAR"),
    ("EMPAQUE", "EMPAQUE_PUBLICAR_DIRECTO", "EMPAQUE_APROBAR"),
)
REVISION_REVIEWED_STEPS = tuple(
    code for code in ONBOARDING_STEPS if code != "REVISION"
)
REVISION_SNAPSHOT_TYPES = {
    "ESTRUCTURA",
    "RUTA",
    "PERFIL_EMPAQUE",
    "REGLA_EMPAQUE",
}


def _json_hash(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _operation_hash(endpoint, actor_id, data):
    return _json_hash({
        "endpoint": endpoint,
        "actor_id": actor_id,
        "data": data,
    })


def _reserve_operation(session, operation_id, endpoint, actor, data):
    if operation_id is None:
        return None, None
    request_hash = _operation_hash(endpoint, actor.id, data)
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
        replay = copy.deepcopy(existing.response_json)
        if existing.estado_http and existing.estado_http >= 400:
            raise ScmServiceError(
                replay.get("code", "IDEMPOTENT_OPERATION_FAILED"),
                replay.get("message", "La operacion previa fallo."),
                status_code=existing.estado_http,
                details=replay.get("details"),
            )
        return None, replay
    operation = ScmOperacion(
        operation_id=operation_id,
        endpoint=endpoint,
        actor_id=actor.id,
        request_sha256=request_hash,
    )
    session.add(operation)
    session.flush()
    return operation, None


def _complete_operation(operation, response, status_code):
    if operation is None:
        return
    operation.response_json = copy.deepcopy(response)
    operation.estado_http = status_code


def _event(
    onboarding,
    actor,
    event_type,
    operation,
    *,
    before=None,
    after=None,
):
    return ScmEvento(
        aggregate_type="ALTA_PRODUCTO_SESION",
        aggregate_id=str(onboarding.id),
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=after,
        operation_id=(operation.operation_id if operation else None),
    )


def _isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _step_data(onboarding):
    draft = onboarding.borrador_json or {}
    return {
        code: copy.deepcopy(draft.get(code) or {})
        for code in ONBOARDING_STEPS
    }


def _step_states(onboarding):
    stored = onboarding.estados_paso_json or {}
    return {
        code: (
            stored.get(code)
            if stored.get(code) in STEP_STATES
            else "PENDIENTE"
        )
        for code in ONBOARDING_STEPS
    }


def _step_application_summary(onboarding, code):
    step_journal = (onboarding.application_journal_json or {}).get(code)
    if not isinstance(step_journal, dict) or not step_journal:
        return None
    candidates = [
        (application_key, entry)
        for application_key, entry in step_journal.items()
        if isinstance(entry, dict)
    ]
    current_refs = (onboarding.referencias_json or {}).get(code) or {}
    matching = [
        pair for pair in candidates
        if (pair[1].get("result") or {}).get("resolved_references")
        == current_refs
    ]
    if matching:
        candidates = matching
    if not candidates:
        return None
    application_key, entry = max(
        candidates,
        key=lambda pair: (
            int(pair[1].get("session_version") or 0),
            str(pair[1].get("recorded_at") or ""),
            str(pair[0]),
        ),
    )
    if not isinstance(entry, dict):
        return None
    result = entry.get("result") or {}
    return {
        "status": entry.get("status"),
        "application_key": application_key,
        "paso": code,
        "created": copy.deepcopy(result.get("created") or []),
        "reused": copy.deepcopy(result.get("reused") or []),
        "pending": copy.deepcopy(result.get("pending") or []),
        "resolved_references": copy.deepcopy(
            result.get("resolved_references") or {}
        ),
    }


def _image_application_summaries(onboarding):
    journal = (onboarding.application_journal_json or {}).get("IMAGENES")
    if not isinstance(journal, dict):
        return []
    latest = {}
    for application_key, entry in journal.items():
        if not isinstance(entry, dict) or entry.get("status") != "APPLIED":
            continue
        result = entry.get("result") or {}
        entity_type = result.get("entity_type")
        entity_id = result.get("entity_id")
        if entity_type not in {"PRODUCTO_TERMINADO", "PIEZA_COLOR"}:
            continue
        if entity_id in (None, ""):
            continue
        identity = (entity_type, str(entity_id))
        candidate = (
            int(entry.get("session_version") or 0),
            str(entry.get("recorded_at") or ""),
            str(application_key),
        )
        current = latest.get(identity)
        if current is None or candidate > current[0]:
            latest[identity] = (candidate, application_key, result)
    return [
        {
            "status": "APPLIED",
            "application_key": application_key,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "mime_type": result.get("mime_type"),
            "size_bytes": result.get("size_bytes"),
            "sha256": result.get("sha256"),
            "imagen_url": result.get("imagen_url"),
        }
        for (entity_type, entity_id), (_order, application_key, result)
        in sorted(latest.items())
    ]


def serialize_onboarding(onboarding):
    draft = _step_data(onboarding)
    states = _step_states(onboarding)
    blockers = onboarding.bloqueos_paso_json or {}
    return {
        "id": str(onboarding.id),
        "titulo": onboarding.titulo,
        "producto_terminado_id": onboarding.producto_terminado_id,
        "estado": onboarding.estado,
        "version": onboarding.version,
        "paso_actual": onboarding.paso_actual,
        "pasos": [
            {
                "codigo": code,
                "estado": states[code],
                "data": draft[code],
                "bloqueos": copy.deepcopy(blockers.get(code) or []),
                "application_status": _step_application_summary(
                    onboarding, code
                ),
            }
            for code in ONBOARDING_STEPS
        ],
        "fuentes": copy.deepcopy(onboarding.fuentes_json or {}),
        "referencias": copy.deepcopy(onboarding.referencias_json or {}),
        "readiness": copy.deepcopy(onboarding.readiness_json or {}),
        "imagenes": _image_application_summaries(onboarding),
        "invalidated_steps": copy.deepcopy(
            onboarding.invalidated_steps_json or []
        ),
        "created_at": _isoformat(onboarding.created_at),
        "updated_at": _isoformat(onboarding.updated_at),
        "finalizada_at": _isoformat(onboarding.finalizada_at),
    }


def _json_object(value, *, field, required=False):
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ScmServiceError(
            "JSON_OBJECT_REQUIRED",
            f"El campo {field} debe ser un objeto JSON.",
            status_code=400,
            details={"field": field},
        )
    return copy.deepcopy(value)


def _optional_text(value, *, field, max_length):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return required_text(value, field=field, max_length=max_length)


def _application_key(value):
    return required_text(
        value,
        field="application_key",
        max_length=100,
    )


def _step_code(value):
    normalized = str(value or "").strip().upper()
    if normalized not in ONBOARDING_STEPS:
        raise ScmServiceError(
            "INVALID_ONBOARDING_STEP",
            "El paso solicitado no pertenece al alta guiada.",
            status_code=404,
            details={"step": normalized, "allowed": list(ONBOARDING_STEPS)},
        )
    return normalized


def _step_state(value):
    if not isinstance(value, str):
        raise ScmServiceError(
            "INVALID_STEP_STATE",
            "estado_paso debe ser texto.",
            status_code=400,
        )
    normalized = value.strip().upper()
    normalized = STEP_ALIASES.get(normalized, normalized)
    if normalized not in CLIENT_STEP_STATES:
        raise ScmServiceError(
            "INVALID_STEP_STATE",
            "estado_paso no es valido.",
            status_code=422,
            details={"allowed": sorted(CLIENT_STEP_STATES)},
        )
    return normalized


def _normalize_revision_snapshot(value, *, required):
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ScmServiceError(
            "INVALID_REVISION_SNAPSHOT",
            "data.revisiones_revisadas debe ser una lista de revisiones.",
            status_code=422,
        )
    normalized = []
    identities = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ScmServiceError(
                "INVALID_REVISION_SNAPSHOT",
                "Cada revision revisada debe ser un objeto.",
                status_code=422,
                details={"index": index},
            )
        reject_unknown_fields(
            item,
            allowed={"tipo", "id", "version", "content_hash"},
        )
        entity_type = str(item.get("tipo") or "").strip().upper()
        entity_id = _readiness_int_reference(item.get("id"))
        version = _readiness_int_reference(item.get("version"))
        content_hash = item.get("content_hash")
        if entity_type not in REVISION_SNAPSHOT_TYPES:
            raise ScmServiceError(
                "INVALID_REVISION_SNAPSHOT",
                "El tipo de revision revisada no es valido.",
                status_code=422,
                details={
                    "index": index,
                    "allowed": sorted(REVISION_SNAPSHOT_TYPES),
                },
            )
        if entity_id is None or version is None:
            raise ScmServiceError(
                "INVALID_REVISION_SNAPSHOT",
                "Cada revision revisada requiere id y version positivos.",
                status_code=422,
                details={"index": index},
            )
        if content_hash is not None and (
            not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in content_hash)
        ):
            raise ScmServiceError(
                "INVALID_REVISION_SNAPSHOT",
                "content_hash debe ser un SHA-256 hexadecimal o null.",
                status_code=422,
                details={"index": index},
            )
        identity = (entity_type, entity_id)
        if identity in identities:
            raise ScmServiceError(
                "INVALID_REVISION_SNAPSHOT",
                "No se puede repetir una revision revisada.",
                status_code=422,
                details={"tipo": entity_type, "id": entity_id},
            )
        identities.add(identity)
        normalized.append({
            "tipo": entity_type,
            "id": entity_id,
            "version": version,
            "content_hash": content_hash,
        })
    return sorted(normalized, key=lambda item: (item["tipo"], item["id"]))


def _validate_revision_payload(payload, *, require_complete):
    reject_unknown_fields(
        payload,
        allowed={
            "confirmaciones",
            "pasos_revisados",
            "revisiones_revisadas",
            "notas",
        },
    )
    confirmations = payload.get("confirmaciones")
    if confirmations is None and require_complete:
        raise ScmServiceError(
            "REVISION_CONFIRMATION_REQUIRED",
            "Complete las confirmaciones antes de cerrar.",
            status_code=422,
        )
    if confirmations is None and not require_complete:
        confirmations = {}
    confirmations = _json_object(
        confirmations,
        field="data.confirmaciones",
        required=require_complete,
    )
    reject_unknown_fields(
        confirmations,
        allowed={
            "datos_fuente_revisados",
            "entiende_que_no_crea_op",
            "pendientes_aceptados",
        },
    )
    for field, value in confirmations.items():
        if not isinstance(value, bool):
            raise ScmServiceError(
                "INVALID_REVISION_CONFIRMATION",
                f"data.confirmaciones.{field} debe ser booleano.",
                status_code=422,
                details={"field": field},
            )
    reviewed = payload.get("pasos_revisados")
    if reviewed is None and not require_complete:
        reviewed = []
    if not isinstance(reviewed, list) or any(
        not isinstance(item, str) for item in reviewed
    ):
        raise ScmServiceError(
            "INVALID_REVISION_CHECKLIST",
            "data.pasos_revisados debe ser una lista de pasos.",
            status_code=422,
        )
    normalized_reviewed = [item.strip().upper() for item in reviewed]
    if len(normalized_reviewed) != len(set(normalized_reviewed)):
        raise ScmServiceError(
            "INVALID_REVISION_CHECKLIST",
            "No se pueden repetir pasos revisados.",
            status_code=422,
        )
    unknown = set(normalized_reviewed) - set(REVISION_REVIEWED_STEPS)
    if unknown:
        raise ScmServiceError(
            "INVALID_REVISION_CHECKLIST",
            "La lista contiene pasos desconocidos.",
            status_code=422,
            details={"unknown_steps": sorted(unknown)},
        )
    if "notas" in payload:
        _optional_text(payload.get("notas"), field="data.notas", max_length=4000)
    revision_snapshot = _normalize_revision_snapshot(
        payload.get("revisiones_revisadas"),
        required=require_complete,
    )
    if require_complete and (
        confirmations.get("datos_fuente_revisados") is not True
        or confirmations.get("entiende_que_no_crea_op") is not True
        or not isinstance(confirmations.get("pendientes_aceptados"), bool)
        or set(normalized_reviewed) != set(REVISION_REVIEWED_STEPS)
    ):
        raise ScmServiceError(
            "REVISION_CONFIRMATION_REQUIRED",
            "Complete las confirmaciones y revise todos los pasos antes de cerrar.",
            status_code=422,
            details={
                "required_steps": list(REVISION_REVIEWED_STEPS),
                "required_confirmations": [
                    "datos_fuente_revisados",
                    "entiende_que_no_crea_op",
                    "pendientes_aceptados",
                ],
            },
        )
    return confirmations, revision_snapshot


def _extract_sources(data):
    result = {}
    for key in ("procedencia", "fuente", "fuentes"):
        if key in data and data[key] is not None:
            result[key] = copy.deepcopy(data[key])
    return result


def _extract_references(data):
    explicit = data.get("referencias")
    if explicit is not None and not isinstance(explicit, dict):
        raise ScmServiceError(
            "INVALID_REFERENCES",
            "data.referencias debe ser un objeto JSON.",
            status_code=400,
        )
    result = copy.deepcopy(explicit or {})
    for key, value in data.items():
        if key.endswith("_ref") and value not in (None, ""):
            result[key] = copy.deepcopy(value)
    return {key: result[key] for key in sorted(result)}


def _product_reference(data):
    """Return a single canonical PT id from supported IDENTIDAD shapes."""
    candidates = []
    explicit = data.get("referencias")
    if isinstance(explicit, dict):
        explicit_id = explicit.get("producto_terminado_id")
        if explicit_id not in (None, ""):
            candidates.append(("referencias.producto_terminado_id", explicit_id))

    product_ref = data.get("producto_ref")
    if product_ref not in (None, ""):
        if isinstance(product_ref, str):
            candidates.append(("producto_ref", product_ref))
        elif isinstance(product_ref, dict):
            nested_id = (
                product_ref.get("cod_sku_pt")
                or product_ref.get("producto_terminado_id")
                or product_ref.get("id")
            )
            if nested_id in (None, ""):
                raise ScmServiceError(
                    "INVALID_PRODUCT_REFERENCE",
                    "producto_ref debe incluir cod_sku_pt.",
                    status_code=400,
                )
            candidates.append(("producto_ref.cod_sku_pt", nested_id))
        else:
            raise ScmServiceError(
                "INVALID_PRODUCT_REFERENCE",
                "producto_ref debe ser un SKU o un objeto de producto.",
                status_code=400,
            )

    normalized = []
    for field, value in candidates:
        normalized.append((
            field,
            required_text(value, field=field, max_length=50),
        ))
    distinct = {value for _, value in normalized}
    if len(distinct) > 1:
        raise ScmServiceError(
            "PRODUCT_REFERENCE_CONFLICT",
            "Las referencias de ProductoTerminado no coinciden.",
            status_code=422,
            details={
                "references": {
                    field: value for field, value in normalized
                }
            },
        )
    return (bool(normalized), next(iter(distinct), None))


def _require_existing_product(session, product_id):
    if product_id is None:
        return None
    product = session.get(ProductoTerminado, product_id)
    if product is None:
        raise ScmServiceError(
            "PRODUCT_NOT_FOUND",
            "El ProductoTerminado de referencia no existe.",
            status_code=404,
            details={"producto_terminado_id": product_id},
        )
    return product.cod_sku_pt


def _refresh_placeholder_title(onboarding, step_payload):
    if onboarding.titulo.strip().casefold() not in PLACEHOLDER_TITLES:
        return
    product = step_payload.get("producto")
    if not isinstance(product, dict):
        return
    candidate = product.get("producto")
    if not isinstance(candidate, str) or not candidate.strip():
        return
    onboarding.titulo = required_text(
        candidate,
        field="data.producto.producto",
        max_length=200,
    )


def _readiness_reference(references, step, *names):
    step_refs = references.get(step) or {}
    for name in names:
        value = step_refs.get(name)
        if value not in (None, ""):
            return value
    return None


def _readiness_int_reference(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _canonical_revision_snapshot(onboarding):
    """Return the exact canonical revisions reviewed by the operator."""

    session = object_session(onboarding)
    references = onboarding.referencias_json or {}
    snapshot = {}

    def add(entity_type, entity):
        if entity is None:
            return
        identity = (entity_type, entity.id)
        snapshot[identity] = {
            "tipo": entity_type,
            "id": entity.id,
            "version": entity.version,
            "content_hash": getattr(entity, "content_hash", None),
        }

    structure_id = _readiness_int_reference(_readiness_reference(
        references,
        "ESTRUCTURA",
        "estructura_revision_ref",
        "estructura_ref",
    ))
    add(
        "ESTRUCTURA",
        session.get(ScmEstructuraRevision, structure_id)
        if structure_id else None,
    )

    packaging_refs = references.get("RUTA_EMPAQUE") or {}
    route_id = _readiness_int_reference(
        packaging_refs.get("ruta_revision_ref")
        or packaging_refs.get("ruta_ref")
    )
    add(
        "RUTA",
        session.get(ScmRutaRevision, route_id) if route_id else None,
    )

    selected_packaging = [
        item for item in packaging_refs.get("empaques") or []
        if isinstance(item, dict)
    ]
    if not selected_packaging:
        selected_packaging = [{
            "perfil_empacable_ref": packaging_refs.get(
                "perfil_empacable_ref"
            ),
            "regla_empaque_revision_ref": packaging_refs.get(
                "regla_empaque_revision_ref"
            ),
        }]
    for item in selected_packaging:
        profile_id = _readiness_int_reference(
            item.get("perfil_empacable_ref")
        )
        rule_id = _readiness_int_reference(
            item.get("regla_empaque_revision_ref")
        )
        add(
            "PERFIL_EMPAQUE",
            session.get(ScmPerfilEmpacable, profile_id)
            if profile_id else None,
        )
        add(
            "REGLA_EMPAQUE",
            session.get(ScmReglaEmpaqueRevision, rule_id)
            if rule_id else None,
        )
    return [
        snapshot[identity]
        for identity in sorted(snapshot)
    ]


def _structural_readiness(onboarding, actor):
    """Evaluate current canonical masters, never the draft as authority."""

    session = object_session(onboarding)
    draft = _step_data(onboarding)
    states = _step_states(onboarding)
    references = onboarding.referencias_json or {}
    revision_snapshot = _canonical_revision_snapshot(onboarding)
    items = []
    blockers_by_step = {code: [] for code in ONBOARDING_STEPS}

    def add(
        code,
        *,
        paso,
        message,
        entity_type="ALTA_PRODUCTO",
        entity_id=None,
        result="BLOCKED",
        action="OPEN_STEP",
    ):
        item = {
            "code": code,
            "severity": "BLOCKER" if result == "BLOCKED" else "WARNING",
            "paso": paso,
            "entity": {"type": entity_type, "id": entity_id},
            "message": message,
            "action": action,
            "result": result,
        }
        items.append(item)
        if paso in blockers_by_step and result != "NOT_APPLICABLE":
            blockers_by_step[paso].append({
                "codigo": code,
                "paso": paso,
                "mensaje": message,
                "entity": copy.deepcopy(item["entity"]),
                "result": result,
            })

    for code in ONBOARDING_STEPS:
        if states[code] != "COMPLETADO":
            add(
                "STEP_NOT_COMPLETED",
                paso=code,
                message=f"El paso {code} aun no fue completado.",
                entity_id=str(onboarding.id),
            )
        elif not draft[code]:
            add(
                "STEP_WITHOUT_EVIDENCE",
                paso=code,
                message=f"El paso {code} no conserva evidencia revisable.",
                entity_id=str(onboarding.id),
            )

    product = (
        session.get(ProductoTerminado, onboarding.producto_terminado_id)
        if onboarding.producto_terminado_id
        else None
    )
    product_article = None
    if product is None:
        add(
            "PRODUCT_NOT_RESOLVED",
            paso="IDENTIDAD",
            message="La sesion no resuelve un ProductoTerminado vigente.",
            entity_type="PRODUCTO_TERMINADO",
            entity_id=onboarding.producto_terminado_id,
        )
    else:
        product_article = session.scalar(
            select(ScmArticulo)
            .join(
                ScmArticuloProducto,
                ScmArticuloProducto.articulo_id == ScmArticulo.id,
            )
            .where(
                ScmArticuloProducto.producto_terminado_id
                == product.cod_sku_pt
            )
        )
        if (
            product_article is None
            or product_article.clase != CLASE_PRODUCTO_TERMINADO
        ):
            add(
                "PRODUCT_ARTICLE_NOT_RESOLVED",
                paso="IDENTIDAD",
                message="El PT no tiene subtipo Articulo SCM resoluble.",
                entity_type="PRODUCTO_TERMINADO",
                entity_id=product.cod_sku_pt,
            )
        elif not product_article.activo:
            add(
                "PRODUCT_ARTICLE_INACTIVE",
                paso="IDENTIDAD",
                message="El Articulo SCM del PT esta inactivo.",
                entity_type="ARTICULO",
                entity_id=product_article.id,
            )
        if str(product.status or "").strip().upper() in {
            "INACTIVO",
            "INACTIVE",
            "RETIRADO",
            "DESCONTINUADO",
        }:
            add(
                "PRODUCT_INACTIVE",
                paso="IDENTIDAD",
                message="El ProductoTerminado esta inactivo.",
                entity_type="PRODUCTO_TERMINADO",
                entity_id=product.cod_sku_pt,
            )

    component_refs = references.get("COMPONENTES") or {}
    mold_ref = component_refs.get("molde_ref")
    mold = session.get(Molde, mold_ref) if mold_ref else None
    if mold is None or not mold.activo:
        add(
            "MOLD_NOT_ACTIVE",
            paso="COMPONENTES",
            message="El molde de la sesion no existe o esta inactivo.",
            entity_type="MOLDE",
            entity_id=mold_ref,
        )
    resolved_piece_ids = set()
    for value in component_refs.get("piezas") or []:
        if not isinstance(value, dict):
            continue
        piece_id = _readiness_int_reference(value.get("pieza_ref"))
        composition_id = _readiness_int_reference(
            value.get("molde_pieza_ref")
        )
        piece = session.get(Pieza, piece_id) if piece_id else None
        composition = (
            session.get(MoldePieza, composition_id)
            if composition_id else None
        )
        if (
            piece is None
            or not piece.activo
            or composition is None
            or not composition.activo
            or composition.pieza_id != piece_id
            or composition.molde_id != mold_ref
        ):
            add(
                "COMPONENT_REFERENCE_NOT_ACTIVE",
                paso="COMPONENTES",
                message="Una pieza o su vinculo con el molde ya no es vigente.",
                entity_type="PIEZA",
                entity_id=piece_id,
            )
        else:
            resolved_piece_ids.add(piece_id)
    if not resolved_piece_ids:
        add(
            "COMPONENTS_NOT_RESOLVED",
            paso="COMPONENTES",
            message="No hay piezas canonicas resueltas para el molde.",
            entity_type="MOLDE",
            entity_id=mold_ref,
        )

    color_refs = references.get("COLORES") or {}
    declared_color_ids = set()
    for value in color_refs.get("colores") or []:
        if not isinstance(value, dict):
            continue
        color_id = _readiness_int_reference(value.get("color_ref"))
        color = session.get(ColorProduccion, color_id) if color_id else None
        if color is None or not color.activo:
            add(
                "COLOR_NOT_ACTIVE",
                paso="COLORES",
                message="Un color declarado no existe o esta inactivo.",
                entity_type="COLOR_PRODUCCION",
                entity_id=color_id,
            )
        else:
            declared_color_ids.add(color_id)

    matrix_variants = []
    for value in color_refs.get("matriz") or []:
        if not isinstance(value, dict):
            continue
        variant_ref = value.get("pieza_color_ref")
        variant = session.get(PiezaColor, variant_ref) if variant_ref else None
        article = None
        if variant is not None:
            article = session.scalar(
                select(ScmArticulo)
                .join(
                    ScmArticuloPiezaColor,
                    ScmArticuloPiezaColor.articulo_id == ScmArticulo.id,
                )
                .where(
                    ScmArticuloPiezaColor.pieza_color_sku == variant.sku
                )
            )
        if (
            variant is None
            or variant.pieza_id not in resolved_piece_ids
            or variant.color_produccion_id not in declared_color_ids
            or variant.pieza_rel is None
            or not variant.pieza_rel.activo
            or variant.color_produccion_rel is None
            or not variant.color_produccion_rel.activo
            or article is None
            or not article.activo
        ):
            add(
                "PIECE_COLOR_NOT_RESOLVED",
                paso="COLORES",
                message="Una PiezaColor usada no es resoluble o esta inactiva.",
                entity_type="PIEZA_COLOR",
                entity_id=variant_ref,
            )
        else:
            matrix_variants.append((variant, article))
    if not matrix_variants:
        add(
            "COLOR_MATRIX_NOT_RESOLVED",
            paso="COLORES",
            message="La matriz no conserva salidas PiezaColor vigentes.",
            entity_type="MOLDE",
            entity_id=mold_ref,
        )

    formula_by_color = {}
    for value in color_refs.get("formulaciones") or []:
        if not isinstance(value, dict):
            continue
        color_id = _readiness_int_reference(value.get("color_ref"))
        formula_by_color[color_id] = value
        if value.get("estado") == "PENDIENTE" or value.get("tipo") == "PENDIENTE":
            add(
                "FORMULATION_PENDING",
                paso="COLORES",
                message="Una formulacion fue declarada pendiente.",
                entity_type="COLOR_PRODUCCION",
                entity_id=color_id,
            )
            continue
        recipe_id = _readiness_int_reference(value.get("receta_ref"))
        recipe = session.get(RecetaColorMaestra, recipe_id) if recipe_id else None
        if (
            recipe is None
            or recipe.color_produccion_id != color_id
            or recipe.estado == "INACTIVA"
            or recipe.producto_scope
            not in {"*", onboarding.producto_terminado_id}
        ):
            add(
                "FORMULATION_NOT_RESOLVED",
                paso="COLORES",
                message="La formulacion aplicable no es resoluble o vigente.",
                entity_type="RECETA_COLOR",
                entity_id=recipe_id,
            )
        elif recipe.estado != "APROBADA":
            add(
                "FORMULATION_NOT_APPROVED",
                paso="COLORES",
                message="La formulacion aun no esta aprobada.",
                entity_type="RECETA_COLOR",
                entity_id=recipe.id,
                result="PENDING_APPROVAL",
            )
        if recipe is not None:
            for line in recipe.lineas:
                material = session.get(ScmMaterial, line.material_id)
                if material is None or not material.activo:
                    add(
                        "FORMULATION_MATERIAL_INACTIVE",
                        paso="COLORES",
                        message=(
                            "La formulacion usa un material inexistente "
                            "o inactivo."
                        ),
                        entity_type="SCM_MATERIAL",
                        entity_id=line.material_id,
                    )
    for color_id in declared_color_ids - set(formula_by_color):
        add(
            "FORMULATION_MISSING",
            paso="COLORES",
            message="Un color declarado no tiene formulacion aplicable.",
            entity_type="COLOR_PRODUCCION",
            entity_id=color_id,
        )

    structure_ref = _readiness_int_reference(_readiness_reference(
        references,
        "ESTRUCTURA",
        "estructura_revision_ref",
        "estructura_ref",
    ))
    structure = (
        session.get(ScmEstructuraRevision, structure_ref)
        if structure_ref else None
    )
    if (
        structure is None
        or product_article is None
        or structure.articulo_resultado_id != product_article.id
    ):
        add(
            "STRUCTURE_NOT_RESOLVED",
            paso="ESTRUCTURA",
            message="La estructura referenciada no corresponde al PT.",
            entity_type="ESTRUCTURA",
            entity_id=structure_ref,
        )
    else:
        if structure.estado in {
            ESTADO_ESTRUCTURA_BORRADOR,
            ESTADO_ESTRUCTURA_PENDIENTE,
        }:
            add(
                "STRUCTURE_NOT_APPROVED",
                paso="ESTRUCTURA",
                message="La estructura aun no esta aprobada.",
                entity_type="ESTRUCTURA",
                entity_id=structure.id,
                result="PENDING_APPROVAL",
            )
        elif structure.estado != ESTADO_ESTRUCTURA_APROBADA:
            add(
                "STRUCTURE_NOT_CURRENT",
                paso="ESTRUCTURA",
                message="La estructura referenciada ya no esta vigente.",
                entity_type="ESTRUCTURA",
                entity_id=structure.id,
            )
        try:
            _assert_acyclic(session, structure)
        except ScmServiceError as error:
            add(
                error.code,
                paso="ESTRUCTURA",
                message=error.message,
                entity_type="ESTRUCTURA",
                entity_id=structure.id,
            )
        visited_wips = set()

        def validate_structure_components(revision, ancestry):
            for line in revision.componentes:
                component = line.articulo_componente
                if component is None or not component.activo:
                    add(
                        "STRUCTURE_COMPONENT_INACTIVE",
                        paso="ESTRUCTURA",
                        message="La estructura usa un articulo inactivo.",
                        entity_type="ARTICULO",
                        entity_id=line.articulo_componente_id,
                    )
                    continue
                if component.clase == CLASE_PIEZA_COLOR:
                    if component.pieza_color is None:
                        add(
                            "STRUCTURE_PIECE_COLOR_NOT_RESOLVED",
                            paso="ESTRUCTURA",
                            message=(
                                "Una PiezaColor de la estructura no es "
                                "resoluble."
                            ),
                            entity_type="ARTICULO",
                            entity_id=component.id,
                        )
                    continue
                if component.clase != CLASE_SUBENSAMBLE_WIP:
                    continue
                if component.id in ancestry:
                    add(
                        "STRUCTURE_CYCLE",
                        paso="ESTRUCTURA",
                        message="Las estructuras WIP forman un ciclo.",
                        entity_type="ARTICULO",
                        entity_id=component.id,
                    )
                    continue
                if component.id in visited_wips:
                    continue
                wip_structure = session.scalar(select(
                    ScmEstructuraRevision
                ).where(
                    ScmEstructuraRevision.articulo_resultado_id
                    == component.id,
                    ScmEstructuraRevision.estado
                    == ESTADO_ESTRUCTURA_APROBADA,
                ))
                if wip_structure is None:
                    pending_wip = session.scalar(select(
                        ScmEstructuraRevision.id
                    ).where(
                        ScmEstructuraRevision.articulo_resultado_id
                        == component.id,
                        ScmEstructuraRevision.estado.in_((
                            ESTADO_ESTRUCTURA_BORRADOR,
                            ESTADO_ESTRUCTURA_PENDIENTE,
                        )),
                    ))
                    add(
                        "WIP_STRUCTURE_NOT_APPROVED",
                        paso="ESTRUCTURA",
                        message=(
                            "Un WIP consumido no tiene estructura aprobada."
                        ),
                        entity_type="ARTICULO",
                        entity_id=component.id,
                        result=(
                            "PENDING_APPROVAL"
                            if pending_wip else "BLOCKED"
                        ),
                    )
                    continue
                visited_wips.add(component.id)
                validate_structure_components(
                    wip_structure,
                    ancestry | {component.id},
                )

        validate_structure_components(
            structure,
            {structure.articulo_resultado_id},
        )

    route_ref = _readiness_int_reference(_readiness_reference(
        references,
        "RUTA_EMPAQUE",
        "ruta_revision_ref",
        "ruta_ref",
    ))
    route = session.get(ScmRutaRevision, route_ref) if route_ref else None
    if (
        route is None
        or product_article is None
        or route.articulo_objetivo_id != product_article.id
    ):
        add(
            "ROUTE_NOT_RESOLVED",
            paso="RUTA_EMPAQUE",
            message="La ruta referenciada no corresponde al PT.",
            entity_type="RUTA",
            entity_id=route_ref,
        )
    else:
        if route.estado == ESTADO_RUTA_BORRADOR:
            add(
                "ROUTE_NOT_APPROVED",
                paso="RUTA_EMPAQUE",
                message="La ruta aun no esta publicada.",
                entity_type="RUTA",
                entity_id=route.id,
                result="PENDING_APPROVAL",
            )
        elif route.estado != ESTADO_RUTA_APROBADA:
            add(
                "ROUTE_NOT_CURRENT",
                paso="RUTA_EMPAQUE",
                message="La ruta referenciada ya no esta vigente.",
                entity_type="RUTA",
                entity_id=route.id,
            )
        try:
            _assert_route_approvable(session, route)
        except ScmServiceError as error:
            add(
                error.code,
                paso="RUTA_EMPAQUE",
                message=error.message,
                entity_type="RUTA",
                entity_id=route.id,
            )

    fabrication_types = {"INYECCION", "SOPLADO"}
    if route is not None and any(
        operation.tipo in fabrication_types
        for operation in route.operaciones
    ):
        if mold is None or not matrix_variants:
            add(
                "FABRICATION_OUTPUTS_NOT_RESOLVED",
                paso="RUTA_EMPAQUE",
                message="La fabricacion no resuelve molde y salidas fisicas.",
                entity_type="RUTA",
                entity_id=route.id,
            )
        for variant, _article in matrix_variants:
            recipe = find_default_recipe(
                session,
                color_produccion_id=variant.color_produccion_id,
                producto_sku=onboarding.producto_terminado_id,
            )
            referenced = formula_by_color.get(
                variant.color_produccion_id
            ) or {}
            referenced_recipe = session.get(
                RecetaColorMaestra,
                _readiness_int_reference(referenced.get("receta_ref")),
            ) if referenced.get("receta_ref") else None
            if recipe is None and (
                referenced_recipe is None
                or referenced_recipe.estado != "APROBADA"
            ):
                add(
                    "FABRICATION_FORMULATION_NOT_APPROVED",
                    paso="COLORES",
                    message="Una salida de fabricacion no tiene formulacion aprobada aplicable.",
                    entity_type="PIEZA_COLOR",
                    entity_id=variant.sku,
                    result=(
                        "PENDING_APPROVAL"
                        if referenced_recipe is not None
                        else "BLOCKED"
                    ),
                )

    packaged_article_ids = {
        operation.articulo_salida_id
        for operation in (route.operaciones if route is not None else [])
    }
    if not packaged_article_ids:
        add(
            "PACKAGING_NOT_APPLICABLE",
            paso="RUTA_EMPAQUE",
            message="No hay salidas de ruta a evaluar para empaque.",
            entity_type="RUTA",
            entity_id=route_ref,
        result="NOT_APPLICABLE",
        action=None,
    )
    packaging_refs = references.get("RUTA_EMPAQUE") or {}
    selected_packaging_by_article = {
        _readiness_int_reference(item.get("articulo_ref")): item
        for item in packaging_refs.get("empaques") or []
        if isinstance(item, dict)
        and _readiness_int_reference(item.get("articulo_ref")) is not None
    }
    for article_id in packaged_article_ids:
        links = session.scalars(select(ScmArticuloPerfil).where(
            ScmArticuloPerfil.articulo_id == article_id,
            ScmArticuloPerfil.activo.is_(True),
            ScmArticuloPerfil.es_predeterminado.is_(True),
        )).all()
        links = [link for link in links if link.perfil and link.perfil.activo]
        if len(links) != 1:
            add(
                "DEFAULT_PACKAGING_PROFILE_MISSING",
                paso="RUTA_EMPAQUE",
                message="La salida requiere un perfil predeterminado activo.",
                entity_type="ARTICULO",
                entity_id=article_id,
            )
            continue
        profile = links[0].perfil
        selected = selected_packaging_by_article.get(article_id)
        if selected is None and article_id == (
            product_article.id if product_article is not None else None
        ):
            legacy_profile = _readiness_int_reference(
                packaging_refs.get("perfil_empacable_ref")
            )
            legacy_rule = _readiness_int_reference(
                packaging_refs.get("regla_empaque_revision_ref")
            )
            if legacy_profile or legacy_rule:
                selected = {
                    "perfil_empacable_ref": legacy_profile,
                    "regla_empaque_revision_ref": legacy_rule,
                }
        if selected is None:
            add(
                "PACKAGING_REFERENCE_NOT_RESOLVED",
                paso="RUTA_EMPAQUE",
                message="La salida no conserva refs de perfil y regla elegidos.",
                entity_type="ARTICULO",
                entity_id=article_id,
            )
            continue
        selected_profile_id = _readiness_int_reference(
            selected.get("perfil_empacable_ref")
        )
        if selected_profile_id != profile.id:
            add(
                "PACKAGING_PROFILE_REFERENCE_MISMATCH",
                paso="RUTA_EMPAQUE",
                message="El perfil elegido no es el predeterminado vigente.",
                entity_type="PERFIL_EMPAQUE",
                entity_id=selected_profile_id,
            )
            continue
        selected_rule_id = _readiness_int_reference(
            selected.get("regla_empaque_revision_ref")
        )
        selected_rule = (
            session.get(ScmReglaEmpaqueRevision, selected_rule_id)
            if selected_rule_id else None
        )
        if (
            selected_rule is None
            or selected_rule.regla.perfil_empacable_id != profile.id
        ):
            add(
                "PACKAGING_RULE_NOT_RESOLVED",
                paso="RUTA_EMPAQUE",
                message="La regla elegida no existe o pertenece a otro perfil.",
                entity_type="REGLA_EMPAQUE",
                entity_id=selected_rule_id,
            )
            continue
        if selected_rule.estado == ESTADO_REGLA_BORRADOR:
            add(
                "PACKAGING_RULE_NOT_APPROVED",
                paso="RUTA_EMPAQUE",
                message="La regla elegida aun no esta aprobada.",
                entity_type="REGLA_EMPAQUE",
                entity_id=selected_rule.id,
                result="PENDING_APPROVAL",
            )
            continue
        if selected_rule.estado != ESTADO_REGLA_APROBADA:
            add(
                "PACKAGING_RULE_NOT_CURRENT",
                paso="RUTA_EMPAQUE",
                message="La regla elegida ya no esta vigente.",
                entity_type="REGLA_EMPAQUE",
                entity_id=selected_rule.id,
            )
            continue
        try:
            _approval_viability(selected_rule)
        except ScmServiceError as error:
            add(
                error.code,
                paso="RUTA_EMPAQUE",
                message=error.message,
                entity_type="REGLA_EMPAQUE",
                entity_id=selected_rule.id,
            )

    revision_confirmations = {}
    if states["REVISION"] == "COMPLETADO":
        try:
            (
                revision_confirmations,
                reviewed_snapshot,
            ) = _validate_revision_payload(
                draft["REVISION"],
                require_complete=True,
            )
            if reviewed_snapshot != revision_snapshot:
                add(
                    "REVISION_CONFIRMATION_STALE",
                    paso="REVISION",
                    message=(
                        "Las revisiones canonicas cambiaron desde la "
                        "confirmacion. Revise y confirme nuevamente."
                    ),
                    entity_id=str(onboarding.id),
                    action="REVIEW_CURRENT_REVISIONS",
                )
        except ScmServiceError as error:
            add(
                error.code,
                paso="REVISION",
                message=error.message,
                entity_id=str(onboarding.id),
            )
    has_pending = any(
        item["result"] == "PENDING_APPROVAL" for item in items
    )
    if (
        has_pending
        and states["REVISION"] == "COMPLETADO"
        and revision_confirmations.get("pendientes_aceptados") is not True
    ):
        add(
            "PENDING_APPROVAL_NOT_ACCEPTED",
            paso="REVISION",
            message=(
                "Confirme que acepta los handoffs pendientes antes de "
                "finalizar la captura."
            ),
            entity_id=str(onboarding.id),
        )
    has_blocker = any(item["result"] == "BLOCKED" for item in items)
    has_pending = any(
        item["result"] == "PENDING_APPROVAL" for item in items
    )
    status = (
        "BLOCKED"
        if has_blocker
        else "PENDING_APPROVAL" if has_pending else "READY"
    )
    blockers = [
        {
            "codigo": item["code"],
            "paso": item["paso"],
            "mensaje": item["message"],
            "entity": copy.deepcopy(item["entity"]),
            "result": item["result"],
        }
        for item in items
        if item["result"] == "BLOCKED"
    ]
    warnings = [
        copy.deepcopy(item)
        for item in items
        if item["result"] == "PENDING_APPROVAL"
    ]

    handoffs = []
    for step, direct_capability, approval_capability in (
        DIRECT_PUBLICATION_HANDOFFS
    ):
        if not actor.tiene_capacidad(direct_capability):
            handoffs.append({
                "paso": step,
                "tipo": "APROBACION_REQUERIDA",
                "capacidad_publicacion_directa": direct_capability,
                "capacidad_aprobacion": approval_capability,
            })

    readiness = {
        "status": status,
        "checked_at": _isoformat(utc_now()),
        "items": items,
        "lista_para_finalizar": status != "BLOCKED",
        "bloqueos": blockers,
        "advertencias": warnings,
        "handoffs": handoffs,
        "evaluada_en_version": onboarding.version,
        "revision_snapshot": revision_snapshot,
    }
    return readiness, blockers_by_step


def _refresh_readiness(onboarding, actor):
    readiness, blockers_by_step = _structural_readiness(onboarding, actor)
    onboarding.readiness_json = readiness
    onboarding.bloqueos_paso_json = blockers_by_step
    return readiness


def _load_actor(session, actor_id):
    return load_actor(
        session,
        actor_id,
        capability="ARTICULO_ADMINISTRAR",
    )


def _can_audit_all(actor):
    return actor.tiene_capacidad("AUTORIZACION_SCM_ADMINISTRAR")


def _load_session(
    session,
    session_id,
    *,
    actor,
    for_update=False,
    allow_audit=False,
):
    statement = select(ScmAltaProductoSesion).where(
        ScmAltaProductoSesion.id == session_id
    )
    if not (allow_audit and _can_audit_all(actor)):
        statement = statement.where(
            ScmAltaProductoSesion.creada_por_id == actor.id
        )
    if for_update:
        statement = statement.with_for_update()
    onboarding = session.scalar(statement)
    if onboarding is None:
        raise ScmServiceError(
            "ONBOARDING_SESSION_NOT_FOUND",
            "La sesion de alta guiada no existe.",
            status_code=404,
        )
    return onboarding


def _ensure_version(onboarding, received):
    parsed = expected_version(received)
    if onboarding.version != parsed:
        raise ScmServiceError(
            "VERSION_CONFLICT",
            "La sesion fue modificada en otra pestaña o por otro usuario.",
            status_code=409,
            details={
                "expected": onboarding.version,
                "received": parsed,
                "current_session": serialize_onboarding(onboarding),
            },
        )


def _ensure_mutable(onboarding):
    if onboarding.estado in IMMUTABLE_SESSION_STATES:
        raise ScmServiceError(
            "SESSION_IMMUTABLE",
            "Una sesion finalizada o abandonada ya no puede modificarse.",
            status_code=409,
            details={"estado": onboarding.estado},
        )


def create_onboarding_session(
    session, *, actor_id, operation_id=None, data
):
    try:
        actor = _load_actor(session, actor_id)
        reject_unknown_fields(data, allowed=CREATE_FIELDS)
        operation, replay = _reserve_operation(
            session,
            operation_id,
            "/api/scm/v1/altas-producto:POST",
            actor,
            data,
        )
        if replay is not None:
            return replay

        initial_data = _json_object(data.get("data"), field="data")
        product_id = _optional_text(
            data.get("producto_terminado_id"),
            field="producto_terminado_id",
            max_length=50,
        )
        data_product_supplied, data_product_id = _product_reference(
            initial_data
        )
        if (
            product_id is not None
            and data_product_supplied
            and product_id != data_product_id
        ):
            raise ScmServiceError(
                "PRODUCT_REFERENCE_CONFLICT",
                "producto_terminado_id no coincide con data.",
                status_code=422,
                details={
                    "producto_terminado_id": product_id,
                    "data_producto_terminado_id": data_product_id,
                },
            )
        product_id = _require_existing_product(
            session, product_id or data_product_id
        )
        derived_title = (
            (initial_data.get("producto") or {}).get("producto")
            if isinstance(initial_data.get("producto"), dict)
            else None
        )
        title = _optional_text(
            data.get("titulo"), field="titulo", max_length=200
        ) or _optional_text(
            derived_title, field="data.producto.producto", max_length=200
        ) or "Nuevo producto terminado"

        draft = {code: {} for code in ONBOARDING_STEPS}
        states = {code: "PENDIENTE" for code in ONBOARDING_STEPS}
        sources = {}
        references = {}
        if initial_data:
            draft["IDENTIDAD"] = initial_data
            states["IDENTIDAD"] = "EN_PROGRESO"
            extracted_sources = _extract_sources(initial_data)
            extracted_references = _extract_references(initial_data)
            if extracted_sources:
                sources["IDENTIDAD"] = extracted_sources
            if extracted_references:
                references["IDENTIDAD"] = extracted_references
        if product_id is not None:
            references.setdefault("IDENTIDAD", {})[
                "producto_ref"
            ] = product_id

        onboarding = ScmAltaProductoSesion(
            id=uuid.uuid4(),
            titulo=title,
            producto_terminado_id=product_id,
            estado="BORRADOR",
            paso_actual="IDENTIDAD",
            borrador_json=draft,
            estados_paso_json=states,
            bloqueos_paso_json={},
            fuentes_json=sources,
            referencias_json=references,
            readiness_json={},
            invalidated_steps_json=[],
            creada_por_id=actor.id,
            actualizada_por_id=actor.id,
        )
        session.add(onboarding)
        session.flush()
        _refresh_readiness(onboarding, actor)
        session.flush()
        response = serialize_onboarding(onboarding)
        session.add(_event(
            onboarding,
            actor,
            "ALTA_PRODUCTO_INICIADA",
            operation,
            after=response,
        ))
        _complete_operation(operation, response, 201)
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ONBOARDING_SESSION_CONFLICT",
            "No se pudo crear la sesion por un conflicto de integridad.",
            status_code=409,
        ) from error


def get_onboarding_session(session, *, actor_id, session_id):
    actor = _load_actor(session, actor_id)
    return serialize_onboarding(_load_session(
        session,
        session_id,
        actor=actor,
        allow_audit=True,
    ))


def list_onboarding_sessions(session, *, actor_id, state=None):
    actor = _load_actor(session, actor_id)
    statement = select(ScmAltaProductoSesion)
    if not _can_audit_all(actor):
        statement = statement.where(
            ScmAltaProductoSesion.creada_por_id == actor.id
        )
    if state is not None:
        normalized = str(state).strip().upper()
        if normalized not in SESSION_STATES:
            raise ScmServiceError(
                "INVALID_SESSION_STATE",
                "El filtro estado no es valido.",
                status_code=400,
                details={"allowed": list(SESSION_STATES)},
            )
        statement = statement.where(
            ScmAltaProductoSesion.estado == normalized
        )
    statement = statement.order_by(
        ScmAltaProductoSesion.updated_at.desc(),
        ScmAltaProductoSesion.id.desc(),
    )
    return [
        serialize_onboarding(item)
        for item in session.scalars(statement).all()
    ]


def update_onboarding_step(
    session,
    *,
    actor_id,
    session_id,
    step_code,
    operation_id=None,
    data,
):
    try:
        actor = _load_actor(session, actor_id)
        code = _step_code(step_code)
        reject_unknown_fields(data, allowed=STEP_UPDATE_FIELDS)
        if "data" not in data:
            raise ScmServiceError(
                "STEP_DATA_REQUIRED",
                "El comando debe incluir data para el paso.",
                status_code=400,
            )
        step_payload = _json_object(
            data.get("data"), field="data", required=True
        )
        operation, replay = _reserve_operation(
            session,
            operation_id,
            f"/api/scm/v1/altas-producto/{session_id}/pasos/{code}:PUT",
            actor,
            data,
        )
        if replay is not None:
            return replay
        onboarding = _load_session(
            session,
            session_id,
            actor=actor,
            for_update=True,
        )
        _ensure_version(onboarding, data.get("expected_version"))
        _ensure_mutable(onboarding)
        before = serialize_onboarding(onboarding)
        draft = _step_data(onboarding)
        states = _step_states(onboarding)
        old_data = draft[code]
        old_state = states[code]
        data_changed = old_data != step_payload
        if "estado_paso" in data:
            new_state = _step_state(data["estado_paso"])
        elif data_changed:
            new_state = "EN_PROGRESO"
        else:
            new_state = old_state
        if code == "REVISION":
            _confirmations, reviewed_snapshot = _validate_revision_payload(
                step_payload,
                require_complete=new_state == "COMPLETADO",
            )
            if (
                new_state == "COMPLETADO"
                and reviewed_snapshot
                != _canonical_revision_snapshot(onboarding)
            ):
                raise ScmServiceError(
                    "REVISION_CONFIRMATION_STALE",
                    (
                        "Las revisiones canonicas cambiaron. "
                        "Revise y confirme nuevamente."
                    ),
                    status_code=409,
                    details={
                        "current_snapshot": (
                            _canonical_revision_snapshot(onboarding)
                        ),
                        "current_session": serialize_onboarding(onboarding),
                    },
                )
        state_changed = old_state != new_state

        if not data_changed and not state_changed:
            response = serialize_onboarding(onboarding)
            _complete_operation(operation, response, 200)
            session.commit()
            return response

        draft[code] = step_payload
        states[code] = new_state
        invalidated = []
        should_invalidate = data_changed or (
            state_changed and new_state != "COMPLETADO"
        )
        if should_invalidate:
            start = ONBOARDING_STEPS.index(code) + 1
            for descendant in ONBOARDING_STEPS[start:]:
                if states[descendant] != "PENDIENTE" or draft[descendant]:
                    states[descendant] = "INVALIDADO"
                    invalidated.append(descendant)

        sources = copy.deepcopy(onboarding.fuentes_json or {})
        references = copy.deepcopy(onboarding.referencias_json or {})
        extracted_sources = _extract_sources(step_payload)
        extracted_references = _extract_references(step_payload)
        if extracted_sources:
            sources[code] = extracted_sources
        else:
            sources.pop(code, None)
        if extracted_references:
            references[code] = extracted_references
        else:
            references.pop(code, None)

        if code == "IDENTIDAD":
            _refresh_placeholder_title(onboarding, step_payload)
            product_supplied, product_id = _product_reference(step_payload)
            if product_supplied:
                onboarding.producto_terminado_id = _require_existing_product(
                    session, product_id
                )

        onboarding.borrador_json = draft
        onboarding.estados_paso_json = states
        onboarding.fuentes_json = sources
        onboarding.referencias_json = references
        onboarding.invalidated_steps_json = invalidated
        onboarding.paso_actual = code
        onboarding.estado = "BORRADOR"
        onboarding.actualizada_por_id = actor.id
        onboarding.updated_at = utc_now()
        onboarding.version += 1
        _refresh_readiness(onboarding, actor)
        session.flush()
        response = serialize_onboarding(onboarding)
        session.add(_event(
            onboarding,
            actor,
            "PASO_ALTA_PRODUCTO_GUARDADO",
            operation,
            before=before,
            after=response,
        ))
        _complete_operation(operation, response, 200)
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ONBOARDING_SESSION_CONFLICT",
            "No se pudo guardar el paso por un conflicto de integridad.",
            status_code=409,
        ) from error


def _structure_piece_colors(session, revision, *, visited=None):
    """Return the canonical PiezaColor rows consumed by an approved BOM tree."""
    visited = set() if visited is None else visited
    if revision.id in visited:
        raise ScmServiceError(
            "STRUCTURE_COLOR_RECOVERY_CYCLE",
            "La estructura contiene un ciclo y no puede restaurar colores.",
            status_code=409,
        )
    visited.add(revision.id)
    variants = []
    try:
        for component in revision.componentes:
            article = component.articulo_componente
            if article.clase == CLASE_PIEZA_COLOR:
                variant = article.pieza_color.pieza_color if article.pieza_color else None
                if (
                    variant is None
                    or variant.pieza_id is None
                    or variant.color_produccion_id is None
                ):
                    raise ScmServiceError(
                        "STRUCTURE_PIECE_COLOR_INCOMPLETE",
                        "La BOM contiene una PiezaColor sin pieza o color canonico.",
                        status_code=409,
                        details={"articulo_id": article.id},
                    )
                variants.append(variant)
                continue
            if article.clase == CLASE_SUBENSAMBLE_WIP:
                nested = session.scalar(select(ScmEstructuraRevision).where(
                    ScmEstructuraRevision.articulo_resultado_id == article.id,
                    ScmEstructuraRevision.estado == ESTADO_ESTRUCTURA_APROBADA,
                ))
                if nested is None:
                    raise ScmServiceError(
                        "WIP_STRUCTURE_NOT_APPROVED",
                        "Un WIP de la BOM no tiene estructura aprobada.",
                        status_code=409,
                        details={"articulo_id": article.id},
                    )
                variants.extend(_structure_piece_colors(
                    session, nested, visited=visited,
                ))
                continue
            raise ScmServiceError(
                "STRUCTURE_COLOR_RECOVERY_UNSUPPORTED_COMPONENT",
                "La BOM contiene un componente que no define PiezaColor.",
                status_code=409,
                details={"articulo_id": article.id, "clase": article.clase},
            )
    finally:
        visited.remove(revision.id)
    return variants


def restore_onboarding_colors_from_structure(
    session,
    *,
    actor_id,
    session_id,
    operation_id=None,
    data,
):
    """Rebuild an invalidated color draft from an already-approved BOM.

    This is an explicit retroactive recovery command. It never creates or
    mutates color masters; the operator must review and apply the rebuilt
    matrix using the normal COLORES command.
    """
    try:
        actor = _load_actor(session, actor_id)
        reject_unknown_fields(data, allowed=VERSION_COMMAND_FIELDS)
        endpoint = (
            f"/api/scm/v1/altas-producto/{session_id}/pasos/COLORES/"
            "restaurar-desde-estructura:POST"
        )
        operation, replay = _reserve_operation(
            session, operation_id, endpoint, actor, data,
        )
        if replay is not None:
            return replay
        onboarding = _load_session(
            session, session_id, actor=actor, for_update=True,
        )
        _ensure_version(onboarding, data.get("expected_version"))
        _ensure_mutable(onboarding)
        _require_actor_capability(actor, "ARTICULO_ADMINISTRAR")
        states = _step_states(onboarding)
        if states["COMPONENTES"] != "COMPLETADO":
            raise ScmServiceError(
                "COMPONENTS_NOT_COMPLETED",
                "Complete COMPONENTES antes de restaurar colores.",
                status_code=409,
            )
        if states["COLORES"] != "INVALIDADO":
            raise ScmServiceError(
                "COLOR_RECOVERY_NOT_REQUIRED",
                "La restauracion solo aplica a una fase COLORES invalidada.",
                status_code=409,
            )
        structure_ref = (
            (onboarding.referencias_json or {})
            .get("ESTRUCTURA", {})
            .get("estructura_revision_ref")
        )
        structure = session.get(ScmEstructuraRevision, structure_ref)
        product_article = _product_article_for_onboarding(session, onboarding)
        if (
            structure is None
            or structure.estado != ESTADO_ESTRUCTURA_APROBADA
            or structure.articulo_resultado_id != product_article.id
        ):
            raise ScmServiceError(
                "APPROVED_STRUCTURE_REQUIRED",
                "La sesion no conserva una BOM aprobada compatible.",
                status_code=409,
            )

        component_refs = (
            (onboarding.referencias_json or {}).get("COMPONENTES") or {}
        )
        component_pieces = [
            item for item in (component_refs.get("piezas") or [])
            if isinstance(item, dict) and item.get("pieza_ref")
        ]
        piece_ids = {int(item["pieza_ref"]) for item in component_pieces}
        variants = _structure_piece_colors(session, structure)
        variants_by_pair = {
            (int(item.pieza_id), int(item.color_produccion_id)): item
            for item in variants
            if int(item.pieza_id) in piece_ids
        }
        recovered_piece_ids = {piece_id for piece_id, _ in variants_by_pair}
        if recovered_piece_ids != piece_ids:
            raise ScmServiceError(
                "BOM_COMPONENT_COLOR_MISMATCH",
                "La BOM no contiene colores para todas las piezas del paso Componentes.",
                status_code=409,
                details={
                    "missing_piece_refs": sorted(piece_ids - recovered_piece_ids),
                },
            )

        colors_by_id = {
            color_id: session.get(ColorProduccion, color_id)
            for _piece_id, color_id in variants_by_pair
        }
        old_formulas = {
            int(item["color_ref"]): item
            for item in (
                (onboarding.referencias_json or {})
                .get("COLORES", {})
                .get("formulaciones") or []
            )
            if isinstance(item, dict) and item.get("color_ref")
        }
        old_matrix = {
            (int(item["pieza_ref"]), int(item["color_ref"])): item
            for item in (
                (onboarding.referencias_json or {})
                .get("COLORES", {})
                .get("matriz") or []
            )
            if (
                isinstance(item, dict)
                and item.get("pieza_ref")
                and item.get("color_ref")
            )
        }
        colors = []
        formulas = []
        for color_id in sorted(colors_by_id):
            color = colors_by_id[color_id]
            colors.append({
                "client_id": f"bom-color-{color_id}",
                "modo": "REUTILIZAR",
                "color_ref": color_id,
                "nombre": color.nombre,
                "familia_color_id": color.familia_color_id,
                "hex": color.hex_referencia or "",
            })
            recipe = None
            old_formula = old_formulas.get(color_id) or {}
            old_recipe_id = old_formula.get("receta_ref")
            if old_recipe_id:
                candidate = session.get(RecetaColorMaestra, old_recipe_id)
                if (
                    candidate is not None
                    and candidate.color_produccion_id == color_id
                    and candidate.estado != "INACTIVA"
                ):
                    recipe = candidate
            if recipe is None:
                recipe = find_default_recipe(
                    session,
                    color_produccion_id=color_id,
                    producto_sku=onboarding.producto_terminado_id,
                )
            formulas.append({
                "color_ref": color_id,
                "color_client_id": f"bom-color-{color_id}",
                **({
                    "tipo": "EXISTENTE",
                    "receta_ref": recipe.id,
                } if recipe is not None else {
                    "tipo": "PENDIENTE",
                    "receta_ref": None,
                    "motivo_pendiente": (
                        "La BOM vigente usa este color, pero no se encontro "
                        "una receta aprobada predeterminada."
                    ),
                }),
            })

        matrix = []
        for piece_id in sorted(piece_ids):
            for color_id in sorted(colors_by_id):
                variant = variants_by_pair.get((piece_id, color_id))
                matrix.append({
                    "pieza_ref": piece_id,
                    "color_ref": color_id,
                    "seleccionada": variant is not None,
                    **({"pieza_color_ref": variant.sku} if variant else {}),
                    **({
                        "receta_ref": old_matrix[
                            (piece_id, color_id)
                        ]["receta_ref"],
                    } if (
                        variant
                        and old_matrix.get((piece_id, color_id), {}).get(
                            "receta_ref"
                        )
                    ) else {}),
                })

        before = serialize_onboarding(onboarding)
        draft = _step_data(onboarding)
        draft["COLORES"] = {
            "colores": colors,
            "matriz": matrix,
            "formulaciones": formulas,
            "recuperada_desde_estructura_ref": structure.id,
        }
        states["COLORES"] = "EN_PROGRESO"
        onboarding.borrador_json = draft
        onboarding.estados_paso_json = states
        onboarding.paso_actual = "COLORES"
        onboarding.estado = "BORRADOR"
        onboarding.actualizada_por_id = actor.id
        onboarding.updated_at = utc_now()
        onboarding.version += 1
        _refresh_readiness(onboarding, actor)
        session.flush()
        response = serialize_onboarding(onboarding)
        response["color_recovery"] = {
            "estructura_revision_ref": structure.id,
            "colores": len(colors),
            "piezas_color": len(variants_by_pair),
        }
        session.add(_event(
            onboarding,
            actor,
            "COLORES_RESTAURADOS_DESDE_ESTRUCTURA",
            operation,
            before=before,
            after=response,
        ))
        _complete_operation(operation, response, 200)
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "COLOR_RECOVERY_CONFLICT",
            "No se pudo restaurar la matriz por un conflicto de integridad.",
            status_code=409,
        ) from error


def _application_response(onboarding, result, *, replayed=False):
    response = serialize_onboarding(onboarding)
    response["application_results"] = copy.deepcopy(result)
    if replayed:
        response["application_results"]["status"] = "REPLAYED"
    return response


def _items_by_client_id(payload, field):
    items = payload.get(field)
    if not isinstance(items, list):
        return {}
    return {
        item.get("client_id"): item
        for item in items
        if isinstance(item, dict) and item.get("client_id")
    }


def _matrix_identity(item):
    if not isinstance(item, dict):
        return None
    piece = item.get("pieza_client_id") or item.get("pieza_ref")
    color = item.get("color_client_id") or item.get("color_ref")
    if piece in (None, "") or color in (None, ""):
        return None
    return str(piece), str(color)


def _formula_identity(item):
    if not isinstance(item, dict):
        return None
    value = item.get("color_client_id") or item.get("color_ref")
    return str(value) if value not in (None, "") else None


def _reference_matches(value, expected):
    return value not in (None, "") and str(value) == str(expected)


def _find_matrix_row(payload, resolved):
    for item in payload.get("matriz") or []:
        if not isinstance(item, dict) or not bool(item.get("seleccionada")):
            continue
        piece_matches = (
            _reference_matches(
                item.get("pieza_ref"), resolved.get("pieza_ref")
            )
            or (
                resolved.get("pieza_client_id") not in (None, "")
                and item.get("pieza_client_id")
                == resolved.get("pieza_client_id")
            )
        )
        color_matches = (
            _reference_matches(
                item.get("color_ref"), resolved.get("color_ref")
            )
            or (
                resolved.get("color_client_id") not in (None, "")
                and item.get("color_client_id")
                == resolved.get("color_client_id")
            )
        )
        if piece_matches and color_matches:
            return item
    return None


def _find_formula(payload, resolved):
    for item in payload.get("formulaciones") or []:
        if not isinstance(item, dict):
            continue
        if _reference_matches(
            item.get("color_ref"), resolved.get("color_ref")
        ) or (
            resolved.get("color_client_id") not in (None, "")
            and item.get("color_client_id")
            == resolved.get("color_client_id")
        ):
            return item
    return None


def _formula_body(item):
    if item is None:
        return None
    return {
        key: copy.deepcopy(value)
        for key, value in item.items()
        if key not in {"color_ref", "color_client_id"}
    }


def _assert_checkpointed_retry_compatible(
    code,
    *,
    previous_payload,
    new_payload,
    result,
):
    """Allow correcting only units that were not checkpointed yet.

    A PARTIAL application already owns canonical rows. The stable
    ``application_key`` is a resume token, not permission to replace those
    rows with a second set of masters.
    """

    resolved = result.get("resolved_references") or {}
    changed = []
    if code == "COMPONENTES":
        if previous_payload.get("moldes") is not None or new_payload.get("moldes") is not None:
            before_groups = {
                item.get("client_id"): item
                for item in (previous_payload.get("moldes") or [])
                if isinstance(item, dict)
            }
            after_groups = {
                item.get("client_id"): item
                for item in (new_payload.get("moldes") or [])
                if isinstance(item, dict)
            }
            for resolved_group in resolved.get("moldes") or []:
                group_id = resolved_group.get("client_id")
                before_group = before_groups.get(group_id)
                after_group = after_groups.get(group_id)
                before_mold = (before_group or {}).get("molde") or {}
                after_mold = (after_group or {}).get("molde") or {}
                mold_reconciled = (
                    str(after_mold.get("modo") or "").upper() == "REUTILIZAR"
                    and _reference_matches(
                        after_mold.get("ref"), resolved_group.get("molde_ref")
                    )
                )
                if before_mold != after_mold and not mold_reconciled:
                    changed.append(f"moldes:{group_id}")
                before_pieces = _items_by_client_id(before_group or {}, "piezas")
                after_pieces = _items_by_client_id(after_group or {}, "piezas")
                for item in resolved_group.get("piezas") or []:
                    client_id = item.get("client_id")
                    before_item = before_pieces.get(client_id)
                    after_item = after_pieces.get(client_id)
                    reconciled = bool(
                        after_item
                        and str(after_item.get("modo") or "").upper() == "REUTILIZAR"
                        and _reference_matches(
                            after_item.get("ref"), item.get("pieza_ref")
                        )
                        and before_item is not None
                        and before_item.get("cavidades") == after_item.get("cavidades")
                        and before_item.get("peso_unitario_gr") == after_item.get("peso_unitario_gr")
                    )
                    if before_item != after_item and not reconciled:
                        changed.append(f"piezas:{client_id}")
        else:
            before_mold = previous_payload.get("molde") or {}
            after_mold = new_payload.get("molde") or {}
            mold_reconciled = (
                str(after_mold.get("modo") or "").upper() == "REUTILIZAR"
                and _reference_matches(
                    after_mold.get("ref"), resolved.get("molde_ref")
                )
            )
            if before_mold != after_mold and not mold_reconciled:
                changed.append("molde")
            before = _items_by_client_id(previous_payload, "piezas")
            after = _items_by_client_id(new_payload, "piezas")
            for item in resolved.get("piezas") or []:
                client_id = item.get("client_id") if isinstance(item, dict) else None
                before_item = before.get(client_id)
                after_item = after.get(client_id)
                reconciled = bool(
                    after_item
                    and str(after_item.get("modo") or "").upper()
                    == "REUTILIZAR"
                    and _reference_matches(
                        after_item.get("ref"), item.get("pieza_ref")
                    )
                    and before_item is not None
                    and before_item.get("cavidades")
                    == after_item.get("cavidades")
                    and before_item.get("peso_unitario_gr")
                    == after_item.get("peso_unitario_gr")
                )
                if before_item != after_item and not reconciled:
                    changed.append(f"piezas:{client_id}")
    elif code == "COLORES":
        before_colors = _items_by_client_id(previous_payload, "colores")
        after_colors = _items_by_client_id(new_payload, "colores")
        for item in resolved.get("colores") or []:
            client_id = item.get("client_id") if isinstance(item, dict) else None
            before_item = before_colors.get(client_id)
            after_item = after_colors.get(client_id)
            reconciled = bool(
                before_item is not None
                and after_item
                and str(after_item.get("modo") or "").upper()
                == "REUTILIZAR"
                and _reference_matches(
                    after_item.get("color_ref"), item.get("color_ref")
                )
            )
            if before_item != after_item and not reconciled:
                changed.append(f"colores:{client_id}")
        for item in resolved.get("matriz") or []:
            if (
                _find_matrix_row(previous_payload, item) is None
                or _find_matrix_row(new_payload, item) is None
            ):
                changed.append(
                    f"matriz:{item.get('pieza_ref')}/{item.get('color_ref')}"
                )
        for item in resolved.get("formulaciones") or []:
            before_formula = _find_formula(previous_payload, item)
            after_formula = _find_formula(new_payload, item)
            reconciled = bool(
                before_formula is not None
                and after_formula is not None
                and item.get("receta_ref") is not None
                and str(after_formula.get("tipo") or "").upper()
                == "EXISTENTE"
                and _reference_matches(
                    after_formula.get("receta_ref"),
                    item.get("receta_ref"),
                )
            )
            if (
                _formula_body(before_formula)
                != _formula_body(after_formula)
                and not reconciled
            ):
                changed.append(f"formulaciones:{item.get('color_ref')}")

    if changed:
        raise ScmServiceError(
            "CHECKPOINTED_APPLICATION_DATA_CHANGED",
            "No se pueden reemplazar unidades ya materializadas por la sesion.",
            status_code=409,
            details={
                "paso": code,
                "changed_units": changed,
                "action": "Conserve las unidades aplicadas y corrija solo la pendiente.",
            },
        )


def _existing_step_application(step_journal, *, key, request_hash):
    """Return the selected journal entry or a safe replay decision."""

    previous = step_journal.get(key)
    if previous is not None:
        return previous
    applied = [
        item for item in step_journal.values()
        if item.get("status") == "APPLIED"
        and not item.get("superseded_by")
    ]
    if applied:
        exact = next(
            (
                item for item in applied
                if item.get("request_sha256") == request_hash
            ),
            None,
        )
        if exact is not None:
            return exact
        raise ScmServiceError(
            "ONBOARDING_STEP_ALREADY_APPLIED",
            "El paso ya materializo maestros y no puede crear sustitutos silenciosos.",
            status_code=422,
            details={
                "action": "Reutilice las referencias existentes o edite el maestro canonico.",
            },
        )
    partial = [
        journal_key
        for journal_key, item in step_journal.items()
        if item.get("status") == "PARTIAL"
    ]
    if partial:
        raise ScmServiceError(
            "PARTIAL_APPLICATION_KEY_REQUIRED",
            "La aplicacion parcial debe reanudarse con su application_key original.",
            status_code=409,
            details={"application_key": partial[0]},
        )
    return None


def _step_has_applied_journal(onboarding, code):
    journal = (onboarding.application_journal_json or {}).get(code) or {}
    return any(
        item.get("status") == "APPLIED"
        for item in journal.values()
        if isinstance(item, dict)
    )


def _require_application_prerequisite(onboarding, *, code):
    required = None
    if code == "COMPONENTES":
        if (
            not onboarding.producto_terminado_id
            or not _step_has_applied_journal(onboarding, "IDENTIDAD")
        ):
            required = "IDENTIDAD"
    elif code == "COLORES":
        component_refs = (onboarding.referencias_json or {}).get(
            "COMPONENTES"
        ) or {}
        if (
            not _step_has_applied_journal(onboarding, "COMPONENTES")
            or not (
                component_refs.get("molde_ref")
                or component_refs.get("moldes")
            )
            or not component_refs.get("piezas")
        ):
            required = "COMPONENTES"
    elif code == "ESTRUCTURA":
        color_refs = (onboarding.referencias_json or {}).get("COLORES") or {}
        if (
            not _step_has_applied_journal(onboarding, "COLORES")
            or not color_refs.get("matriz")
        ):
            required = "COLORES"
    elif code == "RUTA_EMPAQUE":
        structure_refs = (onboarding.referencias_json or {}).get(
            "ESTRUCTURA"
        ) or {}
        if (
            not _step_has_applied_journal(onboarding, "ESTRUCTURA")
            or not (
                structure_refs.get("estructura_revision_ref")
                or structure_refs.get("estructura_ref")
            )
        ):
            required = "ESTRUCTURA"
    if required is not None:
        raise ScmServiceError(
            "ONBOARDING_PREREQUISITE_REQUIRED",
            f"Debe materializar {required} antes de aplicar {code}.",
            status_code=422,
            details={
                "required_step": required,
                "current_session": serialize_onboarding(onboarding),
            },
        )


def _positive_number(value, *, field, integer=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_APPLICATION_VALUE",
            f"{field} debe ser un numero positivo.",
            status_code=422,
            details={"field": field},
        ) from error
    if not math.isfinite(parsed) or parsed <= 0 or (
        integer and not parsed.is_integer()
    ):
        raise ScmServiceError(
            "INVALID_APPLICATION_VALUE",
            f"{field} debe ser un numero positivo.",
            status_code=422,
            details={"field": field},
        )
    return int(parsed) if integer else parsed


def _mode(value, *, field, allowed):
    normalized = required_text(value, field=field, max_length=20).upper()
    if normalized not in allowed:
        raise ScmServiceError(
            "INVALID_APPLICATION_MODE",
            f"{field} no es valido.",
            status_code=422,
            details={"field": field, "allowed": sorted(allowed)},
        )
    return normalized


def _client_id(value, *, field):
    return required_text(value, field=field, max_length=100)


def _identity_product_data(session, step_payload):
    mode = required_text(
        step_payload.get("modo"),
        field="data.modo",
        max_length=20,
    ).upper()
    if mode not in {"NUEVO", "REUTILIZAR", "COPIAR"}:
        raise ScmServiceError(
            "INVALID_IDENTITY_MODE",
            "data.modo debe ser NUEVO, REUTILIZAR o COPIAR.",
            status_code=422,
        )
    if mode == "REUTILIZAR":
        supplied, product_id = _product_reference(step_payload)
        if not supplied:
            raise ScmServiceError(
                "PRODUCT_REFERENCE_REQUIRED",
                "REUTILIZAR requiere producto_ref.",
                status_code=422,
            )
        product_id = _require_existing_product(session, product_id)
        return mode, session.get(ProductoTerminado, product_id), False

    raw_product = step_payload.get("producto")
    if not isinstance(raw_product, dict):
        raise ScmServiceError(
            "PRODUCT_DATA_REQUIRED",
            "NUEVO y COPIAR requieren data.producto.",
            status_code=422,
        )
    product_data = copy.deepcopy(raw_product)
    if mode == "COPIAR":
        source_id = required_text(
            step_payload.get("producto_fuente_ref"),
            field="data.producto_fuente_ref",
            max_length=50,
        )
        source = session.get(ProductoTerminado, source_id)
        if source is None:
            raise ScmServiceError(
                "SOURCE_PRODUCT_NOT_FOUND",
                "El ProductoTerminado fuente no existe.",
                status_code=404,
                details={"producto_fuente_ref": source_id},
            )
        inherited = {
            "producto": source.producto,
            "linea_id": source.linea_id,
            "familia_id": source.familia_id,
            "peso_g": source.peso_g,
            "marca": source.marca,
            "doc_x_paq": source.doc_x_paq,
            "doc_x_bulto": source.doc_x_bulto,
            "codigo_barra": None,
            "um": source.um,
        }
        inherited.update(product_data)
        product_data = inherited
    try:
        product = create_finished_product(session, product_data)
    except CatalogProductError as error:
        raise ScmServiceError(
            error.code,
            error.message,
            status_code=error.status,
            details=error.details,
        ) from error
    return mode, product, True


def _apply_identity(session, onboarding, step_payload):
    mode, product, created = _identity_product_data(session, step_payload)
    result = {
        "created": [],
        "reused": [],
        "pending": [],
        "resolved_references": {
            "producto_terminado_id": product.cod_sku_pt,
        },
    }
    target = {
        "type": "PRODUCTO_TERMINADO",
        "id": product.cod_sku_pt,
    }
    result["created" if created else "reused"].append(target)
    return result


def _apply_components(
    session,
    onboarding,
    step_payload,
    *,
    result,
    checkpoint,
):
    raw_groups = step_payload.get("moldes")
    legacy = raw_groups is None
    if legacy:
        raw_groups = [{
            "client_id": "molde-inicial",
            "molde": step_payload.get("molde"),
            "piezas": step_payload.get("piezas"),
        }]
    elif step_payload.get("molde") is not None or step_payload.get("piezas") is not None:
        raise ScmServiceError(
            "AMBIGUOUS_COMPONENT_GROUPS",
            "Use data.moldes o el formato historico molde/piezas, no ambos.",
            status_code=422,
        )
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ScmServiceError(
            "MOLD_GROUPS_REQUIRED",
            "data.moldes debe contener al menos un grupo Molde-Piezas.",
            status_code=422,
        )

    groups = []
    all_piece_client_ids = []
    for group_index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            raise ScmServiceError(
                "INVALID_MOLD_GROUP", "Cada grupo de molde debe ser un objeto.",
                status_code=422,
            )
        group_client_id = _client_id(
            raw_group.get("client_id"),
            field=f"data.moldes[{group_index}].client_id",
        )
        mold_data = raw_group.get("molde")
        pieces_data = raw_group.get("piezas")
        if not isinstance(mold_data, dict):
            raise ScmServiceError(
                "MOLD_DATA_REQUIRED",
                f"data.moldes[{group_index}].molde debe ser un objeto.",
                status_code=422,
            )
        if not isinstance(pieces_data, list) or not pieces_data:
            raise ScmServiceError(
                "PIECES_REQUIRED",
                f"data.moldes[{group_index}].piezas requiere al menos una pieza.",
                status_code=422,
            )
        piece_client_ids = []
        for piece_index, item in enumerate(pieces_data):
            if not isinstance(item, dict):
                raise ScmServiceError(
                    "INVALID_PIECE_DATA", "Cada pieza debe ser un objeto.",
                    status_code=422,
                )
            piece_client_ids.append(_client_id(
                item.get("client_id"),
                field=(
                    f"data.moldes[{group_index}].piezas[{piece_index}]"
                    ".client_id"
                ),
            ))
        all_piece_client_ids.extend(piece_client_ids)
        groups.append((group_client_id, mold_data, pieces_data, piece_client_ids))
    group_ids = [item[0] for item in groups]
    if len(group_ids) != len(set(group_ids)) or len(all_piece_client_ids) != len(set(all_piece_client_ids)):
        raise ScmServiceError(
            "DUPLICATE_CLIENT_ID",
            "Los client_id de moldes y piezas no pueden repetirse.",
            status_code=422,
        )

    resolved = result.setdefault("resolved_references", {})
    resolved_pieces = resolved.setdefault("piezas", [])
    resolved_groups = resolved.setdefault("moldes", [])
    if not resolved_groups and resolved.get("molde_ref"):
        resolved_groups.append({
            "client_id": group_ids[0],
            "molde_ref": resolved["molde_ref"],
            "piezas": list(resolved_pieces),
        })
    groups_by_client = {
        item.get("client_id"): item for item in resolved_groups
        if isinstance(item, dict)
    }
    used_molds = set()

    for group_index, (group_client_id, mold_data, pieces_data, client_ids) in enumerate(groups):
        resolved_group = groups_by_client.get(group_client_id)
        if resolved_group is None:
            resolved_group = {
                "client_id": group_client_id,
                "molde_ref": None,
                "piezas": [],
            }
            resolved_groups.append(resolved_group)
            groups_by_client[group_client_id] = resolved_group
        mold_ref = resolved_group.get("molde_ref")
        mold = session.get(Molde, mold_ref) if mold_ref else None
        if mold is None:
            mold_mode = _mode(
                mold_data.get("modo"),
                field=f"data.moldes[{group_index}].molde.modo",
                allowed={"NUEVO", "REUTILIZAR"},
            )
            if mold_mode == "REUTILIZAR":
                mold_ref = required_text(
                    mold_data.get("ref"),
                    field=f"data.moldes[{group_index}].molde.ref",
                    max_length=50,
                )
                mold = session.get(Molde, mold_ref)
                if mold is None or not mold.activo:
                    raise ScmServiceError(
                        "MOLD_NOT_FOUND",
                        "El molde reutilizable no existe o esta inactivo.",
                        status_code=404,
                        details={"molde_ref": mold_ref, "client_id": group_client_id},
                    )
                result["reused"].append({
                    "type": "MOLDE", "id": mold.codigo,
                    "client_id": group_client_id,
                })
            else:
                name = required_text(
                    mold_data.get("nombre"),
                    field=f"data.moldes[{group_index}].molde.nombre",
                    max_length=100,
                )
                shot_weight = _positive_number(
                    mold_data.get("peso_tiro_gr"),
                    field=f"data.moldes[{group_index}].molde.peso_tiro_gr",
                )
                cycle = _positive_number(
                    mold_data.get("tiempo_ciclo_std", 30),
                    field=f"data.moldes[{group_index}].molde.tiempo_ciclo_std",
                )
                mold = Molde(
                    codigo=generar_codigo_catalogo("MOLDE", session=session),
                    nombre=name,
                    peso_tiro_gr=shot_weight,
                    tiempo_ciclo_std=cycle,
                    activo=True,
                )
                session.add(mold)
                session.flush()
                result["created"].append({
                    "type": "MOLDE", "id": mold.codigo,
                    "client_id": group_client_id,
                })
            resolved_group["molde_ref"] = mold.codigo
            if legacy:
                resolved["molde_ref"] = mold.codigo
            checkpoint(result)
        if mold.codigo in used_molds:
            raise ScmServiceError(
                "DUPLICATE_MOLD_GROUP",
                "Un molde no puede repetirse en dos grupos de la misma fase.",
                status_code=422,
                details={"molde_ref": mold.codigo},
            )
        used_molds.add(mold.codigo)

        group_pieces = resolved_group.setdefault("piezas", [])
        by_client_id = {
            item["client_id"]: item for item in group_pieces
        }
        for piece_index, item in enumerate(pieces_data):
            client_id = client_ids[piece_index]
            if client_id in by_client_id:
                continue
            piece_mode = _mode(
                item.get("modo"),
                field=(
                    f"data.moldes[{group_index}].piezas[{piece_index}].modo"
                ),
                allowed={"NUEVA", "REUTILIZAR"},
            )
            cavities = _positive_number(
                item.get("cavidades"),
                field=(
                    f"data.moldes[{group_index}].piezas[{piece_index}]"
                    ".cavidades"
                ),
                integer=True,
            )
            unit_weight = _positive_number(
                item.get("peso_unitario_gr"),
                field=(
                    f"data.moldes[{group_index}].piezas[{piece_index}]"
                    ".peso_unitario_gr"
                ),
            )
            if piece_mode == "REUTILIZAR":
                try:
                    piece_id = int(item.get("ref"))
                except (TypeError, ValueError) as error:
                    raise ScmServiceError(
                        "PIECE_REFERENCE_REQUIRED",
                        "REUTILIZAR requiere ref de Pieza.",
                        status_code=422,
                        details={"client_id": client_id},
                    ) from error
                piece = session.get(Pieza, piece_id)
                if piece is None or not piece.activo:
                    raise ScmServiceError(
                        "PIECE_NOT_FOUND",
                        "La Pieza reutilizable no existe o esta inactiva.",
                        status_code=404,
                        details={"client_id": client_id, "pieza_ref": piece_id},
                    )
                result["reused"].append({
                    "type": "PIEZA", "id": piece.id,
                    "client_id": client_id,
                })
            else:
                name = required_text(
                    item.get("nombre"),
                    field=(
                        f"data.moldes[{group_index}].piezas[{piece_index}].nombre"
                    ),
                    max_length=200,
                )
                try:
                    linea, familia, _ = validate_linea_familia(
                        linea_id=item.get("linea_id"),
                        familia_id=item.get("familia_id"),
                        allow_unclassified=True,
                        session=session,
                    )
                except ClassificationError as error:
                    raise ScmServiceError(
                        error.code, str(error), status_code=error.status,
                        details={"client_id": client_id},
                    ) from error
                piece = Pieza(
                    codigo=generar_codigo_catalogo("PIEZA", session=session),
                    nombre=name,
                    linea_id=linea.id if linea else None,
                    familia_id=familia.id if familia else None,
                    peso_nominal_gr=unit_weight,
                    activo=True,
                )
                session.add(piece)
                session.flush()
                result["created"].append({
                    "type": "PIEZA", "id": piece.id,
                    "client_id": client_id,
                })

            composition = session.scalar(select(MoldePieza).where(
                MoldePieza.molde_id == mold.codigo,
                MoldePieza.pieza_id == piece.id,
            ))
            if composition is None:
                composition = MoldePieza(
                    molde_id=mold.codigo,
                    pieza_id=piece.id,
                    cavidades=cavities,
                    peso_unitario_gr=unit_weight,
                    activo=True,
                )
                session.add(composition)
                session.flush()
                result["created"].append({
                    "type": "MOLDE_PIEZA", "id": composition.id,
                    "client_id": client_id,
                })
            elif composition.activo and (
                composition.cavidades != cavities
                or float(composition.peso_unitario_gr) != unit_weight
            ):
                raise ScmServiceError(
                    "MOLD_PIECE_CONFIG_CONFLICT",
                    "La pieza ya pertenece al molde con otra configuracion.",
                    status_code=409,
                    details={
                        "client_id": client_id,
                        "molde_pieza_ref": composition.id,
                    },
                )
            else:
                if not composition.activo:
                    composition.activo = True
                    composition.cavidades = cavities
                    composition.peso_unitario_gr = unit_weight
                    composition.version += 1
                result["reused"].append({
                    "type": "MOLDE_PIEZA", "id": composition.id,
                    "client_id": client_id,
                })

            resolved_item = {
                "client_id": client_id,
                "pieza_ref": piece.id,
                "molde_pieza_ref": composition.id,
                "molde_client_id": group_client_id,
                "molde_ref": mold.codigo,
            }
            group_pieces.append(resolved_item)
            resolved_pieces.append(resolved_item)
            by_client_id[client_id] = resolved_item
            checkpoint(result)

    if len(resolved_groups) == 1:
        resolved["molde_ref"] = resolved_groups[0]["molde_ref"]
    else:
        resolved.pop("molde_ref", None)
    return result


def _as_reference_int(value, *, field):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_REFERENCE",
            f"{field} debe ser un entero positivo.",
            status_code=422,
            details={"field": field},
        ) from error
    if isinstance(value, bool) or parsed <= 0:
        raise ScmServiceError(
            "INVALID_REFERENCE",
            f"{field} debe ser un entero positivo.",
            status_code=422,
            details={"field": field},
        )
    return parsed


def _resolved_reference(value, client_value, *, by_client, field):
    if value not in (None, ""):
        return _as_reference_int(value, field=field)
    client_id = _client_id(client_value, field=f"{field}_client_id")
    resolved = by_client.get(client_id)
    if resolved is None:
        raise ScmServiceError(
            "UNRESOLVED_CLIENT_REFERENCE",
            f"No se pudo resolver {client_id}.",
            status_code=422,
            details={"field": field, "client_id": client_id},
        )
    return resolved


def _apply_colors(
    session,
    onboarding,
    step_payload,
    *,
    result,
    checkpoint,
):
    colors_data = step_payload.get("colores")
    matrix_data = step_payload.get("matriz")
    formulas_data = step_payload.get("formulaciones")
    if not isinstance(colors_data, list) or not colors_data:
        raise ScmServiceError(
            "COLORS_REQUIRED",
            "data.colores debe contener al menos un color.",
            status_code=422,
        )
    if not isinstance(matrix_data, list):
        raise ScmServiceError(
            "COLOR_MATRIX_REQUIRED",
            "data.matriz debe ser una lista.",
            status_code=422,
        )
    if not isinstance(formulas_data, list):
        raise ScmServiceError(
            "FORMULATIONS_REQUIRED",
            "data.formulaciones debe ser una lista.",
            status_code=422,
        )

    component_refs = (onboarding.referencias_json or {}).get(
        "COMPONENTES", {}
    )
    component_pieces = [
        item for item in (component_refs.get("piezas") or [])
        if isinstance(item, dict) and item.get("pieza_ref")
    ]
    mold_refs = {
        item.get("molde_ref")
        for item in (component_refs.get("moldes") or [])
        if isinstance(item, dict) and item.get("molde_ref")
    }
    if component_refs.get("molde_ref"):
        mold_refs.add(component_refs["molde_ref"])
    if not component_pieces or not mold_refs:
        raise ScmServiceError(
            "COMPONENT_REFERENCES_REQUIRED",
            "Aplique COMPONENTES antes de COLORES.",
            status_code=409,
        )
    requested_mold = step_payload.get("color_molde_ref")
    if requested_mold not in (None, "") and requested_mold not in mold_refs:
        raise ScmServiceError(
            "MOLD_REFERENCE_CONFLICT",
            "color_molde_ref no coincide con COMPONENTES.",
            status_code=409,
        )
    mold_piece_ids = set()
    mold_piece_ids_by_mold = {}
    for item in component_pieces:
        composition = session.get(MoldePieza, item.get("molde_pieza_ref"))
        if (
            composition is None
            or not composition.activo
            or composition.pieza_id != item.get("pieza_ref")
            or composition.molde_id not in mold_refs
        ):
            raise ScmServiceError(
                "MOLD_PIECE_REFERENCE_INVALID",
                "Una pieza de COMPONENTES ya no pertenece a su molde activo.",
                status_code=409,
                details={"pieza_ref": item.get("pieza_ref")},
            )
        mold = session.get(Molde, composition.molde_id)
        if mold is None or not mold.activo:
            raise ScmServiceError(
                "MOLD_NOT_FOUND",
                "Un molde de COMPONENTES no existe o esta inactivo.",
                status_code=404,
                details={"molde_ref": composition.molde_id},
            )
        mold_piece_ids.add(composition.pieza_id)
        mold_piece_ids_by_mold.setdefault(composition.molde_id, set()).add(
            composition.pieza_id
        )
    piece_client_refs = {
        item["client_id"]: item["pieza_ref"]
        for item in component_pieces
        if isinstance(item, dict) and item.get("client_id")
    }

    resolved = result.setdefault("resolved_references", {})
    resolved_colors = resolved.setdefault("colores", [])
    resolved_matrix = resolved.setdefault("matriz", [])
    resolved_formulas = resolved.setdefault("formulaciones", [])
    color_client_ids = []
    for index, item in enumerate(colors_data):
        if not isinstance(item, dict):
            raise ScmServiceError(
                "INVALID_COLOR_DATA",
                "Cada color debe ser un objeto.",
                status_code=422,
            )
        color_client_ids.append(_client_id(
            item.get("client_id"), field=f"data.colores[{index}].client_id"
        ))
    if len(color_client_ids) != len(set(color_client_ids)):
        raise ScmServiceError(
            "DUPLICATE_CLIENT_ID",
            "Los client_id de colores no pueden repetirse.",
            status_code=422,
        )

    declared_direct_color_ids = set()
    for index, item in enumerate(colors_data):
        declared_mode = _mode(
            item.get("modo"),
            field=f"data.colores[{index}].modo",
            allowed={"NUEVO", "REUTILIZAR"},
        )
        if declared_mode == "REUTILIZAR":
            declared_direct_color_ids.add(_as_reference_int(
                item.get("color_ref"),
                field=f"data.colores[{index}].color_ref",
            ))
    declared_client_ids = set(color_client_ids)
    for index, item in enumerate(formulas_data):
        if not isinstance(item, dict):
            raise ScmServiceError(
                "INVALID_FORMULATION",
                "Cada formulacion debe ser un objeto.",
                status_code=422,
            )
        direct = item.get("color_ref")
        client_ref = item.get("color_client_id")
        in_scope = False
        if direct not in (None, ""):
            in_scope = (
                _as_reference_int(
                    direct,
                    field=f"data.formulaciones[{index}].color_ref",
                )
                in declared_direct_color_ids
            )
        elif client_ref not in (None, ""):
            in_scope = str(client_ref).strip() in declared_client_ids
        if not in_scope:
            raise ScmServiceError(
                "FORMULATION_REFERENCE_OUT_OF_SCOPE",
                "La formulacion referencia un color no declarado en el paso.",
                status_code=422,
                details={"index": index},
            )

    color_by_client = {
        item["client_id"]: item["color_ref"] for item in resolved_colors
    }
    for index, item in enumerate(colors_data):
        client_id = color_client_ids[index]
        if client_id in color_by_client:
            continue
        color_mode = _mode(
            item.get("modo"),
            field=f"data.colores[{index}].modo",
            allowed={"NUEVO", "REUTILIZAR"},
        )
        created = False
        if color_mode == "REUTILIZAR":
            color_id = _as_reference_int(
                item.get("color_ref"),
                field=f"data.colores[{index}].color_ref",
            )
            color = session.get(ColorProduccion, color_id)
            if color is None or not color.activo:
                raise ScmServiceError(
                    "COLOR_NOT_FOUND",
                    "El color reutilizable no existe o esta inactivo.",
                    status_code=404,
                    details={"client_id": client_id, "color_ref": color_id},
                )
        else:
            name = required_text(
                item.get("nombre"),
                field=f"data.colores[{index}].nombre",
                max_length=50,
            ).upper()
            family_id = _as_reference_int(
                item.get("familia_color_id"),
                field=f"data.colores[{index}].familia_color_id",
            )
            family = session.get(FamiliaColor, family_id)
            if family is None or not family.activo:
                raise ScmServiceError(
                    "COLOR_FAMILY_NOT_FOUND",
                    "La familia de color no existe o esta inactiva.",
                    status_code=404,
                    details={"familia_color_id": family_id},
                )
            try:
                reference_hex = normalize_hex(
                    item.get("hex", item.get("hex_referencia"))
                )
            except ColorRecipeError as error:
                raise ScmServiceError(
                    error.code,
                    error.message,
                    status_code=error.status,
                    details=error.details,
                ) from error
            base = session.scalar(select(ColorBase).where(
                func.upper(ColorBase.nombre) == name
            ))
            if base is None:
                base = ColorBase(nombre=name)
                session.add(base)
                session.flush()
            color = session.scalar(select(ColorProduccion).where(
                ColorProduccion.color_base_id == base.id,
                ColorProduccion.familia_color_id == family.id,
            ))
            if color is None:
                color = ColorProduccion(
                    color_base_id=base.id,
                    familia_color_id=family.id,
                    hex_referencia=reference_hex,
                    activo=True,
                )
                session.add(color)
                session.flush()
                created = True
            elif not color.activo:
                raise ScmServiceError(
                    "COLOR_INACTIVE",
                    "La combinacion de color ya existe pero esta inactiva.",
                    status_code=409,
                )
        result["created" if created else "reused"].append({
            "type": "COLOR_PRODUCCION",
            "id": color.id,
            "client_id": client_id,
        })
        resolved_item = {"client_id": client_id, "color_ref": color.id}
        resolved_colors.append(resolved_item)
        color_by_client[client_id] = color.id
        checkpoint(result)

    selected_pairs = set()
    matrix_rows = []
    for index, item in enumerate(matrix_data):
        if not isinstance(item, dict):
            raise ScmServiceError(
                "INVALID_COLOR_MATRIX",
                "Cada fila de matriz debe ser un objeto.",
                status_code=422,
            )
        if not bool(item.get("seleccionada")):
            continue
        piece_id = _resolved_reference(
            item.get("pieza_ref"),
            item.get("pieza_client_id"),
            by_client=piece_client_refs,
            field=f"data.matriz[{index}].pieza_ref",
        )
        color_id = _resolved_reference(
            item.get("color_ref"),
            item.get("color_client_id"),
            by_client=color_by_client,
            field=f"data.matriz[{index}].color_ref",
        )
        if piece_id not in mold_piece_ids or color_id not in set(color_by_client.values()):
            raise ScmServiceError(
                "COLOR_MATRIX_REFERENCE_OUT_OF_SCOPE",
                "La matriz contiene una referencia fuera del alta.",
                status_code=422,
            )
        pair = (piece_id, color_id)
        if pair in selected_pairs:
            raise ScmServiceError(
                "DUPLICATE_COLOR_MATRIX_ROW",
                "La matriz repite una combinacion Pieza-Color.",
                status_code=422,
            )
        selected_pairs.add(pair)
        recipe_id = None
        if item.get("receta_ref") not in (None, ""):
            recipe_id = _as_reference_int(
                item.get("receta_ref"),
                field=f"data.matriz[{index}].receta_ref",
            )
            recipe = session.get(RecetaColorMaestra, recipe_id)
            if (
                recipe is None
                or recipe.color_produccion_id != color_id
                or recipe.estado == "INACTIVA"
                or recipe.producto_scope
                not in {"*", onboarding.producto_terminado_id}
            ):
                raise ScmServiceError(
                    "PIECE_COLOR_RECIPE_NOT_FOUND",
                    (
                        "La receta especifica de la pieza no existe, esta "
                        "inactiva, pertenece a otro color o a otro producto."
                    ),
                    status_code=422,
                    details={
                        "pieza_ref": piece_id,
                        "color_ref": color_id,
                        "receta_ref": recipe_id,
                    },
                )
        matrix_rows.append((item, piece_id, color_id, recipe_id))

    selected_color_ids = {color_id for _piece_id, color_id in selected_pairs}
    unused_color_ids = set(color_by_client.values()) - selected_color_ids
    if unused_color_ids:
        raise ScmServiceError(
            "UNUSED_COLOR",
            "Cada color declarado debe asociarse al menos a una pieza.",
            status_code=422,
            details={"color_refs": sorted(unused_color_ids)},
        )

    uncovered_piece_ids = mold_piece_ids - {
        piece_id for piece_id, _color_id in selected_pairs
    }
    if uncovered_piece_ids:
        raise ScmServiceError(
            "PIECE_WITHOUT_COLOR",
            "Cada pieza debe tener al menos un color de produccion.",
            status_code=422,
            details={"pieza_refs": sorted(uncovered_piece_ids)},
        )

    # La uniformidad de color pertenece a un golpe de molde, no al PT entero.
    # Si un molde multicavidad produce varias piezas en el mismo golpe, un color
    # seleccionado para una de ellas debe cubrirlas a todas. Moldes distintos
    # pueden tener conjuntos de colores completamente independientes.
    for mold_ref, group_piece_ids in mold_piece_ids_by_mold.items():
        group_color_ids = {
            color_id for piece_id, color_id in selected_pairs
            if piece_id in group_piece_ids
        }
        for color_id in group_color_ids:
            covered = {
                piece_id for piece_id, pair_color in selected_pairs
                if pair_color == color_id and piece_id in group_piece_ids
            }
            if covered == group_piece_ids:
                continue
            raise ScmServiceError(
                "INCOMPLETE_MOLD_COLOR_COVERAGE",
                "Un color debe cubrir todas las piezas del mismo golpe de molde.",
                status_code=422,
                details={
                    "molde_ref": mold_ref,
                    "color_ref": color_id,
                    "missing_piece_refs": sorted(group_piece_ids - covered),
                },
            )

    resolved_pairs = {
        (item["pieza_ref"], item["color_ref"]): item
        for item in resolved_matrix
    }
    reverse_piece_clients = {value: key for key, value in piece_client_refs.items()}
    reverse_color_clients = {value: key for key, value in color_by_client.items()}
    for _, piece_id, color_id, recipe_id in matrix_rows:
        if (piece_id, color_id) in resolved_pairs:
            continue
        piece = session.get(Pieza, piece_id)
        color = session.get(ColorProduccion, color_id)
        variant = session.scalar(select(PiezaColor).where(
            PiezaColor.pieza_id == piece_id,
            PiezaColor.color_produccion_id == color_id,
        ))
        created = False
        if variant is None:
            variant = PiezaColor(
                sku=generar_codigo_catalogo("PIEZA_COLOR", session=session),
                piezas=f"{piece.nombre} {color.nombre}",
                peso=piece.peso_nominal_gr,
                cavidad=None,
                linea_id=None,
                familia_id=None,
                color_produccion_id=color.id,
                pieza_id=piece.id,
                estado_revision="EN_REVISION",
            )
            session.add(variant)
            session.flush()
            created = True
        result["created" if created else "reused"].append({
            "type": "PIEZA_COLOR",
            "id": variant.sku,
        })
        resolved_item = {
            "pieza_ref": piece_id,
            "pieza_client_id": reverse_piece_clients.get(piece_id),
            "color_ref": color_id,
            "color_client_id": reverse_color_clients.get(color_id),
            "pieza_color_ref": variant.sku,
            "receta_ref": recipe_id,
        }
        resolved_matrix.append(resolved_item)
        resolved_pairs[(piece_id, color_id)] = resolved_item
        if recipe_id is not None:
            result["reused"].append({
                "type": "RECETA_COLOR",
                "id": recipe_id,
                "pieza_color_ref": variant.sku,
            })
        checkpoint(result)

    formula_by_color = {
        item["color_ref"]: item for item in resolved_formulas
    }
    seen_formula_colors = set()
    for index, item in enumerate(formulas_data):
        if not isinstance(item, dict):
            raise ScmServiceError(
                "INVALID_FORMULATION",
                "Cada formulacion debe ser un objeto.",
                status_code=422,
            )
        color_id = _resolved_reference(
            item.get("color_ref"),
            item.get("color_client_id"),
            by_client=color_by_client,
            field=f"data.formulaciones[{index}].color_ref",
        )
        if color_id not in set(color_by_client.values()):
            raise ScmServiceError(
                "FORMULATION_REFERENCE_OUT_OF_SCOPE",
                "La formulacion referencia un color no declarado en el paso.",
                status_code=422,
                details={"color_ref": color_id},
            )
        if color_id in seen_formula_colors:
            raise ScmServiceError(
                "DUPLICATE_FORMULATION",
                "Solo se admite una formulacion por color.",
                status_code=422,
            )
        seen_formula_colors.add(color_id)
        if color_id in formula_by_color:
            continue
        formula_type = _mode(
            item.get("tipo"),
            field=f"data.formulaciones[{index}].tipo",
            allowed={"EXISTENTE", "NUEVA", "SIN_PIGMENTO", "PENDIENTE"},
        )
        recipe_id = None
        state = "RESUELTA"
        if formula_type == "EXISTENTE":
            recipe_id = _as_reference_int(
                item.get("receta_ref"),
                field=f"data.formulaciones[{index}].receta_ref",
            )
            recipe = session.get(RecetaColorMaestra, recipe_id)
            if (
                recipe is None
                or recipe.color_produccion_id != color_id
                or recipe.estado == "INACTIVA"
            ):
                raise ScmServiceError(
                    "RECIPE_NOT_FOUND",
                    "La receta no existe, esta inactiva o pertenece a otro color.",
                    status_code=404,
                    details={"receta_ref": recipe_id, "color_ref": color_id},
                )
            result["reused"].append({"type": "RECETA_COLOR", "id": recipe_id})
        elif formula_type == "PENDIENTE":
            reason = required_text(
                item.get("motivo_pendiente"),
                field=f"data.formulaciones[{index}].motivo_pendiente",
                max_length=500,
            )
            state = "PENDIENTE"
            result["pending"].append({
                "type": "RECETA_COLOR",
                "color_ref": color_id,
                "reason": reason,
            })
        else:
            components = item.get("componentes")
            if not isinstance(components, list) or not components:
                raise ScmServiceError(
                    "RECIPE_COMPONENTS_REQUIRED",
                    "La formulacion requiere componentes.",
                    status_code=422,
                    details={"color_ref": color_id},
                )
            if formula_type == "SIN_PIGMENTO":
                if any(
                    str(line.get("tipo_componente") or "").upper()
                    != "MATERIA_PRIMA"
                    for line in components
                    if isinstance(line, dict)
                ):
                    raise ScmServiceError(
                        "PIGMENT_IN_PIGMENT_FREE_RECIPE",
                        "SIN_PIGMENTO solo admite materia prima virgen.",
                        status_code=422,
                    )
                try:
                    fraction = sum(float(line.get("cantidad")) for line in components)
                except (TypeError, ValueError, AttributeError) as error:
                    raise ScmServiceError(
                        "INVALID_RECIPE_FRACTION",
                        "Las fracciones de materia prima son invalidas.",
                        status_code=422,
                    ) from error
                if abs(fraction - 1.0) > 1e-9:
                    raise ScmServiceError(
                        "INVALID_RECIPE_FRACTION",
                        "SIN_PIGMENTO requiere fraccion total de materia prima igual a 1.",
                        status_code=422,
                    )
                state = "SIN_PIGMENTO"
            color = session.get(ColorProduccion, color_id)
            try:
                recipe_payload = create_recipe(session, {
                    "color_produccion_id": color_id,
                    "producto_sku": onboarding.producto_terminado_id,
                    "nombre_variante": (
                        "Sin pigmento" if formula_type == "SIN_PIGMENTO"
                        else f"Alta guiada {color.nombre}"
                    ),
                    "estado": "BORRADOR",
                    "es_default": False,
                    "base_virgen_kg": item.get("base_virgen_kg", 25),
                    "origen": "ALTA_GUIADA",
                    "lineas": components,
                }, commit=False)
            except ColorRecipeError as error:
                raise ScmServiceError(
                    error.code,
                    error.message,
                    status_code=error.status,
                    details=error.details,
                ) from error
            recipe_id = recipe_payload["id"]
            result["created"].append({"type": "RECETA_COLOR", "id": recipe_id})

        resolved_item = {
            "color_ref": color_id,
            "color_client_id": reverse_color_clients.get(color_id),
            "tipo": formula_type,
            "receta_ref": recipe_id,
            "estado": state,
        }
        resolved_formulas.append(resolved_item)
        formula_by_color[color_id] = resolved_item
        checkpoint(result)

    missing_formulas = set(color_by_client.values()) - seen_formula_colors
    if missing_formulas:
        raise ScmServiceError(
            "FORMULATION_COVERAGE_INCOMPLETE",
            "Cada color requiere formulacion o declaracion pendiente.",
            status_code=422,
            details={"missing_color_refs": sorted(missing_formulas)},
        )
    return result


def _canonical_operation_id(onboarding, application_key, unit):
    return uuid.uuid5(
        onboarding.id,
        f"alta-producto:{application_key}:{unit}",
    )


def _require_actor_capability(actor, capability):
    if not actor.tiene_capacidad(capability):
        raise ScmServiceError(
            "CAPABILITY_REQUIRED",
            f"La operacion requiere la capacidad {capability}.",
            status_code=403,
            details={"capability": capability},
        )


def _record_result_once(result, bucket, item):
    rows = result.setdefault(bucket, [])
    if item not in rows:
        rows.append(item)


def _product_article_for_onboarding(session, onboarding):
    if not onboarding.producto_terminado_id:
        raise ScmServiceError(
            "PRODUCT_REFERENCE_REQUIRED",
            "La sesion no tiene ProductoTerminado materializado.",
            status_code=422,
        )
    article = session.scalar(
        select(ScmArticulo)
        .join(
            ScmArticuloProducto,
            ScmArticuloProducto.articulo_id == ScmArticulo.id,
        )
        .where(
            ScmArticuloProducto.producto_terminado_id
            == onboarding.producto_terminado_id
        )
    )
    if article is None or not article.activo:
        raise ScmServiceError(
            "PRODUCT_ARTICLE_NOT_FOUND",
            "El PT no tiene Articulo SCM activo.",
            status_code=422,
        )
    return article


def _structure_payload_with_wips(
    session,
    actor,
    step_payload,
    command,
    *,
    mode,
    resolved,
    result,
    checkpoint,
):
    raw_wips = step_payload.get("wips_nuevos", [])
    if not isinstance(raw_wips, list):
        raise ScmServiceError(
            "WIP_LIST_REQUIRED",
            "data.wips_nuevos debe ser una lista.",
            status_code=400,
        )
    if raw_wips and mode == "REUTILIZAR":
        raise ScmServiceError(
            "WIP_NOT_APPLICABLE",
            "REUTILIZAR no puede crear WIP fuera de la estructura.",
            status_code=422,
        )
    wip_by_client = {}
    resolved_wips = resolved.setdefault("wips", [])
    for existing in resolved_wips:
        if isinstance(existing, dict) and existing.get("client_id"):
            wip_by_client[existing["client_id"]] = existing.get(
                "articulo_ref"
            )
    requested_client_ids = []
    for index, raw in enumerate(raw_wips):
        item = _json_object(
            raw,
            field=f"data.wips_nuevos[{index}]",
            required=True,
        )
        reject_unknown_fields(
            item,
            allowed={
                "client_id",
                "nombre",
                "descripcion",
                "requiere_calidad",
            },
        )
        client_id = _client_id(
            item.get("client_id"),
            field=f"data.wips_nuevos[{index}].client_id",
        )
        requested_client_ids.append(client_id)
        if client_id in wip_by_client:
            continue
        _require_actor_capability(actor, "ARTICULO_ADMINISTRAR")
        payload = {
            key: value
            for key, value in item.items()
            if key in {"nombre", "descripcion", "requiere_calidad"}
        }
        response = create_wip_article(
            session,
            actor_id=actor.id,
            data=payload,
            commit=False,
        )
        article_id = response["id"]
        resolved_item = {
            "client_id": client_id,
            "articulo_ref": article_id,
        }
        resolved_wips.append(resolved_item)
        wip_by_client[client_id] = article_id
        _record_result_once(result, "created", {
            "type": "SUBENSAMBLE_WIP",
            "id": article_id,
            "client_id": client_id,
        })
        checkpoint(result)
    if len(requested_client_ids) != len(set(requested_client_ids)):
        raise ScmServiceError(
            "DUPLICATE_WIP_CLIENT_ID",
            "Los client_id de WIP no pueden repetirse.",
            status_code=422,
        )

    if mode == "REUTILIZAR":
        return None
    payload = _json_object(
        command.get("payload"),
        field="data.estructura.payload",
        required=True,
    )
    components = payload.get("componentes")
    if not isinstance(components, list):
        return payload
    transformed = []
    for index, raw in enumerate(components):
        component = _json_object(
            raw,
            field=f"data.estructura.payload.componentes[{index}]",
            required=True,
        )
        client_ref = component.pop("articulo_client_id", None)
        direct_ref = component.get("articulo_id")
        if client_ref not in (None, "") and direct_ref not in (None, ""):
            raise ScmServiceError(
                "STRUCTURE_COMPONENT_REFERENCE_CONFLICT",
                "Use articulo_id o articulo_client_id, no ambos.",
                status_code=422,
            )
        if client_ref not in (None, ""):
            client_ref = _client_id(
                client_ref,
                field=(
                    "data.estructura.payload.componentes"
                    f"[{index}].articulo_client_id"
                ),
            )
            article_id = wip_by_client.get(client_ref)
            if article_id is None:
                raise ScmServiceError(
                    "UNRESOLVED_WIP_REFERENCE",
                    "La estructura referencia un WIP no declarado.",
                    status_code=422,
                    details={"client_id": client_ref},
                )
            component["articulo_id"] = article_id
        transformed.append(component)
    payload["componentes"] = transformed
    return payload


def _apply_structure(
    session,
    onboarding,
    actor,
    application_key,
    step_payload,
    *,
    result,
    checkpoint,
):
    article = _product_article_for_onboarding(session, onboarding)
    target_id = _as_reference_int(
        step_payload.get("target_article_ref"),
        field="data.target_article_ref",
    )
    if target_id != article.id:
        raise ScmServiceError(
            "STRUCTURE_TARGET_OUT_OF_SCOPE",
            "La estructura debe corresponder al PT de la sesion.",
            status_code=422,
        )
    command = _json_object(
        step_payload.get("estructura"),
        field="data.estructura",
        required=True,
    )
    mode = _mode(
        command.get("modo"),
        field="data.estructura.modo",
        allowed={"NUEVA", "EDITAR", "REUTILIZAR"},
    )
    action = _mode(
        command.get("accion"),
        field="data.estructura.accion",
        allowed={
            "GUARDAR_BORRADOR",
            "ENVIAR_APROBACION",
            "PUBLICAR",
            "VINCULAR",
        },
    )
    if mode in {"NUEVA", "EDITAR"}:
        _require_actor_capability(actor, "ESTRUCTURA_ADMINISTRAR")
    else:
        _require_actor_capability(actor, "ESTRUCTURA_VER")
    if action == "ENVIAR_APROBACION":
        _require_actor_capability(actor, "ESTRUCTURA_ADMINISTRAR")
    elif action == "PUBLICAR":
        _require_actor_capability(actor, "ESTRUCTURA_PUBLICAR_DIRECTO")
    if mode != "NUEVA":
        preflight_structure_id = _as_reference_int(
            command.get("revision_ref"),
            field="data.estructura.revision_ref",
        )
        preflight_structure = session.get(
            ScmEstructuraRevision, preflight_structure_id
        )
        if preflight_structure is None:
            raise ScmServiceError(
                "STRUCTURE_NOT_FOUND",
                "La estructura vinculada no existe.",
                status_code=404,
            )
        if preflight_structure.articulo_resultado_id != article.id:
            raise ScmServiceError(
                "STRUCTURE_TARGET_OUT_OF_SCOPE",
                "La estructura vinculada pertenece a otro articulo.",
                status_code=422,
            )
    resolved = result.setdefault("resolved_references", {})
    structure_payload = _structure_payload_with_wips(
        session,
        actor,
        step_payload,
        command,
        mode=mode,
        resolved=resolved,
        result=result,
        checkpoint=checkpoint,
    )
    structure_id = _readiness_int_reference(
        resolved.get("estructura_revision_ref")
    )
    structure = (
        session.get(ScmEstructuraRevision, structure_id)
        if structure_id else None
    )
    if structure is None:
        if mode == "NUEVA":
            created = create_structure(
                session,
                actor_id=actor.id,
                article_id=article.id,
                data=structure_payload,
                commit=False,
            )
            structure_id = created["id"]
            _record_result_once(result, "created", {
                "type": "ESTRUCTURA",
                "id": structure_id,
            })
        else:
            structure_id = _as_reference_int(
                command.get("revision_ref"),
                field="data.estructura.revision_ref",
            )
            structure = session.get(ScmEstructuraRevision, structure_id)
            if structure is None:
                raise ScmServiceError(
                    "STRUCTURE_NOT_FOUND",
                    "La estructura vinculada no existe.",
                    status_code=404,
                )
            if structure.articulo_resultado_id != article.id:
                raise ScmServiceError(
                    "STRUCTURE_TARGET_OUT_OF_SCOPE",
                    "La estructura vinculada pertenece a otro articulo.",
                    status_code=422,
                )
            if mode == "EDITAR":
                updated = update_structure(
                    session,
                    actor_id=actor.id,
                    structure_id=structure_id,
                    data={
                        "version": command.get("expected_version"),
                        **structure_payload,
                    },
                    commit=False,
                )
                structure_id = updated["id"]
            else:
                _record_result_once(result, "reused", {
                    "type": "ESTRUCTURA",
                    "id": structure_id,
                })
        structure = session.get(ScmEstructuraRevision, structure_id)
        if structure.articulo_resultado_id != article.id:
            raise ScmServiceError(
                "STRUCTURE_TARGET_OUT_OF_SCOPE",
                "La estructura vinculada pertenece a otro articulo.",
                status_code=422,
            )
        resolved.update({
            "estructura_revision_ref": structure.id,
            "estructura_revision_version": structure.version,
            "estado": structure.estado,
        })
        checkpoint(result)

    if structure.articulo_resultado_id != article.id:
        raise ScmServiceError(
            "STRUCTURE_TARGET_OUT_OF_SCOPE",
            "La estructura vinculada pertenece a otro articulo.",
            status_code=422,
        )

    if action in {"ENVIAR_APROBACION", "PUBLICAR"} and (
        structure.estado == ESTADO_ESTRUCTURA_BORRADOR
    ):
        operation_id = _canonical_operation_id(
            onboarding,
            application_key,
            "estructura-publicacion",
        )
        if action == "PUBLICAR":
            published = publish_structure_directly(
                session,
                actor_id=actor.id,
                structure_id=structure.id,
                operation_id=operation_id,
                data={"version": structure.version},
                commit=False,
            )
        else:
            published = send_structure_for_approval(
                session,
                actor_id=actor.id,
                structure_id=structure.id,
                operation_id=operation_id,
                data={"version": structure.version},
                commit=False,
            )
        structure = session.get(ScmEstructuraRevision, published["id"])
        checkpoint(result)

    resolved.update({
        "estructura_revision_ref": structure.id,
        "estructura_revision_version": structure.version,
        "estado": structure.estado,
    })
    result["pending"] = [
        item for item in result.get("pending") or []
        if item.get("type") != "ESTRUCTURA"
    ]
    if structure.estado != ESTADO_ESTRUCTURA_APROBADA:
        result["pending"].append({
            "type": "ESTRUCTURA",
            "id": structure.id,
            "estado": structure.estado,
        })
    return result


def _route_entity(
    session,
    onboarding,
    actor,
    application_key,
    command,
    *,
    result,
    checkpoint,
):
    mode = _mode(
        command.get("modo"),
        field="data.ruta.modo",
        allowed={"NUEVA", "EDITAR", "REUTILIZAR"},
    )
    action = _mode(
        command.get("accion"),
        field="data.ruta.accion",
        allowed={"GUARDAR_BORRADOR", "PUBLICAR", "VINCULAR"},
    )
    if mode in {"NUEVA", "EDITAR"}:
        _require_actor_capability(actor, "RUTA_ADMINISTRAR")
    else:
        _require_actor_capability(actor, "RUTA_VER")
    if action == "PUBLICAR":
        _require_actor_capability(actor, "RUTA_PUBLICAR_DIRECTO")
    article = _product_article_for_onboarding(session, onboarding)
    resolved = result.setdefault("resolved_references", {})
    route_id = _readiness_int_reference(resolved.get("ruta_revision_ref"))
    route = session.get(ScmRutaRevision, route_id) if route_id else None
    if route is None:
        if mode == "NUEVA":
            payload = _json_object(
                command.get("payload"),
                field="data.ruta.payload",
                required=True,
            )
            response = create_route(
                session,
                actor_id=actor.id,
                product_id=onboarding.producto_terminado_id,
                data=payload,
                commit=False,
            )
            route_id = response["id"]
            _record_result_once(result, "created", {
                "type": "RUTA",
                "id": route_id,
            })
        else:
            route_id = _as_reference_int(
                command.get("revision_ref"),
                field="data.ruta.revision_ref",
            )
            route = session.get(ScmRutaRevision, route_id)
            if route is None:
                raise ScmServiceError(
                    "ROUTE_NOT_FOUND",
                    "La ruta vinculada no existe.",
                    status_code=404,
                )
            if route.articulo_objetivo_id != article.id:
                raise ScmServiceError(
                    "ROUTE_TARGET_OUT_OF_SCOPE",
                    "La ruta vinculada pertenece a otro producto.",
                    status_code=422,
                )
            if mode == "EDITAR":
                payload = _json_object(
                    command.get("payload"),
                    field="data.ruta.payload",
                    required=True,
                )
                response = update_route(
                    session,
                    actor_id=actor.id,
                    route_id=route_id,
                    data={
                        "version": command.get("expected_version"),
                        **payload,
                    },
                    commit=False,
                )
            else:
                _record_result_once(result, "reused", {
                    "type": "RUTA",
                    "id": route_id,
                })
        route = session.get(ScmRutaRevision, route_id)
        if route.articulo_objetivo_id != article.id:
            raise ScmServiceError(
                "ROUTE_TARGET_OUT_OF_SCOPE",
                "La ruta vinculada pertenece a otro producto.",
                status_code=422,
            )
        resolved.update({
            "ruta_revision_ref": route.id,
            "ruta_revision_version": route.version,
            "ruta_estado": route.estado,
        })
        checkpoint(result)
    if route.articulo_objetivo_id != article.id:
        raise ScmServiceError(
            "ROUTE_TARGET_OUT_OF_SCOPE",
            "La ruta vinculada pertenece a otro producto.",
            status_code=422,
        )
    if (
        action == "PUBLICAR"
        and route.estado == ESTADO_RUTA_BORRADOR
    ):
        response = publish_route_directly(
            session,
            actor_id=actor.id,
            route_id=route.id,
            operation_id=_canonical_operation_id(
                onboarding, application_key, "ruta-publicacion"
            ),
            data={"version": route.version},
            commit=False,
        )
        route = session.get(ScmRutaRevision, response["id"])
        checkpoint(result)
    resolved.update({
        "ruta_revision_ref": route.id,
        "ruta_revision_version": route.version,
        "ruta_estado": route.estado,
    })
    result["pending"] = [
        item for item in result.get("pending") or []
        if item.get("type") != "RUTA"
    ]
    if route.estado != ESTADO_RUTA_APROBADA:
        _record_result_once(result, "pending", {
            "type": "RUTA",
            "id": route.id,
            "estado": route.estado,
        })
    return route


def _packaging_profile_entity(
    session,
    article,
    actor,
    command,
    *,
    resolved,
    result,
    checkpoint,
):
    mode = _mode(
        command.get("modo"),
        field="data.perfil_empacable.modo",
        allowed={"NUEVO", "EDITAR", "REUTILIZAR"},
    )
    _require_actor_capability(actor, "EMPAQUE_ADMINISTRAR")
    if mode == "REUTILIZAR":
        _require_actor_capability(actor, "EMPAQUE_VER")
    profile_id = _readiness_int_reference(
        resolved.get("perfil_empacable_ref")
    )
    profile = session.get(ScmPerfilEmpacable, profile_id) if profile_id else None
    if profile is None:
        if mode == "NUEVO":
            response = create_packable_profile(
                session,
                actor_id=actor.id,
                data=_json_object(
                    command.get("payload"),
                    field="data.perfil_empacable.payload",
                    required=True,
                ),
                commit=False,
            )
            profile_id = response["id"]
            _record_result_once(result, "created", {
                "type": "PERFIL_EMPAQUE",
                "id": profile_id,
            })
        else:
            profile_id = _as_reference_int(
                command.get("ref"),
                field="data.perfil_empacable.ref",
            )
            profile = session.get(ScmPerfilEmpacable, profile_id)
            if profile is None:
                raise ScmServiceError(
                    "PACKABLE_PROFILE_NOT_FOUND",
                    "El perfil empacable no existe.",
                    status_code=404,
                )
            if not profile.activo:
                raise ScmServiceError(
                    "PACKABLE_PROFILE_NOT_FOUND",
                    "El perfil empacable esta inactivo.",
                    status_code=422,
                )
            if mode == "EDITAR":
                response = update_packable_profile(
                    session,
                    actor_id=actor.id,
                    profile_id=profile_id,
                    data={
                        "version": command.get("expected_version"),
                        **_json_object(
                            command.get("payload"),
                            field="data.perfil_empacable.payload",
                            required=True,
                        ),
                    },
                    commit=False,
                )
            else:
                _record_result_once(result, "reused", {
                    "type": "PERFIL_EMPAQUE",
                    "id": profile_id,
                })
        profile = session.get(ScmPerfilEmpacable, profile_id)
        if profile is None or not profile.activo:
            raise ScmServiceError(
                "PACKABLE_PROFILE_NOT_FOUND",
                "El perfil empacable no existe o esta inactivo.",
                status_code=422,
            )
        resolved["perfil_empacable_ref"] = profile.id
        resolved["perfil_estado"] = "ACTIVO"
        checkpoint(result)

    if command.get("asignar_predeterminado") is not True:
        raise ScmServiceError(
            "DEFAULT_PACKAGING_PROFILE_REQUIRED",
            "El alta requiere asignar el perfil como predeterminado.",
            status_code=422,
        )
    current = session.scalars(select(ScmArticuloPerfil).where(
        ScmArticuloPerfil.articulo_id == article.id,
    )).all()
    defaults = [
        link for link in current
        if link.activo and link.es_predeterminado
    ]
    if not (
        len(defaults) == 1
        and defaults[0].perfil_empacable_id == profile.id
        and any(
            link.perfil_empacable_id == profile.id and link.activo
            for link in current
        )
    ):
        article = session.get(ScmArticulo, article.id)
        profiles_by_id = {
            link.perfil_empacable_id: {
                "perfil_empacable_id": link.perfil_empacable_id,
                "es_predeterminado": False,
                "activo": link.activo,
            }
            for link in current
        }
        profiles_by_id[profile.id] = {
            "perfil_empacable_id": profile.id,
            "es_predeterminado": True,
            "activo": True,
        }
        assign_article_profiles(
            session,
            actor_id=actor.id,
            article_id=article.id,
            data={
                "version": article.version,
                "perfiles": list(profiles_by_id.values()),
            },
            commit=False,
        )
        checkpoint(result)
    return profile


def _packaging_rule_entity(
    session,
    onboarding,
    actor,
    application_key,
    profile,
    command,
    *,
    resolved,
    result,
    checkpoint,
):
    mode = _mode(
        command.get("modo"),
        field="data.regla_empaque.modo",
        allowed={"NUEVA", "EDITAR", "REUTILIZAR"},
    )
    action = _mode(
        command.get("accion"),
        field="data.regla_empaque.accion",
        allowed={"GUARDAR_BORRADOR", "PUBLICAR", "VINCULAR"},
    )
    if mode in {"NUEVA", "EDITAR"}:
        _require_actor_capability(actor, "EMPAQUE_ADMINISTRAR")
    else:
        _require_actor_capability(actor, "EMPAQUE_VER")
    if action == "PUBLICAR":
        _require_actor_capability(actor, "EMPAQUE_PUBLICAR_DIRECTO")
    revision_id = _readiness_int_reference(
        resolved.get("regla_empaque_revision_ref")
    )
    revision = (
        session.get(ScmReglaEmpaqueRevision, revision_id)
        if revision_id else None
    )
    if revision is None:
        if mode == "NUEVA":
            payload = _json_object(
                command.get("payload"),
                field="data.regla_empaque.payload",
                required=True,
            )
            payload["perfil_empacable_id"] = profile.id
            response = create_packaging_rule(
                session,
                actor_id=actor.id,
                data=payload,
                commit=False,
            )
            revision_id = response["revision_id"]
            _record_result_once(result, "created", {
                "type": "REGLA_EMPAQUE",
                "id": revision_id,
            })
        else:
            revision_id = _as_reference_int(
                command.get("revision_ref"),
                field="data.regla_empaque.revision_ref",
            )
            revision = session.get(ScmReglaEmpaqueRevision, revision_id)
            if revision is None:
                raise ScmServiceError(
                    "PACKAGING_RULE_NOT_FOUND",
                    "La regla de empaque no existe.",
                    status_code=404,
                )
            if revision.regla.perfil_empacable_id != profile.id:
                raise ScmServiceError(
                    "PACKAGING_RULE_PROFILE_CONFLICT",
                    "La regla no pertenece al perfil de la sesion.",
                    status_code=422,
                )
            if mode == "EDITAR":
                response = update_packaging_rule(
                    session,
                    actor_id=actor.id,
                    revision_id=revision_id,
                    data={
                        "version": command.get("expected_version"),
                        **_json_object(
                            command.get("payload"),
                            field="data.regla_empaque.payload",
                            required=True,
                        ),
                    },
                    commit=False,
                )
            else:
                _record_result_once(result, "reused", {
                    "type": "REGLA_EMPAQUE",
                    "id": revision_id,
                })
        revision = session.get(ScmReglaEmpaqueRevision, revision_id)
        if revision.regla.perfil_empacable_id != profile.id:
            raise ScmServiceError(
                "PACKAGING_RULE_PROFILE_CONFLICT",
                "La regla no pertenece al perfil de la sesion.",
                status_code=422,
            )
        resolved.update({
            "regla_empaque_revision_ref": revision.id,
            "regla_empaque_revision_version": revision.version,
        })
        checkpoint(result)
    if (
        action == "PUBLICAR"
        and revision.estado == ESTADO_REGLA_BORRADOR
    ):
        response = publish_packaging_rule_directly(
            session,
            actor_id=actor.id,
            revision_id=revision.id,
            operation_id=_canonical_operation_id(
                onboarding,
                application_key,
                (
                    "regla-empaque-publicacion:"
                    f"{resolved.get('articulo_ref')}:{revision.id}"
                ),
            ),
            data={"version": revision.version},
            commit=False,
        )
        revision = session.get(
            ScmReglaEmpaqueRevision, response["revision_id"]
        )
        checkpoint(result)
    resolved.update({
        "regla_empaque_revision_ref": revision.id,
        "regla_empaque_revision_version": revision.version,
    })
    result["pending"] = [
        item for item in result.get("pending") or []
        if not (
            item.get("type") == "REGLA_EMPAQUE"
            and item.get("id") == revision.id
        )
    ]
    if revision.estado != ESTADO_REGLA_APROBADA:
        _record_result_once(result, "pending", {
            "type": "REGLA_EMPAQUE",
            "id": revision.id,
            "estado": revision.estado,
        })
    return revision


def _apply_route_packaging(
    session,
    onboarding,
    actor,
    application_key,
    step_payload,
    *,
    result,
    checkpoint,
):
    article = _product_article_for_onboarding(session, onboarding)
    target_product = required_text(
        step_payload.get("target_product_ref"),
        field="data.target_product_ref",
        max_length=50,
    )
    target_article = _as_reference_int(
        step_payload.get("target_article_ref"),
        field="data.target_article_ref",
    )
    if (
        target_product != onboarding.producto_terminado_id
        or target_article != article.id
    ):
        raise ScmServiceError(
            "ROUTE_TARGET_OUT_OF_SCOPE",
            "Ruta y empaque deben corresponder al PT de la sesion.",
            status_code=422,
        )
    route = _route_entity(
        session,
        onboarding,
        actor,
        application_key,
        _json_object(
            step_payload.get("ruta"), field="data.ruta", required=True
        ),
        result=result,
        checkpoint=checkpoint,
    )
    raw_packagings = step_payload.get("empaques")
    if not isinstance(raw_packagings, list) or not raw_packagings:
        raise ScmServiceError(
            "PACKAGING_OUTPUTS_REQUIRED",
            "empaques debe cubrir cada salida unica de la ruta.",
            status_code=422,
        )
    output_ids = {
        operation.articulo_salida_id
        for operation in route.operaciones
        if operation.articulo_salida_id is not None
    }
    declared_ids = []
    parsed_packagings = []
    for index, raw in enumerate(raw_packagings):
        item = _json_object(
            raw,
            field=f"data.empaques[{index}]",
            required=True,
        )
        reject_unknown_fields(
            item,
            allowed={
                "client_id",
                "articulo_ref",
                "perfil_empacable",
                "regla_empaque",
            },
        )
        output_id = _as_reference_int(
            item.get("articulo_ref"),
            field=f"data.empaques[{index}].articulo_ref",
        )
        declared_ids.append(output_id)
        parsed_packagings.append((index, item, output_id))
    if len(declared_ids) != len(set(declared_ids)):
        raise ScmServiceError(
            "DUPLICATE_PACKAGING_OUTPUT",
            "Cada salida de ruta puede configurarse una sola vez.",
            status_code=422,
        )
    if set(declared_ids) != output_ids:
        raise ScmServiceError(
            "PACKAGING_OUTPUT_COVERAGE_INCOMPLETE",
            "empaques debe coincidir exactamente con las salidas de la ruta.",
            status_code=422,
            details={
                "required_article_refs": sorted(output_ids),
                "received_article_refs": sorted(set(declared_ids)),
            },
        )

    resolved = result.setdefault("resolved_references", {})
    resolved_packagings = []
    seen_client_ids = set()
    for index, item, output_id in parsed_packagings:
        client_id = item.get("client_id")
        if client_id in (None, ""):
            client_id = f"salida-{output_id}"
        else:
            client_id = required_text(
                client_id,
                field=f"data.empaques[{index}].client_id",
                max_length=100,
            )
        if client_id in seen_client_ids:
            raise ScmServiceError(
                "DUPLICATE_PACKAGING_CLIENT_ID",
                "client_id no puede repetirse en empaques.",
                status_code=422,
            )
        seen_client_ids.add(client_id)
        output_article = session.get(ScmArticulo, output_id)
        if output_article is None or not output_article.activo:
            raise ScmServiceError(
                "PACKAGING_OUTPUT_NOT_ACTIVE",
                "Una salida de ruta no existe o esta inactiva.",
                status_code=422,
                details={"articulo_ref": output_id},
            )
        resolved_item = {
            "client_id": client_id,
            "articulo_ref": output_id,
        }
        profile = _packaging_profile_entity(
            session,
            output_article,
            actor,
            _json_object(
                item.get("perfil_empacable"),
                field=f"data.empaques[{index}].perfil_empacable",
                required=True,
            ),
            resolved=resolved_item,
            result=result,
            checkpoint=checkpoint,
        )
        rule = _packaging_rule_entity(
            session,
            onboarding,
            actor,
            application_key,
            profile,
            _json_object(
                item.get("regla_empaque"),
                field=f"data.empaques[{index}].regla_empaque",
                required=True,
            ),
            resolved=resolved_item,
            result=result,
            checkpoint=checkpoint,
        )
        resolved_item["estado"] = rule.estado
        resolved_item["regla_estado"] = rule.estado
        resolved_item.setdefault("perfil_estado", "ACTIVO")
        resolved_packagings.append(resolved_item)
    resolved["empaques"] = resolved_packagings
    checkpoint(result)
    return result


def _store_application_state(
    onboarding,
    *,
    actor,
    code,
    key,
    request_hash,
    step_payload,
    result,
    journal_status,
    step_state,
    supersedes_key=None,
):
    draft = _step_data(onboarding)
    states = _step_states(onboarding)
    data_changed = draft[code] != step_payload
    draft[code] = step_payload
    states[code] = step_state
    invalidated = []
    if data_changed:
        for descendant in ONBOARDING_STEPS[
            ONBOARDING_STEPS.index(code) + 1:
        ]:
            if states[descendant] != "PENDIENTE" or draft[descendant]:
                states[descendant] = "INVALIDADO"
                invalidated.append(descendant)

    references = copy.deepcopy(onboarding.referencias_json or {})
    references[code] = copy.deepcopy(result["resolved_references"])
    if code == "IDENTIDAD":
        product_id = result["resolved_references"][
            "producto_terminado_id"
        ]
        references[code]["producto_ref"] = product_id
        onboarding.producto_terminado_id = product_id
        _refresh_placeholder_title(onboarding, step_payload)

    journal = copy.deepcopy(onboarding.application_journal_json or {})
    if supersedes_key is not None:
        superseded = journal.setdefault(code, {}).get(supersedes_key)
        if isinstance(superseded, dict):
            superseded["superseded_by"] = key
            superseded["superseded_at"] = _isoformat(utc_now())
    journal.setdefault(code, {})[key] = {
        "request_sha256": request_hash,
        "request_data": copy.deepcopy(step_payload),
        "status": journal_status,
        "result": copy.deepcopy(result),
        "session_version": onboarding.version + 1,
        "recorded_at": _isoformat(utc_now()),
        "supersedes_application_key": supersedes_key,
    }
    onboarding.application_journal_json = journal
    onboarding.borrador_json = draft
    onboarding.estados_paso_json = states
    onboarding.referencias_json = references
    sources = copy.deepcopy(onboarding.fuentes_json or {})
    extracted_sources = _extract_sources(step_payload)
    if extracted_sources:
        sources[code] = extracted_sources
    onboarding.fuentes_json = sources
    onboarding.invalidated_steps_json = invalidated
    onboarding.paso_actual = code
    onboarding.estado = "BORRADOR"
    onboarding.actualizada_por_id = actor.id
    onboarding.updated_at = utc_now()
    onboarding.version += 1
    _refresh_readiness(onboarding, actor)


def apply_onboarding_step(
    session,
    *,
    actor_id,
    session_id,
    step_code,
    operation_id,
    data,
):
    """Apply one materialization command and journal its canonical refs."""

    try:
        actor = _load_actor(session, actor_id)
        code = _step_code(step_code)
        if code not in APPLICATION_STEPS:
            raise ScmServiceError(
                "STEP_NOT_APPLICABLE",
                "El paso no tiene un comando de materializacion disponible.",
                status_code=422,
            )
        reject_unknown_fields(data, allowed=APPLICATION_FIELDS)
        step_payload = _json_object(
            data.get("data"), field="data", required=True
        )
        key = _application_key(data.get("application_key"))
        raw_supersedes = data.get("supersedes_application_key")
        supersedes_key = (
            _application_key(raw_supersedes)
            if raw_supersedes not in (None, "") else None
        )
        if supersedes_key is not None and code not in {
            "COMPONENTES",
            "COLORES",
            "ESTRUCTURA",
            "RUTA_EMPAQUE",
        }:
            raise ScmServiceError(
                "SUPERSEDE_NOT_SUPPORTED",
                "El paso no admite reaplicacion explicita.",
                status_code=422,
            )
        if supersedes_key == key:
            raise ScmServiceError(
                "SUPERSEDE_KEY_CONFLICT",
                "La nueva application_key debe diferir de la sustituida.",
                status_code=409,
            )
        endpoint = (
            f"/api/scm/v1/altas-producto/{session_id}/pasos/{code}/aplicar:POST"
        )
        operation, replay = _reserve_operation(
            session,
            operation_id,
            endpoint,
            actor,
            data,
        )
        if replay is not None:
            return replay
        onboarding = _load_session(
            session,
            session_id,
            actor=actor,
            for_update=True,
        )
        _ensure_mutable(onboarding)
        request_hash = _json_hash(step_payload)
        journal = copy.deepcopy(onboarding.application_journal_json or {})
        step_journal = journal.setdefault(code, {})
        if key in step_journal:
            previous = _existing_step_application(
                step_journal,
                key=key,
                request_hash=request_hash,
            )
        elif supersedes_key is not None:
            superseded = step_journal.get(supersedes_key)
            current_application = _step_application_summary(
                onboarding, code
            )
            if (
                not isinstance(superseded, dict)
                or superseded.get("status") != "APPLIED"
                or current_application is None
                or current_application.get("application_key")
                != supersedes_key
            ):
                raise ScmServiceError(
                    "SUPERSEDED_APPLICATION_NOT_CURRENT",
                    "La aplicacion sustituida no es la materializacion vigente.",
                    status_code=409,
                    details={
                        "supersedes_application_key": supersedes_key,
                        "current_application_key": (
                            current_application.get("application_key")
                            if current_application else None
                        ),
                    },
                )
            if (
                code == "COLORES"
                and _step_states(onboarding).get("COLORES") != "EN_PROGRESO"
            ):
                raise ScmServiceError(
                    "COLOR_SUPERSEDE_REQUIRES_PENDING_DATA",
                    (
                        "COLORES solo puede sustituirse cuando la fase fue "
                        "reabierta para correccion."
                    ),
                    status_code=422,
                    details={
                        "action": (
                            "Edite el maestro canonico si necesita cambiar una "
                            "fase de colores ya completada."
                        ),
                    },
                )
            previous = None
        else:
            previous = _existing_step_application(
                step_journal,
                key=key,
                request_hash=request_hash,
            )
        if previous is not None:
            previous_hash = previous.get("request_sha256")
            if (
                previous.get("status") == "APPLIED"
                and previous_hash != request_hash
            ):
                raise ScmServiceError(
                    "APPLICATION_KEY_CONFLICT",
                    "application_key ya fue usada con otros datos.",
                    status_code=409,
                )
            if previous.get("status") == "APPLIED":
                response = _application_response(
                    onboarding,
                    previous["result"],
                    replayed=True,
                )
                _complete_operation(operation, response, 200)
                session.commit()
                return response
            if previous_hash != request_hash:
                _assert_checkpointed_retry_compatible(
                    code,
                    previous_payload=(
                        previous.get("request_data")
                        or _step_data(onboarding)[code]
                    ),
                    new_payload=step_payload,
                    result=previous.get("result") or {},
                )
        _ensure_version(onboarding, data.get("expected_version"))
        _require_application_prerequisite(onboarding, code=code)
        before = serialize_onboarding(onboarding)
        if previous is not None:
            result = copy.deepcopy(previous.get("result") or {})
            result.pop("errors", None)
        else:
            result = {
                "created": [],
                "reused": [],
                "pending": [],
                "resolved_references": {},
            }
        checkpointed = False

        def checkpoint(current_result):
            nonlocal checkpointed
            checkpointed = True

        try:
            if code == "IDENTIDAD":
                result = _apply_identity(session, onboarding, step_payload)
            elif code == "COMPONENTES":
                result = _apply_components(
                    session,
                    onboarding,
                    step_payload,
                    result=result,
                    checkpoint=checkpoint,
                )
            elif code == "COLORES":
                result = _apply_colors(
                    session,
                    onboarding,
                    step_payload,
                    result=result,
                    checkpoint=checkpoint,
                )
            elif code == "ESTRUCTURA":
                result = _apply_structure(
                    session,
                    onboarding,
                    actor,
                    key,
                    step_payload,
                    result=result,
                    checkpoint=checkpoint,
                )
            else:
                result = _apply_route_packaging(
                    session,
                    onboarding,
                    actor,
                    key,
                    step_payload,
                    result=result,
                    checkpoint=checkpoint,
                )
        except ScmServiceError as error:
            if code in {"ESTRUCTURA", "RUTA_EMPAQUE"}:
                # These steps are an atomic application unit.  Their
                # canonical services run with commit=False, so the outer
                # handler rolls every sub-mutation and operation back.
                raise
            if not checkpointed:
                raise
            result.update({
                "application_key": key,
                "paso": code,
                "status": "PARTIAL",
                "errors": [{
                    "unit": code,
                    "code": error.code,
                    "message": error.message,
                }],
            })
            _store_application_state(
                onboarding,
                actor=actor,
                code=code,
                key=key,
                request_hash=request_hash,
                step_payload=step_payload,
                result=result,
                journal_status="PARTIAL",
                step_state="EN_PROGRESO",
            )
            session.flush()
            current = _application_response(onboarding, result)
            partial = ScmServiceError(
                "ONBOARDING_APPLICATION_PARTIAL",
                "La aplicacion quedo parcial y puede reanudarse.",
                status_code=422,
                details={
                    "current_session": current,
                    "application_results": copy.deepcopy(result),
                },
            )
            session.add(_event(
                onboarding,
                actor,
                "PASO_ALTA_PRODUCTO_APLICACION_PARCIAL",
                operation,
                before=before,
                after=current,
            ))
            _complete_operation(operation, partial.to_dict(), 422)
            session.commit()
            raise partial

        result.update({
            "application_key": key,
            "paso": code,
            "status": "APPLIED",
        })
        _store_application_state(
            onboarding,
            actor=actor,
            code=code,
            key=key,
            request_hash=request_hash,
            step_payload=step_payload,
            result=result,
            journal_status="APPLIED",
            step_state=(
                "COMPLETADO"
                if code in {"ESTRUCTURA", "RUTA_EMPAQUE"}
                else "EN_PROGRESO"
                if result.get("pending")
                else "COMPLETADO"
            ),
            supersedes_key=supersedes_key,
        )
        session.flush()
        response = _application_response(onboarding, result)
        session.add(_event(
            onboarding,
            actor,
            "PASO_ALTA_PRODUCTO_APLICADO",
            operation,
            before=before,
            after=response,
        ))
        _complete_operation(operation, response, 200)
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ONBOARDING_APPLICATION_CONFLICT",
            "La aplicacion encontro un conflicto de integridad.",
            status_code=409,
        ) from error


def _cleanup_uncommitted_image_object(storage, stored_key, entity):
    """Remove only a new S3 object that no committed row references."""

    if storage is None or not stored_key or entity is None:
        return
    try:
        persisted_key = getattr(entity, "imagen_storage_key", None)
    except Exception:
        # If persistence cannot be verified, retain the immutable object for
        # later GC instead of risking a database pointer to a deleted object.
        current_app.logger.warning(
            "No se pudo verificar metadata tras fallar commit de imagen %s",
            stored_key,
            exc_info=True,
        )
        return
    if persisted_key == stored_key:
        return
    try:
        storage.delete_key(stored_key)
    except CatalogImageStorageError:
        current_app.logger.warning(
            "No se pudo limpiar el objeto de imagen no confirmado %s",
            stored_key,
            exc_info=True,
        )


def apply_onboarding_image(
    session,
    *,
    actor_id,
    session_id,
    entity_type,
    entity_id,
    operation_id,
    data,
    mime_type,
    content,
):
    """Attach a catalog image owned by the session without JSON/base64."""

    image_storage = None
    stored_key = None
    previous_key = None
    entity = None
    normalized_type = str(entity_type or "").strip().upper()
    if normalized_type not in {"PRODUCTO_TERMINADO", "PIEZA_COLOR"}:
        raise ScmServiceError(
            "INVALID_IMAGE_ENTITY_TYPE",
            "La imagen solo puede asociarse a PT o PiezaColor.",
            status_code=404,
        )
    try:
        actor = _load_actor(session, actor_id)
        reject_unknown_fields(
            data,
            allowed={"expected_version", "application_key"},
        )
        key = _application_key(data.get("application_key"))
        normalized_id = required_text(
            entity_id,
            field="entity_id",
            max_length=100,
        )
        digest = hashlib.sha256(content).hexdigest()
        request_fingerprint = {
            "entity_type": normalized_type,
            "entity_id": normalized_id,
            "mime_type": str(mime_type).lower(),
            "size_bytes": len(content),
            "sha256": digest,
            "application_key": key,
        }
        operation, replay = _reserve_operation(
            session,
            operation_id,
            (
                f"/api/scm/v1/altas-producto/{session_id}/imagenes/"
                f"{normalized_type}/{normalized_id}:POST"
            ),
            actor,
            request_fingerprint,
        )
        if replay is not None:
            return replay
        onboarding = _load_session(
            session,
            session_id,
            actor=actor,
            for_update=True,
        )
        _ensure_mutable(onboarding)
        journal = copy.deepcopy(onboarding.application_journal_json or {})
        image_journal = journal.setdefault("IMAGENES", {})
        request_hash = _json_hash(request_fingerprint)
        previous = image_journal.get(key)
        if previous is not None:
            if previous.get("request_sha256") != request_hash:
                raise ScmServiceError(
                    "APPLICATION_KEY_CONFLICT",
                    "application_key ya fue usada con otra imagen.",
                    status_code=409,
                )
            if previous.get("status") == "APPLIED":
                response = serialize_onboarding(onboarding)
                result = copy.deepcopy(previous.get("result") or {})
                result["status"] = "REPLAYED"
                response["image_results"] = result
                _complete_operation(operation, response, 200)
                session.commit()
                return response

        _ensure_version(onboarding, data.get("expected_version"))
        references = onboarding.referencias_json or {}
        if normalized_type == "PRODUCTO_TERMINADO":
            identity_refs = references.get("IDENTIDAD") or {}
            referenced_id = (
                identity_refs.get("producto_terminado_id")
                or identity_refs.get("producto_ref")
            )
            if (
                onboarding.producto_terminado_id != normalized_id
                or str(referenced_id or "") != normalized_id
            ):
                raise ScmServiceError(
                    "IMAGE_ENTITY_OUT_OF_SCOPE",
                    "El PT no pertenece a las referencias de la sesion.",
                    status_code=404,
                )
            entity = session.get(ProductoTerminado, normalized_id)
            category = "producto-terminado"
            image_url = f"/api/productos/{normalized_id}/imagen"
        else:
            allowed_variants = {
                str(item.get("pieza_color_ref"))
                for item in (
                    (references.get("COLORES") or {}).get("matriz") or []
                )
                if isinstance(item, dict)
                and item.get("pieza_color_ref") not in (None, "")
            }
            if normalized_id not in allowed_variants:
                raise ScmServiceError(
                    "IMAGE_ENTITY_OUT_OF_SCOPE",
                    "La PiezaColor no pertenece a las referencias de la sesion.",
                    status_code=404,
                )
            entity = session.get(PiezaColor, normalized_id)
            category = "pieza-color"
            image_url = f"/api/piezas-color/{normalized_id}/imagen"
        if entity is None:
            raise ScmServiceError(
                "IMAGE_ENTITY_NOT_FOUND",
                "La entidad de imagen no existe.",
                status_code=404,
            )
        try:
            validate_catalog_image_content(mime_type, content)
        except CatalogImageValidationError as error:
            raise ScmServiceError(
                error.code,
                error.message,
                status_code=error.status_code,
            ) from error

        before = serialize_onboarding(onboarding)
        image_storage = get_catalog_image_storage()
        previous_key = getattr(entity, "imagen_storage_key", None)
        try:
            stored_key = image_storage.store(
                entity,
                category=category,
                identity=normalized_id,
                mime_type=str(mime_type).lower(),
                content=content,
            )
        except CatalogImageStorageError as error:
            raise ScmServiceError(
                "IMAGEN_STORAGE_NO_DISPONIBLE",
                str(error),
                status_code=503,
            ) from error
        result = {
            "status": "APPLIED",
            "application_key": key,
            "entity_type": normalized_type,
            "entity_id": normalized_id,
            "mime_type": str(mime_type).lower(),
            "size_bytes": len(content),
            "sha256": digest,
            "imagen_url": image_url,
        }
        image_journal[key] = {
            "request_sha256": request_hash,
            "status": "APPLIED",
            "result": copy.deepcopy(result),
            "session_version": onboarding.version + 1,
            "recorded_at": _isoformat(utc_now()),
        }
        onboarding.application_journal_json = journal
        onboarding.version += 1
        onboarding.actualizada_por_id = actor.id
        onboarding.updated_at = utc_now()
        onboarding.estado = "BORRADOR"
        _refresh_readiness(onboarding, actor)
        session.flush()
        response = serialize_onboarding(onboarding)
        response["image_results"] = result
        session.add(_event(
            onboarding,
            actor,
            "IMAGEN_ALTA_PRODUCTO_ASOCIADA",
            operation,
            before=before,
            after=response,
        ))
        _complete_operation(operation, response, 200)
        session.commit()
        if previous_key and previous_key != stored_key:
            try:
                image_storage.delete_key(previous_key)
            except CatalogImageStorageError:
                current_app.logger.warning(
                    "No se pudo limpiar la imagen reemplazada %s",
                    previous_key,
                    exc_info=True,
                )
        return response
    except ScmServiceError:
        session.rollback()
        _cleanup_uncommitted_image_object(
            image_storage,
            stored_key,
            entity,
        )
        raise
    except IntegrityError as error:
        session.rollback()
        _cleanup_uncommitted_image_object(
            image_storage,
            stored_key,
            entity,
        )
        raise ScmServiceError(
            "ONBOARDING_IMAGE_CONFLICT",
            "No se pudo asociar la imagen por un conflicto de integridad.",
            status_code=409,
        ) from error
    except Exception as error:
        session.rollback()
        _cleanup_uncommitted_image_object(
            image_storage,
            stored_key,
            entity,
        )
        raise ScmServiceError(
            "ONBOARDING_IMAGE_CONFLICT",
            "No se pudo confirmar la asociacion de la imagen.",
            status_code=503,
        ) from error


def validate_onboarding_session(
    session, *, actor_id, session_id, operation_id=None, data
):
    try:
        actor = _load_actor(session, actor_id)
        reject_unknown_fields(data, allowed=VERSION_COMMAND_FIELDS)
        operation, replay = _reserve_operation(
            session,
            operation_id,
            f"/api/scm/v1/altas-producto/{session_id}/validar:POST",
            actor,
            data,
        )
        if replay is not None:
            return replay
        onboarding = _load_session(
            session,
            session_id,
            actor=actor,
            for_update=True,
        )
        _ensure_version(onboarding, data.get("expected_version"))
        _ensure_mutable(onboarding)
        before = serialize_onboarding(onboarding)
        onboarding.version += 1
        onboarding.actualizada_por_id = actor.id
        onboarding.updated_at = utc_now()
        readiness = _refresh_readiness(onboarding, actor)
        onboarding.estado = (
            "LISTA_PARA_PUBLICAR"
            if readiness["lista_para_finalizar"]
            else "CON_BLOQUEOS"
        )
        session.flush()
        response = serialize_onboarding(onboarding)
        session.add(_event(
            onboarding,
            actor,
            "ALTA_PRODUCTO_VALIDADA",
            operation,
            before=before,
            after=response,
        ))
        _complete_operation(operation, response, 200)
        session.commit()
        return response
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ONBOARDING_SESSION_CONFLICT",
            "No se pudo validar la sesion.",
            status_code=409,
        ) from error


def finalize_onboarding_session(
    session, *, actor_id, session_id, operation_id=None, data
):
    blocked_error = None
    response = None
    try:
        actor = _load_actor(session, actor_id)
        reject_unknown_fields(data, allowed=VERSION_COMMAND_FIELDS)
        operation, replay = _reserve_operation(
            session,
            operation_id,
            f"/api/scm/v1/altas-producto/{session_id}/finalizar:POST",
            actor,
            data,
        )
        if replay is not None:
            return replay
        onboarding = _load_session(
            session,
            session_id,
            actor=actor,
            for_update=True,
        )
        _ensure_version(onboarding, data.get("expected_version"))
        _ensure_mutable(onboarding)
        before = serialize_onboarding(onboarding)
        onboarding.version += 1
        onboarding.actualizada_por_id = actor.id
        onboarding.updated_at = utc_now()
        readiness = _refresh_readiness(onboarding, actor)
        if readiness["lista_para_finalizar"]:
            onboarding.estado = "FINALIZADA"
            onboarding.finalizada_at = utc_now()
            event_type = "ALTA_PRODUCTO_FINALIZADA"
        else:
            onboarding.estado = "CON_BLOQUEOS"
            event_type = "ALTA_PRODUCTO_FINALIZACION_BLOQUEADA"
        session.flush()
        response = serialize_onboarding(onboarding)
        session.add(_event(
            onboarding,
            actor,
            event_type,
            operation,
            before=before,
            after=response,
        ))
        if readiness["lista_para_finalizar"]:
            _complete_operation(operation, response, 200)
        else:
            blocked_error = ScmServiceError(
                "SESSION_NOT_READY",
                "La sesion conserva bloqueos antes de finalizar.",
                status_code=422,
                details={"current_session": response},
            )
            _complete_operation(operation, blocked_error.to_dict(), 422)
        session.commit()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ONBOARDING_SESSION_CONFLICT",
            "No se pudo finalizar la sesion.",
            status_code=409,
        ) from error

    if blocked_error is not None:
        raise blocked_error
    return response
