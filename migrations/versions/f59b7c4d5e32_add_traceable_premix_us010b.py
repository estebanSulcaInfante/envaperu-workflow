"""add traceable premix us010b

Revision ID: f59b7c4d5e32
Revises: f58a6b3c4d21
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f59b7c4d5e32"
down_revision = "f58a6b3c4d21"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("scm_reserva_material", sa.Column(
        "cantidad_consumida_kg", sa.Numeric(15, 3), nullable=False, server_default="0",
    ))
    op.drop_constraint("ck_scm_reserva_material_emitida", "scm_reserva_material", type_="check")
    op.create_check_constraint(
        "ck_scm_reserva_material_emitida", "scm_reserva_material",
        "emitida_neta_kg >= 0 AND cantidad_consumida_kg >= 0 AND "
        "emitida_neta_kg + cantidad_consumida_kg <= cantidad_kg",
    )
    op.add_column("scm_emision_material", sa.Column(
        "cantidad_consumida_kg", sa.Numeric(15, 3), nullable=False, server_default="0",
    ))
    op.drop_constraint("ck_scm_emision_material_devuelta", "scm_emision_material", type_="check")
    op.create_check_constraint(
        "ck_scm_emision_material_devuelta", "scm_emision_material",
        "cantidad_devuelta_kg >= 0 AND cantidad_consumida_kg >= 0 AND "
        "cantidad_devuelta_kg + cantidad_consumida_kg <= cantidad_kg",
    )
    op.create_table(
        "scm_lote_premezcla",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column("corrida_fabricacion_id", sa.Uuid(), nullable=False),
        sa.Column("secuencia", sa.Integer(), nullable=False),
        sa.Column("cantidad_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("genealogia_tipo", sa.String(32), nullable=False, server_default="EXACTA"),
        sa.Column("estado", sa.String(24), nullable=False, server_default="DISPONIBLE_MAQUINA"),
        sa.Column("ubicacion_codigo", sa.String(40), nullable=False, server_default="PREPARACION_PRODUCCION"),
        sa.Column("motivo", sa.String(240), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cantidad_kg > 0", name="ck_scm_lote_premezcla_cantidad"),
        sa.CheckConstraint("genealogia_tipo IN ('EXACTA', 'CONJUNTO_CANDIDATOS')", name="ck_scm_lote_premezcla_genealogia"),
        sa.CheckConstraint("estado IN ('DISPONIBLE_MAQUINA', 'CONSUMIDO_MAQUINA', 'ANULADO')", name="ck_scm_lote_premezcla_estado"),
        sa.ForeignKeyConstraint(["corrida_fabricacion_id"], ["scm_corrida_fabricacion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_lote_premezcla_codigo"),
        sa.UniqueConstraint("operation_id", name="uq_scm_lote_premezcla_operation"),
        sa.UniqueConstraint("corrida_fabricacion_id", "secuencia", name="uq_scm_lote_premezcla_corrida_secuencia"),
    )
    op.create_table(
        "scm_lote_premezcla_input",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lote_premezcla_id", sa.Uuid(), nullable=False),
        sa.Column("emision_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_kg", sa.Numeric(15, 3), nullable=False),
        sa.CheckConstraint("cantidad_kg > 0", name="ck_scm_lote_premezcla_input_cantidad"),
        sa.ForeignKeyConstraint(["lote_premezcla_id"], ["scm_lote_premezcla.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["emision_id"], ["scm_emision_material.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lote_premezcla_id", "emision_id", name="uq_scm_lote_premezcla_input_emision"),
    )
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT 'MATERIAL_PREMEZCLA_CONFIRMAR', 'Confirmar transformacion de premezcla', true
        WHERE NOT EXISTS (
          SELECT 1 FROM scm_capacidad WHERE codigo = 'MATERIAL_PREMEZCLA_CONFIRMAR'
        )
    """))
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo rol
        JOIN scm_capacidad capacidad ON capacidad.codigo = 'MATERIAL_PREMEZCLA_CONFIRMAR'
        WHERE rol.codigo = 'JEFE_PRODUCCION'
        AND NOT EXISTS (
          SELECT 1 FROM scm_rol_capacidad rc
          WHERE rc.rol_operativo_id = rol.id AND rc.capacidad_id = capacidad.id
        )
    """))


def downgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad WHERE capacidad_id IN (
          SELECT id FROM scm_capacidad WHERE codigo = 'MATERIAL_PREMEZCLA_CONFIRMAR'
        )
    """))
    op.execute(sa.text("DELETE FROM scm_capacidad WHERE codigo = 'MATERIAL_PREMEZCLA_CONFIRMAR'"))
    op.drop_table("scm_lote_premezcla_input")
    op.drop_table("scm_lote_premezcla")
    op.drop_constraint("ck_scm_emision_material_devuelta", "scm_emision_material", type_="check")
    op.drop_column("scm_emision_material", "cantidad_consumida_kg")
    op.create_check_constraint(
        "ck_scm_emision_material_devuelta", "scm_emision_material",
        "cantidad_devuelta_kg >= 0 AND cantidad_devuelta_kg <= cantidad_kg",
    )
    op.drop_constraint("ck_scm_reserva_material_emitida", "scm_reserva_material", type_="check")
    op.drop_column("scm_reserva_material", "cantidad_consumida_kg")
    op.create_check_constraint(
        "ck_scm_reserva_material_emitida", "scm_reserva_material",
        "emitida_neta_kg >= 0 AND emitida_neta_kg <= cantidad_kg",
    )
