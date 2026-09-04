"""add audited reopening for accidentally closed mangas

Revision ID: f92c3e5a7b94
Revises: f91b2d4e6c83
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "f92c3e5a7b94"
down_revision = "f91b2d4e6c83"
branch_labels = None
depends_on = None


CAPABILITY = "MANGA_REABRIR"


def upgrade():
    with op.batch_alter_table("scm_pesaje_manga") as batch:
        batch.add_column(sa.Column(
            "estado",
            sa.String(length=16),
            nullable=False,
            server_default="VIGENTE",
        ))
        batch.drop_constraint("uq_scm_pesaje_manga_manga", type_="unique")
        batch.create_check_constraint(
            "ck_scm_pesaje_manga_estado",
            "estado IN ('VIGENTE', 'REABIERTO', 'ANULADO')",
        )

    op.execute(sa.text("""
        UPDATE scm_pesaje_manga
        SET estado = 'ANULADO'
        WHERE id IN (SELECT pesaje_id FROM scm_anulacion_pesaje_manga)
    """))
    op.create_index(
        "uq_scm_pesaje_manga_vigente",
        "scm_pesaje_manga",
        ["manga_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'VIGENTE'"),
        sqlite_where=sa.text("estado = 'VIGENTE'"),
    )
    op.create_table(
        "scm_reapertura_manga",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("pesaje_id", sa.Integer(), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=False),
        sa.Column("evidencia", sa.String(length=500), nullable=True),
        sa.Column("reabierta_por_id", sa.Integer(), nullable=False),
        sa.Column(
            "reabierta_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["pesaje_id"], ["scm_pesaje_manga.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reabierta_por_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "public_id", name="uq_scm_reapertura_manga_public_id"
        ),
        sa.UniqueConstraint(
            "pesaje_id", name="uq_scm_reapertura_manga_pesaje"
        ),
        sa.UniqueConstraint(
            "operation_id", name="uq_scm_reapertura_manga_operation"
        ),
    )

    connection = op.get_bind()
    connection.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT :code, :name, true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = :code
        )
    """).bindparams(
        code=CAPABILITY,
        name="Reabrir una manga cerrada accidentalmente",
    ))
    connection.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo AS rol
        JOIN scm_capacidad AS capacidad ON capacidad.codigo = :code
        WHERE rol.codigo IN ('GERENTE_GENERAL', 'JEFE_PRODUCCION')
          AND NOT EXISTS (
              SELECT 1 FROM scm_rol_capacidad AS existing
              WHERE existing.rol_operativo_id = rol.id
                AND existing.capacidad_id = capacidad.id
          )
    """).bindparams(code=CAPABILITY))


def downgrade():
    connection = op.get_bind()
    reopen_count = connection.execute(sa.text(
        "SELECT COUNT(*) FROM scm_reapertura_manga"
    )).scalar_one()
    duplicate_count = connection.execute(sa.text("""
        SELECT COUNT(*) FROM (
            SELECT manga_id FROM scm_pesaje_manga
            GROUP BY manga_id HAVING COUNT(*) > 1
        ) AS duplicate_mangas
    """)).scalar_one()
    if reopen_count or duplicate_count:
        raise RuntimeError(
            "No se puede revertir f92c3e5a7b94 después de reabrir o volver "
            "a pesar una manga; conserve el esquema y revierta solo la aplicación."
        )

    connection.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE capacidad_id = (
            SELECT id FROM scm_capacidad WHERE codigo = :code
        )
    """).bindparams(code=CAPABILITY))
    connection.execute(sa.text(
        "DELETE FROM scm_capacidad WHERE codigo = :code"
    ).bindparams(code=CAPABILITY))
    op.drop_table("scm_reapertura_manga")
    op.drop_index(
        "uq_scm_pesaje_manga_vigente", table_name="scm_pesaje_manga"
    )
    with op.batch_alter_table("scm_pesaje_manga") as batch:
        batch.drop_constraint("ck_scm_pesaje_manga_estado", type_="check")
        batch.create_unique_constraint(
            "uq_scm_pesaje_manga_manga", ["manga_id"]
        )
        batch.drop_column("estado")
