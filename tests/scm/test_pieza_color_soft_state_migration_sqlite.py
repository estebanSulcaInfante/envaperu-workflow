import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION_MODULE = (
    "migrations.versions.f85e4b2d7a10_add_pieza_color_soft_state"
)


def _migration(connection, monkeypatch):
    migration = importlib.import_module(MIGRATION_MODULE)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def test_f85_agrega_estado_a_filas_existentes_y_revierte(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE pieza_color (
                sku VARCHAR(50) PRIMARY KEY,
                piezas VARCHAR(200)
            )
        """))
        connection.execute(text("""
            INSERT INTO pieza_color (sku, piezas)
            VALUES ('PC-HISTORICA', 'Pieza histórica')
        """))

        migration = _migration(connection, monkeypatch)
        migration.upgrade()

        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("pieza_color")
        }
        assert columns["activo"]["nullable"] is False
        assert columns["version"]["nullable"] is False
        row = connection.execute(text("""
            SELECT activo, version
            FROM pieza_color
            WHERE sku = 'PC-HISTORICA'
        """)).mappings().one()
        assert bool(row["activo"]) is True
        assert row["version"] == 1
        assert "ix_pieza_color_activo" in {
            index["name"]
            for index in inspect(connection).get_indexes("pieza_color")
        }

        migration.downgrade()
        names = {
            column["name"]
            for column in inspect(connection).get_columns("pieza_color")
        }
        assert "activo" not in names
        assert "version" not in names
        assert connection.execute(text(
            "SELECT piezas FROM pieza_color WHERE sku = 'PC-HISTORICA'"
        )).scalar_one() == "Pieza histórica"
