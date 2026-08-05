"""harden SCM function search paths

Revision ID: f71d0e6f8b32
Revises: f70c9d5e7a21
Create Date: 2026-08-05
"""

from alembic import op


revision = "f71d0e6f8b32"
down_revision = "f70c9d5e7a21"
branch_labels = None
depends_on = None


FUNCTIONS = (
    "scm_article_child_guard()",
    "scm_article_parent_guard()",
    "scm_assert_article_subtype(integer)",
    "scm_impedir_mutacion_evento()",
    "scm_packaging_rule_identity_guard()",
    "scm_packaging_rule_revision_guard()",
    "scm_route_child_guard()",
    "scm_route_revision_guard()",
    "scm_structure_component_guard()",
    "scm_structure_revision_guard()",
    "scm_validar_clase_material_legacy()",
    "scm_validar_colorante_material()",
    "scm_validar_materia_prima_material()",
    "scm_validar_mutacion_detalle_recepcion()",
    "scm_validar_mutacion_linea_oc()",
)


def upgrade():
    for function in FUNCTIONS:
        op.execute(
            f"ALTER FUNCTION public.{function} "
            "SET search_path = pg_catalog, public"
        )


def downgrade():
    for function in FUNCTIONS:
        op.execute(f"ALTER FUNCTION public.{function} RESET search_path")
