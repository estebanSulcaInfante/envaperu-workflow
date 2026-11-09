from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import scm_mn31_dispatch_service as service


class FakeSession:
    def __init__(self, client):
        self.client = client
        self.commits = 0
        self.rollbacks = 0
        self.events = []

    def get(self, model, key):
        return self.client

    def add(self, value):
        self.events.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _patch_batch_dependencies(monkeypatch, fake_session):
    actor = SimpleNamespace(id=7)
    client = fake_session.client
    operation = SimpleNamespace(operation_id=uuid4())
    monkeypatch.setattr(service, "assert_custody_enabled", lambda: None)
    monkeypatch.setattr(service, "load_actor", lambda *args, **kwargs: actor)
    monkeypatch.setattr(service, "acquire_kg_productive_write_lock", lambda session: None)
    monkeypatch.setattr(service, "_complete", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "actor_snapshot", lambda actor: {"id": actor.id})
    monkeypatch.setattr(service, "_reserve_operation", lambda *args, **kwargs: (operation, None))
    return actor, client


def test_batch_has_one_root_commit_and_deterministic_children(monkeypatch):
    client = SimpleNamespace(id=3, activo=True, to_dict=lambda: {"id": 3})
    session = FakeSession(client)
    _patch_batch_dependencies(monkeypatch, session)
    calls = []

    def confirm(session, **kwargs):
        calls.append(kwargs["operation_id"])
        return {"despacho": {"peso_neto_kg": "2.000"}, "operation_id": str(kwargs["operation_id"])}

    monkeypatch.setattr(service, "confirm_mn31_dispatch", confirm)
    key = uuid4()
    body = {"cliente_id": 3, "motivo": "Venta MN31", "items": [
        {"codigo": "U-2", "version": 1}, {"codigo": "U-1", "version": 1}
    ]}
    result = service.confirm_mn31_dispatch_batch(session, actor_id=7, operation_id=key, data=body)
    assert result["cantidad_mangas"] == 2
    assert result["total_kg"] == "4.000"
    assert session.commits == 1
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_batch_replay_is_exact(monkeypatch):
    client = SimpleNamespace(id=3, activo=True, to_dict=lambda: {"id": 3})
    session = FakeSession(client)
    actor = SimpleNamespace(id=7)
    monkeypatch.setattr(service, "assert_custody_enabled", lambda: None)
    monkeypatch.setattr(service, "load_actor", lambda *args, **kwargs: actor)
    monkeypatch.setattr(service, "acquire_kg_productive_write_lock", lambda session: None)
    monkeypatch.setattr(service, "actor_snapshot", lambda actor: {"id": actor.id})
    key = uuid4()
    state = {"response": None}
    root = SimpleNamespace(operation_id=key)

    def reserve(session, op_id, endpoint, actor, data):
        if op_id == key and state["response"] is not None:
            return root, state["response"]
        return SimpleNamespace(operation_id=op_id), None

    def complete(operation, response, status=200):
        if operation.operation_id == key:
            state["response"] = response

    monkeypatch.setattr(service, "_reserve_operation", reserve)
    monkeypatch.setattr(service, "_complete", complete)
    monkeypatch.setattr(service, "confirm_mn31_dispatch", lambda session, **kwargs: {"despacho": {"peso_neto_kg": "2.000"}, "operation_id": str(kwargs["operation_id"])})
    body = {"cliente_id": 3, "motivo": "Venta MN31", "items": [{"codigo": "U-1", "version": 1}]}
    first = service.confirm_mn31_dispatch_batch(session, actor_id=7, operation_id=key, data=body)
    second = service.confirm_mn31_dispatch_batch(session, actor_id=7, operation_id=key, data=body)
    assert second == first
    assert session.commits == 1


def test_batch_rolls_back_when_second_item_fails(monkeypatch):
    client = SimpleNamespace(id=3, activo=True, to_dict=lambda: {"id": 3})
    session = FakeSession(client)
    _patch_batch_dependencies(monkeypatch, session)
    count = {"value": 0}

    def confirm(session, **kwargs):
        count["value"] += 1
        if count["value"] == 2:
            raise service.ScmServiceError("MN31_WEIGHT_REQUIRED", "sin peso", status_code=409)
        return {"despacho": {"peso_neto_kg": "2.000"}, "operation_id": str(kwargs["operation_id"])}

    monkeypatch.setattr(service, "confirm_mn31_dispatch", confirm)
    with pytest.raises(service.ScmServiceError, match="sin peso"):
        service.confirm_mn31_dispatch_batch(
            session, actor_id=7, operation_id=uuid4(),
            data={"cliente_id": 3, "motivo": "Venta MN31", "items": [
                {"codigo": "U-1", "version": 1}, {"codigo": "U-2", "version": 1}
            ]},
        )
    assert session.commits == 0
    assert session.rollbacks == 1
