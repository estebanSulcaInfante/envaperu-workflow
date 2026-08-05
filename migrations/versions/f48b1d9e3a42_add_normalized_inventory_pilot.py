"""add normalized inventory pilot

Revision ID: f48b1d9e3a42
Revises: f47a0c8e2d31
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa


revision = "f48b1d9e3a42"
down_revision = "f47a0c8e2d31"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_ubicacion_inventario",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(length=40), nullable=False),
        sa.Column("nombre", sa.String(length=120), nullable=False),
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "codigo",
            name="uq_scm_ubicacion_inventario_codigo",
        ),
    )
    op.create_table(
        "scm_saldo_inventario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column("ubicacion_id", sa.Integer(), nullable=False),
        sa.Column(
            "cantidad_fisica",
            sa.Numeric(precision=15, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "cantidad_reservada",
            sa.Numeric(precision=15, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cantidad_fisica >= 0 AND cantidad_reservada >= 0 "
            "AND cantidad_reservada <= cantidad_fisica",
            name="ck_scm_saldo_inventario_cantidades",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_scm_id"],
            ["scm_articulo.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ubicacion_id"],
            ["scm_ubicacion_inventario.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "articulo_scm_id",
            "ubicacion_id",
            name="uq_scm_saldo_inventario_articulo_ubicacion",
        ),
    )
    op.create_table(
        "scm_movimiento_inventario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=32), nullable=False),
        sa.Column(
            "cantidad_delta",
            sa.Numeric(precision=15, scale=3),
            nullable=False,
        ),
        sa.Column(
            "saldo_fisico_resultante",
            sa.Numeric(precision=15, scale=3),
            nullable=False,
        ),
        sa.Column("motivo", sa.String(length=240), nullable=False),
        sa.Column("referencia_tipo", sa.String(length=40), nullable=True),
        sa.Column("referencia_id", sa.String(length=100), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tipo IN ('SALDO_INICIAL', 'INGRESO_PRODUCCION', "
            "'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', 'CONSUMO')",
            name="ck_scm_movimiento_inventario_tipo",
        ),
        sa.CheckConstraint(
            "cantidad_delta <> 0 AND saldo_fisico_resultante >= 0",
            name="ck_scm_movimiento_inventario_cantidad",
        ),
        sa.ForeignKeyConstraint(
            ["saldo_id"],
            ["scm_saldo_inventario.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["trabajador.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_scm_movimiento_inventario_operation",
        ),
    )
    op.create_table(
        "scm_reserva_inventario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_produccion_id", sa.Uuid(), nullable=False),
        sa.Column("orden_produccion_linea_id", sa.Uuid(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column("uso", sa.String(length=40), nullable=False),
        sa.Column(
            "cantidad",
            sa.Numeric(precision=15, scale=3),
            nullable=False,
        ),
        sa.Column(
            "estado",
            sa.String(length=20),
            server_default="RESERVADA",
            nullable=False,
        ),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cantidad > 0",
            name="ck_scm_reserva_inventario_cantidad",
        ),
        sa.CheckConstraint(
            "estado IN ('RESERVADA', 'CONSUMIDA', 'LIBERADA')",
            name="ck_scm_reserva_inventario_estado",
        ),
        sa.ForeignKeyConstraint(
            ["plan_produccion_id"],
            ["scm_plan_produccion.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["orden_produccion_linea_id"],
            ["scm_orden_produccion_linea.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["saldo_id"],
            ["scm_saldo_inventario.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_scm_id"],
            ["scm_articulo.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["trabajador.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_produccion_id",
            "saldo_id",
            "orden_produccion_linea_id",
            "uso",
            name="uq_scm_reserva_inventario_plan_fuente",
        ),
    )
    capabilities = (
        ("INVENTARIO_VER", "Consultar Kardex normalizado"),
        ("INVENTARIO_SALDO_INICIAL", "Registrar saldos iniciales"),
        ("INVENTARIO_AJUSTAR", "Registrar ajustes auditados de inventario"),
    )
    for code, name in capabilities:
        op.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
             WHERE NOT EXISTS (
                SELECT 1 FROM scm_capacidad WHERE codigo = :code
             )
        """).bindparams(code=code, name=name))
    role_capabilities = {
        "ALMACEN_RECEPCION": (
            "INVENTARIO_VER",
            "INVENTARIO_SALDO_INICIAL",
        ),
        "PLANIFICACION": ("INVENTARIO_VER",),
        "GERENCIA": ("INVENTARIO_VER",),
        "SUPERVISOR": ("INVENTARIO_VER",),
        "AUDITORIA_CONSULTA": ("INVENTARIO_VER",),
        "JEFE_PRODUCCION": ("INVENTARIO_VER", "INVENTARIO_AJUSTAR"),
    }
    for role_code, capability_codes in role_capabilities.items():
        for capability_code in capability_codes:
            op.execute(sa.text("""
                INSERT INTO scm_rol_capacidad (
                    rol_operativo_id,
                    capacidad_id
                )
                SELECT role.id, capability.id
                  FROM rol_operativo role
                  JOIN scm_capacidad capability
                    ON capability.codigo = :capability_code
                 WHERE role.codigo = :role_code
                   AND NOT EXISTS (
                       SELECT 1
                         FROM scm_rol_capacidad relation
                        WHERE relation.rol_operativo_id = role.id
                          AND relation.capacidad_id = capability.id
                   )
            """).bindparams(
                role_code=role_code,
                capability_code=capability_code,
            ))


def downgrade():
    op.drop_table("scm_reserva_inventario")
    op.drop_table("scm_movimiento_inventario")
    op.drop_table("scm_saldo_inventario")
    op.drop_table("scm_ubicacion_inventario")
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
         WHERE capacidad_id IN (
             SELECT id FROM scm_capacidad
              WHERE codigo IN (
                  'INVENTARIO_VER',
                  'INVENTARIO_SALDO_INICIAL',
                  'INVENTARIO_AJUSTAR'
              )
         )
    """))
    op.execute(sa.text("""
        DELETE FROM scm_capacidad
         WHERE codigo IN (
             'INVENTARIO_VER',
             'INVENTARIO_SALDO_INICIAL',
             'INVENTARIO_AJUSTAR'
         )
    """))
