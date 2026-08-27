"""add supervised partial manga close

Revision ID: f87b2d4e6a31
Revises: e5a72c4d9f10
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa


revision = "f87b2d4e6a31"
down_revision = "e5a72c4d9f10"
branch_labels = None
depends_on = None


CAPABILITY = (
    "MANGA_FINALIZAR_PARCIAL",
    "Finalizar una manga incompleta y devolver el saldo al plan",
)
ROLES = ("GERENTE_GENERAL", "SUPERVISOR", "JEFE_PRODUCCION")


def upgrade():
    with op.batch_alter_table("scm_pesaje_manga") as batch:
        batch.drop_constraint(
            "ck_scm_pesaje_manga_fuente_cantidad", type_="check"
        )
        batch.create_check_constraint(
            "ck_scm_pesaje_manga_fuente_cantidad",
            "fuente_cantidad IN ('PLAN_CONFIRMADO_POR_PESAJE', "
            "'RESPONSABLE_ARMADO', 'CORRECCION_AUTORIZADA', "
            "'CIERRE_PARCIAL_SUPERVISADO')",
        )

    code, name = CAPABILITY
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT :code, :name, true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = :code
        )
    """).bindparams(code=code, name=name))
    for role_code in ROLES:
        op.execute(sa.text("""
            INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
            SELECT rol.id, capacidad.id
            FROM rol_operativo rol
            JOIN scm_capacidad capacidad ON capacidad.codigo = :capability
            WHERE rol.codigo = :role
              AND NOT EXISTS (
                SELECT 1 FROM scm_rol_capacidad relacion
                WHERE relacion.rol_operativo_id = rol.id
                  AND relacion.capacidad_id = capacidad.id
              )
        """).bindparams(role=role_code, capability=code))


def downgrade():
    code, _name = CAPABILITY
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE capacidad_id IN (
            SELECT id FROM scm_capacidad WHERE codigo = :code
        )
    """).bindparams(code=code))
    op.execute(sa.text(
        "DELETE FROM scm_capacidad WHERE codigo = :code"
    ).bindparams(code=code))

    op.execute(sa.text("""
        UPDATE scm_pesaje_manga
        SET fuente_cantidad = 'PLAN_CONFIRMADO_POR_PESAJE'
        WHERE fuente_cantidad = 'CIERRE_PARCIAL_SUPERVISADO'
    """))
    with op.batch_alter_table("scm_pesaje_manga") as batch:
        batch.drop_constraint(
            "ck_scm_pesaje_manga_fuente_cantidad", type_="check"
        )
        batch.create_check_constraint(
            "ck_scm_pesaje_manga_fuente_cantidad",
            "fuente_cantidad IN ('PLAN_CONFIRMADO_POR_PESAJE', "
            "'RESPONSABLE_ARMADO', 'CORRECCION_AUTORIZADA')",
        )
