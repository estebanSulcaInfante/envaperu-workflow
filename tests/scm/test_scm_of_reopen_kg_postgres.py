from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from app import db
from app.models.producto import Familia, Linea, LineaFamilia
from app.models.scm_ot import ScmCierreProductivoKg, ScmReaperturaProductivaKg, ScmTrabajoOt
from app.services.scm_fabrication_order_service import close_fabrication_order
from app.services.scm_kg_production_service import reopen_fabrication_document_kg
from app.services.scm_service_support import ScmServiceError
from tests.scm.test_scm_inline_assembly_postgres import postgres_inline_app
from tests.scm.test_scm_kg_production import _weigh_kg_fixture
from tests.scm.test_scm_kg_custody import _grant_capabilities


pytestmark = pytest.mark.postgres


def test_pg_of_kg_reopen_and_reclose_keep_versioned_closure_history(
    postgres_inline_app,
):
    app = postgres_inline_app
    with app.app_context():
        linea = Linea(codigo=901071, nombre="OF71 PG")
        familia = Familia(codigo=901071, nombre="OF71 PG")
        db.session.add_all([linea, familia])
        db.session.flush()
        db.session.add(LineaFamilia(linea_id=linea.id, familia_id=familia.id))
        db.session.commit()
        creator, _work_payload, manga, _weighed = _weigh_kg_fixture(quantity=100)
        _grant_capabilities(creator, ("OF_CERRAR",))
        work = db.session.get(ScmTrabajoOt, manga.trabajo_ot_id)
        from app.services.scm_ot_service import transition_color_work

        transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=work.id,
            operation_id=uuid4(),
            data={"version": work.version},
            action="completar",
        )
        order = work.orden_operacion
        close_operation = uuid4()
        close_data = {"version": order.version}
        closed = close_fabrication_order(
            db.session,
            actor_id=creator.id,
            operation_id=close_operation,
            operation_order_id=order.id,
            data=close_data,
        )
        version = order.version
        reopened = reopen_fabrication_document_kg(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            operation_order_id=order.id,
            data={"version": version, "motivo": "Continuar pesaje OF71"},
        )
        assert reopened["orden"]["estado"] == "EN_EJECUCION"
        replay = close_fabrication_order(
            db.session,
            actor_id=creator.id,
            operation_id=close_operation,
            operation_order_id=order.id,
            data=close_data,
        )
        assert replay == closed
        recerrada = close_fabrication_order(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            operation_order_id=order.id,
            data={"version": reopened["orden"]["version"]},
        )
        assert recerrada["kg_medido"] == "12.000"
        rows = ScmCierreProductivoKg.query.filter_by(
            documento_tipo="OF", documento_id=str(order.id)
        ).order_by(ScmCierreProductivoKg.secuencia).all()
        assert [row.secuencia for row in rows] == [1, 2]
        assert [row.vigente for row in rows] == [False, True]
        assert ScmReaperturaProductivaKg.query.count() == 1


def test_pg_of_kg_reopen_has_one_winner_under_concurrent_versioned_commands(
    postgres_inline_app,
):
    app = postgres_inline_app
    with app.app_context():
        linea = Linea(codigo=901072, nombre="OF71 PG race")
        familia = Familia(codigo=901072, nombre="OF71 PG race")
        db.session.add_all([linea, familia])
        db.session.flush()
        db.session.add(LineaFamilia(linea_id=linea.id, familia_id=familia.id))
        db.session.commit()
        creator, _work_payload, manga, _weighed = _weigh_kg_fixture(quantity=100)
        _grant_capabilities(creator, ("OF_CERRAR",))
        work = db.session.get(ScmTrabajoOt, manga.trabajo_ot_id)
        from app.services.scm_ot_service import transition_color_work

        transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=work.id,
            operation_id=uuid4(),
            data={"version": work.version},
            action="completar",
        )
        order = work.orden_operacion
        close_fabrication_order(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            operation_order_id=order.id,
            data={"version": order.version},
        )
        actor_id, order_id, version = creator.id, order.id, order.version
    barrier = Barrier(2)

    def command():
        with app.app_context():
            barrier.wait(timeout=10)
            try:
                reopen_fabrication_document_kg(
                    db.session,
                    actor_id=actor_id,
                    operation_id=uuid4(),
                    operation_order_id=order_id,
                    data={"version": version, "motivo": "Carrera OF71"},
                )
                return "ok"
            except ScmServiceError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in (
            pool.submit(command), pool.submit(command)
        )]
    assert sorted(results) == ["VERSION_CONFLICT", "ok"]
    with app.app_context():
        assert ScmReaperturaProductivaKg.query.count() == 1
