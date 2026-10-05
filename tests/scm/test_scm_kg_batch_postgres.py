"""PostgreSQL concurrency and rollback regressions for KG batches."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from test_scm_kg_custody_postgres import _seed_custody, pg_custody_app


pytestmark = pytest.mark.postgres


def _batch_fixture(app):
    from app import db
    from app.models.scm_catalogos import ScmCapacidad
    from app.models.scm_inventory_kg import ScmSaldoInventarioKg
    from app.models.trabajador import Trabajador

    actor_id, _article_id, _warehouse_id, _location_id, saldo_id, unit_ids = _seed_custody(app)
    actor = db.session.get(Trabajador, actor_id)
    actor.roles[0].capacidades.append(
        ScmCapacidad.query.filter_by(codigo="PICKING_DESPACHAR").one()
    )
    db.session.get(ScmSaldoInventarioKg, saldo_id).cantidad_fisica_kg = 10
    db.session.commit()
    return actor_id, unit_ids, saldo_id


def _request(unit_ids):
    return {
        "items": [{"unit_id": str(unit_id), "version": 1} for unit_id in unit_ids],
        "motivo_operativo": "Concurrencia KG aislada",
        "tenedor_fisico_id": None,
        "documento_destino_tipo": "OA",
        "documento_destino_id": "PG-BATCH",
    }


def _run_batch(app, actor_id, unit_ids, operation_id, barrier=None):
    from app import db
    from app.services.scm_kg_custody_service import withdraw_kg_batch
    from app.services.scm_service_support import ScmServiceError

    with app.app_context():
        try:
            if barrier is not None:
                barrier.wait(timeout=20)
            data = _request(unit_ids)
            data["tenedor_fisico_id"] = actor_id
            return "ok", withdraw_kg_batch(
                db.session, actor_id=actor_id, operation_id=operation_id, data=data
            )
        except ScmServiceError as error:
            db.session.rollback()
            return error.code, None


def test_pg_two_batches_same_pair_have_one_winner(pg_custody_app):
    from app import db
    from app.models.scm_inventory_kg import (
        ScmMovimientoInventarioKg, ScmReservaUnidadKg, ScmRetiroArmadoKg,
        ScmSaldoInventarioKg,
    )

    with pg_custody_app.app_context():
        actor_id, unit_ids, saldo_id = _batch_fixture(pg_custody_app)
    barrier = Barrier(2)
    keys = [uuid4(), uuid4()]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda key: _run_batch(pg_custody_app, actor_id, unit_ids, key, barrier),
            keys,
        ))
    assert sum(result[0] == "ok" for result in results) == 1
    assert sum(result[0] != "ok" for result in results) == 1
    with pg_custody_app.app_context():
        assert ScmRetiroArmadoKg.query.count() == 2
        assert ScmMovimientoInventarioKg.query.count() == 2
        assert ScmReservaUnidadKg.query.filter_by(estado="ACTIVA").count() == 0
        assert db.session.get(ScmSaldoInventarioKg, saldo_id).cantidad_fisica_kg == 0


def test_pg_duplicate_batch_key_concurrently_replays_exact_response(pg_custody_app):
    with pg_custody_app.app_context():
        actor_id, unit_ids, _saldo_id = _batch_fixture(pg_custody_app)
    key = uuid4()
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _index: _run_batch(pg_custody_app, actor_id, unit_ids, key, barrier),
            (0, 1),
        ))
    assert [result[0] for result in results] == ["ok", "ok"]
    assert results[0][1] == results[1][1]


def test_pg_late_batch_failure_rolls_back_root_and_same_key_retries(pg_custody_app, monkeypatch):
    from app import db
    from app.models.scm_auditoria import ScmOperacion
    from app.models.scm_inventory_kg import (
        ScmMovimientoInventarioKg, ScmReservaUnidadKg, ScmRetiroArmadoKg,
        ScmSaldoInventarioKg,
    )
    from app.services import scm_kg_custody_service as service

    with pg_custody_app.app_context():
        actor_id, unit_ids, saldo_id = _batch_fixture(pg_custody_app)
        key = uuid4()
        original = service._delivery_payload
        calls = []

        def fail_second(session, retiro):
            calls.append(retiro.id)
            if len(calls) == 2:
                raise RuntimeError("isolated PG late failure")
            return original(session, retiro)

        monkeypatch.setattr(service, "_delivery_payload", fail_second)
        with pytest.raises(RuntimeError, match="isolated PG late failure"):
            _run_batch(pg_custody_app, actor_id, unit_ids, key)
        assert len(calls) == 2
        assert db.session.get(ScmOperacion, key) is None
        assert ScmRetiroArmadoKg.query.count() == 0
        assert ScmMovimientoInventarioKg.query.count() == 0
        assert ScmReservaUnidadKg.query.count() == 0
        assert db.session.get(ScmSaldoInventarioKg, saldo_id).cantidad_fisica_kg == 10

        monkeypatch.setattr(service, "_delivery_payload", original)
        retried = _run_batch(pg_custody_app, actor_id, unit_ids, key)
        assert retried[0] == "ok"
        assert retried[1]["cantidad_unidades"] == 2
