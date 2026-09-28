"""Corrected history remains a read-only projection over real ORM records."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event

from app import db
from app.models.scm_ot import ScmTramoMangaTrabajo
from app.services.scm_manga_detail_service import list_manga_history
from app.services.scm_production_reports_service import list_production_history
from app.services.scm_weighing_service import approve_weighing_correction, request_weighing_correction
from tests.scm.test_scm_kg_custody import _grant_capabilities
from tests.scm.test_scm_kg_production import _auto_final_kg_fixture


@pytest.mark.parametrize("net", [Decimal("11.900"), Decimal("12.500")])
def test_approved_correction_history_and_drilldown_do_not_dirty_or_write_segments(app, net):
    with app.app_context():
        creator, approver, manga, _station, _label, weighed = _auto_final_kg_fixture(
            app, station_code="CORRECTED-HISTORY",
        )
        _grant_capabilities(creator, ("PESAJE_CORRECCION_SOLICITAR", "OT_VER", "MANGA_PESAJE_VER"))
        _grant_capabilities(approver, ("PESAJE_CORRECCION_APROBAR",))
        db.session.commit()
        correction = request_weighing_correction(
            db.session, actor_id=creator.id,
            weighing_id=UUID(weighed["weighing"]["public_id"]), operation_id=uuid4(),
            data={"proposed": {"peso_bruto_kg": str(net + Decimal("0.100")), "tara_kg": "0.100"},
                  "motivo": "Lectura neta corregida"},
        )["correction"]
        approve_weighing_correction(
            db.session, actor_id=approver.id, correction_id=UUID(correction["id"]),
            operation_id=uuid4(), data={"motivo_aprobacion": "Verificado"},
        )
        db.session.commit()
        segments = ScmTramoMangaTrabajo.query.filter_by(manga_id=manga.id).all()
        original = [(row.id, row.cantidad_inicio_kg, row.cantidad_fin_kg, row.cantidad_atribuida_kg) for row in segments]
        actor_id, public_id = creator.id, str(manga.public_id)
        day = manga.ot.fecha.isoformat()
        filters = {"fecha_desde": day, "fecha_hasta": day, "agrupaciones": "DIA", "medidas": "PESO_KG,MANGAS", "incluir_jerarquia": "1"}
        writes = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split()[0].upper() in {"UPDATE", "INSERT", "DELETE"}:
                writes.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            report = list_production_history(db.session, actor_id=actor_id, filters=filters)
            assert not db.session.dirty, "The report must not mark ledger rows dirty"
            listing = list_manga_history(db.session, actor_id=actor_id, filters=filters, group="[]")
            assert not db.session.dirty, "The drilldown must not mark ledger rows dirty"
            db.session.flush()
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)
        assert writes == []
        assert Decimal(str(report["resumen"]["PESO_KG"])) == net
        assert report["resumen"]["MANGAS"] == listing["total"] == 1
        assert listing["items"][0]["id"] == public_id
        assert Decimal(str(listing["items"][0]["aporte_consulta_kg"])) == net
        db.session.expire_all()
        persisted = ScmTramoMangaTrabajo.query.filter_by(manga_id=manga.id).all()
        assert [(row.id, row.cantidad_inicio_kg, row.cantidad_fin_kg, row.cantidad_atribuida_kg) for row in persisted] == original
