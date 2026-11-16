"""Add shared central quality visit drafts and confirmations."""

from alembic import op
import sqlalchemy as sa


revision = "fg718293a5b6"
down_revision = "mn31d9e8f701"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_visita_calidad",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ot_id", sa.Integer(), nullable=False),
        sa.Column("trabajo_ot_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=16), nullable=False),
        sa.Column("numero", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(length=16), server_default="DRAFT", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
        sa.Column("confirmed_by_id", sa.Integer(), nullable=True),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("payload_json", sa.JSON(), server_default="{}", nullable=False),
        sa.ForeignKeyConstraint(["ot_id"], ["registro_diario_produccion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["trabajo_ot_id"], ["scm_trabajo_ot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ot_id", "trabajo_ot_id", "tipo", "numero", name="uq_scm_visita_calidad_contexto"),
        sa.UniqueConstraint("operation_id", name="uq_scm_visita_calidad_operation"),
        sa.CheckConstraint("tipo IN ('INICIAL', 'PERIODICA')", name="ck_scm_visita_calidad_tipo"),
        sa.CheckConstraint("estado IN ('DRAFT', 'CONFIRMED')", name="ck_scm_visita_calidad_estado"),
        sa.CheckConstraint("numero > 0", name="ck_scm_visita_calidad_numero"),
    )
    op.create_index(
        "ix_scm_visita_calidad_ot_estado",
        "scm_visita_calidad", ["ot_id", "estado", "tipo"], unique=False,
    )
    op.create_index(
        "uq_scm_visita_calidad_confirmada_ot_numero",
        "scm_visita_calidad", ["ot_id", "numero"], unique=True,
        postgresql_where=sa.text("estado = 'CONFIRMED'"),
        sqlite_where=sa.text("estado = 'CONFIRMED'"),
    )
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        VALUES ('CALIDAD_CONTROL_REGISTRAR', 'Registrar borradores y visitas de Calidad', TRUE)
        ON CONFLICT (codigo) DO UPDATE SET activo = TRUE
    """))
    # This capability is explicitly granted to the confirmed operational roles.
    # GERENTE_GENERAL is granted the complete catalog by the initial seed; the
    # idempotent insert below also covers databases where that seed pre-dates
    # this capability.
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
          FROM rol_operativo rol
          JOIN scm_capacidad capacidad
            ON capacidad.codigo IN (
                'CALIDAD_CONTROL_REGISTRAR', 'OT_VER', 'OF_VER'
            )
         WHERE rol.codigo IN ('CALIDAD', 'SUPERVISOR', 'GERENCIA', 'GERENTE_GENERAL')
        ON CONFLICT DO NOTHING
    """))
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'scm_api') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE scm_visita_calidad TO scm_api;
                END IF;
            END
            $$;
        """))


def downgrade():
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
         WHERE capacidad_id = (
             SELECT id FROM scm_capacidad
              WHERE codigo = 'CALIDAD_CONTROL_REGISTRAR'
         )
    """))
    op.drop_index("uq_scm_visita_calidad_confirmada_ot_numero", table_name="scm_visita_calidad")
    op.drop_index("ix_scm_visita_calidad_ot_estado", table_name="scm_visita_calidad")
    op.drop_table("scm_visita_calidad")
    op.execute(sa.text("DELETE FROM scm_capacidad WHERE codigo = 'CALIDAD_CONTROL_REGISTRAR'"))

