"""Real PostgreSQL checks for the OF execution lifecycle.

This module deliberately avoids the repository SQLite conftest.  It only runs
when the caller supplies the dedicated W3 database and fails closed for any
other database identity.
"""

from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


POSTGRES_DB = "enva_of_execution_tests_20261022"
RAW_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if RAW_DATABASE_URL:
    parsed_url = make_url(RAW_DATABASE_URL)
    if parsed_url.database == POSTGRES_DB:
        # Config is imported by app, so bind it without importing tests/conftest.
        os.environ["DATABASE_URL"] = RAW_DATABASE_URL

from app import create_app, db  # noqa: E402
from app.config import Config  # noqa: E402
from app.models.maquina import Maquina, TipoMaquina  # noqa: E402
from app.models.producto import Familia, Linea, LineaFamilia  # noqa: E402
from app.models.registro import RegistroDiarioProduccion  # noqa: E402
from app.models.scm_auditoria import ScmEvento  # noqa: E402
from app.models.scm_ot import ScmTrabajoColor, ScmTrabajoOt  # noqa: E402
from app.models.scm_production_orders import (  # noqa: E402
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.models.trabajador import RolOperativo, Trabajador  # noqa: E402
from app.services.scm_configuration import ensure_initial_scm_configuration  # noqa: E402


pytestmark = pytest.mark.postgres
BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _strict_database_url():
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL W3 checks")
    url = make_url(raw)
    # A repository-wide postgres run may expose another test database.  This
    # suite is deliberately local-only: skipping there is safer than
    # truncating an unapproved database or failing an unrelated CI job.
    if url.database != POSTGRES_DB:
        pytest.skip("W3 requiere TEST_DATABASE_URL apuntando a la DB dedicada")
    if url.host not in {"127.0.0.1", "localhost"}:
        pytest.skip("W3 requiere PostgreSQL loopback")
    if url.port not in {55497, None}:
        pytest.skip("W3 requiere el puerto loopback autorizado")
    return raw


def _reset_dedicated_database(raw):
    """Reset only public data in the explicitly dedicated test database."""
    engine = create_engine(raw, pool_pre_ping=True)
    with engine.begin() as connection:
        tables = connection.execute(text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
        )).scalars().all()
        if tables:
            quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in tables)
            connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    engine.dispose()


@pytest.fixture(scope="module")
def pg_app():
    raw = _strict_database_url()
    _reset_dedicated_database(raw)
    previous_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = raw
    app = create_app()
    app.config.update(TESTING=True)
    try:
        yield app
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        Config.SQLALCHEMY_DATABASE_URI = previous_uri


@pytest.fixture(autouse=True)
def common_pg_fixture(pg_app):
    """Recreate synthetic actors/catalog dependencies per scenario."""
    raw = _strict_database_url()
    _reset_dedicated_database(raw)
    with pg_app.app_context():
        ensure_initial_scm_configuration()
        line = Linea(codigo=1, nombre="W3-LINEA")
        family = Familia(codigo=1, nombre="W3-FAMILIA")
        db.session.add_all([line, family])
        db.session.flush()
        db.session.add(LineaFamilia(linea_id=line.id, familia_id=family.id))

        machine_type = TipoMaquina(
            codigo="W3-INYECCION", nombre="W3 Inyeccion", proceso="PRODUCCION"
        )
        machine = Maquina(
            codigo="W3-MQ-01", nombre="W3 Maquina", tipo_maquina=machine_type,
            estado="OPERATIVA", activo=True,
        )
        jp = Trabajador(codigo="TRB-01", nombres="W3", apellidos="Jefatura", activo=True)
        gg = Trabajador(codigo="W3-GG", nombres="W3", apellidos="Gerencia", activo=True)
        supervisor = Trabajador(codigo="W3-SU", nombres="W3", apellidos="Supervisor", activo=True)
        db.session.add_all([machine_type, machine, jp, gg, supervisor])
        db.session.flush()
        jp.roles.append(RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one())
        gg.roles.append(RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one())
        supervisor.roles.append(RolOperativo.query.filter_by(codigo="SUPERVISOR").one())
        db.session.commit()
    yield
    with pg_app.app_context():
        db.session.remove()


def _active_order(
    app, *, produced=False, second_operation=False,
    work_state="EN_EJECUCION", ot_date=date(2026, 10, 22),
):
    """Use the production-order seed used by the real service, then add OT work."""
    # Imported lazily so this module never imports repository conftest.
    from tests.scm.test_scm_fabrication_replacement import _seed_released_order

    # That shared SQLite-oriented helper assigns JEFE_PRODUCCION itself.  The
    # PostgreSQL fixture already gave the actor the role for the API checks;
    # clear just this association so its seed remains idempotent on PG.
    with app.app_context():
        jp = Trabajador.query.filter_by(codigo="TRB-01").one()
        jp.roles.clear()
        db.session.commit()
    order_id, jp_id, run_id, _recipe_id = _seed_released_order(
        app, direct_assignment=produced
    )
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        order.estado = "EN_EJECUCION"
        order.fabricacion.corridas[0].estado = "EN_EJECUCION"
        if produced:
            output = order.salidas[0]
            output.cantidad_real = Decimal("40.000")
            output.cantidad_rechazada = Decimal("0.000")
            allocation = output.asignaciones[0]
            allocation.cantidad_planificada = Decimal("120.000")
            allocation.cantidad_comprometida = Decimal("100.000")
            allocation.cantidad_satisfecha = Decimal("40.000")
            allocation.estado = "COMPROMETIDA"

        machine = Maquina.query.filter_by(codigo="W3-MQ-01").one()
        ot = RegistroDiarioProduccion(
            codigo_ot=f"OT-W3-{uuid4().hex[:10].upper()}",
            codigo_ot_sintetico=False,
            estado="EN_EJECUCION",
            tipo_ot="FABRICACION",
            maquina_id=machine.id,
            fecha=ot_date,
            turno="DIURNO",
            secuencia_siguiente_manga=1,
            secuencia_siguiente_trabajo=3,
        )
        work = ScmTrabajoOt(
            orden_trabajo=ot,
            codigo=f"{ot.codigo_ot}-T01",
            secuencia=1,
            estado=work_state,
            orden_operacion_id=order.id,
            cantidad_objetivo_un=100,
            created_by_id=jp_id,
        )
        db.session.add_all([ot, work])
        db.session.flush()
        db.session.add(ScmTrabajoColor(
            trabajo_ot_id=work.id,
            corrida_fabricacion_id=UUID(run_id),
            color_nombre_snapshot="W3-COLOR",
        ))

        sibling_id = None
        sibling_work_id = None
        if second_operation:
            sibling = ScmOrdenOperacion(
                codigo=f"OF-W3-SIB-{uuid4().hex[:8].upper()}",
                tipo="FABRICACION", origen_demanda="ORDEN_PRODUCCION",
                estado="EN_EJECUCION", created_by_id=jp_id,
            )
            db.session.add(sibling)
            db.session.flush()
            db.session.add(ScmOrdenFabricacion(orden_operacion_id=sibling.id))
            sibling_work = ScmTrabajoOt(
                orden_trabajo=ot,
                codigo=f"{ot.codigo_ot}-T02",
                secuencia=2,
                estado="PLANIFICADO",
                orden_operacion_id=sibling.id,
                cantidad_objetivo_un=50,
                created_by_id=jp_id,
            )
            db.session.add(sibling_work)
            db.session.flush()
            sibling_id = str(sibling.id)
            sibling_work_id = str(sibling_work.id)
        db.session.commit()
        return {
            "order_id": order_id,
            "run_id": run_id,
            "jp_id": jp_id,
            "gg_id": Trabajador.query.filter_by(codigo="W3-GG").one().id,
            "su_id": Trabajador.query.filter_by(codigo="W3-SU").one().id,
            "work_id": str(work.id),
            "ot_id": ot.id,
            "ot_public_id": str(ot.public_id),
            "sibling_id": sibling_id,
            "sibling_work_id": sibling_work_id,
        }


def _impact(client, order_id, actor_id, action="PAUSAR"):
    response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion={action}",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _command(client, order_id, actor_id, action, impact, key, reason="W3 evidence"):
    return client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/{action.lower()}-ejecucion",
        json={
            "version": impact["version"],
            "impact_hash": impact["impact_hash"],
            "motivo": reason,
        },
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(key)},
    )


def test_pause_guard_replay_and_competitors_use_real_endpoints(pg_app):
    fixture = _active_order(pg_app, second_operation=True)
    with pg_app.test_client() as client:
        impact = _impact(client, fixture["order_id"], fixture["jp_id"])
        assert impact["can_execute"] is True
        key = uuid4()
        paused = _command(client, fixture["order_id"], fixture["jp_id"], "pausar", impact, key)
        assert paused.status_code == 200, paused.get_json()
        assert paused.get_json()["estado"] == "PAUSADA"

        # Real competing entrypoints must observe the aggregate stop.
        start = client.post(
            f"/api/scm/v1/trabajos-color/{fixture['work_id']}/reanudar",
            json={"version": 1, "motivo": "must stay stopped"},
            headers={"X-Actor-Id": str(fixture["jp_id"]), "Idempotency-Key": str(uuid4())},
        )
        assert start.status_code == 409, start.get_json()
        add_manga = client.post(
            f"/api/scm/v1/trabajos-color/{fixture['work_id']}/mangas",
            json={"plan_linea_id": 999999, "cantidad_un": 1},
            headers={"X-Actor-Id": str(fixture["jp_id"]), "Idempotency-Key": str(uuid4())},
        )
        assert add_manga.status_code == 409, add_manga.get_json()

        replay = _command(client, fixture["order_id"], fixture["jp_id"], "pausar", impact, key)
        assert replay.status_code == 200
        mismatch = _command(client, fixture["order_id"], fixture["jp_id"], "pausar", impact, key, "edited")
        assert mismatch.status_code == 409, mismatch.get_json()

    with pg_app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"]))
        assert order.estado == "PAUSADA"
        assert db.session.get(ScmTrabajoOt, UUID(fixture["work_id"])).estado == "PAUSADO"
        assert db.session.get(ScmTrabajoOt, UUID(fixture["sibling_work_id"])).estado == "PLANIFICADO"
        assert ScmEvento.query.filter_by(aggregate_id=fixture["order_id"], tipo="OF_PAUSAR_EJECUCION").count() == 1


def test_cancel_reconciles_partial_fulfillment_without_mutating_produced_facts(pg_app):
    fixture = _active_order(pg_app, produced=True)
    with pg_app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"]))
        output = order.salidas[0]
        before = (output.cantidad_real, output.cantidad_rechazada, len(output.asignaciones))
    with pg_app.test_client() as client:
        impact = _impact(client, fixture["order_id"], fixture["gg_id"], "CANCELAR")
        assert impact["can_execute"] is True
        assert impact["producido"][0]["cantidad_real"] == "40.000"
        assert impact["compromisos_residuales"][0]["cantidad"] == "60.000"
        cancelled = _command(client, fixture["order_id"], fixture["gg_id"], "cancelar", impact, uuid4())
        assert cancelled.status_code == 200, cancelled.get_json()
        assert cancelled.get_json()["estado"] == "CANCELADA"

    with pg_app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"]))
        output = order.salidas[0]
        allocation = output.asignaciones[0]
        assert (output.cantidad_real, output.cantidad_rechazada, len(output.asignaciones)) == before
        assert allocation.estado == "CANCELADA"
        assert allocation.cantidad_satisfecha == Decimal("40.000")
        assert allocation.cantidad_comprometida == Decimal("40.000")
        assert db.session.get(ScmTrabajoOt, UUID(fixture["work_id"])).estado == "CANCELADO"


def test_cancel_stale_when_produced_facts_change_after_impact(pg_app):
    fixture = _active_order(pg_app, produced=True)
    with pg_app.test_client() as client:
        impact = _impact(client, fixture["order_id"], fixture["gg_id"], "CANCELAR")
        with pg_app.app_context():
            output = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"])).salidas[0]
            output.cantidad_real = Decimal("41.000")
            db.session.commit()
        stale = _command(
            client, fixture["order_id"], fixture["gg_id"], "cancelar", impact,
            uuid4(), "produced changed after preflight",
        )
        assert stale.status_code == 409, stale.get_json()
        assert "STALE_IMPACT" in str(stale.get_json())
    with pg_app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"]))
        assert order.estado == "EN_EJECUCION"
        assert order.salidas[0].cantidad_real == Decimal("41.000")


def test_permission_and_stale_hash_fail_without_side_effects(pg_app):
    fixture = _active_order(pg_app)
    with pg_app.test_client() as client:
        impact = _impact(client, fixture["order_id"], fixture["jp_id"], "CANCELAR")
        gerente_impact = _impact(client, fixture["order_id"], fixture["gg_id"], "CANCELAR")
        assert gerente_impact["action"] == "CANCELAR"
        denied = client.post(
            f"/api/scm/v1/ordenes-fabricacion/{fixture['order_id']}/cancelar-ejecucion",
            json={"version": impact["version"], "impact_hash": impact["impact_hash"], "motivo": "no"},
            headers={"X-Actor-Id": str(fixture["su_id"]), "Idempotency-Key": str(uuid4())},
        )
        assert denied.status_code == 403, denied.get_json()

        # A PAUSAR fingerprint cannot be replayed against CANCELAR: the
        # action is part of the canonical impact hash, not only the URL.
        pause_impact = _impact(client, fixture["order_id"], fixture["jp_id"], "PAUSAR")
        action_mismatch = _command(
            client, fixture["order_id"], fixture["jp_id"], "cancelar",
            pause_impact, uuid4(), "action mismatch",
        )
        assert action_mismatch.status_code == 409, action_mismatch.get_json()

        with pg_app.app_context():
            order = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"]))
            order.version += 1
            db.session.commit()
        stale = _command(client, fixture["order_id"], fixture["jp_id"], "pausar", impact, uuid4())
        assert stale.status_code == 409, stale.get_json()

    with pg_app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"]))
        assert order.estado == "EN_EJECUCION"
        assert db.session.get(ScmTrabajoOt, UUID(fixture["work_id"])).estado == "EN_EJECUCION"
        assert ScmEvento.query.filter_by(aggregate_id=fixture["order_id"]).count() == 0


def _concurrent_endpoint_calls(app, left, right):
    """Run two real Flask requests in independent sessions at one barrier."""
    barrier = Barrier(2)

    def invoke(request_factory):
        with app.test_client() as client:
            barrier.wait(timeout=15)
            return request_factory(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, left), pool.submit(invoke, right)]
        return [future.result(timeout=30) for future in futures]


def test_pause_vs_real_ot_start_and_color_work_endpoints_is_atomic(pg_app):
    """The OF lock precedes OT/child locks for competing entrypoints."""
    fixture = _active_order(pg_app)
    with pg_app.app_context():
        ot = db.session.get(RegistroDiarioProduccion, fixture["ot_id"])
        ot.estado = "PLANIFICADA"
        db.session.commit()

    with pg_app.test_client() as client:
        impact = _impact(client, fixture["order_id"], fixture["jp_id"], "PAUSAR")

    def pause(client):
        return _command(
            client, fixture["order_id"], fixture["jp_id"], "pausar", impact,
            uuid4(), "concurrent OT pause",
        )

    def start_ot(client):
        return client.post(
            f"/api/scm/v1/ots/{fixture['ot_public_id']}/iniciar",
            json={"version": 1},
            headers={"X-Actor-Id": str(fixture["jp_id"]), "Idempotency-Key": str(uuid4())},
        )

    responses = _concurrent_endpoint_calls(pg_app, pause, start_ot)
    assert all(response.status_code != 500 for response in responses)
    assert {response.status_code for response in responses} <= {200, 409, 422}
    with pg_app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"]))
        ot = db.session.get(RegistroDiarioProduccion, fixture["ot_id"])
        assert order.estado in {"PAUSADA", "EN_EJECUCION"}
        if order.estado == "PAUSADA":
            assert ot.estado in {"PLANIFICADA", "EN_EJECUCION"}

    # Re-run the same lock graph against the insertion endpoint.  The
    # duplicate color-work check may win the race, but must never produce a
    # 500 or resurrect a paused aggregate.
    fixture = _active_order(pg_app, ot_date=date(2026, 10, 23))
    with pg_app.test_client() as client:
        impact = _impact(client, fixture["order_id"], fixture["jp_id"], "PAUSAR")

    def add_work(client):
        return client.post(
            f"/api/scm/v1/ots/{fixture['ot_public_id']}/trabajos-color",
            json={
                "corrida_fabricacion_id": fixture["run_id"],
                "maquinista_id": fixture["jp_id"],
                "asignaciones": [],
            },
            headers={"X-Actor-Id": str(fixture["jp_id"]), "Idempotency-Key": str(uuid4())},
        )

    responses = _concurrent_endpoint_calls(pg_app, pause, add_work)
    assert all(response.status_code != 500 for response in responses)
    assert {response.status_code for response in responses} <= {200, 201, 409, 422}
    with pg_app.app_context():
        assert db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"])).estado in {
            "PAUSADA", "EN_EJECUCION"
        }


def test_cancel_vs_add_manga_endpoint_is_atomic(pg_app):
    fixture = _active_order(pg_app)
    with pg_app.test_client() as client:
        impact = _impact(client, fixture["order_id"], fixture["gg_id"], "CANCELAR")

    def cancel(client):
        return _command(
            client, fixture["order_id"], fixture["gg_id"], "cancelar", impact,
            uuid4(), "concurrent manga cancel",
        )

    def add_manga(client):
        return client.post(
            f"/api/scm/v1/trabajos-color/{fixture['work_id']}/mangas",
            json={"plan_linea_id": 999999, "cantidad_un": 1},
            headers={"X-Actor-Id": str(fixture["jp_id"]), "Idempotency-Key": str(uuid4())},
        )

    responses = _concurrent_endpoint_calls(pg_app, cancel, add_manga)
    assert all(response.status_code != 500 for response in responses)
    assert {response.status_code for response in responses} <= {200, 201, 404, 409, 422}
    with pg_app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(fixture["order_id"]))
        assert order.estado in {"CANCELADA", "EN_EJECUCION"}

