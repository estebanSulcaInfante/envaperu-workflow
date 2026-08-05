"""add internal supply picking and assembly daily work orders

Revision ID: f52e0b8d7c35
Revises: f51d9a7c6b24
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f52e0b8d7c35"
down_revision = "f51d9a7c6b24"
branch_labels = None
depends_on = None


def _capability(code, name):
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT :code, :name, true
         WHERE NOT EXISTS (SELECT 1 FROM scm_capacidad WHERE codigo = :code)
    """).bindparams(code=code, name=name))


def _assign(role, capability):
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
          FROM rol_operativo rol
          JOIN scm_capacidad capacidad ON capacidad.codigo = :capability
         WHERE rol.codigo = :role
           AND NOT EXISTS (
             SELECT 1 FROM scm_rol_capacidad relation
              WHERE relation.rol_operativo_id = rol.id
                AND relation.capacidad_id = capacidad.id
           )
    """).bindparams(role=role, capability=capability))


def upgrade():
    op.add_column(
        "registro_diario_produccion",
        sa.Column("tipo_ot", sa.String(20), server_default="FABRICACION", nullable=False),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("centro_trabajo_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("responsable_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("cantidad_objetivo", sa.Numeric(15, 3), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("cantidad_confirmada", sa.Numeric(15, 3), server_default="0", nullable=False),
    )
    op.alter_column("registro_diario_produccion", "maquina_id", existing_type=sa.Integer(), nullable=True)
    op.create_foreign_key(
        "fk_registro_diario_centro_trabajo",
        "registro_diario_produccion", "scm_centro_trabajo",
        ["centro_trabajo_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_registro_diario_responsable",
        "registro_diario_produccion", "trabajador",
        ["responsable_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_registro_diario_tipo_ot", "registro_diario_produccion",
        "tipo_ot IN ('FABRICACION', 'ENSAMBLE')",
    )
    op.create_check_constraint(
        "ck_registro_diario_recurso_ot", "registro_diario_produccion",
        "(tipo_ot = 'FABRICACION' AND maquina_id IS NOT NULL) OR "
        "(tipo_ot = 'ENSAMBLE' AND centro_trabajo_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_registro_diario_cantidad_objetivo", "registro_diario_produccion",
        "cantidad_objetivo IS NULL OR cantidad_objetivo > 0",
    )

    op.drop_constraint(
        "ck_scm_existencia_manga_logistica", "scm_existencia_manga", type_="check"
    )
    op.create_check_constraint(
        "ck_scm_existencia_manga_logistica", "scm_existencia_manga",
        "estado_logistico IN ('RECIBIDA_ALMACEN', 'RESERVADA', 'EN_PICKING', "
        "'EN_TRANSITO_PRODUCCION', 'EN_STAGING_ARMADO', 'ABIERTA_EN_CONSUMO', "
        "'CONSUMIDA', 'PENDIENTE_RETORNO', 'EN_TRANSITO_ALMACEN', 'REVERSADA')",
    )
    op.drop_constraint(
        "ck_scm_movimiento_inventario_tipo", "scm_movimiento_inventario", type_="check"
    )
    op.create_check_constraint(
        "ck_scm_movimiento_inventario_tipo", "scm_movimiento_inventario",
        "tipo IN ('SALDO_INICIAL', 'INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', "
        "'AJUSTE_NEGATIVO', 'CONSUMO', 'TRASLADO_SALIDA', 'TRASLADO_ENTRADA', "
        "'RETORNO_SALIDA', 'RETORNO_ENTRADA')",
    )

    op.create_table(
        "scm_solicitud_abastecimiento",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(32), nullable=False),
        sa.Column("orden_ensamble_id", sa.Uuid(), nullable=False),
        sa.Column("orden_trabajo_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(24), server_default="SOLICITADA", nullable=False),
        sa.Column("solicitado_por_id", sa.Integer(), nullable=False),
        sa.Column("solicitado_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("preparada_por_id", sa.Integer()),
        sa.Column("preparada_at", sa.DateTime(timezone=True)),
        sa.Column("despachada_por_id", sa.Integer()),
        sa.Column("despachada_at", sa.DateTime(timezone=True)),
        sa.Column("recibida_por_id", sa.Integer()),
        sa.Column("recibida_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "estado IN ('SOLICITADA', 'EN_PREPARACION', 'LISTA', 'DESPACHADA', "
            "'RECIBIDA', 'CERRADA', 'CANCELADA', 'INCIDENCIA')",
            name="ck_scm_solicitud_abastecimiento_estado",
        ),
        sa.CheckConstraint("version > 0", name="ck_scm_solicitud_abastecimiento_version"),
        sa.ForeignKeyConstraint(["orden_ensamble_id"], ["scm_orden_operacion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["orden_trabajo_id"], ["registro_diario_produccion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["solicitado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["preparada_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["despachada_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recibida_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_solicitud_abastecimiento_codigo"),
        sa.UniqueConstraint("orden_trabajo_id", name="uq_scm_solicitud_abastecimiento_ot"),
    )
    op.create_table(
        "scm_solicitud_abastecimiento_linea",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("solicitud_id", sa.Uuid(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column("cantidad_requerida", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_por_salida", sa.Numeric(15, 6), nullable=False),
        sa.Column("merma_tecnica_pct", sa.Numeric(8, 4), server_default="0", nullable=False),
        sa.CheckConstraint("cantidad_requerida > 0", name="ck_scm_abastecimiento_linea_cantidad"),
        sa.ForeignKeyConstraint(["solicitud_id"], ["scm_solicitud_abastecimiento.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("solicitud_id", "articulo_scm_id", name="uq_scm_abastecimiento_linea_articulo"),
    )
    op.create_table(
        "scm_asignacion_abastecimiento",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("linea_id", sa.Uuid(), nullable=False),
        sa.Column("existencia_manga_id", sa.Uuid(), nullable=False),
        sa.Column("cantidad_asignada", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_consumida", sa.Numeric(15, 3), server_default="0", nullable=False),
        sa.Column("cantidad_retornada", sa.Numeric(15, 3), server_default="0", nullable=False),
        sa.Column("estado", sa.String(32), server_default="RESERVADA", nullable=False),
        sa.Column("asignada_por_id", sa.Integer(), nullable=False),
        sa.Column("asignada_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "estado IN ('RESERVADA', 'EN_PICKING', 'EN_TRANSITO_PRODUCCION', "
            "'EN_STAGING_ARMADO', 'ABIERTA_EN_CONSUMO', 'PENDIENTE_RETORNO', "
            "'EN_TRANSITO_ALMACEN', 'RETORNADA', 'CONSUMIDA', 'CANCELADA')",
            name="ck_scm_asignacion_abastecimiento_estado",
        ),
        sa.CheckConstraint(
            "cantidad_asignada > 0 AND cantidad_consumida >= 0 AND cantidad_retornada >= 0 "
            "AND cantidad_consumida + cantidad_retornada <= cantidad_asignada",
            name="ck_scm_asignacion_abastecimiento_cantidades",
        ),
        sa.ForeignKeyConstraint(["linea_id"], ["scm_solicitud_abastecimiento_linea.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["existencia_manga_id"], ["scm_existencia_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asignada_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("linea_id", "existencia_manga_id", name="uq_scm_asignacion_abastecimiento_manga"),
    )

    op.execute(sa.text("""
        INSERT INTO correlativo_catalogo (clave, prefijo, siguiente_valor, ancho)
        SELECT 'SOLICITUD_ABASTECIMIENTO', 'SA', 1, 6
         WHERE NOT EXISTS (
            SELECT 1 FROM correlativo_catalogo WHERE clave = 'SOLICITUD_ABASTECIMIENTO'
         )
    """))
    op.execute(sa.text("""
        INSERT INTO rol_operativo (codigo, nombre, activo)
        SELECT 'JEFE_ENSAMBLE', 'Jefe de Ensamble', true
         WHERE NOT EXISTS (SELECT 1 FROM rol_operativo WHERE codigo = 'JEFE_ENSAMBLE')
    """))
    capabilities = (
        ("ABASTECIMIENTO_VER", "Consultar abastecimiento interno"),
        ("ABASTECIMIENTO_SOLICITAR", "Solicitar componentes para una OT de Ensamble"),
        ("PICKING_PREPARAR", "Reservar y preparar mangas por QR"),
        ("PICKING_DESPACHAR", "Despachar picking hacia Produccion"),
        ("ABASTECIMIENTO_RECIBIR", "Recibir picking en Mesa de Armado"),
        ("ABASTECIMIENTO_DEVOLVER", "Devolver remanentes desde Armado"),
        ("RETORNO_RECIBIR", "Recibir remanentes en Almacen"),
        ("UNIDAD_LOGISTICA_FRACCIONAR", "Autorizar fraccionamiento fisico de una manga"),
        ("GENEALOGIA_CANDIDATA_CONFIRMAR", "Confirmar genealogia por candidatos"),
        ("ABASTECIMIENTO_CORREGIR_SOLICITAR", "Solicitar correccion de abastecimiento"),
        ("ABASTECIMIENTO_CORREGIR_APROBAR", "Aprobar correccion de abastecimiento"),
        ("ABASTECIMIENTO_EMERGENCIA_APROBAR", "Aprobar abastecimiento no planificado"),
    )
    for code, name in capabilities:
        _capability(code, name)
    for role in (
        "ALMACEN_RECEPCION", "GERENCIA", "SUPERVISOR", "PLANIFICACION",
        "JEFE_PRODUCCION", "JEFE_ENSAMBLE", "AUDITORIA_CONSULTA",
    ):
        _assign(role, "ABASTECIMIENTO_VER")
    for capability in ("PICKING_PREPARAR", "PICKING_DESPACHAR", "RETORNO_RECIBIR"):
        _assign("ALMACEN_RECEPCION", capability)
    for capability in (
        "ABASTECIMIENTO_SOLICITAR", "ABASTECIMIENTO_RECIBIR",
        "ABASTECIMIENTO_DEVOLVER", "GENEALOGIA_CANDIDATA_CONFIRMAR",
        "ABASTECIMIENTO_CORREGIR_SOLICITAR",
    ):
        _assign("JEFE_ENSAMBLE", capability)
        _assign("SUPERVISOR", capability)
    for capability, _ in capabilities:
        _assign("JEFE_PRODUCCION", capability)
    for capability in ("OT_VER", "OT_CREAR", "OT_INICIAR", "OT_CERRAR", "OE_VER", "OE_EJECUTAR"):
        _assign("JEFE_ENSAMBLE", capability)

    for code, name in (
        ("TRANSITO_PRODUCCION", "Transito hacia Produccion"),
        ("MESA_ARMADO", "Mesa de Armado"),
        ("TRANSITO_ALMACEN", "Transito hacia Almacen"),
    ):
        op.execute(sa.text("""
            INSERT INTO scm_ubicacion_inventario
                (codigo, nombre, clases_articulo_json, activo)
            SELECT :code, :name, '["PIEZA_COLOR", "SUBENSAMBLE_WIP"]', true
             WHERE NOT EXISTS (
                SELECT 1 FROM scm_ubicacion_inventario WHERE codigo = :code
             )
        """).bindparams(code=code, name=name))


def downgrade():
    op.drop_table("scm_asignacion_abastecimiento")
    op.drop_table("scm_solicitud_abastecimiento_linea")
    op.drop_table("scm_solicitud_abastecimiento")
    op.drop_constraint("ck_scm_movimiento_inventario_tipo", "scm_movimiento_inventario", type_="check")
    op.create_check_constraint(
        "ck_scm_movimiento_inventario_tipo", "scm_movimiento_inventario",
        "tipo IN ('SALDO_INICIAL', 'INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', "
        "'AJUSTE_NEGATIVO', 'CONSUMO')",
    )
    op.drop_constraint("ck_scm_existencia_manga_logistica", "scm_existencia_manga", type_="check")
    op.create_check_constraint(
        "ck_scm_existencia_manga_logistica", "scm_existencia_manga",
        "estado_logistico IN ('RECIBIDA_ALMACEN', 'REVERSADA')",
    )
    op.drop_constraint("ck_registro_diario_cantidad_objetivo", "registro_diario_produccion", type_="check")
    op.drop_constraint("ck_registro_diario_recurso_ot", "registro_diario_produccion", type_="check")
    op.drop_constraint("ck_registro_diario_tipo_ot", "registro_diario_produccion", type_="check")
    op.drop_constraint("fk_registro_diario_responsable", "registro_diario_produccion", type_="foreignkey")
    op.drop_constraint("fk_registro_diario_centro_trabajo", "registro_diario_produccion", type_="foreignkey")
    op.alter_column("registro_diario_produccion", "maquina_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("registro_diario_produccion", "cantidad_confirmada")
    op.drop_column("registro_diario_produccion", "cantidad_objetivo")
    op.drop_column("registro_diario_produccion", "responsable_id")
    op.drop_column("registro_diario_produccion", "centro_trabajo_id")
    op.drop_column("registro_diario_produccion", "tipo_ot")
