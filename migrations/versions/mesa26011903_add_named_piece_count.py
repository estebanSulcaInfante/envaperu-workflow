"""Persist named, multi-zone physical piece-count sessions.

Revision ID: mesa26011903
Revises: mesa26011902
"""

from alembic import op
import sqlalchemy as sa


revision = "mesa26011903"
down_revision = "mesa26011902"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_mesa_inventario_sesion") as batch:
        batch.add_column(sa.Column("nombre", sa.String(160), nullable=True))
        batch.add_column(sa.Column("piezas_json", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("esperadas_json", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("zonas_json", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("saldo_snapshot_json", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("corte_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("cierre_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("cierre_response_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("scm_mesa_inventario_lectura") as batch:
        batch.add_column(sa.Column("zona_lectura", sa.String(64), nullable=True))
        batch.add_column(sa.Column("zona_existente", sa.String(64), nullable=True))
        batch.add_column(sa.Column("movimiento_corte_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("pesaje_corte_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "scm_mesa_inventario_no_habida",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("sesion_id", sa.Uuid(), sa.ForeignKey("scm_mesa_inventario_sesion.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manga_id", sa.Uuid(), sa.ForeignKey("scm_mesa_inventario_manga.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("sesion_id", "manga_id", name="uq_mesa_inv_no_habida_sesion_manga"),
    )


def downgrade():
    op.drop_table("scm_mesa_inventario_no_habida")
    with op.batch_alter_table("scm_mesa_inventario_lectura") as batch:
        batch.drop_column("pesaje_corte_at")
        batch.drop_column("movimiento_corte_at")
        batch.drop_column("zona_existente")
        batch.drop_column("zona_lectura")
    with op.batch_alter_table("scm_mesa_inventario_sesion") as batch:
        batch.drop_column("cierre_response_json")
        batch.drop_column("cierre_fingerprint")
        batch.drop_column("corte_at")
        batch.drop_column("zonas_json")
        batch.drop_column("saldo_snapshot_json")
        batch.drop_column("esperadas_json")
        batch.drop_column("piezas_json")
        batch.drop_column("nombre")
