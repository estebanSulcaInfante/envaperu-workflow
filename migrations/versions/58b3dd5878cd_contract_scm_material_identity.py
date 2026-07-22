"""contract scm material identity

Revision ID: 58b3dd5878cd
Revises: 91f3774850d8
Create Date: 2026-07-21 13:40:17.447196

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '58b3dd5878cd'
down_revision = '91f3774850d8'
branch_labels = None
depends_on = None


def upgrade():
    # Esta revision se ejecuta solo despues de desplegar el dual-write, drenar
    # las instancias antiguas y pausar todas las altas de catalogo. El bloqueo
    # cierra la ventana entre la ultima reconvergencia y el SET NOT NULL.
    op.execute(sa.text("""
        LOCK TABLE
            scm_categoria_recepcion,
            scm_material,
            materia_prima,
            colorante
        IN SHARE ROW EXCLUSIVE MODE
    """))

    # Los codigos CONTRACT estan reservados para filas creadas durante la
    # ventana expand. Una colision se considera ambigua y aborta la migracion;
    # nunca se reutiliza una identidad solamente porque el codigo coincide.
    op.execute(sa.text("""
        DO $$
        DECLARE
            codigo_colision text;
        BEGIN
            SELECT material.codigo
            INTO codigo_colision
            FROM materia_prima AS materia
            JOIN scm_material AS material
              ON material.codigo =
                 'MP-CONTRACT-' || lpad(
                     materia.id::text,
                     greatest(8, length(materia.id::text)),
                     '0'
                 )
            WHERE materia.scm_material_id IS NULL
            LIMIT 1;

            IF codigo_colision IS NOT NULL THEN
                RAISE EXCEPTION
                    'US010A contract encontro codigo reservado en uso: %',
                    codigo_colision;
            END IF;

            SELECT material.codigo
            INTO codigo_colision
            FROM colorante
            JOIN scm_material AS material
              ON material.codigo =
                 'COL-CONTRACT-' || lpad(
                     colorante.id::text,
                     greatest(8, length(colorante.id::text)),
                     '0'
                 )
            WHERE colorante.scm_material_id IS NULL
            LIMIT 1;

            IF codigo_colision IS NOT NULL THEN
                RAISE EXCEPTION
                    'US010A contract encontro codigo reservado en uso: %',
                    codigo_colision;
            END IF;
        END
        $$
    """))

    op.execute(sa.text("""
        INSERT INTO scm_material (
            codigo,
            nombre,
            clase,
            categoria_recepcion_id,
            unidad_base,
            activo,
            version
        )
        SELECT
            'MP-CONTRACT-' || lpad(
                materia.id::text,
                greatest(8, length(materia.id::text)),
                '0'
            ),
            materia.nombre,
            'MATERIA_PRIMA',
            categoria.id,
            'KG',
            true,
            1
        FROM materia_prima AS materia
        JOIN scm_categoria_recepcion AS categoria
          ON categoria.codigo = CASE upper(trim(coalesce(materia.tipo, '')))
              WHEN 'VIRGEN' THEN 'RESINA_VIRGEN'
              WHEN 'SEGUNDA' THEN 'RESINA_SEGUNDA'
              ELSE 'LEGACY_POR_CONFIGURAR'
          END
        WHERE materia.scm_material_id IS NULL
    """))
    op.execute(sa.text("""
        UPDATE materia_prima AS materia
        SET scm_material_id = material.id
        FROM scm_material AS material
        WHERE materia.scm_material_id IS NULL
          AND material.codigo =
              'MP-CONTRACT-' || lpad(
                  materia.id::text,
                  greatest(8, length(materia.id::text)),
                  '0'
              )
    """))

    op.execute(sa.text("""
        INSERT INTO scm_material (
            codigo,
            nombre,
            clase,
            categoria_recepcion_id,
            unidad_base,
            activo,
            version
        )
        SELECT
            'COL-CONTRACT-' || lpad(
                colorante.id::text,
                greatest(8, length(colorante.id::text)),
                '0'
            ),
            colorante.nombre,
            'COLORANTE',
            categoria.id,
            'KG',
            true,
            1
        FROM colorante
        JOIN scm_categoria_recepcion AS categoria
          ON categoria.codigo = 'LEGACY_POR_CONFIGURAR'
        WHERE colorante.scm_material_id IS NULL
    """))
    op.execute(sa.text("""
        UPDATE colorante
        SET scm_material_id = material.id
        FROM scm_material AS material
        WHERE colorante.scm_material_id IS NULL
          AND material.codigo =
              'COL-CONTRACT-' || lpad(
                  colorante.id::text,
                  greatest(8, length(colorante.id::text)),
                  '0'
              )
    """))

    op.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM materia_prima
                WHERE scm_material_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'US010A contract dejo materia_prima sin identidad SCM';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM colorante
                WHERE scm_material_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'US010A contract dejo colorante sin identidad SCM';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM materia_prima AS materia
                JOIN scm_material AS material
                  ON material.id = materia.scm_material_id
                WHERE material.clase <> 'MATERIA_PRIMA'
            ) THEN
                RAISE EXCEPTION
                    'US010A contract encontro materia_prima con clase invalida';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM colorante
                JOIN scm_material AS material
                  ON material.id = colorante.scm_material_id
                WHERE material.clase <> 'COLORANTE'
            ) THEN
                RAISE EXCEPTION
                    'US010A contract encontro colorante con clase invalida';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM materia_prima
                JOIN colorante
                  ON colorante.scm_material_id =
                     materia_prima.scm_material_id
            ) THEN
                RAISE EXCEPTION
                    'US010A contract encontro identidad compartida entre clases';
            END IF;
        END
        $$
    """))

    op.alter_column(
        'materia_prima',
        'scm_material_id',
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        'colorante',
        'scm_material_id',
        existing_type=sa.Integer(),
        nullable=False,
    )


def downgrade():
    # El rollback es deliberadamente no destructivo. Mantiene las identidades
    # y los vinculos; solo vuelve a habilitar temporalmente escritores expand.
    op.alter_column(
        'colorante',
        'scm_material_id',
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.alter_column(
        'materia_prima',
        'scm_material_id',
        existing_type=sa.Integer(),
        nullable=True,
    )
