"""PostgreSQL race checks for F3 inline WIP assembly.

These tests intentionally use the real Flask/API transaction boundaries.  A
fresh PostgreSQL schema is created for every test and removed afterwards, so
the harness never writes to the UAT schema or to persistent pilot facts.
"""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app import create_app, db
from app.config import Config
from app.models.maquina import Maquina, TipoMaquina
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_assembly_execution import ScmConfirmacionMangaArmado
from app.models.scm_inline_wip import (
    ScmMovimientoWipSalida,
    ScmReservaWipSalida,
    ScmSaldoWipSalida,
)
from app.models.scm_internal_supply import (
    ScmAsignacionPoolArmado,
    ScmSolicitudAbastecimiento,
)
from app.models.scm_inventory import ScmSaldoInventario
from app.models.scm_ot import ScmManga, ScmTrabajoOt
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_internal_supply_service import create_supply_request
from tests.scm.test_scm_inline_assembly import (
    _plan_and_assign,
    _seed_concurrent_wip_flow,
    _stage_previous_component,
)


pytestmark = pytest.mark.postgres

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _isolated_postgres_schema():
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")
    base_url = make_url(raw_url)
    assert base_url.database in {"envaperu_test", "enva_test"}
    assert base_url.host in {"localhost", "127.0.0.1"}
    schema = f"scm_inline_wip_race_{uuid4().hex[:12]}"
    admin_engine = create_engine(base_url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    query = dict(base_url.query)
    query["options"] = f"-csearch_path={schema}"
    return admin_engine, schema, base_url.set(query=query)


def _upgrade_schema(schema_url):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = schema_url.render_as_string(
        hide_password=False
    )
    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "db", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _seed_postgres_baseline(app):
    with app.app_context():
        from app.services.scm_configuration import (
            ensure_initial_scm_configuration,
        )

        ensure_initial_scm_configuration()
        actor = Trabajador(
            codigo="TRB-01",
            nombres="Operador",
            apellidos="Carrera PostgreSQL",
            activo=True,
        )
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="MAQUINISTA").one()
        )
        machine_type = TipoMaquina(
            codigo="INYECCION-INLINE-PG",
            nombre="Inyección inline PostgreSQL",
            proceso="PRODUCCION",
        )
        db.session.add_all([actor, machine_type])
        db.session.flush()
        db.session.add(Maquina(
            codigo="MQ-INLINE-PG",
            nombre="Máquina inline PostgreSQL",
            tipo_maquina_id=machine_type.id,
            estado="OPERATIVA",
            activo=True,
        ))
        db.session.commit()


@pytest.fixture
def postgres_inline_app():
    admin_engine, schema, schema_url = _isolated_postgres_schema()
    app = None
    original_uri = Config.SQLALCHEMY_DATABASE_URI
    try:
        _upgrade_schema(schema_url)
        Config.SQLALCHEMY_DATABASE_URI = schema_url.render_as_string(
            hide_password=False
        )
        app = create_app()
        app.config.update(TESTING=True)
        _seed_postgres_baseline(app)
        yield app
    finally:
        Config.SQLALCHEMY_DATABASE_URI = original_uri
        if app is not None:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        try:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        finally:
            admin_engine.dispose()


def _headers(actor_id):
    return {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(uuid4()),
    }


def _prepare_closable_concurrent_manga(app):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        balance = _stage_previous_component(actor, request)
        pool_assignment = request.lineas[0].asignaciones_pool[0]
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id
                == UUID(assigned["mangas"][0]["public_id"])
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.commit()
        return {
            "actor_id": actor.id,
            "manga_id": manga.public_id,
            "manga_version": manga.version,
            "work_id": color_work.id,
            "work_version": color_work.version,
            "fabrication_order_id": color_work.orden_operacion_id,
            "fabrication_order_version": color_work.orden_operacion.version,
            "pool_assignment_id": pool_assignment.id,
            "balance_id": balance.id,
        }


def _error_code(payload):
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error.get("code") if isinstance(error, dict) else None


def test_close_and_pool_return_are_atomic_and_have_one_winner_on_postgres(
    postgres_inline_app,
):
    """A pool return can never leave a partially credited WIP manga."""

    app = postgres_inline_app
    scenario = _prepare_closable_concurrent_manga(app)
    barrier = Barrier(2)

    def close_manga():
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/scm/v1/mangas/"
                f"{scenario['manga_id']}/cerrar-armado",
                headers=_headers(scenario["actor_id"]),
                json={
                    "version": scenario["manga_version"],
                    "cantidad_real": 10,
                },
            )
            return response.status_code, response.get_json()

    def request_return():
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/scm/v1/abastecimiento/asignaciones-pool/"
                f"{scenario['pool_assignment_id']}/retorno",
                headers=_headers(scenario["actor_id"]),
                json={},
            )
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        close_future = executor.submit(close_manga)
        return_future = executor.submit(request_return)
        close_result = close_future.result(timeout=30)
        return_result = return_future.result(timeout=30)

    assert sorted((close_result[0], return_result[0])) == [200, 409]
    assert all(result[0] < 500 for result in (close_result, return_result))
    loser = close_result if close_result[0] == 409 else return_result
    assert _error_code(loser[1]) in {
        "COMPONENT_STOCK_INSUFFICIENT",
        "SUPPLY_RETURN_NOT_ALLOWED",
    }

    with app.app_context():
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == scenario["manga_id"]
            )
        )
        reservation = ScmReservaWipSalida.query.one()
        balance = db.session.get(ScmSaldoInventario, scenario["balance_id"])
        assignment = db.session.get(
            ScmAsignacionPoolArmado, scenario["pool_assignment_id"]
        )
        confirmations = ScmConfirmacionMangaArmado.query.count()
        movements = ScmMovimientoWipSalida.query.count()

        if close_result[0] == 200:
            assert manga.estado == "CERRADA_ARMADO_PENDIENTE_PESAJE"
            assert reservation.estado == "APLICADA"
            assert Decimal(reservation.cantidad_aplicada) == Decimal("10.000")
            assert Decimal(balance.cantidad_fisica) == Decimal("0.000")
            assert Decimal(balance.cantidad_reservada) == Decimal("0.000")
            assert assignment.estado == "CONSUMIDA"
            assert confirmations == 1
            assert movements == 2
        else:
            assert manga.estado == "PREETIQUETADA"
            assert reservation.estado == "CREDITO_EN_LINEA_PENDIENTE"
            assert Decimal(reservation.cantidad_aplicada) == Decimal("0.000")
            assert Decimal(balance.cantidad_fisica) == Decimal("10.000")
            assert Decimal(balance.cantidad_reservada) == Decimal("10.000")
            assert assignment.estado == "PENDIENTE_RETORNO"
            assert confirmations == 0
            assert movements == 0


def test_close_and_complete_color_work_do_not_deadlock_or_double_credit(
    postgres_inline_app,
):
    """The pending inline reservation serializes source completion/credit."""

    app = postgres_inline_app
    scenario = _prepare_closable_concurrent_manga(app)
    barrier = Barrier(2)

    def close_manga():
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/scm/v1/mangas/"
                f"{scenario['manga_id']}/cerrar-armado",
                headers=_headers(scenario["actor_id"]),
                json={
                    "version": scenario["manga_version"],
                    "cantidad_real": 10,
                },
            )
            return response.status_code, response.get_json()

    def complete_source_work():
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/scm/v1/trabajos-color/"
                f"{scenario['work_id']}/completar",
                headers=_headers(scenario["actor_id"]),
                json={"version": scenario["work_version"]},
            )
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        close_future = executor.submit(close_manga)
        complete_future = executor.submit(complete_source_work)
        close_result = close_future.result(timeout=30)
        complete_result = complete_future.result(timeout=30)

    assert close_result[0] == 200, close_result
    assert complete_result[0] == 409, complete_result
    assert _error_code(complete_result[1]) in {
        "WORK_HAS_PENDING_INLINE_RESERVATIONS",
        "VERSION_CONFLICT",
    }
    assert all(result[0] < 500 for result in (close_result, complete_result))

    with app.app_context():
        work = db.session.get(ScmTrabajoOt, scenario["work_id"])
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == scenario["manga_id"]
            )
        )
        balance = ScmSaldoWipSalida.query.one()
        reservation = ScmReservaWipSalida.query.one()
        assert work.estado == "EN_EJECUCION"
        assert Decimal(work.cantidad_confirmada_un) == Decimal("10.000")
        assert manga.estado == "CERRADA_ARMADO_PENDIENTE_PESAJE"
        assert Decimal(balance.cantidad_acreditada) == Decimal("10.000")
        assert Decimal(balance.cantidad_consumida) == Decimal("10.000")
        assert Decimal(reservation.cantidad_aplicada) == Decimal("10.000")
        assert ScmConfirmacionMangaArmado.query.count() == 1
        assert ScmMovimientoWipSalida.query.count() == 2


def test_close_and_annul_color_work_do_not_deadlock_or_cancel_credit(
    postgres_inline_app,
):
    """An annul attempt cannot erase a pending or newly applied inline fact."""

    app = postgres_inline_app
    scenario = _prepare_closable_concurrent_manga(app)
    with app.app_context():
        work = db.session.get(ScmTrabajoOt, scenario["work_id"])
        work.estado = "PAUSADO"
        db.session.commit()
        scenario["work_version"] = work.version
    barrier = Barrier(2)

    def close_manga():
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/scm/v1/mangas/"
                f"{scenario['manga_id']}/cerrar-armado",
                headers=_headers(scenario["actor_id"]),
                json={
                    "version": scenario["manga_version"],
                    "cantidad_real": 10,
                },
            )
            return response.status_code, response.get_json()

    def annul_source_work():
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/scm/v1/trabajos-color/"
                f"{scenario['work_id']}/anular",
                headers=_headers(scenario["actor_id"]),
                json={
                    "version": scenario["work_version"],
                    "motivo": "Carrera controlada contra cierre de Armado",
                },
            )
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        close_future = executor.submit(close_manga)
        annul_future = executor.submit(annul_source_work)
        close_result = close_future.result(timeout=30)
        annul_result = annul_future.result(timeout=30)

    assert close_result[0] == 200, close_result
    assert annul_result[0] == 409, annul_result
    assert _error_code(annul_result[1]) in {
        "WORK_HAS_PENDING_INLINE_RESERVATIONS",
        "VERSION_CONFLICT",
    }
    assert all(result[0] < 500 for result in (close_result, annul_result))

    with app.app_context():
        work = db.session.get(ScmTrabajoOt, scenario["work_id"])
        reservation = ScmReservaWipSalida.query.one()
        assert work.estado == "PAUSADO"
        assert Decimal(work.cantidad_confirmada_un) == Decimal("10.000")
        assert reservation.estado == "APLICADA"
        assert Decimal(reservation.cantidad_aplicada) == Decimal("10.000")
        assert ScmConfirmacionMangaArmado.query.count() == 1
        assert ScmMovimientoWipSalida.query.count() == 2


def test_close_assembly_and_close_source_of_do_not_deadlock_or_double_credit(
    postgres_inline_app,
):
    """OF closure loses safely while its exact inline source is still active."""

    app = postgres_inline_app
    scenario = _prepare_closable_concurrent_manga(app)
    barrier = Barrier(2)

    def close_manga():
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/scm/v1/mangas/"
                f"{scenario['manga_id']}/cerrar-armado",
                headers=_headers(scenario["actor_id"]),
                json={
                    "version": scenario["manga_version"],
                    "cantidad_real": 10,
                },
            )
            return response.status_code, response.get_json()

    def close_source_order():
        with app.test_client() as client:
            barrier.wait(timeout=10)
            response = client.post(
                "/api/scm/v1/ordenes-fabricacion/"
                f"{scenario['fabrication_order_id']}/cerrar",
                headers=_headers(scenario["actor_id"]),
                json={"version": scenario["fabrication_order_version"]},
            )
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        close_manga_future = executor.submit(close_manga)
        close_order_future = executor.submit(close_source_order)
        manga_result = close_manga_future.result(timeout=30)
        order_result = close_order_future.result(timeout=30)

    assert manga_result[0] == 200, manga_result
    assert order_result[0] == 409, order_result
    assert _error_code(order_result[1]) == "OF_HAS_PENDING_WORKS"
    assert all(result[0] < 500 for result in (manga_result, order_result))

    with app.app_context():
        work = db.session.get(ScmTrabajoOt, scenario["work_id"])
        balance = ScmSaldoWipSalida.query.one()
        reservation = ScmReservaWipSalida.query.one()
        assert work.estado == "EN_EJECUCION"
        assert Decimal(work.cantidad_confirmada_un) == Decimal("10.000")
        assert Decimal(balance.cantidad_acreditada) == Decimal("10.000")
        assert Decimal(balance.cantidad_consumida) == Decimal("10.000")
        assert reservation.estado == "APLICADA"
        assert ScmConfirmacionMangaArmado.query.count() == 1
        assert ScmMovimientoWipSalida.query.count() == 2

