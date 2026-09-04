import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION_MODULE = (
    "migrations.versions.0b548129a29a_add_inventory_explorer_indexes"
)
EXPECTED_INDEXES = {
    "scm_articulo": "ix_scm_articulo_clase_codigo_id",
    "scm_material": "ix_scm_material_clase_codigo_id",
    "scm_ubicacion_inventario": "ix_scm_ubicacion_almacen_codigo_id",
    "scm_movimiento_inventario": "ix_scm_movimiento_inventario_created_id",
    "scm_movimiento_material_inventario": "ix_scm_movimiento_material_created_id",
}


def _previous_schema(connection):
    connection.execute(text("""
        CREATE TABLE scm_articulo (
            id INTEGER PRIMARY KEY, clase VARCHAR(32), codigo VARCHAR(64)
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_material (
            id INTEGER PRIMARY KEY, clase VARCHAR(30), codigo VARCHAR(64)
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_ubicacion_inventario (
            id INTEGER PRIMARY KEY, almacen_id VARCHAR(36), codigo VARCHAR(40)
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_movimiento_inventario (
            id VARCHAR(36) PRIMARY KEY, created_at DATETIME
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_movimiento_material_inventario (
            id VARCHAR(36) PRIMARY KEY, created_at DATETIME
        )
    """))


def test_inventory_explorer_indexes_upgrade_and_downgrade(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _previous_schema(connection)
        migration = importlib.import_module(MIGRATION_MODULE)
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()
        for table, expected in EXPECTED_INDEXES.items():
            names = {item["name"] for item in inspect(connection).get_indexes(table)}
            assert expected in names

        migration.downgrade()
        for table, expected in EXPECTED_INDEXES.items():
            names = {item["name"] for item in inspect(connection).get_indexes(table)}
            assert expected not in names
