"""Test-only builders for rows that predate the KG article policy."""

from sqlalchemy import text, update

from app.models.scm_articulos import ScmArticulo


def mark_legacy_un(session, article):
    """Represent an existing historical UN article without weakening ORM policy."""
    bind = session.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    postgres = bind.dialect.name == "postgresql"
    if sqlite:
        # The production migration first converts every PIEZA/WIP row and only
        # then installs the constraint. These fixtures intentionally exercise
        # the pre-migration legacy state, which SQLite otherwise cannot seed
        # once the ORM metadata includes that final constraint.
        session.execute(text("PRAGMA ignore_check_constraints = ON"))
    elif postgres:
        # PostgreSQL enforces NOT VALID constraints for every new write. Drop
        # for the lifetime of this isolated legacy fixture. PostgreSQL enforces
        # even NOT VALID constraints on later updates to the same historical
        # row, so restoring it would make the pre-policy scenario impossible
        # to exercise. This module is never part of the runtime image.
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(text("""
            ALTER TABLE scm_articulo
            DROP CONSTRAINT IF EXISTS ck_scm_articulo_kg_inventory_policy_v1
        """))
    try:
        session.execute(
            update(ScmArticulo)
            .where(ScmArticulo.id == article.id)
            .values(unidad_inventario="UN")
        )
    finally:
        if sqlite:
            session.execute(text("PRAGMA ignore_check_constraints = OFF"))
    session.expire(article)
    session.refresh(article)
    assert article.unidad_inventario == "UN"
    return article
