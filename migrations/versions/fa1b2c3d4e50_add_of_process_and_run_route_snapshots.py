"""Persist OF process resolution and per-target route references.

Revision ID: fa1b2c3d4e50
Revises: f9a12c3e4d10
"""

from alembic import op
import sqlalchemy as sa


revision = "fa1b2c3d4e50"
down_revision = "f9a12c3e4d10"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_orden_fabricacion") as batch:
        batch.add_column(sa.Column("snapshot_proceso", sa.String(20), nullable=True))
        batch.add_column(sa.Column("fuente_proceso", sa.String(24), nullable=True))
        batch.create_check_constraint(
            "ck_scm_of_snapshot_proceso",
            "snapshot_proceso IS NULL OR snapshot_proceso IN ('INYECCION', 'SOPLADO')",
        )
        batch.create_check_constraint(
            "ck_scm_of_fuente_proceso",
            "fuente_proceso IS NULL OR fuente_proceso IN ('EXPLICITO', 'RUTA_OBJETIVOS', 'RUTA_CABECERA')",
        )
        batch.create_check_constraint(
            "ck_scm_of_process_snapshot_pair",
            "(snapshot_proceso IS NULL AND fuente_proceso IS NULL) OR "
            "(snapshot_proceso IS NOT NULL AND fuente_proceso IS NOT NULL)",
        )

    with op.batch_alter_table("scm_corrida_fabricacion") as batch:
        batch.add_column(sa.Column("operacion_ruta_revision_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("operacion_ruta_hash", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_scm_corrida_ruta_operacion",
            "scm_operacion_ruta",
            ["operacion_ruta_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_scm_corrida_route_reference_pair",
            "(operacion_ruta_revision_id IS NULL AND operacion_ruta_hash IS NULL) OR "
            "(operacion_ruta_revision_id IS NOT NULL AND operacion_ruta_hash IS NOT NULL "
            "AND length(operacion_ruta_hash) = 64)",
        )


def downgrade():
    with op.batch_alter_table("scm_corrida_fabricacion") as batch:
        batch.drop_constraint("ck_scm_corrida_route_reference_pair", type_="check")
        batch.drop_constraint("fk_scm_corrida_ruta_operacion", type_="foreignkey")
        batch.drop_column("operacion_ruta_hash")
        batch.drop_column("operacion_ruta_revision_id")

    with op.batch_alter_table("scm_orden_fabricacion") as batch:
        batch.drop_constraint("ck_scm_of_fuente_proceso", type_="check")
        batch.drop_constraint("ck_scm_of_snapshot_proceso", type_="check")
        batch.drop_constraint("ck_scm_of_process_snapshot_pair", type_="check")
        batch.drop_column("fuente_proceso")
        batch.drop_column("snapshot_proceso")
