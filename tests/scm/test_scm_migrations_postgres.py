import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema


pytestmark = pytest.mark.postgres

BACKEND_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "f02b00ae2e67"
EXPAND_REVISION = "91f3774850d8"
CONTRACT_REVISION = "58b3dd5878cd"
PURCHASE_REVISION = "23a5f8a99a0b"
RECEPTION_DRAFT_REVISION = "7c1e4a9d2b6f"
MOLDE_PIEZA_REVISION = "8f4c2d1a9b7e"
CATALOG_COUNTER_REVISION = "b31f9a2c7d04"
LINEA_FAMILIA_REVISION = "c42d8e6f1a03"
LEGACY_ADOPTION_TARGET = LINEA_FAMILIA_REVISION
WORK_COLOR_REVISION = "f78a7b3c9d20"
HEAD_REVISION = WORK_COLOR_REVISION


def _isolated_postgres_url():
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")

    base_url = make_url(raw_url)
    assert base_url.database in {"envaperu_test", "enva_test"}
    assert base_url.host in {"localhost", "127.0.0.1"}

    schema = f"scm_mig_{uuid4().hex[:12]}"
    admin_engine = create_engine(base_url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        assert connection.execute(
            text("SELECT current_database()")
        ).scalar_one() == "envaperu_test"
        connection.execute(CreateSchema(schema))

    query = dict(base_url.query)
    query["options"] = f"-csearch_path={schema}"
    schema_url = base_url.set(query=query)
    probe_engine = create_engine(schema_url)
    try:
        with probe_engine.connect() as connection:
            assert connection.execute(
                text("SELECT current_schema()")
            ).scalar_one() == schema
    finally:
        probe_engine.dispose()
    return admin_engine, schema, schema_url


def _run_flask_db(schema_url, *args):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = schema_url.render_as_string(
        hide_password=False
    )
    environment.pop("ALEMBIC_LEGACY_BASELINE", None)
    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "db", *args],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _run_flask_db_failure(schema_url, *args):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = schema_url.render_as_string(
        hide_password=False
    )
    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "db", *args],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    return result


def _drop_isolated_schema(admin_engine, schema):
    try:
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
    finally:
        admin_engine.dispose()


def test_migrations_crean_una_base_nueva_y_no_dejan_drift():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    try:
        _run_flask_db(schema_url, "upgrade", "head")
        schema_engine = create_engine(schema_url)
        try:
            tables = set(inspect(schema_engine).get_table_names())
            assert {
                "alembic_version",
                "materia_prima",
                "colorante",
                "correlativo_catalogo",
                "linea_familia",
                "molde_pieza",
                "rol_operativo",
                "scm_capacidad",
                "scm_categoria_recepcion",
                "scm_evento",
                "scm_material",
                "scm_operacion",
                "scm_orden_compra",
                "scm_orden_compra_linea",
                "scm_orden_compra_revision",
                "scm_proveedor",
                "scm_documento_proveedor",
                "scm_recepcion",
                "scm_recepcion_documento",
                "scm_recepcion_linea",
                "scm_pesaje_bolsa",
                "scm_rol_capacidad",
                "scm_articulo",
                "scm_articulo_pieza_color",
                "scm_definicion_wip",
                "scm_articulo_producto",
                "scm_estructura_revision",
                "scm_estructura_componente",
                "scm_centro_trabajo",
                "scm_ruta_revision",
                "scm_operacion_ruta",
                "scm_operacion_precedencia",
                "scm_tipo_contenedor",
                "scm_perfil_empacable",
                "scm_articulo_perfil",
                "scm_regla_empaque",
                "scm_regla_empaque_revision",
                "scm_lote_articulo",
                "scm_plan_manga_op",
                "scm_plan_manga_op_linea",
                "scm_asignacion_plan_manga_ot",
                "scm_manga",
                "scm_solicitud_manga_extra",
                "scm_trabajo_impresion_manga",
                "scm_etiqueta_manga",
                "scm_pesaje_manga",
                "scm_correccion_pesaje_manga",
                "scm_orden_produccion",
                "scm_orden_produccion_linea",
                "scm_orden_operacion",
                "scm_orden_fabricacion",
                "scm_corrida_fabricacion",
                "scm_orden_operacion_salida",
                "scm_asignacion_demanda_suministro",
                "scm_ubicacion_inventario",
                "scm_saldo_inventario",
                "scm_movimiento_inventario",
                "scm_existencia_manga",
                "scm_solicitud_abastecimiento",
                "scm_solicitud_abastecimiento_linea",
                "scm_asignacion_abastecimiento",
                "scm_confirmacion_manga_armado",
                "scm_consumo_componente_armado",
                "scm_pool_origen_armado",
                "scm_pool_origen_candidato",
                "scm_asignacion_pool_armado",
                "scm_correccion_manga_armado",
                "scm_saldo_material_inventario",
                "scm_movimiento_material_inventario",
                "scm_lote_apertura_inventario",
                "scm_lote_apertura_linea",
                "scm_requerimiento_material",
                "scm_reserva_material",
                "scm_emision_material",
                "scm_devolucion_material",
                "scm_lote_premezcla",
                "scm_lote_premezcla_input",
                "scm_trabajo_ot",
                "scm_trabajo_color",
                "scm_asignacion_personal_trabajo_ot",
            } <= tables
            assert "inventario_manga" not in tables
            assert "movimiento_kardex" not in tables
            assert "pieza_componente" not in tables
            assert "tipo" not in {
                column["name"]
                for column in inspect(schema_engine).get_columns("pieza_color")
            }
            with schema_engine.connect() as connection:
                assert connection.execute(
                    text("SELECT current_schema()")
                ).scalar_one() == schema
                function_config = connection.execute(text("""
                    SELECT array_to_string(function.proconfig, ',')
                    FROM pg_proc AS function
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = function.pronamespace
                    WHERE namespace.nspname = current_schema()
                      AND function.proname = 'scm_article_child_guard'
                """)).scalar_one()
                assert "search_path=pg_catalog" in function_config
                assert schema in function_config
                assert connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == HEAD_REVISION
                capacidades = set(connection.execute(
                    text("SELECT codigo FROM scm_capacidad")
                ).scalars())
                assert {
                    "PROVEEDOR_ADMINISTRAR",
                    "OC_CREAR",
                    "OC_APROBAR",
                    "RECEPCION_CONFIRMAR",
                    "ENTRADA_EXCEPCIONAL_REGULARIZAR",
                    "CALIDAD_RESOLVER",
                    "LIBERACION_DIRECTA_ADMINISTRAR",
                    "CORRECCION_SOLICITAR",
                    "CORRECCION_APROBAR",
                    "DEVOLUCION_REGISTRAR",
                    "CONFIG_RECEPCION_ADMINISTRAR",
                    "DOCUMENTO_PROVEEDOR_REGISTRAR",
                    "GENEALOGIA_VER",
                    "ENSAMBLE_PLANIFICAR",
                    "ENSAMBLE_MANGA_CERRAR",
                    "GENEALOGIA_LEGACY_APERTURA",
                    "ENSAMBLE_CORREGIR_SOLICITAR",
                    "ENSAMBLE_CORREGIR_APROBAR",
                    "INVENTARIO_APERTURA_PREPARAR",
                    "INVENTARIO_APERTURA_APROBAR",
                    "OA_VER",
                    "OA_LIBERAR",
                    "OA_EJECUTAR",
                    "OA_ANULAR",
                } <= capacidades
                assert not any(code.startswith("OE_") for code in capacidades)
                assert connection.execute(
                    text("SELECT count(*) FROM trabajador")
                ).scalar_one() == 0
                assert connection.execute(text("""
                    SELECT clave, prefijo, siguiente_valor, ancho
                    FROM correlativo_catalogo
                    ORDER BY clave
                """)).tuples().all() == [
                        ("ADITIVO", "ADT", 1, 6),
                        ("CATEGORIA_RECEPCION", "CAT", 1, 6),
                        ("CENTRO_TRABAJO", "CT", 1, 6),
                        ("COLORANTE", "COL", 1, 6),
                    ("FAMILIA", "FAM", 1, 6),
                    ("FAMILIA_COLOR", "FC", 1, 6),
                    ("LINEA", "LIN", 1, 6),
                    ("MAQUINA", "MAQ", 1, 6),
                    ("MATERIA_PRIMA", "MP", 1, 6),
                        ("MOLDE", "ML", 1, 6),
                        ("ORDEN_ARMADO", "OA", 1, 6),
                        ("ORDEN_FABRICACION", "OF", 1, 6),
                        ("ORDEN_PRODUCCION", "OP", 1, 6),
                        ("ORDEN_TRABAJO", "OT", 1, 6),
                        ("PERFIL_EMPAQUE", "PEM", 1, 6),
                    ("PIEZA", "PZ", 1, 6),
                    ("PIEZA_COLOR", "PC", 1, 6),
                    ("PRESENTACION_COMERCIAL", "PRE", 1, 6),
                    ("PRODUCTO_TERMINADO", "PT", 1, 6),
                    ("PROVEEDOR", "PRV", 1, 6),
                    ("SOLICITUD_ABASTECIMIENTO", "SA", 1, 6),
                    ("SUBENSAMBLE_WIP", "WIP", 1, 6),
                    ("TIPO_CONTENEDOR", "TCO", 1, 6),
                    ("TIPO_MANGA", "TMG", 1, 6),
                    ("TIPO_MAQUINA", "TMQ", 1, 6),
                    ("TRABAJADOR", "TRB", 1, 6),
                ]

            materia_columns = {
                item["name"]: item
                for item in inspect(schema_engine).get_columns("materia_prima")
            }
            colorante_columns = {
                item["name"]: item
                for item in inspect(schema_engine).get_columns("colorante")
            }
            assert materia_columns["scm_material_id"]["nullable"] is False
            assert colorante_columns["scm_material_id"]["nullable"] is False
            pieza_columns = {
                item["name"]
                for item in inspect(schema_engine).get_columns("pieza")
            }
            assert {"codigo", "peso_nominal_gr", "activo", "version"} <= (
                pieza_columns
            )
            assert {"molde_id", "cavidades", "peso_unitario_gr"}.isdisjoint(
                pieza_columns
            )
            for catalog_name in ("linea", "familia"):
                catalog_columns = {
                    item["name"]: item
                    for item in inspect(schema_engine).get_columns(catalog_name)
                }
                assert catalog_columns["activo"]["nullable"] is False
                assert catalog_columns["version"]["nullable"] is False
            with schema_engine.connect() as connection:
                assert connection.execute(text(
                    "SELECT count(*) FROM linea_familia"
                )).scalar_one() == 0
        finally:
            schema_engine.dispose()

        _run_flask_db(schema_url, "check")
    finally:
        _drop_isolated_schema(admin_engine, schema)


def test_trabajo_color_backfill_enlaza_ot_canonica_y_preserva_legacy():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    canonical_ot_public_id = uuid4()
    second_canonical_ot_public_id = uuid4()
    assembly_ot_public_id = uuid4()
    synthetic_ot_public_id = uuid4()
    order_id = uuid4()
    run_id = uuid4()
    try:
        _run_flask_db(schema_url, "upgrade", "f77e6f1b4c98")
        with schema_engine.begin() as connection:
            worker_id = connection.execute(text("""
                INSERT INTO trabajador (
                    codigo, nombres, apellidos, activo
                )
                VALUES ('TR-TC-001', 'Ana', 'Operaria', true)
                RETURNING id
            """)).scalar_one()
            machine_type_id = connection.execute(text("""
                INSERT INTO tipo_maquina (
                    codigo, nombre, proceso, activo, version
                )
                VALUES ('TM-TC-001', 'Inyectora prueba', 'INYECCION', true, 1)
                RETURNING id
            """)).scalar_one()
            machine_id = connection.execute(text("""
                INSERT INTO maquina (
                    codigo, nombre, tipo_maquina_id, estado, activo
                )
                VALUES (
                    'MAQ-TC-001', 'Maquina trabajo color',
                    :machine_type_id, 'OPERATIVA', true
                )
                RETURNING id
            """), {"machine_type_id": machine_type_id}).scalar_one()
            work_center_id = connection.execute(text("""
                INSERT INTO scm_centro_trabajo (
                    codigo, nombre, tipo, activo, version
                )
                VALUES ('CT-TC-001', 'Mesa prueba', 'ENSAMBLE', true, 1)
                RETURNING id
            """)).scalar_one()
            connection.execute(text("""
                INSERT INTO scm_orden_operacion (
                    id, codigo, tipo, origen_demanda, estado, version,
                    created_by_id
                )
                VALUES (
                    :order_id, 'OF-TC-001', 'FABRICACION', 'MANUAL',
                    'EN_EJECUCION', 1, :worker_id
                )
            """), {"order_id": order_id, "worker_id": worker_id})
            connection.execute(text("""
                INSERT INTO scm_orden_fabricacion (
                    orden_operacion_id, snapshot_peso_colada_gr
                )
                VALUES (:order_id, 3.2500)
            """), {"order_id": order_id})
            connection.execute(text("""
                INSERT INTO scm_corrida_fabricacion (
                    id, orden_fabricacion_id, codigo, secuencia,
                    receta_hash, ciclos_objetivo, estado
                )
                VALUES (
                    :run_id, :order_id, 'COR-TC-001', 1,
                    :recipe_hash, 100, 'EN_EJECUCION'
                )
            """), {
                "run_id": run_id,
                "order_id": order_id,
                "recipe_hash": "a" * 64,
            })
            canonical_ot_id = connection.execute(text("""
                INSERT INTO registro_diario_produccion (
                    public_id, codigo_ot, codigo_ot_sintetico, estado,
                    tipo_ot, fecha, turno, maquina_id, orden_operacion_id,
                    corrida_fabricacion_id, cantidad_objetivo,
                    cantidad_confirmada, created_by_id,
                    maquinista_previsto_id, created_at, updated_at,
                    iniciada_at, colada_inicial, colada_final,
                    snapshot_cavidades, snapshot_peso_neto_gr,
                    snapshot_peso_colada_gr
                )
                VALUES (
                    :public_id, 'OT-COLOR-001', false, 'EN_EJECUCION',
                    'FABRICACION', DATE '2026-08-08', 'DIA', :machine_id,
                    :order_id, :run_id, 1000, 250, :worker_id, :worker_id,
                    TIMESTAMPTZ '2026-08-08 12:00:00+00',
                    TIMESTAMPTZ '2026-08-08 13:00:00+00',
                    TIMESTAMPTZ '2026-08-08 12:30:00+00',
                    10, 35, 4, 12.5, 3.25
                )
                RETURNING id
            """), {
                "public_id": canonical_ot_public_id,
                "machine_id": machine_id,
                "order_id": order_id,
                "run_id": run_id,
                "worker_id": worker_id,
            }).scalar_one()
            second_canonical_ot_id = connection.execute(text("""
                INSERT INTO registro_diario_produccion (
                    public_id, codigo_ot, codigo_ot_sintetico, estado,
                    tipo_ot, fecha, turno, maquina_id, orden_operacion_id,
                    corrida_fabricacion_id, cantidad_objetivo,
                    cantidad_confirmada, created_by_id,
                    maquinista_previsto_id
                )
                VALUES (
                    :public_id, 'OT-COLOR-002', false, 'PLANIFICADA',
                    'FABRICACION', DATE '2026-08-08', 'DIA', :machine_id,
                    :order_id, :run_id, 500, 0, :worker_id, :worker_id
                )
                RETURNING id
            """), {
                "public_id": second_canonical_ot_public_id,
                "machine_id": machine_id,
                "order_id": order_id,
                "run_id": run_id,
                "worker_id": worker_id,
            }).scalar_one()
            connection.execute(text("""
                INSERT INTO registro_diario_produccion (
                    public_id, codigo_ot, codigo_ot_sintetico, estado,
                    tipo_ot, fecha, maquina_id, orden_operacion_id,
                    corrida_fabricacion_id
                )
                VALUES (
                    :public_id, 'OT-LEGACY-TC-001', true, 'PLANIFICADA',
                    'FABRICACION', DATE '2026-08-08', :machine_id,
                    :order_id, :run_id
                )
            """), {
                "public_id": synthetic_ot_public_id,
                "machine_id": machine_id,
                "order_id": order_id,
                "run_id": run_id,
            })
            assembly_ot_id = connection.execute(text("""
                INSERT INTO registro_diario_produccion (
                    public_id, codigo_ot, codigo_ot_sintetico, estado,
                    tipo_ot, modo_ejecucion_ensamble,
                    ot_fabricacion_contexto_id, fecha, centro_trabajo_id,
                    responsable_id
                )
                VALUES (
                    :public_id, 'OT-ARMADO-001', false, 'PLANIFICADA',
                    'ENSAMBLE', 'CONCURRENTE', :fabrication_public_id,
                    DATE '2026-08-08', :work_center_id, :worker_id
                )
                RETURNING id
            """), {
                "public_id": assembly_ot_public_id,
                "fabrication_public_id": canonical_ot_public_id,
                "work_center_id": work_center_id,
                "worker_id": worker_id,
            }).scalar_one()

        _run_flask_db(schema_url, "upgrade", WORK_COLOR_REVISION)

        inspector = inspect(schema_engine)
        assert {
            "scm_trabajo_ot",
            "scm_trabajo_color",
            "scm_asignacion_personal_trabajo_ot",
        } <= set(inspector.get_table_names())
        work_columns = {
            column["name"]: column
            for column in inspector.get_columns("scm_trabajo_ot")
        }
        assert work_columns["continua_de_id"]["nullable"] is True
        assert {
            foreign_key["name"]
            for foreign_key in inspector.get_foreign_keys("scm_trabajo_ot")
        } >= {"fk_scm_trabajo_ot_continua_de"}
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "scm_trabajo_ot"
            )
        } >= {"uq_scm_trabajo_ot_continuacion"}
        for table_name in (
            "scm_asignacion_plan_manga_ot",
            "scm_manga",
            "scm_solicitud_manga_extra",
        ):
            columns = {
                column["name"]: column
                for column in inspector.get_columns(table_name)
            }
            assert columns["trabajo_ot_id"]["nullable"] is True
        manga_columns = {
            column["name"]: column
            for column in inspector.get_columns("scm_manga")
        }
        assert manga_columns[
            "asignacion_personal_trabajo_id"
        ]["nullable"] is True
        weighing_columns = {
            column["name"]: column
            for column in inspector.get_columns("scm_pesaje_manga")
        }
        assert weighing_columns[
            "asignacion_personal_trabajo_id"
        ]["nullable"] is True
        color_columns = {
            column["name"]: column
            for column in inspector.get_columns("scm_trabajo_color")
        }
        assert str(color_columns["cavidades_snapshot"]["type"]).upper() == (
            "INTEGER"
        )
        for column_name in (
            "peso_neto_snapshot_g",
            "peso_colada_snapshot_g",
        ):
            assert color_columns[column_name]["type"].precision == 15
            assert color_columns[column_name]["type"].scale == 4
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "scm_trabajo_color"
            )
        } >= {
            "ck_scm_trabajo_color_cavidades",
            "ck_scm_trabajo_color_peso_neto",
            "ck_scm_trabajo_color_peso_colada",
        }
        ot_columns = {
            column["name"]: column
            for column in inspector.get_columns(
                "registro_diario_produccion"
            )
        }
        assert ot_columns["trabajo_color_contexto_id"]["nullable"] is True
        assert ot_columns["secuencia_siguiente_trabajo"]["nullable"] is False
        assert "1" in str(
            ot_columns["secuencia_siguiente_trabajo"]["default"]
        )
        for table_name, foreign_key_name in (
            (
                "scm_asignacion_plan_manga_ot",
                "fk_scm_asignacion_plan_trabajo_ot",
            ),
            ("scm_manga", "fk_scm_manga_trabajo_ot"),
            (
                "scm_solicitud_manga_extra",
                "fk_scm_solicitud_extra_trabajo_ot",
            ),
        ):
            foreign_keys = {
                foreign_key["name"]: foreign_key
                for foreign_key in inspector.get_foreign_keys(table_name)
            }
            work_foreign_key = foreign_keys[foreign_key_name]
            assert work_foreign_key["constrained_columns"] == [
                "trabajo_ot_id", "ot_id",
            ]
            assert work_foreign_key["referred_table"] == "scm_trabajo_ot"
            assert work_foreign_key["referred_columns"] == [
                "id", "orden_trabajo_id",
            ]
        manga_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("scm_manga")
        }
        manga_assignment_fk = manga_foreign_keys[
            "fk_scm_manga_asignacion_personal_trabajo"
        ]
        assert manga_assignment_fk["constrained_columns"] == [
            "asignacion_personal_trabajo_id", "trabajo_ot_id",
        ]
        assert manga_assignment_fk["referred_columns"] == [
            "id", "trabajo_ot_id",
        ]
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "scm_asignacion_personal_trabajo_ot"
            )
        } >= {"uq_scm_asignacion_personal_id_trabajo"}
        assert {
            foreign_key["name"]
            for foreign_key in inspector.get_foreign_keys(
                "scm_pesaje_manga"
            )
        } >= {"fk_scm_pesaje_asignacion_personal"}

        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == WORK_COLOR_REVISION
            work = connection.execute(text("""
                SELECT
                    trabajo.id,
                    trabajo.codigo,
                    trabajo.tipo,
                    trabajo.secuencia,
                    trabajo.estado,
                    trabajo.orden_operacion_id,
                    trabajo.cantidad_objetivo_un,
                    trabajo.cantidad_confirmada_un,
                    trabajo.iniciada_at,
                    color.corrida_fabricacion_id,
                    color.receta_hash_snapshot,
                    color.cavidades_snapshot,
                    color.peso_neto_snapshot_g,
                    color.peso_colada_snapshot_g,
                    color.colada_inicial,
                    color.colada_final
                FROM scm_trabajo_ot AS trabajo
                JOIN scm_trabajo_color AS color
                  ON color.trabajo_ot_id = trabajo.id
                WHERE trabajo.orden_trabajo_id = :ot_id
            """), {"ot_id": canonical_ot_id}).mappings().one()
            assert work["codigo"] == "OT-COLOR-001-TC01"
            assert work["tipo"] == "COLOR"
            assert work["secuencia"] == 1
            assert work["estado"] == "EN_EJECUCION"
            assert work["orden_operacion_id"] == order_id
            assert work["cantidad_objetivo_un"] == Decimal("1000")
            assert work["cantidad_confirmada_un"] == Decimal("250")
            assert work["corrida_fabricacion_id"] == run_id
            assert work["receta_hash_snapshot"] == "a" * 64
            assert work["cavidades_snapshot"] == 4
            assert work["peso_neto_snapshot_g"] == Decimal("12.5")
            assert work["peso_colada_snapshot_g"] == Decimal("3.25")
            assert work["colada_inicial"] == 10
            assert work["colada_final"] == 35
            assert connection.execute(text("""
                SELECT
                    estado,
                    trabajador_id,
                    asignada_por_id,
                    iniciada_at IS NOT NULL
                FROM scm_asignacion_personal_trabajo_ot
                WHERE trabajo_ot_id = :work_id
            """), {"work_id": work["id"]}).one() == (
                "ACTIVA", worker_id, worker_id, True,
            )
            assert connection.execute(text("""
                SELECT trabajo_color_contexto_id
                FROM registro_diario_produccion
                WHERE id = :assembly_ot_id
            """), {
                "assembly_ot_id": assembly_ot_id,
            }).scalar_one() == work["id"]
            assert connection.execute(text("""
                SELECT secuencia_siguiente_trabajo
                FROM registro_diario_produccion
                WHERE id = :canonical_ot_id
            """), {
                "canonical_ot_id": canonical_ot_id,
            }).scalar_one() == 2
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_trabajo_ot
            """)).scalar_one() == 2
            assert connection.execute(text("""
                SELECT trabajo.estado, asignacion.estado
                FROM scm_trabajo_ot AS trabajo
                JOIN scm_asignacion_personal_trabajo_ot AS asignacion
                  ON asignacion.trabajo_ot_id = trabajo.id
                WHERE trabajo.orden_trabajo_id = :ot_id
            """), {
                "ot_id": second_canonical_ot_id,
            }).one() == ("PLANIFICADO", "PREVISTA")
            assert connection.execute(text("""
                SELECT secuencia_siguiente_trabajo
                FROM registro_diario_produccion
                WHERE public_id = :synthetic_public_id
            """), {
                "synthetic_public_id": synthetic_ot_public_id,
            }).scalar_one() == 1
            indexes = dict(connection.execute(text("""
                SELECT indexname, lower(indexdef)
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND indexname IN (
                    'uq_scm_asignacion_plan_trabajo',
                    'uq_scm_asignacion_plan_ot_legacy',
                    'uq_scm_trabajo_ot_activo',
                    'uq_scm_asignacion_personal_activa',
                    'uq_registro_ot_fabricacion_recurso_turno_activa',
                    'ix_scm_asignacion_plan_trabajo',
                    'ix_scm_manga_trabajo',
                    'ix_scm_manga_asignacion_personal',
                    'ix_scm_solicitud_extra_trabajo',
                    'ix_scm_trabajo_ot_created_by',
                    'ix_scm_trabajo_ot_anulada_por',
                    'ix_scm_asignacion_personal_trabajo',
                    'ix_scm_asignacion_personal_asignada_por',
                    'ix_scm_asignacion_personal_finalizada_por'
                  )
            """)).tuples().all())
            assert {
                "uq_scm_asignacion_plan_trabajo",
                "uq_scm_asignacion_plan_ot_legacy",
                "uq_scm_trabajo_ot_activo",
                "uq_scm_asignacion_personal_activa",
                "uq_registro_ot_fabricacion_recurso_turno_activa",
                "ix_scm_asignacion_plan_trabajo",
                "ix_scm_manga_trabajo",
                "ix_scm_manga_asignacion_personal",
                "ix_scm_solicitud_extra_trabajo",
                "ix_scm_trabajo_ot_created_by",
                "ix_scm_trabajo_ot_anulada_por",
                "ix_scm_asignacion_personal_trabajo",
                "ix_scm_asignacion_personal_asignada_por",
                "ix_scm_asignacion_personal_finalizada_por",
            } == set(indexes)
            assert "where (trabajo_ot_id is not null)" in indexes[
                "uq_scm_asignacion_plan_trabajo"
            ]
            assert "where (trabajo_ot_id is null)" in indexes[
                "uq_scm_asignacion_plan_ot_legacy"
            ]
            header_index = indexes[
                "uq_registro_ot_fabricacion_recurso_turno_activa"
            ]
            assert "codigo_ot_sintetico" in header_index
            assert "false" in header_index
            assert "estado" in header_index
            assert "'anulada'" in header_index
            assert "orden_id is null" in header_index
            assert "orden_operacion_id is null" in header_index
            assert "corrida_fabricacion_id is null" in header_index
            assert connection.execute(text("""
                SELECT count(*)
                FROM pg_constraint
                WHERE conrelid = 'scm_asignacion_plan_manga_ot'::regclass
                  AND conname = 'uq_scm_asignacion_plan_ot'
            """)).scalar_one() == 0
            rls_enabled = dict(connection.execute(text("""
                SELECT class.relname, class.relrowsecurity
                FROM pg_class AS class
                JOIN pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = current_schema()
                  AND class.relname IN (
                    'scm_trabajo_ot',
                    'scm_trabajo_color',
                    'scm_asignacion_personal_trabajo_ot'
                  )
            """)).tuples().all())
            assert rls_enabled == {
                "scm_trabajo_ot": True,
                "scm_trabajo_color": True,
                "scm_asignacion_personal_trabajo_ot": True,
            }

        new_work_id = uuid4()
        with schema_engine.begin() as connection:
            new_header_id = connection.execute(text("""
                INSERT INTO registro_diario_produccion (
                    public_id, codigo_ot, codigo_ot_sintetico, estado,
                    tipo_ot, fecha, turno, maquina_id
                )
                VALUES (
                    :public_id, 'OT-CABECERA-001', false, 'PLANIFICADA',
                    'FABRICACION', DATE '2026-08-09', 'NOCHE', :machine_id
                )
                RETURNING id
            """), {
                "public_id": uuid4(),
                "machine_id": machine_id,
            }).scalar_one()
            connection.execute(text("""
                INSERT INTO scm_trabajo_ot (
                    id, orden_trabajo_id, codigo, tipo, secuencia, estado,
                    orden_operacion_id, cantidad_objetivo_un,
                    cantidad_confirmada_un, created_by_id
                )
                VALUES (
                    :work_id, :header_id, 'OT-CABECERA-001-TC01',
                    'COLOR', 1, 'PLANIFICADO', :order_id, 0, 0, :worker_id
                )
            """), {
                "work_id": new_work_id,
                "header_id": new_header_id,
                "order_id": order_id,
                "worker_id": worker_id,
            })
            connection.execute(text("""
                INSERT INTO scm_trabajo_color (
                    trabajo_ot_id, corrida_fabricacion_id
                )
                VALUES (:work_id, :run_id)
            """), {"work_id": new_work_id, "run_id": run_id})
        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO registro_diario_produccion (
                        public_id, codigo_ot, codigo_ot_sintetico, estado,
                        tipo_ot, fecha, turno, maquina_id
                    )
                    VALUES (
                        :public_id, 'OT-CABECERA-002', false, 'BORRADOR',
                        'FABRICACION', DATE '2026-08-09', 'NOCHE', :machine_id
                    )
                """), {"public_id": uuid4(), "machine_id": machine_id})

        failed_downgrade = _run_flask_db_failure(
            schema_url, "downgrade", "f77e6f1b4c98"
        )
        assert "SCM_TRABAJO_COLOR_DOWNGRADE_BLOCKED" in (
            failed_downgrade.stdout + failed_downgrade.stderr
        )
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == WORK_COLOR_REVISION
        with schema_engine.begin() as connection:
            connection.execute(text("""
                DELETE FROM scm_trabajo_color
                WHERE trabajo_ot_id = :work_id
            """), {"work_id": new_work_id})
            connection.execute(text("""
                DELETE FROM scm_trabajo_ot
                WHERE id = :work_id
            """), {"work_id": new_work_id})
            connection.execute(text("""
                DELETE FROM registro_diario_produccion
                WHERE id = :header_id
            """), {"header_id": new_header_id})

        _run_flask_db(schema_url, "downgrade", "f77e6f1b4c98")
        downgraded = inspect(schema_engine)
        assert {
            "scm_trabajo_ot",
            "scm_trabajo_color",
            "scm_asignacion_personal_trabajo_ot",
        }.isdisjoint(downgraded.get_table_names())
        assert "trabajo_ot_id" not in {
            column["name"]
            for column in downgraded.get_columns(
                "scm_asignacion_plan_manga_ot"
            )
        }
        assert {
            constraint["name"]
            for constraint in downgraded.get_unique_constraints(
                "scm_asignacion_plan_manga_ot"
            )
        } >= {"uq_scm_asignacion_plan_ot"}

        _run_flask_db(schema_url, "upgrade", WORK_COLOR_REVISION)
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == WORK_COLOR_REVISION
            assert connection.execute(
                text("SELECT count(*) FROM scm_trabajo_ot")
            ).scalar_one() == 2
            assert connection.execute(text("""
                SELECT trabajo_color_contexto_id IS NOT NULL
                FROM registro_diario_produccion
                WHERE id = :assembly_ot_id
            """), {
                "assembly_ot_id": assembly_ot_id,
            }).scalar_one() is True
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_inicio_concurrente_trabajo_color_serializa_por_maquina():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url, pool_size=4, max_overflow=0)
    order_id = uuid4()
    run_id = uuid4()
    work_ids = (uuid4(), uuid4())
    try:
        _run_flask_db(schema_url, "upgrade", HEAD_REVISION)
        with schema_engine.begin() as connection:
            worker_id = connection.execute(text("""
                INSERT INTO trabajador (
                    codigo, nombres, apellidos, activo
                )
                VALUES ('TR-TC-CONC', 'Rosa', 'Concurrente', true)
                RETURNING id
            """)).scalar_one()
            machine_type_id = connection.execute(text("""
                INSERT INTO tipo_maquina (
                    codigo, nombre, proceso, activo, version
                )
                VALUES (
                    'TM-TC-CONC', 'Tipo concurrencia',
                    'INYECCION', true, 1
                )
                RETURNING id
            """)).scalar_one()
            machine_id = connection.execute(text("""
                INSERT INTO maquina (
                    codigo, nombre, tipo_maquina_id, estado, activo
                )
                VALUES (
                    'MAQ-TC-CONC', 'Maquina concurrencia',
                    :machine_type_id, 'OPERATIVA', true
                )
                RETURNING id
            """), {"machine_type_id": machine_type_id}).scalar_one()
            connection.execute(text("""
                INSERT INTO scm_orden_operacion (
                    id, codigo, tipo, origen_demanda, estado, version,
                    created_by_id
                )
                VALUES (
                    :order_id, 'OF-TC-CONC', 'FABRICACION', 'MANUAL',
                    'PROGRAMADA', 1, :worker_id
                )
            """), {"order_id": order_id, "worker_id": worker_id})
            connection.execute(text("""
                INSERT INTO scm_orden_fabricacion (orden_operacion_id)
                VALUES (:order_id)
            """), {"order_id": order_id})
            connection.execute(text("""
                INSERT INTO scm_corrida_fabricacion (
                    id, orden_fabricacion_id, codigo, secuencia, estado
                )
                VALUES (
                    :run_id, :order_id, 'COR-TC-CONC', 1, 'LIBERADA'
                )
            """), {"run_id": run_id, "order_id": order_id})
            ot_ids = tuple(connection.execute(text("""
                INSERT INTO registro_diario_produccion (
                    public_id, codigo_ot, codigo_ot_sintetico, estado,
                    tipo_ot, fecha, turno, maquina_id,
                    orden_operacion_id, corrida_fabricacion_id,
                    cantidad_objetivo, cantidad_confirmada,
                    created_by_id, maquinista_previsto_id,
                    secuencia_siguiente_trabajo
                )
                VALUES
                    (
                        :public_id_a, 'OT-TC-CONC-A', false,
                        'PLANIFICADA', 'FABRICACION', DATE '2026-08-10',
                        'DIA', :machine_id, :order_id, :run_id,
                        100, 0, :worker_id, :worker_id, 2
                    ),
                    (
                        :public_id_b, 'OT-TC-CONC-B', false,
                        'PLANIFICADA', 'FABRICACION', DATE '2026-08-10',
                        'DIA', :machine_id, :order_id, :run_id,
                        100, 0, :worker_id, :worker_id, 2
                    )
                RETURNING id
            """), {
                "public_id_a": uuid4(),
                "public_id_b": uuid4(),
                "machine_id": machine_id,
                "order_id": order_id,
                "run_id": run_id,
                "worker_id": worker_id,
            }).scalars())
            for sequence, (ot_id, work_id) in enumerate(
                zip(ot_ids, work_ids), start=1
            ):
                connection.execute(text("""
                    INSERT INTO scm_trabajo_ot (
                        id, orden_trabajo_id, codigo, tipo, secuencia,
                        estado, orden_operacion_id, cantidad_objetivo_un,
                        cantidad_confirmada_un, created_by_id
                    )
                    VALUES (
                        :work_id, :ot_id, :code, 'COLOR', 1,
                        'PLANIFICADO', :order_id, 100, 0, :worker_id
                    )
                """), {
                    "work_id": work_id,
                    "ot_id": ot_id,
                    "code": f"OT-TC-CONC-{sequence}-TC01",
                    "order_id": order_id,
                    "worker_id": worker_id,
                })
                connection.execute(text("""
                    INSERT INTO scm_trabajo_color (
                        trabajo_ot_id, corrida_fabricacion_id
                    )
                    VALUES (:work_id, :run_id)
                """), {"work_id": work_id, "run_id": run_id})

        def try_start(work_id):
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    SELECT id
                    FROM maquina
                    WHERE id = :machine_id
                    FOR UPDATE
                """), {"machine_id": machine_id}).scalar_one()
                active_work_id = connection.execute(text("""
                    SELECT trabajo.id
                    FROM scm_trabajo_ot AS trabajo
                    JOIN registro_diario_produccion AS ot
                      ON ot.id = trabajo.orden_trabajo_id
                    WHERE ot.maquina_id = :machine_id
                      AND trabajo.estado = 'EN_EJECUCION'
                      AND trabajo.id <> :work_id
                    LIMIT 1
                """), {
                    "machine_id": machine_id,
                    "work_id": work_id,
                }).scalar_one_or_none()
                if active_work_id is not None:
                    return "MACHINE_BUSY"
                connection.execute(text("""
                    UPDATE scm_trabajo_ot
                    SET estado = 'EN_EJECUCION',
                        iniciada_at = now(),
                        version = version + 1
                    WHERE id = :work_id
                """), {"work_id": work_id})
                return "started"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(try_start, work_ids))

        assert sorted(outcomes) == ["MACHINE_BUSY", "started"]
        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_trabajo_ot AS trabajo
                JOIN registro_diario_produccion AS ot
                  ON ot.id = trabajo.orden_trabajo_id
                WHERE ot.maquina_id = :machine_id
                  AND trabajo.estado = 'EN_EJECUCION'
            """), {"machine_id": machine_id}).scalar_one() == 1
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_trabajo_ot
                WHERE id IN (:work_id_a, :work_id_b)
                  AND estado = 'PLANIFICADO'
            """), {
                "work_id_a": work_ids[0],
                "work_id_b": work_ids[1],
            }).scalar_one() == 1
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def _run_color_work_concurrency_scenario(*, include_weighing_annulment):
    from app.services.scm_ot_service import (
        add_work_mangas,
        assign_color_work_worker,
    )
    from app.services.scm_service_support import ScmServiceError
    from app.services.scm_weighing_service import annul_manga_weighing

    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url, pool_size=4, max_overflow=0)
    order_id = uuid4()
    run_id = uuid4()
    output_id = uuid4()
    work_id = uuid4()
    operation_ids = (uuid4(), uuid4())
    try:
        _run_flask_db(schema_url, "upgrade", HEAD_REVISION)
        with schema_engine.begin() as connection:
            actors = {
                row.codigo: row.id
                for row in connection.execute(text("""
                    INSERT INTO trabajador (
                        codigo, nombres, apellidos, activo
                    )
                    VALUES
                        ('TR-TC-M3-ACTOR', 'Jefa', 'M3', true),
                        ('TR-TC-M3-REL-A', 'Relevo', 'M3 A', true),
                        ('TR-TC-M3-REL-B', 'Relevo', 'M3 B', true)
                    RETURNING id, codigo
                """))
            }
            connection.execute(text("""
                INSERT INTO trabajador_rol (trabajador_id, rol_operativo_id)
                SELECT :actor_id, id
                FROM rol_operativo
                WHERE codigo IN ('SUPERVISOR', 'JEFE_PRODUCCION')
                UNION ALL
                SELECT worker.id, rol.id
                FROM trabajador AS worker
                JOIN rol_operativo AS rol ON rol.codigo = 'MAQUINISTA'
                WHERE worker.id IN (
                    :actor_id, :relief_a_id, :relief_b_id
                )
            """), {
                "actor_id": actors["TR-TC-M3-ACTOR"],
                "relief_a_id": actors["TR-TC-M3-REL-A"],
                "relief_b_id": actors["TR-TC-M3-REL-B"],
            })
            machine_type_id = connection.execute(text("""
                INSERT INTO tipo_maquina (
                    codigo, nombre, proceso, activo, version
                )
                VALUES (
                    'TM-TC-M3', 'Tipo relevo concurrente',
                    'INYECCION', true, 1
                )
                RETURNING id
            """)).scalar_one()
            machine_id = connection.execute(text("""
                INSERT INTO maquina (
                    codigo, nombre, tipo_maquina_id, estado, activo
                )
                VALUES (
                    'MAQ-TC-M3', 'Maquina relevo concurrente',
                    :machine_type_id, 'OPERATIVA', true
                )
                RETURNING id
            """), {"machine_type_id": machine_type_id}).scalar_one()
            connection.execute(text("""
                INSERT INTO scm_orden_operacion (
                    id, codigo, tipo, origen_demanda, estado, version,
                    created_by_id
                )
                VALUES (
                    :order_id, 'OF-TC-M3', 'FABRICACION', 'MANUAL',
                    'EN_EJECUCION', 1, :actor_id
                )
            """), {
                "order_id": order_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
            })
            connection.execute(text("""
                INSERT INTO scm_orden_fabricacion (orden_operacion_id)
                VALUES (:order_id)
            """), {"order_id": order_id})
            connection.execute(text("""
                INSERT INTO scm_corrida_fabricacion (
                    id, orden_fabricacion_id, codigo, secuencia, estado
                )
                VALUES (
                    :run_id, :order_id, 'COR-TC-M3', 1, 'EN_EJECUCION'
                )
            """), {"run_id": run_id, "order_id": order_id})

            article_id = connection.execute(text("""
                INSERT INTO scm_articulo (
                    public_id, codigo, nombre, clase
                )
                VALUES (
                    :public_id, 'WIP-TC-M3', 'WIP relevo concurrente',
                    'SUBENSAMBLE_WIP'
                )
                RETURNING id
            """), {"public_id": uuid4()}).scalar_one()
            connection.execute(text("""
                INSERT INTO scm_definicion_wip (articulo_id, descripcion)
                VALUES (:article_id, 'Fixture PostgreSQL M3-02')
            """), {"article_id": article_id})
            connection.execute(text("""
                INSERT INTO scm_orden_operacion_salida (
                    id, orden_operacion_id, corrida_fabricacion_id,
                    articulo_scm_id, cantidad_por_ciclo_snapshot,
                    peso_unitario_snapshot_g, cantidad_objetivo
                )
                VALUES (
                    :output_id, :order_id, :run_id, :article_id,
                    1, 100, 100
                )
            """), {
                "output_id": output_id,
                "order_id": order_id,
                "run_id": run_id,
                "article_id": article_id,
            })
            lot_id = connection.execute(text("""
                INSERT INTO scm_lote_articulo (
                    public_id, codigo, articulo_id, clase,
                    orden_operacion_salida_id, cantidad_acreditada,
                    estado_calidad, actor_id
                )
                VALUES (
                    :public_id, 'LOT-TC-M3', :article_id,
                    'SALIDA_ORDEN_OPERACION', :output_id, 0,
                    'PLANIFICADO', :actor_id
                )
                RETURNING id
            """), {
                "public_id": uuid4(),
                "article_id": article_id,
                "output_id": output_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
            }).scalar_one()
            profile_id = connection.execute(text("""
                INSERT INTO scm_perfil_empacable (codigo, nombre)
                VALUES ('PEM-TC-M3', 'Perfil relevo concurrente')
                RETURNING id
            """)).scalar_one()
            container_id = connection.execute(text("""
                INSERT INTO scm_tipo_contenedor (
                    codigo, clase, nombre, tara_nominal_g,
                    tolerancia_tara_g, peso_bruto_max_kg
                )
                VALUES (
                    'TCO-TC-M3', 'MANGA', 'Manga relevo concurrente',
                    100, 10, 100
                )
                RETURNING id
            """)).scalar_one()
            rule_id = connection.execute(text("""
                INSERT INTO scm_regla_empaque (
                    perfil_empacable_id, tipo_contenedor_id
                )
                VALUES (:profile_id, :container_id)
                RETURNING id
            """), {
                "profile_id": profile_id,
                "container_id": container_id,
            }).scalar_one()
            rule_revision_id = connection.execute(text("""
                INSERT INTO scm_regla_empaque_revision (
                    regla_id, numero_revision, estado,
                    medicion_fisica_probada, cantidad_objetivo_un,
                    cantidad_maxima_probada_un,
                    peso_neto_operativo_max_kg, margen_seguridad_kg,
                    tolerancia_peso_abs_g, tolerancia_peso_pct,
                    tara_nominal_g_snapshot,
                    tolerancia_tara_g_snapshot,
                    peso_bruto_max_kg_snapshot, content_hash,
                    creada_por_id, aprobada_por_id, aprobada_at
                )
                VALUES (
                    :rule_id, 1, 'APROBADA', true, 100, 100,
                    99, 0, 20, 1, 100, 10, 100, :content_hash,
                    :actor_id, :actor_id, CURRENT_TIMESTAMP
                )
                RETURNING id
            """), {
                "rule_id": rule_id,
                "content_hash": "3" * 64,
                "actor_id": actors["TR-TC-M3-ACTOR"],
            }).scalar_one()
            plan_operation_id = uuid4()
            connection.execute(text("""
                INSERT INTO scm_operacion (
                    operation_id, endpoint, actor_id, request_sha256,
                    estado_http, response_json
                )
                VALUES (
                    :operation_id, '/fixture/m3-plan', :actor_id,
                    :request_hash, 201, CAST('{}' AS JSON)
                )
            """), {
                "operation_id": plan_operation_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
                "request_hash": "4" * 64,
            })
            plan_id = connection.execute(text("""
                INSERT INTO scm_plan_manga_op (
                    orden_operacion_id, revision, estado,
                    calculado_por_id, operation_id, content_hash
                )
                VALUES (
                    :order_id, 1, 'ACTIVO', :actor_id,
                    :operation_id, :content_hash
                )
                RETURNING id
            """), {
                "order_id": order_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
                "operation_id": plan_operation_id,
                "content_hash": "5" * 64,
            }).scalar_one()
            plan_line_id = connection.execute(text("""
                INSERT INTO scm_plan_manga_op_linea (
                    plan_id, orden_operacion_salida_id,
                    lote_articulo_id, perfil_empacable_id,
                    regla_revision_id, tipo_contenedor_id,
                    cantidad_objetivo_un, capacidad_efectiva_un,
                    mangas_propuestas, peso_unitario_snapshot_g,
                    articulo_codigo_snapshot, articulo_nombre_snapshot,
                    color_snapshot, regla_hash_snapshot,
                    tara_nominal_g_snapshot,
                    tolerancia_tara_g_snapshot,
                    peso_bruto_max_kg_snapshot
                )
                VALUES (
                    :plan_id, :output_id, :lot_id, :profile_id,
                    :rule_revision_id, :container_id, 100, 100, 1, 100,
                    'WIP-TC-M3', 'WIP relevo concurrente', 'AZUL',
                    :rule_hash, 100, 10, 100
                )
                RETURNING id
            """), {
                "plan_id": plan_id,
                "output_id": output_id,
                "lot_id": lot_id,
                "profile_id": profile_id,
                "rule_revision_id": rule_revision_id,
                "container_id": container_id,
                "rule_hash": "6" * 64,
            }).scalar_one()

            ot_id = connection.execute(text("""
                INSERT INTO registro_diario_produccion (
                    public_id, codigo_ot, codigo_ot_sintetico, estado,
                    tipo_ot, fecha, turno, maquina_id,
                    cantidad_objetivo, cantidad_confirmada,
                    created_by_id, maquinista_previsto_id,
                    secuencia_siguiente_trabajo, iniciada_at
                )
                VALUES (
                    :public_id, 'OT-TC-M3', false, 'EN_EJECUCION',
                    'FABRICACION', DATE '2026-08-08', 'DIA', :machine_id,
                    100, 0, :actor_id, :actor_id, 2, CURRENT_TIMESTAMP
                )
                RETURNING id
            """), {
                "public_id": uuid4(),
                "machine_id": machine_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
            }).scalar_one()
            connection.execute(text("""
                INSERT INTO scm_trabajo_ot (
                    id, orden_trabajo_id, codigo, tipo, secuencia,
                    estado, orden_operacion_id, cantidad_objetivo_un,
                    cantidad_confirmada_un, iniciada_at, created_by_id
                )
                VALUES (
                    :work_id, :ot_id, 'OT-TC-M3-TC01', 'COLOR', 1,
                    'EN_EJECUCION', :order_id, 100, 0,
                    CURRENT_TIMESTAMP, :actor_id
                )
            """), {
                "work_id": work_id,
                "ot_id": ot_id,
                "order_id": order_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
            })
            connection.execute(text("""
                INSERT INTO scm_trabajo_color (
                    trabajo_ot_id, corrida_fabricacion_id,
                    color_nombre_snapshot
                )
                VALUES (:work_id, :run_id, 'AZUL')
            """), {"work_id": work_id, "run_id": run_id})
            plan_assignment_id = connection.execute(text("""
                INSERT INTO scm_asignacion_plan_manga_ot (
                    plan_linea_id, ot_id, trabajo_ot_id,
                    cantidad_asignada_un, mangas_asignadas,
                    asignada_por_id
                )
                VALUES (
                    :plan_line_id, :ot_id, :work_id, 100, 1, :actor_id
                )
                RETURNING id
            """), {
                "plan_line_id": plan_line_id,
                "ot_id": ot_id,
                "work_id": work_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
            }).scalar_one()
            initial_assignment_id = connection.execute(text("""
                INSERT INTO scm_asignacion_personal_trabajo_ot (
                    id, trabajo_ot_id, trabajador_id, estado,
                    iniciada_at, asignada_por_id, motivo
                )
                VALUES (
                    :assignment_id, :work_id, :actor_id, 'ACTIVA',
                    CURRENT_TIMESTAMP, :actor_id, 'Inicio de turno'
                )
                RETURNING id
            """), {
                "assignment_id": uuid4(),
                "work_id": work_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
            }).scalar_one()
            seeded_manga = connection.execute(text("""
                INSERT INTO scm_manga (
                    public_id, codigo, ot_id, trabajo_ot_id,
                    asignacion_personal_trabajo_id, plan_linea_id,
                    asignacion_id, lote_articulo_id, secuencia_ot,
                    tipo, estado, cantidad_planificada_un,
                    cantidad_asignada_un, maquinista_previsto_id,
                    articulo_codigo_snapshot, articulo_nombre_snapshot,
                    color_snapshot, regla_revision_id_snapshot,
                    regla_hash_snapshot, tipo_contenedor_codigo_snapshot,
                    tipo_contenedor_nombre_snapshot,
                    peso_unitario_snapshot_g, tara_nominal_g_snapshot,
                    tolerancia_tara_g_snapshot,
                    peso_bruto_max_kg_snapshot, created_by_id
                )
                VALUES (
                    :public_id, 'OT-TC-M3-M001', :ot_id, :work_id,
                    :personal_assignment_id, :plan_line_id,
                    :plan_assignment_id, :lot_id, 1, 'NORMAL',
                    'PLANIFICADA', 100, 100, :actor_id,
                    'WIP-TC-M3', 'WIP relevo concurrente', 'AZUL',
                    :rule_revision_id, :rule_hash, 'TCO-TC-M3',
                    'Manga relevo concurrente', 100, 100, 10, 100,
                    :actor_id
                )
                RETURNING id, public_id
            """), {
                "public_id": uuid4(),
                "ot_id": ot_id,
                "work_id": work_id,
                "personal_assignment_id": initial_assignment_id,
                "plan_line_id": plan_line_id,
                "plan_assignment_id": plan_assignment_id,
                "lot_id": lot_id,
                "actor_id": actors["TR-TC-M3-ACTOR"],
                "rule_revision_id": rule_revision_id,
                "rule_hash": "6" * 64,
            }).one()

        payloads = (
            {
                "trabajador_id": actors["TR-TC-M3-REL-A"],
                "motivo": "Relevo concurrente A",
                "version": 1,
            },
            {
                "trabajador_id": actors["TR-TC-M3-REL-B"],
                "motivo": "Relevo concurrente B",
                "version": 1,
            },
        )

        def assign_candidate(candidate):
            operation_id, payload = candidate
            with Session(schema_engine, expire_on_commit=False) as session:
                try:
                    result = assign_color_work_worker(
                        session,
                        actor_id=actors["TR-TC-M3-ACTOR"],
                        work_id=work_id,
                        operation_id=operation_id,
                        data=payload,
                    )
                    return ("assigned", operation_id, payload, result)
                except ScmServiceError as error:
                    return (error.code, operation_id, payload, None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(
                assign_candidate,
                zip(operation_ids, payloads),
            ))

        assert sorted(item[0] for item in outcomes) == [
            "VERSION_CONFLICT",
            "assigned",
        ]
        winner = next(item for item in outcomes if item[0] == "assigned")
        with Session(schema_engine, expire_on_commit=False) as session:
            replay = assign_color_work_worker(
                session,
                actor_id=actors["TR-TC-M3-ACTOR"],
                work_id=work_id,
                operation_id=winner[1],
                data=winner[2],
            )
        assert replay == winner[3]

        with schema_engine.connect() as connection:
            active_index = connection.execute(text("""
                SELECT lower(indexdef)
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'scm_asignacion_personal_trabajo_ot'
                  AND indexname = 'uq_scm_asignacion_personal_activa'
            """)).scalar_one()
            assert "create unique index" in active_index
            assert "where" in active_index
            assert "'activa'" in active_index
            active = connection.execute(text("""
                SELECT id, trabajador_id
                FROM scm_asignacion_personal_trabajo_ot
                WHERE trabajo_ot_id = :work_id
                  AND estado = 'ACTIVA'
            """), {"work_id": work_id}).one()
            assert active.trabajador_id == winner[2]["trabajador_id"]
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_asignacion_personal_trabajo_ot
                WHERE trabajo_ot_id = :work_id
            """), {"work_id": work_id}).scalar_one() == 2
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_asignacion_personal_trabajo_ot
                WHERE trabajo_ot_id = :work_id
                  AND estado = 'CERRADA'
            """), {"work_id": work_id}).scalar_one() == 1
            assert connection.execute(text("""
                SELECT asignacion_personal_trabajo_id
                FROM scm_manga
                WHERE trabajo_ot_id = :work_id
            """), {"work_id": work_id}).scalar_one() == active.id
            assert connection.execute(text("""
                SELECT version
                FROM scm_trabajo_ot
                WHERE id = :work_id
            """), {"work_id": work_id}).scalar_one() == 2
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_operacion
                WHERE operation_id IN (:operation_a, :operation_b)
                  AND response_json IS NOT NULL
            """), {
                "operation_a": operation_ids[0],
                "operation_b": operation_ids[1],
            }).scalar_one() == 1

        if include_weighing_annulment:
            weighing_public_id = uuid4()
            weighing_operation_id = uuid4()
            station_id = str(uuid4())
            with schema_engine.begin() as connection:
                active_assignment_id = connection.execute(text("""
                    SELECT id
                    FROM scm_asignacion_personal_trabajo_ot
                    WHERE trabajo_ot_id = :work_id
                      AND estado = 'ACTIVA'
                """), {"work_id": work_id}).scalar_one()
                connection.execute(text("""
                    UPDATE scm_manga
                    SET estado = 'PESADA',
                        cantidad_confirmada_un = 100,
                        cantidad_contenida_un = 100
                    WHERE id = :manga_id
                """), {"manga_id": seeded_manga.id})
                connection.execute(text("""
                    UPDATE scm_trabajo_ot
                    SET cantidad_confirmada_un = 100
                    WHERE id = :work_id
                """), {"work_id": work_id})
                connection.execute(text("""
                    UPDATE registro_diario_produccion
                    SET secuencia_siguiente_manga = 2
                    WHERE id = :ot_id
                """), {"ot_id": ot_id})
                connection.execute(text("""
                    INSERT INTO estacion_pesaje (
                        station_id, codigo, nombre, ubicacion,
                        estado_admin, token_hash, created_at_utc
                    )
                    VALUES (
                        :station_id, 'PESAJE-TC-M2',
                        'Balanza anulacion concurrente', 'Piloto',
                        'ACTIVA', :token_hash, CURRENT_TIMESTAMP
                    )
                """), {
                    "station_id": station_id,
                    "token_hash": "7" * 64,
                })
                connection.execute(text("""
                    INSERT INTO scm_operacion (
                        operation_id, endpoint, actor_id,
                        request_sha256, estado_http, response_json
                    )
                    VALUES (
                        :operation_id,
                        '/integration/v1/manga-weighings',
                        :actor_id, :request_hash, 201,
                        CAST('{"fixture":"M2-02"}' AS JSON)
                    )
                """), {
                    "operation_id": weighing_operation_id,
                    "actor_id": actors["TR-TC-M3-ACTOR"],
                    "request_hash": "8" * 64,
                })
                weighing_id = connection.execute(text("""
                    INSERT INTO scm_pesaje_manga (
                        public_id, manga_id, operation_id,
                        source_system, station_id, capture_id,
                        peso_bruto_kg, tara_kg, peso_fisico_neto_kg,
                        tara_fuente, cantidad_confirmada,
                        fuente_cantidad, kg_produccion_ot, pesada_at,
                        timezone_snapshot, fecha_local_pesaje,
                        dias_desfase_operativo, alerta_fecha,
                        pesado_por_id,
                        asignacion_personal_trabajo_id,
                        snapshots_json
                    )
                    VALUES (
                        :public_id, :manga_id, :operation_id,
                        'SCM_STATION', :station_id, :capture_id,
                        10.100, 0.100, 10.000, 'TIPO_MANGA', 100,
                        'PLAN_CONFIRMADO_POR_PESAJE', 10,
                        TIMESTAMPTZ '2026-08-08 15:00:00+00',
                        'America/Lima', DATE '2026-08-08', 0, false,
                        :actor_id, :personal_assignment_id,
                        CAST('{"fixture":"M2-02"}' AS JSON)
                    )
                    RETURNING id
                """), {
                    "public_id": weighing_public_id,
                    "manga_id": seeded_manga.id,
                    "operation_id": weighing_operation_id,
                    "station_id": station_id,
                    "capture_id": uuid4(),
                    "actor_id": actors["TR-TC-M3-ACTOR"],
                    "personal_assignment_id": active_assignment_id,
                }).scalar_one()
                original_fact = connection.execute(text("""
                    SELECT
                        public_id, manga_id, operation_id,
                        source_system, station_id, capture_id,
                        peso_bruto_kg, tara_kg, peso_fisico_neto_kg,
                        tara_fuente, cantidad_confirmada,
                        fuente_cantidad, kg_produccion_ot, pesada_at,
                        timezone_snapshot, fecha_local_pesaje,
                        dias_desfase_operativo, alerta_fecha,
                        pesado_por_id,
                        asignacion_personal_trabajo_id,
                        snapshots_json, created_at
                    FROM scm_pesaje_manga
                    WHERE id = :weighing_id
                """), {"weighing_id": weighing_id}).one()

            annul_operation_ids = (uuid4(), uuid4())
            annul_command = {
                "motivo": "Manga descartada en control concurrente M2-02",
            }
            start_barrier = Barrier(2)

            def annul_candidate(operation_id):
                with Session(
                    schema_engine, expire_on_commit=False
                ) as session:
                    start_barrier.wait(timeout=10)
                    try:
                        result = annul_manga_weighing(
                            session,
                            actor_id=actors["TR-TC-M3-ACTOR"],
                            weighing_id=weighing_public_id,
                            operation_id=operation_id,
                            data=annul_command,
                        )
                        return ("annulled", operation_id, result)
                    except ScmServiceError as error:
                        return (error.code, operation_id, None)

            with ThreadPoolExecutor(max_workers=2) as executor:
                annul_outcomes = list(executor.map(
                    annul_candidate,
                    annul_operation_ids,
                ))

            assert sorted(item[0] for item in annul_outcomes) == [
                "WEIGHING_ALREADY_ANNULLED",
                "annulled",
            ]
            annul_winner = next(
                item for item in annul_outcomes
                if item[0] == "annulled"
            )
            assert annul_winner[2]["plan"] == {
                "cantidad_devuelta_un": "100.000",
                "cantidad_asignada_un": "0.000",
                "mangas_asignadas": 0,
            }

            with schema_engine.connect() as connection:
                assert connection.execute(text("""
                    SELECT
                        public_id, manga_id, operation_id,
                        source_system, station_id, capture_id,
                        peso_bruto_kg, tara_kg, peso_fisico_neto_kg,
                        tara_fuente, cantidad_confirmada,
                        fuente_cantidad, kg_produccion_ot, pesada_at,
                        timezone_snapshot, fecha_local_pesaje,
                        dias_desfase_operativo, alerta_fecha,
                        pesado_por_id,
                        asignacion_personal_trabajo_id,
                        snapshots_json, created_at
                    FROM scm_pesaje_manga
                    WHERE id = :weighing_id
                """), {"weighing_id": weighing_id}).one() == original_fact
                assert connection.execute(text("""
                    SELECT cantidad_asignada_un, mangas_asignadas
                    FROM scm_asignacion_plan_manga_ot
                    WHERE id = :assignment_id
                """), {
                    "assignment_id": plan_assignment_id,
                }).one() == (Decimal("0.000"), 0)
                assert connection.execute(text("""
                    SELECT cantidad_objetivo_un, cantidad_confirmada_un
                    FROM scm_trabajo_ot
                    WHERE id = :work_id
                """), {"work_id": work_id}).one() == (
                    Decimal("0.000"), Decimal("0.000"),
                )
                assert connection.execute(text("""
                    SELECT estado
                    FROM scm_manga
                    WHERE id = :manga_id
                """), {
                    "manga_id": seeded_manga.id,
                }).scalar_one() == "ANULADA"
                assert connection.execute(text("""
                    SELECT count(*)
                    FROM scm_pesaje_manga
                    WHERE id = :weighing_id
                """), {"weighing_id": weighing_id}).scalar_one() == 1
                annulment = connection.execute(text("""
                    SELECT operation_id, cantidad_devuelta_plan_un
                    FROM scm_anulacion_pesaje_manga
                    WHERE pesaje_id = :weighing_id
                """), {"weighing_id": weighing_id}).one()
                assert annulment == (
                    annul_winner[1], Decimal("100.000"),
                )
                assert connection.execute(text("""
                    SELECT count(*)
                    FROM scm_evento
                    WHERE aggregate_type = 'PESAJE_MANGA'
                      AND aggregate_id = :aggregate_id
                      AND tipo = 'MANGA_WEIGHING_ANNULLED'
                """), {
                    "aggregate_id": str(weighing_id),
                }).scalar_one() == 1
                completed_annul_operations = connection.execute(text("""
                    SELECT operation_id
                    FROM scm_operacion
                    WHERE operation_id IN (
                        :operation_a, :operation_b
                    )
                      AND response_json IS NOT NULL
                """), {
                    "operation_a": annul_operation_ids[0],
                    "operation_b": annul_operation_ids[1],
                }).scalars().all()
                assert completed_annul_operations == [annul_winner[1]]
                assert connection.execute(text("""
                    SELECT count(*)
                    FROM scm_operacion
                    WHERE operation_id IN (
                        :operation_a, :operation_b
                    )
                """), {
                    "operation_a": annul_operation_ids[0],
                    "operation_b": annul_operation_ids[1],
                }).scalar_one() == 1

            with Session(schema_engine, expire_on_commit=False) as session:
                replacement = add_work_mangas(
                    session,
                    actor_id=actors["TR-TC-M3-ACTOR"],
                    work_id=work_id,
                    operation_id=uuid4(),
                    data={
                        "plan_linea_id": plan_line_id,
                        "cantidad_un": 100,
                    },
                )
            assert len(replacement["mangas"]) == 1
            assert replacement["mangas"][0]["tipo"] == "NORMAL"
            assert replacement["mangas"][0]["estado"] == "PLANIFICADA"

            with schema_engine.connect() as connection:
                assert connection.execute(text("""
                    SELECT count(*)
                    FROM scm_manga
                    WHERE trabajo_ot_id = :work_id
                      AND tipo = 'NORMAL'
                      AND estado = 'ANULADA'
                """), {"work_id": work_id}).scalar_one() == 1
                assert connection.execute(text("""
                    SELECT count(*)
                    FROM scm_manga
                    WHERE trabajo_ot_id = :work_id
                      AND tipo = 'NORMAL'
                      AND estado = 'PLANIFICADA'
                """), {"work_id": work_id}).scalar_one() == 1
                assert connection.execute(text("""
                    SELECT count(*)
                    FROM scm_manga
                    WHERE trabajo_ot_id = :work_id
                      AND tipo = 'NORMAL'
                """), {"work_id": work_id}).scalar_one() == 2
                assert connection.execute(text("""
                    SELECT cantidad_asignada_un, mangas_asignadas
                    FROM scm_asignacion_plan_manga_ot
                    WHERE id = :assignment_id
                """), {
                    "assignment_id": plan_assignment_id,
                }).one() == (Decimal("100.000"), 1)
                assert connection.execute(text("""
                    SELECT cantidad_objetivo_un, cantidad_confirmada_un
                    FROM scm_trabajo_ot
                    WHERE id = :work_id
                """), {"work_id": work_id}).one() == (
                    Decimal("100.000"), Decimal("0.000"),
                )
                assert connection.execute(text("""
                    SELECT
                        public_id, manga_id, operation_id,
                        source_system, station_id, capture_id,
                        peso_bruto_kg, tara_kg, peso_fisico_neto_kg,
                        tara_fuente, cantidad_confirmada,
                        fuente_cantidad, kg_produccion_ot, pesada_at,
                        timezone_snapshot, fecha_local_pesaje,
                        dias_desfase_operativo, alerta_fecha,
                        pesado_por_id,
                        asignacion_personal_trabajo_id,
                        snapshots_json, created_at
                    FROM scm_pesaje_manga
                    WHERE id = :weighing_id
                """), {"weighing_id": weighing_id}).one() == original_fact
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_relevo_concurrente_trabajo_color_conserva_un_responsable_activo():
    _run_color_work_concurrency_scenario(
        include_weighing_annulment=False
    )


def test_anulacion_concurrente_pesaje_devuelve_cupo_una_sola_vez():
    _run_color_work_concurrency_scenario(
        include_weighing_annulment=True
    )


def test_catalog_counter_migration_uses_existing_numeric_suffixes():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        _run_flask_db(schema_url, "upgrade", MOLDE_PIEZA_REVISION)
        with schema_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO linea (id, codigo, nombre)
                VALUES (701, 701, 'Linea correlativos')
            """))
            connection.execute(text("""
                INSERT INTO familia (id, codigo, nombre)
                VALUES (702, 702, 'Familia correlativos')
            """))
            connection.execute(text("""
                INSERT INTO molde (
                    codigo, nombre, peso_tiro_gr, tiempo_ciclo_std, activo
                ) VALUES
                    ('ML-000008', 'Molde correlativo', 100, 20, true),
                    ('MOLDE-LEGACY', 'Molde legacy', 100, 20, true)
            """))
            connection.execute(text("""
                INSERT INTO pieza (
                    codigo, nombre, linea_id, familia_id,
                    peso_nominal_gr, activo, version
                ) VALUES
                    ('PZ-000123', 'Pieza correlativa', 701, 702, 10, true, 1),
                    ('PZ-LEGACY', 'Pieza legacy', 701, 702, 11, true, 1)
            """))
            connection.execute(text("""
                INSERT INTO pieza_color (
                    sku, linea_id, familia_id, tipo, piezas
                ) VALUES
                    ('PC-000055', 701, 702, 'SIMPLE', 'Pieza color correlativa'),
                    ('SKU-LEGACY', 701, 702, 'SIMPLE', 'Pieza color legacy')
            """))
            connection.execute(text("""
                INSERT INTO producto_terminado (
                    cod_sku_pt, linea_id, familia_id, producto
                ) VALUES
                    ('PT-001200', 701, 702, 'Producto correlativo'),
                    ('PT-LEGACY', 701, 702, 'Producto legacy')
            """))

        _run_flask_db(schema_url, "upgrade", CATALOG_COUNTER_REVISION)

        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT clave, siguiente_valor
                FROM correlativo_catalogo
                ORDER BY clave
            """)).tuples().all() == [
                ("MOLDE", 9),
                ("PIEZA", 124),
                ("PIEZA_COLOR", 56),
                ("PRODUCTO_TERMINADO", 1201),
            ]
            assert connection.execute(text("""
                SELECT codigo FROM pieza ORDER BY codigo
            """)).scalars().all() == ["PZ-000123", "PZ-LEGACY"]

        _run_flask_db(schema_url, "downgrade", MOLDE_PIEZA_REVISION)
        assert "correlativo_catalogo" not in inspect(
            schema_engine
        ).get_table_names()
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_legacy_kit_contract_aborta_con_evidencia_y_luego_retira_esquema():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        _run_flask_db(schema_url, "upgrade", "f49b7e5a3d02")
        with schema_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO linea (id, codigo, nombre)
                VALUES (731, 731, 'Linea contract KIT')
            """))
            connection.execute(text("""
                INSERT INTO familia (id, codigo, nombre)
                VALUES (732, 732, 'Familia contract KIT')
            """))
            connection.execute(text("""
                INSERT INTO pieza_color (
                    sku, linea_id, familia_id, tipo, piezas
                ) VALUES (
                    'KIT-CONTRACT-01',
                    731,
                    732,
                    'KIT',
                    'Fixture que debe bloquear'
                )
            """))

        failed = _run_flask_db_failure(
            schema_url,
            "upgrade",
            HEAD_REVISION,
        )
        assert "LEGACY_KIT_PRECONDITION_FAILED" in (
            failed.stdout + failed.stderr
        )
        with schema_engine.begin() as connection:
            connection.execute(text("""
                DELETE FROM pieza_color
                WHERE sku = 'KIT-CONTRACT-01'
            """))

        _run_flask_db(schema_url, "upgrade", HEAD_REVISION)
        inspector = inspect(schema_engine)
        assert "pieza_componente" not in inspector.get_table_names()
        assert "tipo" not in {
            column["name"]
            for column in inspector.get_columns("pieza_color")
        }

        _run_flask_db(schema_url, "downgrade", "f49b7e5a3d02")
        inspector = inspect(schema_engine)
        assert "pieza_componente" in inspector.get_table_names()
        assert "tipo" in {
            column["name"]
            for column in inspector.get_columns("pieza_color")
        }
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_catalog_code_generator_is_unique_under_postgres_concurrency():
    from app.services.catalog_code_generator import generar_codigo_catalogo

    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url, pool_size=12, max_overflow=4)
    try:
        _run_flask_db(schema_url, "upgrade", HEAD_REVISION)
        # Tambien ejercita el upsert concurrente, no solo la fila presembrada
        # por la migracion.
        with schema_engine.begin() as connection:
            connection.execute(text("""
                DELETE FROM correlativo_catalogo WHERE clave = 'PIEZA'
            """))

        def reserve_code(_):
            with Session(schema_engine) as session:
                with session.begin():
                    return generar_codigo_catalogo("PIEZA", session=session)

        with ThreadPoolExecutor(max_workers=12) as executor:
            codes = list(executor.map(reserve_code, range(36)))

        expected = {f"PZ-{number:06d}" for number in range(1, 37)}
        assert set(codes) == expected
        assert len(codes) == len(set(codes))

        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT siguiente_valor
                FROM correlativo_catalogo
                WHERE clave = 'PIEZA'
            """)).scalar_one() == 37
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_article_r1_backfills_catalogs_and_enforces_deferred_subtype():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        _run_flask_db(schema_url, "upgrade", "b7e9f1a4d510")
        with schema_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO linea (
                    id, codigo, nombre, activo, version
                ) VALUES (711, 711, 'Linea R1', true, 1)
            """))
            connection.execute(text("""
                INSERT INTO familia (
                    id, codigo, nombre, activo, version
                ) VALUES (712, 712, 'Familia R1', true, 1)
            """))
            connection.execute(text("""
                INSERT INTO pieza_color (
                    sku, linea_id, familia_id, tipo, piezas
                ) VALUES (
                    'PC-R1-PG-01', 711, 712, 'SIMPLE', 'Asa R1 PG'
                )
            """))
            connection.execute(text("""
                INSERT INTO producto_terminado (
                    cod_sku_pt, linea_id, familia_id, producto
                ) VALUES
                    (
                        'PT-R1-PG-01', 711, 712, 'Balde R1 PG'
                    ),
                    (
                        '', 711, 712, 'Producto legacy sin codigo'
                    )
            """))

        _run_flask_db(schema_url, "upgrade", HEAD_REVISION)

        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT codigo, clase
                FROM scm_articulo
                ORDER BY codigo
            """)).tuples().all() == [
                ("PC-R1-PG-01", "PIEZA_COLOR"),
                ("PT-LEGACY-SIN-CODIGO", "PRODUCTO_TERMINADO"),
                ("PT-R1-PG-01", "PRODUCTO_TERMINADO"),
            ]
            assert connection.execute(text("""
                SELECT count(*) FROM scm_articulo_pieza_color
            """)).scalar_one() == 1
            assert connection.execute(text("""
                SELECT count(*) FROM scm_articulo_producto
            """)).scalar_one() == 2
            assert connection.execute(text("""
                SELECT count(*) FROM trabajador_rol
            """)).scalar_one() == 0
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_rol_capacidad AS relation
                JOIN rol_operativo AS role
                  ON role.id = relation.rol_operativo_id
                JOIN scm_capacidad AS capability
                  ON capability.id = relation.capacidad_id
                WHERE role.codigo = 'MAQUINISTA'
                  AND capability.codigo = 'MANGA_PESAR'
            """)).scalar_one() == 1

        with pytest.raises(DBAPIError) as subtype_error:
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO scm_articulo (
                        public_id, codigo, nombre, clase, unidad_base
                    ) VALUES (
                        :public_id,
                        'WIP-R1-ORPHAN',
                        'WIP sin subtipo',
                        'SUBENSAMBLE_WIP',
                        'UN'
                    )
                """), {"public_id": uuid4()})
        assert "article_subtype_mismatch" in str(
            subtype_error.value.orig
        ).lower()

        with pytest.raises(DBAPIError) as immutable_error:
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    UPDATE scm_articulo
                    SET codigo = 'PC-R1-PG-CHANGED'
                    WHERE codigo = 'PC-R1-PG-01'
                """))
        assert "immutable_article_identity" in str(
            immutable_error.value.orig
        ).lower()

        _run_flask_db(schema_url, "downgrade", "b7e9f1a4d510")
        assert "scm_articulo" not in inspect(
            schema_engine
        ).get_table_names()
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_structure_approval_serializes_concurrent_cycle_and_freezes_lines():
    from app.services.scm_article_service import create_wip_article
    from app.services.scm_service_support import ScmServiceError
    from app.services.scm_structure_service import (
        approve_structure,
        create_structure,
        send_structure_for_approval,
    )

    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(
        schema_url,
        pool_size=6,
        max_overflow=2,
    )
    try:
        _run_flask_db(schema_url, "upgrade", HEAD_REVISION)
        with schema_engine.begin() as connection:
            actors = {
                row.codigo: row.id
                for row in connection.execute(text("""
                    INSERT INTO trabajador (
                        codigo, nombres, apellidos, activo
                    )
                    VALUES
                        ('TRB-R2-CREATOR', 'Creador', 'R2', true),
                        ('TRB-R2-APPROVER', 'Aprobador', 'R2', true)
                    RETURNING id, codigo
                """))
            }
            connection.execute(text("""
                INSERT INTO trabajador_rol (
                    trabajador_id, rol_operativo_id
                )
                SELECT :actor_id, id
                FROM rol_operativo
                WHERE codigo = 'INGENIERIA_SCM'
            """), {"actor_id": actors["TRB-R2-CREATOR"]})
            connection.execute(text("""
                INSERT INTO trabajador_rol (
                    trabajador_id, rol_operativo_id
                )
                SELECT :actor_id, id
                FROM rol_operativo
                WHERE codigo = 'JEFE_PRODUCCION'
            """), {"actor_id": actors["TRB-R2-APPROVER"]})

        with Session(schema_engine, expire_on_commit=False) as session:
            article_a = create_wip_article(
                session,
                actor_id=actors["TRB-R2-CREATOR"],
                data={"nombre": "WIP R2 A"},
            )["id"]
            article_b = create_wip_article(
                session,
                actor_id=actors["TRB-R2-CREATOR"],
                data={"nombre": "WIP R2 B"},
            )["id"]
            revision_a = create_structure(
                session,
                actor_id=actors["TRB-R2-CREATOR"],
                article_id=article_a,
                data={
                    "componentes": [
                        {"articulo_id": article_b, "cantidad": "1"}
                    ]
                },
            )
            revision_a = send_structure_for_approval(
                session,
                actor_id=actors["TRB-R2-CREATOR"],
                structure_id=revision_a["id"],
                operation_id=uuid4(),
                data={"version": revision_a["version"]},
            )
            revision_b = create_structure(
                session,
                actor_id=actors["TRB-R2-CREATOR"],
                article_id=article_b,
                data={
                    "componentes": [
                        {"articulo_id": article_a, "cantidad": "1"}
                    ]
                },
            )
            revision_b = send_structure_for_approval(
                session,
                actor_id=actors["TRB-R2-CREATOR"],
                structure_id=revision_b["id"],
                operation_id=uuid4(),
                data={"version": revision_b["version"]},
            )

        def approve_candidate(candidate):
            with Session(schema_engine, expire_on_commit=False) as session:
                try:
                    result = approve_structure(
                        session,
                        actor_id=actors["TRB-R2-APPROVER"],
                        structure_id=candidate["id"],
                        operation_id=uuid4(),
                        data={"version": candidate["version"]},
                    )
                    return ("approved", result["id"])
                except ScmServiceError as error:
                    return (error.code, candidate["id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(
                approve_candidate,
                (revision_a, revision_b),
            ))

        assert sorted(item[0] for item in outcomes) == [
            "STRUCTURE_CYCLE",
            "approved",
        ]
        approved_id = next(
            item[1] for item in outcomes if item[0] == "approved"
        )
        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_estructura_revision
                WHERE estado = 'APROBADA'
            """)).scalar_one() == 1
            component_id = connection.execute(text("""
                SELECT id
                FROM scm_estructura_componente
                WHERE revision_id = :revision_id
            """), {"revision_id": approved_id}).scalar_one()

        with pytest.raises(DBAPIError) as immutable_error:
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    UPDATE scm_estructura_componente
                    SET cantidad = 2
                    WHERE id = :component_id
                """), {"component_id": component_id})
        assert "structure_immutable" in str(
            immutable_error.value.orig
        ).lower()
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_route_approval_uses_cte_dag_and_freezes_published_operations():
    from app.services.scm_route_service import (
        approve_route,
        create_route,
        create_work_center,
        update_route,
    )
    from app.services.scm_service_support import ScmServiceError

    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        _run_flask_db(schema_url, "upgrade", HEAD_REVISION)
        with schema_engine.begin() as connection:
            actors = {
                row.codigo: row.id
                for row in connection.execute(text("""
                    INSERT INTO trabajador (
                        codigo, nombres, apellidos, activo
                    )
                    VALUES
                        ('TRB-R3-CREATOR', 'Creador', 'R3', true),
                        ('TRB-R3-APPROVER', 'Aprobador', 'R3', true)
                    RETURNING id, codigo
                """))
            }
            connection.execute(text("""
                INSERT INTO trabajador_rol (
                    trabajador_id, rol_operativo_id
                )
                SELECT :actor_id, id
                FROM rol_operativo
                WHERE codigo = 'INGENIERIA_SCM'
            """), {"actor_id": actors["TRB-R3-CREATOR"]})
            connection.execute(text("""
                INSERT INTO trabajador_rol (
                    trabajador_id, rol_operativo_id
                )
                SELECT :actor_id, id
                FROM rol_operativo
                WHERE codigo = 'JEFE_PRODUCCION'
            """), {"actor_id": actors["TRB-R3-APPROVER"]})
            connection.execute(text("""
                INSERT INTO linea (
                    id, codigo, nombre, activo, version
                ) VALUES (731, 731, 'Linea R3', true, 1)
            """))
            connection.execute(text("""
                INSERT INTO familia (
                    id, codigo, nombre, activo, version
                ) VALUES (732, 732, 'Familia R3', true, 1)
            """))
            connection.execute(text("""
                INSERT INTO producto_terminado (
                    cod_sku_pt, linea_id, familia_id, producto
                ) VALUES (
                    'PT-R3-PG-01', 731, 732, 'Producto R3 PG'
                )
            """))
            target_id = connection.execute(text("""
                INSERT INTO scm_articulo (
                    public_id, codigo, nombre, clase, unidad_base
                ) VALUES (
                    :public_id,
                    'PT-R3-PG-01',
                    'Producto R3 PG',
                    'PRODUCTO_TERMINADO',
                    'UN'
                )
                RETURNING id
            """), {"public_id": uuid4()}).scalar_one()
            connection.execute(text("""
                INSERT INTO scm_articulo_producto (
                    articulo_id, producto_terminado_id
                ) VALUES (:article_id, 'PT-R3-PG-01')
            """), {"article_id": target_id})

        with Session(schema_engine, expire_on_commit=False) as session:
            center = create_work_center(
                session,
                actor_id=actors["TRB-R3-CREATOR"],
                data={
                    "nombre": "Centro R3 PG",
                    "tipo": "INYECCION",
                },
            )
            operation = lambda key, sequence: {
                "clave": key,
                "secuencia_visible": sequence,
                "nombre": f"Operacion {key}",
                "tipo": "INYECCION",
                "executor_kind": "OP_OT",
                "centro_trabajo_id": center["id"],
                "articulo_salida_id": target_id,
                "permite_concurrente": False,
            }
            route = create_route(
                session,
                actor_id=actors["TRB-R3-CREATOR"],
                product_id="PT-R3-PG-01",
                data={
                    "operaciones": [
                        operation("A", 10),
                        operation("B", 20),
                        operation("C", 30),
                    ],
                    "precedencias": [
                        {
                            "anterior_clave": "A",
                            "siguiente_clave": "B",
                        },
                        {
                            "anterior_clave": "B",
                            "siguiente_clave": "C",
                        },
                        {
                            "anterior_clave": "C",
                            "siguiente_clave": "A",
                        },
                    ],
                },
            )
            with pytest.raises(ScmServiceError) as cycle_error:
                approve_route(
                    session,
                    actor_id=actors["TRB-R3-APPROVER"],
                    route_id=route["id"],
                    operation_id=uuid4(),
                    data={"version": route["version"]},
                )
            assert cycle_error.value.code == "ROUTE_CYCLE"

            route = update_route(
                session,
                actor_id=actors["TRB-R3-CREATOR"],
                route_id=route["id"],
                data={
                    "version": route["version"],
                    "operaciones": [operation("FINAL", 10)],
                    "precedencias": [],
                },
            )
            route = approve_route(
                session,
                actor_id=actors["TRB-R3-APPROVER"],
                route_id=route["id"],
                operation_id=uuid4(),
                data={"version": route["version"]},
            )
            operation_id = route["operaciones"][0]["id"]

        with pytest.raises(DBAPIError) as immutable_error:
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    UPDATE scm_operacion_ruta
                    SET nombre = 'Mutacion SQL'
                    WHERE id = :operation_id
                """), {"operation_id": operation_id})
        assert "route_immutable" in str(
            immutable_error.value.orig
        ).lower()
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_packaging_rule_snapshots_survive_master_change_and_sql_is_blocked():
    from app.services.scm_packaging_service import (
        approve_packaging_rule,
        calculate_packaging_plan,
        create_container_type,
        create_packable_profile,
        create_packaging_rule,
        update_container_type,
    )

    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        _run_flask_db(schema_url, "upgrade", HEAD_REVISION)
        with schema_engine.begin() as connection:
            actors = {
                row.codigo: row.id
                for row in connection.execute(text("""
                    INSERT INTO trabajador (
                        codigo, nombres, apellidos, activo
                    )
                    VALUES
                        ('TRB-R4-CREATOR', 'Creador', 'R4', true),
                        ('TRB-R4-APPROVER', 'Aprobador', 'R4', true)
                    RETURNING id, codigo
                """))
            }
            connection.execute(text("""
                INSERT INTO trabajador_rol (
                    trabajador_id, rol_operativo_id
                )
                SELECT :actor_id, id
                FROM rol_operativo
                WHERE codigo = 'INGENIERIA_SCM'
            """), {"actor_id": actors["TRB-R4-CREATOR"]})
            connection.execute(text("""
                INSERT INTO trabajador_rol (
                    trabajador_id, rol_operativo_id
                )
                SELECT :actor_id, id
                FROM rol_operativo
                WHERE codigo = 'JEFE_PRODUCCION'
            """), {"actor_id": actors["TRB-R4-APPROVER"]})

        with Session(schema_engine, expire_on_commit=False) as session:
            container = create_container_type(
                session,
                actor_id=actors["TRB-R4-CREATOR"],
                data={
                    "clase": "MANGA",
                    "nombre": "Manga R4 PG",
                    "tara_nominal_g": "1000",
                    "tolerancia_tara_g": "0",
                    "peso_bruto_max_kg": "10",
                },
            )
            profile = create_packable_profile(
                session,
                actor_id=actors["TRB-R4-CREATOR"],
                data={
                    "nombre": "Perfil R4 PG",
                    "descripcion_fisica": "Prueba PostgreSQL",
                },
            )
            rule = create_packaging_rule(
                session,
                actor_id=actors["TRB-R4-CREATOR"],
                data={
                    "perfil_empacable_id": profile["id"],
                    "tipo_contenedor_id": container["id"],
                    "medicion_fisica_probada": True,
                    "cantidad_objetivo_un": 20,
                    "cantidad_maxima_probada_un": 20,
                    "peso_neto_operativo_max_kg": "20",
                    "margen_seguridad_kg": "0",
                    "tolerancia_peso_abs_g": "0",
                    "tolerancia_peso_pct": "0",
                },
            )
            approved = approve_packaging_rule(
                session,
                actor_id=actors["TRB-R4-APPROVER"],
                revision_id=rule["revision_id"],
                operation_id=uuid4(),
                data={"version": rule["version"]},
            )
            assert approved["tara_nominal_g_snapshot"] == "1000.000"

            update_container_type(
                session,
                actor_id=actors["TRB-R4-CREATOR"],
                container_id=container["id"],
                data={
                    "version": container["version"],
                    "tara_nominal_g": "0",
                },
            )
            plan = calculate_packaging_plan(
                session,
                actor_id=actors["TRB-R4-CREATOR"],
                data={
                    "regla_revision_id": approved["revision_id"],
                    "cantidad_planificada_un": 20,
                    "peso_unitario_snapshot_g": "1000",
                },
            )
            assert plan["capacidad_efectiva_un"] == 9

        with pytest.raises(DBAPIError) as immutable_error:
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    UPDATE scm_regla_empaque_revision
                    SET cantidad_objetivo_un = 8
                    WHERE id = :revision_id
                """), {"revision_id": approved["revision_id"]})
        assert "packaging_rule_immutable" in str(
            immutable_error.value.orig
        ).lower()
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_linea_familia_migration_backfills_used_pairs_and_constraints():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        _run_flask_db(schema_url, "upgrade", CATALOG_COUNTER_REVISION)
        with schema_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO linea (id, codigo, nombre) VALUES
                    (801, 801, 'Linea hogar migracion'),
                    (802, 802, 'Linea industrial migracion')
            """))
            connection.execute(text("""
                INSERT INTO familia (id, codigo, nombre) VALUES
                    (901, 901, 'Familia baldes migracion'),
                    (902, 902, 'Familia tapas migracion'),
                    (903, 903, 'Familia jarras migracion')
            """))
            connection.execute(text("""
                INSERT INTO producto_terminado (
                    cod_sku_pt, linea_id, familia_id, producto
                ) VALUES
                    ('PT-LF-01', 801, 901, 'Producto linea familia'),
                    ('PT-LF-02', 801, 902, 'Producto linea familia 2')
            """))
            connection.execute(text("""
                INSERT INTO pieza_color (
                    sku, linea_id, familia_id, tipo, piezas
                ) VALUES
                    ('PC-LF-01', 801, 901, 'SIMPLE', 'Pieza repetida'),
                    ('PC-LF-02', 802, 902, 'SIMPLE', 'Pieza industrial')
            """))
            connection.execute(text("""
                INSERT INTO pieza (
                    codigo, nombre, linea_id, familia_id,
                    peso_nominal_gr, activo, version
                ) VALUES
                    ('PZ-LF-01', 'Pieza clasificada', 802, 903, 15, true, 1),
                    ('PZ-LF-02', 'Pieza sin clasificar', NULL, NULL, 10, true, 1),
                    ('PZ-LF-03', 'Pieza parcial legacy', 801, NULL, 11, true, 1)
            """))

        _run_flask_db(schema_url, "upgrade", LINEA_FAMILIA_REVISION)

        inspector = inspect(schema_engine)
        assert {
            "ix_linea_familia_linea_activo",
            "ix_linea_familia_familia_activo",
        } <= {
            item["name"]
            for item in inspector.get_indexes("linea_familia")
        }
        assert {
            "fk_linea_familia_linea",
            "fk_linea_familia_familia",
        } == {
            item["name"]
            for item in inspector.get_foreign_keys("linea_familia")
        }
        assert all(
            item["options"].get("ondelete") == "RESTRICT"
            for item in inspector.get_foreign_keys("linea_familia")
        )

        with schema_engine.begin() as connection:
            assert connection.execute(text("""
                SELECT linea_id, familia_id, activo, version
                FROM linea_familia
                ORDER BY linea_id, familia_id
            """)).tuples().all() == [
                (801, 901, True, 1),
                (801, 902, True, 1),
                (802, 902, True, 1),
                (802, 903, True, 1),
            ]
            assert connection.execute(text("""
                SELECT activo, version FROM linea WHERE id = 801
            """)).one() == (True, 1)
            assert connection.execute(text("""
                SELECT activo, version FROM familia WHERE id = 901
            """)).one() == (True, 1)

            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO linea_familia (linea_id, familia_id)
                        VALUES (801, 901)
                    """))
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO linea_familia
                            (linea_id, familia_id, version)
                        VALUES (801, 903, 0)
                    """))
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO linea_familia (linea_id, familia_id)
                        VALUES (999999, 903)
                    """))

        _run_flask_db(schema_url, "downgrade", CATALOG_COUNTER_REVISION)
        assert "linea_familia" not in inspect(schema_engine).get_table_names()
        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT count(*) FROM producto_terminado
                WHERE cod_sku_pt LIKE 'PT-LF-%'
            """)).scalar_one() == 2
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_molde_pieza_migration_preserva_identidades_y_bloquea_downgrade_nm():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        _run_flask_db(schema_url, "upgrade", RECEPTION_DRAFT_REVISION)
        with schema_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO linea (id, codigo, nombre)
                VALUES (501, 501, 'Linea migracion')
            """))
            connection.execute(text("""
                INSERT INTO familia (id, codigo, nombre)
                VALUES (601, 601, 'Familia migracion')
            """))
            connection.execute(text("""
                INSERT INTO molde (
                    codigo, nombre, peso_tiro_gr, tiempo_ciclo_std, activo
                ) VALUES
                    ('M-MIG-A', 'Molde migracion A', 100.0, 20.0, true),
                    ('M-MIG-B', 'Molde migracion B', 110.0, 21.0, true)
            """))
            connection.execute(text("""
                INSERT INTO pieza (
                    id, molde_id, nombre, linea_id, familia_id,
                    cavidades, peso_unitario_gr
                ) VALUES
                    (41, 'M-MIG-A', 'Tapa migrada', 501, 601, 2, 12.5),
                    (105, 'M-MIG-B', 'Base migrada', 501, 601, 4, 18.75)
            """))
            connection.execute(text("""
                INSERT INTO pieza_color (
                    sku, linea_id, familia_id, tipo, pieza_id,
                    piezas, cavidad, peso
                ) VALUES
                    ('SKU-MIG-041', 501, 601, 'INYECTADO', 41,
                     'Tapa migrada azul', 2, 12.5),
                    ('SKU-MIG-105', 501, 601, 'INYECTADO', 105,
                     'Base migrada roja', 4, 18.75)
            """))

        _run_flask_db(schema_url, "upgrade", MOLDE_PIEZA_REVISION)

        inspector = inspect(schema_engine)
        pieza_columns = {
            item["name"]
            for item in inspector.get_columns("pieza")
        }
        assert {"codigo", "peso_nominal_gr", "activo", "version"} <= (
            pieza_columns
        )
        assert {"molde_id", "cavidades", "peso_unitario_gr"}.isdisjoint(
            pieza_columns
        )

        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == MOLDE_PIEZA_REVISION
            assert connection.execute(text("""
                SELECT id, codigo, peso_nominal_gr, activo, version
                FROM pieza
                ORDER BY id
            """)).tuples().all() == [
                (41, "PZ-00000041", 12.5, True, 1),
                (105, "PZ-00000105", 18.75, True, 1),
            ]
            assert connection.execute(text("""
                SELECT id, molde_id, pieza_id, cavidades,
                       peso_unitario_gr, activo, version
                FROM molde_pieza
                ORDER BY id
            """)).tuples().all() == [
                (41, "M-MIG-A", 41, 2, 12.5, True, 1),
                (105, "M-MIG-B", 105, 4, 18.75, True, 1),
            ]
            assert connection.execute(text("""
                SELECT sku, pieza_id
                FROM pieza_color
                ORDER BY sku
            """)).tuples().all() == [
                ("SKU-MIG-041", 41),
                ("SKU-MIG-105", 105),
            ]

        with schema_engine.begin() as connection:
            extra_relation_id = connection.execute(text("""
                INSERT INTO molde_pieza (
                    molde_id, pieza_id, cavidades, peso_unitario_gr
                ) VALUES ('M-MIG-B', 41, 1, 12.75)
                RETURNING id
            """)).scalar_one()
        assert extra_relation_id > 105

        failed = _run_flask_db_failure(
            schema_url,
            "downgrade",
            RECEPTION_DRAFT_REVISION,
        )
        assert "sin exactamente un molde" in (
            failed.stdout + failed.stderr
        ).lower()
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == MOLDE_PIEZA_REVISION
            assert connection.execute(
                text("SELECT count(*) FROM molde_pieza")
            ).scalar_one() == 3

        with schema_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM molde_pieza WHERE id = :relation_id"),
                {"relation_id": extra_relation_id},
            )

        _run_flask_db(schema_url, "downgrade", RECEPTION_DRAFT_REVISION)
        assert "molde_pieza" not in inspect(schema_engine).get_table_names()
        legacy_piece_columns = {
            item["name"]
            for item in inspect(schema_engine).get_columns("pieza")
        }
        assert {"molde_id", "cavidades", "peso_unitario_gr"} <= (
            legacy_piece_columns
        )
        assert {"codigo", "peso_nominal_gr", "activo", "version"}.isdisjoint(
            legacy_piece_columns
        )
        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT id, molde_id, cavidades, peso_unitario_gr
                FROM pieza
                ORDER BY id
            """)).tuples().all() == [
                (41, "M-MIG-A", 2, 12.5),
                (105, "M-MIG-B", 4, 18.75),
            ]
            assert connection.execute(text("""
                SELECT sku, pieza_id
                FROM pieza_color
                ORDER BY sku
            """)).tuples().all() == [
                ("SKU-MIG-041", 41),
                ("SKU-MIG-105", 105),
            ]

        _run_flask_db(schema_url, "upgrade", "head")
        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT id, pieza_id
                FROM molde_pieza
                ORDER BY id
            """)).tuples().all() == [(41, 41), (105, 105)]
            assert connection.execute(text("""
                SELECT sku, pieza_id
                FROM pieza_color
                ORDER BY sku
            """)).tuples().all() == [
                ("SKU-MIG-041", 41),
                ("SKU-MIG-105", 105),
            ]
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_purchase_revision_restringe_datos_y_bloquea_downgrade_con_datos():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)

    def assert_integrity_error(statement, parameters=None):
        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(text(statement), parameters or {})

    try:
        _run_flask_db(schema_url, "upgrade", "head")
        inspector = inspect(schema_engine)

        quantity_column = {
            item["name"]: item
            for item in inspector.get_columns("scm_orden_compra_linea")
        }["cantidad_autorizada_kg"]
        assert quantity_column["type"].precision == 15
        assert quantity_column["type"].scale == 3

        operation_column = {
            item["name"]: item
            for item in inspector.get_columns("scm_operacion")
        }["operation_id"]
        assert str(operation_column["type"]).upper() == "UUID"

        provider_uniques = {
            item["name"]
            for item in inspector.get_unique_constraints("scm_proveedor")
        }
        assert {
            "uq_scm_proveedor_codigo",
            "uq_scm_proveedor_ruc",
        } <= provider_uniques
        assert {
            item["name"]
            for item in inspector.get_unique_constraints(
                "scm_orden_compra_revision"
            )
        } >= {"uq_scm_orden_compra_revision_orden_numero"}
        assert {
            item["name"]
            for item in inspector.get_unique_constraints(
                "scm_orden_compra_linea"
            )
        } >= {"uq_scm_orden_compra_linea_revision_numero"}

        with schema_engine.connect() as connection:
            partial_indexes = dict(connection.execute(text("""
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'scm_orden_compra_revision'
                  AND indexname IN (
                      'ux_scm_oc_revision_abierta_orden',
                      'ux_scm_oc_revision_aprobada_orden'
                  )
            """)).tuples().all())
        assert set(partial_indexes) == {
            "ux_scm_oc_revision_abierta_orden",
            "ux_scm_oc_revision_aprobada_orden",
        }
        assert all(
            "CREATE UNIQUE INDEX" in definition
            and " WHERE " in definition
            for definition in partial_indexes.values()
        )
        assert "BORRADOR" in partial_indexes[
            "ux_scm_oc_revision_abierta_orden"
        ]
        assert "PENDIENTE_APROBACION" in partial_indexes[
            "ux_scm_oc_revision_abierta_orden"
        ]
        assert "APROBADA" in partial_indexes[
            "ux_scm_oc_revision_aprobada_orden"
        ]

        operation_id = uuid4()
        with schema_engine.begin() as connection:
            actors = {
                row.codigo: row.id
                for row in connection.execute(text("""
                    INSERT INTO trabajador (
                        codigo,
                        nombres,
                        apellidos,
                        activo
                    )
                    VALUES
                        ('TRB-COM-PG', 'Maria', 'Compras', true),
                        ('TRB-GER-PG', 'Gerencia', 'Planta', true)
                    RETURNING id, codigo
                """))
            }
            material_id = connection.execute(text("""
                INSERT INTO scm_material (
                    codigo,
                    nombre,
                    clase,
                    categoria_recepcion_id,
                    unidad_base,
                    activo,
                    version
                )
                SELECT
                    'MP-PG-0001',
                    'PP virgen PostgreSQL',
                    'MATERIA_PRIMA',
                    id,
                    'KG',
                    true,
                    1
                FROM scm_categoria_recepcion
                WHERE codigo = 'RESINA_VIRGEN'
                RETURNING id
            """)).scalar_one()
            provider_id = connection.execute(text("""
                INSERT INTO scm_proveedor (
                    codigo,
                    razon_social,
                    ruc
                )
                VALUES (
                    'PROV-PG-0001',
                    'Proveedor PostgreSQL',
                    '20524360366'
                )
                RETURNING id
            """)).scalar_one()
            order_id = connection.execute(
                text("""
                    INSERT INTO scm_orden_compra (
                        codigo,
                        proveedor_id
                    )
                    VALUES ('OCM-PG-0001', :provider_id)
                    RETURNING id
                """),
                {"provider_id": provider_id},
            ).scalar_one()
            open_revision_id = connection.execute(
                text("""
                    INSERT INTO scm_orden_compra_revision (
                        orden_id,
                        numero,
                        estado,
                        creada_por_id
                    )
                    VALUES (
                        :order_id,
                        1,
                        'BORRADOR',
                        :creator_id
                    )
                    RETURNING id
                """),
                {
                    "order_id": order_id,
                    "creator_id": actors["TRB-COM-PG"],
                },
            ).scalar_one()
            connection.execute(
                text("""
                    INSERT INTO scm_orden_compra_linea (
                        revision_id,
                        numero_linea,
                        material_id,
                        cantidad_autorizada_kg
                    )
                    VALUES (
                        :revision_id,
                        1,
                        :material_id,
                        1250.000
                    )
                """),
                {
                    "revision_id": open_revision_id,
                    "material_id": material_id,
                },
            )
            connection.execute(
                text("""
                    INSERT INTO scm_operacion (
                        operation_id,
                        endpoint,
                        actor_id,
                        request_sha256,
                        estado_http
                    )
                    VALUES (
                        :operation_id,
                        '/api/scm/v1/ordenes-compra-material/1/aprobar',
                        :actor_id,
                        :request_sha256,
                        200
                    )
                """),
                {
                    "operation_id": operation_id,
                    "actor_id": actors["TRB-GER-PG"],
                    "request_sha256": "a" * 64,
                },
            )
            event_id = connection.execute(
                text("""
                    INSERT INTO scm_evento (
                        aggregate_type,
                        aggregate_id,
                        tipo,
                        actor_id,
                        actor_snapshot,
                        operation_id
                    )
                    VALUES (
                        'ORDEN_COMPRA_MATERIAL',
                        :aggregate_id,
                        'OC_APROBADA',
                        :actor_id,
                        CAST(
                            '{"codigo":"TRB-GER-PG",'
                            '"nombre":"Gerencia Planta"}' AS JSON
                        ),
                        :operation_id
                    )
                    RETURNING id
                """),
                {
                    "aggregate_id": order_id,
                    "actor_id": actors["TRB-GER-PG"],
                    "operation_id": operation_id,
                },
            ).scalar_one()

        with schema_engine.connect() as connection:
            quantity = connection.execute(text("""
                SELECT cantidad_autorizada_kg
                FROM scm_orden_compra_linea
                WHERE revision_id = :revision_id
                  AND numero_linea = 1
            """), {"revision_id": open_revision_id}).scalar_one()
        assert quantity == Decimal("1250.000")
        assert str(quantity) == "1250.000"

        for invalid_quantity in ("0.000", "-0.001"):
            assert_integrity_error(
                """
                    INSERT INTO scm_orden_compra_linea (
                        revision_id,
                        numero_linea,
                        material_id,
                        cantidad_autorizada_kg
                    )
                    VALUES (
                        :revision_id,
                        2,
                        :material_id,
                        :quantity
                    )
                """,
                {
                    "revision_id": open_revision_id,
                    "material_id": material_id,
                    "quantity": invalid_quantity,
                },
            )

        assert_integrity_error(
            """
                INSERT INTO scm_orden_compra_linea (
                    revision_id,
                    numero_linea,
                    material_id,
                    cantidad_autorizada_kg
                )
                VALUES (:revision_id, 1, :material_id, 1.000)
            """,
            {
                "revision_id": open_revision_id,
                "material_id": material_id,
            },
        )
        assert_integrity_error("""
            INSERT INTO scm_proveedor (codigo, razon_social, ruc)
            VALUES ('PROV-PG-0001', 'Código duplicado', '20524360367')
        """)
        assert_integrity_error("""
            INSERT INTO scm_proveedor (codigo, razon_social, ruc)
            VALUES ('PROV-PG-0002', 'RUC duplicado', '20524360366')
        """)
        assert_integrity_error("""
            INSERT INTO scm_proveedor (codigo, razon_social, ruc)
            VALUES ('PROV-PG-0003', 'RUC corto', '123')
        """)
        assert_integrity_error(
            """
                INSERT INTO scm_operacion (
                    operation_id,
                    endpoint,
                    actor_id,
                    request_sha256
                )
                VALUES (
                    :operation_id,
                    '/api/scm/v1/replay-conflictivo',
                    :actor_id,
                    :request_sha256
                )
            """,
            {
                "operation_id": operation_id,
                "actor_id": actors["TRB-GER-PG"],
                "request_sha256": "b" * 64,
            },
        )

        assert_integrity_error(
            """
                INSERT INTO scm_orden_compra_revision (
                    orden_id,
                    numero,
                    estado,
                    creada_por_id,
                    enviada_at,
                    aprobada_por_id,
                    aprobada_at
                )
                VALUES (
                    :order_id,
                    2,
                    'APROBADA',
                    :creator_id,
                    CURRENT_TIMESTAMP,
                    :creator_id,
                    CURRENT_TIMESTAMP
                )
            """,
            {
                "order_id": order_id,
                "creator_id": actors["TRB-COM-PG"],
            },
        )

        with schema_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO scm_orden_compra_revision (
                        orden_id,
                        numero,
                        estado,
                        creada_por_id,
                        enviada_at,
                        aprobada_por_id,
                        aprobada_at
                    )
                    VALUES (
                        :order_id,
                        2,
                        'APROBADA',
                        :creator_id,
                        CURRENT_TIMESTAMP,
                        :approver_id,
                        CURRENT_TIMESTAMP
                    )
                """),
                {
                    "order_id": order_id,
                    "creator_id": actors["TRB-COM-PG"],
                    "approver_id": actors["TRB-GER-PG"],
                },
            )

        assert_integrity_error(
            """
                INSERT INTO scm_orden_compra_revision (
                    orden_id,
                    numero,
                    estado,
                    creada_por_id,
                    enviada_at
                )
                VALUES (
                    :order_id,
                    3,
                    'PENDIENTE_APROBACION',
                    :creator_id,
                    CURRENT_TIMESTAMP
                )
            """,
            {
                "order_id": order_id,
                "creator_id": actors["TRB-COM-PG"],
            },
        )
        assert_integrity_error(
            """
                INSERT INTO scm_orden_compra_revision (
                    orden_id,
                    numero,
                    estado,
                    creada_por_id,
                    enviada_at,
                    aprobada_por_id,
                    aprobada_at
                )
                VALUES (
                    :order_id,
                    4,
                    'APROBADA',
                    :creator_id,
                    CURRENT_TIMESTAMP,
                    :approver_id,
                    CURRENT_TIMESTAMP
                )
            """,
            {
                "order_id": order_id,
                "creator_id": actors["TRB-COM-PG"],
                "approver_id": actors["TRB-GER-PG"],
            },
        )
        assert_integrity_error(
            """
                INSERT INTO scm_orden_compra_revision (
                    orden_id,
                    numero,
                    estado,
                    creada_por_id
                )
                VALUES (
                    :order_id,
                    2,
                    'RECHAZADA',
                    :creator_id
                )
            """,
            {
                "order_id": order_id,
                "creator_id": actors["TRB-COM-PG"],
            },
        )

        with schema_engine.connect() as connection:
            revision_states = dict(connection.execute(
                text("""
                    SELECT estado, count(*)
                    FROM scm_orden_compra_revision
                    GROUP BY estado
                """)
            ).tuples().all())
        assert revision_states == {"APROBADA": 1, "BORRADOR": 1}

        with schema_engine.begin() as connection:
            immutable_order_id = connection.execute(
                text("""
                    INSERT INTO scm_orden_compra (codigo, proveedor_id)
                    VALUES ('OCM-PG-INMUTABLE', :provider_id)
                    RETURNING id
                """),
                {"provider_id": provider_id},
            ).scalar_one()
            immutable_revision_id = connection.execute(
                text("""
                    INSERT INTO scm_orden_compra_revision (
                        orden_id,
                        numero,
                        estado,
                        creada_por_id
                    )
                    VALUES (:order_id, 1, 'BORRADOR', :creator_id)
                    RETURNING id
                """),
                {
                    "order_id": immutable_order_id,
                    "creator_id": actors["TRB-COM-PG"],
                },
            ).scalar_one()
            connection.execute(
                text("""
                    INSERT INTO scm_orden_compra_linea (
                        revision_id,
                        numero_linea,
                        material_id,
                        cantidad_autorizada_kg
                    )
                    VALUES (:revision_id, 1, :material_id, 10.000)
                """),
                {
                    "revision_id": immutable_revision_id,
                    "material_id": material_id,
                },
            )
            connection.execute(
                text("""
                    UPDATE scm_orden_compra_revision
                    SET estado = 'APROBADA',
                        enviada_at = CURRENT_TIMESTAMP,
                        aprobada_por_id = :approver_id,
                        aprobada_at = CURRENT_TIMESTAMP
                    WHERE id = :revision_id
                """),
                {
                    "revision_id": immutable_revision_id,
                    "approver_id": actors["TRB-GER-PG"],
                },
            )

        immutable_line_mutations = (
            """
                UPDATE scm_orden_compra_linea
                SET cantidad_autorizada_kg = 11.000
                WHERE revision_id = :revision_id
            """,
            """
                DELETE FROM scm_orden_compra_linea
                WHERE revision_id = :revision_id
            """,
            """
                INSERT INTO scm_orden_compra_linea (
                    revision_id,
                    numero_linea,
                    material_id,
                    cantidad_autorizada_kg
                )
                VALUES (:revision_id, 2, :material_id, 1.000)
            """,
        )
        for statement in immutable_line_mutations:
            with pytest.raises(DBAPIError) as immutable_error:
                with schema_engine.begin() as connection:
                    connection.execute(
                        text(statement),
                        {
                            "revision_id": immutable_revision_id,
                            "material_id": material_id,
                        },
                    )
            assert "revision" in str(immutable_error.value.orig).lower()

        with schema_engine.connect() as connection:
            immutable_quantity = connection.execute(
                text("""
                    SELECT cantidad_autorizada_kg
                    FROM scm_orden_compra_linea
                    WHERE revision_id = :revision_id
                """),
                {"revision_id": immutable_revision_id},
            ).scalar_one()
        assert immutable_quantity == Decimal("10.000")

        with pytest.raises(DBAPIError) as update_error:
            with schema_engine.begin() as connection:
                connection.execute(
                    text("""
                        UPDATE scm_evento
                        SET motivo = 'Mutación prohibida'
                        WHERE id = :event_id
                    """),
                    {"event_id": event_id},
                )
        assert "append-only" in str(update_error.value.orig).lower()

        with pytest.raises(DBAPIError) as delete_error:
            with schema_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM scm_evento WHERE id = :event_id"),
                    {"event_id": event_id},
                )
        assert "append-only" in str(delete_error.value.orig).lower()

        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM scm_evento")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT motivo FROM scm_evento WHERE id = :event_id"),
                {"event_id": event_id},
            ).scalar_one_or_none() is None

        _run_flask_db(schema_url, "check")
        failed_downgrade = _run_flask_db_failure(
            schema_url,
            "downgrade",
            CONTRACT_REVISION,
        )
        assert "downgrade destructivo bloqueado" in (
            failed_downgrade.stdout + failed_downgrade.stderr
        ).lower()

        expected_purchase_tables = {
            "scm_evento",
            "scm_operacion",
            "scm_orden_compra",
            "scm_orden_compra_linea",
            "scm_orden_compra_revision",
            "scm_proveedor",
        }
        assert expected_purchase_tables <= set(
            inspect(schema_engine).get_table_names()
        )
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
            assert connection.execute(
                text("SELECT count(*) FROM scm_proveedor")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT count(*) FROM scm_operacion")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT count(*) FROM scm_evento")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT count(*) FROM scm_orden_compra")
            ).scalar_one() == 2
            assert connection.execute(
                text("SELECT count(*) FROM scm_orden_compra_revision")
            ).scalar_one() == 3
            assert connection.execute(
                text("SELECT count(*) FROM scm_orden_compra_linea")
            ).scalar_one() == 2
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_reception_draft_permite_documento_compartido_y_protege_detalle_confirmado():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        _run_flask_db(schema_url, "upgrade", "head")
        with schema_engine.begin() as connection:
            actor_id = connection.execute(text("""
                INSERT INTO trabajador (codigo, nombres, apellidos, activo)
                VALUES ('TRB-REC-PG', 'Ana', 'Almacen', true)
                RETURNING id
            """)).scalar_one()
            provider_id = connection.execute(text("""
                INSERT INTO scm_proveedor (codigo, razon_social, ruc)
                VALUES ('PROV-REC-PG', 'Proveedor Recepcion PG', '20524360366')
                RETURNING id
            """)).scalar_one()
            material_id = connection.execute(text("""
                INSERT INTO scm_material (
                    codigo, nombre, clase, categoria_recepcion_id,
                    unidad_base, activo, version
                )
                SELECT 'MP-SEG-REC-PG', 'PP segunda PG', 'MATERIA_PRIMA',
                       id, 'KG', true, 1
                FROM scm_categoria_recepcion
                WHERE codigo = 'RESINA_SEGUNDA'
                RETURNING id
            """)).scalar_one()
            document_id = connection.execute(text("""
                INSERT INTO scm_documento_proveedor (
                    proveedor_id, tipo, serie_normalizada,
                    numero_normalizado, fecha_emision,
                    cantidad_total_documental_kg
                )
                VALUES (
                    :provider_id, 'GUIA_REMISION', 'T002',
                    '00001833', DATE '2026-07-01', 5000.000
                )
                RETURNING id
            """), {"provider_id": provider_id}).scalar_one()
            reception_ids = connection.execute(text("""
                INSERT INTO scm_recepcion (
                    codigo, proveedor_id, recibida_por_id
                )
                VALUES
                    ('REC-PG-001', :provider_id, :actor_id),
                    ('REC-PG-002', :provider_id, :actor_id)
                RETURNING id
            """), {
                "provider_id": provider_id,
                "actor_id": actor_id,
            }).scalars().all()
            for reception_id in reception_ids:
                connection.execute(text("""
                    INSERT INTO scm_recepcion_documento (
                        recepcion_id, documento_id
                    ) VALUES (:reception_id, :document_id)
                """), {
                    "reception_id": reception_id,
                    "document_id": document_id,
                })
            assert connection.execute(text("""
                SELECT count(*) FROM scm_recepcion_documento
                WHERE documento_id = :document_id
            """), {"document_id": document_id}).scalar_one() == 2
            line_id = connection.execute(text("""
                INSERT INTO scm_recepcion_linea (
                    recepcion_id, numero_linea, material_id, modalidad,
                    bultos_recibidos, cantidad_documental_kg,
                    cantidad_medida_kg
                ) VALUES (
                    :reception_id, 1, :material_id,
                    'SEGUNDA_PESAJE_BOLSA', 1, 25.000, 24.950
                )
                RETURNING id
            """), {
                "reception_id": reception_ids[0],
                "material_id": material_id,
            }).scalar_one()
            weight_id = connection.execute(text("""
                INSERT INTO scm_pesaje_bolsa (
                    recepcion_linea_id, secuencia, peso_kg,
                    registrado_por_id
                ) VALUES (:line_id, 1, 24.950, :actor_id)
                RETURNING id
            """), {"line_id": line_id, "actor_id": actor_id}).scalar_one()
            connection.execute(text("""
                UPDATE scm_recepcion
                SET estado = 'CONFIRMADA', confirmada_at = CURRENT_TIMESTAMP
                WHERE id = :reception_id
            """), {"reception_id": reception_ids[0]})

        with pytest.raises(DBAPIError) as captured:
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    UPDATE scm_pesaje_bolsa
                    SET peso_kg = 25.000
                    WHERE id = :weight_id
                """), {"weight_id": weight_id})
        assert captured.value.orig.pgcode == "55000"
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_migration_adopta_legacy_backfill_y_rollback_sin_perder_filas():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url)
    try:
        with schema_engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE materia_prima (
                    id SERIAL PRIMARY KEY,
                    nombre VARCHAR(100) NOT NULL,
                    tipo VARCHAR(50)
                )
            """))
            connection.execute(text("""
                CREATE TABLE colorante (
                    id SERIAL PRIMARY KEY,
                    nombre VARCHAR(100) NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE rol_operativo (
                    id SERIAL PRIMARY KEY,
                    codigo VARCHAR(20) NOT NULL UNIQUE,
                    nombre VARCHAR(100) NOT NULL,
                    activo BOOLEAN
                )
            """))
            connection.execute(text("""
                CREATE TABLE trabajador (
                    id SERIAL PRIMARY KEY,
                    codigo VARCHAR(20) NOT NULL UNIQUE,
                    nombres VARCHAR(100) NOT NULL,
                    apellidos VARCHAR(100) NOT NULL,
                    nombre_corto VARCHAR(100),
                    activo BOOLEAN,
                    observaciones TEXT
                )
            """))
            connection.execute(text("""
                CREATE TABLE trabajador_rol (
                    trabajador_id INTEGER NOT NULL
                        REFERENCES trabajador(id),
                    rol_operativo_id INTEGER NOT NULL
                        REFERENCES rol_operativo(id),
                    PRIMARY KEY (trabajador_id, rol_operativo_id)
                )
            """))
            # La adopcion estampa la revision baseline, por lo que debe
            # representar tambien las tablas legacy que una migracion
            # posterior transforma, aunque este escenario SCM las deje vacias.
            connection.execute(text("""
                CREATE TABLE molde (
                    codigo VARCHAR(50) PRIMARY KEY,
                    nombre VARCHAR(100) NOT NULL,
                    peso_tiro_gr FLOAT NOT NULL,
                    tiempo_ciclo_std FLOAT,
                    activo BOOLEAN,
                    notas TEXT
                )
            """))
            connection.execute(text("""
                CREATE TABLE pieza (
                    id SERIAL PRIMARY KEY,
                    molde_id VARCHAR(50) NOT NULL REFERENCES molde(codigo),
                    nombre VARCHAR(200) NOT NULL,
                    linea_id INTEGER,
                    familia_id INTEGER,
                    cavidades INTEGER NOT NULL,
                    peso_unitario_gr FLOAT NOT NULL,
                    CONSTRAINT uq_molde_pieza_nombre
                        UNIQUE (molde_id, nombre)
                )
            """))
            connection.execute(text("""
                INSERT INTO materia_prima (id, nombre, tipo)
                VALUES
                    (10, 'PP virgen', 'VIRGEN'),
                    (11, 'PP segunda', ' segunda '),
                    (12, 'Molido ambiguo', 'MOLIDO'),
                    (13, 'Sin tipo', NULL)
            """))
            connection.execute(text("""
                INSERT INTO colorante (id, nombre)
                VALUES (20, 'Azul legacy')
            """))
            connection.execute(text("""
                INSERT INTO rol_operativo (codigo, nombre, activo)
                VALUES ('GERENCIA', 'Gerencia ya configurada', true)
            """))
            connection.execute(text("""
                INSERT INTO trabajador (
                    id, codigo, nombres, apellidos, activo
                )
                VALUES (30, 'TRB-LEGACY-30', 'Gerente', 'Legacy', true)
            """))
            connection.execute(text("""
                INSERT INTO trabajador_rol (trabajador_id, rol_operativo_id)
                SELECT 30, id
                FROM rol_operativo
                WHERE codigo = 'GERENCIA'
            """))

        _run_flask_db(schema_url, "stamp", BASELINE_REVISION)
        _run_flask_db(schema_url, "upgrade", EXPAND_REVISION)
        _run_flask_db(schema_url, "upgrade", EXPAND_REVISION)

        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT current_schema()")
            ).scalar_one() == schema
            clasificacion = dict(connection.execute(text("""
                SELECT materia.id, categoria.codigo
                FROM materia_prima AS materia
                JOIN scm_material AS material
                  ON material.id = materia.scm_material_id
                JOIN scm_categoria_recepcion AS categoria
                  ON categoria.id = material.categoria_recepcion_id
                ORDER BY materia.id
            """)).tuples().all())
            assert clasificacion == {
                10: "RESINA_VIRGEN",
                11: "RESINA_SEGUNDA",
                12: "LEGACY_POR_CONFIGURAR",
                13: "LEGACY_POR_CONFIGURAR",
            }
            assert connection.execute(text("""
                SELECT categoria.codigo
                FROM colorante
                JOIN scm_material AS material
                  ON material.id = colorante.scm_material_id
                JOIN scm_categoria_recepcion AS categoria
                  ON categoria.id = material.categoria_recepcion_id
                WHERE colorante.id = 20
            """)).scalar_one() == "LEGACY_POR_CONFIGURAR"
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_material
            """)).scalar_one() == 5
            assert connection.execute(text("""
                SELECT count(*)
                FROM materia_prima
                WHERE scm_material_id IS NULL
            """)).scalar_one() == 0
            assert connection.execute(text("""
                SELECT nombre FROM rol_operativo WHERE codigo = 'GERENCIA'
            """)).scalar_one() == "Gerencia ya configurada"
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_rol_capacidad AS relacion
                JOIN rol_operativo AS rol
                  ON rol.id = relacion.rol_operativo_id
                JOIN scm_capacidad AS capacidad
                  ON capacidad.id = relacion.capacidad_id
                WHERE rol.codigo = 'GERENCIA'
                  AND capacidad.codigo IN ('OC_APROBAR', 'CORRECCION_APROBAR')
            """)).scalar_one() == 0
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_rol_capacidad AS relacion
                JOIN rol_operativo AS rol
                  ON rol.id = relacion.rol_operativo_id
                WHERE rol.codigo = 'CONFIGURACION_SCM'
            """)).scalar_one() == 2
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_rol_capacidad AS relacion
                JOIN rol_operativo AS rol
                  ON rol.id = relacion.rol_operativo_id
                JOIN scm_capacidad AS capacidad
                  ON capacidad.id = relacion.capacidad_id
                WHERE rol.codigo = 'SUPERVISOR'
                  AND capacidad.codigo =
                      'ENTRADA_EXCEPCIONAL_REGULARIZAR'
            """)).scalar_one() == 1
            materia_links_before = dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM materia_prima
                ORDER BY id
            """)).tuples().all())
            colorante_link_before = connection.execute(text("""
                SELECT scm_material_id
                FROM colorante
                WHERE id = 20
            """)).scalar_one()

        materia_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("materia_prima")
        }
        colorante_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("colorante")
        }
        assert materia_columns["scm_material_id"]["nullable"] is True
        assert colorante_columns["scm_material_id"]["nullable"] is True

        with schema_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO materia_prima (id, nombre, tipo)
                VALUES (14, 'Segunda tardia', ' segunda ')
            """))
            connection.execute(text("""
                INSERT INTO colorante (id, nombre)
                VALUES (21, 'Rojo tardio')
            """))
            connection.execute(text("""
                INSERT INTO scm_material (
                    codigo,
                    nombre,
                    clase,
                    categoria_recepcion_id,
                    unidad_base,
                    activo,
                    version
                )
                SELECT
                    'MP-CONTRACT-00000014',
                    'Colision reservada',
                    'COLORANTE',
                    id,
                    'KG',
                    true,
                    1
                FROM scm_categoria_recepcion
                WHERE codigo = 'LEGACY_POR_CONFIGURAR'
            """))

        with schema_engine.connect() as connection:
            assert connection.execute(text("""
                SELECT count(*)
                FROM materia_prima
                WHERE id = 14 AND scm_material_id IS NULL
            """)).scalar_one() == 1
            assert connection.execute(text("""
                SELECT count(*)
                FROM colorante
                WHERE id = 21 AND scm_material_id IS NULL
            """)).scalar_one() == 1
            assert connection.execute(
                text("SELECT count(*) FROM scm_material")
            ).scalar_one() == 6

        failed_contract = _run_flask_db_failure(
            schema_url,
            "upgrade",
            CONTRACT_REVISION,
        )
        assert "MP-CONTRACT-00000014" in (
            failed_contract.stdout + failed_contract.stderr
        )

        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == EXPAND_REVISION
            assert connection.execute(text("""
                SELECT count(*)
                FROM materia_prima
                WHERE id = 14 AND scm_material_id IS NULL
            """)).scalar_one() == 1
            assert connection.execute(text("""
                SELECT count(*)
                FROM colorante
                WHERE id = 21 AND scm_material_id IS NULL
            """)).scalar_one() == 1
            assert connection.execute(text("""
                SELECT count(*)
                FROM scm_material
                WHERE codigo = 'COL-CONTRACT-00000021'
            """)).scalar_one() == 0
            assert connection.execute(
                text("SELECT count(*) FROM scm_material")
            ).scalar_one() == 6
            assert dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM materia_prima
                WHERE id BETWEEN 10 AND 13
                ORDER BY id
            """)).tuples().all()) == materia_links_before
            assert connection.execute(text("""
                SELECT scm_material_id
                FROM colorante
                WHERE id = 20
            """)).scalar_one() == colorante_link_before

        failed_materia_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("materia_prima")
        }
        failed_colorante_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("colorante")
        }
        assert failed_materia_columns["scm_material_id"]["nullable"] is True
        assert failed_colorante_columns["scm_material_id"]["nullable"] is True

        with schema_engine.begin() as connection:
            connection.execute(text("""
                DELETE FROM scm_material
                WHERE codigo = 'MP-CONTRACT-00000014'
            """))

        _run_flask_db(schema_url, "upgrade", LEGACY_ADOPTION_TARGET)
        _run_flask_db(schema_url, "upgrade", LEGACY_ADOPTION_TARGET)

        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == LEGACY_ADOPTION_TARGET
            materia_contract = {
                row.id: (row.codigo, row.categoria)
                for row in connection.execute(text("""
                    SELECT
                        materia.id,
                        material.codigo,
                        categoria.codigo AS categoria
                    FROM materia_prima AS materia
                    JOIN scm_material AS material
                      ON material.id = materia.scm_material_id
                    JOIN scm_categoria_recepcion AS categoria
                      ON categoria.id = material.categoria_recepcion_id
                    ORDER BY materia.id
                """))
            }
            assert materia_contract == {
                10: ("MP-LEGACY-00000010", "RESINA_VIRGEN"),
                11: ("MP-LEGACY-00000011", "RESINA_SEGUNDA"),
                12: ("MP-LEGACY-00000012", "LEGACY_POR_CONFIGURAR"),
                13: ("MP-LEGACY-00000013", "LEGACY_POR_CONFIGURAR"),
                14: ("MP-CONTRACT-00000014", "RESINA_SEGUNDA"),
            }
            colorante_contract = {
                row.id: (row.codigo, row.categoria)
                for row in connection.execute(text("""
                    SELECT
                        colorante.id,
                        material.codigo,
                        categoria.codigo AS categoria
                    FROM colorante
                    JOIN scm_material AS material
                      ON material.id = colorante.scm_material_id
                    JOIN scm_categoria_recepcion AS categoria
                      ON categoria.id = material.categoria_recepcion_id
                    ORDER BY colorante.id
                """))
            }
            assert colorante_contract == {
                20: ("COL-LEGACY-00000020", "LEGACY_POR_CONFIGURAR"),
                21: ("COL-CONTRACT-00000021", "LEGACY_POR_CONFIGURAR"),
            }
            assert connection.execute(
                text("SELECT count(*) FROM scm_material")
            ).scalar_one() == 7
            assert connection.execute(text("""
                SELECT count(*)
                FROM materia_prima
                WHERE scm_material_id IS NULL
            """)).scalar_one() == 0
            assert connection.execute(text("""
                SELECT count(*)
                FROM colorante
                WHERE scm_material_id IS NULL
            """)).scalar_one() == 0
            assert dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM materia_prima
                WHERE id BETWEEN 10 AND 13
                ORDER BY id
            """)).tuples().all()) == materia_links_before
            assert connection.execute(text("""
                SELECT scm_material_id
                FROM colorante
                WHERE id = 20
            """)).scalar_one() == colorante_link_before
            all_materia_links = dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM materia_prima
                ORDER BY id
            """)).tuples().all())
            all_colorante_links = dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM colorante
                ORDER BY id
            """)).tuples().all())
            contract_codes = set(connection.execute(
                text("SELECT codigo FROM scm_material")
            ).scalars())

        contracted_materia_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("materia_prima")
        }
        contracted_colorante_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("colorante")
        }
        assert contracted_materia_columns["scm_material_id"]["nullable"] is False
        assert contracted_colorante_columns["scm_material_id"]["nullable"] is False

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO materia_prima (
                        id, nombre, tipo, scm_material_id
                    )
                    VALUES (90, 'Materia sin identidad', 'VIRGEN', NULL)
                """))

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO colorante (id, nombre, scm_material_id)
                    VALUES (91, 'Colorante sin identidad', NULL)
                """))

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                first_material = connection.execute(text("""
                    SELECT scm_material_id
                    FROM materia_prima
                    WHERE id = 10
                """)).scalar_one()
                connection.execute(
                    text("""
                        UPDATE materia_prima
                        SET scm_material_id = :material_id
                        WHERE id = 11
                    """),
                    {"material_id": first_material},
                )

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    UPDATE materia_prima
                    SET scm_material_id = 999999
                    WHERE id = 13
                """))

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO scm_categoria_recepcion (
                        codigo, nombre, modalidad_default
                    )
                    VALUES ('INVALIDA', 'Inválida', 'PESO_ESTIMADO')
                """))

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO scm_material (
                        codigo,
                        nombre,
                        clase,
                        categoria_recepcion_id,
                        unidad_base
                    )
                    SELECT
                        'INVALIDO-UNIDAD',
                        'Inválido',
                        'MATERIA_PRIMA',
                        id,
                        'GR'
                    FROM scm_categoria_recepcion
                    WHERE codigo = 'RESINA_VIRGEN'
                """))

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                material_materia_prima = connection.execute(text("""
                    SELECT scm_material_id
                    FROM materia_prima
                    WHERE id = 10
                """)).scalar_one()
                connection.execute(
                    text("""
                        UPDATE colorante
                        SET scm_material_id = :material_id
                        WHERE id = 20
                    """),
                    {"material_id": material_materia_prima},
                )

        with pytest.raises(IntegrityError):
            with schema_engine.begin() as connection:
                connection.execute(text("""
                    UPDATE scm_material AS material
                    SET clase = 'COLORANTE'
                    FROM materia_prima AS materia
                    WHERE materia.id = 10
                      AND material.id = materia.scm_material_id
                """))

        _run_flask_db(schema_url, "downgrade", EXPAND_REVISION)

        downgraded_materia_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("materia_prima")
        }
        downgraded_colorante_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("colorante")
        }
        assert downgraded_materia_columns["scm_material_id"]["nullable"] is True
        assert downgraded_colorante_columns["scm_material_id"]["nullable"] is True
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == EXPAND_REVISION
            assert connection.execute(
                text("SELECT count(*) FROM scm_material")
            ).scalar_one() == 7
            assert set(connection.execute(
                text("SELECT codigo FROM scm_material")
            ).scalars()) == contract_codes
            assert dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM materia_prima
                ORDER BY id
            """)).tuples().all()) == all_materia_links
            assert dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM colorante
                ORDER BY id
            """)).tuples().all()) == all_colorante_links

        _run_flask_db(schema_url, "upgrade", LEGACY_ADOPTION_TARGET)
        _run_flask_db(schema_url, "upgrade", LEGACY_ADOPTION_TARGET)
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == LEGACY_ADOPTION_TARGET
            assert connection.execute(
                text("SELECT count(*) FROM scm_material")
            ).scalar_one() == 7
            assert set(connection.execute(
                text("SELECT codigo FROM scm_material")
            ).scalars()) == contract_codes
            assert dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM materia_prima
                ORDER BY id
            """)).tuples().all()) == all_materia_links
            assert dict(connection.execute(text("""
                SELECT id, scm_material_id
                FROM colorante
                ORDER BY id
            """)).tuples().all()) == all_colorante_links

        recontracted_materia_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("materia_prima")
        }
        recontracted_colorante_columns = {
            item["name"]: item
            for item in inspect(schema_engine).get_columns("colorante")
        }
        assert recontracted_materia_columns["scm_material_id"]["nullable"] is False
        assert recontracted_colorante_columns["scm_material_id"]["nullable"] is False

        _run_flask_db(schema_url, "downgrade", BASELINE_REVISION)
        legacy_columns = {
            item["name"]
            for item in inspect(schema_engine).get_columns("materia_prima")
        }
        assert "scm_material_id" not in legacy_columns
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM materia_prima")
            ).scalar_one() == 5
            assert connection.execute(
                text("SELECT count(*) FROM colorante")
            ).scalar_one() == 2
            assert connection.execute(
                text("SELECT count(*) FROM trabajador")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT count(*) FROM trabajador_rol")
            ).scalar_one() == 1

        failed = _run_flask_db_failure(schema_url, "downgrade", "base")
        assert "línea base legacy es irreversible" in (
            failed.stdout + failed.stderr
        ).lower()
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM materia_prima")
            ).scalar_one() == 5

        _run_flask_db(schema_url, "upgrade", LEGACY_ADOPTION_TARGET)
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM scm_material")
            ).scalar_one() == 7
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == LEGACY_ADOPTION_TARGET
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)
