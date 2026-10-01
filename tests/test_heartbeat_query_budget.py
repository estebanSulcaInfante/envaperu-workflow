import copy
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models.estacion_pesaje import EstacionPesaje
from app.services.station_auth import hash_station_token
from app.services.station_monitoring import process_heartbeat


@pytest.mark.parametrize("context_bytes", [16, 65536])
def test_heartbeat_receipt_reads_are_small_and_ack_is_idempotent(app, client, context_bytes):
    station_id = str(uuid.uuid4())
    token = "heartbeat-query-budget-local-test-token"
    with app.app_context():
        db.session.add(EstacionPesaje(
            station_id=station_id, codigo="QUERY-BUDGET", nombre="Test", ubicacion="Local",
            estado_admin="ACTIVA", token_hash=hash_station_token(token),
        ))
        db.session.commit()
    payload = json.loads((Path(__file__).resolve().parents[1] / "contracts" /
                          "station-heartbeat-v1" / "examples.json").read_text())['request']
    payload['heartbeat_id'] = str(uuid.uuid4())
    payload['boot_id'] = str(uuid.uuid4())
    payload['context']['op'] = 'x' * context_bytes
    headers = {"X-Station-Version": "1.1.0-pilot", "X-Correlation-Id": str(uuid.uuid4()), "Authorization": f"Bearer {token}", "Idempotency-Key": payload['heartbeat_id']}
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        first = client.put(f'/api/integration/v1/stations/{station_id}/heartbeat', json=payload, headers=headers)
        first_sql = list(statements)
        statements.clear()
        replay = client.put(f'/api/integration/v1/stations/{station_id}/heartbeat', json=payload, headers=headers)
        replay_sql = list(statements)
        changed = copy.deepcopy(payload)
        changed['sequence'] += 1
        conflict = client.put(f'/api/integration/v1/stations/{station_id}/heartbeat', json=changed, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert first.status_code == replay.status_code == 200
    assert first.get_json() == replay.get_json()
    assert conflict.status_code == 409
    receipt_reads = lambda queries: [sql for sql in queries if 'FROM estacion_heartbeat_recepcion' in sql]
    first_reads, replay_reads = receipt_reads(first_sql), receipt_reads(replay_sql)
    print(json.dumps({"context_bytes": context_bytes, "first_selects": len(first_sql),
                      "replay_selects": len(replay_sql), "first_receipt_reads": len(first_reads),
                      "replay_receipt_reads": len(replay_reads), "response_bytes": len(first.data),
                      "receipt_selects_payload": any('payload_json' in sql for sql in first_reads + replay_reads)}))
    assert len(first_reads) == len(replay_reads) == 1
    assert all('payload_json' not in sql for sql in first_reads + replay_reads)


def test_heartbeat_does_not_ack_failed_commit(app, monkeypatch):
    payload = json.loads((Path(__file__).resolve().parents[1] / 'contracts' /
                          'station-heartbeat-v1' / 'examples.json').read_text())['request']
    with app.app_context():
        station = EstacionPesaje(station_id=str(uuid.uuid4()), codigo='FAIL-COMMIT',
                                nombre='Test', ubicacion='Local', estado_admin='ACTIVA', token_hash='test')
        db.session.add(station)
        db.session.commit()
        def fail_commit():
            raise RuntimeError('simulated commit failure')
        monkeypatch.setattr(db.session, 'commit', fail_commit)
        with pytest.raises(RuntimeError, match='simulated commit failure'):
            process_heartbeat(station, payload, payload['heartbeat_id'])
        db.session.rollback()



@pytest.mark.parametrize('changed_payload', [False, True])
def test_heartbeat_concurrent_receipt_preserves_ack_or_conflict(app, monkeypatch, changed_payload):
    from app.services import station_monitoring as service
    payload = json.loads((Path(__file__).resolve().parents[1] / 'contracts' /
                          'station-heartbeat-v1' / 'examples.json').read_text())['request']
    with app.app_context():
        station = EstacionPesaje(station_id=str(uuid.uuid4()), codigo='RACE',
                                nombre='Test', ubicacion='Local', estado_admin='ACTIVA', token_hash='test')
        db.session.add(station)
        db.session.commit()
        expected_ack = process_heartbeat(station, payload, payload['heartbeat_id'])
        query = service._receipt_ack_query
        calls = []
        class MissingReceipt:
            def one_or_none(self):
                return None
        def race_lookup(heartbeat_id):
            calls.append(heartbeat_id)
            return MissingReceipt() if len(calls) == 1 else query(heartbeat_id)
        monkeypatch.setattr(service, '_receipt_ack_query', race_lookup)
        if changed_payload:
            payload['sequence'] += 1
            with pytest.raises(service.HeartbeatIdempotencyConflict):
                process_heartbeat(station, payload, payload['heartbeat_id'])
        else:
            assert process_heartbeat(station, payload, payload['heartbeat_id']) == expected_ack
        assert len(calls) == 2
