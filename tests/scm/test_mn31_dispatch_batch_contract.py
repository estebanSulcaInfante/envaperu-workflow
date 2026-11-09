from datetime import datetime, timezone
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
    body = {"cliente_id": 3, "documento": "DOC-MN31-01", "motivo": "Venta MN31", "items": [
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
    body = {"cliente_id": 3, "documento": "DOC-MN31-01", "motivo": "Venta MN31", "items": [{"codigo": "U-1", "version": 1}]}
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
            data={"cliente_id": 3, "documento": "DOC-MN31-01", "motivo": "Venta MN31", "items": [
                {"codigo": "U-1", "version": 1}, {"codigo": "U-2", "version": 1}
            ]},
        )
    assert session.commits == 0
    assert session.rollbacks == 1


def test_corrected_weight_snapshot_uses_effective_values_and_correction_id():
    weighing = SimpleNamespace(
        id=11,
        peso_bruto_kg=Decimal("12.000"),
        tara_kg=Decimal("1.000"),
        pesada_at=datetime(2026, 11, 9, 10, tzinfo=timezone.utc),
        pesado_por_id=21,
    )
    correction = SimpleNamespace(
        public_id=uuid4(),
        estado="APLICADA",
        result_projection_json={
            "peso_bruto_kg": "13.500",
            "tara_kg": "1.500",
            "peso_fisico_neto_kg": "12.000",
            "pesada_at": "2026-11-09T11:30:00+00:00",
        },
    )

    class SourceSession:
        def __init__(self):
            self.values = [weighing, correction]

        def scalar(self, statement):
            return self.values.pop(0)

    payload = service._source_snapshot(
        SourceSession(), "PESAJE_FINAL_CORREGIDO", uuid4(), Decimal("12.000"), weighing.pesada_at
    )
    assert payload["bruto_kg"] == "13.500"
    assert payload["tara_kg"] == "1.500"
    assert payload["neto_kg"] == "12.000"
    assert payload["pesado_at"] == "2026-11-09T11:30:00+00:00"
    assert payload["correccion_aplicada_id"] == str(correction.public_id)


def test_productive_change_cannot_reuse_documented_lot(monkeypatch):
    candidate = SimpleNamespace(
        lote_codigo="MN31-20261109-D-01",
        manga=SimpleNamespace(
            ot=SimpleNamespace(fecha=datetime(2026, 11, 9).date(), turno="DIA"),
            pieza_color_sku_snapshot="PC-000468",
        ),
        to_dict=lambda: {"codigo": "MN31-20261109-D-01"},
    )
    manga = SimpleNamespace(
        id=2,
        ot=SimpleNamespace(fecha=datetime(2026, 11, 9).date(), turno="DIA"),
        pieza_color_sku_snapshot="PC-000468",
    )

    class LotSession:
        def scalar(self, statement):
            return None

        def scalars(self, statement):
            return SimpleNamespace(all=lambda: [candidate])

    with pytest.raises(service.ScmServiceError) as error:
        service._lot_for_manga(
            LotSession(), manga,
            selected_code="MN31-20261109-D-01",
            productive_change=True,
        )
    assert error.value.code == "MN31_LOT_SELECTION_REQUIRED"
    assert error.value.details["motivo"] == "CAMBIO_PRODUCTIVO_REQUIERE_NUEVO_LOTE"
