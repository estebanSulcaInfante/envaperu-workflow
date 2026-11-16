from uuid import UUID, uuid4
from datetime import datetime
from types import SimpleNamespace

from test_scm_production_observability import _actor
from test_scm_ot_service import _seed_fabrication_order, _seed_two_work_aggregate
from app.extensions import db
from app.models.scm_quality import ScmVisitaCalidad
from app.models.registro import RegistroDiarioProduccion
from app.models.trabajador import RolOperativo
from app.services.scm_quality_visit_service import _initial_cutoff, _initial_window_closed
from app.services.scm_ot_service import create_fabrication_ot_header


def _headers(actor_id, operation_id=None):
    return {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(operation_id or uuid4()),
    }


def _register_actor():
    return _actor(
        "CAL-REG",
        {"OT_VER", "OF_VER", "CALIDAD_CONTROL_REGISTRAR"},
    )


def _legacy_checks():
    return {
        "peso": {"status": "MEDIDO", "grams": "112"},
        "ciclo": {"status": "MEDIDO", "seconds": 12},
        "color": {"status": "OK"},
        "inyeccion": {"status": "OK"},
        "funcional": {"status": "NA"},
    }


def _wire_payload(client, ot_id, work_id, actor_id):
    projection = client.get(
        f"/api/scm/v1/ots/{ot_id}?vista=calidad",
        headers={"X-Actor-Id": str(actor_id)},
    ).get_json()["ot"]
    work = next(item for item in projection["trabajos_color"] if str(item["id"]) == str(work_id))
    output_id = work["articulos_salida"][0]["id"]
    return output_id, {
        "outputIds": [output_id],
        "checks": {
            "outputChecks": {str(output_id): {"netGrams": "112", "quantity": 5, "classification": "CORRECTO"}},
            "commonChecks": {
                "ciclo": {"seconds": 12, "classification": "CORRECTO"},
                "color": {"status": "OK", "scopeOutputIds": [output_id]},
                "inyeccion": {"status": "OK", "scopeOutputIds": [output_id]},
                "funcional": {"status": "NA", "scopeOutputIds": [output_id]},
            },
        },
    }


def test_shared_quality_draft_survives_reload_and_is_visible_to_second_actor(
    app, client, scm_config,
):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        first_actor = _register_actor()
        second_actor = _actor("CAL-READ", {"OT_VER", "OF_VER"})
        ot_id = seeded["header"]["public_id"]
        work_id = seeded["first"]["trabajo_color"]["id"]
        response = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={
                "trabajo_id": work_id,
                "tipo": "PERIODICA",
                "numero": 2,
                "observed_at": "2026-11-14T14:00:00-05:00",
                "payload": {"pieceKeys": [f"piece:{work_id}:output"], "checks": {f"piece:{work_id}:output": _legacy_checks()}},
            },
            headers=_headers(first_actor.id),
        )
        assert response.status_code == 200, response.get_json()
        visit = response.get_json()["visit"]
        assert visit["estado"] == "DRAFT"
        assert visit["version"] == 1

        db_response = client.get(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            headers={"X-Actor-Id": str(second_actor.id)},
        )
        assert db_response.status_code == 200
        piece_key = f"piece:{work_id}:output"
        assert db_response.get_json()["items"][0]["payload"]["checks"][piece_key]["peso"]["grams"] == "112"


def test_quality_confirm_numbers_are_unique_per_ot_and_stale_updates_conflict(
    app, client, scm_config,
):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = _register_actor()
        ot_id = seeded["header"]["public_id"]
        work_ids = [
            seeded["first"]["trabajo_color"]["id"],
            seeded["second"]["trabajo_color"]["id"],
        ]
        visits = []
        for work_id in work_ids:
            _output_id, quality_payload = _wire_payload(client, ot_id, work_id, actor.id)
            saved = client.patch(
                f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
                    json={"trabajo_id": work_id, "tipo": "PERIODICA", "numero": 2, "observed_at": "2026-11-14T14:00:00-05:00", "payload": quality_payload},
                headers=_headers(actor.id),
            )
            assert saved.status_code == 200
            visits.append(saved.get_json()["visit"])
        assert [visit["numero"] for visit in visits] == [2, 3]
        # Reproduce a legacy collision created before slot allocation existed.
        db.session.get(ScmVisitaCalidad, UUID(visits[1]["id"])).numero = 2
        db.session.commit()

        stale = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={
                "id": visits[0]["id"], "trabajo_id": work_ids[0], "tipo": "PERIODICA",
                    "numero": 2, "version": 99, "observed_at": "2026-11-14T14:00:00-05:00", "payload": quality_payload,
            },
            headers=_headers(actor.id),
        )
        assert stale.status_code == 409
        assert stale.get_json()["error"]["code"] == "VERSION_CONFLICT"

        confirmed = client.put(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas/{visits[0]['id']}/confirmar",
            json={"version": visits[0]["version"]},
            headers=_headers(actor.id),
        )
        assert confirmed.status_code == 200
        assert confirmed.get_json()["visit"]["estado"] == "CONFIRMED"

        conflict = client.put(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas/{visits[1]['id']}/confirmar",
            json={"version": visits[1]["version"]},
            headers=_headers(actor.id),
        )
        assert conflict.status_code == 200
        assert conflict.get_json()["visit"]["numero"] == 3


def test_quality_confirm_is_idempotent_for_same_operation(app, client, scm_config):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = _register_actor()
        ot_id = seeded["header"]["public_id"]
        work_id = seeded["first"]["trabajo_color"]["id"]
        _output_id, quality_payload = _wire_payload(client, ot_id, work_id, actor.id)
        saved = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
                json={"trabajo_id": work_id, "tipo": "PERIODICA", "numero": 2, "observed_at": "2026-11-14T14:00:00-05:00", "payload": quality_payload},
            headers=_headers(actor.id),
        ).get_json()["visit"]
        operation_id = uuid4()
        command = client.put(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas/{saved['id']}/confirmar",
            json={"version": saved["version"]},
            headers=_headers(actor.id, operation_id),
        )
        replay = client.put(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas/{saved['id']}/confirmar",
            json={"version": saved["version"]},
            headers=_headers(actor.id, operation_id),
        )
        assert command.status_code == replay.status_code == 200
        assert command.get_json() == replay.get_json()


def test_quality_workstation_payload_is_persisted_without_piece_shape(app, client, scm_config):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = _register_actor()
        db.session.commit()
        ot_id = seeded["header"]["public_id"]
        work_id = seeded["first"]["trabajo_color"]["id"]
        projection = client.get(
            f"/api/scm/v1/ots/{ot_id}?vista=calidad",
            headers={"X-Actor-Id": str(actor.id)},
        )
        assert projection.status_code == 200
        output_id = projection.get_json()["ot"]["trabajos_color"][0]["articulos_salida"][0]["id"]
        other_output_id = 999999
        wrong_scope = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={
                "trabajo_id": work_id, "tipo": "PERIODICA", "numero": 2,
                "observed_at": "2026-11-14T14:00:00-05:00",
                "payload": {"outputIds": [other_output_id], "checks": {"outputChecks": {str(other_output_id): {"netGrams": "112", "quantity": 5, "classification": "CORRECTO"}}, "commonChecks": {}}},
            },
            headers=_headers(actor.id),
        )
        assert wrong_scope.status_code == 422
        response = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={
                "trabajo_id": work_id,
                "tipo": "PERIODICA",
                "numero": 2,
                "observed_at": "2026-11-14T14:00:00-05:00",
                "payload": {
                    "outputIds": [output_id],
                    "checks": {
                        "outputChecks": {
                            str(output_id): {"netGrams": "112", "quantity": 5, "classification": "CORRECTO"},
                        },
                        "commonChecks": {
                            "ciclo": {"seconds": 12, "classification": "CORRECTO"},
                            "color": {"status": "OK", "scopeOutputIds": [output_id]},
                            "inyeccion": {"status": "OK", "scopeOutputIds": [output_id]},
                            "funcional": {"status": "NA", "scopeOutputIds": [output_id]},
                        },
                    },
                },
            },
            headers=_headers(actor.id),
        )
        assert response.status_code == 200, response.get_json()
        payload = response.get_json()["visit"]["payload"]
        assert "outputChecks" in payload["checks"]
        assert "pieceKeys" not in payload
        confirmed = client.put(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas/{response.get_json()['visit']['id']}/confirmar",
            json={"version": response.get_json()["visit"]["version"]},
            headers=_headers(actor.id),
        )
        assert confirmed.status_code == 200


def test_quality_validation_edges_are_four_xx_and_next_periodic_is_two(app, client, scm_config):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = _register_actor()
        db.session.commit()
        ot_id = seeded["header"]["public_id"]
        work_id = seeded["first"]["trabajo_color"]["id"]
        listing = client.get(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            headers={"X-Actor-Id": str(actor.id)},
        )
        assert listing.status_code == 200
        assert listing.get_json()["next_periodic_number"] == 2
        malformed = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            data="{",
            content_type="application/json",
            headers=_headers(actor.id),
        )
        assert malformed.status_code == 400
        bad_number = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={"trabajo_id": work_id, "tipo": "PERIODICA", "numero": "2", "payload": {}},
            headers=_headers(actor.id),
        )
        assert bad_number.status_code == 400
        projection = client.get(
            f"/api/scm/v1/ots/{ot_id}?vista=calidad",
            headers={"X-Actor-Id": str(actor.id)},
        ).get_json()["ot"]
        output_id = projection["trabajos_color"][0]["articulos_salida"][0]["id"]
        checks = {
            "outputChecks": {str(output_id): {"netGrams": "NaN", "quantity": 5, "classification": "CORRECTO"}},
            "commonChecks": {},
        }
        nonfinite = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={"trabajo_id": work_id, "tipo": "PERIODICA", "numero": 2, "payload": {"outputIds": [output_id], "checks": checks}},
            headers=_headers(actor.id),
        )
        assert nonfinite.status_code == 422
        legacy = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={
                "trabajo_id": work_id, "tipo": "PERIODICA", "numero": 2,
                "observed_at": "2026-11-14T14:00:00-05:00",
                "payload": {"pieceKeys": [f"piece:{work_id}:output"], "checks": {f"piece:{work_id}:output": {"peso": {"status": "MEDIDO"}}}},
            },
            headers=_headers(actor.id),
        )
        assert legacy.status_code == 200
        legacy_confirm = client.put(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas/{legacy.get_json()['visit']['id']}/confirmar",
            json={"version": legacy.get_json()["visit"]["version"]},
            headers=_headers(actor.id),
        )
        assert legacy_confirm.status_code == 422


def test_quality_manga_wire_accepts_own_uuid_and_rejects_foreign_or_invalid(app, client, scm_config):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = _register_actor()
        db.session.commit()
        ot_id = seeded["header"]["public_id"]
        first_work = seeded["first"]["trabajo_color"]["id"]
        second_manga = seeded["second"]["mangas"][0]["public_id"]
        own_manga = seeded["first"]["mangas"][0]["public_id"]
        _output_id, payload = _wire_payload(client, ot_id, first_work, actor.id)
        invalid = {**payload, "mangas": ["not-a-uuid"]}
        invalid_response = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={"trabajo_id": first_work, "tipo": "PERIODICA", "numero": 2, "payload": invalid},
            headers=_headers(actor.id),
        )
        assert invalid_response.status_code == 422
        foreign = {**payload, "mangas": [second_manga]}
        foreign_response = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={"trabajo_id": first_work, "tipo": "PERIODICA", "numero": 2, "payload": foreign},
            headers=_headers(actor.id),
        )
        assert foreign_response.status_code == 422
        valid = {**payload, "mangas": [own_manga]}
        valid_response = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={"trabajo_id": first_work, "tipo": "PERIODICA", "numero": 2, "payload": valid},
            headers=_headers(actor.id),
        )
        assert valid_response.status_code == 200


def test_quality_initial_slot_is_reserved_once_per_ot(app, client, scm_config):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = _register_actor()
        db.session.commit()
        ot_id = seeded["header"]["public_id"]
        first_work = seeded["first"]["trabajo_color"]["id"]
        second_work = seeded["second"]["trabajo_color"]["id"]
        _output_id, first_payload = _wire_payload(client, ot_id, first_work, actor.id)
        first = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={"trabajo_id": first_work, "tipo": "INICIAL", "numero": 1, "observed_at": "2026-11-14T06:00:00-05:00", "payload": first_payload},
            headers=_headers(actor.id),
        )
        assert first.status_code == 200
        _output_id, second_payload = _wire_payload(client, ot_id, second_work, actor.id)
        second = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={"trabajo_id": second_work, "tipo": "INICIAL", "numero": 1, "observed_at": "2026-11-14T06:00:00-05:00", "payload": second_payload},
            headers=_headers(actor.id),
        )
        assert second.status_code == 409
        assert second.get_json()["error"]["code"] == "QUALITY_INITIAL_SLOT_RESERVED"
        assert second.get_json()["error"]["details"]["trabajo_id"] == str(first_work)


def test_quality_draft_replay_rechecks_revoked_read_permissions(app, client, scm_config):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        actor = _register_actor()
        db.session.commit()
        ot_id = seeded["header"]["public_id"]
        work_id = seeded["first"]["trabajo_color"]["id"]
        _output_id, payload = _wire_payload(client, ot_id, work_id, actor.id)
        operation_id = uuid4()
        body = {"trabajo_id": work_id, "tipo": "PERIODICA", "numero": 2, "payload": payload}
        first = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json=body,
            headers=_headers(actor.id, operation_id),
        )
        assert first.status_code == 200
        actor.roles[0].capacidades = []
        db.session.commit()
        replay = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json=body,
            headers=_headers(actor.id, operation_id),
        )
        assert replay.status_code == 403
        assert replay.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"


def test_quality_register_capability_is_assigned_only_to_confirmed_roles(app, scm_config):
    with app.app_context():
        rows = db.session.scalars(db.select(RolOperativo)).all()
        granted = {
            row.codigo for row in rows
            if any(cap.codigo == "CALIDAD_CONTROL_REGISTRAR" for cap in row.capacidades)
        }
        read_granted = {
            row.codigo for row in rows
            if {cap.codigo for cap in row.capacidades} >= {"OT_VER", "OF_VER"}
        }
    assert granted == {"CALIDAD", "SUPERVISOR", "GERENCIA", "GERENTE_GENERAL"}
    assert {"SUPERVISOR", "GERENCIA", "GERENTE_GENERAL"} <= read_granted
    assert "CALIDAD" not in read_granted


def test_quality_projection_reader_pair_can_read_but_cannot_register(
    app, client, scm_config,
):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        reader = _actor("CAL-READ-ONLY", {"OT_VER", "OF_VER"})
        ot_id = seeded["header"]["public_id"]
        work_id = seeded["first"]["trabajo_color"]["id"]
        assert client.get(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            headers={"X-Actor-Id": str(reader.id)},
        ).status_code == 200
        response = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={"trabajo_id": work_id, "tipo": "PERIODICA", "payload": {}},
            headers=_headers(reader.id),
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"


def test_quality_inspector_can_write_without_general_ot_of_reads(
    app, client, scm_config,
):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        inspector = _actor("CAL-INSPECTOR", {"CALIDAD_CONTROL_REGISTRAR"})
        ot_id = seeded["header"]["public_id"]
        work_id = seeded["first"]["trabajo_color"]["id"]
        _output_id, payload = _wire_payload(client, ot_id, work_id, inspector.id)
        response = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={
                "trabajo_id": work_id,
                "tipo": "PERIODICA",
                "numero": 2,
                "observed_at": "2026-11-14T14:00:00-05:00",
                "payload": payload,
            },
            headers=_headers(inspector.id),
        )
        assert response.status_code == 200, response.get_json()


def test_quality_quota_is_persisted_on_ot_and_edited_with_planning_version(
    app, client, scm_config,
):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        planner = _actor("CAL-PLANNER", {"OT_CREAR"})
        ot_id = seeded["header"]["public_id"]
        response = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-configuracion",
            json={
                "version": seeded["header"]["version"],
                "calidad_visitas_periodicas_previstas": 3,
            },
            headers=_headers(planner.id),
        )
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["periodic_quota"] == 3
        quality_reader = _register_actor()
        visits = client.get(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            headers={"X-Actor-Id": str(quality_reader.id)},
        )
        assert visits.status_code == 200
        assert visits.get_json()["periodic_quota"] == 3
        ot = db.session.scalar(
            db.select(RegistroDiarioProduccion).where(
                RegistroDiarioProduccion.public_id == UUID(ot_id)
            )
        )
        assert ot.calidad_visitas_periodicas_previstas == 3
        assert ot.cantidad_objetivo is None


def test_quality_quota_is_accepted_when_creating_the_ot_header(
    app, scm_config,
):
    with app.app_context():
        creator, _approver, order, _run, _output = _seed_fabrication_order()
        header = create_fabrication_ot_header(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            data={
                "maquina_id": order.fabricacion.maquina_prevista_id,
                "fecha_operativa": "2026-11-16",
                "turno": "DIA",
                "calidad_visitas_periodicas_previstas": 3,
            },
        )["ot"]
        assert header["calidad_visitas_periodicas_previstas"] == 3
        ot = db.session.scalar(
            db.select(RegistroDiarioProduccion).where(
                RegistroDiarioProduccion.public_id == UUID(header["public_id"])
            )
        )
        assert ot.calidad_visitas_periodicas_previstas == 3


def test_quality_quota_blocks_confirmation_after_confirmed_count(
    app, client, scm_config,
):
    with app.app_context():
        seeded = _seed_two_work_aggregate(first_quantity=20)
        planner = _actor("CAL-PLANNER-LIMIT", {"OT_CREAR"})
        actor = _register_actor()
        ot_id = seeded["header"]["public_id"]
        configured = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-configuracion",
            json={
                "version": seeded["header"]["version"],
                "calidad_visitas_periodicas_previstas": 1,
            },
            headers=_headers(planner.id),
        )
        assert configured.status_code == 200, configured.get_json()
        work_id = seeded["first"]["trabajo_color"]["id"]
        _output_id, payload = _wire_payload(client, ot_id, work_id, actor.id)
        saved = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={
                "trabajo_id": work_id,
                "tipo": "PERIODICA",
                "numero": 2,
                "observed_at": "2026-11-14T14:00:00-05:00",
                "payload": payload,
            },
            headers=_headers(actor.id),
        )
        assert saved.status_code == 200, saved.get_json()
        first_confirmed = client.put(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas/{saved.get_json()['visit']['id']}/confirmar",
            json={"version": saved.get_json()["visit"]["version"]},
            headers=_headers(actor.id),
        )
        assert first_confirmed.status_code == 200, first_confirmed.get_json()
        second_work_id = seeded["second"]["trabajo_color"]["id"]
        _output_id, second_payload = _wire_payload(client, ot_id, second_work_id, actor.id)
        second_saved = client.patch(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas",
            json={
                "trabajo_id": second_work_id,
                "tipo": "PERIODICA",
                "numero": 3,
                "observed_at": "2026-11-14T15:00:00-05:00",
                "payload": second_payload,
            },
            headers=_headers(actor.id),
        )
        assert second_saved.status_code == 200, second_saved.get_json()
        confirmed = client.put(
            f"/api/scm/v1/ots/{ot_id}/calidad-visitas/{second_saved.get_json()['visit']['id']}/confirmar",
            json={"version": second_saved.get_json()["visit"]["version"]},
            headers=_headers(actor.id),
        )
        assert confirmed.status_code == 409
        assert confirmed.get_json()["error"]["code"] == "QUALITY_PERIODIC_QUOTA_EXCEEDED"


def test_quality_initial_cutoff_uses_lima_date_and_shift():
    day = SimpleNamespace(fecha=datetime(2026, 11, 16).date(), turno="DIA")
    night = SimpleNamespace(fecha=datetime(2026, 11, 16).date(), turno="NOCHE")
    assert _initial_cutoff(day).hour == 8
    assert _initial_cutoff(night).hour == 20
    assert _initial_window_closed(day, now=datetime(2026, 11, 16, 7, 59)) is False
    assert _initial_window_closed(day, now=datetime(2026, 11, 16, 8, 0)) is True
    assert _initial_window_closed(night, now=datetime(2026, 11, 16, 19, 59)) is False
    assert _initial_window_closed(night, now=datetime(2026, 11, 16, 20, 0)) is True
