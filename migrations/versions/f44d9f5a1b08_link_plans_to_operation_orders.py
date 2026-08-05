"""link generated operation orders to planning proposals

Revision ID: f44d9f5a1b08
Revises: f43c8e4f0a97
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "f44d9f5a1b08"
down_revision = "f43c8e4f0a97"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "scm_orden_operacion",
        sa.Column("plan_produccion_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "scm_orden_operacion",
        sa.Column("propuesta_clave", sa.String(length=80), nullable=True),
    )
    op.create_foreign_key(
        "fk_scm_orden_operacion_plan",
        "scm_orden_operacion",
        "scm_plan_produccion",
        ["plan_produccion_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_scm_orden_operacion_plan_propuesta",
        "scm_orden_operacion",
        ["plan_produccion_id", "propuesta_clave"],
    )


def downgrade():
    op.drop_constraint(
        "uq_scm_orden_operacion_plan_propuesta",
        "scm_orden_operacion",
        type_="unique",
    )
    op.drop_constraint(
        "fk_scm_orden_operacion_plan",
        "scm_orden_operacion",
        type_="foreignkey",
    )
    op.drop_column("scm_orden_operacion", "propuesta_clave")
    op.drop_column("scm_orden_operacion", "plan_produccion_id")
