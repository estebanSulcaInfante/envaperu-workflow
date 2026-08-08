import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


MIGRATION_MODULE = (
    "migrations.versions.f80c9d5e1a42_repair_workspace_catalog_text"
)


def test_workspace_catalog_text_repair_restores_only_corrupted_labels(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE rol_operativo (
                id INTEGER PRIMARY KEY,
                nombre VARCHAR(100) NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE scm_capacidad (
                id INTEGER PRIMARY KEY,
                nombre VARCHAR(150) NOT NULL
            )
        """))
        connection.execute(
            text("INSERT INTO rol_operativo (id, nombre) VALUES (1, :bad), (2, :ok)"),
            {"bad": "AlmacÃ©n / RecepciÃ³n", "ok": "Gerente General"},
        )
        connection.execute(
            text("INSERT INTO scm_capacidad (id, nombre) VALUES (1, :bad), (2, :ok)"),
            {
                "bad": "Aprobar correcciones de recepciÃ³n",
                "ok": "Consultar Kardex normalizado",
            },
        )

        migration = importlib.import_module(MIGRATION_MODULE)
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()

        assert connection.execute(text(
            "SELECT nombre FROM rol_operativo ORDER BY id"
        )).scalars().all() == ["Almacén / Recepción", "Gerente General"]
        assert connection.execute(text(
            "SELECT nombre FROM scm_capacidad ORDER BY id"
        )).scalars().all() == [
            "Aprobar correcciones de recepción",
            "Consultar Kardex normalizado",
        ]

        migration.downgrade()
        assert connection.execute(text(
            "SELECT nombre FROM rol_operativo WHERE id = 1"
        )).scalar_one() == "Almacén / Recepción"

