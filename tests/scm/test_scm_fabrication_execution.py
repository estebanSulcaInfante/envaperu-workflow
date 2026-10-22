from datetime import date
from uuid import UUID, uuid4

from app import db
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.scm_ot import ScmTrabajoOt
from app.models.scm_production_orders import ScmOrdenOperacion
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
