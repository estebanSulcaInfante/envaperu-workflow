from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest

from app.extensions import db
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_ot_service import create_fabrication_ot_header
from app.services import scm_ot_service
from tests.scm.test_scm_inline_assembly_postgres import postgres_inline_app

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("actions", [("anular", "iniciar"), ("iniciar", "anular")])
def test_m4_annul_vs_start_has_one_winner(postgres_inline_app, actions):
    app = postgres_inline_app
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(RolOperativo.query.filter_by(codigo="SUPERVISOR").one())
        db.session.commit()
        actor_id = actor.id
        machine = Maquina.query.first()
        annulled_before = ScmEvento.query.filter_by(
            tipo="FABRICATION_OT_HEADER_ANNULLED"
        ).count()
        ot = create_fabrication_ot_header(db.session, actor_id=actor.id, operation_id=uuid4(),
            data={"maquina_id": machine.id, "fecha_operativa": "2026-09-02", "turno": "DIA"})["ot"]
    barrier = Barrier(2)

    def command(action):
        with app.test_client() as client:
            barrier.wait(timeout=10)
            result = client.post(f'/api/scm/v1/ots/{ot["public_id"]}/{action}',
                json={"version": ot["version"], **({"motivo": "Error de jornada"} if action == "anular" else {})},
                headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())})
            return result.status_code, result.json

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(command, action) for action in actions]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(status for status, _ in results) == [200, 409], results
    with app.app_context():
        row = RegistroDiarioProduccion.query.filter_by(public_id=UUID(ot["public_id"])).one()
        assert row.estado in {"ANULADA", "EN_EJECUCION"}
        assert ScmEvento.query.filter_by(tipo="FABRICATION_OT_HEADER_ANNULLED").count() - annulled_before == int(row.estado == "ANULADA")


def test_m4_start_refreshes_identity_after_annul_commits(
    postgres_inline_app, monkeypatch
):
    """A start that read PLANIFICADA before annulment must lose after refresh."""
    app = postgres_inline_app
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(RolOperativo.query.filter_by(codigo="SUPERVISOR").one())
        db.session.commit()
        actor_id = actor.id
        machine = Maquina.query.first()
        ot = create_fabrication_ot_header(
            db.session,
            actor_id=actor_id,
            operation_id=uuid4(),
            data={
                "maquina_id": machine.id,
                "fecha_operativa": "2026-09-02",
                "turno": "DIA",
            },
        )["ot"]

    probe_reached = Event()
    release_start = Event()
    original_guard = scm_ot_service.ensure_of_accepts_execution_write

    def pause_after_start_probe(session, order_id, *, action):
        if action == "agregar mangas":
            probe_reached.set()
            assert release_start.wait(timeout=10)
        return original_guard(session, order_id, action=action)

    monkeypatch.setattr(
        scm_ot_service,
        "ensure_of_accepts_execution_write",
        pause_after_start_probe,
    )

    def start():
        with app.test_client() as client:
            response = client.post(
                f'/api/scm/v1/ots/{ot["public_id"]}/iniciar',
                json={"version": ot["version"]},
                headers={
                    "X-Actor-Id": str(actor_id),
                    "Idempotency-Key": str(uuid4()),
                },
            )
            return response.status_code, response.json

    with ThreadPoolExecutor(max_workers=1) as pool:
        start_future = pool.submit(start)
        assert probe_reached.wait(timeout=10)
        with app.test_client() as client:
            annulled = client.post(
                f'/api/scm/v1/ots/{ot["public_id"]}/anular',
                json={"version": ot["version"], "motivo": "Carrera controlada"},
                headers={
                    "X-Actor-Id": str(actor_id),
                    "Idempotency-Key": str(uuid4()),
                },
            )
        assert annulled.status_code == 200, annulled.json
        release_start.set()
        started = start_future.result(timeout=30)

    assert started[0] == 409, started
    assert started[1]["error"]["code"] in {
        "INVALID_STATE_TRANSITION",
        "VERSION_CONFLICT",
    }
