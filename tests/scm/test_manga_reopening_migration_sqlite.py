import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


MIGRATION_MODULE = (
    "migrations.versions.f92c3e5a7b94_add_audited_manga_reopening"
)


def _previous_schema(connection):
    statements = (
        "CREATE TABLE trabajador (id INTEGER PRIMARY KEY)",
        "CREATE TABLE scm_manga (id INTEGER PRIMARY KEY)",
        "CREATE TABLE scm_operacion (operation_id CHAR(32) PRIMARY KEY)",
        """CREATE TABLE scm_pesaje_manga (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id CHAR(32) NOT NULL UNIQUE,
            manga_id INTEGER NOT NULL,
            operation_id CHAR(32) NOT NULL UNIQUE,
            CONSTRAINT uq_scm_pesaje_manga_manga UNIQUE (manga_id)
        )""",
        """CREATE TABLE scm_anulacion_pesaje_manga (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pesaje_id INTEGER NOT NULL UNIQUE
        )""",
        """CREATE TABLE rol_operativo (
            id INTEGER PRIMARY KEY,
            codigo VARCHAR(50) NOT NULL UNIQUE
        )""",
        """CREATE TABLE scm_capacidad (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo VARCHAR(96) NOT NULL UNIQUE,
            nombre VARCHAR(200) NOT NULL,
            activo BOOLEAN NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE scm_rol_capacidad (
            rol_operativo_id INTEGER NOT NULL,
            capacidad_id INTEGER NOT NULL,
            PRIMARY KEY (rol_operativo_id, capacidad_id)
        )""",
    )
    for statement in statements:
        connection.execute(text(statement))
    connection.execute(text("INSERT INTO trabajador (id) VALUES (10)"))
    connection.execute(text("INSERT INTO scm_manga (id) VALUES (100)"))
    connection.execute(text("""
        INSERT INTO rol_operativo (id, codigo) VALUES
          (1, 'GERENTE_GENERAL'), (2, 'JEFE_PRODUCCION'),
          (3, 'SUPERVISOR'), (4, 'MAQUINISTA')
    """))
    connection.execute(text("""
        INSERT INTO scm_operacion (operation_id) VALUES
          ('operation-old'), ('operation-new'), ('operation-reopen')
    """))
    connection.execute(text("""
        INSERT INTO scm_pesaje_manga (
            public_id, manga_id, operation_id
        ) VALUES ('weighing-old', 100, 'operation-old')
    """))


def test_k6_migration_permite_un_solo_cierre_vigente_y_otorga_autoridad(monkeypatch):
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
        inspector = inspect(connection)
        assert "estado" in {
            column["name"] for column in inspector.get_columns("scm_pesaje_manga")
        }
        assert "scm_reapertura_manga" in inspector.get_table_names()
        assert "uq_scm_pesaje_manga_vigente" in {
            index["name"] for index in inspector.get_indexes("scm_pesaje_manga")
        }
        assert connection.execute(text("""
            SELECT estado FROM scm_pesaje_manga WHERE id = 1
        """)).scalar_one() == "VIGENTE"

        granted = set(connection.execute(text("""
            SELECT rol.codigo
            FROM scm_rol_capacidad relacion
            JOIN scm_capacidad capacidad ON capacidad.id = relacion.capacidad_id
            JOIN rol_operativo rol ON rol.id = relacion.rol_operativo_id
            WHERE capacidad.codigo = 'MANGA_REABRIR'
        """)).scalars())
        assert granted == {"GERENTE_GENERAL", "JEFE_PRODUCCION"}

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""
                    INSERT INTO scm_pesaje_manga (
                        public_id, manga_id, operation_id, estado
                    ) VALUES (
                        'weighing-conflict', 100, 'operation-new', 'VIGENTE'
                    )
                """))

        connection.execute(text("""
            UPDATE scm_pesaje_manga SET estado = 'REABIERTO' WHERE id = 1
        """))
        connection.execute(text("""
            INSERT INTO scm_reapertura_manga (
                public_id, manga_id, pesaje_id, motivo,
                reabierta_por_id, operation_id
            ) VALUES (
                'reopening-1', 100, 1, 'Cierre accidental',
                10, 'operation-reopen'
            )
        """))
        connection.execute(text("""
            INSERT INTO scm_pesaje_manga (
                public_id, manga_id, operation_id, estado
            ) VALUES ('weighing-new', 100, 'operation-new', 'VIGENTE')
        """))
        assert connection.execute(text("""
            SELECT COUNT(*) FROM scm_pesaje_manga
            WHERE manga_id = 100 AND estado = 'VIGENTE'
        """)).scalar_one() == 1

        with pytest.raises(RuntimeError, match="después de reabrir"):
            migration.downgrade()
