"""PostgreSQL contract checks for the KG subledger migration.

These tests deliberately execute the real Alembic migration in an isolated
schema.  They do not use ``db.create_all`` because that path cannot install
the PostgreSQL triggers that guard the UN/KG boundary.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app import db
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmSaldoInventarioKg,
)
from app.models.scm_ot import ScmManga
from app.models.scm_warehouse import ScmExistenciaManga
from app.models.producto import Familia, Linea
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_kg_service import activate_article_for_kg
from app.services.scm_ot_service import acknowledge_station_print_job, transition_color_work
from app.services.scm_service_support import ScmServiceError
from app.services.scm_warehouse_service import receive_manga, resolve_receiving_label
from app.services.scm_weighing_service import confirm_manga_weighing
from app.services.scm_weighing_service import (
    approve_weighing_correction,
    request_weighing_correction,
)
import app.services.scm_weighing_service as weighing_service
import app.services.scm_warehouse_service as warehouse_service
from tests.scm.test_scm_inline_assembly_postgres import postgres_inline_app
from tests.scm.test_scm_kg_receipt import _print_color_manga, _seed_aggregate_color_work
from tests.scm.test_scm_migrations_postgres import (
    _drop_isolated_schema,
    _isolated_postgres_url,
    _run_flask_db,
    _run_flask_db_failure,
)


pytestmark = pytest.mark.postgres

F93 = "f93d4e6a8c02"
F94 = "f94a1b2c3d04"


def _migrated_schema():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    _run_flask_db(schema_url, "upgrade", F94)
    return admin_engine, schema, schema_url


def _article(connection, *, code, unit="UN", article_class="PIEZA_COLOR"):
    article_id = connection.execute(text("""
        INSERT INTO scm_articulo (
            public_id, codigo, nombre, clase, unidad_base,
            unidad_inventario, activo, version
        ) VALUES (:public_id, :codigo, :nombre, :clase, 'UN', :unit, true, 1)
        RETURNING id
    """), {
        "public_id": str(uuid4()),
        "codigo": code,
        "nombre": code,
        "clase": article_class,
        "unit": unit,
    }).scalar_one()
    if article_class == "PIEZA_COLOR":
        connection.execute(text(
            "INSERT INTO pieza_color (sku) VALUES (:sku)"
        ), {"sku": code})
        connection.execute(text("""
            INSERT INTO scm_articulo_pieza_color (articulo_id, pieza_color_sku)
            VALUES (:article_id, :sku)
        """), {"article_id": article_id, "sku": code})
    elif article_class == "SUBENSAMBLE_WIP":
        connection.execute(text("""
            INSERT INTO scm_definicion_wip (articulo_id, requiere_calidad)
            VALUES (:article_id, false)
        """), {"article_id": article_id})
    return article_id


def _location(connection, code="KG-POSTGRES-LOC"):
    return connection.execute(text("""
        INSERT INTO scm_ubicacion_inventario (
            codigo, nombre, clases_articulo_json, activo, version
        ) VALUES (:code, :code, '["PIEZA_COLOR", "SUBENSAMBLE_WIP"]'::jsonb, true, 1)
        RETURNING id
    """), {"code": code}).scalar_one()


def test_f94_upgrade_and_downgrade_round_trip_on_empty_schema():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    try:
        _run_flask_db(schema_url, "upgrade", F93)
        _run_flask_db(schema_url, "upgrade", F94)
        engine = create_engine(schema_url)
        try:
            with engine.connect() as connection:
                tables = set(connection.execute(text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                )).scalars())
                assert {
                    "scm_saldo_inventario_kg",
                    "scm_movimiento_inventario_kg",
                    "scm_existencia_manga_kg",
                } <= tables
        finally:
            engine.dispose()

        _run_flask_db(schema_url, "downgrade", F93)
        _run_flask_db(schema_url, "upgrade", F94)
    finally:
        _drop_isolated_schema(admin_engine, schema)


def test_f94_downgrade_blocks_kg_article_and_subledger_rows():
    admin_engine, schema, schema_url = _migrated_schema()
    engine = create_engine(schema_url)
    try:
        with engine.begin() as connection:
            article_id = _article(connection, code="KG-DOWNGRADE-ARTICLE", unit="KG")
            location_id = _location(connection, code="KG-DOWNGRADE-LOC")
            connection.execute(text("""
                INSERT INTO scm_saldo_inventario_kg (
                    id, articulo_scm_id, ubicacion_id, cantidad_fisica_kg,
                    cantidad_reservada_kg, cantidad_no_disponible_kg, version
                ) VALUES (:id, :article_id, :location_id, 1, 0, 1, 1)
            """), {
                "id": str(uuid4()), "article_id": article_id,
                "location_id": location_id,
            })

        failed = _run_flask_db_failure(schema_url, "downgrade", F93)
        assert "downgrade KG bloqueado" in failed.stdout + failed.stderr
    finally:
        engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_postgres_first_kg_balance_control_vs_receipt_has_one_unique_winner():
    """A control and a receipt cannot create duplicate article/location balances."""
    admin_engine, schema, schema_url = _migrated_schema()
    setup_engine = create_engine(schema_url)
    try:
        with setup_engine.begin() as connection:
            article_id = _article(connection, code="KG-FIRST-BALANCE-RACE", unit="KG")
            location_id = _location(connection, code="KG-FIRST-BALANCE-RACE-LOC")

        barrier = Barrier(2)

        def create_balance(quantity):
            engine = create_engine(schema_url)
            try:
                barrier.wait(timeout=30)
                with engine.begin() as connection:
                    connection.execute(text("""
                        INSERT INTO scm_saldo_inventario_kg (
                            id, articulo_scm_id, ubicacion_id, cantidad_fisica_kg,
                            cantidad_reservada_kg, cantidad_no_disponible_kg, version
                        ) VALUES (:id, :article_id, :location_id, :quantity, 0, 0, 1)
                    """), {
                        "id": str(uuid4()), "article_id": article_id,
                        "location_id": location_id, "quantity": quantity,
                    })
                return "ok"
            except DBAPIError:
                return "conflict"
            finally:
                engine.dispose()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(create_balance, ("5.000", "8.000")))

        assert outcomes.count("ok") == 1
        assert outcomes.count("conflict") == 1
        with setup_engine.connect() as connection:
            row = connection.execute(text("""
                SELECT cantidad_fisica_kg
                FROM scm_saldo_inventario_kg
                WHERE articulo_scm_id = :article_id AND ubicacion_id = :location_id
            """), {"article_id": article_id, "location_id": location_id}).one()
            assert Decimal(row[0]) in {Decimal("5.000"), Decimal("8.000")}
    finally:
        setup_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_f94_article_marker_guards_both_stock_directions():
    admin_engine, schema, schema_url = _migrated_schema()
    engine = create_engine(schema_url)
    try:
        with engine.begin() as connection:
            un_article = _article(connection, code="UN-MARKER-STOCK", unit="UN")
            location_id = _location(connection, code="UN-MARKER-LOC")
            connection.execute(text("""
                INSERT INTO scm_saldo_inventario (
                    id, articulo_scm_id, ubicacion_id, cantidad_fisica,
                    cantidad_reservada, cantidad_no_disponible, version
                ) VALUES (:id, :article_id, :location_id, 1, 0, 1, 1)
            """), {
                "id": str(uuid4()), "article_id": un_article,
                "location_id": location_id,
            })

        with pytest.raises(DBAPIError, match="KG_DOWNGRADE_LEGACY_BALANCE"):
            with engine.begin() as connection:
                connection.execute(text(
                    "UPDATE scm_articulo SET unidad_inventario = 'KG' WHERE id = :id"
                ), {"id": un_article})

        with engine.begin() as connection:
            kg_article = _article(connection, code="KG-MARKER-STOCK", unit="KG")
            location_id = _location(connection, code="KG-MARKER-LOC")
            connection.execute(text("""
                INSERT INTO scm_saldo_inventario_kg (
                    id, articulo_scm_id, ubicacion_id, cantidad_fisica_kg,
                    cantidad_reservada_kg, cantidad_no_disponible_kg, version
                ) VALUES (:id, :article_id, :location_id, 2, 0, 2, 1)
            """), {
                "id": str(uuid4()), "article_id": kg_article,
                "location_id": location_id,
            })

        with pytest.raises(DBAPIError, match="KG_DOWNGRADE_NONZERO"):
            with engine.begin() as connection:
                connection.execute(text(
                    "UPDATE scm_articulo SET unidad_inventario = 'UN' WHERE id = :id"
                ), {"id": kg_article})
    finally:
        engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_f94_rejects_kg_marker_on_product_finished_article():
    admin_engine, schema, schema_url = _migrated_schema()
    engine = create_engine(schema_url)
    try:
        with pytest.raises(DBAPIError, match="KG_CLASS_NOT_ALLOWED|ck_scm_articulo_kg_class"):
            with engine.begin() as connection:
                _article(
                    connection,
                    code="KG-INVALID-PT",
                    unit="KG",
                    article_class="PRODUCTO_TERMINADO",
                )
    finally:
        engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def test_f94_logistic_unit_guard_blocks_insert_and_retag_to_kg():
    admin_engine, schema, schema_url = _migrated_schema()
    engine = create_engine(schema_url)
    try:
        with engine.begin() as connection:
            worker_id = connection.execute(text("""
                INSERT INTO trabajador (codigo, nombres, apellidos, activo)
                VALUES ('TR-KG-GUARD', 'Prueba', 'KG', true)
                RETURNING id
            """)).scalar_one()
            un_article = _article(connection, code="UN-UL-GUARD", unit="UN")
            kg_article = _article(connection, code="KG-UL-GUARD", unit="KG")
            location_id = _location(connection, code="UL-GUARD-LOC")
            lot_id = str(uuid4())
            connection.execute(text("""
                INSERT INTO scm_lote_apertura_inventario (
                    id, codigo, fecha_corte, motivo, estado, version,
                    creado_por_id, create_operation_id
                ) VALUES (:id, 'LOTE-UL-GUARD', CURRENT_DATE, 'guard', 'BORRADOR', 1,
                          :worker_id, :operation_id)
            """), {
                "id": lot_id, "worker_id": worker_id,
                "operation_id": str(uuid4()),
            })
            line_id = connection.execute(text("""
                INSERT INTO scm_lote_apertura_linea (
                    lote_id, articulo_scm_id, ubicacion_codigo, ubicacion_nombre,
                    cantidad, estado_calidad
                ) VALUES (:lot_id, :article_id, 'UL-GUARD-LOC', 'UL guard', 1, 'LIBERADO')
                RETURNING id
            """), {"lot_id": lot_id, "article_id": un_article}).scalar_one()

            base = {
                "id": str(uuid4()), "codigo": "UL-UN-GUARD",
                "qr": "QR-UL-UN-GUARD", "lot_id": lot_id,
                "line_id": line_id, "article_id": un_article,
                "location_id": location_id, "worker_id": worker_id,
                "operation_id": str(uuid4()),
            }
            connection.execute(text("""
                INSERT INTO scm_unidad_logistica_inventario (
                    id, codigo, qr_value, lote_apertura_id, apertura_linea_id,
                    articulo_scm_id, ubicacion_id, peso_bruto_kg, tara_kg,
                    peso_neto_kg, cantidad_disponible_kg, estado_calidad, estado,
                    station_id, capturado_por_id, capture_operation_id
                ) VALUES (:id, :codigo, :qr, :lot_id, :line_id, :article_id,
                          :location_id, 2, 0.5, 1.5, 1.5, 'PENDIENTE', 'REGISTRADA',
                          'STATION-UL-GUARD', :worker_id, :operation_id)
            """), base)

        with pytest.raises(DBAPIError, match="KG_OPERATION_NOT_ENABLED"):
            with engine.begin() as connection:
                connection.execute(text("""
                    UPDATE scm_unidad_logistica_inventario
                    SET articulo_scm_id = :kg_article_id
                    WHERE codigo = 'UL-UN-GUARD'
                """), {"kg_article_id": kg_article})

        base["id"] = str(uuid4())
        base["codigo"] = "UL-KG-GUARD"
        base["qr"] = "QR-UL-KG-GUARD"
        base["article_id"] = kg_article
        base["operation_id"] = str(uuid4())
        with pytest.raises(DBAPIError, match="KG_OPERATION_NOT_ENABLED"):
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO scm_unidad_logistica_inventario (
                        id, codigo, qr_value, lote_apertura_id, apertura_linea_id,
                        articulo_scm_id, ubicacion_id, peso_bruto_kg, tara_kg,
                        peso_neto_kg, cantidad_disponible_kg, estado_calidad, estado,
                        station_id, capturado_por_id, capture_operation_id
                    ) VALUES (:id, :codigo, :qr, :lot_id, :line_id, :article_id,
                              :location_id, 2, 0.5, 1.5, 1.5, 'PENDIENTE', 'REGISTRADA',
                              'STATION-UL-GUARD', :worker_id, :operation_id)
                """), base)
    finally:
        engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


def _prepare_postgres_kg_receipt(app):
    """Build one real weighed KG manga on the PostgreSQL app fixture."""
    with app.app_context():
        if Linea.query.first() is None:
            db.session.add(Linea(codigo=901, nombre="KG PostgreSQL"))
        if Familia.query.first() is None:
            db.session.add(Familia(codigo=901, nombre="KG PostgreSQL"))
        db.session.flush()
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=120)
        )
        manga = ScmManga.query.filter_by(
            public_id=UUID(created["mangas"][0]["public_id"])
        ).one()
        manga.cantidad_planificada_un = Decimal("120")
        manga.cantidad_asignada_un = Decimal("120")
        if manga.asignacion is not None:
            manga.asignacion.cantidad_asignada_un = Decimal("120")
        if manga.plan_linea is not None:
            manga.plan_linea.capacidad_efectiva_un = 120
        db.session.flush()
        work = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]},
            action="iniciar",
        )["trabajo_color"]
        station, label = _print_color_manga(
            actor=creator,
            manga_id=created["mangas"][0]["public_id"],
            station_code="PESAJE-KG-PG",
        )
        weighed = confirm_manga_weighing(
            db.session,
            station_id=station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "12.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-08-11T16:55:00-05:00",
                "reading_stable": True,
            },
        )
        acknowledge_station_print_job(
            db.session,
            station_id=station.station_id,
            print_job_id=UUID(weighed["print_job_id"]),
            data={"results": [{
                "label_id": weighed["post_label"]["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        warehouse_role = RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        warehouse_actor = Trabajador(
            codigo=f"TRB-KG-PG-{uuid4().hex[:8]}",
            nombres="Postgres",
            apellidos="KG",
            activo=True,
            roles=[
                warehouse_role,
                RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one(),
            ],
        )
        db.session.add(warehouse_actor)
        db.session.flush()
        article = manga.lote_articulo.articulo
        activate_article_for_kg(db.session, article_id=article.id)
        db.session.commit()
        app.config["KG_RECEIPT_WRITE_ENABLED"] = True
        candidate = resolve_receiving_label(
            db.session,
            actor_id=warehouse_actor.id,
            label_id=UUID(weighed["post_label"]["public_id"]),
        )
        return {
            "actor_id": warehouse_actor.id,
            "correction_requester_id": creator.id,
            "correction_approver_id": _approver.id,
            "label_id": weighed["post_label"]["public_id"],
            "manga_codigo": candidate["manga_codigo"],
            "manga_id": UUID(candidate["manga_id"]),
            "manga_internal_id": manga.id,
            "weighing_id": UUID(weighed["weighing"]["public_id"]),
            "expected": candidate["expected_weighing_source"],
            "candidate": candidate,
        }


def _prepare_two_postgres_kg_receipts(app):
    """Build two KG mangas from one color work for the first-balance race."""
    with app.app_context():
        if Linea.query.first() is None:
            db.session.add(Linea(codigo=901, nombre="KG PostgreSQL"))
        if Familia.query.first() is None:
            db.session.add(Familia(codigo=901, nombre="KG PostgreSQL"))
        db.session.flush()
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=240)
        )
        assert len(created["mangas"]) >= 2
        mangas = [
            ScmManga.query.filter_by(public_id=UUID(item["public_id"])).one()
            for item in created["mangas"][:2]
        ]
        article = mangas[0].lote_articulo.articulo
        article.unidad_inventario = "KG"
        for manga in mangas:
            manga.cantidad_planificada_un = Decimal("120")
            manga.cantidad_asignada_un = Decimal("120")
            if manga.asignacion is not None:
                manga.asignacion.cantidad_asignada_un = Decimal("120")
            if manga.plan_linea is not None:
                manga.plan_linea.capacidad_efectiva_un = 120
        db.session.flush()
        transition_color_work(
            db.session, actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]), operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]}, action="iniciar",
        )
        weighed = []
        for index, manga in enumerate(mangas):
            station, label = _print_color_manga(
                actor=creator, manga_id=manga.public_id,
                station_code=f"PESAJE-KG-PG-RACE-{index}",
            )
            result = confirm_manga_weighing(
                db.session, station_id=station.station_id, operation_id=uuid4(),
                actor_id=creator.id,
                data={
                    "label_id": label["public_id"], "capture_id": str(uuid4()),
                    "peso_bruto_kg": "12.100", "tara_kg": "0.100",
                    "tara_fuente": "TIPO_MANGA",
                    "pesada_at": f"2026-08-11T16:{55 + index:02d}:00-05:00",
                    "reading_stable": True,
                },
            )
            acknowledge_station_print_job(
                db.session, station_id=station.station_id,
                print_job_id=UUID(result["print_job_id"]),
                data={"results": [{
                    "label_id": result["post_label"]["public_id"],
                    "estado": "IMPRESA", "printer_name": "TSC",
                }]},
            )
            weighed.append(result)
        warehouse_role = RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        warehouse_actor = Trabajador(
            codigo=f"KG-RACE-{uuid4().hex[:8]}",
            nombres="Postgres", apellidos="KG", activo=True,
            roles=[warehouse_role, RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()],
        )
        db.session.add(warehouse_actor)
        db.session.flush()
        activate_article_for_kg(db.session, article_id=article.id)
        db.session.commit()
        app.config["KG_RECEIPT_WRITE_ENABLED"] = True
        scenarios = []
        for manga, result in zip(mangas, weighed):
            candidate = resolve_receiving_label(
                db.session, actor_id=warehouse_actor.id,
                label_id=UUID(result["post_label"]["public_id"]),
            )
            scenarios.append({
                "actor_id": warehouse_actor.id,
                "label_id": result["post_label"]["public_id"],
                "manga_id": UUID(candidate["manga_id"]),
                "manga_internal_id": manga.id,
                "expected": candidate["expected_weighing_source"],
                "candidate": candidate,
            })
        return scenarios


def _receive_in_thread(app, scenario, operation_id, barrier):
    with app.app_context():
        try:
            barrier.wait(timeout=30)
            result = receive_manga(
                db.session,
                actor_id=scenario["actor_id"],
                operation_id=operation_id,
                data={
                    **(
                        {"manga_codigo": scenario["manga_codigo"]}
                        if scenario.get("use_manga_code")
                        else {"label_id": scenario["label_id"]}
                    ),
                    "ubicacion_codigo": "RECEPCION_PIEZAS_WIP",
                    "presencia_confirmada": True,
                    "bolsa_cerrada": True,
                    "coincidencia_etiquetas": True,
                    "expected_weighing_source": scenario["expected"],
                },
            )
            return ("ok", result)
        except ScmServiceError as error:
            db.session.rollback()
            return ("error", error.code)
        finally:
            db.session.remove()


def test_postgres_two_concurrent_kg_receipts_have_one_movement(postgres_inline_app):
    app = postgres_inline_app
    scenario = _prepare_postgres_kg_receipt(app)
    barrier = Barrier(2)
    operations = [uuid4(), uuid4()]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda operation: _receive_in_thread(app, scenario, operation, barrier),
            operations,
        ))

    assert [result[0] for result in results].count("ok") == 1
    assert [result[1] for result in results if result[0] == "error"] == ["MANGA_YA_RECIBIDA"]
    with app.app_context():
        assert ScmMovimientoInventarioKg.query.filter_by(
            referencia_id=str(scenario["manga_id"])
        ).count() == 1
        assert ScmExistenciaMangaKg.query.filter_by(
            manga_id=scenario["manga_internal_id"]
        ).count() == 1
        assert ScmExistenciaManga.query.filter_by(
            articulo_scm_id=scenario["candidate"].get("articulo", {}).get("id")
        ).count() == 0


def test_postgres_two_manga_receipts_create_one_balance_and_sum_both_entries(
    postgres_inline_app,
):
    app = postgres_inline_app
    scenarios = _prepare_two_postgres_kg_receipts(app)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda item: _receive_in_thread(app, item, uuid4(), barrier),
            scenarios,
        ))

    assert [result[0] for result in results] == ["ok", "ok"]
    with app.app_context():
        article_id = scenarios[0]["candidate"]["articulo"]["id"]
        balance = ScmSaldoInventarioKg.query.filter_by(
            articulo_scm_id=article_id,
        ).one()
        assert Decimal(balance.cantidad_fisica_kg) == Decimal("24.000")
        assert ScmExistenciaMangaKg.query.filter(
            ScmExistenciaMangaKg.manga_id.in_(
                [item["manga_internal_id"] for item in scenarios]
            )
        ).count() == 2


def test_postgres_correction_commit_while_receipt_waits_rejects_stale_source(
    postgres_inline_app, monkeypatch,
):
    app = postgres_inline_app
    scenario = _prepare_postgres_kg_receipt(app)
    with app.app_context():
        correction = request_weighing_correction(
            db.session,
            actor_id=scenario["correction_requester_id"],
            weighing_id=scenario["weighing_id"],
            operation_id=uuid4(),
            data={
                "proposed": {"peso_bruto_kg": "12.200"},
                "motivo": "Corrección PG durante recepción",
            },
        )["correction"]

    approval_locked = Event()
    release_approval = Event()
    receiver_loaded = Event()
    original_lock = weighing_service._lock_manga_inventory_authority

    def hold_approval_lock(session, *, manga_id):
        result = original_lock(session, manga_id=manga_id)
        approval_locked.set()
        assert release_approval.wait(timeout=30)
        return result

    monkeypatch.setattr(
        weighing_service, "_lock_manga_inventory_authority", hold_approval_lock,
    )
    original_resolve_label = warehouse_service._resolve_label

    def preload_receiver_identity(session, label_id):
        result = original_resolve_label(session, label_id)
        receiver_loaded.set()
        return result

    monkeypatch.setattr(
        warehouse_service, "_resolve_label", preload_receiver_identity,
    )

    def approve_in_thread():
        with app.app_context():
            try:
                return approve_weighing_correction(
                    db.session,
                    actor_id=scenario["correction_approver_id"],
                    correction_id=UUID(correction["id"]),
                    operation_id=uuid4(),
                    data={"motivo_aprobacion": "Corrección autorizada PG"},
                )
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval_future = executor.submit(approve_in_thread)
        assert approval_locked.wait(timeout=30)
        receipt_future = executor.submit(
            _receive_in_thread, app, scenario, uuid4(), Barrier(1),
        )
        assert receiver_loaded.wait(timeout=30)
        release_approval.set()
        approval_result = approval_future.result(timeout=30)
        receipt_result = receipt_future.result(timeout=30)

    assert approval_result["correction"]["estado"] == "APLICADA"
    assert receipt_result == ("error", "PESAJE_VERSION_CONFLICT")
    with app.app_context():
        assert ScmMovimientoInventarioKg.query.count() == 0
        assert ScmExistenciaMangaKg.query.count() == 0
