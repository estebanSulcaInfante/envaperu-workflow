from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from app import db
from app.models.scm_ot import ScmManga
from app.services import scm_ot_service


def test_calidad_view_is_an_opt_in_read_view():
    assert scm_ot_service._summary_view("calidad") == "calidad"


def test_quality_summary_exposes_work_mold_and_cycle_context():
    work = SimpleNamespace(
        id=uuid4(), codigo="TC-1", secuencia=1, estado="EN_EJECUCION",
        version=1, orden_fabricacion_id=uuid4(), orden_fabricacion_codigo="OF-1",
        corrida_fabricacion_id=uuid4(), corrida_codigo="C-1", color="ROJO",
        color_hex="#f00", molde_id="M-1", molde_nombre="Molde uno",
        snapshot_tiempo_ciclo_seg="30.0000", articulos_salida=[
            {"id": 10, "codigo": "P-1", "nombre": "Pieza", "pieza_id": 7}
        ], cantidad_objetivo_un=10, cantidad_confirmada_un=0,
        asignacion_vigente=None,
    )
    payload = scm_ot_service._serialize_ot_summary(
        SimpleNamespace(
            id=1, public_id=uuid4(), codigo_ot="OT-1", estado="EN_EJECUCION",
            tipo_ot="FABRICACION", version=1, fecha=None, turno="DIA",
            maquina_id=1, maquina_nombre_snapshot="M1", maquina_codigo_snapshot="M1",
            maquina=None, centro_trabajo=None, responsable_id=None,
            responsable=None, maquinista_previsto_id=None, maquinista_previsto=None,
            modo_ejecucion_ensamble=None, orden_operacion_id=None,
            cantidad_objetivo=10, cantidad_confirmada=0,
        ),
        works=[work], manga_summaries={}, quality=True,
    )
    item = payload["trabajos_color_resumen"][0]
    assert item["molde_id"] == "M-1"
    assert item["molde_nombre"] == "Molde uno"
    assert item["snapshot_tiempo_ciclo_seg"] == "30.0000"
    assert item["articulos_salida"][0]["pieza_id"] == 7


def test_quality_journey_and_ot_detail_are_opt_in_and_keep_assembly_shape(
    app, client, scm_config,
):
    from test_scm_production_observability import _actor, _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        quality_actor = _actor("OBS-CAL", {"OT_VER", "OF_VER"})
        actor_id = quality_actor.id
        journey = client.get(
            "/api/scm/v1/jornadas-planta",
            query_string={
                "vista": "calidad",
                "fecha_operativa": "2026-08-09",
                "turno": "DIA",
            },
            headers={"X-Actor-Id": str(actor_id)},
        )
        assert journey.status_code == 200
        payload = journey.get_json()
        work = payload["ots_fabricacion"][0]["trabajos_color_resumen"][0]
        assert {"molde_id", "molde_nombre", "snapshot_tiempo_ciclo_seg"} <= set(work)
        ot_id = payload["ots_fabricacion"][0]["public_id"]
        detail = client.get(
            f"/api/scm/v1/ots/{ot_id}",
            query_string={"vista": "calidad"},
            headers={"X-Actor-Id": str(actor_id)},
        )
        assert detail.status_code == 200
        detail_payload = detail.get_json()["ot"]
        assert "mangas" in detail_payload
        assert "ots_armado" in detail_payload


def test_quality_projection_keeps_ot_ver_capability_boundary(app, client, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        denied_journey = client.get(
            "/api/scm/v1/jornadas-planta",
            query_string={
                "vista": "calidad",
                "fecha_operativa": "2026-08-09",
                "turno": "DIA",
            },
            headers={"X-Actor-Id": str(seeded["base"].id)},
        )
        denied_list = client.get(
            "/api/scm/v1/ots",
            query_string={"vista": "calidad"},
            headers={"X-Actor-Id": str(seeded["base"].id)},
        )
        denied_detail = client.get(
            f"/api/scm/v1/ots/{seeded['fab'].public_id}",
            query_string={"vista": "calidad"},
            headers={"X-Actor-Id": str(seeded["base"].id)},
        )

    assert denied_journey.status_code == 403
    assert denied_journey.get_json()["error"]["details"] == {
        "capabilities_any": ["CALIDAD_CONTROL_REGISTRAR", "OT_VER+OF_VER"]
    }
    assert denied_list.status_code == 403
    assert denied_list.get_json()["error"]["details"] == {
        "capabilities_any": ["CALIDAD_CONTROL_REGISTRAR", "OT_VER+OF_VER"]
    }
    assert denied_detail.status_code == 403


def test_quality_inspector_reads_bounded_projection_without_normal_ot_navigation(
    app, client, scm_config,
):
    from test_scm_production_observability import _actor, _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        inspector = _actor("INSPECTOR-QUALITY", {"CALIDAD_CONTROL_REGISTRAR"})
        quality_detail = client.get(
            f"/api/scm/v1/ots/{seeded['fab'].public_id}",
            query_string={"vista": "calidad"},
            headers={"X-Actor-Id": str(inspector.id)},
        )
        assert quality_detail.status_code == 200
        normal_detail = client.get(
            f"/api/scm/v1/ots/{seeded['fab'].public_id}",
            headers={"X-Actor-Id": str(inspector.id)},
        )
        assert normal_detail.status_code == 403
        quality_visits = client.get(
            f"/api/scm/v1/ots/{seeded['fab'].public_id}/calidad-visitas",
            headers={"X-Actor-Id": str(inspector.id)},
        )
        assert quality_visits.status_code == 200


@pytest.mark.parametrize("two_works", [False, True])
def test_quality_detail_batches_work_context_reads_for_variable_work_volume(
    app, client, scm_config, two_works,
):
    from test_scm_ot_service import _seed_aggregate_color_work, _seed_two_work_aggregate

    with app.app_context():
        if two_works:
            seeded = _seed_two_work_aggregate(first_quantity=20)
            header = seeded["header"]
            actor = seeded["creator"]
        else:
            actor, _approver, _order, _run, _output, _line, header, _created = (
                _seed_aggregate_color_work(quantity=20)
            )
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            response = client.get(
                f"/api/scm/v1/ots/{header['public_id']}",
                query_string={"vista": "calidad"},
                headers={"X-Actor-Id": str(actor.id)},
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)

    assert response.status_code == 200
    assert len(statements) < 50


@pytest.mark.parametrize("quantity", [20, 1200])
def test_quality_detail_sql_does_not_scale_with_manga_rows(
    app, client, scm_config, quantity,
):
    from test_scm_ot_service import _seed_aggregate_color_work

    with app.app_context():
        actor, _approver, _order, _run, _output, _line, header, _created = (
            _seed_aggregate_color_work(quantity=quantity)
        )
        manga_count = ScmManga.query.filter_by(ot_id=header["id"]).count()
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            response = client.get(
                f"/api/scm/v1/ots/{header['public_id']}",
                query_string={"vista": "calidad"},
                headers={"X-Actor-Id": str(actor.id)},
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)

    assert response.status_code == 200
    assert manga_count > 1 if quantity > 20 else manga_count == 1
    assert len(statements) < 50
