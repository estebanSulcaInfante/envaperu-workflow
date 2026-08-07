"""add blow molding as a route operation type

Revision ID: f77e6f1b4c98
Revises: f76d5e0a3b87
Create Date: 2026-08-07
"""

from alembic import op


revision = "f77e6f1b4c98"
down_revision = "f76d5e0a3b87"
branch_labels = None
depends_on = None


WORK_CENTER_TYPES = (
    "tipo IN ('INYECCION', 'SOPLADO', 'PREARMADO', "
    "'ENSAMBLE', 'ACABADO', 'EMPAQUE')"
)
ROUTE_OPERATION_TYPES = (
    "tipo IN ('INYECCION', 'SOPLADO', 'PREARMADO', "
    "'ENSAMBLE', 'ACABADO', 'EMPAQUE')"
)
LEGACY_TYPES = (
    "tipo IN ('INYECCION', 'PREARMADO', 'ENSAMBLE', "
    "'ACABADO', 'EMPAQUE')"
)


def upgrade():
    op.drop_constraint(
        "ck_scm_centro_trabajo_tipo",
        "scm_centro_trabajo",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_centro_trabajo_tipo",
        "scm_centro_trabajo",
        WORK_CENTER_TYPES,
    )
    op.drop_constraint(
        "ck_scm_operacion_ruta_tipo",
        "scm_operacion_ruta",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_operacion_ruta_tipo",
        "scm_operacion_ruta",
        ROUTE_OPERATION_TYPES,
    )


def downgrade():
    op.execute(
        "UPDATE scm_operacion_ruta SET tipo = 'INYECCION' "
        "WHERE tipo = 'SOPLADO'"
    )
    op.execute(
        "UPDATE scm_centro_trabajo SET tipo = 'INYECCION' "
        "WHERE tipo = 'SOPLADO'"
    )
    op.drop_constraint(
        "ck_scm_operacion_ruta_tipo",
        "scm_operacion_ruta",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_operacion_ruta_tipo",
        "scm_operacion_ruta",
        LEGACY_TYPES,
    )
    op.drop_constraint(
        "ck_scm_centro_trabajo_tipo",
        "scm_centro_trabajo",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_centro_trabajo_tipo",
        "scm_centro_trabajo",
        LEGACY_TYPES,
    )
