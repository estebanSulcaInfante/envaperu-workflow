"""Allow direct Armado KG withdrawals in the KG movement ledger."""

import sqlalchemy as sa
from alembic import op


revision = "fc3d4e5f6071"
down_revision = "fb2c3d4e5f60"
branch_labels = None
depends_on = None


_MOVEMENT_TYPES = (
    "'INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', "
    "'TRASLADO_SALIDA', 'TRASLADO_ENTRADA', 'RETIRO_ARMADO', 'RETORNO_ENTRADA'"
)
_LEGACY_MOVEMENT_TYPES = (
    "'INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', "
    "'TRASLADO_SALIDA', 'TRASLADO_ENTRADA', 'RETORNO_ENTRADA'"
)


def upgrade():
    with op.batch_alter_table("scm_movimiento_inventario_kg") as batch_op:
        batch_op.drop_constraint("ck_scm_movimiento_inventario_kg_tipo", type_="check")
        batch_op.create_check_constraint(
            "ck_scm_movimiento_inventario_kg_tipo",
            f"tipo IN ({_MOVEMENT_TYPES})",
        )


def downgrade():
    connection = op.get_bind()
    existing = connection.execute(sa.text(
        "SELECT COUNT(*) FROM scm_movimiento_inventario_kg WHERE tipo = 'RETIRO_ARMADO'"
    )).scalar_one()
    if existing:
        raise RuntimeError(
            "No se puede revertir la restricci�n KG mientras existan movimientos RETIRO_ARMADO."
        )
    with op.batch_alter_table("scm_movimiento_inventario_kg") as batch_op:
        batch_op.drop_constraint("ck_scm_movimiento_inventario_kg_tipo", type_="check")
        batch_op.create_check_constraint(
            "ck_scm_movimiento_inventario_kg_tipo",
            f"tipo IN ({_LEGACY_MOVEMENT_TYPES})",
        )
