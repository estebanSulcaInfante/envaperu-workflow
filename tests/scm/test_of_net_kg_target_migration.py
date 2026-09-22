import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_net_kg_target_migration_adds_guard_and_unassigned_capability(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE scm_corrida_fabricacion (id CHAR(32) PRIMARY KEY, ciclos_objetivo INTEGER)"))
        connection.execute(text("""CREATE TABLE scm_capacidad (
            id INTEGER PRIMARY KEY AUTOINCREMENT, codigo VARCHAR(96) UNIQUE,
            nombre VARCHAR(200), descripcion TEXT, activo BOOLEAN DEFAULT 1
        )"""))
        migration = importlib.import_module(
            "migrations.versions.f9a12c3e4d10_add_of_run_net_kg_target"
        )
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(connection))
        )
        migration.upgrade()
        assert "objetivo_neto_kg" in {
            column["name"] for column in inspect(connection).get_columns("scm_corrida_fabricacion")
        }
        assert connection.execute(text("""
            SELECT codigo FROM scm_capacidad
            WHERE codigo = 'FORMULACION_PUBLICAR_DIRECTO'
        """)).scalar_one() == "FORMULACION_PUBLICAR_DIRECTO"
        connection.execute(text("INSERT INTO scm_corrida_fabricacion (id, objetivo_neto_kg) VALUES ('a', 1.5)"))
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO scm_corrida_fabricacion (id, objetivo_neto_kg) VALUES ('b', 0)"))
        migration.downgrade()
        assert "objetivo_neto_kg" not in {
            column["name"] for column in inspect(connection).get_columns("scm_corrida_fabricacion")
        }
        # A capability assigned after deployment is installation-owned.
        assert connection.execute(text("""
            SELECT COUNT(*) FROM scm_capacidad
            WHERE codigo = 'FORMULACION_PUBLICAR_DIRECTO'
        """)).scalar_one() == 1
