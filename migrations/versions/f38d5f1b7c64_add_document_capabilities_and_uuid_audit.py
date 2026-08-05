"""add OP/OF/OE capabilities and UUID-compatible audit aggregates

Revision ID: f38d5f1b7c64
Revises: f27c4e0a6b53
Create Date: 2026-07-29 15:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f38d5f1b7c64"
down_revision = "f27c4e0a6b53"
branch_labels = None
depends_on = None


CAPABILITIES = (
    ("OP_VER", "Consultar ordenes de produccion"),
    ("OP_CREAR", "Crear ordenes de produccion"),
    ("OP_APROBAR", "Aprobar ordenes de produccion"),
    ("PLANIFICACION_CALCULAR", "Calcular cobertura y propuestas"),
    ("PLANIFICACION_CONFIRMAR", "Confirmar el plan de suministro"),
    ("OF_VER", "Consultar ordenes de fabricacion"),
    ("OF_EDITAR_BORRADOR", "Editar borradores de fabricacion"),
    ("OF_EXCEPCIONAL_CREAR", "Crear fabricacion excepcional"),
    ("OF_LIBERAR", "Liberar ordenes de fabricacion"),
    ("OF_ANULAR", "Anular ordenes de fabricacion"),
    ("OE_VER", "Consultar ordenes de ensamble"),
    ("OE_LIBERAR", "Liberar ordenes de ensamble"),
    ("OE_ANULAR", "Anular ordenes de ensamble"),
)

ROLE_CAPABILITIES = {
    "GERENCIA": ("OP_VER", "OP_APROBAR", "OF_VER", "OE_VER"),
    "PLANIFICACION": (
        "OP_VER",
        "OP_CREAR",
        "PLANIFICACION_CALCULAR",
        "PLANIFICACION_CONFIRMAR",
        "OF_VER",
        "OE_VER",
    ),
    "JEFE_PRODUCCION": (
        "OP_VER",
        "OF_VER",
        "OF_EDITAR_BORRADOR",
        "OF_EXCEPCIONAL_CREAR",
        "OF_LIBERAR",
        "OF_ANULAR",
        "OE_VER",
        "OE_LIBERAR",
        "OE_ANULAR",
    ),
    "SUPERVISOR": ("OP_VER", "OF_VER", "OE_VER"),
    "AUDITORIA_CONSULTA": ("OP_VER", "OF_VER", "OE_VER"),
}


def upgrade():
    op.alter_column(
        "scm_evento",
        "aggregate_id",
        existing_type=sa.Integer(),
        type_=sa.String(64),
        existing_nullable=False,
        postgresql_using="aggregate_id::text",
    )
    for code, name in CAPABILITIES:
        op.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (
                SELECT 1 FROM scm_capacidad WHERE codigo = :code
            )
        """).bindparams(code=code, name=name))
    for role_code, capability_codes in ROLE_CAPABILITIES.items():
        for capability_code in capability_codes:
            op.execute(sa.text("""
                INSERT INTO scm_rol_capacidad (
                    rol_operativo_id, capacidad_id
                )
                SELECT role.id, capability.id
                  FROM rol_operativo AS role
                  JOIN scm_capacidad AS capability
                    ON capability.codigo = :capability_code
                 WHERE role.codigo = :role_code
                   AND NOT EXISTS (
                       SELECT 1
                         FROM scm_rol_capacidad AS relation
                        WHERE relation.rol_operativo_id = role.id
                          AND relation.capacidad_id = capability.id
                   )
            """).bindparams(
                role_code=role_code,
                capability_code=capability_code,
            ))


def downgrade():
    connection = op.get_bind()
    non_numeric = connection.execute(sa.text("""
        SELECT count(*)
          FROM scm_evento
         WHERE aggregate_id !~ '^[0-9]+$'
    """)).scalar_one()
    if non_numeric:
        raise RuntimeError(
            "SCM_DOCUMENT_AUDIT_DOWNGRADE_BLOCKED: existen UUID auditados"
        )
    capability_codes = tuple(code for code, _name in CAPABILITIES)
    connection.execute(
        sa.text("""
            DELETE FROM scm_rol_capacidad
             WHERE capacidad_id IN (
                 SELECT id FROM scm_capacidad
                  WHERE codigo IN :codes
             )
        """).bindparams(
            sa.bindparam("codes", expanding=True, value=capability_codes)
        )
    )
    connection.execute(
        sa.text("""
            DELETE FROM scm_capacidad WHERE codigo IN :codes
        """).bindparams(
            sa.bindparam("codes", expanding=True, value=capability_codes)
        )
    )
    op.alter_column(
        "scm_evento",
        "aggregate_id",
        existing_type=sa.String(64),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="aggregate_id::integer",
    )
