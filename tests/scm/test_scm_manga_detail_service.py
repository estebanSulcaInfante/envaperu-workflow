from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.extensions import db
from app.models.scm_ot import ScmManga
from app.services.scm_manga_detail_service import (
    _parse_group,
    get_manga_detail,
    list_manga_history,
)
from app.services.scm_service_support import ScmServiceError


def test_group_path_requires_exact_json_dimensions():
    assert _parse_group('[{"dimension":"OF","value":"OF-1"}]') == [
        {"dimension": "OF", "value": "OF-1"},
    ]
    with pytest.raises(ScmServiceError):
        _parse_group('[{"dimension":"OF"}]')
    with pytest.raises(ScmServiceError):
        _parse_group('[{"dimension":"NO_EXISTE","value":null}]')


def test_history_manga_list_deduplicates_split_segments_and_drops_unknown(monkeypatch):
    import app.services.scm_manga_detail_service as detail

    manga = SimpleNamespace(id=7, public_id="manga-7", codigo="M-7", estado="PESADA")
    actor = SimpleNamespace(tiene_capacidad=lambda capability: capability in {"OT_VER", "MANGA_PESAJE_VER"})
    rows = [
        {"_manga_id": 7, "_known": True, "PESO_KG": 4, "ARTICULO": "A", "ARTICULO_NOMBRE": "Artículo", "COLOR": "Rojo", "OF": "OF-1", "DIA": "2026-09-01"},
        {"_manga_id": 7, "_known": True, "PESO_KG": 5, "ARTICULO": "A", "ARTICULO_NOMBRE": "Artículo", "COLOR": "Rojo", "OF": "OF-1", "DIA": "2026-09-02"},
        {"_manga_id": 8, "_known": False, "PESO_KG": None, "ARTICULO": "B", "ARTICULO_NOMBRE": "Oculto", "COLOR": "Azul", "OF": "OF-1", "DIA": "2026-09-01"},
    ]
    monkeypatch.setattr(detail, "load_actor", lambda *_args, **_kwargs: actor)
    monkeypatch.setattr(detail, "history_rows_for_manga_detail", lambda *_args, **_kwargs: ({"groups": ["OF", "DIA"]}, rows, [{"mangas": {7: manga}}]))
    payload = detail.list_manga_history(object(), actor_id=1, filters={"desde": "2026-09-01", "hasta": "2026-09-02", "agrupaciones": "OF,DIA"}, group='[{"dimension":"OF","value":"OF-1"}]')
    assert payload["total"] == 1
    assert payload["items"][0]["aporte_consulta_kg"] == 9.0
    assert payload["items"][0]["tramos_consulta"] == 2


def test_detail_returns_read_only_sections_and_hides_weights_without_capability(app):
    from tests.scm.test_scm_production_observability import _seed_observability_graph
    with app.app_context():
        seeded = _seed_observability_graph()
        manga = ScmManga.query.filter_by(codigo="M-OT-OBS-FAB-2-02").first()
        if manga is None:
            manga = ScmManga.query.order_by(ScmManga.id).first()
        payload = get_manga_detail(db.session, actor_id=seeded["base"].id, public_id=manga.public_id)
        assert payload["secciones"]["identidad"]["estado"] == "disponible"
        assert payload["secciones"]["pesajes_correcciones_reaperturas"]["estado"] == "restringido"
        assert payload["secciones"]["tramos"]["estado"] in {"sin_datos", "disponible"}
        assert payload["visibilidad"]["pesaje"] is False
        correction = payload["secciones"]["pesajes_correcciones_reaperturas"]
        assert correction["estado"] == "restringido"

        with pytest.raises(ScmServiceError) as denied:
            list_manga_history(db.session, actor_id=seeded["base"].id, filters={"desde": "2026-08-01", "hasta": "2026-08-31"})
        assert denied.value.code == "MANGA_PESAJE_VER_REQUIRED"
        full_manga = ScmManga.query.filter_by(estado="PENDIENTE_RECEPCION_ALMACEN").first()
        full_payload = get_manga_detail(db.session, actor_id=seeded["full"].id, public_id=full_manga.public_id)
        full_pesajes = full_payload["secciones"]["pesajes_correcciones_reaperturas"]
        assert full_pesajes["estado"] == "disponible"
        correction = full_pesajes["pesajes"][0]["correcciones"][0]
        assert full_pesajes["vigente"]["peso_fisico_neto_kg"] == 11.5
        assert full_pesajes["vigente"]["corregida"] is True
        assert full_pesajes["pesajes"][0]["peso_fisico_neto_kg"] == 12.0
        assert set(correction["resultado"]) <= {
            "peso_bruto_kg", "tara_kg", "peso_fisico_neto_kg", "cantidad_confirmada",
            "kg_produccion_ot", "pesada_at", "fecha_local_pesaje", "dias_desfase_operativo", "alerta_fecha",
        }


def test_detail_documents_project_effective_work_from_real_relations(app):
    from app.services.scm_manga_assignment_projection import effective_assignment, effective_work
    from tests.scm.test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        manga = ScmManga.query.filter_by(estado="PENDIENTE_RECEPCION_ALMACEN").first()
        work = effective_work(manga)
        assert work is not None
        assert work.orden_operacion is not None
        payload = get_manga_detail(db.session, actor_id=seeded["full"].id, public_id=manga.public_id)
        effective = payload["secciones"]["documentos"]["item"]["efectivos"]
        assert effective["of"] == work.orden_operacion.codigo
        assert effective["ot"] == work.orden_trabajo.codigo_ot
        assert effective["trabajo"] == work.codigo
        assert effective["objetivo_color"] == work.trabajo_color.color_nombre_snapshot
        expected_machine = (
            getattr(work.orden_trabajo, "maquina_nombre_snapshot", None)
            or getattr(getattr(work.orden_trabajo, "maquina", None), "codigo", None)
            or getattr(work.orden_trabajo, "maquina_codigo_snapshot", None)
        )
        assert expected_machine
        assert effective["maquina"] == expected_machine
        assignment = effective_assignment(manga)
        expected_responsable = assignment.trabajador.nombre_completo if assignment else None
        assert effective["responsable"] == expected_responsable


def test_detail_color_uses_linked_catalog_hex_without_inventing_missing_reference(app):
    from tests.scm.test_scm_kg_production import _seed_aggregate_color_work
    from tests.scm.test_scm_kg_custody import _grant_capabilities

    with app.app_context():
        creator, _, _, run, _, _, _, created = _seed_aggregate_color_work(quantity=120)
        _grant_capabilities(creator, ("OT_VER",))
        manga_id = UUID(created["mangas"][0]["public_id"])
        catalog_color = run.color_produccion
        for reference in ("#1565C0", None):
            catalog_color.hex_referencia = reference
            payload = get_manga_detail(db.session, actor_id=creator.id, public_id=manga_id)
            identity = payload["secciones"]["identidad"]["item"]["color_identidad"]
            assert identity["id"] == catalog_color.id
            assert identity["hex"] == reference


def test_detail_route_returns_not_found_without_mutating_database(app, client):
    with app.app_context():
        from app.services.scm_configuration import ensure_initial_scm_configuration
        ensure_initial_scm_configuration()
        from app.models.trabajador import Trabajador
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        response = client.get(
            "/api/scm/v1/observabilidad/mangas/00000000-0000-0000-0000-000000000000",
            headers={"X-Actor-Id": str(actor.id)},
        )
        assert response.status_code == 404
        assert response.get_json()["error"]["code"] == "MANGA_NOT_FOUND"


def test_detail_kg_labels_require_inventory_capability(app):
    """A real KG receipt must not leak its unit label through pesaje alone."""
    from tests.scm.test_scm_kg_custody import _grant_capabilities, _received
    from app.models.trabajador import Trabajador

    with app.app_context():
        ctx = _received(app)
        actor = Trabajador(codigo=f"TRB-VIEW-{uuid4().hex[:8]}", nombres="Viewer", apellidos="Pesaje", activo=True)
        db.session.add(actor)
        db.session.flush()
        _grant_capabilities(actor, ("OT_VER", "MANGA_PESAJE_VER"))
        db.session.commit()
        payload = get_manga_detail(db.session, actor_id=actor.id, public_id=ctx["manga"].public_id)
        labels = payload["secciones"]["etiquetas"]
        assert all(item["tipo"] != "KG" for item in labels.get("items", []))


def test_detail_kg_labels_require_unit_location_scope(app):
    """A scoped inventory actor cannot see a KG label outside assigned warehouses."""
    from tests.scm.test_scm_kg_custody import _grant_capabilities, _received
    from app.models.trabajador import Trabajador
    from app.models.scm_inventory_operations import ScmAlmacen

    with app.app_context():
        ctx = _received(app)
        actor = Trabajador(codigo=f"TRB-SCOPE-{uuid4().hex[:8]}", nombres="Viewer", apellidos="Scope", activo=True)
        db.session.add(actor)
        db.session.add(ScmAlmacen(codigo=f"ALM-SCOPE-{uuid4().hex[:8]}", nombre="Scope only", tipo="PIEZAS_WIP"))
        db.session.flush()
        _grant_capabilities(actor, ("OT_VER", "MANGA_PESAJE_VER", "INVENTARIO_VER"))
        # The receipt fixture intentionally has no warehouse membership for this
        # actor. With configured warehouses the scope service fails closed.
        db.session.commit()
        payload = get_manga_detail(db.session, actor_id=actor.id, public_id=ctx["manga"].public_id)
        labels = payload["secciones"]["etiquetas"]
        assert all(item["tipo"] != "KG" for item in labels.get("items", []))


def test_detail_stock_filters_each_historical_movement_by_its_location_scope(app):
    """A manga moved A->B exposes B's ledger events to a B-only actor."""
    from decimal import Decimal
    from uuid import uuid4

    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import ScmMovimientoInventario, ScmSaldoInventario, ScmUbicacionInventario
    from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador, ScmSesionOperacionAlmacen, ScmTransferenciaInventario, ScmTransferenciaItem
    from app.models.trabajador import Trabajador
    from app.models.scm_warehouse import ScmExistenciaManga
    from tests.scm.test_scm_kg_custody import _grant_capabilities
    from tests.scm.test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        _seed_observability_graph()
        manga = ScmManga.query.filter_by(estado="RECIBIDA").first()
        existence = ScmExistenciaManga.query.filter_by(manga_id=manga.id).one()
        article = ScmArticulo(codigo=f"PC-HISTORY-{uuid4().hex[:8].upper()}", nombre="Pieza historia", clase="PIEZA_COLOR")
        warehouse_a = ScmAlmacen(codigo=f"ALM-H-A-{uuid4().hex[:6]}", nombre="Origen A", tipo="PIEZAS_WIP")
        warehouse_b = ScmAlmacen(codigo=f"ALM-H-B-{uuid4().hex[:6]}", nombre="Destino B", tipo="PIEZAS_WIP")
        db.session.add_all([article, warehouse_a, warehouse_b])
        db.session.flush()
        loc_a = ScmUbicacionInventario(codigo=f"H-A-{uuid4().hex[:8]}", nombre="A", tipo="POSICION", almacen_id=warehouse_a.id, clases_articulo_json=["PIEZA_COLOR"])
        loc_b = ScmUbicacionInventario(codigo=f"H-B-{uuid4().hex[:8]}", nombre="B", tipo="POSICION", almacen_id=warehouse_b.id, clases_articulo_json=["PIEZA_COLOR"])
        actor = Trabajador(codigo=f"TRB-H-{uuid4().hex[:8]}", nombres="Scope", apellidos="B", activo=True)
        db.session.add_all([loc_a, loc_b, actor])
        db.session.flush()
        _grant_capabilities(actor, ("OT_VER", "INVENTARIO_VER"))
        db.session.add(ScmAlmacenTrabajador(almacen_id=warehouse_b.id, trabajador_id=actor.id, clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=actor.id))
        operation_session = ScmSesionOperacionAlmacen(tipo="TRANSFERENCIA", modalidad="PICKUP", origen_ubicacion_id=loc_a.id, destino_ubicacion_id=loc_b.id, estado="CONFIRMADA", actor_id=actor.id)
        db.session.add(operation_session)
        db.session.flush()
        transfer = ScmTransferenciaInventario(codigo=f"TRF-H-{uuid4().hex[:8]}", sesion_id=operation_session.id, origen_ubicacion_id=loc_a.id, destino_ubicacion_id=loc_b.id, modalidad="PICKUP", estado="CERRADA", actor_id=actor.id, operation_id=uuid4())
        db.session.add(transfer)
        db.session.flush()
        origin = ScmSaldoInventario(articulo_scm_id=article.id, ubicacion_id=loc_a.id, cantidad_fisica=Decimal("0"))
        destination = ScmSaldoInventario(articulo_scm_id=article.id, ubicacion_id=loc_b.id, cantidad_fisica=Decimal("10"))
        db.session.add_all([origin, destination])
        db.session.flush()
        old = ScmMovimientoInventario(saldo_id=origin.id, tipo="TRASLADO_SALIDA", cantidad_delta=Decimal("-10"), saldo_fisico_resultante=Decimal("0"), motivo="Salida historica A", referencia_tipo="TRANSFERENCIA_INVENTARIO", referencia_id=str(transfer.id), actor_id=actor.id, operation_id=uuid4())
        current = ScmMovimientoInventario(saldo_id=destination.id, tipo="TRASLADO_ENTRADA", cantidad_delta=Decimal("10"), saldo_fisico_resultante=Decimal("10"), motivo="Entrada vigente B", referencia_tipo="TRANSFERENCIA_INVENTARIO", referencia_id=str(transfer.id), actor_id=actor.id, operation_id=uuid4())
        db.session.add_all([old, current])
        db.session.flush()
        existence.articulo_scm_id = article.id
        existence.saldo_id = destination.id
        existence.ubicacion_id = loc_b.id
        existence.movimiento_ingreso_id = current.id
        db.session.add(ScmTransferenciaItem(transferencia_id=transfer.id, existencia_manga_id=existence.id, cantidad=Decimal("10"), movimiento_salida_id=old.id, movimiento_entrada_id=current.id))
        db.session.commit()
        payload = get_manga_detail(db.session, actor_id=actor.id, public_id=manga.public_id)
        movements = payload["secciones"]["stock_movimientos"]["movimientos"]
        current_items = [item for item in movements if item["motivo"] == "Entrada vigente B"]
        assert current_items
        assert current_items[0]["referencia_tipo"] == "TRANSFERENCIA_INVENTARIO"
        assert current_items[0]["actor"]["id"] == actor.id
        assert "timestamp" in current_items[0]
        assert all(item["motivo"] != "Salida historica A" for item in movements)


def test_detail_after_real_weighing_annulment_keeps_negative_typed_movement(app):
    from uuid import UUID
    from tests.scm.test_scm_kg_custody import _grant_capabilities
    from tests.scm.test_scm_kg_production import _auto_final_kg_fixture
    from app.services.scm_weighing_service import annul_manga_weighing

    with app.app_context():
        creator, _approver, manga, _station, _prelabel, weighed = _auto_final_kg_fixture(
            app, station_code=f"DETAIL-ANNUL-{uuid4().hex[:8].upper()}"
        )
        _grant_capabilities(creator, ("OT_VER", "MANGA_PESAJE_VER", "INVENTARIO_VER", "ANULAR_PESAJE"))
        weighing_id = UUID(weighed["weighing"]["public_id"])
        annul_manga_weighing(
            db.session, actor_id=creator.id, weighing_id=weighing_id,
            operation_id=uuid4(), data={"motivo": "Corrección física", "evidencia": "Acta QA"},
        )
        payload = get_manga_detail(db.session, actor_id=creator.id, public_id=manga.public_id)
        history = payload["secciones"]["pesajes_correcciones_reaperturas"]
        assert history["vigente"] is None
        movements = payload["secciones"]["stock_movimientos"]["movimientos"]
        annulments = [item for item in movements if item["referencia_tipo"] == "ANULACION_PESAJE_MANGA"]
        assert annulments
        assert annulments[-1]["cantidad_delta"] < 0


def test_detail_vigente_timestamp_matches_original_and_normalizes_correction_offset(app):
    from tests.scm.test_scm_kg_custody import _grant_capabilities
    from tests.scm.test_scm_kg_production import _auto_final_kg_fixture
    from app.services.scm_weighing_service import (
        approve_weighing_correction,
        request_weighing_correction,
    )

    with app.app_context():
        creator, approver, manga, _station, _prelabel, weighed = _auto_final_kg_fixture(
            app, station_code=f"DETAIL-TIME-{uuid4().hex[:8].upper()}"
        )
        _grant_capabilities(creator, ("OT_VER", "MANGA_PESAJE_VER", "INVENTARIO_VER", "PESAJE_CORRECCION_SOLICITAR"))
        _grant_capabilities(approver, ("PESAJE_CORRECCION_APROBAR",))
        db.session.commit()

        before = get_manga_detail(db.session, actor_id=creator.id, public_id=manga.public_id)
        before_history = before["secciones"]["pesajes_correcciones_reaperturas"]
        assert before_history["pesajes"][0]["pesada_at"] == before_history["vigente"]["pesada_at"]
        assert before_history["vigente"]["pesada_at"].endswith("+00:00")

        correction = request_weighing_correction(
            db.session, actor_id=creator.id,
            weighing_id=UUID(weighed["weighing"]["public_id"]), operation_id=uuid4(),
            data={
                "proposed": {
                    "peso_bruto_kg": "11.600", "tara_kg": "0.100",
                    "pesada_at": "2026-09-19T13:45:00-05:00",
                },
                "motivo": "Corrección con offset explícito",
            },
        )["correction"]
        approve_weighing_correction(
            db.session, actor_id=approver.id,
            correction_id=UUID(correction["id"]), operation_id=uuid4(),
            data={"motivo_aprobacion": "Offset verificado"},
        )
        after = get_manga_detail(db.session, actor_id=creator.id, public_id=manga.public_id)
        vigente = after["secciones"]["pesajes_correcciones_reaperturas"]["vigente"]
        correction_result = after["secciones"]["pesajes_correcciones_reaperturas"]["pesajes"][0]["correcciones"][0]["resultado"]
        assert vigente["pesada_at"] == "2026-09-19T18:45:00+00:00"
        assert correction_result["pesada_at"] == "2026-09-19T18:45:00+00:00"


def test_detail_control_closure_is_event_without_fabricated_vigente(app):
    from tests.scm.test_scm_kg_custody import _grant_capabilities
    from tests.scm.test_scm_kg_production import _print_color_manga
    from tests.scm.test_scm_kg_production import _seed_aggregate_color_work
    from app.models.scm_inventory import ScmUbicacionInventario
    from app.services.scm_kg_production_service import close_kg_from_last_control
    from app.services.scm_weighing_service import register_manga_weighing_control
    from app.services.scm_ot_service import transition_color_work

    with app.app_context():
        station_code = f"DETAIL-CONTROL-{uuid4().hex[:8].upper()}"
        app.config["KG_AUTOMATIC_INTAKE_ENABLED"] = True
        app.config["KG_PRODUCTION_LOCATION_CODE"] = f"{station_code}-LOC"
        creator, _approver, _order, _run, _output, _line, _header, created = _seed_aggregate_color_work(quantity=120)
        manga = ScmManga.query.filter_by(public_id=UUID(created["mangas"][0]["public_id"])).one()
        manga.lote_articulo.articulo.unidad_inventario = "KG"
        db.session.add(ScmUbicacionInventario(
            codigo=f"{station_code}-LOC", nombre="Control detail production",
            clases_articulo_json=["PIEZA_COLOR"], activo=True,
            tipo="PUNTO_PRODUCCION", permite_saldo_libre=True,
        ))
        db.session.flush()
        transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(), data={"version": created["trabajo_color"]["version"]}, action="iniciar",
        )
        station, label = _print_color_manga(actor=creator, manga_id=manga.public_id, station_code=station_code)
        control = register_manga_weighing_control(
            db.session, station_id=station.station_id, operation_id=uuid4(), actor_id=creator.id,
            data={
                "label_id": label["public_id"], "capture_id": str(uuid4()),
                "peso_bruto_kg": "12.100", "tara_kg": "0.100", "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-18T16:55:00-05:00", "reading_stable": True,
                "control_type": "AVANCE_KG",
            },
        )
        _grant_capabilities(creator, ("MANGA_FINALIZAR_PARCIAL", "OT_VER", "MANGA_PESAJE_VER", "INVENTARIO_VER"))
        close_kg_from_last_control(
            db.session, actor_id=creator.id, manga_id=manga.public_id,
            operation_id=uuid4(), data={"motivo": "Cierre desde control", "version": manga.version},
        )
        payload = get_manga_detail(db.session, actor_id=creator.id, public_id=manga.public_id)
        history = payload["secciones"]["pesajes_correcciones_reaperturas"]
        assert history["vigente"] is None
        control_projection = next(
            item for item in history["controles_acumulados"]
            if item["id"] == control["control"]["id"]
        )
        assert control_projection["pesado_at"] == "2026-09-18T21:55:00+00:00"
        assert history["cierres_control"][0]["tipo"] == "CIERRE_DESDE_CONTROL"
        assert history["cierres_control"][0]["simula_pesaje"] is False
        assert history["cierres_control"][0]["control_id"] == control["control"]["id"]
