"""allow canonical WIP and finished-product mangas

Revision ID: f42b7d3e9f86
Revises: f41a6c2d8e75
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "f42b7d3e9f86"
down_revision = "f41a6c2d8e75"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "scm_manga",
        "pieza_color_sku_snapshot",
        existing_type=sa.String(length=50),
        nullable=True,
    )


def downgrade():
    connection = op.get_bind()
    count = connection.execute(sa.text(
        """
        SELECT count(*)
        FROM scm_manga
        WHERE pieza_color_sku_snapshot IS NULL
        """
    )).scalar_one()
    if count:
        raise RuntimeError(
            "No se puede volver a f41: existen mangas canónicas sin "
            "PiezaColor."
        )
    op.alter_column(
        "scm_manga",
        "pieza_color_sku_snapshot",
        existing_type=sa.String(length=50),
        nullable=False,
    )
