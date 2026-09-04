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
from app.models.scm_prepared_material import (
    ScmAsignacionRequerimientoPreparacion,
    ScmBolsaMaterialPreparado,
    ScmRequerimientoMaterialPreparado,
    ScmSaldoMaterialPreparado,
)
from app.models.scm_material_execution import ScmLotePremezcla
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_configuration import ensure_initial_scm_configuration
from tests.scm.test_scm_prepared_material import (
    _headers,
    _seed_free_prepared_bag,
    _seed_l1_actors_and_stock,
    _seed_two_jar_runs_with_one_recipe,
    _seed_work_color,
)
from tests.scm.test_scm_material_execution import _seed_us010b


pytestmark = pytest.mark.postgres

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _isolated_postgres_schema():
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")
    base_url = make_url(raw_url)
    assert base_url.database in {"envaperu_test", "enva_test"}
    assert base_url.host in {"localhost", "127.0.0.1"}
    schema = f"scm_opm_race_{uuid4().hex[:12]}"
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
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _seed_postgres_baseline(app):
    with app.app_context():
        ensure_initial_scm_configuration()
        operator = Trabajador(
            codigo="TRB-01",
            nombres="Planificador",
            apellidos="PostgreSQL",
            activo=True,
        )
        operator.roles.append(
            RolOperativo.query.filter_by(codigo="MAQUINISTA").one()
        )
        machine_type = TipoMaquina(
            codigo="INYECCION-PG",
            nombre="Inyeccion PostgreSQL",
            proceso="PRODUCCION",
        )
        db.session.add_all([operator, machine_type])
        db.session.flush()
        db.session.add(Maquina(
            codigo="MQ-OPM-PG",
            nombre="Maquina OPM PostgreSQL",
            tipo_maquina_id=machine_type.id,
            estado="OPERATIVA",
            activo=True,
        ))
        db.session.commit()


def _seed_postgres_scenario(app):
    _seed_postgres_baseline(app)
    with app.app_context():

        planner_id, recipe_id, run_ids, resin_id = (
            _seed_two_jar_runs_with_one_recipe()
        )
        actors = _seed_l1_actors_and_stock(
            planner_id=planner_id,
            resin_id=resin_id,
        )
        return planner_id, recipe_id, run_ids, actors


def test_same_bag_coverage_and_delivery_races_are_serialized_on_postgres():
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
        planner_id, recipe_id, run_ids, actors = _seed_postgres_scenario(app)

        client = app.test_client()
        operation_key = str(uuid4())
        idempotency_barrier = Barrier(2)

        def calculate_with_same_key():
            with app.test_client() as concurrent_client:
                idempotency_barrier.wait(timeout=10)
                response = concurrent_client.post(
                    "/api/scm/v1/requerimientos-preparacion/calcular",
                    headers={
                        "X-Actor-Id": str(planner_id),
                        "Idempotency-Key": operation_key,
                    },
                    json={"corrida_fabricacion_id": str(run_ids[0])},
                )
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            idempotency_results = list(
                executor.map(lambda _: calculate_with_same_key(), range(2))
            )

        assert [value[0] for value in idempotency_results] == [200, 200]
        assert idempotency_results[0][1] == idempotency_results[1][1]
        requirement_ids = [UUID(idempotency_results[0][1]["id"])]

        for run_id in run_ids[1:]:
            generated = client.post(
                "/api/scm/v1/requerimientos-preparacion/calcular",
                headers=_headers(planner_id),
                json={"corrida_fabricacion_id": str(run_id)},
            )
            assert generated.status_code == 200, generated.get_json()
            requirement_ids.append(UUID(generated.get_json()["id"]))
        with app.app_context():
            requirements = [
                db.session.get(ScmRequerimientoMaterialPreparado, value)
                for value in requirement_ids
            ]
            for requirement in requirements:
                requirement.cantidad_requerida_kg = Decimal("10.000")
                requirement.version += 1
            bag_id = _seed_free_prepared_bag(
                planner_id=planner_id,
                recipe_id=recipe_id,
                composition_hash=requirements[0].composicion_hash,
                location_id=actors["prepared_storage_id"],
                quantity="10.000",
            )
            coverage_targets = [
                (value.id, value.version) for value in requirements
            ]
            db.session.commit()

        coverage_barrier = Barrier(2)

        def cover_once(target):
            requirement_id, requirement_version = target
            with app.test_client() as concurrent_client:
                coverage_barrier.wait(timeout=10)
                response = concurrent_client.post(
                    "/api/scm/v1/requerimientos-preparacion/"
                    f"{requirement_id}/asignaciones-stock",
                    headers=_headers(planner_id),
                    json={
                        "version": requirement_version,
                        "bolsa_ids": [str(bag_id)],
                        "motivo": "Carrera controlada de cobertura PostgreSQL",
                    },
                )
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            coverage_results = list(executor.map(cover_once, coverage_targets))

        assert sorted(value[0] for value in coverage_results) == [201, 409]
        assert all(value[0] < 500 for value in coverage_results)
        with app.app_context():
            assignments = ScmAsignacionRequerimientoPreparacion.query.filter_by(
                bolsa_id=bag_id,
            ).all()
            assert len(assignments) == 1
            assignment_id = assignments[0].id
            winning_run_id = assignments[0].requerimiento.corrida_fabricacion_id
            work_id = _seed_work_color(
                planner_id=planner_id,
                run_id=winning_run_id,
            )
            competing_work_id = _seed_work_color(
                planner_id=planner_id,
                run_id=winning_run_id,
            )
            db.session.commit()

        reserved = client.post(
            f"/api/scm/v1/trabajos-color/{work_id}/reservas-material-preparado",
            headers=_headers(planner_id),
            json={
                "asignacion_id": str(assignment_id),
                "bolsa_id": str(bag_id),
                "motivo": "Reserva valida previa al despacho concurrente",
            },
        )
        assert reserved.status_code == 201, reserved.get_json()
        prepared = client.post(
            "/api/scm/v1/reservas-material-preparado/"
            f"{reserved.get_json()['id']}/preparar-entrega",
            headers=_headers(actors["warehouse_id"]),
            json={
                "version": reserved.get_json()["version"],
                "ubicacion_destino_id": actors["production_point_id"],
                "motivo": "Preparar entrega para carrera PostgreSQL",
            },
        )
        assert prepared.status_code == 201, prepared.get_json()
        delivery = prepared.get_json()["entrega"]
        delivery_barrier = Barrier(2)

        def dispatch():
            with app.test_client() as concurrent_client:
                delivery_barrier.wait(timeout=10)
                response = concurrent_client.post(
                    "/api/scm/v1/entregas-material-preparado/"
                    f"{delivery['id']}/despachar",
                    headers=_headers(actors["warehouse_id"]),
                    json={
                        "version": delivery["version"],
                        "motivo": "Despacho concurrente PostgreSQL",
                    },
                )
                return response.status_code, response.get_json()

        def reserve_again():
            with app.test_client() as concurrent_client:
                delivery_barrier.wait(timeout=10)
                response = concurrent_client.post(
                    "/api/scm/v1/trabajos-color/"
                    f"{competing_work_id}/reservas-material-preparado",
                    headers=_headers(planner_id),
                    json={
                        "asignacion_id": str(assignment_id),
                        "bolsa_id": str(bag_id),
                        "motivo": "Segunda reserva concurrente no permitida",
                    },
                )
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_dispatch = executor.submit(dispatch)
            future_reserve = executor.submit(reserve_again)
            dispatch_result = future_dispatch.result(timeout=20)
            reserve_result = future_reserve.result(timeout=20)

        assert dispatch_result[0] == 200, dispatch_result[1]
        assert reserve_result[0] == 409, reserve_result[1]
        assert dispatch_result[0] < 500 and reserve_result[0] < 500
        with app.app_context():
            source = ScmSaldoMaterialPreparado.query.filter_by(
                receta_revision_id=recipe_id,
                ubicacion_id=actors["prepared_storage_id"],
            ).one()
            destination = ScmSaldoMaterialPreparado.query.filter_by(
                receta_revision_id=recipe_id,
                ubicacion_id=actors["production_point_id"],
            ).one()
            bag = db.session.get(ScmBolsaMaterialPreparado, bag_id)
            assert Decimal(source.cantidad_fisica_kg) == Decimal("0.000")
            assert Decimal(source.cantidad_reservada_kg) == Decimal("0.000")
            assert Decimal(destination.cantidad_fisica_kg) == Decimal("10.000")
            assert Decimal(destination.cantidad_reservada_kg) == Decimal("10.000")
            assert bag.estado == "EMITIDA"
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


def test_legacy_premix_and_canonical_requirement_are_mutually_exclusive():
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
        with app.app_context():
            (
                planner_id,
                warehouse_id,
                order_id,
                run_id,
                _resin_id,
                _pigment_id,
            ) = _seed_us010b()

        client = app.test_client()
        generated = client.post(
            f"/api/scm/v1/ordenes-fabricacion/{order_id}/"
            "requerimientos-material/generar",
            headers=_headers(planner_id),
            json={},
        )
        assert generated.status_code == 201, generated.get_json()
        reserved = client.post(
            f"/api/scm/v1/corridas-fabricacion/{run_id}/materiales/reservar",
            headers=_headers(planner_id),
            json={},
        )
        assert reserved.status_code == 200, reserved.get_json()
        for requirement in reserved.get_json()["requerimientos"]:
            for reservation in requirement["reservas"]:
                emitted = client.post(
                    f"/api/scm/v1/reservas-material/{reservation['id']}/emitir",
                    headers=_headers(warehouse_id),
                    json={
                        "cantidad_kg": reservation["cantidad_kg"],
                        "motivo": "Preparar carrera de cutover PostgreSQL",
                    },
                )
                assert emitted.status_code == 201, emitted.get_json()

        barrier = Barrier(2)

        def confirm_legacy():
            with app.test_client() as concurrent_client:
                barrier.wait(timeout=10)
                response = concurrent_client.post(
                    f"/api/scm/v1/corridas-fabricacion/{run_id}/premezclas",
                    headers=_headers(planner_id),
                    json={
                        "motivo": "Confirmar premezcla legacy en carrera",
                        "genealogia_tipo": "EXACTA",
                    },
                )
                return response.status_code, response.get_json()

        def calculate_canonical():
            with app.test_client() as concurrent_client:
                barrier.wait(timeout=10)
                response = concurrent_client.post(
                    "/api/scm/v1/requerimientos-preparacion/calcular",
                    headers=_headers(planner_id),
                    json={"corrida_fabricacion_id": str(run_id)},
                )
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_legacy = executor.submit(confirm_legacy)
            future_canonical = executor.submit(calculate_canonical)
            results = [
                future_legacy.result(timeout=20),
                future_canonical.result(timeout=20),
            ]

        assert sum(value[0] < 300 for value in results) == 1, results
        assert sorted(value[0] for value in results)[1] == 409
        assert all(value[0] < 500 for value in results)
        with app.app_context():
            canonical_count = ScmRequerimientoMaterialPreparado.query.filter(
                ScmRequerimientoMaterialPreparado.corrida_fabricacion_id
                == run_id,
                ScmRequerimientoMaterialPreparado.estado != "CANCELADA",
            ).count()
            legacy_count = ScmLotePremezcla.query.filter_by(
                corrida_fabricacion_id=run_id,
            ).count()
            assert canonical_count + legacy_count == 1
            assert not (canonical_count and legacy_count)
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
