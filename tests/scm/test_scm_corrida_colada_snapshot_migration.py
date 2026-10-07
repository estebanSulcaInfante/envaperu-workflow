from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _migration_module():
    migrations = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    matches = list(migrations.glob("*_add_run_colada_snapshot.py"))
    assert len(matches) == 1
    spec = spec_from_file_location("add_run_colada_snapshot", matches[0])
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_colada_snapshot_migration_is_nullable_and_preserves_work_history():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE scm_corrida_fabricacion (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql("""
            CREATE TABLE scm_trabajo_color (
                id VARCHAR(36) PRIMARY KEY,
                peso_colada_snapshot_g NUMERIC(15, 4) NOT NULL
            )
        """)
        connection.execute(text(
            "INSERT INTO scm_corrida_fabricacion (id) VALUES ('run-existing')"
        ))
        connection.execute(text(
            "INSERT INTO scm_trabajo_color (id, peso_colada_snapshot_g) "
            "VALUES ('work-existing', 4.2500)"
        ))

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            _migration_module().upgrade()

        run_columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("scm_corrida_fabricacion")
        }
        override = run_columns["snapshot_peso_colada_gr"]
        assert override["nullable"] is True
        assert override["type"].precision == 12
        assert override["type"].scale == 4
        assert connection.execute(text(
            "SELECT snapshot_peso_colada_gr FROM scm_corrida_fabricacion "
            "WHERE id='run-existing'"
        )).scalar_one() is None
        assert connection.execute(text(
            "SELECT peso_colada_snapshot_g FROM scm_trabajo_color "
            "WHERE id='work-existing'"
        )).scalar_one() == 4.25

        connection.execute(text(
            "INSERT INTO scm_corrida_fabricacion (id, snapshot_peso_colada_gr) "
            "VALUES ('run-zero', 0)"
        ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO scm_corrida_fabricacion (id, snapshot_peso_colada_gr) "
                "VALUES ('run-negative', -0.0001)"
            ))

    engine.dispose()
