"""Index exact output route-operation reads used by OF workbench."""

from alembic import op


revision = "fb7c8d9e0a12"
down_revision = "ff60718293a4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_scm_operacion_ruta_output_route_id",
        "scm_operacion_ruta",
        ["articulo_salida_id", "ruta_id", "id"],
    )


def downgrade():
    op.drop_index(
        "ix_scm_operacion_ruta_output_route_id",
        table_name="scm_operacion_ruta",
    )
