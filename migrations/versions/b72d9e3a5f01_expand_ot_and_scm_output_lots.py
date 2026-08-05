"""expand canonical OT identity and add strong SCM output lots

Revision ID: b72d9e3a5f01
Revises: a61c8d2f4e90
Create Date: 2026-07-28 09:00:00.000000
"""
import uuid

from alembic import op
import sqlalchemy as sa


revision = "b72d9e3a5f01"
down_revision = "a61c8d2f4e90"
branch_labels = None
depends_on = None

LEGACY_NAMESPACE = uuid.UUID("42982d44-41d5-5dd8-b99b-d95862946673")


def upgrade():
    connection = op.get_bind()
    op.add_column(
        "registro_diario_produccion",
        sa.Column("public_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("codigo_ot", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column(
            "codigo_ot_sintetico",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("estado", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column(
            "timezone_snapshot",
            sa.String(length=64),
            server_default="America/Lima",
            nullable=False,
        ),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("created_at_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("iniciada_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("cerrada_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("created_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("maquinista_previsto_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "registro_diario_produccion",
        sa.Column(
            "secuencia_siguiente_manga",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )

    rows = connection.execute(sa.text(
        "SELECT id FROM registro_diario_produccion ORDER BY id"
    )).all()
    for (row_id,) in rows:
        public_id = uuid.uuid5(LEGACY_NAMESPACE, f"registro:{row_id}")
        connection.execute(sa.text("""
            UPDATE registro_diario_produccion
               SET public_id = :public_id,
                   codigo_ot = :code,
                   codigo_ot_sintetico = true,
                   estado = 'MIGRADA_PENDIENTE_CLASIFICACION',
                   created_at_source = 'LEGACY_NO_DISPONIBLE'
             WHERE id = :row_id
        """), {
            "public_id": public_id,
            "code": f"OT-LEGACY-{row_id}",
            "row_id": row_id,
        })

    op.alter_column(
        "registro_diario_produccion", "public_id", nullable=False
    )
    op.alter_column(
        "registro_diario_produccion", "codigo_ot", nullable=False
    )
    op.alter_column(
        "registro_diario_produccion", "estado", nullable=False,
        server_default="BORRADOR",
    )
    op.alter_column(
        "registro_diario_produccion", "created_at_source", nullable=False,
        server_default="CENTRAL",
    )
    op.create_unique_constraint(
        "uq_registro_diario_public_id",
        "registro_diario_produccion",
        ["public_id"],
    )
    op.create_unique_constraint(
        "uq_registro_diario_codigo_ot",
        "registro_diario_produccion",
        ["codigo_ot"],
    )
    op.create_check_constraint(
        "ck_registro_diario_estado_ot",
        "registro_diario_produccion",
        "estado IN ('BORRADOR', 'PLANIFICADA', 'EN_EJECUCION', "
        "'CERRADA', 'ANULADA', 'MIGRADA_PENDIENTE_CLASIFICACION')",
    )
    op.create_check_constraint(
        "ck_registro_diario_version_ot",
        "registro_diario_produccion",
        "version > 0",
    )
    op.create_check_constraint(
        "ck_registro_diario_secuencia_manga",
        "registro_diario_produccion",
        "secuencia_siguiente_manga > 0",
    )
    op.create_foreign_key(
        "fk_registro_diario_created_by",
        "registro_diario_produccion",
        "trabajador",
        ["created_by_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_registro_diario_maquinista_previsto",
        "registro_diario_produccion",
        "trabajador",
        ["maquinista_previsto_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "scm_lote_articulo",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("articulo_id", sa.Integer(), nullable=False),
        sa.Column(
            "clase",
            sa.String(length=40),
            server_default="LOTE_SALIDA_PIEZA_COLOR",
            nullable=False,
        ),
        sa.Column(
            "lote_salida_pieza_color_id", sa.Integer(), nullable=False
        ),
        sa.Column(
            "cantidad_acreditada",
            sa.Numeric(precision=15, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "estado_calidad",
            sa.String(length=32),
            server_default="PLANIFICADO",
            nullable=False,
        ),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "record_time",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "clase = 'LOTE_SALIDA_PIEZA_COLOR'",
            name="ck_scm_lote_articulo_clase_c_core",
        ),
        sa.CheckConstraint(
            "cantidad_acreditada >= 0",
            name="ck_scm_lote_articulo_cantidad",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_id"], ["scm_articulo.id"],
            name="fk_scm_lote_articulo_articulo", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lote_salida_pieza_color_id"],
            ["lote_salida_pieza_color.id"],
            name="fk_scm_lote_articulo_salida", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["trabajador.id"],
            name="fk_scm_lote_articulo_actor", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "public_id", name="uq_scm_lote_articulo_public_id"
        ),
        sa.UniqueConstraint("codigo", name="uq_scm_lote_articulo_codigo"),
        sa.UniqueConstraint(
            "lote_salida_pieza_color_id",
            name="uq_scm_lote_articulo_salida",
        ),
    )

    op.execute(sa.text("""
        INSERT INTO correlativo_catalogo (
            clave, prefijo, siguiente_valor, ancho
        )
        SELECT 'ORDEN_TRABAJO', 'OT', 1, 6
        WHERE NOT EXISTS (
            SELECT 1 FROM correlativo_catalogo
            WHERE clave = 'ORDEN_TRABAJO'
        )
    """))


def downgrade():
    connection = op.get_bind()
    count = connection.execute(sa.text(
        "SELECT count(*) FROM scm_lote_articulo"
    )).scalar_one()
    if count:
        raise RuntimeError(
            "SCM_OT_DOWNGRADE_BLOCKED: existen lotes SCM del corte C"
        )
    op.drop_table("scm_lote_articulo")
    op.drop_constraint(
        "fk_registro_diario_maquinista_previsto",
        "registro_diario_produccion", type_="foreignkey",
    )
    op.drop_constraint(
        "fk_registro_diario_created_by",
        "registro_diario_produccion", type_="foreignkey",
    )
    op.drop_constraint(
        "ck_registro_diario_secuencia_manga",
        "registro_diario_produccion", type_="check",
    )
    op.drop_constraint(
        "ck_registro_diario_version_ot",
        "registro_diario_produccion", type_="check",
    )
    op.drop_constraint(
        "ck_registro_diario_estado_ot",
        "registro_diario_produccion", type_="check",
    )
    op.drop_constraint(
        "uq_registro_diario_codigo_ot",
        "registro_diario_produccion", type_="unique",
    )
    op.drop_constraint(
        "uq_registro_diario_public_id",
        "registro_diario_produccion", type_="unique",
    )
    for column in (
        "secuencia_siguiente_manga", "version", "maquinista_previsto_id",
        "created_by_id", "cerrada_at", "iniciada_at", "updated_at",
        "created_at_source", "created_at", "timezone_snapshot", "estado",
        "codigo_ot_sintetico", "codigo_ot", "public_id",
    ):
        op.drop_column("registro_diario_produccion", column)
