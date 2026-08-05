"""normalize order composition snapshots to abstract pieces

Revision ID: d7e9a4c2f105
Revises: c42d8e6f1a03
Create Date: 2026-07-22 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "d7e9a4c2f105"
down_revision = "c42d8e6f1a03"
branch_labels = None
depends_on = None


TABLE = "snapshot_composicion_molde"


def _table_exists(connection):
    return TABLE in sa.inspect(connection).get_table_names()


def _column_names(connection):
    return {
        column["name"]
        for column in sa.inspect(connection).get_columns(TABLE)
    }


def _add_columns(connection):
    columns = _column_names(connection)
    if "pieza_id" not in columns:
        op.add_column(TABLE, sa.Column("pieza_id", sa.Integer(), nullable=True))
    if "pieza_codigo_snapshot" not in columns:
        op.add_column(
            TABLE,
            sa.Column("pieza_codigo_snapshot", sa.String(length=64), nullable=True),
        )
    if "pieza_nombre_snapshot" not in columns:
        op.add_column(
            TABLE,
            sa.Column("pieza_nombre_snapshot", sa.String(length=200), nullable=True),
        )
    if "pieza_sku_legacy" not in columns:
        op.add_column(
            TABLE,
            sa.Column("pieza_sku_legacy", sa.String(length=50), nullable=True),
        )


def _backfill_exact_legacy_identity(connection):
    columns = _column_names(connection)
    if "pieza_sku" not in columns:
        return

    # El único enlace admisible es el determinista por la antigua FK/PK:
    # snapshot.pieza_sku -> pieza_color.sku -> pieza_color.pieza_id. Nunca se
    # infiere por nombre ni se elige una fila arbitraria.
    op.execute(sa.text("""
        UPDATE snapshot_composicion_molde
        SET pieza_sku_legacy = pieza_sku
    """))
    op.execute(sa.text("""
        UPDATE snapshot_composicion_molde
        SET pieza_id = (
                SELECT pieza_color.pieza_id
                FROM pieza_color
                WHERE pieza_color.sku = snapshot_composicion_molde.pieza_sku
            ),
            pieza_codigo_snapshot = (
                SELECT pieza.codigo
                FROM pieza_color
                JOIN pieza ON pieza.id = pieza_color.pieza_id
                WHERE pieza_color.sku = snapshot_composicion_molde.pieza_sku
            ),
            pieza_nombre_snapshot = COALESCE(
                (
                    SELECT pieza.nombre
                    FROM pieza_color
                    JOIN pieza ON pieza.id = pieza_color.pieza_id
                    WHERE pieza_color.sku = snapshot_composicion_molde.pieza_sku
                ),
                (
                    SELECT pieza_color.piezas
                    FROM pieza_color
                    WHERE pieza_color.sku = snapshot_composicion_molde.pieza_sku
                )
            )
    """))

    mismatched_evidence = connection.execute(sa.text("""
        SELECT count(*)
        FROM snapshot_composicion_molde
        WHERE pieza_sku IS NOT NULL
          AND pieza_sku_legacy IS DISTINCT FROM pieza_sku
    """)).scalar_one()
    if mismatched_evidence:
        raise RuntimeError(
            "No se pudo preservar íntegramente pieza_sku como evidencia legacy"
        )


def _replace_old_fk_and_column(connection):
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(TABLE, recreate="always") as batch_op:
            batch_op.drop_column("pieza_sku")
            batch_op.create_foreign_key(
                "fk_snapshot_composicion_pieza",
                "pieza",
                ["pieza_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch_op.create_index(
                "ix_snapshot_composicion_pieza_id",
                ["pieza_id"],
                unique=False,
            )
        return

    inspector = sa.inspect(connection)
    for constraint in inspector.get_foreign_keys(TABLE):
        if constraint.get("constrained_columns") == ["pieza_sku"]:
            op.drop_constraint(
                constraint["name"],
                TABLE,
                type_="foreignkey",
            )
    op.drop_column(TABLE, "pieza_sku")
    op.create_foreign_key(
        "fk_snapshot_composicion_pieza",
        TABLE,
        "pieza",
        ["pieza_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_snapshot_composicion_pieza_id",
        TABLE,
        ["pieza_id"],
        unique=False,
    )


def upgrade():
    connection = op.get_bind()
    # Algunas instalaciones adoptadas contienen el catálogo legacy pero nunca
    # llegaron a crear snapshots de OP. En ese caso no existe evidencia que
    # normalizar y la revisión debe poder avanzar sin fabricar una tabla vacía.
    if not _table_exists(connection):
        return
    _add_columns(connection)
    _backfill_exact_legacy_identity(connection)
    if "pieza_sku" in _column_names(connection):
        _replace_old_fk_and_column(connection)


def _assert_lossless_downgrade(connection):
    row = connection.execute(sa.text("""
        SELECT snapshot.id, snapshot.pieza_sku_legacy
        FROM snapshot_composicion_molde AS snapshot
        LEFT JOIN pieza_color
          ON pieza_color.sku = snapshot.pieza_sku_legacy
        WHERE snapshot.pieza_sku_legacy IS NULL
           OR pieza_color.sku IS NULL
        LIMIT 1
    """)).first()
    if row is not None:
        raise RuntimeError(
            "Downgrade bloqueado: existen snapshots nuevos o evidencia legacy "
            "sin una PiezaColor exacta; no se inventará un SKU representativo"
        )


def downgrade():
    connection = op.get_bind()
    if not _table_exists(connection):
        return
    _assert_lossless_downgrade(connection)

    op.add_column(
        TABLE,
        sa.Column("pieza_sku", sa.String(length=50), nullable=True),
    )
    op.execute(sa.text("""
        UPDATE snapshot_composicion_molde
        SET pieza_sku = pieza_sku_legacy
    """))

    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(TABLE, recreate="always") as batch_op:
            batch_op.drop_index("ix_snapshot_composicion_pieza_id")
            batch_op.drop_constraint(
                "fk_snapshot_composicion_pieza",
                type_="foreignkey",
            )
            batch_op.create_foreign_key(
                "fk_snapshot_composicion_pieza_color_legacy",
                "pieza_color",
                ["pieza_sku"],
                ["sku"],
            )
            batch_op.drop_column("pieza_sku_legacy")
            batch_op.drop_column("pieza_nombre_snapshot")
            batch_op.drop_column("pieza_codigo_snapshot")
            batch_op.drop_column("pieza_id")
        return

    op.drop_index("ix_snapshot_composicion_pieza_id", table_name=TABLE)
    op.drop_constraint(
        "fk_snapshot_composicion_pieza",
        TABLE,
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_snapshot_composicion_pieza_color_legacy",
        TABLE,
        "pieza_color",
        ["pieza_sku"],
        ["sku"],
    )
    op.drop_column(TABLE, "pieza_sku_legacy")
    op.drop_column(TABLE, "pieza_nombre_snapshot")
    op.drop_column(TABLE, "pieza_codigo_snapshot")
    op.drop_column(TABLE, "pieza_id")
