import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from test_station_monitoring_contract import provisioned_station, _auth_headers

CONTRACT = Path(__file__).resolve().parents[1] / "contracts/scm-kg-return-weighing-v1"


def test_contract_copies_and_examples():
    workspace = Path(__file__).resolve().parents[2]
    for filename in ("contract.schema.json", "examples.json"):
        assert (CONTRACT / filename).read_bytes() == (workspace / "contracts/scm-kg-return-weighing-v1" / filename).read_bytes()
        assert (CONTRACT / filename).read_bytes() == (workspace / "modulo-pesaje/backend/contracts/scm-kg-return-weighing-v1" / filename).read_bytes()
    schema = json.loads((CONTRACT / "contract.schema.json").read_text())
    for name, example in json.loads((CONTRACT / "examples.json").read_text()).items():
        Draft202012Validator(schema["$defs"][name], format_checker=FormatChecker()).validate(example)


def test_station_return_routes_authenticate_and_bind_station(client, provisioned_station, monkeypatch):
    example = json.loads((CONTRACT / "examples.json").read_text())["captureRequest"]
    calls = []
    def capture(session, **kwargs):
        calls.append(kwargs)
        return {"measurement": {"neto_kg": "3.125"}}
    monkeypatch.setattr("app.services.scm_kg_custody_service.capture_kg_measurement", capture)
    path = f"/api/integration/v1/stations/{provisioned_station}/kg-return-measurements"
    assert client.post(path, json=example).status_code == 401
    headers = {**_auth_headers(), "Idempotency-Key": str(uuid4())}
    response = client.post(path, headers=headers, json=example)
    assert response.status_code == 200
    assert calls[0]["station_id"] == provisioned_station
    assert calls[0]["data"]["station_version"] == headers["X-Station-Version"]
    assert calls[0]["snapshot"]["source"] == "SERIAL"
    assert client.post(path.replace(provisioned_station, str(uuid4())), headers=headers, json=example).status_code == 403
    assert client.post(path, headers=headers, json={**example, "peso_manual": 4}).status_code == 422
    assert len(calls) == 1
