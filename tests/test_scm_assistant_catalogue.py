from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app.services.scm_assistant_catalogue as catalogue


NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def test_catalogue_is_bounded_and_suggestions_are_replayable_queries():
    result = catalogue.get_catalogue()
    assert result["read_only"] is True
    assert len(result["intents"]) == 4
    assert all(set(item) == {"label", "query"} for item in result["suggestions"])
    assert all(catalogue.plan_query(item["query"], now=NOW)["status"] == "answered" for item in result["suggestions"])


def test_plan_resolves_relative_date_in_lima_and_keeps_source_date():
    result = catalogue.plan_query("Resumen de producción de ayer", now=NOW)
    assert result["status"] == "answered"
    assert result["plan"][0] == {
        "intent": catalogue.INTENT_PRODUCTION_DAILY_SUMMARY,
        "parameters": {"date_lima": "2026-10-05"},
    }


def test_progress_uses_exact_of_and_exact_color_without_fuzzy_conversion():
    azul = catalogue.plan_query("Avance de OF OF-123 color Azul", now=NOW)
    azure = catalogue.plan_query("Avance de OF OF-123 color Azure", now=NOW)
    assert azul["plan"][0]["parameters"]["color"] == "Azul"
    assert azure["plan"][0]["parameters"]["color"] == "Azure"
    assert azure["plan"][0]["parameters"]["color"] != azul["plan"][0]["parameters"]["color"]
    data = {"items": [{"color": "Azul"}, {"color": "Azulado"}], "as_of": "cut"}
    assert [item["color"] for item in catalogue._filter_progress_exact_color(data, "Azul")["items"]] == ["Azul"]


@pytest.mark.parametrize(
    "query",
    [
        "Pesajes del 2026-10-01 al 2026-10-03",
        "Pesajes del 01/10/2026 al 03/10/2026",
    ],
)
def test_period_requires_explicit_lima_endpoints(query):
    result = catalogue.plan_query(query, now=NOW)
    assert result["status"] == "answered"
    params = result["plan"][0]["parameters"]
    assert params["max_weighings"] == 500
    assert params["fecha_desde"] == "2026-10-01"
    assert params["fecha_hasta"] == "2026-10-03"


def test_period_over_cap_and_missing_date_are_clarifications_without_a_plan():
    assert catalogue.plan_query("Pesajes del 2026-10-01 al 2026-10-08")["status"] == "needs_clarification"
    missing = catalogue.plan_query("Pesajes de esta semana")
    assert missing["status"] == "needs_clarification"
    assert missing["plan"] == []


def test_manga_requires_an_exact_identifier_and_preserves_public_uuid():
    missing = catalogue.plan_query("Trazabilidad de una manga")
    assert missing["status"] == "needs_clarification"
    public_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    result = catalogue.plan_query(f"Detalle de manga {public_id}")
    assert result["status"] == "answered"
    assert result["plan"][0]["parameters"] == {"public_id": public_id}


@pytest.mark.parametrize("query", ["drop table scm_manga", "Anula la manga MG-1", "resumen; select * from scm_manga"])
def test_writes_and_injection_are_unsupported_without_execution(query):
    result = catalogue.plan_query(query)
    assert result["status"] == "unsupported"
    assert result["plan"] == []


def test_composition_is_limited_to_two_explicit_clauses_and_validated_as_a_whole():
    result = catalogue.plan_query("Resumen de producción de ayer y además avance de OF OF-1")
    assert result["status"] == "answered"
    assert len(result["plan"]) == 2
    too_many = catalogue.plan_query(
        "Resumen de producción de ayer y además avance de OF OF-1 y además detalle de manga MG-1"
    )
    assert too_many["status"] == "unsupported"


def test_validate_plan_rejects_unknown_fields_and_unanchored_model_entities():
    plan = [{"intent": catalogue.INTENT_PRODUCTION_ORDER_PROGRESS, "parameters": {"of": "OF-1"}}]
    assert catalogue.validate_plan(plan + [{"intent": catalogue.INTENT_MANGA_TRACE, "parameters": {"codigo": "MG-1"}}], "Avance de OF OF-1")
    assert catalogue.validate_plan(plan, "Avance de OF OF-2")
    assert catalogue.validate_plan([{"intent": catalogue.INTENT_PRODUCTION_ORDER_PROGRESS, "parameters": {"of": "OF-1", "sql": "drop"}}])


def test_execute_plan_keeps_daily_and_accumulated_results_separate(monkeypatch):
    monkeypatch.setattr(catalogue, "_prepare_read_only_transaction", lambda session: "2026-10-06T12:00:00+00:00")
    monkeypatch.setattr(catalogue, "_authorize_assistant_read", lambda session, actor_id: None)
    monkeypatch.setattr(catalogue, "_preflight_progress_size", lambda session, of_id: None)
    monkeypatch.setattr(catalogue, "_execute_daily", lambda *args, **kwargs: ({"totals": {"effective_kg": 10}}, "daily-cut"))
    monkeypatch.setattr(catalogue, "_resolve_of_id", lambda session, code: "of-id")
    monkeypatch.setattr(catalogue, "list_production_progress", lambda *args, **kwargs: {"items": [{"of": "OF-1", "kg_finalizados_efectivos": 50}], "as_of": "2026-10-06"})
    plan = catalogue.plan_query("Resumen de producción de ayer y además avance de OF OF-1", now=NOW)["plan"]
    from sqlalchemy.orm import Session
    with Session() as session:
        results = catalogue.execute_plan(session, 7, plan)
    assert [item["intent"] for item in results] == [catalogue.INTENT_PRODUCTION_DAILY_SUMMARY, catalogue.INTENT_PRODUCTION_ORDER_PROGRESS]
    assert results[0]["data"]["totals"]["effective_kg"] == 10
    assert results[1]["data"]["items"][0]["kg_finalizados_efectivos"] == 50
    assert results[0]["as_of_utc"] == "daily-cut"
    assert results[1]["as_of_utc"] == "2026-10-06T12:00:00+00:00"


def test_manga_resolution_reports_bounded_ambiguity(monkeypatch):
    class Scalars:
        def all(self):
            return ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]

    class Session:
        def scalars(self, statement):
            assert "LIMIT" in str(statement.compile(compile_kwargs={"literal_binds": True})).upper()
            return Scalars()

    with pytest.raises(catalogue.CatalogueQueryError) as error:
        catalogue._resolve_manga_public_id(Session(), {"codigo": "MG-1"})
    assert error.value.code == "MANGA_AMBIGUOUS"
    assert len(error.value.details["choices"]) == 2


def test_recommended_daily_and_of_examples_resolve_canonical_identifiers():
    assert catalogue.plan_query('Que pesajes hubo hoy?',now=NOW)['plan'][0]['intent']==catalogue.INTENT_PRODUCTION_DAILY_SUMMARY
    for query in ('Progreso de OF-123 color Azul','Avance de OF OF-123','Avance de OF 123'):
        assert catalogue.plan_query(query,now=NOW)['plan'][0]['parameters']['of']=='OF-000123'
