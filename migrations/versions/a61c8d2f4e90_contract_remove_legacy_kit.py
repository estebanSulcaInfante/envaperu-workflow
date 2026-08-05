"""contract removal of legacy PiezaColor KIT composition

Revision ID: a61c8d2f4e90
Revises: f49b7e5a3d02
Create Date: 2026-07-25 03:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "a61c8d2f4e90"
down_revision = "f49b7e5a3d02"
branch_labels = None
depends_on = None


def _assert_contract_precondition(connection):
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())

    component_count = 0
    if "pieza_componente" in tables:
        component_count = int(connection.execute(
            sa.text("SELECT count(*) FROM pieza_componente")
        ).scalar_one())

    kit_count = 0
    piece_columns = {
        column["name"]
        for column in inspector.get_columns("pieza_color")
    }
    if "tipo" in piece_columns:
        kit_count = int(connection.execute(sa.text("""
            SELECT count(*)
            FROM pieza_color
            WHERE upper(trim(coalesce(tipo, ''))) IN ('KIT', 'COMPONENTE')
        """)).scalar_one())

    if kit_count or component_count:
        raise RuntimeError(
            "LEGACY_KIT_PRECONDITION_FAILED: "
            f"{kit_count} variantes KIT/COMPONENTE y "
            f"{component_count} componentes requieren conciliacion"
        )


def upgrade():
    connection = op.get_bind()
    _assert_contract_precondition(connection)

    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "pieza_componente" in tables:
        op.drop_table("pieza_componente")

    piece_columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("pieza_color")
    }
    if "tipo" in piece_columns:
        op.drop_column("pieza_color", "tipo")


def downgrade():
    connection = op.get_bind()
    piece_columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("pieza_color")
    }
    if "tipo" not in piece_columns:
        op.add_column(
            "pieza_color",
            sa.Column("tipo", sa.String(length=20), nullable=True),
        )
        op.execute("UPDATE pieza_color SET tipo = 'SIMPLE'")

    if "pieza_componente" not in set(
        sa.inspect(connection).get_table_names()
    ):
        op.create_table(
            "pieza_componente",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("kit_sku", sa.String(length=50), nullable=False),
            sa.Column(
                "componente_sku",
                sa.String(length=50),
                nullable=False,
            ),
            sa.Column("cantidad", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["componente_sku"],
                ["pieza_color.sku"],
            ),
            sa.ForeignKeyConstraint(
                ["kit_sku"],
                ["pieza_color.sku"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "kit_sku",
                "componente_sku",
                name="uq_pieza_componente",
            ),
        )
