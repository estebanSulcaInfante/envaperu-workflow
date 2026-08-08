import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


MIGRATION_MODULE = (
    "migrations.versions.f79b8c4d0e31_add_role_workspace_preferences"
)


def _create_previous_schema(connection):
    connection.execute(text("""
        CREATE TABLE trabajador (
            id INTEGER PRIMARY KEY,
            codigo VARCHAR(20) NOT NULL
        )
    """))
    connection.execute(text("""
        CREATE TABLE rol_operativo (
            id INTEGER PRIMARY KEY,
            codigo VARCHAR(20) NOT NULL,
            nombre VARCHAR(100) NOT NULL,
            activo BOOLEAN NOT NULL DEFAULT 1
        )
    """))
    connection.execute(text("""
        CREATE TABLE trabajador_rol (
            trabajador_id INTEGER NOT NULL,
            rol_operativo_id INTEGER NOT NULL,
            PRIMARY KEY (trabajador_id, rol_operativo_id)
        )
    """))


def _run_upgrade(connection, monkeypatch):
    migration = importlib.import_module(MIGRATION_MODULE)
    operations = Operations(MigrationContext.configure(connection))
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    return migration


def test_workspace_role_migration_backfills_only_one_active_role_and_enforces_one_primary(
    monkeypatch,
):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_previous_schema(connection)
        connection.execute(text("""
            INSERT INTO trabajador (id, codigo)
            VALUES (1, 'UNO'), (2, 'VARIOS'), (3, 'UNO-ACTIVO')
        """))
        connection.execute(text("""
            INSERT INTO rol_operativo (id, codigo, nombre, activo)
            VALUES
              (10, 'R1', 'Rol 1', 1),
              (20, 'R2', 'Rol 2', 1),
              (30, 'R3', 'Rol 3', 0),
              (40, 'R4', 'Rol 4', 1)
        """))
        connection.execute(text("""
            INSERT INTO trabajador_rol (trabajador_id, rol_operativo_id)
            VALUES (1, 10), (2, 10), (2, 20), (3, 30), (3, 40)
        """))

        migration = _run_upgrade(connection, monkeypatch)

        assert connection.execute(text("""
            SELECT trabajador_id, rol_operativo_id, es_principal
            FROM trabajador_rol
            ORDER BY trabajador_id, rol_operativo_id
        """)).tuples().all() == [
            (1, 10, 1),
            (2, 10, 0),
            (2, 20, 0),
            (3, 30, 0),
            (3, 40, 1),
        ]

        connection.execute(text("""
            UPDATE trabajador_rol
            SET es_principal = 1
            WHERE trabajador_id = 2 AND rol_operativo_id = 10
        """))
        with pytest.raises(IntegrityError):
            connection.execute(text("""
                UPDATE trabajador_rol
                SET es_principal = 1
                WHERE trabajador_id = 2 AND rol_operativo_id = 20
            """))

        role_columns = {
            item["name"] for item in inspect(connection).get_columns(
                "rol_operativo"
            )
        }
        assert {
            "workspace_focus",
            "workspace_start_feature",
            "version",
        } <= role_columns
        assert "scm_rol_workspace_preferencia" in inspect(
            connection
        ).get_table_names()
        with pytest.raises(IntegrityError):
            connection.execute(text("""
                INSERT INTO scm_rol_workspace_preferencia (
                    rol_operativo_id, feature_key, prioridad, fijada,
                    created_by_id, updated_by_id
                ) VALUES (10, 'invalid.priority', 1000, 0, 1, 1)
            """))

        index_names = {
            item["name"] for item in inspect(connection).get_indexes(
                "scm_rol_workspace_preferencia"
            )
        }
        assert {
            "ix_scm_rol_workspace_preferencia_orden",
            "ix_scm_rol_workspace_preferencia_created_by",
            "ix_scm_rol_workspace_preferencia_updated_by",
        } <= index_names

        migration.downgrade()
        assert "scm_rol_workspace_preferencia" not in inspect(
            connection
        ).get_table_names()
        assert "es_principal" not in {
            item["name"] for item in inspect(connection).get_columns(
                "trabajador_rol"
            )
        }


def test_workspace_role_migration_supports_empty_fresh_catalog(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_previous_schema(connection)
        _run_upgrade(connection, monkeypatch)

        assert connection.execute(text(
            "SELECT count(*) FROM scm_rol_workspace_preferencia"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT count(*) FROM trabajador_rol WHERE es_principal"
        )).scalar_one() == 0
