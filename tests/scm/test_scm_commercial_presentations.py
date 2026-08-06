from uuid import uuid4

from app import db


def _manager_and_product():
    from app.models.producto import ProductoTerminado
    from app.models.trabajador import RolOperativo, Trabajador

    actor = Trabajador.query.filter_by(codigo="TRB-01").one()
    actor.roles.append(
        RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
    )
    product = ProductoTerminado(
        cod_sku_pt="PT-PACK-001",
        producto="Alcancia Pablo Grande",
        linea_id=1,
        familia_id=1,
    )
    db.session.add(product)
    db.session.commit()
    return actor.id, product.cod_sku_pt


def test_new_product_receives_unit_presentation(app, client):
    created = client.post(
        "/api/productos",
        json={
            "producto": "Alcancia Pablo Grande",
            "linea_id": 1,
            "familia_id": 1,
        },
    )
    assert created.status_code == 201
    product_id = created.get_json()["cod_sku_pt"]

    with app.app_context():
        from app.models.scm_commercial import ScmPresentacionComercial

        presentation = ScmPresentacionComercial.query.one()
        assert presentation.producto_terminado_id == product_id
        assert presentation.nombre == "Unidad"
        assert presentation.unidades_base == 1
        assert presentation.predeterminada is True


def test_commercial_presentations_have_one_active_default(
    app,
    client,
    scm_config,
):
    with app.app_context():
        actor_id, product_id = _manager_and_product()

    headers = {"X-Actor-Id": str(actor_id)}
    pack_response = client.post(
        "/api/scm/v1/presentaciones-comerciales",
        headers=headers,
        json={
            "producto_terminado_id": product_id,
            "nombre": "Pack x6",
            "unidades_base": 6,
            "codigo_barra": "7750000000006",
        },
    )
    assert pack_response.status_code == 201
    pack = pack_response.get_json()
    assert pack["codigo"] == "PRE-000001"
    assert pack["predeterminada"] is True

    unit_response = client.post(
        "/api/scm/v1/presentaciones-comerciales",
        headers=headers,
        json={
            "producto_terminado_id": product_id,
            "nombre": "Unidad",
            "unidades_base": 1,
        },
    )
    assert unit_response.status_code == 201
    unit = unit_response.get_json()
    assert unit["predeterminada"] is False

    default_response = client.patch(
        f"/api/scm/v1/presentaciones-comerciales/{unit['id']}",
        headers=headers,
        json={"version": unit["version"], "predeterminada": True},
    )
    assert default_response.status_code == 200
    assert default_response.get_json()["predeterminada"] is True

    listed = client.get(
        f"/api/scm/v1/presentaciones-comerciales?producto_terminado_id={product_id}",
        headers=headers,
    )
    assert listed.status_code == 200
    items = listed.get_json()["items"]
    assert sum(item["predeterminada"] and item["activo"] for item in items) == 1
    unit = next(item for item in items if item["nombre"] == "Unidad")

    blocked = client.patch(
        f"/api/scm/v1/presentaciones-comerciales/{unit['id']}",
        headers=headers,
        json={"version": unit["version"], "activo": False},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["error"]["code"] == "DEFAULT_PRESENTATION_REQUIRED"


def test_demand_in_packs_is_normalized_and_snapshotted(
    app,
    client,
    scm_config,
):
    with app.app_context():
        actor_id, product_id = _manager_and_product()

    headers = {"X-Actor-Id": str(actor_id)}
    pack = client.post(
        "/api/scm/v1/presentaciones-comerciales",
        headers=headers,
        json={
            "producto_terminado_id": product_id,
            "nombre": "Pack x6",
            "unidades_base": 6,
            "predeterminada": True,
        },
    ).get_json()

    created = client.post(
        "/api/scm/v1/ordenes-produccion",
        headers={
            **headers,
            "Idempotency-Key": str(uuid4()),
        },
        json={
            "origen": "PLANIFICACION",
            "fecha_necesidad": "2026-08-20",
            "lineas": [{
                "producto_terminado_id": product_id,
                "presentacion_comercial_id": pack["id"],
                "cantidad_presentaciones": 10,
            }],
        },
    )
    assert created.status_code == 201
    line = created.get_json()["lineas"][0]
    assert line["cantidad_solicitada"] == "60.000"
    assert line["presentacion_comercial"] == {
        "id": pack["id"],
        "codigo": pack["codigo"],
        "nombre": "Pack x6",
        "unidades_base": 6,
        "cantidad": 10,
    }

    with app.app_context():
        from app.models.scm_production_orders import ScmOrdenProduccionLinea

        stored = ScmOrdenProduccionLinea.query.one()
        assert stored.cantidad_solicitada == 60
        assert stored.snapshot_unidades_por_presentacion == 6
