"""guard capabilities excluded from the weighing-only pilot

Revision ID: f89d4e6a8c53
Revises: f88c3e5a7b42
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa


revision = "f89d4e6a8c53"
down_revision = "f88c3e5a7b42"
branch_labels = None
depends_on = None


GUARDED_CAPABILITIES = (
    "OPM_VER",
    "OPM_CREAR",
    "OPM_LIBERAR",
    "OPM_EJECUTAR",
    "OPM_PESO_CONFIRMAR",
    "OPM_CERRAR",
    "MATERIAL_PREPARADO_RECIBIR",
    "MATERIAL_PREPARADO_CALIDAD_RESOLVER",
    "MATERIAL_PREPARADO_RESERVAR",
    "MATERIAL_PREPARADO_EMITIR",
    "MATERIAL_PREPARADO_RECIBIR_MAQUINA",
    "MATERIAL_PREPARADO_CONSUMIR",
    "MATERIAL_PREPARADO_DEVOLVER",
    "MATERIAL_PREPARADO_GENEALOGIA_VER",
    "MANGA_FINALIZAR_PARCIAL",
)

GUARDED_ROLE = "PREPARADOR_MATERIAL"


def _set_active(*, table, code, active):
    op.execute(
        sa.text(
            f"UPDATE {table} SET activo = :active WHERE codigo = :code"
        ).bindparams(code=code, active=active)
    )


def upgrade():
    # Relationships stay intact (no authorization data is deleted), but become
    # ineffective because authorization requires both active role and active
    # capability. The K1 capabilities are intentionally not in this guard.
    for code in GUARDED_CAPABILITIES:
        _set_active(
            table="scm_capacidad", code=code, active=False
        )
    _set_active(
        table="rol_operativo", code=GUARDED_ROLE, active=False
    )


def downgrade():
    # Fail closed: Alembic may remove this revision marker, but a schema
    # downgrade must never reactivate excluded production permissions. A later
    # rollout that enables these modules needs an explicit, audited migration.
    pass
