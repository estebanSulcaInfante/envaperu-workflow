"""Station-contract E2E for KG return custody.

The HTTP adapter is deliberately tiny: CentralApiClient remains the real
station consumer and Flask's test client is the real Central provider.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app import db
from app.models.scm_inventory_kg import ScmMedicionUnidadKg, ScmSaldoInventarioKg, ScmUnidadFisicaKg
from app.models.trabajador import Trabajador
from app.services.scm_kg_custody_service import (
    configure_kg_measurement_context,
    divide_kg_unit,
    receive_kg_return,
    reserve_kg_unit,
    withdraw_kg_unit,
)
from app.services.scm_warehouse_service import decide_manga_quality
from app.services.station_auth import provision_station
from test_scm_kg_custody import _grant_capabilities, _received


_CLIENT_PATH = (
    Path(__file__).resolve().parents[3]
    / "modulo-pesaje"
    / "backend"
    / "app"
    / "services"
    / "central_api_client.py"
)
_CLIENT_SPEC = spec_from_file_location("kg_return_station_client", _CLIENT_PATH)
assert _CLIENT_SPEC and _CLIENT_SPEC.loader
_CLIENT_MODULE = module_from_spec(_CLIENT_SPEC)
_CLIENT_SPEC.loader.exec_module(_CLIENT_MODULE)
CentralApiClient = _CLIENT_MODULE.CentralApiClient
CentralApiError = _CLIENT_MODULE.CentralApiError


class _FlaskResponse:
    def __init__(self, response):
        self.status_code = response.status_code
        self.headers = response.headers
        self._response = response

    def json(self):
        return self._response.get_json()


class _FlaskSession:
    def __init__(self, client):
        self.client = client

    def request(self, method, url, *, headers=None, json=None, timeout=None):
        parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(url)
        return _FlaskResponse(self.client.open(
            parsed.path,
            method=method,
            headers=headers,
            json=json,
        ))


def _station_client(app, actor, *, token=None):
    station_id = str(uuid4())
    station, clear_token = provision_station(
        station_id,
        f"KG-RETURN-{station_id[:8]}",
        "Estación retorno KG de prueba",
        "Almacén QA",
        token=token or f"station-token-{station_id}",
    )
    client = app.test_client()
    central = CentralApiClient(
        "http://127.0.0.1",
        clear_token,
        "station-test-v1",
        session=_FlaskSession(client),
    )
    return station, central


def test_station_central_client_runs_kg_return_flow_without_stock_inflation(app):
    with app.app_context():
        ctx = _received(app)
        actor = ctx["actor"]
        _grant_capabilities(actor, (
            "CALIDAD_MANGA_LIBERAR",
            "PICKING_PREPARAR",
            "PICKING_DESPACHAR",
            "RETORNO_RECIBIR",
            "ABASTECIMIENTO_DEVOLVER",
            "ABASTECIMIENTO_VER",
            "UNIDAD_LOGISTICA_FRACCIONAR",
            "ALMACEN_CONFIG_ADMINISTRAR",
        ))
        released = decide_manga_quality(
            db.session,
            actor_id=actor.id,
            existence_id=ctx["existence"].id,
            operation_id=uuid4(),
            data={
                "decision": "LIBERADA",
                "motivo": "Conforme para retorno",
                "version": ctx["existence"].version,
            },
        )
        unit = db.session.get(
            ScmUnidadFisicaKg,
            UUID(released["existencia"]["unidad_fisica_kg_id"]),
        )
        configured = configure_kg_measurement_context(
            db.session,
            actor_id=actor.id,
            unit_id=unit.id,
            operation_id=uuid4(),
            data={
                "version": unit.version,
                "modo_lectura": "NET_DIRECTO",
            },
        )
        reserved = reserve_kg_unit(
            db.session,
            actor_id=actor.id,
            unit_id=unit.id,
            operation_id=uuid4(),
            data={
                "version": configured["unit"]["version"],
                "documento_destino_tipo": "OA",
                "documento_destino_id": "OA-RETURN-STATION-1",
            },
        )
        withdrawn = withdraw_kg_unit(
            db.session,
            actor_id=actor.id,
            unit_id=unit.id,
            operation_id=uuid4(),
            data={"version": reserved["unit"]["version"]},
        )
        division = divide_kg_unit(
            db.session,
            actor_id=actor.id,
            retiro_id=UUID(withdrawn["retiro"]["id"]),
            operation_id=uuid4(),
            data={
                "version": unit.version,
                "partes": [
                    {"client_ref": "return", "intencion": "RETORNO"},
                    {"client_ref": "remain", "intencion": "PERMANECE"},
                ],
            },
        )
        parts = division["division"]["partes"]
        return_part = next(item for item in parts if item["unidad"]["intencion"] == "RETORNO")
        return_unit = db.session.get(ScmUnidadFisicaKg, UUID(return_part["unidad"]["id"]))
        configure_kg_measurement_context(
            db.session,
            actor_id=actor.id,
            unit_id=return_unit.id,
            operation_id=uuid4(),
            data={
                "version": return_unit.version,
                "modo_lectura": "NET_DIRECTO",
            },
        )
        db.session.refresh(return_unit)
        station, central = _station_client(app, actor)
        station_id = station.station_id

        resolved = central.resolve_kg_return(station_id, {
            "actor_id": actor.id,
            "code": return_unit.codigo,
        })
        assert resolved["unit"]["id"] == str(return_unit.id)
        label = central.get_kg_return_label(station_id, {
            "actor_id": actor.id,
            "unit_id": str(return_unit.id),
        })
        assert label["label"]["public_id"] == resolved["label"]["public_id"]
        acknowledged = central.acknowledge_kg_return_label(
            station_id,
            str(uuid4()),
            {
                "actor_id": actor.id,
                "unit_id": str(return_unit.id),
                "label_id": label["label"]["public_id"],
                "estado": "IMPRESA",
                "payload_hash": label["label"]["payload_hash"],
                "job_id": "KG-RETURN-PRINT-1",
            },
        )
        assert acknowledged["label"]["payload_hash"] == label["label"]["payload_hash"]
        db.session.refresh(return_unit)
        capture_operation = str(uuid4())
        capture_payload = {
            "actor_id": actor.id,
            "unit_id": str(return_unit.id),
            "version": return_unit.version,
            "reading_id": "SERIAL-RETURN-1",
            "captured_at_utc": "2026-09-18T20:00:00+00:00",
            "reading_stable": True,
            "modo_lectura": "NET_DIRECTO",
            "label_id": label["label"]["public_id"],
            "expected_unit_source_nonce": resolved["expected_unit_source_nonce"],
            "scale_snapshot": {
                "source": "SERIAL",
                "reading_id": "SERIAL-RETURN-1",
                "received_at_utc": "2026-09-18T20:00:00+00:00",
                "peso_kg": "3.000",
                "stable": True,
            },
        }
        invalid_capture = {**capture_payload, "scale_snapshot": {
            **capture_payload["scale_snapshot"],
            "source": "MANUAL",
        }}
        with pytest.raises(CentralApiError) as manual_reading:
            central.capture_kg_return(station_id, str(uuid4()), invalid_capture)
        assert manual_reading.value.state == "CONTRACT_CONFLICT"
        assert ScmMedicionUnidadKg.query.count() == 0
        measured = central.capture_kg_return(station_id, capture_operation, capture_payload)
        replay = central.capture_kg_return(station_id, capture_operation, capture_payload)
        assert replay == measured
        assert measured["measurement"]["neto_kg"] == "3.000"
        assert ScmMedicionUnidadKg.query.count() == 1

        received = receive_kg_return(
            db.session,
            actor_id=actor.id,
            unit_id=return_unit.id,
            operation_id=uuid4(),
            data={
                "version": measured["unit"]["version"],
                "measurement_id": measured["measurement"]["id"],
                "ubicacion_codigo": ctx["location"],
            },
        )
        returned = db.session.get(
            type(ctx["existence"]), UUID(received["existencia"]["id"])
        )
        assert returned.estado_calidad == "SIN_CONTROL"
        assert received["existencia"]["cantidad_fisica"] == "3.000"
        balance = db.session.get(ScmSaldoInventarioKg, returned.saldo_id)
        assert balance.cantidad_fisica_kg == 3
        assert balance.cantidad_libre_kg == 3
        assert balance.cantidad_fisica_kg != 15

        unauth = app.test_client().post(
            f"/api/integration/v1/stations/{station_id}/kg-return-units/resolve",
            json={"actor_id": actor.id, "code": return_unit.codigo},
        )
        assert unauth.status_code == 401

        intruder = Trabajador(
            codigo=f"TRB-KG-RETURN-INTRUDER-{uuid4().hex[:8]}",
            nombres="Actor",
            apellidos="Sin alcance",
            activo=True,
        )
        db.session.add(intruder)
        db.session.commit()
        with pytest.raises(CentralApiError) as forbidden:
            central.resolve_kg_return(station_id, {
                "actor_id": intruder.id,
                "code": return_unit.codigo,
            })
        assert forbidden.value.state == "AUTH_ERROR"
        assert forbidden.value.http_status == 403
