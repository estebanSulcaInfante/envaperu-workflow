"""expand OT machine headers with traceable color work

Revision ID: f78a7b3c9d20
Revises: f77e6f1b4c98
Create Date: 2026-08-08
"""

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "f78a7b3c9d20"
down_revision = "f77e6f1b4c98"
branch_labels = None
depends_on = None


NEW_FABRICATION_HEADER = (
    "tipo_ot = 'FABRICACION' "
    "AND codigo_ot_sintetico = false "
    "AND estado <> 'ANULADA' "
    "AND orden_id IS NULL "
    "AND orden_operacion_id IS NULL "
    "AND corrida_fabricacion_id IS NULL"
)


def _assert_backfill_preconditions(connection):
    invalid_context = connection.execute(sa.text("""
        SELECT ot.codigo_ot
        FROM registro_diario_produccion AS ot
        JOIN scm_orden_operacion AS orden
          ON orden.id = ot.orden_operacion_id
        JOIN scm_corrida_fabricacion AS corrida
          ON corrida.id = ot.corrida_fabricacion_id
        WHERE ot.tipo_ot = 'FABRICACION'
          AND ot.codigo_ot_sintetico = false
          AND ot.orden_operacion_id IS NOT NULL
          AND ot.corrida_fabricacion_id IS NOT NULL
          AND (
            orden.tipo <> 'FABRICACION'
            OR corrida.orden_fabricacion_id <> ot.orden_operacion_id
          )
        ORDER BY ot.id
        LIMIT 1
    """)).scalar_one_or_none()
    if invalid_context is not None:
        raise RuntimeError(
            "SCM_TRABAJO_COLOR_BACKFILL_INVALIDO: "
            f"la OT {invalid_context} no vincula una corrida de su OF"
        )

def _create_tables():
    op.create_table(
        "scm_trabajo_ot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("orden_trabajo_id", sa.Integer(), nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column(
            "tipo", sa.String(length=20),
            server_default="COLOR", nullable=False,
        ),
        sa.Column("secuencia", sa.Integer(), nullable=False),
        sa.Column(
            "estado", sa.String(length=24),
            server_default="PLANIFICADO", nullable=False,
        ),
        sa.Column("orden_operacion_id", sa.Uuid(), nullable=False),
        sa.Column("continua_de_id", sa.Uuid(), nullable=True),
        sa.Column(
            "cantidad_objetivo_un",
            sa.Numeric(precision=15, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "cantidad_confirmada_un",
            sa.Numeric(precision=15, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column("iniciada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pausada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("anulada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("motivo_pausa", sa.String(length=500), nullable=True),
        sa.Column("motivo_anulacion", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("anulada_por_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "tipo IN ('COLOR')",
            name="ck_scm_trabajo_ot_tipo",
        ),
        sa.CheckConstraint(
            "secuencia > 0",
            name="ck_scm_trabajo_ot_secuencia",
        ),
        sa.CheckConstraint(
            "estado IN ('PLANIFICADO', 'EN_EJECUCION', 'PAUSADO', "
            "'COMPLETADO', 'ANULADO')",
            name="ck_scm_trabajo_ot_estado",
        ),
        sa.CheckConstraint(
            "cantidad_objetivo_un >= 0 AND cantidad_confirmada_un >= 0",
            name="ck_scm_trabajo_ot_cantidades",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_trabajo_ot_version",
        ),
        sa.ForeignKeyConstraint(
            ["orden_trabajo_id"],
            ["registro_diario_produccion.id"],
            name="fk_scm_trabajo_ot_orden_trabajo",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["orden_operacion_id"],
            ["scm_orden_operacion.id"],
            name="fk_scm_trabajo_ot_orden_operacion",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["trabajador.id"],
            name="fk_scm_trabajo_ot_creador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["anulada_por_id"],
            ["trabajador.id"],
            name="fk_scm_trabajo_ot_anulador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["continua_de_id"],
            ["scm_trabajo_ot.id"],
            name="fk_scm_trabajo_ot_continua_de",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_trabajo_ot_codigo"),
        sa.UniqueConstraint(
            "orden_trabajo_id",
            "secuencia",
            name="uq_scm_trabajo_ot_orden_secuencia",
        ),
        sa.UniqueConstraint(
            "id",
            "orden_trabajo_id",
            name="uq_scm_trabajo_ot_id_orden",
        ),
        sa.UniqueConstraint(
            "continua_de_id",
            name="uq_scm_trabajo_ot_continuacion",
        ),
    )
    op.create_index(
        "ix_scm_trabajo_ot_orden_trabajo",
        "scm_trabajo_ot",
        ["orden_trabajo_id"],
    )
    op.create_index(
        "ix_scm_trabajo_ot_orden_operacion",
        "scm_trabajo_ot",
        ["orden_operacion_id"],
    )
    op.create_index(
        "ix_scm_trabajo_ot_created_by",
        "scm_trabajo_ot",
        ["created_by_id"],
    )
    op.create_index(
        "ix_scm_trabajo_ot_anulada_por",
        "scm_trabajo_ot",
        ["anulada_por_id"],
    )
    op.create_index(
        "uq_scm_trabajo_ot_activo",
        "scm_trabajo_ot",
        ["orden_trabajo_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'EN_EJECUCION'"),
        sqlite_where=sa.text("estado = 'EN_EJECUCION'"),
    )

    op.create_table(
        "scm_trabajo_color",
        sa.Column("trabajo_ot_id", sa.Uuid(), nullable=False),
        sa.Column("corrida_fabricacion_id", sa.Uuid(), nullable=False),
        sa.Column("molde_codigo_snapshot", sa.String(length=50), nullable=True),
        sa.Column("color_id_snapshot", sa.Integer(), nullable=True),
        sa.Column(
            "color_nombre_snapshot", sa.String(length=120), nullable=True,
        ),
        sa.Column("receta_revision_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("receta_hash_snapshot", sa.String(length=64), nullable=True),
        sa.Column("cavidades_snapshot", sa.Integer(), nullable=True),
        sa.Column(
            "peso_neto_snapshot_g",
            sa.Numeric(precision=15, scale=4),
            nullable=True,
        ),
        sa.Column(
            "peso_colada_snapshot_g",
            sa.Numeric(precision=15, scale=4),
            nullable=True,
        ),
        sa.Column("colada_inicial", sa.Integer(), nullable=True),
        sa.Column("colada_final", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "cavidades_snapshot IS NULL OR cavidades_snapshot > 0",
            name="ck_scm_trabajo_color_cavidades",
        ),
        sa.CheckConstraint(
            "peso_neto_snapshot_g IS NULL OR peso_neto_snapshot_g >= 0",
            name="ck_scm_trabajo_color_peso_neto",
        ),
        sa.CheckConstraint(
            "peso_colada_snapshot_g IS NULL OR peso_colada_snapshot_g >= 0",
            name="ck_scm_trabajo_color_peso_colada",
        ),
        sa.ForeignKeyConstraint(
            ["trabajo_ot_id"],
            ["scm_trabajo_ot.id"],
            name="fk_scm_trabajo_color_trabajo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["corrida_fabricacion_id"],
            ["scm_corrida_fabricacion.id"],
            name="fk_scm_trabajo_color_corrida",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("trabajo_ot_id"),
    )
    op.create_index(
        "ix_scm_trabajo_color_corrida",
        "scm_trabajo_color",
        ["corrida_fabricacion_id"],
    )

    op.create_table(
        "scm_asignacion_personal_trabajo_ot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trabajo_ot_id", sa.Uuid(), nullable=False),
        sa.Column("trabajador_id", sa.Integer(), nullable=False),
        sa.Column(
            "estado", sa.String(length=20),
            server_default="PREVISTA", nullable=False,
        ),
        sa.Column(
            "asignada_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("iniciada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalizada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asignada_por_id", sa.Integer(), nullable=True),
        sa.Column("finalizada_por_id", sa.Integer(), nullable=True),
        sa.Column("motivo", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "estado IN ('PREVISTA', 'ACTIVA', 'CERRADA', 'CANCELADA')",
            name="ck_scm_asignacion_personal_estado",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_asignacion_personal_version",
        ),
        sa.CheckConstraint(
            "(estado IN ('PREVISTA', 'ACTIVA') AND finalizada_at IS NULL) OR "
            "(estado IN ('CERRADA', 'CANCELADA') "
            "AND finalizada_at IS NOT NULL)",
            name="ck_scm_asignacion_personal_intervalo",
        ),
        sa.ForeignKeyConstraint(
            ["trabajo_ot_id"],
            ["scm_trabajo_ot.id"],
            name="fk_scm_asignacion_personal_trabajo",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["trabajador_id"],
            ["trabajador.id"],
            name="fk_scm_asignacion_personal_trabajador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asignada_por_id"],
            ["trabajador.id"],
            name="fk_scm_asignacion_personal_asignador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finalizada_por_id"],
            ["trabajador.id"],
            name="fk_scm_asignacion_personal_finalizador",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "trabajo_ot_id",
            name="uq_scm_asignacion_personal_id_trabajo",
        ),
    )
    op.create_index(
        "ix_scm_asignacion_personal_trabajo",
        "scm_asignacion_personal_trabajo_ot",
        ["trabajo_ot_id"],
    )
    op.create_index(
        "ix_scm_asignacion_personal_trabajador",
        "scm_asignacion_personal_trabajo_ot",
        ["trabajador_id"],
    )
    op.create_index(
        "ix_scm_asignacion_personal_asignada_por",
        "scm_asignacion_personal_trabajo_ot",
        ["asignada_por_id"],
    )
    op.create_index(
        "ix_scm_asignacion_personal_finalizada_por",
        "scm_asignacion_personal_trabajo_ot",
        ["finalizada_por_id"],
    )
    op.create_index(
        "uq_scm_asignacion_personal_activa",
        "scm_asignacion_personal_trabajo_ot",
        ["trabajo_ot_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'ACTIVA'"),
        sqlite_where=sa.text("estado = 'ACTIVA'"),
    )


def _expand_existing_tables():
    op.add_column(
        "registro_diario_produccion",
        sa.Column(
            "secuencia_siguiente_trabajo",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("trabajo_color_contexto_id", sa.Uuid(), nullable=True),
    )
    op.drop_constraint(
        "ck_registro_diario_contexto_ensamble",
        "registro_diario_produccion",
        type_="check",
    )
    op.create_check_constraint(
        "ck_registro_diario_contexto_ensamble",
        "registro_diario_produccion",
        "(modo_ejecucion_ensamble = 'CONCURRENTE' AND ("
        "ot_fabricacion_contexto_id IS NOT NULL OR "
        "trabajo_color_contexto_id IS NOT NULL)) OR "
        "(modo_ejecucion_ensamble IS DISTINCT FROM 'CONCURRENTE' AND "
        "ot_fabricacion_contexto_id IS NULL AND "
        "trabajo_color_contexto_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_registro_diario_secuencia_trabajo",
        "registro_diario_produccion",
        "secuencia_siguiente_trabajo > 0",
    )
    op.create_foreign_key(
        "fk_registro_diario_trabajo_color_contexto",
        "registro_diario_produccion",
        "scm_trabajo_ot",
        ["trabajo_color_contexto_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_registro_diario_trabajo_color_contexto",
        "registro_diario_produccion",
        ["trabajo_color_contexto_id"],
    )

    for table_name in (
        "scm_asignacion_plan_manga_ot",
        "scm_manga",
        "scm_solicitud_manga_extra",
    ):
        op.add_column(
            table_name,
            sa.Column("trabajo_ot_id", sa.Uuid(), nullable=True),
        )

    op.create_foreign_key(
        "fk_scm_asignacion_plan_trabajo_ot",
        "scm_asignacion_plan_manga_ot",
        "scm_trabajo_ot",
        ["trabajo_ot_id", "ot_id"],
        ["id", "orden_trabajo_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scm_manga_trabajo_ot",
        "scm_manga",
        "scm_trabajo_ot",
        ["trabajo_ot_id", "ot_id"],
        ["id", "orden_trabajo_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scm_solicitud_extra_trabajo_ot",
        "scm_solicitud_manga_extra",
        "scm_trabajo_ot",
        ["trabajo_ot_id", "ot_id"],
        ["id", "orden_trabajo_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_scm_asignacion_plan_trabajo",
        "scm_asignacion_plan_manga_ot",
        ["trabajo_ot_id"],
    )
    op.create_index(
        "ix_scm_manga_trabajo",
        "scm_manga",
        ["trabajo_ot_id"],
    )
    op.create_index(
        "ix_scm_solicitud_extra_trabajo",
        "scm_solicitud_manga_extra",
        ["trabajo_ot_id"],
    )

    op.add_column(
        "scm_manga",
        sa.Column(
            "asignacion_personal_trabajo_id", sa.Uuid(), nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_scm_manga_asignacion_personal_trabajo",
        "scm_manga",
        "scm_asignacion_personal_trabajo_ot",
        ["asignacion_personal_trabajo_id", "trabajo_ot_id"],
        ["id", "trabajo_ot_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_scm_manga_asignacion_personal",
        "scm_manga",
        ["asignacion_personal_trabajo_id"],
    )

    op.add_column(
        "scm_pesaje_manga",
        sa.Column(
            "asignacion_personal_trabajo_id", sa.Uuid(), nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_scm_pesaje_asignacion_personal",
        "scm_pesaje_manga",
        "scm_asignacion_personal_trabajo_ot",
        ["asignacion_personal_trabajo_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_scm_pesaje_asignacion_personal",
        "scm_pesaje_manga",
        ["asignacion_personal_trabajo_id"],
    )

    op.drop_constraint(
        "uq_scm_asignacion_plan_ot",
        "scm_asignacion_plan_manga_ot",
        type_="unique",
    )
    op.create_index(
        "uq_scm_asignacion_plan_trabajo",
        "scm_asignacion_plan_manga_ot",
        ["plan_linea_id", "trabajo_ot_id"],
        unique=True,
        postgresql_where=sa.text("trabajo_ot_id IS NOT NULL"),
        sqlite_where=sa.text("trabajo_ot_id IS NOT NULL"),
    )
    op.create_index(
        "uq_scm_asignacion_plan_ot_legacy",
        "scm_asignacion_plan_manga_ot",
        ["plan_linea_id", "ot_id"],
        unique=True,
        postgresql_where=sa.text("trabajo_ot_id IS NULL"),
        sqlite_where=sa.text("trabajo_ot_id IS NULL"),
    )


def _backfill_color_work(connection):
    migration_time = datetime.now(timezone.utc)
    rows = connection.execute(sa.text("""
        SELECT
            ot.id AS orden_trabajo_id,
            ot.codigo_ot,
            ot.estado,
            ot.orden_operacion_id,
            ot.corrida_fabricacion_id,
            ot.cantidad_objetivo,
            ot.cantidad_confirmada,
            ot.iniciada_at,
            ot.cerrada_at,
            ot.created_at,
            ot.updated_at,
            ot.created_by_id,
            ot.maquinista_previsto_id,
            ot.snapshot_cavidades,
            ot.snapshot_peso_neto_gr,
            ot.snapshot_peso_colada_gr,
            ot.colada_inicial,
            ot.colada_final,
            fabricacion.molde_id,
            corrida.color_produccion_id,
            corrida.receta_revision_id,
            corrida.receta_hash,
            color_base.nombre AS color_base_nombre,
            familia_color.nombre AS familia_color_nombre
        FROM registro_diario_produccion AS ot
        JOIN scm_corrida_fabricacion AS corrida
          ON corrida.id = ot.corrida_fabricacion_id
         AND corrida.orden_fabricacion_id = ot.orden_operacion_id
        JOIN scm_orden_fabricacion AS fabricacion
          ON fabricacion.orden_operacion_id = ot.orden_operacion_id
        LEFT JOIN color_produccion AS color
          ON color.id = corrida.color_produccion_id
        LEFT JOIN color_base
          ON color_base.id = color.color_base_id
        LEFT JOIN familia_color
          ON familia_color.id = color.familia_color_id
        WHERE ot.tipo_ot = 'FABRICACION'
          AND ot.codigo_ot_sintetico = false
          AND ot.orden_operacion_id IS NOT NULL
          AND ot.corrida_fabricacion_id IS NOT NULL
        ORDER BY ot.id
    """)).mappings().all()

    work_state = {
        "BORRADOR": "PLANIFICADO",
        "PLANIFICADA": "PLANIFICADO",
        "EN_EJECUCION": "EN_EJECUCION",
        "CERRADA": "COMPLETADO",
        "ANULADA": "ANULADO",
        "MIGRADA_PENDIENTE_CLASIFICACION": "PLANIFICADO",
    }
    assignment_state = {
        "BORRADOR": "PREVISTA",
        "PLANIFICADA": "PREVISTA",
        "EN_EJECUCION": "ACTIVA",
        "CERRADA": "CERRADA",
        "ANULADA": "CANCELADA",
        "MIGRADA_PENDIENTE_CLASIFICACION": "PREVISTA",
    }

    for row in rows:
        trabajo_id = uuid.uuid4()
        created_at = row["created_at"] or migration_time
        updated_at = row["updated_at"] or created_at
        color_parts = (
            row["color_base_nombre"], row["familia_color_nombre"]
        )
        color_name = " ".join(
            part.strip() for part in color_parts if part and part.strip()
        ) or None
        connection.execute(sa.text("""
            INSERT INTO scm_trabajo_ot (
                id, orden_trabajo_id, codigo, tipo, secuencia, estado,
                orden_operacion_id, cantidad_objetivo_un,
                cantidad_confirmada_un, iniciada_at, completada_at,
                created_by_id, created_at, updated_at, version
            )
            VALUES (
                :id, :orden_trabajo_id, :codigo, 'COLOR', 1, :estado,
                :orden_operacion_id, :cantidad_objetivo_un,
                :cantidad_confirmada_un, :iniciada_at, :completada_at,
                :created_by_id, :created_at, :updated_at, 1
            )
        """), {
            "id": trabajo_id,
            "orden_trabajo_id": row["orden_trabajo_id"],
            "codigo": f"{row['codigo_ot']}-TC01",
            "estado": work_state[row["estado"]],
            "orden_operacion_id": row["orden_operacion_id"],
            "cantidad_objetivo_un": row["cantidad_objetivo"] or 0,
            "cantidad_confirmada_un": row["cantidad_confirmada"] or 0,
            "iniciada_at": row["iniciada_at"],
            "completada_at": (
                row["cerrada_at"] if row["estado"] == "CERRADA" else None
            ),
            "created_by_id": row["created_by_id"],
            "created_at": created_at,
            "updated_at": updated_at,
        })
        connection.execute(sa.text("""
            INSERT INTO scm_trabajo_color (
                trabajo_ot_id, corrida_fabricacion_id,
                molde_codigo_snapshot, color_id_snapshot,
                color_nombre_snapshot, receta_revision_id_snapshot,
                receta_hash_snapshot, cavidades_snapshot,
                peso_neto_snapshot_g, peso_colada_snapshot_g,
                colada_inicial, colada_final
            )
            VALUES (
                :trabajo_ot_id, :corrida_fabricacion_id,
                :molde_codigo_snapshot, :color_id_snapshot,
                :color_nombre_snapshot, :receta_revision_id_snapshot,
                :receta_hash_snapshot, :cavidades_snapshot,
                :peso_neto_snapshot_g, :peso_colada_snapshot_g,
                :colada_inicial, :colada_final
            )
        """), {
            "trabajo_ot_id": trabajo_id,
            "corrida_fabricacion_id": row["corrida_fabricacion_id"],
            "molde_codigo_snapshot": row["molde_id"],
            "color_id_snapshot": row["color_produccion_id"],
            "color_nombre_snapshot": (
                color_name[:120] if color_name is not None else None
            ),
            "receta_revision_id_snapshot": row["receta_revision_id"],
            "receta_hash_snapshot": row["receta_hash"],
            "cavidades_snapshot": row["snapshot_cavidades"],
            "peso_neto_snapshot_g": row["snapshot_peso_neto_gr"],
            "peso_colada_snapshot_g": row["snapshot_peso_colada_gr"],
            "colada_inicial": row["colada_inicial"],
            "colada_final": row["colada_final"],
        })

        asignacion_id = None
        if row["maquinista_previsto_id"] is not None:
            asignacion_id = uuid.uuid4()
            estado_asignacion = assignment_state[row["estado"]]
            iniciada_at = None
            finalizada_at = None
            if estado_asignacion in {"ACTIVA", "CERRADA"}:
                iniciada_at = row["iniciada_at"] or created_at
            if estado_asignacion == "CERRADA":
                finalizada_at = row["cerrada_at"] or updated_at
            elif estado_asignacion == "CANCELADA":
                finalizada_at = updated_at
            connection.execute(sa.text("""
                INSERT INTO scm_asignacion_personal_trabajo_ot (
                    id, trabajo_ot_id, trabajador_id, estado,
                    asignada_at, iniciada_at, finalizada_at,
                    asignada_por_id, version
                )
                VALUES (
                    :id, :trabajo_ot_id, :trabajador_id, :estado,
                    :asignada_at, :iniciada_at, :finalizada_at,
                    :asignada_por_id, 1
                )
            """), {
                "id": asignacion_id,
                "trabajo_ot_id": trabajo_id,
                "trabajador_id": row["maquinista_previsto_id"],
                "estado": estado_asignacion,
                "asignada_at": created_at,
                "iniciada_at": iniciada_at,
                "finalizada_at": finalizada_at,
                "asignada_por_id": row["created_by_id"],
            })

        params = {
            "trabajo_id": trabajo_id,
            "orden_trabajo_id": row["orden_trabajo_id"],
            "asignacion_id": asignacion_id,
        }
        connection.execute(sa.text("""
            UPDATE scm_asignacion_plan_manga_ot
            SET trabajo_ot_id = :trabajo_id
            WHERE ot_id = :orden_trabajo_id
        """), params)
        connection.execute(sa.text("""
            UPDATE scm_manga
            SET trabajo_ot_id = :trabajo_id,
                asignacion_personal_trabajo_id = :asignacion_id
            WHERE ot_id = :orden_trabajo_id
        """), params)
        connection.execute(sa.text("""
            UPDATE scm_solicitud_manga_extra
            SET trabajo_ot_id = :trabajo_id
            WHERE ot_id = :orden_trabajo_id
        """), params)
        if asignacion_id is not None:
            connection.execute(sa.text("""
                UPDATE scm_pesaje_manga AS pesaje
                SET asignacion_personal_trabajo_id = :asignacion_id
                FROM scm_manga AS manga
                WHERE pesaje.manga_id = manga.id
                  AND manga.ot_id = :orden_trabajo_id
            """), params)

        connection.execute(sa.text("""
            UPDATE registro_diario_produccion
            SET secuencia_siguiente_trabajo = 2
            WHERE id = :orden_trabajo_id
        """), params)

    connection.execute(sa.text("""
        UPDATE registro_diario_produccion AS armado
        SET trabajo_color_contexto_id = trabajo.id
        FROM registro_diario_produccion AS fabricacion
        JOIN scm_trabajo_ot AS trabajo
          ON trabajo.orden_trabajo_id = fabricacion.id
         AND trabajo.tipo = 'COLOR'
        WHERE armado.modo_ejecucion_ensamble = 'CONCURRENTE'
          AND armado.ot_fabricacion_contexto_id = fabricacion.public_id
    """))

    crossed_pairs = connection.execute(sa.text("""
        SELECT count(*)
        FROM (
            SELECT asignacion.id
            FROM scm_asignacion_plan_manga_ot AS asignacion
            JOIN scm_trabajo_ot AS trabajo
              ON trabajo.id = asignacion.trabajo_ot_id
            WHERE trabajo.orden_trabajo_id <> asignacion.ot_id
            UNION ALL
            SELECT manga.id
            FROM scm_manga AS manga
            JOIN scm_trabajo_ot AS trabajo
              ON trabajo.id = manga.trabajo_ot_id
            WHERE trabajo.orden_trabajo_id <> manga.ot_id
            UNION ALL
            SELECT solicitud.id
            FROM scm_solicitud_manga_extra AS solicitud
            JOIN scm_trabajo_ot AS trabajo
              ON trabajo.id = solicitud.trabajo_ot_id
            WHERE trabajo.orden_trabajo_id <> solicitud.ot_id
        ) AS inconsistencias
    """)).scalar_one()
    if crossed_pairs:
        raise RuntimeError(
            "SCM_TRABAJO_COLOR_BACKFILL_CRUZADO: existen dependencias "
            "asociadas a un trabajo de otra OT"
        )


def upgrade():
    connection = op.get_bind()
    _assert_backfill_preconditions(connection)
    _create_tables()
    _expand_existing_tables()
    _backfill_color_work(connection)
    op.create_index(
        "uq_registro_ot_fabricacion_recurso_turno_activa",
        "registro_diario_produccion",
        ["maquina_id", "fecha", "turno"],
        unique=True,
        postgresql_where=sa.text(NEW_FABRICATION_HEADER),
        sqlite_where=sa.text(NEW_FABRICATION_HEADER),
    )
    if connection.dialect.name == "postgresql":
        for table_name in (
            "scm_trabajo_ot",
            "scm_trabajo_color",
            "scm_asignacion_personal_trabajo_ot",
        ):
            op.execute(
                f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"
            )


def _assert_downgrade_is_lossless(connection):
    new_header = connection.execute(sa.text("""
        SELECT codigo_ot
        FROM registro_diario_produccion
        WHERE tipo_ot = 'FABRICACION'
          AND codigo_ot_sintetico = false
          AND orden_id IS NULL
          AND orden_operacion_id IS NULL
          AND corrida_fabricacion_id IS NULL
        ORDER BY id
        LIMIT 1
    """)).scalar_one_or_none()
    if new_header is not None:
        raise RuntimeError(
            "SCM_TRABAJO_COLOR_DOWNGRADE_BLOCKED: la cabecera nueva "
            f"{new_header} no puede representarse en f77e6f1b4c98"
        )

    invalid_work_count = connection.execute(sa.text("""
        SELECT ot.codigo_ot, count(trabajo.id)
        FROM registro_diario_produccion AS ot
        LEFT JOIN scm_trabajo_ot AS trabajo
          ON trabajo.orden_trabajo_id = ot.id
        WHERE ot.tipo_ot = 'FABRICACION'
          AND ot.codigo_ot_sintetico = false
          AND ot.orden_operacion_id IS NOT NULL
          AND ot.corrida_fabricacion_id IS NOT NULL
        GROUP BY ot.id, ot.codigo_ot
        HAVING count(trabajo.id) <> 1
        ORDER BY ot.id
        LIMIT 1
    """)).first()
    if invalid_work_count is not None:
        raise RuntimeError(
            "SCM_TRABAJO_COLOR_DOWNGRADE_BLOCKED: "
            f"la OT {invalid_work_count[0]} tiene "
            f"{invalid_work_count[1]} trabajos; se exige exactamente uno"
        )

    non_representable_work = connection.execute(sa.text("""
        SELECT trabajo.codigo
        FROM scm_trabajo_ot AS trabajo
        JOIN registro_diario_produccion AS ot
          ON ot.id = trabajo.orden_trabajo_id
        LEFT JOIN scm_trabajo_color AS color
          ON color.trabajo_ot_id = trabajo.id
        WHERE ot.tipo_ot <> 'FABRICACION'
           OR ot.codigo_ot_sintetico = true
           OR ot.orden_operacion_id IS NULL
           OR ot.corrida_fabricacion_id IS NULL
           OR trabajo.tipo <> 'COLOR'
           OR trabajo.secuencia <> 1
           OR trabajo.codigo <> ot.codigo_ot || '-TC01'
           OR trabajo.continua_de_id IS NOT NULL
           OR trabajo.orden_operacion_id IS DISTINCT FROM ot.orden_operacion_id
           OR color.corrida_fabricacion_id IS DISTINCT FROM ot.corrida_fabricacion_id
           OR trabajo.cantidad_objetivo_un IS DISTINCT FROM
              coalesce(ot.cantidad_objetivo, 0)
           OR trabajo.cantidad_confirmada_un IS DISTINCT FROM
              coalesce(ot.cantidad_confirmada, 0)
           OR trabajo.estado IS DISTINCT FROM CASE ot.estado
                WHEN 'BORRADOR' THEN 'PLANIFICADO'
                WHEN 'PLANIFICADA' THEN 'PLANIFICADO'
                WHEN 'EN_EJECUCION' THEN 'EN_EJECUCION'
                WHEN 'CERRADA' THEN 'COMPLETADO'
                WHEN 'ANULADA' THEN 'ANULADO'
                WHEN 'MIGRADA_PENDIENTE_CLASIFICACION' THEN 'PLANIFICADO'
              END
        ORDER BY trabajo.codigo
        LIMIT 1
    """)).scalar_one_or_none()
    if non_representable_work is not None:
        raise RuntimeError(
            "SCM_TRABAJO_COLOR_DOWNGRADE_BLOCKED: el trabajo "
            f"{non_representable_work} contiene estado o genealogia "
            "no representable por la OT anterior"
        )

    assignment_history = connection.execute(sa.text("""
        SELECT trabajo.codigo, count(asignacion.id)
        FROM scm_trabajo_ot AS trabajo
        JOIN scm_asignacion_personal_trabajo_ot AS asignacion
          ON asignacion.trabajo_ot_id = trabajo.id
        GROUP BY trabajo.id, trabajo.codigo
        HAVING count(asignacion.id) > 1
        ORDER BY trabajo.codigo
        LIMIT 1
    """)).first()
    if assignment_history is not None:
        raise RuntimeError(
            "SCM_TRABAJO_COLOR_DOWNGRADE_BLOCKED: el trabajo "
            f"{assignment_history[0]} posee {assignment_history[1]} "
            "asignaciones de personal"
        )

    non_representable_assignment = connection.execute(sa.text("""
        SELECT trabajo.codigo
        FROM scm_asignacion_personal_trabajo_ot AS asignacion
        JOIN scm_trabajo_ot AS trabajo
          ON trabajo.id = asignacion.trabajo_ot_id
        JOIN registro_diario_produccion AS ot
          ON ot.id = trabajo.orden_trabajo_id
        WHERE asignacion.trabajador_id IS DISTINCT FROM
              ot.maquinista_previsto_id
           OR asignacion.estado IS DISTINCT FROM CASE ot.estado
                WHEN 'BORRADOR' THEN 'PREVISTA'
                WHEN 'PLANIFICADA' THEN 'PREVISTA'
                WHEN 'EN_EJECUCION' THEN 'ACTIVA'
                WHEN 'CERRADA' THEN 'CERRADA'
                WHEN 'ANULADA' THEN 'CANCELADA'
                WHEN 'MIGRADA_PENDIENTE_CLASIFICACION' THEN 'PREVISTA'
              END
        ORDER BY trabajo.codigo
        LIMIT 1
    """)).scalar_one_or_none()
    if non_representable_assignment is not None:
        raise RuntimeError(
            "SCM_TRABAJO_COLOR_DOWNGRADE_BLOCKED: la asignacion de "
            f"{non_representable_assignment} no coincide con el "
            "maquinista/estado representable en la OT anterior"
        )


def downgrade():
    connection = op.get_bind()
    _assert_downgrade_is_lossless(connection)
    duplicated_legacy_assignments = connection.execute(sa.text("""
        SELECT 1
        FROM scm_asignacion_plan_manga_ot
        GROUP BY plan_linea_id, ot_id
        HAVING count(*) > 1
        LIMIT 1
    """)).scalar_one_or_none()
    if duplicated_legacy_assignments is not None:
        raise RuntimeError(
            "SCM_TRABAJO_COLOR_DOWNGRADE_BLOCKED: una linea de plan esta "
            "asignada a varios trabajos de la misma OT"
        )

    op.drop_index(
        "uq_registro_ot_fabricacion_recurso_turno_activa",
        table_name="registro_diario_produccion",
    )
    op.drop_index(
        "uq_scm_asignacion_plan_ot_legacy",
        table_name="scm_asignacion_plan_manga_ot",
    )
    op.drop_index(
        "uq_scm_asignacion_plan_trabajo",
        table_name="scm_asignacion_plan_manga_ot",
    )
    op.create_unique_constraint(
        "uq_scm_asignacion_plan_ot",
        "scm_asignacion_plan_manga_ot",
        ["plan_linea_id", "ot_id"],
    )

    op.drop_index(
        "ix_scm_pesaje_asignacion_personal",
        table_name="scm_pesaje_manga",
    )
    op.drop_constraint(
        "fk_scm_pesaje_asignacion_personal",
        "scm_pesaje_manga",
        type_="foreignkey",
    )
    op.drop_column(
        "scm_pesaje_manga", "asignacion_personal_trabajo_id"
    )

    op.drop_index(
        "ix_scm_manga_asignacion_personal",
        table_name="scm_manga",
    )
    op.drop_constraint(
        "fk_scm_manga_asignacion_personal_trabajo",
        "scm_manga",
        type_="foreignkey",
    )
    op.drop_column("scm_manga", "asignacion_personal_trabajo_id")

    op.drop_index(
        "ix_scm_solicitud_extra_trabajo",
        table_name="scm_solicitud_manga_extra",
    )
    op.drop_index(
        "ix_scm_manga_trabajo",
        table_name="scm_manga",
    )
    op.drop_index(
        "ix_scm_asignacion_plan_trabajo",
        table_name="scm_asignacion_plan_manga_ot",
    )
    op.drop_constraint(
        "fk_scm_solicitud_extra_trabajo_ot",
        "scm_solicitud_manga_extra",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_scm_manga_trabajo_ot",
        "scm_manga",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_scm_asignacion_plan_trabajo_ot",
        "scm_asignacion_plan_manga_ot",
        type_="foreignkey",
    )
    op.drop_column("scm_solicitud_manga_extra", "trabajo_ot_id")
    op.drop_column("scm_manga", "trabajo_ot_id")
    op.drop_column("scm_asignacion_plan_manga_ot", "trabajo_ot_id")

    op.drop_index(
        "ix_registro_diario_trabajo_color_contexto",
        table_name="registro_diario_produccion",
    )
    op.drop_constraint(
        "fk_registro_diario_trabajo_color_contexto",
        "registro_diario_produccion",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_registro_diario_contexto_ensamble",
        "registro_diario_produccion",
        type_="check",
    )
    op.drop_column(
        "registro_diario_produccion", "trabajo_color_contexto_id"
    )
    op.create_check_constraint(
        "ck_registro_diario_contexto_ensamble",
        "registro_diario_produccion",
        "(modo_ejecucion_ensamble = 'CONCURRENTE' AND "
        "ot_fabricacion_contexto_id IS NOT NULL) OR "
        "(modo_ejecucion_ensamble IS DISTINCT FROM 'CONCURRENTE' AND "
        "ot_fabricacion_contexto_id IS NULL)",
    )

    op.drop_table("scm_asignacion_personal_trabajo_ot")
    op.drop_table("scm_trabajo_color")
    op.drop_table("scm_trabajo_ot")

    op.drop_constraint(
        "ck_registro_diario_secuencia_trabajo",
        "registro_diario_produccion",
        type_="check",
    )
    op.drop_column(
        "registro_diario_produccion", "secuencia_siguiente_trabajo"
    )
