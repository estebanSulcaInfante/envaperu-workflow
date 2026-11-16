"""Persist the quality periodic-visit quota on the OT header.

Revision ID: hg8293ab6c7d
Revises: fg718293a5b6
"""

from alembic import op
import sqlalchemy as sa


revision = "hg8293ab6c7d"
down_revision = "fg718293a5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "registro_diario_produccion",
        sa.Column("calidad_visitas_periodicas_previstas", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_registro_diario_calidad_visitas_periodicas_previstas",
        "registro_diario_produccion",
        "calidad_visitas_periodicas_previstas IS NULL OR calidad_visitas_periodicas_previstas >= 0",
    )


def downgrade():
    op.drop_constraint(
        "ck_registro_diario_calidad_visitas_periodicas_previstas",
        "registro_diario_produccion",
        type_="check",
    )
    op.drop_column("registro_diario_produccion", "calidad_visitas_periodicas_previstas")
