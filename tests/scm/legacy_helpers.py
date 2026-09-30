"""Test-only builders for rows that predate the KG article policy."""

from sqlalchemy import update

from app.models.scm_articulos import ScmArticulo


def mark_legacy_un(session, article):
    """Represent an existing historical UN article without weakening ORM policy."""
    session.execute(
        update(ScmArticulo)
        .where(ScmArticulo.id == article.id)
        .values(unidad_inventario="UN")
    )
    session.expire(article)
    session.refresh(article)
    assert article.unidad_inventario == "UN"
    return article
