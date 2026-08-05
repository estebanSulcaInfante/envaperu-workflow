"""allow production manager to generate manga prelabels

Revision ID: f47a0c8e2d31
Revises: f46f9b7d1c20
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "f47a0c8e2d31"
down_revision = "f46f9b7d1c20"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT role.id, capability.id
          FROM rol_operativo role
          JOIN scm_capacidad capability
            ON capability.codigo = 'MANGA_ETIQUETA_PRE_GENERAR'
         WHERE role.codigo = 'JEFE_PRODUCCION'
           AND NOT EXISTS (
               SELECT 1
                 FROM scm_rol_capacidad relation
                WHERE relation.rol_operativo_id = role.id
                  AND relation.capacidad_id = capability.id
           )
    """))


def downgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
         WHERE rol_operativo_id IN (
                   SELECT id
                     FROM rol_operativo
                    WHERE codigo = 'JEFE_PRODUCCION'
               )
           AND capacidad_id IN (
                   SELECT id
                     FROM scm_capacidad
                    WHERE codigo = 'MANGA_ETIQUETA_PRE_GENERAR'
               )
    """))
