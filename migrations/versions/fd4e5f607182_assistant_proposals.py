"""Durable assistant proposals and immutable review history, no business writes."""
from alembic import op
import sqlalchemy as sa

revision = 'fd4e5f607182'
down_revision = 'fc3d4e5f6071'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('scm_assistant_proposal',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('trabajador.id'), nullable=False),
        sa.Column('need_key', sa.String(64), nullable=False),
        sa.Column('need', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('diff', sa.Text(), nullable=False),
        sa.Column('diff_sha256', sa.String(64)),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('approved_version', sa.Integer()),
        sa.Column('approved_sha256', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('actor_id', 'need_key', name='uq_assistant_proposal_need'))
    op.create_table('scm_assistant_proposal_revision',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('proposal_id', sa.String(36), sa.ForeignKey('scm_assistant_proposal.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('trabajador.id'), nullable=False),
        sa.Column('action', sa.String(24), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('proposal_id', 'version', name='uq_assistant_proposal_revision'))


def downgrade():
    # Explicit operator migration only. Export review history before rollback.
    op.drop_table('scm_assistant_proposal_revision')
    op.drop_table('scm_assistant_proposal')
