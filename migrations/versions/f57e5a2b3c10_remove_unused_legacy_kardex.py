"""remove unused legacy kardex

Revision ID: f57e5a2b3c10
Revises: f56d4f1a2c09
Create Date: 2026-08-03

The factory never loaded Kardex balances into these tables. Historical
weighings live in their own tables and are intentionally unaffected.
"""

from alembic import op
import sqlalchemy as sa


revision = "f57e5a2b3c10"
down_revision = "f56d4f1a2c09"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("movimiento_kardex")
    op.drop_index(
        "ix_inventario_manga_pesaje_id",
        table_name="inventario_manga",
    )
    op.drop_table("inventario_manga")


def downgrade():
    op.create_table(
        "inventario_manga",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pesaje_id", sa.Integer(), nullable=False),
        sa.Column("control_peso_id", sa.Integer(), nullable=True),
        sa.Column("nro_op", sa.String(length=20), nullable=True),
        sa.Column("molde", sa.String(length=100), nullable=True),
        sa.Column("color", sa.String(length=100), nullable=True),
        sa.Column("peso_kg", sa.Float(), nullable=True),
        sa.Column("pieza_sku", sa.String(length=50), nullable=True),
        sa.Column("pieza_nombre", sa.String(length=100), nullable=True),
        sa.Column("extra1", sa.String(length=200), nullable=True),
        sa.Column("extra2", sa.String(length=200), nullable=True),
        sa.Column("extra3", sa.String(length=200), nullable=True),
        sa.Column("locacion_actual", sa.String(length=100), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("fecha_ingreso", sa.DateTime(), nullable=True),
        sa.Column("fecha_despacho", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["control_peso_id"], ["control_peso.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inventario_manga_pesaje_id",
        "inventario_manga",
        ["pesaje_id"],
        unique=True,
    )
    op.create_table(
        "movimiento_kardex",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inventario_manga_id", sa.Integer(), nullable=False),
        sa.Column("tipo_operacion", sa.String(length=30), nullable=False),
        sa.Column("locacion_origen", sa.String(length=100), nullable=True),
        sa.Column("locacion_destino", sa.String(length=100), nullable=True),
        sa.Column("operario_id", sa.String(length=50), nullable=True),
        sa.Column("metadatos", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["inventario_manga_id"], ["inventario_manga.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
