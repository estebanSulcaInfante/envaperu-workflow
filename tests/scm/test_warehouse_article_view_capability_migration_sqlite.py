import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


MIGRATION_MODULE = (
    "migrations.versions.f86a1c3e9b20_grant_article_view_to_warehouse"
)


def _previous_schema(connection):
    connection.execute(text("""
        CREATE TABLE rol_operativo (
            id INTEGER PRIMARY KEY,
            codigo VARCHAR(50) NOT NULL UNIQUE
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_capacidad (
            id INTEGER PRIMARY KEY,
            codigo VARCHAR(80) NOT NULL UNIQUE
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
        INSERT INTO rol_operativo (id, codigo)
        VALUES (1, 'ALMACEN_RECEPCION'), (2, 'GERENTE_GENERAL')
    """))
    connection.execute(text("""
        INSERT INTO scm_capacidad (id, codigo)
        VALUES (1, 'ARTICULO_VER')
    """))


def _warehouse_has_article_view(connection):
    return bool(connection.execute(text("""
        SELECT 1
        FROM scm_rol_capacidad relacion
        JOIN rol_operativo rol ON rol.id = relacion.rol_operativo_id
        JOIN scm_capacidad capacidad ON capacidad.id = relacion.capacidad_id
        WHERE rol.codigo = 'ALMACEN_RECEPCION'
          AND capacidad.codigo = 'ARTICULO_VER'
    """)).scalar())


def test_migration_grants_article_lookup_to_warehouse_role_and_is_reentrant(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _previous_schema(connection)
        migration = importlib.import_module(MIGRATION_MODULE)
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()
        assert _warehouse_has_article_view(connection)

        migration.upgrade()
        assert connection.execute(text(
            "SELECT COUNT(*) FROM scm_rol_capacidad"
        )).scalar() == 1

        migration.downgrade()
        assert not _warehouse_has_article_view(connection)
        assert connection.execute(text(
            "SELECT codigo FROM scm_capacidad"
        )).scalars().all() == ["ARTICULO_VER"]
