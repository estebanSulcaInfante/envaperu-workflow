"""add physical inventory units

Revision ID: e5a72c4d9f10
Revises: d4f61b2a8c30
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


revision = "e5a72c4d9f10"
down_revision = "d4f61b2a8c30"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_lectura_peso_preparacion") as batch:
        batch.drop_constraint("ck_scm_lectura_prep_metodo", type_="check")
        batch.create_check_constraint(
            "ck_scm_lectura_prep_metodo",
            "metodo IN ('CONTINGENCIA_MANUAL', 'BALANZA_ESTACION')",
        )
    with op.batch_alter_table("scm_bolsa_material_preparado") as batch:
        batch.drop_constraint("ck_scm_bolsa_mat_prep_metodo", type_="check")
        batch.create_check_constraint(
            "ck_scm_bolsa_mat_prep_metodo",
            "metodo IN ('CONTINGENCIA_MANUAL', 'BALANZA_ESTACION')",
        )
    with op.batch_alter_table("scm_lote_apertura_inventario") as batch:
        batch.add_column(sa.Column(
            "metodo", sa.String(32), nullable=False,
            server_default="TABULAR_CONTINGENCIA",
        ))

    op.create_table(
        "scm_unidad_logistica_inventario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column("qr_value", sa.String(100), nullable=False),
        sa.Column("lote_apertura_id", sa.Uuid(), nullable=False),
        sa.Column("apertura_linea_id", sa.Integer(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=True),
        sa.Column("material_scm_id", sa.Integer(), nullable=True),
        sa.Column("ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("peso_bruto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("tara_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("peso_neto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_disponible_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("estado_calidad", sa.String(16), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="REGISTRADA"),
        sa.Column("station_id", sa.String(64), nullable=False),
        sa.Column("capturado_por_id", sa.Integer(), nullable=False),
        sa.Column("capture_operation_id", sa.Uuid(), nullable=False),
        sa.Column("reading_stable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "(articulo_scm_id IS NOT NULL) <> (material_scm_id IS NOT NULL)",
            name="ck_scm_unidad_logistica_item_unico",
        ),
        sa.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND "
            "peso_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_unidad_logistica_pesos",
        ),
        sa.CheckConstraint(
            "estado IN ('REGISTRADA', 'DISPONIBLE', 'BLOQUEADA', 'CONSUMIDA', 'ANULADA')",
            name="ck_scm_unidad_logistica_estado",
        ),
        sa.ForeignKeyConstraint(["lote_apertura_id"], ["scm_lote_apertura_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["apertura_linea_id"], ["scm_lote_apertura_linea.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["material_scm_id"], ["scm_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["capturado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_unidad_logistica_codigo"),
        sa.UniqueConstraint("qr_value", name="uq_scm_unidad_logistica_qr"),
        sa.UniqueConstraint("capture_operation_id", name="uq_scm_unidad_logistica_capture"),
    )
    op.create_index("ix_scm_unidad_logistica_apertura", "scm_unidad_logistica_inventario", ["lote_apertura_id"])
    op.create_index("ix_scm_unidad_logistica_linea", "scm_unidad_logistica_inventario", ["apertura_linea_id"])
    op.create_index("ix_scm_unidad_logistica_ubicacion", "scm_unidad_logistica_inventario", ["ubicacion_id", "estado"])
    op.create_table(
        "scm_uso_unidad_logistica_preparacion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lectura_id", sa.Uuid(), nullable=False),
        sa.Column("unidad_logistica_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False, server_default="RESERVADA"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("cantidad_kg > 0", name="ck_scm_uso_ul_prep_cantidad"),
        sa.CheckConstraint("estado IN ('RESERVADA', 'CONSUMIDA', 'LIBERADA')", name="ck_scm_uso_ul_prep_estado"),
        sa.ForeignKeyConstraint(["lectura_id"], ["scm_lectura_peso_preparacion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["unidad_logistica_id"], ["scm_unidad_logistica_inventario.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lectura_id", "unidad_logistica_id", name="uq_scm_uso_ul_prep_fuente"),
    )
    op.create_index("ix_scm_uso_ul_prep_unidad", "scm_uso_unidad_logistica_preparacion", ["unidad_logistica_id", "estado"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE scm_unidad_logistica_inventario ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE scm_unidad_logistica_inventario FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE scm_uso_unidad_logistica_preparacion ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE scm_uso_unidad_logistica_preparacion FORCE ROW LEVEL SECURITY")
        op.execute(sa.text("""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON TABLE scm_unidad_logistica_inventario FROM anon;
                REVOKE ALL ON TABLE scm_uso_unidad_logistica_preparacion FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON TABLE scm_unidad_logistica_inventario FROM authenticated;
                REVOKE ALL ON TABLE scm_uso_unidad_logistica_preparacion FROM authenticated;
              END IF;
            END $$;
        """))


def downgrade():
    op.drop_index("ix_scm_uso_ul_prep_unidad", table_name="scm_uso_unidad_logistica_preparacion")
    op.drop_table("scm_uso_unidad_logistica_preparacion")
    op.drop_index("ix_scm_unidad_logistica_ubicacion", table_name="scm_unidad_logistica_inventario")
    op.drop_index("ix_scm_unidad_logistica_linea", table_name="scm_unidad_logistica_inventario")
    op.drop_index("ix_scm_unidad_logistica_apertura", table_name="scm_unidad_logistica_inventario")
    op.drop_table("scm_unidad_logistica_inventario")
    with op.batch_alter_table("scm_bolsa_material_preparado") as batch:
        batch.drop_constraint("ck_scm_bolsa_mat_prep_metodo", type_="check")
        batch.create_check_constraint(
            "ck_scm_bolsa_mat_prep_metodo", "metodo = 'CONTINGENCIA_MANUAL'",
        )
    with op.batch_alter_table("scm_lectura_peso_preparacion") as batch:
        batch.drop_constraint("ck_scm_lectura_prep_metodo", type_="check")
        batch.create_check_constraint(
            "ck_scm_lectura_prep_metodo", "metodo = 'CONTINGENCIA_MANUAL'",
        )
    with op.batch_alter_table("scm_lote_apertura_inventario") as batch:
        batch.drop_column("metodo")

