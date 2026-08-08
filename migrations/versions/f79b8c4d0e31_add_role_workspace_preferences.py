"""add role workspace preferences and explicit primary role

Revision ID: f79b8c4d0e31
Revises: f78a7b3c9d20
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "f79b8c4d0e31"
down_revision = "f78a7b3c9d20"
branch_labels = None
depends_on = None


def _backfill_unambiguous_primary_roles():
    op.execute(sa.text("""
        UPDATE trabajador_rol AS assignment
        SET es_principal = true
        WHERE EXISTS (
            SELECT 1
            FROM rol_operativo AS assigned_role
            WHERE assigned_role.id = assignment.rol_operativo_id
              AND assigned_role.activo IS true
        )
          AND 1 = (
            SELECT count(*)
            FROM trabajador_rol AS candidate
            JOIN rol_operativo AS candidate_role
              ON candidate_role.id = candidate.rol_operativo_id
             AND candidate_role.activo IS true
            WHERE candidate.trabajador_id = assignment.trabajador_id
          )
    """))


AUTHORIZATION_TABLES = (
    "rol_operativo",
    "trabajador_rol",
    "scm_capacidad",
    "scm_rol_capacidad",
    "scm_rol_workspace_preferencia",
)


def _protect_authorization_tables_on_postgres(connection):
    if connection.dialect.name != "postgresql":
        return
    for table_name in AUTHORIZATION_TABLES:
        op.execute(
            f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"
        )
    # Supabase roles are not present in every PostgreSQL test/runtime. Revoke
    # direct Data API access only when those roles exist; Flask remains the
    # authority for every read/write of this table.
    op.execute(sa.text("""
        DO $body$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL PRIVILEGES ON TABLE rol_operativo, trabajador_rol,
              scm_capacidad, scm_rol_capacidad,
              scm_rol_workspace_preferencia FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL PRIVILEGES ON TABLE rol_operativo, trabajador_rol,
              scm_capacidad, scm_rol_capacidad,
              scm_rol_workspace_preferencia FROM authenticated;
          END IF;
        END
        $body$;
    """))


def upgrade():
    connection = op.get_bind()

    op.add_column(
        "rol_operativo",
        sa.Column("workspace_focus", sa.Text(), nullable=True),
    )
    op.add_column(
        "rol_operativo",
        sa.Column(
            "workspace_start_feature",
            sa.String(length=80),
            nullable=True,
        ),
    )
    op.add_column(
        "rol_operativo",
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )
    if connection.dialect.name != "sqlite":
        op.create_check_constraint(
            "ck_rol_operativo_workspace_version",
            "rol_operativo",
            "version > 0",
        )

    op.add_column(
        "trabajador_rol",
        sa.Column(
            "es_principal",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    _backfill_unambiguous_primary_roles()
    op.create_index(
        "uq_trabajador_rol_principal",
        "trabajador_rol",
        ["trabajador_id"],
        unique=True,
        postgresql_where=sa.text("es_principal IS true"),
        sqlite_where=sa.text("es_principal IS true"),
    )

    op.create_table(
        "scm_rol_workspace_preferencia",
        sa.Column("rol_operativo_id", sa.Integer(), nullable=False),
        sa.Column("feature_key", sa.String(length=80), nullable=False),
        sa.Column("prioridad", sa.SmallInteger(), nullable=False),
        sa.Column(
            "fijada",
            sa.Boolean(),
            server_default=sa.false(),
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
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "prioridad BETWEEN 0 AND 999",
            name="ck_scm_rol_workspace_preferencia_prioridad",
        ),
        sa.ForeignKeyConstraint(
            ["rol_operativo_id"],
            ["rol_operativo.id"],
            name="fk_scm_rol_workspace_preferencia_rol",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["trabajador.id"],
            name="fk_scm_rol_workspace_preferencia_creador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["trabajador.id"],
            name="fk_scm_rol_workspace_preferencia_actualizador",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("rol_operativo_id", "feature_key"),
    )
    op.create_index(
        "ix_scm_rol_workspace_preferencia_orden",
        "scm_rol_workspace_preferencia",
        ["rol_operativo_id", "fijada", "prioridad", "feature_key"],
    )
    op.create_index(
        "ix_scm_rol_workspace_preferencia_created_by",
        "scm_rol_workspace_preferencia",
        ["created_by_id"],
    )
    op.create_index(
        "ix_scm_rol_workspace_preferencia_updated_by",
        "scm_rol_workspace_preferencia",
        ["updated_by_id"],
    )
    _protect_authorization_tables_on_postgres(connection)


def downgrade():
    connection = op.get_bind()
    # RLS and revoked Data API grants on the pre-existing authorization
    # tables are intentionally not reverted. We do not know their historical
    # ACLs, so a downgrade remains fail-closed instead of inventing grants.
    op.drop_index(
        "ix_scm_rol_workspace_preferencia_updated_by",
        table_name="scm_rol_workspace_preferencia",
    )
    op.drop_index(
        "ix_scm_rol_workspace_preferencia_created_by",
        table_name="scm_rol_workspace_preferencia",
    )
    op.drop_index(
        "ix_scm_rol_workspace_preferencia_orden",
        table_name="scm_rol_workspace_preferencia",
    )
    op.drop_table("scm_rol_workspace_preferencia")

    op.drop_index(
        "uq_trabajador_rol_principal",
        table_name="trabajador_rol",
    )
    with op.batch_alter_table("trabajador_rol") as batch_op:
        batch_op.drop_column("es_principal")

    with op.batch_alter_table("rol_operativo") as batch_op:
        if connection.dialect.name != "sqlite":
            batch_op.drop_constraint(
                "ck_rol_operativo_workspace_version",
                type_="check",
            )
        batch_op.drop_column("version")
        batch_op.drop_column("workspace_start_feature")
        batch_op.drop_column("workspace_focus")
