from uuid import uuid4
import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_ot import (
    ScmEtiquetaManga,
    ScmLoteArticulo,
    ScmManga,
    ScmTrabajoImpresionManga,
)
from app.models.scm_production_orders import ScmOrdenOperacion, ScmOrdenOperacionSalida
from app.models.trabajador import Trabajador
from app.services.scm_service_support import ScmServiceError
from app.services.scm_weighing_service import resolve_manga_web_ficha


def test_web_ficha_accepts_manga_code_with_inventory_count_read_capability(app):
    from tests.scm.test_scm_kg_custody import _grant_capabilities
    from tests.scm.test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        manga = ScmManga.query.filter_by(codigo="M-OT-OBS-FAB-02").one()
        actor = Trabajador(
            codigo="FICHA-C66",
            nombres="C66",
            apellidos="Consulta",
            activo=True,
        )
        db.session.add(actor)
        db.session.flush()
        _grant_capabilities(actor, ("INVENTARIO_CONTEO_C66",))
        article = ScmArticulo(
            codigo="PC-FICHA-KG",
            nombre="Pieza ficha KG",
            clase="PIEZA_COLOR",
            unidad_inventario="KG",
        )
        db.session.add(article)
        db.session.flush()
        output = ScmOrdenOperacionSalida(
            orden_operacion_id=ScmOrdenOperacion.query.filter_by(
                codigo="OF-OBS-001"
            ).one().id,
            articulo_scm_id=article.id,
            cantidad_objetivo=1,
            excedente_objetivo=0,
        )
        db.session.add(output)
        db.session.flush()
        lot = ScmLoteArticulo(
            codigo="LOTE-FICHA-KG",
            articulo_id=article.id,
            clase="SALIDA_ORDEN_OPERACION",
            orden_operacion_salida_id=output.id,
            cantidad_acreditada=1,
        )
        db.session.add(lot)
        db.session.flush()
        manga.lote_articulo_id = lot.id
        print_job = ScmTrabajoImpresionManga(
            public_id=uuid4(),
            estado="PROCESADO",
            payload_hash="d" * 64,
            solicitado_por_id=seeded["full"].id,
        )
        db.session.add(print_job)
        db.session.flush()
        label = ScmEtiquetaManga(
            manga_id=manga.id,
            trabajo_impresion_id=print_job.public_id,
            tipo="PREPESAJE",
            version=1,
            estado="IMPRESA",
            payload_json={},
            payload_hash="e" * 64,
        )
        db.session.add(label)
        db.session.commit()

        payload = resolve_manga_web_ficha(
            db.session, actor_id=actor.id, entrada=manga.codigo.lower()
        )
        qr_payload = resolve_manga_web_ficha(
            db.session,
            actor_id=actor.id,
            entrada='{"v":1,"label_id":"%s"}' % label.public_id,
        )

        assert payload["entrada"] == {
            "tipo": "MANGA_CODE",
            "label_id": None,
            "codigo": manga.codigo,
        }
        assert payload["manga"]["codigo"] == manga.codigo
        assert payload["pesaje"]["peso_neto_kg"] == "11.500"
        assert payload["pesaje"]["origen"] == "CORRECCION_AUTORIZADA"
        assert payload["acciones"]["reabrir"]["permitida_por_capacidad"] is False
        assert qr_payload["manga"]["id"] == payload["manga"]["id"]
        assert qr_payload["entrada"]["tipo"] == "QR_JSON"
        assert qr_payload["etiqueta"]["sustituida"] is False
        assert "historial" not in payload
        assert "trabajadores" not in payload
        assert seeded["full"].id != actor.id


def test_web_ficha_rejects_account_without_any_read_capability(app):
    from tests.scm.test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        manga = ScmManga.query.filter_by(codigo="M-OT-OBS-FAB-02").one()

        try:
            resolve_manga_web_ficha(
                db.session, actor_id=seeded["denied"].id, entrada=manga.codigo
            )
        except ScmServiceError as error:
            assert error.code == "CAPABILITY_REQUIRED"
            assert error.details["capabilities_any"] == [
                "MANGA_PESAJE_VER",
                "INVENTARIO_VER",
                "INVENTARIO_CONTEO_C66",
            ]
        else:
            raise AssertionError("La ficha no debe abrirse sin capacidad de lectura")


def test_web_ficha_route_keeps_query_read_only_and_rejects_bad_qr(app, client):
    from tests.scm.test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        before = {
            "mangas": db.session.query(ScmManga).count(),
            "version": ScmManga.query.filter_by(codigo="M-OT-OBS-FAB-02").one().version,
        }
        response = client.get(
            "/api/scm/v1/mangas/ficha?entrada=%7Bbad-json",
            headers={"X-Actor-Id": str(seeded["full"].id)},
        )
        assert response.status_code == 422
        assert response.get_json()["error"]["code"] == "MANGA_IDENTIFIER_INVALID"
        after = {
            "mangas": db.session.query(ScmManga).count(),
            "version": ScmManga.query.filter_by(codigo="M-OT-OBS-FAB-02").one().version,
        }
        assert after == before


def test_kg_control_close_reopen_and_continue_only_projects_delta(app):
    from decimal import Decimal
    from uuid import UUID

    from app.models.scm_inventory import ScmUbicacionInventario
    from app.models.scm_inventory_kg import (
        ScmMovimientoInventarioKg,
        ScmSaldoInventarioKg,
    )
    from app.services.scm_kg_production_service import close_kg_from_last_control
    from app.services.scm_ot_service import transition_color_work
    from app.services.scm_weighing_service import register_manga_weighing_control, reopen_manga_after_accidental_close
    from tests.scm.test_scm_kg_production import _print_color_manga, _seed_aggregate_color_work
    from tests.scm.test_scm_kg_custody import _grant_capabilities

    with app.app_context():
        app.config.update(
            KG_AUTOMATIC_INTAKE_ENABLED=True,
            KG_PRODUCTION_LOCATION_CODE="FICHA-CLOSE-LOC",
        )
        creator, _approver, _order, _run, _output, _line, _header, created = _seed_aggregate_color_work(quantity=120)
        manga = ScmManga.query.filter_by(public_id=UUID(created["mangas"][0]["public_id"])).one()
        manga.lote_articulo.articulo.unidad_inventario = "KG"
        db.session.add(ScmUbicacionInventario(
            codigo="FICHA-CLOSE-LOC", nombre="Ficha close production",
            clases_articulo_json=["PIEZA_COLOR"], activo=True,
            tipo="PUNTO_PRODUCCION", permite_saldo_libre=True,
        ))
        db.session.flush()
        transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(), data={"version": created["trabajo_color"]["version"]}, action="iniciar",
        )
        station, label = _print_color_manga(
            actor=creator, manga_id=manga.public_id, station_code="FICHA-CLOSE-STATION"
        )
        first = register_manga_weighing_control(
            db.session, station_id=station.station_id, operation_id=uuid4(), actor_id=creator.id,
            data={
                "label_id": label["public_id"], "capture_id": str(uuid4()),
                "peso_bruto_kg": "5.100", "tara_kg": "0.100", "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-18T16:55:00-05:00", "reading_stable": True,
                "control_type": "AVANCE_KG",
            },
        )
        _grant_capabilities(creator, (
            "MANGA_FINALIZAR_PARCIAL", "MANGA_REABRIR", "MANGA_PESAJE_VER",
        ))
        db.session.commit()
        closed = close_kg_from_last_control(
            db.session, actor_id=creator.id, manga_id=manga.public_id,
            operation_id=uuid4(), data={"motivo": "Cierre de control", "version": manga.version},
        )
        assert closed["cierre"]["simula_pesaje"] is False
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal("5.000")
        assert ScmMovimientoInventarioKg.query.count() == 1
        ficha_closed = resolve_manga_web_ficha(
            db.session, actor_id=creator.id, entrada=manga.codigo
        )
        assert ficha_closed["pesaje"]["peso_neto_kg"] == "5.000"
        assert ficha_closed["pesaje"]["origen"] == "CONTROL_CIERRE"

        db.session.refresh(manga)
        reopened = reopen_manga_after_accidental_close(
            db.session, actor_id=creator.id, manga_id=manga.public_id,
            operation_id=uuid4(), data={
                "version": manga.version, "tipo_reapertura": "CONTINUAR_LLENADO",
                "motivo": "Completar llenado", "evidencia": "FICHA-CONTROL",
            },
        )
        assert reopened["linea_base"]["peso_neto_kg"] == "5.000"
        assert reopened["pesaje_invalidado"] is None
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal("5.000")
        assert ScmMovimientoInventarioKg.query.count() == 1
        ficha_reopened = resolve_manga_web_ficha(
            db.session, actor_id=creator.id, entrada=manga.codigo
        )
        assert ficha_reopened["pesaje"]["peso_neto_kg"] == "5.000"
        assert ficha_reopened["pesaje"]["origen"] == "CONTROL_REABIERTO"
        assert ficha_reopened["acciones"]["cerrar_desde_control"]["elegible"] is False
        assert ficha_reopened["acciones"]["cerrar_desde_control"]["razon"] == "KG_CONTROL_REQUIRED"

        second = register_manga_weighing_control(
            db.session, station_id=station.station_id, operation_id=uuid4(), actor_id=creator.id,
            data={
                "label_id": label["public_id"], "capture_id": str(uuid4()),
                "peso_bruto_kg": "5.800", "tara_kg": "0.100", "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-18T17:05:00-05:00", "reading_stable": True,
                "control_type": "AVANCE_KG",
            },
        )
        assert second["inventario_kg"]["delta_kg"] == "0.700"
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal("5.700")
        assert ScmMovimientoInventarioKg.query.count() == 2
        assert first["control"]["peso_neto_kg"] == "5.000"
        ficha_progress = resolve_manga_web_ficha(
            db.session, actor_id=creator.id, entrada=manga.codigo
        )
        assert ficha_progress["pesaje"]["peso_neto_kg"] == "5.700"
        assert ficha_progress["pesaje"]["origen"] == "CONTROL_PESO"
        db.session.refresh(manga)
        close_operation = uuid4()
        close_version = manga.version
        final_close = close_kg_from_last_control(
            db.session, actor_id=creator.id, manga_id=manga.public_id,
            operation_id=close_operation,
            data={"motivo": "Cierre final de control", "version": close_version},
        )
        replay = close_kg_from_last_control(
            db.session, actor_id=creator.id, manga_id=manga.public_id,
            operation_id=close_operation,
            data={"motivo": "Cierre final de control", "version": close_version},
        )
        assert replay == final_close
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal("5.700")
        assert ScmMovimientoInventarioKg.query.count() == 2


def test_reopen_blocks_retired_kg_unit_without_mutating_custody(app):
    from decimal import Decimal
    from app.models.scm_inventory_kg import (
        ScmExistenciaMangaKg,
        ScmMovimientoInventarioKg,
        ScmSaldoInventarioKg,
        ScmUnidadFisicaKg,
    )
    from app.models.scm_ot import ScmPesajeManga
    from app.services.scm_weighing_service import reopen_manga_after_accidental_close
    from tests.scm.test_scm_kg_production import _auto_final_kg_fixture, _grant_capabilities

    with app.app_context():
        _creator, approver, manga, _station, _prelabel, _weighed = _auto_final_kg_fixture(
            app, station_code="FICHA-REOPEN-RETIRED"
        )
        _grant_capabilities(approver, ("MANGA_REABRIR", "MANGA_PESAJE_VER"))
        db.session.commit()
        existence = ScmExistenciaMangaKg.query.one()
        unit = db.session.get(ScmUnidadFisicaKg, existence.unidad_fisica_kg_id)
        saldo = db.session.get(ScmSaldoInventarioKg, existence.saldo_id)
        unit_state = (unit.estado, unit.estado_logistico, unit.version)
        manga_state = (manga.estado, manga.version)
        weighing_count = ScmPesajeManga.query.filter_by(manga_id=manga.id).count()
        movement_count = ScmMovimientoInventarioKg.query.count()
        unit.estado_logistico = "RETIRADA_ARMADO"
        unit.saldo_id = None
        unit.ubicacion_id = None
        db.session.commit()
        ficha = resolve_manga_web_ficha(
            db.session, actor_id=approver.id, entrada=manga.codigo
        )
        assert ficha["acciones"]["reabrir"]["elegible"] is False
        assert ficha["acciones"]["reabrir"]["razon"] == "KG_REOPEN_DOWNSTREAM_REQUIRED"
        assert ficha["acciones"]["reabrir"]["por_tipo"][1]["razon"] == "KG_REOPEN_DOWNSTREAM_REQUIRED"

        with pytest.raises(ScmServiceError) as blocked:
            reopen_manga_after_accidental_close(
                db.session, actor_id=approver.id, manga_id=manga.public_id,
                operation_id=uuid4(), data={
                    "version": manga.version,
                    "tipo_reapertura": "CONTINUAR_LLENADO",
                    "motivo": "Intento sobre unidad retirada",
                },
            )
        assert blocked.value.code == "KG_REOPEN_DOWNSTREAM_REQUIRED"
        db.session.refresh(manga)
        db.session.refresh(unit)
        db.session.refresh(existence)
        db.session.refresh(saldo)
        assert (manga.estado, manga.version) == manga_state
        assert (unit.estado, unit.estado_logistico, unit.version) == (
            "ACTIVA", "RETIRADA_ARMADO", unit_state[2]
        )
        assert (unit.saldo_id, unit.ubicacion_id) == (None, None)
        assert existence.estado_logistico == "DISPONIBLE_PRODUCCION"
        assert Decimal(saldo.cantidad_fisica_kg) == Decimal("12.000")
        assert ScmMovimientoInventarioKg.query.count() == movement_count
        assert ScmPesajeManga.query.filter_by(manga_id=manga.id).count() == weighing_count


def test_control_reopen_migration_creates_audited_ledger_and_safe_downgrade():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE trabajador (id INTEGER PRIMARY KEY)",
            "CREATE TABLE scm_manga (id INTEGER PRIMARY KEY)",
            "CREATE TABLE scm_control_peso_manga (id INTEGER PRIMARY KEY)",
            "CREATE TABLE scm_operacion (operation_id CHAR(32) PRIMARY KEY)",
        ):
            connection.execute(text(statement))
        migration = importlib.import_module(
            "migrations.versions.mesa26011906_reopen_kg_control_close"
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        assert "scm_reapertura_control_peso_kg" in inspector.get_table_names()
        columns = {
            column["name"]
            for column in inspector.get_columns("scm_reapertura_control_peso_kg")
        }
        assert {"manga_id", "control_id", "peso_base_neto_kg", "operation_id"} <= columns
        migration.downgrade()
        assert "scm_reapertura_control_peso_kg" not in inspect(connection).get_table_names()
        migration.upgrade()
        connection.execute(text("INSERT INTO trabajador (id) VALUES (1)"))
        connection.execute(text("INSERT INTO scm_manga (id) VALUES (1)"))
        connection.execute(text("INSERT INTO scm_control_peso_manga (id) VALUES (1)"))
        connection.execute(text("INSERT INTO scm_operacion (operation_id) VALUES ('op-1')"))
        connection.execute(text("""
            INSERT INTO scm_reapertura_control_peso_kg (
                public_id, manga_id, control_id, peso_base_neto_kg,
                motivo, reabierta_por_id, operation_id
            ) VALUES ('public-1', 1, 1, 5.000, 'Continuar', 1, 'op-1')
        """))
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""
                    INSERT INTO scm_reapertura_control_peso_kg (
                        public_id, manga_id, control_id, peso_base_neto_kg,
                        motivo, reabierta_por_id, operation_id
                    ) VALUES ('public-2', 1, 1, 5.000, 'Duplicado', 1, 'op-1')
                """))
        with pytest.raises(RuntimeError, match="existen reaperturas"):
            migration.downgrade()
