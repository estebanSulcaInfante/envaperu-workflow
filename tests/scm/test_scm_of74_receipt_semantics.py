"""Receipt semantics after correcting an unreceived manga's article identity.

The identity changes below are test setup, not a production correction API.
"""

from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

from app import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_inventory import ScmMovimientoInventario
from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg
from app.models.scm_ot import ScmEtiquetaManga, ScmPesajeManga
from app.models.scm_production_orders import ScmOrdenOperacionSalida
from app.services.scm_warehouse_service import receive_manga, resolve_receiving_label
from tests.scm import test_scm_kg_receipt as receipt_fixtures


def _row_snapshot(row):
    return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def test_receipt_of_reclassified_un_history_uses_physical_kg_without_rewriting_history(app, monkeypatch):
    with app.app_context():
        original_seed = receipt_fixtures._seed_aggregate_color_work

        def seed_un_history(**kwargs):
            result = original_seed(**kwargs)
            result[4].articulo.unidad_inventario = "UN"
            db.session.commit()
            return result

        monkeypatch.setattr(receipt_fixtures, "_seed_aggregate_color_work", seed_un_history)
        # Keep the source catalog article UN throughout fixture creation.
        monkeypatch.setattr(receipt_fixtures, "activate_article_for_kg", lambda *a, **kw: None)
        ctx = receipt_fixtures._prepare_kg_receipt(app)
        manga = ctx["manga"]
        original_of_id = manga.trabajo.orden_operacion_id
        original_manga_id = manga.public_id
        source_article = ctx["article"]
        assert source_article.unidad_inventario == "UN"
        weighing = ScmPesajeManga.query.filter_by(manga_id=manga.id).one()
        labels = ScmEtiquetaManga.query.filter_by(manga_id=manga.id).all()
        original_weighing = _row_snapshot(weighing)
        original_labels = {label.id: _row_snapshot(label) for label in labels}
        original_quantities = {
            field: getattr(manga, field)
            for field in (
                "cantidad_planificada_un", "cantidad_asignada_un",
                "cantidad_confirmada_un", "cantidad_contenida_un",
                "peso_unitario_snapshot_g",
            )
        }
        destination = ScmArticulo(
            codigo="NORMAL-KG-RECTIFIED", nombre="Normal KG rectified",
            clase="PIEZA_COLOR", unidad_inventario="KG",
        )
        db.session.add(destination)
        db.session.flush()
        manga.lote_articulo.articulo = destination
        db.session.get(
            ScmOrdenOperacionSalida, manga.plan_linea.orden_operacion_salida_id,
        ).articulo = destination
        db.session.commit()
        candidate = resolve_receiving_label(
            db.session, actor_id=ctx["actor"].id, label_id=ctx["label_id"],
        )
        assert candidate["cantidad_confirmada"] == "120.000"
        assert candidate["peso_neto_kg"] == "12.000"
        assert candidate["articulo"]["id"] == destination.id
        assert candidate["articulo"]["codigo"] == destination.codigo
        assert candidate["manga_id"] == str(original_manga_id)
        assert candidate["trabajo_color"]["orden_fabricacion_id"] == str(original_of_id)
        ctx["candidate"] = candidate
        operation_id = uuid4()
        data = receipt_fixtures._receive_data(ctx)
        result = receive_manga(
            db.session, actor_id=ctx["actor"].id,
            operation_id=operation_id, data=data,
        )
        assert result["existencia"]["unidad"] == "KG"
        assert result["existencia"]["cantidad_fisica"] == "12.000"
        existence = ScmExistenciaMangaKg.query.filter_by(manga_id=manga.id).one()
        assert existence.articulo_scm_id == destination.id
        assert existence.cantidad_fisica_kg == Decimal("12.000")
        assert ScmMovimientoInventario.query.count() == 0
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert _row_snapshot(weighing) == original_weighing
        assert {
            label.id: _row_snapshot(label)
            for label in ScmEtiquetaManga.query.filter_by(manga_id=manga.id).all()
        } == original_labels
        assert {field: getattr(manga, field) for field in original_quantities} == original_quantities
        assert source_article.unidad_inventario == "UN"
        # Replaying the receipt must not produce another physical stock fact.
        replay = receive_manga(
            db.session, actor_id=ctx["actor"].id,
            operation_id=operation_id, data=data,
        )
        assert replay == result
        assert ScmMovimientoInventarioKg.query.count() == 1
