"""add multiwarehouse scope and inventory transfers

Revision ID: f83a2b4c6d70
Revises: 606aba7e7f3c
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa


revision = "f83a2b4c6d70"
down_revision = "606aba7e7f3c"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_almacen",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("tipo", sa.String(32), nullable=False),
        sa.Column("configuracion_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("activo", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("tipo IN ('MATERIAS_PRIMAS', 'PIEZAS_WIP', 'PRODUCTO_TERMINADO', 'GENERAL_CONTINGENCIA')", name="ck_scm_almacen_tipo"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_almacen_codigo"),
    )
    with op.batch_alter_table("scm_ubicacion_inventario") as batch:
        batch.add_column(sa.Column("almacen_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tipo", sa.String(24), nullable=True))
        batch.add_column(sa.Column("permite_saldo_libre", sa.Boolean(), server_default=sa.true(), nullable=False))
        batch.add_column(sa.Column("version", sa.Integer(), server_default="1", nullable=False))
        batch.create_foreign_key("fk_scm_ubicacion_almacen", "scm_almacen", ["almacen_id"], ["id"], ondelete="RESTRICT")
        batch.create_foreign_key("fk_scm_ubicacion_parent", "scm_ubicacion_inventario", ["parent_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_scm_ubicacion_almacen", "scm_ubicacion_inventario", ["almacen_id", "tipo"])
    op.create_table(
        "scm_almacen_trabajador",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("almacen_id", sa.Uuid(), nullable=False),
        sa.Column("trabajador_id", sa.Integer(), nullable=False),
        sa.Column("clases_articulo_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("activo", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("asignado_por_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["almacen_id"], ["scm_almacen.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trabajador_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asignado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("almacen_id", "trabajador_id", name="uq_scm_almacen_trabajador"),
    )
    op.create_index("ix_scm_almacen_trabajador_scope", "scm_almacen_trabajador", ["trabajador_id", "activo", "almacen_id"])
    op.create_table(
        "scm_sesion_operacion_almacen",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("modalidad", sa.String(16), nullable=False),
        sa.Column("origen_ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("destino_ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(16), server_default="ABIERTA", nullable=False),
        sa.Column("contexto_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["origen_ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["destino_ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("tipo IN ('ENTRADA', 'SALIDA', 'TRANSFERENCIA', 'RETORNO')", name="ck_scm_sesion_operacion_tipo"),
        sa.CheckConstraint("modalidad IN ('PICKUP', 'ENTREGA')", name="ck_scm_sesion_operacion_modalidad"),
        sa.CheckConstraint("estado IN ('ABIERTA', 'LISTA', 'CONFIRMADA', 'CANCELADA', 'EXPIRADA')", name="ck_scm_sesion_operacion_estado"),
    )
    op.create_table(
        "scm_sesion_operacion_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("existencia_manga_id", sa.Uuid(), nullable=False),
        sa.Column("codigo_escaneado", sa.String(120), nullable=False),
        sa.Column("cantidad_snapshot", sa.Numeric(15, 3), nullable=False),
        sa.Column("estado", sa.String(16), server_default="VALIDA", nullable=False),
        sa.Column("motivo", sa.String(240), nullable=True),
        sa.Column("orden", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["sesion_id"], ["scm_sesion_operacion_almacen.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["existencia_manga_id"], ["scm_existencia_manga.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sesion_id", "existencia_manga_id", name="uq_scm_sesion_item_existencia"),
        sa.CheckConstraint("estado IN ('VALIDA', 'RECHAZADA')", name="ck_scm_sesion_item_estado"),
    )
    op.create_table(
        "scm_transferencia_inventario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("sesion_id", sa.Uuid(), nullable=False),
        sa.Column("origen_ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("destino_ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("modalidad", sa.String(16), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("custodio_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("incidencia_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["sesion_id"], ["scm_sesion_operacion_almacen.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["origen_ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["destino_ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["custodio_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_transferencia_codigo"),
        sa.UniqueConstraint("operation_id", name="uq_scm_transferencia_operation"),
        sa.CheckConstraint("modalidad IN ('PICKUP', 'ENTREGA')", name="ck_scm_transferencia_modalidad"),
        sa.CheckConstraint("estado IN ('RECIBIDA', 'EN_TRANSITO', 'CERRADA', 'INCIDENCIA', 'RETORNADA')", name="ck_scm_transferencia_estado"),
    )
    op.create_table(
        "scm_transferencia_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transferencia_id", sa.Uuid(), nullable=False),
        sa.Column("existencia_manga_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad", sa.Numeric(15, 3), nullable=False),
        sa.Column("movimiento_salida_id", sa.Uuid(), nullable=True),
        sa.Column("movimiento_transito_id", sa.Uuid(), nullable=True),
        sa.Column("movimiento_entrada_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["transferencia_id"], ["scm_transferencia_inventario.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["existencia_manga_id"], ["scm_existencia_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["movimiento_salida_id"], ["scm_movimiento_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["movimiento_transito_id"], ["scm_movimiento_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["movimiento_entrada_id"], ["scm_movimiento_inventario.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transferencia_id", "existencia_manga_id", name="uq_scm_transferencia_item_existencia"),
    )


def downgrade():
    op.drop_table("scm_transferencia_item")
    op.drop_table("scm_transferencia_inventario")
    op.drop_table("scm_sesion_operacion_item")
    op.drop_table("scm_sesion_operacion_almacen")
    op.drop_index("ix_scm_almacen_trabajador_scope", table_name="scm_almacen_trabajador")
    op.drop_table("scm_almacen_trabajador")
    op.drop_index("ix_scm_ubicacion_almacen", table_name="scm_ubicacion_inventario")
    with op.batch_alter_table("scm_ubicacion_inventario") as batch:
        batch.drop_constraint("fk_scm_ubicacion_parent", type_="foreignkey")
        batch.drop_constraint("fk_scm_ubicacion_almacen", type_="foreignkey")
        batch.drop_column("version")
        batch.drop_column("permite_saldo_libre")
        batch.drop_column("tipo")
        batch.drop_column("parent_id")
        batch.drop_column("almacen_id")
    op.drop_table("scm_almacen")
