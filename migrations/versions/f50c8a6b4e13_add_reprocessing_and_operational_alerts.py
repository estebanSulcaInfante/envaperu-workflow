"""add reprocessing ledger and operational alerts

Revision ID: f50c8a6b4e13
Revises: f48b1d9e3a42
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f50c8a6b4e13"
down_revision = "f48b1d9e3a42"
branch_labels = None
depends_on = None


def _timestamps(updated=False):
    columns = [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)]
    if updated:
        columns.append(sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    return columns


def upgrade():
    op.create_table(
        "scm_familia_material_reproceso",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("descripcion", sa.Text()),
        sa.Column("activo", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(updated=True),
        sa.CheckConstraint("version > 0", name="ck_scm_familia_material_rep_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_familia_material_rep_codigo"),
    )
    op.create_table(
        "scm_proceso_material_reproceso",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("descripcion", sa.Text()),
        sa.Column("activo", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(updated=True),
        sa.CheckConstraint("version > 0", name="ck_scm_proceso_material_rep_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_proceso_material_rep_codigo"),
    )
    op.create_table(
        "scm_condicion_merma",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("recuperable", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("descripcion", sa.Text()),
        sa.Column("activo", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(updated=True),
        sa.CheckConstraint("version > 0", name="ck_scm_condicion_merma_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_condicion_merma_codigo"),
    )
    op.create_table(
        "scm_regla_compatibilidad_reproceso",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("nombre", sa.String(160), nullable=False),
        sa.Column("estado", sa.String(20), server_default="BORRADOR", nullable=False),
        sa.Column("familia_objetivo_id", sa.Integer(), nullable=False),
        sa.Column("proceso_objetivo_id", sa.Integer(), nullable=False),
        sa.Column("familia_aporte_id", sa.Integer(), nullable=False),
        sa.Column("proceso_aporte_id", sa.Integer(), nullable=False),
        sa.Column("color_objetivo_id", sa.Integer()),
        sa.Column("familia_color_objetivo_id", sa.Integer()),
        sa.Column("color_aporte_id", sa.Integer()),
        sa.Column("familia_color_aporte_id", sa.Integer()),
        sa.Column("resultado", sa.String(20), nullable=False),
        sa.Column("porcentaje_maximo", sa.Numeric(5, 2)),
        sa.Column("simetrica", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("notas", sa.Text()),
        sa.Column("creado_por_id", sa.Integer(), nullable=False),
        sa.Column("aprobado_por_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("revision > 0", name="ck_scm_regla_rep_revision"),
        sa.CheckConstraint("estado IN ('BORRADOR','APROBADA','RETIRADA')", name="ck_scm_regla_rep_estado"),
        sa.CheckConstraint("resultado IN ('COMPATIBLE','CONDICIONADA','INCOMPATIBLE')", name="ck_scm_regla_rep_resultado"),
        sa.CheckConstraint("porcentaje_maximo IS NULL OR (porcentaje_maximo > 0 AND porcentaje_maximo <= 100)", name="ck_scm_regla_rep_porcentaje"),
        sa.ForeignKeyConstraint(["familia_objetivo_id"], ["scm_familia_material_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["familia_aporte_id"], ["scm_familia_material_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["proceso_objetivo_id"], ["scm_proceso_material_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["proceso_aporte_id"], ["scm_proceso_material_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["color_objetivo_id"], ["color_base.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["color_aporte_id"], ["color_base.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["familia_color_objetivo_id"], ["familia_color.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["familia_color_aporte_id"], ["familia_color.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["creado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["aprobado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", "revision", name="uq_scm_regla_rep_codigo_revision"),
    )
    op.create_table(
        "scm_lote_merma_recuperable",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column("familia_material_id", sa.Integer(), nullable=False),
        sa.Column("proceso_origen_id", sa.Integer(), nullable=False),
        sa.Column("condicion_id", sa.Integer(), nullable=False),
        sa.Column("color_id", sa.Integer(), nullable=False),
        sa.Column("familia_color_id", sa.Integer()),
        sa.Column("material_id", sa.Integer()),
        sa.Column("ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("origen_tipo", sa.String(40), nullable=False),
        sa.Column("origen_id", sa.String(64), nullable=False),
        sa.Column("estado", sa.String(20), server_default="ALMACENADA", nullable=False),
        sa.Column("peso_bruto_almacen_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("tara_kg", sa.Numeric(15, 3), server_default="0", nullable=False),
        sa.Column("peso_neto_almacen_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("saldo_disponible_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("saldo_reservado_kg", sa.Numeric(15, 3), server_default="0", nullable=False),
        sa.Column("observaciones", sa.Text()),
        sa.Column("pesado_por_id", sa.Integer(), nullable=False),
        sa.Column("pesado_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(updated=True),
        sa.CheckConstraint("estado IN ('ALMACENADA','RESERVADA','CONSUMIDA','BLOQUEADA','ANULADA')", name="ck_scm_lote_merma_estado"),
        sa.CheckConstraint("peso_neto_almacen_kg > 0", name="ck_scm_lote_merma_peso"),
        sa.CheckConstraint("saldo_disponible_kg >= 0 AND saldo_reservado_kg >= 0 AND saldo_reservado_kg <= saldo_disponible_kg", name="ck_scm_lote_merma_saldo"),
        sa.CheckConstraint("version > 0", name="ck_scm_lote_merma_version"),
        sa.ForeignKeyConstraint(["familia_material_id"], ["scm_familia_material_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["proceso_origen_id"], ["scm_proceso_material_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["condicion_id"], ["scm_condicion_merma.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["color_id"], ["color_base.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["familia_color_id"], ["familia_color.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["material_id"], ["scm_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pesado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_lote_merma_codigo"),
    )
    op.create_table(
        "scm_orden_molienda",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column("estado", sa.String(32), server_default="BORRADOR", nullable=False),
        sa.Column("familia_objetivo_id", sa.Integer(), nullable=False),
        sa.Column("proceso_objetivo_id", sa.Integer(), nullable=False),
        sa.Column("color_objetivo_id", sa.Integer(), nullable=False),
        sa.Column("familia_color_objetivo_id", sa.Integer()),
        sa.Column("material_salida_id", sa.Integer(), nullable=False),
        sa.Column("tolerancia_custodia_kg", sa.Numeric(15, 3), server_default="1", nullable=False),
        sa.Column("tolerancia_balance_kg", sa.Numeric(15, 3)),
        sa.Column("entrada_real_kg", sa.Numeric(15, 3)),
        sa.Column("salida_real_kg", sa.Numeric(15, 3)),
        sa.Column("perdida_real_kg", sa.Numeric(15, 3)),
        sa.Column("diferencia_balance_kg", sa.Numeric(15, 3)),
        sa.Column("mezcla_excepcional", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("excepcion_motivo", sa.Text()),
        sa.Column("excepcion_aprobada_por_id", sa.Integer()),
        sa.Column("creado_por_id", sa.Integer(), nullable=False),
        sa.Column("ejecutado_por_id", sa.Integer()),
        sa.Column("cerrado_por_id", sa.Integer()),
        sa.Column("notas", sa.Text()),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("estado IN ('BORRADOR','VALIDADA','BLOQUEADA_COMPATIBILIDAD','EN_EJECUCION','CERRADA','ANULADA')", name="ck_scm_orden_molienda_estado"),
        sa.CheckConstraint("version > 0", name="ck_scm_orden_molienda_version"),
        sa.ForeignKeyConstraint(["familia_objetivo_id"], ["scm_familia_material_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["proceso_objetivo_id"], ["scm_proceso_material_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["color_objetivo_id"], ["color_base.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["familia_color_objetivo_id"], ["familia_color.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["material_salida_id"], ["scm_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["excepcion_aprobada_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["creado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ejecutado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cerrado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_orden_molienda_codigo"),
    )
    op.create_table(
        "scm_orden_molienda_aporte",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("orden_id", sa.Uuid(), nullable=False),
        sa.Column("lote_merma_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_planificada_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("peso_pre_molino_kg", sa.Numeric(15, 3)),
        sa.Column("diferencia_custodia_kg", sa.Numeric(15, 3)),
        sa.Column("porcentaje_real", sa.Numeric(7, 4)),
        sa.Column("resultado_compatibilidad", sa.String(20)),
        sa.Column("regla_revision_id", sa.Integer()),
        sa.Column("regla_snapshot", sa.JSON()),
        sa.Column("excede_tolerancia", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("motivo_diferencia", sa.Text()),
        sa.Column("autorizado_por_id", sa.Integer()),
        sa.Column("pesado_por_id", sa.Integer()),
        sa.Column("pesado_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint("cantidad_planificada_kg > 0", name="ck_scm_aporte_planificado"),
        sa.CheckConstraint("peso_pre_molino_kg IS NULL OR peso_pre_molino_kg > 0", name="ck_scm_aporte_pre_molino"),
        sa.ForeignKeyConstraint(["orden_id"], ["scm_orden_molienda.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lote_merma_id"], ["scm_lote_merma_recuperable.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["regla_revision_id"], ["scm_regla_compatibilidad_reproceso.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["autorizado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pesado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("orden_id", "lote_merma_id", name="uq_scm_orden_molienda_aporte_lote"),
    )
    op.create_table(
        "scm_movimiento_merma",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lote_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("cantidad_delta_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("saldo_resultante_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("referencia_tipo", sa.String(40)),
        sa.Column("referencia_id", sa.String(64)),
        sa.Column("motivo", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("tipo IN ('INGRESO_ALMACEN','RESERVA_MOLIENDA','LIBERACION_RESERVA','CONSUMO_MOLIENDA','AJUSTE')", name="ck_scm_movimiento_merma_tipo"),
        sa.ForeignKeyConstraint(["lote_id"], ["scm_lote_merma_recuperable.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "scm_lote_material_recuperado",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column("orden_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(30), server_default="PENDIENTE_LIBERACION", nullable=False),
        sa.Column("peso_neto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("saldo_disponible_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("composicion_snapshot", sa.JSON(), nullable=False),
        sa.Column("mezcla_excepcional", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("producido_por_id", sa.Integer(), nullable=False),
        sa.Column("liberado_por_id", sa.Integer()),
        sa.Column("producido_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("liberado_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("estado IN ('PENDIENTE_LIBERACION','DISPONIBLE','BLOQUEADO','ANULADO')", name="ck_scm_lote_material_recuperado_estado"),
        sa.CheckConstraint("peso_neto_kg > 0", name="ck_scm_lote_material_recuperado_peso"),
        sa.ForeignKeyConstraint(["orden_id"], ["scm_orden_molienda.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["material_id"], ["scm_material.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["producido_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["liberado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_lote_material_recuperado_codigo"),
    )
    op.create_table(
        "scm_regla_alerta",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(64), nullable=False),
        sa.Column("nombre", sa.String(160), nullable=False),
        sa.Column("descripcion", sa.Text()),
        sa.Column("activo", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_regla_alerta_codigo"),
    )
    op.create_table(
        "scm_regla_alerta_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("regla_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(20), server_default="BORRADOR", nullable=False),
        sa.Column("umbral", sa.Numeric(15, 3), nullable=False),
        sa.Column("unidad", sa.String(24), nullable=False),
        sa.Column("severidad", sa.String(20), nullable=False),
        sa.Column("alcance", sa.String(40), server_default="PRODUCCION", nullable=False),
        sa.Column("creado_por_id", sa.Integer(), nullable=False),
        sa.Column("aprobado_por_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("estado IN ('BORRADOR','APROBADA','RETIRADA')", name="ck_scm_regla_alerta_revision_estado"),
        sa.CheckConstraint("severidad IN ('INFO','ADVERTENCIA','CRITICA')", name="ck_scm_regla_alerta_revision_severidad"),
        sa.CheckConstraint("unidad IN ('HORAS','DIAS_CALENDARIO','KG','PORCENTAJE')", name="ck_scm_regla_alerta_revision_unidad"),
        sa.CheckConstraint("umbral > 0", name="ck_scm_regla_alerta_revision_umbral"),
        sa.ForeignKeyConstraint(["regla_id"], ["scm_regla_alerta.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["creado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["aprobado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("regla_id", "revision", name="uq_scm_regla_alerta_revision"),
    )
    op.create_table(
        "scm_alerta_operativa",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("regla_revision_id", sa.Integer(), nullable=False),
        sa.Column("huella", sa.String(64), nullable=False),
        sa.Column("tipo", sa.String(64), nullable=False),
        sa.Column("agregado_tipo", sa.String(64), nullable=False),
        sa.Column("agregado_id", sa.String(64), nullable=False),
        sa.Column("estado", sa.String(20), server_default="ABIERTA", nullable=False),
        sa.Column("severidad", sa.String(20), nullable=False),
        sa.Column("resumen", sa.String(240), nullable=False),
        sa.Column("detalle", sa.JSON(), nullable=False),
        sa.Column("asignada_a_id", sa.Integer()),
        sa.Column("detectada_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reconocida_at", sa.DateTime(timezone=True)),
        sa.Column("cerrada_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("estado IN ('ABIERTA','RECONOCIDA','RESUELTA','DESCARTADA')", name="ck_scm_alerta_operativa_estado"),
        sa.CheckConstraint("severidad IN ('INFO','ADVERTENCIA','CRITICA')", name="ck_scm_alerta_operativa_severidad"),
        sa.ForeignKeyConstraint(["regla_revision_id"], ["scm_regla_alerta_revision.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asignada_a_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("huella", name="uq_scm_alerta_operativa_huella"),
    )
    op.create_table(
        "scm_alerta_evento",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alerta_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("motivo", sa.Text()),
        sa.Column("detalle", sa.JSON()),
        *_timestamps(),
        sa.CheckConstraint("tipo IN ('CREADA','RECONOCIDA','ASIGNADA','RESUELTA','DESCARTADA')", name="ck_scm_alerta_evento_tipo"),
        sa.ForeignKeyConstraint(["alerta_id"], ["scm_alerta_operativa.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    capabilities = (
        ("MERMA_RECUPERABLE_REGISTRAR", "Registrar y pesar merma recuperable"),
        ("MOLIENDA_VER", "Consultar merma, molienda y genealogia"),
        ("MOLIENDA_ORDEN_CREAR", "Crear y preparar ordenes de molienda"),
        ("MOLIENDA_EJECUTAR", "Pesar, ejecutar y cerrar molienda"),
        ("MOLIENDA_REGLA_ADMINISTRAR", "Administrar maestros y reglas de reproceso"),
        ("MOLIENDA_REGLA_APROBAR", "Aprobar reglas de compatibilidad"),
        ("MOLIENDA_EXCEPCION_APROBAR", "Autorizar excepciones de molienda"),
        ("MOLIENDA_LOTE_LIBERAR", "Liberar material recuperado"),
        ("MOLIENDA_ANULAR", "Anular ordenes y lotes de molienda"),
        ("ALERTA_VER", "Consultar alertas operativas"),
        ("ALERTA_GESTIONAR", "Reconocer, asignar y cerrar alertas"),
        ("ALERTA_CONFIGURAR", "Administrar reglas y umbrales de alerta"),
    )
    for code, name in capabilities:
        op.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
             WHERE NOT EXISTS (SELECT 1 FROM scm_capacidad WHERE codigo = :code)
        """).bindparams(code=code, name=name))
    op.execute(sa.text("""
        INSERT INTO rol_operativo (codigo, nombre, activo)
        SELECT 'OPERADOR_MOLINO', 'Operador de Molino', true
         WHERE NOT EXISTS (SELECT 1 FROM rol_operativo WHERE codigo = 'OPERADOR_MOLINO')
    """))
    role_capabilities = {
        "ALMACEN_RECEPCION": ("MERMA_RECUPERABLE_REGISTRAR", "MOLIENDA_VER", "ALERTA_VER"),
        "OPERADOR_MOLINO": ("MOLIENDA_VER", "MOLIENDA_ORDEN_CREAR", "MOLIENDA_EJECUTAR", "ALERTA_VER"),
        "SUPERVISOR": ("MOLIENDA_VER", "MOLIENDA_ORDEN_CREAR", "ALERTA_VER"),
        "CONFIGURACION_SCM": ("MOLIENDA_VER", "MOLIENDA_REGLA_ADMINISTRAR", "ALERTA_VER", "ALERTA_CONFIGURAR"),
        "INGENIERIA_SCM": ("MOLIENDA_VER", "MOLIENDA_REGLA_ADMINISTRAR", "ALERTA_VER", "ALERTA_CONFIGURAR"),
        "GERENCIA": ("MOLIENDA_VER", "ALERTA_VER"),
        "AUDITORIA_CONSULTA": ("MOLIENDA_VER", "ALERTA_VER"),
        "JEFE_PRODUCCION": tuple(code for code, _ in capabilities),
    }
    for role_code, capability_codes in role_capabilities.items():
        for capability_code in capability_codes:
            op.execute(sa.text("""
                INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
                SELECT role.id, capability.id
                  FROM rol_operativo role
                  JOIN scm_capacidad capability ON capability.codigo = :capability_code
                 WHERE role.codigo = :role_code
                   AND NOT EXISTS (
                       SELECT 1 FROM scm_rol_capacidad relation
                        WHERE relation.rol_operativo_id = role.id
                          AND relation.capacidad_id = capability.id
                   )
            """).bindparams(role_code=role_code, capability_code=capability_code))


def downgrade():
    for table in (
        "scm_alerta_evento",
        "scm_alerta_operativa",
        "scm_regla_alerta_revision",
        "scm_regla_alerta",
        "scm_lote_material_recuperado",
        "scm_movimiento_merma",
        "scm_orden_molienda_aporte",
        "scm_orden_molienda",
        "scm_lote_merma_recuperable",
        "scm_regla_compatibilidad_reproceso",
        "scm_condicion_merma",
        "scm_proceso_material_reproceso",
        "scm_familia_material_reproceso",
    ):
        op.drop_table(table)
    codes = (
        "MERMA_RECUPERABLE_REGISTRAR", "MOLIENDA_VER", "MOLIENDA_ORDEN_CREAR",
        "MOLIENDA_EJECUTAR", "MOLIENDA_REGLA_ADMINISTRAR", "MOLIENDA_REGLA_APROBAR",
        "MOLIENDA_EXCEPCION_APROBAR", "MOLIENDA_LOTE_LIBERAR", "MOLIENDA_ANULAR",
        "ALERTA_VER", "ALERTA_GESTIONAR", "ALERTA_CONFIGURAR",
    )
    quoted = ",".join(f"'{code}'" for code in codes)
    op.execute(sa.text(f"DELETE FROM scm_rol_capacidad WHERE capacidad_id IN (SELECT id FROM scm_capacidad WHERE codigo IN ({quoted}))"))
    op.execute(sa.text(f"DELETE FROM scm_capacidad WHERE codigo IN ({quoted})"))
    op.execute(sa.text("DELETE FROM rol_operativo WHERE codigo = 'OPERADOR_MOLINO' AND NOT EXISTS (SELECT 1 FROM trabajador_rol WHERE rol_operativo_id = rol_operativo.id)"))
