"""Contrato API de la relación N:M entre piezas globales y moldes."""

from app.extensions import db
from app.models.molde import MoldePieza, Pieza


def _crear_molde(client, nombre, peso_tiro):
    response = client.post(
        "/api/moldes",
        json={
            "nombre": nombre,
            "peso_tiro_gr": peso_tiro,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["codigo"]


def test_pieza_global_se_asocia_a_dos_moldes_y_se_desvincula_sin_borrarla(
    client,
    app,
):
    pieza_response = client.post(
        "/api/piezas",
        json={
            "nombre": "Tapa compartida API",
            "peso_nominal_gr": 12.5,
            "linea_id": 1,
            "familia_id": 1,
        },
    )
    assert pieza_response.status_code == 201, pieza_response.get_json()
    pieza = pieza_response.get_json()
    assert pieza["codigo"] == "PZ-000001"

    molde_a = _crear_molde(client, "Molde API N:M A", 60.0)
    molde_b = _crear_molde(client, "Molde API N:M B", 90.0)
    assert (molde_a, molde_b) == ("ML-000001", "ML-000002")

    relacion_a_response = client.post(
        f"/api/moldes/{molde_a}/formas",
        json={
            "pieza_id": pieza["id"],
            "cavidades": 4,
            "peso_unitario_gr": 12.5,
        },
    )
    assert relacion_a_response.status_code == 201, relacion_a_response.get_json()
    relacion_a = relacion_a_response.get_json()

    relacion_b_response = client.post(
        f"/api/moldes/{molde_b}/formas",
        json={
            "pieza_id": pieza["id"],
            "cavidades": 6,
            "peso_unitario_gr": 13.0,
        },
    )
    assert relacion_b_response.status_code == 201, relacion_b_response.get_json()
    relacion_b = relacion_b_response.get_json()

    color_response = client.post("/api/colores", json={"nombre": "AZUL NM"})
    assert color_response.status_code == 201, color_response.get_json()
    color_id = color_response.get_json()["id"]

    variante_a_response = client.post(
        f"/api/formas/{relacion_a['id']}/colores",
        json={"color_id": color_id},
    )
    assert variante_a_response.status_code == 201, variante_a_response.get_json()
    variante_a = variante_a_response.get_json()
    assert variante_a["sku"] == "PC-000001"

    # El segundo molde reutiliza el mismo SKU: PiezaColor pertenece a Pieza.
    variante_b_response = client.post(
        f"/api/formas/{relacion_b['id']}/colores",
        json={"color_id": color_id},
    )
    assert variante_b_response.status_code == 200, variante_b_response.get_json()
    variante_b = variante_b_response.get_json()
    assert variante_b["existed"] is True
    assert variante_b["sku"] == variante_a["sku"]

    update_response = client.put(
        f"/api/formas/{relacion_a['id']}",
        json={
            "version": relacion_a["version"],
            "cavidades": 3,
            "peso_unitario_gr": 12.0,
        },
    )
    assert update_response.status_code == 200, update_response.get_json()
    relacion_a_actualizada = update_response.get_json()
    assert relacion_a_actualizada["cavidades"] == 3
    assert relacion_a_actualizada["peso_unitario_gr"] == 12.0

    stale_response = client.put(
        f"/api/formas/{relacion_a['id']}",
        json={
            "version": relacion_a["version"],
            "cavidades": 2,
            "peso_unitario_gr": 11.0,
        },
    )
    assert stale_response.status_code == 409
    assert stale_response.get_json()["codigo"] == "VERSION_CONFLICT"

    # La configuración del segundo molde y el maestro no cambian.
    molde_b_response = client.get(f"/api/moldes/{molde_b}")
    assert molde_b_response.status_code == 200
    forma_b = molde_b_response.get_json()["formas"][0]
    assert forma_b["id"] == relacion_b["id"]
    assert forma_b["cavidades"] == 6
    assert forma_b["peso_unitario_gr"] == 13.0

    pieza_response = client.get(f"/api/piezas/{pieza['id']}")
    assert pieza_response.status_code == 200
    assert pieza_response.get_json()["peso_nominal_gr"] == 12.5

    delete_response = client.delete(f"/api/formas/{relacion_a['id']}")
    assert delete_response.status_code == 200, delete_response.get_json()

    # Desvincular es lógico: la pieza y ambas relaciones siguen persistidas.
    pieza_response = client.get(f"/api/piezas/{pieza['id']}")
    assert pieza_response.status_code == 200
    pieza_final = pieza_response.get_json()
    assert pieza_final["codigo"] == "PZ-000001"
    assert [molde["molde_id"] for molde in pieza_final["moldes"]] == [
        molde_b
    ]

    molde_a_response = client.get(f"/api/moldes/{molde_a}")
    assert molde_a_response.status_code == 200
    assert molde_a_response.get_json()["formas"] == []

    with app.app_context():
        assert db.session.get(Pieza, pieza["id"]) is not None
        relaciones = MoldePieza.query.filter_by(pieza_id=pieza["id"]).all()
        assert len(relaciones) == 2
        estados = {item.molde_id: item.activo for item in relaciones}
        assert estados == {
            molde_a: False,
            molde_b: True,
        }
