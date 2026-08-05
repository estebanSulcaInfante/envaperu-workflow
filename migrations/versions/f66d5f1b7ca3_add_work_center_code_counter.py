"""add automatic work center code counter

Revision ID: f66d5f1b7ca3
Revises: f65c4e0a6b92
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "f66d5f1b7ca3"
down_revision = "f65c4e0a6b92"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.execute(sa.text("""
        INSERT INTO correlativo_catalogo (
            clave, prefijo, siguiente_valor, ancho
        )
        SELECT
            'CENTRO_TRABAJO',
            'CT',
            COALESCE((
                SELECT max(substring(codigo from 4)::integer) + 1
                FROM scm_centro_trabajo
                WHERE codigo ~ '^CT-[0-9]{6}$'
            ), 1),
            6
        WHERE NOT EXISTS (
            SELECT 1 FROM correlativo_catalogo
            WHERE clave = 'CENTRO_TRABAJO'
        )
    """))


def downgrade():
    connection = op.get_bind()
    connection.execute(sa.text("""
        DELETE FROM correlativo_catalogo
        WHERE clave = 'CENTRO_TRABAJO'
    """))
