"""repair mojibake in workspace authorization catalog labels

Revision ID: f80c9d5e1a42
Revises: f79b8c4d0e31
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "f80c9d5e1a42"
down_revision = "f79b8c4d0e31"
branch_labels = None
depends_on = None


CATALOG_TABLES = (
    "rol_operativo",
    "scm_capacidad",
)


def _repair_mojibake(value):
    """Undo the UTF-8-as-Latin-1 corruption found in migrated labels."""
    if not isinstance(value, str) or not any(mark in value for mark in ("Ã", "Â")):
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def upgrade():
    connection = op.get_bind()
    for table_name in CATALOG_TABLES:
        catalog = sa.table(
            table_name,
            sa.column("id", sa.Integer),
            sa.column("nombre", sa.String),
        )
        rows = connection.execute(
            sa.select(catalog.c.id, catalog.c.nombre)
        ).mappings().all()
        for row in rows:
            repaired = _repair_mojibake(row["nombre"])
            if repaired == row["nombre"]:
                continue
            connection.execute(
                catalog.update()
                .where(catalog.c.id == row["id"])
                .values(nombre=repaired)
            )


def downgrade():
    # The previous byte corruption is not valid business data and must not be
    # recreated during rollback.
    pass

