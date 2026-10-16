"""Test-only builders for rows that predate the KG article policy."""

from sqlalchemy import text, update

from app.models.scm_articulos import ScmArticulo


def mark_legacy_un(session, article):
    """Represent an existing historical UN article without weakening ORM policy."""
    bind = session.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite:
        # The production migration first converts every PIEZA/WIP row and only
        # then installs the constraint. These fixtures intentionally exercise
        # the pre-migration legacy state, which SQLite otherwise cannot seed
        # once the ORM metadata includes that final constraint.
        session.execute(text("PRAGMA ignore_check_constraints = ON"))
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
