"""enforce controlled inventory opening

Revision ID: f56d4f1a2c09
Revises: f55c3e0f9b68
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f56d4f1a2c09"
down_revision = "f55c3e0f9b68"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE rol_operativo_id = (
          SELECT id FROM rol_operativo WHERE codigo = 'ALMACEN_RECEPCION'
        )
        AND capacidad_id = (
          SELECT id FROM scm_capacidad WHERE codigo = 'INVENTARIO_SALDO_INICIAL'
        )
    """))


def downgrade():
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo rol
        JOIN scm_capacidad capacidad
          ON capacidad.codigo = 'INVENTARIO_SALDO_INICIAL'
        WHERE rol.codigo = 'ALMACEN_RECEPCION'
        AND NOT EXISTS (
          SELECT 1 FROM scm_rol_capacidad rc
          WHERE rc.rol_operativo_id = rol.id
          AND rc.capacidad_id = capacidad.id
        )
    """))
