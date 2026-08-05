"""add controlled inventory opening batches

Revision ID: f55c3e0f9b68
Revises: f54b2d9e8a57
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f55c3e0f9b68"
down_revision = "f54b2d9e8a57"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_saldo_material_inventario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("cantidad_fisica_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("cantidad_reservada_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("cantidad_no_disponible_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "cantidad_fisica_kg >= 0 AND cantidad_reservada_kg >= 0 "
            "AND cantidad_no_disponible_kg >= 0 AND "
            "cantidad_reservada_kg + cantidad_no_disponible_kg <= cantidad_fisica_kg",
            name="ck_scm_saldo_material_cantidades",
        ),
        sa.ForeignKeyConstraint(["material_id"], ["scm_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("material_id", "ubicacion_id", name="uq_scm_saldo_material_ubicacion"),
    )
    op.create_table(
        "scm_movimiento_material_inventario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(32), nullable=False),
        sa.Column("cantidad_delta_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("saldo_fisico_resultante_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("motivo", sa.String(240), nullable=False),
        sa.Column("referencia_tipo", sa.String(40), nullable=True),
        sa.Column("referencia_id", sa.String(100), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "tipo IN ('SALDO_INICIAL', 'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', "
            "'RESERVA', 'LIBERACION_RESERVA', 'EMISION', 'DEVOLUCION', "
            "'CONSUMO', 'INGRESO_MOLIENDA')",
            name="ck_scm_movimiento_material_tipo",
        ),
        sa.CheckConstraint(
            "cantidad_delta_kg <> 0 AND saldo_fisico_resultante_kg >= 0",
            name="ck_scm_movimiento_material_cantidad",
        ),
        sa.ForeignKeyConstraint(["saldo_id"], ["scm_saldo_material_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_scm_movimiento_material_operation"),
    )
    op.create_table(
        "scm_lote_apertura_inventario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("fecha_corte", sa.Date(), nullable=False),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column("estado", sa.String(28), nullable=False, server_default="BORRADOR"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("creado_por_id", sa.Integer(), nullable=False),
        sa.Column("creado_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("enviado_por_id", sa.Integer(), nullable=True),
        sa.Column("enviado_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resuelto_por_id", sa.Integer(), nullable=True),
        sa.Column("resuelto_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("motivo_resolucion", sa.String(500), nullable=True),
        sa.Column("create_operation_id", sa.Uuid(), nullable=False),
        sa.Column("approval_operation_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "estado IN ('BORRADOR', 'PENDIENTE_APROBACION', 'APLICADO', 'RECHAZADO')",
            name="ck_scm_lote_apertura_estado",
        ),
        sa.ForeignKeyConstraint(["creado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["enviado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resuelto_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_lote_apertura_codigo"),
        sa.UniqueConstraint("create_operation_id", name="uq_scm_lote_apertura_create_operation"),
        sa.UniqueConstraint("approval_operation_id", name="uq_scm_lote_apertura_approval_operation"),
    )
    op.create_table(
        "scm_lote_apertura_linea",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lote_id", sa.Uuid(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=True),
        sa.Column("material_scm_id", sa.Integer(), nullable=True),
        sa.Column("ubicacion_codigo", sa.String(40), nullable=False),
        sa.Column("ubicacion_nombre", sa.String(120), nullable=False),
        sa.Column("cantidad", sa.Numeric(15, 3), nullable=False),
        sa.Column("estado_calidad", sa.String(16), nullable=False, server_default="LIBERADO"),
        sa.Column("observacion", sa.String(500), nullable=True),
        sa.Column("movimiento_id", sa.Uuid(), nullable=True),
        sa.Column("movimiento_material_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("cantidad > 0", name="ck_scm_lote_apertura_linea_cantidad"),
        sa.CheckConstraint(
            "estado_calidad IN ('LIBERADO', 'PENDIENTE')",
            name="ck_scm_lote_apertura_linea_calidad",
        ),
        sa.CheckConstraint(
            "(articulo_scm_id IS NOT NULL) <> (material_scm_id IS NOT NULL)",
            name="ck_scm_lote_apertura_linea_item_unico",
        ),
        sa.ForeignKeyConstraint(["lote_id"], ["scm_lote_apertura_inventario.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["material_scm_id"], ["scm_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["movimiento_id"], ["scm_movimiento_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["movimiento_material_id"], ["scm_movimiento_material_inventario.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lote_id", "articulo_scm_id", "ubicacion_codigo",
            name="uq_scm_lote_apertura_linea_fuente",
        ),
        sa.UniqueConstraint(
            "lote_id", "material_scm_id", "ubicacion_codigo",
            name="uq_scm_lote_apertura_linea_material_fuente",
        ),
    )
    for code, name in (
        ("INVENTARIO_APERTURA_PREPARAR", "Preparar lotes de apertura inicial"),
        ("INVENTARIO_APERTURA_APROBAR", "Aprobar lotes de apertura inicial"),
    ):
        op.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (SELECT 1 FROM scm_capacidad WHERE codigo = :code)
        """).bindparams(code=code, name=name))
    for role, capability in (
        ("ALMACEN_RECEPCION", "INVENTARIO_APERTURA_PREPARAR"),
        ("JEFE_PRODUCCION", "INVENTARIO_APERTURA_APROBAR"),
    ):
        op.execute(sa.text("""
            INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
            SELECT rol.id, capacidad.id FROM rol_operativo rol
            JOIN scm_capacidad capacidad ON capacidad.codigo = :capability
            WHERE rol.codigo = :role
            AND NOT EXISTS (
              SELECT 1 FROM scm_rol_capacidad rc
              WHERE rc.rol_operativo_id = rol.id AND rc.capacidad_id = capacidad.id
            )
        """).bindparams(role=role, capability=capability))


def downgrade():
    op.drop_table("scm_lote_apertura_linea")
    op.drop_table("scm_lote_apertura_inventario")
    op.drop_table("scm_movimiento_material_inventario")
    op.drop_table("scm_saldo_material_inventario")
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE capacidad_id IN (
          SELECT id FROM scm_capacidad
          WHERE codigo IN (
            'INVENTARIO_APERTURA_PREPARAR',
            'INVENTARIO_APERTURA_APROBAR'
          )
        )
    """))
    op.execute(sa.text("""
        DELETE FROM scm_capacidad
        WHERE codigo IN (
          'INVENTARIO_APERTURA_PREPARAR',
          'INVENTARIO_APERTURA_APROBAR'
        )
    """))
