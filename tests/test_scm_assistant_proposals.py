from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models.scm_assistant_proposal import ScmAssistantProposal as Proposal, ScmAssistantProposalRevision as Revision
from app.services import scm_assistant_proposals as service
from app.services.scm_service_support import ScmServiceError


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'proposals.db'))
    Proposal.__table__.create(engine)
    Revision.__table__.create(engine)
    monkeypatch.setattr(service, 'load_actor', lambda *a, **kw: SimpleNamespace(tiene_capacidad=lambda c: True))
    yield engine
    engine.dispose()


def test_durable_dedup_actor_separation(store):
    with Session(store) as s:
        first = service.create_proposal(s, actor_id=1, need='Consultar avance por OF'); s.commit()
    with Session(store) as s:
        again = service.create_proposal(s, actor_id=1, need='  CONSULTAR   avance por OF ')
        other = service.create_proposal(s, actor_id=2, need='Consultar avance por OF'); s.commit()
        assert first['id'] == again['id'] != other['id']
        assert len(service.list_proposals(s, actor_id=1)) == 1
        assert again['execution_supported'] is False
        with pytest.raises(ScmServiceError) as error:
            service.revise_proposal(s, actor_id=2, proposal_id=first['id'], data={'expected_version':1,'summary':'x'})
        assert error.value.status_code == 404


def test_exact_diff_approval_and_revisions_invalidate(store):
    with Session(store) as s:
        p = service.create_proposal(s, actor_id=1, need='Necesidad'); s.commit()
        p = service.revise_proposal(s, actor_id=1, proposal_id=p['id'], data={'expected_version':1,'summary':'Cambio manual','diff':'--- a/x\n+++ b/x\n+texto\n','status':'LISTA_REVISION'}); s.commit()
        assert p['version'] == 2
        with pytest.raises(ScmServiceError):
            service.decide_proposal(s, actor_id=1, proposal_id=p['id'], data={'expected_version':2,'diff_sha256':'wrong','decision':'approve'})
        p = service.decide_proposal(s, actor_id=1, proposal_id=p['id'], data={'expected_version':2,'diff_sha256':p['diff_sha256'],'decision':'approve'}); s.commit()
        assert p['status']=='APROBADA' and p['approved_version']==2
        approved_hash=p['approved_sha256']
        p = service.revise_proposal(s, actor_id=1, proposal_id=p['id'], data={'expected_version':p['version'],'diff':'--- a/x\n+++ b/x\n+otro\n'}); s.commit()
        assert p['status']=='EN_PREPARACION' and p['approved_sha256'] is None
        history=service.proposal_history(s, actor_id=1, proposal_id=p['id'])['items']
        assert len(history)==4 and history[1]['snapshot']['approved_sha256']==approved_hash


def test_cannot_approve_backlog_or_bypass_state(store):
    with Session(store) as s:
        p=service.create_proposal(s,actor_id=1,need='Necesidad');s.commit()
        for data in [{'expected_version':1,'status':'APROBADA'}, {'expected_version':1,'status':'LISTA_REVISION'}, {'expected_version':False,'summary':'x'}]:
            with pytest.raises(ScmServiceError):service.revise_proposal(s,actor_id=1,proposal_id=p['id'],data=data)
            s.rollback()
        with pytest.raises(ScmServiceError):
            service.decide_proposal(s,actor_id=1,proposal_id=p['id'],data={'expected_version':1,'diff_sha256':None,'decision':'approve'})


def test_stale_version_cannot_overwrite(store):
    with Session(store) as s:
        p=service.create_proposal(s,actor_id=1,need='Necesidad');s.commit()
        service.revise_proposal(s,actor_id=1,proposal_id=p['id'],data={'expected_version':1,'summary':'nuevo'});s.commit()
        with pytest.raises(ScmServiceError) as error:
            service.revise_proposal(s,actor_id=1,proposal_id=p['id'],data={'expected_version':1,'summary':'viejo'})
        assert error.value.status_code==409


def test_two_sessions_cannot_approve_stale_diff(store):
    from sqlalchemy.orm.exc import StaleDataError
    with Session(store) as s:
        p=service.create_proposal(s,actor_id=1,need='Concurrente');s.commit()
        p=service.revise_proposal(s,actor_id=1,proposal_id=p['id'],data={'expected_version':1,'summary':'S','diff':'diff original','status':'LISTA_REVISION'});s.commit()
    with Session(store) as one, Session(store) as two:
        old=two.get(Proposal,p['id'])
        assert old.version==2
        service.revise_proposal(one,actor_id=1,proposal_id=p['id'],data={'expected_version':2,'diff':'diff cambiado'});one.commit()
        with pytest.raises((ScmServiceError,StaleDataError)):
            service.decide_proposal(two,actor_id=1,proposal_id=p['id'],data={'expected_version':2,'diff_sha256':p['diff_sha256'],'decision':'approve'})
            two.commit()
        two.rollback()
    with Session(store) as s:
        current=s.get(Proposal,p['id'])
        assert current.diff=='diff cambiado' and current.approved_sha256 is None


def test_role_alone_cannot_approve(store,monkeypatch):
    with Session(store) as s:
        p=service.create_proposal(s,actor_id=1,need='Permiso');s.commit()
        monkeypatch.setattr(service,'load_actor',lambda *a,**kw:SimpleNamespace(tiene_capacidad=lambda c:False))
        with pytest.raises(ScmServiceError) as error:
            service.decide_proposal(s,actor_id=1,proposal_id=p['id'],data={})
        assert error.value.status_code==403


def test_history_pages_newest_first_without_losing_revisions(store):
    with Session(store) as s:
        p = service.create_proposal(s, actor_id=1, need='Historial largo')
        s.commit()
        for index in range(24):
            p = service.revise_proposal(s, actor_id=1, proposal_id=p['id'], data={'expected_version':p['version'], 'summary':str(index)})
            s.commit()
        first = service.proposal_history(s, actor_id=1, proposal_id=p['id'])
        second = service.proposal_history(s, actor_id=1, proposal_id=p['id'], before_version=first['next_before_version'])
        versions = [r['version'] for r in first['items'] + second['items']]
        assert versions == list(range(25, 0, -1))
        assert first['has_more'] and not second['has_more']
        assert second['next_before_version'] is None
        with pytest.raises(ScmServiceError):
            service.proposal_history(s, actor_id=1, proposal_id=p['id'], before_version=True)
