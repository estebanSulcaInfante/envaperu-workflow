"""Add supervised, append-only OT/Trabajo correction after weighing.

Revision ID: f99a1b2c3d09
Revises: f98a1b2c3d08
"""

from alembic import op
import sqlalchemy as sa


revision = "f99a1b2c3d09"
down_revision = "f98a1b2c3d08"
branch_labels = None
depends_on = None

CAPABILITY = "MANGA_REATRIBUIR_TRABAJO"
CAPABILITY_MARKER = "Creada por f99a1b2c3d09 para correccion auditada de manga."


def _seed_capability():
    bind = op.get_bind()
    if bind.execute(sa.text("""
        SELECT 1 FROM scm_capacidad WHERE codigo = :code
    """), {"code": CAPABILITY}).first() is not None:
        # A pre-existing capability and its role map are installation-owned.
        # Adopting it must not broaden permissions or make downgrade lossy.
        return
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, descripcion, activo)
        SELECT :code,
               'Corregir la OT/Trabajo de una manga pesada', :marker, true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad
            WHERE codigo = :code
        )
    """).bindparams(code=CAPABILITY, marker=CAPABILITY_MARKER))
    # No operational role receives the capability in this migration.  The
    # release remains technically deployed but disabled until physical UAT and
    # the label replacement procedure are approved.


def upgrade():
    bind = op.get_bind()
    op.create_table(
        "scm_correccion_asignacion_manga",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("origen_ot_id", sa.Integer(), nullable=False),
        sa.Column("origen_trabajo_ot_id", sa.Uuid(), nullable=False),
        sa.Column("destino_ot_id", sa.Integer(), nullable=False),
        sa.Column("destino_trabajo_ot_id", sa.Uuid(), nullable=False),
        sa.Column("origen_asignacion_id", sa.Uuid(), nullable=True),
        sa.Column("destino_asignacion_id", sa.Uuid(), nullable=False),
        sa.Column("destino_asignacion_plan_id", sa.Integer(), nullable=False),
        sa.Column("tramo_objetivo_id", sa.Uuid(), nullable=True),
        sa.Column("manga_version_antes", sa.Integer(), nullable=False),
        sa.Column("manga_version_despues", sa.Integer(), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=False),
        sa.Column("evidencia_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("estado", sa.String(length=16), nullable=False, server_default="APLICADA"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("estado = 'APLICADA'", name="ck_scm_correccion_asignacion_manga_estado"),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["origen_ot_id"], ["registro_diario_produccion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["origen_trabajo_ot_id"], ["scm_trabajo_ot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["destino_ot_id"], ["registro_diario_produccion.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["destino_trabajo_ot_id"], ["scm_trabajo_ot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["origen_asignacion_id"], ["scm_asignacion_personal_trabajo_ot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["destino_asignacion_id"], ["scm_asignacion_personal_trabajo_ot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["destino_asignacion_plan_id"], ["scm_asignacion_plan_manga_ot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tramo_objetivo_id"], ["scm_tramo_manga_trabajo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_correccion_asignacion_manga_public"),
        sa.UniqueConstraint("manga_id", name="uq_scm_correccion_asignacion_manga_manga"),
        sa.UniqueConstraint("operation_id", name="uq_scm_correccion_asignacion_manga_operation"),
    )
    op.create_index(
        "ix_scm_correccion_asignacion_manga_destino",
        "scm_correccion_asignacion_manga",
        ["destino_trabajo_ot_id"],
    )
    if bind.dialect.name == "postgresql":
        schema = bind.execute(sa.text("SELECT current_schema()" )).scalar_one()
        quoted_schema = bind.dialect.identifier_preparer.quote(schema)
        qualified = f"{quoted_schema}.scm_correccion_asignacion_manga"
        op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
        # RLS is an additional boundary, not a replacement for PostgreSQL
        # grants.  The application/migration role retains its owner grants;
        # exposed Supabase roles are revoked below.  This policy lets only
        # roles that already have explicit table privileges pass RLS.
        op.execute(sa.text(f"""
            CREATE POLICY scm_correccion_asignacion_backend_access
            ON {qualified}
            FOR ALL
            TO PUBLIC
            USING (true)
            WITH CHECK (true)
        """))
        op.execute(sa.text(f"""
            REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM PUBLIC;
            DO $body$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM authenticated;
              END IF;
            END
            $body$;
        """))
        op.execute(sa.text("""
            CREATE FUNCTION scm_guard_assignment_correction_immutable()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $fn$
            BEGIN
                RAISE EXCEPTION 'manga assignment corrections are append-only';
            END;
            $fn$;
        """))
        op.execute(sa.text("""
            CREATE TRIGGER trg_scm_assignment_correction_immutable
            BEFORE UPDATE OR DELETE ON scm_correccion_asignacion_manga
            FOR EACH ROW EXECUTE FUNCTION scm_guard_assignment_correction_immutable()
        """))
        op.execute(
            f"ALTER FUNCTION {quoted_schema}.scm_guard_assignment_correction_immutable() "
            f"SET search_path = pg_catalog, {quoted_schema}"
        )
        op.execute(sa.text(f"""
            REVOKE ALL PRIVILEGES ON FUNCTION
              {quoted_schema}.scm_guard_assignment_correction_immutable() FROM PUBLIC;
            DO $body$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL PRIVILEGES ON FUNCTION
                  {quoted_schema}.scm_guard_assignment_correction_immutable() FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL PRIVILEGES ON FUNCTION
                  {quoted_schema}.scm_guard_assignment_correction_immutable() FROM authenticated;
              END IF;
            END
            $body$;
        """))
    _seed_capability()


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM scm_correccion_asignacion_manga")).scalar_one():
        raise RuntimeError("downgrade bloqueado: existen correcciones de asignacion aplicadas")
    capability = bind.execute(sa.text("""
        SELECT id, descripcion
        FROM scm_capacidad
        WHERE codigo = :code
    """), {"code": CAPABILITY}).mappings().first()
    if capability and capability["descripcion"] == CAPABILITY_MARKER:
        current_roles = {
            row[0]
            for row in bind.execute(sa.text("""
                SELECT role.codigo
                FROM scm_rol_capacidad AS relation
                JOIN rol_operativo AS role
                  ON role.id = relation.rol_operativo_id
                WHERE relation.capacidad_id = :capability_id
            """), {"capability_id": capability["id"]})
        }
        if current_roles:
            raise RuntimeError(
                "downgrade bloqueado: la correccion ya fue habilitada; "
                "preserve la autorizacion con un forward fix"
            )
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "DROP TRIGGER IF EXISTS trg_scm_assignment_correction_immutable "
            "ON scm_correccion_asignacion_manga"
        ))
        op.execute(sa.text(
            "DROP FUNCTION IF EXISTS scm_guard_assignment_correction_immutable()"
        ))
    if capability and capability["descripcion"] == CAPABILITY_MARKER:
        op.execute(sa.text("""
            DELETE FROM scm_capacidad
            WHERE id = :capability_id AND descripcion = :marker
        """).bindparams(
            capability_id=capability["id"], marker=CAPABILITY_MARKER,
        ))
    op.drop_index("ix_scm_correccion_asignacion_manga_destino", table_name="scm_correccion_asignacion_manga")
    op.drop_table("scm_correccion_asignacion_manga")
