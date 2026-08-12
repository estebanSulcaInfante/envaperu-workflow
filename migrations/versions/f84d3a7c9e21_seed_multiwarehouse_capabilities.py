"""seed capabilities required by multiwarehouse configuration

Revision ID: f84d3a7c9e21
Revises: f83a2b4c6d70
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa


revision = "f84d3a7c9e21"
down_revision = "f83a2b4c6d70"
branch_labels = None
depends_on = None


CAPABILITIES = (
    ("ALMACEN_CONFIG_ADMINISTRAR", "Administrar almacenes y ubicaciones"),
    ("ALMACEN_SCOPE_ADMINISTRAR", "Administrar alcance de almacenes por trabajador"),
    ("INVENTARIO_MOVILIZAR", "Ejecutar movimientos entre ubicaciones"),
    ("INVENTARIO_CONTROL_TRANSVERSAL", "Consultar inventario de todos los almacenes"),
)

ASSIGNMENTS = {
    "GERENTE_GENERAL": tuple(code for code, _name in CAPABILITIES),
    "CONFIGURACION_SCM": (
        "INVENTARIO_VER",
        "ALMACEN_CONFIG_ADMINISTRAR",
        "ALMACEN_SCOPE_ADMINISTRAR",
        "INVENTARIO_CONTROL_TRANSVERSAL",
    ),
    "ALMACEN_RECEPCION": ("INVENTARIO_MOVILIZAR",),
    "GERENCIA": ("INVENTARIO_CONTROL_TRANSVERSAL",),
    "AUDITORIA_CONSULTA": ("INVENTARIO_CONTROL_TRANSVERSAL",),
}


def upgrade():
    for code, name in CAPABILITIES:
        op.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (
                SELECT 1 FROM scm_capacidad WHERE codigo = :code
            )
        """).bindparams(code=code, name=name))

    for role_code, capability_codes in ASSIGNMENTS.items():
        for capability_code in capability_codes:
            op.execute(sa.text("""
                INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
                SELECT rol.id, capacidad.id
                FROM rol_operativo rol
                JOIN scm_capacidad capacidad ON capacidad.codigo = :capability_code
                WHERE rol.codigo = :role_code
                  AND NOT EXISTS (
                    SELECT 1 FROM scm_rol_capacidad relacion
                    WHERE relacion.rol_operativo_id = rol.id
                      AND relacion.capacidad_id = capacidad.id
                  )
            """).bindparams(
                role_code=role_code,
                capability_code=capability_code,
            ))


def downgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE rol_operativo_id IN (
            SELECT id FROM rol_operativo WHERE codigo = 'CONFIGURACION_SCM'
        )
          AND capacidad_id IN (
            SELECT id FROM scm_capacidad WHERE codigo = 'INVENTARIO_VER'
          )
    """))
    codes = tuple(code for code, _name in CAPABILITIES)
    placeholders = ", ".join(f"'{code}'" for code in codes)
    op.execute(sa.text(f"""
        DELETE FROM scm_rol_capacidad
        WHERE capacidad_id IN (
            SELECT id FROM scm_capacidad WHERE codigo IN ({placeholders})
        )
    """))
    op.execute(sa.text(
        f"DELETE FROM scm_capacidad WHERE codigo IN ({placeholders})"
    ))
