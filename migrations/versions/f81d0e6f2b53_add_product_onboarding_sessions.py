"""add durable product onboarding sessions

Revision ID: f81d0e6f2b53
Revises: f80c9d5e1a42
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f81d0e6f2b53"
down_revision = "f80c9d5e1a42"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _protect_table_on_postgres(connection):
    if connection.dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE scm_alta_producto_sesion ENABLE ROW LEVEL SECURITY"
    )
    # El backend Flask es la unica autoridad de este agregado. La Data API
    # queda cerrada tanto para usuarios anonimos como autenticados; no se crea
    # una politica permisiva basada solo en autenticacion.
    op.execute(sa.text("""
        DO $body$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL PRIVILEGES ON TABLE scm_alta_producto_sesion
              FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL PRIVILEGES ON TABLE scm_alta_producto_sesion
              FROM authenticated;
          END IF;
        END
        $body$;
    """))


def upgrade():
    connection = op.get_bind()
    json_type = _json_type()
    op.create_table(
        "scm_alta_producto_sesion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("producto_terminado_id", sa.String(50), nullable=True),
        sa.Column(
            "estado",
            sa.String(32),
            server_default="BORRADOR",
            nullable=False,
        ),
        sa.Column(
            "paso_actual",
            sa.String(32),
            server_default="IDENTIDAD",
            nullable=False,
        ),
        sa.Column(
            "borrador_json",
            json_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "estados_paso_json",
            json_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "bloqueos_paso_json",
            json_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "fuentes_json",
            json_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "referencias_json",
            json_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "readiness_json",
            json_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "invalidated_steps_json",
            json_type,
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "application_journal_json",
            json_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("creada_por_id", sa.Integer(), nullable=False),
        sa.Column("actualizada_por_id", sa.Integer(), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finalizada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandonada_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "estado IN ('BORRADOR', 'CON_BLOQUEOS', "
            "'LISTA_PARA_PUBLICAR', 'FINALIZADA', 'ABANDONADA')",
            name="ck_scm_alta_producto_estado",
        ),
        sa.CheckConstraint(
            "paso_actual IN ('IDENTIDAD', 'COMPONENTES', 'COLORES', "
            "'ESTRUCTURA', 'RUTA_EMPAQUE', 'REVISION')",
            name="ck_scm_alta_producto_paso_actual",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_alta_producto_version",
        ),
        sa.ForeignKeyConstraint(
            ["producto_terminado_id"],
            ["producto_terminado.cod_sku_pt"],
            name="fk_scm_alta_producto_producto",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["creada_por_id"],
            ["trabajador.id"],
            name="fk_scm_alta_producto_creada_por",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actualizada_por_id"],
            ["trabajador.id"],
            name="fk_scm_alta_producto_actualizada_por",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scm_alta_producto_estado_actualizada",
        "scm_alta_producto_sesion",
        ["estado", "updated_at"],
    )
    op.create_index(
        "ix_scm_alta_producto_producto",
        "scm_alta_producto_sesion",
        ["producto_terminado_id"],
    )
    op.create_index(
        "ix_scm_alta_producto_creada_por",
        "scm_alta_producto_sesion",
        ["creada_por_id"],
    )
    op.create_index(
        "ix_scm_alta_producto_actualizada_por",
        "scm_alta_producto_sesion",
        ["actualizada_por_id"],
    )
    _protect_table_on_postgres(connection)


def downgrade():
    op.drop_index(
        "ix_scm_alta_producto_actualizada_por",
        table_name="scm_alta_producto_sesion",
    )
    op.drop_index(
        "ix_scm_alta_producto_creada_por",
        table_name="scm_alta_producto_sesion",
    )
    op.drop_index(
        "ix_scm_alta_producto_producto",
        table_name="scm_alta_producto_sesion",
    )
    op.drop_index(
        "ix_scm_alta_producto_estado_actualizada",
        table_name="scm_alta_producto_sesion",
    )
    op.drop_table("scm_alta_producto_sesion")
