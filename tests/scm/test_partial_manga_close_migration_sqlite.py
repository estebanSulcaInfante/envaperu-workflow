import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


MIGRATION_MODULE = (
    "migrations.versions.f87b2d4e6a31_add_supervised_partial_manga_close"
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo VARCHAR(80) NOT NULL UNIQUE,
            nombre VARCHAR(200) NOT NULL,
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
        CREATE TABLE scm_pesaje_manga (
            id INTEGER PRIMARY KEY,
            fuente_cantidad VARCHAR(36) NOT NULL,
            CONSTRAINT ck_scm_pesaje_manga_fuente_cantidad CHECK (
                fuente_cantidad IN (
                    'PLAN_CONFIRMADO_POR_PESAJE',
                    'RESPONSABLE_ARMADO',
                    'CORRECCION_AUTORIZADA'
                )
            )
        )
    """))
    connection.execute(text("""
        INSERT INTO rol_operativo (id, codigo)
        VALUES
            (1, 'GERENTE_GENERAL'),
            (2, 'SUPERVISOR'),
            (3, 'JEFE_PRODUCCION'),
            (4, 'MAQUINISTA')
    """))


def test_migration_habilita_cierre_parcial_solo_a_roles_supervisores(monkeypatch):
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
        granted_roles = connection.execute(text("""
            SELECT rol.codigo
            FROM scm_rol_capacidad relacion
            JOIN rol_operativo rol ON rol.id = relacion.rol_operativo_id
            JOIN scm_capacidad capacidad ON capacidad.id = relacion.capacidad_id
            WHERE capacidad.codigo = 'MANGA_FINALIZAR_PARCIAL'
            ORDER BY rol.codigo
        """)).scalars().all()
        assert granted_roles == [
            "GERENTE_GENERAL", "JEFE_PRODUCCION", "SUPERVISOR"
        ]
        connection.execute(text("""
            INSERT INTO scm_pesaje_manga (id, fuente_cantidad)
            VALUES (1, 'CIERRE_PARCIAL_SUPERVISADO')
        """))

        migration.downgrade()
        assert connection.execute(text("""
            SELECT fuente_cantidad FROM scm_pesaje_manga WHERE id = 1
        """)).scalar_one() == "PLAN_CONFIRMADO_POR_PESAJE"
        assert connection.execute(text("""
            SELECT COUNT(*) FROM scm_capacidad
            WHERE codigo = 'MANGA_FINALIZAR_PARCIAL'
        """)).scalar_one() == 0
