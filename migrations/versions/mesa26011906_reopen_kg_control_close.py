"""Audit reopening of a KG manga closed from a measured control."""

from alembic import op
import sqlalchemy as sa


revision = "mesa26011906"
down_revision = "mesa26011905"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_reapertura_control_peso_kg",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("control_id", sa.Integer(), nullable=False),
        sa.Column("tipo_reapertura", sa.String(length=28), nullable=False,
                  server_default="CONTINUAR_LLENADO"),
        sa.Column("peso_base_neto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=False),
        sa.Column("evidencia", sa.String(length=500), nullable=True),
        sa.Column("reabierta_por_id", sa.Integer(), nullable=False),
        sa.Column("reabierta_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "tipo_reapertura = 'CONTINUAR_LLENADO'",
            name="ck_scm_reapertura_control_kg_tipo",
        ),
        sa.CheckConstraint(
            "peso_base_neto_kg > 0",
            name="ck_scm_reapertura_control_kg_base",
        ),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["control_id"], ["scm_control_peso_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reabierta_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_reapertura_control_kg_public"),
        sa.UniqueConstraint("control_id", name="uq_scm_reapertura_control_kg_control"),
        sa.UniqueConstraint("operation_id", name="uq_scm_reapertura_control_kg_operation"),
    )
    op.create_index(
        "ix_scm_reapertura_control_kg_manga",
        "scm_reapertura_control_peso_kg",
        ["manga_id", "reabierta_at"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT COUNT(*) FROM scm_reapertura_control_peso_kg"
    )).scalar_one():
        raise RuntimeError(
            "downgrade KG control reopen bloqueado: existen reaperturas"
        )
    op.drop_index(
        "ix_scm_reapertura_control_kg_manga",
        table_name="scm_reapertura_control_peso_kg",
    )
    op.drop_table("scm_reapertura_control_peso_kg")
