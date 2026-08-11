"""allow linked piece colors without duplicated classification

Revision ID: f82e1f7a3c64
Revises: f81d0e6f2b53
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "f82e1f7a3c64"
down_revision = "f81d0e6f2b53"
branch_labels = None
depends_on = None


def _alter_nullable(nullable):
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("pieza_color") as batch:
            batch.alter_column(
                "linea_id",
                existing_type=sa.Integer(),
                nullable=nullable,
            )
            batch.alter_column(
                "familia_id",
                existing_type=sa.Integer(),
                nullable=nullable,
            )
        return
    if connection.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.alter_column(
        "pieza_color",
        "linea_id",
        existing_type=sa.Integer(),
        nullable=nullable,
    )
    op.alter_column(
        "pieza_color",
        "familia_id",
        existing_type=sa.Integer(),
        nullable=nullable,
    )


def upgrade():
    # Metadata-only on PostgreSQL: no backfill and no ACL/RLS change.
    _alter_nullable(True)


def downgrade():
    connection = op.get_bind()
    null_rows = connection.scalar(sa.text("""
        SELECT count(*)
        FROM pieza_color
        WHERE linea_id IS NULL OR familia_id IS NULL
    """))
    if null_rows:
        raise RuntimeError(
            "Downgrade f82 bloqueado: existen PiezaColor sin Linea/Familia. "
            "Clasifique o retire esas filas explicitamente antes de volver a f81."
        )
    _alter_nullable(False)
