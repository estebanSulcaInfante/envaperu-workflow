"""PostgreSQL-only W2 invariants for PT manual history and first-row races."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateSchema, DropSchema

from app import create_app, db
from app.config import Config
from app.models.scm_articulos import ScmArticulo
from app.models.scm_articulos import ScmArticuloProducto
from app.models.scm_catalogos import ScmCapacidad
from app.models.scm_inventory import ScmMovimientoInventario, ScmSaldoInventario, ScmUbicacionInventario
from app.models.producto import Familia, Linea, ProductoTerminado
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_kg_pt_availability_service import register_pt_manual_movement
from app.services.scm_service_support import ScmServiceError


pytestmark = pytest.mark.postgres
BACKEND_ROOT = Path(__file__).resolve().parents[2]

KG_PILOT_TABLES = (
    "scm_saldo_inventario_kg",
    "scm_movimiento_inventario_kg",
    "scm_existencia_manga_kg",
    "scm_unidad_fisica_kg",
    "scm_division_unidad_kg",
    "scm_reserva_unidad_kg",
    "scm_retiro_armado_kg",
    "scm_retiro_armado_kg_item",
    "scm_medicion_unidad_kg",
    "scm_etiqueta_unidad_kg",
    "scm_atribucion_produccion_kg",
    "scm_cierre_productivo_kg",
)

KG_FUNCTIONS = (
    "scm_kg_article_guard",
    "scm_kg_movement_guard",
    "scm_kg_logistic_unit_guard",
    "scm_kg_article_marker_guard",
    "scm_kg_custody_append_only",
    "scm_guard_pt_manual_movement_immutable",
)


def _schema_fixture():
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required")
    base_url = make_url(raw_url)
    schema = f"scm_w2_{uuid4().hex[:12]}"
    admin_engine = create_engine(base_url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    query = dict(base_url.query)
    query["options"] = f"-csearch_path={schema}"
    return admin_engine, schema, base_url.set(query=query)


def _upgrade(schema_url):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = schema_url.render_as_string(hide_password=False)
    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "db", "upgrade", "f98a1b2c3d08"],
        cwd=BACKEND_ROOT, env=environment, capture_output=True, text=True,
        timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def postgres_w2_app():
    admin_engine, schema, schema_url = _schema_fixture()
    original_uri = Config.SQLALCHEMY_DATABASE_URI
    app = None
    try:
        _upgrade(schema_url)
        Config.SQLALCHEMY_DATABASE_URI = schema_url.render_as_string(hide_password=False)
        app = create_app()
        app.config.update(TESTING=True, PT_MANUAL_WRITE_ENABLED=True)
        with app.app_context():
            ensure_initial_scm_configuration()
            db.session.commit()
        yield app
    finally:
        Config.SQLALCHEMY_DATABASE_URI = original_uri
        if app is not None:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def test_f98_seeds_pt_manual_capability_without_users(postgres_w2_app):
    with postgres_w2_app.app_context():
        capability = ScmCapacidad.query.filter_by(codigo="INVENTARIO_PT_MOVIMIENTO").one()
        assert capability.nombre == "Registrar entradas y salidas manuales de PT"
        for role_code in ("GERENTE_GENERAL", "ALMACEN_RECEPCION"):
            role = RolOperativo.query.filter_by(codigo=role_code).one()
            assert capability not in role.capacidades
        assert Trabajador.query.filter(Trabajador.codigo.like("TRB-W2-PG-%")).count() == 0


def test_postgres_kg_pilot_tables_and_functions_are_locked_down(postgres_w2_app):
    with postgres_w2_app.app_context():
        schema = db.session.execute(text("SELECT current_schema()")).scalar_one()
        qualified = lambda name: f'"{schema}"."{name}"'
        for table in KG_PILOT_TABLES:
            security = db.session.execute(text("""
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = :schema AND c.relname = :table
            """), {"schema": schema, "table": table}).one()
            assert security == (True, True)
            assert not db.session.execute(
                text("SELECT has_table_privilege('public', :table, 'SELECT')"),
                {"table": qualified(table)},
            ).scalar_one()
            for role in ("anon", "authenticated"):
                exists = db.session.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                    {"role": role},
                ).scalar()
                if exists:
                    assert not db.session.execute(
                        text("SELECT has_table_privilege(:role, :table, 'SELECT')"),
                        {"role": role, "table": qualified(table)},
                    ).scalar_one()

        for function in KG_FUNCTIONS:
            function_security = db.session.execute(text("""
                SELECT p.proconfig
                FROM pg_proc AS p
                JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = :schema AND p.proname = :function
                  AND pg_get_function_identity_arguments(p.oid) = ''
            """), {"schema": schema, "function": function}).one()
            assert any(
                str(item).startswith("search_path=pg_catalog")
                and schema in str(item)
                for item in (function_security[0] or ())
            )
            assert not db.session.execute(
                text("SELECT has_function_privilege('public', :function, 'EXECUTE')"),
                {"function": f'{qualified(function)}()'},
            ).scalar_one()


def _actor(app):
    with app.app_context():
        role = RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        capability = ScmCapacidad.query.filter_by(codigo="INVENTARIO_PT_MOVIMIENTO").one()
        if capability not in role.capacidades:
            role.capacidades.append(capability)
        actor = Trabajador(
            codigo=f"TRB-W2-PG-{uuid4().hex[:8]}", nombres="W2", apellidos="PG", activo=True,
            roles=[role],
        )
        db.session.add(actor)
        db.session.flush()
        db.session.commit()
        return actor.id


def _target(app):
    with app.app_context():
        actor_id = _actor(app)
        code = f"PT-W2-PG-{uuid4().hex[:8].upper()}"
        linea = Linea(codigo=int(uuid4().hex[:6], 16) % 900000 + 100000, nombre="Linea W2 PG")
        familia = Familia(codigo=int(uuid4().hex[:6], 16) % 900000 + 100000, nombre="Familia W2 PG")
        product = ProductoTerminado(cod_sku_pt=code, producto="PT W2 PG", linea_rel=linea, familia_rel=familia)
        article = ScmArticulo(
            codigo=code, nombre="PT W2 PG",
            clase="PRODUCTO_TERMINADO", unidad_base="UN", unidad_inventario="UN",
        )
        location = ScmUbicacionInventario(
            codigo=f"W2-PG-{uuid4().hex[:8].upper()}", nombre="Ubicacion W2 PG",
            clases_articulo_json=["PRODUCTO_TERMINADO"],
        )
        db.session.add_all([product, article, location])
        db.session.flush()
        article.producto = ScmArticuloProducto(producto_terminado=product)
        db.session.commit()
        return actor_id, article.id, location.id


def test_postgres_pt_manual_rows_are_append_only(postgres_w2_app):
    app = postgres_w2_app
    actor_id, article_id, location_id = _target(app)
    with app.app_context():
        register_pt_manual_movement(
            db.session, actor_id=actor_id, operation_id=uuid4(), data={
                "articulo_scm_id": article_id, "ubicacion_id": location_id,
                "tipo": "ENTRADA", "cantidad": 2, "version": 1,
                "fecha_operativa": "2026-09-19", "motivo": "Prueba append only",
            },
        )
        movement_id = db.session.scalar(db.select(ScmMovimientoInventario.id))
        with pytest.raises(DBAPIError, match="append-only|PT manual"):
            db.session.execute(text(
                "UPDATE scm_movimiento_inventario SET motivo = 'mutado' WHERE id = :id"
            ), {"id": str(movement_id)})
        db.session.rollback()
        with pytest.raises(DBAPIError, match="append-only|PT manual"):
            db.session.execute(text(
                "DELETE FROM scm_movimiento_inventario WHERE id = :id"
            ), {"id": str(movement_id)})
        db.session.rollback()


def test_postgres_first_pt_manual_row_has_one_winner_and_deterministic_retry(postgres_w2_app):
    app = postgres_w2_app
    actor_id, article_id, location_id = _target(app)

    def attempt():
        with app.app_context():
            try:
                payload = register_pt_manual_movement(
                    db.session, actor_id=actor_id, operation_id=uuid4(), data={
                        "articulo_scm_id": article_id, "ubicacion_id": location_id,
                        "tipo": "ENTRADA", "cantidad": 1, "version": 1,
                        "fecha_operativa": "2026-09-19", "motivo": "Carrera inicial",
                    },
                )
                return "ok", payload["saldo"]["version"]
            except Exception as error:  # assert no raw IntegrityError escapes the boundary
                db.session.rollback()
                return "error", getattr(error, "code", type(error).__name__)
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _item: attempt(), range(2)))
    assert [item[0] for item in outcomes].count("ok") == 1
    assert all(item[1] in {"VERSION_CONFLICT", "PT_MANUAL_BALANCE_CONCURRENT_CREATE"} or item[0] == "ok" for item in outcomes)
    with app.app_context():
        balance = db.session.scalar(db.select(ScmSaldoInventario).where(
            ScmSaldoInventario.articulo_scm_id == article_id,
            ScmSaldoInventario.ubicacion_id == location_id,
        ))
        assert balance.cantidad_fisica == 1
        assert db.session.scalar(db.select(db.func.count(ScmMovimientoInventario.id)).where(
            ScmMovimientoInventario.saldo_id == balance.id,
        )) == 1


def test_postgres_same_idempotency_key_replays_and_conflicting_payload_fails(postgres_w2_app):
    app = postgres_w2_app
    actor_id, article_id, location_id = _target(app)
    operation_id = uuid4()
    command = {
        "articulo_scm_id": article_id, "ubicacion_id": location_id,
        "tipo": "ENTRADA", "cantidad": 1, "version": 1,
        "fecha_operativa": "2026-09-19", "motivo": "Replay PG",
    }
    with app.app_context():
        first = register_pt_manual_movement(
            db.session, actor_id=actor_id, operation_id=operation_id, data=command,
        )
        replay = register_pt_manual_movement(
            db.session, actor_id=actor_id, operation_id=operation_id, data=command,
        )
        assert replay == first
        with pytest.raises(ScmServiceError, match="ya fue usada") as error:
            register_pt_manual_movement(
                db.session, actor_id=actor_id, operation_id=operation_id,
                data={**command, "cantidad": 2},
            )
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
        db.session.rollback()
        assert db.session.scalar(db.select(db.func.count(ScmMovimientoInventario.id))) == 1


def test_postgres_same_idempotency_key_concurrent_requests_have_one_effect(postgres_w2_app):
    app = postgres_w2_app
    actor_id, article_id, location_id = _target(app)
    operation_id = uuid4()
    command = {
        "articulo_scm_id": article_id, "ubicacion_id": location_id,
        "tipo": "ENTRADA", "cantidad": 1, "version": 1,
        "fecha_operativa": "2026-09-19", "motivo": "Replay PG concurrente",
    }
    start = Barrier(2)

    def attempt():
        with app.app_context():
            try:
                start.wait(timeout=30)
                payload = register_pt_manual_movement(
                    db.session, actor_id=actor_id, operation_id=operation_id, data=command,
                )
                return "ok", payload
            except Exception as error:
                db.session.rollback()
                return "error", getattr(error, "code", type(error).__name__)
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _item: attempt(), range(2)))
    assert [item[0] for item in outcomes] == ["ok", "ok"]
    assert outcomes[0][1] == outcomes[1][1]
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(ScmMovimientoInventario.id))) == 1
