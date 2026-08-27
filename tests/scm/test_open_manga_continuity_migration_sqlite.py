import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


MIGRATION_MODULE = (
    "migrations.versions.f88c3e5a7b42_add_open_manga_shift_continuity"
)
NEW_TABLES = {
    "scm_tramo_manga_trabajo",
    "scm_control_peso_manga",
}
EXPECTED_GRANTS = {
    "MANGA_CONTROL_PESO_REGISTRAR": {
        "MAQUINISTA",
        "OPERADOR_PESAJE",
        "SUPERVISOR",
        "JEFE_PRODUCCION",
        "GERENTE_GENERAL",
    },
    "MANGA_TRANSFERIR_OT": {
        "SUPERVISOR",
        "JEFE_PRODUCCION",
        "GERENTE_GENERAL",
    },
}


def _previous_schema(connection):
    connection.execute(text("""
        CREATE TABLE trabajador (
            id INTEGER PRIMARY KEY
        )
    """))
    connection.execute(text("""
        CREATE TABLE rol_operativo (
            id INTEGER PRIMARY KEY,
            codigo VARCHAR(50) NOT NULL UNIQUE
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_capacidad (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo VARCHAR(96) NOT NULL UNIQUE,
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
        CREATE TABLE scm_manga (
            id INTEGER PRIMARY KEY,
            estado VARCHAR(32) NOT NULL DEFAULT 'PLANIFICADA',
            CONSTRAINT ck_scm_manga_estado CHECK (
                estado IN (
                    'PLANIFICADA', 'PREETIQUETADA', 'EN_ARMADO',
                    'CERRADA_ARMADO_PENDIENTE_PESAJE', 'PESADA',
                    'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN',
                    'RECIBIDA', 'ANULADA'
                )
            )
        )
    """))
    connection.execute(text(
        "CREATE TABLE scm_trabajo_ot (id CHAR(32) PRIMARY KEY)"
    ))
    connection.execute(text("""
        CREATE TABLE scm_asignacion_personal_trabajo_ot (
            id CHAR(32) PRIMARY KEY
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_asignacion_plan_manga_ot (
            id INTEGER PRIMARY KEY
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_operacion (
            operation_id CHAR(32) PRIMARY KEY
        )
    """))
    connection.execute(text("""
        CREATE TABLE estacion_pesaje (
            station_id VARCHAR(36) PRIMARY KEY
        )
    """))
    connection.execute(text("""
        INSERT INTO rol_operativo (id, codigo) VALUES
          (1, 'MAQUINISTA'),
          (2, 'OPERADOR_PESAJE'),
          (3, 'SUPERVISOR'),
          (4, 'JEFE_PRODUCCION'),
          (5, 'GERENTE_GENERAL'),
          (6, 'ALMACEN_RECEPCION')
    """))
    connection.execute(text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        VALUES ('MANGA_PESAR', 'Pesaje final existente', 1)
    """))
    connection.execute(text("INSERT INTO trabajador (id) VALUES (10)"))
    connection.execute(text("""
        INSERT INTO scm_manga (id, estado)
        VALUES (100, 'PREETIQUETADA'), (101, 'PLANIFICADA')
    """))
    connection.execute(text(
        "INSERT INTO scm_trabajo_ot (id) VALUES ('work-1'), ('work-2')"
    ))
    connection.execute(text("""
        INSERT INTO scm_asignacion_personal_trabajo_ot (id)
        VALUES ('person-assignment-1'), ('person-assignment-2')
    """))
    connection.execute(text("""
        INSERT INTO scm_asignacion_plan_manga_ot (id) VALUES (1), (2)
    """))
    connection.execute(text("""
        INSERT INTO scm_operacion (operation_id)
        VALUES ('operation-1'), ('operation-2')
    """))
    connection.execute(text("""
        INSERT INTO estacion_pesaje (station_id) VALUES ('station-1')
    """))


def _check_sql(inspector, table_name):
    return " ".join(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints(table_name)
    ).upper()


def _grants(connection):
    rows = connection.execute(text("""
        SELECT capacidad.codigo, rol.codigo
        FROM scm_rol_capacidad relacion
        JOIN scm_capacidad capacidad
          ON capacidad.id = relacion.capacidad_id
        JOIN rol_operativo rol
          ON rol.id = relacion.rol_operativo_id
        WHERE capacidad.codigo IN (
            'MANGA_CONTROL_PESO_REGISTRAR',
            'MANGA_TRANSFERIR_OT'
        )
    """)).all()
    return {
        capability: {role for code, role in rows if code == capability}
        for capability in EXPECTED_GRANTS
    }


def _expect_integrity_error(connection, statement):
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(text(statement))


def test_open_manga_continuity_migration_upgrade_downgrade_and_seed(monkeypatch):
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
        assert NEW_TABLES <= set(inspector.get_table_names())
        assert {
            "ix_scm_tramo_manga_trabajo",
            "ix_scm_tramo_manga_asignacion",
            "uq_scm_tramo_manga_abierto",
        } == {
            item["name"]
            for item in inspector.get_indexes("scm_tramo_manga_trabajo")
        }
        open_index = next(
            item
            for item in inspector.get_indexes("scm_tramo_manga_trabajo")
            if item["name"] == "uq_scm_tramo_manga_abierto"
        )
        assert open_index["unique"] == 1
        assert "ESTADO IN ('PROGRAMADO', 'ACTIVO')" in str(
            open_index["dialect_options"]["sqlite_where"]
        ).upper()
        assert {
            item["name"]
            for item in inspector.get_indexes("scm_control_peso_manga")
        } == {"ix_scm_control_peso_manga_manga"}
        assert {
            item["name"]
            for item in inspector.get_unique_constraints(
                "scm_control_peso_manga"
            )
        } == {
            "uq_scm_control_peso_manga_public",
            "uq_scm_control_peso_manga_tramo",
            "uq_scm_control_peso_manga_operation",
            "uq_scm_control_peso_manga_capture",
        }

        manga_checks = _check_sql(inspector, "scm_manga")
        assert "CONTINUIDAD_PENDIENTE" in manga_checks
        assert "EN_LLENADO" in manga_checks
        tramo_checks = _check_sql(inspector, "scm_tramo_manga_trabajo")
        assert all(
            state in tramo_checks
            for state in ("PROGRAMADO", "ACTIVO", "CERRADO", "ANULADO")
        )
        control_checks = _check_sql(inspector, "scm_control_peso_manga")
        assert "CORTE_TURNO" in control_checks

        connection.execute(text("""
            UPDATE scm_manga SET estado = 'CONTINUIDAD_PENDIENTE'
            WHERE id = 100
        """))
        connection.execute(text("""
            UPDATE scm_manga SET estado = 'EN_LLENADO'
            WHERE id = 101
        """))
        _expect_integrity_error(
            connection,
            "INSERT INTO scm_manga (id, estado) VALUES (102, 'DESCONOCIDO')",
        )

        connection.execute(text("""
            INSERT INTO scm_tramo_manga_trabajo (
                id, manga_id, trabajo_ot_id,
                asignacion_personal_trabajo_id, asignacion_plan_id,
                secuencia, estado, cantidad_inicio_un, cantidad_fin_un,
                cantidad_atribuida_un, created_by_id
            ) VALUES (
                'segment-1', 100, 'work-1',
                'person-assignment-1', 1,
                1, 'CERRADO', 0, 20, 20, 10
            ), (
                'segment-2', 100, 'work-2',
                'person-assignment-2', 2,
                2, 'ACTIVO', 20, NULL, 0, 10
            )
        """))
        _expect_integrity_error(connection, """
            INSERT INTO scm_tramo_manga_trabajo (
                id, manga_id, trabajo_ot_id,
                asignacion_personal_trabajo_id,
                secuencia, estado, cantidad_inicio_un,
                cantidad_atribuida_un, created_by_id
            ) VALUES (
                'segment-3', 100, 'work-2',
                'person-assignment-2',
                3, 'PROGRAMADO', 20, 0, 10
            )
        """)
        _expect_integrity_error(connection, """
            INSERT INTO scm_tramo_manga_trabajo (
                id, manga_id, trabajo_ot_id,
                asignacion_personal_trabajo_id,
                secuencia, estado, cantidad_inicio_un,
                cantidad_atribuida_un, created_by_id
            ) VALUES (
                'segment-invalid', 101, 'work-1',
                'person-assignment-1',
                1, 'DESCONOCIDO', 0, 0, 10
            )
        """)
        connection.execute(text("""
            INSERT INTO scm_control_peso_manga (
                public_id, manga_id, tramo_id, operation_id,
                source_system, station_id, capture_id, tipo,
                peso_bruto_kg, tara_kg, peso_neto_kg, tara_fuente,
                conteo_acumulado_un, motivo, pesado_at,
                fecha_local_pesaje, pesado_por_id
            ) VALUES (
                'control-1', 100, 'segment-2', 'operation-1',
                'SCM_STATION', 'station-1', 'capture-1', 'CORTE_TURNO',
                1.100, 0.100, 1.000, 'TIPO_MANGA',
                30, 'CAMBIO_TURNO', '2026-08-26T20:00:00+00:00',
                '2026-08-26', 10
            )
        """))
        _expect_integrity_error(connection, """
            INSERT INTO scm_control_peso_manga (
                public_id, manga_id, tramo_id, operation_id,
                source_system, station_id, capture_id, tipo,
                peso_bruto_kg, tara_kg, peso_neto_kg, tara_fuente,
                conteo_acumulado_un, motivo, pesado_at,
                fecha_local_pesaje, pesado_por_id
            ) VALUES (
                'control-invalid', 101, 'segment-2', 'operation-2',
                'SCM_STATION', 'station-1', 'capture-2', 'OTRO',
                1.100, 0.100, 1.000, 'TIPO_MANGA',
                30, 'CAMBIO_TURNO', '2026-08-26T20:00:00+00:00',
                '2026-08-26', 10
            )
        """)

        assert _grants(connection) == EXPECTED_GRANTS
        assert connection.execute(text("""
            SELECT COUNT(*) FROM scm_capacidad
            WHERE codigo IN (
                'MANGA_PESAR',
                'MANGA_CONTROL_PESO_REGISTRAR',
                'MANGA_TRANSFERIR_OT'
            )
        """)).scalar_one() == 3

        connection.execute(text("""
            UPDATE scm_manga SET estado = 'PREETIQUETADA'
            WHERE id IN (100, 101)
        """))
        migration.downgrade()
        inspector = inspect(connection)
        assert not (NEW_TABLES & set(inspector.get_table_names()))
        assert connection.execute(text(
            "SELECT codigo FROM scm_capacidad ORDER BY codigo"
        )).scalars().all() == ["MANGA_PESAR"]
        assert connection.execute(text(
            "SELECT COUNT(*) FROM scm_rol_capacidad"
        )).scalar_one() == 0
        downgraded_checks = _check_sql(inspector, "scm_manga")
        assert "CONTINUIDAD_PENDIENTE" not in downgraded_checks
        assert "EN_LLENADO" not in downgraded_checks
        _expect_integrity_error(connection, """
            INSERT INTO scm_manga (id, estado)
            VALUES (103, 'EN_LLENADO')
        """)

