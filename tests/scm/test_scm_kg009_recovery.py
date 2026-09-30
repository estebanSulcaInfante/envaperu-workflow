import pytest
from sqlalchemy import text
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app import db
from app.models.scm_auditoria import ScmOperacion
from app.models.scm_articulos import ScmArticulo
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg
from app.models.scm_ot import ScmAtribucionProduccionKg, ScmManga, ScmPesajeManga
from app.services.scm_kg_recovery_service import (
    _check_recovery_deadline,
    _derived_un_projection,
    _kg_projection_hash,
    _kg_projection_complete,
    apply_kg_recovery,
    preview_kg_recovery,
    _source_un_quantity_conflict,
    _source_snapshot_hash,
    _source_ids,
)
from app.services.scm_kg_service import deactivate_article_from_kg
import app.services.scm_kg_pilot_service as kg_pilot_service
import app.services.scm_kg_recovery_service as recovery_service
from app.services.scm_kg_pilot_service import prepare_kg_pilot
from app.services.scm_ot_service import transition_color_work
from app.services.scm_weighing_service import (
    confirm_manga_weighing,
    reopen_manga_after_accidental_close,
)
from app.services.scm_service_support import ScmServiceError
from test_scm_kg_custody import _grant_capabilities
from test_scm_ot_service import _print_color_manga, _seed_aggregate_color_work


def _seed_recovery_service_fixture(app, *, station_code):
    """Build one real final weighing that has no KG intake yet."""
    creator, _approver, _order, _run, output, _line, _header, created = (
        _seed_aggregate_color_work(quantity=120)
    )
    article = output.articulo
    # New piece-color rows are KG by policy.  Move this article to UN through
    # the governed route to represent the historical omission KG009 recovers.
    deactivate_article_from_kg(db.session, article_id=article.id)
    db.session.flush()
    work_id = UUID(created["trabajo_color"]["id"])
    transition_color_work(
        db.session,
        actor_id=creator.id,
        work_id=work_id,
        operation_id=uuid4(),
        data={"version": created["trabajo_color"]["version"]},
        action="iniciar",
    )
    station, label = _print_color_manga(
        actor=creator,
        manga_id=created["mangas"][0]["public_id"],
        station_code=station_code,
    )
    weighed = confirm_manga_weighing(
        db.session,
        station_id=station.station_id,
        operation_id=uuid4(),
        actor_id=creator.id,
        data={
            "label_id": label["public_id"],
            "capture_id": str(uuid4()),
            "peso_bruto_kg": "12.100",
            "tara_kg": "0.100",
            "tara_fuente": "TIPO_MANGA",
            "pesada_at": "2026-09-30T16:55:00-05:00",
            "reading_stable": True,
        },
    )
    location_code = f"{station_code}-LOC".upper()
    db.session.add(ScmUbicacionInventario(
        codigo=location_code,
        nombre="KG009 production",
        clases_articulo_json=["PIEZA_COLOR"],
        activo=True,
        tipo="PUNTO_PRODUCCION",
        permite_saldo_libre=True,
    ))
    _grant_capabilities(creator, ["ALMACEN_CONFIG_ADMINISTRAR"])
    db.session.commit()
    app.config["KG_PRODUCTION_LOCATION_CODE"] = location_code
    manga = ScmManga.query.filter_by(
        public_id=UUID(created["mangas"][0]["public_id"])
    ).one()
    weighing = ScmPesajeManga.query.filter_by(
        public_id=UUID(weighed["weighing"]["public_id"])
    ).one()
    return {
        "creator": creator,
        "approver": _approver,
        "article": article,
        "manga": manga,
        "station": station,
        "prelabel": label,
        "weighing": weighing,
        "weighed": weighed,
    }


def test_kg009_article_policy_sets_piece_wip_kg_and_pt_un(app):
    with app.app_context():
        db.session.add_all([
            ScmArticulo(codigo="PC-KG009-001", nombre="Pieza", clase="PIEZA_COLOR"),
            ScmArticulo(codigo="WIP-KG009-001", nombre="WIP", clase="SUBENSAMBLE_WIP"),
            ScmArticulo(codigo="PT-KG009-001", nombre="PT", clase="PRODUCTO_TERMINADO"),
        ])
        db.session.commit()
        assert ScmArticulo.query.filter_by(codigo="PC-KG009-001").one().unidad_inventario == "KG"
        assert ScmArticulo.query.filter_by(codigo="WIP-KG009-001").one().unidad_inventario == "KG"
        assert ScmArticulo.query.filter_by(codigo="PT-KG009-001").one().unidad_inventario == "UN"


def test_kg009_requires_explicit_source_selection():
    with pytest.raises(ScmServiceError) as error:
        _source_ids([])
    assert error.value.code == "SOURCE_SELECTION_REQUIRED"


def test_kg009_distinguishes_derived_un_mirror_from_explicit_count():
    weighing = SimpleNamespace(
        fuente_cantidad="PLAN_CONFIRMADO_POR_PESAJE",
        snapshots_json={
            "cierre_parcial": False,
            "cantidad_asignada_original_un": "12.000",
            "cantidad_devuelta_plan_un": "0.000",
        },
    )
    assert _derived_un_projection(
        None, weighing, Decimal("12"), Decimal("12")
    )
    weighing.snapshots_json["cantidad_devuelta_plan_un"] = "1.000"
    assert not _derived_un_projection(
        None, weighing, Decimal("12"), Decimal("12")
    )


def test_kg009_noop_requires_complete_coherent_kg_projection():
    source_id = uuid4()
    expected_hash = _kg_projection_hash(source_id, Decimal("4.250"))
    saldo = SimpleNamespace(articulo_scm_id=7, ubicacion_id=11)
    operation_id = uuid4()
    movement_id = uuid4()
    active = SimpleNamespace(
        id=uuid4(),
        movimiento_ingreso_id=movement_id,
        operation_id=operation_id,
        pesaje_public_id=source_id,
        manga_id=13,
        articulo_scm_id=7,
        saldo_id=saldo.id if hasattr(saldo, "id") else 17,
        ubicacion_id=11,
        origen_tipo="PRODUCCION",
        projection_sha256=expected_hash,
        cantidad_fisica_kg=Decimal("4.250"),
        peso_neto_snapshot_kg=Decimal("4.250"),
    )
    # The movement must point to the same balance as the existence.
    active.saldo_id = 17
    movement = SimpleNamespace(
        id=movement_id,
        operation_id=operation_id,
        saldo_id=17,
        saldo=saldo,
        pesaje_public_id=source_id,
        referencia_tipo="PESAJE_MANGA",
        referencia_id=str(source_id),
        tipo="INGRESO_PRODUCCION",
        fuente_tipo="KG_AUTO_PRODUCCION",
        correccion_aplicada_public_id=None,
        projection_sha256=expected_hash,
        cantidad_delta_kg=Decimal("4.250"),
        peso_neto_snapshot_kg=Decimal("4.250"),
    )
    evidence = SimpleNamespace(
        manga_id=13,
        pesaje_id=19,
        trabajo_ot_id=23,
        tipo="NETO_MEDIDO",
        calidad="MEDIDA_DIRECTA",
        cantidad_kg=Decimal("4.250"),
        base_json={"pesaje_public_id": str(source_id)},
    )
    assert _kg_projection_complete(
        active,
        movement,
        evidence,
        Decimal("4.250"),
        source_id,
        article_id=7,
        manga_id=13,
        weighing_id=19,
        owner_work_id=23,
    )
    assert not _kg_projection_complete(
        active, None, evidence, Decimal("4.250"), source_id
    )


def test_kg009_recovered_source_un_delta_is_allowed_only_with_complete_kg():
    assert not _source_un_quantity_conflict("0", "12", True)
    assert _source_un_quantity_conflict("5", "12", True, work_quantity="5")
    assert _source_un_quantity_conflict("0", "12", False)


def test_kg009_recovery_deadline_is_bounded():
    with pytest.raises(ScmServiceError) as error:
        _check_recovery_deadline(0)
    assert error.value.code == "KG_RECOVERY_DEADLINE_EXCEEDED"


def test_kg009_source_hash_covers_mutable_projection_snapshot():
    snapshot = {
        "pesaje_public_id": str(uuid4()),
        "manga": {"version": 4, "cantidad_confirmada_un": "12.000"},
        "tramos": [],
    }
    original = _source_snapshot_hash(snapshot)
    snapshot["manga"]["cantidad_confirmada_un"] = "11.000"
    assert _source_snapshot_hash(snapshot) != original


def test_kg009_pilot_advisory_lock_precedes_article_row_lock(monkeypatch):
    calls = []

    monkeypatch.setattr(
        kg_pilot_service,
        "acquire_kg_productive_write_lock",
        lambda _session: calls.append("advisory"),
    )
    monkeypatch.setattr(
        kg_pilot_service,
        "load_actor",
        lambda *_args, **_kwargs: SimpleNamespace(id=7),
    )
    monkeypatch.setattr(
        kg_pilot_service,
        "_reserve_operation",
        lambda *_args, **_kwargs: (calls.append("reserve") or (None, {"replayed": True})),
    )
    class ReplaySession:
        def rollback(self):
            calls.append("rollback")

    # The replay path proves reservation is reached before the advisory call;
    # no database or row lock is needed for this ordering contract.
    result = kg_pilot_service.prepare_kg_pilot(
        ReplaySession(),
        actor_id=7,
        article_ids=[1],
        reason="orden lock",
        operation_id=uuid4(),
        apply=True,
    )
    assert result == {"replayed": True}
    assert calls == ["reserve", "rollback"]


def test_kg009_real_service_preview_apply_and_replay_release_session(app):
    with app.app_context():
        ctx = _seed_recovery_service_fixture(
            app, station_code=f"PESAJE-KG009-{uuid4().hex[:8]}"
        )
        source_id = ctx["weighing"].public_id
        preview = preview_kg_recovery(
            db.session,
            actor_id=ctx["creator"].id,
            article_ids=[ctx["article"].id],
            reason="Recuperacion KG009 de pesaje omitido",
            source_pesaje_ids=[source_id],
        )
        assert preview["mode"] == "DRY_RUN"
        assert preview["apply_allowed"] is True
        assert preview["sources"][0]["status"] == "CANDIDATE"
        source_hash = preview["sources"][0]["source_snapshot_hash"]
        assert ScmExistenciaMangaKg.query.count() == 0
        assert ScmMovimientoInventarioKg.query.count() == 0

        operation_id = uuid4()
        args = dict(
            actor_id=ctx["creator"].id,
            article_ids=[ctx["article"].id],
            reason="Recuperacion KG009 de pesaje omitido",
            operation_id=operation_id,
            source_pesaje_ids=[source_id],
            source_snapshot_hashes={str(source_id): source_hash},
        )
        applied = apply_kg_recovery(db.session, **args)
        assert applied["mode"] == "APPLIED"
        assert not db.session().in_transaction()
        assert ScmExistenciaMangaKg.query.count() == 1
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert ctx["article"].unidad_inventario == "KG"
        assert ctx["manga"].cantidad_confirmada_un is None
        assert ctx["manga"].version > 1
        db.session.rollback()

        post_apply_preview = preview_kg_recovery(
            db.session,
            actor_id=ctx["creator"].id,
            article_ids=[ctx["article"].id],
            reason="Verificar proyección KG009",
            source_pesaje_ids=[source_id],
        )
        assert post_apply_preview["apply_allowed"] is True
        assert post_apply_preview["sources"][0]["status"] == "ALREADY_APPLIED"
        db.session.rollback()

        replay = apply_kg_recovery(db.session, **args)
        assert replay == applied
        assert not db.session().in_transaction()
        assert ScmExistenciaMangaKg.query.count() == 1
        assert ScmMovimientoInventarioKg.query.count() == 1
        # A PostgreSQL advisory xact lock must not remain held by a replay.
        db.session.rollback()


def test_kg009_real_preview_rejects_manga_work_source_conflict(app):
    with app.app_context():
        ctx = _seed_recovery_service_fixture(
            app, station_code=f"PESAJE-KG009-CONFLICT-{uuid4().hex[:8]}"
        )
        ctx["manga"].cantidad_confirmada_un = Decimal("5")
        ctx["manga"].cantidad_contenida_un = Decimal("5")
        ctx["manga"].trabajo.cantidad_confirmada_un = Decimal("5")
        db.session.commit()
        preview = preview_kg_recovery(
            db.session,
            actor_id=ctx["creator"].id,
            article_ids=[ctx["article"].id],
            reason="Revisar divergencia UN frente a NET",
            source_pesaje_ids=[ctx["weighing"].public_id],
        )
        conflicts = preview["sources"][0]["conflicts"]
        assert preview["apply_allowed"] is False
        assert "UN_SOURCE_MANGA_QUANTITY_MISMATCH" in conflicts
        assert ScmExistenciaMangaKg.query.count() == 0


def test_kg009_preview_excludes_new_final_after_reopened_historical_weighing(app):
    """A manga with a reopened historical final remains outside recovery."""
    with app.app_context():
        ctx = _seed_recovery_service_fixture(
            app, station_code=f"PESAJE-KG009-REOPEN-{uuid4().hex[:8]}"
        )
        _grant_capabilities(ctx["approver"], ["MANGA_REABRIR"])
        db.session.commit()
        old_source_id = ctx["weighing"].public_id
        manga = db.session.get(ScmManga, ctx["manga"].id)
        reopened = reopen_manga_after_accidental_close(
            db.session,
            actor_id=ctx["approver"].id,
            manga_id=manga.public_id,
            operation_id=uuid4(),
            data={
                "version": manga.version,
                "motivo": "Continuar llenado tras cierre histórico",
                "evidencia": "KG009-REOPEN-TEST",
            },
        )
        assert reopened["pesaje_invalidado"]["public_id"] == str(old_source_id)
        manga = db.session.get(ScmManga, manga.id)
        new_weighing = confirm_manga_weighing(
            db.session,
            station_id=ctx["station"].station_id,
            operation_id=uuid4(),
            actor_id=ctx["creator"].id,
            data={
                "label_id": ctx["prelabel"]["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "13.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-09-30T17:10:00-05:00",
                "reading_stable": True,
            },
        )
        new_source_id = UUID(new_weighing["weighing"]["public_id"])
        assert new_source_id != old_source_id
        assert ScmPesajeManga.query.filter_by(public_id=old_source_id).one().estado == "REABIERTO"
        preview = preview_kg_recovery(
            db.session,
            actor_id=ctx["creator"].id,
            article_ids=[ctx["article"].id],
            reason="Recuperar solo el final vigente tras reapertura",
            source_pesaje_ids=[new_source_id],
        )
        assert preview["apply_allowed"] is False
        assert preview["sources"][0]["pesaje_public_id"] == str(new_source_id)
        assert "REOPENING_PRESENT" in preview["sources"][0]["conflicts"]


def test_kg009_late_deadline_rolls_back_final_unit_of_work(app, monkeypatch):
    """A deadline reached after the final flush cannot commit recovery facts."""
    with app.app_context():
        ctx = _seed_recovery_service_fixture(
            app, station_code=f"PESAJE-KG009-DEADLINE-{uuid4().hex[:8]}"
        )
        article_id = ctx["article"].id
        source_id = ctx["weighing"].public_id
        preview = preview_kg_recovery(
            db.session,
            actor_id=ctx["creator"].id,
            article_ids=[ctx["article"].id],
            reason="KG009 deadline rollback",
            source_pesaje_ids=[source_id],
        )
        # SQLite's legacy transaction mode can release a SAVEPOINT without a
        # surrounding BEGIN, making the idempotency reservation survive a
        # rollback.  Start the real outer transaction used by PostgreSQL so
        # this late-deadline contract is tested against the same boundary.
        db.session.rollback()
        db.session.execute(text("BEGIN"))
        original_check = recovery_service._check_recovery_deadline
        checks = {"count": 0}

        def expire_after_mutation(deadline):
            checks["count"] += 1
            if checks["count"] >= 4:
                raise ScmServiceError(
                    "KG_RECOVERY_DEADLINE_EXCEEDED",
                    "La recuperación excedió su ventana transaccional.",
                    status_code=409,
                )
            return original_check(deadline)

        monkeypatch.setattr(
            recovery_service, "_check_recovery_deadline", expire_after_mutation
        )
        operation_id = uuid4()
        with pytest.raises(ScmServiceError) as error:
            apply_kg_recovery(
                db.session,
                actor_id=ctx["creator"].id,
                article_ids=[ctx["article"].id],
                reason="KG009 deadline rollback",
                operation_id=operation_id,
                source_pesaje_ids=[source_id],
                source_snapshot_hashes={
                    str(source_id): preview["sources"][0]["source_snapshot_hash"]
                },
            )
        assert error.value.code == "KG_RECOVERY_DEADLINE_EXCEEDED"
        assert checks["count"] >= 4
        db.session.remove()
        assert ScmOperacion.query.filter_by(operation_id=operation_id).count() == 0
        assert ScmExistenciaMangaKg.query.count() == 0
        assert ScmMovimientoInventarioKg.query.count() == 0
        assert db.session.get(ScmArticulo, article_id).unidad_inventario == "UN"


def test_kg009_governed_deactivation_bypasses_guard_only_after_checks(app):
    with app.app_context():
        article = ScmArticulo(
            codigo=f"KG009-GUARD-{uuid4().hex[:8].upper()}",
            nombre="Guard KG009",
            clase="PIEZA_COLOR",
        )
        db.session.add(article)
        db.session.commit()
        article = db.session.get(ScmArticulo, article.id)
        assert article.unidad_inventario == "KG"
        article.unidad_inventario = "UN"
        with pytest.raises(ValueError, match="KG_ARTICLE_UNIT_IMMUTABLE"):
            db.session.flush()
        db.session.rollback()
        article = db.session.get(ScmArticulo, article.id)
        assert article.unidad_inventario == "KG"

        deactivated = deactivate_article_from_kg(
            db.session, article_id=article.id
        )
        db.session.commit()
        assert deactivated.unidad_inventario == "UN"


def test_kg009_real_pilot_replay_releases_session(app):
    with app.app_context():
        creator, _approver, _order, _run, output, *_ = (
            _seed_aggregate_color_work(quantity=120)
        )
        deactivate_article_from_kg(db.session, article_id=output.articulo.id)
        _grant_capabilities(creator, ["ALMACEN_CONFIG_ADMINISTRAR"])
        db.session.commit()
        args = dict(
            actor_id=creator.id,
            article_ids=[output.articulo.id],
            reason="Opt-in KG009 de artículo histórico",
            operation_id=uuid4(),
            apply=True,
        )
        applied = prepare_kg_pilot(db.session, **args)
        assert applied["mode"] == "APPLIED"
        assert not db.session().in_transaction()
        replay = prepare_kg_pilot(db.session, **args)
        assert replay == applied
        assert not db.session().in_transaction()
