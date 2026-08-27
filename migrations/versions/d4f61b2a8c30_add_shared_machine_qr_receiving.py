"""add shared machine QR receiving evidence

Revision ID: d4f61b2a8c30
Revises: c3a91f6e2d47
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


revision = "d4f61b2a8c30"
down_revision = "c3a91f6e2d47"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_ubicacion_inventario") as batch:
        batch.add_column(sa.Column("maquina_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_scm_ubicacion_inventario_maquina",
            "maquina",
            ["maquina_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_scm_ubicacion_inventario_maquina", ["maquina_id"]
        )

    # Backfill conservador para los puntos ya creados por la UAT. Solo enlaza
    # cuando el codigo hace explicita la misma maquina; no infiere por nombre.
    op.execute(sa.text(
        "UPDATE scm_ubicacion_inventario "
        "SET maquina_id = ("
        "  SELECT maquina.id FROM maquina "
        "  WHERE scm_ubicacion_inventario.codigo = 'P-ENVA-' || maquina.codigo"
        ") "
        "WHERE maquina_id IS NULL AND tipo = 'PUNTO_PRODUCCION' "
        "AND EXISTS ("
        "  SELECT 1 FROM maquina "
        "  WHERE scm_ubicacion_inventario.codigo = 'P-ENVA-' || maquina.codigo"
        ")"
    ))

    with op.batch_alter_table("scm_emision_material_preparado") as batch:
        batch.add_column(
            sa.Column("maquina_recepcion_id", sa.Integer(), nullable=True)
        )
        batch.add_column(sa.Column("punto_recepcion_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("maquina_qr_snapshot", sa.String(80), nullable=True))
        batch.add_column(sa.Column("bolsa_qr_snapshot", sa.String(80), nullable=True))
        batch.add_column(sa.Column("recepcion_metodo", sa.String(32), nullable=True))
        batch.create_foreign_key(
            "fk_scm_emision_mat_prep_maquina_recepcion",
            "maquina",
            ["maquina_recepcion_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_scm_emision_mat_prep_punto_recepcion",
            "scm_ubicacion_inventario",
            ["punto_recepcion_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_scm_emision_mat_prep_maquina", ["maquina_recepcion_id"]
        )


def downgrade():
    with op.batch_alter_table("scm_emision_material_preparado") as batch:
        batch.drop_index("ix_scm_emision_mat_prep_maquina")
        batch.drop_constraint(
            "fk_scm_emision_mat_prep_punto_recepcion", type_="foreignkey"
        )
        batch.drop_constraint(
            "fk_scm_emision_mat_prep_maquina_recepcion", type_="foreignkey"
        )
        batch.drop_column("recepcion_metodo")
        batch.drop_column("bolsa_qr_snapshot")
        batch.drop_column("maquina_qr_snapshot")
        batch.drop_column("punto_recepcion_id")
        batch.drop_column("maquina_recepcion_id")

    with op.batch_alter_table("scm_ubicacion_inventario") as batch:
        batch.drop_constraint(
            "uq_scm_ubicacion_inventario_maquina", type_="unique"
        )
        batch.drop_constraint(
            "fk_scm_ubicacion_inventario_maquina", type_="foreignkey"
        )
        batch.drop_column("maquina_id")
