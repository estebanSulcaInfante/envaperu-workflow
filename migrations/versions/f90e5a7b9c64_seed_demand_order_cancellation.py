"""seed audited demand-order cancellation capability

Revision ID: f90e5a7b9c64
Revises: f89d4e6a8c53
Create Date: 2026-08-28
"""

from alembic import op
import sqlalchemy as sa


revision = "f90e5a7b9c64"
down_revision = "f89d4e6a8c53"
branch_labels = None
depends_on = None


CAPABILITY = (
    "OP_CANCELAR",
    "Cancelar ordenes de produccion no planificadas",
)
ROLES = ("GERENTE_GENERAL",)


def upgrade():
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
