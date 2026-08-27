"""add inventory explorer indexes

Revision ID: 0b548129a29a
Revises: f86a1c3e9b20
Create Date: 2026-08-15 12:31:17.536175

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '0b548129a29a'
down_revision = 'f86a1c3e9b20'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_scm_articulo_clase_codigo_id",
        "scm_articulo",
        ["clase", "codigo", "id"],
        unique=False,
    )
    op.create_index(
        "ix_scm_material_clase_codigo_id",
        "scm_material",
        ["clase", "codigo", "id"],
        unique=False,
    )
    op.create_index(
        "ix_scm_ubicacion_almacen_codigo_id",
        "scm_ubicacion_inventario",
        ["almacen_id", "codigo", "id"],
        unique=False,
    )
    op.create_index(
        "ix_scm_movimiento_inventario_created_id",
        "scm_movimiento_inventario",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_scm_movimiento_material_created_id",
        "scm_movimiento_material_inventario",
        ["created_at", "id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_scm_movimiento_material_created_id",
        table_name="scm_movimiento_material_inventario",
    )
    op.drop_index(
        "ix_scm_movimiento_inventario_created_id",
        table_name="scm_movimiento_inventario",
    )
    op.drop_index(
        "ix_scm_ubicacion_almacen_codigo_id",
        table_name="scm_ubicacion_inventario",
    )
    op.drop_index(
        "ix_scm_material_clase_codigo_id",
        table_name="scm_material",
    )
    op.drop_index(
        "ix_scm_articulo_clase_codigo_id",
        table_name="scm_articulo",
    )
