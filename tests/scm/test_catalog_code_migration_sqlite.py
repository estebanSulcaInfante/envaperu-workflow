import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "b31f9a2c7d04_add_catalog_code_counters.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_b31f9a2c7d04_sqlite_test",
        MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_counter_migration_seeds_existing_numeric_suffixes_in_sqlite():
    engine = create_engine("sqlite:///:memory:")
    migration = _load_migration()
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE pieza (codigo VARCHAR(64))"))
            connection.execute(text("CREATE TABLE pieza_color (sku VARCHAR(50))"))
            connection.execute(text(
                "CREATE TABLE producto_terminado (cod_sku_pt VARCHAR(50))"
            ))
            connection.execute(text("CREATE TABLE molde (codigo VARCHAR(50))"))
            connection.execute(text("""
                INSERT INTO pieza (codigo) VALUES
                    ('PZ-000009'), ('PZ-000042'), ('PZ-LEGACY'), (NULL)
            """))
            connection.execute(text("""
                INSERT INTO pieza_color (sku) VALUES
                    ('PC-7'), ('pc-000099'), ('SKU-LEGACY')
            """))
            connection.execute(text("""
                INSERT INTO producto_terminado (cod_sku_pt) VALUES
                    ('PT-001200'), ('PT-EXTERNO')
            """))
            connection.execute(text("""
                INSERT INTO molde (codigo) VALUES
                    ('ML-000003'), ('MOLDE-ANTIGUO')
            """))

            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            assert connection.execute(text("""
                SELECT clave, prefijo, siguiente_valor, ancho
                FROM correlativo_catalogo
                ORDER BY clave
            """)).tuples().all() == [
                ("MOLDE", "ML", 4, 6),
                ("PIEZA", "PZ", 43, 6),
                ("PIEZA_COLOR", "PC", 100, 6),
                ("PRODUCTO_TERMINADO", "PT", 1201, 6),
            ]

            assert connection.execute(text(
                "SELECT codigo FROM pieza ORDER BY codigo"
            )).scalars().all() == [None, "PZ-000009", "PZ-000042", "PZ-LEGACY"]

            migration.downgrade()
            assert "correlativo_catalogo" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()

