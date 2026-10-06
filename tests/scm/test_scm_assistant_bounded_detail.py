from sqlalchemy import select
from sqlalchemy.orm import Session
from app.services.scm_assistant_catalogue import bounded_orm_reads


def test_real_manga_graph_uses_dedicated_session_under_budget(app):
    from app.extensions import db
    from app.models.scm_ot import ScmManga
    from app.models.scm_inventory_kg import ScmExistenciaMangaKg
    from app.services.scm_manga_detail_service import get_manga_detail
    from tests.scm.test_scm_manga_detail_integration import _kg_scenario, _reader
    _kg_scenario(app)
    with app.app_context():
        reader=_reader('ASSISTANT-BUDGET',('OT_VER','MANGA_PESAJE_VER','INVENTARIO_VER','GENEALOGIA_VER','INVENTARIO_CONTROL_TRANSVERSAL'))
        actor_id=reader.id
        public_id=db.session.scalar(select(ScmManga.public_id).join(ScmExistenciaMangaKg,ScmExistenciaMangaKg.manga_id==ScmManga.id))
        engine=db.engine
        db.session.remove()
    # No Flask app context: a remaining Model.query would fail here.
    with Session(engine) as dedicated:
        with bounded_orm_reads(dedicated):
            result=get_manga_detail(dedicated,actor_id=actor_id,public_id=public_id)
        assert result['secciones']['stock_movimientos']['movimientos']
        assert result['secciones']['genealogia']['items']
