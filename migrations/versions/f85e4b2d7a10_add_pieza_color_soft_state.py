"""add PiezaColor soft state

Revision ID: f85e4b2d7a10
Revises: f84d3a7c9e21
Create Date: 2026-08-27 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f85e4b2d7a10"
down_revision = "f84d3a7c9e21"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("pieza_color") as batch_op:
        batch_op.add_column(sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ))
        batch_op.add_column(sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ))
        batch_op.create_check_constraint(
            "ck_pieza_color_version",
            "version > 0",
        )
        batch_op.create_index(
            "ix_pieza_color_activo",
            ["activo"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("pieza_color") as batch_op:
        batch_op.drop_index("ix_pieza_color_activo")
        batch_op.drop_constraint(
            "ck_pieza_color_version",
            type_="check",
        )
        batch_op.drop_column("version")
        batch_op.drop_column("activo")
