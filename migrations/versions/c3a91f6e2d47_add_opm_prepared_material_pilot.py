"""add canonical OPM and prepared-material pilot

Revision ID: c3a91f6e2d47
Revises: 0b548129a29a
Create Date: 2026-08-17
"""

from alembic import op
import sqlalchemy as sa


revision = "c3a91f6e2d47"
down_revision = "0b548129a29a"
branch_labels = None
depends_on = None


NEW_TABLES = (
    "scm_requerimiento_material_preparado",
    "scm_orden_preparacion_material",
    "scm_lectura_peso_preparacion",
    "scm_aprobacion_lectura_peso_preparacion",
    "scm_aporte_preparacion_material",
    "scm_lote_material_preparado",
    "scm_bolsa_material_preparado",
    "scm_decision_calidad_material_preparado",
    "scm_asignacion_requerimiento_preparacion",
    "scm_reserva_material_preparado",
    "scm_emision_material_preparado",
    "scm_recepcion_bolsa_material_preparado",
    "scm_saldo_material_preparado",
    "scm_movimiento_material_preparado",
)

CAPABILITIES = (
    ("OPM_VER", "Consultar preparaciones de material"),
    ("OPM_CREAR", "Crear requerimientos y ordenes de preparacion"),
    ("OPM_LIBERAR", "Liberar ordenes de preparacion de material"),
    ("OPM_EJECUTAR", "Registrar ejecucion y pesos de preparacion"),
    ("OPM_PESO_CONFIRMAR", "Confirmar peso manual como segundo actor"),
    ("OPM_CERRAR", "Conciliar y cerrar ordenes de preparacion"),
    ("MATERIAL_PREPARADO_RECIBIR", "Recibir bolsas de material preparado"),
    (
        "MATERIAL_PREPARADO_CALIDAD_RESOLVER",
        "Resolver Calidad de material preparado",
    ),
    ("MATERIAL_PREPARADO_RESERVAR", "Reservar bolsas completas preparadas"),
    ("MATERIAL_PREPARADO_EMITIR", "Preparar y despachar material preparado"),
    (
        "MATERIAL_PREPARADO_RECIBIR_MAQUINA",
        "Confirmar recepcion de material preparado en maquina",
    ),
    ("MATERIAL_PREPARADO_CONSUMIR", "Consumir bolsas completas preparadas"),
    ("MATERIAL_PREPARADO_DEVOLVER", "Retornar bolsas completas preparadas"),
    (
        "MATERIAL_PREPARADO_GENEALOGIA_VER",
        "Consultar genealogia de material preparado",
    ),
)

ROLE_CAPABILITIES = {
    "GERENTE_GENERAL": tuple(code for code, _name in CAPABILITIES),
    "JEFE_PRODUCCION": (
        "OPM_VER", "OPM_CREAR", "OPM_LIBERAR", "OPM_EJECUTAR",
        "OPM_PESO_CONFIRMAR", "OPM_CERRAR", "MATERIAL_PREPARADO_RESERVAR",
        "MATERIAL_PREPARADO_RECIBIR_MAQUINA",
        "MATERIAL_PREPARADO_CONSUMIR", "MATERIAL_PREPARADO_GENEALOGIA_VER",
    ),
    "CONFIGURACION_SCM": (
        "OPM_VER", "OPM_CREAR", "OPM_LIBERAR", "OPM_EJECUTAR",
        "OPM_PESO_CONFIRMAR", "OPM_CERRAR", "MATERIAL_PREPARADO_RESERVAR",
        "MATERIAL_PREPARADO_RECIBIR_MAQUINA",
        "MATERIAL_PREPARADO_CONSUMIR", "MATERIAL_PREPARADO_GENEALOGIA_VER",
    ),
    "SUPERVISOR": (
        "OPM_VER", "OPM_PESO_CONFIRMAR", "MATERIAL_PREPARADO_RESERVAR",
        "MATERIAL_PREPARADO_RECIBIR_MAQUINA",
        "MATERIAL_PREPARADO_CONSUMIR", "MATERIAL_PREPARADO_GENEALOGIA_VER",
    ),
    "PREPARADOR_MATERIAL": (
        "INVENTARIO_VER", "OPM_VER", "OPM_EJECUTAR",
        "MATERIAL_PREPARADO_GENEALOGIA_VER",
    ),
    "ALMACEN_RECEPCION": (
        "OPM_VER", "MATERIAL_PREPARADO_RECIBIR",
        "MATERIAL_PREPARADO_EMITIR", "MATERIAL_PREPARADO_DEVOLVER",
        "MATERIAL_PREPARADO_GENEALOGIA_VER",
    ),
    "CALIDAD": (
        "OPM_VER", "MATERIAL_PREPARADO_CALIDAD_RESOLVER",
        "MATERIAL_PREPARADO_GENEALOGIA_VER",
    ),
}


def _seed_authorization(connection):
    for code, name in CAPABILITIES:
        connection.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (
                SELECT 1 FROM scm_capacidad WHERE codigo = :code
            )
        """), {"code": code, "name": name})
    connection.execute(sa.text("""
        INSERT INTO rol_operativo (codigo, nombre, activo)
        SELECT 'PREPARADOR_MATERIAL', 'Preparador de material', true
        WHERE NOT EXISTS (
            SELECT 1 FROM rol_operativo WHERE codigo = 'PREPARADOR_MATERIAL'
        )
    """))
    for role_code, capability_codes in ROLE_CAPABILITIES.items():
        for capability_code in capability_codes:
            connection.execute(sa.text("""
                INSERT INTO scm_rol_capacidad (
                    rol_operativo_id, capacidad_id
                )
                SELECT role.id, capability.id
                FROM rol_operativo AS role
                JOIN scm_capacidad AS capability
                  ON capability.codigo = :capability_code
                WHERE role.codigo = :role_code
                  AND NOT EXISTS (
                    SELECT 1 FROM scm_rol_capacidad AS existing
                    WHERE existing.rol_operativo_id = role.id
                      AND existing.capacidad_id = capability.id
                  )
            """), {
                "role_code": role_code,
                "capability_code": capability_code,
            })


def _remove_authorization(connection):
    capability_codes = tuple(code for code, _name in CAPABILITIES)
    for code in capability_codes:
        connection.execute(sa.text("""
            DELETE FROM scm_rol_capacidad
            WHERE capacidad_id IN (
                SELECT id FROM scm_capacidad WHERE codigo = :code
            )
        """), {"code": code})
        connection.execute(sa.text("""
            DELETE FROM scm_capacidad
            WHERE codigo = :code
              AND NOT EXISTS (
                SELECT 1 FROM scm_rol_capacidad
                WHERE capacidad_id = scm_capacidad.id
              )
        """), {"code": code})
    connection.execute(sa.text("""
        DELETE FROM rol_operativo
        WHERE codigo = 'PREPARADOR_MATERIAL'
          AND NOT EXISTS (
            SELECT 1 FROM trabajador_rol
            WHERE rol_operativo_id = rol_operativo.id
          )
    """))


def _protect_tables_on_postgres(connection):
    if connection.dialect.name != "postgresql":
        return
    schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    preparer = connection.dialect.identifier_preparer
    quoted_schema = preparer.quote(schema)
    for table_name in NEW_TABLES:
        qualified = f"{quoted_schema}.{preparer.quote(table_name)}"
        op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
        op.execute(sa.text(f"""
            DO $body$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM anon;
              END IF;
              IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'authenticated'
              ) THEN
                REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM authenticated;
              END IF;
            END
            $body$;
        """))


def upgrade():
    op.create_table('scm_requerimiento_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('corrida_fabricacion_id', sa.Uuid(), nullable=False),
    sa.Column('receta_revision_id', sa.Integer(), nullable=False),
    sa.Column('cantidad_requerida_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('composicion_hash', sa.String(length=64), nullable=False),
    sa.Column('composicion_snapshot_json', sa.JSON(), nullable=False),
    sa.Column('estado', sa.String(length=24), server_default='PENDIENTE', nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("estado IN ('PENDIENTE', 'CUBIERTA_PARCIAL', 'CUBIERTA', 'SATISFECHA', 'CANCELADA')", name='ck_scm_req_mat_prep_estado'),
    sa.CheckConstraint('cantidad_requerida_kg > 0', name='ck_scm_req_mat_prep_cantidad'),
    sa.ForeignKeyConstraint(['corrida_fabricacion_id'], ['scm_corrida_fabricacion.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['receta_revision_id'], ['receta_color_maestra.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('corrida_fabricacion_id', name='uq_scm_req_mat_prep_corrida'),
    sa.UniqueConstraint('operation_id', name='uq_scm_req_mat_prep_operacion')
    )
    op.create_index('ix_scm_req_mat_prep_creador', 'scm_requerimiento_material_preparado', ['created_by_id'], unique=False)
    op.create_index('ix_scm_req_mat_prep_estado_cursor', 'scm_requerimiento_material_preparado', ['estado', 'created_at', 'id'], unique=False)
    op.create_index('ix_scm_req_mat_prep_receta', 'scm_requerimiento_material_preparado', ['receta_revision_id'], unique=False)
    op.create_table('scm_orden_preparacion_material',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('codigo', sa.String(length=64), nullable=False),
    sa.Column('receta_revision_id', sa.Integer(), nullable=False),
    sa.Column('composicion_hash', sa.String(length=64), nullable=False),
    sa.Column('cantidad_objetivo_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('estado', sa.String(length=24), server_default='BORRADOR', nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('perdida_kg', sa.Numeric(precision=15, scale=3), nullable=True),
    sa.Column('muestra_kg', sa.Numeric(precision=15, scale=3), nullable=True),
    sa.Column('remanente_equipo_kg', sa.Numeric(precision=15, scale=3), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('released_by_id', sa.Integer(), nullable=True),
    sa.Column('started_by_id', sa.Integer(), nullable=True),
    sa.Column('closed_by_id', sa.Integer(), nullable=True),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("estado IN ('BORRADOR', 'LIBERADA', 'EN_PREPARACION', 'PENDIENTE_CONCILIACION', 'CERRADA', 'ANULADA')", name='ck_scm_opm_estado'),
    sa.CheckConstraint('cantidad_objetivo_kg > 0', name='ck_scm_opm_cantidad_objetivo'),
    sa.ForeignKeyConstraint(['closed_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['receta_revision_id'], ['receta_color_maestra.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['released_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['started_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('codigo', name='uq_scm_opm_codigo'),
    sa.UniqueConstraint('operation_id', name='uq_scm_opm_operacion')
    )
    op.create_index('ix_scm_opm_cerrador', 'scm_orden_preparacion_material', ['closed_by_id'], unique=False)
    op.create_index('ix_scm_opm_creador', 'scm_orden_preparacion_material', ['created_by_id'], unique=False)
    op.create_index('ix_scm_opm_estado_cursor', 'scm_orden_preparacion_material', ['estado', 'created_at', 'id'], unique=False)
    op.create_index('ix_scm_opm_iniciador', 'scm_orden_preparacion_material', ['started_by_id'], unique=False)
    op.create_index('ix_scm_opm_liberador', 'scm_orden_preparacion_material', ['released_by_id'], unique=False)
    op.create_index('ix_scm_opm_receta', 'scm_orden_preparacion_material', ['receta_revision_id'], unique=False)
    op.create_table('scm_lectura_peso_preparacion',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('orden_preparacion_id', sa.Uuid(), nullable=False),
    sa.Column('asignacion_requerimiento_id', sa.Uuid(), nullable=True),
    sa.Column('tipo_uso', sa.String(length=24), nullable=False),
    sa.Column('peso_bruto_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('tara_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('peso_neto_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('metodo', sa.String(length=32), nullable=False),
    sa.Column('evidencia_ref', sa.String(length=160), nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('estado', sa.String(length=36), server_default='PENDIENTE_SEGUNDA_CONFIRMACION', nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('invalidated_by_id', sa.Integer(), nullable=True),
    sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invalidation_reason', sa.String(length=240), nullable=True),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("estado IN ('PENDIENTE_SEGUNDA_CONFIRMACION', 'APROBADA', 'INVALIDADA', 'UTILIZADA')", name='ck_scm_lectura_prep_estado'),
    sa.CheckConstraint("(tipo_uso = 'APORTE' AND asignacion_requerimiento_id IS NULL) OR tipo_uso = 'BOLSA_SALIDA'", name='ck_scm_lectura_prep_asignacion'),
    sa.CheckConstraint("metodo = 'CONTINGENCIA_MANUAL'", name='ck_scm_lectura_prep_metodo'),
    sa.CheckConstraint("tipo_uso IN ('APORTE', 'BOLSA_SALIDA')", name='ck_scm_lectura_prep_tipo_uso'),
    sa.CheckConstraint('peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND peso_neto_kg = peso_bruto_kg - tara_kg', name='ck_scm_lectura_prep_pesos'),
    sa.ForeignKeyConstraint(['created_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['invalidated_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['orden_preparacion_id'], ['scm_orden_preparacion_material.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('operation_id', name='uq_scm_lectura_prep_operacion')
    )
    op.create_index('ix_scm_lectura_prep_creador', 'scm_lectura_peso_preparacion', ['created_by_id'], unique=False)
    op.create_index('ix_scm_lectura_prep_asignacion', 'scm_lectura_peso_preparacion', ['asignacion_requerimiento_id'], unique=False)
    op.create_index('ix_scm_lectura_prep_estado', 'scm_lectura_peso_preparacion', ['estado', 'created_at', 'id'], unique=False)
    op.create_index('ix_scm_lectura_prep_invalidador', 'scm_lectura_peso_preparacion', ['invalidated_by_id'], unique=False)
    op.create_index('ix_scm_lectura_prep_orden', 'scm_lectura_peso_preparacion', ['orden_preparacion_id'], unique=False)
    op.create_table('scm_aprobacion_lectura_peso_preparacion',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('lectura_id', sa.Uuid(), nullable=False),
    sa.Column('lectura_version', sa.Integer(), nullable=False),
    sa.Column('peso_bruto_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('tara_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('peso_neto_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint('peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND peso_neto_kg = peso_bruto_kg - tara_kg', name='ck_scm_aprob_lect_prep_pesos'),
    sa.ForeignKeyConstraint(['actor_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lectura_id'], ['scm_lectura_peso_preparacion.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('lectura_id', name='uq_scm_aprobacion_lectura_prep'),
    sa.UniqueConstraint('operation_id', name='uq_scm_aprob_lect_prep_operacion')
    )
    op.create_index('ix_scm_aprob_lect_prep_actor', 'scm_aprobacion_lectura_peso_preparacion', ['actor_id'], unique=False)
    op.create_table('scm_aporte_preparacion_material',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('orden_preparacion_id', sa.Uuid(), nullable=False),
    sa.Column('emision_id', sa.Uuid(), nullable=False),
    sa.Column('lectura_id', sa.Uuid(), nullable=False),
    sa.Column('peso_bruto_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('tara_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('peso_neto_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('metodo', sa.String(length=32), nullable=False),
    sa.Column('evidencia_ref', sa.String(length=160), nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('estado', sa.String(length=32), server_default='INCORPORADO', nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('confirmed_by_id', sa.Integer(), nullable=True),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("estado = 'INCORPORADO'", name='ck_scm_aporte_prep_estado'),
    sa.CheckConstraint("metodo = 'CONTINGENCIA_MANUAL'", name='ck_scm_aporte_prep_metodo'),
    sa.CheckConstraint('peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND peso_neto_kg = peso_bruto_kg - tara_kg', name='ck_scm_aporte_prep_pesos'),
    sa.ForeignKeyConstraint(['confirmed_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['emision_id'], ['scm_emision_material.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lectura_id'], ['scm_lectura_peso_preparacion.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['orden_preparacion_id'], ['scm_orden_preparacion_material.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('lectura_id'),
    sa.UniqueConstraint('operation_id', name='uq_scm_aporte_prep_operacion')
    )
    op.create_index('ix_scm_aporte_prep_confirmador', 'scm_aporte_preparacion_material', ['confirmed_by_id'], unique=False)
    op.create_index('ix_scm_aporte_prep_creador', 'scm_aporte_preparacion_material', ['created_by_id'], unique=False)
    op.create_index('ix_scm_aporte_prep_emision', 'scm_aporte_preparacion_material', ['emision_id'], unique=False)
    op.create_index('ix_scm_aporte_prep_lectura', 'scm_aporte_preparacion_material', ['lectura_id'], unique=False)
    op.create_index('ix_scm_aporte_prep_orden', 'scm_aporte_preparacion_material', ['orden_preparacion_id'], unique=False)
    op.create_table('scm_lote_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('codigo', sa.String(length=64), nullable=False),
    sa.Column('orden_preparacion_id', sa.Uuid(), nullable=False),
    sa.Column('receta_revision_id', sa.Integer(), nullable=False),
    sa.Column('cantidad_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('estado', sa.String(length=24), server_default='PENDIENTE_RECEPCION', nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("estado IN ('PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', 'DISPONIBLE', 'BLOQUEADO', 'RECHAZADO', 'AGOTADO')", name='ck_scm_lote_mat_prep_estado'),
    sa.CheckConstraint('cantidad_kg > 0', name='ck_scm_lote_mat_prep_cantidad'),
    sa.ForeignKeyConstraint(['created_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['orden_preparacion_id'], ['scm_orden_preparacion_material.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['receta_revision_id'], ['receta_color_maestra.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('codigo', name='uq_scm_lote_mat_prep_codigo'),
    sa.UniqueConstraint('orden_preparacion_id', name='uq_scm_lote_mat_prep_opm')
    )
    op.create_index('ix_scm_lote_mat_prep_creador', 'scm_lote_material_preparado', ['created_by_id'], unique=False)
    op.create_index('ix_scm_lote_mat_prep_estado_cursor', 'scm_lote_material_preparado', ['estado', 'created_at', 'id'], unique=False)
    op.create_index('ix_scm_lote_mat_prep_receta', 'scm_lote_material_preparado', ['receta_revision_id'], unique=False)
    op.create_table('scm_bolsa_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('codigo', sa.String(length=64), nullable=False),
    sa.Column('orden_preparacion_id', sa.Uuid(), nullable=False),
    sa.Column('lote_id', sa.Uuid(), nullable=True),
    sa.Column('lectura_id', sa.Uuid(), nullable=False),
    sa.Column('asignacion_requerimiento_id', sa.Uuid(), nullable=True),
    sa.Column('secuencia', sa.Integer(), nullable=False),
    sa.Column('peso_bruto_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('tara_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('peso_neto_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('metodo', sa.String(length=32), nullable=False),
    sa.Column('evidencia_ref', sa.String(length=160), nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('estado', sa.String(length=32), server_default='PENDIENTE_RECEPCION', nullable=False),
    sa.Column('ubicacion_id', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('confirmed_by_id', sa.Integer(), nullable=True),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("estado IN ('PENDIENTE_CONFIRMACION', 'PESADA', 'PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', 'DISPONIBLE', 'RESERVADA', 'EMITIDA', 'CONSUMIDA', 'DEVUELTA', 'BLOQUEADA', 'RECHAZADA')", name='ck_scm_bolsa_mat_prep_estado'),
    sa.CheckConstraint("metodo = 'CONTINGENCIA_MANUAL'", name='ck_scm_bolsa_mat_prep_metodo'),
    sa.CheckConstraint('peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND peso_neto_kg = peso_bruto_kg - tara_kg', name='ck_scm_bolsa_mat_prep_pesos'),
    sa.CheckConstraint('secuencia > 0', name='ck_scm_bolsa_mat_prep_secuencia'),
    sa.ForeignKeyConstraint(['confirmed_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lectura_id'], ['scm_lectura_peso_preparacion.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lote_id'], ['scm_lote_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['orden_preparacion_id'], ['scm_orden_preparacion_material.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ubicacion_id'], ['scm_ubicacion_inventario.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('codigo', name='uq_scm_bolsa_mat_prep_codigo'),
    sa.UniqueConstraint('lectura_id'),
    sa.UniqueConstraint('operation_id', name='uq_scm_bolsa_mat_prep_operacion'),
    sa.UniqueConstraint('orden_preparacion_id', 'secuencia', name='uq_scm_bolsa_mat_prep_seq')
    )
    op.create_index('ix_scm_bolsa_mat_prep_confirmador', 'scm_bolsa_material_preparado', ['confirmed_by_id'], unique=False)
    op.create_index('ix_scm_bolsa_mat_prep_creador', 'scm_bolsa_material_preparado', ['created_by_id'], unique=False)
    op.create_index('ix_scm_bolsa_mat_prep_estado', 'scm_bolsa_material_preparado', ['estado', 'created_at', 'id'], unique=False)
    op.create_index('ix_scm_bolsa_mat_prep_lectura', 'scm_bolsa_material_preparado', ['lectura_id'], unique=False)
    op.create_index('ix_scm_bolsa_mat_prep_asignacion', 'scm_bolsa_material_preparado', ['asignacion_requerimiento_id'], unique=False)
    op.create_index('ix_scm_bolsa_mat_prep_lote', 'scm_bolsa_material_preparado', ['lote_id'], unique=False)
    op.create_index('ix_scm_bolsa_mat_prep_orden', 'scm_bolsa_material_preparado', ['orden_preparacion_id'], unique=False)
    op.create_index('ix_scm_bolsa_mat_prep_ubicacion', 'scm_bolsa_material_preparado', ['ubicacion_id'], unique=False)
    op.create_table('scm_decision_calidad_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('lote_id', sa.Uuid(), nullable=False),
    sa.Column('bolsa_id', sa.Uuid(), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("decision IN ('LIBERAR', 'BLOQUEAR', 'RECHAZAR')", name='ck_scm_calidad_mat_prep_decision'),
    sa.ForeignKeyConstraint(['actor_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['bolsa_id'], ['scm_bolsa_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lote_id'], ['scm_lote_material_preparado.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('bolsa_id', name='uq_scm_calidad_mat_prep_bolsa'),
    sa.UniqueConstraint('operation_id', name='uq_scm_calidad_mat_prep_operacion')
    )
    op.create_index('ix_scm_calidad_mat_prep_actor', 'scm_decision_calidad_material_preparado', ['actor_id'], unique=False)
    op.create_index('ix_scm_calidad_mat_prep_bolsa', 'scm_decision_calidad_material_preparado', ['bolsa_id'], unique=False)
    op.create_index('ix_scm_calidad_mat_prep_lote', 'scm_decision_calidad_material_preparado', ['lote_id'], unique=False)
    op.create_table('scm_asignacion_requerimiento_preparacion',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('orden_preparacion_id', sa.Uuid(), nullable=True),
    sa.Column('requerimiento_id', sa.Uuid(), nullable=False),
    sa.Column('tipo_fuente', sa.String(length=32), nullable=False),
    sa.Column('lote_id', sa.Uuid(), nullable=True),
    sa.Column('bolsa_id', sa.Uuid(), nullable=True),
    sa.Column('cantidad_planificada_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('cantidad_comprometida_kg', sa.Numeric(precision=15, scale=3), server_default='0', nullable=False),
    sa.Column('cantidad_consumida_kg', sa.Numeric(precision=15, scale=3), server_default='0', nullable=False),
    sa.Column('estado', sa.String(length=16), server_default='PLANIFICADA', nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('released_by_id', sa.Integer(), nullable=True),
    sa.Column('motivo_liberacion', sa.String(length=240), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("(tipo_fuente = 'OPM_ESPERADA' AND orden_preparacion_id IS NOT NULL AND lote_id IS NULL AND bolsa_id IS NULL) OR (tipo_fuente = 'LOTE_PREPARADO_STOCK' AND orden_preparacion_id IS NULL AND lote_id IS NOT NULL AND bolsa_id IS NOT NULL)", name='ck_scm_asig_req_prep_fuente'),
    sa.CheckConstraint("estado IN ('PLANIFICADA', 'COMPROMETIDA', 'SATISFECHA', 'LIBERADA', 'CANCELADA')", name='ck_scm_asig_req_prep_estado'),
    sa.CheckConstraint("tipo_fuente IN ('LOTE_PREPARADO_STOCK', 'OPM_ESPERADA')", name='ck_scm_asig_req_prep_tipo_fuente'),
    sa.CheckConstraint('cantidad_planificada_kg > 0 AND cantidad_comprometida_kg >= 0 AND cantidad_consumida_kg >= 0 AND cantidad_consumida_kg <= cantidad_comprometida_kg AND cantidad_comprometida_kg <= cantidad_planificada_kg', name='ck_scm_asig_req_prep_cantidades'),
    sa.ForeignKeyConstraint(['bolsa_id'], ['scm_bolsa_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lote_id'], ['scm_lote_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['orden_preparacion_id'], ['scm_orden_preparacion_material.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['released_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requerimiento_id'], ['scm_requerimiento_material_preparado.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('orden_preparacion_id', 'requerimiento_id', name='uq_scm_asig_req_prep_opm_req')
    )
    op.create_index('ix_scm_asig_req_prep_bolsa', 'scm_asignacion_requerimiento_preparacion', ['bolsa_id'], unique=False)
    op.create_index('ix_scm_asig_req_prep_creador', 'scm_asignacion_requerimiento_preparacion', ['created_by_id'], unique=False)
    op.create_index('ix_scm_asig_req_prep_liberador', 'scm_asignacion_requerimiento_preparacion', ['released_by_id'], unique=False)
    op.create_index('ix_scm_asig_req_prep_lote', 'scm_asignacion_requerimiento_preparacion', ['lote_id'], unique=False)
    op.create_index('ix_scm_asig_req_prep_opm', 'scm_asignacion_requerimiento_preparacion', ['orden_preparacion_id'], unique=False)
    op.create_index('ix_scm_asig_req_prep_req', 'scm_asignacion_requerimiento_preparacion', ['requerimiento_id'], unique=False)
    op.create_index('uq_scm_asig_req_prep_bolsa_activa', 'scm_asignacion_requerimiento_preparacion', ['bolsa_id'], unique=True, postgresql_where=sa.text("tipo_fuente = 'LOTE_PREPARADO_STOCK' AND estado IN ('PLANIFICADA', 'COMPROMETIDA')"), sqlite_where=sa.text("tipo_fuente = 'LOTE_PREPARADO_STOCK' AND estado IN ('PLANIFICADA', 'COMPROMETIDA')"))
    with op.batch_alter_table('scm_bolsa_material_preparado') as batch_op:
        batch_op.create_foreign_key(
            'fk_scm_bolsa_mat_prep_asignacion',
            'scm_asignacion_requerimiento_preparacion',
            ['asignacion_requerimiento_id'],
            ['id'],
            ondelete='RESTRICT',
        )
    with op.batch_alter_table('scm_lectura_peso_preparacion') as batch_op:
        batch_op.create_foreign_key(
            'fk_scm_lectura_prep_asignacion',
            'scm_asignacion_requerimiento_preparacion',
            ['asignacion_requerimiento_id'],
            ['id'],
            ondelete='RESTRICT',
        )
    op.create_table('scm_reserva_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('bolsa_id', sa.Uuid(), nullable=False),
    sa.Column('asignacion_id', sa.Uuid(), nullable=False),
    sa.Column('trabajo_ot_id', sa.Uuid(), nullable=False),
    sa.Column('requerimiento_id', sa.Uuid(), nullable=False),
    sa.Column('ubicacion_origen_id', sa.Integer(), nullable=False),
    sa.Column('cantidad_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('estado', sa.String(length=16), server_default='ACTIVA', nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("estado IN ('ACTIVA', 'CONSUMIDA', 'DEVUELTA', 'LIBERADA', 'CANCELADA')", name='ck_scm_reserva_mat_prep_estado'),
    sa.CheckConstraint('cantidad_kg > 0', name='ck_scm_reserva_mat_prep_cantidad'),
    sa.ForeignKeyConstraint(['asignacion_id'], ['scm_asignacion_requerimiento_preparacion.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['bolsa_id'], ['scm_bolsa_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requerimiento_id'], ['scm_requerimiento_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['trabajo_ot_id'], ['scm_trabajo_ot.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ubicacion_origen_id'], ['scm_ubicacion_inventario.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('operation_id', name='uq_scm_reserva_mat_prep_operacion')
    )
    op.create_index('ix_scm_reserva_mat_prep_actor', 'scm_reserva_material_preparado', ['created_by_id'], unique=False)
    op.create_index('ix_scm_reserva_mat_prep_asignacion', 'scm_reserva_material_preparado', ['asignacion_id'], unique=False)
    op.create_index('ix_scm_reserva_mat_prep_bolsa', 'scm_reserva_material_preparado', ['bolsa_id'], unique=False)
    op.create_index('ix_scm_reserva_mat_prep_cursor', 'scm_reserva_material_preparado', ['created_at', 'id'], unique=False)
    op.create_index('ix_scm_reserva_mat_prep_origen', 'scm_reserva_material_preparado', ['ubicacion_origen_id'], unique=False)
    op.create_index('ix_scm_reserva_mat_prep_req', 'scm_reserva_material_preparado', ['requerimiento_id'], unique=False)
    op.create_index('ix_scm_reserva_mat_prep_trabajo', 'scm_reserva_material_preparado', ['trabajo_ot_id'], unique=False)
    op.create_index('uq_scm_reserva_mat_prep_activa', 'scm_reserva_material_preparado', ['bolsa_id'], unique=True, postgresql_where=sa.text("estado = 'ACTIVA'"), sqlite_where=sa.text("estado = 'ACTIVA'"))
    op.create_table('scm_emision_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('reserva_id', sa.Uuid(), nullable=False),
    sa.Column('ubicacion_destino_id', sa.Integer(), nullable=False),
    sa.Column('ubicacion_retorno_id', sa.Integer(), nullable=True),
    sa.Column('estado', sa.String(length=24), server_default='PREPARADA', nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=False),
    sa.Column('dispatched_by_id', sa.Integer(), nullable=True),
    sa.Column('received_by_id', sa.Integer(), nullable=True),
    sa.Column('returned_by_id', sa.Integer(), nullable=True),
    sa.Column('closed_by_id', sa.Integer(), nullable=True),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('returned_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("estado IN ('PREPARADA', 'EN_TRANSITO', 'RECIBIDA_MAQUINA', 'CERRADA', 'RETORNADA_TOTAL', 'CANCELADA')", name='ck_scm_emision_mat_prep_estado'),
    sa.ForeignKeyConstraint(['actor_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['closed_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['dispatched_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['received_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['reserva_id'], ['scm_reserva_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['returned_by_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ubicacion_destino_id'], ['scm_ubicacion_inventario.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ubicacion_retorno_id'], ['scm_ubicacion_inventario.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('operation_id', name='uq_scm_emision_mat_prep_operacion'),
    sa.UniqueConstraint('reserva_id', name='uq_scm_emision_mat_prep_reserva')
    )
    op.create_index('ix_scm_emision_mat_prep_actor', 'scm_emision_material_preparado', ['actor_id'], unique=False)
    op.create_index('ix_scm_emision_mat_prep_cerrador', 'scm_emision_material_preparado', ['closed_by_id'], unique=False)
    op.create_index('ix_scm_emision_mat_prep_cursor', 'scm_emision_material_preparado', ['created_at', 'id'], unique=False)
    op.create_index('ix_scm_emision_mat_prep_despachador', 'scm_emision_material_preparado', ['dispatched_by_id'], unique=False)
    op.create_index('ix_scm_emision_mat_prep_destino', 'scm_emision_material_preparado', ['ubicacion_destino_id'], unique=False)
    op.create_index('ix_scm_emision_mat_prep_receptor', 'scm_emision_material_preparado', ['received_by_id'], unique=False)
    op.create_index('ix_scm_emision_mat_prep_retornador', 'scm_emision_material_preparado', ['returned_by_id'], unique=False)
    op.create_index('ix_scm_emision_mat_prep_retorno', 'scm_emision_material_preparado', ['ubicacion_retorno_id'], unique=False)
    op.create_table('scm_recepcion_bolsa_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('bolsa_id', sa.Uuid(), nullable=False),
    sa.Column('ubicacion_id', sa.Integer(), nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['bolsa_id'], ['scm_bolsa_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ubicacion_id'], ['scm_ubicacion_inventario.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('bolsa_id', name='uq_scm_recepcion_bolsa_mat_prep'),
    sa.UniqueConstraint('operation_id', name='uq_scm_recepcion_mat_prep_operacion')
    )
    op.create_index('ix_scm_recepcion_mat_prep_actor', 'scm_recepcion_bolsa_material_preparado', ['actor_id'], unique=False)
    op.create_index('ix_scm_recepcion_mat_prep_ubicacion', 'scm_recepcion_bolsa_material_preparado', ['ubicacion_id'], unique=False)
    op.create_table('scm_saldo_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('receta_revision_id', sa.Integer(), nullable=False),
    sa.Column('ubicacion_id', sa.Integer(), nullable=False),
    sa.Column('cantidad_fisica_kg', sa.Numeric(precision=15, scale=3), server_default='0', nullable=False),
    sa.Column('cantidad_reservada_kg', sa.Numeric(precision=15, scale=3), server_default='0', nullable=False),
    sa.Column('cantidad_no_disponible_kg', sa.Numeric(precision=15, scale=3), server_default='0', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint('cantidad_fisica_kg >= 0 AND cantidad_reservada_kg >= 0 AND cantidad_no_disponible_kg >= 0 AND cantidad_reservada_kg + cantidad_no_disponible_kg <= cantidad_fisica_kg', name='ck_scm_saldo_mat_prep_cantidades'),
    sa.ForeignKeyConstraint(['receta_revision_id'], ['receta_color_maestra.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ubicacion_id'], ['scm_ubicacion_inventario.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('receta_revision_id', 'ubicacion_id', name='uq_scm_saldo_mat_prep_receta_ubicacion')
    )
    op.create_index('ix_scm_saldo_mat_prep_actualizado', 'scm_saldo_material_preparado', ['updated_at', 'id'], unique=False)
    op.create_index('ix_scm_saldo_mat_prep_receta', 'scm_saldo_material_preparado', ['receta_revision_id'], unique=False)
    op.create_index('ix_scm_saldo_mat_prep_ubicacion', 'scm_saldo_material_preparado', ['ubicacion_id'], unique=False)
    op.create_table('scm_movimiento_material_preparado',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('saldo_id', sa.Uuid(), nullable=False),
    sa.Column('bolsa_id', sa.Uuid(), nullable=False),
    sa.Column('tipo', sa.String(length=24), nullable=False),
    sa.Column('delta_fisico_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('delta_reservado_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('delta_no_disponible_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('saldo_fisico_resultante_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('saldo_reservado_resultante_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('saldo_no_disponible_resultante_kg', sa.Numeric(precision=15, scale=3), nullable=False),
    sa.Column('motivo', sa.String(length=240), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("tipo IN ('RECEPCION', 'LIBERACION_CALIDAD', 'BLOQUEO_CALIDAD', 'RESERVA', 'LIBERACION_RESERVA', 'EMISION_SALIDA', 'EMISION_ENTRADA', 'CONSUMO', 'RETORNO_SALIDA', 'RETORNO_ENTRADA')", name='ck_scm_mov_mat_prep_tipo'),
    sa.CheckConstraint('delta_fisico_kg <> 0 OR delta_reservado_kg <> 0 OR delta_no_disponible_kg <> 0', name='ck_scm_mov_mat_prep_delta'),
    sa.CheckConstraint('saldo_fisico_resultante_kg >= 0 AND saldo_reservado_resultante_kg >= 0 AND saldo_no_disponible_resultante_kg >= 0', name='ck_scm_mov_mat_prep_resultado'),
    sa.ForeignKeyConstraint(['actor_id'], ['trabajador.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['bolsa_id'], ['scm_bolsa_material_preparado.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['saldo_id'], ['scm_saldo_material_preparado.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('operation_id', name='uq_scm_mov_mat_prep_operacion')
    )
    op.create_index('ix_scm_mov_mat_prep_actor', 'scm_movimiento_material_preparado', ['actor_id'], unique=False)
    op.create_index('ix_scm_mov_mat_prep_bolsa', 'scm_movimiento_material_preparado', ['bolsa_id'], unique=False)
    op.create_index('ix_scm_mov_mat_prep_cursor', 'scm_movimiento_material_preparado', ['created_at', 'id'], unique=False)
    op.create_index('ix_scm_mov_mat_prep_saldo', 'scm_movimiento_material_preparado', ['saldo_id'], unique=False)
    with op.batch_alter_table("scm_requerimiento_material") as batch_op:
        batch_op.add_column(sa.Column(
            "orden_preparacion_material_id", sa.Uuid(), nullable=True,
        ))
        batch_op.alter_column(
            "corrida_fabricacion_id", existing_type=sa.Uuid(),
            nullable=True,
        )
        batch_op.create_foreign_key(
            "fk_scm_req_material_opm",
            "scm_orden_preparacion_material",
            ["orden_preparacion_material_id"], ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_scm_req_material_opm_material",
            ["orden_preparacion_material_id", "material_id"],
        )
        batch_op.create_check_constraint(
            "ck_scm_req_material_origen",
            "(corrida_fabricacion_id IS NOT NULL AND "
            "orden_preparacion_material_id IS NULL) OR "
            "(corrida_fabricacion_id IS NULL AND "
            "orden_preparacion_material_id IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_scm_req_material_opm",
            ["orden_preparacion_material_id"],
        )
    _seed_authorization(op.get_bind())
    _protect_tables_on_postgres(op.get_bind())


def downgrade():
    connection = op.get_bind()
    populated_tables = []
    for table_name in NEW_TABLES:
        # NEW_TABLES is a migration-owned constant, never user input.
        row_count = connection.execute(sa.text(
            f"SELECT COUNT(*) FROM {table_name}"
        )).scalar_one()
        if row_count:
            populated_tables.append(table_name)
    opm_raw_requirements = connection.execute(sa.text("""
        SELECT COUNT(*)
        FROM scm_requerimiento_material
        WHERE orden_preparacion_material_id IS NOT NULL
    """)).scalar_one()
    if populated_tables or opm_raw_requirements:
        raise RuntimeError(
            "OPM_DATA_REQUIRES_EXPLICIT_ROLLBACK: existen datos OPM/LMP; "
            "migralos o retiralos de forma auditada antes de degradar el "
            f"esquema. tablas={','.join(populated_tables) or '(raw-inputs)'}"
        )
    _remove_authorization(op.get_bind())
    with op.batch_alter_table("scm_requerimiento_material") as batch_op:
        batch_op.drop_index("ix_scm_req_material_opm")
        batch_op.drop_constraint(
            "ck_scm_req_material_origen", type_="check",
        )
        batch_op.drop_constraint(
            "uq_scm_req_material_opm_material", type_="unique",
        )
        batch_op.drop_constraint("fk_scm_req_material_opm", type_="foreignkey")
        batch_op.alter_column(
            "corrida_fabricacion_id", existing_type=sa.Uuid(),
            nullable=False,
        )
        batch_op.drop_column("orden_preparacion_material_id")
    op.drop_table("scm_movimiento_material_preparado")
    op.drop_table("scm_saldo_material_preparado")
    op.drop_table("scm_recepcion_bolsa_material_preparado")
    op.drop_table("scm_emision_material_preparado")
    op.drop_table("scm_reserva_material_preparado")
    with op.batch_alter_table("scm_bolsa_material_preparado") as batch_op:
        batch_op.drop_constraint(
            "fk_scm_bolsa_mat_prep_asignacion", type_="foreignkey"
        )
    with op.batch_alter_table("scm_lectura_peso_preparacion") as batch_op:
        batch_op.drop_constraint(
            "fk_scm_lectura_prep_asignacion", type_="foreignkey"
        )
    op.drop_table("scm_asignacion_requerimiento_preparacion")
    op.drop_table("scm_decision_calidad_material_preparado")
    op.drop_table("scm_bolsa_material_preparado")
    op.drop_table("scm_lote_material_preparado")
    op.drop_table("scm_aporte_preparacion_material")
    op.drop_table("scm_aprobacion_lectura_peso_preparacion")
    op.drop_table("scm_lectura_peso_preparacion")
    op.drop_table("scm_orden_preparacion_material")
    op.drop_table("scm_requerimiento_material_preparado")
