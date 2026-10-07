from types import SimpleNamespace
from uuid import UUID, uuid4
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.services.scm_postpesaje_reprint_service import (
    _hash,
    _normalized_items,
    _job_dict,
    acknowledge_reprint_job,
)
from app.services.scm_service_support import ScmServiceError


def test_real_postpesaje_preview_confirm_claim_ack_contract(app):
    """Exercise the source resolver with the repository's real OT fixture."""
    from test_scm_ot_service import _print_color_manga, _seed_fabrication_order, acknowledge_station_print_job
    from app.extensions import db
    from app.models.scm_ot import ScmEtiquetaManga, ScmPesajeManga, ScmTrabajoImpresionManga
    from app.models.scm_catalogos import ScmCapacidad
    from app.models.trabajador import RolOperativo
    from app.services.scm_ot_service import create_fabrication_ot, generate_prelabels, transition_color_work
    from app.services.scm_weighing_service import confirm_manga_weighing

    app.config["POSTPESAJE_REPRINT_ENABLED"] = True
    with app.app_context():
        creator, _approver, order, run, _output = _seed_fabrication_order()
        capability = ScmCapacidad(codigo="MANGA_ETIQUETA_POST_REIMPRIMIR", nombre="Reimpresion postpesaje", activo=True)
        role = RolOperativo(codigo="REPRINT_FIXTURE", nombre="REPRINT_FIXTURE", activo=True, capacidades=[capability])
        creator.roles.append(role)
        db.session.add(role)
        db.session.commit()
        from app.services.scm_ot_service import recalculate_fabrication_manga_plan
        plan = recalculate_fabrication_manga_plan(db.session, actor_id=creator.id, order_id=order.id, operation_id=uuid4(), data={})["plan"]
        created = create_fabrication_ot(
            db.session, actor_id=creator.id, order_id=order.id, operation_id=uuid4(),
            data={"corrida_fabricacion_id": str(run.id), "fecha_operativa": "2026-10-07", "turno": "DIA", "maquinista_id": creator.id, "asignaciones": [{"plan_linea_id": plan["lineas"][0]["id"], "cantidad_un": 50}]},
        )
        work = created["trabajo_color"]
        manga_id = created["trabajo_color"]["mangas"][0]["public_id"]
        station, prelabel = _print_color_manga(actor=creator, manga_id=manga_id, station_code="PESAJE-REPRINT-CONTRACT")
        transition_color_work(db.session, actor_id=creator.id, work_id=UUID(work["id"]), operation_id=uuid4(), data={"version": work["version"]}, action="iniciar")
        weighed = confirm_manga_weighing(
            db.session, station_id=station.station_id, operation_id=uuid4(), actor_id=creator.id,
            data={"label_id": prelabel["public_id"], "capture_id": str(uuid4()), "peso_bruto_kg": "5.100", "tara_kg": "0.100", "tara_fuente": "TIPO_MANGA", "pesada_at": "2026-10-07T15:00:00-05:00", "reading_stable": True},
        )
        post = ScmEtiquetaManga.query.filter_by(public_id=UUID(weighed["post_label"]["public_id"])).one()
        original_payload = deepcopy(post.payload_json)
        original_hash = post.payload_hash
        original_job = db.session.get(ScmTrabajoImpresionManga, UUID(weighed["print_job_id"]))
        original_job_payload = deepcopy(original_job.payload_hash)
        acknowledge_station_print_job(
            db.session, station_id=station.station_id, print_job_id=UUID(weighed["print_job_id"]),
            data={"results": [{"label_id": str(post.public_id), "estado": "IMPRESA", "printer_name": "TSC"}]},
        )
        pesaje = ScmPesajeManga.query.filter_by(manga_id=post.manga_id, estado="VIGENTE").one()
        import app.services.scm_postpesaje_reprint_service as service
        preview = service.preview_reprint(db.session, actor_id=creator.id, data={"station_id": station.station_id, "items": [{"source_label_id": str(post.public_id), "pesaje_id": str(pesaje.public_id), "copias": 2}]})
        confirmed = service.confirm_reprint(db.session, actor_id=creator.id, data={"operation_id": str(uuid4()), "station_id": station.station_id, "motivo": "Contrato edge", "preview_digest": preview["preview_digest"], "items": [{"source_label_id": str(post.public_id), "pesaje_id": str(pesaje.public_id), "copias": 2}]})
        attempt_id = uuid4()
        claim = service.claim_reprint_job(db.session, station_id=station.station_id, job_id=UUID(confirmed["jobs"][0]["copy_job_id"]), attempt_id=attempt_id)
        assert claim["job_kind"] == "POSTPESAJE_COPY"
        assert claim["renderer_version"] == "POSTPESAJE_COPY_TSPL_1"
        assert claim["copias"] == 2
        assert claim["payload"]["document_type"] == "POSTPESAJE"
        assert "qr" not in claim["payload"]
        receipt = service.acknowledge_reprint_job(db.session, station_id=station.station_id, job_id=UUID(claim["copy_job_id"]), data={"attempt_id": str(attempt_id), "result": "NOT_EMITTED", "expected_bytes": 100, "bytes_written": 0, "document_started": False, "write_attempted": True, "simulated": False, "job_id": 77, "error": "fixture", "rendered_payload_hash": claim["payload_hash"], "renderer_version": "POSTPESAJE_COPY_TSPL_1", "printer_name": "FIXTURE"})
        assert receipt["result"] == "NOT_EMITTED"
        from app.services.scm_weighing_service import current_postpesaje_source
        source = current_postpesaje_source(db.session, post.manga)
        assert source["label"]["public_id"] == str(post.public_id)
        assert source["pesaje"] == {"public_id": str(pesaje.public_id), "estado": "VIGENTE"}
        db.session.refresh(post)
        db.session.refresh(original_job)
        assert post.payload_json == original_payload
        assert post.payload_hash == original_hash
        assert original_job.payload_hash == original_job_payload
        out = Path(__file__).resolve().parents[2].parent / "output" / "post_reprint_contract_claim.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(claim, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _job(*, state="CLAIMED", ack=None):
    source_label_id = uuid4()
    pesaje_id = uuid4()
    attempt_id = uuid4()
    request = SimpleNamespace(request_id=uuid4(), items=[])
    item = SimpleNamespace(
        item_id=uuid4(),
        request=request,
        source_label_id=source_label_id,
        source_snapshot_json={"weighing_id": str(pesaje_id)},
        job=None,
    )
    row = SimpleNamespace(
        job_id=uuid4(),
        item=item,
        station_id="ST-01",
        renderer_version="POSTPESAJE_COPY_TSPL_1",
        authorized_copies=2,
        source_payload_hash="a" * 64,
        source_payload_json={"document_type": "POSTPESAJE"},
        estado=state,
        attempt_id=attempt_id,
        claimed_at=None,
        acknowledged_at=None,
        ack_result_json=ack,
    )
    item.job = row
    request.items = [item]
    return row, attempt_id


def test_job_contract_is_flat_and_preserves_pending_mapping():
    row, attempt_id = _job(state="QUEUED")
    payload = _job_dict(row)
    assert payload["copy_job_id"] == str(row.job_id)
    assert payload["job_kind"] == "POSTPESAJE_COPY"
    assert payload["renderer_version"] == "POSTPESAJE_COPY_TSPL_1"
    assert payload["copias"] == 2
    assert payload["payload_hash"] == "a" * 64
    assert payload["payload"]["document_type"] == "POSTPESAJE"
    assert payload["estado"] == "PENDING"
    assert payload["attempt_id"] == str(attempt_id)


def test_items_require_explicit_pesaje_id_before_source_lookup():
    with pytest.raises(ScmServiceError) as error:
        _normalized_items(SimpleNamespace(), [{"source_label_id": str(uuid4()), "copias": 1}], "ST-01")
    assert error.value.code == "INVALID_UUID"
    assert "pesaje_id" in error.value.message


def test_ack_replay_is_idempotent_and_different_evidence_is_rejected(app):
    app.config["POSTPESAJE_REPRINT_ENABLED"] = True
    with app.app_context():
        row, attempt_id = _job(state="ACK_ACCEPTED")
        row.ack_result_json = {
            "attempt_id": str(attempt_id), "result": "ACCEPTED", "expected_bytes": 12,
            "bytes_written": 12, "document_started": True, "write_attempted": True,
            "simulated": False, "job_id": 73, "rendered_payload_hash": "b" * 64,
            "renderer_version": "POSTPESAJE_COPY_TSPL_1", "printer_name": "P-01",
        }

        class Session:
            def get(self, _model, _id):
                return SimpleNamespace(estado_admin="ACTIVA")

            def scalar(self, _query):
                return row

        session = Session()
        ack = {
            "attempt_id": str(attempt_id), "result": "ACCEPTED", "expected_bytes": 12,
            "bytes_written": 12, "document_started": True, "write_attempted": True,
            "simulated": False, "job_id": 73, "rendered_payload_hash": "b" * 64,
            "renderer_version": "POSTPESAJE_COPY_TSPL_1", "printer_name": "P-01",
        }
        replay = acknowledge_reprint_job(session, station_id="ST-01", job_id=row.job_id, data=ack)
        assert replay["result"] == "ACCEPTED"
        assert replay["job_id"] == 73
        assert replay["copy_job_id"] == str(row.job_id)
        changed = dict(ack, bytes_written=11)
        with pytest.raises(ScmServiceError) as error:
            acknowledge_reprint_job(session, station_id="ST-01", job_id=row.job_id, data=changed)
        assert error.value.code == "ACK_ALREADY_RECORDED"


def test_hash_is_canonical_for_payload_contract():
    assert _hash({"b": 2, "a": 1}) == _hash({"a": 1, "b": 2})


def test_feature_flag_and_capability_are_enforced_without_actor_bypass(app, client):
    response = client.post(
        "/api/scm/v1/reimpresiones-postpesaje/preview",
        headers={"X-Actor-Id": "1"},
        json={},
    )
    assert response.status_code == 404
    app.config["POSTPESAJE_REPRINT_ENABLED"] = True
    response = client.post(
        "/api/scm/v1/reimpresiones-postpesaje/preview",
        headers={"X-Actor-Id": "1"},
        json={},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("state", ["GENERADA", "FALLIDA_SIN_EMISION", "EMISION_INCIERTA"])
def test_pending_original_post_label_is_not_a_current_source(state):
    from app.services.scm_weighing_service import current_postpesaje_source
    label = SimpleNamespace(tipo="POSTPESAJE", estado=state, version=1, id=1)
    manga = SimpleNamespace(estado="PESADA", id=1, etiquetas=[label])
    class Session:
        def scalar(self, _query):
            raise AssertionError("pending original must be rejected before weighing lookup")
    assert current_postpesaje_source(Session(), manga) is None


def test_exact_replay_returns_ledger_before_stale_source_revalidation(app, monkeypatch):
    import app.services.scm_postpesaje_reprint_service as service
    station_id = str(uuid4())
    operation_id = uuid4()
    data = {
        "operation_id": str(operation_id), "station_id": station_id,
        "motivo": "replay", "preview_digest": "d" * 64,
        "items": [{"source_label_id": str(uuid4()), "pesaje_id": str(uuid4()), "copias": 1}],
    }
    fingerprint = _hash({"actor_id": 5, "motivo": "replay", "station_id": station_id, "operation_id": str(operation_id), "preview_digest": "d" * 64, "items": data["items"]})
    existing = SimpleNamespace(request_id=uuid4(), operation_id=operation_id, actor_id=5, station_id=station_id, motivo="replay", preview_digest="d" * 64, renderer_version="POSTPESAJE_COPY_TSPL_1", estado="QUEUED", created_at=None, items=[], request_fingerprint=fingerprint)
    monkeypatch.setattr(service, "load_actor", lambda session, actor_id, capability=None: SimpleNamespace(id=5))

    class Session:
        def get(self, _model, _id):
            return SimpleNamespace(estado_admin="ACTIVA")
        def scalar(self, _query):
            return existing

    with app.app_context():
        app.config["POSTPESAJE_REPRINT_ENABLED"] = True
        replay = service.confirm_reprint(Session(), actor_id=5, data=data)
    assert replay["operation_id"] == str(operation_id)


def test_confirm_recovers_unique_race_raised_by_first_flush(app, monkeypatch):
    import app.services.scm_postpesaje_reprint_service as service
    from sqlalchemy.exc import IntegrityError
    station_id, operation_id = str(uuid4()), uuid4()
    label_id, pesaje_id, manga_id, weighing_id = uuid4(), uuid4(), 1, 1
    data = {"operation_id": str(operation_id), "station_id": station_id, "motivo": "race", "preview_digest": "e" * 64, "items": [{"source_label_id": str(label_id), "pesaje_id": str(pesaje_id), "copias": 1}]}
    item = {"sequence": 1, "source_label_id": label_id, "manga_id": manga_id, "weighing_id": weighing_id, "source_pesaje_id": pesaje_id, "copies": 1, "source_payload_hash": "a" * 64, "source_payload": {"document_type": "POSTPESAJE"}, "source_snapshot": {"weighing_id": str(pesaje_id), "snapshot_hash": "b" * 64}, "source_snapshot_hash": "b" * 64, "manga_public_id": str(uuid4()), "manga_codigo": "M-1", "weighing_public_id": str(pesaje_id)}
    monkeypatch.setattr(service, "load_actor", lambda session, actor_id, capability=None: SimpleNamespace(id=5))
    monkeypatch.setattr(service, "_normalized_items", lambda session, raw, station: ([item], 1, "e" * 64))
    fingerprint = _hash({"actor_id": 5, "motivo": "race", "station_id": station_id, "operation_id": str(operation_id), "preview_digest": "e" * 64, "items": data["items"]})
    existing = SimpleNamespace(request_id=uuid4(), operation_id=operation_id, actor_id=5, station_id=station_id, motivo="race", preview_digest="e" * 64, renderer_version="POSTPESAJE_COPY_TSPL_1", estado="QUEUED", created_at=None, items=[], request_fingerprint=fingerprint)

    class RacingSession:
        def __init__(self): self.reads = 0
        def get(self, _model, _id): return SimpleNamespace(estado_admin="ACTIVA")
        def scalar(self, _query):
            self.reads += 1
            return None if self.reads == 1 else existing
        def add(self, _row): pass
        def flush(self): raise IntegrityError("INSERT", {}, RuntimeError("duplicate"))
        def rollback(self): pass

    with app.app_context():
        app.config["POSTPESAJE_REPRINT_ENABLED"] = True
        replay = service.confirm_reprint(RacingSession(), actor_id=5, data=data)
    assert replay["operation_id"] == str(operation_id)
