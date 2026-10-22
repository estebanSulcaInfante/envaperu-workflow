"""Add aggregate OF execution lifecycle states and work pause provenance."""

from alembic import op
import sqlalchemy as sa


revision = "ff60718293a4"
down_revision = "b7f31e9c120a"
branch_labels = None
depends_on = None


def upgrade():
    # Capabilities are part of the migration contract as well as the runtime
    # configuration seeder: a freshly migrated database must not depend on a
    # later bootstrap call to enforce the actor boundary.
    bind = op.get_bind()
    bind.execute(sa.text(
        """
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        VALUES
            ('OF_PAUSAR_EJECUCION', 'Pausar y reanudar la ejecucion de OF', TRUE),
            ('OF_CANCELAR_EJECUCION', 'Cancelar definitivamente la ejecucion de OF', TRUE)
        ON CONFLICT (codigo) DO UPDATE SET activo = TRUE
        """
    ))
    bind.execute(sa.text(
        """
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, cap.id
          FROM rol_operativo rol
          CROSS JOIN scm_capacidad cap
         WHERE rol.codigo IN ('GERENTE_GENERAL', 'JEFE_PRODUCCION')
           AND cap.codigo IN ('OF_PAUSAR_EJECUCION', 'OF_CANCELAR_EJECUCION')
        ON CONFLICT DO NOTHING
        """
    ))
    bind.execute(sa.text(
        """
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, cap.id
          FROM rol_operativo rol
          CROSS JOIN scm_capacidad cap
         WHERE rol.codigo = 'SUPERVISOR'
           AND cap.codigo = 'OF_PAUSAR_EJECUCION'
        ON CONFLICT DO NOTHING
        """
    ))
    with op.batch_alter_table("scm_orden_operacion") as batch:
        batch.drop_constraint("ck_scm_orden_operacion_estado", type_="check")
        batch.create_check_constraint(
            "ck_scm_orden_operacion_estado",
            "estado IN ('BORRADOR', 'LIBERADA', 'PROGRAMADA', 'EN_EJECUCION', 'PAUSADA', 'CERRADA', 'CANCELADA', 'ANULADA')",
        )
    with op.batch_alter_table("scm_corrida_fabricacion") as batch:
        batch.drop_constraint("ck_scm_corrida_estado", type_="check")
        batch.create_check_constraint(
            "ck_scm_corrida_estado",
            "estado IN ('BORRADOR', 'LIBERADA', 'EN_EJECUCION', 'PAUSADA', 'COMPLETADA', 'CANCELADA', 'ANULADA')",
        )
    with op.batch_alter_table("scm_trabajo_ot") as batch:
        batch.drop_constraint("ck_scm_trabajo_ot_estado", type_="check")
        batch.create_check_constraint(
            "ck_scm_trabajo_ot_estado",
            "estado IN ('PLANIFICADO', 'EN_EJECUCION', 'PAUSADO', 'COMPLETADO', 'CANCELADO', 'ANULADO')",
        )
        batch.add_column(sa.Column("estado_previo_pausa", sa.String(length=24), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        """
        SELECT 1
          FROM scm_orden_operacion
         WHERE estado IN ('PAUSADA', 'CANCELADA')
         LIMIT 1
        """
    )).first() or bind.execute(sa.text(
        """
        SELECT 1
          FROM scm_corrida_fabricacion
         WHERE estado IN ('PAUSADA', 'CANCELADA')
         LIMIT 1
        """
    )).first() or bind.execute(sa.text(
        """
        SELECT 1
          FROM scm_trabajo_ot
         WHERE estado = 'CANCELADO' OR estado_previo_pausa IS NOT NULL
         LIMIT 1
        """
    )).first():
        raise RuntimeError(
            "OF_EXECUTION_LIFECYCLE_DOWNGRADE_REQUIRES_RECONCILIATION"
        )
    with op.batch_alter_table("scm_trabajo_ot") as batch:
        batch.drop_column("estado_previo_pausa")
        batch.drop_constraint("ck_scm_trabajo_ot_estado", type_="check")
        batch.create_check_constraint(
            "ck_scm_trabajo_ot_estado",
            "estado IN ('PLANIFICADO', 'EN_EJECUCION', 'PAUSADO', 'COMPLETADO', 'ANULADO')",
        )
    with op.batch_alter_table("scm_corrida_fabricacion") as batch:
        batch.drop_constraint("ck_scm_corrida_estado", type_="check")
        batch.create_check_constraint(
            "ck_scm_corrida_estado",
            "estado IN ('BORRADOR', 'LIBERADA', 'EN_EJECUCION', 'COMPLETADA', 'ANULADA')",
        )
    with op.batch_alter_table("scm_orden_operacion") as batch:
        batch.drop_constraint("ck_scm_orden_operacion_estado", type_="check")
        batch.create_check_constraint(
            "ck_scm_orden_operacion_estado",
            "estado IN ('BORRADOR', 'LIBERADA', 'PROGRAMADA', 'EN_EJECUCION', 'CERRADA', 'ANULADA')",
        )
    bind.execute(sa.text(
        """
        DELETE FROM scm_rol_capacidad
         WHERE capacidad_id IN (
             SELECT id FROM scm_capacidad
              WHERE codigo IN ('OF_PAUSAR_EJECUCION', 'OF_CANCELAR_EJECUCION')
         )
        """
    ))
    bind.execute(sa.text(
        """
        DELETE FROM scm_capacidad
         WHERE codigo IN ('OF_PAUSAR_EJECUCION', 'OF_CANCELAR_EJECUCION')
        """
    ))
