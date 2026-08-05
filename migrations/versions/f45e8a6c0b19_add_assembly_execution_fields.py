"""add assembly execution fields

Revision ID: f45e8a6c0b19
Revises: f44d9f5a1b08
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "f45e8a6c0b19"
down_revision = "f44d9f5a1b08"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("scm_orden_operacion", sa.Column(
        "started_by_id", sa.Integer(), nullable=True,
    ))
    op.add_column("scm_orden_operacion", sa.Column(
        "started_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.add_column("scm_orden_operacion", sa.Column(
        "closed_by_id", sa.Integer(), nullable=True,
    ))
    op.add_column("scm_orden_operacion", sa.Column(
        "closed_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.create_foreign_key(
        "fk_scm_orden_operacion_started_by",
        "scm_orden_operacion", "trabajador",
        ["started_by_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scm_orden_operacion_closed_by",
        "scm_orden_operacion", "trabajador",
        ["closed_by_id"], ["id"], ondelete="RESTRICT",
    )
    op.add_column("scm_orden_operacion_salida", sa.Column(
        "cantidad_real", sa.Numeric(15, 3), nullable=True,
    ))
    op.add_column("scm_orden_operacion_salida", sa.Column(
        "cantidad_rechazada", sa.Numeric(15, 3), nullable=True,
    ))
    op.create_check_constraint(
        "ck_scm_salida_cantidades_reales",
        "scm_orden_operacion_salida",
        "(cantidad_real IS NULL OR cantidad_real >= 0) AND "
        "(cantidad_rechazada IS NULL OR cantidad_rechazada >= 0)",
    )
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT 'OE_EJECUTAR', 'Ejecutar y cerrar ordenes de ensamble', true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = 'OE_EJECUTAR'
        )
    """))
    for role_code in ("SUPERVISOR", "JEFE_PRODUCCION"):
        op.execute(sa.text("""
            INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
            SELECT role.id, capability.id
              FROM rol_operativo role
              JOIN scm_capacidad capability
                ON capability.codigo = 'OE_EJECUTAR'
             WHERE role.codigo = :role_code
               AND NOT EXISTS (
                   SELECT 1 FROM scm_rol_capacidad relation
                    WHERE relation.rol_operativo_id = role.id
                      AND relation.capacidad_id = capability.id
               )
        """).bindparams(role_code=role_code))


def downgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
         WHERE capacidad_id IN (
             SELECT id FROM scm_capacidad WHERE codigo = 'OE_EJECUTAR'
         )
    """))
    op.execute(sa.text(
        "DELETE FROM scm_capacidad WHERE codigo = 'OE_EJECUTAR'"
    ))
    op.drop_constraint(
        "ck_scm_salida_cantidades_reales",
        "scm_orden_operacion_salida",
        type_="check",
    )
    op.drop_column("scm_orden_operacion_salida", "cantidad_rechazada")
    op.drop_column("scm_orden_operacion_salida", "cantidad_real")
    op.drop_constraint(
        "fk_scm_orden_operacion_closed_by",
        "scm_orden_operacion",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_scm_orden_operacion_started_by",
        "scm_orden_operacion",
        type_="foreignkey",
    )
    op.drop_column("scm_orden_operacion", "closed_at")
    op.drop_column("scm_orden_operacion", "closed_by_id")
    op.drop_column("scm_orden_operacion", "started_at")
    op.drop_column("scm_orden_operacion", "started_by_id")
