"""Read projection integration over existing canonical business scenarios.

The scenarios arrange real ledger/label/assembly facts through services; these
assertions exercise the new detail rather than treating their old suites as proof.
"""
from decimal import Decimal
from uuid import uuid4

from app import db
from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg, ScmUnidadFisicaKg
from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
from app.models.scm_ot import ScmManga
from app.models.scm_auditoria import ScmEvento
from app.models.scm_assembly_execution import ScmConfirmacionMangaArmado
from app.models.trabajador import Trabajador
from app.services.scm_manga_detail_service import get_manga_detail
from test_scm_kg_custody import (
    _grant_capabilities,
    test_kg_division_child_second_withdrawal_has_independent_delivery_limit as _kg_scenario,
)
from test_scm_ot_service import test_pesaje_scm_es_idempotente_y_no_crea_kardex as _assembly_scenario


def _reader(code, caps):
    actor = Trabajador(codigo=code, nombres="Consulta", apellidos="Detalle", activo=True)
    db.session.add(actor)
    db.session.flush()
    _grant_capabilities(actor, caps)
    db.session.commit()
    return actor


def test_detail_after_real_division_and_two_returns_has_all_linked_facts(app):
    _kg_scenario(app)
    with app.app_context():
        reader = _reader("READ-DS04C-KG", (
            "OT_VER", "MANGA_PESAJE_VER", "INVENTARIO_VER", "GENEALOGIA_VER",
            "INVENTARIO_CONTROL_TRANSVERSAL",
        ))
        root = ScmUnidadFisicaKg.query.filter_by(unidad_padre_id=None).one()
        manga = ScmExistenciaMangaKg.query.filter(ScmExistenciaMangaKg.manga_id.isnot(None)).one().manga
        detail = get_manga_detail(db.session, actor_id=reader.id, public_id=manga.public_id)
        sections = detail["secciones"]
        linked = sections["stock_movimientos"]["movimientos"]
        expected = ScmMovimientoInventarioKg.query.all()
        assert len(linked) == len(expected)
        assert sorted(Decimal(str(row["cantidad_delta"])) for row in linked) == sorted(
            row.cantidad_delta_kg for row in expected
        )
        assert sum(row["cantidad_delta"] for row in linked) == 2
        assert all(row["timestamp"] and row["actor"]["nombre"] for row in linked)
        genealogy = sections["genealogia"]["items"]
        assert len(genealogy) == 3
        children = [row for row in genealogy if row["padre"]]
        assert len(children) == 2
        assert all(row["raiz"]["codigo"] == root.codigo for row in genealogy)
        assert all(row["padre"]["codigo"] == root.codigo for row in children)
        assert sorted(row["kg_verificados"] for row in children if row["kg_verificados"] is not None) == [2]
        assert len([row for row in children if row["kg_verificados"] is None]) == 1
        kg_labels = [row for row in sections["etiquetas"]["items"] if row["tipo"] == "KG"]
        assert len(kg_labels) >= 3


def test_detail_custody_separates_original_receipt_from_current_descendant_units(app):
    """A partial return must not project the root receipt as current custody."""
    _kg_scenario(app)
    with app.app_context():
        reader = _reader("READ-DS04C-CUSTODY", (
            "OT_VER", "MANGA_PESAJE_VER", "INVENTARIO_VER", "GENEALOGIA_VER",
        ))
        manga = ScmExistenciaMangaKg.query.filter(
            ScmExistenciaMangaKg.manga_id.isnot(None)
        ).one().manga
        stock = get_manga_detail(
            db.session, actor_id=reader.id, public_id=manga.public_id
        )["secciones"]["stock_movimientos"]
        current = stock["custodia_vigente"]
        historical = stock["recepciones_historicas"]
        assert len(historical) == 1
        assert historical[0]["cantidad_fisica_kg"] > 3.2
        assert all(item.get("cantidad_fisica_kg") != historical[0]["cantidad_fisica_kg"] for item in current)
        returned = [item for item in current if item.get("cantidad_fisica_kg") == 2]
        assert len(returned) == 1
        unknown = [item for item in current if item.get("cantidad_fisica_kg") is None]
        assert len(unknown) == 1
        assert unknown[0]["motivo"] == "remanente_sin_medicion"


def test_detail_assembly_origin_respects_each_source_scope_and_no_document_bypass(app):
    _assembly_scenario(app)
    with app.app_context():
        confirmation = ScmConfirmacionMangaArmado.query.one()
        manga = confirmation.manga
        source = confirmation.consumos[0].asignacion_abastecimiento.existencia
        reader = _reader("READ-DS04C-OA", (
            "OT_VER", "MANGA_PESAJE_VER", "INVENTARIO_VER", "GENEALOGIA_VER",
        ))
        warehouse = ScmAlmacen(codigo="DS04C-FUENTE", nombre="Fuente", tipo="PIEZAS_WIP")
        other = ScmAlmacen(codigo="DS04C-OTRO", nombre="Otro", tipo="PIEZAS_WIP")
        db.session.add_all([warehouse, other])
        db.session.flush()
        source.ubicacion.almacen_id = warehouse.id
        membership = ScmAlmacenTrabajador(
            almacen_id=other.id, trabajador_id=reader.id,
            clases_articulo_json=[source.articulo.clase], asignado_por_id=reader.id,
        )
        db.session.add(membership)
        db.session.commit()
        denied = get_manga_detail(db.session, actor_id=reader.id, public_id=manga.public_id)
        assert "confirmacion_armado" not in denied["secciones"]["documentos"]["item"]["efectivos"]
        hidden = denied["secciones"]["genealogia"]["armado"]["origen"][0]
        assert hidden["estado"] == "restringido"
        assert not {"manga_codigo", "articulo", "cantidad_incorporada_un"}.intersection(hidden)
        membership.almacen_id = warehouse.id
        db.session.commit()
        allowed = get_manga_detail(db.session, actor_id=reader.id, public_id=manga.public_id)
        origin = allowed["secciones"]["genealogia"]["armado"]["origen"][0]
        assert origin["estado"] == "disponible"
        assert origin["manga_codigo"] == source.manga.codigo
        assert origin["cantidad_incorporada_un"] == 10
        reader.roles = [role for role in reader.roles if role.codigo == "OT_VER"]
        db.session.commit()
        limited = get_manga_detail(db.session, actor_id=reader.id, public_id=manga.public_id)
        assert limited["secciones"]["genealogia"]["estado"] == "restringido"
        assert "confirmacion_armado" not in limited["secciones"]["documentos"]["item"]["efectivos"]


def test_detail_after_real_reopen_keeps_original_but_no_current_final_weight(app):
    from test_scm_kg_production import _auto_final_kg_fixture
    from app.services.scm_weighing_service import reopen_manga_after_accidental_close
    with app.app_context():
        _creator, approver, manga, _station, _label, _weighed = _auto_final_kg_fixture(
            app, station_code="DS04C-REOPEN"
        )
        _grant_capabilities(approver, ("MANGA_REABRIR", "OT_VER", "MANGA_PESAJE_VER"))
        db.session.commit()
        reopen_manga_after_accidental_close(
            db.session, actor_id=approver.id, manga_id=manga.public_id,
            operation_id=uuid4(), data={"version": manga.version,
            "motivo": "Completar manga", "evidencia": "Caso de consulta DS04C"},
        )
        history = get_manga_detail(db.session, actor_id=approver.id,
            public_id=manga.public_id)["secciones"]["pesajes_correcciones_reaperturas"]
        assert history["vigente"] is None
        assert history["pesajes"][0]["estado"] == "REABIERTO"
        assert history["reaperturas"][0]["motivo"] == "Completar manga"


def test_control_closure_history_is_independent_of_current_logistics_and_needs_event(app):
    from test_scm_manga_detail_service import test_detail_control_closure_is_event_without_fabricated_vigente as arrange
    arrange(app)
    with app.app_context():
        event = ScmEvento.query.filter_by(tipo="KG_MANGA_CLOSED_FROM_LAST_CONTROL").one()
        manga = ScmManga.query.filter_by(id=int(event.aggregate_id)).one() if event.aggregate_id.isdigit() else ScmManga.query.filter_by(public_id=event.aggregate_id).one()
        reader = _reader("READ-DS04C-CONTROL", ("OT_VER", "MANGA_PESAJE_VER"))
        def history():
            return get_manga_detail(db.session, actor_id=reader.id,
                public_id=manga.public_id)["secciones"]["pesajes_correcciones_reaperturas"]
        prior = history()["cierres_control"]
        # Projection fixture: receiving changes state, never the historical fact.
        manga.estado = "RECIBIDA"
        db.session.flush()
        assert history()["cierres_control"] == prior
        # Adversarial fixture with a control but no closure event. No inferred closure.
        manga.estado = "PENDIENTE_RECEPCION_ALMACEN"
        event.tipo = "OTRO_EVENTO_DE_PRUEBA"
        db.session.flush()
        assert history()["cierres_control"] == []
