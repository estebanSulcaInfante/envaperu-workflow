"""Focused tests for the opt-in OT journey read model."""

import json
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event

from app import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_auditoria import ScmOperacion
from app.models.scm_production_orders import ScmOrdenOperacionSalida
from app.models.scm_ot import (
    ScmAsignacionPlanMangaOt,
    ScmCorreccionAsignacionManga,
    ScmEtiquetaManga,
    ScmPlanMangaOpLinea,
    ScmManga,
    ScmTramoMangaTrabajo,
    ScmTrabajoImpresionManga,
    ScmTrabajoOt,
)
from app.services import scm_ot_service
from app.services.scm_service_support import ScmServiceError


def _worker(worker_id=7, name="Renato Peña"):
    return SimpleNamespace(id=worker_id, nombre_completo=name)


def _header(*, tipo="FABRICACION"):
    worker = _worker()
    machine = SimpleNamespace(id=13, nombre="Haitian 3000", codigo="INY-13")
    center = SimpleNamespace(id=4, codigo="CENTRO-4", nombre="Mesa 4", tipo="ARMADO")
    return SimpleNamespace(
        id=31,
        public_id=uuid4(),
        codigo_ot="OT-000031",
        estado="EN_EJECUCION",
        tipo_ot=tipo,
        version=3,
        fecha=None,
        turno="DIA",
        maquina_id=machine.id,
        maquina_nombre_snapshot=machine.nombre,
        maquina_codigo_snapshot=machine.codigo,
        maquina=machine,
        centro_trabajo=center,
        centro_trabajo_id=center.id,
        responsable_id=worker.id,
        responsable=worker,
        maquinista_previsto_id=worker.id,
        maquinista_previsto=worker,
        modo_ejecucion_ensamble="MESA" if tipo == "ENSAMBLE" else None,
        orden_operacion_id=uuid4(),
        cantidad_objetivo=100,
        cantidad_confirmada=40,
        trabajos_ot=[],
        trabajo_color_contexto_id=None,
        ot_fabricacion_contexto_id=None,
    )


def test_summary_payload_is_compact_and_never_calls_full_serializer(monkeypatch):
    header = _header()
    work = SimpleNamespace(
        id=uuid4(),
        codigo="TC-31-01",
        secuencia=1,
        estado="EN_EJECUCION",
        version=2,
        orden_operacion_id=header.orden_operacion_id,
        orden_fabricacion_id=header.orden_operacion_id,
        orden_fabricacion_codigo="OF-000031",
        corrida_fabricacion_id=uuid4(),
        corrida_codigo="C01",
        color="AZUL SÓLIDO",
        color_hex="#123456",
        articulos_salida=[{"id": 1, "codigo": "PC-AZUL", "nombre": "Pieza azul"}],
        cantidad_objetivo_un=100,
        cantidad_confirmada_un=40,
        asignacion_vigente=None,
    )
    counts = {work.id: {
        "total": 2,
        "cerradas": 1,
        "abiertas": 1,
        "etiqueta_generada": 0,
        "etiqueta_impresa": 0,
        "preetiqueta_sin_estado": 0,
        "pendientes": 0,
        "anuladas": 0,
    }}
    monkeypatch.setattr(
        scm_ot_service, "_serialize_ot", lambda *_a, **_kw: pytest.fail("full serializer used")
    )

    payload = scm_ot_service._serialize_ot_summary(
        header,
        works=[work],
        manga_summaries=counts,
    )

    assert set(payload) == {
        "public_id", "codigo_ot", "estado", "tipo_ot", "version",
        "fecha_operativa", "turno", "maquina_id", "maquina", "maquina_codigo",
        "centro_trabajo", "responsable_id", "responsable", "maquinista_previsto_id",
        "maquinista_previsto", "modo_ejecucion_ensamble", "orden_operacion_id",
        "cantidad_objetivo", "cantidad_confirmada", "trabajos_color_resumen",
        "mangas_resumen",
    }
    assert "mangas" not in payload
    assert "detalles" not in payload
    assert "trabajos_color" not in payload
    assert payload["trabajos_color_resumen"][0]["mangas_resumen"] == counts[work.id]


def test_manga_summary_uses_frontend_state_buckets():
    rows = [
        ("PLANIFICADA", None, 2),
        ("EN_LLENADO", None, 1),
        ("PESADA", None, 1),
        ("PREETIQUETADA", "GENERADA", 1),
        ("PREETIQUETADA", "IMPRESA", 2),
        ("PREETIQUETADA", None, 1),
        ("ANULADA", None, 3),
        ("BORRADA", None, 4),
    ]

    summary = scm_ot_service._empty_manga_summary()
    for state, label_state, count in rows:
        scm_ot_service._accumulate_manga_summary(
            summary, state=state, label_state=label_state, count=count
        )

    assert summary == {
        "total": 11,
        "cerradas": 1,
        "abiertas": 1,
        "etiqueta_generada": 1,
        "etiqueta_impresa": 2,
        "preetiqueta_sin_estado": 1,
        "pendientes": 2,
        "anuladas": 3,
        "por_estado": {
            "PLANIFICADA": 2,
            "EN_LLENADO": 1,
            "PESADA": 1,
            "PREETIQUETADA": 4,
            "ANULADA": 3,
            "BORRADA": 4,
        },
    }


def test_invalid_summary_view_is_explicit_400(monkeypatch):
    class Session:
        pass

    monkeypatch.setattr(scm_ot_service, "load_actor", lambda *_args, **_kwargs: None)
    with pytest.raises(ScmServiceError) as error:
        scm_ot_service.list_ots(Session(), actor_id=1, view="compacto")
    assert error.value.status_code == 400
    assert error.value.code == "OT_VIEW_INVALID"


def test_summary_integration_counts_work_mangas_without_full_graph(app, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        pending = ScmManga.query.filter_by(codigo="M-OT-OBS-FAB-01").one()
        print_job = ScmTrabajoImpresionManga(
            public_id=uuid4(),
            estado="PROCESADO",
            payload_hash="d" * 64,
            solicitado_por_id=seeded["full"].id,
        )
        db.session.add(print_job)
        db.session.flush()
        db.session.add(ScmEtiquetaManga(
            manga_id=pending.id,
            trabajo_impresion_id=print_job.public_id,
            tipo="PREPESAJE",
            version=1,
            estado="IMPRESA",
            payload_json={},
            payload_hash="e" * 64,
        ))
        db.session.commit()
        payload = scm_ot_service.list_ots(
            db.session,
            actor_id=seeded["full"].id,
            operational_date="2026-08-09",
            shift="DIA",
            tipo_ot="FABRICACION",
            view="resumen",
        )
        db.session.expire_all()
        legacy = scm_ot_service.list_ots(
            db.session,
            actor_id=seeded["full"].id,
            operational_date="2026-08-09",
            shift="DIA",
            tipo_ot="FABRICACION",
        )

    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert set(item) == {
        "public_id", "codigo_ot", "estado", "tipo_ot", "version",
        "fecha_operativa", "turno", "maquina_id", "maquina", "maquina_codigo",
        "centro_trabajo", "responsable_id", "responsable", "maquinista_previsto_id",
        "maquinista_previsto", "modo_ejecucion_ensamble", "orden_operacion_id",
        "cantidad_objetivo", "cantidad_confirmada", "trabajos_color_resumen",
        "mangas_resumen",
    }
    assert [work["codigo"] for work in item["trabajos_color_resumen"]] == [
        "TC-OBS-ROJO", "TC-OBS-AZUL"
    ]
    assert item["trabajos_color_resumen"][0]["mangas_resumen"]["etiqueta_impresa"] == 1
    assert item["trabajos_color_resumen"][1]["mangas_resumen"]["cerradas"] == 2
    assert item["mangas_resumen"]["total"] == 4
    legacy_by_code = {
        work["codigo"]: work for work in legacy["items"][0]["trabajos_color"]
    }
    for work in item["trabajos_color_resumen"]:
        expected = scm_ot_service._empty_manga_summary()
        for manga in legacy_by_code[work["codigo"]]["mangas"]:
            label = manga.get("etiqueta_vigente") or {}
            scm_ot_service._accumulate_manga_summary(
                expected,
                state=manga.get("estado"),
                label_state=label.get("estado"),
                count=1,
            )
        assert work["mangas_resumen"] == expected


def test_assembly_summary_keeps_order_supply_and_context_compact(app, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        payload = scm_ot_service.list_ots(
            db.session,
            actor_id=seeded["full"].id,
            operational_date="2026-08-10",
            shift="DIA",
            tipo_ot="ENSAMBLE",
            view="resumen",
        )

    item = payload["items"][0]
    assert item["orden_armado"]["codigo"] == "OA-OBS-001"
    assert set(item["orden_armado"]) == {"id", "codigo", "salida"}
    assert "mangas" not in item
    assert item["mangas_resumen"]["total"] == 1


def test_assembly_summary_keeps_concurrent_fabrication_context_compact(app, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        assembly = seeded["assembly"]
        red = ScmTrabajoOt.query.filter_by(codigo="TC-OBS-ROJO").one()
        assembly.modo_ejecucion_ensamble = "CONCURRENTE"
        assembly.trabajo_color_contexto_id = red.id
        assembly.ot_fabricacion_contexto_id = seeded["fab"].public_id
        db.session.commit()
        payload = scm_ot_service.list_ots(
            db.session,
            actor_id=seeded["full"].id,
            operational_date="2026-08-10",
            shift="DIA",
            tipo_ot="ENSAMBLE",
            view="resumen",
        )

    item = payload["items"][0]
    assert item["modo_ejecucion_ensamble"] == "CONCURRENTE"
    assert item["ot_fabricacion_contexto"]["codigo_ot"] == "OT-OBS-FAB"
    assert item["trabajo_color_contexto"]["codigo"] == "TC-OBS-ROJO"
    assert "mangas" not in item["trabajo_color_contexto"]


def test_summary_sql_growth_is_bounded_for_cold_1_25_100_fixture(app, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        prototype = ScmManga.query.filter_by(codigo="M-OT-OBS-FAB-02").one()
        counts = {}
        payload_sizes = {}
        for amount in (1, 25, 100):
            extras = []
            for index in range(amount):
                values = {
                    column.name: getattr(prototype, column.name)
                    for column in ScmManga.__table__.columns
                    if column.name not in {"id", "public_id", "codigo", "secuencia_ot"}
                }
                values.update({
                    "codigo": f"M-COLD-{amount:03d}-{index:03d}",
                    "secuencia_ot": 1000 + amount + index,
                })
                extras.append(ScmManga(**values))
            db.session.add_all(extras)
            db.session.commit()
            db.session.expire_all()
            query_count = 0

            def count_query(*_args):
                nonlocal query_count
                query_count += 1

            event.listen(db.engine, "before_cursor_execute", count_query)
            try:
                payload = scm_ot_service.list_ots(
                    db.session,
                    actor_id=seeded["full"].id,
                    operational_date="2026-08-09",
                    shift="DIA",
                    tipo_ot="FABRICACION",
                    view="resumen",
                )
            finally:
                event.remove(db.engine, "before_cursor_execute", count_query)
            legacy = scm_ot_service.list_ots(
                db.session,
                actor_id=seeded["full"].id,
                operational_date="2026-08-09",
                shift="DIA",
                tipo_ot="FABRICACION",
            )
            counts[amount] = query_count
            payload_sizes[amount] = {
                "summary_bytes": len(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)),
                "legacy_bytes": len(json.dumps(legacy, sort_keys=True, separators=(",", ":"), default=str)),
            }
            assert payload["items"][0]["mangas_resumen"]["total"] >= amount + 4
            db.session.query(ScmManga).filter(
                ScmManga.codigo.like(f"M-COLD-{amount:03d}-%")
            ).delete(synchronize_session=False)
            db.session.commit()

        assert counts[100] <= counts[1] + 4
        assert counts[100] <= counts[25] + 4
        print({"summary_sql_queries_1_25_100": counts, "summary_vs_legacy_bytes_1_25_100": payload_sizes})


def test_summary_route_forwards_opt_in_view_and_server_filters(app, client, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        response = client.get(
            "/api/scm/v1/ots",
            query_string={
                "vista": "resumen",
                "fecha_operativa": "2026-08-09",
                "turno": "DIA",
                "maquina_id": "1",
            },
            headers={"X-Actor-Id": str(seeded["full"].id)},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["fecha_operativa"] == "2026-08-09"
    invalid = client.get(
        "/api/scm/v1/ots",
        query_string={
            "vista": "resumen",
            "fecha_operativa": "2026-08-09",
            "turno": "XYZ",
        },
        headers={"X-Actor-Id": str(seeded["full"].id)},
    )
    assert invalid.status_code == 400


def test_journey_summary_route_keeps_both_families_and_machine_filter(app, client, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        response = client.get(
            "/api/scm/v1/jornadas-planta",
            query_string={
                "vista": "resumen",
                "fecha_operativa": "2026-08-09",
                "turno": "DIA",
                "maquina_id": "1",
            },
            headers={"X-Actor-Id": str(seeded["full"].id)},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["fecha_operativa"] == "2026-08-09"
    assert len(payload["ots_fabricacion"]) == 1
    assert payload["ots_armado"] == []
    invalid = client.get(
        "/api/scm/v1/jornadas-planta",
        query_string={
            "vista": "resumen",
            "fecha_operativa": "2026-08-09",
            "turno": "XYZ",
        },
        headers={"X-Actor-Id": str(seeded["full"].id)},
    )
    assert invalid.status_code == 400


def test_summary_output_articles_follow_current_run_not_sibling_run(app, scm_config):
    from test_scm_ot_service import _seed_two_work_aggregate

    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = seeded["creator"]
        second_run = seeded["runs"][1]
        second_output = ScmOrdenOperacionSalida.query.filter_by(
            corrida_fabricacion_id=second_run.id,
        ).one()
        alternate = ScmArticulo(
            codigo="PC-READ-RUN-B",
            nombre="Pieza color corrida B",
            clase="PIEZA_COLOR",
            unidad_inventario="KG",
        )
        db.session.add(alternate)
        db.session.flush()
        second_output.articulo_scm_id = alternate.id
        db.session.commit()
        db.session.expire_all()
        summary = scm_ot_service.list_ots(
            db.session,
            actor_id=actor.id,
            operational_date="2026-08-11",
            shift="DIA",
            tipo_ot="FABRICACION",
            view="resumen",
        )
        legacy = scm_ot_service.list_ots(
            db.session,
            actor_id=actor.id,
            operational_date="2026-08-11",
            shift="DIA",
            tipo_ot="FABRICACION",
        )

    summary_by_code = {
        item["codigo"]: [article["codigo"] for article in item["articulos_salida"]]
        for item in summary["items"][0]["trabajos_color_resumen"]
    }
    legacy_by_code = {
        item["codigo"]: [article["codigo"] for article in item["articulos_salida"]]
        for item in legacy["items"][0]["trabajos_color"]
    }
    assert summary_by_code == legacy_by_code


def test_summary_manga_buckets_match_legacy_origin_and_continuity_segments(
    app, scm_config,
):
    from test_scm_ot_service import _seed_two_work_aggregate

    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = seeded["creator"]
        source_work = db.session.get(
            ScmTrabajoOt, UUID(seeded["first"]["trabajo_color"]["id"]),
        )
        target_work = db.session.get(
            ScmTrabajoOt, UUID(seeded["second"]["trabajo_color"]["id"]),
        )
        source_manga = source_work.mangas[0]
        target_assignment = target_work.asignaciones_personal[0]
        db.session.add(ScmTramoMangaTrabajo(
            manga_id=source_manga.id,
            trabajo_ot_id=target_work.id,
            asignacion_personal_trabajo_id=target_assignment.id,
            secuencia=1,
            estado="PROGRAMADO",
            cantidad_inicio_un=0,
            cantidad_atribuida_un=0,
            created_by_id=actor.id,
        ))
        db.session.commit()
        db.session.expire_all()
        summary = scm_ot_service.list_ots(
            db.session,
            actor_id=actor.id,
            operational_date="2026-08-11",
            shift="DIA",
            tipo_ot="FABRICACION",
            view="resumen",
        )
        legacy = scm_ot_service.list_ots(
            db.session,
            actor_id=actor.id,
            operational_date="2026-08-11",
            shift="DIA",
            tipo_ot="FABRICACION",
        )

    expected = {}
    for work in legacy["items"][0]["trabajos_color"]:
        bucket = scm_ot_service._empty_manga_summary()
        for manga in work["mangas"]:
            label = manga.get("etiqueta_vigente") or {}
            scm_ot_service._accumulate_manga_summary(
                bucket,
                state=manga.get("estado"),
                label_state=label.get("estado"),
                count=1,
            )
        expected[work["codigo"]] = bucket
    actual = {
        work["codigo"]: work["mangas_resumen"]
        for work in summary["items"][0]["trabajos_color_resumen"]
    }
    assert actual == expected


def test_summary_manga_buckets_match_legacy_assignment_correction_overlay(
    app, scm_config,
):
    from test_scm_ot_service import _seed_two_work_aggregate

    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = seeded["creator"]
        source_work = db.session.get(
            ScmTrabajoOt, UUID(seeded["first"]["trabajo_color"]["id"]),
        )
        target_work = db.session.get(
            ScmTrabajoOt, UUID(seeded["second"]["trabajo_color"]["id"]),
        )
        source_manga = source_work.mangas[0]
        target_assignment = target_work.asignaciones_personal[0]
        target_plan = ScmAsignacionPlanMangaOt.query.filter_by(
            trabajo_ot_id=target_work.id,
        ).one()
        operation_id = uuid4()
        db.session.add(ScmOperacion(
            operation_id=operation_id,
            endpoint="TEST /mangas/correccion",
            actor_id=actor.id,
            request_sha256="f" * 64,
        ))
        db.session.add(ScmCorreccionAsignacionManga(
            manga_id=source_manga.id,
            origen_ot_id=source_work.orden_trabajo_id,
            origen_trabajo_ot_id=source_work.id,
            destino_ot_id=target_work.orden_trabajo_id,
            destino_trabajo_ot_id=target_work.id,
            origen_asignacion_id=source_manga.asignacion_personal_trabajo_id,
            destino_asignacion_id=target_assignment.id,
            destino_asignacion_plan_id=target_plan.id,
            manga_version_antes=source_manga.version,
            manga_version_despues=source_manga.version + 1,
            motivo="Correccion sintetica para lectura",
            actor_id=actor.id,
            operation_id=operation_id,
        ))
        db.session.commit()
        db.session.expire_all()
        summary = scm_ot_service.list_ots(
            db.session,
            actor_id=actor.id,
            operational_date="2026-08-11",
            shift="DIA",
            tipo_ot="FABRICACION",
            view="resumen",
        )
        legacy = scm_ot_service.list_ots(
            db.session,
            actor_id=actor.id,
            operational_date="2026-08-11",
            shift="DIA",
            tipo_ot="FABRICACION",
        )

    expected = {}
    for work in legacy["items"][0]["trabajos_color"]:
        bucket = scm_ot_service._empty_manga_summary()
        for manga in work["mangas"]:
            label = manga.get("etiqueta_vigente") or {}
            scm_ot_service._accumulate_manga_summary(
                bucket,
                state=manga.get("estado"),
                label_state=label.get("estado"),
                count=1,
            )
        expected[work["codigo"]] = bucket
    actual = {
        work["codigo"]: work["mangas_resumen"]
        for work in summary["items"][0]["trabajos_color_resumen"]
    }
    assert actual == expected


@pytest.mark.parametrize("amount", (1, 25, 100))
def test_summary_cold_fabrication_headers_with_real_work_assignment_and_output(
    app, scm_config, amount,
):
    """Measure real Fabricacion headers, not empty synthetic OA rows."""
    from test_scm_ot_service import _seed_aggregate_color_work

    with app.app_context():
        creator, _approver, order, run, _output, line, _header, _created = (
            _seed_aggregate_color_work(quantity=20, operational_date="2026-08-11")
        )
        plan_line = db.session.get(ScmPlanMangaOpLinea, line["id"])
        plan_line.cantidad_objetivo_un = amount * 20
        db.session.commit()
        machine_id = order.fabricacion.maquina_prevista_id
        for index in range(1, amount):
            header = scm_ot_service.create_fabrication_ot_header(
                db.session,
                actor_id=creator.id,
                operation_id=uuid4(),
                data={
                    "maquina_id": machine_id,
                    "fecha_operativa": str(date(2026, 8, 11) + timedelta(days=index)),
                    "turno": "DIA",
                    "maquinista_predeterminado_id": creator.id,
                },
            )["ot"]
            scm_ot_service.add_color_work(
                db.session,
                actor_id=creator.id,
                ot_id=UUID(header["public_id"]),
                operation_id=uuid4(),
                data={
                    "corrida_fabricacion_id": str(run.id),
                    "maquinista_id": creator.id,
                    "asignaciones": [{
                        "plan_linea_id": line["id"],
                        "cantidad_un": 20,
                    }],
                },
            )

        db.session.expire_all()
        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(db.engine, "before_cursor_execute", count_query)
        try:
            summary = scm_ot_service.list_ots(
                db.session,
                actor_id=creator.id,
                shift="DIA",
                tipo_ot="FABRICACION",
                view="resumen",
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", count_query)
        legacy = scm_ot_service.list_ots(
            db.session,
            actor_id=creator.id,
            shift="DIA",
            tipo_ot="FABRICACION",
        )
        metrics = {
            "summary_queries": query_count,
            "summary_bytes": len(json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str)),
            "legacy_bytes": len(json.dumps(legacy, sort_keys=True, separators=(",", ":"), default=str)),
            "work_rows": sum(len(item["trabajos_color_resumen"]) for item in summary["items"]),
        }
        assert len(summary["items"]) == amount
        assert metrics["work_rows"] == amount
        assert all(item["trabajos_color_resumen"] for item in summary["items"])
        print({f"fabrication_header_summary_cold_{amount}": metrics})


def test_summary_work_scope_keeps_origin_manga_from_previous_ot_date(
    app, scm_config,
):
    """A destination OT must retain inherited origin mangas in its work bucket."""
    from test_scm_ot_service import _seed_two_work_aggregate

    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = seeded["creator"]
        order = seeded["order"]
        first_run = seeded["runs"][0]
        line = seeded["lines"][str(first_run.id)]
        destination_header = scm_ot_service.create_fabrication_ot_header(
            db.session,
            actor_id=actor.id,
            operation_id=uuid4(),
            data={
                "maquina_id": order.fabricacion.maquina_prevista_id,
                "fecha_operativa": "2026-08-12",
                "turno": "DIA",
                "maquinista_predeterminado_id": actor.id,
            },
        )["ot"]
        destination = scm_ot_service.add_color_work(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(destination_header["public_id"]),
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(first_run.id),
                "maquinista_id": actor.id,
                "asignaciones": [{
                    "plan_linea_id": line["id"],
                    "cantidad_un": 20,
                }],
            },
        )
        source_work = db.session.get(
            ScmTrabajoOt, UUID(seeded["first"]["trabajo_color"]["id"]),
        )
        destination_work = db.session.get(
            ScmTrabajoOt, UUID(destination["trabajo_color"]["id"]),
        )
        source_manga = source_work.mangas[0]
        destination_assignment = destination_work.asignaciones_personal[0]
        db.session.add(ScmTramoMangaTrabajo(
            manga_id=source_manga.id,
            trabajo_ot_id=destination_work.id,
            asignacion_personal_trabajo_id=destination_assignment.id,
            secuencia=1,
            estado="PROGRAMADO",
            cantidad_inicio_un=0,
            cantidad_atribuida_un=0,
            created_by_id=actor.id,
        ))
        db.session.commit()
        db.session.expire_all()
        summary = scm_ot_service.list_ots(
            db.session,
            actor_id=actor.id,
            operational_date="2026-08-12",
            shift="DIA",
            tipo_ot="FABRICACION",
            view="resumen",
        )
        legacy = scm_ot_service.list_ots(
            db.session,
            actor_id=actor.id,
            operational_date="2026-08-12",
            shift="DIA",
            tipo_ot="FABRICACION",
        )

    assert len(summary["items"]) == len(legacy["items"]) == 1
    summary_work = summary["items"][0]["trabajos_color_resumen"][0]
    legacy_work = legacy["items"][0]["trabajos_color"][0]
    expected = scm_ot_service._empty_manga_summary()
    for manga in legacy_work["mangas"]:
        label = manga.get("etiqueta_vigente") or {}
        scm_ot_service._accumulate_manga_summary(
            expected,
            state=manga.get("estado"),
            label_state=label.get("estado"),
            count=1,
        )
    assert summary_work["mangas_resumen"] == expected
    assert expected["total"] >= 2

