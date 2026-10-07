"""Real transaction races in a UUID schema on a dedicated local test database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import text
from app import db
from app.models.scm_inventory_kg import ScmMovimientoInventarioKg, ScmSaldoInventarioKg
from app.models.scm_ot import ScmPesajeManga
from app.services.scm_weighing_service import confirm_manga_weighing
from app.services.scm_service_support import ScmServiceError
from tests.scm.test_scm_kg009_concurrency_postgres import (
    kg009_schema_url, postgres_kg009_app, _reset_dedicated_data,
)
from tests.scm.test_kg_intake_configuration import prepare_capture

pytestmark = pytest.mark.postgres


def race(app, commands):
    barrier = Barrier(len(commands))
    def run(command):
        with app.app_context():
            try:
                db.session.execute(text("SET statement_timeout='15s'"))
                barrier.wait(timeout=10)
                return confirm_manga_weighing(db.session, **command)
            except ScmServiceError as error:
                db.session.rollback()
                return {'error': error.code}
            finally:
                db.session.remove()
    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        return list(pool.map(run, commands))


def test_pg_same_operation_commits_one_ingress(postgres_kg009_app):
    app = postgres_kg009_app
    _reset_dedicated_data(app)
    with app.app_context():
        command = prepare_capture(app, created_at='2026-08-01T00:00:00+00:00')
        command['data']['pesada_at'] = '2026-09-17T19:00:00-05:00'
    result = race(app, [command, command])
    assert all('error' not in item for item in result), result
    assert result[0] == result[1]
    with app.app_context():
        assert ScmPesajeManga.query.count() == 1
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('12.000')


def test_pg_offline_capture_parallel_retries_leave_no_effect(postgres_kg009_app):
    app = postgres_kg009_app
    _reset_dedicated_data(app)
    with app.app_context():
        command = prepare_capture(app, created_at='2026-09-17T23:59:59+00:00')
        command['data']['pesada_at'] = '2026-09-17T23:59:59+00:00'
    result = race(app, [command, command])
    assert result == [{'error': 'KG_INTAKE_CUTOVER_REVIEW_REQUIRED'}] * 2
    with app.app_context():
        assert ScmPesajeManga.query.count() == 0
        assert ScmMovimientoInventarioKg.query.count() == 0
        assert ScmSaldoInventarioKg.query.count() == 0


def test_pg_different_operations_same_capture_do_not_duplicate(postgres_kg009_app):
    app = postgres_kg009_app
    _reset_dedicated_data(app)
    with app.app_context():
        command = prepare_capture(app)
    second = {**command, 'operation_id': uuid4()}
    result = race(app, [command, second])
    assert any('error' not in item for item in result), result
    with app.app_context():
        assert ScmPesajeManga.query.count() == 1
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('12.000')

def test_pg_two_new_mangas_share_one_balance(postgres_kg009_app):
    from datetime import datetime
    from uuid import UUID
    from app.models.scm_ot import ScmManga, ScmEtiquetaManga
    from app.models.trabajador import Trabajador
    from tests.scm.test_scm_kg_receipt import _print_color_manga
    app = postgres_kg009_app
    _reset_dedicated_data(app)
    with app.app_context():
        command = prepare_capture(app, quantity=240)
        first_label = ScmEtiquetaManga.query.filter_by(public_id=UUID(command['data']['label_id'])).one()
        other = ScmManga.query.filter(ScmManga.id != first_label.manga_id,
            ScmManga.lote_articulo_id == first_label.manga.lote_articulo_id).first()
        assert other is not None
        other.created_at = datetime.fromisoformat('2026-09-18T00:00:00+00:00')
        station, label = _print_color_manga(actor=db.session.get(Trabajador, command['actor_id']),
            manga_id=other.public_id, station_code='CUT-SECOND')
        second = {**command, 'station_id': station.station_id, 'operation_id': uuid4(),
                  'data': {**command['data'], 'label_id': label['public_id'], 'capture_id': str(uuid4())}}
        db.session.commit()
    result = race(app, [command, second])
    assert all('error' not in item for item in result), result
    with app.app_context():
        assert ScmPesajeManga.query.count() == 2
        assert ScmMovimientoInventarioKg.query.count() == 2
        assert ScmSaldoInventarioKg.query.count() == 1
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('24.000')
