"""Audited, explicit article opt-in; never activates station or release gates."""
from sqlalchemy import select

from app.models.scm_articulos import ScmArticulo
from app.services.scm_kg_service import activate_article_for_kg
from app.services.scm_ot_service import _complete_operation, _event, _reserve_operation
from app.services.scm_service_support import ScmServiceError, load_actor


def prepare_kg_pilot(session, *, actor_id, article_ids, reason, operation_id, apply=False):
    actor = load_actor(session, actor_id, capability="ALMACEN_CONFIG_ADMINISTRAR")
    if not isinstance(reason, str) or not reason.strip():
        raise ScmServiceError("REASON_REQUIRED", "Registre el motivo y el alcance del opt-in.")
    ids = sorted(set(article_ids))
    if not ids or len(ids) > 100 or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in ids):
        raise ScmServiceError("INVALID_ARTICLES", "Seleccione entre 1 y 100 artículos explícitos.")
    data = {"article_ids": ids, "reason": reason.strip()}
    try:
        operation = None
        if apply:
            operation, replay = _reserve_operation(session, operation_id, "CLI /kg-pilot/opt-in", actor, data)
            if replay is not None:
                return replay
        items = []
        for article_id in ids:
            article = session.scalar(select(ScmArticulo).where(ScmArticulo.id == article_id).with_for_update())
            before = article.unidad_inventario if article is not None else None
            article = activate_article_for_kg(session, article_id=article_id)
            item = {"article_id": article.id, "codigo": article.codigo, "before": before,
                    "after": article.unidad_inventario, "version": article.version}
            items.append(item)
            if apply and before != "KG":
                session.add(_event("ARTICULO", article.id, "KG_PILOT_OPT_IN", actor, operation,
                                   {**item, "reason": reason.strip()}))
        result = {"mode": "APPLIED" if apply else "DRY_RUN", "items": items,
                  "operation_id": str(operation_id) if apply else None,
                  "release_constraint": "no_habilitar_en_planta"}
        if apply:
            _complete_operation(operation, result, 200)
            session.commit()
        else:
            session.rollback()
        return result
    except Exception:
        session.rollback()
        raise
