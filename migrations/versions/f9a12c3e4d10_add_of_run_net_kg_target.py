"""Persist net kilogram target for fabrication runs.

Revision ID: f9a12c3e4d10
Revises: f99a1b2c3d09
"""

from alembic import op
import sqlalchemy as sa


revision = "f9a12c3e4d10"
down_revision = "f99a1b2c3d09"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_corrida_fabricacion") as batch:
        batch.add_column(
            sa.Column("objetivo_neto_kg", sa.Numeric(15, 6), nullable=True),
        )
        batch.create_check_constraint(
            "ck_scm_corrida_objetivo_neto_kg",
            "objetivo_neto_kg IS NULL OR objetivo_neto_kg > 0",
        )
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, descripcion, activo)
        SELECT 'FORMULACION_PUBLICAR_DIRECTO',
               'Aprobar y publicar formulaciones de material',
               'Creada por f9a12c3e4d10 para publicación explícita de formulaciones.',
               true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad
            WHERE codigo = 'FORMULACION_PUBLICAR_DIRECTO'
        )
    """))


def downgrade():
    # Leave the capability row in place: deleting it would erase any role
    # assignments created after upgrade. Older code ignores this unused row.
    with op.batch_alter_table("scm_corrida_fabricacion") as batch:
        batch.drop_constraint("ck_scm_corrida_objetivo_neto_kg", type_="check")
        batch.drop_column("objetivo_neto_kg")
