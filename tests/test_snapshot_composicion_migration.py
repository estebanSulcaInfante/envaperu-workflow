"""Prueba aislada de la revisión que normaliza snapshots de composición."""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


migration = importlib.import_module(
    'migrations.versions.d7e9a4c2f105_normalize_order_piece_snapshots'
)


def _legacy_database():
    engine = sa.create_engine('sqlite:///:memory:')
    metadata = sa.MetaData()
    pieza = sa.Table(
        'pieza',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('codigo', sa.String(64), nullable=False),
        sa.Column('nombre', sa.String(200), nullable=False),
    )
    pieza_color = sa.Table(
        'pieza_color',
        metadata,
        sa.Column('sku', sa.String(50), primary_key=True),
        sa.Column('piezas', sa.String(200)),
        sa.Column('pieza_id', sa.Integer, sa.ForeignKey('pieza.id')),
    )
    orden = sa.Table(
        'orden_produccion',
        metadata,
        sa.Column('numero_op', sa.String(20), primary_key=True),
    )
    snapshot = sa.Table(
        'snapshot_composicion_molde',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column(
            'orden_id',
            sa.String(20),
            sa.ForeignKey('orden_produccion.numero_op'),
            nullable=False,
        ),
        sa.Column(
            'pieza_sku',
            sa.String(50),
            sa.ForeignKey('pieza_color.sku'),
        ),
        sa.Column('cavidades', sa.Integer, nullable=False),
        sa.Column('peso_unit_gr', sa.Float, nullable=False),
    )
    metadata.create_all(engine)
    return engine, pieza, pieza_color, orden, snapshot


def _run(connection, function):
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        function()


def test_upgrade_preserva_evidencia_y_solo_enlaza_por_sku_exacto():
    engine, pieza, pieza_color, orden, snapshot = _legacy_database()
    with engine.begin() as connection:
        connection.execute(pieza.insert(), {
            'id': 10,
            'codigo': 'PZ-000010',
            'nombre': 'Tapa histórica',
        })
        connection.execute(pieza_color.insert(), [
            {'sku': 'PC-RESUELTA', 'piezas': 'Tapa azul', 'pieza_id': 10},
            {'sku': 'PC-SIN-PIEZA', 'piezas': 'Registro por conciliar', 'pieza_id': None},
        ])
        connection.execute(orden.insert(), [
            {'numero_op': 'OP-LEGACY-1'},
            {'numero_op': 'OP-LEGACY-2'},
        ])
        connection.execute(snapshot.insert(), [
            {
                'id': 1,
                'orden_id': 'OP-LEGACY-1',
                'pieza_sku': 'PC-RESUELTA',
                'cavidades': 2,
                'peso_unit_gr': 15,
            },
            {
                'id': 2,
                'orden_id': 'OP-LEGACY-2',
                'pieza_sku': 'PC-SIN-PIEZA',
                'cavidades': 1,
                'peso_unit_gr': 8,
            },
        ])

        _run(connection, migration.upgrade)

        inspector = sa.inspect(connection)
        columns = {item['name'] for item in inspector.get_columns(migration.TABLE)}
        assert 'pieza_sku' not in columns
        assert {
            'pieza_id',
            'pieza_codigo_snapshot',
            'pieza_nombre_snapshot',
            'pieza_sku_legacy',
        } <= columns
        assert any(
            foreign_key['constrained_columns'] == ['pieza_id']
            and foreign_key['referred_table'] == 'pieza'
            for foreign_key in inspector.get_foreign_keys(migration.TABLE)
        )

        rows = connection.execute(sa.text("""
            SELECT id, pieza_id, pieza_codigo_snapshot,
                   pieza_nombre_snapshot, pieza_sku_legacy
            FROM snapshot_composicion_molde
            ORDER BY id
        """)).mappings().all()
        assert dict(rows[0]) == {
            'id': 1,
            'pieza_id': 10,
            'pieza_codigo_snapshot': 'PZ-000010',
            'pieza_nombre_snapshot': 'Tapa histórica',
            'pieza_sku_legacy': 'PC-RESUELTA',
        }
        assert dict(rows[1]) == {
            'id': 2,
            'pieza_id': None,
            'pieza_codigo_snapshot': None,
            'pieza_nombre_snapshot': 'Registro por conciliar',
            'pieza_sku_legacy': 'PC-SIN-PIEZA',
        }


def test_downgrade_bloquea_snapshot_nuevo_sin_sku_legacy():
    engine, pieza, _, orden, _ = _legacy_database()
    with engine.begin() as connection:
        connection.execute(pieza.insert(), {
            'id': 11,
            'codigo': 'PZ-000011',
            'nombre': 'Pieza nueva',
        })
        connection.execute(orden.insert(), {'numero_op': 'OP-NUEVA'})
        _run(connection, migration.upgrade)
        connection.execute(sa.text("""
            INSERT INTO snapshot_composicion_molde (
                id, orden_id, pieza_id, pieza_codigo_snapshot,
                pieza_nombre_snapshot, pieza_sku_legacy,
                cavidades, peso_unit_gr
            ) VALUES (
                3, 'OP-NUEVA', 11, 'PZ-000011',
                'Pieza nueva', NULL, 1, 10
            )
        """))

        with pytest.raises(RuntimeError, match='Downgrade bloqueado'):
            _run(connection, migration.downgrade)
