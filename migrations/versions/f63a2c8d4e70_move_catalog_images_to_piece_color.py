"""move catalog images from abstract pieces to piece-color SKUs

Revision ID: f63a2c8d4e70
Revises: f62e0b8d7c36
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "f63a2c8d4e70"
down_revision = "f62e0b8d7c36"
branch_labels = None
depends_on = None


def upgrade():
    # Pieza es una forma abstracta. No se copian sus imágenes porque una misma
    # forma puede tener varios colores y la fotografía debe identificar al SKU.
    op.add_column("pieza_color", sa.Column("imagen_mime", sa.String(32), nullable=True))
    op.add_column("pieza_color", sa.Column("imagen_data", sa.LargeBinary(), nullable=True))
    op.drop_column("pieza", "imagen_data")
    op.drop_column("pieza", "imagen_mime")


def downgrade():
    op.add_column("pieza", sa.Column("imagen_mime", sa.String(32), nullable=True))
    op.add_column("pieza", sa.Column("imagen_data", sa.LargeBinary(), nullable=True))
    op.drop_column("pieza_color", "imagen_data")
    op.drop_column("pieza_color", "imagen_mime")
