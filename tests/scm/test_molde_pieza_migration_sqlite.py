import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "8f4c2d1a9b7e_normalize_molde_pieza_many_to_many.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_8f4c2d1a9b7e_sqlite_test",
        MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(connection):
    connection.execute(text("""
        CREATE TABLE molde (
            codigo VARCHAR(50) PRIMARY KEY,
            nombre VARCHAR(100) NOT NULL,
            peso_tiro_gr FLOAT NOT NULL
        )
    """))
    connection.execute(text("""
        CREATE TABLE pieza (
            id INTEGER PRIMARY KEY,
            molde_id VARCHAR(50) NOT NULL REFERENCES molde(codigo),
            nombre VARCHAR(200) NOT NULL,
            linea_id INTEGER,
            familia_id INTEGER,
            cavidades INTEGER NOT NULL,
            peso_unitario_gr FLOAT NOT NULL,
            CONSTRAINT uq_molde_pieza_nombre UNIQUE (molde_id, nombre)
        )
    """))
    connection.execute(text("""
        CREATE TABLE pieza_color (
            sku VARCHAR(50) PRIMARY KEY,
            pieza_id INTEGER REFERENCES pieza(id)
        )
    """))
    connection.execute(text("""
        INSERT INTO molde (codigo, nombre, peso_tiro_gr)
        VALUES
            ('M-SQLITE-A', 'Molde SQLite A', 90.0),
            ('M-SQLITE-B', 'Molde SQLite B', 95.0)
    """))
    connection.execute(text("""
        INSERT INTO pieza (
            id, molde_id, nombre, cavidades, peso_unitario_gr
        ) VALUES
            (7, 'M-SQLITE-A', 'Tapa SQLite', 2, 10.5),
            (19, 'M-SQLITE-B', 'Base SQLite', 3, 15.25)
    """))
    connection.execute(text("""
        INSERT INTO pieza_color (sku, pieza_id)
        VALUES ('SKU-SQLITE-007', 7), ('SKU-SQLITE-019', 19)
    """))


def test_revision_molde_pieza_roundtrip_en_sqlite():
    engine = create_engine("sqlite:///:memory:")
    migration = _load_migration()
    try:
        with engine.begin() as connection:
            _legacy_schema(connection)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            columns = {
                item["name"]
                for item in inspect(connection).get_columns("pieza")
            }
            assert {"codigo", "peso_nominal_gr", "activo", "version"} <= columns
            assert {"molde_id", "cavidades", "peso_unitario_gr"}.isdisjoint(
                columns
            )
            assert connection.execute(text("""
                SELECT id, codigo, peso_nominal_gr
                FROM pieza
                ORDER BY id
            """)).tuples().all() == [
                (7, "PZ-00000007", 10.5),
                (19, "PZ-00000019", 15.25),
            ]
            assert connection.execute(text("""
                SELECT id, molde_id, pieza_id, cavidades, peso_unitario_gr
                FROM molde_pieza
                ORDER BY id
            """)).tuples().all() == [
                (7, "M-SQLITE-A", 7, 2, 10.5),
                (19, "M-SQLITE-B", 19, 3, 15.25),
            ]
            assert connection.execute(text("""
                SELECT sku, pieza_id FROM pieza_color ORDER BY sku
            """)).tuples().all() == [
                ("SKU-SQLITE-007", 7),
                ("SKU-SQLITE-019", 19),
            ]

            extra_relation_id = connection.execute(text("""
                INSERT INTO molde_pieza (
                    molde_id, pieza_id, cavidades, peso_unitario_gr
                ) VALUES ('M-SQLITE-B', 7, 1, 10.75)
                RETURNING id
            """)).scalar_one()
            assert extra_relation_id > 19
            with pytest.raises(RuntimeError, match="sin exactamente un molde"):
                migration.downgrade()

            connection.execute(
                text("DELETE FROM molde_pieza WHERE id = :relation_id"),
                {"relation_id": extra_relation_id},
            )
            connection.execute(text("DELETE FROM molde_pieza WHERE id = 19"))
            with pytest.raises(RuntimeError, match="sin exactamente un molde"):
                migration.downgrade()
            connection.execute(text("""
                INSERT INTO molde_pieza (
                    id, molde_id, pieza_id, cavidades, peso_unitario_gr
                ) VALUES (19, 'M-SQLITE-B', 19, 3, 15.25)
            """))
            migration.downgrade()

            assert "molde_pieza" not in inspect(connection).get_table_names()
            assert connection.execute(text("""
                SELECT id, molde_id, cavidades, peso_unitario_gr
                FROM pieza
                ORDER BY id
            """)).tuples().all() == [
                (7, "M-SQLITE-A", 2, 10.5),
                (19, "M-SQLITE-B", 3, 15.25),
            ]
            assert connection.execute(text("""
                SELECT sku, pieza_id FROM pieza_color ORDER BY sku
            """)).tuples().all() == [
                ("SKU-SQLITE-007", 7),
                ("SKU-SQLITE-019", 19),
            ]
    finally:
        engine.dispose()
