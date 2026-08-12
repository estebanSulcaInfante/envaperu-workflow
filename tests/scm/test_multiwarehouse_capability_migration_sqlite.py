import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


MIGRATION_MODULE = "migrations.versions.f84d3a7c9e21_seed_multiwarehouse_capabilities"


def _previous_schema(connection):
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
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        VALUES ('INVENTARIO_VER', 'Consultar Kardex', 1)
    """))
    connection.execute(text("""
        INSERT INTO rol_operativo (id, codigo) VALUES
        (1, 'GERENTE_GENERAL'),
        (2, 'CONFIGURACION_SCM'),
        (3, 'ALMACEN_RECEPCION'),
        (4, 'GERENCIA'),
        (5, 'AUDITORIA_CONSULTA')
    """))


def _assignments(connection):
    return set(connection.execute(text("""
        SELECT rol.codigo, capacidad.codigo
        FROM scm_rol_capacidad relacion
        JOIN rol_operativo rol ON rol.id = relacion.rol_operativo_id
        JOIN scm_capacidad capacidad ON capacidad.id = relacion.capacidad_id
    """)).all())


def test_migration_registers_multiwarehouse_capabilities_and_safe_role_defaults(monkeypatch):
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
        codes = set(connection.execute(text(
            "SELECT codigo FROM scm_capacidad"
        )).scalars())
        assert codes == {
            "INVENTARIO_VER",
            "ALMACEN_CONFIG_ADMINISTRAR",
            "ALMACEN_SCOPE_ADMINISTRAR",
            "INVENTARIO_MOVILIZAR",
            "INVENTARIO_CONTROL_TRANSVERSAL",
        }
        assignments = _assignments(connection)
        assert ("GERENTE_GENERAL", "ALMACEN_CONFIG_ADMINISTRAR") in assignments
        assert ("GERENTE_GENERAL", "ALMACEN_SCOPE_ADMINISTRAR") in assignments
        assert ("GERENTE_GENERAL", "INVENTARIO_MOVILIZAR") in assignments
        assert ("GERENTE_GENERAL", "INVENTARIO_CONTROL_TRANSVERSAL") in assignments
        assert ("CONFIGURACION_SCM", "ALMACEN_CONFIG_ADMINISTRAR") in assignments
        assert ("CONFIGURACION_SCM", "ALMACEN_SCOPE_ADMINISTRAR") in assignments
        assert ("CONFIGURACION_SCM", "INVENTARIO_VER") in assignments
        assert ("ALMACEN_RECEPCION", "INVENTARIO_MOVILIZAR") in assignments
        assert ("GERENCIA", "INVENTARIO_CONTROL_TRANSVERSAL") in assignments
        assert ("AUDITORIA_CONSULTA", "INVENTARIO_CONTROL_TRANSVERSAL") in assignments
        assert ("ALMACEN_RECEPCION", "ALMACEN_CONFIG_ADMINISTRAR") not in assignments

        # Reentrant upgrade must remain safe for restored/partially seeded DBs.
        migration.upgrade()
        assert _assignments(connection) == assignments

        migration.downgrade()
        assert connection.execute(text(
            "SELECT codigo FROM scm_capacidad"
        )).scalars().all() == ["INVENTARIO_VER"]
