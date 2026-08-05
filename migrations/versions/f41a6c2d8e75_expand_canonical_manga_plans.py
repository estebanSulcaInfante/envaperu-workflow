"""expand manga plans and OT for canonical fabrication outputs

Revision ID: f41a6c2d8e75
Revises: f38d5f1b7c64
Create Date: 2026-07-29 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f41a6c2d8e75"
down_revision = "f38d5f1b7c64"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "scm_lote_articulo",
        sa.Column("orden_operacion_salida_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_scm_lote_articulo_salida_canonica",
        "scm_lote_articulo",
        "scm_orden_operacion_salida",
        ["orden_operacion_salida_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_scm_lote_articulo_salida_canonica",
        "scm_lote_articulo",
        ["orden_operacion_salida_id"],
    )
    op.alter_column(
        "scm_lote_articulo",
        "lote_salida_pieza_color_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.drop_constraint(
        "ck_scm_lote_articulo_clase_c_core",
        "scm_lote_articulo",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_lote_articulo_clase_c_core",
        "scm_lote_articulo",
        "clase IN "
        "('LOTE_SALIDA_PIEZA_COLOR', 'SALIDA_ORDEN_OPERACION')",
    )
    op.create_check_constraint(
        "ck_scm_lote_articulo_origen_exclusivo",
        "scm_lote_articulo",
        "(clase = 'LOTE_SALIDA_PIEZA_COLOR' "
        "AND lote_salida_pieza_color_id IS NOT NULL "
        "AND orden_operacion_salida_id IS NULL) OR "
        "(clase = 'SALIDA_ORDEN_OPERACION' "
        "AND orden_operacion_salida_id IS NOT NULL "
        "AND lote_salida_pieza_color_id IS NULL)",
    )

    op.alter_column(
        "scm_plan_manga_op",
        "orden_id",
        existing_type=sa.String(20),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_scm_plan_manga_of_revision",
        "scm_plan_manga_op",
        ["orden_operacion_id", "revision"],
    )
    op.create_check_constraint(
        "ck_scm_plan_manga_origen_exclusivo",
        "scm_plan_manga_op",
        "(orden_id IS NOT NULL AND orden_operacion_id IS NULL) OR "
        "(orden_id IS NULL AND orden_operacion_id IS NOT NULL)",
    )
    op.create_index(
        "ux_scm_plan_manga_of_activo",
        "scm_plan_manga_op",
        ["orden_operacion_id"],
        unique=True,
        postgresql_where=sa.text(
            "estado = 'ACTIVO' AND orden_operacion_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "estado = 'ACTIVO' AND orden_operacion_id IS NOT NULL"
        ),
    )

    op.alter_column(
        "scm_plan_manga_op_linea",
        "lote_salida_pieza_color_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.alter_column(
        "scm_plan_manga_op_linea",
        "pieza_color_sku_snapshot",
        existing_type=sa.String(50),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_scm_plan_manga_linea_salida_canonica",
        "scm_plan_manga_op_linea",
        ["plan_id", "orden_operacion_salida_id"],
    )
    op.create_check_constraint(
        "ck_scm_plan_manga_linea_origen_exclusivo",
        "scm_plan_manga_op_linea",
        "(lote_salida_pieza_color_id IS NOT NULL "
        "AND orden_operacion_salida_id IS NULL) OR "
        "(lote_salida_pieza_color_id IS NULL "
        "AND orden_operacion_salida_id IS NOT NULL)",
    )
    op.alter_column(
        "registro_diario_produccion",
        "orden_id",
        existing_type=sa.String(20),
        nullable=True,
    )


def downgrade():
    connection = op.get_bind()
    canonical_lots = connection.execute(sa.text("""
        SELECT count(*) FROM scm_lote_articulo
         WHERE orden_operacion_salida_id IS NOT NULL
    """)).scalar_one()
    canonical_plans = connection.execute(sa.text("""
        SELECT count(*) FROM scm_plan_manga_op
         WHERE orden_operacion_id IS NOT NULL
    """)).scalar_one()
    canonical_ots = connection.execute(sa.text("""
        SELECT count(*) FROM registro_diario_produccion
         WHERE orden_id IS NULL AND orden_operacion_id IS NOT NULL
    """)).scalar_one()
    if canonical_lots or canonical_plans or canonical_ots:
        raise RuntimeError(
            "SCM_CANONICAL_MANGA_DOWNGRADE_BLOCKED: existen hechos OF v2"
        )

    op.alter_column(
        "registro_diario_produccion",
        "orden_id",
        existing_type=sa.String(20),
        nullable=False,
    )
    op.drop_constraint(
        "ck_scm_plan_manga_linea_origen_exclusivo",
        "scm_plan_manga_op_linea",
        type_="check",
    )
    op.drop_constraint(
        "uq_scm_plan_manga_linea_salida_canonica",
        "scm_plan_manga_op_linea",
        type_="unique",
    )
    op.alter_column(
        "scm_plan_manga_op_linea",
        "pieza_color_sku_snapshot",
        existing_type=sa.String(50),
        nullable=False,
    )
    op.alter_column(
        "scm_plan_manga_op_linea",
        "lote_salida_pieza_color_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_index(
        "ux_scm_plan_manga_of_activo",
        table_name="scm_plan_manga_op",
    )
    op.drop_constraint(
        "ck_scm_plan_manga_origen_exclusivo",
        "scm_plan_manga_op",
        type_="check",
    )
    op.drop_constraint(
        "uq_scm_plan_manga_of_revision",
        "scm_plan_manga_op",
        type_="unique",
    )
    op.alter_column(
        "scm_plan_manga_op",
        "orden_id",
        existing_type=sa.String(20),
        nullable=False,
    )
    op.drop_constraint(
        "ck_scm_lote_articulo_origen_exclusivo",
        "scm_lote_articulo",
        type_="check",
    )
    op.drop_constraint(
        "ck_scm_lote_articulo_clase_c_core",
        "scm_lote_articulo",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_lote_articulo_clase_c_core",
        "scm_lote_articulo",
        "clase = 'LOTE_SALIDA_PIEZA_COLOR'",
    )
    op.alter_column(
        "scm_lote_articulo",
        "lote_salida_pieza_color_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_constraint(
        "uq_scm_lote_articulo_salida_canonica",
        "scm_lote_articulo",
        type_="unique",
    )
    op.drop_constraint(
        "fk_scm_lote_articulo_salida_canonica",
        "scm_lote_articulo",
        type_="foreignkey",
    )
    op.drop_column("scm_lote_articulo", "orden_operacion_salida_id")
