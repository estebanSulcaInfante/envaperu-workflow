"""add assembly execution mode and fabrication context

Revision ID: f67e6a2c8db4
Revises: f66d5f1b7ca3
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "f67e6a2c8db4"
down_revision = "f66d5f1b7ca3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "registro_diario_produccion",
        sa.Column("modo_ejecucion_ensamble", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("ot_fabricacion_contexto_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_registro_diario_ot_fabricacion_contexto",
        "registro_diario_produccion",
        "registro_diario_produccion",
        ["ot_fabricacion_contexto_id"],
        ["public_id"],
        ondelete="RESTRICT",
    )
    op.execute(sa.text("""
        UPDATE registro_diario_produccion
        SET modo_ejecucion_ensamble = 'MESA'
        WHERE tipo_ot = 'ENSAMBLE'
    """))
    op.create_check_constraint(
        "ck_registro_diario_modo_ensamble",
        "registro_diario_produccion",
        "(tipo_ot = 'ENSAMBLE' AND modo_ejecucion_ensamble IN "
        "('MESA', 'CONCURRENTE')) OR "
        "(tipo_ot <> 'ENSAMBLE' AND modo_ejecucion_ensamble IS NULL)",
    )
    op.create_check_constraint(
        "ck_registro_diario_contexto_ensamble",
        "registro_diario_produccion",
        "(modo_ejecucion_ensamble = 'CONCURRENTE' AND "
        "ot_fabricacion_contexto_id IS NOT NULL) OR "
        "(modo_ejecucion_ensamble IS DISTINCT FROM 'CONCURRENTE' AND "
        "ot_fabricacion_contexto_id IS NULL)",
    )


def downgrade():
    op.drop_constraint(
        "ck_registro_diario_contexto_ensamble",
        "registro_diario_produccion",
        type_="check",
    )
    op.drop_constraint(
        "ck_registro_diario_modo_ensamble",
        "registro_diario_produccion",
        type_="check",
    )
    op.drop_constraint(
        "fk_registro_diario_ot_fabricacion_contexto",
        "registro_diario_produccion",
        type_="foreignkey",
    )
    op.drop_column("registro_diario_produccion", "ot_fabricacion_contexto_id")
    op.drop_column("registro_diario_produccion", "modo_ejecucion_ensamble")
