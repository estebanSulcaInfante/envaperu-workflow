"""add bootstrap general manager role

Revision ID: f62e0b8d7c36
Revises: f61d9a7c6b25
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f62e0b8d7c36"
down_revision = "f61d9a7c6b25"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.execute(sa.text("""
        INSERT INTO rol_operativo (codigo, nombre, activo)
        SELECT 'GERENTE_GENERAL', 'Gerente General', true
        WHERE NOT EXISTS (
            SELECT 1 FROM rol_operativo WHERE codigo = 'GERENTE_GENERAL'
        )
    """))
    connection.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo AS rol
        CROSS JOIN scm_capacidad AS capacidad
        WHERE rol.codigo = 'GERENTE_GENERAL'
          AND NOT EXISTS (
              SELECT 1
              FROM scm_rol_capacidad AS existing
              WHERE existing.rol_operativo_id = rol.id
                AND existing.capacidad_id = capacidad.id
          )
    """))


def downgrade():
    connection = op.get_bind()
    connection.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE rol_operativo_id = (
            SELECT id FROM rol_operativo WHERE codigo = 'GERENTE_GENERAL'
        )
    """))
    connection.execute(sa.text("""
        DELETE FROM rol_operativo WHERE codigo = 'GERENTE_GENERAL'
    """))

