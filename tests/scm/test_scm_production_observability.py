from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import event

from app.extensions import db
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_catalogos import ScmCapacidad
from app.models.scm_ot import (
    ScmAnulacionPesajeManga,
    ScmCorreccionPesajeManga,
    ScmManga,
    ScmPesajeManga,
    ScmTrabajoColor,
    ScmTrabajoOt,
)
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    ScmOrdenProduccion,
    ScmPlanProduccion,
)
from app.models.scm_reproceso import (
    ScmAlertaOperativa,
    ScmReglaAlertaRevision,
)
from app.models.scm_rutas import ScmCentroTrabajo
from app.models.scm_warehouse import ScmExistenciaManga
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_production_observability_service import (
    get_production_ot_observability,
    list_production_ot_observability,
)


HASH = "a" * 64
NOW = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)


def _actor(code, capability_codes):
    role = RolOperativo(
        codigo=f"R-{code}"[:20],
        nombre=f"Rol {code}",
        capacidades=list(
            ScmCapacidad.query.filter(
                ScmCapacidad.codigo.in_(capability_codes)
            ).all()
        ),
    )
    actor = Trabajador(
        codigo=code,
        nombres=code,
        apellidos="Observabilidad",
        activo=True,
        roles=[role],
    )
    db.session.add_all([role, actor])
    db.session.flush()
    return actor


def _manga(*, ot, work, sequence, state, worker, confirmed=None):
    return ScmManga(
        codigo=f"M-{ot.codigo_ot}-{sequence:02d}",
        ot_id=ot.id,
        trabajo_ot_id=work.id if work else None,
        plan_linea_id=1000 + sequence,
        lote_articulo_id=2000 + sequence,
        secuencia_ot=sequence,
        estado=state,
        cantidad_planificada_un=100,
        cantidad_asignada_un=100,
        cantidad_confirmada_un=confirmed,
        maquinista_previsto_id=worker.id,
        articulo_codigo_snapshot="PC-OBS",
        articulo_nombre_snapshot="Pieza observada",
        color_snapshot=(
            work.trabajo_color.color_nombre_snapshot if work else None
        ),
        regla_revision_id_snapshot=1,
        regla_hash_snapshot=HASH,
        tipo_contenedor_codigo_snapshot="MANGA-100",
        tipo_contenedor_nombre_snapshot="Manga 100",
        peso_unitario_snapshot_g=100,
        tara_nominal_g_snapshot=100,
        tolerancia_tara_g_snapshot=10,
        peso_bruto_max_kg_snapshot=20,
        created_by_id=worker.id,
        created_at=NOW,
    )


def _weighing(*, manga, worker, net, standard, weighed_at):
    weighing = ScmPesajeManga(
        manga_id=manga.id,
        operation_id=uuid4(),
        source_system="TEST",
        station_id="ST-OBS",
        capture_id=uuid4(),
        peso_bruto_kg=Decimal(net) + Decimal("0.100"),
        tara_kg=Decimal("0.100"),
        peso_fisico_neto_kg=Decimal(net),
        tara_fuente="TIPO_MANGA",
        cantidad_confirmada=Decimal("100"),
        kg_produccion_ot=Decimal(standard),
        pesada_at=weighed_at,
        timezone_snapshot="America/Lima",
        fecha_local_pesaje=weighed_at.date(),
        dias_desfase_operativo=0,
        pesado_por_id=worker.id,
        snapshots_json={},
        created_at=weighed_at,
    )
    db.session.add(weighing)
    db.session.flush()
    return weighing


def _seed_observability_graph():
    from app.services.scm_configuration import ensure_initial_scm_configuration

    ensure_initial_scm_configuration()
    full = _actor(
        "OBS-FULL",
        {
            "OT_VER",
            "MANGA_PESAJE_VER",
            "ALERTA_VER",
            "RECEPCION_MANGA_VER",
            "CALIDAD_MANGA_VER",
        },
    )
    base = _actor("OBS-BASE", {"OT_VER"})
    denied = _actor("OBS-NO", {"OP_VER"})
    machine_worker = Trabajador.query.filter_by(codigo="TRB-01").one()

    op = ScmOrdenProduccion(
        codigo="OP-OBS-001",
        origen="PILOTO",
        fecha_necesidad=date(2026, 8, 10),
        estado="EN_COBERTURA",
        created_by_id=full.id,
    )
    plan = ScmPlanProduccion(
        orden_produccion=op,
        revision=2,
        estado="CONFIRMADO",
        input_hash=HASH,
        content_hash="b" * 64,
        propuesta_json={},
        calculado_por_id=full.id,
        confirmado_por_id=full.id,
        operation_id=uuid4(),
        confirmado_at=NOW,
    )
    of = ScmOrdenOperacion(
        codigo="OF-OBS-001",
        tipo="FABRICACION",
        origen_demanda="PLANIFICADA",
        plan_produccion=plan,
        propuesta_clave="OF-1",
        estado="EN_EJECUCION",
        created_by_id=full.id,
    )
    oa = ScmOrdenOperacion(
        codigo="OA-OBS-001",
        tipo="ENSAMBLE",
        origen_demanda="PLANIFICADA",
        plan_produccion=plan,
        propuesta_clave="OA-1",
        estado="PROGRAMADA",
        created_by_id=full.id,
    )
    center = ScmCentroTrabajo(
        codigo="ARMADO-OBS",
        nombre="Mesa de armado observada",
        tipo="ENSAMBLE",
    )
    db.session.add_all([op, plan, of, oa, center])
    db.session.flush()

    fab = RegistroDiarioProduccion(
        codigo_ot="OT-OBS-FAB",
        codigo_ot_sintetico=False,
        estado="EN_EJECUCION",
        tipo_ot="FABRICACION",
        fecha=date(2026, 8, 9),
        turno="DIA",
        maquina_id=1,
        responsable_id=machine_worker.id,
        maquinista_previsto_id=machine_worker.id,
        created_by_id=full.id,
        created_at=NOW,
        updated_at=NOW,
    )
    assembly = RegistroDiarioProduccion(
        codigo_ot="OT-OBS-ARM",
        codigo_ot_sintetico=False,
        estado="PLANIFICADA",
        tipo_ot="ENSAMBLE",
        modo_ejecucion_ensamble="MESA",
        fecha=date(2026, 8, 10),
        turno="DIA",
        centro_trabajo_id=center.id,
        responsable_id=machine_worker.id,
        maquinista_previsto_id=machine_worker.id,
        orden_operacion_id=oa.id,
        cantidad_objetivo=200,
        cantidad_confirmada=50,
        created_by_id=full.id,
        created_at=NOW,
        updated_at=NOW,
    )
    db.session.add_all([fab, assembly])
    db.session.flush()

    red = ScmTrabajoOt(
        orden_trabajo_id=fab.id,
        codigo="TC-OBS-ROJO",
        secuencia=1,
        estado="COMPLETADO",
        orden_operacion_id=of.id,
        cantidad_objetivo_un=200,
        cantidad_confirmada_un=190,
        completada_at=NOW,
        created_by_id=full.id,
    )
    blue = ScmTrabajoOt(
        orden_trabajo_id=fab.id,
        codigo="TC-OBS-AZUL",
        secuencia=2,
        estado="EN_EJECUCION",
        orden_operacion_id=of.id,
        cantidad_objetivo_un=300,
        cantidad_confirmada_un=180,
        iniciada_at=NOW,
        created_by_id=full.id,
    )
    db.session.add_all([red, blue])
    db.session.flush()
    db.session.add_all([
        ScmTrabajoColor(
            trabajo_ot_id=red.id,
            corrida_fabricacion_id=uuid4(),
            color_nombre_snapshot="ROJO",
        ),
        ScmTrabajoColor(
            trabajo_ot_id=blue.id,
            corrida_fabricacion_id=uuid4(),
            color_nombre_snapshot="AZUL",
        ),
    ])
    db.session.flush()

    pending = _manga(
        ot=fab,
        work=red,
        sequence=1,
        state="PREETIQUETADA",
        worker=machine_worker,
    )
    corrected = _manga(
        ot=fab,
        work=blue,
        sequence=2,
        state="PENDIENTE_RECEPCION_ALMACEN",
        worker=machine_worker,
        confirmed=95,
    )
    received = _manga(
        ot=fab,
        work=blue,
        sequence=3,
        state="RECIBIDA",
        worker=machine_worker,
        confirmed=80,
    )
    annulled = _manga(
        ot=fab,
        work=blue,
        sequence=4,
        state="ANULADA",
        worker=machine_worker,
        confirmed=100,
    )
    assembly_pending = _manga(
        ot=assembly,
        work=None,
        sequence=1,
        state="CERRADA_ARMADO_PENDIENTE_PESAJE",
        worker=machine_worker,
        confirmed=50,
    )
    db.session.add_all([
        pending,
        corrected,
        received,
        annulled,
        assembly_pending,
    ])
    db.session.flush()

    corrected_weighing = _weighing(
        manga=corrected,
        worker=machine_worker,
        net="12.000",
        standard="11.000",
        weighed_at=NOW,
    )
    received_weighing = _weighing(
        manga=received,
        worker=machine_worker,
        net="8.000",
        standard="8.000",
        weighed_at=NOW,
    )
    annulled_weighing = _weighing(
        manga=annulled,
        worker=machine_worker,
        net="10.000",
        standard="10.000",
        weighed_at=NOW,
    )
    correction = ScmCorreccionPesajeManga(
        pesaje_id=corrected_weighing.id,
        estado="APLICADA",
        proposed_json={"peso_fisico_neto_kg": "11.500"},
        reason="Ajuste de balanza",
        requested_by_id=full.id,
        requested_at=NOW,
        request_operation_id=uuid4(),
        resolved_by_id=full.id,
        resolved_at=NOW,
        approval_operation_id=uuid4(),
        resolution_reason="Evidencia validada",
        result_projection_json={
            "peso_bruto_kg": "11.600",
            "tara_kg": "0.100",
            "peso_fisico_neto_kg": "11.500",
            "cantidad_confirmada": "95.000",
            "kg_produccion_ot": "10.800",
            "pesada_at": NOW.isoformat(),
            "fecha_local_pesaje": "2026-08-10",
            "dias_desfase_operativo": 0,
            "alerta_fecha": False,
        },
    )
    annulment = ScmAnulacionPesajeManga(
        pesaje_id=annulled_weighing.id,
        motivo="Defecto confirmado",
        anulada_por_id=full.id,
        anulada_at=NOW,
        operation_id=uuid4(),
        cantidad_devuelta_plan_un=100,
    )
    existence = ScmExistenciaManga(
        manga_id=received.id,
        etiqueta_resuelta_id=100,
        articulo_scm_id=100,
        saldo_id=uuid4(),
        ubicacion_id=100,
        movimiento_ingreso_id=uuid4(),
        operation_id=uuid4(),
        resuelta_por="QR_FINAL",
        estado_logistico="RECIBIDA_ALMACEN",
        estado_calidad="LIBERADA",
        cantidad_fisica=80,
        cantidad_reservada=0,
        peso_neto_snapshot_kg=8,
        recibida_por_id=full.id,
        recibida_at=NOW,
        calidad_por_id=full.id,
        calidad_at=NOW,
    )
    rule_revision = ScmReglaAlertaRevision.query.first()
    alert = ScmAlertaOperativa(
        regla_revision_id=rule_revision.id,
        huella="c" * 64,
        tipo="PESAJE_FECHA_OPERATIVA_DIFERENTE",
        agregado_tipo="PESAJE_MANGA",
        agregado_id=str(corrected_weighing.public_id),
        estado="ABIERTA",
        severidad="CRITICA",
        resumen="Pesaje fuera de fecha",
        detalle={"manga": corrected.codigo},
        detectada_at=NOW,
        updated_at=NOW,
    )
    db.session.add_all([correction, annulment, existence, alert])
    db.session.commit()
    return {
        "full": full,
        "base": base,
        "denied": denied,
        "fab": fab,
        "assembly": assembly,
    }


def _get(client, path, actor):
    return client.get(path, headers={"X-Actor-Id": str(actor.id)})


def test_list_observability_unifies_fabrication_multicolor_and_assembly(
    app, client, scm_config
):
    with app.app_context():
        seeded = _seed_observability_graph()
        actor = seeded["full"]

        response = _get(
            client,
            "/api/scm/v1/observabilidad/ots?fecha_desde=2026-08-09&fecha_hasta=2026-08-10",
            actor,
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["page"] == {
            "next_cursor": None,
            "limit": 25,
            "has_more": False,
        }
        assert payload["as_of"].endswith("+00:00")
        assert [item["ot"]["tipo"] for item in payload["items"]] == [
            "ARMADO",
            "FABRICACION",
        ]
        fabrication = payload["items"][1]
        assert fabrication["trabajo_actual"]["color"] == "AZUL"
        assert fabrication["trabajos_resumen"] == {
            "total": 2,
            "por_estado": {"COMPLETADO": 1, "EN_EJECUCION": 1},
        }
        assert fabrication["mangas_resumen"]["pendientes_pesaje"] == 1
        assert fabrication["mangas_resumen"]["pendientes_recepcion"] == 1
        assert fabrication["mangas_resumen"]["recibidas"] == 1
        assert fabrication["mangas_resumen"]["anuladas"] == 1
        assert fabrication["pesaje_resumen"] == {
            "cantidad": 2,
            "neto_kg": 19.5,
            "peso_fisico_neto_kg": 19.5,
            "kg_produccion_estandar": 18.8,
            "ultimo_pesaje_at": NOW.isoformat(),
        }
        assert fabrication["alertas_resumen"] == {
            "abiertas": 1,
            "criticas": 1,
        }
        assert fabrication["upstream"]["op"]["codigo"] == "OP-OBS-001"
        assert fabrication["upstream"]["orden"]["codigo"] == "OF-OBS-001"


def test_observability_detail_respects_permissions_and_effective_facts(
    app, client, scm_config
):
    with app.app_context():
        seeded = _seed_observability_graph()
        full = seeded["full"]
        base = seeded["base"]
        denied = seeded["denied"]
        public_id = seeded["fab"].public_id

        base_response = _get(
            client,
            f"/api/scm/v1/observabilidad/ots/{public_id}",
            base,
        )
        assert base_response.status_code == 200
        base_item = base_response.get_json()["item"]
        assert base_item["pesaje_resumen"] is None
        assert base_item["alertas_resumen"] is None
        assert base_item["visibilidad"] == {
            "pesaje": False,
            "alertas": False,
            "almacen": False,
            "calidad": False,
        }
        assert all(
            manga["pesaje"] is None and manga["almacen"] is None
            for work in base_item["trabajos"]
            for manga in work["mangas"]
        )

        full_response = _get(
            client,
            f"/api/scm/v1/observabilidad/ots/{public_id}",
            full,
        )
        assert full_response.status_code == 200
        full_item = full_response.get_json()["item"]
        mangas = {
            manga["codigo"]: manga
            for work in full_item["trabajos"]
            for manga in work["mangas"]
        }
        corrected = mangas["M-OT-OBS-FAB-02"]
        annulled = mangas["M-OT-OBS-FAB-04"]
        assert corrected["pesaje"]["peso_fisico_neto_kg"] == 11.5
        assert corrected["pesaje"]["kg_produccion_estandar"] == 10.8
        assert corrected["pesaje"]["corregido"] is True
        assert annulled["pesaje"]["estado"] == "ANULADO"

        denied_response = _get(
            client,
            "/api/scm/v1/observabilidad/ots",
            denied,
        )
        assert denied_response.status_code == 403
        assert denied_response.get_json()["error"]["details"] == {
            "capability": "OT_VER"
        }

        quality_only = _actor(
            "OBS-QUALITY", {"OT_VER", "CALIDAD_MANGA_VER"}
        )
        db.session.commit()
        quality_response = _get(
            client,
            f"/api/scm/v1/observabilidad/ots/{public_id}",
            quality_only,
        )
        quality_item = quality_response.get_json()["item"]
        quality_mangas = [
            manga
            for work in quality_item["trabajos"]
            for manga in work["mangas"]
        ]
        received_quality = next(
            manga
            for manga in quality_mangas
            if manga["codigo"] == "M-OT-OBS-FAB-03"
        )
        assert quality_item["visibilidad"] == {
            "pesaje": False,
            "alertas": False,
            "almacen": False,
            "calidad": True,
        }
        assert received_quality["almacen"] == {
            "estado_logistico": None,
            "recibida_at": None,
            "cantidad_fisica_un": None,
            "peso_neto_snapshot_kg": None,
            "estado_calidad": "LIBERADA",
            "calidad_at": NOW.isoformat(),
        }


def test_observability_filters_cursor_and_sensitive_alert_filter(
    app, client, scm_config
):
    with app.app_context():
        seeded = _seed_observability_graph()
        full = seeded["full"]
        base = seeded["base"]

        first = _get(
            client,
            "/api/scm/v1/observabilidad/ots?limit=1&tipo_ot=ARMADO",
            full,
        )
        assert first.status_code == 200
        first_payload = first.get_json()
        assert first_payload["page"]["has_more"] is False

        first = _get(
            client,
            "/api/scm/v1/observabilidad/ots?limit=1",
            full,
        )
        first_payload = first.get_json()
        cursor = first_payload["page"]["next_cursor"]
        assert first_payload["page"]["has_more"] is True
        second = _get(
            client,
            f"/api/scm/v1/observabilidad/ots?limit=1&cursor={cursor}",
            full,
        )
        assert second.status_code == 200
        assert second.get_json()["as_of"] == first_payload["as_of"]
        assert second.get_json()["items"][0]["ot"]["codigo"] == "OT-OBS-FAB"

        invalid = _get(
            client,
            "/api/scm/v1/observabilidad/ots?cursor=no-es-cursor",
            full,
        )
        assert invalid.status_code == 400
        assert invalid.get_json()["error"]["code"] == (
            "INVALID_OBSERVABILITY_CURSOR"
        )
        mismatch = _get(
            client,
            f"/api/scm/v1/observabilidad/ots?limit=1&tipo_ot=ARMADO&cursor={cursor}",
            full,
        )
        assert mismatch.status_code == 409
        assert mismatch.get_json()["error"]["code"] == (
            "OBSERVABILITY_CURSOR_FILTER_MISMATCH"
        )

        pending = _get(
            client,
            "/api/scm/v1/observabilidad/ots?pendientes_pesaje=true",
            base,
        )
        assert pending.status_code == 200
        assert len(pending.get_json()["items"]) == 2
        running_alias = _get(
            client,
            "/api/scm/v1/observabilidad/ots?en_ejecucion=true",
            full,
        )
        assert [
            item["ot"]["codigo"]
            for item in running_alias.get_json()["items"]
        ] == ["OT-OBS-FAB"]
        by_responsible_search = _get(
            client,
            "/api/scm/v1/observabilidad/ots?q=Juan%20P.",
            full,
        )
        assert {
            item["ot"]["codigo"]
            for item in by_responsible_search.get_json()["items"]
        } == {"OT-OBS-FAB", "OT-OBS-ARM"}
        combined = _get(
            client,
            "/api/scm/v1/observabilidad/ots"
            "?fecha_desde=2026-08-09&fecha_hasta=2026-08-09"
            "&tipo=FABRICACION&estado=EN_EJECUCION&turno=DIA"
            "&recurso=MQ-01&responsable=TRB-01&op=OP-OBS-001"
            "&orden=OF-OBS-001&color=AZUL",
            full,
        )
        assert combined.status_code == 200
        assert [
            item["ot"]["codigo"] for item in combined.get_json()["items"]
        ] == ["OT-OBS-FAB"]
        alert_forbidden = _get(
            client,
            "/api/scm/v1/observabilidad/ots?alertas=true",
            base,
        )
        assert alert_forbidden.status_code == 403
        assert alert_forbidden.get_json()["error"]["details"] == {
            "capability": "ALERTA_VER"
        }
        warehouse_forbidden = _get(
            client,
            "/api/scm/v1/observabilidad/ots?pendientes_almacen=true",
            base,
        )
        assert warehouse_forbidden.status_code == 403
        assert warehouse_forbidden.get_json()["error"]["details"] == {
            "capability": "RECEPCION_MANGA_VER"
        }
        warehouse_allowed = _get(
            client,
            "/api/scm/v1/observabilidad/ots?almacen=true",
            full,
        )
        assert warehouse_allowed.status_code == 200
        assert [
            item["ot"]["codigo"]
            for item in warehouse_allowed.get_json()["items"]
        ] == ["OT-OBS-FAB"]

        rule_revision = ScmReglaAlertaRevision.query.first()
        db.session.add(ScmAlertaOperativa(
            regla_revision_id=rule_revision.id,
            huella="d" * 64,
            tipo="OT_SIN_ACTIVIDAD",
            agregado_tipo="ORDEN_TRABAJO",
            agregado_id=str(seeded["assembly"].public_id),
            estado="ABIERTA",
            severidad="ADVERTENCIA",
            resumen="OT sin actividad",
            detalle={},
            detectada_at=NOW,
            updated_at=NOW,
        ))
        db.session.commit()
        with_alerts = _get(
            client,
            "/api/scm/v1/observabilidad/ots?alertas=true",
            full,
        )
        assert {
            item["ot"]["codigo"]
            for item in with_alerts.get_json()["items"]
        } == {"OT-OBS-FAB", "OT-OBS-ARM"}


def test_observability_cursor_keeps_creation_snapshot_and_all_annulled_is_not_received(
    app, client, scm_config
):
    with app.app_context():
        seeded = _seed_observability_graph()
        actor = seeded["full"]
        first = _get(
            client,
            "/api/scm/v1/observabilidad/ots?limit=1",
            actor,
        ).get_json()
        cursor = first["page"]["next_cursor"]
        center_id = seeded["assembly"].centro_trabajo_id
        db.session.add(RegistroDiarioProduccion(
            codigo_ot="OT-OBS-LATE",
            codigo_ot_sintetico=False,
            estado="PLANIFICADA",
            tipo_ot="ENSAMBLE",
            modo_ejecucion_ensamble="MESA",
            fecha=date(2026, 8, 9),
            turno="DIA",
            centro_trabajo_id=center_id,
            responsable_id=1,
            created_by_id=actor.id,
            created_at=datetime.now(timezone.utc),
        ))
        assembly_manga = ScmManga.query.filter_by(
            ot_id=seeded["assembly"].id
        ).one()
        assembly_manga.estado = "ANULADA"
        assembly_manga.anulada_at = NOW
        assembly_manga.anulada_por_id = actor.id
        assembly_manga.motivo_anulacion = "Prueba de borde"
        db.session.commit()

        second = _get(
            client,
            f"/api/scm/v1/observabilidad/ots?limit=1&cursor={cursor}",
            actor,
        )
        assert second.status_code == 200
        assert second.get_json()["items"][0]["ot"]["codigo"] == "OT-OBS-FAB"
        assembly_detail = _get(
            client,
            f"/api/scm/v1/observabilidad/ots/{seeded['assembly'].public_id}",
            actor,
        ).get_json()["item"]
        assert assembly_detail["mangas_resumen"]["anuladas"] == 1
        assert assembly_detail["etapa_actual"] != "RECIBIDA"


def test_observability_summary_groups_day_and_month_without_mixing_units_kg(
    app, client, scm_config
):
    with app.app_context():
        actor = _seed_observability_graph()["full"]

        daily = _get(
            client,
            "/api/scm/v1/observabilidad/resumen?granularidad=DIA&fecha_desde=2026-08-09&fecha_hasta=2026-08-10",
            actor,
        )
        assert daily.status_code == 200
        payload = daily.get_json()
        assert payload["granularidad"] == "DIA"
        assert payload["periodo"] == {
            "fecha_desde": "2026-08-09",
            "fecha_hasta": "2026-08-10",
        }
        assert [item["periodo"] for item in payload["series"]] == [
            "2026-08-09",
            "2026-08-10",
        ]
        assert payload["totales"]["ots"] == 2
        assert payload["totales"]["objetivo_un"] == 700.0
        assert payload["totales"]["confirmado_un"] == 420.0
        assert payload["totales"]["peso_fisico_neto_kg"] == 19.5
        assert payload["totales"]["kg_produccion_estandar"] == 18.8

        monthly = _get(
            client,
            "/api/scm/v1/observabilidad/resumen?granularidad=MES&desde=2026-08-01&hasta=2026-08-31",
            actor,
        )
        assert monthly.status_code == 200
        assert [item["periodo"] for item in monthly.get_json()["series"]] == [
            "2026-08"
        ]


def test_observability_query_count_is_bounded_after_ot_page(app, scm_config):
    with app.app_context():
        seeded = _seed_observability_graph()
        actor = seeded["full"]
        for index in range(12):
            db.session.add(RegistroDiarioProduccion(
                codigo_ot=f"OT-OBS-EMPTY-{index:02d}",
                codigo_ot_sintetico=False,
                estado="PLANIFICADA",
                tipo_ot="FABRICACION",
                fecha=date(2026, 7, index + 1),
                turno="DIA",
                maquina_id=1,
                responsable_id=1,
                created_by_id=actor.id,
            ))
        db.session.commit()
        db.session.expire_all()
        statements = []

        def before_cursor_execute(
            _conn, _cursor, statement, _parameters, _context, _executemany
        ):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", before_cursor_execute)
        try:
            payload = list_production_ot_observability(
                db.session,
                actor_id=actor.id,
                filters={"limit": "25"},
            )
        finally:
            event.remove(
                db.engine, "before_cursor_execute", before_cursor_execute
            )

        assert len(payload["items"]) == 14
        assert len(statements) <= 18


def test_observability_summary_is_not_truncated_at_list_page_size(
    app, client, scm_config
):
    with app.app_context():
        actor = _seed_observability_graph()["full"]
        first_day = date(2026, 1, 1)
        for index in range(101):
            db.session.add(RegistroDiarioProduccion(
                codigo_ot=f"OT-OBS-SUM-{index:03d}",
                codigo_ot_sintetico=False,
                estado="PLANIFICADA",
                tipo_ot="FABRICACION",
                fecha=first_day + timedelta(days=index),
                turno="DIA",
                maquina_id=1,
                responsable_id=1,
                created_by_id=actor.id,
            ))
        db.session.commit()

        response = _get(
            client,
            "/api/scm/v1/observabilidad/resumen"
            "?granularidad=MES&desde=2026-01-01&hasta=2026-08-10",
            actor,
        )

        assert response.status_code == 200
        assert response.get_json()["totales"]["ots"] == 103


def test_observability_missing_detail_is_404(app, scm_config):
    with app.app_context():
        actor = _seed_observability_graph()["full"]
        try:
            get_production_ot_observability(
                db.session,
                actor_id=actor.id,
                public_id=UUID(str(uuid4())),
            )
        except Exception as error:
            assert getattr(error, "code", None) == "OBSERVABILITY_OT_NOT_FOUND"
            assert getattr(error, "status_code", None) == 404
        else:
            raise AssertionError("La OT inexistente no debe producir detalle")


def test_manga_observability_lists_and_finds_any_manga_by_code(
    app, client, scm_config
):
    with app.app_context():
        seeded = _seed_observability_graph()
        response = _get(
            client,
            "/api/scm/v1/observabilidad/mangas?q=M-OT-OBS-FAB-02",
            seeded["full"],
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["page"] == {
            "next_cursor": None,
            "limit": 25,
            "has_more": False,
        }
        assert len(payload["items"]) == 1
        item = payload["items"][0]
        assert item["manga"]["codigo"] == "M-OT-OBS-FAB-02"
        assert item["manga"]["articulo"]["codigo"] == "PC-OBS"
        assert item["manga"]["color"] == "AZUL"
        assert item["manga"]["pesaje"]["peso_fisico_neto_kg"] == 11.5
        assert item["ot"]["codigo"] == "OT-OBS-FAB"
        assert item["trabajo"]["color"] == "AZUL"
        assert item["upstream"]["orden"]["codigo"] == "OF-OBS-001"

        base_response = _get(
            client,
            "/api/scm/v1/observabilidad/mangas?manga=M-OT-OBS-FAB-03",
            seeded["base"],
        )
        assert base_response.status_code == 200
        base_item = base_response.get_json()["items"][0]
        assert base_item["manga"]["pesaje"] is None
        assert base_item["manga"]["almacen"] is None


def test_manga_observability_filters_state_article_and_keeps_cursor_snapshot(
    app, client, scm_config
):
    with app.app_context():
        seeded = _seed_observability_graph()
        actor = seeded["full"]
        first = _get(
            client,
            "/api/scm/v1/observabilidad/mangas?limit=2&articulo=PC-OBS",
            actor,
        )
        assert first.status_code == 200
        first_payload = first.get_json()
        assert len(first_payload["items"]) == 2
        assert first_payload["page"]["has_more"] is True
        cursor = first_payload["page"]["next_cursor"]

        second = _get(
            client,
            f"/api/scm/v1/observabilidad/mangas?limit=2&articulo=PC-OBS&cursor={cursor}",
            actor,
        )
        assert second.status_code == 200
        assert second.get_json()["as_of"] == first_payload["as_of"]

        filtered = _get(
            client,
            "/api/scm/v1/observabilidad/mangas?estado_manga=ANULADA",
            actor,
        )
