"""KG005-KG007 production evidence tests."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app import db
from app.models.scm_ot import (
    ScmAtribucionProduccionKg,
    ScmCierreProductivoKg,
    ScmControlPesoManga,
    ScmManga,
    ScmPesajeManga,
    ScmTramoMangaTrabajo,
    ScmTrabajoOt,
    ScmLoteArticulo,
)
from app.models.scm_articulos import ScmArticulo
from app.models.scm_production_orders import ScmOrdenOperacion, ScmOrdenOperacionSalida
from app.models.trabajador import RolOperativo, Trabajador
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_inventory import ScmMovimientoInventario, ScmSaldoInventario, ScmUbicacionInventario
from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg, ScmSaldoInventarioKg
from app.services.scm_kg_production_service import (
    close_kg_from_last_control,
    close_productive_document_kg,
    preview_kg_attribution,
    record_kg_production_evidence,
)
from app.services.scm_ot_service import (
    acknowledge_station_print_job,
    add_color_work,
    create_fabrication_ot,
    create_fabrication_ot_header,
    transition_ot,
    list_pending_manga_continuities,
    recalculate_fabrication_manga_plan,
    transition_color_work,
    _serialize_ot,
)
from app.services.scm_weighing_service import (
    annul_manga_weighing,
    approve_weighing_correction,
    confirm_manga_weighing,
    register_manga_weighing_control,
    request_weighing_correction,
    reopen_manga_after_accidental_close,
)
from app.services.scm_fabrication_order_service import close_fabrication_order
from app.services.scm_service_support import ScmServiceError
from app.services.scm_warehouse_service import resolve_receiving_label
from tests.scm.test_scm_kg_receipt import _print_color_manga, _seed_aggregate_color_work
from tests.scm.test_scm_kg_custody import _grant_capabilities
from tests.scm.test_scm_inline_assembly import (
    _plan_and_assign,
    _seed_concurrent_wip_flow,
)
from tests.scm.test_scm_ot_service import _seed_fabrication_order


def _weigh_kg_fixture(*, quantity=120):
    creator, _approver, _order, _run, _output, _line, _header, created = (
        _seed_aggregate_color_work(quantity=quantity)
    )
    manga = ScmManga.query.filter_by(
        public_id=UUID(created["mangas"][0]["public_id"])
    ).one()
    manga.lote_articulo.articulo.unidad_inventario = "KG"
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
        station_code="PESAJE-KG005-EVIDENCE",
    )
    weighing_data = {
        "label_id": label["public_id"],
        "capture_id": str(uuid4()),
        "peso_bruto_kg": "12.100",
        "tara_kg": "0.100",
        "tara_fuente": "TIPO_MANGA",
        "pesada_at": "2026-09-18T16:55:00-05:00",
        "reading_stable": True,
    }
    weighed = confirm_manga_weighing(
        db.session,
        station_id=station.station_id,
        operation_id=uuid4(),
        actor_id=creator.id,
        data=weighing_data,
    )
    return creator, work, manga, weighed


def _auto_final_kg_fixture(app, *, station_code):
    app.config["KG_AUTOMATIC_INTAKE_ENABLED"] = True
    app.config["KG_PRODUCTION_LOCATION_CODE"] = f"{station_code}-LOC"
    creator, approver, _order, _run, _output, _line, _header, created = (
        _seed_aggregate_color_work(quantity=120)
    )
    manga = ScmManga.query.filter_by(
        public_id=UUID(created["mangas"][0]["public_id"])
    ).one()
    manga.lote_articulo.articulo.unidad_inventario = "KG"
    db.session.add(ScmUbicacionInventario(
        codigo=f"{station_code}-LOC", nombre="Pilot production test",
        clases_articulo_json=["PIEZA_COLOR"], activo=True,
        tipo="PUNTO_PRODUCCION", permite_saldo_libre=True,
    ))
    db.session.flush()
    transition_color_work(
        db.session, actor_id=creator.id,
        work_id=UUID(created["trabajo_color"]["id"]), operation_id=uuid4(),
        data={"version": created["trabajo_color"]["version"]}, action="iniciar",
    )
    station, prelabel = _print_color_manga(
        actor=creator, manga_id=manga.public_id, station_code=station_code,
    )
    weighed = confirm_manga_weighing(
        db.session, station_id=station.station_id, operation_id=uuid4(),
        actor_id=creator.id,
        data={
            "label_id": prelabel["public_id"], "capture_id": str(uuid4()),
            "peso_bruto_kg": "12.100", "tara_kg": "0.100",
            "tara_fuente": "TIPO_MANGA",
            "pesada_at": "2026-09-18T16:55:00-05:00",
            "reading_stable": True,
        },
    )
    return creator, approver, manga, station, prelabel, weighed


def test_kg_wip_weighing_uses_net_without_confirming_un_units(app):
    with app.app_context():
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=120)
        )
        manga = ScmManga.query.filter_by(
            public_id=UUID(created["mangas"][0]["public_id"])
        ).one()
        article = manga.lote_articulo.articulo
        article.unidad_inventario = "KG"
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
            station_code="PESAJE-KG005",
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
                "pesada_at": "2026-09-18T16:55:00-05:00",
                "reading_stable": True,
            },
        )

        weighing = ScmPesajeManga.query.filter_by(
            public_id=UUID(weighed["weighing"]["public_id"])
        ).one()
        db.session.refresh(manga)
        db.session.refresh(manga.trabajo)
        assert Decimal(weighing.peso_fisico_neto_kg) == Decimal("12.000")
        assert Decimal(weighing.cantidad_confirmada) == Decimal("0.000")
        assert manga.cantidad_confirmada_un is None
        assert manga.cantidad_contenida_un is None
        assert Decimal(manga.trabajo.cantidad_confirmada_un) == Decimal("0")
        assert ScmSaldoInventario.query.count() == 0
        assert ScmMovimientoInventario.query.count() == 0

def test_kg_control_rejects_equal_or_lower_and_final_uses_net_once(app):
    with app.app_context():
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=120)
        )
        manga = ScmManga.query.filter_by(
            public_id=UUID(created["mangas"][0]["public_id"])
        ).one()
        manga.lote_articulo.articulo.unidad_inventario = "KG"
        work = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]},
            action="iniciar",
        )
        station, label = _print_color_manga(
            actor=creator,
            manga_id=manga.public_id,
            station_code="PESAJE-KG-CONTROL",
        )
        control_data = {
            "label_id": label["public_id"],
            "capture_id": str(uuid4()),
            "peso_bruto_kg": "5.100",
            "tara_kg": "0.100",
            "tara_fuente": "TIPO_MANGA",
            "pesada_at": "2026-09-18T16:55:00-05:00",
            "reading_stable": True,
            "control_type": "AVANCE_KG",
        }
        first = register_manga_weighing_control(
            db.session,
            station_id=station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data=control_data,
        )
        assert first["control"]["peso_neto_kg"] == "5.000"
        assert first["control"]["aporte_desde_control_anterior_kg"] == "5.000"
        assert ScmControlPesoManga.query.count() == 1
        for net in ("5.000", "4.000"):
            with pytest.raises(ScmServiceError) as rejected:
                register_manga_weighing_control(
                    db.session,
                    station_id=station.station_id,
                    operation_id=uuid4(),
                    actor_id=creator.id,
                    data={
                        **control_data,
                        "capture_id": str(uuid4()),
                        "peso_bruto_kg": f"{Decimal(net) + Decimal('0.100'):.3f}",
                    },
                )
            assert rejected.value.code == "CONTROL_WEIGHT_NOT_MONOTONIC"
        assert ScmControlPesoManga.query.count() == 1
        final = confirm_manga_weighing(
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
                "pesada_at": "2026-09-18T17:00:00-05:00",
                "reading_stable": True,
            },
        )
        assert final["weighing"]["peso_fisico_neto_kg"] == "12.000"
        assert final["weighing"]["cantidad_confirmada"] == "0.000"
        assert ScmControlPesoManga.query.count() == 1


def test_kg_pilot_projects_cumulative_controls_as_deltas_and_replays_without_duplication(app):
    with app.app_context():
        app.config["KG_AUTOMATIC_INTAKE_ENABLED"] = True
        app.config["KG_PRODUCTION_LOCATION_CODE"] = "PILOT_PRODUCTION"
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=120)
        )
        manga = ScmManga.query.filter_by(public_id=UUID(created["mangas"][0]["public_id"])).one()
        manga.lote_articulo.articulo.unidad_inventario = "KG"
        db.session.add(ScmUbicacionInventario(
            codigo="PILOT_PRODUCTION", nombre="Pilot production",
            clases_articulo_json=["PIEZA_COLOR"], activo=True,
            tipo="PUNTO_PRODUCCION", permite_saldo_libre=True,
        ))
        db.session.flush()
        transition_color_work(
            db.session, actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]), operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]}, action="iniciar",
        )
        station, label = _print_color_manga(
            actor=creator, manga_id=manga.public_id, station_code="PESAJE-KG-PILOT-DELTAS",
        )
        base = {
            "label_id": label["public_id"], "tara_kg": "0.100",
            "tara_fuente": "TIPO_MANGA", "pesada_at": "2026-09-18T16:55:00-05:00",
            "reading_stable": True, "control_type": "AVANCE_KG",
        }
        operations = []
        for index, net in enumerate(("5.000", "8.000", "12.000"), 1):
            operation = uuid4(); operations.append(operation)
            payload = {
                **base, "capture_id": str(uuid4()),
                "peso_bruto_kg": f"{Decimal(net) + Decimal('0.100'):.3f}",
            }
            first = register_manga_weighing_control(
                db.session, station_id=station.station_id, operation_id=operation,
                actor_id=creator.id, data=payload,
            )
            assert first["inventario_kg"]["delta_kg"] == ("5.000", "3.000", "4.000")[index - 1]
            if index == 2:
                replay = register_manga_weighing_control(
                    db.session, station_id=station.station_id, operation_id=operation,
                    actor_id=creator.id, data=payload,
                )
                assert replay["control"] == first["control"]
                assert replay["idempotent_replay"] is True
        movements = ScmMovimientoInventarioKg.query.order_by(
            ScmMovimientoInventarioKg.created_at, ScmMovimientoInventarioKg.id,
        ).all()
        assert sorted(Decimal(row.cantidad_delta_kg) for row in movements) == [Decimal("3"), Decimal("4"), Decimal("5")]
        existence = ScmExistenciaMangaKg.query.one()
        balance = ScmSaldoInventarioKg.query.one()
        assert Decimal(existence.cantidad_fisica_kg) == Decimal("12")
        assert Decimal(balance.cantidad_fisica_kg) == Decimal("12")
        assert existence.estado_logistico == "EN_PRODUCCION"
        assert existence.estado_calidad == "SIN_CONTROL"


def test_kg_close_from_last_control_http_requires_capability_and_is_idempotent(
    app, client,
):
    with app.app_context():
        app.config["KG_AUTOMATIC_INTAKE_ENABLED"] = True
        app.config["KG_PRODUCTION_LOCATION_CODE"] = "PILOT_PRODUCTION_HTTP"
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=120)
        )
        manga = ScmManga.query.filter_by(
            public_id=UUID(created["mangas"][0]["public_id"])
        ).one()
        manga.lote_articulo.articulo.unidad_inventario = "KG"
        db.session.add(ScmUbicacionInventario(
            codigo="PILOT_PRODUCTION_HTTP", nombre="Pilot production HTTP",
            clases_articulo_json=["PIEZA_COLOR"], activo=True,
            tipo="PUNTO_PRODUCCION", permite_saldo_libre=True,
        ))
        db.session.flush()
        transition_color_work(
            db.session, actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]), operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]}, action="iniciar",
        )
        station, label = _print_color_manga(
            actor=creator, manga_id=manga.public_id,
            station_code="PESAJE-KG-CLOSE-HTTP",
        )
        control = register_manga_weighing_control(
            db.session, station_id=station.station_id, operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": label["public_id"], "capture_id": str(uuid4()),
                "peso_bruto_kg": "12.100", "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-18T16:55:00-05:00",
                "reading_stable": True, "control_type": "AVANCE_KG",
            },
        )
        manga_public_id = str(manga.public_id)
        actor_id = creator.id
        unauthorized = Trabajador(
            codigo=f"TRB-KG-CLOSE-DENY-{uuid4().hex[:8]}",
            nombres="Sin", apellidos="Permiso", activo=True,
        )
        db.session.add(unauthorized)
        db.session.flush()
        unauthorized_id = unauthorized.id
        version = manga.version
        db.session.commit()

    endpoint = f"/api/scm/v1/mangas/{manga_public_id}/cerrar-desde-control"
    operation_id = str(uuid4())
    denied = client.post(
        endpoint,
        headers={"X-Actor-Id": str(unauthorized_id), "Idempotency-Key": operation_id},
        json={"motivo": "Cierre HTTP"},
    )
    assert denied.status_code == 403

    with app.app_context():
        actor = db.session.get(type(creator), actor_id)
        _grant_capabilities(actor, ("MANGA_FINALIZAR_PARCIAL",))
        db.session.commit()

    missing_version = client.post(
        endpoint,
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
        json={"motivo": "Cierre sin versión"},
    )
    assert missing_version.status_code == 422
    assert missing_version.get_json()["error"]["code"] == "INVALID_VERSION"

    invalid_version = client.post(
        endpoint,
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
        json={"motivo": "Cierre con versión inválida", "version": "abc"},
    )
    assert invalid_version.status_code == 422
    assert invalid_version.get_json()["error"]["code"] == "INVALID_VERSION"

    stale_version = client.post(
        endpoint,
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
        json={"motivo": "Cierre obsoleto", "version": version - 1},
    )
    assert stale_version.status_code == 409
    assert stale_version.get_json()["error"]["code"] == "VERSION_CONFLICT"

    payload = {"motivo": "Cierre administrativo desde control KG", "version": version}
    headers = {"X-Actor-Id": str(actor_id), "Idempotency-Key": operation_id}
    closed = client.post(endpoint, headers=headers, json=payload)
    assert closed.status_code == 200
    body = closed.get_json()
    assert body["cierre"]["tipo"] == "DESDE_ULTIMO_CONTROL"
    assert body["cierre"]["simula_pesaje"] is False
    assert body["control_fuente"]["unidad_evidencia"] == "KG"
    assert body["inventario_kg"]["delta_kg"] == "0.000"

    replay = client.post(endpoint, headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.get_json() == body

    second_close = client.post(
        endpoint,
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
        json=payload,
    )
    assert second_close.status_code == 409
    assert second_close.get_json()["error"]["code"] == "MANGA_CLOSE_FROM_CONTROL_NOT_ALLOWED"

    with app.app_context():
        existence = ScmExistenciaMangaKg.query.one()
        assert existence.estado_logistico == "DISPONIBLE_PRODUCCION"
        assert existence.estado_calidad == "SIN_CONTROL"
        assert ScmMovimientoInventarioKg.query.count() == 1

        warehouse_role = RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        receiver = Trabajador(
            codigo=f"TRB-KG-CLOSE-RECEIVE-{uuid4().hex[:8]}",
            nombres="Recepcion",
            apellidos="KG",
            activo=True,
            roles=[warehouse_role],
        )
        db.session.add(receiver)
        db.session.flush()
        candidate = resolve_receiving_label(
            db.session,
            actor_id=receiver.id,
            label_id=UUID(label["public_id"]),
        )
        assert candidate["received"] is False
        assert candidate["recibible"] is True
        assert candidate["expected_weighing_source"]["pesaje_public_id"] is None


def test_kg_control_close_does_not_pause_sibling_manga_or_color_work(app):
    with app.app_context():
        app.config["KG_AUTOMATIC_INTAKE_ENABLED"] = True
        app.config["KG_PRODUCTION_LOCATION_CODE"] = "PILOT_PRODUCTION_SIBLING"
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=240)
        )
        assert len(created["mangas"]) >= 2
        manga_rows = [
            ScmManga.query.filter_by(public_id=UUID(item["public_id"])).one()
            for item in created["mangas"]
        ]
        manga_rows[0].lote_articulo.articulo.unidad_inventario = "KG"
        db.session.add(ScmUbicacionInventario(
            codigo="PILOT_PRODUCTION_SIBLING", nombre="Pilot production sibling",
            clases_articulo_json=["PIEZA_COLOR"], activo=True,
            tipo="PUNTO_PRODUCCION", permite_saldo_libre=True,
        ))
        db.session.flush()
        transition_color_work(
            db.session, actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]), operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]}, action="iniciar",
        )
        for index, manga in enumerate(manga_rows[:2]):
            station, label = _print_color_manga(
                actor=creator, manga_id=manga.public_id,
                station_code=f"PESAJE-KG-SIBLING-{index}",
            )
            register_manga_weighing_control(
                db.session, station_id=station.station_id, operation_id=uuid4(),
                actor_id=creator.id,
                data={
                    "label_id": label["public_id"], "capture_id": str(uuid4()),
                    "peso_bruto_kg": "12.100", "tara_kg": "0.100",
                    "tara_fuente": "TIPO_MANGA",
                    "pesada_at": f"2026-09-18T16:{55 + index:02d}:00-05:00",
                    "reading_stable": True, "control_type": "AVANCE_KG",
                },
            )
        _grant_capabilities(creator, ("MANGA_FINALIZAR_PARCIAL",))
        db.session.flush()
        close_kg_from_last_control(
            db.session, actor_id=creator.id, manga_id=manga_rows[0].public_id,
            operation_id=uuid4(),
            data={"motivo": "Cierre manga hermana", "version": manga_rows[0].version},
        )
        db.session.refresh(manga_rows[1])
        db.session.refresh(manga_rows[0].trabajo)
        assert manga_rows[1].estado == "EN_LLENADO"
        assert manga_rows[0].trabajo.estado == "EN_EJECUCION"


def test_kg_auto_correction_reopen_reweigh_and_annul_keep_ledger_coherent(app):
    with app.app_context():
        creator, approver, manga, station, prelabel, weighed = _auto_final_kg_fixture(
            app, station_code="PESAJE-KG-AUTO-LIFECYCLE",
        )
        _grant_capabilities(creator, ("PESAJE_CORRECCION_SOLICITAR",))
        _grant_capabilities(approver, (
            "PESAJE_CORRECCION_APROBAR", "MANGA_REABRIR", "PESAJE_ANULAR",
        ))
        db.session.commit()
        weighing_id = UUID(weighed["weighing"]["public_id"])

        correction = request_weighing_correction(
            db.session, actor_id=creator.id, weighing_id=weighing_id,
            operation_id=uuid4(),
            data={
                "proposed": {"peso_bruto_kg": "11.600", "tara_kg": "0.100"},
                "motivo": "Corrección de lectura KG",
            },
        )["correction"]
        corrected = approve_weighing_correction(
            db.session, actor_id=approver.id,
            correction_id=UUID(correction["id"]), operation_id=uuid4(),
            data={"motivo_aprobacion": "Evidencia KG conciliada"},
        )
        assert corrected["ajuste_inventario_kg"]["delta_kg"] == "-0.500"
        balance = ScmSaldoInventarioKg.query.one()
        assert Decimal(balance.cantidad_fisica_kg) == Decimal("11.500")

        manga = db.session.get(ScmManga, manga.id)
        reopened = reopen_manga_after_accidental_close(
            db.session, actor_id=approver.id, manga_id=manga.public_id,
            operation_id=uuid4(),
            data={
                "version": manga.version,
                "motivo": "Reapertura para completar llenado KG",
                "evidencia": "UAT-KG-AUTO",
            },
        )
        assert reopened["manga"]["estado"] == "EN_LLENADO"
        existence = ScmExistenciaMangaKg.query.one()
        assert existence.estado_logistico == "EN_PRODUCCION"
        assert existence.estado_calidad == "SIN_CONTROL"
        assert Decimal(ScmSaldoInventarioKg.query.one().cantidad_fisica_kg) == Decimal("11.500")

        replacement = confirm_manga_weighing(
            db.session, station_id=station.station_id, operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": prelabel["public_id"], "capture_id": str(uuid4()),
                "peso_bruto_kg": "12.600", "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-18T17:00:00-05:00",
                "reading_stable": True,
            },
        )
        assert replacement["inventario_kg"]["delta_kg"] == "1.000"
        assert Decimal(ScmSaldoInventarioKg.query.one().cantidad_fisica_kg) == Decimal("12.500")

        annulled = annul_manga_weighing(
            db.session, actor_id=approver.id,
            weighing_id=UUID(replacement["weighing"]["public_id"]),
            operation_id=uuid4(), data={"motivo": "Anulación de prueba KG"},
        )
        assert annulled["manga"]["estado"] == "ANULADA"
        assert Decimal(ScmSaldoInventarioKg.query.one().cantidad_fisica_kg) == Decimal("0")
        assert ScmExistenciaMangaKg.query.one().estado_logistico == "REVERSADA"


def test_kg_production_evidence_is_idempotent_and_keeps_bom_pending(app):
    with app.app_context():
        creator, _work, _manga, weighed = _weigh_kg_fixture()
        operation_id = uuid4()
        first = record_kg_production_evidence(
            db.session,
            actor_id=creator.id,
            weighing_id=weighed["weighing"]["public_id"],
            operation_id=operation_id,
        )
        replay = record_kg_production_evidence(
            db.session,
            actor_id=creator.id,
            weighing_id=weighed["weighing"]["public_id"],
            operation_id=operation_id,
        )
        assert first["kg_medido"] == "12.000"
        assert first["atribucion_estado"] == "PENDIENTE_BOM"
        assert first["inventario_creado"] is False
        assert replay == first
        assert ScmAtribucionProduccionKg.query.count() == 1
        assert ScmCierreProductivoKg.query.count() == 0


def test_kg_attribution_never_invents_missing_bom_basis():
    assert preview_kg_attribution(net_kg="12.000")["estado"] == "PENDIENTE_BOM"
    with pytest.raises(ScmServiceError) as pending:
        preview_kg_attribution(net_kg="12.000", bom_basis={"componentes": []})
    assert getattr(pending.value, "code", None) == "KG_BOM_ATTRIBUTION_PENDING"


def test_kg_wip_bom_estimate_separates_fresh_and_previous_mass(app):
    basis = {
        "revision": "BOM-KG5-001",
        "componentes": [
            {"codigo": "TAPA", "cantidad": 1, "peso_referencia_kg": "0.100", "origen": "FRESCO"},
            {"codigo": "PICO", "cantidad": 1, "peso_referencia_kg": "0.020", "origen": "STOCK_PREVIO"},
        ],
    }
    preview = preview_kg_attribution(net_kg="12.000", bom_basis=basis)
    assert preview["estado"] == "ESTIMADA_BOM"
    assert preview["kg_fabricacion_estimado"] == "10.000"
    assert preview["kg_previo_estimado"] == "2.000"
    with app.app_context():
        creator, _work, _manga, weighed = _weigh_kg_fixture()
        confirmed_weighing = ScmPesajeManga.query.filter_by(
            public_id=UUID(weighed["weighing"]["public_id"])
        ).one()
        # A weighing station cannot authorize a BOM split.  With no resolvable
        # OA structure this remains measured evidence pending BOM.
        assert confirmed_weighing.atribucion_kg_estado == "PENDIENTE_BOM"
        assert confirmed_weighing.kg_fabricacion_estimado is None
        assert confirmed_weighing.kg_previo_estimado is None
        result = record_kg_production_evidence(
            db.session,
            actor_id=creator.id,
            weighing_id=weighed["weighing"]["public_id"],
            operation_id=uuid4(),
            data={"bom_basis": basis},  # ignored: only frozen OA may classify
        )
        assert result["kg_fabricacion_estimado"] is None
        assert result["kg_previo_estimado"] is None
        assert result["atribucion_estado"] == "PENDIENTE_BOM"
        assert ScmAtribucionProduccionKg.query.count() == 1
        weighing = ScmPesajeManga.query.filter_by(
            public_id=UUID(weighed["weighing"]["public_id"])
        ).one()
        assert weighing.atribucion_kg_estado == "PENDIENTE_BOM"
        assert weighing.kg_fabricacion_estimado is None
        assert weighing.kg_previo_estimado is None
        assert ScmSaldoInventario.query.count() == 0
        assert ScmMovimientoInventario.query.count() == 0

def test_kg_wip_oa_resolves_real_frozen_bom_without_client_basis(app, scm_config):
    """A concurrent OA supplies the fresh component; station sends only weight."""
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        created, assigned = _plan_and_assign(actor, order, center, color_work)
        manga = ScmManga.query.filter_by(
            public_id=UUID(assigned["mangas"][0]["public_id"])
        ).one()
        article = manga.lote_articulo.articulo
        article.unidad_inventario = "KG"
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.flush()
        station, label = _print_color_manga(
            actor=actor,
            manga_id=manga.public_id,
            station_code="PESAJE-KG007-OA",
        )

        weighed = confirm_manga_weighing(
            db.session,
            station_id=station.station_id,
            operation_id=uuid4(),
            actor_id=actor.id,
            data={
                "label_id": label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "12.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-18T16:55:00-05:00",
                "reading_stable": True,
            },
        )
        weighing = ScmPesajeManga.query.filter_by(
            public_id=UUID(weighed["weighing"]["public_id"])
        ).one()
        assert weighing.atribucion_kg_estado == "ESTIMADA_BOM"
        assert weighing.kg_fabricacion_estimado == Decimal("10.000")
        assert weighing.kg_previo_estimado == Decimal("2.000")
        rows = ScmAtribucionProduccionKg.query.filter_by(
            pesaje_id=weighing.id
        ).order_by(ScmAtribucionProduccionKg.id).all()
        assert [row.tipo for row in rows] == [
            "NETO_MEDIDO",
            "FABRICACION_ESTIMADA",
            "COMPONENTE_PREVIO_ESTIMADO",
        ]
        assert rows[0].calidad == "MEDIDA_DIRECTA"
        assert all(row.calidad == "ESTIMADA_BOM" for row in rows[1:])
        assert rows[1].base_json["revision"]
        assert rows[1].base_json["metodo"] == "MASA_REFERENCIA_Q_PESO"
        assert rows[1].trabajo_ot_id == color_work.id
        assert rows[2].trabajo_ot_id is None
        captured_base = rows[1].base_json
        _tapa.pieza_color.pieza_color.peso = Decimal("999")
        db.session.flush()
        replay_after_master_change = record_kg_production_evidence(
            db.session,
            actor_id=actor.id,
            weighing_id=weighing.public_id,
            operation_id=uuid4(),
        )
        db.session.expire_all()
        persisted_estimate = ScmAtribucionProduccionKg.query.filter_by(
            pesaje_id=weighing.id,
            tipo="FABRICACION_ESTIMADA",
        ).one()
        assert replay_after_master_change["atribucion_estado"] == "ESTIMADA_BOM"
        assert persisted_estimate.base_json == captured_base
        # This flow must remain KG-only; the frozen BOM estimate is not stock.
        assert ScmSaldoInventario.query.count() == 0
        assert ScmMovimientoInventario.query.count() == 0
        closed = close_productive_document_kg(
            db.session,
            actor_id=actor.id,
            documento_tipo="OA",
            documento_id=order.id,
            operation_id=uuid4(),
            data={"version": order.version},
        )
        assert closed["kg_medido"] == "12.000"
        assert closed["cierre"]["kg_fabricacion_estimado"] == "10.000"
        assert closed["cierre"]["kg_previo_estimado"] == "2.000"
        assert closed["inventario_creado"] is False
        db.session.refresh(order)
        assert order.estado == "CERRADA"
        source_ot = db.session.get(
            RegistroDiarioProduccion, color_work.orden_trabajo_id
        )
        source_of = db.session.get(ScmOrdenOperacion, color_work.orden_operacion_id)
        source_ot_close = transition_ot(
            db.session,
            actor_id=actor.id,
            public_id=source_ot.public_id,
            operation_id=uuid4(),
            data={"version": source_ot.version},
            action="cerrar",
        )
        assert Decimal(source_ot_close["kg_medido"]) == Decimal("0")
        assert source_ot_close["cierre"]["kg_fabricacion_estimado"] == "10.000"
        assert source_ot_close["cierre"]["kg_previo_estimado"] is None
        source_of_close = close_fabrication_order(
            db.session,
            actor_id=actor.id,
            operation_id=uuid4(),
            operation_order_id=source_of.id,
            data={"version": source_of.version},
        )
        assert Decimal(source_of_close["kg_medido"]) == Decimal("0")
        assert source_of_close["cierre"]["kg_fabricacion_estimado"] == "10.000"
        assert source_of_close["cierre"]["kg_previo_estimado"] is None
        assert sum(
            Decimal(row.cantidad_kg)
            for row in ScmAtribucionProduccionKg.query.filter_by(
                tipo="FABRICACION_ESTIMADA"
            ).all()
        ) == Decimal("10.000")
        assert _serialize_ot(source_ot)["unidad_inventario"] == "KG"


def test_kg_ot_close_records_measured_summary_without_unit_credit(app):
    with app.app_context():
        creator, _work, manga, weighed = _weigh_kg_fixture(quantity=100)
        work = db.session.get(ScmTrabajoOt, manga.trabajo_ot_id)
        completed = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=work.id,
            operation_id=uuid4(),
            data={"version": work.version},
            action="completar",
        )
        assert completed["trabajo_color"]["estado"] == "COMPLETADO"
        ot = work.orden_trabajo
        result = close_productive_document_kg(
            db.session,
            actor_id=creator.id,
            documento_tipo="OT",
            documento_id=ot.public_id,
            operation_id=uuid4(),
            data={"version": ot.version},
        )
        assert result["kg_medido"] == "12.000"
        assert result["inventario_creado"] is False
        assert result["un_confirmadas"] is False
        assert ScmCierreProductivoKg.query.count() == 1
        db.session.refresh(ot)
        assert ot.estado == "CERRADA"
        assert ScmSaldoInventario.query.count() == 0
        assert ScmMovimientoInventario.query.count() == 0


def test_kg_continuity_closes_each_ot_by_owned_segment_delta(app):
    """A 5 kg boundary and 9 kg final weigh close as 5 + 4 across two OTs."""
    with app.app_context():
        creator, _approver, order, run, _output = _seed_fabrication_order()
        plan = recalculate_fabrication_manga_plan(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={},
        )["plan"]
        source = create_fabrication_ot(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(run.id),
                "fecha_operativa": "2026-09-18",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": plan["lineas"][0]["id"],
                    "cantidad_un": 50,
                }],
            },
        )
        source_work = source["trabajo_color"]
        manga_public_id = source_work["mangas"][0]["public_id"]
        manga = ScmManga.query.filter_by(public_id=UUID(manga_public_id)).one()
        manga.lote_articulo.articulo.unidad_inventario = "KG"
        db.session.flush()
        source_work = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(source_work["id"]),
            operation_id=uuid4(),
            data={"version": source_work["version"]},
            action="iniciar",
        )["trabajo_color"]
        station, label = _print_color_manga(
            actor=creator,
            manga_id=manga_public_id,
            station_code="PESAJE-KG007-CONTINUITY",
        )
        boundary = register_manga_weighing_control(
            db.session,
            station_id=station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "5.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-18T10:00:00-05:00",
                "reading_stable": True,
                "motivo": "CAMBIO_TURNO",
            },
        )
        assert boundary["control"]["peso_neto_kg"] == "5.000"
        source_ot_id = source["ot"]["public_id"]
        target_header = create_fabrication_ot_header(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            data={
                "maquina_id": order.fabricacion.maquina_prevista_id,
                "fecha_operativa": "2026-09-18",
                "turno": "NOCHE",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        candidates = list_pending_manga_continuities(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(target_header["public_id"]),
            corrida_fabricacion_id=str(run.id),
        )["items"]
        assert [item["manga"]["public_id"] for item in candidates] == [manga_public_id]
        target = add_color_work(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(target_header["public_id"]),
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(run.id),
                "maquinista_id": creator.id,
                "asignaciones": [],
                "continuidad_manga_ids": [manga_public_id],
            },
        )
        # The source OT can close immediately at its 5 kg boundary; the
        # physical manga remains open in the target OT.
        source_close = close_productive_document_kg(
            db.session,
            actor_id=creator.id,
            documento_tipo="OT",
            documento_id=source_ot_id,
            operation_id=uuid4(),
            data={},
        )
        assert source_close["kg_medido"] == "5.000"
        assert source_close["cierre"]["kg_fabricacion_estimado"] is None
        assert source_close["cierre"]["kg_previo_estimado"] is None
        target_work = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(target["trabajo_color"]["id"]),
            operation_id=uuid4(),
            data={"version": target["trabajo_color"]["version"]},
            action="iniciar",
        )["trabajo_color"]
        final = confirm_manga_weighing(
            db.session,
            station_id=station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "9.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-18T18:00:00-05:00",
                "reading_stable": True,
            },
        )
        assert final["weighing"]["peso_fisico_neto_kg"] == "9.000"
        segments = ScmTramoMangaTrabajo.query.order_by(ScmTramoMangaTrabajo.secuencia).all()
        assert [Decimal(item.cantidad_atribuida_kg) for item in segments] == [
            Decimal("5.000"), Decimal("4.000")
        ]
        rows = ScmAtribucionProduccionKg.query.filter_by(tipo="NETO_MEDIDO").all()
        assert len(rows) == 1
        assert rows[0].trabajo_ot_id == UUID(target_work["id"])
        weighing_model = ScmPesajeManga.query.one()
        # Simulate a resolved BOM projection on the final NET evidence.  OT
        # closure must keep it pending/consolidated instead of attaching 10/2
        # to the 4 kg target delta.
        weighing_model.atribucion_kg_estado = "ESTIMADA_BOM"
        weighing_model.kg_fabricacion_estimado = Decimal("10.000")
        weighing_model.kg_previo_estimado = Decimal("2.000")
        db.session.flush()

        target_close = close_productive_document_kg(
            db.session,
            actor_id=creator.id,
            documento_tipo="OT",
            documento_id=target_header["public_id"],
            operation_id=uuid4(),
            data={},
        )
        assert source_close["kg_medido"] == "5.000"
        assert target_close["kg_medido"] == "4.000"
        assert target_close["cierre"]["kg_fabricacion_estimado"] is None
        assert target_close["cierre"]["kg_previo_estimado"] is None
        assert any(
            item["codigo"] == "KG_BOM_ESTIMATE_CONSOLIDATED_AT_OF_OA"
            for item in target_close["cierre"]["pendientes"]
        )


def test_kg_of_close_includes_output_manga_without_unit_credit(app):
    with app.app_context():
        creator, _work, manga, _weighed = _weigh_kg_fixture(quantity=100)
        work = db.session.get(ScmTrabajoOt, manga.trabajo_ot_id)
        transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=work.id,
            operation_id=uuid4(),
            data={"version": work.version},
            action="completar",
        )
        order = work.orden_operacion
        result = close_productive_document_kg(
            db.session,
            actor_id=creator.id,
            documento_tipo="OF",
            documento_id=order.id,
            operation_id=uuid4(),
            data={"version": order.version},
        )
        assert result["kg_medido"] == "12.000"
        assert result["inventario_creado"] is False
        assert result["un_confirmadas"] is False
        db.session.refresh(order)
        assert order.estado == "CERRADA"
        assert ScmSaldoInventario.query.count() == 0
        assert ScmMovimientoInventario.query.count() == 0


def test_kg_document_rejects_mixed_kg_and_un_outputs_without_effects(app):
    with app.app_context():
        creator, work_payload, manga, _weighed = _weigh_kg_fixture(quantity=100)
        work = db.session.get(ScmTrabajoOt, manga.trabajo_ot_id)
        transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=work.id,
            operation_id=uuid4(),
            data={"version": work.version},
            action="completar",
        )
        order = work.orden_operacion
        output = ScmOrdenOperacionSalida.query.filter_by(
            orden_operacion_id=order.id
        ).first()
        un_article = ScmArticulo(
            codigo=f"UN-MIX-{uuid4().hex[:8].upper()}",
            nombre="Salida UN mezclada",
            clase="PIEZA_COLOR",
            unidad_inventario="UN",
        )
        db.session.add(un_article)
        db.session.flush()
        un_output = ScmOrdenOperacionSalida(
            orden_operacion_id=order.id,
            corrida_fabricacion_id=output.corrida_fabricacion_id,
            articulo_scm_id=un_article.id,
            cantidad_objetivo=1,
            peso_unitario_snapshot_g=1,
        )
        db.session.add(un_output)
        db.session.flush()
        un_lot = ScmLoteArticulo(
            codigo=f"LOT-UN-MIX-{uuid4().hex[:8].upper()}",
            articulo_id=un_article.id,
            clase="SALIDA_ORDEN_OPERACION",
            orden_operacion_salida_id=un_output.id,
            cantidad_acreditada=0,
            actor_id=creator.id,
        )
        db.session.add(un_lot)
        db.session.flush()
        source_values = {
            column.name: getattr(manga, column.name)
            for column in ScmManga.__table__.columns
            if column.name not in {
                "id", "public_id", "codigo", "secuencia_ot", "lote_articulo_id",
                "articulo_codigo_snapshot", "articulo_nombre_snapshot",
            }
        }
        mixed = ScmManga(
            **source_values,
            public_id=uuid4(),
            codigo=f"{manga.codigo}-UN",
            secuencia_ot=2,
            lote_articulo_id=un_lot.id,
            articulo_codigo_snapshot=un_article.codigo,
            articulo_nombre_snapshot=un_article.nombre,
        )
        db.session.add(mixed)
        db.session.flush()
        db.session.expire(work, ["mangas"])
        ot = work.orden_trabajo
        before_state = ot.estado
        before_version = ot.version
        before_closures = ScmCierreProductivoKg.query.count()
        with pytest.raises(ScmServiceError) as error:
            close_productive_document_kg(
                db.session,
                actor_id=creator.id,
                documento_tipo="OT",
                documento_id=ot.public_id,
                operation_id=uuid4(),
                data={},
            )
        assert error.value.code == "KG_DOCUMENT_MIXED_UNITS"
        db.session.refresh(ot)
        assert ot.estado == before_state
        assert ot.version == before_version
        assert ScmCierreProductivoKg.query.count() == before_closures
