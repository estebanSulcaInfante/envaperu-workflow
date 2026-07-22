from dataclasses import dataclass

import pytest

from app.extensions import db
from app.models.scm_auditoria import ScmEvento
from app.models.scm_recepcion import (
    ScmDocumentoProveedor,
    ScmPesajeBolsa,
    ScmRecepcion,
    ScmRecepcionDocumento,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_material_service import create_materia_prima_with_scm


API_BASE = "/api/scm/v1"
DOCUMENTS_URL = f"{API_BASE}/documentos-proveedor"
RECEPTIONS_URL = f"{API_BASE}/recepciones/materiales"


@dataclass(frozen=True)
class ReceptionContext:
    compras_id: int
    almacen_id: int
    sin_permiso_id: int
    material_segunda_id: int
    material_virgen_id: int


@pytest.fixture
def reception_context(app, scm_config):
    del scm_config
    with app.app_context():
        roles = {
            item.codigo: item
            for item in db.session.scalars(db.select(RolOperativo))
        }
        compras = Trabajador(
            codigo="TRB-COM-REC",
            nombres="Maria",
            apellidos="Compras",
            activo=True,
            roles=[roles["COMPRAS"]],
        )
        almacen = Trabajador(
            codigo="TRB-ALM-REC",
            nombres="Ana",
            apellidos="Almacen",
            activo=True,
            roles=[roles["ALMACEN_RECEPCION"]],
        )
        sin_permiso = Trabajador(
            codigo="TRB-AUD-REC",
            nombres="Ada",
            apellidos="Auditoria",
            activo=True,
            roles=[roles["AUDITORIA_CONSULTA"]],
        )
        db.session.add_all([compras, almacen, sin_permiso])
        segunda = create_materia_prima_with_scm(
            session=db.session,
            nombre="PP segunda para bolsas",
            tipo="SEGUNDA",
            codigo_scm="MP-SEG-BOLSA",
        )
        virgen = create_materia_prima_with_scm(
            session=db.session,
            nombre="PP virgen sin repesaje",
            tipo="VIRGEN",
            codigo_scm="MP-VIR-REC",
        )
        db.session.commit()
        return ReceptionContext(
            compras_id=compras.id,
            almacen_id=almacen.id,
            sin_permiso_id=sin_permiso.id,
            material_segunda_id=segunda.scm_material_id,
            material_virgen_id=virgen.scm_material_id,
        )


def _headers(actor_id):
    return {"X-Actor-Id": str(actor_id)}


def _assert_error(response, status, code):
    assert response.status_code == status, response.get_json()
    payload = response.get_json()
    assert payload["error"]["code"] == code
    return payload["error"]


def _create_provider(client, actor_id):
    response = client.post(
        f"{API_BASE}/proveedores",
        headers=_headers(actor_id),
        json={
            "codigo": "PROV-REC-BOLSA",
            "razon_social": "Proveedor de recepciones parciales",
            "ruc": "20524360366",
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _create_document(client, actor_id, provider_id):
    response = client.post(
        DOCUMENTS_URL,
        headers=_headers(actor_id),
        json={
            "proveedor_id": provider_id,
            "tipo": "GUIA_REMISION",
            "serie": "t002",
            "numero": "00001833",
            "fecha_emision": "2026-07-01",
            "cantidad_total_documental_kg": "5000.000",
            "referencia": "F502-00004747",
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _second_line(material_id, weights):
    return {
        "numero_linea": 1,
        "material_id": material_id,
        "bultos_recibidos": len(weights),
        "cantidad_documental_kg": "50.000",
        "pesajes_bolsa": [
            {
                "secuencia": index,
                "peso_kg": weight,
                "balanza_codigo_snapshot": "BAL-RECEPCION-01",
            }
            for index, weight in enumerate(weights, start=1)
        ],
    }


def test_documento_puede_cubrir_varias_recepciones_y_segunda_conserva_cada_pesaje(
    app,
    client,
    reception_context,
):
    context = reception_context
    provider = _create_provider(client, context.compras_id)
    document = _create_document(client, context.almacen_id, provider["id"])

    first = client.post(
        RECEPTIONS_URL,
        headers=_headers(context.almacen_id),
        json={
            "codigo": "REC-PARCIAL-001",
            "proveedor_id": provider["id"],
            "documentos_ids": [document["id"]],
            "lineas": [_second_line(context.material_segunda_id, ["24.900", "24.950"])],
        },
    )
    assert first.status_code == 201, first.get_json()
    first_payload = first.get_json()
    assert first_payload["estado"] == "BORRADOR"
    assert first_payload["lineas"][0]["modalidad"] == "SEGUNDA_PESAJE_BOLSA"
    assert first_payload["lineas"][0]["cantidad_medida_kg"] == "49.850"
    assert [item["peso_kg"] for item in first_payload["lineas"][0]["pesajes_bolsa"]] == [
        "24.900",
        "24.950",
    ]

    second = client.post(
        RECEPTIONS_URL,
        headers=_headers(context.almacen_id),
        json={
            "codigo": "REC-PARCIAL-002",
            "proveedor_id": provider["id"],
            "documentos_ids": [document["id"]],
            "lineas": [_second_line(context.material_segunda_id, ["25.000"])],
        },
    )
    assert second.status_code == 201, second.get_json()

    detail = client.get(
        f"{DOCUMENTS_URL}/{document['id']}",
        headers=_headers(context.sin_permiso_id),
    )
    assert detail.status_code == 200
    assert detail.get_json()["recepciones_count"] == 2

    with app.app_context():
        assert ScmDocumentoProveedor.query.count() == 1
        assert ScmRecepcion.query.count() == 2
        assert ScmRecepcionDocumento.query.count() == 2
        assert ScmPesajeBolsa.query.count() == 3
        assert ScmEvento.query.filter_by(tipo="RECEPCION_BORRADOR_CREADA").count() == 2


def test_recepcion_valida_modalidad_permisos_y_rollback(
    app,
    client,
    reception_context,
):
    context = reception_context
    provider = _create_provider(client, context.compras_id)

    denied_document = client.post(
        DOCUMENTS_URL,
        headers=_headers(context.sin_permiso_id),
        json={
            "proveedor_id": provider["id"],
            "tipo": "FACTURA",
            "serie": "F001",
            "numero": "1",
            "fecha_emision": "2026-07-01",
        },
    )
    _assert_error(denied_document, 403, "CAPABILITY_REQUIRED")
    document = _create_document(client, context.compras_id, provider["id"])

    denied_reception = client.post(
        RECEPTIONS_URL,
        headers=_headers(context.sin_permiso_id),
        json={
            "codigo": "REC-DENIED",
            "proveedor_id": provider["id"],
            "documentos_ids": [document["id"]],
            "lineas": [_second_line(context.material_segunda_id, ["25.000"])],
        },
    )
    _assert_error(denied_reception, 403, "CAPABILITY_REQUIRED")

    mismatched = client.post(
        RECEPTIONS_URL,
        headers=_headers(context.almacen_id),
        json={
            "codigo": "REC-MISMATCH",
            "proveedor_id": provider["id"],
            "documentos_ids": [document["id"]],
            "lineas": [{
                "numero_linea": 1,
                "material_id": context.material_segunda_id,
                "bultos_recibidos": 2,
                "cantidad_documental_kg": "50.000",
                "pesajes_bolsa": [{"secuencia": 1, "peso_kg": "25.000"}],
            }],
        },
    )
    _assert_error(mismatched, 422, "BAG_WEIGHT_COUNT_MISMATCH")

    virgin_with_weights = client.post(
        RECEPTIONS_URL,
        headers=_headers(context.almacen_id),
        json={
            "codigo": "REC-VIRGEN-PESADA",
            "proveedor_id": provider["id"],
            "documentos_ids": [document["id"]],
            "lineas": [{
                "numero_linea": 1,
                "material_id": context.material_virgen_id,
                "bultos_recibidos": 1,
                "cantidad_documental_kg": "25.000",
                "pesajes_bolsa": [{"secuencia": 1, "peso_kg": "25.000"}],
            }],
        },
    )
    _assert_error(virgin_with_weights, 422, "VIRGIN_BAG_WEIGHTS_FORBIDDEN")

    with app.app_context():
        assert ScmRecepcion.query.count() == 0
        assert ScmPesajeBolsa.query.count() == 0


def test_documento_identidad_unica_y_version_optimista(
    client,
    reception_context,
):
    context = reception_context
    provider = _create_provider(client, context.compras_id)
    document = _create_document(client, context.compras_id, provider["id"])

    duplicate = client.post(
        DOCUMENTS_URL,
        headers=_headers(context.compras_id),
        json={
            "proveedor_id": provider["id"],
            "tipo": "GUIA_REMISION",
            "serie": " T002 ",
            "numero": "00001833",
            "fecha_emision": "2026-07-02",
        },
    )
    _assert_error(duplicate, 409, "SUPPLIER_DOCUMENT_CONFLICT")

    updated = client.patch(
        f"{DOCUMENTS_URL}/{document['id']}",
        headers=_headers(context.compras_id),
        json={"version": 1, "observacion": "Entrega fraccionada"},
    )
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["version"] == 2

    stale = client.patch(
        f"{DOCUMENTS_URL}/{document['id']}",
        headers=_headers(context.compras_id),
        json={"version": 1, "referencia": "OTRA"},
    )
    _assert_error(stale, 409, "STALE_VERSION")

    filtered = client.get(
        f"{DOCUMENTS_URL}?proveedor_id={provider['id']}",
        headers=_headers(context.sin_permiso_id),
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.get_json()["items"]] == [document["id"]]


def test_borrador_reemplaza_pesajes_con_version_y_rechaza_noop(
    app,
    client,
    reception_context,
):
    context = reception_context
    provider = _create_provider(client, context.compras_id)
    document = _create_document(client, context.almacen_id, provider["id"])
    original_line = _second_line(
        context.material_segunda_id,
        ["24.900", "24.950"],
    )
    created = client.post(
        RECEPTIONS_URL,
        headers=_headers(context.almacen_id),
        json={
            "codigo": "REC-EDITABLE-001",
            "proveedor_id": provider["id"],
            "documentos_ids": [document["id"]],
            "lineas": [original_line],
        },
    )
    assert created.status_code == 201, created.get_json()
    reception = created.get_json()
    replacement_line = _second_line(
        context.material_segunda_id,
        ["25.000", "25.100"],
    )

    updated = client.patch(
        f"{RECEPTIONS_URL}/{reception['id']}",
        headers=_headers(context.almacen_id),
        json={"version": 1, "lineas": [replacement_line]},
    )
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["version"] == 2
    assert updated.get_json()["lineas"][0]["cantidad_medida_kg"] == "50.100"

    stale = client.patch(
        f"{RECEPTIONS_URL}/{reception['id']}",
        headers=_headers(context.almacen_id),
        json={"version": 1, "observacion": "fuera de version"},
    )
    _assert_error(stale, 409, "STALE_VERSION")

    no_op = client.patch(
        f"{RECEPTIONS_URL}/{reception['id']}",
        headers=_headers(context.almacen_id),
        json={"version": 2, "lineas": [replacement_line]},
    )
    _assert_error(no_op, 400, "NO_CHANGES")

    with app.app_context():
        weights = ScmPesajeBolsa.query.order_by(ScmPesajeBolsa.secuencia).all()
        assert [str(item.peso_kg) for item in weights] == ["25.000", "25.100"]
