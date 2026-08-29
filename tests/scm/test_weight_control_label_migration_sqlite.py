import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


K1_MODULE = "migrations.versions.f88c3e5a7b42_add_open_manga_shift_continuity"
K2_MODULE = (
    "migrations.versions."
    "f91b6c8d0e75_add_control_weight_labels_and_same_ot_relief"
)


def _base_schema(connection):
    statements = (
        "CREATE TABLE trabajador (id INTEGER PRIMARY KEY)",
        "CREATE TABLE rol_operativo (id INTEGER PRIMARY KEY, codigo VARCHAR(50) UNIQUE)",
        "CREATE TABLE scm_capacidad (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo VARCHAR(96) UNIQUE, nombre VARCHAR(200), descripcion TEXT, activo BOOLEAN DEFAULT 1)",
        "CREATE TABLE scm_rol_capacidad (rol_operativo_id INTEGER, capacidad_id INTEGER, PRIMARY KEY (rol_operativo_id, capacidad_id))",
        """CREATE TABLE scm_manga (
            id INTEGER PRIMARY KEY,
            estado VARCHAR(32) NOT NULL DEFAULT 'PLANIFICADA',
            CONSTRAINT ck_scm_manga_estado CHECK (estado IN (
                'PLANIFICADA', 'PREETIQUETADA', 'EN_ARMADO',
                'CERRADA_ARMADO_PENDIENTE_PESAJE', 'PESADA',
                'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN',
                'RECIBIDA', 'ANULADA'
            ))
        )""",
        "CREATE TABLE scm_trabajo_ot (id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE scm_asignacion_personal_trabajo_ot (id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE scm_asignacion_plan_manga_ot (id INTEGER PRIMARY KEY)",
        "CREATE TABLE scm_operacion (operation_id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE estacion_pesaje (station_id VARCHAR(36) PRIMARY KEY)",
        "CREATE TABLE scm_trabajo_impresion_manga (public_id CHAR(32) PRIMARY KEY)",
        """CREATE TABLE scm_etiqueta_manga (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id CHAR(32) NOT NULL UNIQUE,
            manga_id INTEGER NOT NULL,
            trabajo_impresion_id CHAR(32) NOT NULL,
            tipo VARCHAR(20) NOT NULL,
            version INTEGER NOT NULL,
            CONSTRAINT ck_scm_etiqueta_manga_tipo
                CHECK (tipo IN ('PREPESAJE', 'POSTPESAJE')),
            CONSTRAINT uq_scm_etiqueta_manga_version
                UNIQUE (manga_id, tipo, version)
        )""",
    )
    for statement in statements:
        connection.execute(text(statement))
    connection.execute(text("""
        INSERT INTO rol_operativo (id, codigo) VALUES
          (1, 'MAQUINISTA'), (2, 'OPERADOR_PESAJE'),
          (3, 'SUPERVISOR'), (4, 'JEFE_PRODUCCION'),
          (5, 'GERENTE_GENERAL')
    """))
    connection.execute(text("INSERT INTO trabajador (id) VALUES (10)"))
    connection.execute(text(
        "INSERT INTO scm_manga (id, estado) VALUES (100, 'PREETIQUETADA')"
    ))
    connection.execute(text(
        "INSERT INTO scm_trabajo_ot (id) VALUES ('work-1')"
    ))
    connection.execute(text(
        "INSERT INTO scm_asignacion_personal_trabajo_ot (id) VALUES ('assignment-1')"
    ))
    connection.execute(text(
        "INSERT INTO scm_asignacion_plan_manga_ot (id) VALUES (1)"
    ))
    connection.execute(text(
        "INSERT INTO scm_operacion (operation_id) VALUES ('operation-1')"
    ))
    connection.execute(text(
        "INSERT INTO estacion_pesaje (station_id) VALUES ('station-1')"
    ))
    connection.execute(text(
        "INSERT INTO scm_trabajo_impresion_manga (public_id) VALUES ('job-1')"
    ))


def _apply(connection, monkeypatch, module_name, action):
    migration = importlib.import_module(module_name)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    getattr(migration, action)()


def test_k2_migration_adds_control_label_delta_and_same_ot_capability(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _base_schema(connection)
        _apply(connection, monkeypatch, K1_MODULE, "upgrade")
        connection.execute(text("""
            INSERT INTO scm_tramo_manga_trabajo (
                id, manga_id, trabajo_ot_id,
                asignacion_personal_trabajo_id, asignacion_plan_id,
                secuencia, estado, cantidad_inicio_un,
                cantidad_atribuida_un, created_by_id
            ) VALUES (
                'segment-1', 100, 'work-1', 'assignment-1', 1,
                1, 'ACTIVO', 0, 0, 10
            )
        """))
        connection.execute(text("""
            INSERT INTO scm_control_peso_manga (
                public_id, manga_id, tramo_id, operation_id,
                source_system, station_id, capture_id, tipo,
                peso_bruto_kg, tara_kg, peso_neto_kg, tara_fuente,
                conteo_acumulado_un, motivo, pesado_at,
                fecha_local_pesaje, pesado_por_id
            ) VALUES (
                'control-1', 100, 'segment-1', 'operation-1',
                'SCM_STATION', 'station-1', 'capture-1', 'CORTE_TURNO',
                2.100, 0.100, 2.000, 'TIPO_MANGA',
                20, 'CAMBIO_TURNO', '2026-08-29T10:00:00+00:00',
                '2026-08-29', 10
            )
        """))

        _apply(connection, monkeypatch, K2_MODULE, "upgrade")
        inspector = inspect(connection)
        assert {
            "aporte_desde_control_anterior_kg", "etiqueta_id"
        } <= {
            item["name"]
            for item in inspector.get_columns("scm_control_peso_manga")
        }
        assert connection.execute(text("""
            SELECT aporte_desde_control_anterior_kg
            FROM scm_control_peso_manga WHERE public_id = 'control-1'
        """)).scalar_one() == 2
        label_check = " ".join(
            item["sqltext"]
            for item in inspector.get_check_constraints("scm_etiqueta_manga")
        )
        assert "CONTROL_PESO" in label_check
        assert "uq_scm_control_peso_manga_etiqueta" in {
            item["name"]
            for item in inspector.get_unique_constraints(
                "scm_control_peso_manga"
            )
        }

        connection.execute(text("""
            INSERT INTO scm_etiqueta_manga (
                public_id, manga_id, trabajo_impresion_id, tipo, version
            ) VALUES ('control-label-1', 100, 'job-1', 'CONTROL_PESO', 1)
        """))
        label_id = connection.execute(text("""
            SELECT id FROM scm_etiqueta_manga
            WHERE public_id = 'control-label-1'
        """)).scalar_one()
        connection.execute(text("""
            UPDATE scm_control_peso_manga SET etiqueta_id = :label_id
            WHERE public_id = 'control-1'
        """), {"label_id": label_id})

        granted = set(connection.execute(text("""
            SELECT rol.codigo
            FROM scm_rol_capacidad relacion
            JOIN scm_capacidad capacidad ON capacidad.id = relacion.capacidad_id
            JOIN rol_operativo rol ON rol.id = relacion.rol_operativo_id
            WHERE capacidad.codigo = 'MANGA_REASIGNAR_MAQUINISTA'
        """)).scalars())
        assert granted == {"SUPERVISOR", "JEFE_PRODUCCION", "GERENTE_GENERAL"}

        connection.execute(text(
            "UPDATE scm_control_peso_manga SET etiqueta_id = NULL"
        ))
        connection.execute(text(
            "DELETE FROM scm_etiqueta_manga WHERE tipo = 'CONTROL_PESO'"
        ))
        _apply(connection, monkeypatch, K2_MODULE, "downgrade")
        inspector = inspect(connection)
        assert "CONTROL_PESO" not in " ".join(
            item["sqltext"]
            for item in inspector.get_check_constraints("scm_etiqueta_manga")
        )
        assert "aporte_desde_control_anterior_kg" not in {
            item["name"]
            for item in inspector.get_columns("scm_control_peso_manga")
        }
        assert connection.execute(text("""
            SELECT COUNT(*) FROM scm_capacidad
            WHERE codigo = 'MANGA_REASIGNAR_MAQUINISTA'
        """)).scalar_one() == 0


