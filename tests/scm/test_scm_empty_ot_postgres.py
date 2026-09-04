from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from app.extensions import db
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_ot_service import create_fabrication_ot_header
from tests.scm.test_scm_inline_assembly_postgres import postgres_inline_app

pytestmark = pytest.mark.postgres


def test_m4_annul_vs_start_has_one_winner(postgres_inline_app):
    app = postgres_inline_app
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(RolOperativo.query.filter_by(codigo="SUPERVISOR").one())
        db.session.commit()
        actor_id = actor.id
        ot = create_fabrication_ot_header(db.session, actor_id=actor.id, operation_id=uuid4(),
            data={"maquina_id": Maquina.query.first().id, "fecha_operativa": "2026-09-02", "turno": "DIA"})["ot"]
    barrier = Barrier(2)

    def command(action):
        with app.test_client() as client:
            barrier.wait(timeout=10)
            result = client.post(f'/api/scm/v1/ots/{ot["public_id"]}/{action}',
                json={"version": ot["version"], **({"motivo": "Error de jornada"} if action == "anular" else {})},
                headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())})
            return result.status_code, result.json

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(command, action) for action in ("anular", "iniciar")]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(status for status, _ in results) == [200, 409], results
    with app.app_context():
        row = RegistroDiarioProduccion.query.filter_by(public_id=UUID(ot["public_id"])).one()
        assert row.estado in {"ANULADA", "EN_EJECUCION"}
        assert ScmEvento.query.filter_by(tipo="FABRICATION_OT_HEADER_ANNULLED").count() == int(row.estado == "ANULADA")
