import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.extensions import db
from app.models.control_peso import ControlPeso
from app.models.maquina import Maquina
from app.models.orden import OrdenProduccion


pytestmark = pytest.mark.contract

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / "sync-pesajes-legacy-v1"


def load_json(filename):
    return json.loads((CONTRACT_DIR / filename).read_text(encoding="utf-8"))


def validate_contract(definition, instance):
    schema = load_json("contract.schema.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema["$defs"][definition]).validate(instance)


def test_sync_provider_matches_legacy_v1_contract(client, app):
    examples = load_json("examples.json")
    request_payload = examples["request"]
    validate_contract("request", request_payload)

    with app.app_context():
        machine = Maquina.query.filter_by(codigo="MQ-01").one()
        db.session.add(
            OrdenProduccion(
                numero_op="OP-CONTRACT-001",
                maquina_id=machine.id,
                producto="PRODUCTO CONTRACT",
                molde="MOLDE CONTRACT",
            )
        )
        db.session.commit()

    response = client.post("/api/sync/pesajes", json=request_payload)

    assert response.status_code == 200
    response_payload = response.get_json()
    validate_contract("response", response_payload)
    assert response_payload["synced"] == [{"local_id": 1}]
    assert response_payload["errors"] == []

    with app.app_context():
        assert ControlPeso.query.count() == 1
