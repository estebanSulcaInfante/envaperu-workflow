"""Focused contract checks for the Estación 2 real-return adapter."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.scm_service_support import ScmServiceError
from app.services.scm_mesa_returns import _capture_values, capture_id


def _payload(**capture):
    return {"captura": {"token": "edge-token-1", "codigo": "RET-1", **capture}}


def test_nt_capture_uses_attested_net_without_proration():
    marker = SimpleNamespace(codigo="RET-1")
    token, base, measured, gross, tare, net, captured_at = _capture_values(
        {**_payload(kg="3.250", base="NT", captured_at="2026-11-19T10:00:00Z"), "tara": "0.750", "bruto": "4.000"},
        marker,
    )
    assert token == "edge-token-1"
    assert base == "NT"
    assert str(measured) == "3.250"
    assert str(net) == "3.250"
    assert str(gross) == "4.000"
    assert captured_at.tzinfo is not None


def test_gross_capture_requires_bruto_to_match_edge_weight():
    marker = SimpleNamespace(codigo="RET-1")
    with pytest.raises(ScmServiceError) as error:
        _capture_values(
            {**_payload(kg="4.000", base="GS", captured_at="2026-11-19T10:00:00Z"), "bruto": "4.100", "tara": "0.500"},
            marker,
        )
    assert error.value.code == "CAPTURE_WEIGHT_CONFLICT"


def test_capture_token_is_deterministic_for_recovery():
    assert capture_id("edge-token-1") == capture_id("edge-token-1")
    assert capture_id("edge-token-1") != capture_id("edge-token-2")


def test_label_binding_uses_custody_resolver_for_current_identity(monkeypatch):
    from uuid import uuid4

    from app.services import scm_kg_custody_service
    from app.services.scm_mesa_returns import _assert_current_label

    label_id = uuid4()
    unit = SimpleNamespace(id=uuid4())
    calls = []

    def resolve(_session, qr):
        calls.append(qr)
        return unit

    monkeypatch.setattr(scm_kg_custody_service, "_unit_by_code", resolve)
    _assert_current_label(SimpleNamespace(), unit, label_id)
    assert calls == [f'{{"v":1,"label_id":"{label_id}"}}']

    monkeypatch.setattr(scm_kg_custody_service, "_unit_by_code", lambda _session, _qr: None)
    with pytest.raises(ScmServiceError) as error:
        _assert_current_label(SimpleNamespace(), unit, label_id)
    assert error.value.code == "LABEL_NOT_CURRENT"


def test_confirmed_receive_operation_replays_even_after_source_state_changes(app):
    from uuid import uuid4

    from app import db
    from app.models.scm_auditoria import ScmOperacion
    from app.services.scm_mesa_returns import _operation

    with app.app_context():
        actor = _state_actor(db, uuid4)
        operation_id = uuid4()
        payload = {"codigo": "RET-REPLAY", "label_id": str(uuid4()), "captura": {"token": "replay", "base": "NT", "kg": "1.000", "captured_at": "2026-11-19T10:00:00+00:00"}}
        _operation(db.session, operation_id, "POST /receive", actor.id, payload)
        operation = db.session.get(ScmOperacion, operation_id)
        operation.response_json = {"code": "RET-REPLAY", "net_kg": "1.000"}
        operation.estado_http = 201
        db.session.commit()

        _, replay, status = _operation(db.session, operation_id, "POST /receive", actor.id, payload)
        assert replay == {"code": "RET-REPLAY", "net_kg": "1.000"}
        assert status == 201


def _state_actor(db, uuid4):
    from app.models.scm_catalogos import ScmCapacidad
    from app.models.trabajador import RolOperativo, Trabajador

    capability = ScmCapacidad(codigo="RETORNO_RECIBIR", nombre="Retorno Mesa")
    role = RolOperativo(codigo=f"STATE-{uuid4().hex[:8]}", nombre="Mesa")
    role.capacidades.append(capability)
    actor = Trabajador(codigo="ESTACION-2", nombres="Mesa", apellidos="Dos", activo=True)
    actor.roles.append(role)
    db.session.add_all([capability, role, actor])
    db.session.flush()
    return actor


def test_vista_state_empty_is_a_safe_station_contract(app):
    from uuid import uuid4

    from app import db
    from app.services.scm_mesa_returns import vista_state

    with app.app_context():
        actor = _state_actor(db, uuid4)
        db.session.commit()
        state = vista_state(db.session, actor.id)
        assert state == {"units": [], "receipts": [], "pending_unions": []}


def test_vista_state_lists_two_pending_unions_with_aliases(app):
    import json
    from uuid import uuid4

    from app import db
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_auditoria import ScmOperacion
    from app.models.scm_inventory_kg import ScmUnidadFisicaKg
    from app.models.scm_mesa_returns import ScmMesaRetornoUnion
    from app.services.scm_mesa_returns import vista_state

    with app.app_context():
        actor = _state_actor(db, uuid4)
        article = ScmArticulo(codigo=f"PC-{uuid4().hex[:8]}".upper(), nombre="Pieza visible", clase="PIEZA_COLOR", unidad_inventario="KG")
        db.session.add(article)
        db.session.flush()
        units = []
        unions = []
        for index in (1, 2):
            unit = ScmUnidadFisicaKg(public_id=uuid4(), codigo=f"RET-P-{index}", articulo_scm_id=article.id,
                                     estado="ACTIVA", estado_logistico="PENDIENTE_VERIFICACION", estado_calidad="SIN_CONTROL")
            db.session.add(unit)
            db.session.flush()
            unit.unidad_raiz_id = unit.id
            operation = ScmOperacion(operation_id=uuid4(), endpoint=f"state-{index}", actor_id=actor.id,
                                     request_sha256=(str(index) * 64), response_json=None)
            db.session.add(operation)
            db.session.flush()
            union = ScmMesaRetornoUnion(codigo=unit.codigo, ciclo=1, articulo_scm_id=article.id,
                                        pieza_color_sku=f"SKU-{index}", color="ROJO", unidad_fisica_id=unit.id,
                                        station_id="station-2", actor_id=actor.id, operation_id=operation.operation_id)
            db.session.add(union)
            unions.append(union)
        db.session.commit()
        state = vista_state(db.session, actor.id)
        json.dumps(state)
        assert len(state["units"]) == 2
        assert {item["code"] for item in state["units"]} == {"RET-P-1", "RET-P-2"}
        assert all(item["status"] == "pending" and item["aliases"] == [item["code"]] for item in state["units"])


def test_simple_return_can_complete_a_second_cycle(app):
    """The same canonical QR is reusable only after a new physical withdrawal."""
    from uuid import uuid4

    from app import db
    from app.models.estacion_pesaje import EstacionPesaje
    from app.models.scm_auditoria import ScmOperacion
    from app.models.scm_catalogos import ScmCapacidad
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import ScmUbicacionInventario
    from app.models.scm_inventory_kg import (
        ScmReservaUnidadKg,
        ScmRetiroArmadoKg,
        ScmRetiroArmadoKgItem,
        ScmUnidadFisicaKg,
    )
    from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
    from app.models.scm_mesa_returns import ScmMesaRetornoRecibo
    from app.models.trabajador import RolOperativo, Trabajador
    from app.services.scm_mesa_returns import dispatch

    with app.app_context():
        capability = ScmCapacidad(codigo="RETORNO_RECIBIR", nombre="Retorno Mesa")
        role = RolOperativo(codigo=f"MESA-{uuid4().hex[:8]}", nombre="Mesa")
        role.capacidades.append(capability)
        actor = Trabajador(codigo="ESTACION-2", nombres="Mesa", apellidos="Dos", activo=True)
        actor.roles.append(role)
        article = ScmArticulo(codigo=f"PC-{uuid4().hex[:8]}".upper(), nombre="Pieza", clase="PIEZA_COLOR", unidad_inventario="KG")
        warehouse = ScmAlmacen(codigo=f"WH-{uuid4().hex[:8]}".upper(), nombre="Produccion", tipo="PIEZAS_WIP")
        location = ScmUbicacionInventario(codigo="PRODUCCION_KG", nombre="Produccion KG", almacen=warehouse, tipo="POSICION")
        station = EstacionPesaje(station_id=str(uuid4()), codigo="MESA-ESTACION-2", nombre="Mesa", ubicacion="Mesa", token_hash=uuid4().hex)
        db.session.add_all([capability, role, actor, article, warehouse, location, station])
        db.session.flush()
        db.session.add(ScmAlmacenTrabajador(almacen_id=warehouse.id, trabajador_id=actor.id,
                                            clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=actor.id))
        unit = ScmUnidadFisicaKg(public_id=uuid4(), codigo="BOLSA-1", articulo_scm_id=article.id,
                                 estado="ACTIVA", estado_logistico="RETIRADA_ARMADO", estado_calidad="SIN_CONTROL",
                                 kg_entregado=5, almacen_responsable_id=warehouse.id, version=1)
        db.session.add(unit)
        db.session.flush()
        unit.unidad_raiz_id = unit.id
        withdrawal_op = ScmOperacion(operation_id=uuid4(), endpoint="fixture", actor_id=actor.id, request_sha256="a" * 64, response_json={}, estado_http=201)
        db.session.add(withdrawal_op)
        db.session.flush()
        withdrawal = ScmRetiroArmadoKg(codigo="RET-1", actor_id=actor.id, almacen_responsable_id=warehouse.id, operation_id=withdrawal_op.operation_id)
        db.session.add(withdrawal)
        db.session.flush()
        reservation = ScmReservaUnidadKg(unidad_id=unit.id, cantidad_snapshot_kg=5, estado="RETIRADA", actor_id=actor.id, operation_id=withdrawal_op.operation_id)
        db.session.add(reservation)
        db.session.flush()
        db.session.add(ScmRetiroArmadoKgItem(retiro_id=withdrawal.id, unidad_id=unit.id, reserva_id=reservation.id, neto_entregado_kg=5))
        db.session.commit()

        station_id = station.station_id
        from app.services.scm_service_support import ScmServiceError
        location.activo = False
        db.session.commit()
        with pytest.raises(ScmServiceError) as inactive:
            dispatch(db.session, actor.id, station_id, "/receive", {
                "code": unit.codigo, "net_kg": "4.000",
                "captura": {"token": "inactive", "codigo": unit.codigo, "kg": "4.000", "base": "NT", "captured_at": "2026-11-19T09:00:00Z"},
            }, uuid4())
        assert inactive.value.code == "LOCATION_NOT_FOUND"
        location.activo = True
        assignment = db.session.query(ScmAlmacenTrabajador).filter_by(almacen_id=warehouse.id, trabajador_id=actor.id).one()
        db.session.delete(assignment)
        db.session.commit()
        with pytest.raises(ScmServiceError) as out_of_scope:
            dispatch(db.session, actor.id, station_id, "/receive", {
                "code": unit.codigo, "net_kg": "4.000",
                "captura": {"token": "out-of-scope", "codigo": unit.codigo, "kg": "4.000", "base": "NT", "captured_at": "2026-11-19T09:30:00Z"},
            }, uuid4())
        assert out_of_scope.value.code == "LOCATION_NOT_FOUND"
        db.session.add(ScmAlmacenTrabajador(almacen_id=warehouse.id, trabajador_id=actor.id,
                                            clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=actor.id))
        db.session.commit()
        first = dispatch(db.session, actor.id, station_id, "/receive", {
            "code": unit.codigo, "net_kg": "4.000",
            "captura": {"token": "cycle-1", "codigo": unit.codigo, "kg": "4.000", "base": "NT", "captured_at": "2026-11-19T10:00:00Z"},
        }, uuid4())
        assert first[0]["neto"] == "4.000"
        assert unit.estado == "ACTIVA"
        assert unit.estado_logistico == "RECIBIDA_ALMACEN"

        # New real withdrawal of the same canonical unit.
        unit.estado_logistico = "RETIRADA_ARMADO"
        unit.saldo_id = None
        unit.ubicacion_id = None
        unit.recepcion_vigente_id = None
        unit.kg_entregado = 4
        unit.kg_verificados = None
        withdrawal2_op = ScmOperacion(operation_id=uuid4(), endpoint="fixture-2", actor_id=actor.id, request_sha256="b" * 64, response_json={}, estado_http=201)
        db.session.add(withdrawal2_op)
        db.session.flush()
        withdrawal2 = ScmRetiroArmadoKg(codigo="RET-2", actor_id=actor.id, almacen_responsable_id=warehouse.id, operation_id=withdrawal2_op.operation_id)
        db.session.add(withdrawal2)
        db.session.flush()
        reservation2 = ScmReservaUnidadKg(unidad_id=unit.id, cantidad_snapshot_kg=4, estado="RETIRADA", actor_id=actor.id, operation_id=withdrawal2_op.operation_id)
        db.session.add(reservation2)
        db.session.flush()
        db.session.add(ScmRetiroArmadoKgItem(retiro_id=withdrawal2.id, unidad_id=unit.id, reserva_id=reservation2.id, neto_entregado_kg=4))
        db.session.commit()

        second = dispatch(db.session, actor.id, station_id, "/receive", {
            "code": unit.codigo, "net_kg": "3.000",
            "captura": {"token": "cycle-2", "codigo": unit.codigo, "kg": "3.000", "base": "NT", "captured_at": "2026-11-19T11:00:00Z"},
        }, uuid4())
        assert second[0]["neto"] == "3.000"
        assert second[0]["operation_id"] != first[0]["operation_id"]
        assert db.session.query(ScmMesaRetornoRecibo).count() == 2
