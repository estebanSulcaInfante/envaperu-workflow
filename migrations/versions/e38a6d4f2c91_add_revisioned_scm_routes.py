"""add revisioned SCM routes, operations and DAG precedences

Revision ID: e38a6d4f2c91
Revises: d21f8c3b4a70
Create Date: 2026-07-24 20:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "e38a6d4f2c91"
down_revision = "d21f8c3b4a70"
branch_labels = None
depends_on = None


def _create_postgres_guards(connection):
    if connection.dialect.name != "postgresql":
        return

    op.execute("""
        CREATE FUNCTION scm_route_revision_guard()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.estado <> 'BORRADOR' THEN
                    RAISE EXCEPTION
                        'ROUTE_IMMUTABLE: only drafts can be deleted';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.estado = 'APROBADA' THEN
                IF NEW.estado <> 'RETIRADA'
                   OR NEW.articulo_objetivo_id
                        IS DISTINCT FROM OLD.articulo_objetivo_id
                   OR NEW.numero_revision
                        IS DISTINCT FROM OLD.numero_revision
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                   OR NEW.aprobada_por_id
                        IS DISTINCT FROM OLD.aprobada_por_id
                   OR NEW.aprobada_at IS DISTINCT FROM OLD.aprobada_at
                THEN
                    RAISE EXCEPTION
                        'ROUTE_IMMUTABLE: approved revision content';
                END IF;
            ELSIF OLD.estado = 'RETIRADA' THEN
                RAISE EXCEPTION
                    'ROUTE_IMMUTABLE: retired revision';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION scm_route_child_guard()
        RETURNS trigger AS $$
        DECLARE
            target_route integer;
            route_status varchar(32);
        BEGIN
            target_route := CASE
                WHEN TG_OP = 'DELETE' THEN OLD.ruta_id
                ELSE NEW.ruta_id
            END;
            SELECT estado INTO route_status
            FROM scm_ruta_revision
            WHERE id = target_route;
            IF route_status IS NOT NULL
               AND route_status <> 'BORRADOR'
            THEN
                RAISE EXCEPTION
                    'ROUTE_IMMUTABLE: operations and edges are frozen';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_scm_route_revision_guard
        BEFORE UPDATE OR DELETE ON scm_ruta_revision
        FOR EACH ROW EXECUTE FUNCTION scm_route_revision_guard()
    """)
    op.execute("""
        CREATE TRIGGER trg_scm_route_operation_guard
        BEFORE INSERT OR UPDATE OR DELETE ON scm_operacion_ruta
        FOR EACH ROW EXECUTE FUNCTION scm_route_child_guard()
    """)
    op.execute("""
        CREATE TRIGGER trg_scm_route_precedence_guard
        BEFORE INSERT OR UPDATE OR DELETE ON scm_operacion_precedencia
        FOR EACH ROW EXECUTE FUNCTION scm_route_child_guard()
    """)


def upgrade():
    connection = op.get_bind()
    op.create_table(
        "scm_centro_trabajo",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("nombre", sa.String(length=160), nullable=False),
        sa.Column("tipo", sa.String(length=32), nullable=False),
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_centro_trabajo_codigo",
        ),
        sa.CheckConstraint(
            "tipo IN "
            "('INYECCION', 'PREARMADO', 'ENSAMBLE', "
            "'ACABADO', 'EMPAQUE')",
            name="ck_scm_centro_trabajo_tipo",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_centro_trabajo_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "codigo",
            name="uq_scm_centro_trabajo_codigo",
        ),
    )
    op.create_table(
        "scm_ruta_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("articulo_objetivo_id", sa.Integer(), nullable=False),
        sa.Column("numero_revision", sa.Integer(), nullable=False),
        sa.Column(
            "estado",
            sa.String(length=32),
            server_default="BORRADOR",
            nullable=False,
        ),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("creada_por_id", sa.Integer(), nullable=False),
        sa.Column("aprobada_por_id", sa.Integer(), nullable=True),
        sa.Column("aprobada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retirada_por_id", sa.Integer(), nullable=True),
        sa.Column("retirada_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "estado IN ('BORRADOR', 'APROBADA', 'RETIRADA')",
            name="ck_scm_ruta_revision_estado",
        ),
        sa.CheckConstraint(
            "numero_revision > 0",
            name="ck_scm_ruta_revision_numero",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_ruta_revision_version",
        ),
        sa.CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name="ck_scm_ruta_revision_hash",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_objetivo_id"],
            ["scm_articulo.id"],
            name="fk_scm_ruta_revision_objetivo",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["creada_por_id"],
            ["trabajador.id"],
            name="fk_scm_ruta_revision_creador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["aprobada_por_id"],
            ["trabajador.id"],
            name="fk_scm_ruta_revision_aprobador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirada_por_id"],
            ["trabajador.id"],
            name="fk_scm_ruta_revision_retirador",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "articulo_objetivo_id",
            "numero_revision",
            name="uq_scm_ruta_articulo_revision",
        ),
    )
    op.create_index(
        "ux_scm_ruta_aprobada_articulo",
        "scm_ruta_revision",
        ["articulo_objetivo_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'APROBADA'"),
        sqlite_where=sa.text("estado = 'APROBADA'"),
    )
    op.create_table(
        "scm_operacion_ruta",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ruta_id", sa.Integer(), nullable=False),
        sa.Column("clave", sa.String(length=64), nullable=False),
        sa.Column("secuencia_visible", sa.Integer(), nullable=False),
        sa.Column("nombre", sa.String(length=160), nullable=False),
        sa.Column("tipo", sa.String(length=32), nullable=False),
        sa.Column("executor_kind", sa.String(length=32), nullable=False),
        sa.Column("centro_trabajo_id", sa.Integer(), nullable=False),
        sa.Column("articulo_salida_id", sa.Integer(), nullable=False),
        sa.Column("estructura_revision_id", sa.Integer(), nullable=True),
        sa.Column(
            "permite_concurrente",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "clave = upper(trim(clave)) AND length(clave) > 0",
            name="ck_scm_operacion_ruta_clave",
        ),
        sa.CheckConstraint(
            "secuencia_visible > 0",
            name="ck_scm_operacion_ruta_secuencia",
        ),
        sa.CheckConstraint(
            "tipo IN "
            "('INYECCION', 'PREARMADO', 'ENSAMBLE', "
            "'ACABADO', 'EMPAQUE')",
            name="ck_scm_operacion_ruta_tipo",
        ),
        sa.CheckConstraint(
            "executor_kind IN ('OP_OT', 'ORDEN_OPERACION')",
            name="ck_scm_operacion_ruta_executor",
        ),
        sa.CheckConstraint(
            "(executor_kind = 'OP_OT' "
            "AND estructura_revision_id IS NULL) OR "
            "(executor_kind = 'ORDEN_OPERACION' "
            "AND estructura_revision_id IS NOT NULL)",
            name="ck_scm_operacion_ruta_estructura_executor",
        ),
        sa.ForeignKeyConstraint(
            ["ruta_id"],
            ["scm_ruta_revision.id"],
            name="fk_scm_operacion_ruta_revision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["centro_trabajo_id"],
            ["scm_centro_trabajo.id"],
            name="fk_scm_operacion_ruta_centro",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_salida_id"],
            ["scm_articulo.id"],
            name="fk_scm_operacion_ruta_salida",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["estructura_revision_id"],
            ["scm_estructura_revision.id"],
            name="fk_scm_operacion_ruta_estructura",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ruta_id",
            "clave",
            name="uq_scm_operacion_ruta_clave",
        ),
        sa.UniqueConstraint(
            "ruta_id",
            "secuencia_visible",
            name="uq_scm_operacion_ruta_secuencia",
        ),
        sa.UniqueConstraint(
            "ruta_id",
            "id",
            name="uq_scm_operacion_ruta_parent_id",
        ),
    )
    op.create_table(
        "scm_operacion_precedencia",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ruta_id", sa.Integer(), nullable=False),
        sa.Column("operacion_anterior_id", sa.Integer(), nullable=False),
        sa.Column("operacion_siguiente_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "operacion_anterior_id <> operacion_siguiente_id",
            name="ck_scm_operacion_precedencia_no_self",
        ),
        sa.ForeignKeyConstraint(
            ["ruta_id"],
            ["scm_ruta_revision.id"],
            name="fk_scm_operacion_precedencia_ruta",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ruta_id", "operacion_anterior_id"],
            ["scm_operacion_ruta.ruta_id", "scm_operacion_ruta.id"],
            name="fk_scm_precedencia_anterior_misma_ruta",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ruta_id", "operacion_siguiente_id"],
            ["scm_operacion_ruta.ruta_id", "scm_operacion_ruta.id"],
            name="fk_scm_precedencia_siguiente_misma_ruta",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ruta_id",
            "operacion_anterior_id",
            "operacion_siguiente_id",
            name="uq_scm_operacion_precedencia_arista",
        ),
    )
    _create_postgres_guards(connection)


def downgrade():
    connection = op.get_bind()
    route_count = connection.execute(
        sa.text("SELECT count(*) FROM scm_ruta_revision")
    ).scalar_one()
    center_count = connection.execute(
        sa.text("SELECT count(*) FROM scm_centro_trabajo")
    ).scalar_one()
    if route_count or center_count:
        raise RuntimeError(
            "Downgrade bloqueado: existen rutas o centros SCM"
        )

    if connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_scm_route_precedence_guard "
            "ON scm_operacion_precedencia"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_scm_route_operation_guard "
            "ON scm_operacion_ruta"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_scm_route_revision_guard "
            "ON scm_ruta_revision"
        )
        op.execute("DROP FUNCTION IF EXISTS scm_route_child_guard()")
        op.execute("DROP FUNCTION IF EXISTS scm_route_revision_guard()")

    op.drop_table("scm_operacion_precedencia")
    op.drop_table("scm_operacion_ruta")
    op.drop_index(
        "ux_scm_ruta_aprobada_articulo",
        table_name="scm_ruta_revision",
    )
    op.drop_table("scm_ruta_revision")
    op.drop_table("scm_centro_trabajo")
