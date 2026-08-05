"""add controlled SCM weighing annulment

Revision ID: f61d9a7c6b25
Revises: f60c8d5e6f43
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f61d9a7c6b25"
down_revision = "f60c8d5e6f43"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_scm_asignacion_plan_cantidad",
        "scm_asignacion_plan_manga_ot",
        type_="check",
    )
    op.drop_constraint(
        "ck_scm_asignacion_plan_mangas",
        "scm_asignacion_plan_manga_ot",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_asignacion_plan_cantidad",
        "scm_asignacion_plan_manga_ot",
        "cantidad_asignada_un >= 0",
    )
    op.create_check_constraint(
        "ck_scm_asignacion_plan_mangas",
        "scm_asignacion_plan_manga_ot",
        "mangas_asignadas >= 0",
    )
    op.create_table(
        "scm_anulacion_pesaje_manga",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("pesaje_id", sa.Integer(), nullable=False),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column("evidencia", sa.String(500), nullable=True),
        sa.Column("anulada_por_id", sa.Integer(), nullable=False),
        sa.Column(
            "anulada_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_devuelta_plan_un", sa.Numeric(15, 3), nullable=False),
        sa.Column("ot_reabierta", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["pesaje_id"], ["scm_pesaje_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["anulada_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_anulacion_pesaje_public_id"),
        sa.UniqueConstraint("pesaje_id", name="uq_scm_anulacion_pesaje_pesaje"),
        sa.UniqueConstraint("operation_id", name="uq_scm_anulacion_pesaje_operation"),
    )
    connection = op.get_bind()
    connection.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT 'ANULAR_PESAJE', 'Anular un pesaje SCM de forma controlada', true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = 'ANULAR_PESAJE'
        )
    """))
    connection.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo AS rol
        JOIN scm_capacidad AS capacidad ON capacidad.codigo = 'ANULAR_PESAJE'
        WHERE rol.codigo = 'JEFE_PRODUCCION'
          AND NOT EXISTS (
              SELECT 1 FROM scm_rol_capacidad AS existing
              WHERE existing.rol_operativo_id = rol.id
                AND existing.capacidad_id = capacidad.id
          )
    """))


def downgrade():
    connection = op.get_bind()
    connection.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE capacidad_id = (
            SELECT id FROM scm_capacidad WHERE codigo = 'ANULAR_PESAJE'
        )
    """))
    connection.execute(sa.text(
        "DELETE FROM scm_capacidad WHERE codigo = 'ANULAR_PESAJE'"
    ))
    op.drop_table("scm_anulacion_pesaje_manga")
    op.drop_constraint("ck_scm_asignacion_plan_mangas", "scm_asignacion_plan_manga_ot", type_="check")
    op.drop_constraint("ck_scm_asignacion_plan_cantidad", "scm_asignacion_plan_manga_ot", type_="check")
    op.create_check_constraint("ck_scm_asignacion_plan_mangas", "scm_asignacion_plan_manga_ot", "mangas_asignadas > 0")
    op.create_check_constraint("ck_scm_asignacion_plan_cantidad", "scm_asignacion_plan_manga_ot", "cantidad_asignada_un > 0")
