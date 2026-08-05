"""expand OP demand, OF/OE operation and supply allocation model

Revision ID: f16b3d9e5a42
Revises: e05a2c8d4f31
Create Date: 2026-07-29 13:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f16b3d9e5a42"
down_revision = "e05a2c8d4f31"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_orden_produccion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(32), nullable=False),
        sa.Column("origen", sa.String(32), nullable=False),
        sa.Column("referencia_origen", sa.String(100), nullable=True),
        sa.Column("fecha_necesidad", sa.Date(), nullable=False),
        sa.Column(
            "prioridad", sa.String(24),
            server_default="NORMAL", nullable=False,
        ),
        sa.Column(
            "estado", sa.String(24),
            server_default="BORRADOR", nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "estado IN ('BORRADOR', 'APROBADA', 'PLANIFICADA', "
            "'EN_COBERTURA', 'COMPLETADA', 'CANCELADA')",
            name="ck_scm_orden_produccion_estado",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_scm_orden_produccion_version"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["trabajador.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"], ["trabajador.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "codigo", name="uq_scm_orden_produccion_codigo"
        ),
    )

    op.create_table(
        "scm_orden_produccion_linea",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("orden_produccion_id", sa.Uuid(), nullable=False),
        sa.Column("producto_terminado_id", sa.String(50), nullable=False),
        sa.Column(
            "cantidad_solicitada", sa.Numeric(15, 3), nullable=False
        ),
        sa.Column("fecha_necesidad", sa.Date(), nullable=True),
        sa.Column("estructura_revision_id", sa.Integer(), nullable=True),
        sa.Column("estructura_hash", sa.String(64), nullable=True),
        sa.Column("ruta_revision_id", sa.Integer(), nullable=True),
        sa.Column("ruta_hash", sa.String(64), nullable=True),
        sa.Column(
            "estado", sa.String(24),
            server_default="ACTIVA", nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "cantidad_solicitada > 0", name="ck_scm_op_linea_cantidad"
        ),
        sa.CheckConstraint(
            "estado IN ('ACTIVA', 'CANCELADA', 'SATISFECHA')",
            name="ck_scm_op_linea_estado",
        ),
        sa.CheckConstraint("version > 0", name="ck_scm_op_linea_version"),
        sa.ForeignKeyConstraint(
            ["orden_produccion_id"], ["scm_orden_produccion.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["producto_terminado_id"],
            ["producto_terminado.cod_sku_pt"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["estructura_revision_id"], ["scm_estructura_revision.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ruta_revision_id"], ["scm_ruta_revision.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "scm_orden_operacion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(32), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("origen_demanda", sa.String(32), nullable=False),
        sa.Column("motivo", sa.Text(), nullable=True),
        sa.Column(
            "estado", sa.String(24),
            server_default="BORRADOR", nullable=False,
        ),
        sa.Column(
            "operacion_ruta_revision_id", sa.Integer(), nullable=True
        ),
        sa.Column("operacion_ruta_hash", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        # Nullable solo para backfill legacy sin actor demostrable.
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("released_by_id", sa.Integer(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "tipo IN ('FABRICACION', 'ENSAMBLE')",
            name="ck_scm_orden_operacion_tipo",
        ),
        sa.CheckConstraint(
            "estado IN ('BORRADOR', 'LIBERADA', 'PROGRAMADA', "
            "'EN_EJECUCION', 'CERRADA', 'ANULADA')",
            name="ck_scm_orden_operacion_estado",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_scm_orden_operacion_version"
        ),
        sa.ForeignKeyConstraint(
            ["operacion_ruta_revision_id"], ["scm_operacion_ruta.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["trabajador.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["released_by_id"], ["trabajador.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "codigo", name="uq_scm_orden_operacion_codigo"
        ),
    )

    op.create_table(
        "scm_orden_fabricacion",
        sa.Column("orden_operacion_id", sa.Uuid(), nullable=False),
        sa.Column("molde_id", sa.String(50), nullable=True),
        sa.Column("maquina_prevista_id", sa.Integer(), nullable=True),
        sa.Column(
            "snapshot_tiempo_ciclo_seg", sa.Numeric(12, 4), nullable=True
        ),
        sa.Column(
            "snapshot_horas_turno", sa.Numeric(8, 3), nullable=True
        ),
        sa.Column(
            "snapshot_peso_colada_gr", sa.Numeric(12, 4), nullable=True
        ),
        sa.Column("codigo_legacy_op", sa.String(20), nullable=True),
        sa.CheckConstraint(
            "snapshot_tiempo_ciclo_seg IS NULL "
            "OR snapshot_tiempo_ciclo_seg > 0",
            name="ck_scm_of_tiempo_ciclo",
        ),
        sa.CheckConstraint(
            "snapshot_horas_turno IS NULL OR snapshot_horas_turno > 0",
            name="ck_scm_of_horas_turno",
        ),
        sa.CheckConstraint(
            "snapshot_peso_colada_gr IS NULL "
            "OR snapshot_peso_colada_gr >= 0",
            name="ck_scm_of_peso_colada",
        ),
        sa.ForeignKeyConstraint(
            ["orden_operacion_id"], ["scm_orden_operacion.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["molde_id"], ["molde.codigo"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["maquina_prevista_id"], ["maquina.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["codigo_legacy_op"], ["orden_produccion.numero_op"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("orden_operacion_id"),
        sa.UniqueConstraint(
            "codigo_legacy_op", name="uq_scm_of_codigo_legacy"
        ),
    )

    op.create_table(
        "scm_corrida_fabricacion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("orden_fabricacion_id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(48), nullable=False),
        sa.Column("secuencia", sa.Integer(), nullable=False),
        sa.Column("color_produccion_id", sa.Integer(), nullable=True),
        sa.Column("receta_revision_id", sa.Integer(), nullable=True),
        sa.Column("receta_hash", sa.String(64), nullable=True),
        sa.Column("ciclos_objetivo", sa.Integer(), nullable=True),
        sa.Column(
            "estado", sa.String(24),
            server_default="BORRADOR", nullable=False,
        ),
        sa.Column("lote_color_legacy_id", sa.Integer(), nullable=True),
        sa.Column("meta_kg_legacy", sa.Numeric(15, 6), nullable=True),
        sa.CheckConstraint(
            "secuencia > 0", name="ck_scm_corrida_secuencia"
        ),
        sa.CheckConstraint(
            "ciclos_objetivo IS NULL OR ciclos_objetivo > 0",
            name="ck_scm_corrida_ciclos",
        ),
        sa.CheckConstraint(
            "estado IN ('BORRADOR', 'LIBERADA', 'EN_EJECUCION', "
            "'COMPLETADA', 'ANULADA')",
            name="ck_scm_corrida_estado",
        ),
        sa.ForeignKeyConstraint(
            ["orden_fabricacion_id"],
            ["scm_orden_fabricacion.orden_operacion_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["color_produccion_id"], ["color_produccion.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receta_revision_id"], ["receta_color_maestra.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lote_color_legacy_id"], ["lote_color.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "orden_fabricacion_id", "secuencia",
            name="uq_scm_corrida_of_secuencia",
        ),
        sa.UniqueConstraint(
            "orden_fabricacion_id", "codigo",
            name="uq_scm_corrida_of_codigo",
        ),
        sa.UniqueConstraint(
            "lote_color_legacy_id", name="uq_scm_corrida_lote_legacy"
        ),
    )

    op.create_table(
        "scm_orden_operacion_salida",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("orden_operacion_id", sa.Uuid(), nullable=False),
        sa.Column("corrida_fabricacion_id", sa.Uuid(), nullable=True),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column(
            "cantidad_por_ciclo_snapshot", sa.Numeric(12, 4), nullable=True
        ),
        sa.Column(
            "peso_unitario_snapshot_g", sa.Numeric(12, 4), nullable=True
        ),
        sa.Column("cantidad_objetivo", sa.Numeric(15, 3), nullable=False),
        sa.Column(
            "kg_estandar_objetivo", sa.Numeric(15, 6), nullable=True
        ),
        sa.Column(
            "excedente_objetivo", sa.Numeric(15, 3),
            server_default="0", nullable=False,
        ),
        sa.Column("lote_salida_legacy_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "cantidad_objetivo > 0", name="ck_scm_salida_cantidad"
        ),
        sa.CheckConstraint(
            "cantidad_por_ciclo_snapshot IS NULL "
            "OR cantidad_por_ciclo_snapshot > 0",
            name="ck_scm_salida_por_ciclo",
        ),
        sa.CheckConstraint(
            "peso_unitario_snapshot_g IS NULL "
            "OR peso_unitario_snapshot_g > 0",
            name="ck_scm_salida_peso_unitario",
        ),
        sa.CheckConstraint(
            "kg_estandar_objetivo IS NULL OR kg_estandar_objetivo >= 0",
            name="ck_scm_salida_kg_estandar",
        ),
        sa.CheckConstraint(
            "excedente_objetivo >= 0",
            name="ck_scm_salida_excedente",
        ),
        sa.ForeignKeyConstraint(
            ["orden_operacion_id"], ["scm_orden_operacion.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["corrida_fabricacion_id"], ["scm_corrida_fabricacion.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_scm_id"], ["scm_articulo.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lote_salida_legacy_id"], ["lote_salida_pieza_color.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "orden_operacion_id", "corrida_fabricacion_id",
            "articulo_scm_id",
            name="uq_scm_salida_orden_corrida_articulo",
        ),
        sa.UniqueConstraint(
            "lote_salida_legacy_id", name="uq_scm_salida_legacy"
        ),
    )

    op.create_table(
        "scm_asignacion_demanda_suministro",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("orden_produccion_linea_id", sa.Uuid(), nullable=False),
        sa.Column("fuente_tipo", sa.String(24), nullable=False),
        sa.Column("orden_operacion_salida_id", sa.Uuid(), nullable=True),
        sa.Column("lote_articulo_id", sa.Integer(), nullable=True),
        sa.Column(
            "cantidad_planificada", sa.Numeric(15, 3),
            server_default="0", nullable=False,
        ),
        sa.Column(
            "cantidad_comprometida", sa.Numeric(15, 3),
            server_default="0", nullable=False,
        ),
        sa.Column(
            "cantidad_satisfecha", sa.Numeric(15, 3),
            server_default="0", nullable=False,
        ),
        sa.Column(
            "estado", sa.String(24),
            server_default="PLANIFICADA", nullable=False,
        ),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "fuente_tipo IN ('STOCK', 'SALIDA_ORDEN')",
            name="ck_scm_asignacion_fuente_tipo",
        ),
        sa.CheckConstraint(
            "(fuente_tipo = 'STOCK' AND lote_articulo_id IS NOT NULL "
            "AND orden_operacion_salida_id IS NULL) OR "
            "(fuente_tipo = 'SALIDA_ORDEN' "
            "AND orden_operacion_salida_id IS NOT NULL "
            "AND lote_articulo_id IS NULL)",
            name="ck_scm_asignacion_fuente_exclusiva",
        ),
        sa.CheckConstraint(
            "cantidad_planificada >= 0 AND cantidad_comprometida >= 0 "
            "AND cantidad_satisfecha >= 0",
            name="ck_scm_asignacion_cantidades",
        ),
        sa.CheckConstraint(
            "estado IN ('PLANIFICADA', 'COMPROMETIDA', "
            "'SATISFECHA', 'CANCELADA')",
            name="ck_scm_asignacion_estado",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_scm_asignacion_version"
        ),
        sa.ForeignKeyConstraint(
            ["orden_produccion_linea_id"],
            ["scm_orden_produccion_linea.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["orden_operacion_salida_id"],
            ["scm_orden_operacion_salida.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lote_articulo_id"], ["scm_lote_articulo.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", name="uq_scm_asignacion_operation"
        ),
    )

    op.add_column(
        "registro_diario_produccion",
        sa.Column("orden_operacion_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("corrida_fabricacion_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_registro_diario_orden_operacion",
        "registro_diario_produccion", "scm_orden_operacion",
        ["orden_operacion_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_registro_diario_corrida_fabricacion",
        "registro_diario_produccion", "scm_corrida_fabricacion",
        ["corrida_fabricacion_id"], ["id"], ondelete="RESTRICT",
    )

    op.add_column(
        "scm_plan_manga_op",
        sa.Column("orden_operacion_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_scm_plan_manga_orden_operacion",
        "scm_plan_manga_op", "scm_orden_operacion",
        ["orden_operacion_id"], ["id"], ondelete="RESTRICT",
    )
    op.add_column(
        "scm_plan_manga_op_linea",
        sa.Column("orden_operacion_salida_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_scm_plan_manga_linea_salida_canonica",
        "scm_plan_manga_op_linea", "scm_orden_operacion_salida",
        ["orden_operacion_salida_id"], ["id"], ondelete="RESTRICT",
    )

    for key, prefix in (
        ("ORDEN_PRODUCCION", "OP"),
        ("ORDEN_FABRICACION", "OF"),
        ("ORDEN_ENSAMBLE", "OE"),
    ):
        op.execute(sa.text("""
            INSERT INTO correlativo_catalogo (
                clave, prefijo, siguiente_valor, ancho
            )
            SELECT :key, :prefix, 1, 6
            WHERE NOT EXISTS (
                SELECT 1 FROM correlativo_catalogo WHERE clave = :key
            )
        """).bindparams(key=key, prefix=prefix))


def downgrade():
    connection = op.get_bind()
    for table_name in (
        "scm_asignacion_demanda_suministro",
        "scm_orden_operacion_salida",
        "scm_corrida_fabricacion",
        "scm_orden_fabricacion",
        "scm_orden_operacion",
        "scm_orden_produccion_linea",
        "scm_orden_produccion",
    ):
        count = connection.execute(
            sa.text(f"SELECT count(*) FROM {table_name}")
        ).scalar_one()
        if count:
            raise RuntimeError(
                "SCM_TS010P_DOWNGRADE_BLOCKED: existen hechos documentales"
            )

    op.drop_constraint(
        "fk_scm_plan_manga_linea_salida_canonica",
        "scm_plan_manga_op_linea", type_="foreignkey",
    )
    op.drop_column(
        "scm_plan_manga_op_linea", "orden_operacion_salida_id"
    )
    op.drop_constraint(
        "fk_scm_plan_manga_orden_operacion",
        "scm_plan_manga_op", type_="foreignkey",
    )
    op.drop_column("scm_plan_manga_op", "orden_operacion_id")
    op.drop_constraint(
        "fk_registro_diario_corrida_fabricacion",
        "registro_diario_produccion", type_="foreignkey",
    )
    op.drop_constraint(
        "fk_registro_diario_orden_operacion",
        "registro_diario_produccion", type_="foreignkey",
    )
    op.drop_column(
        "registro_diario_produccion", "corrida_fabricacion_id"
    )
    op.drop_column("registro_diario_produccion", "orden_operacion_id")

    op.drop_table("scm_asignacion_demanda_suministro")
    op.drop_table("scm_orden_operacion_salida")
    op.drop_table("scm_corrida_fabricacion")
    op.drop_table("scm_orden_fabricacion")
    op.drop_table("scm_orden_operacion")
    op.drop_table("scm_orden_produccion_linea")
    op.drop_table("scm_orden_produccion")
