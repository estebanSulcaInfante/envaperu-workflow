"""add revisioned SCM structures and immutable components

Revision ID: d21f8c3b4a70
Revises: c91d4e7a2b60
Create Date: 2026-07-24 18:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "d21f8c3b4a70"
down_revision = "c91d4e7a2b60"
branch_labels = None
depends_on = None


def _create_postgres_guards(connection):
    if connection.dialect.name != "postgresql":
        return

    op.execute("""
        CREATE FUNCTION scm_structure_revision_guard()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.estado <> 'BORRADOR' THEN
                    RAISE EXCEPTION
                        'STRUCTURE_IMMUTABLE: only drafts can be deleted';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.estado = 'PENDIENTE_APROBACION' THEN
                IF NEW.estado NOT IN ('APROBADA', 'RECHAZADA')
                   OR NEW.articulo_resultado_id
                        IS DISTINCT FROM OLD.articulo_resultado_id
                   OR NEW.numero_revision
                        IS DISTINCT FROM OLD.numero_revision
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                   OR NEW.enviada_at IS DISTINCT FROM OLD.enviada_at
                THEN
                    RAISE EXCEPTION
                        'STRUCTURE_IMMUTABLE: pending revision content';
                END IF;
            ELSIF OLD.estado = 'APROBADA' THEN
                IF NEW.estado <> 'RETIRADA'
                   OR NEW.articulo_resultado_id
                        IS DISTINCT FROM OLD.articulo_resultado_id
                   OR NEW.numero_revision
                        IS DISTINCT FROM OLD.numero_revision
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                   OR NEW.enviada_at IS DISTINCT FROM OLD.enviada_at
                   OR NEW.aprobada_por_id
                        IS DISTINCT FROM OLD.aprobada_por_id
                   OR NEW.aprobada_at IS DISTINCT FROM OLD.aprobada_at
                THEN
                    RAISE EXCEPTION
                        'STRUCTURE_IMMUTABLE: approved revision content';
                END IF;
            ELSIF OLD.estado IN ('RECHAZADA', 'RETIRADA') THEN
                RAISE EXCEPTION
                    'STRUCTURE_IMMUTABLE: terminal revision';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION scm_structure_component_guard()
        RETURNS trigger AS $$
        DECLARE
            target_revision integer;
            revision_status varchar(32);
        BEGIN
            target_revision := CASE
                WHEN TG_OP = 'DELETE' THEN OLD.revision_id
                ELSE NEW.revision_id
            END;
            SELECT estado INTO revision_status
            FROM scm_estructura_revision
            WHERE id = target_revision;
            IF revision_status IS NOT NULL
               AND revision_status <> 'BORRADOR'
            THEN
                RAISE EXCEPTION
                    'STRUCTURE_IMMUTABLE: components are frozen';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_scm_structure_revision_guard
        BEFORE UPDATE OR DELETE ON scm_estructura_revision
        FOR EACH ROW EXECUTE FUNCTION scm_structure_revision_guard()
    """)
    op.execute("""
        CREATE TRIGGER trg_scm_structure_component_guard
        BEFORE INSERT OR UPDATE OR DELETE ON scm_estructura_componente
        FOR EACH ROW EXECUTE FUNCTION scm_structure_component_guard()
    """)


def upgrade():
    connection = op.get_bind()
    op.create_table(
        "scm_estructura_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("articulo_resultado_id", sa.Integer(), nullable=False),
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
        sa.Column("enviada_at", sa.DateTime(timezone=True), nullable=True),
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
            "estado IN "
            "('BORRADOR', 'PENDIENTE_APROBACION', 'APROBADA', "
            "'RECHAZADA', 'RETIRADA')",
            name="ck_scm_estructura_revision_estado",
        ),
        sa.CheckConstraint(
            "numero_revision > 0",
            name="ck_scm_estructura_revision_numero",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_estructura_revision_version",
        ),
        sa.CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name="ck_scm_estructura_revision_hash",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_resultado_id"],
            ["scm_articulo.id"],
            name="fk_scm_estructura_revision_resultado",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["creada_por_id"],
            ["trabajador.id"],
            name="fk_scm_estructura_revision_creador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["aprobada_por_id"],
            ["trabajador.id"],
            name="fk_scm_estructura_revision_aprobador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirada_por_id"],
            ["trabajador.id"],
            name="fk_scm_estructura_revision_retirador",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "articulo_resultado_id",
            "numero_revision",
            name="uq_scm_estructura_articulo_revision",
        ),
    )
    op.create_index(
        "ux_scm_estructura_aprobada_articulo",
        "scm_estructura_revision",
        ["articulo_resultado_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'APROBADA'"),
        sqlite_where=sa.text("estado = 'APROBADA'"),
    )
    op.create_table(
        "scm_estructura_componente",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("secuencia", sa.Integer(), nullable=False),
        sa.Column("articulo_componente_id", sa.Integer(), nullable=False),
        sa.Column("cantidad", sa.Numeric(precision=15, scale=6), nullable=False),
        sa.Column(
            "unidad",
            sa.String(length=10),
            server_default="UN",
            nullable=False,
        ),
        sa.Column(
            "merma_tecnica_pct",
            sa.Numeric(precision=7, scale=4),
            nullable=True,
        ),
        sa.CheckConstraint(
            "secuencia > 0",
            name="ck_scm_estructura_componente_secuencia",
        ),
        sa.CheckConstraint(
            "cantidad > 0",
            name="ck_scm_estructura_componente_cantidad",
        ),
        sa.CheckConstraint(
            "unidad = 'UN'",
            name="ck_scm_estructura_componente_unidad",
        ),
        sa.CheckConstraint(
            "cantidad = CAST(cantidad AS INTEGER)",
            name="ck_scm_estructura_componente_cantidad_discreta",
        ),
        sa.CheckConstraint(
            "merma_tecnica_pct IS NULL OR "
            "(merma_tecnica_pct >= 0 AND merma_tecnica_pct < 100)",
            name="ck_scm_estructura_componente_merma",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["scm_estructura_revision.id"],
            name="fk_scm_estructura_componente_revision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_componente_id"],
            ["scm_articulo.id"],
            name="fk_scm_estructura_componente_articulo",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "revision_id",
            "secuencia",
            name="uq_scm_estructura_componente_secuencia",
        ),
        sa.UniqueConstraint(
            "revision_id",
            "articulo_componente_id",
            name="uq_scm_estructura_componente_articulo",
        ),
    )
    _create_postgres_guards(connection)


def downgrade():
    connection = op.get_bind()
    row_count = connection.execute(
        sa.text("SELECT count(*) FROM scm_estructura_revision")
    ).scalar_one()
    if row_count:
        raise RuntimeError(
            "Downgrade bloqueado: existen estructuras SCM revisionadas"
        )

    if connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_scm_structure_component_guard "
            "ON scm_estructura_componente"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_scm_structure_revision_guard "
            "ON scm_estructura_revision"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS scm_structure_component_guard()"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS scm_structure_revision_guard()"
        )

    op.drop_table("scm_estructura_componente")
    op.drop_index(
        "ux_scm_estructura_aprobada_articulo",
        table_name="scm_estructura_revision",
    )
    op.drop_table("scm_estructura_revision")
