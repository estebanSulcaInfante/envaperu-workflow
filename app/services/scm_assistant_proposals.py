"""Actor-scoped durable proposals, optimistic versions and exact-diff decisions."""
import hashlib
import unicodedata
from sqlalchemy import select, func
from app.models.scm_assistant_proposal import ScmAssistantProposal as Proposal, ScmAssistantProposalRevision as Revision
from app.services.scm_service_support import ScmServiceError, load_actor


def fail(code, message, status=400):
    raise ScmServiceError(code, message, status_code=status)


def _text(value, name, limit, required=False):
    if not isinstance(value, str) or len(value.encode('utf-8')) > limit or (required and not value.strip()) or '\x00' in value:
        fail('PROPOSAL_INVALID', f'Campo {name} inválido.')
    return value


def _authorize(session, actor_id):
    return load_actor(session, actor_id, capability='ASISTENTE_PRODUCCION_USAR')


def _record(session, proposal, action):
    session.flush()
    session.add(Revision(proposal_id=proposal.id, actor_id=proposal.actor_id,
                         version=proposal.version, action=action, snapshot=proposal.to_dict()))
    session.flush()
    return proposal.to_dict()


def list_proposals(session, *, actor_id):
    _authorize(session, actor_id)
    rows = session.scalars(select(Proposal).where(Proposal.actor_id == actor_id).order_by(Proposal.updated_at.desc()).limit(100)).all()
    return [p.to_dict() for p in rows]


def create_proposal(session, *, actor_id, need):
    _authorize(session, actor_id)
    need = _text(need, 'need', 2000, True).strip()
    normalized = ' '.join(unicodedata.normalize('NFKC', need).casefold().split())
    key = hashlib.sha256(normalized.encode()).hexdigest()
    existing = session.scalar(select(Proposal).where(Proposal.actor_id == actor_id, Proposal.need_key == key))
    if existing:
        return existing.to_dict() | {'deduplicated': True}
    if session.scalar(select(func.count()).select_from(Proposal).where(Proposal.actor_id == actor_id)) >= 100:
        fail('PROPOSAL_LIMIT', 'Hay 100 propuestas; revisa las existentes antes de añadir otra.', 422)
    p = Proposal(actor_id=actor_id, need_key=key, need=need)
    session.add(p)
    return _record(session, p, 'CREATED')


def _get(session, actor_id, proposal_id, version):
    _authorize(session, actor_id)
    p = session.scalar(select(Proposal).where(Proposal.id == proposal_id, Proposal.actor_id == actor_id).with_for_update())
    if p is None:
        fail('PROPOSAL_NOT_FOUND', 'Propuesta no encontrada.', 404)
    if isinstance(version, bool) or not isinstance(version, int) or version != p.version:
        fail('PROPOSAL_VERSION_CONFLICT', 'La propuesta cambió; vuelve a revisar la versión actual.', 409)
    return p


def revise_proposal(session, *, actor_id, proposal_id, data):
    if not isinstance(data, dict) or set(data) - {'expected_version', 'summary', 'diff', 'status'}:
        fail('PROPOSAL_INVALID', 'Campos no admitidos.')
    p = _get(session, actor_id, proposal_id, data.get('expected_version'))
    if len(data) < 2:
        fail('PROPOSAL_INVALID', 'Falta el cambio propuesto.')
    for field, limit in [('summary', 4000), ('diff', 64000)]:
        if field in data:
            setattr(p, field, _text(data[field], field, limit))
    p.diff_sha256 = hashlib.sha256(p.diff.encode('utf-8')).hexdigest() if p.diff.strip() else None
    state = data.get('status', 'EN_PREPARACION')
    if state not in {'PENDIENTE', 'EN_PREPARACION', 'LISTA_REVISION'}:
        fail('PROPOSAL_INVALID_STATE', 'Usa la decisión explícita para aprobar o rechazar.')
    if state == 'LISTA_REVISION' and (not p.diff_sha256 or not p.summary.strip()):
        fail('PROPOSAL_DIFF_REQUIRED', 'La revisión requiere resumen y diff exacto.')
    p.status = state
    p.approved_version = p.approved_sha256 = None
    # Even a textual/no-op resubmission creates a new reviewable revision.
    p.version += 1
    return _record(session, p, 'REVISED')


def decide_proposal(session, *, actor_id, proposal_id, data):
    actor = _authorize(session, actor_id)
    if not actor.tiene_capacidad('AUTORIZACION_SCM_ADMINISTRAR'):
        fail('PROPOSAL_REVIEW_FORBIDDEN', 'La revisión requiere autorización administrativa.', 403)
    if not isinstance(data, dict) or set(data) != {'expected_version', 'diff_sha256', 'decision'}:
        fail('PROPOSAL_INVALID', 'La decisión debe identificar versión y hash del diff.')
    p = _get(session, actor_id, proposal_id, data['expected_version'])
    if p.status != 'LISTA_REVISION' or not p.diff_sha256 or data['diff_sha256'] != p.diff_sha256:
        fail('PROPOSAL_DIFF_CONFLICT', 'El diff no coincide con la revisión pendiente.', 409)
    if data['decision'] not in {'approve', 'reject'}:
        fail('PROPOSAL_INVALID', 'Decisión inválida.')
    if data['decision'] == 'approve':
        p.approved_version, p.approved_sha256 = p.version, p.diff_sha256
        p.status = 'APROBADA'
    else:
        p.approved_version = p.approved_sha256 = None
        p.status = 'RECHAZADA'
    return _record(session, p, data['decision'].upper())


def proposal_history(session, *, actor_id, proposal_id, before_version=None):
    _authorize(session, actor_id)
    p = session.scalar(select(Proposal).where(Proposal.id == proposal_id, Proposal.actor_id == actor_id))
    if p is None:
        fail('PROPOSAL_NOT_FOUND', 'Propuesta no encontrada.', 404)
    statement = select(Revision).where(Revision.proposal_id == proposal_id)
    if before_version is not None:
        if isinstance(before_version,bool) or not isinstance(before_version,int) or before_version <= 0:
            fail('PROPOSAL_INVALID_CURSOR','Cursor de historial inválido.')
        statement = statement.where(Revision.version < before_version)
    rows = session.scalars(statement.order_by(Revision.version.desc()).limit(21)).all()
    has_more = len(rows)>20
    rows = rows[:20]
    return {'items':[{'version':r.version,'action':r.action,'snapshot':r.snapshot,'created_at':r.created_at.isoformat()} for r in rows],
            'has_more':has_more,'next_before_version':rows[-1].version if has_more else None}
