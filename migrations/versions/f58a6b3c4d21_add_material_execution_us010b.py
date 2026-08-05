"""add material execution us010b

Revision ID: f58a6b3c4d21
Revises: f57e5a2b3c10
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f58a6b3c4d21"
down_revision = "f57e5a2b3c10"
branch_labels = None
depends_on = None


CAPABILITIES = (
    ("MATERIAL_REQUERIMIENTO_GENERAR", "Generar requerimientos de material de una OF"),
    ("MATERIAL_RESERVAR", "Reservar material para una OF"),
    ("MATERIAL_EMITIR", "Emitir material reservado a Produccion"),
    ("MATERIAL_DEVOLVER", "Devolver material emitido al almacen"),
)


def upgrade():
    op.create_table(
        "scm_requerimiento_material",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("corrida_fabricacion_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("tipo_componente", sa.String(20), nullable=False),
        sa.Column("cantidad_plan_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("receta_revision_id", sa.Integer(), nullable=False),
        sa.Column("calculo_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cantidad_plan_kg > 0", name="ck_scm_req_material_cantidad"),
        sa.CheckConstraint(
            "tipo_componente IN ('MATERIA_PRIMA', 'COLORANTE', 'ADITIVO')",
            name="ck_scm_req_material_tipo",
        ),
        sa.ForeignKeyConstraint(["corrida_fabricacion_id"], ["scm_corrida_fabricacion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["material_id"], ["scm_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["receta_revision_id"], ["receta_color_maestra.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("corrida_fabricacion_id", "material_id", name="uq_scm_req_material_corrida_material"),
    )
    op.create_table(
        "scm_reserva_material",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("requerimiento_id", sa.Uuid(), nullable=False),
        sa.Column("saldo_material_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("emitida_neta_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("estado", sa.String(16), nullable=False, server_default="ACTIVA"),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cantidad_kg > 0", name="ck_scm_reserva_material_cantidad"),
        sa.CheckConstraint("emitida_neta_kg >= 0 AND emitida_neta_kg <= cantidad_kg", name="ck_scm_reserva_material_emitida"),
        sa.CheckConstraint("estado IN ('ACTIVA', 'LIBERADA')", name="ck_scm_reserva_material_estado"),
        sa.ForeignKeyConstraint(["requerimiento_id"], ["scm_requerimiento_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["saldo_material_id"], ["scm_saldo_material_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("requerimiento_id", "saldo_material_id", name="uq_scm_reserva_material_fuente"),
    )
    op.create_table(
        "scm_emision_material",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reserva_id", sa.Uuid(), nullable=False),
        sa.Column("saldo_destino_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_devuelta_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("motivo", sa.String(240), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cantidad_kg > 0", name="ck_scm_emision_material_cantidad"),
        sa.CheckConstraint("cantidad_devuelta_kg >= 0 AND cantidad_devuelta_kg <= cantidad_kg", name="ck_scm_emision_material_devuelta"),
        sa.ForeignKeyConstraint(["reserva_id"], ["scm_reserva_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["saldo_destino_id"], ["scm_saldo_material_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_scm_emision_material_operation"),
    )
    op.create_table(
        "scm_devolucion_material",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("emision_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("motivo", sa.String(240), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cantidad_kg > 0", name="ck_scm_devolucion_material_cantidad"),
        sa.ForeignKeyConstraint(["emision_id"], ["scm_emision_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_scm_devolucion_material_operation"),
    )
    for code, name in CAPABILITIES:
        op.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (SELECT 1 FROM scm_capacidad WHERE codigo = :code)
        """).bindparams(code=code, name=name))
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo rol
        JOIN scm_capacidad capacidad ON (
          (rol.codigo = 'JEFE_PRODUCCION' AND capacidad.codigo IN (
            'MATERIAL_REQUERIMIENTO_GENERAR', 'MATERIAL_RESERVAR'
          )) OR
          (rol.codigo = 'ALMACEN_RECEPCION' AND capacidad.codigo IN (
            'MATERIAL_EMITIR', 'MATERIAL_DEVOLVER'
          ))
        )
        WHERE NOT EXISTS (
          SELECT 1 FROM scm_rol_capacidad rc
          WHERE rc.rol_operativo_id = rol.id AND rc.capacidad_id = capacidad.id
        )
    """))


def downgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE capacidad_id IN (
          SELECT id FROM scm_capacidad WHERE codigo IN (
            'MATERIAL_REQUERIMIENTO_GENERAR', 'MATERIAL_RESERVAR',
            'MATERIAL_EMITIR', 'MATERIAL_DEVOLVER'
          )
        )
    """))
    op.execute(sa.text("""
        DELETE FROM scm_capacidad WHERE codigo IN (
          'MATERIAL_REQUERIMIENTO_GENERAR', 'MATERIAL_RESERVAR',
          'MATERIAL_EMITIR', 'MATERIAL_DEVOLVER'
        )
    """))
    op.drop_table("scm_devolucion_material")
    op.drop_table("scm_emision_material")
    op.drop_table("scm_reserva_material")
    op.drop_table("scm_requerimiento_material")
