"""Focused contracts for the paged OF read path.

These tests intentionally use the SQLite test schema and synthetic rows.  They
exercise shape, ordering, accent folding and empty scope semantics without
depending on historical production data.
"""

from datetime import datetime, timedelta, timezone
import json
import tracemalloc
from uuid import UUID

import pytest

from app import db
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
)
from app.models.producto import ColorBase, ColorProduccion, FamiliaColor
from app.services import scm_fabrication_order_service as fabrication_service
from app.services.scm_production_reports_service import (
    _filters,
    _load_rows,
    list_production_progress,
)
from app.services.scm_service_support import ScmServiceError
from sqlalchemy import event


def _seed_operations(count=101):
    now = datetime.now(timezone.utc)
    operations = []
    for index in range(count):
        operation = ScmOrdenOperacion(
            codigo=f"OF-SUM-{index:04d}",
            tipo="FABRICACION",
            origen_demanda="PRUEBA_SINTETICA",
            motivo="Ázul crema" if index == 0 else f"motivo-{index}",
            created_by_id=1,
            created_at=now - timedelta(seconds=index),
        )
        operation.fabricacion = ScmOrdenFabricacion(orden_operacion=operation)
        operations.append(operation)
    db.session.add_all(operations)
    db.session.commit()
    return operations


def test_of_summary_is_paged_and_does_not_serialize_legacy_graph(app, monkeypatch):
    with app.app_context():
        operations = _seed_operations()
        monkeypatch.setattr(fabrication_service, "load_actor", lambda *args, **kwargs: None)

        payload = fabrication_service.list_fabrication_orders(
            db.session,
            actor_id=1,
            filters={"vista": "resumen", "pagina": "2", "tamano": "25"},
        )

        assert payload["pagination"] == {
            "page": 2,
            "page_size": 25,
            "total": 101,
            "total_pages": 5,
        }
        assert len(payload["items"]) == 25
        assert payload["items"][0]["id"] == str(operations[25].id)
        assert set(payload["items"][0]) == {
            "id", "codigo", "estado", "origen_demanda", "motivo", "created_at",
            "procedencia", "molde", "corridas",
        }
        assert "asignaciones" not in payload["items"][0]


def test_of_detail_identity_does_not_expand_catalog_dto():
    class RichMold:
        codigo = "M-01"
        nombre = "Molde 01"
        cavidades = 24
        piezas = ["pieza interna"]
        notas = "solo catálogo editable"

    class RichMachine:
        id = 7
        codigo = "INY-01"
        nombre = "Inyectora 01"
        numero_serie = "SERIAL-PRIVATE"
        observaciones = "solo catálogo editable"

    mold, machine = fabrication_service._detail_catalog_identity(
        RichMold(), RichMachine(),
    )

    assert mold == {"codigo": "M-01", "nombre": "Molde 01"}
    assert machine == {"id": 7, "codigo": "INY-01", "nombre": "Inyectora 01"}


def test_of_summary_search_folds_uppercase_spanish_accents_and_escapes_wildcards(app, monkeypatch):
    with app.app_context():
        _seed_operations(3)
        monkeypatch.setattr(fabrication_service, "load_actor", lambda *args, **kwargs: None)

        accent_match = fabrication_service.list_fabrication_orders(
            db.session,
            actor_id=1,
            filters={"vista": "resumen", "q": "AZUL"},
        )
        wildcard_literal = fabrication_service.list_fabrication_orders(
            db.session,
            actor_id=1,
            filters={"vista": "resumen", "q": "%"},
        )

        assert accent_match["pagination"]["total"] == 1
        assert wildcard_literal["pagination"]["total"] == 0


def test_of_summary_search_matches_composite_color_identity(app, monkeypatch):
    with app.app_context():
        operations = _seed_operations(3)
        base = ColorBase(nombre="AZUL")
        family = FamiliaColor(nombre="SOLIDO")
        db.session.add_all([base, family])
        db.session.flush()
        color = ColorProduccion(color_base_id=base.id, familia_color_id=family.id, codigo_legacy=77)
        operations[0].fabricacion.corridas.append(
            ScmCorridaFabricacion(codigo="RUN-COLOR", secuencia=1, color_produccion=color),
        )
        db.session.commit()
        monkeypatch.setattr(fabrication_service, "load_actor", lambda *args, **kwargs: None)

        payload = fabrication_service.list_fabrication_orders(
            db.session,
            actor_id=1,
            filters={"vista": "resumen", "q": "azul sólido"},
        )

        assert payload["pagination"]["total"] == 1


def test_of_summary_metrics_are_measured_on_the_same_synthetic_dataset(app, monkeypatch, capsys):
    with app.app_context():
        _seed_operations()
        monkeypatch.setattr(fabrication_service, "load_actor", lambda *args, **kwargs: None)
        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(db.engine, "before_cursor_execute", count_query)
        try:
            tracemalloc.start()
            payload = fabrication_service.list_fabrication_orders(
                db.session,
                actor_id=1,
                filters={"vista": "resumen", "pagina": 1, "tamano": 25},
            )
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            python_current, python_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            page_25_queries = query_count
            db.session.expire_all()
            query_count = 0
            page_100_payload = fabrication_service.list_fabrication_orders(
                db.session,
                actor_id=1,
                filters={"vista": "resumen", "pagina": 1, "tamano": 100},
            )
            page_100_queries = query_count
        finally:
            event.remove(db.engine, "before_cursor_execute", count_query)

        print(json.dumps({
            "summary_response_bytes": len(encoded),
            "python_allocated_current_bytes": python_current,
            "python_allocated_peak_bytes": python_peak,
            "sql_queries": page_25_queries,
            "page_25_queries": page_25_queries,
            "page_100_queries": page_100_queries,
        }))
        assert len(payload["items"]) == 25
        assert len(page_100_payload["items"]) == 100
        assert len(encoded) > 0
        assert page_25_queries > 0
        assert page_100_queries > 0
        assert abs(page_100_queries - page_25_queries) <= 2


def test_of_progress_empty_scope_is_not_promoted_to_global_read():
    assert _filters({"of_ids": []}, require_dates=False)["of_ids"] == set()
    assert _filters({}, require_dates=False)["of_ids"] is None


def test_of_progress_scope_deduplicates_before_distinct_limit():
    repeated = ["00000000-0000-0000-0000-000000000001"] * 101
    assert _filters({"of_ids": repeated}, require_dates=False)["of_ids"] == {
        UUID("00000000-0000-0000-0000-000000000001")
    }
    distinct = [f"00000000-0000-0000-0000-{index:012d}" for index in range(101)]
    with pytest.raises(ScmServiceError) as error:
        _filters({"of_ids": distinct}, require_dates=False)
    assert error.value.code == "INVALID_OF_SCOPE"


def test_of_progress_scope_filters_runs_before_the_report_graph_is_loaded(app):
    with app.app_context():
        first = ScmOrdenOperacion(
            codigo="OF-SCOPE-1", tipo="FABRICACION", origen_demanda="PRUEBA", created_by_id=1,
        )
        second = ScmOrdenOperacion(
            codigo="OF-SCOPE-2", tipo="FABRICACION", origen_demanda="PRUEBA", created_by_id=1,
        )
        first.fabricacion = ScmOrdenFabricacion(orden_operacion=first)
        second.fabricacion = ScmOrdenFabricacion(orden_operacion=second)
        first.fabricacion.corridas.append(ScmCorridaFabricacion(codigo="RUN-SCOPE-1", secuencia=1))
        second.fabricacion.corridas.append(ScmCorridaFabricacion(codigo="RUN-SCOPE-2", secuencia=1))
        db.session.add_all([first, second])
        db.session.commit()

        scoped = _load_rows(db.session, _filters({"of_ids": [str(first.id)]}, require_dates=False))
        empty = _load_rows(db.session, _filters({"of_ids": []}, require_dates=False))

        assert {item["orden"].id for item in scoped} == {first.id}
        assert empty == []


def test_of_progress_scope_keeps_manga_graph_and_permission_projection(app, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        order = ScmOrdenOperacion.query.filter_by(codigo="OF-OBS-001").one()
        fabrication = ScmOrdenFabricacion(orden_operacion_id=order.id)
        db.session.add(fabrication)
        db.session.flush()
        corrida = ScmCorridaFabricacion(
            orden_fabricacion_id=order.id,
            codigo="RUN-SCOPE-MANGA",
            secuencia=1,
            objetivo_neto_kg=10,
        )
        db.session.add(corrida)
        db.session.flush()
        from app.models.scm_ot import ScmManga, ScmTrabajoOt

        color_work = ScmTrabajoOt.query.filter_by(codigo="TC-OBS-AZUL").one().trabajo_color
        color_work.corrida_fabricacion_id = corrida.id
        db.session.commit()

        prototype = ScmManga.query.filter_by(trabajo_ot_id=color_work.trabajo_ot_id).first()
        assert prototype is not None
        manga_identity = {
            "ot_id": prototype.ot_id,
            "trabajo_ot_id": prototype.trabajo_ot_id,
            "plan_linea_id": prototype.plan_linea_id,
            "lote_articulo_id": prototype.lote_articulo_id,
            "maquinista_previsto_id": prototype.maquinista_previsto_id,
            "created_by_id": prototype.created_by_id,
        }
        bounded_query_counts = {}
        for amount in (1, 25, 100):
            extras = [
                ScmManga(
                    codigo=f"M-OBS-DENSE-{amount:03d}-{index:03d}",
                    **manga_identity,
                    secuencia_ot=1000 + index,
                    estado="PLANIFICADA",
                    cantidad_planificada_un=100,
                    cantidad_asignada_un=100,
                    articulo_codigo_snapshot="PC-OBS",
                    articulo_nombre_snapshot="Pieza observada",
                    color_snapshot="AZUL",
                    regla_revision_id_snapshot=1,
                    regla_hash_snapshot="a" * 64,
                    tipo_contenedor_codigo_snapshot="MANGA-100",
                    tipo_contenedor_nombre_snapshot="Manga 100",
                    peso_unitario_snapshot_g=100,
                    tara_nominal_g_snapshot=100,
                    tolerancia_tara_g_snapshot=10,
                    peso_bruto_max_kg_snapshot=20,
                )
                for index in range(amount)
            ]
            db.session.add_all(extras)
            db.session.commit()
            dense_queries = 0

            def count_dense_query(*_args):
                nonlocal dense_queries
                dense_queries += 1

            event.listen(db.engine, "before_cursor_execute", count_dense_query)
            try:
                dense_rows = _load_rows(
                    db.session,
                    _filters({"of_ids": str(order.id)}, require_dates=False),
                )
            finally:
                event.remove(db.engine, "before_cursor_execute", count_dense_query)
            bounded_query_counts[amount] = dense_queries
            assert dense_rows and sum(len(item["mangas"]) for item in dense_rows) >= amount
            for manga in extras:
                db.session.delete(manga)
            db.session.commit()

        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(db.engine, "before_cursor_execute", count_query)
        try:
            tracemalloc.start()
            payload = list_production_progress(
                db.session,
                actor_id=seeded["full"].id,
                filters={"of_ids": str(order.id)},
            )
            scoped_current, scoped_peak = tracemalloc.get_traced_memory()
            scoped_queries = query_count
            tracemalloc.stop()

            db.session.expire_all()
            query_count = 0
            tracemalloc.start()
            global_payload = list_production_progress(
                db.session,
                actor_id=seeded["full"].id,
                filters={},
            )
            global_current, global_peak = tracemalloc.get_traced_memory()
            global_queries = query_count
            tracemalloc.stop()
        finally:
            event.remove(db.engine, "before_cursor_execute", count_query)

        assert payload["items"]
        assert {item["of_id"] for item in payload["items"]} == {str(order.id)}
        assert any(item["mangas"]["total"] > 0 for item in payload["items"])
        assert payload["visibilidad"]["pesaje"] is True
        assert global_payload["items"] == payload["items"]
        assert bounded_query_counts[100] <= bounded_query_counts[1] + 4
        assert bounded_query_counts[100] <= bounded_query_counts[25] + 4
        print(json.dumps({
            "scoped_sql_queries": scoped_queries,
            "global_sql_queries": global_queries,
            "scoped_python_allocated_current_bytes": scoped_current,
            "scoped_python_allocated_peak_bytes": scoped_peak,
            "global_python_allocated_current_bytes": global_current,
            "global_python_allocated_peak_bytes": global_peak,
            "scoped_response_bytes": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            "global_response_bytes": len(json.dumps(global_payload, ensure_ascii=False, separators=(",", ":"))),
            "dense_query_counts_1_25_100": bounded_query_counts,
        }))
        assert scoped_queries > 0
        assert global_queries > 0
