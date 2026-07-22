import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "c42d8e6f1a03_normalize_linea_familia_many_to_many.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_c42d8e6f1a03_sqlite_test",
        MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_previous_schema(connection):
    connection.execute(text("""
        CREATE TABLE linea (
            id INTEGER PRIMARY KEY,
            codigo INTEGER NOT NULL UNIQUE,
            nombre VARCHAR(50) NOT NULL UNIQUE
        )
    """))
    connection.execute(text("""
        CREATE TABLE familia (
            id INTEGER PRIMARY KEY,
            codigo INTEGER NOT NULL UNIQUE,
            nombre VARCHAR(100) NOT NULL UNIQUE
        )
    """))
    for table_name, nullable in (
        ("producto_terminado", "NOT NULL"),
        ("pieza_color", "NOT NULL"),
        ("pieza", "NULL"),
    ):
        connection.execute(text(f"""
            CREATE TABLE {table_name} (
                id INTEGER PRIMARY KEY,
                linea_id INTEGER {nullable},
                familia_id INTEGER {nullable},
                FOREIGN KEY (linea_id) REFERENCES linea(id),
                FOREIGN KEY (familia_id) REFERENCES familia(id)
            )
        """))


def test_linea_familia_migration_backfills_distinct_pairs_in_sqlite():
    engine = create_engine("sqlite:///:memory:")
    migration = _load_migration()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            _create_previous_schema(connection)
            connection.execute(text("""
                INSERT INTO linea (id, codigo, nombre) VALUES
                    (1, 10, 'HOGAR'),
                    (2, 20, 'INDUSTRIAL')
            """))
            connection.execute(text("""
                INSERT INTO familia (id, codigo, nombre) VALUES
                    (10, 100, 'BALDES'),
                    (20, 200, 'JARRAS'),
                    (30, 300, 'TAPAS')
            """))
            connection.execute(text("""
                INSERT INTO producto_terminado
                    (id, linea_id, familia_id)
                VALUES (1, 1, 10), (2, 1, 20)
            """))
            connection.execute(text("""
                INSERT INTO pieza_color (id, linea_id, familia_id)
                VALUES (1, 1, 10), (2, 2, 20)
            """))
            connection.execute(text("""
                INSERT INTO pieza (id, linea_id, familia_id)
                VALUES
                    (1, 2, 30),
                    (2, NULL, NULL),
                    (3, 1, NULL)
            """))

            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            assert connection.execute(text("""
                SELECT linea_id, familia_id, activo, version
                FROM linea_familia
                ORDER BY linea_id, familia_id
            """)).tuples().all() == [
                (1, 10, True, 1),
                (1, 20, True, 1),
                (2, 20, True, 1),
                (2, 30, True, 1),
            ]

            inspector = inspect(connection)
            for table_name in ("linea", "familia"):
                columns = {
                    column["name"]: column
                    for column in inspector.get_columns(table_name)
                }
                assert columns["activo"]["nullable"] is False
                assert columns["version"]["nullable"] is False
                assert connection.execute(text(
                    f"SELECT activo, version FROM {table_name} ORDER BY id LIMIT 1"
                )).one() == (True, 1)

            indexes = {
                item["name"]
                for item in inspector.get_indexes("linea_familia")
            }
            assert {
                "ix_linea_familia_linea_activo",
                "ix_linea_familia_familia_activo",
            } <= indexes

            savepoint = connection.begin_nested()
            with pytest.raises(IntegrityError):
                connection.execute(text("""
                    INSERT INTO linea_familia (linea_id, familia_id)
                    VALUES (1, 10)
                """))
            savepoint.rollback()

            savepoint = connection.begin_nested()
            with pytest.raises(IntegrityError):
                connection.execute(text("""
                    UPDATE linea_familia SET version = 0 WHERE id = 1
                """))
            savepoint.rollback()

            savepoint = connection.begin_nested()
            with pytest.raises(IntegrityError):
                connection.execute(text("""
                    DELETE FROM familia WHERE id = 10
                """))
            savepoint.rollback()

            migration.downgrade()

            inspector = inspect(connection)
            assert "linea_familia" not in inspector.get_table_names()
            assert {"activo", "version"}.isdisjoint(
                column["name"] for column in inspector.get_columns("linea")
            )
            assert connection.execute(text(
                "SELECT count(*) FROM producto_terminado"
            )).scalar_one() == 2
    finally:
        engine.dispose()


def test_linea_familia_migration_repairs_missing_catalogs_in_adopted_sqlite():
    engine = create_engine("sqlite:///:memory:")
    migration = _load_migration()
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE pieza (
                    id INTEGER PRIMARY KEY,
                    linea_id INTEGER,
                    familia_id INTEGER
                )
            """))
            connection.execute(text("""
                INSERT INTO pieza (id, linea_id, familia_id)
                VALUES (1, 404, 505)
            """))

            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            inspector = inspect(connection)
            assert {"linea", "familia", "linea_familia"} <= set(
                inspector.get_table_names()
            )
            assert connection.execute(text(
                "SELECT count(*) FROM linea_familia"
            )).scalar_one() == 0

            migration.downgrade()
            inspector = inspect(connection)
            assert "linea_familia" not in inspector.get_table_names()
            assert {"id", "codigo", "nombre"} == {
                column["name"] for column in inspector.get_columns("linea")
            }
            assert connection.execute(text(
                "SELECT linea_id, familia_id FROM pieza WHERE id = 1"
            )).one() == (404, 505)
    finally:
        engine.dispose()
