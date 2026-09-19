"""Business regressions for the KG custody pilot."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app import db
from app.models.scm_auditoria import ScmEvento
from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg
from app.models.scm_inventory_kg import ScmMedicionUnidadKg, ScmUnidadFisicaKg
from app.models.trabajador import RolOperativo
from app.services.scm_warehouse_service import decide_manga_quality, receive_manga
from app.services.scm_service_support import ScmServiceError
from app.services.scm_kg_custody_service import (
    acknowledge_kg_label,
    capture_kg_measurement,
    configure_kg_measurement_context,
    divide_kg_unit,
    get_kg_retiro,
    receive_kg_return,
    reserve_kg_unit,
    resolve_kg_return,
    withdraw_kg_unit,
)
from test_scm_kg_receipt import _prepare_kg_receipt, _receive_data


def _received(app):
    ctx = _prepare_kg_receipt(app)
    result = receive_manga(db.session, actor_id=ctx["actor"].id,
                          operation_id=uuid4(), data=_receive_data(ctx))
    ctx["existence"] = db.session.get(ScmExistenciaMangaKg, UUID(result["existencia"]["id"]))
    ctx["actor"].roles.append(RolOperativo.query.filter_by(codigo="CALIDAD").one())
    db.session.commit()
    app.config["KG_CUSTODY_WRITE_ENABLED"] = True
    return ctx


def _grant_capabilities(actor, capabilities):
    from app.models.scm_catalogos import ScmCapacidad
    for capability in capabilities:
        role = RolOperativo.query.filter_by(codigo=capability).first()
        if role is None:
            role = RolOperativo(codigo=capability, nombre=capability)
            db.session.add(role)
            db.session.flush()
        capacity = ScmCapacidad.query.filter_by(codigo=capability).first()
        if capacity is None:
            capacity = ScmCapacidad(codigo=capability, nombre=capability)
            db.session.add(capacity)
            db.session.flush()
        if capacity not in role.capacidades:
            role.capacidades.append(capacity)
        if role not in actor.roles:
            actor.roles.append(role)


def test_quality_releases_only_measured_kg_and_replay_is_one_event(app):
    with app.app_context():
        from app.services.scm_configuration import ensure_initial_scm_configuration
        ensure_initial_scm_configuration()
        ctx = _received(app)
        existence = ctx["existence"]
        operation = uuid4()
        data = {"decision": "LIBERADA", "motivo": "Inspección conforme", "version": existence.version}
        result = decide_manga_quality(db.session, actor_id=ctx["actor"].id,
            existence_id=existence.id, operation_id=operation, data=data)
        assert result["existencia"]["unidad_inventario"] == "KG"
        assert existence.saldo.cantidad_fisica_kg == Decimal("12")
        assert existence.saldo.cantidad_no_disponible_kg == Decimal("0")
        assert existence.saldo.cantidad_libre_kg == Decimal("12")
        replay = decide_manga_quality(db.session, actor_id=ctx["actor"].id,
            existence_id=existence.id, operation_id=operation, data=data)
        assert replay == result
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert ScmEvento.query.filter_by(operation_id=operation).count() == 1

@pytest.mark.parametrize("decision", ["LIBERADA", "BLOQUEADA", "RECHAZADA"])
def test_quality_disabled_has_no_effect(app, decision):
    with app.app_context():
        from app.services.scm_configuration import ensure_initial_scm_configuration
        ensure_initial_scm_configuration()
        ctx = _received(app)
        app.config["KG_CUSTODY_WRITE_ENABLED"] = False
        e = ctx["existence"]
        with pytest.raises(ScmServiceError, match="deshabilitadas"):
            decide_manga_quality(db.session, actor_id=ctx["actor"].id,
                existence_id=e.id, operation_id=uuid4(),
                data={"decision": decision, "motivo": "Inspección", "version": e.version})
        assert e.estado_calidad == "PENDIENTE"
        assert e.saldo.cantidad_no_disponible_kg == Decimal("12")


def test_quality_stale_version_does_not_release_kg(app):
    with app.app_context():
        ctx = _received(app)
        e = ctx["existence"]
        with pytest.raises(ScmServiceError) as error:
            decide_manga_quality(db.session, actor_id=ctx["actor"].id,
                existence_id=e.id, operation_id=uuid4(),
                data={"decision": "LIBERADA", "motivo": "Inspección", "version": e.version + 1})
        assert error.value.code == "CONFLICTO_CONCURRENCIA"
        assert e.saldo.cantidad_no_disponible_kg == Decimal("12")


def test_quality_rechecks_scope_even_for_successful_replay(app):
    from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
    with app.app_context():
        ctx = _received(app)
        e = ctx["existence"]
        warehouse = ScmAlmacen(codigo="KG-QA", nombre="Almacén QA", tipo="PIEZAS_WIP")
        db.session.add(warehouse)
        db.session.flush()
        e.ubicacion.almacen_id = warehouse.id
        membership = ScmAlmacenTrabajador(almacen_id=warehouse.id, trabajador_id=ctx["actor"].id,
            clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=ctx["creator"].id)
        db.session.add(membership)
        db.session.commit()
        key = uuid4()
        data = {"decision": "LIBERADA", "motivo": "Inspección", "version": e.version}
        decide_manga_quality(db.session, actor_id=ctx["actor"].id,
                             existence_id=e.id, operation_id=key, data=data)
        membership.activo = False
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            decide_manga_quality(db.session, actor_id=ctx["actor"].id,
                                 existence_id=e.id, operation_id=key, data=data)
        assert error.value.status_code == 403
        assert e.saldo.cantidad_libre_kg == Decimal("12")


def test_kg_reserve_withdraw_measure_and_receive_keeps_return_out_of_free_stock(app):
    with app.app_context():
        from app.services.scm_configuration import ensure_initial_scm_configuration
        ensure_initial_scm_configuration()
        ctx = _received(app)
        actor = ctx["actor"]
        _grant_capabilities(actor, ("PICKING_PREPARAR", "PICKING_DESPACHAR", "RETORNO_RECIBIR", "ABASTECIMIENTO_DEVOLVER", "ALMACEN_CONFIG_ADMINISTRAR", "CALIDAD_MANGA_LIBERAR"))
        existence = ctx["existence"]
        released = decide_manga_quality(db.session, actor_id=actor.id, existence_id=existence.id, operation_id=uuid4(), data={"decision": "LIBERADA", "motivo": "Conforme", "version": existence.version})
        unit = db.session.get(ScmUnidadFisicaKg, UUID(released["existencia"]["unidad_fisica_kg_id"]))
        app.config["KG_CUSTODY_WRITE_ENABLED"] = True
        configured = configure_kg_measurement_context(db.session, actor_id=actor.id, unit_id=unit.id, operation_id=uuid4(), data={"version": unit.version, "modo_lectura": "NET_DIRECTO"})
        reserved = reserve_kg_unit(db.session, actor_id=actor.id, unit_id=unit.id, operation_id=uuid4(), data={"version": configured["unit"]["version"], "documento_destino_tipo": "OA", "documento_destino_id": "OA-KG-1"})
        withdrawn = withdraw_kg_unit(db.session, actor_id=actor.id, unit_id=unit.id, operation_id=uuid4(), data={"version": reserved["unit"]["version"]})
        assert withdrawn["retiro"]["kg_entregado"] == "12.000"
        unit = db.session.get(ScmUnidadFisicaKg, unit.id)
        resolved = resolve_kg_return(db.session, actor_id=actor.id, code=unit.codigo)
        acknowledge_kg_label(db.session, actor_id=actor.id, station_id="RETORNO-01", unit_id=unit.id,
            label_id=resolved["label"]["public_id"], operation_id=uuid4(),
            data={"estado": "IMPRESA", "payload_hash": resolved["label"]["payload_hash"], "job_id": "JOB-KG-1"})
        capture_data = {"version": unit.version, "reading_id": "READ-1", "label_id": resolved["label"]["public_id"], "expected_unit_source_nonce": resolved["expected_unit_source_nonce"], "intencion": "RETORNO"}
        with pytest.raises(ScmServiceError) as source_error:
            capture_kg_measurement(db.session, actor_id=actor.id, station_id="RETORNO-01", unit_id=unit.id, operation_id=uuid4(), data=capture_data, snapshot={"reading_id": "READ-1", "received_at_utc": "2026-09-18T20:00:00+00:00", "peso_kg": "3.000", "stable": True})
        assert source_error.value.code == "SCALE_READING_SOURCE_INVALID"
        capture_operation = uuid4()
        snapshot = {"source": "SERIAL", "reading_id": "READ-1", "received_at_utc": "2026-09-18T20:00:00+00:00", "peso_kg": "3.000", "stable": True}
        measured = capture_kg_measurement(db.session, actor_id=actor.id, station_id="RETORNO-01", unit_id=unit.id, operation_id=capture_operation, data=capture_data, snapshot=snapshot)
        assert capture_kg_measurement(db.session, actor_id=actor.id, station_id="RETORNO-01", unit_id=unit.id, operation_id=capture_operation, data=capture_data, snapshot=snapshot) == measured
        received = receive_kg_return(db.session, actor_id=actor.id, unit_id=unit.id, operation_id=uuid4(), data={"version": measured["unit"]["version"], "measurement_id": measured["measurement"]["id"], "ubicacion_codigo": ctx["location"]})
        assert received["existencia"]["cantidad_fisica"] == "3.000"
        assert received["existencia"]["estado_calidad"] == "SIN_CONTROL"
        assert Decimal(unit.saldo.cantidad_libre_kg if unit.saldo else 0) == Decimal("3")
        current = db.session.get(ScmUnidadFisicaKg, unit.id)
        returned_existence = db.session.get(ScmExistenciaMangaKg, UUID(received["existencia"]["id"]))
        current = db.session.get(ScmUnidadFisicaKg, unit.id)
        reserved_again = reserve_kg_unit(db.session, actor_id=actor.id, unit_id=unit.id,
            operation_id=uuid4(), data={"version": current.version,
            "documento_destino_tipo": "OA", "documento_destino_id": "OA-KG-2"})
        withdrawn_again = withdraw_kg_unit(db.session, actor_id=actor.id, unit_id=unit.id,
            operation_id=uuid4(), data={"version": reserved_again["unit"]["version"]})
        current = db.session.get(ScmUnidadFisicaKg, unit.id)
        next_resolved = resolve_kg_return(db.session, actor_id=actor.id, code=current.codigo)
        with pytest.raises(ScmServiceError) as excess:
            capture_kg_measurement(db.session, actor_id=actor.id, station_id="RETORNO-01", unit_id=unit.id,
                operation_id=uuid4(), data={"version": current.version, "reading_id": "READ-2",
                "label_id": next_resolved["label"]["public_id"], "expected_unit_source_nonce": next_resolved["expected_unit_source_nonce"],
                "intencion": "RETORNO"}, snapshot={"source": "SERIAL", "reading_id": "READ-2",
                "received_at_utc": "2026-09-18T20:05:00+00:00", "peso_kg": "6.000", "stable": True})
        assert excess.value.code == "KG_RETURN_EXCEEDS_DELIVERY"
        current = db.session.get(ScmUnidadFisicaKg, unit.id)
        valid_second = capture_kg_measurement(db.session, actor_id=actor.id, station_id="RETORNO-01", unit_id=unit.id,
            operation_id=uuid4(), data={"version": current.version, "reading_id": "READ-2",
            "label_id": next_resolved["label"]["public_id"], "expected_unit_source_nonce": next_resolved["expected_unit_source_nonce"],
            "intencion": "RETORNO"}, snapshot={"source": "SERIAL", "reading_id": "READ-2",
            "received_at_utc": "2026-09-18T20:06:00+00:00", "peso_kg": "2.000", "stable": True})
        received_second = receive_kg_return(db.session, actor_id=actor.id, unit_id=unit.id,
            operation_id=uuid4(), data={"version": valid_second["unit"]["version"],
            "measurement_id": valid_second["measurement"]["id"], "ubicacion_codigo": ctx["location"]})
        assert received_second["existencia"]["cantidad_fisica"] == "2.000"


def test_kg_division_makes_historical_parent_and_unmeasured_child_has_no_mass(app, client):
    with app.app_context():
        from app.services.scm_configuration import ensure_initial_scm_configuration
        ensure_initial_scm_configuration()
        ctx = _received(app)
        actor = ctx["actor"]
        _grant_capabilities(actor, ("CALIDAD_MANGA_LIBERAR", "PICKING_PREPARAR", "PICKING_DESPACHAR", "UNIDAD_LOGISTICA_FRACCIONAR", "ABASTECIMIENTO_VER", "RETORNO_RECIBIR", "ALMACEN_CONFIG_ADMINISTRAR"))
        existence = ctx["existence"]
        released = decide_manga_quality(db.session, actor_id=actor.id, existence_id=existence.id, operation_id=uuid4(), data={"decision": "LIBERADA", "motivo": "Conforme", "version": existence.version})
        unit = db.session.get(ScmUnidadFisicaKg, UUID(released["existencia"]["unidad_fisica_kg_id"]))
        app.config["KG_CUSTODY_WRITE_ENABLED"] = True
        reserved = reserve_kg_unit(db.session, actor_id=actor.id, unit_id=unit.id, operation_id=uuid4(), data={"version": unit.version, "documento_destino_tipo": "OA", "documento_destino_id": "OA-KG-2"})
        withdrawn = withdraw_kg_unit(db.session, actor_id=actor.id, unit_id=unit.id, operation_id=uuid4(), data={"version": reserved["unit"]["version"]})
        division = divide_kg_unit(db.session, actor_id=actor.id, retiro_id=UUID(withdrawn["retiro"]["id"]), operation_id=uuid4(), data={"version": unit.version, "partes": [{"client_ref": "A", "intencion": "RETORNO"}, {"client_ref": "B", "intencion": "PERMANECE"}]})
        parent = db.session.get(ScmUnidadFisicaKg, unit.id)
        assert parent.estado == "HISTORICA"
        assert all(part["unidad"]["kg_verificados"] is None for part in division["division"]["partes"])
        assert ScmMedicionUnidadKg.query.count() == 0
        retained_id = UUID(division["division"]["partes"][1]["unidad"]["id"])
        retained = db.session.get(ScmUnidadFisicaKg, retained_id)
        prepared_response = client.post(
            f"/api/scm/v1/unidades-kg/{retained.id}/retorno/preparar",
            headers={"X-Actor-Id": str(actor.id), "Idempotency-Key": str(uuid4())},
            json={"version": retained.version, "motivo": "Remanente solicitado"},
        )
        assert prepared_response.status_code == 200
        prepared = prepared_response.get_json()
        assert prepared["unit"]["intencion"] == "RETORNO"
        retained = db.session.get(ScmUnidadFisicaKg, retained.id)
        configured = configure_kg_measurement_context(
            db.session,
            actor_id=actor.id,
            unit_id=retained.id,
            operation_id=uuid4(),
            data={"version": retained.version, "modo_lectura": "NET_DIRECTO"},
        )
        resolved = resolve_kg_return(db.session, actor_id=actor.id, code=retained.codigo)
        acknowledged = acknowledge_kg_label(
            db.session,
            actor_id=actor.id,
            station_id="RETORNO-01",
            unit_id=retained.id,
            label_id=resolved["label"]["public_id"],
            operation_id=uuid4(),
            data={"estado": "IMPRESA", "payload_hash": resolved["label"]["payload_hash"], "job_id": "JOB-RETAINED-1"},
        )
        assert acknowledged["label"]["estado"] == "IMPRESA"
        measured = capture_kg_measurement(
            db.session,
            actor_id=actor.id,
            station_id="RETORNO-01",
            unit_id=retained.id,
            operation_id=uuid4(),
            data={"version": configured["unit"]["version"], "reading_id": "READ-RETAINED-1", "label_id": resolved["label"]["public_id"], "expected_unit_source_nonce": resolved["expected_unit_source_nonce"]},
            snapshot={"source": "SERIAL", "reading_id": "READ-RETAINED-1", "received_at_utc": "2026-09-18T20:20:00+00:00", "peso_kg": "1.000", "stable": True},
        )
        assert measured["measurement"]["neto_kg"] == "1.000"


def test_kg_division_child_second_withdrawal_has_independent_delivery_limit(app):
    with app.app_context():
        from app.services.scm_configuration import ensure_initial_scm_configuration

        ensure_initial_scm_configuration()
        ctx = _received(app)
        actor = ctx["actor"]
        _grant_capabilities(actor, (
            "CALIDAD_MANGA_LIBERAR", "PICKING_PREPARAR", "PICKING_DESPACHAR",
            "UNIDAD_LOGISTICA_FRACCIONAR", "RETORNO_RECIBIR",
            "ABASTECIMIENTO_VER", "ALMACEN_CONFIG_ADMINISTRAR",
        ))
        released = decide_manga_quality(
            db.session,
            actor_id=actor.id,
            existence_id=ctx["existence"].id,
            operation_id=uuid4(),
            data={"decision": "LIBERADA", "motivo": "Conforme", "version": ctx["existence"].version},
        )
        root = db.session.get(ScmUnidadFisicaKg, UUID(released["existencia"]["unidad_fisica_kg_id"]))
        reserved = reserve_kg_unit(
            db.session, actor_id=actor.id, unit_id=root.id, operation_id=uuid4(),
            data={"version": root.version, "documento_destino_tipo": "OA", "documento_destino_id": "OA-CHILD-1"},
        )
        withdrawn = withdraw_kg_unit(
            db.session, actor_id=actor.id, unit_id=root.id, operation_id=uuid4(),
            data={"version": reserved["unit"]["version"]},
        )
        division = divide_kg_unit(
            db.session, actor_id=actor.id, retiro_id=UUID(withdrawn["retiro"]["id"]), operation_id=uuid4(),
            data={"version": root.version, "partes": [{"intencion": "RETORNO"}, {"intencion": "PERMANECE"}]},
        )
        child = db.session.get(
            ScmUnidadFisicaKg,
            UUID(division["division"]["partes"][0]["unidad"]["id"]),
        )
        configured = configure_kg_measurement_context(
            db.session, actor_id=actor.id, unit_id=child.id, operation_id=uuid4(),
            data={"version": child.version, "modo_lectura": "NET_DIRECTO"},
        )
        resolved = resolve_kg_return(db.session, actor_id=actor.id, code=child.codigo)
        acknowledge_kg_label(
            db.session, actor_id=actor.id, station_id="RETORNO-CHILD", unit_id=child.id,
            label_id=resolved["label"]["public_id"], operation_id=uuid4(),
            data={"estado": "IMPRESA", "payload_hash": resolved["label"]["payload_hash"], "job_id": "JOB-CHILD-1"},
        )
        measured = capture_kg_measurement(
            db.session, actor_id=actor.id, station_id="RETORNO-CHILD", unit_id=child.id,
            operation_id=uuid4(),
            data={"version": configured["unit"]["version"], "reading_id": "CHILD-READ-1", "label_id": resolved["label"]["public_id"], "expected_unit_source_nonce": resolved["expected_unit_source_nonce"]},
            snapshot={"source": "SERIAL", "reading_id": "CHILD-READ-1", "received_at_utc": "2026-09-18T20:30:00+00:00", "peso_kg": "3.000", "stable": True},
        )
        received = receive_kg_return(
            db.session, actor_id=actor.id, unit_id=child.id, operation_id=uuid4(),
            data={"version": measured["unit"]["version"], "measurement_id": measured["measurement"]["id"], "ubicacion_codigo": ctx["location"]},
        )
        returned = db.session.get(ScmExistenciaMangaKg, UUID(received["existencia"]["id"]))
        current = db.session.get(ScmUnidadFisicaKg, child.id)
        reserved_again = reserve_kg_unit(
            db.session, actor_id=actor.id, unit_id=child.id, operation_id=uuid4(),
            data={"version": current.version, "documento_destino_tipo": "OA", "documento_destino_id": "OA-CHILD-2"},
        )
        second_withdrawal = withdraw_kg_unit(
            db.session, actor_id=actor.id, unit_id=child.id, operation_id=uuid4(),
            data={"version": reserved_again["unit"]["version"]},
        )
        current = db.session.get(ScmUnidadFisicaKg, child.id)
        next_resolved = resolve_kg_return(db.session, actor_id=actor.id, code=child.codigo)
        with pytest.raises(ScmServiceError) as excess:
            capture_kg_measurement(
                db.session, actor_id=actor.id, station_id="RETORNO-CHILD", unit_id=child.id,
                operation_id=uuid4(),
                data={"version": current.version, "reading_id": "CHILD-READ-2", "label_id": next_resolved["label"]["public_id"], "expected_unit_source_nonce": next_resolved["expected_unit_source_nonce"]},
                snapshot={"source": "SERIAL", "reading_id": "CHILD-READ-2", "received_at_utc": "2026-09-18T20:31:00+00:00", "peso_kg": "4.000", "stable": True},
            )
        assert excess.value.code == "KG_RETURN_EXCEEDS_DELIVERY"
        current = db.session.get(ScmUnidadFisicaKg, child.id)
        valid = capture_kg_measurement(
            db.session, actor_id=actor.id, station_id="RETORNO-CHILD", unit_id=child.id,
            operation_id=uuid4(),
            data={"version": current.version, "reading_id": "CHILD-READ-2", "label_id": next_resolved["label"]["public_id"], "expected_unit_source_nonce": next_resolved["expected_unit_source_nonce"]},
            snapshot={"source": "SERIAL", "reading_id": "CHILD-READ-2", "received_at_utc": "2026-09-18T20:32:00+00:00", "peso_kg": "2.000", "stable": True},
        )
        assert valid["measurement"]["neto_kg"] == "2.000"
        measurement = db.session.get(ScmMedicionUnidadKg, UUID(valid["measurement"]["id"]))
        assert measurement.retiro_id == UUID(second_withdrawal["retiro"]["id"])
        receive_second = receive_kg_return(
            db.session,
            actor_id=actor.id,
            unit_id=child.id,
            operation_id=uuid4(),
            data={
                "version": valid["unit"]["version"],
                "measurement_id": valid["measurement"]["id"],
                "ubicacion_codigo": ctx["location"],
            },
        )
        assert receive_second["existencia"]["cantidad_fisica"] == "2.000"
        first_delivery = get_kg_retiro(
            db.session, actor_id=actor.id, retiro_id=UUID(withdrawn["retiro"]["id"]),
        )
        second_delivery = get_kg_retiro(
            db.session, actor_id=actor.id, retiro_id=UUID(second_withdrawal["retiro"]["id"]),
        )
        assert first_delivery["kg_retornado_recibido"] == "3.000"
        assert first_delivery["kg_retorno_verificado_pendiente"] == "0.000"
        assert second_delivery["kg_retornado_recibido"] == "2.000"
        assert second_delivery["kg_retorno_verificado_pendiente"] == "0.000"
