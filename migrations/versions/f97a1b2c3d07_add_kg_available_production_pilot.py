"""Make measured KG available from Production in the controlled pilot.

The migration is additive.  Existing receipts retain their historical quality
state; only the W1 path uses ``SIN_CONTROL`` and production logistics states.
"""

from alembic import op
import sqlalchemy as sa


revision = "f97a1b2c3d07"
down_revision = "f96a1b2c3d06"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_saldo_inventario_kg") as batch:
        batch.add_column(sa.Column("cantidad_retirada_kg", sa.Numeric(15, 3), nullable=False, server_default="0"))
        batch.add_column(sa.Column("atributo_proceso", sa.String(16), nullable=False, server_default="PROCESO"))
        batch.drop_constraint("ck_scm_saldo_inventario_kg_cantidades", type_="check")
        batch.create_check_constraint(
            "ck_scm_saldo_inventario_kg_cantidades",
            "cantidad_fisica_kg >= 0 AND cantidad_reservada_kg >= 0 AND cantidad_no_disponible_kg >= 0 AND cantidad_retirada_kg >= 0 AND cantidad_reservada_kg + cantidad_no_disponible_kg <= cantidad_fisica_kg",
        )
        batch.create_check_constraint(
            "ck_scm_saldo_inventario_kg_atributo_proceso",
            "atributo_proceso IN ('PROCESO', 'TERMINADA', 'MIXTA')",
        )
    with op.batch_alter_table("scm_movimiento_inventario_kg") as batch:
        batch.add_column(sa.Column("atributo_proceso", sa.String(16), nullable=False, server_default="PROCESO"))
    with op.batch_alter_table("scm_existencia_manga_kg") as batch:
        batch.add_column(sa.Column("atributo_proceso", sa.String(16), nullable=False, server_default="PROCESO"))
        batch.drop_constraint("ck_scm_existencia_manga_kg_logistica", type_="check")
        batch.create_check_constraint(
            "ck_scm_existencia_manga_kg_logistica",
            "estado_logistico IN ('EN_PRODUCCION', 'DISPONIBLE_PRODUCCION', 'RECIBIDA_ALMACEN', 'REVERSADA')",
        )
        batch.drop_constraint("ck_scm_existencia_manga_kg_calidad", type_="check")
        batch.create_check_constraint(
            "ck_scm_existencia_manga_kg_calidad",
            "estado_calidad IN ('SIN_CONTROL', 'PENDIENTE', 'LIBERADA', 'BLOQUEADA', 'RECHAZADA')",
        )
        batch.create_check_constraint(
            "ck_scm_existencia_manga_kg_atributo_proceso",
            "atributo_proceso IN ('PROCESO', 'TERMINADA')",
        )
    with op.batch_alter_table("scm_unidad_fisica_kg") as batch:
        batch.add_column(sa.Column("atributo_proceso", sa.String(16), nullable=False, server_default="PROCESO"))
        batch.drop_constraint("ck_scm_unidad_fisica_kg_logistica", type_="check")
        batch.create_check_constraint(
            "ck_scm_unidad_fisica_kg_logistica",
            "estado_logistico IN ('EN_PRODUCCION', 'DISPONIBLE_PRODUCCION', 'RECIBIDA_ALMACEN', 'ALMACENADA_CONTROLADA', 'RESERVADA', 'RETIRADA_ARMADO', 'REPESADA_PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', 'PENDIENTE_VERIFICACION', 'REVERSADA')",
        )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM scm_existencia_manga_kg WHERE estado_logistico IN ('EN_PRODUCCION', 'DISPONIBLE_PRODUCCION') OR estado_calidad = 'SIN_CONTROL'")).scalar_one():
        raise RuntimeError("downgrade KG availability bloqueado: existen existencias de produccion")
    if bind.execute(sa.text("SELECT COUNT(*) FROM scm_unidad_fisica_kg WHERE estado_logistico IN ('EN_PRODUCCION', 'DISPONIBLE_PRODUCCION') OR estado_calidad = 'SIN_CONTROL'")).scalar_one():
        raise RuntimeError("downgrade KG availability bloqueado: existen unidades de produccion")
    if bind.execute(sa.text("SELECT COUNT(*) FROM scm_saldo_inventario_kg WHERE cantidad_retirada_kg <> 0 OR atributo_proceso <> 'PROCESO'")).scalar_one():
        raise RuntimeError("downgrade KG availability bloqueado: existen proyecciones W1")

    with op.batch_alter_table("scm_unidad_fisica_kg") as batch:
        batch.drop_constraint("ck_scm_unidad_fisica_kg_logistica", type_="check")
        batch.drop_column("atributo_proceso")
        batch.create_check_constraint(
            "ck_scm_unidad_fisica_kg_logistica",
            "estado_logistico IN ('RECIBIDA_ALMACEN', 'ALMACENADA_CONTROLADA', 'RESERVADA', 'RETIRADA_ARMADO', 'REPESADA_PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', 'PENDIENTE_VERIFICACION', 'REVERSADA')",
        )
    with op.batch_alter_table("scm_existencia_manga_kg") as batch:
        batch.drop_constraint("ck_scm_existencia_manga_kg_atributo_proceso", type_="check")
        batch.drop_constraint("ck_scm_existencia_manga_kg_calidad", type_="check")
        batch.drop_constraint("ck_scm_existencia_manga_kg_logistica", type_="check")
        batch.drop_column("atributo_proceso")
        batch.create_check_constraint("ck_scm_existencia_manga_kg_logistica", "estado_logistico IN ('RECIBIDA_ALMACEN', 'REVERSADA')")
        batch.create_check_constraint("ck_scm_existencia_manga_kg_calidad", "estado_calidad IN ('PENDIENTE', 'LIBERADA', 'BLOQUEADA', 'RECHAZADA')")
    with op.batch_alter_table("scm_movimiento_inventario_kg") as batch:
        batch.drop_column("atributo_proceso")
    with op.batch_alter_table("scm_saldo_inventario_kg") as batch:
        batch.drop_constraint("ck_scm_saldo_inventario_kg_atributo_proceso", type_="check")
        batch.drop_constraint("ck_scm_saldo_inventario_kg_cantidades", type_="check")
        batch.drop_column("atributo_proceso")
        batch.drop_column("cantidad_retirada_kg")
        batch.create_check_constraint(
            "ck_scm_saldo_inventario_kg_cantidades",
            "cantidad_fisica_kg >= 0 AND cantidad_reservada_kg >= 0 AND cantidad_no_disponible_kg >= 0 AND cantidad_reservada_kg + cantidad_no_disponible_kg <= cantidad_fisica_kg",
        )
