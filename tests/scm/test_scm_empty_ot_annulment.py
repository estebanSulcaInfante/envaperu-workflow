from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models.estacion_pesaje import EstacionPesaje
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.scm_inline_wip import ScmReservaWipSalida
from app.models.scm_ot import (
    ScmAsignacionPlanMangaOt,
    ScmAsignacionPersonalTrabajoOt,
    ScmControlPesoManga,
    ScmEtiquetaManga,
    ScmManga,
    ScmPesajeManga,
    ScmTramoMangaTrabajo,
)
from app.models.trabajador import RolOperativo
from app.services import scm_ot_service as service
from app.services.scm_assembly_execution_service import (
    assign_assembly_output_mangas,
    recalculate_assembly_manga_plan,
)
from app.services.scm_internal_supply_service import (
    create_assembly_ot,
    create_supply_request,
)
from tests.scm.test_scm_inline_assembly import _seed_concurrent_wip_flow
from tests.scm.test_scm_ot_service import _seed_fabrication_order, _seed_aggregate_color_work


def seed_empty():
    creator, approver, order, run, _ = _seed_fabrication_order()
    header = service.create_fabrication_ot_header(
        db.session, actor_id=creator.id, operation_id=uuid4(),
        data={"maquina_id": order.fabricacion.maquina_prevista_id,
              "fecha_operativa": "2026-09-02", "turno": "DIA"},
    )["ot"]
    return creator, approver, run, header


def post(client, actor, header, *, key=None, version=None, motivo="Creada por error"):
    return client.post(
        f'/api/scm/v1/ots/{header["public_id"]}/anular',
        json={"version": header["version"] if version is None else version, "motivo": motivo},
        headers={"X-Actor-Id": str(actor.id), "Idempotency-Key": str(key or uuid4())},
    )


def seed_cancelled_assembly_ot(*, quantity=10, with_label=False):
    actor, order, center, color_work, *_ = _seed_concurrent_wip_flow()
    order.salidas[0].cantidad_objetivo = quantity
    color_work.cantidad_objetivo_un = quantity
    color_work.orden_operacion.salidas[0].cantidad_objetivo = quantity
    for output in color_work.trabajo_color.corrida.salidas:
        if output.orden_operacion_id == color_work.orden_operacion_id:
            output.cantidad_objetivo = quantity
    db.session.commit()
    recalculate_assembly_manga_plan(
        db.session,
        actor_id=actor.id,
        order_id=order.id,
        operation_id=uuid4(),
        data={},
    )
    created = create_assembly_ot(
        db.session,
        actor_id=actor.id,
        order_id=order.id,
        operation_id=uuid4(),
        data={
            "fecha_operativa": "2026-08-29",
            "turno": "DIA",
            "centro_trabajo_id": center.id,
            "responsable_id": actor.id,
            "cantidad_objetivo": quantity,
            "modo_ejecucion": "CONCURRENTE",
            "trabajo_color_contexto_id": str(color_work.id),
        },
    )["ot"]
    assigned = assign_assembly_output_mangas(
        db.session,
        actor_id=actor.id,
        ot_id=UUID(created["public_id"]),
        operation_id=uuid4(),
        data={"version": created["version"]},
    )
    if with_label:
        service.generate_prelabels(
            db.session,
            actor_id=actor.id,
            manga_id=UUID(assigned["mangas"][0]["public_id"]),
            operation_id=uuid4(),
            data={"manga_ids": [assigned["mangas"][0]["public_id"]]},
        )
    for manga in assigned["mangas"]:
        service.annul_manga(
            db.session,
            actor_id=actor.id,
            manga_id=UUID(manga["public_id"]),
            operation_id=uuid4(),
            data={"motivo": "Manga creada por error"},
        )
    db.session.expire_all()
    return actor, created


def test_assembly_ot_annulment_accepts_only_cancelled_children_and_replays(
    app, client
):
    with app.app_context():
        actor, header = seed_cancelled_assembly_ot()
        ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(header["public_id"])
        ).one()
        assert ScmManga.query.filter_by(ot_id=ot.id).count() == 1
        assert ScmManga.query.filter_by(ot_id=ot.id, estado="ANULADA").count() == 1
        assert ScmReservaWipSalida.query.count() == 1
        assert all(
            item.estado == "CANCELADA"
            and item.cantidad_aplicada == 0
            for item in ScmReservaWipSalida.query.all()
        )
        listed = client.get(
            f'/api/scm/v1/ots/{header["public_id"]}',
            headers={"X-Actor-Id": str(actor.id)},
        )
        assert listed.status_code == 200, listed.json
        assert listed.json["ot"]["bloqueo_anulacion"] == (
            "La elegibilidad de anulación requiere verificación transaccional al anular."
        )

        key = uuid4()
        result = post(client, actor, header, key=key)
        assert result.status_code == 200, result.json
        assert result.json["ot"]["estado"] == "ANULADA"
        assert post(client, actor, header, key=key).json == result.json
        assert ScmEvento.query.filter_by(
            tipo="ASSEMBLY_OT_HEADER_ANNULLED"
        ).count() == 1
        db.session.expire_all()
        assert db.session.get(RegistroDiarioProduccion, ot.id).estado == "ANULADA"
        assert ScmManga.query.filter_by(ot_id=ot.id).count() == 1
        assert ScmReservaWipSalida.query.count() == 1


def test_assembly_ot_annulment_rejects_non_cancelled_reservation(app, client):
    with app.app_context():
        actor, header = seed_cancelled_assembly_ot()
        reservation = ScmReservaWipSalida.query.first()
        reservation.estado = "CREDITO_EN_LINEA_PENDIENTE"
        db.session.commit()
        result = post(client, actor, header)
        assert result.status_code == 409, result.json
        assert result.json["error"]["code"] == "OT_ANNULMENT_BLOCKED"


@pytest.mark.parametrize(
    "guard",
    [
        "applied_reservation",
        "nonzero_quota",
        "active_supply",
        "active_label",
        "historical_weighing",
        "historical_control",
        "unknown_ot_link",
        "no_oa",
        "invalid_oa",
    ],
)
def test_assembly_annulment_fails_closed_for_operational_guards(app, client, guard):
    with app.app_context():
        actor, header = seed_cancelled_assembly_ot(
            with_label=guard == "active_label"
        )
        ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(header["public_id"])
        ).one()
        manga = ScmManga.query.filter_by(ot_id=ot.id).one()

        if guard == "applied_reservation":
            reservation = ScmReservaWipSalida.query.one()
            reservation.cantidad_aplicada = Decimal("1")
        elif guard == "nonzero_quota":
            assignment = ScmAsignacionPlanMangaOt.query.filter_by(ot_id=ot.id).one()
            assignment.cantidad_asignada_un = Decimal("1")
        elif guard == "active_supply":
            # The request service requires an inline reservation while it is
            # being created; restore the cancelled reservation immediately
            # afterwards so this case isolates the active supply request.
            reservation = ScmReservaWipSalida.query.one()
            reservation.estado = "CREDITO_EN_LINEA_PENDIENTE"
            db.session.commit()
            create_supply_request(
                db.session,
                actor_id=actor.id,
                ot_id=UUID(header["public_id"]),
                operation_id=uuid4(),
            )
            reservation.estado = "CANCELADA"
        elif guard == "active_label":
            label = ScmEtiquetaManga.query.filter_by(manga_id=manga.id).one()
            label.estado = "GENERADA"
        elif guard == "historical_weighing":
            station_id = str(uuid4())
            db.session.add(EstacionPesaje(
                station_id=station_id,
                codigo=station_id,
                nombre="Estación de prueba",
                ubicacion="Pruebas",
                token_hash="a" * 64,
            ))
            db.session.flush()
            db.session.add(ScmPesajeManga(
                manga_id=manga.id,
                operation_id=uuid4(),
                source_system="TEST",
                station_id=station_id,
                capture_id=uuid4(),
                peso_bruto_kg=Decimal("1.100"),
                tara_kg=Decimal("0.100"),
                peso_fisico_neto_kg=Decimal("1"),
                tara_fuente="TIPO_MANGA",
                cantidad_confirmada=Decimal("1"),
                fuente_cantidad="RESPONSABLE_ARMADO",
                estado="ANULADO",
                kg_produccion_ot=Decimal("1"),
                atribucion_kg_estado="PENDIENTE",
                pesada_at=service.utc_now(),
                timezone_snapshot="America/Lima",
                fecha_local_pesaje=date(2026, 8, 29),
                dias_desfase_operativo=0,
                pesado_por_id=actor.id,
                snapshots_json={},
            ))
        elif guard == "historical_control":
            # The control is historical, but the manga link remains an
            # operational fact and must stay visible to the census.
            station_id = str(uuid4())
            db.session.add(EstacionPesaje(
                station_id=station_id,
                codigo=station_id,
                nombre="Estación de control",
                ubicacion="Pruebas",
                token_hash="b" * 64,
            ))
            personal = ScmAsignacionPersonalTrabajoOt(
                trabajo_ot_id=ot.trabajo_color_contexto_id,
                trabajador_id=actor.id,
                estado="PREVISTA",
                asignada_por_id=actor.id,
            )
            db.session.add(personal)
            db.session.flush()
            tramo = ScmTramoMangaTrabajo(
                manga_id=manga.id,
                trabajo_ot_id=ot.trabajo_color_contexto_id,
                asignacion_personal_trabajo_id=personal.id,
                secuencia=1,
                estado="ANULADO",
                cantidad_inicio_un=Decimal("1"),
                cantidad_atribuida_un=Decimal("0"),
                created_by_id=actor.id,
            )
            db.session.add(tramo)
            db.session.flush()
            db.session.add(ScmControlPesoManga(
                manga_id=manga.id,
                tramo_id=tramo.id,
                operation_id=uuid4(),
                source_system="TEST",
                station_id=station_id,
                capture_id=uuid4(),
                tipo="AVANCE_KG",
                peso_bruto_kg=Decimal("1.100"),
                tara_kg=Decimal("0.100"),
                peso_neto_kg=Decimal("1"),
                tara_fuente="TIPO_MANGA",
                unidad_evidencia="KG",
                motivo="Histórico anulado",
                pesado_at=service.utc_now(),
                timezone_snapshot="America/Lima",
                fecha_local_pesaje=date(2026, 8, 29),
                pesado_por_id=actor.id,
            ))
        elif guard == "unknown_ot_link":
            from app.models.registro import DetalleProduccionHora

            db.session.add(DetalleProduccionHora(registro_id=ot.id, hora="08:01"))
        elif guard == "no_oa":
            ot.orden_operacion_id = None
        elif guard == "invalid_oa":
            ot.orden_operacion_id = ot.trabajo_color_contexto.orden_operacion_id
        db.session.commit()

        result = post(client, actor, header)
        assert result.status_code == 409, result.json
        assert result.json["error"]["code"] == "OT_ANNULMENT_BLOCKED"
        assert RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(header["public_id"])
        ).one().estado == "PLANIFICADA"
        assert RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(header["public_id"])
        ).one().estado == "PLANIFICADA"


def test_assembly_ot_annulment_fails_closed_for_unknown_operational_link(
    app, client
):
    from app.models.registro import DetalleProduccionHora

    with app.app_context():
        actor, header = seed_cancelled_assembly_ot()
        ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(header["public_id"])
        ).one()
        db.session.add(DetalleProduccionHora(registro_id=ot.id, hora="08:00"))
        db.session.commit()
        result = post(client, actor, header)
        assert result.status_code == 409, result.json
        assert result.json["error"]["code"] == "OT_ANNULMENT_BLOCKED"


def test_planned_assembly_list_does_not_run_mutation_dependency_census(app):
    """Read eligibility stays bounded; mutation is the authoritative census."""
    with app.app_context():
        actor, _ = seed_cancelled_assembly_ot()
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lower())

        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            listed = service.list_ots(
                db.session,
                actor_id=actor.id,
                tipo_ot="ENSAMBLE",
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)

        assert len(listed["items"]) == 1
        # The list serializer has a fixed eager-load budget on this fixture;
        # the per-OT dependency census would add one probe per FK/manga.
        assert len(statements) <= 60
        for marker in (
            "scm_reserva_wip_salida.manga_id = ",
            "scm_asignacion_plan_manga_ot.ot_id = ",
            "scm_etiqueta_manga.manga_id = ",
        ):
            # Batched eager reads use IN; these equality probes are the
            # mutation-only census and must not appear in a list response.
            assert not any(marker in statement for statement in statements), marker


def test_m4_annul_empty_ot_audited_replay_and_new_identity(app, client):
    with app.app_context():
        creator, approver, run, header = seed_empty()
        key = uuid4()
        # Different authorized actor: ownership is not a restriction.
        result = post(client, approver, header, key=key)
        assert result.status_code == 200, result.json
        assert result.json["ot"]["estado"] == "ANULADA"
        assert post(client, approver, header, key=key).json == result.json
        event = ScmEvento.query.filter_by(tipo="FABRICATION_OT_HEADER_ANNULLED").one()
        assert event.motivo == "Creada por error"
        assert event.actor_id == approver.id
        assert event.before_json["estado"] == "PLANIFICADA"
        assert event.after_json["estado"] == "ANULADA"
        ot = RegistroDiarioProduccion.query.filter_by(public_id=UUID(header["public_id"])).one()
        assert ot.iniciada_at is None and ot.cerrada_at is None
        assert result.json["ot"]["anulacion"]["motivo"] == event.motivo
        new = service.create_fabrication_ot_header(
            db.session, actor_id=creator.id, operation_id=uuid4(),
            data={"maquina_id": ot.maquina_id, "fecha_operativa": "2026-09-02", "turno": "DIA"},
        )["ot"]
        assert new["public_id"] != header["public_id"]
        assert new["codigo_ot"] != header["codigo_ot"]
        with pytest.raises(service.ScmServiceError):
            service.add_color_work(db.session, actor_id=creator.id,
                ot_id=ot.public_id, operation_id=uuid4(),
                data={"corrida_fabricacion_id": str(run.id), "maquinista_id": creator.id, "asignaciones": []})
        with pytest.raises(service.ScmServiceError):
            service.transition_ot(db.session, actor_id=creator.id, public_id=ot.public_id,
                operation_id=uuid4(), data={"version": ot.version}, action="iniciar")


@pytest.mark.parametrize("case", ["reason", "permission", "version", "started", "closed", "legacy"])
def test_m4_invalid_annulment_is_atomic(app, client, case):
    with app.app_context():
        creator, _, _, header = seed_empty()
        ot = RegistroDiarioProduccion.query.filter_by(public_id=UUID(header["public_id"])).one()
        kwargs = {}
        expected = 409
        if case == "reason":
            kwargs["motivo"] = "   "
            expected = 400
        elif case == "permission":
            creator.roles = [RolOperativo.query.filter_by(codigo="PLANIFICACION").one()]
            expected = 403
        elif case == "version":
            kwargs["version"] = header["version"] + 1
        elif case == "started":
            ot.iniciada_at = service.utc_now()
        elif case == "closed":
            ot.estado = "CERRADA"
        elif case == "legacy":
            ot.codigo_ot_sintetico = True
        db.session.commit()
        result = post(client, creator, header, **kwargs)
        assert result.status_code == expected, result.json
        assert ScmEvento.query.filter_by(tipo="FABRICATION_OT_HEADER_ANNULLED").count() == 0
        db.session.refresh(ot)
        assert ot.estado != "ANULADA"


@pytest.mark.parametrize("work_state", ["PLANIFICADO", "ANULADO"])
def test_m4_work_even_if_annulled_blocks_header(app, client, work_state):
    with app.app_context():
        creator, _, _, _, _, _, header, created = _seed_aggregate_color_work()
        ot = RegistroDiarioProduccion.query.filter_by(public_id=UUID(header["public_id"])).one()
        ot.trabajos_ot[0].estado = work_state
        db.session.commit()
        result = post(client, creator, {**header, "version": ot.version})
        assert result.status_code == 409
        assert result.json["error"]["code"] == "OT_ANNULMENT_BLOCKED"


def test_m4_hourly_record_without_work_blocks_annulment(app, client):
    from app.models.registro import DetalleProduccionHora
    with app.app_context():
        creator, _, _, header = seed_empty()
        ot = RegistroDiarioProduccion.query.filter_by(public_id=UUID(header["public_id"])).one()
        db.session.add(DetalleProduccionHora(registro_id=ot.id, hora="08:00"))
        db.session.commit()
        result = post(client, creator, header)
        assert result.status_code == 409
        assert result.json["error"]["code"] == "OT_ANNULMENT_BLOCKED"


def test_m4_capability_only_approved_roles(app):
    from app.services.scm_configuration import ensure_initial_scm_configuration
    with app.app_context():
        ensure_initial_scm_configuration()
        actual = {role.codigo for role in RolOperativo.query.all()
                  if any(cap.codigo == "OT_ANULAR" for cap in role.capacidades)}
        assert actual == {"GERENTE_GENERAL", "JEFE_PRODUCCION", "SUPERVISOR"}
