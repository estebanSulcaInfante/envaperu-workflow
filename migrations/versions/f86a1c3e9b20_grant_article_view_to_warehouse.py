"""grant article lookup to warehouse operators

Revision ID: f86a1c3e9b20
Revises: f85c4b2d7a11
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa


revision = "f86a1c3e9b20"
down_revision = "f85c4b2d7a11"
branch_labels = None
depends_on = None


ROLE_CODE = "ALMACEN_RECEPCION"
CAPABILITY_CODE = "ARTICULO_VER"


def upgrade():
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo rol
        JOIN scm_capacidad capacidad ON capacidad.codigo = :capability_code
        WHERE rol.codigo = :role_code
          AND NOT EXISTS (
            SELECT 1
            FROM scm_rol_capacidad relacion
            WHERE relacion.rol_operativo_id = rol.id
              AND relacion.capacidad_id = capacidad.id
          )
    """).bindparams(
        role_code=ROLE_CODE,
        capability_code=CAPABILITY_CODE,
    ))

def downgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE rol_operativo_id IN (
            SELECT id FROM rol_operativo WHERE codigo = :role_code
        )
          AND capacidad_id IN (
            SELECT id FROM scm_capacidad WHERE codigo = :capability_code
          )
    """).bindparams(
        role_code=ROLE_CODE,
        capability_code=CAPABILITY_CODE,
    ))
