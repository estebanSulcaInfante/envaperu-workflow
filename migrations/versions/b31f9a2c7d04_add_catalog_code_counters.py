"""add transactional catalog code counters

Revision ID: b31f9a2c7d04
Revises: 8f4c2d1a9b7e
Create Date: 2026-07-22 14:00:00.000000

"""
import re

from alembic import op
import sqlalchemy as sa


revision = "b31f9a2c7d04"
down_revision = "8f4c2d1a9b7e"
branch_labels = None
depends_on = None


CODE_SOURCES = (
    ("PIEZA", "PZ", "pieza", "codigo"),
    ("PIEZA_COLOR", "PC", "pieza_color", "sku"),
    ("PRODUCTO_TERMINADO", "PT", "producto_terminado", "cod_sku_pt"),
    ("MOLDE", "ML", "molde", "codigo"),
)


def _next_value(connection, *, prefix, table_name, column_name):
    """Read legacy/imported identifiers once to seed a counter safely."""

    # Algunas instalaciones adoptadas fueron estampadas sobre subconjuntos
    # legacy del esquema. En ellas la clave igualmente debe comenzar en 1.
    inspector = sa.inspect(connection)
    if table_name not in inspector.get_table_names():
        return 1
    available_columns = {
        column["name"] for column in inspector.get_columns(table_name)
    }
    if column_name not in available_columns:
        return 1

    source = sa.table(table_name, sa.column(column_name, sa.String()))
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    maximum = 0
    for raw_code in connection.execute(sa.select(source.c[column_name])).scalars():
        match = pattern.fullmatch(str(raw_code or "").strip().upper())
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum + 1


def upgrade():
    connection = op.get_bind()
    op.create_table(
        "correlativo_catalogo",
        sa.Column("clave", sa.String(length=32), nullable=False),
        sa.Column("prefijo", sa.String(length=8), nullable=False),
        sa.Column("siguiente_valor", sa.BigInteger(), nullable=False),
        sa.Column(
            "ancho",
            sa.SmallInteger(),
            server_default=sa.text("6"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "clave = upper(trim(clave)) AND length(clave) > 0",
            name="ck_correlativo_catalogo_clave_normalizada",
        ),
        sa.CheckConstraint(
            "prefijo = upper(trim(prefijo)) AND length(prefijo) > 0",
            name="ck_correlativo_catalogo_prefijo_normalizado",
        ),
        sa.CheckConstraint(
            "siguiente_valor > 0",
            name="ck_correlativo_catalogo_siguiente_positivo",
        ),
        sa.CheckConstraint(
            "ancho > 0",
            name="ck_correlativo_catalogo_ancho_positivo",
        ),
        sa.PrimaryKeyConstraint("clave"),
        sa.UniqueConstraint(
            "prefijo",
            name="uq_correlativo_catalogo_prefijo",
        ),
    )

    counter_table = sa.table(
        "correlativo_catalogo",
        sa.column("clave", sa.String()),
        sa.column("prefijo", sa.String()),
        sa.column("siguiente_valor", sa.BigInteger()),
        sa.column("ancho", sa.SmallInteger()),
    )
    op.bulk_insert(
        counter_table,
        [
            {
                "clave": key,
                "prefijo": prefix,
                "siguiente_valor": _next_value(
                    connection,
                    prefix=prefix,
                    table_name=table_name,
                    column_name=column_name,
                ),
                "ancho": 6,
            }
            for key, prefix, table_name, column_name in CODE_SOURCES
        ],
    )


def downgrade():
    op.drop_table("correlativo_catalogo")
