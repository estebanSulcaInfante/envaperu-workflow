"""normalize linea and familia as an explicit many-to-many catalog

Revision ID: c42d8e6f1a03
Revises: b31f9a2c7d04
Create Date: 2026-07-22 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "c42d8e6f1a03"
down_revision = "b31f9a2c7d04"
branch_labels = None
depends_on = None


PAIR_SOURCES = (
    "producto_terminado",
    "pieza_color",
    "pieza",
)


def _ensure_catalog_table(connection, table_name, name_length):
    """Repair legacy-adoption schemas that were stamped without catalogs."""

    if table_name in sa.inspect(connection).get_table_names():
        return
    op.create_table(
        table_name,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.Integer(), nullable=False),
        sa.Column("nombre", sa.String(length=name_length), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo"),
        sa.UniqueConstraint("nombre"),
    )


def _add_catalog_lifecycle_columns(table_name):
    # Column-level checks are intentional: SQLite supports them in ADD COLUMN
    # without rebuilding parent tables referenced by existing foreign keys.
    op.add_column(
        table_name,
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "version",
            sa.Integer(),
            sa.CheckConstraint(
                "version > 0",
                name=f"ck_{table_name}_version",
            ),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )


def _source_selects(connection):
    """Build a portable UNION containing only real, non-null classifications."""

    inspector = sa.inspect(connection)
    available_tables = set(inspector.get_table_names())
    selects = []
    for table_name in PAIR_SOURCES:
        if table_name not in available_tables:
            continue
        columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        if not {"linea_id", "familia_id"} <= columns:
            continue
        source = sa.table(
            table_name,
            sa.column("linea_id", sa.Integer()),
            sa.column("familia_id", sa.Integer()),
        )
        linea = sa.table("linea", sa.column("id", sa.Integer()))
        familia = sa.table("familia", sa.column("id", sa.Integer()))
        selects.append(
            sa.select(source.c.linea_id, source.c.familia_id)
            .join(linea, linea.c.id == source.c.linea_id)
            .join(familia, familia.c.id == source.c.familia_id)
            .where(
                source.c.linea_id.is_not(None),
                source.c.familia_id.is_not(None),
            )
        )
    return selects


def _backfill_used_pairs(connection):
    selects = _source_selects(connection)
    if not selects:
        return

    used_pairs = selects[0]
    if len(selects) > 1:
        used_pairs = used_pairs.union(*selects[1:])

    target = sa.table(
        "linea_familia",
        sa.column("linea_id", sa.Integer()),
        sa.column("familia_id", sa.Integer()),
    )
    connection.execute(
        sa.insert(target).from_select(
            ["linea_id", "familia_id"],
            used_pairs,
        )
    )


def upgrade():
    connection = op.get_bind()

    _ensure_catalog_table(connection, "linea", 50)
    _ensure_catalog_table(connection, "familia", 100)
    _add_catalog_lifecycle_columns("linea")
    _add_catalog_lifecycle_columns("familia")

    op.create_table(
        "linea_familia",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("linea_id", sa.Integer(), nullable=False),
        sa.Column("familia_id", sa.Integer(), nullable=False),
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_linea_familia_version",
        ),
        sa.ForeignKeyConstraint(
            ["linea_id"],
            ["linea.id"],
            name="fk_linea_familia_linea",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["familia_id"],
            ["familia.id"],
            name="fk_linea_familia_familia",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "linea_id",
            "familia_id",
            name="uq_linea_familia_linea_familia",
        ),
    )
    op.create_index(
        "ix_linea_familia_linea_activo",
        "linea_familia",
        ["linea_id", "activo"],
    )
    op.create_index(
        "ix_linea_familia_familia_activo",
        "linea_familia",
        ["familia_id", "activo"],
    )

    _backfill_used_pairs(connection)


def downgrade():
    op.drop_table("linea_familia")
    op.drop_column("familia", "version")
    op.drop_column("familia", "activo")
    op.drop_column("linea", "version")
    op.drop_column("linea", "activo")
