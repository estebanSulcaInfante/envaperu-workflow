"""Contract tests for the station-2 Mesa inventory adapter."""

import uuid
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_inventory_kg import (
    ScmEtiquetaUnidadKg,
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
    ScmSaldoInventarioKg,
    ScmUnidadFisicaKg,
)
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_mesa_inventory import ScmMesaInventarioManga, ScmMesaInventarioMovimiento, ScmMesaInventarioPesaje
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_mesa_inventory import dispatch, vista_state
from app.services.scm_kg_custody_service import resolve_kg_outgoing
from app.services.scm_service_support import ScmServiceError


@pytest.fixture
def mesa_actor(app, scm_config):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one())
        article = ScmArticulo(
            codigo="PC-MESA-TEST", nombre="Pieza Mesa de prueba",
            clase="PIEZA_COLOR", unidad_base="UN", unidad_inventario="KG",
        )
        db.session.add(article)
        location = ScmUbicacionInventario(
            codigo="PRODUCCION_KG", nombre="Saldo KG de producción",
            tipo="PUNTO_PRODUCCION", permite_saldo_libre=True,
            clases_articulo_json=["PIEZA_COLOR"], activo=True,
        )
        db.session.add(location)
        db.session.flush()
        db.session.add(ScmSaldoInventarioKg(articulo_scm_id=article.id, ubicacion_id=location.id))
        db.session.commit()
        return actor.id, article.id


def _call(actor_id, method, path, data=None, operation_id=None):
    return dispatch(db.session, actor_id, method, path, data or {}, operation_id or uuid.uuid4())


def _capture(code, kg):
    return {
        "token": "opaque-scale-token", "codigo": code, "kg": f"{kg:.3f}",
        "base": "NT", "captured_at": "2026-11-19T10:00:00+00:00",
        "reading_id": "edge-reading-1",
    }


def _close_full_walk(actor_id, existing=()):
    zones = (
        "Z-ALMACEN-PIEZAS", "Z-ALMACEN-ARMADO-3P",
        "Z-ALMACEN-LLANTAS-3P", "Z-ALMACEN-ARMADO-4P",
    )
    covered = set()
    for session_id, zone in existing:
        _call(actor_id, "POST", f"/sesiones/{session_id}/cerrar", {"cobertura": True})
        covered.add(zone)
    for index, zone in enumerate(zones):
        if zone in covered:
            continue
        opened, _ = _call(actor_id, "POST", "/sesiones", {"zona": zone, "clave": f"walk-{index}-{uuid.uuid4().hex[:8]}"})
        _call(actor_id, "POST", f"/sesiones/{opened['id']}/cerrar", {"cobertura": True})


def test_state_requires_view_capability(app):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        with pytest.raises(ScmServiceError) as error:
            vista_state(db.session, actor.id)
        assert error.value.code == "CAPABILITY_REQUIRED"


def test_faltante_is_blocked_until_all_zone_sessions_are_closed(app, mesa_actor):
    actor_id, article_id = mesa_actor
    with app.app_context():
        old, _ = _call(actor_id, "POST", "/antiguas", {"articulo": article_id, "zona": "Z-ALMACEN-PIEZAS", "clave": "old-partial"})
        code = old["codigo"]
        _call(actor_id, "POST", "/pesajes", {"codigo": code, "bruto": "5.000", "tara": "1.000", "captura": _capture(code, 5), "clave": "weight-partial"})
        _call(actor_id, "POST", "/confirmaciones", {"codigo": code, "motivo": "Confirmación", "clave": "confirm-partial"})
        for index, zone in enumerate(("Z-ALMACEN-PIEZAS", "Z-ALMACEN-ARMADO-3P", "Z-ALMACEN-LLANTAS-3P", "Z-ALMACEN-ARMADO-4P")):
            opened, _ = _call(actor_id, "POST", "/sesiones", {"zona": zone, "clave": f"partial-{index}"})
            _call(actor_id, "POST", f"/sesiones/{opened['id']}/cerrar", {})
        with pytest.raises(ScmServiceError) as error:
            _call(actor_id, "POST", "/revision", {"codigo": code, "cobertura": True, "movimientos_revisados": True, "busqueda": True})
        assert error.value.code == "WALK_INCOMPLETE"


def test_manual_weight_is_rejected_and_confirmation_projects_canonical_ledger(app, mesa_actor):
    actor_id, article_id = mesa_actor
    with app.app_context():
        # First valid catalog article: the canonical location exists but its
        # zero balance row has not been materialized yet.
        db.session.query(ScmSaldoInventarioKg).delete()
        db.session.commit()
        old, status = _call(actor_id, "POST", "/antiguas", {"codigo": "", "articulo": article_id, "zona": "Z-ALMACEN-PIEZAS", "clave": "old-1"})
        assert status == 201
        code = old["codigo"]
        with pytest.raises(ScmServiceError) as error:
            _call(actor_id, "POST", "/pesajes", {"codigo": code, "bruto": "5.000", "tara": "1.000", "clave": "manual-1"})
        assert error.value.code == "CAPTURE_REQUIRED"
        weighed, status = _call(actor_id, "POST", "/pesajes", {
            "codigo": code, "bruto": "5.000", "tara": "1.000",
            "captura": _capture(code, 5), "clave": "weight-1",
        })
        assert status == 201 and weighed["neto"] == "4.000"
        confirmed, status = _call(actor_id, "POST", "/confirmaciones", {
            "codigo": code, "motivo": "Hallazgo confirmado", "clave": "confirm-1",
        })
        assert status == 201
        assert db.session.query(ScmMovimientoInventarioKg).count() == 1
        balance = db.session.query(ScmSaldoInventarioKg).one()
        assert Decimal(balance.cantidad_fisica_kg) == Decimal("4.000")
        existence = db.session.query(ScmExistenciaMangaKg).one()
        assert existence.estado_logistico == "RECIBIDA_ALMACEN"
        local_movement = db.session.query(ScmMesaInventarioMovimiento).one()
        assert str(local_movement.kardex_movimiento_id) == str(confirmed["kardex_movimiento_id"])
        outgoing = resolve_kg_outgoing(db.session, actor_id=actor_id, code=code)
        assert outgoing["unit"]["codigo"].startswith("INV-MESA-")
        assert outgoing["unit"]["estado_logistico"] == "RECIBIDA_ALMACEN"
        assert outgoing["unit"]["kg_verificados"] == "4.000"
        unit = db.session.query(ScmUnidadFisicaKg).one()
        label_id = uuid.uuid4()
        db.session.add(ScmEtiquetaUnidadKg(
            public_id=label_id, unidad_id=unit.id, version=1,
            payload_json={"v": 1, "label_id": str(label_id)},
            payload_hash="0" * 64, estado="GENERADA",
        ))
        db.session.commit()
        opened, _ = _call(actor_id, "POST", "/sesiones", {"zona": "Z-ALMACEN-PIEZAS", "clave": "qr-session"})
        _call(actor_id, "POST", f"/sesiones/{opened['id']}/lecturas", {"codigo": f"L1|{label_id}"})
        assert db.session.query(ScmMesaInventarioManga).count() == 1
        # The capture token is used for validation only and is not persisted.
        assert "token" not in db.session.query(ScmMesaInventarioPesaje).one().__dict__


def test_faltante_and_reappearance_project_one_negative_and_one_new_positive(app, mesa_actor):
    actor_id, article_id = mesa_actor
    with app.app_context():
        old, _ = _call(actor_id, "POST", "/antiguas", {"articulo": article_id, "zona": "Z-ALMACEN-PIEZAS", "clave": "old-2"})
        code = old["codigo"]
        session, _ = _call(actor_id, "POST", "/sesiones", {"zona": "Z-ALMACEN-PIEZAS", "clave": "session-2"})
        _call(actor_id, "POST", f"/sesiones/{session['id']}/lecturas", {"codigo": code})
        _call(actor_id, "POST", "/pesajes", {"codigo": code, "bruto": "5.000", "tara": "1.000", "captura": _capture(code, 5), "clave": "weight-2"})
        _call(actor_id, "POST", "/confirmaciones", {"codigo": code, "motivo": "Confirmación", "clave": "confirm-2"})
        _close_full_walk(actor_id, ((session["id"], "Z-ALMACEN-PIEZAS"),))
        review, _ = _call(actor_id, "POST", "/revision", {"codigo": code, "cobertura": True, "movimientos_revisados": True, "busqueda": True})
        missing_operation = uuid.uuid4()
        missing_body = {"codigo": code, "token": review["token"], "motivo": "No localizado", "clave": "missing-2"}
        missing, _ = _call(actor_id, "POST", "/faltantes", missing_body, missing_operation)
        replay, replay_status = _call(actor_id, "POST", "/faltantes", missing_body, missing_operation)
        assert replay_status == 201 and replay == missing
        assert db.session.query(ScmMovimientoInventarioKg).count() == 2
        assert Decimal(db.session.query(ScmSaldoInventarioKg).one().cantidad_fisica_kg) == Decimal("0.000")
        assert db.session.query(ScmExistenciaMangaKg).one().estado_logistico == "REVERSADA"
        assert db.session.query(ScmUnidadFisicaKg).one().estado == "HISTORICA"

        # A later scan reopens only this identity; it does not create a second
        # canonical unit or duplicate the positive adjustment.
        session, _ = _call(actor_id, "POST", "/sesiones", {"zona": "Z-ALMACEN-PIEZAS", "clave": "session-3"})
        _call(actor_id, "POST", f"/sesiones/{session['id']}/lecturas", {"codigo": code})
        with pytest.raises(ScmServiceError) as error:
            _call(actor_id, "POST", "/confirmaciones", {"codigo": code, "motivo": "Sin pesaje", "clave": "confirm-unweighed"})
        assert error.value.code == "WEIGHT_REQUIRED"
        _call(actor_id, "POST", "/pesajes", {"codigo": code, "bruto": "5.000", "tara": "1.000", "captura": _capture(code, 5), "clave": "weight-3"})
        _call(actor_id, "POST", "/confirmaciones", {"codigo": code, "motivo": "Reaparición", "clave": "confirm-3"})
        assert db.session.query(ScmMovimientoInventarioKg).count() == 3
        assert db.session.query(ScmUnidadFisicaKg).count() == 1
        assert Decimal(db.session.query(ScmSaldoInventarioKg).one().cantidad_fisica_kg) == Decimal("4.000")
        assert db.session.query(ScmExistenciaMangaKg).filter_by(estado_logistico="RECIBIDA_ALMACEN").count() == 1


def test_custody_scan_is_observation_only_and_cannot_be_weighed_or_confirmed(app, mesa_actor):
    actor_id, article_id = mesa_actor
    with app.app_context():
        unit = ScmUnidadFisicaKg(
            public_id=uuid.uuid4(), codigo="INV-ARMADO-OBS-001",
            articulo_scm_id=article_id, estado="ACTIVA",
            estado_logistico="RETIRADA_ARMADO", estado_calidad="SIN_CONTROL",
        )
        db.session.add(unit)
        db.session.commit()
        opened, _ = _call(actor_id, "POST", "/sesiones", {"zona": "Z-ALMACEN-PIEZAS", "clave": "custody-scan"})
        _call(actor_id, "POST", f"/sesiones/{opened['id']}/lecturas", {"codigo": unit.codigo})
        manga = db.session.query(ScmMesaInventarioManga).one()
        assert manga.estado == "HALLAZGO"
        assert db.session.query(ScmMovimientoInventarioKg).count() == 0
        with pytest.raises(ScmServiceError) as error:
            _call(actor_id, "POST", "/pesajes", {
                "codigo": unit.codigo, "bruto": "2.000", "tara": "0.500",
                "captura": _capture(unit.codigo, 2), "clave": "custody-weight",
            })
        assert error.value.code == "WEIGHING_NOT_PENDING"
        with pytest.raises(ScmServiceError) as error:
            _call(actor_id, "POST", "/confirmaciones", {"codigo": unit.codigo, "motivo": "No debe ingresar", "clave": "custody-confirm"})
        assert error.value.code == "WEIGHT_REQUIRED"
        assert db.session.query(ScmMovimientoInventarioKg).count() == 0
