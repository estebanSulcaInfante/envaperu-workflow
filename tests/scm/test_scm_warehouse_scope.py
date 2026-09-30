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
    from app.models.scm_catalogos import ScmMaterial, ScmCategoriaRecepcion
    from app.models.scm_inventory import ScmSaldoInventario, ScmSaldoMaterialInventario
    from app.models.scm_inventory_kg import ScmSaldoInventarioKg
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
        kg_article = ScmArticulo(
            codigo="PC-SCOPE-KG-018",
            nombre="Pieza KG para alcance",
            clase="PIEZA_COLOR",
            unidad_inventario="KG",
        )
        material = ScmMaterial(
            codigo="MP-SCOPE-018",
            nombre="Material KG para alcance",
            clase="MATERIA_PRIMA",
            categoria_recepcion_id=ScmCategoriaRecepcion.query.filter_by(
                codigo="LEGACY_POR_CONFIGURAR"
            ).one().id,
        )
        db.session.add_all([scoped, article, kg_article, material])
        db.session.commit()
        admin_id = admin.id
        scoped_id = scoped.id
        article_id = article.id
        kg_article_id = kg_article.id
        material_id = material.id

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
            "clases_articulo": ["PIEZA_COLOR", "MATERIA_PRIMA"],
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
        db.session.add(ScmSaldoInventarioKg(
            articulo_scm_id=kg_article_id,
            ubicacion_id=persisted_location.id,
            cantidad_fisica_kg=7.250,
            cantidad_reservada_kg=1.250,
            cantidad_no_disponible_kg=0.500,
        ))
        # An opt-in KG article can retain a legacy UN row; compatibility UN
        # aggregates and the UN explorer preserve this row.
        db.session.add(ScmSaldoInventario(
            articulo_scm_id=kg_article_id,
            ubicacion_id=persisted_location.id,
            cantidad_fisica=99,
        ))
        db.session.add(ScmSaldoMaterialInventario(
            material_id=material_id,
            ubicacion_id=persisted_location.id,
            cantidad_fisica_kg=14.500,
            cantidad_reservada_kg=2.500,
            cantidad_no_disponible_kg=1.000,
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
            "clases_articulo": ["PIEZA_COLOR", "MATERIA_PRIMA"],
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
    explorer_items = visible_explorer.get_json()["items"]
    assert [
        item["articulo"]["codigo"]
        for item in explorer_items
    ] == ["PC-SCOPE-018", "PC-SCOPE-KG-018"]
    assert next(
        item for item in explorer_items
        if item["articulo"]["codigo"] == "PC-SCOPE-KG-018"
    )["articulo"]["unidad"] == "UN"
    warehouse_explorer = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(scoped_id),
        query_string={
            "kardex": "PIEZAS_WIP", "almacen_id": warehouse["id"], "limite": 25,
        },
    )
    assert warehouse_explorer.status_code == 200
    assert [
        item["articulo"]["codigo"]
        for item in warehouse_explorer.get_json()["items"]
    ] == ["PC-SCOPE-018", "PC-SCOPE-KG-018"]
    material_explorer = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(scoped_id),
        query_string={
            "kardex": "MATERIALES", "almacen_id": warehouse["id"], "limite": 25,
        },
    )
    assert material_explorer.status_code == 200
    assert [
        item["articulo"]["codigo"]
        for item in material_explorer.get_json()["items"]
    ] == ["MP-SCOPE-018"]
    visible_summary = client.get(
        "/api/scm/v1/inventario/resumen", headers=_headers(scoped_id),
    )
    assert visible_summary.status_code == 200
    summary_payload = visible_summary.get_json()
    assert summary_payload["items"][0]["fisico"] == "111.000"
    expected_families = [{
        "almacen_id": str(warehouse["id"]),
        "almacen_codigo": "ALM-UAT-A",
        "almacen_nombre": "Almacen configurable A",
        "clase": "PIEZA_COLOR",
        "unidad": "UN",
        "posiciones": 2,
        "fisico": "111.000",
        "reservado": "0.000",
        "no_disponible": "0.000",
        "libre": "111.000",
    }, {
        "almacen_id": str(warehouse["id"]),
        "almacen_codigo": "ALM-UAT-A",
        "almacen_nombre": "Almacen configurable A",
        "clase": "PIEZA_COLOR",
        "unidad": "KG",
        "posiciones": 1,
        "fisico": "7.250",
        "reservado": "1.250",
        "no_disponible": "0.500",
        "libre": "5.500",
    }, {
        "almacen_id": str(warehouse["id"]),
        "almacen_codigo": "ALM-UAT-A",
        "almacen_nombre": "Almacen configurable A",
        "clase": "MATERIA_PRIMA",
        "unidad": "KG",
        "posiciones": 1,
        "fisico": "14.500",
        "reservado": "2.500",
        "no_disponible": "1.000",
        "libre": "11.000",
    }]
    family_key = lambda item: (item["clase"], item["unidad"])
    assert sorted(summary_payload["familias"], key=family_key) == sorted(
        expected_families, key=family_key,
    )

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


def test_resumen_y_explorador_conservan_saldo_sin_almacen_explicito(
    app, client, scm_config,
):
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import ScmSaldoInventario, ScmUbicacionInventario
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
        )
        article = ScmArticulo(
            codigo="PC-NO-WAREHOUSE-018",
            nombre="Pieza sin almacen",
            clase="PIEZA_COLOR",
        )
        location = ScmUbicacionInventario(
            codigo="POS-SIN-ALMACEN",
            nombre="Posicion sin almacen",
            tipo="POSICION",
            clases_articulo_json=["PIEZA_COLOR"],
        )
        db.session.add_all([article, location])
        db.session.flush()
        db.session.add(ScmSaldoInventario(
            articulo_scm_id=article.id,
            ubicacion_id=location.id,
            cantidad_fisica=9,
        ))
        db.session.commit()
        actor_id = actor.id

    summary = client.get(
        "/api/scm/v1/inventario/resumen", headers=_headers(actor_id),
    )
    assert summary.status_code == 200
    family = next(
        item for item in summary.get_json()["familias"]
        if item["clase"] == "PIEZA_COLOR" and item["unidad"] == "UN"
    )
    assert family["almacen_id"] is None
    assert family["almacen_codigo"] is None
    assert family["fisico"] == "9.000"

    explorer = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(actor_id),
        query_string={
            "kardex": "PIEZAS_WIP", "almacen_id": "SIN_ALMACEN", "limite": 25,
        },
    )
    assert explorer.status_code == 200
    assert [item["articulo"]["codigo"] for item in explorer.get_json()["items"]] == [
        "PC-NO-WAREHOUSE-018"
    ]
