"""Add Estación 2 return unions and receipt audit.

Revision ID: mesa26011902
Revises: mesa26011901
"""

from alembic import op
import sqlalchemy as sa


revision = "mesa26011902"
down_revision = "mesa26011901"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_mesa_retorno_union",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(120), nullable=False),
        sa.Column("ciclo", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column("pieza_color_sku", sa.String(80)),
        sa.Column("color", sa.String(120)),
        sa.Column("zona_hallazgo", sa.String(64)),
        sa.Column("estado", sa.String(32), nullable=False, server_default="PENDIENTE_PESAJE"),
        sa.Column("unidad_fisica_id", sa.Uuid(), nullable=False),
        sa.Column("bruto_kg", sa.Numeric(15, 3)),
        sa.Column("tara_kg", sa.Numeric(15, 3)),
        sa.Column("neto_kg", sa.Numeric(15, 3)),
        sa.Column("captura_token", sa.String(160)),
        sa.Column("captura_base", sa.String(2)),
        sa.Column("captura_at", sa.DateTime(timezone=True)),
        sa.Column("captura_reading_id", sa.String(160)),
        sa.Column("captura_payload_hash", sa.String(64)),
        sa.Column("station_id", sa.String(80), nullable=False, server_default="ESTACION-2"),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("movimiento_id", sa.Uuid()),
        sa.Column("existencia_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("estado IN ('PENDIENTE_PESAJE', 'PESADA_PENDIENTE_CONFIRMACION', 'RECIBIDA')", name="ck_mesa_retorno_union_estado"),
        sa.ForeignKeyConstraint(["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["unidad_fisica_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["movimiento_id"], ["scm_movimiento_inventario_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["existencia_id"], ["scm_existencia_manga_kg.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", "ciclo", name="uq_mesa_retorno_union_code_cycle"),
        sa.UniqueConstraint("operation_id", name="uq_mesa_retorno_union_operation"),
        sa.UniqueConstraint("captura_token", name="uq_mesa_retorno_union_capture"),
    )
    op.create_table(
        "scm_mesa_retorno_origen",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("union_id", sa.Uuid(), nullable=False),
        sa.Column("ciclo", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unidad_fisica_id", sa.Uuid(), nullable=False),
        sa.Column("retiro_id", sa.Uuid()),
        sa.Column("lote_id", sa.String(120)),
        sa.Column("lote_snapshot", sa.String(240)),
        sa.Column("ot_date", sa.String(32)),
        sa.Column("ot_shift", sa.String(32)),
        sa.Column("neto_retirado_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("pieza_color_sku", sa.String(80)),
        sa.Column("color", sa.String(120)),
        sa.Column("estado", sa.String(24), nullable=False, server_default="RESERVADO_UNION"),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("estado IN ('RESERVADO_UNION', 'VACIADO_TERMINAL')", name="ck_mesa_retorno_origin_state"),
        sa.ForeignKeyConstraint(["union_id"], ["scm_mesa_retorno_union.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["unidad_fisica_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retiro_id"], ["scm_retiro_armado_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("union_id", "unidad_fisica_id", "ciclo", name="uq_mesa_retorno_origin_unit_cycle"),
        sa.UniqueConstraint("retiro_id", "unidad_fisica_id", "ciclo", name="uq_mesa_retorno_origin_withdrawal_cycle"),
    )
    op.create_table(
        "scm_mesa_retorno_recibo",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("union_id", sa.Uuid(), nullable=False),
        sa.Column("ciclo", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unidad_fisica_id", sa.Uuid(), nullable=False),
        sa.Column("movimiento_id", sa.Uuid(), nullable=False),
        sa.Column("existencia_id", sa.Uuid(), nullable=False),
        sa.Column("captura_token", sa.String(160), nullable=False),
        sa.Column("neto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["union_id"], ["scm_mesa_retorno_union.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["unidad_fisica_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["movimiento_id"], ["scm_movimiento_inventario_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["existencia_id"], ["scm_existencia_manga_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("captura_token", name="uq_mesa_retorno_receipt_capture"),
        sa.UniqueConstraint("operation_id", name="uq_mesa_retorno_receipt_operation"),
    )


def downgrade():
    op.drop_table("scm_mesa_retorno_recibo")
    op.drop_table("scm_mesa_retorno_origen")
    op.drop_table("scm_mesa_retorno_union")
