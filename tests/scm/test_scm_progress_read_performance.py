"""Focused performance contracts for the local progress read path.

The production report intentionally keeps the physical-evidence rules in
Python.  These tests cover the safe part of the optimization: index manga
associations once while preserving correction, annulment, and ordering rules.
"""

import json
import time
import tracemalloc

from app import db
from app.models.scm_ot import ScmTrabajoOt
from app.models.scm_production_orders import ScmCorridaFabricacion, ScmOrdenFabricacion, ScmOrdenOperacion
from app.services.scm_production_reports_service import list_production_progress
from app.services.scm_production_reports_service import _associate_mangas_to_runs
from sqlalchemy import event
from types import SimpleNamespace


def test_progress_json_contract_and_measurements(app, scm_config):
    """Verify the authorized projection with independent semantic expectations."""
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        order = ScmOrdenOperacion.query.filter_by(codigo="OF-OBS-001").one()
        fabrication = ScmOrdenFabricacion(orden_operacion_id=order.id)
        db.session.add(fabrication)
        db.session.flush()
        corrida = ScmCorridaFabricacion(
            orden_fabricacion_id=order.id,
            codigo="RUN-PERF-PARITY",
            secuencia=1,
            objetivo_neto_kg=10,
        )
        db.session.add(corrida)
        db.session.flush()
        corrida_id = corrida.id
        color_work = ScmTrabajoOt.query.filter_by(codigo="TC-OBS-AZUL").one().trabajo_color
        color_work.corrida_fabricacion_id = corrida.id
        db.session.commit()

        filters = {"of_ids": str(order.id)}
        actor_id = seeded["full"].id
        def measure():
            db.session.remove()
            sql_statements = 0

            def count_statement(*_args):
                nonlocal sql_statements
                sql_statements += 1

            event.listen(db.engine, "before_cursor_execute", count_statement)
            try:
                tracemalloc.start()
                started = time.perf_counter()
                payload = list_production_progress(
                    db.session,
                    actor_id=actor_id,
                    filters=filters,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                _current, peak_bytes = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            finally:
                event.remove(db.engine, "before_cursor_execute", count_statement)
            return payload, {
                "sql_statements": sql_statements,
                "response_bytes": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":" )).encode("utf-8")),
                "elapsed_ms_observed": round(elapsed_ms, 3),
                "tracemalloc_peak_bytes": peak_bytes,
            }

        measurements = []
        for repetition in range(3):
            actual, current_metrics = measure()
            item = next(
                item for item in actual["items"] if item["corrida_id"] == str(corrida_id)
            )
            assert {row["of_id"] for row in actual["items"]} == {str(order.id)}
            assert item["mangas"] == {"total": 2, "conocidas": 2}
            assert item["kg_finalizados_efectivos"] == 19.5
            assert item["coverage"]["estado"] == "COMPLETA"
            assert actual["visibilidad"]["pesaje"] is True
            measurements.append({
                "repetition": repetition + 1,
                "optimized": current_metrics,
            })
        print(json.dumps({"measurements": measurements}))


def test_manga_association_reads_each_correction_once_for_many_runs():
    accesses = 0

    class Manga:
        def __init__(self, manga_id, work_id):
            self.id = manga_id
            self.trabajo_ot_id = work_id
            self.ot_id = None
            self.tramos_trabajo = []

        @property
        def correccion_asignacion(self):
            nonlocal accesses
            accesses += 1
            return None

    mangas = {index: Manga(index, 1) for index in range(1, 101)}
    runs = {
        index: {
            "works": [(SimpleNamespace(id=1), None, None)],
            "ots": [],
            "mangas": {},
        }
        for index in range(1, 26)
    }

    _associate_mangas_to_runs(runs, mangas)

    assert accesses == len(mangas)
    assert all(len(run["mangas"]) == len(mangas) for run in runs.values())


def test_manga_association_preserves_work_ot_correction_order():
    class Correction:
        def __init__(self, origin=None, target=None):
            self.origen_trabajo_ot_id = origin
            self.destino_trabajo_ot_id = target

    class Manga:
        def __init__(self, manga_id, work=None, ot=None, correction=None):
            self.id = manga_id
            self.trabajo_ot_id = work
            self.ot_id = ot
            self.tramos_trabajo = []
            self.correccion_asignacion = correction

    mangas = {
        1: Manga(1, work=10),
        2: Manga(2, ot=20),
        3: Manga(3, correction=Correction(origin=30)),
        4: Manga(4, work=10, correction=Correction(target=30)),
        5: Manga(5, work=11, correction=Correction(origin=10)),
    }
    runs = {
        1: {
            "works": [(SimpleNamespace(id=10), None, None), (SimpleNamespace(id=30), None, None)],
            "ots": [SimpleNamespace(id=20)],
            "mangas": {},
        },
        2: {
            "works": [(SimpleNamespace(id=11), None, None)],
            "ots": [],
            "mangas": {},
        },
        3: {
            "works": [(SimpleNamespace(id=30), None, None)],
            "ots": [],
            "mangas": {},
        },
    }

    def baseline_association(source_runs):
        mangas_by_work = {}
        mangas_by_ot = {}
        for manga in mangas.values():
            if manga.trabajo_ot_id is not None:
                mangas_by_work.setdefault(manga.trabajo_ot_id, {})[manga.id] = manga
            if manga.ot_id is not None:
                mangas_by_ot.setdefault(manga.ot_id, {})[manga.id] = manga
            for segment in manga.tramos_trabajo:
                if segment.trabajo_ot_id is not None:
                    mangas_by_work.setdefault(segment.trabajo_ot_id, {})[manga.id] = manga
        for run in source_runs.values():
            work_ids = {item[0].id for item in run["works"]}
            ot_ids = {ot.id for ot in run["ots"]}
            associated = {}
            for work_id in work_ids:
                associated.update(mangas_by_work.get(work_id, {}))
            for ot_id in ot_ids:
                associated.update(mangas_by_ot.get(ot_id, {}))
            for manga in mangas.values():
                correction = manga.correccion_asignacion
                if correction is not None and (
                    correction.origen_trabajo_ot_id in work_ids
                    or correction.destino_trabajo_ot_id in work_ids
                ):
                    associated[manga.id] = manga
            run["mangas"].update(associated)
        return source_runs

    expected = baseline_association(
        {
            key: {**value, "mangas": {}}
            for key, value in runs.items()
        }
    )
    _associate_mangas_to_runs(runs, mangas)

    assert [list(run["mangas"]) for run in runs.values()] == [
        list(run["mangas"]) for run in expected.values()
    ]
