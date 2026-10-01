import copy
import json
import uuid
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models.estacion_pesaje import EstacionPesaje, EstacionAvanceProduccion
from app.services.station_auth import hash_station_token

TOKEN = 'sync-check-local-token'


@pytest.fixture
def sync_station(app):
    station_id = str(uuid.uuid4())
    db.session.add(EstacionPesaje(station_id=station_id, codigo='SYNC-CHECK', nombre='Test',
                                ubicacion='Local', estado_admin='ACTIVA', token_hash=hash_station_token(TOKEN)))
    db.session.commit()
    return station_id


def headers():
    return {'Authorization': f'Bearer {TOKEN}', 'X-Station-Version': 'test',
            'X-Correlation-Id': str(uuid.uuid4())}


def progress():
    return json.loads((Path(__file__).resolve().parents[1] / 'contracts' /
                       'station-production-progress-v1/examples.json').read_text())['request']


def send(client, station_id, payload):
    return client.put(f'/api/integration/v1/stations/{station_id}/production-progress',
                      json=payload, headers={**headers(), 'Idempotency-Key': payload['report_id']})


def check(client, station_id, payload, **extra):
    return client.post(f'/api/integration/v1/stations/{station_id}/monitoring/sync-check',
                       headers=headers(), json={'contract_version': 'station-monitoring-sync-v1',
                       'window_start_date': payload['window_start_date'],
                       'window_end_date': payload['window_end_date'], 'include_legacy': False, **extra})


def test_check_uses_materialized_state_not_historic_receipt(app, client, sync_station):
    payload = progress()
    assert send(client, sync_station, payload).status_code == 200
    before = check(client, sync_station, payload, pending_report_id=payload['report_id'])
    assert before.status_code == 200
    assert before.json['pending_report_received'] is True
    db.session.query(EstacionAvanceProduccion).filter_by(station_id=sync_station).delete()
    db.session.commit()
    lost = check(client, sync_station, payload, pending_report_id=payload['report_id'])
    assert lost.json['progress_fingerprint'] != before.json['progress_fingerprint']
    assert lost.json['pending_report_received'] is True
    payload['report_id'] = str(uuid.uuid4())
    assert send(client, sync_station, payload).status_code == 200
    assert check(client, sync_station, payload).json['progress_fingerprint'] == before.json['progress_fingerprint']


def test_check_auth_and_window_validation(client, sync_station):
    payload = progress()
    assert client.post(f'/api/integration/v1/stations/{sync_station}/monitoring/sync-check', json={}).status_code == 401
    assert check(client, str(uuid.uuid4()), payload).status_code == 403
    assert check(client, sync_station, payload, window_start_date='2000-01-01').status_code == 422


@pytest.mark.parametrize('row_count', [2, 100])
def test_check_query_budget_and_payload_independent_of_rows(app, client, sync_station, row_count):
    payload = progress()
    template = payload['rows'][0]
    payload['rows'] = [{**template, 'op': f'OP-{i}'} for i in range(row_count)]
    payload['report_id'] = str(uuid.uuid4())
    assert send(client, sync_station, payload).status_code == 200
    sql = []
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            sql.append(statement)
    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        started = perf_counter()
        response = check(client, sync_station, payload)
        elapsed_ms = (perf_counter() - started) * 1000
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)
    assert response.status_code == 200
    assert len(sql) == 2
    assert all('payload_json' not in query for query in sql)
    print(json.dumps({'rows_scanned': row_count, 'selects': len(sql), 'response_bytes': len(response.data), 'local_request_ms': round(elapsed_ms, 3), 'full_report_json_bytes': len(json.dumps(payload, separators=(',', ':')).encode())}))
    assert len(response.data) < 700


def test_fingerprint_fixture_matches_materialized_and_canonical_forms(app, client, sync_station):
    payload = progress()
    assert send(client, sync_station, payload).status_code == 200
    response = check(client, sync_station, payload)
    assert response.status_code == 200
    from app.services.monitoring_sync_fingerprint import progress_fingerprint, closure_fingerprint
    assert response.json['progress_fingerprint'] == progress_fingerprint(payload)
    variants = copy.deepcopy(payload)
    variants['rows'].reverse()
    for row in variants['rows']:
        row['weight_kg'] += '0'
        row['op'] = '  ' + row['op'].lower() + ' '
        row['first_capture_at_utc'] = row['first_capture_at_utc'].replace('+00:00', 'Z')
    assert progress_fingerprint(variants) == progress_fingerprint(payload)
    assert closure_fingerprint([]) != closure_fingerprint([{'op': 'OP-A', 'mold': None,
        'reason': None, 'closed_at_local': '2026-10-01 10:00:00'}])


def _complete_import(station_id):
    from app.models.legacy_pesaje import EstacionImportacionPesajeLegacy
    record = EstacionImportacionPesajeLegacy(import_id=str(uuid.uuid4()), station_id=station_id,
        source_sha256='a' * 64, source_size_bytes=0, source_schema_version=1,
        source_total_rows=0, source_active_rows=0, source_deleted_rows=0,
        manifest_json='{}', total_chunks=1, chunks_received=1, status='COMPLETE')
    db.session.add(record)
    db.session.commit()
    return record.import_id


def test_closure_actual_state_reopen_and_a_b_a_without_new_captures(app, client, sync_station):
    payload = progress()
    _complete_import(sync_station)
    closure = {'op': 'OP-1', 'mold': None, 'reason': 'audit', 'closed_at_local': '2026-07-17 10:00:00'}
    states = []
    for closures in ([closure], [], [closure]):
        batch = {'contract_version': 'station-legacy-continuity-v1', 'batch_id': str(uuid.uuid4()),
                 'rows': [], 'closures': closures}
        response = client.put(f"/api/integration/v1/stations/{sync_station}/legacy-history/deltas/{batch['batch_id']}",
            headers={**headers(), 'Idempotency-Key': batch['batch_id']}, json=batch)
        assert response.status_code == 200
        state = check(client, sync_station, payload, include_legacy=True, pending_batch_id=batch['batch_id'])
        assert state.status_code == 200
        assert state.json['pending_batch_received'] is True
        assert state.json['legacy_state']['high_watermark'] == 0
        states.append(state.json['closures_fingerprint'])
    assert states[0] == states[2] != states[1]


def test_legacy_check_projects_five_queries_no_bodies(app, client, sync_station):
    _complete_import(sync_station)
    sql = []
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            sql.append(statement)
    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        response = check(client, sync_station, progress(), include_legacy=True)
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)
    assert response.status_code == 200
    assert len(sql) == 5
    assert all('payload_json' not in item and 'manifest_json' not in item for item in sql)
    print(json.dumps({'legacy_selects': len(sql), 'response_bytes': len(response.data)}))


def test_other_station_receipt_and_rows_are_not_visible(app, client, sync_station):
    other_id = str(uuid.uuid4())
    other_token = 'other-station-token'
    db.session.add(EstacionPesaje(station_id=other_id, codigo='OTHER', nombre='Other', ubicacion='Local',
                                estado_admin='ACTIVA', token_hash=hash_station_token(other_token)))
    db.session.commit()
    payload = progress()
    other_headers = {**headers(), 'Authorization': f'Bearer {other_token}', 'Idempotency-Key': payload['report_id']}
    assert client.put(f'/api/integration/v1/stations/{other_id}/production-progress',
                      headers=other_headers, json=payload).status_code == 200
    response = check(client, sync_station, payload, pending_report_id=payload['report_id'])
    assert response.status_code == 200
    assert response.json['pending_report_received'] is False
    from app.services.monitoring_sync_fingerprint import progress_fingerprint
    assert response.json['progress_fingerprint'] == progress_fingerprint({**payload, 'rows': []})


def test_shared_contract_examples_and_hashes():
    from jsonschema import Draft202012Validator, FormatChecker
    from app.services.monitoring_sync_fingerprint import progress_fingerprint, closure_fingerprint
    contract = Path(__file__).resolve().parents[1] / 'contracts/station-monitoring-sync-v1'
    examples = json.loads((contract / 'examples.json').read_text())
    schema = json.loads((contract / 'contract.schema.json').read_text())
    for definition in ('request', 'response'):
        Draft202012Validator(schema['$defs'][definition], format_checker=FormatChecker()).validate(examples[definition])
    fixture = examples['fingerprint_fixture']
    assert progress_fingerprint(fixture['progress']) == fixture['progress_fingerprint']
    assert closure_fingerprint(fixture['closures']) == fixture['closures_fingerprint']


def test_oversized_materialized_state_requires_full_snapshot_not_truncated_hash(app, client, sync_station):
    payload = progress()
    assert send(client, sync_station, payload).status_code == 200
    sample = EstacionAvanceProduccion.query.filter_by(station_id=sync_station).first()
    values = {column.name: getattr(sample, column.name) for column in EstacionAvanceProduccion.__table__.columns if column.name != 'id'}
    db.session.execute(EstacionAvanceProduccion.__table__.insert(),
                       [{**values, 'group_key': f'extra-{index}', 'op': f'OP-{index}'} for index in range(5000)])
    db.session.commit()
    response = check(client, sync_station, payload)
    assert response.status_code == 200
    assert response.json['progress_fingerprint'] is None
