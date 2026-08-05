"""add catalog image object storage metadata

Revision ID: f70c9d5e7a21
Revises: f69b8c4e6d10
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa


revision = "f70c9d5e7a21"
down_revision = "f69b8c4e6d10"
branch_labels = None
depends_on = None


def _add_storage_columns(table_name):
    op.add_column(
        table_name,
        sa.Column("imagen_storage_key", sa.String(512), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("imagen_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("imagen_size_bytes", sa.Integer(), nullable=True),
    )


def _drop_storage_columns(table_name):
    op.drop_column(table_name, "imagen_size_bytes")
    op.drop_column(table_name, "imagen_sha256")
    op.drop_column(table_name, "imagen_storage_key")


def upgrade():
    _add_storage_columns("producto_terminado")
    _add_storage_columns("pieza_color")


def downgrade():
    _drop_storage_columns("pieza_color")
    _drop_storage_columns("producto_terminado")
