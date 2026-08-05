"""add OT manga plans, assignments, identities and prelabels

Revision ID: c83e0f4b6a12
Revises: b72d9e3a5f01
Create Date: 2026-07-28 09:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "c83e0f4b6a12"
down_revision = "b72d9e3a5f01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_plan_manga_op",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("orden_id", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "estado", sa.String(length=20),
            server_default="ACTIVO", nullable=False,
        ),
        sa.Column("calculado_por_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "revision > 0", name="ck_scm_plan_manga_revision"
        ),
        sa.CheckConstraint(
            "estado IN ('ACTIVO', 'SUPERADO')",
            name="ck_scm_plan_manga_estado",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_scm_plan_manga_hash"
        ),
        sa.ForeignKeyConstraint(
            ["orden_id"], ["orden_produccion.numero_op"],
            name="fk_scm_plan_manga_op_orden", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["calculado_por_id"], ["trabajador.id"],
            name="fk_scm_plan_manga_calculador", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["scm_operacion.operation_id"],
            name="fk_scm_plan_manga_operacion", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "orden_id", "revision", name="uq_scm_plan_manga_op_revision"
        ),
    )
    op.create_index(
        "ux_scm_plan_manga_op_activo",
        "scm_plan_manga_op",
        ["orden_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'ACTIVO'"),
    )

    op.create_table(
        "scm_plan_manga_op_linea",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("lote_salida_pieza_color_id", sa.Integer(), nullable=False),
        sa.Column("lote_articulo_id", sa.Integer(), nullable=False),
        sa.Column("perfil_empacable_id", sa.Integer(), nullable=False),
        sa.Column("regla_revision_id", sa.Integer(), nullable=False),
        sa.Column("tipo_contenedor_id", sa.Integer(), nullable=False),
        sa.Column(
            "cantidad_objetivo_un",
            sa.Numeric(precision=15, scale=3), nullable=False,
        ),
        sa.Column("capacidad_efectiva_un", sa.Integer(), nullable=False),
        sa.Column("mangas_propuestas", sa.Integer(), nullable=False),
        sa.Column(
            "peso_unitario_snapshot_g",
            sa.Numeric(precision=12, scale=4), nullable=False,
        ),
        sa.Column(
            "articulo_codigo_snapshot", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "articulo_nombre_snapshot", sa.String(length=200), nullable=False
        ),
        sa.Column(
            "pieza_color_sku_snapshot", sa.String(length=50), nullable=False
        ),
        sa.Column("color_snapshot", sa.String(length=120), nullable=True),
        sa.Column(
            "regla_hash_snapshot", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "tara_nominal_g_snapshot",
            sa.Numeric(precision=12, scale=3), nullable=False,
        ),
        sa.Column(
            "tolerancia_tara_g_snapshot",
            sa.Numeric(precision=12, scale=3), nullable=False,
        ),
        sa.Column(
            "peso_bruto_max_kg_snapshot",
            sa.Numeric(precision=12, scale=3), nullable=False,
        ),
        sa.CheckConstraint(
            "cantidad_objetivo_un > 0",
            name="ck_scm_plan_manga_linea_objetivo",
        ),
        sa.CheckConstraint(
            "capacidad_efectiva_un > 0",
            name="ck_scm_plan_manga_linea_capacidad",
        ),
        sa.CheckConstraint(
            "mangas_propuestas > 0",
            name="ck_scm_plan_manga_linea_mangas",
        ),
        sa.CheckConstraint(
            "peso_unitario_snapshot_g > 0",
            name="ck_scm_plan_manga_linea_peso",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["scm_plan_manga_op.id"],
            name="fk_scm_plan_manga_linea_plan", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lote_salida_pieza_color_id"],
            ["lote_salida_pieza_color.id"],
            name="fk_scm_plan_manga_linea_salida", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lote_articulo_id"], ["scm_lote_articulo.id"],
            name="fk_scm_plan_manga_linea_lote_articulo",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["perfil_empacable_id"], ["scm_perfil_empacable.id"],
            name="fk_scm_plan_manga_linea_perfil", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["regla_revision_id"], ["scm_regla_empaque_revision.id"],
            name="fk_scm_plan_manga_linea_regla", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tipo_contenedor_id"], ["scm_tipo_contenedor.id"],
            name="fk_scm_plan_manga_linea_contenedor", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id", "lote_salida_pieza_color_id",
            name="uq_scm_plan_manga_linea_salida",
        ),
    )

    op.create_table(
        "scm_asignacion_plan_manga_ot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_linea_id", sa.Integer(), nullable=False),
        sa.Column("ot_id", sa.Integer(), nullable=False),
        sa.Column(
            "cantidad_asignada_un",
            sa.Numeric(precision=15, scale=3), nullable=False,
        ),
        sa.Column("mangas_asignadas", sa.Integer(), nullable=False),
        sa.Column("asignada_por_id", sa.Integer(), nullable=False),
        sa.Column(
            "asignada_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "cantidad_asignada_un > 0",
            name="ck_scm_asignacion_plan_cantidad",
        ),
        sa.CheckConstraint(
            "mangas_asignadas > 0",
            name="ck_scm_asignacion_plan_mangas",
        ),
        sa.ForeignKeyConstraint(
            ["plan_linea_id"], ["scm_plan_manga_op_linea.id"],
            name="fk_scm_asignacion_plan_linea", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ot_id"], ["registro_diario_produccion.id"],
            name="fk_scm_asignacion_plan_ot", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asignada_por_id"], ["trabajador.id"],
            name="fk_scm_asignacion_plan_actor", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_linea_id", "ot_id", name="uq_scm_asignacion_plan_ot"
        ),
    )

    op.create_table(
        "scm_solicitud_manga_extra",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("ot_id", sa.Integer(), nullable=False),
        sa.Column("plan_linea_id", sa.Integer(), nullable=False),
        sa.Column(
            "cantidad_solicitada_un",
            sa.Numeric(precision=15, scale=3), nullable=False,
        ),
        sa.Column("motivo", sa.String(length=250), nullable=False),
        sa.Column(
            "estado", sa.String(length=20),
            server_default="PENDIENTE", nullable=False,
        ),
        sa.Column("solicitada_por_id", sa.Integer(), nullable=False),
        sa.Column(
            "solicitada_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("resuelta_por_id", sa.Integer(), nullable=True),
        sa.Column("resuelta_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "estado IN ('PENDIENTE', 'APROBADA', 'RECHAZADA')",
            name="ck_scm_solicitud_manga_extra_estado",
        ),
        sa.CheckConstraint(
            "cantidad_solicitada_un > 0",
            name="ck_scm_solicitud_manga_extra_cantidad",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_scm_solicitud_manga_extra_version"
        ),
        sa.ForeignKeyConstraint(
            ["ot_id"], ["registro_diario_produccion.id"],
            name="fk_scm_solicitud_extra_ot", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_linea_id"], ["scm_plan_manga_op_linea.id"],
            name="fk_scm_solicitud_extra_linea", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["solicitada_por_id"], ["trabajador.id"],
            name="fk_scm_solicitud_extra_solicitante", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resuelta_por_id"], ["trabajador.id"],
            name="fk_scm_solicitud_extra_resolutor", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )

    op.create_table(
        "scm_manga",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(length=80), nullable=False),
        sa.Column("ot_id", sa.Integer(), nullable=False),
        sa.Column("plan_linea_id", sa.Integer(), nullable=False),
        sa.Column("asignacion_id", sa.Integer(), nullable=True),
        sa.Column("lote_articulo_id", sa.Integer(), nullable=False),
        sa.Column("secuencia_ot", sa.Integer(), nullable=False),
        sa.Column(
            "tipo", sa.String(length=16),
            server_default="NORMAL", nullable=False,
        ),
        sa.Column(
            "estado", sa.String(length=32),
            server_default="PLANIFICADA", nullable=False,
        ),
        sa.Column(
            "cantidad_planificada_un",
            sa.Numeric(precision=15, scale=3), nullable=False,
        ),
        sa.Column(
            "cantidad_asignada_un",
            sa.Numeric(precision=15, scale=3), nullable=False,
        ),
        sa.Column(
            "cantidad_confirmada_un",
            sa.Numeric(precision=15, scale=3), nullable=True,
        ),
        sa.Column(
            "cantidad_contenida_un",
            sa.Numeric(precision=15, scale=3), nullable=True,
        ),
        sa.Column("maquinista_previsto_id", sa.Integer(), nullable=False),
        sa.Column(
            "articulo_codigo_snapshot", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "articulo_nombre_snapshot", sa.String(length=200), nullable=False
        ),
        sa.Column(
            "pieza_color_sku_snapshot", sa.String(length=50), nullable=False
        ),
        sa.Column("color_snapshot", sa.String(length=120), nullable=True),
        sa.Column("regla_revision_id_snapshot", sa.Integer(), nullable=False),
        sa.Column(
            "regla_hash_snapshot", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "tipo_contenedor_codigo_snapshot",
            sa.String(length=64), nullable=False,
        ),
        sa.Column(
            "tipo_contenedor_nombre_snapshot",
            sa.String(length=160), nullable=False,
        ),
        sa.Column(
            "peso_unitario_snapshot_g",
            sa.Numeric(precision=12, scale=4), nullable=False,
        ),
        sa.Column(
            "tara_nominal_g_snapshot",
            sa.Numeric(precision=12, scale=3), nullable=False,
        ),
        sa.Column(
            "tolerancia_tara_g_snapshot",
            sa.Numeric(precision=12, scale=3), nullable=False,
        ),
        sa.Column(
            "peso_bruto_max_kg_snapshot",
            sa.Numeric(precision=12, scale=3), nullable=False,
        ),
        sa.Column("motivo_extra", sa.String(length=250), nullable=True),
        sa.Column("extra_solicitada_por_id", sa.Integer(), nullable=True),
        sa.Column("extra_aprobada_por_id", sa.Integer(), nullable=True),
        sa.Column(
            "extra_aprobada_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("anulada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("anulada_por_id", sa.Integer(), nullable=True),
        sa.Column("motivo_anulacion", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "tipo IN ('NORMAL', 'EXTRA')", name="ck_scm_manga_tipo"
        ),
        sa.CheckConstraint(
            "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'PESADA', "
            "'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', 'ANULADA')",
            name="ck_scm_manga_estado",
        ),
        sa.CheckConstraint(
            "cantidad_asignada_un > 0", name="ck_scm_manga_cantidad"
        ),
        sa.CheckConstraint(
            "secuencia_ot > 0", name="ck_scm_manga_secuencia"
        ),
        sa.ForeignKeyConstraint(
            ["ot_id"], ["registro_diario_produccion.id"],
            name="fk_scm_manga_ot", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_linea_id"], ["scm_plan_manga_op_linea.id"],
            name="fk_scm_manga_plan_linea", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asignacion_id"], ["scm_asignacion_plan_manga_ot.id"],
            name="fk_scm_manga_asignacion", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lote_articulo_id"], ["scm_lote_articulo.id"],
            name="fk_scm_manga_lote_articulo", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["maquinista_previsto_id"], ["trabajador.id"],
            name="fk_scm_manga_maquinista", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extra_solicitada_por_id"], ["trabajador.id"],
            name="fk_scm_manga_extra_solicitante", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["extra_aprobada_por_id"], ["trabajador.id"],
            name="fk_scm_manga_extra_aprobador", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["trabajador.id"],
            name="fk_scm_manga_creador", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["anulada_por_id"], ["trabajador.id"],
            name="fk_scm_manga_anulador", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_manga_public_id"),
        sa.UniqueConstraint("codigo", name="uq_scm_manga_codigo"),
        sa.UniqueConstraint(
            "ot_id", "secuencia_ot", name="uq_scm_manga_ot_secuencia"
        ),
    )

    op.create_table(
        "scm_trabajo_impresion_manga",
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column(
            "estado", sa.String(length=20),
            server_default="GENERADO", nullable=False,
        ),
        sa.Column(
            "plantilla_version", sa.String(length=32),
            server_default="PREPESAJE_TSPL_1", nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("solicitado_por_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("station_id", sa.String(length=36), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "estado IN ('GENERADO', 'PROCESADO', 'PARCIAL', 'FALLIDO')",
            name="ck_scm_trabajo_impresion_estado",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_scm_trabajo_impresion_hash",
        ),
        sa.ForeignKeyConstraint(
            ["solicitado_por_id"], ["trabajador.id"],
            name="fk_scm_trabajo_impresion_solicitante",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["station_id"], ["estacion_pesaje.station_id"],
            name="fk_scm_trabajo_impresion_estacion", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("public_id"),
    )

    op.create_table(
        "scm_etiqueta_manga",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("trabajo_impresion_id", sa.Uuid(), nullable=False),
        sa.Column(
            "tipo", sa.String(length=20),
            server_default="PREPESAJE", nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "estado", sa.String(length=28),
            server_default="GENERADA", nullable=False,
        ),
        sa.Column(
            "plantilla_version", sa.String(length=32),
            server_default="PREPESAJE_TSPL_1", nullable=False,
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("printed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("station_id", sa.String(length=36), nullable=True),
        sa.Column("printer_name", sa.String(length=160), nullable=True),
        sa.Column("error_tecnico", sa.String(length=500), nullable=True),
        sa.Column("invalidada_por_id", sa.Integer(), nullable=True),
        sa.Column("invalidada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "motivo_invalidacion", sa.String(length=500), nullable=True
        ),
        sa.Column("reemplazada_por_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "tipo = 'PREPESAJE'",
            name="ck_scm_etiqueta_manga_tipo_c_core",
        ),
        sa.CheckConstraint(
            "estado IN ('GENERADA', 'IMPRESA', 'FALLIDA_SIN_EMISION', "
            "'EMISION_INCIERTA', 'INVALIDADA')",
            name="ck_scm_etiqueta_manga_estado",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_scm_etiqueta_manga_version"
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_scm_etiqueta_manga_hash",
        ),
        sa.ForeignKeyConstraint(
            ["manga_id"], ["scm_manga.id"],
            name="fk_scm_etiqueta_manga_manga", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["trabajo_impresion_id"], ["scm_trabajo_impresion_manga.public_id"],
            name="fk_scm_etiqueta_manga_trabajo", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["station_id"], ["estacion_pesaje.station_id"],
            name="fk_scm_etiqueta_manga_estacion", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invalidada_por_id"], ["trabajador.id"],
            name="fk_scm_etiqueta_manga_invalidada_por",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reemplazada_por_id"], ["scm_etiqueta_manga.id"],
            name="fk_scm_etiqueta_manga_reemplazo", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "public_id", name="uq_scm_etiqueta_manga_public_id"
        ),
        sa.UniqueConstraint(
            "manga_id", "tipo", "version",
            name="uq_scm_etiqueta_manga_version",
        ),
    )


def downgrade():
    connection = op.get_bind()
    facts = connection.execute(sa.text("""
        SELECT
          (SELECT count(*) FROM scm_manga)
          + (SELECT count(*) FROM scm_etiqueta_manga)
    """)).scalar_one()
    if facts:
        raise RuntimeError(
            "SCM_OT_DOWNGRADE_BLOCKED: existen mangas o etiquetas"
        )
    op.drop_table("scm_etiqueta_manga")
    op.drop_table("scm_trabajo_impresion_manga")
    op.drop_table("scm_manga")
    op.drop_table("scm_solicitud_manga_extra")
    op.drop_table("scm_asignacion_plan_manga_ot")
    op.drop_table("scm_plan_manga_op_linea")
    op.drop_index(
        "ux_scm_plan_manga_op_activo",
        table_name="scm_plan_manga_op",
    )
    op.drop_table("scm_plan_manga_op")
