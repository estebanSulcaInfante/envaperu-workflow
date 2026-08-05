"""add controlled candidate and legacy assembly sources

Revision ID: f54b2d9e8a57
Revises: f53a1c8e7d46
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f54b2d9e8a57"
down_revision = "f53a1c8e7d46"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_scm_existencia_manga_logistica", "scm_existencia_manga", type_="check")
    op.create_check_constraint(
        "ck_scm_existencia_manga_logistica", "scm_existencia_manga",
        "estado_logistico IN ('RECIBIDA_ALMACEN', 'RESERVADA', 'EN_PICKING', "
        "'EN_TRANSITO_PRODUCCION', 'EN_STAGING_ARMADO', 'ABIERTA_EN_CONSUMO', "
        "'CONSUMIDA', 'AGRUPADA_CANDIDATOS', 'PENDIENTE_RETORNO', "
        "'EN_TRANSITO_ALMACEN', 'REVERSADA')",
    )
    op.create_table(
        "scm_pool_origen_armado",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("modo", sa.String(28), nullable=False),
        sa.Column("cantidad_inicial", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_disponible", sa.Numeric(15, 3), nullable=False),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column("creado_por_id", sa.Integer(), nullable=False),
        sa.Column("creado_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "modo IN ('CONJUNTO_CANDIDATOS', 'LEGACY_SIN_ORIGEN')",
            name="ck_scm_pool_origen_modo",
        ),
        sa.CheckConstraint(
            "cantidad_inicial > 0 AND cantidad_disponible >= 0 AND "
            "cantidad_disponible <= cantidad_inicial",
            name="ck_scm_pool_origen_cantidad",
        ),
        sa.ForeignKeyConstraint(["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["saldo_id"], ["scm_saldo_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["creado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_scm_pool_origen_operation"),
    )
    op.create_table(
        "scm_pool_origen_candidato",
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("existencia_manga_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["pool_id"], ["scm_pool_origen_armado.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["existencia_manga_id"], ["scm_existencia_manga.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("pool_id", "existencia_manga_id"),
    )
    op.create_table(
        "scm_asignacion_pool_armado",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("linea_id", sa.Uuid(), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_asignada", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_consumida", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("cantidad_retornada", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("estado", sa.String(32), nullable=False, server_default="RESERVADA"),
        sa.Column("asignada_por_id", sa.Integer(), nullable=False),
        sa.Column("asignada_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "estado IN ('RESERVADA', 'EN_PICKING', 'EN_TRANSITO_PRODUCCION', "
            "'EN_STAGING_ARMADO', 'ABIERTA_EN_CONSUMO', 'PENDIENTE_RETORNO', "
            "'EN_TRANSITO_ALMACEN', 'RETORNADA', 'CONSUMIDA', 'CANCELADA')",
            name="ck_scm_asignacion_pool_estado",
        ),
        sa.CheckConstraint(
            "cantidad_asignada > 0 AND cantidad_consumida >= 0 AND "
            "cantidad_retornada >= 0 AND cantidad_consumida + cantidad_retornada <= cantidad_asignada",
            name="ck_scm_asignacion_pool_cantidad",
        ),
        sa.ForeignKeyConstraint(["linea_id"], ["scm_solicitud_abastecimiento_linea.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pool_id"], ["scm_pool_origen_armado.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["saldo_id"], ["scm_saldo_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asignada_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("linea_id", "pool_id", name="uq_scm_asignacion_pool_linea"),
    )
    op.alter_column("scm_consumo_componente_armado", "asignacion_abastecimiento_id", nullable=True)
    op.add_column("scm_consumo_componente_armado", sa.Column("asignacion_pool_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_scm_consumo_armado_pool", "scm_consumo_componente_armado",
        "scm_asignacion_pool_armado", ["asignacion_pool_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_scm_consumo_armado_pool", "scm_consumo_componente_armado",
        ["confirmacion_id", "asignacion_pool_id"],
    )
    op.create_check_constraint(
        "ck_scm_consumo_armado_fuente_unica", "scm_consumo_componente_armado",
        "(asignacion_abastecimiento_id IS NOT NULL) <> (asignacion_pool_id IS NOT NULL)",
    )
    op.create_table(
        "scm_correccion_manga_armado",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("confirmacion_id", sa.Uuid(), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False, server_default="PENDIENTE"),
        sa.Column("cantidad_anterior", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_propuesta", sa.Numeric(15, 3), nullable=False),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column("solicitada_por_id", sa.Integer(), nullable=False),
        sa.Column("solicitada_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("request_operation_id", sa.Uuid(), nullable=False),
        sa.Column("resuelta_por_id", sa.Integer(), nullable=True),
        sa.Column("resuelta_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_operation_id", sa.Uuid(), nullable=True),
        sa.Column("motivo_resolucion", sa.String(500), nullable=True),
        sa.Column("efectos_json", sa.JSON(), nullable=True),
        sa.CheckConstraint("estado IN ('PENDIENTE', 'RECHAZADA', 'APLICADA')", name="ck_scm_correccion_armado_estado"),
        sa.CheckConstraint("cantidad_propuesta > 0", name="ck_scm_correccion_armado_cantidad"),
        sa.ForeignKeyConstraint(["confirmacion_id"], ["scm_confirmacion_manga_armado.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["solicitada_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resuelta_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_operation_id", name="uq_scm_correccion_armado_request"),
        sa.UniqueConstraint("approval_operation_id", name="uq_scm_correccion_armado_approval"),
    )
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT 'GENEALOGIA_LEGACY_APERTURA', 'Autorizar apertura de stock legacy contado', true
        WHERE NOT EXISTS (SELECT 1 FROM scm_capacidad WHERE codigo = 'GENEALOGIA_LEGACY_APERTURA')
    """))
    for code, name in (
        ("ENSAMBLE_CORREGIR_SOLICITAR", "Solicitar correccion de cantidad de Armado"),
        ("ENSAMBLE_CORREGIR_APROBAR", "Aprobar correccion compensatoria de Armado"),
    ):
        op.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (SELECT 1 FROM scm_capacidad WHERE codigo = :code)
        """).bindparams(code=code, name=name))
    for role, capability in (
        ("JEFE_PRODUCCION", "ENSAMBLE_CORREGIR_SOLICITAR"),
        ("JEFE_PRODUCCION", "ENSAMBLE_CORREGIR_APROBAR"),
        ("JEFE_ENSAMBLE", "ENSAMBLE_CORREGIR_SOLICITAR"),
        ("SUPERVISOR", "ENSAMBLE_CORREGIR_SOLICITAR"),
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
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id FROM rol_operativo rol
        JOIN scm_capacidad capacidad ON capacidad.codigo = 'GENEALOGIA_LEGACY_APERTURA'
        WHERE rol.codigo = 'JEFE_PRODUCCION'
        AND NOT EXISTS (
          SELECT 1 FROM scm_rol_capacidad rc
          WHERE rc.rol_operativo_id = rol.id AND rc.capacidad_id = capacidad.id
        )
    """))


def downgrade():
    op.drop_table("scm_correccion_manga_armado")
    op.drop_constraint("ck_scm_consumo_armado_fuente_unica", "scm_consumo_componente_armado", type_="check")
    op.drop_constraint("uq_scm_consumo_armado_pool", "scm_consumo_componente_armado", type_="unique")
    op.drop_constraint("fk_scm_consumo_armado_pool", "scm_consumo_componente_armado", type_="foreignkey")
    op.drop_column("scm_consumo_componente_armado", "asignacion_pool_id")
    op.alter_column("scm_consumo_componente_armado", "asignacion_abastecimiento_id", nullable=False)
    op.drop_table("scm_asignacion_pool_armado")
    op.drop_table("scm_pool_origen_candidato")
    op.drop_table("scm_pool_origen_armado")
    op.drop_constraint("ck_scm_existencia_manga_logistica", "scm_existencia_manga", type_="check")
    op.create_check_constraint(
        "ck_scm_existencia_manga_logistica", "scm_existencia_manga",
        "estado_logistico IN ('RECIBIDA_ALMACEN', 'RESERVADA', 'EN_PICKING', "
        "'EN_TRANSITO_PRODUCCION', 'EN_STAGING_ARMADO', 'ABIERTA_EN_CONSUMO', "
        "'CONSUMIDA', 'PENDIENTE_RETORNO', 'EN_TRANSITO_ALMACEN', 'REVERSADA')",
    )
