"""govern color families

Revision ID: f5c7d9e2b308
Revises: e4b6c8d1a207
Create Date: 2026-07-22 23:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "f5c7d9e2b308"
down_revision = "e4b6c8d1a207"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    op.add_column(
        "familia_color",
        sa.Column("activo", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "familia_color",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("familia_color", recreate="always") as batch_op:
            batch_op.create_check_constraint("ck_familia_color_version", "version >= 1")
    else:
        op.create_check_constraint(
            "ck_familia_color_version",
            "familia_color",
            "version >= 1",
        )


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("familia_color", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_familia_color_version", type_="check")
            batch_op.drop_column("version")
            batch_op.drop_column("activo")
    else:
        op.drop_constraint("ck_familia_color_version", "familia_color", type_="check")
        op.drop_column("familia_color", "version")
        op.drop_column("familia_color", "activo")
