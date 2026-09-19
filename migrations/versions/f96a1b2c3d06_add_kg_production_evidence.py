"""add KG production evidence and document closures

Revision ID: f96a1b2c3d06
Revises: f95a1b2c3d05

The migration is additive.  Existing UN history and KG receipts remain
unchanged; the new columns are nullable or carry neutral defaults.
"""

from alembic import op
import sqlalchemy as sa


revision = "f96a1b2c3d06"
down_revision = "f95a1b2c3d05"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_tramo_manga_trabajo") as batch:
        batch.add_column(sa.Column("cantidad_inicio_kg", sa.Numeric(15, 3), nullable=True))
        batch.add_column(sa.Column("cantidad_fin_kg", sa.Numeric(15, 3), nullable=True))
        batch.add_column(sa.Column("cantidad_atribuida_kg", sa.Numeric(15, 3), nullable=True, server_default="0"))
        batch.add_column(sa.Column("calidad_evidencia_kg", sa.String(32), nullable=True))

    with op.batch_alter_table("scm_control_peso_manga") as batch:
        batch.add_column(sa.Column("unidad_evidencia", sa.String(4), nullable=False, server_default="UN"))
        batch.add_column(sa.Column("calidad_evidencia", sa.String(32), nullable=False, server_default="MEDIDA_DIRECTA"))
        batch.drop_constraint("ck_scm_control_peso_manga_conteo", type_="check")
        batch.create_check_constraint(
            "ck_scm_control_peso_manga_conteo",
            "(unidad_evidencia = 'KG' AND conteo_acumulado_un IS NULL) OR "
            "(unidad_evidencia = 'UN' AND "
            "((tipo = 'CORTE_TURNO' AND conteo_acumulado_un > 0) OR "
            "(tipo = 'AVANCE_KG' AND conteo_acumulado_un IS NULL)))",
        )

    with op.batch_alter_table("scm_pesaje_manga") as batch:
        batch.add_column(sa.Column("kg_fabricacion_estimado", sa.Numeric(15, 3), nullable=True))
        batch.add_column(sa.Column("kg_previo_estimado", sa.Numeric(15, 3), nullable=True))
        batch.add_column(sa.Column("atribucion_kg_estado", sa.String(24), nullable=False, server_default="PENDIENTE"))
        batch.add_column(sa.Column("atribucion_kg_base_json", sa.JSON(), nullable=True))

    op.create_table(
        "scm_atribucion_produccion_kg",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("pesaje_id", sa.Integer(), nullable=False),
        sa.Column("tramo_id", sa.Uuid(), nullable=True),
        # OA output mangas have no TrabajoColor owner; their manga/pesaje and
        # closure rows retain the assembly OT identity.
        sa.Column("trabajo_ot_id", sa.Uuid(), nullable=True),
        sa.Column("tipo", sa.String(32), nullable=False),
        sa.Column("cantidad_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("calidad", sa.String(24), nullable=False),
        sa.Column("base_json", sa.JSON(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("tipo IN ('NETO_MEDIDO', 'FABRICACION_ESTIMADA', 'COMPONENTE_PREVIO_ESTIMADO', 'FRONTERA_KG')", name="ck_scm_atribucion_kg_tipo"),
        sa.CheckConstraint("calidad IN ('MEDIDA_DIRECTA', 'ESTIMADA_BOM', 'PENDIENTE')", name="ck_scm_atribucion_kg_calidad"),
        sa.CheckConstraint("cantidad_kg > 0", name="ck_scm_atribucion_kg_cantidad"),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pesaje_id"], ["scm_pesaje_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tramo_id"], ["scm_tramo_manga_trabajo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["trabajo_ot_id"], ["scm_trabajo_ot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_atribucion_kg_public"),
        sa.UniqueConstraint("operation_id", "tipo", name="uq_scm_atribucion_kg_operation_tipo"),
    )
    op.create_index("ix_scm_atribucion_kg_manga", "scm_atribucion_produccion_kg", ["manga_id"])
    op.create_index("ix_scm_atribucion_kg_trabajo", "scm_atribucion_produccion_kg", ["trabajo_ot_id"])

    op.create_table(
        "scm_cierre_productivo_kg",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("documento_tipo", sa.String(2), nullable=False),
        sa.Column("documento_id", sa.String(64), nullable=False),
        sa.Column("ot_id", sa.Integer(), nullable=True),
        sa.Column("tipo_cierre", sa.String(8), nullable=False),
        sa.Column("kg_medido", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("kg_fabricacion_estimado", sa.Numeric(15, 3), nullable=True),
        sa.Column("kg_previo_estimado", sa.Numeric(15, 3), nullable=True),
        sa.Column("desviacion_plan_kg", sa.Numeric(15, 3), nullable=True),
        sa.Column("motivo", sa.String(500), nullable=True),
        sa.Column("pendientes_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("evidencia_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("documento_tipo IN ('OT', 'OF', 'OA')", name="ck_scm_cierre_kg_documento"),
        sa.CheckConstraint("tipo_cierre IN ('NORMAL', 'PARCIAL')", name="ck_scm_cierre_kg_tipo"),
        sa.CheckConstraint("kg_medido >= 0", name="ck_scm_cierre_kg_cantidad"),
        sa.ForeignKeyConstraint(["ot_id"], ["registro_diario_produccion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_cierre_kg_public"),
        sa.UniqueConstraint("documento_tipo", "documento_id", name="uq_scm_cierre_kg_documento"),
        sa.UniqueConstraint("operation_id", name="uq_scm_cierre_kg_operation"),
    )
    op.create_index("ix_scm_cierre_kg_ot", "scm_cierre_productivo_kg", ["ot_id"])


def downgrade():
    bind = op.get_bind()
    for table in ("scm_cierre_productivo_kg", "scm_atribucion_produccion_kg"):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError("downgrade KG production bloqueado: existen evidencias o cierres")
    if bind.execute(sa.text("SELECT COUNT(*) FROM scm_pesaje_manga WHERE atribucion_kg_estado <> 'PENDIENTE' OR kg_fabricacion_estimado IS NOT NULL OR kg_previo_estimado IS NOT NULL")).scalar_one():
        raise RuntimeError("downgrade KG production bloqueado: existen atribuciones en pesajes")
    if bind.execute(sa.text("SELECT COUNT(*) FROM scm_tramo_manga_trabajo WHERE cantidad_inicio_kg IS NOT NULL OR cantidad_fin_kg IS NOT NULL OR cantidad_atribuida_kg <> 0")).scalar_one():
        raise RuntimeError("downgrade KG production bloqueado: existen fronteras KG")
    if bind.execute(sa.text("SELECT COUNT(*) FROM scm_control_peso_manga WHERE unidad_evidencia = 'KG' OR calidad_evidencia <> 'MEDIDA_DIRECTA'")).scalar_one():
        raise RuntimeError("downgrade KG production bloqueado: existen controles KG")

    op.drop_index("ix_scm_cierre_kg_ot", table_name="scm_cierre_productivo_kg")
    op.drop_table("scm_cierre_productivo_kg")
    op.drop_index("ix_scm_atribucion_kg_trabajo", table_name="scm_atribucion_produccion_kg")
    op.drop_index("ix_scm_atribucion_kg_manga", table_name="scm_atribucion_produccion_kg")
    op.drop_table("scm_atribucion_produccion_kg")
    with op.batch_alter_table("scm_pesaje_manga") as batch:
        batch.drop_column("atribucion_kg_base_json")
        batch.drop_column("atribucion_kg_estado")
        batch.drop_column("kg_previo_estimado")
        batch.drop_column("kg_fabricacion_estimado")
    with op.batch_alter_table("scm_control_peso_manga") as batch:
        batch.drop_constraint("ck_scm_control_peso_manga_conteo", type_="check")
        batch.drop_column("calidad_evidencia")
        batch.drop_column("unidad_evidencia")
        batch.create_check_constraint(
            "ck_scm_control_peso_manga_conteo",
            "(tipo = 'CORTE_TURNO' AND conteo_acumulado_un > 0) OR "
            "(tipo = 'AVANCE_KG' AND conteo_acumulado_un IS NULL)",
        )
    with op.batch_alter_table("scm_tramo_manga_trabajo") as batch:
        batch.drop_column("calidad_evidencia_kg")
        batch.drop_column("cantidad_atribuida_kg")
        batch.drop_column("cantidad_fin_kg")
        batch.drop_column("cantidad_inicio_kg")
