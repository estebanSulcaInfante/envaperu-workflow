"""Persist the station-2 Mesa pilot inventory evidence.

Revision ID: mesa26011901
Revises: hg8293ab6c7d
"""

from alembic import op
import sqlalchemy as sa


revision = "mesa26011901"
down_revision = "hg8293ab6c7d"
branch_labels = None
depends_on = None


UUID = sa.Uuid()
TS = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "scm_mesa_inventario_campana",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False, server_default="ABIERTA"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("creado_por_id", sa.Integer(), sa.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", TS),
        sa.UniqueConstraint("codigo", name="uq_mesa_inv_campana_codigo"),
        sa.CheckConstraint("estado IN ('ABIERTA', 'CERRADA')", name="ck_mesa_inv_campana_estado"),
    )
    op.create_table(
        "scm_mesa_inventario_sesion",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("campana_id", UUID, sa.ForeignKey("scm_mesa_inventario_campana.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("zona_id", sa.String(64), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False, server_default="ABIERTA"),
        sa.Column("clave", sa.String(120), nullable=False),
        sa.Column("cobertura_confirmada", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("creado_por_id", sa.Integer(), sa.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", TS),
        sa.UniqueConstraint("clave", name="uq_mesa_inv_sesion_clave"),
        sa.CheckConstraint("estado IN ('ABIERTA', 'CERRADA')", name="ck_mesa_inv_sesion_estado"),
    )
    op.create_table(
        "scm_mesa_inventario_manga",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("codigo", sa.String(120), nullable=False),
        sa.Column("unidad_fisica_id", UUID, sa.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT")),
        sa.Column("kardex_existence_id", UUID, sa.ForeignKey("scm_existencia_manga_kg.id", ondelete="RESTRICT")),
        sa.Column("articulo_id", sa.Integer(), sa.ForeignKey("scm_articulo.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("zona_hallazgo", sa.String(64)),
        sa.Column("estado", sa.String(48), nullable=False, server_default="PENDIENTE_PESAJE"),
        sa.Column("bruto_kg", sa.Numeric(15, 3)), sa.Column("tara_kg", sa.Numeric(15, 3)), sa.Column("neto_kg", sa.Numeric(15, 3)),
        sa.Column("baja_motivo", sa.String(240)), sa.Column("baja_movimiento_id", UUID), sa.Column("baja_referencia", sa.String(120)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()), sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("codigo", name="uq_mesa_inv_manga_codigo"),
        sa.CheckConstraint("estado IN ('HALLAZGO', 'PENDIENTE_PESAJE', 'PESADA_PENDIENTE_CONFIRMACION', 'DISPONIBLE', 'BAJA_COMPROBADA', 'BAJA_NO_RECUPERABLE')", name="ck_mesa_inv_manga_estado"),
    )
    op.create_index("ix_mesa_inv_manga_estado", "scm_mesa_inventario_manga", ["estado"])
    op.create_table(
        "scm_mesa_inventario_lectura",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("sesion_id", UUID, sa.ForeignKey("scm_mesa_inventario_sesion.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manga_id", UUID, sa.ForeignKey("scm_mesa_inventario_manga.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("zona_anterior", sa.String(64)), sa.Column("estado_anterior", sa.String(48)), sa.Column("neto_anterior_kg", sa.Numeric(15, 3)),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("sesion_id", "manga_id", name="uq_mesa_inv_lectura_sesion_manga"),
    )
    op.create_table(
        "scm_mesa_inventario_pesaje",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("manga_id", UUID, sa.ForeignKey("scm_mesa_inventario_manga.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("bruto_kg", sa.Numeric(15, 3), nullable=False), sa.Column("tara_kg", sa.Numeric(15, 3), nullable=False), sa.Column("neto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("lectura_id", sa.String(120), nullable=False), sa.Column("station_id", sa.String(80), nullable=False),
        sa.Column("captura_base", sa.String(2)), sa.Column("captura_kg", sa.Numeric(15, 3)), sa.Column("captura_at", TS),
        sa.Column("clave", sa.String(120), nullable=False), sa.Column("actor_id", sa.Integer(), sa.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("clave", name="uq_mesa_inv_pesaje_clave"),
    )
    op.create_table(
        "scm_mesa_inventario_revision",
        sa.Column("id", UUID, primary_key=True, nullable=False), sa.Column("codigo", sa.String(120), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False, server_default="ACTIVA"), sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("used_at", TS), sa.Column("actor_id", sa.Integer(), sa.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("codigo", "estado", name="uq_mesa_inv_revision_codigo_estado"),
    )
    op.create_table(
        "scm_mesa_inventario_movimiento",
        sa.Column("id", UUID, primary_key=True, nullable=False), sa.Column("manga_id", UUID, sa.ForeignKey("scm_mesa_inventario_manga.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("tipo", sa.String(32), nullable=False), sa.Column("delta_kg", sa.Numeric(15, 3), nullable=False), sa.Column("motivo", sa.String(240), nullable=False),
        sa.Column("clave", sa.String(120), nullable=False), sa.Column("vinculo_baja_id", UUID),
        sa.Column("kardex_movimiento_id", UUID, sa.ForeignKey("scm_movimiento_inventario_kg.id", ondelete="RESTRICT")),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()), sa.UniqueConstraint("clave", name="uq_mesa_inv_movimiento_clave"),
    )
    op.create_table(
        "scm_mesa_inventario_solicitud",
        sa.Column("clave", sa.String(120), primary_key=True), sa.Column("operacion", sa.String(48), nullable=False), sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("respuesta_json", sa.JSON(), nullable=False), sa.Column("status_code", sa.Integer(), nullable=False), sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("clave", name="uq_mesa_inv_solicitud_clave"),
    )


def downgrade():
    op.drop_table("scm_mesa_inventario_solicitud")
    op.drop_table("scm_mesa_inventario_movimiento")
    op.drop_table("scm_mesa_inventario_revision")
    op.drop_table("scm_mesa_inventario_pesaje")
    op.drop_table("scm_mesa_inventario_lectura")
    op.drop_index("ix_mesa_inv_manga_estado", table_name="scm_mesa_inventario_manga")
    op.drop_table("scm_mesa_inventario_manga")
    op.drop_table("scm_mesa_inventario_sesion")
    op.drop_table("scm_mesa_inventario_campana")
