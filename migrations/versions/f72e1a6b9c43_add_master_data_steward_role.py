"""add restricted master data steward role

Revision ID: f72e1a6b9c43
Revises: f71d0e6f8b32
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa


revision = "f72e1a6b9c43"
down_revision = "f71d0e6f8b32"
branch_labels = None
depends_on = None


NEW_CAPABILITIES = (
    (
        "CATALOGO_PROVEEDOR_ADMINISTRAR",
        "Administrar catálogo de proveedores",
        "Permite mantener proveedores sin acceder a compras, recepciones ni documentos operativos.",
    ),
    (
        "CATALOGO_MATERIAL_ADMINISTRAR",
        "Administrar catálogos de materiales",
        "Permite mantener materias primas y sus categorías sin administrar la configuración operativa de recepción.",
    ),
    (
        "CATALOGO_PLANTA_ADMINISTRAR",
        "Administrar catálogos de planta",
        "Permite mantener máquinas y recursos maestros sin operar órdenes de fabricación.",
    ),
)

ROLE_CODE = "GESTOR_MAESTROS"
ROLE_CAPABILITIES = (
    "ARTICULO_VER",
    "ARTICULO_ADMINISTRAR",
    "ESTRUCTURA_VER",
    "ESTRUCTURA_ADMINISTRAR",
    "ESTRUCTURA_PUBLICAR_DIRECTO",
    "RUTA_VER",
    "RUTA_ADMINISTRAR",
    "RUTA_PUBLICAR_DIRECTO",
    "EMPAQUE_VER",
    "EMPAQUE_ADMINISTRAR",
    "EMPAQUE_PUBLICAR_DIRECTO",
    "CATALOGO_PROVEEDOR_ADMINISTRAR",
    "CATALOGO_MATERIAL_ADMINISTRAR",
    "CATALOGO_PLANTA_ADMINISTRAR",
)


def upgrade():
    connection = op.get_bind()
    for code, name, description in NEW_CAPABILITIES:
        connection.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, descripcion, activo)
            SELECT :code, :name, :description, true
            WHERE NOT EXISTS (
                SELECT 1 FROM scm_capacidad WHERE codigo = :code
            )
        """), {"code": code, "name": name, "description": description})

    connection.execute(sa.text("""
        INSERT INTO rol_operativo (codigo, nombre, activo)
        SELECT :code, 'Gestor de datos maestros', true
        WHERE NOT EXISTS (
            SELECT 1 FROM rol_operativo WHERE codigo = :code
        )
    """), {"code": ROLE_CODE})

    connection.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT role.id, capability.id
        FROM rol_operativo AS role
        JOIN scm_capacidad AS capability
          ON capability.codigo IN :capability_codes
        WHERE role.codigo = :role_code
          AND NOT EXISTS (
              SELECT 1
              FROM scm_rol_capacidad AS existing
              WHERE existing.rol_operativo_id = role.id
                AND existing.capacidad_id = capability.id
          )
    """).bindparams(
        sa.bindparam("capability_codes", expanding=True, value=ROLE_CAPABILITIES),
        role_code=ROLE_CODE,
    ))


def downgrade():
    connection = op.get_bind()
    connection.execute(sa.text("""
        DELETE FROM scm_rol_capacidad AS assignment
        USING rol_operativo AS role
        WHERE assignment.rol_operativo_id = role.id
          AND role.codigo = :role_code
    """), {"role_code": ROLE_CODE})
    connection.execute(
        sa.text("DELETE FROM rol_operativo WHERE codigo = :role_code"),
        {"role_code": ROLE_CODE},
    )
    connection.execute(sa.text("""
        DELETE FROM scm_capacidad
        WHERE codigo IN :capability_codes
          AND NOT EXISTS (
              SELECT 1 FROM scm_rol_capacidad
              WHERE capacidad_id = scm_capacidad.id
          )
    """).bindparams(
        sa.bindparam(
            "capability_codes",
            expanding=True,
            value=tuple(code for code, _name, _description in NEW_CAPABILITIES),
        )
    ))
