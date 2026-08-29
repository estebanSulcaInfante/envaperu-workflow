"""add control weight labels and same-OT open manga relief

Revision ID: f91b6c8d0e75
Revises: f90e5a7b9c64
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa


revision = "f91b6c8d0e75"
down_revision = "f90e5a7b9c64"
branch_labels = None
depends_on = None


CAPABILITY = "MANGA_REASIGNAR_MAQUINISTA"
ROLES = ("SUPERVISOR", "JEFE_PRODUCCION", "GERENTE_GENERAL")
CAPABILITY_MARKER = "Creada por migracion f91b6c8d0e75"


def _seed_capability():
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, descripcion, activo)
        SELECT :code, :name, :marker, true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = :code
        )
    """).bindparams(
        code=CAPABILITY,
        name="Reasignar una manga abierta dentro de la misma OT",
        marker=CAPABILITY_MARKER,
    ))
    for role in ROLES:
        op.execute(sa.text("""
            INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
            SELECT rol.id, capacidad.id
            FROM rol_operativo rol
            JOIN scm_capacidad capacidad ON capacidad.codigo = :capability
            WHERE rol.codigo = :role
              AND NOT EXISTS (
                SELECT 1 FROM scm_rol_capacidad relacion
                WHERE relacion.rol_operativo_id = rol.id
                  AND relacion.capacidad_id = capacidad.id
              )
        """).bindparams(role=role, capability=CAPABILITY))


def upgrade():
    with op.batch_alter_table("scm_etiqueta_manga") as batch:
        batch.drop_constraint("ck_scm_etiqueta_manga_tipo", type_="check")
        batch.create_check_constraint(
            "ck_scm_etiqueta_manga_tipo",
            "tipo IN ('PREPESAJE', 'POSTPESAJE', 'CONTROL_PESO')",
        )

    with op.batch_alter_table("scm_control_peso_manga") as batch:
        batch.add_column(sa.Column(
            "aporte_desde_control_anterior_kg",
            sa.Numeric(15, 3),
            nullable=False,
            server_default="0",
        ))
        batch.add_column(sa.Column("etiqueta_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_scm_control_peso_manga_etiqueta",
            "scm_etiqueta_manga",
            ["etiqueta_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_scm_control_peso_manga_etiqueta", ["etiqueta_id"]
        )

    op.execute(sa.text("""
        UPDATE scm_control_peso_manga
        SET aporte_desde_control_anterior_kg = peso_neto_kg
        WHERE aporte_desde_control_anterior_kg = 0
    """))
    _seed_capability()


def _assert_safe_downgrade():
    bind = op.get_bind()
    facts = bind.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM scm_etiqueta_manga
           WHERE tipo = 'CONTROL_PESO') AS labels,
          (SELECT COUNT(*) FROM scm_control_peso_manga
           WHERE etiqueta_id IS NOT NULL) AS linked_controls
    """)).mappings().one()
    if facts["labels"] or facts["linked_controls"]:
        raise RuntimeError(
            "f91 downgrade bloqueado: existen stickers o controles K2; "
            "conservar el esquema y aplicar forward-fix"
        )

    capability = bind.execute(sa.text("""
        SELECT id, descripcion FROM scm_capacidad WHERE codigo = :code
    """), {"code": CAPABILITY}).mappings().first()
    if capability is None or capability["descripcion"] != CAPABILITY_MARKER:
        return False
    grants = {
        row[0]
        for row in bind.execute(sa.text("""
            SELECT rol.codigo
            FROM scm_rol_capacidad relacion
            JOIN rol_operativo rol ON rol.id = relacion.rol_operativo_id
            WHERE relacion.capacidad_id = :capability_id
        """), {"capability_id": capability["id"]})
    }
    if grants != set(ROLES):
        raise RuntimeError(
            "f91 downgrade bloqueado: los grants de la capacidad K2 "
            "fueron modificados después de la migración"
        )
    return True


def downgrade():
    capability_created_here = _assert_safe_downgrade()
    if capability_created_here:
        op.execute(sa.text("""
            DELETE FROM scm_rol_capacidad
            WHERE capacidad_id IN (
                SELECT id FROM scm_capacidad
                WHERE codigo = :code AND descripcion = :marker
            )
        """).bindparams(code=CAPABILITY, marker=CAPABILITY_MARKER))
        op.execute(sa.text("""
            DELETE FROM scm_capacidad
            WHERE codigo = :code AND descripcion = :marker
        """).bindparams(code=CAPABILITY, marker=CAPABILITY_MARKER))

    with op.batch_alter_table("scm_control_peso_manga") as batch:
        batch.drop_constraint(
            "uq_scm_control_peso_manga_etiqueta", type_="unique"
        )
        batch.drop_constraint(
            "fk_scm_control_peso_manga_etiqueta", type_="foreignkey"
        )
        batch.drop_column("etiqueta_id")
        batch.drop_column("aporte_desde_control_anterior_kg")

    with op.batch_alter_table("scm_etiqueta_manga") as batch:
        batch.drop_constraint("ck_scm_etiqueta_manga_tipo", type_="check")
        batch.create_check_constraint(
            "ck_scm_etiqueta_manga_tipo",
            "tipo IN ('PREPESAJE', 'POSTPESAJE')",
        )
