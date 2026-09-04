"""add repeatable kg-only progress controls

Revision ID: f91b2d4e6c83
Revises: f92c7d9e1f86
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa


revision = "f91b2d4e6c83"
down_revision = "f92c7d9e1f86"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_control_peso_manga") as batch:
        batch.drop_constraint(
            "uq_scm_control_peso_manga_tramo", type_="unique"
        )
        batch.drop_constraint(
            "ck_scm_control_peso_manga_tipo", type_="check"
        )
        batch.drop_constraint(
            "ck_scm_control_peso_manga_conteo", type_="check"
        )
        batch.alter_column(
            "conteo_acumulado_un",
            existing_type=sa.Numeric(15, 3),
            nullable=True,
        )
        batch.create_check_constraint(
            "ck_scm_control_peso_manga_tipo",
            "tipo IN ('CORTE_TURNO', 'AVANCE_KG')",
        )
        batch.create_check_constraint(
            "ck_scm_control_peso_manga_conteo",
            "(tipo = 'CORTE_TURNO' AND conteo_acumulado_un > 0) OR "
            "(tipo = 'AVANCE_KG' AND conteo_acumulado_un IS NULL)",
        )

    op.create_index(
        "uq_scm_control_peso_manga_corte_tramo",
        "scm_control_peso_manga",
        ["tramo_id"],
        unique=True,
        postgresql_where=sa.text("tipo = 'CORTE_TURNO'"),
        sqlite_where=sa.text("tipo = 'CORTE_TURNO'"),
    )


def downgrade():
    connection = op.get_bind()
    progress_count = connection.execute(sa.text(
        "SELECT COUNT(*) FROM scm_control_peso_manga "
        "WHERE tipo = 'AVANCE_KG'"
    )).scalar_one()
    if progress_count:
        raise RuntimeError(
            "No se puede revertir f91b2d4e6c83 mientras existan "
            "controles AVANCE_KG. Conserve la versión de aplicación o "
            "realice una conciliación explícita."
        )

    op.drop_index(
        "uq_scm_control_peso_manga_corte_tramo",
        table_name="scm_control_peso_manga",
    )
    with op.batch_alter_table("scm_control_peso_manga") as batch:
        batch.drop_constraint(
            "ck_scm_control_peso_manga_tipo", type_="check"
        )
        batch.drop_constraint(
            "ck_scm_control_peso_manga_conteo", type_="check"
        )
        batch.alter_column(
            "conteo_acumulado_un",
            existing_type=sa.Numeric(15, 3),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_scm_control_peso_manga_tipo",
            "tipo = 'CORTE_TURNO'",
        )
        batch.create_check_constraint(
            "ck_scm_control_peso_manga_conteo",
            "conteo_acumulado_un > 0",
        )
        batch.create_unique_constraint(
            "uq_scm_control_peso_manga_tramo", ["tramo_id"]
        )
