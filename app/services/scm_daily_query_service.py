"""Bounded local prototype for the ``production_daily_summary`` intent.

This module deliberately has a small read-only surface.  It does not accept
SQL, shell snippets, arbitrary report names, or credentials.  The local
adapter reads the existing SCM weighing facts and projects them into an
OF/color summary using the same effective-weighing rules as the production
reports service.  A future remote adapter can implement :class:`DailyAdapter`
once authentication is approved; until then it reports ``AUTH_PENDING``.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import argparse
import json
import os
import threading
import time as monotonic_time
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import joinedload, selectinload

from app.models.scm_ot import (
    ScmAnulacionPesajeManga,
    ScmCorreccionPesajeManga,
    ScmManga,
    ScmPesajeManga,
    ScmTrabajoColor,
    ScmTrabajoOt,
)
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
)
from app.services.scm_production_reports_service import _d
from app.services.scm_manga_assignment_projection import effective_work
from app.services.scm_service_support import ScmServiceError, load_actor


INTENT_PRODUCTION_DAILY_SUMMARY = "production_daily_summary"
ADAPTER_VERSION = "scm-daily-v1"
LIMA = ZoneInfo("America/Lima")
DEFAULT_TTL_SECONDS = 60
DEFAULT_MAX_WEIGHINGS = 500
DEFAULT_MAX_GROUPS = 100


class DailyQueryError(ScmServiceError):
    """A user-safe, bounded failure for the experimental query surface."""


class DailyAdapter(Protocol):
    auth_state: str

    def permission_scope(self, actor_id: int) -> str: ...

    def query(
        self,
        *,
        actor_id: int,
        day: date,
        max_weighings: int,
    ) -> Iterable[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class DailyLimits:
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_weighings: int = DEFAULT_MAX_WEIGHINGS
    max_groups: int = DEFAULT_MAX_GROUPS

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0 or self.max_weighings <= 0 or self.max_groups <= 0:
            raise ValueError("daily query limits must be positive (TTL may be zero)")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _utc(value: datetime) -> datetime:
    """Normalize DB timestamps, including SQLite's naive UTC values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_lima_date(value: Any = None, *, now: datetime | None = None) -> date:
    """Resolve an ISO date or ``ayer`` in America/Lima deterministically."""
    if value in (None, "", "hoy"):
        instant = now or datetime.now(timezone.utc)
        return _utc(instant).astimezone(LIMA).date()
    if str(value).strip().lower() == "ayer":
        instant = now or datetime.now(timezone.utc)
        return _utc(instant).astimezone(LIMA).date() - timedelta(days=1)
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as error:
        raise DailyQueryError(
            "INVALID_DAILY_DATE",
            "La fecha debe ser YYYY-MM-DD, 'hoy' o 'ayer'.",
            status_code=400,
        ) from error


def lima_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=LIMA).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=LIMA).astimezone(timezone.utc)
    return start, end


def _json_filters(day: date) -> str:
    return json.dumps(
        {"fecha_lima": day.isoformat(), "group_by": ["OF", "COLOR"]},
        sort_keys=True,
        separators=(",", ":"),
    )


def build_cache_key(
    *,
    actor_id: int,
    permission_scope: str,
    day: date,
    version: str = ADAPTER_VERSION,
) -> tuple[str, str, str, str, str]:
    """Cache key includes actor and permission scope to prevent cross-actor reuse."""
    return (str(actor_id), permission_scope, _json_filters(day), version, INTENT_PRODUCTION_DAILY_SUMMARY)


class DailySummaryCache:
    """Process-local TTL cache; it never writes SCM data or persists payloads."""

    def __init__(self, *, clock=monotonic_time.monotonic):
        self._clock = clock
        self._entries: OrderedDict[tuple[Any, ...], tuple[float, Mapping[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: tuple[Any, ...]) -> Mapping[str, Any] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, payload = entry
            if expires_at <= self._clock():
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return payload

    def set(self, key: tuple[Any, ...], payload: Mapping[str, Any], ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        with self._lock:
            self._entries[key] = (self._clock() + ttl_seconds, payload)
            self._entries.move_to_end(key)

    def invalidate(self, *, actor_id: int | None = None) -> int:
        with self._lock:
            if actor_id is None:
                count = len(self._entries)
                self._entries.clear()
                return count
            prefix = str(actor_id)
            keys = [key for key in self._entries if key and key[0] == prefix]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)


class DailyQueryLog:
    """Bounded in-memory audit of actor-scoped queries and measured usage only."""

    def __init__(self, *, max_entries: int = 200):
        self._max_entries = max(1, max_entries)
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def append(self, entry: Mapping[str, Any]) -> None:
        safe = {
            key: deepcopy(entry[key])
            for key in ("trace_id", "actor_id", "intent", "date_lima", "latency_ms", "cache_hit", "status", "query", "plan", "usage")
            if key in entry
        }
        with self._lock:
            self._entries.append(safe)
            del self._entries[:-self._max_entries]

    def list(self, *, actor_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = deepcopy(self._entries)
        return rows if actor_id is None else [item for item in rows if item.get("actor_id") == actor_id]


def authorize_daily_actor(session, actor_id: int, config: Any):
    """Require the persisted read capabilities for this intent."""
    actor = load_actor(session, actor_id, capability="OT_VER")
    if not actor.tiene_capacidad("ASISTENTE_PRODUCCION_USAR"):
        raise DailyQueryError(
            "ASISTENTE_PRODUCCION_USAR_REQUIRED",
            "La consulta diaria requiere ASISTENTE_PRODUCCION_USAR.",
            status_code=403,
            details={"capability": "ASISTENTE_PRODUCCION_USAR"},
        )
    if not actor.tiene_capacidad("MANGA_PESAJE_VER"):
        raise DailyQueryError(
            "MANGA_PESAJE_VER_REQUIRED",
            "La consulta diaria requiere MANGA_PESAJE_VER.",
            status_code=403,
            details={"capability": "MANGA_PESAJE_VER"},
        )
    return actor


def _target_for_run(corrida: Any) -> tuple[Decimal | None, str | None, str | None]:
    outputs = list(getattr(corrida, "salidas", ()) or ())
    if len(outputs) == 1 and _decimal(getattr(corrida, "objetivo_neto_kg", None)):
        return _decimal(corrida.objetivo_neto_kg), "KG", "NETA"
    if len(outputs) == 1 and _decimal(getattr(outputs[0], "kg_estandar_objetivo", None)):
        return _decimal(outputs[0].kg_estandar_objetivo), "KG", "ESTANDAR"
    return None, None, None


def _row_from_weighing(
    weighing: Any,
    correction: Any | None = None,
    *,
    include_excluded: bool = False,
    latest_vigente_ids: set[Any] | None = None,
) -> dict[str, Any] | None:
    manga = getattr(weighing, "manga", None)
    state = str(getattr(weighing, "estado", "")).upper()
    excluded_reason = None
    if state == "VIGENTE" and latest_vigente_ids is not None and getattr(weighing, "id", None) not in latest_vigente_ids:
        excluded_reason = "NOT_LATEST_VIGENTE_FOR_MANGA"
    elif state not in {"VIGENTE", "ANULADO"}:
        excluded_reason = "NON_EFFECTIVE_STATE"
    if excluded_reason:
        if not include_excluded:
            return None
        return {
            "excluded": True,
            "excluded_reason": excluded_reason,
            "weighing_id": getattr(weighing, "id", None),
            "manga_id": getattr(getattr(weighing, "manga", None), "id", None),
            "pesada_at": getattr(weighing, "pesada_at", None),
        }
    work = effective_work(manga) or getattr(manga, "trabajo", None)
    color_work = getattr(work, "trabajo_color", None)
    corrida = getattr(color_work, "corrida", None)
    of = getattr(getattr(corrida, "orden_fabricacion", None), "orden_operacion", None)
    if manga is None or work is None or color_work is None or corrida is None or of is None:
        if not include_excluded:
            return None
        return {
            "excluded": True,
            "excluded_reason": "MISSING_OF_COLOR_CONTEXT",
            "weighing_id": getattr(weighing, "id", None),
            "manga_id": getattr(manga, "id", None),
            "pesada_at": getattr(weighing, "pesada_at", None),
        }
    color = (
        getattr(getattr(corrida, "color_produccion", None), "nombre", None)
        or getattr(color_work, "color_nombre_snapshot", None)
        or getattr(manga, "color_snapshot", None)
        or "SIN_COLOR"
    )
    original = _decimal(getattr(weighing, "peso_fisico_neto_kg", None))
    effective = original
    projection = getattr(correction, "result_projection_json", None) if correction else None
    if projection:
        effective = _decimal(projection.get("peso_fisico_neto_kg")) or effective
    cancelled = state == "ANULADO" or getattr(weighing, "anulacion", None) is not None
    target, unit, kind = _target_for_run(corrida)
    return {
        "weighing_id": getattr(weighing, "id", None),
        "manga_id": getattr(manga, "id", None),
        "pesada_at": getattr(weighing, "pesada_at", None),
        "of": getattr(of, "codigo", None),
        "of_state": getattr(of, "estado", None),
        "color": str(color),
        "corrida": getattr(corrida, "codigo", None),
        "cancelled": cancelled,
        "original_kg": original,
        "effective_kg": effective,
        "target_kg": target,
        "target_unit": unit,
        "target_kind": kind,
        "target_identity": (str(getattr(corrida, "id", "")), kind, unit),
    }


def summarize_daily_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    day: date,
    max_groups: int = DEFAULT_MAX_GROUPS,
    max_weighings: int = DEFAULT_MAX_WEIGHINGS,
) -> dict[str, Any]:
    """Aggregate only effective net weighings, retaining cancellations separately."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    effective_total = Decimal("0")
    cancelled_total = Decimal("0")
    accepted = 0
    excluded_count = 0
    excluded_weighing_ids: list[Any] = []
    excluded_manga_ids: list[Any] = []
    excluded_reasons: dict[str, int] = {}
    for raw in rows:
        timestamp = raw.get("pesada_at")
        if not isinstance(timestamp, datetime):
            continue
        if _utc(timestamp).astimezone(LIMA).date() != day:
            continue
        accepted += 1
        if accepted > max_weighings:
            raise DailyQueryError("DAILY_QUERY_LIMIT_EXCEEDED", "La lectura diaria supera el límite configurado.", status_code=422)
        if raw.get("excluded"):
            excluded_count += 1
            if raw.get("weighing_id") is not None:
                excluded_weighing_ids.append(raw["weighing_id"])
            if raw.get("manga_id") is not None:
                excluded_manga_ids.append(raw["manga_id"])
            reason = str(raw.get("excluded_reason") or "UNKNOWN")
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
            continue
        of = str(raw.get("of") or "SIN_OF")
        color = str(raw.get("color") or "SIN_COLOR")
        key = (of, color)
        item = grouped.setdefault(key, {
            "of": of,
            "color": color,
            "weighings": 0,
            "effective_kg": Decimal("0"),
            "cancelled_weighings": 0,
            "cancelled_kg": Decimal("0"),
            "weighing_ids": [],
            "manga_ids": [],
            "cancelled_weighing_ids": [],
            "cancelled_manga_ids": [],
        })
        if raw.get("cancelled"):
            cancelled = _decimal(raw.get("effective_kg")) or _decimal(raw.get("original_kg")) or Decimal("0")
            item["cancelled_weighings"] += 1
            item["cancelled_kg"] += cancelled
            if raw.get("weighing_id") is not None:
                item["cancelled_weighing_ids"].append(raw["weighing_id"])
            if raw.get("manga_id") is not None and raw["manga_id"] not in item["cancelled_manga_ids"]:
                item["cancelled_manga_ids"].append(raw["manga_id"])
            cancelled_total += cancelled
            continue
        effective = _decimal(raw.get("effective_kg"))
        if effective is None:
            continue
        if raw.get("weighing_id") is not None:
            item["weighing_ids"].append(raw["weighing_id"])
        if raw.get("manga_id") is not None and raw["manga_id"] not in item["manga_ids"]:
            item["manga_ids"].append(raw["manga_id"])
        item["weighings"] += 1
        item["effective_kg"] += effective
        effective_total += effective
    if len(grouped) > max_groups:
        raise DailyQueryError("DAILY_QUERY_GROUP_LIMIT_EXCEEDED", "La consulta diaria supera el límite de grupos.", status_code=422)
    output = []
    for item in sorted(grouped.values(), key=lambda row: (row["of"], row["color"])):
        item["effective_kg"] = _number(item["effective_kg"])
        item["net_kg"] = item["effective_kg"]
        item["cancelled_kg"] = _number(item["cancelled_kg"])
        item["target_comparison"] = None
        item["target_reason"] = "DAILY_WEIGHT_TOTAL_IS_NOT_OF_PROGRESS"
        output.append(item)
    return {
        "intent": INTENT_PRODUCTION_DAILY_SUMMARY,
        "date_lima": day.isoformat(),
        "timezone": "America/Lima",
        "groups": output,
        "totals": {
            "effective_weighings": sum(item["weighings"] for item in output),
            "effective_kg": _number(effective_total),
            "cancelled_weighings": sum(item["cancelled_weighings"] for item in output),
            "cancelled_kg": _number(cancelled_total),
            "net_kg": _number(effective_total),
            "excluded_count": excluded_count,
        },
        "excluded": {
            "count": excluded_count,
            "weighing_ids": excluded_weighing_ids,
            "manga_ids": excluded_manga_ids,
            "reasons": excluded_reasons,
        },
        "target_policy": "La meta OF no se compara contra un corte diario; el avance/meta compatible pertenece a list_production_progress. Las anulaciones no se suman al efectivo.",
    }


class LocalProductionDailyAdapter:
    """Fixed-shape SQLAlchemy read adapter; no arbitrary query input is accepted."""

    auth_state = "LOCAL_NO_SECRET"

    def __init__(self, session, *, config: Any, limits: DailyLimits | None = None, snapshot_clock=None):
        self.session = session
        self.config = config
        self.limits = limits or DailyLimits()
        self.snapshot_at_utc: str | None = None
        self._snapshot_clock = snapshot_clock or (lambda: datetime.now(timezone.utc))

    def permission_scope(self, actor_id: int) -> str:
        self._prepare_read_only_transaction()
        self._set_fallback_snapshot()
        actor = authorize_daily_actor(self.session, actor_id, self.config)
        capabilities = sorted(
            item.codigo for role in actor.roles for item in role.capacidades if item.activo
        )
        return ",".join(capabilities)

    def query(self, *, actor_id: int, day: date, max_weighings: int) -> Iterable[Mapping[str, Any]]:
        authorize_daily_actor(self.session, actor_id, self.config)
        start, end = lima_window(day)
        statement = (
            select(ScmPesajeManga)
            .where(ScmPesajeManga.pesada_at >= start, ScmPesajeManga.pesada_at < end)
            .options(
                joinedload(ScmPesajeManga.manga)
                .joinedload(ScmManga.trabajo)
                .joinedload(ScmTrabajoOt.trabajo_color)
                .joinedload(ScmTrabajoColor.corrida)
                .joinedload(ScmCorridaFabricacion.orden_fabricacion)
                .joinedload(ScmOrdenFabricacion.orden_operacion),
                selectinload(ScmPesajeManga.anulacion),
            )
            .order_by(ScmPesajeManga.pesada_at, ScmPesajeManga.id)
            .limit(max_weighings + 1)
        )
        weighings = self.session.scalars(statement).unique().all()
        if len(weighings) > max_weighings:
            raise DailyQueryError("DAILY_QUERY_LIMIT_EXCEEDED", "La lectura diaria supera el límite configurado.", status_code=422)
        manga_ids = [item.manga_id for item in weighings if item.manga_id is not None]
        latest_vigente_ids = set(self.session.scalars(
            select(func.max(ScmPesajeManga.id))
            .where(
                ScmPesajeManga.manga_id.in_(manga_ids),
                ScmPesajeManga.estado == "VIGENTE",
            )
            .group_by(ScmPesajeManga.manga_id)
        ).all()) if manga_ids else set()
        ids = [item.id for item in weighings]
        corrections = self.session.scalars(
            select(ScmCorreccionPesajeManga)
            .where(ScmCorreccionPesajeManga.pesaje_id.in_(ids), ScmCorreccionPesajeManga.estado == "APLICADA")
            .order_by(ScmCorreccionPesajeManga.id)
        ).all() if ids else []
        latest = {}
        for correction in corrections:
            latest[correction.pesaje_id] = correction
        return [
            row
            for weighing in weighings
            if (row := _row_from_weighing(
                weighing,
                latest.get(weighing.id),
                include_excluded=True,
                latest_vigente_ids=latest_vigente_ids,
            )) is not None
        ]

    def _prepare_read_only_transaction(self) -> None:
        """Pin the CLI's PostgreSQL transaction to repeatable-read/read-only."""
        bind = self.session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        if not self.session.in_transaction():
            self.session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        read_only = str(self.session.execute(text("SHOW transaction_read_only")).scalar() or "").lower()
        isolation = str(self.session.execute(text("SHOW transaction_isolation")).scalar() or "").lower()
        if read_only not in {"on", "true"} or isolation != "repeatable read":
            raise DailyQueryError(
                "DAILY_QUERY_READ_ONLY_REQUIRED",
                "La consulta diaria requiere una transacción PostgreSQL REPEATABLE READ y READ ONLY.",
                status_code=503,
            )

        snapshot = self.session.execute(text("SELECT transaction_timestamp()")).scalar()
        if isinstance(snapshot, datetime):
            self.snapshot_at_utc = _utc(snapshot).isoformat()
        else:
            self.snapshot_at_utc = _utc(self._snapshot_clock()).isoformat()

    def _set_fallback_snapshot(self) -> None:
        if self.snapshot_at_utc is None:
            self.snapshot_at_utc = _utc(self._snapshot_clock()).isoformat()


class PendingAuthAdapter:
    """Placeholder for a future connector; it never accepts or stores secrets."""

    auth_state = "AUTH_PENDING"

    def permission_scope(self, actor_id: int) -> str:
        return "AUTH_PENDING"

    def query(self, *, actor_id: int, day: date, max_weighings: int):
        raise DailyQueryError(
            "SCM_AUTH_PENDING",
            "El adaptador SCM remoto está pendiente de autenticación aprobada.",
            status_code=503,
        )


class DailyQueryService:
    def __init__(self, adapter: DailyAdapter, *, limits: DailyLimits | None = None, cache: DailySummaryCache | None = None, audit: DailyQueryLog | None = None, clock=monotonic_time.monotonic, snapshot_clock=None):
        self.adapter = adapter
        self.limits = limits or DailyLimits()
        self.cache = cache or DailySummaryCache(clock=clock)
        self.audit = audit or DailyQueryLog()
        self._clock = clock
        self._snapshot_clock = snapshot_clock or (lambda: datetime.now(timezone.utc))

    def invalidate(self, *, actor_id: int | None = None) -> int:
        """Invalidate cached summaries globally or for one actor."""
        return self.cache.invalidate(actor_id=actor_id)

    def execute(self, *, intent: str, actor_id: int, date_value: Any = "ayer", now: datetime | None = None) -> dict[str, Any]:
        trace_id = uuid4().hex
        started = self._clock()
        day = None
        cache_hit = False
        status = "ok"
        try:
            day = resolve_lima_date(date_value, now=now)
            if intent != INTENT_PRODUCTION_DAILY_SUMMARY:
                raise DailyQueryError("INTENT_NOT_ALLOWED", "Solo production_daily_summary está habilitado en este prototipo.", status_code=400)
            permission_scope = self.adapter.permission_scope(actor_id)
            key = build_cache_key(actor_id=actor_id, permission_scope=permission_scope, day=day)
            cached = self.cache.get(key)
            if cached is not None:
                payload = dict(cached)
                cache_hit = True
            else:
                rows = self.adapter.query(actor_id=actor_id, day=day, max_weighings=self.limits.max_weighings)
                payload = summarize_daily_rows(rows, day=day, max_groups=self.limits.max_groups, max_weighings=self.limits.max_weighings)
                payload["as_of_utc"] = (
                    getattr(self.adapter, "snapshot_at_utc", None)
                    or _utc(self._snapshot_clock()).isoformat()
                )
                self.cache.set(key, payload, self.limits.ttl_seconds)
            payload["trace_id"] = trace_id
            payload["adapter"] = getattr(self.adapter, "auth_state", "UNKNOWN")
            payload["cache_hit"] = cache_hit
            return payload
        except DailyQueryError as error:
            status = error.code
            raise
        finally:
            self.audit.append({
                "trace_id": trace_id,
                "actor_id": actor_id,
                "intent": intent,
                "date_lima": day.isoformat() if day else None,
                "latency_ms": round((self._clock() - started) * 1000, 3),
                "cache_hit": cache_hit,
                "status": "cache" if cache_hit else status,
            })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local bounded SCM daily summary prototype")
    parser.add_argument("--intent", required=True, choices=[INTENT_PRODUCTION_DAILY_SUMMARY])
    parser.add_argument("--actor-id", required=True, type=int)
    parser.add_argument("--date", default="ayer", dest="date_value")
    args = parser.parse_args(argv)
    # Keep the local CLI connection read-only before Flask creates its engine.
    os.environ.setdefault("PGOPTIONS", "-c default_transaction_read_only=on")
    from app import create_app
    from app.extensions import db
    from sqlalchemy.orm import Session

    app = create_app()
    with app.app_context():
        # A dedicated session avoids the Flask scoped_session's implicit work
        # before SET TRANSACTION and makes the rollback boundary explicit.
        with Session(db.engine) as session:
            try:
                service = DailyQueryService(LocalProductionDailyAdapter(session, config=app.config))
                print(json.dumps(service.execute(intent=args.intent, actor_id=args.actor_id, date_value=args.date_value), ensure_ascii=False, sort_keys=True))
            finally:
                session.rollback()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the local CLI smoke command
    raise SystemExit(main())
