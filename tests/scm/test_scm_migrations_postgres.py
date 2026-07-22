import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
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
HEAD_REVISION = LINEA_FAMILIA_REVISION


def _isolated_postgres_url():
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")

    base_url = make_url(raw_url)
    assert base_url.database == "envaperu_test"
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
            } <= tables
            with schema_engine.connect() as connection:
                assert connection.execute(
                    text("SELECT current_schema()")
                ).scalar_one() == schema
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
                } <= capacidades
                assert connection.execute(
                    text("SELECT count(*) FROM trabajador")
                ).scalar_one() == 0
                assert connection.execute(text("""
                    SELECT clave, prefijo, siguiente_valor, ancho
                    FROM correlativo_catalogo
                    ORDER BY clave
                """)).tuples().all() == [
                    ("MOLDE", "ML", 1, 6),
                    ("PIEZA", "PZ", 1, 6),
                    ("PIEZA_COLOR", "PC", 1, 6),
                    ("PRODUCTO_TERMINADO", "PT", 1, 6),
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

        _run_flask_db(schema_url, "upgrade", "head")
        _run_flask_db(schema_url, "upgrade", "head")

        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
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

        _run_flask_db(schema_url, "upgrade", "head")
        _run_flask_db(schema_url, "upgrade", "head")
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
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

        _run_flask_db(schema_url, "upgrade", "head")
        with schema_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM scm_material")
            ).scalar_one() == 7
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
    finally:
        schema_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)
