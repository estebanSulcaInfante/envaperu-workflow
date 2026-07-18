import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import create_engine, inspect

from app.extensions import db
from app.models.control_peso import ControlPeso
from app.models.estacion_pesaje import (
    EstacionEstadoActual,
    EstacionHeartbeatRecepcion,
    EstacionPesaje,
)
from app.services.station_auth import hash_station_token
from app.services.station_monitoring import classify_communication
from scripts.migrate_station_monitoring import (
    STATION_MONITORING_TABLES,
    create_station_monitoring_tables,
)


pytestmark = pytest.mark.contract

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = WORKSPACE_ROOT / "contracts" / "station-capabilities-v1"
HEARTBEAT_DIR = WORKSPACE_ROOT / "contracts" / "station-heartbeat-v1"
CONTINUITY_DIR = WORKSPACE_ROOT / "contracts" / "station-legacy-continuity-v1"
STATION_TOKEN = "central-monitoring-test-token-with-high-entropy-0001"


def _load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _validator(contract_dir, definition):
    schema = _load_json(contract_dir / "contract.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema["$defs"][definition],
        format_checker=FormatChecker(),
    )


def test_provider_contract_copies_match_workspace_canonical():
    provider_root = Path(__file__).resolve().parents[1]
    for contract in (
        "station-capabilities-v1",
        "station-heartbeat-v1",
        "station-production-progress-v1",
        "station-legacy-continuity-v1",
    ):
        for filename in ("contract.schema.json", "examples.json"):
            canonical = WORKSPACE_ROOT / "contracts" / contract / filename
            provider = provider_root / "contracts" / contract / filename
            assert hashlib.sha256(canonical.read_bytes()).digest() == hashlib.sha256(
                provider.read_bytes()
            ).digest()


def test_station_monitoring_schema_migration_is_scoped_and_idempotent():
    engine = create_engine("sqlite://")

    first_created = create_station_monitoring_tables(engine)
    second_created = create_station_monitoring_tables(engine)

    assert first_created == list(STATION_MONITORING_TABLES)
    assert second_created == []
    assert set(inspect(engine).get_table_names()) == set(STATION_MONITORING_TABLES)


def test_legacy_continuity_examples_match_contract():
    examples = _load_json(CONTINUITY_DIR / "examples.json")
    schema = _load_json(CONTINUITY_DIR / "contract.schema.json")
    for definition in (
        "syncState",
        "deltaRequest",
        "deltaResponse",
        "commandList",
        "commandAckRequest",
    ):
        Draft202012Validator(
            {**schema, "$ref": f"#/$defs/{definition}"},
            format_checker=FormatChecker(),
        ).validate(examples[definition])


def _auth_headers(token=STATION_TOKEN):
    return {
        "Authorization": f"Bearer {token}",
        "X-Station-Version": "1.1.0-pilot",
        "X-Correlation-Id": str(uuid.uuid4()),
    }


@pytest.fixture
def provisioned_station(app):
    station_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(
            EstacionPesaje(
                station_id=station_id,
                codigo="PESAJE-PLANTA-01",
                nombre="Balanza principal",
                ubicacion="Produccion - Balanza principal",
                estado_admin="ACTIVA",
                token_hash=hash_station_token(STATION_TOKEN),
            )
        )
        db.session.commit()
    return station_id


def _heartbeat_payload(station_id, *, sequence=1, boot_id=None, heartbeat_id=None):
    payload = copy.deepcopy(_load_json(HEARTBEAT_DIR / "examples.json")["request"])
    payload["heartbeat_id"] = heartbeat_id or str(uuid.uuid4())
    payload["boot_id"] = boot_id or str(uuid.uuid4())
    payload["sequence"] = sequence
    payload["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload["context"]["op"] = f"OP-{station_id[:8]}"
    return payload


def _put_heartbeat(client, station_id, payload, token=STATION_TOKEN):
    headers = _auth_headers(token)
    headers["Idempotency-Key"] = payload["heartbeat_id"]
    return client.put(
        f"/api/integration/v1/stations/{station_id}/heartbeat",
        headers=headers,
        json=payload,
    )


def test_capabilities_requires_station_auth_and_matches_contract(
    client,
    provisioned_station,
):
    assert client.get("/api/integration/v1/capabilities").status_code == 401

    response = client.get(
        "/api/integration/v1/capabilities",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.get_json()
    _validator(CAPABILITIES_DIR, "response").validate(payload)
    assert set(payload["supported_contracts"]) == {
        "heartbeat",
        "catalog",
        "weight_event",
    }
    assert (
        "station-production-progress-v1"
        in payload["supported_contracts"]["weight_event"]
    )
    assert payload["features"] == {
        "catalog_snapshot": False,
        "legacy_weight_ingest_enabled": False,
        "monitoring": True,
        "remote_hardware_commands": False,
        "pilot_data_commands": True,
    }


def test_provisioning_cli_shows_token_once_and_stores_only_hash(runner, app):
    station_id = str(uuid.uuid4())
    result = runner.invoke(
        args=[
            "provision-weighing-station",
            "--station-id",
            station_id,
            "--code",
            "PESAJE-CLI-01",
            "--name",
            "Balanza CLI",
            "--location",
            "Planta de pruebas",
        ]
    )

    assert result.exit_code == 0
    token = next(
        line.removeprefix("TOKEN_ONCE=")
        for line in result.output.splitlines()
        if line.startswith("TOKEN_ONCE=")
    )
    assert len(token) >= 32
    with app.app_context():
        station = db.session.get(EstacionPesaje, station_id)
        assert station.token_hash == hash_station_token(token)
        assert station.token_hash != token


def test_heartbeat_is_idempotent_and_never_creates_control_peso(
    client,
    app,
    provisioned_station,
):
    payload = _heartbeat_payload(provisioned_station, sequence=48)
    request_validator = _validator(HEARTBEAT_DIR, "request")
    response_validator = _validator(HEARTBEAT_DIR, "response")
    request_validator.validate(payload)

    first = _put_heartbeat(client, provisioned_station, payload)
    replay = _put_heartbeat(client, provisioned_station, payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.get_json() == first.get_json()
    response_validator.validate(first.get_json())

    with app.app_context():
        assert EstacionHeartbeatRecepcion.query.count() == 1
        state = db.session.get(EstacionEstadoActual, provisioned_station)
        assert state.heartbeat_id == payload["heartbeat_id"]
        assert state.sequence == 48
        assert ControlPeso.query.count() == 0

    changed = copy.deepcopy(payload)
    changed["sequence"] = 49
    conflict = _put_heartbeat(client, provisioned_station, changed)
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_token_is_bound_to_one_station(client, app, provisioned_station):
    other_station_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(
            EstacionPesaje(
                station_id=other_station_id,
                codigo="PESAJE-PILOTO-02",
                nombre="Balanza piloto",
                ubicacion="Laboratorio",
                estado_admin="ACTIVA",
                token_hash=hash_station_token("another-station-token-0002"),
            )
        )
        db.session.commit()

    response = _put_heartbeat(
        client,
        other_station_id,
        _heartbeat_payload(other_station_id),
        token=STATION_TOKEN,
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "STATION_ID_MISMATCH"


def test_late_sequence_is_recorded_without_replacing_current_state(
    client,
    app,
    provisioned_station,
):
    boot_id = str(uuid.uuid4())
    current = _heartbeat_payload(provisioned_station, sequence=8, boot_id=boot_id)
    late = _heartbeat_payload(provisioned_station, sequence=7, boot_id=boot_id)
    late["context"]["op"] = "OP-ATRASADA"

    assert _put_heartbeat(client, provisioned_station, current).status_code == 200
    assert _put_heartbeat(client, provisioned_station, late).status_code == 200

    with app.app_context():
        state = db.session.get(EstacionEstadoActual, provisioned_station)
        receipts = EstacionHeartbeatRecepcion.query.order_by(
            EstacionHeartbeatRecepcion.id
        ).all()
        assert state.sequence == 8
        assert json.loads(state.context_json)["op"] != "OP-ATRASADA"
        assert len(receipts) == 2
        assert receipts[0].applied_to_current is True
        assert receipts[1].applied_to_current is False


def test_monitor_is_read_only_and_calculates_recency(
    client,
    provisioned_station,
):
    now = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
    assert classify_communication(None, now=now) == "NUNCA_REPORTO"
    assert (
        classify_communication(now - timedelta(seconds=30), now=now)
        == "RECIENTE"
    )
    assert (
        classify_communication(now - timedelta(seconds=120), now=now)
        == "ATRASADA"
    )
    assert (
        classify_communication(now - timedelta(seconds=301), now=now)
        == "SIN_COMUNICACION"
    )

    before = client.get("/api/monitoring/v1/weighing-stations")
    assert before.status_code == 200
    item = before.get_json()["items"][0]
    assert item["communication_status"] == "NUNCA_REPORTO"
    assert "token_hash" not in item

    heartbeat = _heartbeat_payload(provisioned_station)
    assert _put_heartbeat(client, provisioned_station, heartbeat).status_code == 200

    detail = client.get(
        f"/api/monitoring/v1/weighing-stations/{provisioned_station}"
    )
    assert detail.status_code == 200
    body = detail.get_json()
    assert body["communication_status"] == "RECIENTE"
    assert body["local_summary"]["source"] == "LOCAL_REPORTED_LEGACY"
    assert "token_hash" not in body

    assert (
        client.post(
            f"/api/monitoring/v1/weighing-stations/{provisioned_station}",
            json={"command": "shutdown"},
        ).status_code
        == 405
    )
