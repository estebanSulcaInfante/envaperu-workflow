from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app import db
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.scm_ot import ScmTrabajoOt
from app.models.scm_production_orders import ScmOrdenOperacion
from app.services.scm_fabrication_execution_service import _impact
from tests.scm.test_scm_fabrication_replacement import _seed_released_order


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
    collections = [
        [SimpleNamespace(id="manga-2", estado="CERRADA", version=1), SimpleNamespace(id="manga-1", estado="CERRADA", version=1)],
        [SimpleNamespace(id="tramo-2", estado="CERRADO"), SimpleNamespace(id="tramo-1", estado="CERRADO")],
        [SimpleNamespace(id="reservation-2", estado="ACTIVA", emitida_neta_kg=Decimal("0"), cantidad_consumida_kg=Decimal("0")), SimpleNamespace(id="reservation-1", estado="ACTIVA", emitida_neta_kg=Decimal("0"), cantidad_consumida_kg=Decimal("0"))],
        [SimpleNamespace(id="wip-2"), SimpleNamespace(id="wip-1")],
        [SimpleNamespace(id="prepared-2"), SimpleNamespace(id="prepared-1")],
    ]
    if reverse:
        outputs.reverse()
        for output in outputs:
            output.asignaciones.reverse()
        works.reverse()
        collections = [list(reversed(rows)) for rows in collections]
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
