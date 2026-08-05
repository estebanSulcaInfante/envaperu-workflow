"""add provider contact fields

Revision ID: f74b3c8d1e65
Revises: f73a2b7c0d54
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa


revision = "f74b3c8d1e65"
down_revision = "f73a2b7c0d54"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "scm_proveedor",
        sa.Column("contacto", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "scm_proveedor",
        sa.Column("telefono", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "scm_proveedor",
        sa.Column("whatsapp", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "scm_proveedor",
        sa.Column("correo", sa.String(length=254), nullable=True),
    )


def downgrade():
    op.drop_column("scm_proveedor", "correo")
    op.drop_column("scm_proveedor", "whatsapp")
    op.drop_column("scm_proveedor", "telefono")
    op.drop_column("scm_proveedor", "contacto")
