"""rename assembly-order acronym from OE to OA

Revision ID: f76d5e0a3b87
Revises: f75c4d9e2a76
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa


revision = "f76d5e0a3b87"
down_revision = "f75c4d9e2a76"
branch_labels = None
depends_on = None


def _rename(source, target):
    op.execute(sa.text(
        "UPDATE scm_capacidad "
        "SET codigo = replace(codigo, :source, :target) "
        "WHERE codigo LIKE :pattern"
    ).bindparams(
        source=f"{source}_",
        target=f"{target}_",
        pattern=f"{source}_%",
    ))
    op.execute(sa.text(
        "UPDATE correlativo_catalogo "
        "SET clave = :target_key, prefijo = :target_prefix "
        "WHERE clave = :source_key"
    ).bindparams(
        target_key="ORDEN_ARMADO" if target == "OA" else "ORDEN_ENSAMBLE",
        target_prefix=target,
        source_key="ORDEN_ENSAMBLE" if source == "OE" else "ORDEN_ARMADO",
    ))
    op.execute(sa.text(
        "UPDATE scm_orden_operacion "
        "SET codigo = :target || substr(codigo, 3) "
        "WHERE tipo = 'ENSAMBLE' AND codigo LIKE :pattern"
    ).bindparams(target=target, pattern=f"{source}-%"))
    op.execute(sa.text(
        "UPDATE scm_manga "
        "SET codigo = :target || substr(codigo, 3) "
        "WHERE codigo LIKE :pattern"
    ).bindparams(target=target, pattern=f"{source}%"))
    op.execute(sa.text(
        "UPDATE scm_evento "
        "SET aggregate_type = :target "
        "WHERE aggregate_type = :source"
    ).bindparams(
        target="ORDEN_ARMADO" if target == "OA" else "ORDEN_ENSAMBLE",
        source="ORDEN_ENSAMBLE" if source == "OE" else "ORDEN_ARMADO",
    ))
    op.execute(sa.text(
        "UPDATE scm_evento "
        "SET tipo = replace(tipo, :source, :target) "
        "WHERE tipo LIKE :pattern"
    ).bindparams(
        source=f"{source}_",
        target=f"{target}_",
        pattern=f"{source}_%",
    ))


def upgrade():
    _rename("OE", "OA")


def downgrade():
    _rename("OA", "OE")
