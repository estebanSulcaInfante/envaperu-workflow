import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


MIGRATION_MODULE = (
    "migrations.versions.c3a91f6e2d47_add_opm_prepared_material_pilot"
)
NEW_TABLES = {
    "scm_requerimiento_material_preparado",
    "scm_orden_preparacion_material",
    "scm_asignacion_requerimiento_preparacion",
    "scm_lectura_peso_preparacion",
    "scm_aprobacion_lectura_peso_preparacion",
    "scm_aporte_preparacion_material",
    "scm_lote_material_preparado",
    "scm_bolsa_material_preparado",
    "scm_decision_calidad_material_preparado",
    "scm_reserva_material_preparado",
    "scm_emision_material_preparado",
    "scm_recepcion_bolsa_material_preparado",
    "scm_saldo_material_preparado",
    "scm_movimiento_material_preparado",
}


def _previous_schema(connection):
    connection.execute(text(
        "CREATE TABLE scm_corrida_fabricacion (id CHAR(32) PRIMARY KEY)"
    ))
    connection.execute(text(
        "CREATE TABLE receta_color_maestra (id INTEGER PRIMARY KEY)"
    ))
    connection.execute(text(
        "CREATE TABLE trabajador (id INTEGER PRIMARY KEY)"
    ))
    connection.execute(text("""
        CREATE TABLE rol_operativo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo VARCHAR(64) NOT NULL UNIQUE,
            nombre VARCHAR(120) NOT NULL,
            activo BOOLEAN NOT NULL
        )
    """))
    connection.execute(text("""
        CREATE TABLE scm_capacidad (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo VARCHAR(96) NOT NULL UNIQUE,
            nombre VARCHAR(160) NOT NULL,
            activo BOOLEAN NOT NULL
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
        VALUES ('INVENTARIO_VER', 'Consultar Kardex normalizado', true)
    """))
    connection.execute(text("""
        CREATE TABLE trabajador_rol (
            trabajador_id INTEGER NOT NULL,
            rol_operativo_id INTEGER NOT NULL,
            PRIMARY KEY (trabajador_id, rol_operativo_id)
        )
    """))
    connection.execute(text("""
        INSERT INTO rol_operativo (codigo, nombre, activo) VALUES
          ('GERENTE_GENERAL', 'Gerente General', true),
          ('JEFE_PRODUCCION', 'Jefe de Produccion', true),
          ('CONFIGURACION_SCM', 'Configuracion SCM', true),
          ('SUPERVISOR', 'Supervisor', true),
          ('ALMACEN_RECEPCION', 'Almacen', true),
          ('CALIDAD', 'Calidad', true)
    """))
    connection.execute(text(
        "CREATE TABLE scm_emision_material (id CHAR(32) PRIMARY KEY)"
    ))
    connection.execute(text(
        "CREATE TABLE scm_ubicacion_inventario (id INTEGER PRIMARY KEY)"
    ))
    connection.execute(text(
        "CREATE TABLE scm_trabajo_ot (id CHAR(32) PRIMARY KEY)"
    ))
    connection.execute(text(
        "CREATE TABLE scm_material (id INTEGER PRIMARY KEY)"
    ))
    connection.execute(text("""
        CREATE TABLE scm_requerimiento_material (
            id CHAR(32) PRIMARY KEY,
            corrida_fabricacion_id CHAR(32) NOT NULL,
            material_id INTEGER NOT NULL
        )
    """))
    connection.execute(text(
        "INSERT INTO scm_corrida_fabricacion (id) VALUES ('run1')"
    ))
    connection.execute(text(
        "INSERT INTO receta_color_maestra (id) VALUES (1)"
    ))
    connection.execute(text(
        "INSERT INTO trabajador (id) VALUES (1), (2)"
    ))


def test_opm_migration_upgrade_constraints_indexes_and_downgrade(monkeypatch):
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
        raw_columns = {
            value["name"]: value
            for value in inspector.get_columns("scm_requerimiento_material")
        }
        assert raw_columns["corrida_fabricacion_id"]["nullable"] is True
        assert "orden_preparacion_material_id" in raw_columns
        assert {
            "ix_scm_req_mat_prep_estado_cursor",
            "ix_scm_req_mat_prep_receta",
        } <= {
            value["name"] for value in inspector.get_indexes(
                "scm_requerimiento_material_preparado"
            )
        }
        assert "uq_scm_reserva_mat_prep_activa" in {
            value["name"] for value in inspector.get_indexes(
                "scm_reserva_material_preparado"
            )
        }
        assert connection.execute(text("""
            SELECT COUNT(*) FROM scm_capacidad
            WHERE codigo IN ('OPM_CREAR', 'MATERIAL_PREPARADO_CONSUMIR')
        """)).scalar_one() == 2
        assert connection.execute(text("""
            SELECT COUNT(*) FROM rol_operativo
            WHERE codigo = 'PREPARADOR_MATERIAL'
        """)).scalar_one() == 1
        assert connection.execute(text("""
            SELECT COUNT(*)
            FROM scm_rol_capacidad AS role_capability
            JOIN rol_operativo AS role
              ON role.id = role_capability.rol_operativo_id
            JOIN scm_capacidad AS capability
              ON capability.id = role_capability.capacidad_id
            WHERE role.codigo = 'PREPARADOR_MATERIAL'
              AND capability.codigo = 'INVENTARIO_VER'
        """)).scalar_one() == 1
        uncovered_foreign_keys = []
        for table_name in sorted(NEW_TABLES):
            indexed_prefixes = {
                tuple(value["column_names"])
                for value in inspector.get_indexes(table_name)
            }
            indexed_prefixes.update(
                tuple(value["column_names"])
                for value in inspector.get_unique_constraints(table_name)
            )
            indexed_prefixes.add(tuple(
                inspector.get_pk_constraint(table_name)["constrained_columns"]
            ))
            for foreign_key in inspector.get_foreign_keys(table_name):
                constrained = tuple(foreign_key["constrained_columns"])
                if not any(
                    indexed[:len(constrained)] == constrained
                    for indexed in indexed_prefixes
                ):
                    uncovered_foreign_keys.append((table_name, constrained))
        assert uncovered_foreign_keys == []

        connection.execute(text("""
            INSERT INTO scm_orden_preparacion_material (
                id, codigo, receta_revision_id, composicion_hash,
                cantidad_objetivo_kg, estado, motivo, created_by_id,
                operation_id
            ) VALUES (
                'opm1', 'OPM-TEST-001', 1, 'hash', 10,
                'EN_PREPARACION', 'prueba', 1, 'op-create'
            )
        """))
        with pytest.raises(IntegrityError):
            connection.execute(text("""
                INSERT INTO scm_lectura_peso_preparacion (
                    id, orden_preparacion_id, tipo_uso,
                    peso_bruto_kg, tara_kg, peso_neto_kg,
                    metodo, evidencia_ref, motivo, estado,
                    created_by_id, operation_id
                ) VALUES (
                    'read1', 'opm1', 'BOLSA_SALIDA',
                    10.100, 0.100, 9.500,
                    'CONTINGENCIA_MANUAL', 'EV-01', 'prueba',
                    'PENDIENTE_SEGUNDA_CONFIRMACION', 1, 'read-op'
                )
            """))

        connection.execute(text(
            "DELETE FROM scm_orden_preparacion_material WHERE id = 'opm1'"
        ))

        migration.downgrade()
        inspector = inspect(connection)
        assert not (NEW_TABLES & set(inspector.get_table_names()))
        raw_columns = {
            value["name"]: value
            for value in inspector.get_columns("scm_requerimiento_material")
        }
        assert raw_columns["corrida_fabricacion_id"]["nullable"] is False
        assert "orden_preparacion_material_id" not in raw_columns


def test_opm_migration_explicitly_protects_supabase_data_api():
    migration = importlib.import_module(MIGRATION_MODULE)
    source = open(migration.__file__, encoding="utf-8").read()
    assert "SELECT current_schema()" in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "FROM anon" in source
    assert "FROM authenticated" in source


def test_opm_migration_downgrade_fails_closed_before_partial_schema(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _previous_schema(connection)
        connection.execute(text(
            "INSERT INTO scm_material (id) VALUES (1)"
        ))
        migration = importlib.import_module(MIGRATION_MODULE)
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        connection.execute(text("""
            INSERT INTO scm_orden_preparacion_material (
                id, codigo, receta_revision_id, composicion_hash,
                cantidad_objetivo_kg, estado, motivo, created_by_id,
                operation_id
            ) VALUES (
                'opm-block', 'OPM-BLOCK-001', 1, 'hash', 10,
                'BORRADOR', 'prueba rollback', 1, 'op-block-create'
            )
        """))
        with pytest.raises(RuntimeError, match="OPM_DATA_REQUIRES_EXPLICIT_ROLLBACK"):
            migration.downgrade()

        inspector = inspect(connection)
        assert NEW_TABLES <= set(inspector.get_table_names())
        assert "orden_preparacion_material_id" in {
            value["name"]
            for value in inspector.get_columns("scm_requerimiento_material")
        }
