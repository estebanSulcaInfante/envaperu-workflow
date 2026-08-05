"""add auditable production planning snapshots

Revision ID: f43c8e4f0a97
Revises: f42b7d3e9f86
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "f43c8e4f0a97"
down_revision = "f42b7d3e9f86"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_plan_produccion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("orden_produccion_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "estado",
            sa.String(length=24),
            server_default="CALCULADO",
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("propuesta_json", sa.JSON(), nullable=False),
        sa.Column("calculado_por_id", sa.Integer(), nullable=False),
        sa.Column("confirmado_por_id", sa.Integer(), nullable=True),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "confirmado_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_scm_plan_produccion_revision",
        ),
        sa.CheckConstraint(
            "estado IN ('CALCULADO', 'CONFIRMADO', 'SUPERADO')",
            name="ck_scm_plan_produccion_estado",
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64 AND length(content_hash) = 64",
            name="ck_scm_plan_produccion_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["orden_produccion_id"],
            ["scm_orden_produccion.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["calculado_por_id"],
            ["trabajador.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmado_por_id"],
            ["trabajador.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "orden_produccion_id",
            "revision",
            name="uq_scm_plan_produccion_revision",
        ),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_scm_plan_produccion_operation",
        ),
    )
    op.create_index(
        "ux_scm_plan_produccion_calculado",
        "scm_plan_produccion",
        ["orden_produccion_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'CALCULADO'"),
        sqlite_where=sa.text("estado = 'CALCULADO'"),
    )


def downgrade():
    op.drop_index(
        "ux_scm_plan_produccion_calculado",
        table_name="scm_plan_produccion",
    )
    op.drop_table("scm_plan_produccion")
