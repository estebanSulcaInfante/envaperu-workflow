"""add governed master color recipes and visual color metadata

Revision ID: e4b6c8d1a207
Revises: d7e9a4c2f105
Create Date: 2026-07-22 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "e4b6c8d1a207"
down_revision = "d7e9a4c2f105"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()

    op.add_column(
        "color_produccion",
        sa.Column("hex_referencia", sa.String(length=7), nullable=True),
    )
    op.add_column(
        "color_produccion",
        sa.Column("activo", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "color_produccion",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("color_produccion", recreate="always") as batch_op:
            batch_op.create_check_constraint(
                "ck_color_produccion_version",
                "version > 0",
            )
    else:
        op.create_check_constraint(
            "ck_color_produccion_version",
            "color_produccion",
            "version > 0",
        )

    op.add_column(
        "colorante",
        sa.Column(
            "tipo",
            sa.String(length=20),
            server_default="COLORANTE",
            nullable=False,
        ),
    )
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("colorante", recreate="always") as batch_op:
            batch_op.create_check_constraint(
                "ck_colorante_tipo",
                "tipo IN ('COLORANTE', 'ADITIVO')",
            )
    else:
        op.create_check_constraint(
            "ck_colorante_tipo",
            "colorante",
            "tipo IN ('COLORANTE', 'ADITIVO')",
        )

    op.create_table(
        "receta_color_maestra",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("color_produccion_id", sa.Integer(), nullable=False),
        sa.Column("producto_sku", sa.String(length=50), nullable=True),
        sa.Column("producto_scope", sa.String(length=60), nullable=False),
        sa.Column("nombre_variante", sa.String(length=120), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("estado", sa.String(length=20), server_default="BORRADOR", nullable=False),
        sa.Column("es_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("base_virgen_kg", sa.Numeric(10, 3), server_default="25", nullable=False),
        sa.Column("notas", sa.String(length=500), nullable=True),
        sa.Column("origen", sa.String(length=30), server_default="MANUAL", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "estado IN ('BORRADOR', 'APROBADA', 'INACTIVA')",
            name="ck_receta_color_maestra_estado",
        ),
        sa.CheckConstraint("revision > 0", name="ck_receta_color_maestra_revision"),
        sa.CheckConstraint("version > 0", name="ck_receta_color_maestra_version"),
        sa.CheckConstraint(
            "base_virgen_kg > 0",
            name="ck_receta_color_maestra_base_virgen",
        ),
        sa.ForeignKeyConstraint(
            ["color_produccion_id"],
            ["color_produccion.id"],
            name="fk_receta_color_maestra_color",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["producto_sku"],
            ["producto_terminado.cod_sku_pt"],
            name="fk_receta_color_maestra_producto",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "color_produccion_id",
            "producto_scope",
            "nombre_variante",
            "revision",
            name="uq_receta_color_maestra_revision",
        ),
    )
    op.create_index(
        "uq_receta_color_maestra_default",
        "receta_color_maestra",
        ["color_produccion_id", "producto_scope"],
        unique=True,
        postgresql_where=sa.text("es_default AND estado = 'APROBADA'"),
        sqlite_where=sa.text("es_default = 1 AND estado = 'APROBADA'"),
    )

    op.create_table(
        "receta_color_linea",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("receta_id", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("tipo_componente", sa.String(length=20), nullable=False),
        sa.Column("cantidad", sa.Numeric(12, 4), nullable=False),
        sa.Column("unidad", sa.String(length=20), nullable=False),
        sa.Column("base_kg", sa.Numeric(10, 3), nullable=True),
        sa.Column("orden", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "tipo_componente IN ('MATERIA_PRIMA', 'COLORANTE', 'ADITIVO')",
            name="ck_receta_color_linea_tipo",
        ),
        sa.CheckConstraint(
            "unidad IN ('FRACCION', 'GRAMOS')",
            name="ck_receta_color_linea_unidad",
        ),
        sa.CheckConstraint("cantidad > 0", name="ck_receta_color_linea_cantidad"),
        sa.CheckConstraint(
            "(tipo_componente = 'MATERIA_PRIMA' AND unidad = 'FRACCION' "
            "AND base_kg IS NULL) OR "
            "(tipo_componente IN ('COLORANTE', 'ADITIVO') AND unidad = 'GRAMOS' "
            "AND base_kg IS NOT NULL AND base_kg > 0)",
            name="ck_receta_color_linea_semantica",
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["scm_material.id"],
            name="fk_receta_color_linea_material",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receta_id"],
            ["receta_color_maestra.id"],
            name="fk_receta_color_linea_receta",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "receta_id",
            "material_id",
            "tipo_componente",
            name="uq_receta_color_linea_material",
        ),
    )


def downgrade():
    connection = op.get_bind()

    op.drop_table("receta_color_linea")
    op.drop_index(
        "uq_receta_color_maestra_default",
        table_name="receta_color_maestra",
    )
    op.drop_table("receta_color_maestra")

    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("colorante", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_colorante_tipo", type_="check")
            batch_op.drop_column("tipo")
    else:
        op.drop_constraint("ck_colorante_tipo", "colorante", type_="check")
        op.drop_column("colorante", "tipo")

    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("color_produccion", recreate="always") as batch_op:
            batch_op.drop_constraint(
                "ck_color_produccion_version",
                type_="check",
            )
            batch_op.drop_column("version")
            batch_op.drop_column("activo")
            batch_op.drop_column("hex_referencia")
    else:
        op.drop_constraint(
            "ck_color_produccion_version",
            "color_produccion",
            type_="check",
        )
        op.drop_column("color_produccion", "version")
        op.drop_column("color_produccion", "activo")
        op.drop_column("color_produccion", "hex_referencia")
