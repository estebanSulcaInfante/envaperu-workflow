import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


MIGRATION_MODULE = (
    "migrations.versions.f85e4b2d7a10_seed_fabrication_close_capability"
)


def test_migration_registers_fabrication_close_for_authorized_roles(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE rol_operativo (
                id INTEGER PRIMARY KEY,
                codigo VARCHAR(50) NOT NULL UNIQUE
            )
        """))
        connection.execute(text("""
            CREATE TABLE scm_capacidad (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo VARCHAR(80) NOT NULL UNIQUE,
                nombre VARCHAR(160) NOT NULL,
                activo BOOLEAN NOT NULL DEFAULT 1
            )
        """))
        connection.execute(text("""
            CREATE TABLE scm_rol_capacidad (
                rol_operativo_id INTEGER NOT NULL,
                capacidad_id INTEGER NOT NULL,
                PRIMARY KEY (rol_operativo_id, capacidad_id)
            )
        """))
        connection.execute(text("""
            INSERT INTO rol_operativo (id, codigo) VALUES
            (1, 'GERENTE_GENERAL'),
            (2, 'JEFE_PRODUCCION'),
            (3, 'SUPERVISOR')
        """))
        migration = importlib.import_module(MIGRATION_MODULE)
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()
        migration.upgrade()
        assignments = set(connection.execute(text("""
            SELECT rol.codigo, capacidad.codigo
            FROM scm_rol_capacidad relacion
            JOIN rol_operativo rol ON rol.id = relacion.rol_operativo_id
            JOIN scm_capacidad capacidad ON capacidad.id = relacion.capacidad_id
        """)).all())
        assert assignments == {
            ("GERENTE_GENERAL", "OF_CERRAR"),
            ("JEFE_PRODUCCION", "OF_CERRAR"),
        }

        migration.downgrade()
        assert connection.execute(text(
            "SELECT codigo FROM scm_capacidad"
        )).scalars().all() == []
