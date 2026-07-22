"""persist OP recipe snapshots, physical outputs and machine type governance

Revision ID: b7e9f1a4d510
Revises: a6d8e0f3c409
Create Date: 2026-07-23 00:30:00.000000

"""
import re

from alembic import op
import sqlalchemy as sa


revision = "b7e9f1a4d510"
down_revision = "a6d8e0f3c409"
branch_labels = None
depends_on = None


def _next_type_code(connection):
    source = sa.table("tipo_maquina", sa.column("codigo", sa.String()))
    pattern = re.compile(r"^TMQ-(\d+)$")
    maximum = 0
    for raw in connection.execute(sa.select(source.c.codigo)).scalars():
        match = pattern.fullmatch(str(raw or "").strip().upper())
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum + 1


def upgrade():
    connection = op.get_bind()
    with op.batch_alter_table("tipo_maquina") as batch_op:
        batch_op.add_column(
            sa.Column("version", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.create_check_constraint("ck_tipo_maquina_version", "version > 0")

    counter = sa.table(
        "correlativo_catalogo",
        sa.column("clave", sa.String()),
        sa.column("prefijo", sa.String()),
        sa.column("siguiente_valor", sa.BigInteger()),
        sa.column("ancho", sa.SmallInteger()),
    )
    connection.execute(counter.insert().values(
        clave="TIPO_MAQUINA",
        prefijo="TMQ",
        siguiente_valor=_next_type_code(connection),
        ancho=6,
    ))

    with op.batch_alter_table("lote_color") as batch_op:
        batch_op.add_column(sa.Column("receta_color_maestra_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("receta_revision_snapshot", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("receta_nombre_snapshot", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("receta_base_virgen_kg_snapshot", sa.Numeric(10, 3), nullable=True))
        batch_op.create_foreign_key(
            "fk_lote_color_receta_maestra",
            "receta_color_maestra",
            ["receta_color_maestra_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.create_table(
        "lote_salida_pieza_color",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lote_color_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_pieza_id", sa.Integer(), nullable=False),
        sa.Column("pieza_id", sa.Integer(), nullable=False),
        sa.Column("pieza_color_sku", sa.String(length=50), nullable=False),
        sa.Column("cavidades_snapshot", sa.Integer(), nullable=False),
        sa.Column("peso_unitario_snapshot_gr", sa.Numeric(12, 4), nullable=False),
        sa.Column("cantidad_objetivo", sa.Numeric(14, 4), server_default="0", nullable=False),
        sa.Column("kg_objetivo_neto", sa.Numeric(14, 4), server_default="0", nullable=False),
        sa.Column("cantidad_buena_real", sa.Numeric(14, 4), server_default="0", nullable=False),
        sa.Column("cantidad_rechazada_real", sa.Numeric(14, 4), server_default="0", nullable=False),
        sa.Column("kg_bueno_real", sa.Numeric(14, 4), server_default="0", nullable=False),
        sa.CheckConstraint("cavidades_snapshot > 0", name="ck_lote_salida_cavidades"),
        sa.CheckConstraint("peso_unitario_snapshot_gr > 0", name="ck_lote_salida_peso_unitario"),
        sa.CheckConstraint(
            "cantidad_objetivo >= 0 AND kg_objetivo_neto >= 0",
            name="ck_lote_salida_objetivos_no_negativos",
        ),
        sa.CheckConstraint(
            "cantidad_buena_real >= 0 AND cantidad_rechazada_real >= 0 AND kg_bueno_real >= 0",
            name="ck_lote_salida_reales_no_negativos",
        ),
        sa.ForeignKeyConstraint(["lote_color_id"], ["lote_color.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_pieza_id"], ["snapshot_composicion_molde.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pieza_id"], ["pieza.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pieza_color_sku"], ["pieza_color.sku"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lote_color_id", "pieza_id", name="uq_lote_salida_lote_pieza"),
    )
    op.create_index(
        "ix_lote_salida_pieza_color_sku",
        "lote_salida_pieza_color",
        ["pieza_color_sku"],
    )


def downgrade():
    op.drop_index("ix_lote_salida_pieza_color_sku", table_name="lote_salida_pieza_color")
    op.drop_table("lote_salida_pieza_color")
    with op.batch_alter_table("lote_color") as batch_op:
        batch_op.drop_constraint("fk_lote_color_receta_maestra", type_="foreignkey")
        batch_op.drop_column("receta_base_virgen_kg_snapshot")
        batch_op.drop_column("receta_nombre_snapshot")
        batch_op.drop_column("receta_revision_snapshot")
        batch_op.drop_column("receta_color_maestra_id")
    counter = sa.table("correlativo_catalogo", sa.column("clave", sa.String()))
    op.get_bind().execute(
        sa.delete(counter).where(counter.c.clave == "TIPO_MAQUINA")
    )
    with op.batch_alter_table("tipo_maquina") as batch_op:
        batch_op.drop_constraint("ck_tipo_maquina_version", type_="check")
        batch_op.drop_column("version")
