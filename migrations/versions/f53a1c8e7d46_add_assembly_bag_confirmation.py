"""add assembly bag confirmation and exact component consumption

Revision ID: f53a1c8e7d46
Revises: f52e0b8d7c35
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f53a1c8e7d46"
down_revision = "f52e0b8d7c35"
branch_labels = None
depends_on = None


def _capability(code, name):
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT :code, :name, true
         WHERE NOT EXISTS (SELECT 1 FROM scm_capacidad WHERE codigo = :code)
    """).bindparams(code=code, name=name))


def _assign(role, capability):
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
          FROM rol_operativo rol
          JOIN scm_capacidad capacidad ON capacidad.codigo = :capability
         WHERE rol.codigo = :role
           AND NOT EXISTS (
             SELECT 1 FROM scm_rol_capacidad relation
              WHERE relation.rol_operativo_id = rol.id
                AND relation.capacidad_id = capacidad.id
           )
    """).bindparams(role=role, capability=capability))


def upgrade():
    op.drop_constraint("ck_scm_manga_estado", "scm_manga", type_="check")
    op.create_check_constraint(
        "ck_scm_manga_estado",
        "scm_manga",
        "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'EN_ARMADO', "
        "'CERRADA_ARMADO_PENDIENTE_PESAJE', 'PESADA', "
        "'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', "
        "'RECIBIDA', 'ANULADA')",
    )
    op.drop_constraint(
        "ck_scm_pesaje_manga_fuente_cantidad",
        "scm_pesaje_manga",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_pesaje_manga_fuente_cantidad",
        "scm_pesaje_manga",
        "fuente_cantidad IN ('PLAN_CONFIRMADO_POR_PESAJE', "
        "'RESPONSABLE_ARMADO', 'CORRECCION_AUTORIZADA')",
    )
    op.drop_constraint(
        "ck_scm_existencia_manga_cantidad",
        "scm_existencia_manga",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_existencia_manga_cantidad",
        "scm_existencia_manga",
        "cantidad_fisica >= 0 AND cantidad_reservada >= 0 "
        "AND cantidad_reservada <= cantidad_fisica AND "
        "((estado_logistico = 'CONSUMIDA' AND cantidad_fisica = 0) OR "
        "(estado_logistico <> 'CONSUMIDA' AND cantidad_fisica > 0))",
    )

    op.create_table(
        "scm_confirmacion_manga_armado",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("orden_ensamble_id", sa.Uuid(), nullable=False),
        sa.Column("orden_trabajo_id", sa.Integer(), nullable=False),
        sa.Column("articulo_salida_id", sa.Integer(), nullable=False),
        sa.Column("estructura_revision_id", sa.Integer(), nullable=False),
        sa.Column("estructura_hash", sa.String(length=64), nullable=False),
        sa.Column("cantidad_planificada", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_real", sa.Numeric(15, 3), nullable=False),
        sa.Column("diferencia_cantidad", sa.Numeric(15, 3), nullable=False),
        sa.Column("motivo_diferencia", sa.String(length=500), nullable=True),
        sa.Column("confirmado_por_id", sa.Integer(), nullable=False),
        sa.Column(
            "confirmado_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "cantidad_planificada > 0 AND cantidad_real > 0",
            name="ck_scm_confirmacion_armado_cantidad",
        ),
        sa.CheckConstraint(
            "length(estructura_hash) = 64 AND length(payload_hash) = 64",
            name="ck_scm_confirmacion_armado_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["orden_ensamble_id"], ["scm_orden_operacion.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["orden_trabajo_id"], ["registro_diario_produccion.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["articulo_salida_id"], ["scm_articulo.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["estructura_revision_id"], ["scm_estructura_revision.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["confirmado_por_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manga_id", name="uq_scm_confirmacion_armado_manga"),
        sa.UniqueConstraint(
            "operation_id", name="uq_scm_confirmacion_armado_operation"
        ),
    )

    capabilities = (
        ("GENEALOGIA_VER", "Consultar genealogia exacta de mangas"),
        ("ENSAMBLE_PLANIFICAR", "Planificar mangas de salida de Ensamble"),
        (
            "ENSAMBLE_MANGA_CERRAR",
            "Confirmar cantidad y consumos de una manga de Armado",
        ),
    )
    for code, name in capabilities:
        _capability(code, name)
    for role in (
        "GERENCIA", "AUDITORIA_CONSULTA", "SUPERVISOR",
        "PLANIFICACION", "JEFE_PRODUCCION", "JEFE_ENSAMBLE",
        "ALMACEN_RECEPCION",
    ):
        _assign(role, "GENEALOGIA_VER")
    for role in ("PLANIFICACION", "JEFE_PRODUCCION", "JEFE_ENSAMBLE"):
        _assign(role, "ENSAMBLE_PLANIFICAR")
    for role in ("SUPERVISOR", "JEFE_PRODUCCION", "JEFE_ENSAMBLE"):
        _assign(role, "ENSAMBLE_MANGA_CERRAR")
    op.create_table(
        "scm_consumo_componente_armado",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("confirmacion_id", sa.Uuid(), nullable=False),
        sa.Column("asignacion_abastecimiento_id", sa.Uuid(), nullable=False),
        sa.Column("articulo_componente_id", sa.Integer(), nullable=False),
        sa.Column("cantidad_incorporada", sa.Numeric(15, 3), nullable=False),
        sa.Column(
            "cantidad_merma", sa.Numeric(15, 3), nullable=False,
            server_default="0",
        ),
        sa.Column(
            "nivel_genealogia", sa.String(length=28), nullable=False,
            server_default="EXACTA",
        ),
        sa.CheckConstraint(
            "cantidad_incorporada > 0 AND cantidad_merma >= 0",
            name="ck_scm_consumo_armado_cantidad",
        ),
        sa.CheckConstraint(
            "nivel_genealogia IN ('EXACTA', 'CONJUNTO_CANDIDATOS', "
            "'LEGACY_SIN_ORIGEN')",
            name="ck_scm_consumo_armado_genealogia",
        ),
        sa.ForeignKeyConstraint(
            ["confirmacion_id"], ["scm_confirmacion_manga_armado.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asignacion_abastecimiento_id"],
            ["scm_asignacion_abastecimiento.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_componente_id"], ["scm_articulo.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "confirmacion_id", "asignacion_abastecimiento_id",
            name="uq_scm_consumo_armado_asignacion",
        ),
    )


def downgrade():
    op.drop_table("scm_consumo_componente_armado")
    op.drop_table("scm_confirmacion_manga_armado")
    op.drop_constraint(
        "ck_scm_pesaje_manga_fuente_cantidad",
        "scm_pesaje_manga",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_pesaje_manga_fuente_cantidad",
        "scm_pesaje_manga",
        "fuente_cantidad IN ('PLAN_CONFIRMADO_POR_PESAJE', "
        "'CORRECCION_AUTORIZADA')",
    )
    op.drop_constraint(
        "ck_scm_existencia_manga_cantidad",
        "scm_existencia_manga",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_existencia_manga_cantidad",
        "scm_existencia_manga",
        "cantidad_fisica > 0 AND cantidad_reservada >= 0 "
        "AND cantidad_reservada <= cantidad_fisica",
    )
    op.drop_constraint("ck_scm_manga_estado", "scm_manga", type_="check")
    op.create_check_constraint(
        "ck_scm_manga_estado",
        "scm_manga",
        "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'PESADA', "
        "'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', "
        "'RECIBIDA', 'ANULADA')",
    )
