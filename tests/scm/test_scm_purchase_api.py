from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.extensions import db
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_material_service import create_materia_prima_with_scm


API_BASE = "/api/scm/v1"


@dataclass(frozen=True)
class ScmPurchaseContext:
    compras_id: int
    gerencia_id: int
    sin_permiso_id: int
    material_id: int


@pytest.fixture
def scm_purchase_context(app, scm_config):
    del scm_config

    with app.app_context():
        roles = {
            rol.codigo: rol
            for rol in db.session.scalars(db.select(RolOperativo))
        }
        compras = Trabajador(
            codigo="TRB-COM-01",
            nombres="Maria",
            apellidos="Compras",
            activo=True,
            # También puede aprobar: REC-40 debe fallar por segregación,
            # no por ausencia de OC_APROBAR.
            roles=[roles["COMPRAS"], roles["GERENCIA"]],
        )
        gerencia = Trabajador(
            codigo="TRB-GER-01",
            nombres="Gerencia",
            apellidos="Planta",
            activo=True,
            roles=[roles["GERENCIA"]],
        )
        sin_permiso = Trabajador(
            codigo="TRB-AUD-01",
            nombres="Ada",
            apellidos="Auditoria",
            activo=True,
            roles=[roles["AUDITORIA_CONSULTA"]],
        )
        db.session.add_all([compras, gerencia, sin_permiso])
        material_legacy = create_materia_prima_with_scm(
            session=db.session,
            nombre="Polipropileno virgen de prueba",
            tipo="VIRGEN",
            codigo_scm="MP-PP-VIRGEN",
        )
        db.session.commit()

        return ScmPurchaseContext(
            compras_id=compras.id,
            gerencia_id=gerencia.id,
            sin_permiso_id=sin_permiso.id,
            material_id=material_legacy.scm_material_id,
        )


def _headers(actor_id, *, idempotency_key=None):
    headers = {"X-Actor-Id": str(actor_id)}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = str(idempotency_key)
    return headers


def _assert_error(response, status, code):
    assert response.status_code == status, response.get_json()
    payload = response.get_json()
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]
    return payload["error"]


def _create_provider(client, actor_id):
    response = client.post(
        f"{API_BASE}/proveedores",
        headers=_headers(actor_id, idempotency_key=uuid4()),
        json={
            "codigo": "PROV-TEST-01",
            "razon_social": "Proveedor canonico de prueba",
            "ruc": "20-524360-366",
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_proveedor_crud_logico_exige_capacidad_y_version_optimista(
    client,
    scm_purchase_context,
):
    context = scm_purchase_context
    payload = {
        "codigo": "PROV-TEST-01",
        "razon_social": "Proveedor canonico de prueba",
        "ruc": "20-524360-366",
        "contacto": "  Piero Compras  ",
        "telefono": "01 708-2613",
        "whatsapp": "+51 998 123 628",
        "correo": "  COMPRAS@PROVEEDOR.PE ",
    }

    denied = client.post(
        f"{API_BASE}/proveedores",
        headers=_headers(context.sin_permiso_id, idempotency_key=uuid4()),
        json=payload,
    )
    _assert_error(denied, 403, "CAPABILITY_REQUIRED")

    created = client.post(
        f"{API_BASE}/proveedores",
        headers=_headers(context.compras_id, idempotency_key=uuid4()),
        json=payload,
    )
    assert created.status_code == 201, created.get_json()
    provider = created.get_json()
    assert provider["codigo"] == "PROV-TEST-01"
    assert provider["razon_social"] == "Proveedor canonico de prueba"
    assert provider["ruc"] == "20524360366"
    assert provider["contacto"] == "Piero Compras"
    assert provider["telefono"] == "01 708-2613"
    assert provider["whatsapp"] == "+51 998 123 628"
    assert provider["correo"] == "compras@proveedor.pe"
    assert provider["activo"] is True
    assert provider["version"] == 1

    listed = client.get(
        f"{API_BASE}/proveedores",
        headers=_headers(context.compras_id),
    )
    assert listed.status_code == 200, listed.get_json()
    assert [item["id"] for item in listed.get_json()["items"]] == [
        provider["id"]
    ]

    detail = client.get(
        f"{API_BASE}/proveedores/{provider['id']}",
        headers=_headers(context.compras_id),
    )
    assert detail.status_code == 200, detail.get_json()
    assert detail.get_json() == provider

    updated = client.patch(
        f"{API_BASE}/proveedores/{provider['id']}",
        headers=_headers(context.compras_id),
        json={
            "version": 1,
            "razon_social": "Proveedor canonico actualizado",
            "contacto": "Maria Abastecimiento",
            "telefono": None,
        },
    )
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["razon_social"] == (
        "Proveedor canonico actualizado"
    )
    assert updated.get_json()["contacto"] == "Maria Abastecimiento"
    assert updated.get_json()["telefono"] is None
    assert updated.get_json()["version"] == 2

    stale = client.patch(
        f"{API_BASE}/proveedores/{provider['id']}",
        headers=_headers(context.compras_id),
        json={
            "version": 1,
            "razon_social": "Este cambio obsoleto no debe aplicarse",
        },
    )
    error = _assert_error(stale, 409, "STALE_VERSION")
    assert error["details"] == {"expected": 2, "received": 1}

    deactivated = client.patch(
        f"{API_BASE}/proveedores/{provider['id']}",
        headers=_headers(context.compras_id),
        json={"version": 2, "activo": False},
    )
    assert deactivated.status_code == 200, deactivated.get_json()
    assert deactivated.get_json()["activo"] is False
    assert deactivated.get_json()["version"] == 3

    historical_detail = client.get(
        f"{API_BASE}/proveedores/{provider['id']}",
        headers=_headers(context.compras_id),
    )
    assert historical_detail.status_code == 200
    assert historical_detail.get_json()["activo"] is False


def test_proveedor_rechaza_correo_invalido(
    client,
    scm_purchase_context,
):
    response = client.post(
        f"{API_BASE}/proveedores",
        headers=_headers(
            scm_purchase_context.compras_id,
            idempotency_key=uuid4(),
        ),
        json={
            "razon_social": "Proveedor con correo invalido",
            "correo": "correo-sin-dominio",
        },
    )

    _assert_error(response, 422, "INVALID_EMAIL")


def test_rec_40_oc_versionada_rechaza_autoaprobacion_y_aprueba_gerencia(
    app,
    client,
    scm_purchase_context,
):
    context = scm_purchase_context
    provider = _create_provider(client, context.compras_id)

    created = client.post(
        f"{API_BASE}/ordenes-compra-material",
        headers=_headers(context.compras_id),
        json={
            "proveedor_id": provider["id"],
            "lineas": [
                {
                    "numero_linea": 1,
                    "material_id": context.material_id,
                    "cantidad_autorizada_kg": "1250.000",
                    "fecha_requerida": "2026-07-31",
                    "observacion": "Fixture canonico REC-40",
                }
            ],
        },
    )
    assert created.status_code == 201, created.get_json()
    order = created.get_json()
    assert order["codigo"].startswith("OCM-")
    assert order["proveedor_id"] == provider["id"]
    assert order["estado"] == "ACTIVA"
    assert order["version"] == 1
    assert order["revision_actual"]["numero"] == 1
    assert order["revision_actual"]["estado"] == "BORRADOR"
    assert order["revision_actual"]["creada_por_id"] == context.compras_id
    assert order["revision_actual"]["lineas"][0][
        "cantidad_autorizada_kg"
    ] == "1250.000"

    sent = client.post(
        (
            f"{API_BASE}/ordenes-compra-material/{order['id']}"
            "/enviar-aprobacion"
        ),
        headers=_headers(context.compras_id, idempotency_key=uuid4()),
        json={"version": 1, "revision_numero": 1},
    )
    assert sent.status_code == 200, sent.get_json()
    pending = sent.get_json()
    assert pending["version"] == 2
    assert pending["revision_actual"]["estado"] == "PENDIENTE_APROBACION"

    stale = client.post(
        (
            f"{API_BASE}/ordenes-compra-material/{order['id']}"
            "/enviar-aprobacion"
        ),
        headers=_headers(context.compras_id, idempotency_key=uuid4()),
        json={"version": 1, "revision_numero": 1},
    )
    _assert_error(stale, 409, "STALE_VERSION")

    self_approval = client.post(
        f"{API_BASE}/ordenes-compra-material/{order['id']}/aprobar",
        headers=_headers(context.compras_id, idempotency_key=uuid4()),
        json={"version": 2, "revision_numero": 1},
    )
    _assert_error(
        self_approval,
        403,
        "PURCHASE_ORDER_SELF_APPROVAL_FORBIDDEN",
    )

    approval_key = uuid4()
    approval_body = {"version": 2, "revision_numero": 1}
    approved = client.post(
        f"{API_BASE}/ordenes-compra-material/{order['id']}/aprobar",
        headers=_headers(
            context.gerencia_id,
            idempotency_key=approval_key,
        ),
        json=approval_body,
    )
    assert approved.status_code == 200, approved.get_json()
    approved_payload = approved.get_json()
    assert approved_payload["version"] == 3
    assert approved_payload["revision_actual"]["estado"] == "APROBADA"
    assert approved_payload["revision_actual"]["aprobada_por_id"] == (
        context.gerencia_id
    )

    detail = client.get(
        f"{API_BASE}/ordenes-compra-material/{order['id']}",
        headers=_headers(context.compras_id),
    )
    assert detail.status_code == 200, detail.get_json()
    current = detail.get_json()
    assert len(current["revisiones"]) == 1
    line = current["revision_actual"]["lineas"][0]
    assert line["cantidad_autorizada_kg"] == "1250.000"
    assert line["cantidad_recibida_kg"] == "0.000"
    assert line["saldo_kg"] == "1250.000"

    replay = client.post(
        f"{API_BASE}/ordenes-compra-material/{order['id']}/aprobar",
        headers=_headers(
            context.gerencia_id,
            idempotency_key=approval_key,
        ),
        json=approval_body,
    )
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json() == approved_payload

    conflicting_replay = client.post(
        f"{API_BASE}/ordenes-compra-material/{order['id']}/aprobar",
        headers=_headers(
            context.gerencia_id,
            idempotency_key=approval_key,
        ),
        json={"version": 3, "revision_numero": 1},
    )
    _assert_error(conflicting_replay, 409, "IDEMPOTENCY_CONFLICT")

    with app.app_context():
        operations = ScmOperacion.query.order_by(ScmOperacion.created_at).all()
        assert len(operations) == 2
        approval_operation = db.session.get(ScmOperacion, approval_key)
        assert approval_operation.estado_http == 200
        assert approval_operation.response_json == approved_payload

        events = ScmEvento.query.filter_by(
            aggregate_type="SCM_ORDEN_COMPRA",
            aggregate_id=order["id"],
        ).order_by(ScmEvento.id).all()
        assert [event.tipo for event in events] == [
            "ORDEN_COMPRA_CREADA",
            "ORDEN_COMPRA_ENVIADA_APROBACION",
            "ORDEN_COMPRA_APROBADA",
        ]
        assert events[-1].actor_snapshot == {
            "id": context.gerencia_id,
            "codigo": "TRB-GER-01",
            "nombre": "Gerencia Planta",
        }


def test_nueva_revision_clona_lineas_y_supera_la_aprobada_anterior(
    client,
    scm_purchase_context,
):
    context = scm_purchase_context
    provider = _create_provider(client, context.compras_id)
    created = client.post(
        f"{API_BASE}/ordenes-compra-material",
        headers=_headers(context.compras_id),
        json={
            "proveedor_id": provider["id"],
            "lineas": [{
                "numero_linea": 1,
                "material_id": context.material_id,
                "cantidad_autorizada_kg": "1250.000",
            }],
        },
    ).get_json()
    order_id = created["id"]

    sent = client.post(
        f"{API_BASE}/ordenes-compra-material/{order_id}/enviar-aprobacion",
        headers=_headers(context.compras_id, idempotency_key=uuid4()),
        json={"version": 1, "revision_numero": 1},
    )
    assert sent.status_code == 200, sent.get_json()
    first_approval = client.post(
        f"{API_BASE}/ordenes-compra-material/{order_id}/aprobar",
        headers=_headers(context.gerencia_id, idempotency_key=uuid4()),
        json={"version": 2, "revision_numero": 1},
    )
    assert first_approval.status_code == 200, first_approval.get_json()

    new_revision = client.post(
        f"{API_BASE}/ordenes-compra-material/{order_id}/revisiones",
        headers=_headers(context.compras_id),
        json={"version": 3},
    )
    assert new_revision.status_code == 201, new_revision.get_json()
    revised = new_revision.get_json()
    assert revised["version"] == 4
    assert revised["revision_actual"]["numero"] == 2
    assert revised["revision_actual"]["estado"] == "BORRADOR"
    assert revised["revision_actual"]["lineas"][0][
        "cantidad_autorizada_kg"
    ] == "1250.000"
    assert revised["revisiones"][0]["estado"] == "APROBADA"

    second_sent = client.post(
        f"{API_BASE}/ordenes-compra-material/{order_id}/enviar-aprobacion",
        headers=_headers(context.compras_id, idempotency_key=uuid4()),
        json={"version": 4, "revision_numero": 2},
    )
    assert second_sent.status_code == 200, second_sent.get_json()
    second_approval = client.post(
        f"{API_BASE}/ordenes-compra-material/{order_id}/aprobar",
        headers=_headers(context.gerencia_id, idempotency_key=uuid4()),
        json={"version": 5, "revision_numero": 2},
    )
    assert second_approval.status_code == 200, second_approval.get_json()
    final = second_approval.get_json()
    assert final["version"] == 6
    assert [item["estado"] for item in final["revisiones"]] == [
        "SUPERADA",
        "APROBADA",
    ]


def test_editar_revision_borrador_reemplaza_lineas_con_doble_version(
    app,
    client,
    scm_purchase_context,
):
    context = scm_purchase_context
    provider = _create_provider(client, context.compras_id)
    created_response = client.post(
        f"{API_BASE}/ordenes-compra-material",
        headers=_headers(context.compras_id),
        json={"proveedor_id": provider["id"], "lineas": []},
    )
    assert created_response.status_code == 201, created_response.get_json()
    created = created_response.get_json()
    order_id = created["id"]
    revision_url = (
        f"{API_BASE}/ordenes-compra-material/{order_id}/revisiones/1"
    )
    detail_url = f"{API_BASE}/ordenes-compra-material/{order_id}"

    assert created["version"] == 1
    assert created["revision_actual"]["version"] == 1
    assert created["revision_actual"]["estado"] == "BORRADOR"
    assert created["revision_actual"]["lineas"] == []

    def current_order():
        response = client.get(
            detail_url,
            headers=_headers(context.compras_id),
        )
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    initial_lines = [
        {
            "numero_linea": 1,
            "material_id": context.material_id,
            "cantidad_autorizada_kg": "1250.000",
            "fecha_requerida": "2026-08-01",
            "observacion": "Primera linea del borrador",
        },
        {
            "numero_linea": 2,
            "material_id": context.material_id,
            "cantidad_autorizada_kg": "625.500",
        },
    ]
    denied = client.patch(
        revision_url,
        headers=_headers(context.sin_permiso_id),
        json={
            "version": 1,
            "revision_version": 1,
            "lineas": initial_lines,
        },
    )
    error = _assert_error(denied, 403, "CAPABILITY_REQUIRED")
    assert error["details"] == {"capability": "OC_CREAR"}
    assert current_order() == created

    first_update_response = client.patch(
        revision_url,
        headers=_headers(context.compras_id),
        json={
            "version": 1,
            "revision_version": 1,
            "lineas": initial_lines,
        },
    )
    assert first_update_response.status_code == 200, (
        first_update_response.get_json()
    )
    first_update = first_update_response.get_json()
    assert first_update["id"] == order_id
    assert first_update["version"] == 2
    assert len(first_update["revisiones"]) == 1
    revision = first_update["revision_actual"]
    assert revision["numero"] == 1
    assert revision["version"] == 2
    assert revision["estado"] == "BORRADOR"
    assert [line["numero_linea"] for line in revision["lineas"]] == [1, 2]
    assert [
        line["cantidad_autorizada_kg"] for line in revision["lineas"]
    ] == ["1250.000", "625.500"]

    stale_order = client.patch(
        revision_url,
        headers=_headers(context.compras_id),
        json={
            "version": 1,
            "revision_version": 2,
            "lineas": [{
                "numero_linea": 3,
                "material_id": context.material_id,
                "cantidad_autorizada_kg": "300.000",
            }],
        },
    )
    _assert_error(stale_order, 409, "STALE_VERSION")
    assert current_order() == first_update

    stale_revision = client.patch(
        revision_url,
        headers=_headers(context.compras_id),
        json={
            "version": 2,
            "revision_version": 1,
            "lineas": [{
                "numero_linea": 3,
                "material_id": context.material_id,
                "cantidad_autorizada_kg": "300.000",
            }],
        },
    )
    _assert_error(stale_revision, 409, "STALE_VERSION")
    assert current_order() == first_update

    invalid_line_cases = [
        (
            [{
                "numero_linea": 0,
                "material_id": context.material_id,
                "cantidad_autorizada_kg": "1.000",
            }],
            400,
            "POSITIVE_INTEGER_REQUIRED",
        ),
        (
            [{
                "numero_linea": 1,
                "material_id": 999999,
                "cantidad_autorizada_kg": "1.000",
            }],
            404,
            "MATERIAL_NOT_FOUND",
        ),
        (
            [{
                "numero_linea": 1,
                "material_id": context.material_id,
                "cantidad_autorizada_kg": "0.000",
            }],
            422,
            "INVALID_QUANTITY",
        ),
        (
            [
                {
                    "numero_linea": 1,
                    "material_id": context.material_id,
                    "cantidad_autorizada_kg": "1.000",
                },
                {
                    "numero_linea": 1,
                    "material_id": context.material_id,
                    "cantidad_autorizada_kg": "2.000",
                },
            ],
            422,
            "DUPLICATE_LINE_NUMBER",
        ),
    ]
    for lines, status, code in invalid_line_cases:
        invalid = client.patch(
            revision_url,
            headers=_headers(context.compras_id),
            json={
                "version": 2,
                "revision_version": 2,
                "lineas": lines,
            },
        )
        _assert_error(invalid, status, code)
        assert current_order() == first_update

    replacement_lines = [{
        "numero_linea": 3,
        "material_id": context.material_id,
        "cantidad_autorizada_kg": "300.000",
        "observacion": "Reemplazo completo",
    }]
    replacement_response = client.patch(
        revision_url,
        headers=_headers(context.compras_id),
        json={
            "version": 2,
            "revision_version": 2,
            "lineas": replacement_lines,
        },
    )
    assert replacement_response.status_code == 200, (
        replacement_response.get_json()
    )
    replacement = replacement_response.get_json()
    assert replacement["version"] == 3
    assert replacement["revision_actual"]["version"] == 3
    assert len(replacement["revision_actual"]["lineas"]) == 1
    replacement_line = replacement["revision_actual"]["lineas"][0]
    assert replacement_line["numero_linea"] == 3
    assert replacement_line["material_id"] == context.material_id
    assert replacement_line["cantidad_autorizada_kg"] == "300.000"
    assert replacement_line["observacion"] == "Reemplazo completo"

    sent_response = client.post(
        f"{detail_url}/enviar-aprobacion",
        headers=_headers(context.compras_id, idempotency_key=uuid4()),
        json={"version": 3, "revision_numero": 1},
    )
    assert sent_response.status_code == 200, sent_response.get_json()
    pending = sent_response.get_json()
    assert pending["version"] == 4
    assert pending["revision_actual"]["version"] == 4
    assert pending["revision_actual"]["estado"] == (
        "PENDIENTE_APROBACION"
    )

    closed_edit = client.patch(
        revision_url,
        headers=_headers(context.compras_id),
        json={
            "version": 4,
            "revision_version": 4,
            "lineas": initial_lines,
        },
    )
    _assert_error(
        closed_edit,
        409,
        "INVALID_PURCHASE_ORDER_TRANSITION",
    )
    assert current_order() == pending

    with app.app_context():
        events = ScmEvento.query.filter_by(
            aggregate_type="SCM_ORDEN_COMPRA",
            aggregate_id=order_id,
        ).order_by(ScmEvento.id).all()
        assert [event.tipo for event in events] == [
            "ORDEN_COMPRA_CREADA",
            "ORDEN_COMPRA_REVISION_ACTUALIZADA",
            "ORDEN_COMPRA_REVISION_ACTUALIZADA",
            "ORDEN_COMPRA_ENVIADA_APROBACION",
        ]
        update_events = [
            event
            for event in events
            if event.tipo == "ORDEN_COMPRA_REVISION_ACTUALIZADA"
        ]
        assert all(
            event.actor_snapshot["id"] == context.compras_id
            for event in update_events
        )
