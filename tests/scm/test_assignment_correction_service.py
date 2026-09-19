from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from decimal import Decimal
from datetime import date
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from app import db
from app.models.registro import RegistroDiarioProduccion
from app.models.producto import Familia, Linea
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmSaldoInventarioKg,
)
from app.models.scm_ot import (
    ScmAtribucionProduccionKg,
    ScmAsignacionPlanMangaOt,
    ScmAsignacionPersonalTrabajoOt,
    ScmCorreccionAsignacionManga,
    ScmManga,
    ScmTrabajoColor,
    ScmTrabajoOt,
)
from app.services.scm_assignment_correction_service import (
    _compatibility,
    _preview_payload,
    apply_assignment_correction,
    preview_assignment_correction,
)
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.trabajador import Trabajador
from app.models.scm_catalogos import ScmCapacidad
from app.services.scm_ot_service import create_fabrication_ot_header
from app.services.scm_service_support import ScmServiceError
from app.services.scm_weighing_service import (
    confirm_manga_weighing,
    reopen_manga_after_accidental_close,
    register_manga_weighing_control,
)
from app.services.scm_kg_production_service import close_productive_document_kg
from app.services import scm_weighing_service
from app.services.scm_manga_assignment_projection import (
    effective_assignment,
    effective_assignment_for_segment,
    effective_plan_assignment,
    effective_segment,
    effective_work,
    effective_work_for_segment,
)
from tests.scm.test_scm_kg_custody import _grant_capabilities
from tests.scm.test_scm_kg_production import _auto_final_kg_fixture
from tests.scm.test_scm_inline_assembly_postgres import postgres_inline_app
from test_scm_ot_service import (
    _print_color_manga,
    _seed_aggregate_color_work,
)
from app.services.scm_ot_service import add_normal_mangas, transition_color_work


def _grant_assignment_correction(actor):
    capability = ScmCapacidad.query.filter_by(
        codigo="MANGA_REATRIBUIR_TRABAJO"
    ).first()
    if capability is None:
        capability = ScmCapacidad(
            codigo="MANGA_REATRIBUIR_TRABAJO",
            nombre="Corregir OT/Trabajo de manga KG",
        )
        db.session.add(capability)
        db.session.flush()
    role = actor.roles[0]
    if capability not in role.capacidades:
        role.capacidades.append(capability)
    db.session.flush()


def _work(*, work_id=None, machine=10, operation="op-1", run="run-1", color=7, recipe=9):
    work_id = work_id or uuid4()
    return SimpleNamespace(
        id=work_id,
        codigo="TRABAJO-1",
        orden_trabajo_id=1,
        orden_operacion_id=operation,
        orden_trabajo=SimpleNamespace(maquina_id=machine, codigo_ot="OT-1", estado="EN_EJECUCION"),
        trabajo_color=SimpleNamespace(
            corrida_fabricacion_id=run,
            color_id_snapshot=color,
            receta_revision_id_snapshot=recipe,
            receta_hash_snapshot="hash-1",
        ),
        orden_operacion=SimpleNamespace(salidas=[]),
        estado="EN_EJECUCION",
        asignaciones_personal=[],
    )


def _manga(source):
    return SimpleNamespace(
        lote_articulo=SimpleNamespace(articulo_id=22),
        plan_linea=SimpleNamespace(orden_operacion_salida_id=None),
    )


def test_compatibilidad_rechaza_cambio_de_maquina_y_conserva_identidad_de_salida():
    source = _work()
    target = _work(machine=11)
    _left, _right, blockers = _compatibility(source, target, _manga(source))

    assert {item["code"] for item in blockers} == {"MAQUINA_DISTINTA"}


def test_preview_payload_expone_bloqueos_y_confirma_que_no_mueve_stock():
    source = _work()
    target = _work()
    target.id = uuid4()
    manga = SimpleNamespace(public_id=uuid4(), codigo="M-01", version=3, estado="PESADA")
    left, right, _compatibility_blockers = _compatibility(source, target, _manga(source))
    result = {
        "manga": manga,
        "source": source,
        "target": target,
        "compatibility": [{"code": "COLOR_DISTINTO", "message": "color"}],
        "blockers": [{"code": "KG_RESERVA_ACTIVA", "message": "reserva"}],
        "candidates": [],
        "checks": {"origen": left, "destino": right},
    }

    payload = _preview_payload(result)

    assert payload["puede_aplicar"] is False
    assert payload["compatibilidad"][0]["code"] == "COLOR_DISTINTO"
    assert payload["bloqueos"][0]["code"] == "KG_RESERVA_ACTIVA"
    assert payload["sin_cambio_stock_kg"] is True


def test_overlay_no_muta_origen_y_un_tramo_real_posterior_gana():
    source = _work()
    target = _work()
    source_assignment = SimpleNamespace(id=uuid4(), trabajo_ot_id=source.id)
    target_assignment = SimpleNamespace(id=uuid4(), trabajo_ot_id=target.id)
    source_plan = SimpleNamespace(id=10)
    target_plan = SimpleNamespace(id=20)
    original_segment = SimpleNamespace(
        id=uuid4(), secuencia=1, trabajo=source,
        asignacion_personal_trabajo=source_assignment,
    )
    correction = SimpleNamespace(
        public_id=uuid4(), destino_trabajo=target,
        destino_asignacion=target_assignment,
        destino_asignacion_plan=target_plan,
        tramo_objetivo=original_segment,
    )
    manga = SimpleNamespace(
        trabajo=source,
        asignacion_personal_trabajo=source_assignment,
        asignacion=source_plan,
        tramos_trabajo=[original_segment],
        correccion_asignacion=correction,
    )

    assert effective_work(manga) is target
    assert effective_assignment(manga) is target_assignment
    assert effective_plan_assignment(manga) is target_plan
    assert effective_segment(manga) is original_segment
    assert manga.trabajo is source
    assert manga.asignacion is source_plan
    assert original_segment.trabajo is source

    later_segment = SimpleNamespace(
        id=uuid4(), secuencia=2, trabajo=source,
        asignacion_personal_trabajo=source_assignment,
        asignacion_plan_id=source_plan.id,
        asignacion_plan=source_plan,
    )
    manga.tramos_trabajo.append(later_segment)
    assert effective_segment(manga) is later_segment
    assert effective_work(manga) is source
    assert effective_plan_assignment(manga) is source_plan
    assert effective_work_for_segment(manga, original_segment) is target
    assert effective_work_for_segment(manga, later_segment) is source
    assert effective_assignment_for_segment(manga, original_segment) is target_assignment
    assert effective_assignment_for_segment(manga, later_segment) is source_assignment


def test_preview_y_aplicacion_rechazan_actor_sin_capacidad(app):
    with app.app_context():
        creator, _approver, manga, _station, _label, _weighed = (
            _auto_final_kg_fixture(app, station_code="PESAJE-REATRIBUCION-SIN-PERMISO")
        )
        unauthorized = Trabajador(
            codigo=f"SIN-CORR-{uuid4().hex[:8]}",
            nombres="Sin",
            apellidos="Permiso",
            activo=True,
        )
        db.session.add(unauthorized)
        db.session.commit()
        payload = {
            "destino_trabajo_ot_id": str(uuid4()),
            "destino_asignacion_id": str(uuid4()),
            "version": manga.version,
            "motivo": "Intento sin capacidad explicita.",
        }

        with pytest.raises(ScmServiceError) as preview_error:
            preview_assignment_correction(
                db.session,
                actor_id=unauthorized.id,
                manga_id=manga.public_id,
                data={"destino_trabajo_ot_id": payload["destino_trabajo_ot_id"]},
            )
        assert preview_error.value.status_code == 403
        assert preview_error.value.code == "CAPABILITY_REQUIRED"

        with pytest.raises(ScmServiceError) as apply_error:
            apply_assignment_correction(
                db.session,
                actor_id=unauthorized.id,
                manga_id=manga.public_id,
                operation_id=uuid4(),
                data=payload,
            )
        assert apply_error.value.status_code == 403
        assert apply_error.value.code == "CAPABILITY_REQUIRED"
        assert ScmCorreccionAsignacionManga.query.count() == 0


def test_preview_rechaza_manga_un_fuera_del_piloto_kg(app):
    with app.app_context():
        creator, _approver, manga, _station, _label, _weighed = (
            _auto_final_kg_fixture(app, station_code="PESAJE-REATRIBUCION-UN")
        )
        _grant_assignment_correction(creator)
        manga.lote_articulo.articulo.unidad_inventario = "UN"
        db.session.commit()

        with pytest.raises(ScmServiceError) as error:
            preview_assignment_correction(
                db.session,
                actor_id=creator.id,
                manga_id=manga.public_id,
                data={"destino_trabajo_ot_id": str(uuid4())},
            )

        assert error.value.code == "KG_MANGA_REQUIRED"
        assert error.value.status_code == 409
def test_etiqueta_de_control_usa_trabajo_vigente_del_tramo_corregido():
    source = _work()
    target = _work()
    source.orden_operacion.codigo = "OF-1"
    target.orden_operacion.codigo = "OF-1"
    source.orden_trabajo.public_id = uuid4()
    source.orden_trabajo.fecha = date(2026, 9, 19)
    target.orden_trabajo.fecha = date(2026, 9, 19)
    target.orden_trabajo.public_id = uuid4()
    target.orden_trabajo.codigo_ot = "OT-B"
    source_assignment = SimpleNamespace(id=uuid4(), trabajo_ot_id=source.id)
    target_assignment = SimpleNamespace(
        id=uuid4(), trabajo_ot_id=target.id,
        trabajador=SimpleNamespace(nombre_completo="Operario B"),
    )
    segment = SimpleNamespace(
        id=uuid4(), secuencia=1, trabajo=source,
        asignacion_personal_trabajo=source_assignment,
    )
    correction = SimpleNamespace(
        tramo_objetivo_id=segment.id,
        tramo_objetivo=segment,
        destino_trabajo=target,
        destino_asignacion=target_assignment,
    )
    manga = SimpleNamespace(
        trabajo=source,
        ot=source.orden_trabajo,
        public_id=uuid4(),
        codigo="M-01",
        pieza_color_sku_snapshot="P-01",
        articulo_nombre_snapshot="Pieza",
        color_snapshot="Azul",
        tipo="NORMAL",
        peso_unitario_snapshot_g=Decimal("10"),
        correccion_asignacion=correction,
    )
    control = SimpleNamespace(
        tramo=segment,
        tipo="AVANCE_KG",
        conteo_acumulado_un=None,
        peso_neto_kg=Decimal("8"),
        aporte_desde_control_anterior_kg=Decimal("3"),
    )

    payload = scm_weighing_service._control_label_payload(
        manga, control, uuid4(), 1,
    )

    assert payload["trabajo_color_id"] == str(target.id)
    assert payload["trabajo_color_codigo"] == target.codigo
    assert payload["of_ot"] == "OF-1 - OT-B"


def test_correccion_aplicada_conserva_ingreso_y_saldo_kg_y_replay_no_duplica(app):
    with app.app_context():
        creator, _approver, manga, _station, _label, _weighed = (
            _auto_final_kg_fixture(app, station_code="PESAJE-REATRIBUCION")
        )
        _grant_assignment_correction(creator)
        source = manga.trabajo
        source_color = source.trabajo_color
        target_header = create_fabrication_ot_header(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            data={
                "maquina_id": source.orden_trabajo.maquina_id,
                "fecha_operativa": "2026-09-19",
                "turno": "DIA",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        target_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(target_header["public_id"])
        ).one()
        target = ScmTrabajoOt(
            orden_trabajo_id=target_ot.id,
            codigo=f"{target_ot.codigo_ot}-C01",
            tipo="COLOR",
            secuencia=1,
            estado="PLANIFICADO",
            orden_operacion_id=source.orden_operacion_id,
            cantidad_objetivo_un=0,
            cantidad_confirmada_un=0,
            created_by_id=creator.id,
        )
        target.trabajo_color = ScmTrabajoColor(
            corrida_fabricacion_id=source_color.corrida_fabricacion_id,
            molde_codigo_snapshot=source_color.molde_codigo_snapshot,
            color_id_snapshot=source_color.color_id_snapshot,
            color_nombre_snapshot=source_color.color_nombre_snapshot,
            receta_revision_id_snapshot=source_color.receta_revision_id_snapshot,
            receta_hash_snapshot=source_color.receta_hash_snapshot,
            cavidades_snapshot=source_color.cavidades_snapshot,
            peso_neto_snapshot_g=source_color.peso_neto_snapshot_g,
            peso_colada_snapshot_g=source_color.peso_colada_snapshot_g,
        )
        assignment = ScmAsignacionPersonalTrabajoOt(
            trabajo=target,
            trabajador_id=creator.id,
            estado="PREVISTA",
            asignada_por_id=creator.id,
        )
        db.session.add_all([target, assignment])
        db.session.flush()
        target_plan = ScmAsignacionPlanMangaOt(
            plan_linea_id=manga.plan_linea_id,
            ot_id=target_ot.id,
            trabajo_ot_id=target.id,
            cantidad_asignada_un=0,
            mangas_asignadas=0,
            asignada_por_id=creator.id,
        )
        db.session.add(target_plan)
        db.session.commit()
        source_plan = manga.asignacion
        moved_quantity = Decimal(manga.cantidad_asignada_un)
        source_plan_before = (
            Decimal(source_plan.cantidad_asignada_un), source_plan.mangas_asignadas,
        )
        work_total_before = (
            Decimal(source.cantidad_objetivo_un) + Decimal(target.cantidad_objetivo_un)
        )

        movement_ids_before = {
            str(item.id) for item in ScmMovimientoInventarioKg.query.all()
        }
        balances_before = {
            str(item.id): (item.cantidad_fisica_kg, item.cantidad_reservada_kg)
            for item in ScmSaldoInventarioKg.query.all()
        }
        attributions_before = {
            str(item.public_id): (item.trabajo_ot_id, item.base_json)
            for item in ScmAtribucionProduccionKg.query.filter_by(manga_id=manga.id).all()
        }
        operation_id = uuid4()
        payload = {
            "destino_trabajo_ot_id": str(target.id),
            "destino_asignacion_id": str(assignment.id),
            "version": manga.version,
            "motivo": "Se selecciono la OT anterior por error al iniciar el turno.",
        }

        preview = preview_assignment_correction(
            db.session,
            actor_id=creator.id,
            manga_id=manga.public_id,
            data={
                "destino_trabajo_ot_id": str(target.id),
                "destino_asignacion_id": str(assignment.id),
            },
        )
        assert preview["puede_aplicar"], (
            preview["bloqueos"],
            [(item.estado, item.secuencia) for item in manga.tramos_trabajo],
        )
        assert preview["kg_referencia"] == "12.000"
        assert preview["kg_fuente"] == "PESAJE_FINAL"

        applied = apply_assignment_correction(
            db.session,
            actor_id=creator.id,
            manga_id=manga.public_id,
            operation_id=operation_id,
            data=payload,
        )
        replay = apply_assignment_correction(
            db.session,
            actor_id=creator.id,
            manga_id=manga.public_id,
            operation_id=operation_id,
            data=payload,
        )

        db.session.refresh(manga)
        db.session.refresh(source_plan)
        db.session.refresh(target_plan)
        db.session.refresh(source)
        db.session.refresh(target)
        # The correction is an append-only overlay: physical identity,
        # source OT and historical segment remain untouched.
        assert manga.trabajo_ot_id == source.id
        assert manga.ot_id != target_ot.id
        assert manga.asignacion_id == source_plan.id
        assert all(item.asignacion_plan_id != target_plan.id for item in manga.tramos_trabajo)
        assert applied["manga"]["trabajo_color_id"] == str(target.id)
        assert manga.correccion_asignacion.destino_trabajo_ot_id == target.id
        assert manga.version == payload["version"] + 1
        assert applied["correccion"]["manga_version_despues"] == payload["version"] + 1
        transfer = applied["correccion"]["evidencia"]["transferencia"]
        assert transfer["cantidad_un"] == str(moved_quantity)
        assert transfer["total_objetivo_un_antes"] == transfer["total_objetivo_un_despues"]
        assert Decimal(source_plan.cantidad_asignada_un) == source_plan_before[0] - moved_quantity
        assert source_plan.mangas_asignadas == source_plan_before[1] - 1
        assert Decimal(target_plan.cantidad_asignada_un) == moved_quantity
        assert target_plan.mangas_asignadas == 1
        assert Decimal(source.cantidad_objetivo_un) + Decimal(target.cantidad_objetivo_un) == work_total_before
        assert applied["correccion"]["id"] == replay["correccion"]["id"]
        assert ScmCorreccionAsignacionManga.query.count() == 1
        transfer = manga.correccion_asignacion.evidencia_json["transferencia"]
        assert transfer["origen"]["plan"]["antes"]["mangas_asignadas"] == source_plan_before[1]
        assert transfer["origen"]["plan"]["despues"]["mangas_asignadas"] == source_plan.mangas_asignadas
        assert Decimal(
            transfer["destino"]["trabajo"]["antes"]["cantidad_objetivo_un"]
        ) == Decimal("0")
        assert Decimal(transfer["total_objetivo_un_antes"]) == Decimal(
            transfer["total_objetivo_un_despues"]
        )
        second_preview = preview_assignment_correction(
            db.session,
            actor_id=creator.id,
            manga_id=manga.public_id,
            data={
                "destino_trabajo_ot_id": str(target.id),
                "destino_asignacion_id": str(assignment.id),
            },
        )
        assert second_preview["puede_aplicar"] is False
        assert any(
            item["code"] == "MANGA_ALREADY_CORRECTED"
            and item["correccion_id"] == applied["correccion"]["id"]
            for item in second_preview["bloqueos"]
        )
        assert {str(item.id) for item in ScmMovimientoInventarioKg.query.all()} == movement_ids_before
        assert {
            str(item.id): (item.cantidad_fisica_kg, item.cantidad_reservada_kg)
            for item in ScmSaldoInventarioKg.query.all()
        } == balances_before
        assert {
            str(item.public_id): (item.trabajo_ot_id, item.base_json)
            for item in ScmAtribucionProduccionKg.query.filter_by(manga_id=manga.id).all()
        } == attributions_before

        # A legacy final weighing can have no physical segment.  Its measured
        # kilograms must close on the effective destination OT after correction.
        target.estado = "COMPLETADO"
        target.version += 1
        target_ot.estado = "EN_EJECUCION"
        db.session.commit()
        target_close = close_productive_document_kg(
            db.session,
            actor_id=creator.id,
            documento_tipo="OT",
            documento_id=target_ot.public_id,
            operation_id=uuid4(),
            data={"version": target_ot.version},
        )
        assert target_close["cierre"]["kg_medido"] == "12.000"


def test_final_legacy_corregido_reabre_y_conserva_trabajo_asignacion_y_plan_destino(app):
    with app.app_context():
        creator, _approver, manga, station, label, _weighed = (
            _auto_final_kg_fixture(app, station_code="PESAJE-REATRIBUCION-REAPERTURA")
        )
        _grant_assignment_correction(creator)
        _grant_capabilities(creator, ("MANGA_REABRIR",))
        source = manga.trabajo
        source_color = source.trabajo_color
        target_header = create_fabrication_ot_header(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            data={
                "maquina_id": source.orden_trabajo.maquina_id,
                "fecha_operativa": "2026-09-19",
                "turno": "DIA",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        target_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(target_header["public_id"])
        ).one()
        target_ot.estado = "EN_EJECUCION"
        target = ScmTrabajoOt(
            orden_trabajo_id=target_ot.id,
            codigo=f"{target_ot.codigo_ot}-C01",
            tipo="COLOR",
            secuencia=1,
            estado="PAUSADO",
            orden_operacion_id=source.orden_operacion_id,
            cantidad_objetivo_un=0,
            cantidad_confirmada_un=0,
            created_by_id=creator.id,
        )
        target.trabajo_color = ScmTrabajoColor(
            corrida_fabricacion_id=source_color.corrida_fabricacion_id,
            molde_codigo_snapshot=source_color.molde_codigo_snapshot,
            color_id_snapshot=source_color.color_id_snapshot,
            color_nombre_snapshot=source_color.color_nombre_snapshot,
            receta_revision_id_snapshot=source_color.receta_revision_id_snapshot,
            receta_hash_snapshot=source_color.receta_hash_snapshot,
            cavidades_snapshot=source_color.cavidades_snapshot,
            peso_neto_snapshot_g=source_color.peso_neto_snapshot_g,
            peso_colada_snapshot_g=source_color.peso_colada_snapshot_g,
        )
        assignment = ScmAsignacionPersonalTrabajoOt(
            trabajo=target,
            trabajador_id=creator.id,
            estado="PREVISTA",
            asignada_por_id=creator.id,
        )
        db.session.add_all([target, assignment])
        db.session.flush()
        target_plan = ScmAsignacionPlanMangaOt(
            plan_linea_id=manga.plan_linea_id,
            ot_id=target_ot.id,
            trabajo_ot_id=target.id,
            cantidad_asignada_un=0,
            mangas_asignadas=0,
            asignada_por_id=creator.id,
        )
        db.session.add(target_plan)
        db.session.commit()

        apply_assignment_correction(
            db.session,
            actor_id=creator.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={
                "destino_trabajo_ot_id": str(target.id),
                "destino_asignacion_id": str(assignment.id),
                "version": manga.version,
                "motivo": "OT de color elegida por error antes del primer pesaje.",
            },
        )
        reopened = reopen_manga_after_accidental_close(
            db.session,
            actor_id=creator.id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={
                "version": manga.version,
                "motivo": "Continuar llenado tras corregir la OT.",
                "tipo_reapertura": "CONTINUAR_LLENADO",
            },
        )
        assert reopened["manga"]["estado"] == "EN_LLENADO"
        db.session.refresh(manga)
        segment = manga.tramos_trabajo[-1]
        assert segment.trabajo_ot_id == target.id
        assert segment.asignacion_personal_trabajo_id == assignment.id
        assert segment.asignacion_plan_id == target_plan.id

        control = register_manga_weighing_control(
            db.session,
            station_id=station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "14.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-19T11:00:00-05:00",
                "reading_stable": True,
                "control_type": "AVANCE_KG",
            },
        )
        final = confirm_manga_weighing(
            db.session,
            station_id=station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "16.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-19T11:10:00-05:00",
                "reading_stable": True,
            },
        )
        assert control["control"]["tramo_id"] == str(segment.id)
        assert final["weighing"]["trabajo_color_id"] == str(target.id)
        assert manga.tramos_trabajo[-1].asignacion_plan_id == target_plan.id


@pytest.mark.postgres
def test_postgres_dos_correcciones_concurrentes_conservan_totales_del_plan(
    postgres_inline_app,
):
    app = postgres_inline_app
    with app.app_context():
        app.config["KG_AUTOMATIC_INTAKE_ENABLED"] = True
        app.config["KG_PRODUCTION_LOCATION_CODE"] = "PILOT-CORRECTION-PG"
        if Linea.query.first() is None:
            db.session.add(Linea(codigo=990001, nombre="Linea KG correccion"))
        if Familia.query.first() is None:
            db.session.add(Familia(codigo=990001, nombre="Familia KG correccion"))
        db.session.add(ScmUbicacionInventario(
            codigo="PILOT-CORRECTION-PG",
            nombre="Produccion correccion PostgreSQL",
            clases_articulo_json=["PIEZA_COLOR"],
            activo=True,
            tipo="PUNTO_PRODUCCION",
            permite_saldo_libre=True,
        ))
        db.session.commit()
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=400)
        )
        assert len(created["mangas"]) >= 4
        _grant_assignment_correction(creator)
        source = ScmTrabajoOt.query.filter_by(id=UUID(created["trabajo_color"]["id"])).one()
        source_color = source.trabajo_color
        transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=source.id,
            operation_id=uuid4(),
            data={"version": source.version},
            action="iniciar",
        )
        mangas = [
            ScmManga.query.filter_by(public_id=UUID(item["public_id"])).one()
            for item in created["mangas"][:4]
        ]
        transferred_quantity = sum(
            (Decimal(manga.cantidad_asignada_un) for manga in mangas[:2]),
            Decimal("0.000"),
        )
        weighing_contexts = []
        for index, manga in enumerate(mangas, 1):
            manga.lote_articulo.articulo.unidad_inventario = "KG"
            station, label = _print_color_manga(
                actor=creator,
                manga_id=manga.public_id,
                station_code=f"PESAJE-REATRIBUCION-PG-{index}",
            )
            weighing_contexts.append((station.station_id, label["public_id"]))
            register_manga_weighing_control(
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
                    "pesada_at": f"2026-09-19T10:0{index}:00-05:00",
                    "reading_stable": True,
                    "control_type": "AVANCE_KG",
                },
            )

        target_header = create_fabrication_ot_header(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            data={
                "maquina_id": source.orden_trabajo.maquina_id,
                "fecha_operativa": "2026-09-20",
                "turno": "DIA",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        target_ot = RegistroDiarioProduccion.query.filter_by(
            public_id=UUID(target_header["public_id"])
        ).one()
        target = ScmTrabajoOt(
            orden_trabajo_id=target_ot.id,
            codigo=f"{target_ot.codigo_ot}-C01",
            tipo="COLOR",
            secuencia=1,
            estado="PAUSADO",
            orden_operacion_id=source.orden_operacion_id,
            cantidad_objetivo_un=0,
            cantidad_confirmada_un=0,
            created_by_id=creator.id,
        )
        target.trabajo_color = ScmTrabajoColor(
            corrida_fabricacion_id=source_color.corrida_fabricacion_id,
            molde_codigo_snapshot=source_color.molde_codigo_snapshot,
            color_id_snapshot=source_color.color_id_snapshot,
            color_nombre_snapshot=source_color.color_nombre_snapshot,
            receta_revision_id_snapshot=source_color.receta_revision_id_snapshot,
            receta_hash_snapshot=source_color.receta_hash_snapshot,
            cavidades_snapshot=source_color.cavidades_snapshot,
            peso_neto_snapshot_g=source_color.peso_neto_snapshot_g,
            peso_colada_snapshot_g=source_color.peso_colada_snapshot_g,
        )
        assignment = ScmAsignacionPersonalTrabajoOt(
            trabajo=target,
            trabajador_id=creator.id,
            estado="PREVISTA",
            asignada_por_id=creator.id,
        )
        db.session.add_all([target, assignment])
        db.session.flush()
        target_plan = ScmAsignacionPlanMangaOt(
            plan_linea_id=mangas[0].plan_linea_id,
            ot_id=target_ot.id,
            trabajo_ot_id=target.id,
            cantidad_asignada_un=0,
            mangas_asignadas=0,
            asignada_por_id=creator.id,
        )
        db.session.add(target_plan)
        alternate = ScmTrabajoOt(
            orden_trabajo_id=target_ot.id,
            codigo=f"{target_ot.codigo_ot}-C02",
            tipo="COLOR",
            secuencia=2,
            estado="PAUSADO",
            orden_operacion_id=source.orden_operacion_id,
            cantidad_objetivo_un=0,
            cantidad_confirmada_un=0,
            created_by_id=creator.id,
        )
        alternate.trabajo_color = ScmTrabajoColor(
            corrida_fabricacion_id=source_color.corrida_fabricacion_id,
            molde_codigo_snapshot=source_color.molde_codigo_snapshot,
            color_id_snapshot=source_color.color_id_snapshot,
            color_nombre_snapshot=source_color.color_nombre_snapshot,
            receta_revision_id_snapshot=source_color.receta_revision_id_snapshot,
            receta_hash_snapshot=source_color.receta_hash_snapshot,
            cavidades_snapshot=source_color.cavidades_snapshot,
            peso_neto_snapshot_g=source_color.peso_neto_snapshot_g,
            peso_colada_snapshot_g=source_color.peso_colada_snapshot_g,
        )
        alternate_assignment = ScmAsignacionPersonalTrabajoOt(
            trabajo=alternate,
            trabajador_id=creator.id,
            estado="PREVISTA",
            asignada_por_id=creator.id,
        )
        db.session.add_all([alternate, alternate_assignment])
        db.session.flush()
        alternate_plan = ScmAsignacionPlanMangaOt(
            plan_linea_id=mangas[0].plan_linea_id,
            ot_id=target_ot.id,
            trabajo_ot_id=alternate.id,
            cantidad_asignada_un=0,
            mangas_asignadas=0,
            asignada_por_id=creator.id,
        )
        db.session.add(alternate_plan)
        mangas[0].plan_linea.cantidad_objetivo_un = Decimal("500.000")
        db.session.commit()
        actor_id = creator.id
        source_id = source.id
        plan_line_id = mangas[0].plan_linea_id
        target_id = target.id
        assignment_id = assignment.id
        target_plan_id = target_plan.id
        manga_inputs = [(manga.public_id, manga.version) for manga in mangas[:2]]
        contested_input = (mangas[2].public_id, mangas[2].version)
        add_race_input = (mangas[3].public_id, mangas[3].version)
        source_ot_public_id = source.orden_trabajo.public_id
        alternate_id = alternate.id
        alternate_assignment_id = alternate_assignment.id
        alternate_plan_id = alternate_plan.id

    barrier = Barrier(2)

    def correct(item):
        manga_id, version = item
        with app.app_context():
            try:
                barrier.wait(timeout=30)
                return apply_assignment_correction(
                    db.session,
                    actor_id=actor_id,
                    manga_id=manga_id,
                    operation_id=uuid4(),
                    data={
                        "destino_trabajo_ot_id": str(target_id),
                        "destino_asignacion_id": str(assignment_id),
                        "version": version,
                        "motivo": "Correccion concurrente PostgreSQL del piloto.",
                    },
                )
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(correct, manga_inputs))

    assert len(results) == 2
    with app.app_context():
        source_plan = ScmAsignacionPlanMangaOt.query.filter_by(
            plan_linea_id=plan_line_id,
            trabajo_ot_id=source_id,
        ).one()
        final_target_plan = db.session.get(ScmAsignacionPlanMangaOt, target_plan_id)
        assert Decimal(source_plan.cantidad_asignada_un) == (
            Decimal("400.000") - transferred_quantity
        )
        assert source_plan.mangas_asignadas == len(created["mangas"]) - 2
        assert Decimal(final_target_plan.cantidad_asignada_un) == transferred_quantity
        assert final_target_plan.mangas_asignadas == 2
        first_manga = ScmManga.query.filter_by(public_id=manga_inputs[0][0]).one()
        station_id, label_id = weighing_contexts[0]
        second_control = register_manga_weighing_control(
            db.session,
            station_id=station_id,
            operation_id=uuid4(),
            actor_id=actor_id,
            data={
                "label_id": label_id,
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "8.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-20T10:10:00-05:00",
                "reading_stable": True,
                "control_type": "AVANCE_KG",
            },
        )
        assert second_control["inventario_kg"]["delta_kg"] == "3.000"
        final = confirm_manga_weighing(
            db.session,
            station_id=station_id,
            operation_id=uuid4(),
            actor_id=actor_id,
            data={
                "label_id": label_id,
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "12.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-20T10:20:00-05:00",
                "reading_stable": True,
            },
        )
        assert final["weighing"]["trabajo_color_id"] == str(target_id)
        existence = ScmExistenciaMangaKg.query.filter_by(manga_id=first_manga.id).one()
        movements = ScmMovimientoInventarioKg.query.filter_by(
            saldo_id=existence.saldo_id
        ).all()
        assert sorted(Decimal(item.cantidad_delta_kg) for item in movements) == [
            Decimal("3.000"), Decimal("4.000"), Decimal("5.000"),
            Decimal("5.000"), Decimal("5.000"), Decimal("5.000"),
        ]

    # Two destination Trabajos competing for the same manga must yield one
    # immutable correction and one deterministic conflict.
    same_manga_barrier = Barrier(2)

    def correct_same_manga(target_tuple):
        destination_id, destination_assignment_id = target_tuple
        with app.app_context():
            try:
                same_manga_barrier.wait(timeout=30)
                try:
                    return ("ok", apply_assignment_correction(
                        db.session,
                        actor_id=actor_id,
                        manga_id=contested_input[0],
                        operation_id=uuid4(),
                        data={
                            "destino_trabajo_ot_id": str(destination_id),
                            "destino_asignacion_id": str(destination_assignment_id),
                            "version": contested_input[1],
                            "motivo": "Dos destinos concurrentes sobre la misma manga.",
                        },
                    ))
                except ScmServiceError as error:
                    return ("error", error.code)
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        contested_results = list(executor.map(
            correct_same_manga,
            ((target_id, assignment_id), (alternate_id, alternate_assignment_id)),
        ))

    assert [status for status, _payload in contested_results].count("ok") == 1
    assert [status for status, _payload in contested_results].count("error") == 1
    assert contested_results[0][1] != contested_results[1][1]
    with app.app_context():
        contested_manga = ScmManga.query.filter_by(public_id=contested_input[0]).one()
        assert ScmCorreccionAsignacionManga.query.filter_by(
            manga_id=contested_manga.id
        ).count() == 1
        final_source_plan = ScmAsignacionPlanMangaOt.query.filter_by(
            plan_linea_id=plan_line_id,
            trabajo_ot_id=source_id,
        ).one()
        final_target_plan = db.session.get(ScmAsignacionPlanMangaOt, target_plan_id)
        final_alternate_plan = db.session.get(
            ScmAsignacionPlanMangaOt, alternate_plan_id
        )
        assert (
            Decimal(final_source_plan.cantidad_asignada_un)
            + Decimal(final_target_plan.cantidad_asignada_un)
            + Decimal(final_alternate_plan.cantidad_asignada_un)
        ) == Decimal("400.000")

    add_barrier = Barrier(2)

    def correct_fourth_manga():
        with app.app_context():
            try:
                add_barrier.wait(timeout=30)
                return apply_assignment_correction(
                    db.session,
                    actor_id=actor_id,
                    manga_id=add_race_input[0],
                    operation_id=uuid4(),
                    data={
                        "destino_trabajo_ot_id": str(target_id),
                        "destino_asignacion_id": str(assignment_id),
                        "version": add_race_input[1],
                        "motivo": "Correccion concurrente con alta de manga.",
                    },
                )
            finally:
                db.session.remove()

    def add_one_manga():
        with app.app_context():
            try:
                add_barrier.wait(timeout=30)
                return add_normal_mangas(
                    db.session,
                    actor_id=actor_id,
                    public_id=source_ot_public_id,
                    operation_id=uuid4(),
                    data={
                        "plan_linea_id": plan_line_id,
                        "trabajo_color_id": str(source_id),
                        "cantidad_un": "100",
                    },
                )
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_correction = executor.submit(correct_fourth_manga)
        future_addition = executor.submit(add_one_manga)
        future_correction.result(timeout=60)
        future_addition.result(timeout=60)

    with app.app_context():
        final_source_plan = ScmAsignacionPlanMangaOt.query.filter_by(
            plan_linea_id=plan_line_id,
            trabajo_ot_id=source_id,
        ).one()
        final_target_plan = db.session.get(ScmAsignacionPlanMangaOt, target_plan_id)
        final_alternate_plan = db.session.get(
            ScmAsignacionPlanMangaOt, alternate_plan_id
        )
        assert (
            Decimal(final_source_plan.cantidad_asignada_un)
            + Decimal(final_target_plan.cantidad_asignada_un)
            + Decimal(final_alternate_plan.cantidad_asignada_un)
        ) == Decimal("500.000")
        assert final_source_plan.mangas_asignadas == 1
