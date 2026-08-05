"""add warehouse manga receiving and quality availability

Revision ID: f51d9a7c6b24
Revises: f50c8a6b4e13
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f51d9a7c6b24"
down_revision = "f50c8a6b4e13"
branch_labels = None
depends_on = None


def _insert_capability(code, name):
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT :code, :name, true
         WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = :code
         )
    """).bindparams(code=code, name=name))


def _assign(role_code, capability_code):
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
          FROM rol_operativo rol
          JOIN scm_capacidad capacidad ON capacidad.codigo = :capability
         WHERE rol.codigo = :role
           AND NOT EXISTS (
                SELECT 1
                  FROM scm_rol_capacidad relation
                 WHERE relation.rol_operativo_id = rol.id
                   AND relation.capacidad_id = capacidad.id
           )
    """).bindparams(role=role_code, capability=capability_code))


def upgrade():
    op.add_column(
        "scm_ubicacion_inventario",
        sa.Column(
            "clases_articulo_json",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.add_column(
        "scm_saldo_inventario",
        sa.Column(
            "cantidad_no_disponible",
            sa.Numeric(precision=15, scale=3),
            server_default="0",
            nullable=False,
        ),
    )
    op.drop_constraint(
        "ck_scm_saldo_inventario_cantidades",
        "scm_saldo_inventario",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_saldo_inventario_cantidades",
        "scm_saldo_inventario",
        "cantidad_fisica >= 0 AND cantidad_reservada >= 0 "
        "AND cantidad_no_disponible >= 0 "
        "AND cantidad_reservada + cantidad_no_disponible <= cantidad_fisica",
    )
    op.drop_constraint("ck_scm_manga_estado", "scm_manga", type_="check")
    op.create_check_constraint(
        "ck_scm_manga_estado",
        "scm_manga",
        "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'PESADA', "
        "'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', "
        "'RECIBIDA', 'ANULADA')",
    )

    op.create_table(
        "scm_sesion_recepcion_manga",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(48), nullable=False),
        sa.Column("punto_ingreso", sa.String(80), nullable=False),
        sa.Column("estado", sa.String(16), server_default="ABIERTA", nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column(
            "abierta_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("cerrada_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "estado IN ('ABIERTA', 'CERRADA')",
            name="ck_scm_sesion_recepcion_estado",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_sesion_recepcion_codigo"),
    )
    op.create_table(
        "scm_existencia_manga",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("sesion_id", sa.Uuid()),
        sa.Column("etiqueta_resuelta_id", sa.Integer(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("movimiento_ingreso_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("resuelta_por", sa.String(24), nullable=False),
        sa.Column(
            "estado_logistico",
            sa.String(32),
            server_default="RECIBIDA_ALMACEN",
            nullable=False,
        ),
        sa.Column(
            "estado_calidad",
            sa.String(20),
            server_default="PENDIENTE",
            nullable=False,
        ),
        sa.Column("cantidad_fisica", sa.Numeric(15, 3), nullable=False),
        sa.Column(
            "cantidad_reservada",
            sa.Numeric(15, 3),
            server_default="0",
            nullable=False,
        ),
        sa.Column("peso_neto_snapshot_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("recibida_por_id", sa.Integer(), nullable=False),
        sa.Column(
            "recibida_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("calidad_por_id", sa.Integer()),
        sa.Column("calidad_at", sa.DateTime(timezone=True)),
        sa.Column("calidad_motivo", sa.String(500)),
        sa.Column("calidad_evidencia", sa.String(500)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "estado_logistico IN ('RECIBIDA_ALMACEN', 'REVERSADA')",
            name="ck_scm_existencia_manga_logistica",
        ),
        sa.CheckConstraint(
            "estado_calidad IN ('PENDIENTE', 'LIBERADA', 'BLOQUEADA', 'RECHAZADA')",
            name="ck_scm_existencia_manga_calidad",
        ),
        sa.CheckConstraint(
            "resuelta_por IN ('QR_FINAL', 'QR_PREETIQUETA', 'CODIGO_MANUAL')",
            name="ck_scm_existencia_manga_resolucion",
        ),
        sa.CheckConstraint(
            "cantidad_fisica > 0 AND cantidad_reservada >= 0 "
            "AND cantidad_reservada <= cantidad_fisica",
            name="ck_scm_existencia_manga_cantidad",
        ),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["sesion_id"], ["scm_sesion_recepcion_manga.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["etiqueta_resuelta_id"], ["scm_etiqueta_manga.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["saldo_id"], ["scm_saldo_inventario.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["movimiento_ingreso_id"],
            ["scm_movimiento_inventario.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["recibida_por_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["calidad_por_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manga_id", name="uq_scm_existencia_manga_manga"),
        sa.UniqueConstraint(
            "movimiento_ingreso_id", name="uq_scm_existencia_manga_movimiento"
        ),
        sa.UniqueConstraint(
            "operation_id", name="uq_scm_existencia_manga_operation"
        ),
    )
    op.create_table(
        "scm_rechazo_recepcion_manga",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("etiqueta_resuelta_id", sa.Integer()),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column("evidencia", sa.String(500)),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["etiqueta_resuelta_id"], ["scm_etiqueta_manga.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", name="uq_scm_rechazo_recepcion_operation"
        ),
    )

    capabilities = (
        ("RECEPCION_MANGA_VER", "Consultar recepcion de mangas"),
        ("RECEPCION_MANGA_CONFIRMAR", "Confirmar ingreso de mangas al almacen"),
        ("RECEPCION_MANGA_RECHAZAR", "Rechazar mangas antes de recibirlas"),
        ("RECEPCION_MANGA_BUSCAR_MANUAL", "Buscar mangas manualmente durante la recepcion"),
        ("CALIDAD_MANGA_VER", "Consultar mangas pendientes de calidad"),
        ("CALIDAD_MANGA_LIBERAR", "Liberar existencias de manga"),
        ("CALIDAD_MANGA_BLOQUEAR", "Bloquear existencias de manga"),
        ("CALIDAD_MANGA_RECHAZAR", "Rechazar existencias de manga"),
    )
    for code, name in capabilities:
        _insert_capability(code, name)

    for role in (
        "ALMACEN_RECEPCION", "CALIDAD", "GERENCIA", "SUPERVISOR",
        "JEFE_PRODUCCION", "AUDITORIA_CONSULTA",
    ):
        _assign(role, "RECEPCION_MANGA_VER")
    for role in ("CALIDAD", "GERENCIA", "SUPERVISOR", "JEFE_PRODUCCION", "AUDITORIA_CONSULTA"):
        _assign(role, "CALIDAD_MANGA_VER")
    for capability in (
        "RECEPCION_MANGA_CONFIRMAR",
        "RECEPCION_MANGA_RECHAZAR",
        "RECEPCION_MANGA_BUSCAR_MANUAL",
    ):
        _assign("ALMACEN_RECEPCION", capability)
    for capability in (
        "CALIDAD_MANGA_LIBERAR",
        "CALIDAD_MANGA_BLOQUEAR",
        "CALIDAD_MANGA_RECHAZAR",
    ):
        _assign("CALIDAD", capability)

    op.execute(sa.text("""
        INSERT INTO scm_ubicacion_inventario
            (codigo, nombre, clases_articulo_json, activo)
        SELECT 'RECEPCION_PIEZAS_WIP', 'Recepcion de piezas y WIP',
               '["PIEZA_COLOR", "SUBENSAMBLE_WIP"]', true
         WHERE NOT EXISTS (
            SELECT 1 FROM scm_ubicacion_inventario
             WHERE codigo = 'RECEPCION_PIEZAS_WIP'
         )
    """))
    op.execute(sa.text("""
        INSERT INTO scm_ubicacion_inventario
            (codigo, nombre, clases_articulo_json, activo)
        SELECT 'RECEPCION_PT', 'Recepcion de producto terminado',
               '["PRODUCTO_TERMINADO"]', true
         WHERE NOT EXISTS (
            SELECT 1 FROM scm_ubicacion_inventario
             WHERE codigo = 'RECEPCION_PT'
         )
    """))


def downgrade():
    op.drop_table("scm_rechazo_recepcion_manga")
    op.drop_table("scm_existencia_manga")
    op.drop_table("scm_sesion_recepcion_manga")
    op.drop_constraint("ck_scm_manga_estado", "scm_manga", type_="check")
    op.create_check_constraint(
        "ck_scm_manga_estado",
        "scm_manga",
        "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'PESADA', "
        "'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', 'ANULADA')",
    )
    op.drop_constraint(
        "ck_scm_saldo_inventario_cantidades",
        "scm_saldo_inventario",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_saldo_inventario_cantidades",
        "scm_saldo_inventario",
        "cantidad_fisica >= 0 AND cantidad_reservada >= 0 "
        "AND cantidad_reservada <= cantidad_fisica",
    )
    op.drop_column("scm_saldo_inventario", "cantidad_no_disponible")
    op.drop_column("scm_ubicacion_inventario", "clases_articulo_json")
