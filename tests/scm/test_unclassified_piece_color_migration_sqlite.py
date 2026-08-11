import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION_MODULE = (
    "migrations.versions.f82e1f7a3c64_allow_unclassified_piece_colors"
)


def _migration(connection, monkeypatch):
    migration = importlib.import_module(MIGRATION_MODULE)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _nullable_by_name(connection):
    return {
        column["name"]: column["nullable"]
        for column in inspect(connection).get_columns("pieza_color")
    }


def test_f82_sqlite_expand_and_fail_closed_downgrade(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE pieza_color (
                sku VARCHAR(50) PRIMARY KEY,
                linea_id INTEGER NOT NULL,
                familia_id INTEGER NOT NULL,
                pieza_id INTEGER
            )
        """))
        migration = _migration(connection, monkeypatch)
        migration.upgrade()
        nullable = _nullable_by_name(connection)
        assert nullable["linea_id"] is True
        assert nullable["familia_id"] is True

        connection.execute(text("""
            INSERT INTO pieza_color (sku, linea_id, familia_id, pieza_id)
            VALUES ('PC-SIN-CLASIFICACION', NULL, NULL, 1)
        """))
        with pytest.raises(RuntimeError, match="Downgrade f82 bloqueado"):
            migration.downgrade()

        connection.execute(text(
            "DELETE FROM pieza_color WHERE sku = 'PC-SIN-CLASIFICACION'"
        ))
        connection.execute(text("""
            INSERT INTO pieza_color (sku, linea_id, familia_id, pieza_id)
            VALUES ('PC-LEGACY', 1, 1, NULL)
        """))
        migration.downgrade()
        nullable = _nullable_by_name(connection)
        assert nullable["linea_id"] is False
        assert nullable["familia_id"] is False

        migration.upgrade()
        nullable = _nullable_by_name(connection)
        assert nullable["linea_id"] is True
        assert nullable["familia_id"] is True
