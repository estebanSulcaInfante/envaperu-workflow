"""seed capability for reconciled fabrication closure

Revision ID: f85c4b2d7a11
Revises: f85e4b2d7a10
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa


revision = "f85c4b2d7a11"
down_revision = "f85e4b2d7a10"
branch_labels = None
depends_on = None


CAPABILITY = (
    "OF_CERRAR",
    "Cerrar ordenes de fabricacion y acreditar demanda",
)
ROLES = ("GERENTE_GENERAL", "JEFE_PRODUCCION")


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
