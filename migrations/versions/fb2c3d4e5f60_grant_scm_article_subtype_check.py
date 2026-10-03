"""Grant the runtime role access to the SCM article subtype guard helper.

Downgrade intentionally retains the grant: without recording prior ACL state,
revoking here could remove a privilege that predated this revision.

Revision ID: fb2c3d4e5f60
Revises: fa1b2c3d4e50
"""

from alembic import op
import sqlalchemy as sa


revision = "fb2c3d4e5f60"
down_revision = "fa1b2c3d4e50"
branch_labels = None
depends_on = None


_ROLE = "scm_api"
_FUNCTION = "scm_assert_article_subtype"


def _postgres_function_context():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return None

    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    preparer = bind.dialect.identifier_preparer
    quoted_schema = preparer.quote_identifier(schema)
    signature = f"{quoted_schema}.{_FUNCTION}(integer)"
    function_oid = bind.execute(
        sa.text("SELECT to_regprocedure(:signature)::oid"),
        {"signature": signature},
    ).scalar_one_or_none()
    if function_oid is None:
        raise RuntimeError(
            f"No existe la funcion {signature} requerida por esta migracion"
        )
    return bind, quoted_schema


def upgrade():
    context = _postgres_function_context()
    if context is None:
        return

    bind, quoted_schema = context
    role_exists = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"
    ), {"role": _ROLE}).scalar_one()

    if role_exists:
        # PostgreSQL GRANT is idempotent, so this remains safe if repeated.
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {quoted_schema}."
            f"{_FUNCTION}(integer) TO {_ROLE}"
        )


def downgrade():
    context = _postgres_function_context()
    if context is None:
        return

    # Conservatively retain the grant. A grant-only migration cannot distinguish
    # a privilege it introduced from one that already existed before upgrade.
