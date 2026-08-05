"""add optional catalog images

Revision ID: f60c8d5e6f43
Revises: f59b7c4d5e32
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f60c8d5e6f43"
down_revision = "f59b7c4d5e32"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pieza", sa.Column("imagen_mime", sa.String(32), nullable=True))
    op.add_column("pieza", sa.Column("imagen_data", sa.LargeBinary(), nullable=True))
    op.add_column("producto_terminado", sa.Column("imagen_mime", sa.String(32), nullable=True))
    op.add_column("producto_terminado", sa.Column("imagen_data", sa.LargeBinary(), nullable=True))
    op.create_table(
        "scm_reversion_recepcion_manga",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("existencia_id", sa.Uuid(), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False, server_default="PENDIENTE"),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column("evidencia", sa.String(500), nullable=True),
        sa.Column("solicitada_por_id", sa.Integer(), nullable=False),
        sa.Column("solicitada_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resuelta_por_id", sa.Integer(), nullable=True),
        sa.Column("resuelta_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolucion_motivo", sa.String(500), nullable=True),
        sa.Column("request_operation_id", sa.Uuid(), nullable=False),
        sa.Column("resolution_operation_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("estado IN ('PENDIENTE', 'APROBADA', 'RECHAZADA')", name="ck_scm_reversion_recepcion_estado"),
        sa.ForeignKeyConstraint(["existencia_id"], ["scm_existencia_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["solicitada_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resuelta_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolution_operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_operation_id", name="uq_scm_reversion_recepcion_request"),
        sa.UniqueConstraint("resolution_operation_id", name="uq_scm_reversion_recepcion_resolution"),
    )


def downgrade():
    op.drop_table("scm_reversion_recepcion_manga")
    op.drop_column("producto_terminado", "imagen_data")
    op.drop_column("producto_terminado", "imagen_mime")
    op.drop_column("pieza", "imagen_data")
    op.drop_column("pieza", "imagen_mime")
