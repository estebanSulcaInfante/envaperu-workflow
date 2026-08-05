"""drop legacy product numeric code

Revision ID: f46f9b7d1c20
Revises: f45e8a6c0b19
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "f46f9b7d1c20"
down_revision = "f45e8a6c0b19"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "producto_terminado",
        "producto",
        existing_type=sa.String(length=200),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_producto_terminado_nombre_no_vacio",
        "producto_terminado",
        "length(trim(producto)) > 0",
    )
    op.drop_column("producto_terminado", "cod_producto")


def downgrade():
    op.add_column(
        "producto_terminado",
        sa.Column("cod_producto", sa.Integer(), nullable=True),
    )
    op.drop_constraint(
        "ck_producto_terminado_nombre_no_vacio",
        "producto_terminado",
        type_="check",
    )
    op.alter_column(
        "producto_terminado",
        "producto",
        existing_type=sa.String(length=200),
        nullable=True,
    )
