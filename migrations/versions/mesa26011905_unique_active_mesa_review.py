"""Allow historical Mesa review rows while keeping one active token.

Revision ID: mesa26011905
Revises: mesa26011904
"""

from alembic import op
import sqlalchemy as sa


revision = "mesa26011905"
down_revision = "mesa26011904"
branch_labels = None
depends_on = None


def upgrade():
    # The original (codigo, estado) constraint incorrectly allowed only one
    # INVALIDADA/CONSUMIDA row per manga, which breaks the second review cycle.
    # Dropping it preserves every existing row; the partial unique index only
    # protects the live-token invariant.
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        # SQLite cannot drop a table constraint in-place; Alembic's batch
        # mode recreates the table while retaining all rows and FKs.
        with op.batch_alter_table("scm_mesa_inventario_revision", recreate="always") as batch:
            batch.drop_constraint("uq_mesa_inv_revision_codigo_estado", type_="unique")
    elif dialect == "postgresql":
        op.drop_constraint(
            "uq_mesa_inv_revision_codigo_estado",
            "scm_mesa_inventario_revision",
            type_="unique",
        )
    else:
        raise RuntimeError(f"Unsupported database for Mesa review index: {dialect}")
    kwargs = {}
    if dialect == "postgresql":
        kwargs["postgresql_where"] = sa.text("estado = 'ACTIVA'")
    elif dialect == "sqlite":
        kwargs["sqlite_where"] = sa.text("estado = 'ACTIVA'")
    op.create_index(
        "uq_mesa_inv_revision_codigo_activa",
        "scm_mesa_inventario_revision",
        ["codigo"],
        unique=True,
        **kwargs,
    )


def downgrade():
    op.drop_index(
        "uq_mesa_inv_revision_codigo_activa",
        table_name="scm_mesa_inventario_revision",
    )
    # This is intentionally strict: restoring the old constraint is unsafe if
    # historical rows already contain more than one inactive review.
    op.create_unique_constraint(
        "uq_mesa_inv_revision_codigo_estado",
        "scm_mesa_inventario_revision",
        ["codigo", "estado"],
    )
