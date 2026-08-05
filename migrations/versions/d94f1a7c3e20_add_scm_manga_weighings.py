"""add SCM manga weighings and post-weigh labels

Revision ID: d94f1a7c3e20
Revises: c83e0f4b6a12
"""

from alembic import op
import sqlalchemy as sa


revision = "d94f1a7c3e20"
down_revision = "c83e0f4b6a12"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_scm_etiqueta_manga_tipo_c_core",
        "scm_etiqueta_manga",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_etiqueta_manga_tipo",
        "scm_etiqueta_manga",
        "tipo IN ('PREPESAJE', 'POSTPESAJE')",
    )
    op.create_table(
        "scm_pesaje_manga",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("source_system", sa.String(40), nullable=False),
        sa.Column("station_id", sa.String(36), nullable=False),
        sa.Column("capture_id", sa.Uuid(), nullable=False),
        sa.Column("peso_bruto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("tara_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("peso_fisico_neto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("tara_fuente", sa.String(28), nullable=False),
        sa.Column("cantidad_confirmada", sa.Numeric(15, 3), nullable=False),
        sa.Column(
            "fuente_cantidad", sa.String(36),
            server_default="PLAN_CONFIRMADO_POR_PESAJE", nullable=False,
        ),
        sa.Column("kg_produccion_ot", sa.Numeric(15, 3), nullable=False),
        sa.Column("pesada_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "timezone_snapshot", sa.String(64),
            server_default="America/Lima", nullable=False,
        ),
        sa.Column("fecha_local_pesaje", sa.Date(), nullable=False),
        sa.Column("dias_desfase_operativo", sa.Integer(), nullable=False),
        sa.Column(
            "alerta_fecha", sa.Boolean(),
            server_default=sa.false(), nullable=False,
        ),
        sa.Column("motivo_desfase_texto", sa.String(500), nullable=True),
        sa.Column("pesado_por_id", sa.Integer(), nullable=False),
        sa.Column("snapshots_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 "
            "AND peso_fisico_neto_kg > 0",
            name="ck_scm_pesaje_manga_pesos_positivos",
        ),
        sa.CheckConstraint(
            "peso_fisico_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_pesaje_manga_neto",
        ),
        sa.CheckConstraint(
            "tara_fuente IN ('TIPO_MANGA', 'MEDIDA_AUTORIZADA', "
            "'CORRECCION')",
            name="ck_scm_pesaje_manga_tara_fuente",
        ),
        sa.CheckConstraint(
            "fuente_cantidad IN ('PLAN_CONFIRMADO_POR_PESAJE', "
            "'CORRECCION_AUTORIZADA')",
            name="ck_scm_pesaje_manga_fuente_cantidad",
        ),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["station_id"], ["estacion_pesaje.station_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["pesado_por_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_pesaje_manga_public_id"),
        sa.UniqueConstraint("manga_id", name="uq_scm_pesaje_manga_manga"),
        sa.UniqueConstraint(
            "operation_id", name="uq_scm_pesaje_manga_operation"
        ),
        sa.UniqueConstraint(
            "source_system", "capture_id",
            name="uq_scm_pesaje_manga_capture",
        ),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT count(*) FROM scm_pesaje_manga")
    ).scalar_one():
        raise RuntimeError("SCM_PESAJE_DOWNGRADE_BLOCKED: existen pesajes")
    op.drop_table("scm_pesaje_manga")
    op.drop_constraint(
        "ck_scm_etiqueta_manga_tipo",
        "scm_etiqueta_manga",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scm_etiqueta_manga_tipo_c_core",
        "scm_etiqueta_manga",
        "tipo = 'PREPESAJE'",
    )
