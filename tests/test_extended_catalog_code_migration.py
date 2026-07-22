import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


migration = importlib.import_module(
    "migrations.versions.a6d8e0f3c409_extend_catalog_code_counters"
)


def _run(connection, operation):
    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        operation()
    finally:
        migration.op = original_op


def test_extended_counters_start_after_existing_text_and_numeric_codes():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    counter = sa.Table(
        "correlativo_catalogo",
        metadata,
        sa.Column("clave", sa.String(32), primary_key=True),
        sa.Column("prefijo", sa.String(8), unique=True, nullable=False),
        sa.Column("siguiente_valor", sa.BigInteger, nullable=False),
        sa.Column("ancho", sa.SmallInteger, nullable=False),
    )
    material = sa.Table(
        "scm_material",
        metadata,
        sa.Column("codigo", sa.String(50)),
    )
    linea = sa.Table(
        "linea",
        metadata,
        sa.Column("codigo", sa.Integer),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(material.insert(), [
            {"codigo": "MP-000007"},
            {"codigo": "COL-000003"},
            {"codigo": "MP-IMPORTADO"},
        ])
        connection.execute(linea.insert(), [{"codigo": 12}])
        _run(connection, migration.upgrade)

        rows = {
            row.clave: (row.prefijo, row.siguiente_valor)
            for row in connection.execute(sa.select(counter)).all()
        }
        assert rows["MATERIA_PRIMA"] == ("MP", 8)
        assert rows["COLORANTE"] == ("COL", 4)
        assert rows["LINEA"] == ("LIN", 13)
        assert rows["PROVEEDOR"] == ("PRV", 1)

        _run(connection, migration.downgrade)
        assert connection.scalar(sa.select(sa.func.count()).select_from(counter)) == 0
