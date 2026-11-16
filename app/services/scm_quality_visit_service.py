"""Central shared drafts and confirmations for OT quality visits."""

from datetime import datetime, timezone
import math
from urllib.parse import unquote
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.models.registro import RegistroDiarioProduccion
from app.models.scm_ot import ScmTrabajoOt
from app.models.scm_ot import ScmManga, ScmTramoMangaTrabajo
from app.models.scm_production_orders import ScmOrdenOperacionSalida
from app.models.scm_quality import ScmVisitaCalidad
from app.services.scm_ot_service import (
    _complete_operation,
    _event,
    _QUALITY_QUOTA_MISSING,
    _load_quality_read_actor,
    _reserve_operation,
    _quality_periodic_quota,
)
from app.services.scm_service_support import (
    ScmServiceError,
    expected_version,
    load_actor,
    load_actor_any,
    reject_unknown_fields,
    required_text,
)


def _parse_uuid(value, *, field):
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "INVALID_UUID", f"{field} debe ser un UUID valido.", status_code=400,
            details={"field": field},
        ) from error


def _parse_datetime(value):
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_DATETIME", "observed_at debe ser una fecha ISO valida.", status_code=400,
            details={"field": "observed_at"},
        ) from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_quality_context(session, *, actor_id, public_id, trabajo_id=None):
    # Read visibility is bounded to the quality capability or the pre-existing
    # pair of OT/OF readers; it never grants either capability.
    _load_quality_read_actor(session, actor_id)
    ot = session.scalar(
        select(RegistroDiarioProduccion).where(
            RegistroDiarioProduccion.public_id == public_id,
            RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
        )
    )
    if ot is None:
        raise ScmServiceError("OT_NOT_FOUND", "La OT SCM no existe.", status_code=404)
    if trabajo_id is None:
        return ot, None
    work = session.scalar(
        select(ScmTrabajoOt).where(
            ScmTrabajoOt.id == trabajo_id,
            ScmTrabajoOt.orden_trabajo_id == ot.id,
            ScmTrabajoOt.tipo == "COLOR",
        )
    )
    if work is None:
        raise ScmServiceError(
            "QUALITY_WORK_NOT_FOUND", "El trabajo no pertenece a la OT.", status_code=404,
        )
    return ot, work


def _require_register_actor(session, actor_id):
    return load_actor(session, actor_id, capability="CALIDAD_CONTROL_REGISTRAR")


def _initial_cutoff(ot):
    if ot.fecha is None or ot.turno not in {"DIA", "NOCHE"}:
        return None
    from zoneinfo import ZoneInfo

    cutoff_hour = 8 if ot.turno == "DIA" else 20
    return datetime(
        ot.fecha.year, ot.fecha.month, ot.fecha.day,
        cutoff_hour, 0, tzinfo=ZoneInfo("America/Lima"),
    )


def _initial_window_closed(ot, *, now=None):
    cutoff = _initial_cutoff(ot)
    if cutoff is None:
        return False
    from zoneinfo import ZoneInfo

    current = now or datetime.now(ZoneInfo("America/Lima"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("America/Lima"))
    return current >= cutoff


def _serialize_visit(visit):
    payload = visit.to_dict()
    payload["created_by_id"] = visit.created_by_id
    payload["updated_by_id"] = visit.updated_by_id
    payload["confirmed_by_id"] = visit.confirmed_by_id
    return payload


def _payload_piece_view(payload, work):
    piece_keys = payload.get("pieceKeys", payload.get("piece_keys"))
    checks = payload.get("checks") or payload.get("checksByPiece") or {}
    if isinstance(piece_keys, list) and piece_keys:
        return piece_keys, checks if isinstance(checks, dict) else {}
    output_ids = payload.get("outputIds", payload.get("output_ids", []))
    ot_public = getattr(getattr(work, "orden_trabajo", None), "public_id", None)
    if not isinstance(output_ids, list) or not output_ids or ot_public is None:
        return piece_keys or [], checks if isinstance(checks, dict) else {}
    output_checks = checks.get("outputChecks", {}) if isinstance(checks, dict) else {}
    common_checks = checks.get("commonChecks", {}) if isinstance(checks, dict) else {}
    derived_keys = [f"{ot_public}:{work.id}:{output_id}" for output_id in output_ids]
    derived_checks = {}
    for output_id, piece_key in zip(output_ids, derived_keys):
        derived_checks[piece_key] = {
            "peso": output_checks.get(str(output_id), output_checks.get(output_id, {})),
            **{key: value for key, value in common_checks.items() if key != "affectedMangas"},
        }
    return derived_keys, derived_checks


def _validate_finite_measurements(payload):
    checks = payload.get("checks") or {}
    if not isinstance(checks, dict):
        return
    output_checks = checks.get("outputChecks") or {}
    if isinstance(output_checks, dict):
        for row in output_checks.values():
            if not isinstance(row, dict):
                continue
            for field in ("netGrams", "quantity"):
                if field not in row or row[field] in (None, ""):
                    continue
                if isinstance(row[field], bool):
                    raise ScmServiceError("QUALITY_VISIT_MEASUREMENT_INVALID", "Las mediciones deben ser numericas y finitas.", status_code=422)
                try:
                    parsed = float(row[field])
                except (TypeError, ValueError) as error:
                    raise ScmServiceError("QUALITY_VISIT_MEASUREMENT_INVALID", "Las mediciones deben ser numericas y finitas.", status_code=422) from error
                if not math.isfinite(parsed):
                    raise ScmServiceError("QUALITY_VISIT_MEASUREMENT_INVALID", "Las mediciones deben ser numericas y finitas.", status_code=422)
    common = checks.get("commonChecks") or {}
    cycle = common.get("ciclo") if isinstance(common, dict) else None
    if isinstance(cycle, dict) and cycle.get("seconds") not in (None, ""):
        try:
            parsed = float(cycle["seconds"])
        except (TypeError, ValueError) as error:
            raise ScmServiceError("QUALITY_VISIT_MEASUREMENT_INVALID", "Los segundos deben ser numericos y finitos.", status_code=422) from error
        if not math.isfinite(parsed):
            raise ScmServiceError("QUALITY_VISIT_MEASUREMENT_INVALID", "Los segundos deben ser numericos y finitos.", status_code=422)


def _validate_payload_scope(session, *, payload, work):
    _validate_finite_measurements(payload)
    piece_keys, piece_checks = _payload_piece_view(payload, work)
    if not isinstance(piece_keys, list) or not piece_keys or any(
        not isinstance(value, str) or not value.strip() for value in piece_keys
    ):
        raise ScmServiceError(
            "QUALITY_VISIT_PIECES_REQUIRED",
            "La visita debe incluir al menos una salida del trabajo.",
            status_code=422,
        )
    if len(set(piece_keys)) != len(piece_keys):
        raise ScmServiceError("QUALITY_VISIT_PIECES_DUPLICATED", "Las salidas no pueden repetirse.", status_code=422)
    checks = piece_checks
    if not isinstance(checks, dict) or any(key not in piece_keys for key in checks):
        raise ScmServiceError("QUALITY_VISIT_PIECES_OUT_OF_SCOPE", "Las lecturas deben pertenecer a las salidas seleccionadas.", status_code=422)
    work_marker = str(work.id)
    if any(
        len(key.split(":")) < 3 or key.split(":")[1] != work_marker
        for key in piece_keys
    ):
        raise ScmServiceError("QUALITY_VISIT_PIECES_OUT_OF_SCOPE", "Una salida no pertenece al trabajo observado.", status_code=422)
    output_values = payload.get(
        "output_ids", payload.get("salida_ids", payload.get("outputIds", []))
    )
    if output_values:
        if isinstance(payload.get("checks"), dict) and isinstance(payload["checks"].get("outputChecks"), dict):
            output_keys = {str(key) for key in payload["checks"]["outputChecks"]}
            if not output_keys.issubset({str(value) for value in output_values}):
                raise ScmServiceError("QUALITY_VISIT_SCOPE_INVALID", "Una lectura de salida queda fuera de las salidas revisadas.", status_code=422)
        expected_output_tokens = {str(value) for value in output_values}
        actual_output_tokens = {unquote(key.split(":", 2)[2]) for key in piece_keys}
        if actual_output_tokens != expected_output_tokens:
            raise ScmServiceError(
                "QUALITY_VISIT_PIECES_OUT_OF_SCOPE",
                "Las piezas declaradas no coinciden con las salidas del trabajo.",
                status_code=422,
            )
    raw_manga_values = payload.get("manga_ids", payload.get("mangas", []))
    if raw_manga_values in (None, ""):
        manga_values = []
    elif not isinstance(raw_manga_values, list):
        raise ScmServiceError("QUALITY_VISIT_MANGAS_INVALID", "mangas debe ser una lista.", status_code=422)
    else:
        manga_values = raw_manga_values
    if manga_values:
        requested = []
        for value in manga_values:
            raw = value.get("id") if isinstance(value, dict) else value
            if raw in (None, ""):
                raise ScmServiceError("QUALITY_VISIT_MANGAS_INVALID", "La manga requiere id.", status_code=422)
            requested.append(str(raw))
        requested_ints = [int(value) for value in requested if value.isdigit()]
        requested_public = []
        for value in requested:
            if value.isdigit():
                continue
            try:
                requested_public.append(_parse_uuid(value, field="manga_ids"))
            except ScmServiceError as error:
                raise ScmServiceError("QUALITY_VISIT_MANGAS_INVALID", "La manga requiere public_id UUID.", status_code=422) from error
        owned = set(session.scalars(select(ScmManga.id).where(
            ScmManga.id.in_(requested_ints), ScmManga.trabajo_ot_id == work.id,
        )).all()) if requested_ints else set()
        owned.update(session.scalars(select(ScmTramoMangaTrabajo.manga_id).where(
            ScmTramoMangaTrabajo.manga_id.in_(requested_ints), ScmTramoMangaTrabajo.trabajo_ot_id == work.id,
        )).all()) if requested_ints else set()
        owned_public = set()
        if requested_public:
            owned_public = {
                str(value) for value in session.scalars(select(ScmManga.public_id).where(
                    ScmManga.public_id.in_(requested_public), ScmManga.trabajo_ot_id == work.id,
                )).all()
            }
            owned_public.update(
                str(value) for value in session.scalars(select(ScmManga.public_id).join(
                    ScmTramoMangaTrabajo, ScmTramoMangaTrabajo.manga_id == ScmManga.id,
                ).where(
                    ScmManga.public_id.in_(requested_public), ScmTramoMangaTrabajo.trabajo_ot_id == work.id,
                )).all()
            )
        if owned != set(requested_ints) or owned_public != {str(value) for value in requested_public}:
            raise ScmServiceError("QUALITY_VISIT_MANGAS_OUT_OF_SCOPE", "Una manga no pertenece al trabajo observado.", status_code=422)
    checks_tree = payload.get("checks") or payload.get("checksByPiece") or {}
    nested_mangas = []
    def collect_affected(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "affectedMangas" and isinstance(child, list):
                    nested_mangas.extend(child)
                else:
                    collect_affected(child)
        elif isinstance(value, list):
            for child in value:
                collect_affected(child)
    collect_affected(checks_tree)
    if output_values:
        selected_outputs = {str(value) for value in output_values}
        for manga in nested_mangas:
            if isinstance(manga, dict) and manga.get("outputId") is not None and str(manga["outputId"]) not in selected_outputs:
                raise ScmServiceError(
                    "QUALITY_VISIT_SCOPE_INVALID",
                    "Una manga afectada apunta a una salida no revisada.",
                    status_code=422,
                )
    if nested_mangas:
        _validate_payload_scope(session, payload={"pieceKeys": piece_keys, "checks": {}, "mangas": nested_mangas}, work=work)
    if output_values:
        if not isinstance(output_values, list):
            raise ScmServiceError("QUALITY_VISIT_OUTPUTS_INVALID", "salida_ids debe ser una lista.", status_code=422)
        output_uuid_ids = []
        output_article_ids = []
        for value in output_values:
            try:
                output_uuid_ids.append(_parse_uuid(value, field="output_ids"))
            except ScmServiceError:
                try:
                    output_article_ids.append(int(value))
                except (TypeError, ValueError) as error:
                    raise ScmServiceError("QUALITY_VISIT_OUTPUTS_INVALID", "salida_ids contiene una identidad no valida.", status_code=422) from error
        output_filters = [ScmOrdenOperacionSalida.orden_operacion_id == work.orden_operacion_id]
        corrida_id = getattr(getattr(work, "trabajo_color", None), "corrida_fabricacion_id", None)
        if corrida_id is not None:
            output_filters.append(ScmOrdenOperacionSalida.corrida_fabricacion_id == corrida_id)
        rows = session.scalars(select(ScmOrdenOperacionSalida).where(
            *output_filters,
            or_(
                ScmOrdenOperacionSalida.id.in_(output_uuid_ids) if output_uuid_ids else False,
                ScmOrdenOperacionSalida.articulo_scm_id.in_(output_article_ids) if output_article_ids else False,
            ),
        )).all()
        allowed = {str(row.id) for row in rows} | {str(row.articulo_scm_id) for row in rows}
        if any(str(value) not in allowed for value in output_values):
            raise ScmServiceError("QUALITY_VISIT_OUTPUTS_OUT_OF_SCOPE", "Una salida no pertenece al trabajo observado.", status_code=422)


def _validate_confirmation_payload(session, *, visit, work):
    payload = visit.payload_json if isinstance(visit.payload_json, dict) else {}
    output_values = payload.get("outputIds", payload.get("output_ids", []))
    if not isinstance(output_values, list) or not output_values:
        raise ScmServiceError(
            "QUALITY_VISIT_OUTPUTS_REQUIRED",
            "La confirmacion requiere salidas verificables del trabajo.",
            status_code=422,
        )
    _validate_payload_scope(session, payload=payload, work=work)
    if not payload.get("observed_at") and not visit.observed_at:
        raise ScmServiceError("QUALITY_VISIT_OBSERVED_AT_REQUIRED", "La visita requiere observed_at.", status_code=422)
    checks = payload.get("checks") or {}
    if isinstance(checks, dict) and isinstance(checks.get("outputChecks"), dict):
        output_ids = payload.get("outputIds", payload.get("output_ids", []))
        for output_id in output_ids:
            row = checks["outputChecks"].get(str(output_id), checks["outputChecks"].get(output_id, {}))
            try:
                if isinstance(row.get("netGrams"), bool) or isinstance(row.get("quantity"), bool):
                    raise ValueError("boolean measurement")
                net = float(row.get("netGrams"))
                quantity = float(row.get("quantity"))
            except (AttributeError, TypeError, ValueError) as error:
                raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "Cada salida requiere neto y cantidad.", status_code=422) from error
            if not math.isfinite(net) or not math.isfinite(quantity) or net <= 0 or quantity <= 0 or quantity != int(quantity) or row.get("classification") not in {"CORRECTO", "ALTO", "BAJO"}:
                raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "Neto, cantidad y clasificacion de salida no son validos.", status_code=422)
        common = checks.get("commonChecks") or {}
        cycle = common.get("ciclo") or {}
        try:
            cycle_seconds = float(cycle.get("seconds"))
        except (TypeError, ValueError) as error:
            raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "El ciclo observado no es valido.", status_code=422) from error
        if not math.isfinite(cycle_seconds) or cycle_seconds <= 0 or cycle.get("classification") not in {"CORRECTO", "LENTO", "RAPIDO"}:
            raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "El ciclo observado no es valido.", status_code=422)
        for family in ("color", "inyeccion", "funcional"):
            row = common.get(family) or {}
            allowed_statuses = {"OK", "INCIDENCIA"} | ({"NA"} if family == "funcional" else set())
            if row.get("status") not in allowed_statuses or not row.get("scopeOutputIds"):
                raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "Cada familia requiere estado y salidas revisadas.", status_code=422)
            selected = {str(value) for value in output_ids}
            scoped = {str(value) for value in row.get("scopeOutputIds", [])}
            if not scoped.issubset(selected):
                raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "El alcance de una familia no coincide con las salidas revisadas.", status_code=422)
            status_by_output = row.get("statusByOutput") or {}
            if not {str(value) for value in status_by_output}.issubset(selected):
                raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "Una lectura de familia apunta fuera de las salidas revisadas.", status_code=422)
        return
    piece_keys, checks = _payload_piece_view(payload, work)
    required_families = {"peso", "ciclo", "color", "inyeccion", "funcional"}
    for piece_key in piece_keys:
        value = checks.get(piece_key)
        if not isinstance(value, dict) or not required_families.issubset(value):
            raise ScmServiceError(
                "QUALITY_VISIT_DATA_INCOMPLETE",
                "Cada salida debe conservar las cinco familias de lectura antes de confirmar.",
                status_code=422,
            )
        statuses = {key: value[key].get("status") for key in required_families if isinstance(value[key], dict)}
        if statuses.get("peso") != "MEDIDO" or statuses.get("ciclo") != "MEDIDO":
            raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "Peso y ciclo deben estar medidos.", status_code=422)
        if statuses.get("color") not in {"OK", "INCIDENCIA"} or statuses.get("inyeccion") not in {"OK", "INCIDENCIA"} or statuses.get("funcional") not in {"OK", "INCIDENCIA", "NA"}:
            raise ScmServiceError("QUALITY_VISIT_DATA_INCOMPLETE", "Las familias de control no están resueltas.", status_code=422)


def _next_visit_number(session, *, ot_id):
    return max(2, 1 + int(session.scalar(select(func.max(ScmVisitaCalidad.numero)).where(
        ScmVisitaCalidad.ot_id == ot_id,
    )) or 0))


def _next_free_visit_number(session, *, ot_id, start):
    candidate = max(2, int(start))
    while session.scalar(select(ScmVisitaCalidad.id).where(
        ScmVisitaCalidad.ot_id == ot_id,
        ScmVisitaCalidad.numero == candidate,
    )) is not None:
        candidate += 1
    return candidate


def _quality_quota_summary(session, *, ot):
    confirmed = int(session.scalar(select(func.count()).where(
        ScmVisitaCalidad.ot_id == ot.id,
        ScmVisitaCalidad.tipo == "PERIODICA",
        ScmVisitaCalidad.estado == "CONFIRMED",
    )) or 0)
    drafts = int(session.scalar(select(func.count()).where(
        ScmVisitaCalidad.ot_id == ot.id,
        ScmVisitaCalidad.tipo == "PERIODICA",
        ScmVisitaCalidad.estado == "DRAFT",
    )) or 0)
    quota = ot.calidad_visitas_periodicas_previstas
    return {
        "periodic_quota": quota,
        "calidad_visitas_periodicas_previstas": quota,
        "periodic_confirmed": confirmed,
        "periodic_drafts": drafts,
        "periodic_remaining": (
            max(0, quota - confirmed) if quota is not None else None
        ),
    }


def list_quality_visits(session, *, actor_id, public_id):
    ot, _ = _load_quality_context(session, actor_id=actor_id, public_id=public_id)
    visits = session.scalars(
        select(ScmVisitaCalidad)
        .options(joinedload(ScmVisitaCalidad.trabajo))
        .where(ScmVisitaCalidad.ot_id == ot.id)
        .order_by(ScmVisitaCalidad.tipo, ScmVisitaCalidad.numero, ScmVisitaCalidad.recorded_at)
    ).all()
    quota_summary = _quality_quota_summary(session, ot=ot)
    machine = ot.maquina
    return {
        "items": [_serialize_visit(item) for item in visits],
        "next_periodic_number": _next_visit_number(session, ot_id=ot.id),
        **quota_summary,
        "availability": {
            "maquina_id": ot.maquina_id,
            "maquina_codigo": ot.maquina_codigo_snapshot or (
                machine.codigo if machine is not None else None
            ),
            "maquina_nombre": ot.maquina_nombre_snapshot or (
                machine.nombre if machine is not None else None
            ),
            "maquina_activa": machine.activo if machine is not None else None,
            "maquina_estado": machine.estado if machine is not None else None,
            "fuente": "OT_CENTRAL",
            "fecha_operativa": ot.fecha.isoformat() if ot.fecha else None,
            "turno": ot.turno,
        },
    }


def update_quality_configuration(
    session, *, actor_id, public_id, operation_id, data
):
    """Edit only the Quality quota through the existing planning capability."""
    actor = load_actor_any(
        session, actor_id, capabilities=("OT_CREAR", "OPERACION_PLANIFICAR")
    )
    reject_unknown_fields(
        data,
        allowed={
            "version",
            "calidad_visitas_periodicas_previstas",
            "visitas_periodicas_previstas",
            "periodic_quota",
        },
    )
    operation, replay = _reserve_operation(
        session,
        operation_id,
        f"/ots/{public_id}/calidad-configuracion",
        actor,
        data,
    )
    if replay is not None:
        return replay
    try:
        ot = session.scalar(select(RegistroDiarioProduccion).where(
            RegistroDiarioProduccion.public_id == public_id,
            RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
        ).with_for_update())
        if ot is None:
            raise ScmServiceError("OT_NOT_FOUND", "La OT SCM no existe.", status_code=404)
        if ot.estado not in {"PLANIFICADA", "EN_EJECUCION"}:
            raise ScmServiceError(
                "QUALITY_PERIODIC_QUOTA_STATE_INVALID",
                "La cuota de Calidad solo se puede configurar en una OT planificada o en ejecución.",
                status_code=409,
            )
        if "version" not in data or expected_version(data.get("version")) != ot.version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La OT cambio. Actualiza antes de editar la configuración.",
                status_code=409,
            )
        quota = _quality_periodic_quota(data)
        if quota is _QUALITY_QUOTA_MISSING:
            raise ScmServiceError(
                "QUALITY_PERIODIC_QUOTA_REQUIRED",
                "La configuración debe indicar la cuota de visitas periódicas o null.",
                status_code=400,
            )
        confirmed = int(session.scalar(select(func.count()).where(
            ScmVisitaCalidad.ot_id == ot.id,
            ScmVisitaCalidad.tipo == "PERIODICA",
            ScmVisitaCalidad.estado == "CONFIRMED",
        )) or 0)
        if quota is not None and quota < confirmed:
            raise ScmServiceError(
                "QUALITY_PERIODIC_QUOTA_BELOW_CONFIRMED",
                "La cuota no puede ser menor que las visitas periódicas confirmadas.",
                status_code=409,
                details={"periodic_confirmed": confirmed},
            )
        ot.calidad_visitas_periodicas_previstas = quota
        ot.version += 1
        session.flush()
        summary = _quality_quota_summary(session, ot=ot)
        response = {
            "ot": {
                "public_id": str(ot.public_id),
                "codigo_ot": ot.codigo_ot,
                "version": ot.version,
                "calidad_visitas_periodicas_previstas": quota,
            },
            **summary,
        }
        session.add(_event(
            "ORDEN_TRABAJO", ot.id, "QUALITY_PERIODIC_QUOTA_UPDATED",
            actor, operation, response,
        ))
        _complete_operation(operation, response, status=200)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def save_quality_visit_draft(session, *, actor_id, public_id, operation_id, data):
    actor = _require_register_actor(session, actor_id)
    # Read visibility is part of the write contract, including idempotent
    # replays; never reveal a previous response after OF/OT access is revoked.
    reject_unknown_fields(
        data,
        allowed={"id", "trabajo_id", "tipo", "numero", "version", "observed_at", "payload"},
    )
    operation, replay = _reserve_operation(
        session, operation_id, f"/ots/{public_id}/calidad-visitas", actor, data,
    )
    if replay is not None:
        return replay
    try:
        trabajo_id = _parse_uuid(data.get("trabajo_id"), field="trabajo_id")
        ot, work = _load_quality_context(
            session, actor_id=actor_id, public_id=public_id, trabajo_id=trabajo_id,
        )
        ot = session.scalar(select(RegistroDiarioProduccion).where(
            RegistroDiarioProduccion.id == ot.id,
        ).with_for_update())
        tipo = required_text(data.get("tipo"), field="tipo", max_length=16).upper()
        if tipo not in {"INICIAL", "PERIODICA"}:
            raise ScmServiceError("QUALITY_VISIT_TYPE_INVALID", "tipo debe ser INICIAL o PERIODICA.", status_code=400)
        numero = data.get("numero")
        if numero is None:
            numero = 1 if tipo == "INICIAL" else _next_visit_number(session, ot_id=ot.id)
        if not isinstance(numero, int) or isinstance(numero, bool) or numero <= 0:
            raise ScmServiceError("QUALITY_VISIT_NUMBER_INVALID", "numero debe ser entero positivo.", status_code=400)
        if tipo == "INICIAL" and numero != 1:
            raise ScmServiceError("QUALITY_INITIAL_NUMBER_INVALID", "El arranque usa la ranura 1.", status_code=400)
        if tipo == "PERIODICA" and numero < 2:
            raise ScmServiceError("QUALITY_PERIODIC_NUMBER_INVALID", "La primera visita periodica usa la ranura 2.", status_code=400)
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ScmServiceError("QUALITY_VISIT_PAYLOAD_REQUIRED", "payload debe ser un objeto.", status_code=400)
        _validate_payload_scope(session, payload=payload, work=work)
        visit = None
        if data.get("id"):
            visit = session.scalar(select(ScmVisitaCalidad).where(
                ScmVisitaCalidad.id == _parse_uuid(data["id"], field="id"),
            ).with_for_update())
            if visit is not None and visit.ot_id != ot.id:
                raise ScmServiceError("QUALITY_VISIT_NOT_FOUND", "La visita no pertenece a la OT.", status_code=404)
            if visit is not None and (
                visit.trabajo_ot_id != work.id or visit.tipo != tipo or visit.numero != numero
            ):
                raise ScmServiceError("QUALITY_VISIT_CONTEXT_IMMUTABLE", "El contexto de la visita no se puede cambiar.", status_code=409)
        if visit is None:
            visit = session.scalar(select(ScmVisitaCalidad).where(
                ScmVisitaCalidad.ot_id == ot.id,
                ScmVisitaCalidad.trabajo_ot_id == work.id,
                ScmVisitaCalidad.tipo == tipo,
                ScmVisitaCalidad.numero == numero,
            ).with_for_update())
        if visit is None and tipo == "INICIAL":
            owner = session.scalar(select(ScmVisitaCalidad).where(
                ScmVisitaCalidad.ot_id == ot.id,
                ScmVisitaCalidad.tipo == "INICIAL",
            ))
            if owner is not None and owner.trabajo_ot_id != work.id:
                raise ScmServiceError(
                    "QUALITY_INITIAL_SLOT_RESERVED",
                    "El control de arranque ya esta reservado por otro trabajo de la OT.",
                    status_code=409,
                    details={
                        "trabajo_id": str(owner.trabajo_ot_id),
                        "tipo": owner.tipo,
                        "numero": owner.numero,
                        "estado": owner.estado,
                    },
                )
        if visit is None and tipo == "PERIODICA":
            if session.scalar(select(ScmVisitaCalidad.id).where(
                ScmVisitaCalidad.ot_id == ot.id,
                ScmVisitaCalidad.numero == numero,
            )) is not None:
                numero = _next_free_visit_number(
                    session, ot_id=ot.id, start=numero + 1,
                )
                visit = session.scalar(select(ScmVisitaCalidad).where(
                    ScmVisitaCalidad.ot_id == ot.id,
                    ScmVisitaCalidad.trabajo_ot_id == work.id,
                    ScmVisitaCalidad.tipo == tipo,
                    ScmVisitaCalidad.numero == numero,
                ).with_for_update())
        if visit is not None:
            if visit.estado == "CONFIRMED":
                raise ScmServiceError("QUALITY_VISIT_CONFIRMED", "La visita confirmada no admite cambios de borrador.", status_code=409)
            if "version" not in data or expected_version(data.get("version")) != visit.version:
                raise ScmServiceError("VERSION_CONFLICT", "La visita cambio. Actualiza antes de guardar.", status_code=409)
            visit.version += 1
            visit.updated_by_id = actor.id
        else:
            visit = ScmVisitaCalidad(
                ot_id=ot.id, trabajo_ot_id=work.id, tipo=tipo, numero=numero,
                created_by_id=actor.id, updated_by_id=actor.id,
            )
            session.add(visit)
        visit.observed_at = _parse_datetime(data.get("observed_at"))
        visit.payload_json = payload
        visit.operation_id = operation.operation_id
        session.flush()
        response = {"visit": _serialize_visit(visit)}
        _complete_operation(operation, response, status=200)
        session.commit()
        return response
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "QUALITY_VISIT_CONFLICT",
            "Otra tablet creo el mismo borrador; vuelve a leer la visita.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise


def confirm_quality_visit(session, *, actor_id, public_id, visit_id, operation_id, data):
    actor = _require_register_actor(session, actor_id)
    reject_unknown_fields(data, allowed={"version"})
    operation, replay = _reserve_operation(
        session, operation_id, f"/ots/{public_id}/calidad-visitas/{visit_id}/confirmar", actor, data,
    )
    if replay is not None:
        return replay
    try:
        ot = session.scalar(select(RegistroDiarioProduccion).where(
            RegistroDiarioProduccion.public_id == public_id,
            RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
        ).with_for_update())
        if ot is None:
            raise ScmServiceError("OT_NOT_FOUND", "La OT SCM no existe.", status_code=404)
        visit = session.scalar(select(ScmVisitaCalidad).where(
            ScmVisitaCalidad.id == _parse_uuid(visit_id, field="visit_id"),
            ScmVisitaCalidad.ot_id == ot.id,
        ).with_for_update())
        if visit is None:
            raise ScmServiceError("QUALITY_VISIT_NOT_FOUND", "La visita no pertenece a la OT.", status_code=404)
        if visit.estado == "CONFIRMED":
            raise ScmServiceError("QUALITY_VISIT_CONFIRMED", "La visita ya fue confirmada.", status_code=409)
        if expected_version(data.get("version")) != visit.version:
            raise ScmServiceError("VERSION_CONFLICT", "La visita cambio. Actualiza antes de confirmar.", status_code=409)
        work = session.scalar(select(ScmTrabajoOt).where(
            ScmTrabajoOt.id == visit.trabajo_ot_id,
            ScmTrabajoOt.orden_trabajo_id == ot.id,
            ScmTrabajoOt.tipo == "COLOR",
        ))
        if work is None:
            raise ScmServiceError("QUALITY_WORK_NOT_FOUND", "El trabajo no pertenece a la OT.", status_code=404)
        _validate_confirmation_payload(session, visit=visit, work=work)
        if visit.tipo == "INICIAL" and _initial_window_closed(ot):
            raise ScmServiceError(
                "QUALITY_INITIAL_WINDOW_CLOSED",
                "La ventana del control de arranque ya cerro; registra una visita periodica.",
                status_code=409,
            )
        existing_initial = session.scalar(select(ScmVisitaCalidad).where(
            ScmVisitaCalidad.ot_id == ot.id,
            ScmVisitaCalidad.tipo == "INICIAL",
            ScmVisitaCalidad.estado == "CONFIRMED",
            ScmVisitaCalidad.id != visit.id,
        ))
        if visit.tipo == "INICIAL" and existing_initial is not None:
            raise ScmServiceError("QUALITY_INITIAL_ALREADY_CONFIRMED", "La OT ya tiene un arranque confirmado.", status_code=409)
        existing_number = session.scalar(select(ScmVisitaCalidad).where(
            ScmVisitaCalidad.ot_id == ot.id,
            ScmVisitaCalidad.numero == visit.numero,
            ScmVisitaCalidad.estado == "CONFIRMED",
            ScmVisitaCalidad.id != visit.id,
        ))
        if existing_number is not None:
            if visit.tipo != "PERIODICA":
                raise ScmServiceError("QUALITY_VISIT_NUMBER_CONFLICT", "El numero de visita ya esta confirmado en la OT.", status_code=409)
            visit.numero = _next_free_visit_number(
                session, ot_id=ot.id, start=visit.numero + 1,
            )
        visit.estado = "CONFIRMED"
        visit.version += 1
        visit.confirmed_at = datetime.now(timezone.utc)
        visit.confirmed_by_id = actor.id
        visit.updated_by_id = actor.id
        visit.operation_id = operation.operation_id
        session.add(_event("ORDEN_TRABAJO", ot.id, "QUALITY_VISIT_CONFIRMED", actor, operation, _serialize_visit(visit)))
        session.flush()
        response = {"visit": _serialize_visit(visit)}
        _complete_operation(operation, response, status=200)
        session.commit()
        return response
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "QUALITY_VISIT_CONFLICT",
            "Otra tablet creo o confirmo el mismo slot; vuelve a leer la visita.",
            status_code=409,
        ) from error
    except Exception:
        session.rollback()
        raise

