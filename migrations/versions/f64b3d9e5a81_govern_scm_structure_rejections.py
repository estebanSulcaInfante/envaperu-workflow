"""govern SCM structure rejection and discard

Revision ID: f64b3d9e5a81
Revises: f63a2c8d4e70
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "f64b3d9e5a81"
down_revision = "f63a2c8d4e70"
branch_labels = None
depends_on = None


def _install_guard():
    op.execute("""
        CREATE OR REPLACE FUNCTION scm_structure_revision_guard()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'STRUCTURE_IMMUTABLE: physical deletion is forbidden';
            END IF;

            IF OLD.estado = 'PENDIENTE_APROBACION' THEN
                IF NEW.estado NOT IN ('APROBADA', 'RECHAZADA')
                   OR NEW.articulo_resultado_id IS DISTINCT FROM OLD.articulo_resultado_id
                   OR NEW.numero_revision IS DISTINCT FROM OLD.numero_revision
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                   OR NEW.enviada_at IS DISTINCT FROM OLD.enviada_at
                THEN
                    RAISE EXCEPTION
                        'STRUCTURE_IMMUTABLE: pending revision content';
                END IF;
            ELSIF OLD.estado = 'BORRADOR' AND NEW.estado = 'DESCARTADA' THEN
                IF NEW.articulo_resultado_id IS DISTINCT FROM OLD.articulo_resultado_id
                   OR NEW.numero_revision IS DISTINCT FROM OLD.numero_revision
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                THEN
                    RAISE EXCEPTION
                        'STRUCTURE_IMMUTABLE: discarded revision content';
                END IF;
            ELSIF OLD.estado = 'APROBADA' THEN
                IF NEW.estado <> 'RETIRADA'
                   OR NEW.articulo_resultado_id IS DISTINCT FROM OLD.articulo_resultado_id
                   OR NEW.numero_revision IS DISTINCT FROM OLD.numero_revision
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                   OR NEW.enviada_at IS DISTINCT FROM OLD.enviada_at
                   OR NEW.aprobada_por_id IS DISTINCT FROM OLD.aprobada_por_id
                   OR NEW.aprobada_at IS DISTINCT FROM OLD.aprobada_at
                THEN
                    RAISE EXCEPTION
                        'STRUCTURE_IMMUTABLE: approved revision content';
                END IF;
            ELSIF OLD.estado IN ('RECHAZADA', 'RETIRADA', 'DESCARTADA') THEN
                RAISE EXCEPTION
                    'STRUCTURE_IMMUTABLE: terminal revision';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)


def _install_legacy_guard():
    op.execute("""
        CREATE OR REPLACE FUNCTION scm_structure_revision_guard()
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
                   OR NEW.articulo_resultado_id IS DISTINCT FROM OLD.articulo_resultado_id
                   OR NEW.numero_revision IS DISTINCT FROM OLD.numero_revision
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                   OR NEW.enviada_at IS DISTINCT FROM OLD.enviada_at
                THEN
                    RAISE EXCEPTION
                        'STRUCTURE_IMMUTABLE: pending revision content';
                END IF;
            ELSIF OLD.estado = 'APROBADA' THEN
                IF NEW.estado <> 'RETIRADA'
                   OR NEW.articulo_resultado_id IS DISTINCT FROM OLD.articulo_resultado_id
                   OR NEW.numero_revision IS DISTINCT FROM OLD.numero_revision
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                   OR NEW.enviada_at IS DISTINCT FROM OLD.enviada_at
                   OR NEW.aprobada_por_id IS DISTINCT FROM OLD.aprobada_por_id
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


def upgrade():
    op.drop_constraint(
        "ck_scm_estructura_revision_estado",
        "scm_estructura_revision",
        type_="check",
    )
    op.add_column(
        "scm_estructura_revision",
        sa.Column("rechazada_por_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "scm_estructura_revision",
        sa.Column("rechazada_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scm_estructura_revision",
        sa.Column("motivo_rechazo", sa.String(500), nullable=True),
    )
    op.add_column(
        "scm_estructura_revision",
        sa.Column("descartada_por_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "scm_estructura_revision",
        sa.Column("descartada_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scm_estructura_revision",
        sa.Column("motivo_descarte", sa.String(500), nullable=True),
    )
    op.create_foreign_key(
        "fk_scm_estructura_revision_rechazador",
        "scm_estructura_revision",
        "trabajador",
        ["rechazada_por_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scm_estructura_revision_descartador",
        "scm_estructura_revision",
        "trabajador",
        ["descartada_por_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_scm_estructura_revision_estado",
        "scm_estructura_revision",
        "estado IN ('BORRADOR', 'PENDIENTE_APROBACION', 'APROBADA', "
        "'RECHAZADA', 'RETIRADA', 'DESCARTADA')",
    )
    op.create_check_constraint(
        "ck_scm_estructura_revision_rechazo",
        "scm_estructura_revision",
        "(estado = 'RECHAZADA' AND rechazada_por_id IS NOT NULL "
        "AND rechazada_at IS NOT NULL AND motivo_rechazo IS NOT NULL) OR "
        "(estado <> 'RECHAZADA' AND rechazada_por_id IS NULL "
        "AND rechazada_at IS NULL AND motivo_rechazo IS NULL)",
    )
    op.create_check_constraint(
        "ck_scm_estructura_revision_descarte",
        "scm_estructura_revision",
        "(estado = 'DESCARTADA' AND descartada_por_id IS NOT NULL "
        "AND descartada_at IS NOT NULL AND motivo_descarte IS NOT NULL) OR "
        "(estado <> 'DESCARTADA' AND descartada_por_id IS NULL "
        "AND descartada_at IS NULL AND motivo_descarte IS NULL)",
    )
    _install_guard()


def downgrade():
    op.execute(
        "DROP TRIGGER IF EXISTS trg_scm_structure_revision_guard "
        "ON scm_estructura_revision"
    )
    op.execute("DELETE FROM scm_estructura_revision WHERE estado = 'DESCARTADA'")
    op.execute("UPDATE scm_estructura_revision SET estado = 'PENDIENTE_APROBACION', "
               "rechazada_por_id = NULL, rechazada_at = NULL, motivo_rechazo = NULL "
               "WHERE estado = 'RECHAZADA'")
    op.drop_constraint(
        "ck_scm_estructura_revision_descarte",
        "scm_estructura_revision",
        type_="check",
    )
    op.drop_constraint(
        "ck_scm_estructura_revision_rechazo",
        "scm_estructura_revision",
        type_="check",
    )
    op.drop_constraint(
        "ck_scm_estructura_revision_estado",
        "scm_estructura_revision",
        type_="check",
    )
    op.drop_constraint(
        "fk_scm_estructura_revision_descartador",
        "scm_estructura_revision",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_scm_estructura_revision_rechazador",
        "scm_estructura_revision",
        type_="foreignkey",
    )
    for column in (
        "motivo_descarte",
        "descartada_at",
        "descartada_por_id",
        "motivo_rechazo",
        "rechazada_at",
        "rechazada_por_id",
    ):
        op.drop_column("scm_estructura_revision", column)
    op.create_check_constraint(
        "ck_scm_estructura_revision_estado",
        "scm_estructura_revision",
        "estado IN ('BORRADOR', 'PENDIENTE_APROBACION', 'APROBADA', "
        "'RECHAZADA', 'RETIRADA')",
    )
    _install_legacy_guard()
    op.execute("""
        CREATE TRIGGER trg_scm_structure_revision_guard
        BEFORE UPDATE OR DELETE ON scm_estructura_revision
        FOR EACH ROW EXECUTE FUNCTION scm_structure_revision_guard()
    """)
