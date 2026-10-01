"""Read-only packaging projection: contract, bounded SQL and legacy compatibility."""
import json

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_empaque import ScmArticuloPerfil, ScmPerfilEmpacable
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_configuration import ensure_initial_scm_configuration

PATH = '/api/scm/v1/empaque/asignaciones'


@pytest.fixture
def actor_id(app):
    with app.app_context():
        ensure_initial_scm_configuration()
        actor = Trabajador.query.filter_by(codigo='TRB-01').one()
        actor.roles.append(RolOperativo.query.filter_by(codigo='INGENIERIA_SCM').one())
        db.session.commit()
        return actor.id


def seed(count=5, *, start=1, default=True, active=True, profile_active=True, article_active=True):
    profile = ScmPerfilEmpacable(codigo=f'PEM-{start:05}', nombre=f'Perfil {start}', descripcion_fisica='Apilado vertical', activo=profile_active)
    db.session.add(profile)
    db.session.flush()
    ids = []
    for index in range(start, start + count):
        article = ScmArticulo(codigo=f'WIP-{index:05}', nombre=f'Artículo {index}', clase='SUBENSAMBLE_WIP', activo=article_active)
        db.session.add(article)
        db.session.flush()
        db.session.add(ScmArticuloPerfil(articulo_id=article.id, perfil_empacable_id=profile.id, es_predeterminado=default, activo=active))
        ids.append(article.id)
    db.session.commit()
    return ids


def get(client, actor_id, **query):
    return client.get(PATH, headers={'X-Actor-Id': str(actor_id)}, query_string=query)


def test_pages_are_keyset_bounded_and_new_rows_before_cursor_do_not_repeat(app, client, actor_id):
    with app.app_context():
        ids = seed(6, start=10)
    first = get(client, actor_id, limite=2)
    assert first.status_code == 200
    page = first.get_json()
    assert page['total'] == 6 and page['has_more']
    assert [row['articulo']['id'] for row in page['items']] == ids[:2]
    with app.app_context():
        seed(1, start=1)
    second = get(client, actor_id, limite=2, cursor=page['next_cursor']).get_json()
    assert [row['articulo']['id'] for row in second['items']] == ids[2:4]
    third = get(client, actor_id, limite=2, cursor=second['next_cursor']).get_json()
    assert [row['articulo']['id'] for row in third['items']] == ids[4:]
    assert third['next_cursor'] is None and not third['has_more']


def test_projection_preserves_default_link_semantics_and_legacy_contract(app, client, actor_id):
    with app.app_context():
        visible = seed(1, start=1, profile_active=False, article_active=False)
        seed(1, start=2, default=False)
        seed(1, start=3, active=False)
        article = ScmArticulo(codigo='WIP-SIN-PERFIL', nombre='Sin perfil', clase='SUBENSAMBLE_WIP')
        db.session.add(article)
        db.session.commit()
    response = get(client, actor_id)
    assert response.status_code == 200
    data = response.get_json()
    assert data['total'] == 1 and len(data['items']) == 1
    row = data['items'][0]
    assert row['articulo']['id'] == visible[0]
    assert row['articulo']['activo'] is False and row['perfil']['activo'] is False
    assert set(row['articulo']) == {'id', 'codigo', 'nombre', 'clase', 'version', 'activo'}
    assert set(row['perfil']) == {'id', 'codigo', 'nombre', 'descripcion_fisica', 'activo'}
    legacy = client.get(f'/api/scm/v1/articulos/{visible[0]}/perfiles-empaque', headers={'X-Actor-Id': str(actor_id)}).get_json()
    assert set(legacy) == {'articulo_id', 'perfiles'}
    assert legacy['perfiles'][0]['perfil']['version'] == 1


def test_search_filters_server_side_and_treats_wildcards_literally(app, client, actor_id):
    with app.app_context():
        ids = seed(3)
        db.session.get(ScmArticulo, ids[1]).nombre = 'Árbol 100%_azul'
        db.session.commit()
    for query in ['100%_', 'Árbol', 'wip-00002']:
        response = get(client, actor_id, q=query, limite=1)
        assert response.status_code == 200
        assert response.get_json()['total'] == 1
        assert response.get_json()['items'][0]['articulo']['id'] == ids[1]
    assert get(client, actor_id, q='inexistente').get_json()['items'] == []


@pytest.mark.parametrize('query', [{'limite': 0}, {'limite': 101}, {'limite': 'abc'}, {'cursor': 'invalid'}, {'q': 'x' * 201}])
def test_rejects_invalid_paging_input(client, actor_id, query):
    response = get(client, actor_id, **query)
    assert response.status_code == 400
    assert response.get_json()['error']['code'] == 'INVALID_PACKAGING_PAGE'


def test_cursor_cannot_be_reused_with_another_query(app, client, actor_id):
    with app.app_context():
        seed(3)
    cursor = get(client, actor_id, limite=1).get_json()['next_cursor']
    assert get(client, actor_id, q='another', cursor=cursor).status_code == 400


def test_requires_actor_and_both_catalog_capabilities(app, client, actor_id):
    assert client.get(PATH).status_code == 400
    with app.app_context():
        denied = Trabajador(codigo='NO-CAP', nombres='Sin', apellidos='Permiso', activo=True)
        db.session.add(denied)
        db.session.commit()
        denied_id = denied.id
    assert get(client, denied_id).status_code == 403
    assert get(client, actor_id).status_code == 200


def test_data_query_count_and_payload_are_bounded_by_page(app, client, actor_id):
    with app.app_context():
        seed(110)
        statements = []
        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith('SELECT'):
                statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', capture)
        try:
            response = get(client, actor_id, limite=5)
        finally:
            event.remove(db.engine, 'before_cursor_execute', capture)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['total'] == 110 and len(payload['items']) == 5
    projection_selects = [sql for sql in statements if 'scm_articulo_perfil' in sql]
    assert len(projection_selects) == 2
    assert any('LIMIT' in sql.upper() for sql in projection_selects)
    assert all(' OFFSET ' not in sql.upper() or 'OFFSET ?' in sql for sql in projection_selects)
    assert len(json.dumps(payload).encode()) < 5000
@pytest.mark.parametrize('capability', ['ARTICULO_VER', 'EMPAQUE_VER'])
def test_one_capability_alone_does_not_expose_projection(app, client, actor_id, capability):
    from app.models.scm_catalogos import ScmCapacidad
    with app.app_context():
        role = RolOperativo(codigo='ONLY-ONE', nombre='Single read permission', capacidades=[ScmCapacidad.query.filter_by(codigo=capability).one()])
        actor = Trabajador(codigo='ONE-READER', nombres='One', apellidos='Reader', activo=True, roles=[role])
        db.session.add(actor)
        db.session.commit()
        reader_id = actor.id
    response = get(client, reader_id)
    assert response.status_code == 403
    assert response.get_json()['error']['code'] == 'CAPABILITY_REQUIRED'


def test_inactive_actor_and_cross_actor_cursor_are_rejected(app, client, actor_id):
    with app.app_context():
        seed(3)
        role = RolOperativo.query.filter_by(codigo='INGENIERIA_SCM').one()
        second = Trabajador(codigo='SECOND-READER', nombres='Another', apellidos='Reader', activo=True, roles=[role])
        db.session.add(second)
        db.session.commit()
        second_id = second.id
    cursor = get(client, actor_id, limite=1).get_json()['next_cursor']
    assert get(client, second_id, cursor=cursor).status_code == 400
    with app.app_context():
        db.session.get(Trabajador, second_id).activo = False
        db.session.commit()
    assert get(client, second_id).status_code == 403


def test_max_page_and_unicode_cursor_round_trip(app, client, actor_id):
    with app.app_context():
        ids = seed(102)
        db.session.get(ScmArticulo, ids[-1]).codigo = 'WIP-ÁRBOL'
        db.session.commit()
    first = get(client, actor_id, limite=100).get_json()
    assert len(first['items']) == 100 and first['has_more']
    last = get(client, actor_id, limite=100, cursor=first['next_cursor']).get_json()
    assert len(last['items']) == 2 and not last['has_more']
    assert set(item['articulo']['id'] for item in first['items']).isdisjoint(item['articulo']['id'] for item in last['items'])
    assert get(client, actor_id, q='ÁRBOL').get_json()['total'] == 1

@pytest.mark.parametrize('size', [5, 110])
def test_measured_legacy_fanout_vs_paged_projection(app, client, actor_id, size, tmp_path):
    from time import perf_counter
    from sqlalchemy.orm import Session
    with app.app_context():
        ids = seed(size)
        sql_count = [0]
        relationship_count = [0]
        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            sql_count[0] += statement.lstrip().upper().startswith('SELECT')
        def capture_orm(state):
            relationship_count[0] += int(state.is_relationship_load)
        event.listen(db.engine, 'before_cursor_execute', capture)
        event.listen(Session, 'do_orm_execute', capture_orm)
        try:
            measurements = {}
            for mode in ('legacy', 'paged'):
                sql_count[0] = relationship_count[0] = 0
                elapsed_start = perf_counter()
                requests = response_bytes = rows = 0
                if mode == 'legacy':
                    for article_id in ids:
                        db.session.remove()
                        response = client.get(f'/api/scm/v1/articulos/{article_id}/perfiles-empaque', headers={'X-Actor-Id': str(actor_id)})
                        assert response.status_code == 200
                        requests += 1
                        response_bytes += len(response.data)
                        rows += len(response.get_json()['perfiles'])
                else:
                    cursor = None
                    while True:
                        db.session.remove()
                        response = get(client, actor_id, limite=25, **({'cursor': cursor} if cursor else {}))
                        assert response.status_code == 200
                        payload = response.get_json()
                        requests += 1
                        response_bytes += len(response.data)
                        rows += len(payload['items'])
                        cursor = payload['next_cursor']
                        if not cursor:
                            break
                measurements[mode] = {'http_requests': requests, 'sql_selects': sql_count[0], 'relationship_selects': relationship_count[0], 'json_uncompressed_bytes': response_bytes, 'returned_rows': rows, 'elapsed_ms_instrumented_sqlite': round((perf_counter() - elapsed_start) * 1000, 2)}
        finally:
            event.remove(db.engine, 'before_cursor_execute', capture)
            event.remove(Session, 'do_orm_execute', capture_orm)
    assert measurements['legacy']['returned_rows'] == measurements['paged']['returned_rows'] == size
    assert measurements['paged']['sql_selects'] < measurements['legacy']['sql_selects']
    assert measurements['paged']['relationship_selects'] == 0
    print(json.dumps({'size': size, 'postgres_to_backend_wire_bytes': None, 'measurements': measurements}))
