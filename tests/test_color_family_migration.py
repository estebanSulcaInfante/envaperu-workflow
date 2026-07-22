"""Prueba aislada de gobierno del catálogo FamiliaColor."""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


migration = importlib.import_module(
    'migrations.versions.f5c7d9e2b308_govern_color_families'
)


def _run(connection, function):
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        function()


def test_upgrade_preserves_families_and_adds_governance_fields():
    engine = sa.create_engine('sqlite:///:memory:')
    metadata = sa.MetaData()
    family = sa.Table(
        'familia_color',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('codigo', sa.Integer, unique=True),
        sa.Column('nombre', sa.String(50), unique=True, nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(family.insert(), {'id': 1, 'codigo': 1, 'nombre': 'SOLIDO'})
        _run(connection, migration.upgrade)

        inspector = sa.inspect(connection)
        assert {'activo', 'version'} <= {
            item['name'] for item in inspector.get_columns('familia_color')
        }
        row = connection.execute(sa.text(
            'SELECT nombre, activo, version FROM familia_color WHERE id = 1'
        )).one()
        assert tuple(row) == ('SOLIDO', True, 1)

        _run(connection, migration.downgrade)
        columns = {
            item['name'] for item in sa.inspect(connection).get_columns('familia_color')
        }
        assert 'activo' not in columns
        assert 'version' not in columns
