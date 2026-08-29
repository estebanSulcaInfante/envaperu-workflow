from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import update

from app.extensions import db
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.models.producto import PiezaColor
from app.models.scm_articulos import ScmArticulo, ScmDefinicionWip
from app.models.scm_empaque import (
    ScmArticuloPerfil,
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_assembly_execution import ScmConsumoComponenteArmado
from app.models.scm_estructuras import (
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_ot import ScmTrabajoColor, ScmTrabajoOt
from app.models.scm_ot import ScmManga
from app.models.scm_inline_wip import (
    ScmMovimientoWipSalida,
    ScmReservaWipSalida,
    ScmSaldoWipSalida,
)
from app.models.scm_internal_supply import (
    ScmAsignacionPoolArmado,
    ScmPoolOrigenArmado,
)
from app.models.scm_inventory import (
    ScmSaldoInventario,
    ScmUbicacionInventario,
)
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.models.scm_rutas import (
    ScmCentroTrabajo,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_assembly_execution_service import (
    approve_assembly_quantity_correction,
    assign_assembly_output_mangas,
    close_assembly_manga,
    recalculate_assembly_manga_plan,
    request_assembly_quantity_correction,
)
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_internal_supply_service import (
    create_assembly_ot,
    create_supply_request,
)
from app.services.scm_fabrication_order_service import close_fabrication_order
from app.services.scm_ot_service import (
    annul_manga,
    recalculate_fabrication_manga_plan,
    transition_color_work,
)
from app.services.scm_service_support import ScmServiceError
import pytest


def _add_roles(actor, *codes):
    for code in codes:
        role = RolOperativo.query.filter_by(codigo=code).one()
        if role not in actor.roles:
            actor.roles.append(role)


def _seed_concurrent_wip_flow():
    ensure_initial_scm_configuration()
    actor = Trabajador.query.filter_by(codigo="TRB-01").one()
    _add_roles(
        actor,
        "INGENIERIA_SCM",
        "SUPERVISOR",
        "JEFE_PRODUCCION",
        "JEFE_ENSAMBLE",
    )

    # Piece-color articles are catalog identities, not free-standing rows.
    # Seed the legacy source rows so the canonical dual-write creates their
    # mandatory subtypes on both SQLite and PostgreSQL.
    db.session.add_all([
        PiezaColor(
            sku="PC-F3-TAPA",
            piezas="Tapa fresca F3",
            peso=100,
        ),
        PiezaColor(
            sku="PC-F3-PICO",
            piezas="Pico previo F3",
            peso=20,
        ),
    ])
    db.session.flush()
    tapa = ScmArticulo.query.filter_by(codigo="PC-F3-TAPA").one()
    pico = ScmArticulo.query.filter_by(codigo="PC-F3-PICO").one()
    salida_wip = ScmArticulo(
        codigo="WIP-F3-TAPA-PICO",
        nombre="Tapa con pico F3",
        clase="SUBENSAMBLE_WIP",
        definicion_wip=ScmDefinicionWip(
            descripcion="Tapa fresca armada con un pico previo",
            requiere_calidad=False,
        ),
    )
    center = ScmCentroTrabajo(
        codigo="CT-F3-LINEA",
        nombre="Armado concurrente F3",
        tipo="ENSAMBLE",
    )
    structure = ScmEstructuraRevision(
        articulo_resultado=salida_wip,
        numero_revision=1,
        # PostgreSQL freezes children as soon as a revision is approved.
        # Build the draft first and publish it only after its BOM is flushed.
        estado="BORRADOR",
        content_hash=None,
        creada_por_id=actor.id,
        componentes=[
            ScmEstructuraComponente(
                secuencia=1,
                articulo_componente=tapa,
                cantidad=1,
                unidad="UN",
            ),
            ScmEstructuraComponente(
                secuencia=2,
                articulo_componente=pico,
                cantidad=1,
                unidad="UN",
            ),
        ],
    )
    route = ScmRutaRevision(
        articulo_objetivo=salida_wip,
        numero_revision=1,
        # Route operations follow the same draft-before-publish contract.
        estado="BORRADOR",
        content_hash=None,
        creada_por_id=actor.id,
    )
    db.session.add_all([salida_wip, center, structure, route])
    db.session.flush()
    route_operation = ScmOperacionRuta(
        ruta=route,
        clave="ARMAR_EN_LINEA",
        secuencia_visible=10,
        nombre="Incorporar pico a tapa fresca",
        tipo="ENSAMBLE",
        executor_kind="ORDEN_OPERACION",
        centro_trabajo=center,
        articulo_salida=salida_wip,
        estructura_revision=structure,
        permite_concurrente=True,
    )
    db.session.add(route_operation)
    db.session.flush()
    structure.estado = "APROBADA"
    structure.content_hash = "a" * 64
    structure.aprobada_por_id = actor.id
    route.estado = "APROBADA"
    route.content_hash = "b" * 64
    route.aprobada_por_id = actor.id
    db.session.flush()

    assembly_order = ScmOrdenOperacion(
        codigo="OA-F3-000001",
        tipo="ENSAMBLE",
        origen_demanda="REPOSICION_WIP",
        motivo="Piloto sintético F3",
        estado="LIBERADA",
        operacion_ruta_revision_id=route_operation.id,
        operacion_ruta_hash=route.content_hash,
        created_by_id=actor.id,
        released_by_id=actor.id,
        salidas=[
            ScmOrdenOperacionSalida(
                articulo=salida_wip,
                cantidad_objetivo=Decimal("10"),
                peso_unitario_snapshot_g=Decimal("120"),
            )
        ],
    )

    machine = Maquina.query.first()
    fabrication_order = ScmOrdenOperacion(
        codigo="OF-F3-000001",
        tipo="FABRICACION",
        origen_demanda="EXCEPCIONAL",
        estado="EN_EJECUCION",
        created_by_id=actor.id,
        released_by_id=actor.id,
    )
    fabrication = ScmOrdenFabricacion(
        orden_operacion=fabrication_order,
        maquina_prevista_id=machine.id,
    )
    run = ScmCorridaFabricacion(
        orden_fabricacion=fabrication,
        codigo="OF-F3-000001-C01",
        secuencia=1,
        ciclos_objetivo=10,
        estado="EN_EJECUCION",
    )
    fabrication_output = ScmOrdenOperacionSalida(
        orden_operacion=fabrication_order,
        corrida_fabricacion=run,
        articulo=tapa,
        cantidad_por_ciclo_snapshot=1,
        peso_unitario_snapshot_g=100,
        cantidad_objetivo=10,
    )
    fabrication_ot = RegistroDiarioProduccion(
        codigo_ot="OT-F3-FAB-01",
        codigo_ot_sintetico=False,
        estado="EN_EJECUCION",
        tipo_ot="FABRICACION",
        maquina_id=machine.id,
        orden_operacion_id=fabrication_order.id,
        corrida_fabricacion_id=run.id,
        fecha=date(2026, 8, 29),
        turno="DIA",
        created_by_id=actor.id,
        secuencia_siguiente_trabajo=2,
    )
    db.session.add_all(
        [assembly_order, fabrication_order, fabrication_ot, fabrication_output]
    )
    db.session.flush()
    color_work = ScmTrabajoOt(
        orden_trabajo_id=fabrication_ot.id,
        codigo="OT-F3-FAB-01-TC01",
        tipo="COLOR",
        secuencia=1,
        estado="EN_EJECUCION",
        orden_operacion_id=fabrication_order.id,
        cantidad_objetivo_un=0,
        cantidad_confirmada_un=0,
        created_by_id=actor.id,
    )
    color_snapshot = ScmTrabajoColor(
        trabajo=color_work,
        corrida=run,
        color_nombre_snapshot="BLANCO F3",
    )

    container = ScmTipoContenedor(
        codigo="MANGA-F3",
        clase="MANGA",
        nombre="Manga F3",
        tara_nominal_g=100,
        tolerancia_tara_g=10,
        peso_bruto_max_kg=20,
    )
    profile = ScmPerfilEmpacable(
        codigo="PERF-F3-WIP",
        nombre="Tapa y pico apilados F3",
    )
    rule = ScmReglaEmpaque(perfil=profile, tipo_contenedor=container)
    rule_revision = ScmReglaEmpaqueRevision(
        regla=rule,
        numero_revision=1,
        estado="APROBADA",
        medicion_fisica_probada=True,
        cantidad_objetivo_un=10,
        cantidad_maxima_probada_un=10,
        peso_neto_operativo_max_kg=10,
        margen_seguridad_kg=0,
        tolerancia_peso_abs_g=20,
        tolerancia_peso_pct=1,
        tara_nominal_g_snapshot=100,
        tolerancia_tara_g_snapshot=10,
        peso_bruto_max_kg_snapshot=20,
        content_hash="c" * 64,
        creada_por_id=actor.id,
        aprobada_por_id=actor.id,
    )
    profile_link = ScmArticuloPerfil(
        articulo=salida_wip,
        perfil=profile,
        es_predeterminado=True,
        activo=True,
    )
    fresh_output_profile_link = ScmArticuloPerfil(
        articulo=tapa,
        perfil=profile,
        es_predeterminado=True,
        activo=True,
    )
    db.session.add_all(
        [
            color_work,
            color_snapshot,
            container,
            profile,
            rule,
            rule_revision,
            profile_link,
            fresh_output_profile_link,
        ]
    )
    db.session.commit()
    recalculate_fabrication_manga_plan(
        db.session,
        actor_id=actor.id,
        order_id=fabrication_order.id,
        operation_id=uuid4(),
        data={},
    )
    return actor, assembly_order, center, color_work, tapa, pico


def _plan_and_assign(
    actor, order, center, color_work, *, operational_date="2026-08-29"
):
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
            "fecha_operativa": operational_date,
            "turno": "DIA",
            "centro_trabajo_id": center.id,
            "responsable_id": actor.id,
            "cantidad_objetivo": 10,
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
    return created, assigned


def test_concurrent_output_reserves_fresh_component_and_supply_omits_it(
    app, scm_config
):
    with app.app_context():
        actor, order, center, color_work, tapa, pico = (
            _seed_concurrent_wip_flow()
        )
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )

        reservations = assigned["reservas_produccion_linea"]
        assert len(reservations) == 1
        assert reservations[0]["articulo"]["id"] == tapa.id
        assert reservations[0]["estado"] == "CREDITO_EN_LINEA_PENDIENTE"
        assert reservations[0]["cantidad_reservada"] == "10.000"

        request = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        assert [line["articulo"]["id"] for line in request["lineas"]] == [
            pico.id
        ]


def test_api_real_flow_reserves_inline_quota_without_fake_fabrication_manga(
    app, client, scm_config
):
    with app.app_context():
        actor, order, center, seeded_work, tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        fabrication_order = seeded_work.orden_operacion
        run = seeded_work.trabajo_color.corrida
        machine_id = seeded_work.orden_trabajo.maquina_id
        actor_id = actor.id
        order_id = order.id
        center_id = center.id
        run_id = str(run.id)
        fabrication_order_id = str(fabrication_order.id)
        seeded_work.estado = "PAUSADO"
        db.session.commit()

    headers = {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(uuid4()),
    }
    planned = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{fabrication_order_id}/plan-mangas/recalcular",
        headers=headers,
        json={},
    )
    assert planned.status_code == 201

    header = client.post(
        "/api/scm/v1/ots/fabricacion",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "maquina_id": machine_id,
            "fecha_operativa": "2026-08-30",
            "turno": "DIA",
            "maquinista_predeterminado_id": actor_id,
        },
    )
    assert header.status_code == 201
    fabrication_ot_id = header.get_json()["ot"]["public_id"]

    work_response = client.post(
        f"/api/scm/v1/ots/{fabrication_ot_id}/trabajos-color",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "corrida_fabricacion_id": run_id,
            "maquinista_id": actor_id,
            "asignaciones": [],
        },
    )
    assert work_response.status_code == 201
    work_payload = work_response.get_json()["trabajo_color"]
    work_id = UUID(work_payload["id"])
    started = client.post(
        f"/api/scm/v1/trabajos-color/{work_id}/iniciar",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"version": work_payload["version"]},
    )
    assert started.status_code == 200

    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        order = db.session.get(ScmOrdenOperacion, UUID(str(order_id)))
        center = db.session.get(ScmCentroTrabajo, center_id)
        work = db.session.get(ScmTrabajoOt, work_id)
        created, assigned = _plan_and_assign(
            actor,
            order,
            center,
            work,
            operational_date="2026-08-30",
        )

        assert assigned["reservas_produccion_linea"][0][
            "cantidad_reservada"
        ] == "10.000"
        assert format(work.cantidad_objetivo_un, "f") == "10.000"
        assert ScmManga.query.filter_by(trabajo_ot_id=work.id).count() == 0
        assert ScmReservaWipSalida.query.filter_by(
            saldo_id=ScmSaldoWipSalida.query.filter_by(
                trabajo_color_id=work.id
            ).one().id
        ).count() == 1
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        from app.models.scm_internal_supply import ScmSolicitudAbastecimiento

        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        _stage_previous_component(actor, request)
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == UUID(
                    assigned["mangas"][0]["public_id"]
                )
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.commit()
        closed = close_assembly_manga(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={"version": manga.version, "cantidad_real": 10},
        )

        assert closed["manga"]["estado"] == (
            "CERRADA_ARMADO_PENDIENTE_PESAJE"
        )
        assert format(work.cantidad_confirmada_un, "f") == "10.000"


def _stage_previous_component(actor, request, quantity="10"):
    line = request.lineas[0]
    location = ScmUbicacionInventario.query.filter_by(
        codigo="MESA_ARMADO"
    ).one_or_none()
    if location is None:
        location = ScmUbicacionInventario(
            codigo="MESA_ARMADO",
            nombre="Mesa de Armado",
            clases_articulo_json=["PIEZA_COLOR", "SUBENSAMBLE_WIP"],
        )
        db.session.add(location)
        db.session.flush()
    balance = ScmSaldoInventario(
        articulo_scm_id=line.articulo_scm_id,
        ubicacion_id=location.id,
        cantidad_fisica=Decimal(quantity),
        cantidad_reservada=Decimal(quantity),
        cantidad_no_disponible=0,
    )
    pool = ScmPoolOrigenArmado(
        articulo_scm_id=line.articulo_scm_id,
        saldo=balance,
        modo="LEGACY_SIN_ORIGEN",
        cantidad_inicial=Decimal(quantity),
        cantidad_disponible=Decimal(quantity),
        motivo="Fixture F3 de stock previo trazable",
        creado_por_id=actor.id,
        operation_id=uuid4(),
    )
    assignment = ScmAsignacionPoolArmado(
        linea=line,
        pool=pool,
        saldo=balance,
        cantidad_asignada=Decimal(quantity),
        estado="EN_STAGING_ARMADO",
        asignada_por_id=actor.id,
    )
    request.estado = "RECIBIDA"
    request.recibida_por_id = actor.id
    db.session.add_all([balance, pool, assignment])
    db.session.commit()
    return balance


def test_close_concurrent_manga_credits_and_consumes_fresh_output_atomically(
    app, scm_config
):
    with app.app_context():
        actor, order, center, color_work, tapa, pico = (
            _seed_concurrent_wip_flow()
        )
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        from app.models.scm_internal_supply import ScmSolicitudAbastecimiento

        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        pico_balance = _stage_previous_component(actor, request)
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == UUID(
                    assigned["mangas"][0]["public_id"]
                )
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.commit()

        operation_id = uuid4()
        closed = close_assembly_manga(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=operation_id,
            data={"version": manga.version, "cantidad_real": 10},
        )
        replay = close_assembly_manga(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=operation_id,
            data={"version": manga.version - 1, "cantidad_real": 10},
        )

        assert replay == closed
        assert closed["manga"]["estado"] == (
            "CERRADA_ARMADO_PENDIENTE_PESAJE"
        )
        assert closed["pesaje_creado"] is False
        assert closed["kardex_salida_creado"] is False
        assert ScmSaldoWipSalida.query.count() == 1
        saldo = ScmSaldoWipSalida.query.one()
        assert saldo.trabajo_color_id == color_work.id
        assert saldo.articulo_id == tapa.id
        assert format(saldo.cantidad_acreditada, "f") == "10.000"
        assert format(saldo.cantidad_consumida, "f") == "10.000"
        assert format(saldo.cantidad_disponible, "f") == "0.000"
        reservation = ScmReservaWipSalida.query.one()
        assert reservation.estado == "APLICADA"
        assert format(reservation.cantidad_aplicada, "f") == "10.000"
        assert ScmMovimientoWipSalida.query.count() == 2
        assert {
            item.tipo for item in ScmMovimientoWipSalida.query.all()
        } == {"SALIDA_BUENA_CONFIRMADA", "CONSUMO_EN_LINEA_ARMADO"}
        assert format(color_work.cantidad_confirmada_un, "f") == "10.000"
        assert format(pico_balance.cantidad_fisica, "f") == "0.000"
        assert ScmConsumoComponenteArmado.query.count() == 2
        assert {
            item.procedencia
            for item in ScmConsumoComponenteArmado.query.all()
        } == {"PRODUCIDO_OT_ACTUAL", "CONSUMIDO_STOCK_PREVIO"}

        color_work.estado = "PAUSADO"
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            transition_color_work(
                db.session,
                actor_id=actor.id,
                work_id=color_work.id,
                operation_id=uuid4(),
                data={
                    "version": color_work.version,
                    "motivo": "No debe borrar producción ya incorporada",
                },
                action="anular",
            )
        assert error.value.code == "WORK_HAS_PRODUCTION_FACTS"


def test_close_concurrent_manga_rolls_back_when_previous_stock_is_insufficient(
    app, scm_config
):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        from app.models.scm_internal_supply import ScmSolicitudAbastecimiento

        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        pico_balance = _stage_previous_component(actor, request, quantity="9")
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == UUID(
                    assigned["mangas"][0]["public_id"]
                )
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.commit()

        with pytest.raises(ScmServiceError) as error:
            close_assembly_manga(
                db.session,
                actor_id=actor.id,
                manga_id=manga.public_id,
                operation_id=uuid4(),
                data={"version": manga.version, "cantidad_real": 10},
            )

        assert error.value.code == "COMPONENT_STOCK_INSUFFICIENT"
        db.session.expire_all()
        saldo = ScmSaldoWipSalida.query.one()
        reservation = ScmReservaWipSalida.query.one()
        refreshed_work = db.session.get(ScmTrabajoOt, color_work.id)
        refreshed_balance = db.session.get(
            ScmSaldoInventario, pico_balance.id
        )
        assert format(saldo.cantidad_acreditada, "f") == "0.000"
        assert format(saldo.cantidad_consumida, "f") == "0.000"
        assert reservation.estado == "CREDITO_EN_LINEA_PENDIENTE"
        assert format(reservation.cantidad_aplicada, "f") == "0.000"
        assert format(refreshed_work.cantidad_confirmada_un, "f") == "0.000"
        assert format(refreshed_balance.cantidad_fisica, "f") == "9.000"
        assert ScmMovimientoWipSalida.query.count() == 0
        assert ScmConsumoComponenteArmado.query.count() == 0


def test_close_revalidates_supply_after_stale_availability_snapshot(
    app, scm_config
):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        from app.models.scm_internal_supply import ScmSolicitudAbastecimiento

        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        _stage_previous_component(actor, request)
        pool_assignment = request.lineas[0].asignaciones_pool[0]
        assert pool_assignment.estado == "EN_STAGING_ARMADO"
        db.session.execute(
            update(ScmAsignacionPoolArmado)
            .where(ScmAsignacionPoolArmado.id == pool_assignment.id)
            .values(estado="RETORNADA"),
            execution_options={"synchronize_session": False},
        )
        # Conserva deliberadamente el objeto ya cargado: representa la
        # prevalidación que quedó obsoleta mientras otra transacción retornaba
        # el abastecimiento antes del bloqueo definitivo.
        assert pool_assignment.estado == "EN_STAGING_ARMADO"
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == UUID(
                    assigned["mangas"][0]["public_id"]
                )
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"

        with pytest.raises(ScmServiceError) as error:
            close_assembly_manga(
                db.session,
                actor_id=actor.id,
                manga_id=manga.public_id,
                operation_id=uuid4(),
                data={"version": manga.version, "cantidad_real": 10},
            )

        assert error.value.code == "COMPONENT_STOCK_INSUFFICIENT"
        db.session.expire_all()
        assert format(
            ScmSaldoWipSalida.query.one().cantidad_acreditada, "f"
        ) == "0.000"
        assert ScmMovimientoWipSalida.query.count() == 0


def test_partial_inline_close_releases_unused_source_quota(app, scm_config):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        from app.models.scm_internal_supply import ScmSolicitudAbastecimiento

        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        _stage_previous_component(actor, request)
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == UUID(
                    assigned["mangas"][0]["public_id"]
                )
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.commit()

        close_assembly_manga(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={
                "version": manga.version,
                "cantidad_real": 9,
                "motivo_diferencia": "Una tapa no quedó conforme",
            },
        )

        reservation = ScmReservaWipSalida.query.one()
        assert reservation.estado == "APLICADA"
        assert format(reservation.cantidad_reservada, "f") == "10.000"
        assert format(reservation.cantidad_aplicada, "f") == "9.000"
        assert format(
            reservation.asignacion_plan.cantidad_asignada_un, "f"
        ) == "9.000"
        assert format(color_work.cantidad_objetivo_un, "f") == "9.000"
        assert format(color_work.cantidad_confirmada_un, "f") == "9.000"


def test_fabrication_close_counts_applied_inline_credit_without_fake_manga(
    app, scm_config
):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        from app.models.scm_internal_supply import ScmSolicitudAbastecimiento

        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        _stage_previous_component(actor, request)
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == UUID(
                    assigned["mangas"][0]["public_id"]
                )
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.commit()
        close_assembly_manga(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={"version": manga.version, "cantidad_real": 10},
        )

        fabrication_order = color_work.orden_operacion
        transition_color_work(
            db.session,
            actor_id=actor.id,
            work_id=color_work.id,
            operation_id=uuid4(),
            data={"version": color_work.version},
            action="completar",
        )
        closed = close_fabrication_order(
            db.session,
            actor_id=actor.id,
            operation_id=uuid4(),
            operation_order_id=fabrication_order.id,
            data={"version": fabrication_order.version},
        )

        assert closed["estado"] == "CERRADA"
        assert format(fabrication_order.salidas[0].cantidad_real, "f") == (
            "10.000"
        )
        assert ScmManga.query.filter_by(
            trabajo_ot_id=color_work.id
        ).count() == 0


def test_fabrication_close_blocks_pending_inline_reservation(app, scm_config):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        _created, _assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        fabrication_order = color_work.orden_operacion
        color_work.estado = "COMPLETADO"
        db.session.commit()

        with pytest.raises(ScmServiceError) as error:
            close_fabrication_order(
                db.session,
                actor_id=actor.id,
                operation_id=uuid4(),
                operation_order_id=fabrication_order.id,
                data={"version": fabrication_order.version},
            )

        assert error.value.code == "OF_HAS_PENDING_INLINE_RESERVATIONS"


def test_inline_correction_blocks_after_source_fabrication_order_closed(
    app, scm_config
):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        approver = Trabajador(
            codigo="TRB-F3-CLOSED-OF-APPROVER",
            nombres="Aprobadora",
            apellidos="OF cerrada",
            activo=True,
            roles=[
                RolOperativo.query.filter_by(
                    codigo="JEFE_PRODUCCION"
                ).one()
            ],
        )
        db.session.add(approver)
        db.session.commit()
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        from app.models.scm_internal_supply import ScmSolicitudAbastecimiento

        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        _stage_previous_component(actor, request)
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == UUID(
                    assigned["mangas"][0]["public_id"]
                )
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.commit()
        close_assembly_manga(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={"version": manga.version, "cantidad_real": 10},
        )
        fabrication_order = color_work.orden_operacion
        color_work.estado = "COMPLETADO"
        db.session.commit()
        close_fabrication_order(
            db.session,
            actor_id=actor.id,
            operation_id=uuid4(),
            operation_order_id=fabrication_order.id,
            data={"version": fabrication_order.version},
        )
        correction = request_assembly_quantity_correction(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={
                "cantidad_propuesta": 9,
                "motivo": "Conteo detectado después del cierre de OF",
            },
        )["correccion"]

        with pytest.raises(ScmServiceError) as error:
            approve_assembly_quantity_correction(
                db.session,
                actor_id=approver.id,
                correction_id=UUID(correction["id"]),
                operation_id=uuid4(),
                data={
                    "motivo_aprobacion": "Validación posterior al cierre"
                },
            )

        assert error.value.code == "INLINE_SOURCE_OF_REOPEN_REQUIRED"
        db.session.expire_all()
        assert format(
            db.session.get(ScmTrabajoOt, color_work.id).cantidad_confirmada_un,
            "f",
        ) == "10.000"
        assert format(
            ScmSaldoWipSalida.query.one().cantidad_acreditada, "f"
        ) == "10.000"


def test_annul_unweighed_inline_manga_releases_pending_reservation(
    app, scm_config
):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        _created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        cancelled = annul_manga(
            db.session,
            actor_id=actor.id,
            manga_id=UUID(assigned["mangas"][0]["public_id"]),
            operation_id=uuid4(),
            data={"motivo": "Manga creada por error en el piloto F3"},
        )

        assert cancelled["manga"]["estado"] == "ANULADA"
        reservation = ScmReservaWipSalida.query.one()
        assert reservation.estado == "CANCELADA"
        assert format(reservation.cantidad_aplicada, "f") == "0.000"
        assert format(
            reservation.asignacion_plan.cantidad_asignada_un, "f"
        ) == "0.000"
        assert format(color_work.cantidad_objetivo_un, "f") == "0.000"


def test_annul_color_work_blocks_pending_inline_reservation(app, scm_config):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        _plan_and_assign(actor, order, center, color_work)
        color_work.estado = "PAUSADO"
        db.session.commit()

        with pytest.raises(ScmServiceError) as error:
            transition_color_work(
                db.session,
                actor_id=actor.id,
                work_id=color_work.id,
                operation_id=uuid4(),
                data={
                    "version": color_work.version,
                    "motivo": "Intento de anulación con destino pendiente",
                },
                action="anular",
            )

        assert error.value.code == "WORK_HAS_PENDING_INLINE_RESERVATIONS"
        db.session.expire_all()
        assert db.session.get(ScmTrabajoOt, color_work.id).estado == "PAUSADO"


def test_inline_quantity_correction_compensates_both_ledgers_before_weighing(
    app, scm_config
):
    with app.app_context():
        actor, order, center, color_work, _tapa, _pico = (
            _seed_concurrent_wip_flow()
        )
        approver = Trabajador(
            codigo="TRB-F3-APPROVER",
            nombres="Aprobadora",
            apellidos="F3",
            activo=True,
            roles=[
                RolOperativo.query.filter_by(
                    codigo="JEFE_PRODUCCION"
                ).one()
            ],
        )
        db.session.add(approver)
        db.session.commit()
        created, assigned = _plan_and_assign(
            actor, order, center, color_work
        )
        request_payload = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=UUID(created["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        from app.models.scm_internal_supply import ScmSolicitudAbastecimiento

        request = db.session.get(
            ScmSolicitudAbastecimiento, UUID(request_payload["id"])
        )
        pico_balance = _stage_previous_component(actor, request)
        manga = db.session.scalar(
            db.select(ScmManga).where(
                ScmManga.public_id == UUID(
                    assigned["mangas"][0]["public_id"]
                )
            )
        )
        manga.estado = "PREETIQUETADA"
        assembly_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(created["public_id"])
        ).one()
        assembly_ot.estado = "EN_EJECUCION"
        db.session.commit()
        close_assembly_manga(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={"version": manga.version, "cantidad_real": 10},
        )
        correction = request_assembly_quantity_correction(
            db.session,
            actor_id=actor.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={
                "cantidad_propuesta": 9,
                "motivo": "Conteo físico rectificado antes del pesaje",
            },
        )["correccion"]
        corrected = approve_assembly_quantity_correction(
            db.session,
            actor_id=approver.id,
            correction_id=UUID(correction["id"]),
            operation_id=uuid4(),
            data={"motivo_aprobacion": "Conteo verificado por jefatura"},
        )

        assert corrected["manga"]["cantidad_confirmada_un"] == "9"
        saldo = ScmSaldoWipSalida.query.one()
        reservation = ScmReservaWipSalida.query.one()
        assert format(saldo.cantidad_acreditada, "f") == "9.000"
        assert format(saldo.cantidad_consumida, "f") == "9.000"
        assert format(reservation.cantidad_aplicada, "f") == "9.000"
        assert format(
            reservation.asignacion_plan.cantidad_asignada_un, "f"
        ) == "9.000"
        assert format(color_work.cantidad_objetivo_un, "f") == "9.000"
        assert format(color_work.cantidad_confirmada_un, "f") == "9.000"
        assert format(pico_balance.cantidad_fisica, "f") == "1.000"
        assert ScmMovimientoWipSalida.query.count() == 4
        assert {
            item.tipo for item in ScmMovimientoWipSalida.query.all()
        } == {
            "SALIDA_BUENA_CONFIRMADA",
            "CONSUMO_EN_LINEA_ARMADO",
            "REVERSO_SALIDA_BUENA",
            "REVERSO_CONSUMO_EN_LINEA_ARMADO",
        }

