"""normalize pieza and molde as a many-to-many relationship

Revision ID: 8f4c2d1a9b7e
Revises: 7c1e4a9d2b6f
Create Date: 2026-07-22 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "8f4c2d1a9b7e"
down_revision = "7c1e4a9d2b6f"
branch_labels = None
depends_on = None


def _drop_legacy_piece_constraints(connection):
    inspector = sa.inspect(connection)
    for constraint in inspector.get_unique_constraints("pieza"):
        if set(constraint.get("column_names") or ()) == {"molde_id", "nombre"}:
            op.drop_constraint(constraint["name"], "pieza", type_="unique")
    for constraint in inspector.get_foreign_keys("pieza"):
        if constraint.get("constrained_columns") == ["molde_id"]:
            op.drop_constraint(constraint["name"], "pieza", type_="foreignkey")


def _backfill_piece_codes(connection):
    if connection.dialect.name == "sqlite":
        # printf is built into SQLite; LPAD is PostgreSQL-specific.
        op.execute(sa.text("""
            UPDATE pieza
            SET codigo = printf('PZ-%08d', id)
        """))
        return

    op.execute(sa.text("""
        UPDATE pieza
        SET codigo = 'PZ-' || lpad(CAST(id AS varchar), 8, '0')
    """))


def _normalize_piece_table(connection):
    """Remove legacy 1:N columns and finish the global piece schema."""
    if connection.dialect.name == "sqlite":
        # SQLite cannot add/drop named constraints in place. A single batch
        # rebuild also removes constraints that reference dropped molde_id.
        with op.batch_alter_table("pieza", recreate="always") as batch_op:
            batch_op.alter_column(
                "codigo",
                existing_type=sa.String(length=64),
                nullable=False,
            )
            batch_op.create_check_constraint(
                "ck_pieza_codigo_normalizado",
                "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            )
            batch_op.create_check_constraint(
                "ck_pieza_version",
                "version > 0",
            )
            batch_op.create_unique_constraint("uq_pieza_codigo", ["codigo"])
            batch_op.drop_column("cavidades")
            batch_op.drop_column("molde_id")
            batch_op.alter_column(
                "peso_unitario_gr",
                new_column_name="peso_nominal_gr",
                existing_type=sa.Float(),
                existing_nullable=False,
            )
        return

    op.alter_column("pieza", "codigo", nullable=False)
    op.create_check_constraint(
        "ck_pieza_codigo_normalizado",
        "pieza",
        "codigo = upper(trim(codigo)) AND length(codigo) > 0",
    )
    op.create_check_constraint("ck_pieza_version", "pieza", "version > 0")
    op.create_unique_constraint("uq_pieza_codigo", "pieza", ["codigo"])
    _drop_legacy_piece_constraints(connection)
    op.drop_column("pieza", "cavidades")
    op.drop_column("pieza", "molde_id")
    op.alter_column(
        "pieza",
        "peso_unitario_gr",
        new_column_name="peso_nominal_gr",
        existing_type=sa.Float(),
        existing_nullable=False,
    )


def _assert_downgrade_is_lossless(connection):
    incompatible = connection.execute(sa.text("""
        SELECT pieza.id, count(molde_pieza.id) AS relaciones
        FROM pieza
        LEFT JOIN molde_pieza ON molde_pieza.pieza_id = pieza.id
        GROUP BY pieza.id
        HAVING count(molde_pieza.id) <> 1
        LIMIT 1
    """)).first()
    if incompatible is not None:
        raise RuntimeError(
            "El modelo N:M contiene piezas sin exactamente un molde; "
            "downgrade 1:N bloqueado para evitar perdida de datos"
        )

    # The legacy schema also identified a piece by (molde_id, nombre).
    # N:M permits two distinct global pieces with that same legacy identity.
    duplicate_legacy_identity = connection.execute(sa.text("""
        SELECT relacion.molde_id, pieza.nombre, count(*) AS piezas
        FROM molde_pieza AS relacion
        JOIN pieza ON pieza.id = relacion.pieza_id
        GROUP BY relacion.molde_id, pieza.nombre
        HAVING count(*) > 1
        LIMIT 1
    """)).first()
    if duplicate_legacy_identity is not None:
        raise RuntimeError(
            "El modelo N:M contiene piezas con nombre repetido en un molde; "
            "downgrade 1:N bloqueado para preservar la identidad legacy"
        )


def _restore_legacy_piece_table(connection):
    op.add_column(
        "pieza",
        sa.Column("molde_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "pieza",
        sa.Column("cavidades", sa.Integer(), nullable=True),
    )

    if connection.dialect.name == "sqlite":
        op.execute(sa.text("""
            UPDATE pieza
            SET molde_id = (
                    SELECT relacion.molde_id
                    FROM molde_pieza AS relacion
                    WHERE relacion.pieza_id = pieza.id
                ),
                cavidades = (
                    SELECT relacion.cavidades
                    FROM molde_pieza AS relacion
                    WHERE relacion.pieza_id = pieza.id
                ),
                peso_nominal_gr = (
                    SELECT relacion.peso_unitario_gr
                    FROM molde_pieza AS relacion
                    WHERE relacion.pieza_id = pieza.id
                )
        """))
        with op.batch_alter_table("pieza", recreate="always") as batch_op:
            batch_op.alter_column(
                "peso_nominal_gr",
                new_column_name="peso_unitario_gr",
                existing_type=sa.Float(),
                existing_nullable=False,
            )
            batch_op.drop_constraint("uq_pieza_codigo", type_="unique")
            batch_op.drop_constraint("ck_pieza_version", type_="check")
            batch_op.drop_constraint(
                "ck_pieza_codigo_normalizado",
                type_="check",
            )
            batch_op.drop_column("version")
            batch_op.drop_column("activo")
            batch_op.drop_column("codigo")
            batch_op.alter_column(
                "molde_id",
                existing_type=sa.String(length=50),
                nullable=False,
            )
            batch_op.alter_column(
                "cavidades",
                existing_type=sa.Integer(),
                nullable=False,
            )
            batch_op.create_foreign_key(
                "fk_pieza_molde_legacy",
                "molde",
                ["molde_id"],
                ["codigo"],
                ondelete="RESTRICT",
            )
            batch_op.create_unique_constraint(
                "uq_molde_pieza_nombre",
                ["molde_id", "nombre"],
            )
        return

    op.execute(sa.text("""
        UPDATE pieza
        SET molde_id = relacion.molde_id,
            cavidades = relacion.cavidades,
            peso_nominal_gr = relacion.peso_unitario_gr
        FROM molde_pieza AS relacion
        WHERE relacion.pieza_id = pieza.id
    """))
    op.alter_column(
        "pieza",
        "peso_nominal_gr",
        new_column_name="peso_unitario_gr",
        existing_type=sa.Float(),
        existing_nullable=False,
    )
    op.drop_constraint("uq_pieza_codigo", "pieza", type_="unique")
    op.drop_constraint("ck_pieza_version", "pieza", type_="check")
    op.drop_constraint("ck_pieza_codigo_normalizado", "pieza", type_="check")
    op.drop_column("pieza", "version")
    op.drop_column("pieza", "activo")
    op.drop_column("pieza", "codigo")
    op.alter_column("pieza", "molde_id", nullable=False)
    op.alter_column("pieza", "cavidades", nullable=False)
    op.create_foreign_key(
        "fk_pieza_molde_legacy",
        "pieza",
        "molde",
        ["molde_id"],
        ["codigo"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_molde_pieza_nombre",
        "pieza",
        ["molde_id", "nombre"],
    )


def upgrade():
    connection = op.get_bind()
    op.create_table(
        "molde_pieza",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("molde_id", sa.String(length=50), nullable=False),
        sa.Column("pieza_id", sa.Integer(), nullable=False),
        sa.Column("cavidades", sa.Integer(), nullable=False),
        sa.Column("peso_unitario_gr", sa.Float(), nullable=False),
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "cavidades > 0",
            name="ck_molde_pieza_cavidades_positivas",
        ),
        sa.CheckConstraint(
            "peso_unitario_gr > 0",
            name="ck_molde_pieza_peso_positivo",
        ),
        sa.CheckConstraint("version > 0", name="ck_molde_pieza_version"),
        sa.ForeignKeyConstraint(
            ["molde_id"],
            ["molde.codigo"],
            name="fk_molde_pieza_molde",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pieza_id"],
            ["pieza.id"],
            name="fk_molde_pieza_pieza",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "molde_id",
            "pieza_id",
            name="uq_molde_pieza_molde_pieza",
        ),
    )
    op.create_index(
        "ix_molde_pieza_pieza_id",
        "molde_pieza",
        ["pieza_id"],
    )
    op.execute(sa.text("""
        INSERT INTO molde_pieza (
            id, molde_id, pieza_id, cavidades, peso_unitario_gr
        )
        SELECT id, molde_id, id, cavidades, peso_unitario_gr
        FROM pieza
        ORDER BY id
    """))

    if connection.dialect.name == "postgresql":
        op.execute(sa.text("""
            SELECT setval(
                pg_get_serial_sequence('molde_pieza', 'id'),
                COALESCE((SELECT max(id) FROM molde_pieza), 1),
                EXISTS (SELECT 1 FROM molde_pieza)
            )
        """))

    legacy_count = connection.execute(
        sa.text("SELECT count(*) FROM pieza")
    ).scalar_one()
    migrated_count = connection.execute(
        sa.text("SELECT count(*) FROM molde_pieza")
    ).scalar_one()
    if legacy_count != migrated_count:
        raise RuntimeError(
            "No todas las piezas legacy obtuvieron una composicion de molde"
        )

    op.add_column("pieza", sa.Column("codigo", sa.String(length=64), nullable=True))
    op.add_column(
        "pieza",
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "pieza",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    _backfill_piece_codes(connection)
    _normalize_piece_table(connection)


def downgrade():
    connection = op.get_bind()
    _assert_downgrade_is_lossless(connection)
    _restore_legacy_piece_table(connection)
    op.drop_table("molde_pieza")
