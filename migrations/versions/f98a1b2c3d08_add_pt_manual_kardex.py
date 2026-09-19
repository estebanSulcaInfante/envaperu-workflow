"""Extend the canonical UN Kardex with PT manual movements.

Revision ID: f98a1b2c3d08
Revises: f97a1b2c3d07
"""

from alembic import op
import sqlalchemy as sa


revision = "f98a1b2c3d08"
down_revision = "f97a1b2c3d07"
branch_labels = None
depends_on = None


_MOVEMENT_CHECK = (
    "tipo IN ('SALDO_INICIAL', 'INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', "
    "'AJUSTE_NEGATIVO', 'CONSUMO', 'TRASLADO_SALIDA', 'TRASLADO_ENTRADA', "
    "'RETORNO_SALIDA', 'RETORNO_ENTRADA', 'ENTRADA_MANUAL_PT', "
    "'SALIDA_MANUAL_PT', 'AJUSTE_POSITIVO_MANUAL_PT', "
    "'AJUSTE_NEGATIVO_MANUAL_PT')"
)


def _seed_pt_manual_capability():
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT 'INVENTARIO_PT_MOVIMIENTO',
               'Registrar entradas y salidas manuales de PT', true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad
            WHERE codigo = 'INVENTARIO_PT_MOVIMIENTO'
        )
    """))
def _remove_pt_manual_capability():
    op.execute(sa.text("""
        DELETE FROM scm_capacidad
        WHERE codigo = 'INVENTARIO_PT_MOVIMIENTO'
          AND NOT EXISTS (
              SELECT 1 FROM scm_rol_capacidad
              WHERE capacidad_id = scm_capacidad.id
          )
    """))


def upgrade():
    bind = op.get_bind()
    with op.batch_alter_table("scm_movimiento_inventario") as batch:
        batch.drop_constraint("ck_scm_movimiento_inventario_tipo", type_="check")
        batch.create_check_constraint("ck_scm_movimiento_inventario_tipo", _MOVEMENT_CHECK)
        batch.add_column(sa.Column("fecha_operativa", sa.Date(), nullable=True))
        batch.add_column(sa.Column("referencia", sa.String(length=120), nullable=True))
    op.create_index(
        "ix_scm_movimiento_inventario_fecha_operativa_id",
        "scm_movimiento_inventario",
        ["fecha_operativa", "id"],
    )
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION scm_guard_pt_manual_movement_immutable()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $fn$
            BEGIN
                IF OLD.tipo IN ('ENTRADA_MANUAL_PT', 'SALIDA_MANUAL_PT',
                                'AJUSTE_POSITIVO_MANUAL_PT', 'AJUSTE_NEGATIVO_MANUAL_PT')
                   OR NEW.tipo IN ('ENTRADA_MANUAL_PT', 'SALIDA_MANUAL_PT',
                                   'AJUSTE_POSITIVO_MANUAL_PT', 'AJUSTE_NEGATIVO_MANUAL_PT') THEN
                    RAISE EXCEPTION 'PT manual movements are append-only';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $fn$;
        """))
        op.execute(sa.text("""
            CREATE TRIGGER trg_scm_pt_manual_movement_immutable
            BEFORE UPDATE OR DELETE ON scm_movimiento_inventario
            FOR EACH ROW EXECUTE FUNCTION scm_guard_pt_manual_movement_immutable()
        """))
    _seed_pt_manual_capability()


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT COUNT(*) FROM scm_movimiento_inventario "
        "WHERE tipo IN ('ENTRADA_MANUAL_PT', 'SALIDA_MANUAL_PT', "
        "'AJUSTE_POSITIVO_MANUAL_PT', 'AJUSTE_NEGATIVO_MANUAL_PT')"
    )).scalar_one():
        raise RuntimeError("downgrade PT manual bloqueado: existen movimientos historicos")
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_scm_pt_manual_movement_immutable ON scm_movimiento_inventario"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS scm_guard_pt_manual_movement_immutable()"))
    _remove_pt_manual_capability()
    op.drop_index("ix_scm_movimiento_inventario_fecha_operativa_id", table_name="scm_movimiento_inventario")
    with op.batch_alter_table("scm_movimiento_inventario") as batch:
        batch.drop_column("referencia")
        batch.drop_column("fecha_operativa")
        batch.drop_constraint("ck_scm_movimiento_inventario_tipo", type_="check")
        batch.create_check_constraint(
            "ck_scm_movimiento_inventario_tipo",
            "tipo IN ('SALDO_INICIAL', 'INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', "
            "'AJUSTE_NEGATIVO', 'CONSUMO', 'TRASLADO_SALIDA', "
            "'TRASLADO_ENTRADA', 'RETORNO_SALIDA', 'RETORNO_ENTRADA')",
        )
