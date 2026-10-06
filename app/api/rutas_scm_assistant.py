"""Private experimental assistant. Identity is server verified; default denied."""
import os
import unicodedata
from time import perf_counter
from uuid import UUID
from uuid import uuid4

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.extensions import db
from app.services.scm_daily_query_service import (
    DailyQueryService, DailySummaryCache, DailyQueryLog, DailyLimits,
    LocalProductionDailyAdapter, DailyQueryError,
)
from app.services.scm_assistant_gateway import narrate
from app.services.scm_service_support import ScmServiceError

scm_assistant_bp = Blueprint('scm_assistant', __name__)
INTENT = 'production_daily_summary'


def settings():
    keys = ('SCM_ASSISTANT_ENABLED', 'SCM_ASSISTANT_ACTOR_ID', 'SCM_ASSISTANT_AUTH_USER_ID',
            'SCM_ASSISTANT_PROVIDER', 'SCM_OPENCLAW_URL', 'SCM_OPENCLAW_TOKEN',
            'SCM_OPENCLAW_POLICY_CONFIRMED', 'SCM_OPENCLAW_PRIVATE_HOST',
            'SCM_OPENCLAW_TOKEN_FILE', 'SCM_OPENCLAW_CA_FILE',
            'SCM_ASSISTANT_V2_ENABLED', 'SCM_ASSISTANT_V2_MODEL_CALLS_ENABLED',
            'SCM_OPENCLAW_BACKEND_MODEL', 'SCM_OPENCLAW_MODEL_VERIFIED')
    return {k: current_app.config.get(k, os.environ.get(k, '')) for k in keys}


def identity(config):
    if str(config['SCM_ASSISTANT_ENABLED']).lower() != 'true':
        raise DailyQueryError('ASSISTANT_DISABLED', 'La prueba personal aún no está habilitada.', status_code=403)
    actor = getattr(g, 'scm_actor', None)
    claims = getattr(g, 'scm_auth_claims', {})
    try:
        actual_user = UUID(str(claims.get('sub')))
        actor_user = UUID(str(actor.auth_user_id)) if actor else None
    except (ValueError, TypeError, AttributeError):
        raise DailyQueryError('ASSISTANT_IDENTITY_PENDING', 'Falta verificar y vincular la identidad personal SCM.', status_code=403)
    if current_app.config.get('SCM_AUTH_MODE') != 'supabase' or actor is None or not actor.activo or actual_user != actor_user:
        raise DailyQueryError('ASSISTANT_NOT_AUTHORIZED', 'Esta prueba solo está disponible para la identidad autorizada.', status_code=403)
    for capability in ('ASISTENTE_PRODUCCION_USAR', 'OT_VER', 'MANGA_PESAJE_VER'):
        if not actor.tiene_capacidad(capability):
            raise DailyQueryError('CAPABILITY_REQUIRED', 'La consulta requiere permisos de lectura de producción y pesajes.', status_code=403)
    return actor.id


def provider_linked(config, actor_id):
    """A transferable capability never transfers a personal provider session."""
    try:
        return (actor_id == int(config['SCM_ASSISTANT_ACTOR_ID']) and
                UUID(str(g.scm_auth_claims.get('sub'))) == UUID(str(config['SCM_ASSISTANT_AUTH_USER_ID'])))
    except (ValueError, TypeError, KeyError):
        return False


@scm_assistant_bp.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@scm_assistant_bp.errorhandler(ScmServiceError)
def service_error(error):
    return jsonify(error=error.to_dict()), error.status_code


@scm_assistant_bp.get('/asistente/estado')
def status():
    config = settings()
    try:
        actor_id = identity(config)
    except ScmServiceError as error:
        return jsonify(available=False, auth_state='AUTH_PENDING', message=error.message,
                       provider=config.get('SCM_ASSISTANT_PROVIDER') or 'deterministic')
    if config.get('SCM_ASSISTANT_PROVIDER') == 'openclaw' and not provider_linked(config, actor_id):
        return jsonify(available=True, auth_state='READY', scm_state='READY',
                       provider_state='PROVIDER_NOT_LINKED', provider='openclaw',
                       message='Rol habilitado. Falta vincular tu propio proveedor; puedes consultar el resumen de solo lectura.')
    provider_ready = (config.get('SCM_ASSISTANT_PROVIDER') != 'openclaw' or
                      (bool(config.get('SCM_OPENCLAW_TOKEN') or config.get('SCM_OPENCLAW_TOKEN_FILE')) and str(config.get('SCM_OPENCLAW_POLICY_CONFIRMED')).lower() == 'true'))
    return jsonify(available=True, auth_state='READY', scm_state='READY',
                   provider_state='CONFIGURED_NOT_VERIFIED' if provider_ready and config.get('SCM_ASSISTANT_PROVIDER') == 'openclaw' else ('READY' if provider_ready else 'AUTH_PENDING'),
                   message='Consulta personal de solo lectura.',
                   provider=config.get('SCM_ASSISTANT_PROVIDER') or 'deterministic')


def _query_pattern(value):
    if not isinstance(value, str) or len(value) > 300:
        return None
    normalized = ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))
    normalized = ' '.join(''.join(c if c.isalnum() or c.isspace() else ' ' for c in normalized).split())
    accepted = {'que tal fue el avance de produccion ayer', 'resumen de produccion',
                'resumen de produccion ayer', 'avance de produccion', 'avance de produccion ayer'}
    return normalized if normalized in accepted else None


@scm_assistant_bp.post('/asistente/consulta')
def query():
    started = perf_counter()
    config = settings()
    actor_id = identity(config)
    if request.content_length is None or request.content_length > 4096:
        raise DailyQueryError('ASSISTANT_PAYLOAD_LIMIT', 'La consulta supera el tamaño permitido.', status_code=413)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) - {'query', 'date_lima', 'intent', 'refresh'}:
        raise DailyQueryError('ASSISTANT_INVALID_REQUEST', 'La consulta tiene campos no admitidos.', status_code=400)
    pattern = _query_pattern(body.get('query'))
    if not pattern or body.get('intent') != INTENT or not isinstance(body.get('refresh', False), bool):
        raise DailyQueryError('INTENT_NOT_ALLOWED', 'Por ahora puedes consultar el resumen de producción de un día.', status_code=400)
    state = current_app.extensions.setdefault('scm_assistant_state', {
        'cache': DailySummaryCache(), 'audit': DailyQueryLog(), 'patterns': DailyQueryLog(),
    })
    if body.get('refresh'):
        state['cache'].invalidate(actor_id=actor_id)
    # Dedicated session: the authentication middleware has already queried its own.
    with Session(db.engine) as session:
        adapter = LocalProductionDailyAdapter(session, config={})
        service = DailyQueryService(adapter, cache=state['cache'], audit=state['audit'])
        result = service.execute(intent=INTENT, actor_id=actor_id, date_value=body.get('date_lima', 'ayer'))
        session.rollback()
    # Preserve the data cutoff on cache hits; response time is not source time.
    result.setdefault('as_of_utc', None)
    for group in result.get('groups', []):
        group.setdefault('net_kg', group.get('effective_kg'))
    result['cache'] = {'hit': result.get('cache_hit', False), 'ttl_seconds': DailyLimits().ttl_seconds}
    result['limitations'] = [
        'Pesajes efectivos SCM central por fecha de captura en Lima; no equivalen a toda la producción fabricada.',
        'Anulados separados. No se suman legacy, etiquetas, controles abiertos ni cierres sin pesaje.',
        'No hay meta diaria comparable. El avance acumulado de OF se consulta por separado.',
    ]
    result['provider'] = ({'mode': 'openclaw', 'status': 'PROVIDER_NOT_LINKED', 'usage': None}
                          if config.get('SCM_ASSISTANT_PROVIDER') == 'openclaw' and not provider_linked(config, actor_id)
                          else narrate(result, config))
    state['patterns'].append({'actor_id': actor_id, 'query': pattern, 'intent': INTENT,
                              'trace_id': result['trace_id'], 'date_lima': result['date_lima'],
                              'latency_ms': round((perf_counter() - started) * 1000, 3),
                              'usage': result['provider'].get('usage')})
    return jsonify(result)


def v2_identity():
    config = settings()
    actor_id = identity(config)
    if str(config.get('SCM_ASSISTANT_V2_ENABLED')).lower() != 'true':
        raise DailyQueryError('ASSISTANT_V2_DISABLED', 'La ampliación todavía no está habilitada.', status_code=403)
    return config, actor_id


def bounded_body(limit=4096):
    if request.content_length is None or request.content_length > limit:
        raise DailyQueryError('ASSISTANT_PAYLOAD_LIMIT', 'La solicitud supera el límite.', status_code=413)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise DailyQueryError('ASSISTANT_INVALID_REQUEST', 'Se requiere un objeto JSON.', status_code=400)
    return body


@scm_assistant_bp.get('/asistente/catalogo')
def catalogue():
    v2_identity()
    from app.services.scm_assistant_catalogue import get_catalogue
    return jsonify(get_catalogue())


@scm_assistant_bp.post('/asistente/chat')
def chat():
    config, actor_id = v2_identity()
    body = bounded_body()
    if set(body) - {'query', 'refresh'} or not isinstance(body.get('query'), str) or len(body['query']) > 1000 or not isinstance(body.get('refresh', False), bool):
        raise DailyQueryError('ASSISTANT_INVALID_REQUEST', 'Consulta inválida.', status_code=400)
    from app.services.scm_assistant_catalogue import plan_query, execute_plan, get_catalogue, CatalogueQueryError
    started = perf_counter()
    state = current_app.extensions.setdefault('scm_assistant_v2_state', {'cache':DailySummaryCache(), 'audit':DailyQueryLog()})
    planned = plan_query(body['query'])
    result = dict(planned)
    result.update(trace_id=uuid4().hex, results=[], suggestions=get_catalogue().get('suggestions', []))
    # Deliberately no model call: catalogue routing is deterministic in this candidate.
    # This flag is independent of the deployed daily narration policy.
    result['execution_mode'] = 'deterministic_catalogue'
    mode = config.get('SCM_ASSISTANT_PROVIDER') or 'deterministic'
    result['provider'] = {'mode':mode, 'status':'ACTIVATION_PENDING' if mode=='openclaw' else 'READY', 'usage':None,
                          'message':'Respuesta calculada por servicios SCM. La sesión del modelo no se comprueba ni utiliza en esta consulta.'}
    if mode=='openclaw' and not provider_linked(config, actor_id):
        result['provider']['status'] = 'PROVIDER_NOT_LINKED'
    if planned.get('status') == 'answered':
        with Session(db.engine) as session:
            try:
                result['results'] = execute_plan(session, actor_id=actor_id, plan=planned['plan'], cache=state['cache'], refresh=body.get('refresh',False))
            except CatalogueQueryError as error:
                if error.code not in {'MANGA_AMBIGUOUS', 'OF_AMBIGUOUS'}:
                    raise
                result.update(status='needs_clarification', message=error.message, plan=[], results=[],
                              clarification={'question':error.message, 'fields':[error.details.get('field','identifier')],
                                             'choices':error.details.get('choices',[])[:10]})
            finally:
                session.rollback()
    state['audit'].append({'actor_id':actor_id, 'trace_id':result['trace_id'], 'query':body['query'],
                           'status':result['status'], 'plan':result['plan'],
                           'latency_ms':round((perf_counter()-started)*1000,3), 'usage':None})
    return jsonify(result)


def proposal_request(action, proposal_id=None):
    config, actor_id = v2_identity()
    from app.services import scm_assistant_proposals as proposals
    if action == 'decision' and not provider_linked(config, actor_id):
        raise DailyQueryError('PROPOSAL_OWNER_REQUIRED', 'La revisión humana requiere al propietario autorizado.', status_code=403)
    with Session(db.engine) as session:
        try:
            if action == 'list':
                return jsonify(items=proposals.list_proposals(session, actor_id=actor_id), execution_supported=False,
                               can_review=provider_linked(config, actor_id) and g.scm_actor.tiene_capacidad('AUTORIZACION_SCM_ADMINISTRAR'))
            if action == 'history':
                cursor = request.args.get('before_version')
                if cursor is not None and not cursor.isdecimal():
                    raise DailyQueryError('PROPOSAL_INVALID_CURSOR','Cursor de historial inválido.',status_code=400)
                return jsonify(proposals.proposal_history(session, actor_id=actor_id, proposal_id=proposal_id, before_version=int(cursor) if cursor else None))
            body = bounded_body(70000)
            if action == 'create':
                if set(body) != {'need'}:
                    raise DailyQueryError('PROPOSAL_INVALID','Solo se acepta la necesidad.',status_code=400)
                result = proposals.create_proposal(session, actor_id=actor_id, need=body['need'])
            elif action == 'revise':
                result = proposals.revise_proposal(session, actor_id=actor_id, proposal_id=proposal_id, data=body)
            else:
                result = proposals.decide_proposal(session, actor_id=actor_id, proposal_id=proposal_id, data=body)
            session.commit()
            return jsonify(result)
        except (IntegrityError, StaleDataError):
            session.rollback()
            raise DailyQueryError('PROPOSAL_VERSION_CONFLICT','La propuesta cambió; vuelve a cargarla.',status_code=409)
        finally:
            session.rollback()


@scm_assistant_bp.route('/asistente/propuestas', methods=['GET','POST'])
def proposal_collection():
    return proposal_request('list' if request.method == 'GET' else 'create')


@scm_assistant_bp.patch('/asistente/propuestas/<proposal_id>')
def proposal_revision(proposal_id):
    return proposal_request('revise', proposal_id)


@scm_assistant_bp.post('/asistente/propuestas/<proposal_id>/decision')
def proposal_decision(proposal_id):
    return proposal_request('decision', proposal_id)


@scm_assistant_bp.get('/asistente/propuestas/<proposal_id>/historial')
def proposal_versions(proposal_id):
    return proposal_request('history', proposal_id)
