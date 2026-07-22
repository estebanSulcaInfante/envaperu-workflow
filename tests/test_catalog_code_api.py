"""Contrato HTTP de los códigos automáticos e inmutables del catálogo."""

import pytest

from app.extensions import db
from app.models.molde import Molde, Pieza
from app.models.producto import PiezaColor, ProductoTerminado


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
