from types import SimpleNamespace
from uuid import UUID

import pytest
from flask import Flask, g

from app.extensions import db
from app.api.rutas_scm_assistant import scm_assistant_bp
from app.services import scm_assistant_gateway as gateway

USER = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://', SCM_AUTH_MODE='supabase',
                      SCM_ASSISTANT_ENABLED='true', SCM_ASSISTANT_ACTOR_ID='42', SCM_ASSISTANT_AUTH_USER_ID=USER)
    db.init_app(app)
    @app.before_request
    def existing_verified_auth():
        g.scm_actor = SimpleNamespace(id=42, activo=True, auth_user_id=UUID(USER), tiene_capacidad=lambda _: True)
        g.scm_auth_claims = {'sub': USER}
    app.register_blueprint(scm_assistant_bp, url_prefix='/api/scm/v1')
    return app


def test_default_off_never_reads_data(app):
    app.config['SCM_ASSISTANT_ENABLED'] = ''
    client = app.test_client()
    assert client.get('/api/scm/v1/asistente/estado').json['available'] is False
    result = client.post('/api/scm/v1/asistente/consulta', json={})
    assert result.status_code == 403
    assert result.headers['Cache-Control'] == 'private, no-store'


@pytest.mark.parametrize('key,value', [('SCM_AUTH_MODE','local_actor')])
def test_unverified_or_other_identity_denied(app,key,value):
    app.config[key] = value
    assert app.test_client().post('/api/scm/v1/asistente/consulta', json={}).status_code == 403


def test_actor_header_cannot_change_verified_identity(app):
    app.config['SCM_ASSISTANT_ACTOR_ID'] = '5'
    app.config['SCM_ASSISTANT_PROVIDER'] = 'openclaw'
    result = app.test_client().get('/api/scm/v1/asistente/estado', headers={'X-Actor-Id':'5'}).json
    assert result['available'] is True
    assert result['provider_state'] == 'PROVIDER_NOT_LINKED'


@pytest.mark.parametrize('missing', ['ASISTENTE_PRODUCCION_USAR', 'OT_VER', 'MANGA_PESAJE_VER'])
def test_role_does_not_grant_data_permissions(app, missing):
    @app.before_request
    def restrict_capabilities():
        g.scm_actor.tiene_capacidad = lambda cap: cap != missing
    assert app.test_client().post('/api/scm/v1/asistente/consulta', json={}).status_code == 403


def test_reassigned_role_cannot_use_personal_provider(app, monkeypatch):
    from app.api import rutas_scm_assistant as route
    app.config.update(SCM_ASSISTANT_PROVIDER='openclaw', SCM_ASSISTANT_ACTOR_ID='99')
    monkeypatch.setattr(route, 'narrate', lambda *a: pytest.fail('provider must not run'))
    monkeypatch.setattr(route.DailyQueryService, 'execute', lambda *a, **kw: {
        'groups': [], 'trace_id': 'test', 'date_lima': '2026-10-05', 'totals': {}})
    result = app.test_client().post('/api/scm/v1/asistente/consulta', json={
        'query': 'resumen de produccion', 'intent': 'production_daily_summary'})
    assert result.status_code == 200
    assert result.json['provider']['status'] == 'PROVIDER_NOT_LINKED'


@pytest.mark.parametrize('body', [
    {'query':'drop table','intent':'production_daily_summary'},
    {'query':'resumen de produccion','intent':'shell'},
    {'query':'resumen de produccion','intent':'production_daily_summary','actor_id':5},
    {'query':'resumen de produccion','intent':'production_daily_summary','refresh':'yes'},
])
def test_only_known_shape_and_intent(app,body):
    assert app.test_client().post('/api/scm/v1/asistente/consulta', json=body).status_code == 400


def test_success_preserves_authoritative_data_without_external_call(app,monkeypatch):
    from app.api import rutas_scm_assistant as route
    monkeypatch.setattr(route.DailyQueryService, 'execute', lambda *a,**kw: {
        'intent':'production_daily_summary','date_lima':'2026-10-05','timezone':'America/Lima',
        'groups':[], 'totals':{'effective_kg':'0.000'}, 'trace_id':'test',
        'as_of_utc':'2026-10-06T06:32:41Z','cache_hit':False})
    result=app.test_client().post('/api/scm/v1/asistente/consulta',json={
        'query':'¿Qué tal fue el avance de producción ayer?','intent':'production_daily_summary','date_lima':'2026-10-05'})
    assert result.status_code == 200
    assert result.json['as_of_utc'] == '2026-10-06T06:32:41Z'
    assert result.json['provider'] == {'mode':'deterministic','status':'READY','usage':None}


SUMMARY={'date_lima':'2026-10-05','as_of_utc':'cut','groups':[{'of':'OF1','color':'R','net_kg':'3.000','weighings':1,'weighing_ids':[9]}], 'totals':{'effective_kg':'3.000'}}


def test_gateway_pending_does_not_make_request(monkeypatch):
    monkeypatch.setattr(gateway.requests,'Session',lambda:pytest.fail('network forbidden'))
    assert gateway.narrate(SUMMARY,{'SCM_ASSISTANT_PROVIDER':'openclaw'})['status']=='AUTH_PENDING'


def test_gateway_rejects_unapproved_destination():
    config={'SCM_ASSISTANT_PROVIDER':'openclaw','SCM_OPENCLAW_TOKEN':'test-only','SCM_OPENCLAW_POLICY_CONFIRMED':'true','SCM_OPENCLAW_URL':'https://example.com'}
    assert gateway.narrate(SUMMARY,config)['status']=='CONFIGURATION_REQUIRED'
    config.update(SCM_OPENCLAW_URL='http://gateway.internal:18789',SCM_OPENCLAW_PRIVATE_HOST='gateway.internal')
    assert gateway.narrate(SUMMARY,config)['status']=='CONFIGURATION_REQUIRED'


def test_gateway_minimizes_and_never_executes_tool_response(monkeypatch):
    import json
    captured={}
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_content(self,n):
            yield json.dumps({'choices':[{'finish_reason':'tool_calls','message':{'content':'','tool_calls':[{'function':{'name':'shell'}}]}}]}).encode()
    class Transport:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,url,**kwargs): captured.update(kwargs);return Response()
    monkeypatch.setattr(gateway.requests,'Session',Transport)
    result=gateway.narrate(SUMMARY,{'SCM_ASSISTANT_PROVIDER':'openclaw','SCM_OPENCLAW_TOKEN':'test-only','SCM_OPENCLAW_POLICY_CONFIRMED':'true'})
    assert result['status']=='INVALID_RESPONSE'
    payload=captured['json']
    assert payload['tool_choice']=='none' and 'tools' not in payload and 'user' not in payload
    assert 'weighing_ids' not in payload['messages'][1]['content']
    assert captured['allow_redirects'] is False
    assert captured['verify'] is True


def test_gateway_file_secret_and_ca_verify(tmp_path, monkeypatch):
    import json
    token = tmp_path / 'token'
    token.write_text('fake-test-token\n')
    ca = tmp_path / 'ca.crt'
    ca.write_text('fake-test-ca')
    captured = {}
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_content(self, n):
            yield json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'Resumen'}}]}).encode()
    class Transport:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, **kwargs): captured.update(kwargs); return Response()
    monkeypatch.setattr(gateway.requests, 'Session', Transport)
    config = {'SCM_ASSISTANT_PROVIDER':'openclaw', 'SCM_OPENCLAW_POLICY_CONFIRMED':'true',
              'SCM_OPENCLAW_TOKEN_FILE':str(token), 'SCM_OPENCLAW_CA_FILE':str(ca),
              'SCM_OPENCLAW_URL':'https://openclaw-scm:18789', 'SCM_OPENCLAW_PRIVATE_HOST':'openclaw-scm'}
    assert gateway.narrate(SUMMARY, config)['status'] == 'READY'
    assert captured['verify'] == str(ca)
    assert captured['headers']['Authorization'] == 'Bearer fake-test-token'
    token.unlink()
    monkeypatch.setattr(gateway.requests, 'Session', lambda: pytest.fail('missing secret must deny network'))
    assert gateway.narrate(SUMMARY, config)['status'] == 'CONFIGURATION_REQUIRED'
