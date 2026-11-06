"""Version KG OF closures and add the append-only reopening ledger.

Existing rows are the first current closure for their document.  A downgrade
is refused once the new history semantics have been used.
"""

from alembic import op
import sqlalchemy as sa


revision = "fc4e5f607182"
down_revision = "fb7c8d9e0a12"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scm_cierre_productivo_kg") as batch:
        batch.drop_constraint("uq_scm_cierre_kg_documento", type_="unique")
        batch.add_column(
            sa.Column("secuencia", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("vigente", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.create_unique_constraint(
            "uq_scm_cierre_kg_documento_secuencia",
            ["documento_tipo", "documento_id", "secuencia"],
        )

    op.create_index(
        "uq_scm_cierre_kg_documento_vigente",
        "scm_cierre_productivo_kg",
        ["documento_tipo", "documento_id"],
        unique=True,
        postgresql_where=sa.text("vigente IS TRUE"),
        sqlite_where=sa.text("vigente = 1"),
    )
    op.create_table(
        "scm_reapertura_productiva_kg",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("documento_tipo", sa.String(length=2), nullable=False),
        sa.Column("documento_id", sa.String(length=64), nullable=False),
        sa.Column("cierre_previo_id", sa.Integer(), nullable=False),
        sa.Column("secuencia_previa", sa.Integer(), nullable=False),
        sa.Column("version_previa", sa.Integer(), nullable=False),
        sa.Column("version_resultante", sa.Integer(), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("documento_tipo = 'OF'", name="ck_scm_reapertura_kg_documento_tipo"),
        sa.ForeignKeyConstraint(["cierre_previo_id"], ["scm_cierre_productivo_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_reapertura_kg_public"),
        sa.UniqueConstraint("operation_id", name="uq_scm_reapertura_kg_operation"),
    )
    op.create_index(
        "ix_scm_reapertura_kg_documento",
        "scm_reapertura_productiva_kg",
        ["documento_tipo", "documento_id"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM scm_reapertura_productiva_kg")).scalar_one():
        raise RuntimeError("downgrade KG reopen bloqueado: existen reaperturas")
    if bind.execute(sa.text(
        "SELECT COUNT(*) FROM scm_cierre_productivo_kg "
        "WHERE secuencia <> 1 OR vigente IS NOT TRUE"
    )).scalar_one():
        raise RuntimeError("downgrade KG reopen bloqueado: existe historial versionado")

    op.drop_index("ix_scm_reapertura_kg_documento", table_name="scm_reapertura_productiva_kg")
    op.drop_table("scm_reapertura_productiva_kg")
    op.drop_index("uq_scm_cierre_kg_documento_vigente", table_name="scm_cierre_productivo_kg")
    with op.batch_alter_table("scm_cierre_productivo_kg") as batch:
        batch.drop_constraint("uq_scm_cierre_kg_documento_secuencia", type_="unique")
        batch.drop_column("vigente")
        batch.drop_column("secuencia")
        batch.create_unique_constraint(
            "uq_scm_cierre_kg_documento",
            ["documento_tipo", "documento_id"],
        )
