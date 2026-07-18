import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.extensions import db
from app.models.control_peso import ControlPeso
from app.models.estacion_pesaje import (
    EstacionAvanceProduccion,
    EstacionPesaje,
    EstacionReporteAvanceRecepcion,
)
from app.models.orden import OrdenProduccion
from app.services.station_auth import hash_station_token


pytestmark = pytest.mark.contract

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = WORKSPACE_ROOT / "contracts" / "station-production-progress-v1"
STATION_TOKEN = "production-progress-test-token-with-high-entropy-0001"


def _load_example():
    return json.loads(
        (CONTRACT_DIR / "examples.json").read_text(encoding="utf-8")
    )["request"]


def _validator(definition):
    schema = json.loads(
        (CONTRACT_DIR / "contract.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema["$defs"][definition],
        format_checker=FormatChecker(),
    )


@pytest.fixture
def progress_station(app):
    station_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(
            EstacionPesaje(
                station_id=station_id,
                codigo="PESAJE-PROGRESO-01",
                nombre="Balanza de produccion",
                ubicacion="Produccion",
                estado_admin="ACTIVA",
                token_hash=hash_station_token(STATION_TOKEN),
            )
        )
        db.session.add(
            OrdenProduccion(
                numero_op="OP-1401",
                producto="Tapa 38 mm",
                calculo_peso_produccion=100.0,
            )
        )
        db.session.commit()
    return station_id


def _headers(payload):
    return {
        "Authorization": f"Bearer {STATION_TOKEN}",
        "Idempotency-Key": payload["report_id"],
        "X-Station-Version": "1.1.0-pilot",
        "X-Correlation-Id": str(uuid.uuid4()),
    }


def _put(client, station_id, payload):
    return client.put(
        f"/api/integration/v1/stations/{station_id}/production-progress",
        headers=_headers(payload),
        json=payload,
    )


def test_progress_contract_copies_match_and_examples_validate():
    for repository in ("backend", "modulo-pesaje/backend"):
        copy_dir = WORKSPACE_ROOT / repository / "contracts" / CONTRACT_DIR.name
        for filename in ("contract.schema.json", "examples.json"):
            assert hashlib.sha256(
                (CONTRACT_DIR / filename).read_bytes()
            ).digest() == hashlib.sha256((copy_dir / filename).read_bytes()).digest()

    examples = json.loads(
        (CONTRACT_DIR / "examples.json").read_text(encoding="utf-8")
    )
    _validator("request").validate(examples["request"])
    _validator("response").validate(examples["response"])


def test_snapshot_replay_separates_orders_and_never_creates_inventory(
    client,
    app,
    progress_station,
):
    payload = _load_example()

    first = _put(client, progress_station, payload)
    replay = _put(client, progress_station, payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.get_json() == first.get_json()
    assert first.get_json()["rows_applied"] == 2

    with app.app_context():
        assert EstacionReporteAvanceRecepcion.query.count() == 1
        assert EstacionAvanceProduccion.query.count() == 2
        assert ControlPeso.query.count() == 0

    response = client.get(
        "/api/monitoring/v1/production-progress?date=2026-07-17"
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["source"] == "LOCAL_REPORTED_LEGACY"
    assert body["summary"] == {
        "bags": 3,
        "production_orders": 2,
        "stations_reporting": 1,
        "weight_kg": "75.150",
    }
    assert [item["op"] for item in body["items"]] == ["OP-1401", "OP-1402"]

    op_1401, op_1402 = body["items"]
    assert op_1401["weight_kg"] == "50.250"
    assert op_1401["target_kg"] == "100.000"
    assert op_1401["progress_percent"] == 50.3
    assert op_1401["target_status"] == "AVAILABLE"
    assert op_1402["weight_kg"] == "24.900"
    assert op_1402["target_kg"] is None
    assert op_1402["progress_percent"] is None
    assert op_1402["target_status"] == "OP_NOT_FOUND"


def test_new_snapshot_replaces_window_and_conflicting_replay_is_rejected(
    client,
    app,
    progress_station,
):
    payload = _load_example()
    assert _put(client, progress_station, payload).status_code == 200

    conflicting = copy.deepcopy(payload)
    conflicting["rows"][0]["weight_kg"] = "99.000"
    conflict = _put(client, progress_station, conflicting)
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "IDEMPOTENCY_CONFLICT"

    replacement = copy.deepcopy(payload)
    replacement["report_id"] = str(uuid.uuid4())
    replacement["rows"] = [copy.deepcopy(payload["rows"][0])]
    replacement["rows"][0]["bags"] = 1
    replacement["rows"][0]["weight_kg"] = "25.125"
    replacement["rows"][0]["first_capture_at_utc"] = (
        replacement["rows"][0]["last_capture_at_utc"]
    )

    applied = _put(client, progress_station, replacement)
    assert applied.status_code == 200
    assert applied.get_json()["rows_applied"] == 1

    with app.app_context():
        rows = EstacionAvanceProduccion.query.all()
        assert len(rows) == 1
        assert rows[0].op == "OP-1401"
        assert str(rows[0].weight_kg) == "25.125"
        assert EstacionReporteAvanceRecepcion.query.count() == 2
        assert ControlPeso.query.count() == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(window_start_date="2026-07-18"),
        lambda payload: payload["rows"].append(copy.deepcopy(payload["rows"][0])),
        lambda payload: payload["rows"][0].update(weight_kg="0.000"),
    ],
)
def test_invalid_or_ambiguous_snapshot_is_rejected_without_replacement(
    client,
    app,
    progress_station,
    mutate,
):
    baseline = _load_example()
    assert _put(client, progress_station, baseline).status_code == 200

    invalid = _load_example()
    invalid["report_id"] = str(uuid.uuid4())
    mutate(invalid)
    response = _put(client, progress_station, invalid)

    assert response.status_code == 422
    with app.app_context():
        assert EstacionAvanceProduccion.query.count() == 2
        assert EstacionReporteAvanceRecepcion.query.count() == 1
        assert ControlPeso.query.count() == 0
