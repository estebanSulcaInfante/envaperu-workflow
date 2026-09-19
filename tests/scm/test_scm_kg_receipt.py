from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_inventory import (
    ScmMovimientoInventario,
    ScmSaldoInventario,
    ScmUbicacionInventario,
)
from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg, ScmSaldoInventarioKg
from app.models.scm_ot import ScmManga
from app.models.scm_ot import ScmPesajeManga
from app.models.trabajador import RolOperativo, Trabajador
from app.models.scm_warehouse import ScmExistenciaManga
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_kg_service import activate_article_for_kg
from app.services.scm_kg_service import explore_kg_balances
from app.services.scm_kg_service import list_kg_balances, list_kg_movements
from app.services.scm_inventory_service import explore_inventory_balances
from app.services.scm_ot_service import acknowledge_station_print_job, transition_color_work
from app.services.scm_warehouse_service import receive_manga, resolve_receiving_label
from app.services.scm_service_support import ScmServiceError

from test_scm_ot_service import _print_color_manga, _seed_aggregate_color_work


def _prepare_kg_receipt(app, article_class="PIEZA_COLOR"):
    """Create one final weighed KG manga and its authorized receiver."""
    ensure_initial_scm_configuration()
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
    article = manga.lote_articulo.articulo
    article.clase = article_class
    db.session.flush()
    work = transition_color_work(
        db.session, actor_id=creator.id,
        work_id=UUID(created["trabajo_color"]["id"]), operation_id=uuid4(),
        data={"version": created["trabajo_color"]["version"]}, action="iniciar",
    )["trabajo_color"]
    station, label = _print_color_manga(
        actor=creator, manga_id=created["mangas"][0]["public_id"],
        station_code=f"PESAJE-KG-{uuid4().hex[:8]}",
    )
    from app.services.scm_weighing_service import confirm_manga_weighing
    weighed = confirm_manga_weighing(
        db.session, station_id=station.station_id, operation_id=uuid4(),
        actor_id=creator.id,
        data={
            "label_id": label["public_id"], "capture_id": str(uuid4()),
            "peso_bruto_kg": "12.100", "tara_kg": "0.100",
            "tara_fuente": "TIPO_MANGA", "pesada_at": "2026-08-11T16:55:00-05:00",
            "reading_stable": True,
        },
    )
    acknowledge_station_print_job(
        db.session, station_id=station.station_id,
        print_job_id=UUID(weighed["print_job_id"]),
        data={"results": [{"label_id": weighed["post_label"]["public_id"], "estado": "IMPRESA", "printer_name": "TSC"}]},
    )
    warehouse_role = RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
    warehouse_actor = Trabajador(
        codigo=f"TRB-KG-{uuid4().hex[:8]}", nombres="UAT", apellidos="KG",
        activo=True, roles=[warehouse_role],
    )
    db.session.add(warehouse_actor)
    db.session.flush()
    activate_article_for_kg(db.session, article_id=article.id)
    db.session.commit()
    app.config["KG_RECEIPT_WRITE_ENABLED"] = True
    candidate = resolve_receiving_label(
        db.session, actor_id=warehouse_actor.id,
        label_id=UUID(weighed["post_label"]["public_id"]),
    )
    return {
        "creator": creator, "actor": warehouse_actor, "article": article,
        "manga": manga, "label_id": UUID(weighed["post_label"]["public_id"]),
        "candidate": candidate, "location": "RECEPCION_PIEZAS_WIP",
    }


def _receive_data(ctx, source=None):
    return {
        "label_id": str(ctx["label_id"]),
        "ubicacion_codigo": ctx["location"],
        "presencia_confirmada": True,
        "bolsa_cerrada": True,
        "coincidencia_etiquetas": True,
        "expected_weighing_source": source or ctx["candidate"]["expected_weighing_source"],
    }


def test_kg_article_contract_exposes_separate_ledger(app):
    with app.app_context():
        article = ScmArticulo(
            codigo="KG-RED-001",
            nombre="Pieza KG RED",
            clase="PIEZA_COLOR",
            unidad_inventario="KG",
        )
        db.session.add(article)
        db.session.flush()

        assert article.unidad_base == "UN"
        assert article.unidad_inventario == "KG"

        location = ScmUbicacionInventario(
            codigo="KG-RED-LOC",
            nombre="Ubicacion KG RED",
            clases_articulo_json=["PIEZA_COLOR"],
        )
        db.session.add(location)
        db.session.flush()

        from app.models.scm_inventory_kg import ScmSaldoInventarioKg

        balance = ScmSaldoInventarioKg(
            articulo_scm_id=article.id,
            ubicacion_id=location.id,
            cantidad_fisica_kg=Decimal("12.000"),
            cantidad_no_disponible_kg=Decimal("12.000"),
        )
        db.session.add(balance)
        db.session.flush()
        assert balance.unidad == "KG"


def test_kg_explorer_cursor_is_paginated_and_not_reusable_as_un(app):
    with app.app_context():
        ensure_initial_scm_configuration()
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
        )
        rows = []
        for index in range(30):
            article = ScmArticulo(
                codigo=f"KG-PAGE-{index:03d}",
                nombre=f"Pieza KG pagina {index}",
                clase="PIEZA_COLOR",
                unidad_inventario="KG",
            )
            location = ScmUbicacionInventario(
                codigo=f"KG-PAGE-LOC-{index:03d}",
                nombre=f"Ubicacion KG pagina {index}",
                clases_articulo_json=["PIEZA_COLOR"],
            )
            db.session.add_all([article, location])
            rows.append((article, location))
        db.session.flush()
        for article, location in rows:
            db.session.add(ScmSaldoInventarioKg(
                articulo_scm_id=article.id,
                ubicacion_id=location.id,
                cantidad_fisica_kg=Decimal("1.000"),
            ))
        db.session.commit()

        first = explore_kg_balances(
            db.session, actor_id=actor.id, limit=25,
        )
        assert len(first["items"]) == 25
        assert first["page"]["has_more"] is True
        cursor = first["page"]["next_cursor"]
        assert cursor
        second = explore_kg_balances(
            db.session, actor_id=actor.id, limit=25, cursor=cursor,
        )
        assert len(second["items"]) == 5
        assert set(item["id"] for item in first["items"]).isdisjoint(
            item["id"] for item in second["items"]
        )
        with pytest.raises(ScmServiceError) as wrong_ledger:
            explore_inventory_balances(
                db.session,
                actor_id=actor.id,
                ledger="PIEZAS_WIP",
                limit=25,
                cursor=cursor,
            )
        assert wrong_ledger.value.code == "INVALID_INVENTORY_CURSOR"


@pytest.mark.parametrize("article_class", ["PIEZA_COLOR", "SUBENSAMBLE_WIP"])
def test_kg_receipt_is_net_measured_and_not_plan_units(app, article_class):
    with app.app_context():
        ensure_initial_scm_configuration()
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=120)
        )
        # The shared fixture may split a 120-unit assignment at the default
        # 100-unit packaging capacity. For this isolated KG scenario, the
        # selected manga is the synthetic 120-UN plan documented by KG001.
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
            station_code="PESAJE-KG-RED",
        )
        from app.services.scm_weighing_service import confirm_manga_weighing

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
            codigo="TRB-KG-RED",
            nombres="UAT",
            apellidos="KG",
            activo=True,
            roles=[warehouse_role],
        )
        db.session.add(warehouse_actor)
        db.session.flush()
        manga = ScmManga.query.filter_by(public_id=UUID(created["mangas"][0]["public_id"])).one()
        article = manga.lote_articulo.articulo
        article.clase = article_class
        activate_article_for_kg(db.session, article_id=article.id)
        db.session.commit()
        app.config["KG_RECEIPT_WRITE_ENABLED"] = True

        candidate = resolve_receiving_label(
            db.session,
            actor_id=warehouse_actor.id,
            label_id=UUID(weighed["post_label"]["public_id"]),
        )
        assert candidate["cantidad_confirmada"] == "120.000"
        assert candidate["peso_neto_kg"] == "12.000"
        received = receive_manga(
            db.session,
            actor_id=warehouse_actor.id,
            operation_id=uuid4(),
            data={
                "label_id": weighed["post_label"]["public_id"],
                "ubicacion_codigo": "RECEPCION_PIEZAS_WIP",
                "presencia_confirmada": True,
                "bolsa_cerrada": True,
                "coincidencia_etiquetas": True,
                "expected_weighing_source": candidate["expected_weighing_source"],
            },
        )
        assert received["existencia"]["unidad"] == "KG"
        assert received["existencia"]["cantidad_fisica"] == "12.000"
        assert received["existencia"]["cantidad_no_disponible"] == "12.000"
        assert received["existencia"]["cantidad_libre"] == "0.000"
        assert ScmSaldoInventario.query.filter_by(articulo_scm_id=article.id).count() == 0
        assert ScmMovimientoInventario.query.count() == 0
        assert ScmExistenciaManga.query.filter_by(articulo_scm_id=article.id).count() == 0
        assert ScmSaldoInventarioKg.query.filter_by(articulo_scm_id=article.id).one().cantidad_fisica_kg == Decimal("12.000")
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert ScmExistenciaMangaKg.query.filter_by(articulo_scm_id=article.id).count() == 1

        recovered = resolve_receiving_label(
            db.session,
            actor_id=warehouse_actor.id,
            label_id=UUID(weighed["post_label"]["public_id"]),
        )
        assert recovered["received"] is True
        assert recovered["existencia"]["cantidad_fisica"] == "12.000"
        app.config["KG_RECEIPT_WRITE_ENABLED"] = False
        with pytest.raises(ScmServiceError) as disabled:
            receive_manga(
                db.session,
                actor_id=warehouse_actor.id,
                operation_id=uuid4(),
                data={
                    "label_id": weighed["post_label"]["public_id"],
                    "ubicacion_codigo": "RECEPCION_PIEZAS_WIP",
                    "presencia_confirmada": True,
                    "bolsa_cerrada": True,
                    "coincidencia_etiquetas": True,
                    "expected_weighing_source": candidate["expected_weighing_source"],
                },
            )
        assert disabled.value.code == "KG_OPERATION_NOT_ENABLED"


def test_kg_receipt_rejects_stale_source_without_stock_side_effect(app):
    with app.app_context():
        ctx = _prepare_kg_receipt(app)
        stale = dict(ctx["candidate"]["expected_weighing_source"])
        stale["projection_sha256"] = "0" * 64
        with pytest.raises(ScmServiceError) as conflict:
            receive_manga(
                db.session, actor_id=ctx["actor"].id, operation_id=uuid4(),
                data=_receive_data(ctx, stale),
            )
        assert conflict.value.code == "PESAJE_VERSION_CONFLICT"
        assert ScmMovimientoInventarioKg.query.count() == 0
        assert ScmExistenciaMangaKg.query.count() == 0


def test_kg_read_paths_remain_available_when_write_flag_is_off(app):
    with app.app_context():
        ctx = _prepare_kg_receipt(app)
        ctx["actor"].roles.append(
            RolOperativo.query.filter_by(codigo="PREPARADOR_MATERIAL").one()
        )
        app.config["KG_RECEIPT_WRITE_ENABLED"] = False
        resolved = resolve_receiving_label(
            db.session, actor_id=ctx["actor"].id, label_id=ctx["label_id"]
        )
        assert resolved.get("received") is not True
        assert list_kg_balances(db.session, actor_id=ctx["actor"].id)["unidad"] == "KG"
        assert list_kg_movements(db.session, actor_id=ctx["actor"].id)["unidad"] == "KG"
        assert explore_kg_balances(db.session, actor_id=ctx["actor"].id)["unidad"] == "KG"


def test_kg_replay_scope_revocation_hides_persisted_result(app):
    with app.app_context():
        ctx = _prepare_kg_receipt(app)
        actor = ctx["actor"]
        actor.roles.append(RolOperativo.query.filter_by(codigo="PREPARADOR_MATERIAL").one())
        warehouse = ScmAlmacen(codigo="KG-SCOPE-REPLAY", nombre="Scope replay", tipo="PIEZAS_WIP")
        db.session.add(warehouse)
        db.session.flush()
        location = ScmUbicacionInventario.query.filter_by(codigo=ctx["location"]).one()
        location.almacen_id = warehouse.id
        assignment = ScmAlmacenTrabajador(
            almacen_id=warehouse.id, trabajador_id=actor.id,
            clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=actor.id,
        )
        db.session.add(assignment)
        db.session.commit()
        operation_id = uuid4()
        receive_manga(
            db.session, actor_id=actor.id, operation_id=operation_id,
            data=_receive_data(ctx),
        )
        assignment.activo = False
        db.session.commit()
        with pytest.raises(ScmServiceError) as revoked:
            receive_manga(
                db.session, actor_id=actor.id, operation_id=operation_id,
                data=_receive_data(ctx),
            )
        assert revoked.value.code in {"LOCATION_NOT_FOUND", "INVENTORY_SCOPE_FORBIDDEN"}
        assert ScmExistenciaMangaKg.query.count() == 1


def test_kg_different_key_cannot_disclose_receipt_outside_location_scope(app):
    with app.app_context():
        ctx = _prepare_kg_receipt(app)
        actor = ctx["actor"]
        actor.roles.append(RolOperativo.query.filter_by(codigo="PREPARADOR_MATERIAL").one())
        warehouse_a = ScmAlmacen(codigo="KG-SCOPE-A", nombre="Scope A", tipo="PIEZAS_WIP")
        warehouse_b = ScmAlmacen(codigo="KG-SCOPE-B", nombre="Scope B", tipo="PIEZAS_WIP")
        db.session.add_all([warehouse_a, warehouse_b])
        db.session.flush()
        location = ScmUbicacionInventario.query.filter_by(codigo=ctx["location"]).one()
        location.almacen_id = warehouse_b.id
        db.session.add(ScmAlmacenTrabajador(
            almacen_id=warehouse_a.id, trabajador_id=actor.id,
            clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=actor.id,
        ))
        db.session.commit()
        with pytest.raises(ScmServiceError) as forbidden:
            receive_manga(
                db.session, actor_id=actor.id, operation_id=uuid4(),
                data=_receive_data(ctx),
            )
        assert forbidden.value.code == "INVENTORY_SCOPE_FORBIDDEN"
        assert "existencia" not in (forbidden.value.details or {})


def test_kg_list_scope_filters_classes_within_same_warehouse(app):
    with app.app_context():
        ensure_initial_scm_configuration()
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(RolOperativo.query.filter_by(codigo="PREPARADOR_MATERIAL").one())
        warehouse = ScmAlmacen(codigo="KG-SCOPE-CLASS", nombre="Scope classes", tipo="PIEZAS_WIP")
        db.session.add(warehouse)
        db.session.flush()
        location = ScmUbicacionInventario(codigo="KG-SCOPE-CLASS-LOC", nombre="Class loc", almacen_id=warehouse.id, clases_articulo_json=["PIEZA_COLOR", "SUBENSAMBLE_WIP"])
        piece = ScmArticulo(codigo="KG-SCOPE-PIECE", nombre="Piece", clase="PIEZA_COLOR", unidad_inventario="KG")
        wip = ScmArticulo(codigo="KG-SCOPE-WIP", nombre="WIP", clase="SUBENSAMBLE_WIP", unidad_inventario="KG")
        db.session.add_all([location, piece, wip])
        db.session.flush()
        db.session.add_all([
            ScmSaldoInventarioKg(articulo_scm_id=piece.id, ubicacion_id=location.id, cantidad_fisica_kg=Decimal("1")),
            ScmSaldoInventarioKg(articulo_scm_id=wip.id, ubicacion_id=location.id, cantidad_fisica_kg=Decimal("2")),
            ScmAlmacenTrabajador(almacen_id=warehouse.id, trabajador_id=actor.id, clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=actor.id),
        ])
        db.session.commit()
        items = list_kg_balances(db.session, actor_id=actor.id)["items"]
        assert [item["articulo"]["codigo"] for item in items] == ["KG-SCOPE-PIECE"]
