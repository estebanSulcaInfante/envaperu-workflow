import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError


MIGRATION_MODULE = (
    "migrations.versions.f92c7d9e1f86_add_inline_wip_reservations"
)


def _previous_schema(connection):
    statements = (
        "CREATE TABLE trabajador (id INTEGER PRIMARY KEY)",
        "CREATE TABLE rol_operativo (id INTEGER PRIMARY KEY, codigo VARCHAR(50) UNIQUE)",
        "CREATE TABLE scm_capacidad (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo VARCHAR(96) UNIQUE, nombre VARCHAR(200), descripcion TEXT, activo BOOLEAN DEFAULT 1)",
        "CREATE TABLE scm_rol_capacidad (rol_operativo_id INTEGER, capacidad_id INTEGER, PRIMARY KEY (rol_operativo_id, capacidad_id))",
        "CREATE TABLE scm_trabajo_ot (id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE scm_orden_operacion_salida (id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE scm_articulo (id INTEGER PRIMARY KEY)",
        "CREATE TABLE scm_manga (id INTEGER PRIMARY KEY)",
        "CREATE TABLE scm_asignacion_plan_manga_ot (id INTEGER PRIMARY KEY)",
        "CREATE TABLE scm_operacion (operation_id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE scm_confirmacion_manga_armado (id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE scm_asignacion_abastecimiento (id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE scm_asignacion_pool_armado (id CHAR(32) PRIMARY KEY)",
        """CREATE TABLE scm_consumo_componente_armado (
            id CHAR(32) PRIMARY KEY,
            confirmacion_id CHAR(32) NOT NULL,
            asignacion_pool_id CHAR(32),
            asignacion_abastecimiento_id CHAR(32),
            articulo_componente_id INTEGER NOT NULL,
            cantidad_incorporada NUMERIC(15, 3) NOT NULL,
            cantidad_merma NUMERIC(15, 3) NOT NULL DEFAULT 0,
            nivel_genealogia VARCHAR(28) NOT NULL DEFAULT 'EXACTA',
            CONSTRAINT ck_scm_consumo_armado_cantidad
                CHECK (cantidad_incorporada > 0 AND cantidad_merma >= 0),
            CONSTRAINT ck_scm_consumo_armado_genealogia
                CHECK (nivel_genealogia IN (
                    'EXACTA', 'CONJUNTO_CANDIDATOS', 'LEGACY_SIN_ORIGEN'
                )),
            CONSTRAINT ck_scm_consumo_armado_fuente_unica
                CHECK (
                    (asignacion_abastecimiento_id IS NOT NULL) <>
                    (asignacion_pool_id IS NOT NULL)
                ),
            CONSTRAINT uq_scm_consumo_armado_asignacion
                UNIQUE (confirmacion_id, asignacion_abastecimiento_id),
            CONSTRAINT uq_scm_consumo_armado_pool
                UNIQUE (confirmacion_id, asignacion_pool_id)
        )""",
    )
    for statement in statements:
        connection.execute(text(statement))
    connection.execute(text("""
        INSERT INTO rol_operativo (id, codigo) VALUES
          (1, 'JEFE_PRODUCCION'), (2, 'GERENTE_GENERAL'),
          (3, 'SUPERVISOR')
    """))
    connection.execute(text("""
        INSERT INTO scm_capacidad (codigo, nombre, descripcion, activo)
        VALUES ('OF_EXCEPCIONAL_CREAR', 'Crear OF excepcional', NULL, 1)
    """))
    connection.execute(text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capability.id
        FROM rol_operativo AS rol
        CROSS JOIN scm_capacidad AS capability
        WHERE rol.codigo IN ('JEFE_PRODUCCION', 'GERENTE_GENERAL')
          AND capability.codigo = 'OF_EXCEPCIONAL_CREAR'
    """))
    connection.execute(text("INSERT INTO trabajador (id) VALUES (10)"))
    connection.execute(text("INSERT INTO scm_trabajo_ot (id) VALUES ('work-1')"))
    connection.execute(text("INSERT INTO scm_trabajo_ot (id) VALUES ('work-2')"))
    connection.execute(text(
        "INSERT INTO scm_orden_operacion_salida (id) VALUES ('output-1')"
    ))
    connection.execute(text(
        "INSERT INTO scm_orden_operacion_salida (id) VALUES ('output-2')"
    ))
    connection.execute(text("INSERT INTO scm_articulo (id) VALUES (20)"))
    connection.execute(text("INSERT INTO scm_manga (id) VALUES (30)"))
    connection.execute(text(
        "INSERT INTO scm_asignacion_plan_manga_ot (id) VALUES (40)"
    ))
    connection.execute(text(
        "INSERT INTO scm_operacion (operation_id) VALUES ('operation-1')"
    ))
    connection.execute(text(
        "INSERT INTO scm_confirmacion_manga_armado (id) VALUES ('confirm-1')"
    ))


def _migration(connection, monkeypatch):
    migration = importlib.import_module(MIGRATION_MODULE)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def test_inline_wip_migration_constraints_catalog_and_safe_downgrade(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        _previous_schema(connection)
        migration = _migration(connection, monkeypatch)
        migration.upgrade()

        inspector = inspect(connection)
        assert {
            "scm_saldo_wip_salida",
            "scm_reserva_wip_salida",
            "scm_movimiento_wip_salida",
        } <= set(inspector.get_table_names())
        assert {"reserva_wip_salida_id", "procedencia"} <= {
            item["name"]
            for item in inspector.get_columns(
                "scm_consumo_componente_armado"
            )
        }
        capability_count = connection.execute(text("""
            SELECT count(*)
            FROM scm_capacidad
            WHERE codigo = 'OA_EXCEPCIONAL_CREAR'
        """)).scalar_one()
        grants = set(connection.execute(text("""
            SELECT rol.codigo
            FROM scm_rol_capacidad relacion
            JOIN scm_capacidad capacidad
              ON capacidad.id = relacion.capacidad_id
            JOIN rol_operativo rol
              ON rol.id = relacion.rol_operativo_id
            WHERE capacidad.codigo = 'OA_EXCEPCIONAL_CREAR'
        """)).scalars())
        assert capability_count == 1
        assert grants == {"JEFE_PRODUCCION", "GERENTE_GENERAL"}
        close_grants = set(connection.execute(text("""
            SELECT rol.codigo
            FROM scm_rol_capacidad relacion
            JOIN scm_capacidad capacidad
              ON capacidad.id = relacion.capacidad_id
            JOIN rol_operativo rol
              ON rol.id = relacion.rol_operativo_id
            WHERE capacidad.codigo = 'OF_CERRAR'
        """)).scalars())
        assert close_grants == {"JEFE_PRODUCCION", "GERENTE_GENERAL"}

        connection.execute(text("""
            INSERT INTO scm_saldo_wip_salida (
                id, trabajo_color_id, orden_operacion_salida_id,
                articulo_id, cantidad_acreditada, cantidad_consumida
            ) VALUES (
                'saldo-1', 'work-1', 'output-1', 20, 10, 10
            )
        """))
        connection.execute(text("""
            INSERT INTO scm_reserva_wip_salida (
                id, saldo_id, manga_id, asignacion_plan_id,
                articulo_componente_id,
                cantidad_reservada, cantidad_aplicada, estado,
                creada_por_id, operation_id
            ) VALUES (
                'reserve-1', 'saldo-1', 30, 40, 20, 10, 10,
                'APLICADA', 10, 'operation-1'
            )
        """))
        connection.execute(text("""
            INSERT INTO scm_saldo_wip_salida (
                id, trabajo_color_id, orden_operacion_salida_id,
                articulo_id, cantidad_acreditada, cantidad_consumida
            ) VALUES (
                'saldo-2', 'work-2', 'output-2', 20, 0, 0
            )
        """))
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""
                    INSERT INTO scm_consumo_componente_armado (
                        id, confirmacion_id, reserva_wip_salida_id,
                        articulo_componente_id, cantidad_incorporada,
                        cantidad_merma, nivel_genealogia, procedencia
                    ) VALUES (
                        'consume-invalid', 'confirm-1', 'reserve-1', 20,
                        10, 0, 'EXACTA', 'CONSUMIDO_STOCK_PREVIO'
                    )
                """))
        connection.execute(text("""
            INSERT INTO scm_consumo_componente_armado (
                id, confirmacion_id, reserva_wip_salida_id,
                articulo_componente_id, cantidad_incorporada,
                cantidad_merma, nivel_genealogia, procedencia
            ) VALUES (
                'consume-1', 'confirm-1', 'reserve-1', 20,
                10, 0, 'EXACTA', 'PRODUCIDO_OT_ACTUAL'
            )
        """))
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""
                    INSERT INTO scm_movimiento_wip_salida (
                        id, saldo_id, reserva_id, confirmacion_id, tipo,
                        cantidad, effect_key, actor_id, operation_id
                    ) VALUES (
                        'move-invalid', 'saldo-2', 'reserve-1',
                        'confirm-1', 'SALIDA_BUENA_CONFIRMADA', 1,
                        'effect-invalid', 10, 'operation-1'
                    )
                """))
        connection.execute(text("""
            INSERT INTO scm_movimiento_wip_salida (
                id, saldo_id, reserva_id, confirmacion_id, tipo,
                cantidad, effect_key, actor_id, operation_id
            ) VALUES (
                'move-1', 'saldo-1', 'reserve-1', 'confirm-1',
                'SALIDA_BUENA_CONFIRMADA', 10, 'effect-1', 10,
                'operation-1'
            )
        """))

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""
                    UPDATE scm_saldo_wip_salida
                    SET cantidad_consumida = 11 WHERE id = 'saldo-1'
                """))
        with pytest.raises(RuntimeError, match="hechos de producción"):
            migration.downgrade()

        connection.execute(text(
            "DELETE FROM scm_movimiento_wip_salida"
        ))
        connection.execute(text(
            "DELETE FROM scm_consumo_componente_armado"
        ))
        connection.execute(text("DELETE FROM scm_reserva_wip_salida"))
        connection.execute(text("DELETE FROM scm_saldo_wip_salida"))
        migration.downgrade()
        inspector = inspect(connection)
        assert "scm_saldo_wip_salida" not in inspector.get_table_names()
        assert "reserva_wip_salida_id" not in {
            item["name"]
            for item in inspector.get_columns(
                "scm_consumo_componente_armado"
            )
        }
        assert connection.execute(text("""
            SELECT count(*) FROM scm_capacidad
            WHERE codigo = 'OA_EXCEPCIONAL_CREAR'
        """)).scalar_one() == 0
        assert connection.execute(text("""
            SELECT count(*) FROM scm_capacidad
            WHERE codigo = 'OF_CERRAR'
        """)).scalar_one() == 0


def test_inline_wip_downgrade_preserves_preexisting_capability(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        _previous_schema(connection)
        connection.execute(text("""
            INSERT INTO scm_capacidad (
                codigo, nombre, descripcion, activo
            ) VALUES (
                'OA_EXCEPCIONAL_CREAR', 'Capacidad preexistente',
                'Configurada antes de F3', 1
            )
        """))
        migration = _migration(connection, monkeypatch)
        migration.upgrade()
        migration.downgrade()

        capability = connection.execute(text("""
            SELECT nombre, descripcion
            FROM scm_capacidad
            WHERE codigo = 'OA_EXCEPCIONAL_CREAR'
        """)).one()
        assert capability == (
            "Capacidad preexistente",
            "Configurada antes de F3",
        )


def test_inline_wip_postgres_tables_are_rls_protected(monkeypatch):
    migration = importlib.import_module(MIGRATION_MODULE)
    executed = []

    class _SchemaResult:
        @staticmethod
        def scalar_one():
            return "uat_f3"

    class _Connection:
        dialect = postgresql.dialect()

        @staticmethod
        def execute(_statement):
            return _SchemaResult()

    class _Operation:
        @staticmethod
        def execute(statement):
            executed.append(str(statement))

    monkeypatch.setattr(migration, "op", _Operation())
    migration._protect_tables_on_postgres(_Connection())

    sql = "\n".join(executed)
    for table_name in migration.NEW_TABLES:
        assert (
            f'ALTER TABLE uat_f3.{table_name} ENABLE ROW LEVEL SECURITY'
            in sql
        )
        assert (
            f'ALTER TABLE uat_f3.{table_name} FORCE ROW LEVEL SECURITY'
            in sql
        )
        assert f"REVOKE ALL PRIVILEGES ON TABLE uat_f3.{table_name}" in sql
