import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.extensions import db
from app.models.control_peso import ControlPeso
from app.models.estacion_pesaje import EstacionPesaje
from app.models.legacy_pesaje import (
    EstacionCierreOpLegacy,
    EstacionImportacionPesajeLegacy,
    EstacionImportacionPesajeLegacyChunk,
    EstacionPesajeLegacy,
)
from app.services.station_auth import hash_station_token


pytestmark = pytest.mark.contract

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = WORKSPACE_ROOT / "contracts" / "station-legacy-history-v1"
STATION_TOKEN = "legacy-history-test-token-with-high-entropy-0001"


def _examples():
    return json.loads((CONTRACT_DIR / "examples.json").read_text(encoding="utf-8"))


def _validator(definition):
    schema = json.loads(
        (CONTRACT_DIR / "contract.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    selected_schema = {
        **schema,
        "$ref": f"#/$defs/{definition}",
    }
    return Draft202012Validator(
        selected_schema,
        format_checker=FormatChecker(),
    )


@pytest.fixture
def history_station(app):
    station_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(
            EstacionPesaje(
                station_id=station_id,
                codigo="PESAJE-HISTORIA-01",
                nombre="Balanza historica",
                ubicacion="Produccion",
                estado_admin="ACTIVA",
                token_hash=hash_station_token(STATION_TOKEN),
            )
        )
        db.session.commit()
    return station_id


def _headers(import_id, chunk_index):
    return {
        "Authorization": f"Bearer {STATION_TOKEN}",
        "Idempotency-Key": f"{import_id}:{chunk_index}",
        "X-Station-Version": "1.1.0-pilot",
        "X-Correlation-Id": str(uuid.uuid4()),
    }


def _put(client, station_id, payload):
    return client.put(
        (
            f"/api/integration/v1/stations/{station_id}/legacy-history/"
            f"imports/{payload['import_id']}/chunks/{payload['chunk_index']}"
        ),
        headers=_headers(payload["import_id"], payload["chunk_index"]),
        json=payload,
    )


def test_contract_copies_match_and_examples_validate():
    for repository in ("backend", "modulo-pesaje/backend"):
        copy_dir = WORKSPACE_ROOT / repository / "contracts" / CONTRACT_DIR.name
        for filename in ("contract.schema.json", "examples.json"):
            assert hashlib.sha256(
                (CONTRACT_DIR / filename).read_bytes()
            ).digest() == hashlib.sha256((copy_dir / filename).read_bytes()).digest()

    examples = _examples()
    _validator("request").validate(examples["request"])
    _validator("response").validate(examples["response"])


def test_complete_import_preserves_deleted_rows_closure_and_raw_values(
    client,
    app,
    history_station,
):
    payload = _examples()["request"]

    first = _put(client, history_station, payload)
    replay = _put(client, history_station, payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.get_json() == first.get_json()
    assert first.get_json()["status"] == "COMPLETE"

    with app.app_context():
        imported = db.session.get(
            EstacionImportacionPesajeLegacy,
            payload["import_id"],
        )
        assert imported.status == "COMPLETE"
        assert imported.source_total_rows == 2
        assert EstacionImportacionPesajeLegacyChunk.query.count() == 1
        assert EstacionPesajeLegacy.query.count() == 2
        assert EstacionPesajeLegacy.query.filter_by(is_deleted=True).count() == 1
        assert EstacionCierreOpLegacy.query.one().op_raw == "OP-0069"
        blanco = EstacionPesajeLegacy.query.filter_by(legacy_pesaje_id=1001).one()
        assert blanco.color_raw == "blanco "
        assert blanco.color_normalized == "BLANCO"
        pending = EstacionPesajeLegacy.query.filter_by(legacy_pesaje_id=1002).one()
        assert pending.op_raw == "OP-213"
        assert pending.op_resolution_status == "PENDIENTE_MAPEO"
        assert ControlPeso.query.count() == 0


def test_conflicting_chunk_and_conflicting_legacy_row_are_rejected(
    client,
    app,
    history_station,
):
    payload = _examples()["request"]
    assert _put(client, history_station, payload).status_code == 200

    changed_chunk = copy.deepcopy(payload)
    changed_chunk["rows"][0]["weight_kg"] = "25.500"
    conflict = _put(client, history_station, changed_chunk)
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "IDEMPOTENCY_CONFLICT"

    other_import = copy.deepcopy(payload)
    other_import["import_id"] = str(uuid.uuid4())
    other_import["manifest"]["source_sha256"] = "a" * 64
    other_import["rows"][0]["weight_kg"] = "25.500"
    row_conflict = _put(client, history_station, other_import)
    assert row_conflict.status_code == 409
    assert row_conflict.get_json()["code"] == "LEGACY_ROW_CONFLICT"

    with app.app_context():
        original = EstacionPesajeLegacy.query.filter_by(legacy_pesaje_id=1001).one()
        assert str(original.weight_kg) == "25.125"


def test_order_index_separates_pending_closed_and_deleted_totals(
    client,
    history_station,
):
    payload = _examples()["request"]
    payload["rows"].append(
        {
            "legacy_id": 1003,
            "weight_kg": "10.000",
            "captured_at_local": "2026-07-17 10:00:00",
            "deleted_at_local": None,
            "op": "OP-0069",
            "ot": "025000",
            "mold": "ESCURRIDOR PORTAVAJILLA",
            "color": "VERDE",
            "machine_code": "HT-160A",
            "shift": "DIA",
            "operator": None,
            "raw": {},
        }
    )
    payload["manifest"].update(
        source_total_rows=3,
        source_active_rows=2,
        source_deleted_rows=1,
    )
    assert _put(client, history_station, payload).status_code == 200

    response = client.get("/api/monitoring/v1/legacy-production-orders?per_page=20")
    assert response.status_code == 200
    body = response.get_json()
    assert body["summary"] == {
        "active_bags": 2,
        "active_weight_kg": "35.125",
        "closed_orders": 1,
        "pending_mapping_orders": 1,
        "raw_orders": 3,
    }
    by_op = {item["op_raw"]: item for item in body["items"]}
    assert by_op["OP-0069"]["status"] == "CERRADA_LEGACY"
    assert by_op["OP-0069"]["active_weight_kg"] == "10.000"
    assert by_op["OP-0213"]["status"] == "SIN_CIERRE_LEGACY"
    assert by_op["OP-213"]["status"] == "PENDIENTE_MAPEO"
    assert by_op["OP-213"]["active_bags"] == 0
    assert by_op["OP-213"]["deleted_bags"] == 1

    pending = client.get(
        "/api/monitoring/v1/legacy-production-orders?status=PENDIENTE_MAPEO"
    ).get_json()
    assert [item["op_raw"] for item in pending["items"]] == ["OP-213"]

    detail = client.get(
        "/api/monitoring/v1/legacy-production-orders/detail",
        query_string={"station_id": history_station, "op": "OP-213"},
    )
    assert detail.status_code == 200
    assert detail.get_json()["captures"][0]["is_deleted"] is True
