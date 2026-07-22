"""extend transactional catalog code counters

Revision ID: a6d8e0f3c409
Revises: f5c7d9e2b308
Create Date: 2026-07-22 23:30:00.000000

"""
import re

from alembic import op
import sqlalchemy as sa


revision = "a6d8e0f3c409"
down_revision = "f5c7d9e2b308"
branch_labels = None
depends_on = None


TEXT_CODE_SOURCES = (
    ("MATERIA_PRIMA", "MP", "scm_material", "codigo"),
    ("COLORANTE", "COL", "scm_material", "codigo"),
    ("ADITIVO", "ADT", "scm_material", "codigo"),
    ("PROVEEDOR", "PRV", "scm_proveedor", "codigo"),
    ("CATEGORIA_RECEPCION", "CAT", "scm_categoria_recepcion", "codigo"),
    ("TRABAJADOR", "TRB", "trabajador", "codigo"),
    ("MAQUINA", "MAQ", "maquina", "codigo"),
)

NUMERIC_CODE_SOURCES = (
    ("LINEA", "LIN", "linea", "codigo"),
    ("FAMILIA", "FAM", "familia", "codigo"),
    ("FAMILIA_COLOR", "FC", "familia_color", "codigo"),
)


def _has_column(connection, table_name, column_name):
    inspector = sa.inspect(connection)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _next_text_value(connection, prefix, table_name, column_name):
    if not _has_column(connection, table_name, column_name):
        return 1
    source = sa.table(table_name, sa.column(column_name, sa.String()))
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    maximum = 0
    for raw_code in connection.execute(sa.select(source.c[column_name])).scalars():
        match = pattern.fullmatch(str(raw_code or "").strip().upper())
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum + 1


def _next_numeric_value(connection, table_name, column_name):
    if not _has_column(connection, table_name, column_name):
        return 1
    source = sa.table(table_name, sa.column(column_name, sa.Integer()))
    maximum = connection.scalar(sa.select(sa.func.max(source.c[column_name])))
    return int(maximum or 0) + 1


def upgrade():
    connection = op.get_bind()
    counter = sa.table(
        "correlativo_catalogo",
        sa.column("clave", sa.String()),
        sa.column("prefijo", sa.String()),
        sa.column("siguiente_valor", sa.BigInteger()),
        sa.column("ancho", sa.SmallInteger()),
    )
    rows = [
        {
            "clave": key,
            "prefijo": prefix,
            "siguiente_valor": _next_text_value(
                connection, prefix, table_name, column_name
            ),
            "ancho": 6,
        }
        for key, prefix, table_name, column_name in TEXT_CODE_SOURCES
    ]
    rows.extend(
        {
            "clave": key,
            "prefijo": prefix,
            "siguiente_valor": _next_numeric_value(
                connection, table_name, column_name
            ),
            "ancho": 6,
        }
        for key, prefix, table_name, column_name in NUMERIC_CODE_SOURCES
    )
    op.bulk_insert(counter, rows)


def downgrade():
    keys = [row[0] for row in TEXT_CODE_SOURCES + NUMERIC_CODE_SOURCES]
    counter = sa.table(
        "correlativo_catalogo",
        sa.column("clave", sa.String()),
    )
    op.get_bind().execute(sa.delete(counter).where(counter.c.clave.in_(keys)))
