"""Prueba aislada de la revisión del maestro de colores y recetas."""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


migration = importlib.import_module(
    'migrations.versions.e4b6c8d1a207_add_master_color_recipes'
)


def _legacy_database():
    engine = sa.create_engine('sqlite:///:memory:')
    metadata = sa.MetaData()
    sa.Table(
        'familia_color',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
    )
    sa.Table(
        'color_base',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
    )
    color = sa.Table(
        'color_produccion',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('color_base_id', sa.Integer, sa.ForeignKey('color_base.id'), nullable=False),
        sa.Column('familia_color_id', sa.Integer, sa.ForeignKey('familia_color.id'), nullable=False),
        sa.Column('codigo_legacy', sa.Integer),
    )
    sa.Table(
        'producto_terminado',
        metadata,
        sa.Column('cod_sku_pt', sa.String(50), primary_key=True),
    )
    material = sa.Table(
        'scm_material',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
    )
    colorante = sa.Table(
        'colorante',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('nombre', sa.String(100), nullable=False),
        sa.Column('scm_material_id', sa.Integer, sa.ForeignKey('scm_material.id'), nullable=False),
    )
    metadata.create_all(engine)
    return engine, metadata.tables, color, material, colorante


def _run(connection, function):
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        function()


def test_upgrade_preserves_existing_colors_and_adds_governed_recipe_schema():
    engine, tables, color, material, colorante = _legacy_database()
    with engine.begin() as connection:
        connection.execute(tables['familia_color'].insert(), {'id': 1})
        connection.execute(tables['color_base'].insert(), {'id': 1})
        connection.execute(color.insert(), {
            'id': 1,
            'color_base_id': 1,
            'familia_color_id': 1,
            'codigo_legacy': 10,
        })
        connection.execute(material.insert(), {'id': 1})
        connection.execute(colorante.insert(), {
            'id': 1,
            'nombre': 'AMARILLO',
            'scm_material_id': 1,
        })

        _run(connection, migration.upgrade)

        inspector = sa.inspect(connection)
        assert {'hex_referencia', 'activo', 'version'} <= {
            item['name'] for item in inspector.get_columns('color_produccion')
        }
        assert 'tipo' in {
            item['name'] for item in inspector.get_columns('colorante')
        }
        assert {'receta_color_maestra', 'receta_color_linea'} <= set(inspector.get_table_names())
        migrated_color = connection.execute(sa.text(
            'SELECT id, activo, version FROM color_produccion WHERE id = 1'
        )).one()
        assert tuple(migrated_color) == (1, True, 1)
        assert connection.execute(sa.text(
            'SELECT tipo FROM colorante WHERE id = 1'
        )).scalar_one() == 'COLORANTE'

        _run(connection, migration.downgrade)
        inspector = sa.inspect(connection)
        assert 'receta_color_maestra' not in inspector.get_table_names()
        assert 'tipo' not in {
            item['name'] for item in inspector.get_columns('colorante')
        }
        assert 'hex_referencia' not in {
            item['name'] for item in inspector.get_columns('color_produccion')
        }
