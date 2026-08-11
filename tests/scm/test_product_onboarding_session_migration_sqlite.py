import importlib
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


MIGRATION_MODULE = (
    "migrations.versions.f81d0e6f2b53_add_product_onboarding_sessions"
)


def _previous_schema(connection):
    connection.execute(text("""
        CREATE TABLE trabajador (
            id INTEGER PRIMARY KEY,
            codigo VARCHAR(20) NOT NULL
        )
    """))
    connection.execute(text("""
        CREATE TABLE producto_terminado (
            cod_sku_pt VARCHAR(50) PRIMARY KEY
        )
    """))
    connection.execute(text(
        "INSERT INTO trabajador (id, codigo) VALUES (1, 'TR-001')"
    ))
    connection.execute(text(
        "INSERT INTO producto_terminado (cod_sku_pt) VALUES ('PT-001')"
    ))


def _run_upgrade(connection, monkeypatch):
    migration = importlib.import_module(MIGRATION_MODULE)
    operations = Operations(MigrationContext.configure(connection))
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    return migration


def test_session_migration_sqlite_contract_and_downgrade(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _previous_schema(connection)
        migration = _run_upgrade(connection, monkeypatch)

        inspector = inspect(connection)
        assert "scm_alta_producto_sesion" in inspector.get_table_names()
        columns = {
            item["name"] for item in inspector.get_columns(
                "scm_alta_producto_sesion"
            )
        }
        assert {
            "id",
            "titulo",
            "producto_terminado_id",
            "estado",
            "paso_actual",
            "borrador_json",
            "estados_paso_json",
            "bloqueos_paso_json",
            "fuentes_json",
            "referencias_json",
            "readiness_json",
            "invalidated_steps_json",
            "application_journal_json",
            "creada_por_id",
            "actualizada_por_id",
            "version",
            "created_at",
            "updated_at",
            "finalizada_at",
            "abandonada_at",
        } <= columns
        index_names = {
            item["name"] for item in inspector.get_indexes(
                "scm_alta_producto_sesion"
            )
        }
        assert {
            "ix_scm_alta_producto_estado_actualizada",
            "ix_scm_alta_producto_producto",
            "ix_scm_alta_producto_creada_por",
            "ix_scm_alta_producto_actualizada_por",
        } <= index_names

        connection.execute(text("""
            INSERT INTO scm_alta_producto_sesion (
                id, titulo, producto_terminado_id, estado, paso_actual,
                creada_por_id, actualizada_por_id
            ) VALUES (
                :id, 'Alta PT', 'PT-001', 'BORRADOR', 'IDENTIDAD', 1, 1
            )
        """), {"id": uuid4().hex})
        row = connection.execute(text("""
            SELECT estado, paso_actual, version, borrador_json,
                   invalidated_steps_json
            FROM scm_alta_producto_sesion
        """)).mappings().one()
        assert row["estado"] == "BORRADOR"
        assert row["paso_actual"] == "IDENTIDAD"
        assert row["version"] == 1
        assert row["borrador_json"] == "{}"
        assert row["invalidated_steps_json"] == "[]"

        with pytest.raises(IntegrityError):
            connection.execute(text("""
                UPDATE scm_alta_producto_sesion SET version = 0
            """))
        with pytest.raises(IntegrityError):
            connection.execute(text("""
                UPDATE scm_alta_producto_sesion SET paso_actual = 'OTRO'
            """))

        migration.downgrade()
        assert "scm_alta_producto_sesion" not in inspect(
            connection
        ).get_table_names()
