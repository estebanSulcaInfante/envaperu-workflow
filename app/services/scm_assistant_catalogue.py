"""Deterministic, read-only catalogue and router for the SCM assistant.

The assistant is deliberately a small compiler: Spanish text is normalized,
matched against a fixed vocabulary, and compiled into one of the four read
intents below.  No model, SQL, shell command, or user supplied callable is
accepted by this module.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import re
import unicodedata
from typing import Any, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, event
from contextlib import contextmanager

from app.models.scm_ot import ScmControlPesoManga, ScmEtiquetaManga, ScmManga, ScmPesajeManga, ScmTrabajoColor
from app.models.scm_production_orders import ScmCorridaFabricacion, ScmOrdenOperacion
from app.services.scm_daily_query_service import (
    DailyLimits,
    DailyQueryError,
    DailyQueryService,
    DailySummaryCache,
    INTENT_PRODUCTION_DAILY_SUMMARY,
    LocalProductionDailyAdapter,
    lima_window,
    resolve_lima_date,
    summarize_daily_rows,
)
from app.services.scm_manga_detail_service import get_manga_detail
from app.services.scm_production_reports_service import list_production_progress
from app.services.scm_service_support import ScmServiceError, load_actor


INTENT_PRODUCTION_ORDER_PROGRESS = "production_order_progress"
INTENT_WEIGHING_PERIOD = "weighing_period"
INTENT_MANGA_TRACE = "manga_trace"
ALLOWED_INTENTS = frozenset(
    {
        INTENT_PRODUCTION_DAILY_SUMMARY,
        INTENT_PRODUCTION_ORDER_PROGRESS,
        INTENT_WEIGHING_PERIOD,
        INTENT_MANGA_TRACE,
    }
)
LIMA = ZoneInfo("America/Lima")
MAX_CLAUSES = 2
MAX_QUERY_LENGTH = 512
MAX_PERIOD_DAYS = 7
MAX_PERIOD_WEIGHINGS = 500
MAX_PROGRESS_ITEMS = 500
MAX_MANGA_CODE_LENGTH = 80
MAX_RESPONSE_BYTES = 1_000_000
READ_TIMEOUT_MS = 5_000


class CatalogueQueryError(ScmServiceError):
    """Safe validation or bounded read failure for the catalogue surface."""


_WRITE_OR_INJECTION = re.compile(
    r"(?:--|/\*|\*/|;|\b(?:select|insert|update|delete|drop|alter|truncate|grant|revoke|exec|execute|union\s+select|shell|sql|curl|wget|powershell|rm\s+-)\b|"
    r"\b(?:crear|crea|actualiza|actualizar|modifica|modificar|elimina|eliminar|borra|borrar|anula|anular|corrige|corregir|inicia|iniciar|cierra|cerrar|recibe|recibir|mueve|mover|imprime|imprimir|escribe|escribir)\b)",
    re.IGNORECASE,
)
_CLAUSE_SEPARATOR = re.compile(r"\s+(?:y\s+adem[aá]s|adem[aá]s|y\s+también|y\s+tambien)\s+", re.IGNORECASE)
_UUID_RE = re.compile(
    r"(?<![0-9a-f])(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"(?<!\d)(?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})(?!\d)")
_OF_RE = re.compile(
    r"\b(?:of|orden\s+(?:de\s+)?fabricaci[oó]n)\s*(?:n[uú]mero|nro\.?|num\.?|#|:|-)?\s*(?P<of>[A-Za-z0-9][A-Za-z0-9_.\-/]{0,47})\b",
    re.IGNORECASE,
)
_MANGA_VALUE_RE = re.compile(
    r"\bmanga\s*(?:(?:c[oó]digo|id|n[uú]mero|nro\.?|#)\s*)?(?P<value>[A-Za-z0-9][A-Za-z0-9_.\-/]{0,79})\b",
    re.IGNORECASE,
)
_COLOR_RE = re.compile(r"\bcolor\s*(?:es\s*)?(?:=|:)?\s*(?P<color>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9_-]{1,39})\b", re.IGNORECASE)
_SAFE_ENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,79}$")


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() or char.isspace() else " " for char in value).split())


def _clarification(message: str, question: str, fields: list[str], *, choices: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": "needs_clarification",
        "message": message,
        "plan": [],
        "clarification": {"question": question, "fields": fields, "choices": choices or []},
    }


def _unsupported(message: str) -> dict[str, Any]:
    return {"status": "unsupported", "message": message, "plan": []}


def _parse_date_token(token: str, *, now: datetime | None = None) -> date | str | None:
    if not isinstance(token, str):
        return None
    token = token.strip()
    lowered = _normalize(token)
    if lowered in {"hoy", "ayer"}:
        return resolve_lima_date(lowered, now=now)
    try:
        if "/" in token:
            day, month, year = (int(item) for item in token.split("/"))
            return date(year, month, day)
        return date.fromisoformat(token)
    except (TypeError, ValueError):
        return None


def _date_values(clause: str, *, now: datetime | None = None) -> list[date]:
    values: list[date] = []
    for match in _DATE_RE.finditer(clause):
        parsed = _parse_date_token(match.group("date"), now=now)
        if isinstance(parsed, date):
            values.append(parsed)
    normalized = _normalize(clause)
    for word in ("ayer", "hoy"):
        if re.search(rf"\b{word}\b", normalized):
            parsed = _parse_date_token(word, now=now)
            if isinstance(parsed, date) and parsed not in values:
                values.append(parsed)
    return values


def _extract_of(clause: str) -> str | None:
    match = _OF_RE.search(clause)
    if not match:
        return None
    value = match.group("of").strip(".,;:!? )(")
    if value.isdigit():
        return f"OF-{int(value):06d}"
    if re.fullmatch(r"OF-\d+", value, re.IGNORECASE):
        return f"OF-{int(value[3:]):06d}"
    return value if value else None


def _extract_manga(clause: str) -> dict[str, Any] | None:
    uuid_match = _UUID_RE.search(clause)
    if uuid_match:
        return {"public_id": str(UUID(uuid_match.group("uuid")))}
    match = _MANGA_VALUE_RE.search(clause)
    if not match:
        return None
    value = match.group("value").strip(".,;:!? )(")
    if not value or _normalize(value) in {"de", "por", "para", "con", "trazabilidad"}:
        return None
    if value.isdigit():
        return {"id": int(value)}
    return {"codigo": value}


def _parse_clause(clause: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    normalized = _normalize(clause)
    dates = _date_values(clause, now=now)
    has_weighing = bool(re.search(r"\bpesaj(?:e|es|ado|ar)?\b", normalized))
    has_manga_marker = bool(re.search(r"\b(?:trazabilidad|historial|detalle)\b", normalized)) or bool(re.search(r"\bmanga\b", normalized))
    of = _extract_of(clause)
    has_progress = bool(re.search(r"\b(?:avance|progreso|produccion acumulada|fabricado)\b", normalized))

    # A period is valid only with two explicit endpoints.  Relative phrases
    # such as "esta semana" are intentionally left for clarification.
    range_match = re.search(
        r"(?:(?:del?|desde)\s+(?P<start>\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\s+(?:al?|hasta)|entre\s+(?P<start_between>\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\s+y)\s+(?P<end>\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
        clause,
        re.IGNORECASE,
    )
    if has_weighing and range_match:
        start_token = range_match.group("start") or range_match.group("start_between")
        start = _parse_date_token(start_token, now=now)
        end = _parse_date_token(range_match.group("end"), now=now)
        if not isinstance(start, date) or not isinstance(end, date):
            return None
        if start > end:
            return {"_clarify": ("El rango de pesajes está invertido.", "¿Qué fecha debe ser el inicio?", ["fecha_desde"])}
        if (end - start).days + 1 > MAX_PERIOD_DAYS:
            return {"_clarify": ("El período de pesajes no puede superar siete días.", "Indica un rango Lima de hasta siete días.", ["fecha_desde", "fecha_hasta"])}
        return {"intent": INTENT_WEIGHING_PERIOD, "parameters": {"fecha_desde": start.isoformat(), "fecha_hasta": end.isoformat(), "max_weighings": MAX_PERIOD_WEIGHINGS}}
    if has_weighing and len(dates) == 1 and not range_match:
        return {"intent": INTENT_PRODUCTION_DAILY_SUMMARY, "parameters": {"date_lima": dates[0].isoformat()}}
    if has_weighing and (len(dates) != 2 or not range_match):
        return {"_clarify": ("Para leer pesajes necesito dos fechas explícitas.", "¿Qué rango YYYY-MM-DD a YYYY-MM-DD en Lima debo consultar?", ["fecha_desde", "fecha_hasta"])}

    if has_manga_marker and (of is None or not has_progress):
        manga = _extract_manga(clause)
        if manga is None:
            return {"_clarify": ("Falta identificar la manga.", "Indica el UUID público, código exacto o id de la manga.", ["manga"])}
        return {"intent": INTENT_MANGA_TRACE, "parameters": manga}

    if has_progress and of is not None:
        parameters: dict[str, Any] = {"of": of}
        color_match = _COLOR_RE.search(clause)
        if color_match:
            # Preserve the lexical value; matching is exact in the canonical
            # read model and no Azure/Azul or other fuzzy conversion occurs.
            parameters["color"] = color_match.group("color")
        return {"intent": INTENT_PRODUCTION_ORDER_PROGRESS, "parameters": parameters}
    if has_progress and re.search(r"\bof\b", normalized):
        return {"_clarify": ("Falta el código de la OF.", "¿Qué OF exacta debo consultar?", ["of"])}

    if re.search(r"\b(?:resumen|produccion|produccion diaria|pesajes del dia|que tal fue)\b", normalized):
        if len(dates) != 1:
            return {"_clarify": ("Falta una fecha para el resumen diario.", "¿Qué fecha Lima debo resumir?", ["date_lima"]), "choices": ["hoy", "ayer"]}
        return {"intent": INTENT_PRODUCTION_DAILY_SUMMARY, "parameters": {"date_lima": dates[0].isoformat()}}
    return None


def plan_query(query: str, now: datetime | None = None) -> dict[str, Any]:
    """Compile Spanish free text into a validated read-only execution plan."""
    if not isinstance(query, str) or not query.strip():
        return _clarification("Necesito una consulta SCM.", "¿Quieres un resumen diario, avance de OF, rango de pesajes o trazabilidad de manga?", ["query"])
    if len(query) > MAX_QUERY_LENGTH:
        return _unsupported("La consulta supera el límite permitido.")
    if _WRITE_OR_INJECTION.search(query):
        return _unsupported("Solo están habilitadas consultas de lectura de SCM.")
    clauses = [part.strip() for part in _CLAUSE_SEPARATOR.split(query) if part.strip()]
    if len(clauses) > MAX_CLAUSES:
        return _unsupported("Puedes combinar como máximo dos consultas de lectura.")
    compiled: list[dict[str, Any]] = []
    for clause in clauses:
        parsed = _parse_clause(clause, now=now)
        if parsed is None:
            return _unsupported("No reconozco una consulta de lectura habilitada en el catálogo SCM.")
        if "_clarify" in parsed:
            message, question, fields = parsed["_clarify"][:3]
            choices = parsed.get("choices", [])
            return _clarification(message, question, fields, choices=choices)
        compiled.append(parsed)
    validation = validate_plan(compiled)
    if validation is not None:
        return _clarification(validation, "¿Puedes indicar los datos exactos de la consulta?", [])
    labels = {INTENT_PRODUCTION_DAILY_SUMMARY: "resumen diario", INTENT_PRODUCTION_ORDER_PROGRESS: "avance de OF", INTENT_WEIGHING_PERIOD: "pesajes del período", INTENT_MANGA_TRACE: "trazabilidad de manga"}
    return {"status": "answered", "message": "; ".join(labels[item["intent"]] for item in compiled), "plan": compiled}


def validate_plan(plan: Any, query: str | None = None) -> str | None:
    """Validate a server-owned plan before any read is started.

    Returns a user-safe reason, or ``None`` for a valid plan.  ``query`` is
    accepted for future model adapters but is never executed or interpolated.
    """
    if query is not None and (not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH):
        return "La consulta de origen no es válida."
    if query is not None:
        if _WRITE_OR_INJECTION.search(query):
            return "La consulta de origen solo puede solicitar lecturas."
        grounded = plan_query(query)
        if grounded.get("status") != "answered" or grounded.get("plan") != plan:
            return "El plan no coincide con las entidades y fechas expresadas."
    if not isinstance(plan, list) or not 1 <= len(plan) <= MAX_CLAUSES:
        return "El plan debe contener una o dos consultas."
    for item in plan:
        if not isinstance(item, dict) or set(item) != {"intent", "parameters"}:
            return "El plan contiene campos no admitidos."
        intent, parameters = item["intent"], item["parameters"]
        if intent not in ALLOWED_INTENTS or not isinstance(parameters, dict):
            return "El plan contiene una intención no habilitada."
        if intent == INTENT_PRODUCTION_DAILY_SUMMARY:
            if set(parameters) != {"date_lima"} or not isinstance(parameters["date_lima"], str):
                return "El resumen diario requiere una fecha Lima."
            if _parse_date_token(parameters["date_lima"]) is None:
                return "La fecha Lima no es válida."
        elif intent == INTENT_PRODUCTION_ORDER_PROGRESS:
            if set(parameters) - {"of", "color"} or not isinstance(parameters.get("of"), str) or not parameters["of"] or not _SAFE_ENTITY_RE.fullmatch(parameters["of"]):
                return "El avance requiere el código exacto de la OF."
            if "color" in parameters and (not isinstance(parameters["color"], str) or not parameters["color"] or not _SAFE_ENTITY_RE.fullmatch(parameters["color"])):
                return "El color exacto no es válido."
        elif intent == INTENT_WEIGHING_PERIOD:
            if set(parameters) != {"fecha_desde", "fecha_hasta", "max_weighings"}:
                return "El período requiere fecha de inicio y fin explícitas."
            start = _parse_date_token(parameters["fecha_desde"])
            end = _parse_date_token(parameters["fecha_hasta"])
            if not isinstance(start, date) or not isinstance(end, date) or start > end:
                return "El rango de pesajes no es válido."
            if (end - start).days + 1 > MAX_PERIOD_DAYS or parameters["max_weighings"] != MAX_PERIOD_WEIGHINGS:
                return "El rango de pesajes admite hasta siete días y 500 lecturas."
        elif intent == INTENT_MANGA_TRACE:
            if set(parameters) != {"public_id"} and set(parameters) != {"codigo"} and set(parameters) != {"id"}:
                return "La trazabilidad requiere UUID público, código exacto o id."
            if "public_id" in parameters:
                try:
                    UUID(parameters["public_id"])
                except (ValueError, TypeError, AttributeError):
                    return "El UUID público de manga no es válido."
            if "codigo" in parameters and (not isinstance(parameters["codigo"], str) or not 1 <= len(parameters["codigo"]) <= MAX_MANGA_CODE_LENGTH or not _SAFE_ENTITY_RE.fullmatch(parameters["codigo"])):
                return "El código exacto de manga no es válido."
            if "id" in parameters and (not isinstance(parameters["id"], int) or isinstance(parameters["id"], bool) or parameters["id"] <= 0):
                return "El id de manga no es válido."
    return None


def get_catalogue() -> dict[str, Any]:
    """Return the bounded user-facing catalogue for the assistant UI/API."""
    intents = [
        {"intent": INTENT_PRODUCTION_DAILY_SUMMARY, "label": "Resumen diario", "description": "Pesajes efectivos por OF y color en una fecha Lima; anulados separados.", "examples": ["Resumen de producción de ayer", "¿Qué pesajes hubo hoy?"]},
        {"intent": INTENT_PRODUCTION_ORDER_PROGRESS, "label": "Avance de OF", "description": "Avance acumulado de una OF, opcionalmente por color exacto.", "examples": ["Avance de la OF OF-123", "Progreso de OF-123 color Azul"]},
        {"intent": INTENT_WEIGHING_PERIOD, "label": "Pesajes por período", "description": "Pesajes en un rango explícito de hasta siete días y 500 lecturas.", "examples": ["Pesajes del 2026-10-01 al 2026-10-03"]},
        {"intent": INTENT_MANGA_TRACE, "label": "Trazabilidad de manga", "description": "Detalle de una manga por UUID público, código exacto o id.", "examples": ["Trazabilidad de manga MG-001", "Detalle de manga 42"]},
    ]
    suggestions = [
        {"label": "Resumen de producción de ayer", "query": "Resumen de producción de ayer"},
        {"label": "Avance de una OF", "query": "Avance de OF OF-123"},
        {"label": "Pesajes de un período", "query": "Pesajes del 2026-10-01 al 2026-10-03"},
        {"label": "Trazabilidad de manga", "query": "Trazabilidad de manga MG-001"},
    ]
    return {"version": "scm-assistant-catalogue-v2", "read_only": True, "intents": intents, "suggestions": suggestions}


def _prepare_read_only_transaction(session) -> str | None:
    """Pin PostgreSQL reads to one repeatable read-only snapshot."""
    try:
        bind = session.get_bind()
        dialect = bind.dialect.name
    except (AttributeError, TypeError) as error:
        raise CatalogueQueryError("SCM_ASSISTANT_SESSION_REQUIRED", "La consulta requiere una sesión de lectura válida.", status_code=503) from error
    if dialect != "postgresql":
        return None
    if not session.in_transaction():
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    session.execute(text(f"SET LOCAL statement_timeout = {READ_TIMEOUT_MS}"))
    try:
        readonly = str(session.execute(text("SHOW transaction_read_only")).scalar() or "").lower()
        isolation = str(session.execute(text("SHOW transaction_isolation")).scalar() or "").lower()
    except AttributeError as error:
        raise CatalogueQueryError("SCM_ASSISTANT_SESSION_REQUIRED", "La consulta requiere una sesión de lectura válida.", status_code=503) from error
    if readonly not in {"on", "true"} or isolation != "repeatable read":
        raise CatalogueQueryError("SCM_ASSISTANT_READ_ONLY_REQUIRED", "La consulta requiere una transacción PostgreSQL de solo lectura.", status_code=503)
    snapshot = session.execute(text("SELECT transaction_timestamp()")).scalar()
    return _as_utc(snapshot).isoformat() if isinstance(snapshot, datetime) else None


def _as_utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc))


def _source_cutoff(data: Mapping[str, Any], fallback: str | None) -> str | None:
    value = data.get("as_of_utc")
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if isinstance(value, str) and value:
        return value
    return fallback


def _resolve_manga_public_id(session, parameters: Mapping[str, Any]) -> UUID:
    if "public_id" in parameters:
        return UUID(parameters["public_id"])
    if "codigo" in parameters:
        statement = select(ScmManga.public_id).where(ScmManga.codigo == parameters["codigo"])
    else:
        statement = select(ScmManga.public_id).where(ScmManga.id == parameters["id"])
    values = list(session.scalars(statement.limit(2)).all())
    if not values:
        raise CatalogueQueryError("MANGA_NOT_FOUND", "La manga exacta no existe.", status_code=404)
    if len(values) > 1:
        raise CatalogueQueryError("MANGA_AMBIGUOUS", "El identificador coincide con más de una manga; indica el UUID público.", status_code=409, details={"field": "manga", "choices": [str(value) for value in values[:10]]})
    return UUID(str(values[0]))


def _authorize_assistant_read(session, actor_id: int) -> None:
    """Require the assistant capability for every tool, including wrappers."""
    actor = load_actor(session, actor_id, capability="OT_VER")
    if not actor.tiene_capacidad("ASISTENTE_PRODUCCION_USAR"):
        raise CatalogueQueryError(
            "ASISTENTE_PRODUCCION_USAR_REQUIRED",
            "La lectura del asistente requiere ASISTENTE_PRODUCCION_USAR.",
            status_code=403,
            details={"capability": "ASISTENTE_PRODUCCION_USAR"},
        )


def _resolve_of_id(session, code: str):
    """Resolve the OF code exactly before passing an id-scoped filter."""
    value = session.scalar(select(ScmOrdenOperacion.id).where(ScmOrdenOperacion.codigo == code))
    if value is None:
        raise CatalogueQueryError("OF_NOT_FOUND", "La OF exacta no existe.", status_code=404)
    return value


def _preflight_progress_size(session, of_id) -> None:
    """Bound runs and mangas before the canonical graph loader runs."""
    run_ids = list(
        session.scalars(
            select(ScmCorridaFabricacion.id)
            .where(ScmCorridaFabricacion.orden_fabricacion_id == of_id)
            .limit(MAX_PROGRESS_ITEMS + 1)
        ).all()
    )
    if len(run_ids) > MAX_PROGRESS_ITEMS:
        raise CatalogueQueryError("ASSISTANT_LIMIT_EXCEEDED", "La lectura de avance supera el límite de corridas.", status_code=422)
    if not run_ids:
        return
    manga_ids = list(
        session.scalars(
            select(ScmManga.id)
            .join(ScmTrabajoColor, ScmManga.trabajo_ot_id == ScmTrabajoColor.trabajo_ot_id)
            .where(ScmTrabajoColor.corrida_fabricacion_id.in_(run_ids))
            .limit(MAX_PROGRESS_ITEMS + 1)
        ).all()
    )
    if len(manga_ids) > MAX_PROGRESS_ITEMS:
        raise CatalogueQueryError("ASSISTANT_LIMIT_EXCEEDED", "La lectura de avance supera el límite de mangas.", status_code=422)


def _preflight_manga_size(session, public_id) -> None:
    """Bound child collections before the canonical manga projection expands them."""
    manga_id = session.scalar(select(ScmManga.id).where(ScmManga.public_id == public_id))
    if manga_id is None:
        return
    collections = (
        ("pesajes", select(ScmPesajeManga.id).where(ScmPesajeManga.manga_id == manga_id)),
        ("controles", select(ScmControlPesoManga.id).where(ScmControlPesoManga.manga_id == manga_id)),
        ("etiquetas", select(ScmEtiquetaManga.public_id).where(ScmEtiquetaManga.manga_id == manga_id)),
    )
    for label, statement in collections:
        values = list(session.scalars(statement.limit(MAX_PROGRESS_ITEMS + 1)).all())
        if len(values) > MAX_PROGRESS_ITEMS:
            raise CatalogueQueryError("ASSISTANT_LIMIT_EXCEEDED", f"La trazabilidad supera el límite de {label} permitido.", status_code=422)


def _filter_progress_exact_color(data: dict[str, Any], color: str) -> dict[str, Any]:
    """Apply exact color equality after the canonical service's broad filter."""
    expected = color.casefold()
    data = dict(data)
    data["items"] = [item for item in data.get("items", []) if str(item.get("color") or "").casefold() == expected]
    return data


def _execute_daily(session, actor_id: int, parameters: Mapping[str, Any], cache: DailySummaryCache | None, refresh: bool) -> tuple[dict[str, Any], str | None]:
    adapter = LocalProductionDailyAdapter(session, config={})
    service = DailyQueryService(adapter, limits=DailyLimits(), cache=cache or DailySummaryCache())
    if refresh:
        service.invalidate(actor_id=actor_id)
    data = service.execute(intent=INTENT_PRODUCTION_DAILY_SUMMARY, actor_id=actor_id, date_value=parameters["date_lima"])
    return data, _source_cutoff(data, getattr(adapter, "snapshot_at_utc", None))


def _execute_period(session, actor_id: int, parameters: Mapping[str, Any], cutoff: str | None) -> tuple[dict[str, Any], str | None]:
    start = date.fromisoformat(parameters["fecha_desde"])
    end = date.fromisoformat(parameters["fecha_hasta"])
    adapter = LocalProductionDailyAdapter(session, config={})
    rows: list[Mapping[str, Any]] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        remaining = MAX_PERIOD_WEIGHINGS - len(rows)
        if remaining <= 0:
            # Probe one row so an exactly-500 period remains valid while a
            # 501st source row is rejected before it enters the projection.
            if list(adapter.query(actor_id=actor_id, day=day, max_weighings=1)):
                raise CatalogueQueryError("ASSISTANT_LIMIT_EXCEEDED", "El período supera 500 lecturas.", status_code=422)
            continue
        rows.extend(adapter.query(actor_id=actor_id, day=day, max_weighings=remaining))
        if len(rows) > MAX_PERIOD_WEIGHINGS:
            raise CatalogueQueryError("ASSISTANT_LIMIT_EXCEEDED", "El período supera 500 lecturas.", status_code=422)
    groups_by_day = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        groups_by_day.append(summarize_daily_rows(rows, day=day, max_weighings=MAX_PERIOD_WEIGHINGS))
    data = {"intent": INTENT_WEIGHING_PERIOD, "fecha_desde": start.isoformat(), "fecha_hasta": end.isoformat(), "timezone": "America/Lima", "days": groups_by_day, "totals": {"readings": len(rows)}}
    return data, _source_cutoff(data, getattr(adapter, "snapshot_at_utc", None) or cutoff)


def _execute_plan(session, actor_id: int, plan: Any, cache: DailySummaryCache | None = None, refresh: bool = False) -> list[dict[str, Any]]:
    """Execute a validated plan using only canonical read services.

    Every item keeps its own intent, source, and source cutoff.  Daily kg and
    accumulated OF progress are never combined into one total.
    """
    reason = validate_plan(plan)
    if reason:
        raise CatalogueQueryError("INVALID_ASSISTANT_PLAN", reason, status_code=400)
    snapshot = _prepare_read_only_transaction(session)
    results: list[dict[str, Any]] = []
    for item in plan:
        intent = item["intent"]
        parameters = item["parameters"]
        _authorize_assistant_read(session, actor_id)
        if intent == INTENT_PRODUCTION_DAILY_SUMMARY:
            data, cutoff = _execute_daily(session, actor_id, parameters, cache, refresh)
            source = "scm_daily_query_service.production_daily_summary"
        elif intent == INTENT_PRODUCTION_ORDER_PROGRESS:
            of_id = _resolve_of_id(session, parameters["of"])
            _preflight_progress_size(session, of_id)
            filters = {"of_ids": {of_id}}
            if "color" in parameters:
                filters["color"] = parameters["color"]
            data = list_production_progress(session, actor_id=actor_id, filters=filters)
            if "color" in parameters:
                data = _filter_progress_exact_color(data, parameters["color"])
            if len(data.get("items", [])) > MAX_PROGRESS_ITEMS:
                raise CatalogueQueryError("ASSISTANT_LIMIT_EXCEEDED", "La lectura de avance supera el límite permitido.", status_code=422)
            cutoff = _source_cutoff(data, snapshot)
            source = "scm_production_reports_service.list_production_progress"
        elif intent == INTENT_WEIGHING_PERIOD:
            data, cutoff = _execute_period(session, actor_id, parameters, snapshot)
            source = "scm_daily_query_service.weighing_period"
        else:
            public_id = _resolve_manga_public_id(session, parameters)
            _preflight_manga_size(session, public_id)
            data = get_manga_detail(session, actor_id=actor_id, public_id=public_id)
            cutoff = _source_cutoff(data, snapshot)
            source = "scm_manga_detail_service.get_manga_detail"
        if len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise CatalogueQueryError("ASSISTANT_RESPONSE_LIMIT", "La respuesta de lectura supera el límite permitido.", status_code=422)
        results.append({"intent": intent, "data": data, "source": source, "as_of_utc": cutoff})
    return results


@contextmanager
def bounded_orm_reads(session):
    """Bound every ORM load, including canonical eager/lazy relationship loads.

    A dedicated assistant Session owns this listener; other callers are unchanged.
    Results are checked before canonical services can expand their graphs.
    """
    budget = {'queries':0, 'rows':0}
    def bounded(execution):
        if not execution.is_orm_statement:
            return execution.invoke_statement()
        if not execution.is_select:
            raise CatalogueQueryError('ASSISTANT_READ_ONLY_REQUIRED', 'Solo se permiten lecturas.', status_code=403)
        budget['queries'] += 1
        if budget['queries'] > 200:
            raise CatalogueQueryError('ASSISTANT_LIMIT_EXCEEDED', 'La consulta supera 200 lecturas internas.', status_code=422)
        statement = execution.statement
        # Never widen an existing canonical limit.
        limit = getattr(statement, '_limit_clause', None)
        existing = getattr(limit, 'value', None)
        execution.statement = statement.limit(min(existing, 501) if isinstance(existing, int) else 501)
        frozen = execution.invoke_statement().unique().freeze()
        count = len(frozen.data)
        budget['rows'] += count
        if count > 500 or budget['rows'] > 4000:
            raise CatalogueQueryError('ASSISTANT_LIMIT_EXCEEDED', 'La consulta supera el límite de filas de lectura.', status_code=422)
        return frozen()
    event.listen(session, 'do_orm_execute', bounded, retval=True)
    try:
        yield
    finally:
        event.remove(session, 'do_orm_execute', bounded)


def execute_plan(session, actor_id: int, plan: Any, cache: DailySummaryCache | None = None, refresh: bool = False) -> list[dict[str, Any]]:
    with bounded_orm_reads(session):
        return _execute_plan(session, actor_id, plan, cache, refresh)


__all__ = [
    "ALLOWED_INTENTS",
    "INTENT_MANGA_TRACE",
    "INTENT_PRODUCTION_DAILY_SUMMARY",
    "INTENT_PRODUCTION_ORDER_PROGRESS",
    "INTENT_WEIGHING_PERIOD",
    "CatalogueQueryError",
    "execute_plan",
    "get_catalogue",
    "get_manga_detail",
    "list_production_progress",
    "plan_query",
    "validate_plan",
]
