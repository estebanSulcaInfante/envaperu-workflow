import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from flask import Flask, g

from app.api.rutas_scm_assistant import scm_assistant_bp
from app.extensions import db
from app.services import scm_agent_chat_gateway as gateway


OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture
def app():
    application = Flask(__name__)
    application.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SCM_AUTH_MODE="supabase",
        SCM_ASSISTANT_ENABLED="true",
        SCM_ASSISTANT_ACTOR_ID="42",
        SCM_ASSISTANT_AUTH_USER_ID=OWNER,
        SCM_ASSISTANT_PROVIDER="openclaw",
        SCM_AGENT_CHAT_ENABLED="true",
        SCM_OPENCLAW_TOKEN="test-token",
        SCM_OPENCLAW_POLICY_CONFIRMED="true",
        SCM_OPENCLAW_MODEL_VERIFIED="true",
        SCM_OPENCLAW_BACKEND_MODEL="test/verified-model",
    )
    db.init_app(application)

    @application.before_request
    def verified_auth():
        g.scm_actor = SimpleNamespace(
            id=42,
            activo=True,
            auth_user_id=UUID(OWNER),
            tiene_capacidad=lambda _capability: True,
        )
        g.scm_auth_claims = {"sub": OWNER}

    application.register_blueprint(scm_assistant_bp, url_prefix="/api/scm/v1")
    return application


def valid_body(**overrides):
    body = {"query": "¿Qué órdenes están en curso?", "history": [], "client_request_id": str(uuid4())}
    body.update(overrides)
    return body


def ready_response(answer="Respuesta de solo lectura"):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": answer},
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


def test_status_contract_requires_owner_and_reports_ready(app):
    response = app.test_client().get("/api/scm/v1/asistente/agente/estado")
    assert response.status_code == 200
    assert response.json == {
        "available": True,
        "status": "READY",
        "message": "Proveedor OpenClaw disponible para consultas de solo lectura.",
        "provider": "openclaw",
        "read_only": True,
        "attachments_supported": False,
    }

    app.config["SCM_ASSISTANT_ACTOR_ID"] = "99"
    response = app.test_client().get("/api/scm/v1/asistente/agente/estado")
    assert response.status_code == 403
    assert response.json["error"]["code"] == "PROVIDER_OWNER_REQUIRED"


@pytest.mark.parametrize("enabled", ["false", None])
def test_agent_kill_switch_is_closed_before_provider(app, monkeypatch, enabled):
    if enabled is None:
        app.config.pop("SCM_AGENT_CHAT_ENABLED", None)
        monkeypatch.delenv("SCM_AGENT_CHAT_ENABLED", raising=False)
    else:
        app.config["SCM_AGENT_CHAT_ENABLED"] = enabled
    monkeypatch.setattr(gateway, "complete_agent_chat", lambda *args: pytest.fail("provider must not run"))

    status = app.test_client().get("/api/scm/v1/asistente/agente/estado")
    assert status.status_code == 200
    assert status.json["available"] is False
    assert status.json["status"] == "UNAVAILABLE"

    response = app.test_client().post("/api/scm/v1/asistente/agente/chat", json=valid_body())
    assert response.status_code == 503
    assert response.json["error"]["code"] == "AGENT_DISABLED"


def test_chat_sends_stateless_plain_messages_without_tools(app, monkeypatch):
    captured = {}

    def fake_complete(query, history, client_request_id, config):
        captured.update(query=query, history=history, request_id=client_request_id, config=config)
        return {"status": "answered", "answer": "Respuesta de solo lectura", "usage": {"total_tokens": 7}}

    monkeypatch.setattr(gateway, "complete_agent_chat", fake_complete)
    body = valid_body(history=[{"role": "user", "content": "Hola"}, {"role": "assistant", "content": "Hola."}])
    response = app.test_client().post("/api/scm/v1/asistente/agente/chat", json=body)
    assert response.status_code == 200
    assert response.json["status"] == "answered"
    assert response.json["answer"] == "Respuesta de solo lectura"
    assert response.json["client_request_id"] == body["client_request_id"]
    assert response.json["provider"] == {"status": "READY", "usage": {"total_tokens": 7}}
    assert response.json["sources"] == []
    assert response.json["read_only"] is True
    assert captured["config"]["SCM_ASSISTANT_PROVIDER"] == "openclaw"


def test_plain_text_does_not_reject_literal_angle_brackets(app, monkeypatch):
    monkeypatch.setattr(
        gateway,
        "complete_agent_chat",
        lambda query, history, request_id, config: {
            "status": "answered",
            "answer": "Comparación: <10 y >5",
            "usage": None,
        },
    )
    response = app.test_client().post(
        "/api/scm/v1/asistente/agente/chat",
        json=valid_body(query="Compara <10 y >5"),
    )
    assert response.status_code == 200
    assert response.json["answer"] == "Comparación: <10 y >5"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": "x", "history": [], "client_request_id": str(uuid4()), "extra": 1},
        {"query": "x", "history": [{"role": "assistant", "content": "x"}], "client_request_id": str(uuid4())},
        {"query": "x", "history": [{"role": "system", "content": "x"}], "client_request_id": str(uuid4())},
        {"query": "x", "history": [{"role": "user", "content": "x"}], "client_request_id": "not-uuid"},
    ],
)
def test_chat_rejects_invalid_shape_without_calling_provider(app, monkeypatch, body):
    monkeypatch.setattr(gateway, "complete_agent_chat", lambda *args: pytest.fail("provider must not run"))
    response = app.test_client().post("/api/scm/v1/asistente/agente/chat", json=body)
    assert response.status_code == 400


def test_chat_provider_failure_is_sanitized(app, monkeypatch):
    monkeypatch.setattr(gateway, "complete_agent_chat", lambda *args: {"status": "TIMEOUT"})
    response = app.test_client().post("/api/scm/v1/asistente/agente/chat", json=valid_body())
    assert response.status_code == 504
    assert response.json["error"]["code"] == "AGENT_TIMEOUT"
    assert "raw provider" not in response.get_data(as_text=True)


def test_chat_rejects_oversized_serialized_payload(app, monkeypatch):
    monkeypatch.setattr(gateway, "complete_agent_chat", lambda *args: pytest.fail("provider must not run"))
    body = valid_body(
        history=[
            {"role": "user", "content": "x" * 4000},
            {"role": "assistant", "content": "x" * 4000},
            {"role": "user", "content": "x" * 4000},
            {"role": "assistant", "content": "x" * 4000},
        ]
    )
    response = app.test_client().post("/api/scm/v1/asistente/agente/chat", json=body)
    assert response.status_code == 413


def test_service_payload_and_transport_constraints(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_content(self, _size):
            yield json.dumps(ready_response()).encode("utf-8")

    class Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return Response()

    monkeypatch.setattr(gateway.requests, "Session", Session)
    result = gateway.complete_agent_chat(
        "Consulta",
        [{"role": "user", "content": "Anterior"}, {"role": "assistant", "content": "Respuesta"}],
        str(uuid4()),
        {
            "SCM_ASSISTANT_PROVIDER": "openclaw",
            "SCM_AGENT_CHAT_ENABLED": "true",
            "SCM_OPENCLAW_TOKEN": "test-token",
            "SCM_OPENCLAW_POLICY_CONFIRMED": "true",
            "SCM_OPENCLAW_MODEL_VERIFIED": "true",
            "SCM_OPENCLAW_BACKEND_MODEL": "test/verified-model",
        },
    )
    assert result["status"] == "answered"
    payload = captured["kwargs"]["json"]
    assert payload["model"] == "openclaw/scm-personal"
    assert "tool_choice" not in payload and "tools" not in payload
    assert all(message["role"] != "system" for message in payload["messages"])
    assert captured["kwargs"]["timeout"] == (3, 42)
    assert captured["kwargs"]["allow_redirects"] is False
    assert captured["kwargs"]["verify"] is True
    assert captured["kwargs"]["headers"]["x-openclaw-model"] == "test/verified-model"
    assert captured["kwargs"]["headers"]["X-Forwarded-Proto"] == "https"
    assert captured["kwargs"]["headers"]["X-Forwarded-For"]


def _service_config(**overrides):
    config = {
        "SCM_ASSISTANT_PROVIDER": "openclaw",
        "SCM_AGENT_CHAT_ENABLED": "true",
        "SCM_OPENCLAW_TOKEN": "test-token",
        "SCM_OPENCLAW_POLICY_CONFIRMED": "true",
        "SCM_OPENCLAW_MODEL_VERIFIED": "true",
        "SCM_OPENCLAW_BACKEND_MODEL": "test/verified-model",
    }
    config.update(overrides)
    return config


def _mock_session(monkeypatch, body, *, status_code=200):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_content(self, _size):
            yield body

    class MockSession:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured.update(url=url, kwargs=kwargs, trust_env=self.trust_env)
            response = Response()
            response.status_code = status_code
            return response

    monkeypatch.setattr(gateway.requests, "Session", MockSession)
    return captured


@pytest.mark.parametrize(
    "url",
    ["http://example.com:18789", "https://example.com:18789", "https://127.0.0.1:18789/path"],
)
def test_service_rejects_non_loopback_or_non_exact_destination(monkeypatch, url):
    monkeypatch.setattr(gateway.requests, "Session", lambda: pytest.fail("network must not run"))
    result = gateway._complete({}, _service_config(SCM_OPENCLAW_URL=url))
    assert result["status"] == "UNAVAILABLE"


def test_service_accepts_token_file_and_ca_file(tmp_path, monkeypatch):
    token_file = tmp_path / "openclaw.token"
    token_file.write_text("file-token\n", encoding="utf-8")
    ca_file = tmp_path / "openclaw-ca.crt"
    ca_file.write_text("test-ca", encoding="utf-8")
    captured = _mock_session(monkeypatch, json.dumps(ready_response()).encode("utf-8"))
    result = gateway._complete(
        {"model": "openclaw/scm-personal", "messages": []},
        _service_config(
            SCM_OPENCLAW_TOKEN="",
            SCM_OPENCLAW_TOKEN_FILE=str(token_file),
            SCM_OPENCLAW_CA_FILE=str(ca_file),
            SCM_OPENCLAW_URL="https://openclaw-scm:18789",
            SCM_OPENCLAW_PRIVATE_HOST="openclaw-scm",
        ),
    )
    assert result["status"] == "answered"
    assert captured["kwargs"]["verify"] == str(ca_file)
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer file-token"
    assert captured["trust_env"] is False


@pytest.mark.parametrize(
    "model_config",
    [
        {"SCM_OPENCLAW_BACKEND_MODEL": "", "SCM_OPENCLAW_MODEL_VERIFIED": ""},
        {"SCM_OPENCLAW_BACKEND_MODEL": None, "SCM_OPENCLAW_MODEL_VERIFIED": None},
    ],
)
def test_empty_or_absent_model_override_uses_fixed_agent_model(monkeypatch, model_config):
    captured = _mock_session(monkeypatch, json.dumps(ready_response()).encode("utf-8"))
    config = _service_config(**model_config)
    if model_config["SCM_OPENCLAW_BACKEND_MODEL"] is None:
        config.pop("SCM_OPENCLAW_BACKEND_MODEL")
        config.pop("SCM_OPENCLAW_MODEL_VERIFIED")
    result = gateway._complete(
        {"model": "openclaw/scm-personal", "messages": []},
        config,
    )
    assert result["status"] == "answered"
    assert captured["kwargs"]["json"]["model"] == "openclaw/scm-personal"
    assert "x-openclaw-model" not in captured["kwargs"]["headers"]


def test_nonempty_unverified_model_override_remains_closed(monkeypatch):
    monkeypatch.setattr(gateway.requests, "Session", lambda: pytest.fail("network must not run"))
    result = gateway._complete(
        {},
        _service_config(SCM_OPENCLAW_BACKEND_MODEL="custom/model", SCM_OPENCLAW_MODEL_VERIFIED="false"),
    )
    assert result["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("change", [
    {"SCM_OPENCLAW_TOKEN_FILE": "missing-token-file", "SCM_OPENCLAW_TOKEN": ""},
    {"SCM_OPENCLAW_CA_FILE": "missing-ca-file"},
])
def test_service_invalid_secret_or_ca_never_reaches_network(monkeypatch, change):
    monkeypatch.setattr(gateway.requests, "Session", lambda: pytest.fail("network must not run"))
    result = gateway._complete({}, _service_config(**change))
    assert result["status"] == "UNAVAILABLE"


@pytest.mark.parametrize(
    "response_kind", ["malformed", "tool_calls", "bad_finish", "oversized"],
)
def test_service_rejects_malformed_tool_bad_finish_and_oversized_output(monkeypatch, response_kind):
    response_body = {
        "malformed": b"not-json",
        "tool_calls": json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "x", "tool_calls": [{"id": "shell"}]}}]}).encode("utf-8"),
        "bad_finish": json.dumps({"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "x"}}]}).encode("utf-8"),
        "oversized": json.dumps(ready_response()).encode("utf-8") + b"x" * (128 * 1024),
    }[response_kind]
    _mock_session(monkeypatch, response_body)
    with pytest.raises(gateway.AgentGatewayError) as raised:
        gateway.complete_agent_chat("Consulta", [], str(uuid4()), _service_config())
    assert raised.value.status_code == 503
    assert raised.value.code == "AGENT_UNAVAILABLE"


def test_service_busy_is_nonblocking_and_timeout_releases_slot(monkeypatch):
    assert gateway._IN_FLIGHT.acquire(blocking=False)
    try:
        with pytest.raises(gateway.AgentGatewayError) as raised:
            gateway.complete_agent_chat("Consulta", [], str(uuid4()), _service_config())
        assert raised.value.code == "AGENT_BUSY"
    finally:
        gateway._IN_FLIGHT.release()

    monkeypatch.setattr(gateway, "_complete", lambda *_args: {"status": "TIMEOUT", "usage": None})
    with pytest.raises(gateway.AgentGatewayError) as raised:
        gateway.complete_agent_chat("Consulta", [], str(uuid4()), _service_config())
    assert raised.value.code == "AGENT_TIMEOUT"

    monkeypatch.setattr(gateway, "_complete", lambda *_args: {"status": "answered", "answer": "ok", "usage": None})
    assert gateway.complete_agent_chat("Consulta", [], str(uuid4()), _service_config())["answer"] == "ok"


def test_requests_timeout_maps_to_504_without_leaking_provider_error(monkeypatch):
    class TimeoutSession:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            raise gateway.requests.Timeout("provider detail must not escape")

    monkeypatch.setattr(gateway.requests, "Session", TimeoutSession)
    with pytest.raises(gateway.AgentGatewayError) as raised:
        gateway.complete_agent_chat("Consulta", [], str(uuid4()), _service_config())
    assert raised.value.status_code == 504
    assert raised.value.code == "AGENT_TIMEOUT"
    assert "provider detail" not in str(raised.value)


@pytest.mark.parametrize("enabled", ["false", None])
def test_service_kill_switch_never_invokes_session(monkeypatch, enabled):
    config = _service_config(SCM_AGENT_CHAT_ENABLED=enabled) if enabled is not None else _service_config()
    if enabled is None:
        config.pop("SCM_AGENT_CHAT_ENABLED")
    monkeypatch.setattr(gateway.requests, "Session", lambda: pytest.fail("network must not run"))
    with pytest.raises(gateway.AgentGatewayError) as raised:
        gateway.complete_agent_chat("Consulta", [], str(uuid4()), config)
    assert raised.value.code == "AGENT_DISABLED"
    assert raised.value.status_code == 503

