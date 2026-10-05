"""HTTP batch atomicity and recovery, using only the isolated test fixture."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app import db
from app.models.scm_auditoria import ScmOperacion
from app.models.scm_catalogos import ScmCapacidad
from app.models.scm_inventory_kg import (
    ScmMovimientoInventarioKg, ScmReservaUnidadKg, ScmRetiroArmadoKg,
    ScmSaldoInventarioKg, ScmUnidadFisicaKg,
)
from app.models.scm_inventory_operations import ScmAlmacenTrabajador
from app.models.trabajador import Trabajador
from app.services.scm_configuration import ensure_initial_scm_configuration
from test_scm_kg_custody_postgres import _seed_custody

ENDPOINT = "/api/scm/v1/retiros-armado-kg/lotes"


@pytest.fixture
def batch_data(app):
    with app.app_context():
        ensure_initial_scm_configuration()
        actor_id, _, warehouse_id, _, balance_id, unit_ids = _seed_custody(app)
        actor = db.session.get(Trabajador, actor_id)
        for code in ("PICKING_DESPACHAR", "ABASTECIMIENTO_VER"):
            actor.roles[0].capacidades.append(ScmCapacidad.query.filter_by(codigo=code).one())
        receiver = Trabajador(codigo="BATCH-RECEIVER", nombres="Receiver", apellidos="UAT", activo=True)
        db.session.add(receiver)
        db.session.get(ScmSaldoInventarioKg, balance_id).cantidad_fisica_kg = 10
        db.session.commit()
        app.config["KG_CUSTODY_WRITE_ENABLED"] = True
        body = {"items": [{"unit_id": str(uid), "version": 1} for uid in unit_ids],
                "motivo_operativo": "Armado UAT", "tenedor_fisico_id": receiver.id}
        return {"actor": actor_id, "receiver": receiver.id, "warehouse": warehouse_id,
                "balance": balance_id, "units": unit_ids, "body": body}


def headers(data, key):
    return {"X-Actor-Id": str(data["actor"]), "Idempotency-Key": str(key)}


def assert_untouched(app, data, key):
    with app.app_context():
        balance = db.session.get(ScmSaldoInventarioKg, data["balance"])
        assert balance.cantidad_fisica_kg == Decimal("10")
        assert balance.cantidad_reservada_kg == 0
        assert balance.cantidad_retirada_kg == 0
        assert ScmRetiroArmadoKg.query.count() == 0
        assert ScmMovimientoInventarioKg.query.count() == 0
        assert ScmReservaUnidadKg.query.count() == 0
        assert db.session.get(ScmOperacion, key) is None


def test_http_batch_one_commit_replay_get_and_independent_causal_returns(app, client, batch_data):
    data, key = batch_data, uuid4()
    result = client.post(ENDPOINT, headers=headers(data, key), json=data["body"])
    assert result.status_code == 201, result.get_json()
    payload = result.get_json()
    assert payload["cantidad_unidades"] == 2
    assert payload["total_kg"] == "10.000"
    assert len(payload["retiros"]) == 2
    assert all(len(item["items"]) == 1 for item in payload["retiros"])
    assert all(item["tenedor_fisico_id"] == data["receiver"] for item in payload["retiros"])
    assert client.post(ENDPOINT, headers=headers(data, key), json=data["body"]).get_json() == payload
    recovered = client.get(f"{ENDPOINT}/{key}", headers=headers(data, key))
    assert recovered.status_code == 200
    assert recovered.get_json() == payload
    with app.app_context():
        balance = db.session.get(ScmSaldoInventarioKg, data["balance"])
        assert balance.cantidad_fisica_kg == 0
        assert balance.cantidad_reservada_kg == 0
        assert balance.cantidad_retirada_kg == 10
        assert ScmMovimientoInventarioKg.query.count() == 2
        assert ScmReservaUnidadKg.query.filter_by(estado="ACTIVA").count() == 0
        # Existing division contract must still work for one causal retiro.
        from app.services.scm_kg_custody_service import divide_kg_unit
        first = payload["retiros"][0]
        unit = db.session.get(ScmUnidadFisicaKg, UUID(first["items"][0]["unidad"]["id"]))
        divided = divide_kg_unit(db.session, actor_id=data["actor"], retiro_id=UUID(first["id"]),
                                operation_id=uuid4(), data={"version": unit.version,
                                    "partes": [{"intencion": "RETORNO"}, {"intencion": "PERMANECE"}]})
        assert len(divided["division"]["partes"]) == 2
        assert all(part["unidad"]["kg_verificados"] is None for part in divided["division"]["partes"])


@pytest.mark.parametrize("failure", ["stale", "blocked", "missing", "receiver", "capability", "flag"])
def test_http_batch_rejection_does_not_leave_first_item_withdrawn(app, client, batch_data, failure):
    data, key = batch_data, uuid4()
    if failure == "stale":
        data["body"]["items"][1]["version"] = 99
    elif failure == "missing":
        data["body"]["items"][1]["unit_id"] = str(uuid4())
    elif failure == "receiver":
        data["body"]["tenedor_fisico_id"] = False
    elif failure == "flag":
        app.config["KG_CUSTODY_WRITE_ENABLED"] = False
    else:
        with app.app_context():
            if failure == "blocked":
                db.session.get(ScmUnidadFisicaKg, data["units"][1]).estado_calidad = "BLOQUEADA"
            else:
                actor = db.session.get(Trabajador, data["actor"])
                role = actor.roles[0]
                role.capacidades.remove(ScmCapacidad.query.filter_by(codigo="PICKING_DESPACHAR").one())
            db.session.commit()
    result = client.post(ENDPOINT, headers=headers(data, key), json=data["body"])
    assert result.status_code in (403, 404, 409, 422), result.get_json()
    assert_untouched(app, data, key)


def test_http_batch_aggregates_shared_balance_before_writing(app, client, batch_data):
    data, key = batch_data, uuid4()
    with app.app_context():
        for uid in data["units"]:
            db.session.get(ScmUnidadFisicaKg, uid).kg_verificados = 8
        db.session.commit()
    result = client.post(ENDPOINT, headers=headers(data, key), json=data["body"])
    assert result.status_code == 409, result.get_json()
    assert_untouched(app, data, key)


def test_http_batch_rolls_back_after_second_movement_fault(app, client, batch_data, monkeypatch):
    from app.services import scm_kg_custody_service as service
    data, key = batch_data, uuid4()
    original = service._delivery_payload
    calls = []
    def fail_second(session, retiro):
        calls.append(retiro.id)
        if len(calls) == 2:
            raise RuntimeError("isolated late failure")
        return original(session, retiro)
    monkeypatch.setattr(service, "_delivery_payload", fail_second)
    with pytest.raises(RuntimeError, match="isolated late failure"):
        client.post(ENDPOINT, headers=headers(data, key), json=data["body"])
    assert len(calls) == 2
    assert_untouched(app, data, key)
    # A late failure must leave the original immutable intent safely retryable.
    monkeypatch.setattr(service, "_delivery_payload", original)
    retried = client.post(ENDPOINT, headers=headers(data, key), json=data["body"])
    assert retried.status_code == 201, retried.get_json()
    assert retried.get_json()["cantidad_unidades"] == 2
    with app.app_context():
        assert ScmMovimientoInventarioKg.query.count() == 2


def test_http_batch_replay_checks_request_and_current_scope(app, client, batch_data):
    data, key = batch_data, uuid4()
    assert client.post(ENDPOINT, headers=headers(data, key), json=data["body"]).status_code == 201
    changed = {**data["body"], "motivo_operativo": "Otra causa"}
    assert client.post(ENDPOINT, headers=headers(data, key), json=changed).status_code == 409
    with app.app_context():
        membership = ScmAlmacenTrabajador.query.filter_by(trabajador_id=data["actor"], almacen_id=data["warehouse"]).one()
        membership.activo = False
        db.session.commit()
    assert client.get(f"{ENDPOINT}/{key}", headers=headers(data, key)).status_code in (403, 404)
    assert client.post(ENDPOINT, headers=headers(data, key), json=data["body"]).status_code in (403, 404)
