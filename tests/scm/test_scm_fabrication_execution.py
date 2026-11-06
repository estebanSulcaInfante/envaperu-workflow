from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app import db
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_catalogos import ScmCategoriaRecepcion, ScmMaterial
from app.models.scm_inline_wip import ScmReservaWipSalida, ScmSaldoWipSalida
from app.models.scm_inventory import ScmSaldoMaterialInventario, ScmUbicacionInventario
from app.models.scm_material_execution import ScmRequerimientoMaterial, ScmReservaMaterial
from app.models.scm_ot import (
    ScmControlPesoManga,
    ScmManga,
    ScmPesajeManga,
    ScmTramoMangaTrabajo,
    ScmTrabajoOt,
)
from app.models.scm_material_execution import ScmLotePremezcla
from app.models.scm_prepared_material import (
    ScmAsignacionRequerimientoPreparacion,
    ScmOrdenPreparacionMaterial,
    ScmRequerimientoMaterialPreparado,
)
from app.models.scm_production_orders import ScmOrdenOperacion
from app.models.receta_color import RecetaColorMaestra
from app.services.scm_fabrication_execution_service import _impact
from app.services.scm_ot_service import transition_color_work
from app.services.scm_weighing_service import (
    confirm_manga_weighing,
    register_manga_weighing_control,
)
from tests.scm.test_scm_fabrication_replacement import _seed_released_order
from tests.scm.test_scm_ot_service import _print_color_manga, _seed_aggregate_color_work


def _seed_active_execution(app):
    order_id, actor_id, run_id, _recipe_id = _seed_released_order(app)
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        order.estado = "EN_EJECUCION"
        order.fabricacion.corridas[0].estado = "EN_EJECUCION"
        machine = Maquina.query.first()
        ot = RegistroDiarioProduccion(
            codigo_ot=f"OT-EXEC-{uuid4().hex[:8]}", codigo_ot_sintetico=False,
            estado="EN_EJECUCION", tipo_ot="FABRICACION", maquina_id=machine.id,
            fecha=date(2026, 10, 22), turno="DIURNO", secuencia_siguiente_manga=1,
            secuencia_siguiente_trabajo=2,
        )
        work = ScmTrabajoOt(
            orden_trabajo=ot, codigo=f"{ot.codigo_ot}-T01", secuencia=1,
            estado="EN_EJECUCION", orden_operacion_id=order.id,
            cantidad_objetivo_un=100,
        )
        db.session.add_all([ot, work])
        db.session.commit()
    return str(order_id), actor_id


def test_pause_resume_is_atomic_and_replay_safe(app, client, scm_config):
    order_id, actor_id = _seed_active_execution(app)
    impact = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=PAUSAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert impact.status_code == 200, impact.json
    payload = impact.get_json()
    assert payload["can_execute"] is True
    key = str(uuid4())
    paused = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/pausar-ejecucion",
        json={"version": payload["version"], "impact_hash": payload["impact_hash"], "motivo": "Cambio de prioridad"},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": key},
    )
    assert paused.status_code == 200, paused.json
    assert paused.get_json()["estado"] == "PAUSADA"
    replay = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/pausar-ejecucion",
        json={"version": payload["version"], "impact_hash": payload["impact_hash"], "motivo": "Cambio de prioridad"},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": key},
    )
    assert replay.status_code == 200
    with app.app_context():
        assert ScmEvento.query.filter_by(aggregate_id=order_id, tipo="OF_PAUSAR_EJECUCION").count() == 1


def test_pause_without_own_executable_work_is_a_readable_blocker(app, client, scm_config):
    order_id, actor_id, _run_id, _recipe_id = _seed_released_order(app)
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        order.estado = "EN_EJECUCION"
        db.session.commit()
    response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=PAUSAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["can_execute"] is False
    assert any(item["code"] == "NO_EXECUTABLE_WORKS" for item in body["blockers"])


@pytest.mark.parametrize("work_state", ["PAUSADO", "ANULADO", "COMPLETADO"])
def test_pause_accepts_historical_work_without_executable_work(
    app, client, scm_config, work_state,
):
    order_id, actor_id = _seed_active_execution(app)
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        work = ScmTrabajoOt.query.filter_by(orden_operacion_id=order.id).one()
        work.estado = work_state
        work.estado_previo_pausa = None
        db.session.commit()

    impact = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=PAUSAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert impact.status_code == 200, impact.get_json()
    body = impact.get_json()
    assert body["can_execute"] is True
    assert body["trabajos"]["ejecutables"] == 0
    assert body["trabajos"]["afectados"] == 0

    paused = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/pausar-ejecucion",
        json={
            "version": body["version"],
            "impact_hash": body["impact_hash"],
            "motivo": "Suspender OF con historial",
        },
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert paused.status_code == 200, paused.get_json()
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        assert order.estado == "PAUSADA"
        assert order.fabricacion.corridas[0].estado == "PAUSADA"
        work = ScmTrabajoOt.query.filter_by(orden_operacion_id=order.id).one()
        assert work.estado == work_state
        assert work.estado_previo_pausa is None


def test_pause_mixed_work_history_only_suspends_executable_work(app, client, scm_config):
    order_id, actor_id = _seed_active_execution(app)
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        active = ScmTrabajoOt.query.filter_by(orden_operacion_id=order.id).one()
        active_code = active.codigo
        paused = ScmTrabajoOt(
            orden_trabajo_id=active.orden_trabajo_id,
            codigo=f"{active.codigo}-PAUSED",
            secuencia=2,
            estado="PAUSADO",
            orden_operacion_id=order.id,
            cantidad_objetivo_un=20,
            estado_previo_pausa=None,
        )
        annulled = ScmTrabajoOt(
            orden_trabajo_id=active.orden_trabajo_id,
            codigo=f"{active.codigo}-ANNULLED",
            secuencia=3,
            estado="ANULADO",
            orden_operacion_id=order.id,
            cantidad_objetivo_un=20,
        )
        db.session.add_all([paused, annulled])
        db.session.commit()

    impact = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=PAUSAR",
        headers={"X-Actor-Id": str(actor_id)},
    ).get_json()
    assert impact["can_execute"] is True
    paused_response = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/pausar-ejecucion",
        json={
            "version": impact["version"],
            "impact_hash": impact["impact_hash"],
            "motivo": "Suspender trabajo activo",
        },
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert paused_response.status_code == 200, paused_response.get_json()
    with app.app_context():
        states = {
            work.codigo: (work.estado, work.estado_previo_pausa)
            for work in ScmTrabajoOt.query.filter_by(orden_operacion_id=UUID(order_id)).all()
        }
        assert states[active_code] == ("PAUSADO", "EN_EJECUCION")
        assert states[f"{active_code}-PAUSED"] == ("PAUSADO", None)
        assert states[f"{active_code}-ANNULLED"] == ("ANULADO", None)


def test_resume_accepts_paused_order_without_aggregate_markers(app, client, scm_config):
    order_id, actor_id = _seed_active_execution(app)
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        order.estado = "PAUSADA"
        order.fabricacion.corridas[0].estado = "PAUSADA"
        work = ScmTrabajoOt.query.filter_by(orden_operacion_id=order.id).one()
        work.estado = "PAUSADO"
        work.estado_previo_pausa = None
        db.session.commit()

    impact_response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=REANUDAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert impact_response.status_code == 200, impact_response.get_json()
    impact = impact_response.get_json()
    assert impact["can_execute"] is True

    resumed = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/reanudar-ejecucion",
        json={
            "version": impact["version"],
            "impact_hash": impact["impact_hash"],
            "motivo": "Retomar OF sin reactivar pausa individual",
        },
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert resumed.status_code == 200, resumed.get_json()
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        assert order.estado == "EN_EJECUCION"
        assert order.fabricacion.corridas[0].estado == "EN_EJECUCION"
        work = ScmTrabajoOt.query.filter_by(orden_operacion_id=order.id).one()
        assert work.estado == "PAUSADO"
        assert work.estado_previo_pausa is None


def _seed_cancel_case(app, order_state, work_state=None):
    if work_state is None:
        order_id, actor_id, _run_id, _recipe_id = _seed_released_order(app)
    else:
        order_id, actor_id = _seed_active_execution(app)
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        order.estado = order_state
        order.fabricacion.corridas[0].estado = (
            "EN_EJECUCION" if order_state in {"EN_EJECUCION", "PAUSADA"} else "LIBERADA"
        )
        if work_state is not None:
            work = ScmTrabajoOt.query.filter_by(orden_operacion_id=order.id).one()
            work.estado = work_state
        db.session.commit()
    return order_id, actor_id


@pytest.mark.parametrize(
    ("order_state", "work_state"),
    [
        ("LIBERADA", None),
        ("PROGRAMADA", "ANULADO"),
        ("EN_EJECUCION", "ANULADO"),
        ("PAUSADA", None),
        ("PAUSADA", "ANULADO"),
    ],
)
def test_cancel_empty_or_all_annulled_work_is_available_for_authorized_states(
    app, client, scm_config, order_state, work_state,
):
    order_id, actor_id = _seed_cancel_case(app, order_state, work_state)
    impact_response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=CANCELAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert impact_response.status_code == 200, impact_response.get_json()
    impact = impact_response.get_json()
    assert impact["can_execute"] is True
    assert impact["trabajos"]["afectados"] == 0

    key = str(uuid4())
    cancelled = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/cancelar-ejecucion",
        json={"version": impact["version"], "impact_hash": impact["impact_hash"], "motivo": "OF duplicada"},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": key},
    )
    assert cancelled.status_code == 200, cancelled.get_json()
    assert cancelled.get_json()["estado"] == "CANCELADA"
    replay = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/cancelar-ejecucion",
        json={"version": impact["version"], "impact_hash": impact["impact_hash"], "motivo": "OF duplicada"},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": key},
    )
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["estado"] == "CANCELADA"
    with app.app_context():
        assert ScmEvento.query.filter_by(aggregate_id=order_id, tipo="OF_CANCELAR_EJECUCION").count() == 1


@pytest.mark.parametrize(
    ("order_state", "work_state"),
    [("PROGRAMADA", "PLANIFICADO"), ("EN_EJECUCION", "COMPLETADO")],
)
def test_cancel_without_pending_work_keeps_preexecution_and_completed_guards(
    app, client, scm_config, order_state, work_state,
):
    order_id, actor_id = _seed_cancel_case(app, order_state, work_state)
    response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=CANCELAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["can_execute"] is False
    assert any(item["code"] == "NO_PENDING_WORKS" for item in body["blockers"])


@pytest.mark.parametrize("order_state", ["BORRADOR", "CERRADA", "CANCELADA", "ANULADA"])
def test_cancel_empty_keeps_terminal_and_draft_states_blocked(app, client, scm_config, order_state):
    order_id, actor_id = _seed_cancel_case(app, order_state)
    response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=CANCELAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["can_execute"] is False
    assert any(item["code"] == "OF_NOT_CANCELLABLE" for item in body["blockers"])


def test_cancel_empty_blocks_live_prepared_requirement_for_own_run(app, client, scm_config):
    order_id, actor_id, run_id, recipe_id = _seed_released_order(app)
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        requirement = ScmRequerimientoMaterialPreparado(
            corrida_fabricacion_id=UUID(run_id),
            receta_revision_id=recipe_id,
            cantidad_requerida_kg=Decimal("1.000"),
            composicion_hash="a" * 64,
            composicion_snapshot_json={},
            estado="CUBIERTA",
            created_by_id=actor_id,
            operation_id=uuid4(),
        )
        db.session.add(requirement)
        opm = ScmOrdenPreparacionMaterial(
            codigo=f"OPM-CANCEL-{uuid4().hex[:8]}", receta_revision_id=recipe_id,
            composicion_hash="a" * 64, cantidad_objetivo_kg=Decimal("1.000"),
            estado="LIBERADA", motivo="preparacion viva", created_by_id=actor_id,
            operation_id=uuid4(),
        )
        db.session.add(opm)
        db.session.flush()
        db.session.add(ScmAsignacionRequerimientoPreparacion(
            orden=opm, requerimiento=requirement, tipo_fuente="OPM_ESPERADA",
            cantidad_planificada_kg=Decimal("1.000"), cantidad_comprometida_kg=Decimal("1.000"),
            cantidad_consumida_kg=Decimal("0.000"), estado="COMPROMETIDA",
            motivo="preparacion viva", created_by_id=actor_id,
        ))
        db.session.add(ScmLotePremezcla(
            codigo=f"PREM-CANCEL-{uuid4().hex[:8]}", corrida_fabricacion_id=UUID(run_id),
            secuencia=1, cantidad_kg=Decimal("1.000"), genealogia_tipo="EXACTA",
            estado="DISPONIBLE_MAQUINA", ubicacion_codigo="PREPARACION_PRODUCCION",
            motivo="custodia viva", actor_id=actor_id, operation_id=uuid4(),
        ))
        db.session.flush()
        version_before = order.version
        db.session.commit()

    response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=CANCELAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["can_execute"] is False
    assert any(item["code"] == "PENDING_PREPARED_REQUIREMENTS" for item in body["blockers"])
    assert any(item["code"] == "PENDING_PREMIX_LOTS" for item in body["blockers"])
    assert body["requerimientos_material_preparado_pendientes"] == 1
    assert body["lotes_premezcla_pendientes"] == 1
    cancelled = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/cancelar-ejecucion",
        json={"version": body["version"], "impact_hash": body["impact_hash"], "motivo": "OF duplicada"},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert cancelled.status_code == 409, cancelled.get_json()
    assert cancelled.get_json()["error"]["code"] == "PENDING_PREPARED_REQUIREMENTS"
    with app.app_context():
        assert db.session.get(ScmOrdenOperacion, UUID(order_id)).version == version_before
        assert ScmRequerimientoMaterialPreparado.query.one().estado == "CUBIERTA"
        assert ScmAsignacionRequerimientoPreparacion.query.one().estado == "COMPROMETIDA"
        assert ScmLotePremezcla.query.one().estado == "DISPONIBLE_MAQUINA"
        assert ScmEvento.query.filter_by(aggregate_id=order_id).count() == 0


def test_cancel_empty_rejects_stale_impact_when_prepared_requirement_appears(app, client, scm_config):
    order_id, actor_id, run_id, recipe_id = _seed_released_order(app)
    impact_response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=CANCELAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert impact_response.status_code == 200, impact_response.get_json()
    impact = impact_response.get_json()
    assert impact["can_execute"] is True

    with app.app_context():
        db.session.add(ScmRequerimientoMaterialPreparado(
            corrida_fabricacion_id=UUID(run_id),
            receta_revision_id=recipe_id,
            cantidad_requerida_kg=Decimal("1.000"),
            composicion_hash="b" * 64,
            composicion_snapshot_json={},
            estado="CUBIERTA",
            created_by_id=actor_id,
            operation_id=uuid4(),
        ))
        db.session.commit()
        version_before = db.session.get(ScmOrdenOperacion, UUID(order_id)).version

    stale = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/cancelar-ejecucion",
        json={"version": impact["version"], "impact_hash": impact["impact_hash"], "motivo": "OF duplicada"},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert stale.status_code == 409, stale.get_json()
    assert stale.get_json()["error"]["code"] == "STALE_IMPACT"
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        assert order.estado == "LIBERADA"
        assert order.version == version_before
        assert ScmEvento.query.filter_by(aggregate_id=order_id, tipo="OF_CANCELAR_EJECUCION").count() == 0


def test_summary_http_accepts_paused_and_cancelled_filters(app, client, scm_config):
    order_id, actor_id, _run_id, _recipe_id = _seed_released_order(app)

    for state in ("PAUSADA", "CANCELADA"):
        with app.app_context():
            order = db.session.get(ScmOrdenOperacion, UUID(order_id))
            order.estado = state
            db.session.commit()

        response = client.get(
            "/api/scm/v1/ordenes-fabricacion",
            query_string={"vista": "resumen", "estado": state},
            headers={"X-Actor-Id": str(actor_id)},
        )

        assert response.status_code == 200, response.get_json()
        payload = response.get_json()
        assert payload["filters"]["estado"] == state
        assert [item["id"] for item in payload["items"]] == [order_id]


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ImpactSession:
    def __init__(self, collections):
        self._collections = iter(collections)

    def scalars(self, _statement):
        return _Rows(next(self._collections))

    def scalar(self, _statement):
        return Decimal("0")


def _hash_order_fixture(*, reverse=False):
    def assignment(identifier):
        return SimpleNamespace(
            id=identifier,
            version=1,
            estado="PENDIENTE",
            cantidad_comprometida=Decimal("10"),
            cantidad_satisfecha=Decimal("2"),
            cantidad_planificada=Decimal("10"),
        )

    outputs = [
        SimpleNamespace(
            id="output-2",
            articulo=SimpleNamespace(unidad_inventario="UN"),
            cantidad_objetivo=Decimal("20"),
            cantidad_real=Decimal("0"),
            cantidad_rechazada=Decimal("0"),
            asignaciones=[assignment("assignment-2"), assignment("assignment-1")],
        ),
        SimpleNamespace(
            id="output-1",
            articulo=SimpleNamespace(unidad_inventario="UN"),
            cantidad_objetivo=Decimal("30"),
            cantidad_real=Decimal("1"),
            cantidad_rechazada=Decimal("0"),
            asignaciones=[assignment("assignment-4"), assignment("assignment-3")],
        ),
    ]
    works = [
        SimpleNamespace(id="work-2", estado="EN_EJECUCION", version=1, estado_previo_pausa=None),
        SimpleNamespace(id="work-1", estado="EN_EJECUCION", version=1, estado_previo_pausa=None),
    ]
    prepared_assignments = [
        SimpleNamespace(
            id="prepared-assignment-2", estado="COMPROMETIDA",
            cantidad_planificada_kg=Decimal("2"), cantidad_comprometida_kg=Decimal("2"),
            cantidad_consumida_kg=Decimal("0"),
        ),
        SimpleNamespace(
            id="prepared-assignment-1", estado="COMPROMETIDA",
            cantidad_planificada_kg=Decimal("1"), cantidad_comprometida_kg=Decimal("1"),
            cantidad_consumida_kg=Decimal("0"),
        ),
    ]
    prepared_requirements = [SimpleNamespace(
        id="prepared-requirement-1", estado="CUBIERTA", version=1,
        cantidad_requerida_kg=Decimal("3"), asignaciones=prepared_assignments,
    )]
    premezcla_lots = [SimpleNamespace(
        id="premezcla-lot-1", estado="DISPONIBLE_MAQUINA", cantidad_kg=Decimal("3"),
    )]
    collections = [
        [SimpleNamespace(id="manga-2", estado="CERRADA", version=1), SimpleNamespace(id="manga-1", estado="CERRADA", version=1)],
        [SimpleNamespace(id="tramo-2", estado="CERRADO"), SimpleNamespace(id="tramo-1", estado="CERRADO")],
        [SimpleNamespace(id="reservation-2", estado="ACTIVA", emitida_neta_kg=Decimal("0"), cantidad_consumida_kg=Decimal("0")), SimpleNamespace(id="reservation-1", estado="ACTIVA", emitida_neta_kg=Decimal("0"), cantidad_consumida_kg=Decimal("0"))],
        prepared_requirements,
        premezcla_lots,
        [SimpleNamespace(id="wip-2"), SimpleNamespace(id="wip-1")],
        [SimpleNamespace(id="prepared-2"), SimpleNamespace(id="prepared-1")],
    ]
    if reverse:
        outputs.reverse()
        for output in outputs:
            output.asignaciones.reverse()
        works.reverse()
        collections = [list(reversed(rows)) for rows in collections]
        for requirement in prepared_requirements:
            requirement.asignaciones.reverse()
    order = SimpleNamespace(
        id="of-hash",
        version=3,
        estado="EN_EJECUCION",
        codigo="OF-HASH",
        salidas=outputs,
        fabricacion=SimpleNamespace(corridas=[SimpleNamespace(id="run-1")]),
    )
    return _ImpactSession(collections), order, works


def test_impact_hash_is_stable_when_relationship_and_query_order_changes():
    first_session, first_order, first_works = _hash_order_fixture()
    second_session, second_order, second_works = _hash_order_fixture(reverse=True)

    first = _impact(first_session, first_order, "PAUSAR", first_works)
    second = _impact(second_session, second_order, "PAUSAR", second_works)

    assert first["impact_hash"] == second["impact_hash"]

    changed_session, changed_order, changed_works = _hash_order_fixture(reverse=True)
    changed_order.salidas[0].cantidad_real = Decimal("99")
    changed = _impact(changed_session, changed_order, "PAUSAR", changed_works)
    assert changed["impact_hash"] != second["impact_hash"]


def test_pause_preserves_open_physical_obligations_while_cancel_keeps_blockers():
    session, order, works = _hash_order_fixture()
    session._collections = iter([
        [SimpleNamespace(id="manga-open", estado="CONTINUIDAD_PENDIENTE", version=4)],
        [SimpleNamespace(id="tramo-open", estado="ACTIVO")],
        [SimpleNamespace(id="reservation-active", estado="ACTIVA", emitida_neta_kg=Decimal("0"), cantidad_consumida_kg=Decimal("0"))],
        [SimpleNamespace(
            id="prepared-requirement", estado="CUBIERTA", version=1,
            cantidad_requerida_kg=Decimal("3"), asignaciones=[],
        )],
        [SimpleNamespace(id="premix-lot", estado="DISPONIBLE_MAQUINA", cantidad_kg=Decimal("3"))],
        [SimpleNamespace(id="wip-pending")],
        [SimpleNamespace(id="prepared-pending")],
    ])

    pause = _impact(session, order, "PAUSAR", works)

    assert pause["can_execute"] is True
    assert pause["mangas_abiertas"] == 1
    assert pause["wip_pendiente"] == 1
    assert pause["material_preparado_pendiente"] == 1
    assert not {item["code"] for item in pause["blockers"]} & {
        "OPEN_MANGAS", "OPEN_MANGA_SEGMENTS", "PENDING_INLINE_WIP",
        "PENDING_PREPARED_MATERIAL",
    }

    session._collections = iter([
        [SimpleNamespace(id="manga-open", estado="CONTINUIDAD_PENDIENTE", version=4)],
        [SimpleNamespace(id="tramo-open", estado="ACTIVO")],
        [SimpleNamespace(id="reservation-active", estado="ACTIVA", emitida_neta_kg=Decimal("0"), cantidad_consumida_kg=Decimal("0"))],
        [SimpleNamespace(
            id="prepared-requirement", estado="CUBIERTA", version=1,
            cantidad_requerida_kg=Decimal("3"), asignaciones=[],
        )],
        [SimpleNamespace(id="premix-lot", estado="DISPONIBLE_MAQUINA", cantidad_kg=Decimal("3"))],
        [SimpleNamespace(id="wip-pending")],
        [SimpleNamespace(id="prepared-pending")],
    ])
    cancel = _impact(session, order, "CANCELAR", works)

    assert cancel["can_execute"] is False
    assert {
        "OPEN_MANGAS", "OPEN_MANGA_SEGMENTS", "PENDING_INLINE_WIP",
        "PENDING_PREPARED_MATERIAL", "ACTIVE_MATERIAL_RESERVATIONS",
    } <= {item["code"] for item in cancel["blockers"]}


def test_pause_endpoint_preserves_real_manga_control_and_weighing_facts(
    app, client, scm_config,
):
    with app.app_context():
        creator, _approver, order, run, output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=200)
        )
        order.estado = "EN_EJECUCION"
        run.estado = "EN_EJECUCION"
        db.session.commit()
        started = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]},
            action="iniciar",
        )
        work_id = UUID(started["trabajo_color"]["id"])
        manga_ids = [UUID(item["public_id"]) for item in created["mangas"]]
        assert len(manga_ids) >= 2
        mangas = ScmManga.query.filter(ScmManga.public_id.in_(manga_ids)).order_by(ScmManga.id).all()
        assert len(mangas) == len(manga_ids)
        for manga in mangas:
            manga.lote_articulo.articulo.unidad_inventario = "KG"
        db.session.flush()

        control_station, control_label = _print_color_manga(
            actor=creator, manga_id=manga_ids[0], station_code="PAUSE-CONTROL-REAL",
        )
        control = register_manga_weighing_control(
            db.session,
            station_id=control_station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": control_label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "5.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-11-06T16:55:00-05:00",
                "reading_stable": True,
                "control_type": "AVANCE_KG",
            },
        )
        final_station, final_label = _print_color_manga(
            actor=creator, manga_id=manga_ids[1], station_code="PAUSE-PESAJE-REAL",
        )
        final = confirm_manga_weighing(
            db.session,
            station_id=final_station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": final_label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "12.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-11-06T17:00:00-05:00",
                "reading_stable": True,
            },
        )
        assert control["control"]["peso_neto_kg"] == "5.000"
        assert final["weighing"]["peso_fisico_neto_kg"] == "12.000"
        wip_operation_id = uuid4()
        material_operation_id = uuid4()
        db.session.add(ScmOperacion(
            operation_id=wip_operation_id,
            endpoint="TEST /pause/wip",
            actor_id=creator.id,
            request_sha256="a" * 64,
            estado_http=201,
            response_json={},
        ))
        db.session.add(ScmOperacion(
            operation_id=material_operation_id,
            endpoint="TEST /pause/material",
            actor_id=creator.id,
            request_sha256="b" * 64,
            estado_http=201,
            response_json={},
        ))
        db.session.flush()
        wip_saldo = ScmSaldoWipSalida(
            trabajo_color_id=work_id,
            orden_operacion_salida_id=output.id,
            articulo_id=output.articulo_scm_id,
            cantidad_acreditada=Decimal("1.000"),
            cantidad_consumida=Decimal("0.000"),
        )
        db.session.add(wip_saldo)
        db.session.flush()
        assert mangas[0].asignacion_id is not None
        wip_reservation = ScmReservaWipSalida(
            saldo_id=wip_saldo.id,
            manga_id=mangas[0].id,
            asignacion_plan_id=mangas[0].asignacion_id,
            articulo_componente_id=output.articulo_scm_id,
            cantidad_reservada=Decimal("1.000"),
            cantidad_aplicada=Decimal("0.000"),
            estado="CREDITO_EN_LINEA_PENDIENTE",
            creada_por_id=creator.id,
            operation_id=wip_operation_id,
        )
        db.session.add(wip_reservation)
        category = ScmCategoriaRecepcion.query.first()
        material = ScmMaterial(
            codigo=f"MP-PAUSE-{uuid4().hex[:8].upper()}",
            nombre="Materia prima fixture pausa",
            clase="MATERIA_PRIMA",
            categoria_recepcion_id=category.id,
        )
        location = ScmUbicacionInventario(
            codigo=f"PAUSE-MAT-{uuid4().hex[:8].upper()}",
            nombre="Ubicacion material fixture pausa",
            clases_articulo_json=["MATERIA_PRIMA"],
        )
        db.session.add_all([material, location])
        db.session.flush()
        recipe = RecetaColorMaestra(
            color_produccion_id=run.color_produccion_id,
            producto_scope="*",
            nombre_variante=f"PAUSE-{uuid4().hex[:8]}",
            revision=1,
            estado="APROBADA",
            base_virgen_kg=Decimal("25"),
        )
        db.session.add(recipe)
        db.session.flush()
        run.receta_revision_id = recipe.id
        material_balance = ScmSaldoMaterialInventario(
            material_id=material.id,
            ubicacion_id=location.id,
            cantidad_fisica_kg=Decimal("2.000"),
            cantidad_reservada_kg=Decimal("1.000"),
        )
        requirement = ScmRequerimientoMaterial(
            corrida_fabricacion_id=run.id,
            material_id=material.id,
            tipo_componente="MATERIA_PRIMA",
            cantidad_plan_kg=Decimal("1.000"),
            receta_revision_id=run.receta_revision_id,
            calculo_snapshot_json={"fixture": "pause"},
            created_by_id=creator.id,
        )
        db.session.add_all([material_balance, requirement])
        db.session.flush()
        material_reservation = ScmReservaMaterial(
            requerimiento_id=requirement.id,
            saldo_material_id=material_balance.id,
            cantidad_kg=Decimal("1.000"),
            emitida_neta_kg=Decimal("0.000"),
            cantidad_consumida_kg=Decimal("0.000"),
            estado="ACTIVA",
            created_by_id=creator.id,
        )
        db.session.add(material_reservation)
        db.session.commit()
        requirement_id = requirement.id
        db.session.expire_all()
        order = db.session.get(ScmOrdenOperacion, order.id)
        physical_before = {
            "order_version": order.version,
            "output": (
                output.cantidad_real,
                output.cantidad_rechazada,
                [(a.id, a.estado, a.cantidad_comprometida, a.cantidad_satisfecha)
                 for a in output.asignaciones],
            ),
            "mangas": [
                (m.id, m.estado, m.version, m.cantidad_confirmada_un)
                for m in ScmManga.query.filter(ScmManga.id.in_([m.id for m in mangas])).order_by(ScmManga.id)
            ],
            "tramos": [
                (t.id, t.estado, t.cantidad_inicio_kg, t.cantidad_fin_kg, t.cantidad_atribuida_kg)
                for t in ScmTramoMangaTrabajo.query.filter(ScmTramoMangaTrabajo.manga_id.in_([m.id for m in mangas])).order_by(ScmTramoMangaTrabajo.id)
            ],
            "controls": [
                (c.id, c.tipo, c.peso_neto_kg, c.aporte_desde_control_anterior_kg)
                for c in ScmControlPesoManga.query.filter(ScmControlPesoManga.manga_id.in_([m.id for m in mangas])).order_by(ScmControlPesoManga.id)
            ],
            "weighings": [
                (p.id, p.estado, p.peso_fisico_neto_kg)
                for p in ScmPesajeManga.query.filter(ScmPesajeManga.manga_id.in_([m.id for m in mangas])).order_by(ScmPesajeManga.id)
            ],
            "wip": [
                (wip_saldo.id, wip_saldo.cantidad_acreditada, wip_saldo.cantidad_consumida,
                 wip_reservation.estado, wip_reservation.cantidad_aplicada),
            ],
            "material": [
                (material_reservation.id, material_reservation.estado,
                 material_reservation.emitida_neta_kg, material_reservation.cantidad_consumida_kg),
            ],
        }
        order_id, actor_id = str(order.id), creator.id

    impact_response = client.get(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/impacto-ejecucion?accion=PAUSAR",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert impact_response.status_code == 200, impact_response.get_json()
    impact = impact_response.get_json()
    assert impact["can_execute"] is True
    assert impact["mangas_abiertas"] >= 1
    paused = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/pausar-ejecucion",
        json={
            "version": impact["version"],
            "impact_hash": impact["impact_hash"],
            "motivo": "Preservar hechos fisicos durante pausa",
        },
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert paused.status_code == 200, paused.get_json()

    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        assert order.estado == "PAUSADA"
        assert order.fabricacion.corridas[0].estado == "PAUSADA"
        assert {
            (m.id, m.estado, m.version, m.cantidad_confirmada_un)
            for m in ScmManga.query.filter(ScmManga.id.in_([row[0] for row in physical_before["mangas"]]))
        } == {row for row in physical_before["mangas"]}
        assert [
            (t.id, t.estado, t.cantidad_inicio_kg, t.cantidad_fin_kg, t.cantidad_atribuida_kg)
            for t in ScmTramoMangaTrabajo.query.filter(ScmTramoMangaTrabajo.manga_id.in_([row[0] for row in physical_before["mangas"]])).order_by(ScmTramoMangaTrabajo.id)
        ] == physical_before["tramos"]
        assert [
            (c.id, c.tipo, c.peso_neto_kg, c.aporte_desde_control_anterior_kg)
            for c in ScmControlPesoManga.query.filter(ScmControlPesoManga.manga_id.in_([row[0] for row in physical_before["mangas"]])).order_by(ScmControlPesoManga.id)
        ] == physical_before["controls"]
        assert [
            (p.id, p.estado, p.peso_fisico_neto_kg)
            for p in ScmPesajeManga.query.filter(ScmPesajeManga.manga_id.in_([row[0] for row in physical_before["mangas"]])).order_by(ScmPesajeManga.id)
        ] == physical_before["weighings"]
        assert [
            (s.id, s.cantidad_acreditada, s.cantidad_consumida, r.estado, r.cantidad_aplicada)
            for s in ScmSaldoWipSalida.query.filter_by(trabajo_color_id=work_id).all()
            for r in s.reservas
        ] == physical_before["wip"]
        assert [
            (r.id, r.estado, r.emitida_neta_kg, r.cantidad_consumida_kg)
            for r in ScmReservaMaterial.query.filter_by(requerimiento_id=requirement_id).all()
        ] == physical_before["material"]
