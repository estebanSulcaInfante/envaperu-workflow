"""Contrato HTTP de los códigos automáticos e inmutables del catálogo."""

import io

import pytest

from app.extensions import db
from app.models.molde import Molde, Pieza
from app.models.producto import PiezaColor, ProductoTerminado


@pytest.mark.parametrize("entity", ["pieza_color", "producto"])
def test_imagen_opcional_catalogo_se_guarda_consulta_y_elimina(client, entity):
    if entity == "pieza_color":
        created = client.post("/api/piezas", json={
            "nombre": "Tapa foto",
            "peso_nominal_gr": 8,
            "linea_id": 1,
            "familia_id": 1,
        }).get_json()
        variant = client.post("/api/piezas-color", json={
            "nombre": "Tapa foto azul",
            "pieza_id": created["id"],
        }).get_json()
        path = f"/api/piezas-color/{variant['sku']}/imagen"
    else:
        created = client.post("/api/productos", json={"producto": "Balde foto", "linea_id": 1, "familia_id": 1}).get_json()
        path = f"/api/productos/{created['cod_sku_pt']}/imagen"

    content = b"\x89PNG\r\n\x1a\nsmall-test-image"
    uploaded = client.put(path, data={"imagen": (io.BytesIO(content), "foto.png")}, content_type="multipart/form-data")
    assert uploaded.status_code == 200, uploaded.get_json()
    assert uploaded.get_json()["imagen_url"] == path
    fetched = client.get(path)
    assert fetched.status_code == 200
    assert fetched.mimetype == "image/png"
    assert fetched.data == content
    assert client.delete(path).status_code == 200
    assert client.get(path).status_code == 404


def test_imagen_catalogo_rechaza_formato_no_admitido(client):
    piece = client.post("/api/piezas", json={
        "nombre": "Tapa",
        "peso_nominal_gr": 8,
        "linea_id": 1,
        "familia_id": 1,
    }).get_json()
    variant = client.post("/api/piezas-color", json={
        "nombre": "Tapa azul",
        "pieza_id": piece["id"],
    }).get_json()
    response = client.put(
        f"/api/piezas-color/{variant['sku']}/imagen",
        data={"imagen": (io.BytesIO(b"not-an-image"), "foto.gif")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 415
    assert response.get_json()["codigo"] == "IMAGEN_FORMATO_INVALIDO"


def test_producto_bloquea_eliminacion_directa_y_conserva_el_maestro(client, app):
    created = client.post("/api/productos", json={
        "producto": "Producto conservado",
        "linea_id": 1,
        "familia_id": 1,
    }).get_json()

    response = client.delete(f"/api/productos/{created['cod_sku_pt']}")

    assert response.status_code == 409
    assert response.get_json()["codigo"] == "ELIMINACION_DIRECTA_BLOQUEADA"
    with app.app_context():
        assert db.session.get(ProductoTerminado, created["cod_sku_pt"]) is not None


@pytest.mark.parametrize(
    ("path", "field", "payload"),
    [
        (
            "/api/piezas",
            "codigo",
            {"codigo": "PZ-MANUAL", "nombre": "Tapa", "peso_nominal_gr": 8},
        ),
        (
            "/api/moldes",
            "codigo",
            {"codigo": "ML-MANUAL", "nombre": "Molde", "peso_tiro_gr": 50},
        ),
        (
            "/api/moldes",
            "piezas.codigo",
            {
                "nombre": "Molde",
                "peso_tiro_gr": 50,
                "piezas": [
                    {
                        "codigo": "PZ-MANUAL",
                        "nombre": "Tapa",
                        "cavidades": 2,
                        "peso_unitario_gr": 8,
                    }
                ],
            },
        ),
        (
            "/api/piezas-color",
            "sku",
            {
                "sku": "PC-MANUAL",
                "nombre": "Tapa azul",
                "linea_id": 1,
                "familia_id": 1,
            },
        ),
        (
            "/api/productos",
            "cod_sku_pt",
            {
                "cod_sku_pt": "PT-MANUAL",
                "producto": "Balde",
                "linea_id": 1,
                "familia_id": 1,
            },
        ),
    ],
)
def test_crud_normal_rechaza_identificadores_manuales(client, path, field, payload):
    response = client.post(path, json=payload)

    assert response.status_code == 400
    assert response.get_json() == {
        "error": f"{field} es automático y no admite asignación manual",
        "codigo": "CODIGO_MANUAL_NO_PERMITIDO",
        "campo": field,
    }


def test_crud_normal_asigna_series_independientes(client):
    pieza = client.post(
        "/api/piezas",
        json={
            "nombre": "Tapa",
            "peso_nominal_gr": 8,
            "linea_id": 1,
            "familia_id": 1,
        },
    )
    molde = client.post(
        "/api/moldes",
        json={"nombre": "Molde de tapa", "peso_tiro_gr": 50},
    )
    variante = client.post(
        "/api/piezas-color",
        json={
            "nombre": "Tapa azul",
            "pieza_id": pieza.get_json()["id"],
            "linea_id": 1,
            "familia_id": 1,
        },
    )
    producto = client.post(
        "/api/productos",
        json={
            "producto": "Balde terminado",
            "linea_id": 1,
            "familia_id": 1,
        },
    )

    assert pieza.status_code == 201, pieza.get_json()
    assert molde.status_code == 201, molde.get_json()
    assert variante.status_code == 201, variante.get_json()
    assert producto.status_code == 201, producto.get_json()
    assert pieza.get_json()["codigo"] == "PZ-000001"
    assert molde.get_json()["codigo"] == "ML-000001"
    assert variante.get_json()["sku"] == "PC-000001"
    assert producto.get_json()["cod_sku_pt"] == "PT-000001"


def test_identificadores_no_cambian_en_edicion(client):
    pieza = client.post(
        "/api/piezas",
        json={"nombre": "Tapa", "peso_nominal_gr": 8},
    ).get_json()
    molde = client.post(
        "/api/moldes",
        json={"nombre": "Molde de tapa", "peso_tiro_gr": 50},
    ).get_json()
    variante = client.post(
        "/api/piezas-color",
        json={"nombre": "Tapa azul", "linea_id": 1, "familia_id": 1},
    ).get_json()
    producto = client.post(
        "/api/productos",
        json={"producto": "Balde", "linea_id": 1, "familia_id": 1},
    ).get_json()

    responses = [
        client.put(
            f"/api/piezas/{pieza['id']}",
            json={"version": pieza["version"], "codigo": "PZ-OTRO"},
        ),
        client.put(f"/api/moldes/{molde['codigo']}", json={"codigo": "ML-OTRO"}),
        client.put(f"/api/piezas-color/{variante['sku']}", json={"sku": "PC-OTRO"}),
        client.put(
            f"/api/productos/{producto['cod_sku_pt']}",
            json={"cod_sku_pt": "PT-OTRO"},
        ),
    ]

    assert [response.status_code for response in responses] == [400, 400, 400, 400]
    assert all(
        response.get_json()["codigo"] == "CODIGO_INMUTABLE"
        for response in responses
    )


def test_producto_detalle_expone_clasificacion_y_logistica_editables(client):
    created = client.post(
        "/api/productos",
        json={
            "producto": "Balde completo",
            "linea_id": 1,
            "familia_id": 1,
            "peso_g": 650,
            "doc_x_paq": 6,
            "doc_x_bulto": 24,
        },
    )
    assert created.status_code == 201, created.get_json()

    response = client.get(
        f"/api/productos/{created.get_json()['cod_sku_pt']}",
    )

    assert response.status_code == 200
    expected = {
        "linea_id": 1,
        "familia_id": 1,
        "peso_g": 650,
        "doc_x_paq": 6,
        "doc_x_bulto": 24,
    }
    assert {
        key: response.get_json()[key]
        for key in expected
    } == expected


def test_producto_rechaza_nombre_vacio_al_crear_y_editar(client):
    created = client.post(
        "/api/productos",
        json={"producto": "Balde", "linea_id": 1, "familia_id": 1},
    )
    assert created.status_code == 201, created.get_json()

    create_response = client.post(
        "/api/productos",
        json={"producto": "   ", "linea_id": 1, "familia_id": 1},
    )
    update_response = client.put(
        f"/api/productos/{created.get_json()['cod_sku_pt']}",
        json={"producto": "   "},
    )

    assert create_response.status_code == 400
    assert update_response.status_code == 400
    assert create_response.get_json()["error"] == "producto es obligatorio"
    assert update_response.get_json()["error"] == "producto es obligatorio"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("peso_g", -0.1),
        ("peso_g", "no-numérico"),
        ("doc_x_paq", 0),
        ("doc_x_bulto", 1.5),
    ],
)
def test_producto_rechaza_referencias_logisticas_invalidas(client, field, value):
    response = client.post(
        "/api/productos",
        json={
            "producto": "Balde",
            "linea_id": 1,
            "familia_id": 1,
            field: value,
        },
    )

    assert response.status_code == 400
    assert response.get_json()["codigo"] == "VALOR_INVALIDO"
    assert response.get_json()["campo"] == field


def test_configuracion_cascada_usa_las_mismas_series(client, app):
    response = client.post(
        "/api/configurar-producto",
        json={
            "linea_id": 1,
            "familia_id": 1,
            "molde": {
                "nombre": "Molde balde",
                "peso_tiro_gr": 120,
                "tiempo_ciclo_std": 30,
                "usar_existente": False,
            },
            "piezas": [
                {
                    "nombre": "Cuerpo de balde",
                    "cavidades": 1,
                    "peso_unitario_gr": 100,
                }
            ],
            "producto_terminado": {"producto": "Balde terminado"},
        },
    )

    assert response.status_code == 201, response.get_json()
    result = response.get_json()["resultado"]
    assert result["molde_creado"] == "ML-000001"
    assert result["producto_terminado"] == "PT-000001"
    assert result["piezas_creadas"] == ["PC-000001"]

    with app.app_context():
        assert db.session.get(Molde, "ML-000001") is not None
        assert Pieza.query.filter_by(codigo="PZ-000001").one()
        assert db.session.get(PiezaColor, "PC-000001") is not None
        assert db.session.get(ProductoTerminado, "PT-000001") is not None


@pytest.mark.parametrize("legacy_type", ["KIT", "COMPONENTE"])
def test_pieza_color_rechaza_nuevas_clasificaciones_legacy(
    client,
    legacy_type,
):
    response = client.post(
        "/api/piezas-color",
        json={
            "nombre": f"Variante {legacy_type}",
            "tipo": legacy_type,
            "linea_id": 1,
            "familia_id": 1,
        },
    )

    assert response.status_code == 422
    assert response.get_json()["codigo"] == "LEGACY_KIT_NOT_SUPPORTED"


def test_pieza_color_no_permite_recrear_componentes_legacy(client):
    variant = client.post(
        "/api/piezas-color",
        json={
            "nombre": "Pieza simple",
            "linea_id": 1,
            "familia_id": 1,
        },
    ).get_json()

    detail = client.get(
        f"/api/piezas-color/{variant['sku']}",
    ).get_json()
    assert "tipo" not in detail
    assert "componentes" not in detail

    response = client.put(
        f"/api/piezas-color/{variant['sku']}",
        json={"componentes": []},
    )

    assert response.status_code == 422
    assert response.get_json()["codigo"] == "LEGACY_KIT_NOT_SUPPORTED"
