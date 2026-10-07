"""Add optional colada-weight snapshot to each fabrication run.

Revision ID: fe5f60718293
Revises: fd4e5f607182
"""

from alembic import op
import sqlalchemy as sa


revision = "fe5f60718293"
down_revision = "fd4e5f607182"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_corrida_fabricacion") as batch:
        batch.add_column(
            sa.Column("snapshot_peso_colada_gr", sa.Numeric(12, 4), nullable=True),
        )
        batch.create_check_constraint(
            "ck_scm_corrida_peso_colada_nonnegative",
            "snapshot_peso_colada_gr IS NULL OR snapshot_peso_colada_gr >= 0",
        )


def downgrade():
    with op.batch_alter_table("scm_corrida_fabricacion") as batch:
        batch.drop_constraint(
            "ck_scm_corrida_peso_colada_nonnegative",
            type_="check",
        )
        batch.drop_column("snapshot_peso_colada_gr")
