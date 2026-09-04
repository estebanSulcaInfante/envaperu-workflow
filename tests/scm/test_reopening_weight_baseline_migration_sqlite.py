import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.scm.test_manga_reopening_migration_sqlite import (
    MIGRATION_MODULE as K6_MODULE,
    _previous_schema,
)


K8_MODULE = (
    "migrations.versions.f93d4e6a8c02_add_reopening_weight_baseline"
)


def _apply(connection, monkeypatch, module_name, action):
    migration = importlib.import_module(module_name)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    getattr(migration, action)()


def _seed_reopening(connection):
    connection.execute(text("""
        UPDATE scm_pesaje_manga SET estado = 'REABIERTO' WHERE id = 1
    """))
    connection.execute(text("""
        INSERT INTO scm_reapertura_manga (
            public_id, manga_id, pesaje_id, motivo,
            reabierta_por_id, operation_id
        ) VALUES (
            'reopening-existing', 100, 1, 'Cierre accidental previo',
            10, 'operation-reopen'
        )
    """))


def test_k8_migration_clasifica_legacy_y_exige_linea_base_coherente(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _previous_schema(connection)
        _apply(connection, monkeypatch, K6_MODULE, "upgrade")
        _seed_reopening(connection)

        _apply(connection, monkeypatch, K8_MODULE, "upgrade")
        inspector = inspect(connection)
        columns = {
            item["name"]: item
            for item in inspector.get_columns("scm_reapertura_manga")
        }
        assert columns["tipo_reapertura"]["nullable"] is False
        assert columns["peso_base_neto_kg"]["nullable"] is True
        assert connection.execute(text("""
            SELECT tipo_reapertura, peso_base_neto_kg
            FROM scm_reapertura_manga
            WHERE public_id = 'reopening-existing'
        """)).one() == ("CIERRE_ACCIDENTAL", None)

        connection.execute(text("""
            INSERT INTO scm_manga (id) VALUES (101)
        """))
        connection.execute(text("""
            INSERT INTO scm_operacion (operation_id) VALUES
              ('operation-weighing-continue'), ('operation-reopening-continue')
        """))
        connection.execute(text("""
            INSERT INTO scm_pesaje_manga (
                public_id, manga_id, operation_id, estado
            ) VALUES (
                'weighing-continue', 101, 'operation-weighing-continue',
                'REABIERTO'
            )
        """))
        connection.execute(text("""
            INSERT INTO scm_reapertura_manga (
                public_id, manga_id, pesaje_id, motivo,
                tipo_reapertura, peso_base_neto_kg,
                reabierta_por_id, operation_id
            ) VALUES (
                'reopening-continue', 101, 2, 'Agregar más piezas',
                'CONTINUAR_LLENADO', 5.000,
                10, 'operation-reopening-continue'
            )
        """))

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""
                    UPDATE scm_reapertura_manga
                    SET peso_base_neto_kg = 1.000
                    WHERE public_id = 'reopening-existing'
                """))
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""
                    UPDATE scm_reapertura_manga
                    SET peso_base_neto_kg = NULL
                    WHERE public_id = 'reopening-continue'
                """))

        with pytest.raises(RuntimeError, match="línea base"):
            _apply(connection, monkeypatch, K8_MODULE, "downgrade")


def test_k8_migration_revierte_si_no_existen_lineas_base(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _previous_schema(connection)
        _apply(connection, monkeypatch, K6_MODULE, "upgrade")
        _seed_reopening(connection)
        _apply(connection, monkeypatch, K8_MODULE, "upgrade")

        _apply(connection, monkeypatch, K8_MODULE, "downgrade")
        columns = {
            item["name"]
            for item in inspect(connection).get_columns("scm_reapertura_manga")
        }
        assert "tipo_reapertura" not in columns
        assert "peso_base_neto_kg" not in columns
