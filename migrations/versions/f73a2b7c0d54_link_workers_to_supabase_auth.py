"""link SCM workers to Supabase Auth identities

Revision ID: f73a2b7c0d54
Revises: f72e1a6b9c43
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa


revision = "f73a2b7c0d54"
down_revision = "f72e1a6b9c43"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "trabajador",
        sa.Column("auth_user_id", sa.Uuid(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_trabajador_auth_user_id",
        "trabajador",
        ["auth_user_id"],
    )


def downgrade():
    op.drop_constraint(
        "uq_trabajador_auth_user_id",
        "trabajador",
        type_="unique",
    )
    op.drop_column("trabajador", "auth_user_id")

