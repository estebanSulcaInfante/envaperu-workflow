"""Durable human-reviewed backlog; never an executable code queue."""
from datetime import datetime, timezone
from uuid import uuid4
from app.extensions import db


def now():
    return datetime.now(timezone.utc)


class ScmAssistantProposal(db.Model):
    __tablename__ = 'scm_assistant_proposal'
    __table_args__ = (db.UniqueConstraint('actor_id', 'need_key', name='uq_assistant_proposal_need'),)
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    actor_id = db.Column(db.Integer, db.ForeignKey('trabajador.id'), nullable=False)
    need_key = db.Column(db.String(64), nullable=False)
    need = db.Column(db.Text, nullable=False)
    summary = db.Column(db.Text, nullable=False, default='')
    diff = db.Column(db.Text, nullable=False, default='')
    diff_sha256 = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(24), nullable=False, default='PENDIENTE')
    version = db.Column(db.Integer, nullable=False, default=1)
    approved_version = db.Column(db.Integer, nullable=True)
    approved_sha256 = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    __mapper_args__ = {'version_id_col': version}

    def to_dict(self):
        return {k: getattr(self, k) for k in ('id', 'actor_id', 'need', 'summary', 'diff', 'diff_sha256', 'status', 'version', 'approved_version', 'approved_sha256')} | {
            'created_at': self.created_at.isoformat(), 'updated_at': self.updated_at.isoformat(),
            'execution_supported': False,
        }


class ScmAssistantProposalRevision(db.Model):
    __tablename__ = 'scm_assistant_proposal_revision'
    __table_args__ = (db.UniqueConstraint('proposal_id', 'version', name='uq_assistant_proposal_revision'),)
    id = db.Column(db.Integer, primary_key=True)
    proposal_id = db.Column(db.String(36), db.ForeignKey('scm_assistant_proposal.id'), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('trabajador.id'), nullable=False)
    action = db.Column(db.String(24), nullable=False)
    snapshot = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
