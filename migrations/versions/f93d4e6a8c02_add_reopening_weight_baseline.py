"""add reopening type and retained weight baseline

Revision ID: f93d4e6a8c02
Revises: f92c3e5a7b94
Create Date: 2026-09-04
"""

from alembic import op
import sqlalchemy as sa


revision = "f93d4e6a8c02"
down_revision = "f92c3e5a7b94"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_reapertura_manga") as batch:
        batch.add_column(sa.Column(
            "tipo_reapertura",
            sa.String(length=28),
            nullable=False,
            server_default="CIERRE_ACCIDENTAL",
        ))
        batch.add_column(sa.Column(
            "peso_base_neto_kg",
            sa.Numeric(15, 3),
            nullable=True,
        ))
        batch.create_check_constraint(
            "ck_scm_reapertura_manga_tipo",
            "tipo_reapertura IN ('CIERRE_ACCIDENTAL', 'CONTINUAR_LLENADO')",
        )
        batch.create_check_constraint(
            "ck_scm_reapertura_manga_base",
            "(tipo_reapertura = 'CIERRE_ACCIDENTAL' AND "
            "peso_base_neto_kg IS NULL) OR "
            "(tipo_reapertura = 'CONTINUAR_LLENADO' AND "
            "peso_base_neto_kg IS NOT NULL AND "
            "peso_base_neto_kg > 0)",
        )


def downgrade():
    connection = op.get_bind()
    retained_count = connection.execute(sa.text(
        "SELECT COUNT(*) FROM scm_reapertura_manga "
        "WHERE tipo_reapertura = 'CONTINUAR_LLENADO'"
    )).scalar_one()
    if retained_count:
        raise RuntimeError(
            "No se puede revertir f93d4e6a8c02 después de conservar una "
            "línea base de reapertura; revierta solo la aplicación."
        )

    with op.batch_alter_table("scm_reapertura_manga") as batch:
        batch.drop_constraint("ck_scm_reapertura_manga_base", type_="check")
        batch.drop_constraint("ck_scm_reapertura_manga_tipo", type_="check")
        batch.drop_column("peso_base_neto_kg")
        batch.drop_column("tipo_reapertura")
