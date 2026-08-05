"""add direct structure publication for leadership roles

Revision ID: f65c4e0a6b92
Revises: f64b3d9e5a81
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "f65c4e0a6b92"
down_revision = "f64b3d9e5a81"
branch_labels = None
depends_on = None


CAPABILITY_CODE = "ESTRUCTURA_PUBLICAR_DIRECTO"
LEADERSHIP_ROLE_PREDICATE = "(left(rol.codigo, 5) = 'JEFE_' OR rol.codigo IN ('GERENCIA', 'GERENTE_GENERAL'))"


def upgrade():
    connection = op.get_bind()
    connection.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, descripcion, activo)
        SELECT
            'ESTRUCTURA_PUBLICAR_DIRECTO',
            'Publicar estructuras directamente como jefatura',
            'Permite publicar un borrador BOM sin solicitud de aprobación separada.',
            true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad
            WHERE codigo = 'ESTRUCTURA_PUBLICAR_DIRECTO'
        )
    """))
    connection.execute(sa.text(f"""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo AS rol
        JOIN scm_capacidad AS capacidad
          ON capacidad.codigo IN (
              'ESTRUCTURA_ADMINISTRAR',
              '{CAPABILITY_CODE}'
          )
        WHERE {LEADERSHIP_ROLE_PREDICATE}
          AND NOT EXISTS (
              SELECT 1
              FROM scm_rol_capacidad AS existing
              WHERE existing.rol_operativo_id = rol.id
                AND existing.capacidad_id = capacidad.id
          )
    """))


def downgrade():
    connection = op.get_bind()
    connection.execute(sa.text(f"""
        DELETE FROM scm_rol_capacidad AS assignment
        USING rol_operativo AS rol, scm_capacidad AS capacidad
        WHERE assignment.rol_operativo_id = rol.id
          AND assignment.capacidad_id = capacidad.id
          AND capacidad.codigo = '{CAPABILITY_CODE}'
          AND {LEADERSHIP_ROLE_PREDICATE}
    """))
    connection.execute(sa.text(f"""
        DELETE FROM scm_capacidad
        WHERE codigo = '{CAPABILITY_CODE}'
          AND NOT EXISTS (
              SELECT 1 FROM scm_rol_capacidad
              WHERE capacidad_id = scm_capacidad.id
          )
    """))
