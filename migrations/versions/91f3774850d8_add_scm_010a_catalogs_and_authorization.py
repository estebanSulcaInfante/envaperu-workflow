"""add scm 010a catalogs and authorization

Revision ID: 91f3774850d8
Revises: f02b00ae2e67
Create Date: 2026-07-21 12:54:53.212908

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '91f3774850d8'
down_revision = 'f02b00ae2e67'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('scm_capacidad',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('codigo', sa.String(length=64), nullable=False),
    sa.Column('nombre', sa.String(length=120), nullable=False),
    sa.Column('descripcion', sa.Text(), nullable=True),
    sa.Column('activo', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('codigo', name='uq_scm_capacidad_codigo')
    )
    op.create_table('scm_categoria_recepcion',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('codigo', sa.String(length=64), nullable=False),
    sa.Column('nombre', sa.String(length=120), nullable=False),
    sa.Column('modalidad_default', sa.String(length=40), nullable=False),
    sa.Column('lote_externo_obligatorio', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('recepcion_habilitada', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('activo', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("NOT (modalidad_default = 'POR_CONFIGURAR' AND recepcion_habilitada)", name='ck_scm_categoria_recepcion_configurada'),
    sa.CheckConstraint("modalidad_default IN ('VIRGEN_CONFIANZA_PROVEEDOR', 'SEGUNDA_PESAJE_BOLSA', 'POR_CONFIGURAR')", name='ck_scm_categoria_recepcion_modalidad'),
    sa.CheckConstraint('version > 0', name='ck_scm_categoria_recepcion_version'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('codigo', name='uq_scm_categoria_recepcion_codigo')
    )
    op.create_table('scm_material',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('codigo', sa.String(length=64), nullable=False),
    sa.Column('nombre', sa.String(length=120), nullable=False),
    sa.Column('clase', sa.String(length=30), nullable=False),
    sa.Column('categoria_recepcion_id', sa.Integer(), nullable=False),
    sa.Column('unidad_base', sa.String(length=10), server_default='KG', nullable=False),
    sa.Column('activo', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("clase IN ('MATERIA_PRIMA', 'COLORANTE')", name='ck_scm_material_clase'),
    sa.CheckConstraint("unidad_base = 'KG'", name='ck_scm_material_unidad_base'),
    sa.CheckConstraint('version > 0', name='ck_scm_material_version'),
    sa.ForeignKeyConstraint(['categoria_recepcion_id'], ['scm_categoria_recepcion.id'], name='fk_scm_material_categoria_recepcion', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('codigo', name='uq_scm_material_codigo')
    )
    op.create_table('scm_rol_capacidad',
    sa.Column('rol_operativo_id', sa.Integer(), nullable=False),
    sa.Column('capacidad_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['capacidad_id'], ['scm_capacidad.id'], name='fk_scm_rol_capacidad_capacidad', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['rol_operativo_id'], ['rol_operativo.id'], name='fk_scm_rol_capacidad_rol_operativo', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('rol_operativo_id', 'capacidad_id')
    )
    with op.batch_alter_table('colorante', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scm_material_id', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint('uq_colorante_scm_material_id', ['scm_material_id'])
        batch_op.create_foreign_key('fk_colorante_scm_material', 'scm_material', ['scm_material_id'], ['id'], ondelete='RESTRICT')

    with op.batch_alter_table('materia_prima', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scm_material_id', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint('uq_materia_prima_scm_material_id', ['scm_material_id'])
        batch_op.create_foreign_key('fk_materia_prima_scm_material', 'scm_material', ['scm_material_id'], ['id'], ondelete='RESTRICT')

    op.execute(sa.text("""
        CREATE FUNCTION scm_validar_materia_prima_material()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            material_clase text;
        BEGIN
            IF NEW.scm_material_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT clase INTO material_clase
            FROM scm_material
            WHERE id = NEW.scm_material_id;
            IF material_clase IS NOT NULL
               AND material_clase <> 'MATERIA_PRIMA' THEN
                RAISE EXCEPTION
                    'scm_material % no pertenece a MATERIA_PRIMA',
                    NEW.scm_material_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """))
    op.execute(sa.text("""
        CREATE FUNCTION scm_validar_colorante_material()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            material_clase text;
        BEGIN
            IF NEW.scm_material_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT clase INTO material_clase
            FROM scm_material
            WHERE id = NEW.scm_material_id;
            IF material_clase IS NOT NULL
               AND material_clase <> 'COLORANTE' THEN
                RAISE EXCEPTION
                    'scm_material % no pertenece a COLORANTE',
                    NEW.scm_material_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """))
    op.execute(sa.text("""
        CREATE FUNCTION scm_validar_clase_material_legacy()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.clase = 'MATERIA_PRIMA'
               AND EXISTS (
                   SELECT 1 FROM colorante
                   WHERE scm_material_id = NEW.id
               ) THEN
                RAISE EXCEPTION
                    'scm_material % ya está vinculado como COLORANTE', NEW.id
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.clase = 'COLORANTE'
               AND EXISTS (
                   SELECT 1 FROM materia_prima
                   WHERE scm_material_id = NEW.id
               ) THEN
                RAISE EXCEPTION
                    'scm_material % ya está vinculado como MATERIA_PRIMA', NEW.id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_materia_prima_scm_material_clase
        BEFORE INSERT OR UPDATE OF scm_material_id ON materia_prima
        FOR EACH ROW EXECUTE FUNCTION scm_validar_materia_prima_material()
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_colorante_scm_material_clase
        BEFORE INSERT OR UPDATE OF scm_material_id ON colorante
        FOR EACH ROW EXECUTE FUNCTION scm_validar_colorante_material()
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_scm_material_clase_legacy
        BEFORE UPDATE OF clase ON scm_material
        FOR EACH ROW EXECUTE FUNCTION scm_validar_clase_material_legacy()
    """))

    categoria = sa.table(
        'scm_categoria_recepcion',
        sa.column('codigo', sa.String),
        sa.column('nombre', sa.String),
        sa.column('modalidad_default', sa.String),
        sa.column('lote_externo_obligatorio', sa.Boolean),
        sa.column('recepcion_habilitada', sa.Boolean),
        sa.column('activo', sa.Boolean),
        sa.column('version', sa.Integer),
    )
    op.bulk_insert(categoria, [
        {
            'codigo': 'RESINA_VIRGEN',
            'nombre': 'Resina virgen',
            'modalidad_default': 'VIRGEN_CONFIANZA_PROVEEDOR',
            'lote_externo_obligatorio': False,
            'recepcion_habilitada': True,
            'activo': True,
            'version': 1,
        },
        {
            'codigo': 'RESINA_SEGUNDA',
            'nombre': 'Resina de segunda',
            'modalidad_default': 'SEGUNDA_PESAJE_BOLSA',
            'lote_externo_obligatorio': False,
            'recepcion_habilitada': True,
            'activo': True,
            'version': 1,
        },
        {
            'codigo': 'LEGACY_POR_CONFIGURAR',
            'nombre': 'Legacy por configurar',
            'modalidad_default': 'POR_CONFIGURAR',
            'lote_externo_obligatorio': False,
            'recepcion_habilitada': False,
            'activo': True,
            'version': 1,
        },
    ])

    capacidad = sa.table(
        'scm_capacidad',
        sa.column('codigo', sa.String),
        sa.column('nombre', sa.String),
        sa.column('activo', sa.Boolean),
    )
    op.bulk_insert(capacidad, [
        {'codigo': 'PROVEEDOR_ADMINISTRAR', 'nombre': 'Administrar proveedores', 'activo': True},
        {'codigo': 'OC_CREAR', 'nombre': 'Crear órdenes de compra de material', 'activo': True},
        {'codigo': 'OC_APROBAR', 'nombre': 'Aprobar órdenes de compra de material', 'activo': True},
        {'codigo': 'RECEPCION_CONFIRMAR', 'nombre': 'Confirmar recepciones de material', 'activo': True},
        {'codigo': 'ENTRADA_EXCEPCIONAL_REGULARIZAR', 'nombre': 'Regularizar entradas excepcionales', 'activo': True},
        {'codigo': 'CALIDAD_RESOLVER', 'nombre': 'Resolver decisiones de Calidad', 'activo': True},
        {'codigo': 'LIBERACION_DIRECTA_ADMINISTRAR', 'nombre': 'Administrar políticas de liberación directa', 'activo': True},
        {'codigo': 'CORRECCION_SOLICITAR', 'nombre': 'Solicitar correcciones de recepción', 'activo': True},
        {'codigo': 'CORRECCION_APROBAR', 'nombre': 'Aprobar correcciones de recepción', 'activo': True},
        {'codigo': 'DEVOLUCION_REGISTRAR', 'nombre': 'Registrar devoluciones a proveedor', 'activo': True},
        {'codigo': 'CONFIG_RECEPCION_ADMINISTRAR', 'nombre': 'Administrar configuración de recepción', 'activo': True},
    ])

    op.execute(sa.text("""
        INSERT INTO rol_operativo (codigo, nombre, activo)
        VALUES
            ('COMPRAS', 'Compras', true),
            ('ALMACEN_RECEPCION', 'Almacén / Recepción', true),
            ('CALIDAD', 'Calidad', true),
            ('GERENCIA', 'Gerencia', true),
            ('SUPERVISOR', 'Supervisor', true),
            ('CONFIGURACION_SCM', 'Configuración SCM', true),
            ('AUDITORIA_CONSULTA', 'Auditoría / Consulta', true)
        ON CONFLICT (codigo) DO NOTHING
    """))
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM (VALUES
            ('COMPRAS', 'PROVEEDOR_ADMINISTRAR'),
            ('COMPRAS', 'OC_CREAR'),
            ('ALMACEN_RECEPCION', 'RECEPCION_CONFIRMAR'),
            ('ALMACEN_RECEPCION', 'CORRECCION_SOLICITAR'),
            ('ALMACEN_RECEPCION', 'DEVOLUCION_REGISTRAR'),
            ('CALIDAD', 'CALIDAD_RESOLVER'),
            ('GERENCIA', 'OC_APROBAR'),
            ('GERENCIA', 'CORRECCION_APROBAR'),
            ('SUPERVISOR', 'ENTRADA_EXCEPCIONAL_REGULARIZAR'),
            ('CONFIGURACION_SCM', 'CONFIG_RECEPCION_ADMINISTRAR'),
            ('CONFIGURACION_SCM', 'LIBERACION_DIRECTA_ADMINISTRAR')
        ) AS asignacion(rol_codigo, capacidad_codigo)
        JOIN rol_operativo AS rol ON rol.codigo = asignacion.rol_codigo
        JOIN scm_capacidad AS capacidad
          ON capacidad.codigo = asignacion.capacidad_codigo
        WHERE NOT EXISTS (
            SELECT 1
            FROM trabajador_rol AS asignacion_existente
            WHERE asignacion_existente.rol_operativo_id = rol.id
        )
        ON CONFLICT (rol_operativo_id, capacidad_id) DO NOTHING
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
            'MP-LEGACY-' || lpad(materia.id::text, 8, '0'),
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
    """))
    op.execute(sa.text("""
        UPDATE materia_prima AS materia
        SET scm_material_id = material.id
        FROM scm_material AS material
        WHERE material.codigo =
            'MP-LEGACY-' || lpad(materia.id::text, 8, '0')
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
            'COL-LEGACY-' || lpad(colorante.id::text, 8, '0'),
            colorante.nombre,
            'COLORANTE',
            categoria.id,
            'KG',
            true,
            1
        FROM colorante
        JOIN scm_categoria_recepcion AS categoria
          ON categoria.codigo = 'LEGACY_POR_CONFIGURAR'
    """))
    op.execute(sa.text("""
        UPDATE colorante
        SET scm_material_id = material.id
        FROM scm_material AS material
        WHERE material.codigo =
            'COL-LEGACY-' || lpad(colorante.id::text, 8, '0')
    """))

    op.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM materia_prima WHERE scm_material_id IS NULL
            ) THEN
                RAISE EXCEPTION 'US010A backfill dejó materia_prima sin material común';
            END IF;
            IF EXISTS (
                SELECT 1 FROM colorante WHERE scm_material_id IS NULL
            ) THEN
                RAISE EXCEPTION 'US010A backfill dejó colorante sin material común';
            END IF;
        END
        $$
    """))

    # Las FKs permanecen nullable durante la fase expand. Se vuelven NOT NULL
    # cuando todos los escritores legacy creen también la identidad scm_material.


def downgrade():
    op.execute(sa.text(
        'DROP TRIGGER trg_scm_material_clase_legacy ON scm_material'
    ))
    op.execute(sa.text(
        'DROP TRIGGER trg_colorante_scm_material_clase ON colorante'
    ))
    op.execute(sa.text(
        'DROP TRIGGER trg_materia_prima_scm_material_clase ON materia_prima'
    ))
    op.execute(sa.text('DROP FUNCTION scm_validar_clase_material_legacy()'))
    op.execute(sa.text('DROP FUNCTION scm_validar_colorante_material()'))
    op.execute(sa.text('DROP FUNCTION scm_validar_materia_prima_material()'))

    with op.batch_alter_table('materia_prima', schema=None) as batch_op:
        batch_op.drop_constraint('fk_materia_prima_scm_material', type_='foreignkey')
        batch_op.drop_constraint('uq_materia_prima_scm_material_id', type_='unique')
        batch_op.drop_column('scm_material_id')

    with op.batch_alter_table('colorante', schema=None) as batch_op:
        batch_op.drop_constraint('fk_colorante_scm_material', type_='foreignkey')
        batch_op.drop_constraint('uq_colorante_scm_material_id', type_='unique')
        batch_op.drop_column('scm_material_id')

    op.drop_table('scm_rol_capacidad')
    op.drop_table('scm_material')
    op.drop_table('scm_categoria_recepcion')
    op.drop_table('scm_capacidad')
    # Los roles se conservan: pudieron existir antes de 010A y no es seguro
    # inferir por código cuáles pertenecían exclusivamente a esta revisión.
