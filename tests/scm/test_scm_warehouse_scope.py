from uuid import uuid4

from app import db


def _headers(actor_id, *, operation=False):
    headers = {"X-Actor-Id": str(actor_id)}
    if operation:
        headers["Idempotency-Key"] = str(uuid4())
    return headers


def test_almacen_configurable_activa_scope_fail_closed(
    app, client, scm_config,
):
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import ScmSaldoInventario
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        admin = Trabajador.query.filter_by(codigo="TRB-01").one()
        admin.roles.append(
            RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
        )
        scoped = Trabajador(
            codigo="TRB-ALM-SCOPE",
            nombres="Almacenera",
            apellidos="Piezas",
            activo=True,
        )
        scoped.roles.append(
            RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        )
        article = ScmArticulo(
            codigo="PC-SCOPE-018",
            nombre="Pieza para alcance",
            clase="PIEZA_COLOR",
        )
        db.session.add_all([scoped, article])
        db.session.commit()
        admin_id = admin.id
        scoped_id = scoped.id
        article_id = article.id

    created = client.post(
        "/api/scm/v1/almacenes",
        headers=_headers(admin_id, operation=True),
        json={
            "codigo": "ALM-UAT-A",
            "nombre": "Almacen configurable A",
            "tipo": "PIEZAS_WIP",
        },
    )
    assert created.status_code == 201
    warehouse = created.get_json()

    location = client.post(
        f"/api/scm/v1/almacenes/{warehouse['id']}/ubicaciones",
        headers=_headers(admin_id, operation=True),
        json={
            "codigo": "POS-UAT-A1",
            "nombre": "Posicion A1",
            "tipo": "POSICION",
            "clases_articulo": ["PIEZA_COLOR"],
        },
    )
    assert location.status_code == 201

    with app.app_context():
        from app.models.scm_inventory import ScmUbicacionInventario

        persisted_location = ScmUbicacionInventario.query.filter_by(
            codigo="POS-UAT-A1"
        ).one()
        db.session.add(ScmSaldoInventario(
            articulo_scm_id=article_id,
            ubicacion_id=persisted_location.id,
            cantidad_fisica=12,
        ))
        db.session.commit()

    hidden = client.get(
        "/api/scm/v1/inventario/saldos",
        headers=_headers(scoped_id),
    )
    assert hidden.status_code == 200
    assert hidden.get_json()["items"] == []
    hidden_explorer = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(scoped_id),
        query_string={"kardex": "PIEZAS_WIP", "limite": 25},
    )
    assert hidden_explorer.status_code == 200
    assert hidden_explorer.get_json()["items"] == []
    hidden_summary = client.get(
        "/api/scm/v1/inventario/resumen", headers=_headers(scoped_id),
    )
    assert hidden_summary.status_code == 200
    assert hidden_summary.get_json()["items"] == []

    assigned = client.post(
        f"/api/scm/v1/almacenes/{warehouse['id']}/trabajadores",
        headers=_headers(admin_id, operation=True),
        json={
            "trabajador_id": scoped_id,
            "clases_articulo": ["PIEZA_COLOR"],
        },
    )
    assert assigned.status_code == 201

    visible = client.get(
        "/api/scm/v1/inventario/saldos",
        headers=_headers(scoped_id),
    )
    assert visible.status_code == 200
    assert [item["articulo"]["codigo"] for item in visible.get_json()["items"]] == [
        "PC-SCOPE-018"
    ]
    visible_explorer = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(scoped_id),
        query_string={"kardex": "PIEZAS_WIP", "limite": 25},
    )
    assert visible_explorer.status_code == 200
    assert [
        item["articulo"]["codigo"]
        for item in visible_explorer.get_json()["items"]
    ] == ["PC-SCOPE-018"]
    visible_summary = client.get(
        "/api/scm/v1/inventario/resumen", headers=_headers(scoped_id),
    )
    assert visible_summary.status_code == 200
    assert visible_summary.get_json()["items"][0]["fisico"] == "12.000"

    reach = client.get(
        "/api/scm/v1/mi-alcance-almacen",
        headers=_headers(scoped_id),
    )
    assert reach.status_code == 200
    assert reach.get_json()["almacenes"][0]["codigo"] == "ALM-UAT-A"


def test_detalle_de_almacen_fuera_de_scope_no_filtra_existencia(
    app, client, scm_config,
):
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        admin = Trabajador.query.filter_by(codigo="TRB-01").one()
        admin.roles.append(
            RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
        )
        outsider = Trabajador(
            codigo="TRB-ALM-OUT",
            nombres="Fuera",
            apellidos="Alcance",
            activo=True,
        )
        outsider.roles.append(
            RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        )
        db.session.add(outsider)
        db.session.commit()
        admin_id, outsider_id = admin.id, outsider.id

    created = client.post(
        "/api/scm/v1/almacenes",
        headers=_headers(admin_id, operation=True),
        json={"codigo": "ALM-PRIVADO", "nombre": "Privado", "tipo": "PIEZAS_WIP"},
    ).get_json()
    response = client.get(
        f"/api/scm/v1/almacenes/{created['id']}",
        headers=_headers(outsider_id),
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "WAREHOUSE_NOT_FOUND"
