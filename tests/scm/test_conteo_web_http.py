"""HTTP boundary for the shared SCM count (device credentials have a different API)."""
from unittest.mock import patch
from uuid import uuid4

import pytest
from flask import Flask, g
from app.api.rutas_inventario_c66 import conteo_c66_bp


@pytest.fixture
def http_app():
    app = Flask(__name__)
    app.register_blueprint(conteo_c66_bp, url_prefix="/conteo")
    return app


def test_no_web_identity_cannot_read_or_write_even_with_actor_header(http_app):
    c = http_app.test_client()
    assert c.get("/conteo/estado", headers={"X-Actor-Id":"1"}).status_code == 401
    assert c.post("/conteo/sesiones", json={}).status_code == 401


def test_web_uses_operation_key_and_keeps_adjustment_explicit(http_app):
    @http_app.before_request
    def fixture_verified_session():
        g.scm_actor_id = 73

    c = http_app.test_client()
    path = "app.api.rutas_inventario_c66"
    with patch(path + ".load_actor") as authorize, patch(path + ".warehouse_scope", return_value={"configured": False}), patch(path + ".inventory.dispatch", return_value=({"id":"test"}, 201)) as command:
        key = uuid4()
        body = {"nombre": "Conteo mañana", "zona": "Z1", "piezas": ["Tapa"]}
        assert c.post("/conteo/sesiones", json=body).status_code == 422
        assert c.post("/conteo/sesiones", json=body, headers={"Idempotency-Key":str(key)}).status_code == 201
        assert command.call_args.args[1:] == (73, "POST", "/sesiones", {**body, "clave":str(key)}, key)
        assert authorize.call_args.kwargs == {"capability":"INVENTARIO_CONTEO_C66"}
        for op in ("pesajes", "confirmaciones", "receive"):
            assert c.post("/conteo/"+op, json={}).status_code == 404


def test_web_exposes_review_queue_and_commands_with_separate_capability(http_app):
    @http_app.before_request
    def fixture_verified_session():
        g.scm_actor_id = 73

    c = http_app.test_client()
    path = "app.api.rutas_inventario_c66"
    with patch(path + ".load_actor") as authorize, \
            patch(path + ".warehouse_scope", return_value={"configured": False}), \
            patch(path + ".inventory.vista_state_revision", return_value={"revision": {"candidatas": []}}) as view, \
            patch(path + ".inventory.dispatch", return_value=({"token": "review-1"}, 201)) as command:
        assert c.get("/conteo/revision").status_code == 200
        assert view.call_args.args == (view.call_args.args[0], 73)
        assert authorize.call_args.kwargs == {"capability": "INVENTARIO_VER"}

        key = uuid4()
        body = {"codigo": "INV-MESA-001", "cobertura": True,
                "movimientos_revisados": True, "busqueda": True}
        response = c.post("/conteo/revision", json=body,
                          headers={"Idempotency-Key": str(key)})
        assert response.status_code == 201
        assert command.call_args.args[1:] == (73, "POST", "/revision", body, key)

        authorize.reset_mock()
        key = uuid4()
        body = {"codigo": "INV-MESA-001", "token": "review-1",
                "motivo": "Búsqueda dirigida completada", "clave": "ajuste-1"}
        response = c.post("/conteo/faltantes", json=body,
                          headers={"Idempotency-Key": str(key)})
        assert response.status_code == 201
        assert authorize.call_args.kwargs == {"capability": "INVENTARIO_AJUSTAR"}
        assert command.call_args.args[1:] == (73, "POST", "/faltantes", body, key)
