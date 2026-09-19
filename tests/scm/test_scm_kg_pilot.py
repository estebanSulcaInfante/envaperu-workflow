from uuid import uuid4

import pytest

from app import db
from app.models.scm_auditoria import ScmEvento
from app.services.scm_kg_pilot_service import prepare_kg_pilot
from app.services.scm_service_support import ScmServiceError
from test_scm_kg_custody import _grant_capabilities
from test_scm_ot_service import _seed_aggregate_color_work


def test_opt_in_dry_run_then_apply_is_audited_and_idempotent(app):
    with app.app_context():
        creator, _, _, _, output, *_ = _seed_aggregate_color_work(quantity=120)
        _grant_capabilities(creator, ["ALMACEN_CONFIG_ADMINISTRAR"])
        db.session.commit()
        article_id, actor_id = output.articulo.id, creator.id
        operation = uuid4()
        args = dict(actor_id=actor_id, article_ids=[article_id], reason="Piloto piezas septiembre", operation_id=operation)
        dry = prepare_kg_pilot(db.session, **args)
        assert dry["mode"] == "DRY_RUN"
        assert output.articulo.unidad_inventario == "UN"
        assert ScmEvento.query.filter_by(operation_id=operation).count() == 0
        result = prepare_kg_pilot(db.session, **args, apply=True)
        assert result["mode"] == "APPLIED"
        assert output.articulo.unidad_inventario == "KG"
        assert prepare_kg_pilot(db.session, **args, apply=True) == result
        assert ScmEvento.query.filter_by(operation_id=operation).count() == 1


def test_opt_in_requires_existing_administration_capability(app):
    with app.app_context():
        actor, _, _, _, output, *_ = _seed_aggregate_color_work(quantity=120)
        actor.roles.clear()
        db.session.commit()
        with pytest.raises(ScmServiceError):
            prepare_kg_pilot(db.session, actor_id=actor.id, article_ids=[output.articulo.id],
                             reason="Piloto", operation_id=uuid4(), apply=True)
        assert output.articulo.unidad_inventario == "UN"
