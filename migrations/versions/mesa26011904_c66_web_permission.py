"""Acceso web del PDA, Almacén y Gerencia al conteo de piezas."""
from alembic import op
import sqlalchemy as sa


revision = "mesa26011904"
down_revision = "mesa26011903"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        INSERT INTO scm_capacidad(codigo, nombre, descripcion, activo)
        VALUES ('INVENTARIO_CONTEO_C66', 'Contar mangas por pieza',
                'Abrir, registrar y cerrar conteos físicos del almacén de piezas.', true)
    """))
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad(rol_operativo_id, capacidad_id)
        SELECT rol.id, cap.id FROM rol_operativo rol CROSS JOIN scm_capacidad cap
        WHERE rol.codigo IN ('PDA_C66', 'ALMACEN_RECEPCION', 'GERENCIA', 'GERENTE_GENERAL')
          AND cap.codigo = 'INVENTARIO_CONTEO_C66'
    """))


def downgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad WHERE capacidad_id IN
          (SELECT id FROM scm_capacidad WHERE codigo = 'INVENTARIO_CONTEO_C66')
    """))
    op.execute(sa.text("DELETE FROM scm_capacidad WHERE codigo = 'INVENTARIO_CONTEO_C66'"))
