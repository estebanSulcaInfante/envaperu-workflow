from uuid import UUID, uuid4

import pytest

from app.extensions import db
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_auditoria import ScmEvento
from app.models.trabajador import RolOperativo
from app.services import scm_ot_service as service
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
