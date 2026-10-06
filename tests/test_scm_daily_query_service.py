from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.scm_daily_query_service import (
    ADAPTER_VERSION,
    DailyLimits,
    DailyQueryError,
    DailyQueryLog,
    DailyQueryService,
    DailySummaryCache,
    PendingAuthAdapter,
    INTENT_PRODUCTION_DAILY_SUMMARY,
    build_cache_key,
    resolve_lima_date,
    summarize_daily_rows,
    authorize_daily_actor,
)
import app.services.scm_daily_query_service as daily_service


DAY = date(2026, 10, 5)


def _row(*, of="OF-1", color="ROJO", kg="10", target="20", cancelled=False, corrida="C1", at="2026-10-05T12:00:00+00:00"):
    return {
        "pesada_at": datetime.fromisoformat(at),
        "of": of,
        "color": color,
        "effective_kg": kg,
        "original_kg": kg,
        "cancelled": cancelled,
        "target_kg": target,
        "target_unit": "KG",
        "target_kind": "NETA",
        "target_identity": (corrida, "NETA", "KG"),
    }


def test_resolve_lima_ayer_uses_the_lima_calendar():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert resolve_lima_date("ayer", now=now) == DAY
    assert resolve_lima_date("2026-10-05", now=now) == DAY


def test_summary_groups_effective_and_cancelled_separately_without_daily_target_comparison():
    result = summarize_daily_rows(
        [_row(kg="10"), _row(kg="5"), _row(kg="99", cancelled=True)],
        day=DAY,
    )
    group = result["groups"][0]
    assert group["weighings"] == 2
    assert group["effective_kg"] == 15.0
    assert group["cancelled_weighings"] == 1
    assert group["cancelled_kg"] == 99.0
    assert group["net_kg"] == 15.0
    assert "weighing_ids" in group and "manga_ids" in group
    assert group["target_comparison"] is None
    assert group["target_reason"] == "DAILY_WEIGHT_TOTAL_IS_NOT_OF_PROGRESS"
    assert result["totals"] == {
        "effective_weighings": 2,
        "effective_kg": 15.0,
        "cancelled_weighings": 1,
        "cancelled_kg": 99.0,
        "net_kg": 15.0,
        "excluded_count": 0,
    }


def test_summary_reports_non_vigente_rows_as_excluded():
    result = summarize_daily_rows(
        [{
            "excluded": True,
            "weighing_id": 44,
            "manga_id": 9,
            "pesada_at": datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
        }],
        day=DAY,
    )
    assert result["totals"]["excluded_count"] == 1
    assert result["excluded"] == {
        "count": 1,
        "weighing_ids": [44],
        "manga_ids": [9],
        "reasons": {"UNKNOWN": 1},
    }


def test_group_ids_keep_effective_and_cancelled_cardinalities_separate():
    effective = _row(kg="10")
    effective.update(weighing_id=1, manga_id=8)
    cancelled = _row(kg="99", cancelled=True)
    cancelled.update(weighing_id=2, manga_id=9, effective_kg="98")
    group = summarize_daily_rows([effective, cancelled], day=DAY)["groups"][0]
    assert group["weighing_ids"] == [1]
    assert group["manga_ids"] == [8]
    assert group["cancelled_weighing_ids"] == [2]
    assert group["cancelled_manga_ids"] == [9]
    assert group["cancelled_kg"] == 98.0


def test_summary_does_not_compare_mixed_or_non_kg_targets():
    row = _row(target="20")
    row["target_unit"] = "UN"
    result = summarize_daily_rows([row], day=DAY)
    assert result["groups"][0]["target_comparison"] is None
    assert result["groups"][0]["target_reason"] == "DAILY_WEIGHT_TOTAL_IS_NOT_OF_PROGRESS"


def test_reopened_weighing_is_excluded_and_effective_assignment_helper_is_used(monkeypatch):
    work = SimpleNamespace(id="work-effective", trabajo_color=None)
    corrida = SimpleNamespace(
        id="run-1",
        codigo="C1",
        orden_fabricacion=SimpleNamespace(
            orden_operacion=SimpleNamespace(codigo="OF-1", estado="EN_EJECUCION")
        ),
        salidas=[],
        objetivo_neto_kg=None,
    )
    work.trabajo_color = SimpleNamespace(corrida=corrida)
    manga = SimpleNamespace(id=1, trabajo=SimpleNamespace(), color_snapshot="ROJO")
    weighing = SimpleNamespace(
        estado="REABIERTO",
        manga=manga,
        pesada_at=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
        peso_fisico_neto_kg="4.000",
    )
    assert daily_service._row_from_weighing(weighing) is None
    weighing.estado = "VIGENTE"
    monkeypatch.setattr(daily_service, "effective_work", lambda _manga: work)
    row = daily_service._row_from_weighing(weighing)
    assert row["of"] == "OF-1"


def test_older_vigente_row_is_excluded_when_a_later_row_is_current():
    weighing = SimpleNamespace(
        id=10,
        estado="VIGENTE",
        manga=SimpleNamespace(id=7),
        pesada_at=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
    )
    row = daily_service._row_from_weighing(
        weighing,
        include_excluded=True,
        latest_vigente_ids={11},
    )
    assert row["excluded"] is True
    assert row["excluded_reason"] == "NOT_LATEST_VIGENTE_FOR_MANGA"


class _Clock:
    value = 100.0

    def __call__(self):
        return self.value


class _FakeAdapter:
    auth_state = "LOCAL_NO_SECRET"

    def __init__(self):
        self.calls = []

    def permission_scope(self, actor_id):
        self.calls.append(("permission", actor_id))
        return f"actor:{actor_id}:OT_VER,MANGA_PESAJE_VER"

    def query(self, *, actor_id, day, max_weighings):
        self.calls.append(("query", actor_id, day, max_weighings))
        return [_row(of=f"OF-{actor_id}")]


def test_cache_miss_uses_adapter_transaction_snapshot_for_as_of():
    adapter = _FakeAdapter()
    adapter.snapshot_at_utc = "2026-10-06T06:32:41+00:00"
    service = DailyQueryService(adapter)
    result = service.execute(intent=INTENT_PRODUCTION_DAILY_SUMMARY, actor_id=1, date_value=DAY)
    assert result["as_of_utc"] == adapter.snapshot_at_utc


def test_cache_key_and_service_are_actor_and_permission_scoped():
    clock = _Clock()
    cache = DailySummaryCache(clock=clock)
    adapter = _FakeAdapter()
    service = DailyQueryService(
        adapter,
        limits=DailyLimits(ttl_seconds=10),
        cache=cache,
        audit=DailyQueryLog(),
        clock=clock,
    )
    first = service.execute(intent=INTENT_PRODUCTION_DAILY_SUMMARY, actor_id=1, date_value=DAY)
    cached = service.execute(intent=INTENT_PRODUCTION_DAILY_SUMMARY, actor_id=1, date_value=DAY)
    other_actor = service.execute(intent=INTENT_PRODUCTION_DAILY_SUMMARY, actor_id=2, date_value=DAY)
    assert first["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert cached["as_of_utc"] == first["as_of_utc"]
    assert other_actor["cache_hit"] is False
    assert [call[0] for call in adapter.calls].count("query") == 2
    assert build_cache_key(actor_id=1, permission_scope="A", day=DAY) != build_cache_key(actor_id=2, permission_scope="A", day=DAY)
    assert build_cache_key(actor_id=1, permission_scope="A", day=DAY, version=ADAPTER_VERSION)
    assert service.invalidate(actor_id=1) == 1


def test_cache_ttl_and_actor_invalidation():
    clock = _Clock()
    cache = DailySummaryCache(clock=clock)
    key_a = build_cache_key(actor_id=1, permission_scope="A", day=DAY)
    key_b = build_cache_key(actor_id=2, permission_scope="A", day=DAY)
    cache.set(key_a, {"value": "a"}, 5)
    cache.set(key_b, {"value": "b"}, 5)
    assert cache.get(key_a) == {"value": "a"}
    assert cache.invalidate(actor_id=1) == 1
    assert cache.get(key_a) is None
    assert cache.get(key_b) == {"value": "b"}
    clock.value += 6
    assert cache.get(key_b) is None


def test_only_supported_intent_and_remote_auth_pending_are_exposed():
    service = DailyQueryService(PendingAuthAdapter())
    with pytest.raises(DailyQueryError) as error:
        service.execute(intent="free_sql", actor_id=1, date_value=DAY)
    assert error.value.code == "INTENT_NOT_ALLOWED"
    with pytest.raises(DailyQueryError) as error:
        service.execute(intent=INTENT_PRODUCTION_DAILY_SUMMARY, actor_id=1, date_value=DAY)
    assert error.value.code == "SCM_AUTH_PENDING"
    assert "token" not in str(service.audit.list()).lower()


def test_daily_access_is_reassignable_by_capability_without_allowlist(monkeypatch):
    class Actor:
        id = 4
        codigo = "TRB-000003"

        def tiene_capacidad(self, capability):
            return capability in {"OT_VER", "MANGA_PESAJE_VER", "ASISTENTE_PRODUCCION_USAR"}

    monkeypatch.setattr(daily_service, "load_actor", lambda *_args, **_kwargs: Actor())
    actor = authorize_daily_actor(object(), 4, {})
    assert actor.id == 4


def test_daily_access_requires_assistant_capability(monkeypatch):
    class Actor:
        id = 4

        def tiene_capacidad(self, capability):
            return capability in {"OT_VER", "MANGA_PESAJE_VER"}

    monkeypatch.setattr(daily_service, "load_actor", lambda *_args, **_kwargs: Actor())
    with pytest.raises(DailyQueryError) as error:
        authorize_daily_actor(object(), 4, {})
    assert error.value.code == "ASISTENTE_PRODUCCION_USAR_REQUIRED"
