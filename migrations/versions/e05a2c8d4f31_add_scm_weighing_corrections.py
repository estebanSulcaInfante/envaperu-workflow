"""add SCM weighing corrections

Revision ID: e05a2c8d4f31
Revises: d94f1a7c3e20
"""

from alembic import op
import sqlalchemy as sa


revision = "e05a2c8d4f31"
down_revision = "d94f1a7c3e20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_correccion_pesaje_manga",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("pesaje_id", sa.Integer(), nullable=False),
        sa.Column(
            "estado", sa.String(20),
            server_default="PENDIENTE", nullable=False,
        ),
        sa.Column("proposed_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("evidence_reference", sa.String(500), nullable=True),
        sa.Column("requested_by_id", sa.Integer(), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("request_operation_id", sa.Uuid(), nullable=False),
        sa.Column("resolved_by_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_operation_id", sa.Uuid(), nullable=True),
        sa.Column("resolution_reason", sa.String(500), nullable=True),
        sa.Column("result_projection_json", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "estado IN ('PENDIENTE', 'RECHAZADA', 'APLICADA')",
            name="ck_scm_correccion_pesaje_estado",
        ),
        sa.ForeignKeyConstraint(
            ["pesaje_id"], ["scm_pesaje_manga.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["request_operation_id"], ["scm_operacion.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_operation_id"], ["scm_operacion.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "public_id", name="uq_scm_correccion_pesaje_public_id"
        ),
        sa.UniqueConstraint(
            "request_operation_id",
            name="uq_scm_correccion_pesaje_request_operation",
        ),
        sa.UniqueConstraint(
            "approval_operation_id",
            name="uq_scm_correccion_pesaje_approval_operation",
        ),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT count(*) FROM scm_correccion_pesaje_manga")
    ).scalar_one():
        raise RuntimeError(
            "SCM_CORRECCION_PESAJE_DOWNGRADE_BLOCKED: existen correcciones"
        )
    op.drop_table("scm_correccion_pesaje_manga")
