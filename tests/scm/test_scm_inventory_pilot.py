from uuid import uuid4

from app import db


def test_saldo_inicial_individual_exige_lote_controlado(
    app,
    client,
    scm_config,
):
    from app.models.scm_articulos import ScmArticulo
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        )
        article = ScmArticulo(
            codigo="WIP-INVENTARIO-UAT",
            nombre="Articulo inventariable UAT",
            clase="SUBENSAMBLE_WIP",
        )
        db.session.add(article)
        db.session.flush()
        actor_id = actor.id
        article_id = article.id
        db.session.commit()

    operation_id = str(uuid4())
    headers = {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": operation_id,
    }
    payload = {
        "tipo": "SALDO_INICIAL",
        "articulo_scm_id": article_id,
        "cantidad": 25,
        "ubicacion_codigo": "ALMACEN_UAT",
        "ubicacion_nombre": "Almacen UAT",
        "motivo": "Conteo de apertura UAT",
    }
    created = client.post(
        "/api/scm/v1/inventario/movimientos",
        headers=headers,
        json=payload,
    )
    assert created.status_code == 409
    assert created.get_json()["error"]["code"] == (
        "INITIAL_BALANCE_BATCH_REQUIRED"
    )

    balances = client.get(
        "/api/scm/v1/inventario/saldos",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert balances.status_code == 200
    assert balances.get_json()["items"] == []

    movements = client.get(
        "/api/scm/v1/inventario/movimientos",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert movements.status_code == 200
    assert movements.get_json()["items"] == []


def test_lote_apertura_exige_cuatro_ojos_y_aplica_lineas_atomicamente(
    app, client, scm_config,
):
    from app.models.scm_articulos import ScmArticulo
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        preparer = Trabajador.query.filter_by(codigo="TRB-01").one()
        from app.models.scm_catalogos import ScmCategoriaRecepcion, ScmMaterial
        preparer.roles.append(
            RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        )
        # También tiene capacidad de aprobación para probar que el backend
        # bloquea la autoaprobación aunque el permiso exista.
        preparer.roles.append(
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        )
        preparer.roles.append(
            RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
        )
        category_id = ScmCategoriaRecepcion.query.filter_by(
            codigo="LEGACY_POR_CONFIGURAR"
        ).one().id
        approver = Trabajador(
            codigo="TRB-APERTURA-JP", nombres="Jefa", apellidos="Produccion",
            activo=True,
        )
        approver.roles.append(
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        )
        released = ScmArticulo(
            codigo="PZ-APERTURA-LIB", nombre="Pieza apertura liberada",
            clase="PIEZA_COLOR",
        )
        pending = ScmMaterial(
            codigo="MP-APERTURA-PEN", nombre="Material apertura pendiente",
            clase="MATERIA_PRIMA",
            categoria_recepcion_id=category_id,
        )
        db.session.add_all([approver, released, pending])
        db.session.commit()
        preparer_id = preparer.id
        approver_id = approver.id
        released_id = released.id
        pending_id = pending.id

    create_response = client.post(
        "/api/scm/v1/inventario/aperturas",
        headers={
            "X-Actor-Id": str(preparer_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={
            "fecha_corte": "2026-08-03",
            "motivo": "Conteo fisico de puesta en marcha",
            "metodo": "TABULAR_CONTINGENCIA",
            "lineas": [
                {
                    "articulo_scm_id": released_id,
                    "cantidad": "12",
                    "ubicacion_codigo": "ALMACEN_PIEZAS",
                    "ubicacion_nombre": "Almacen de piezas",
                    "estado_calidad": "LIBERADO",
                },
                {
                    "material_scm_id": pending_id,
                    "cantidad": "25.5",
                    "ubicacion_codigo": "ALMACEN_MP",
                    "ubicacion_nombre": "Almacen de materia prima",
                    "estado_calidad": "PENDIENTE",
                },
            ],
        },
    )
    assert create_response.status_code == 201
    draft = create_response.get_json()
    assert draft["estado"] == "BORRADOR"
    assert draft["total_lineas"] == 2

    submitted_response = client.post(
        f"/api/scm/v1/inventario/aperturas/{draft['id']}/enviar",
        headers={
            "X-Actor-Id": str(preparer_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={"version": draft["version"]},
    )
    assert submitted_response.status_code == 200
    submitted = submitted_response.get_json()
    assert submitted["estado"] == "PENDIENTE_APROBACION"

    self_approval = client.post(
        f"/api/scm/v1/inventario/aperturas/{draft['id']}/resolver",
        headers={
            "X-Actor-Id": str(preparer_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={
            "version": submitted["version"], "decision": "APROBAR",
            "motivo_resolucion": "Intento invalido",
        },
    )
    assert self_approval.status_code == 409

    approved_response = client.post(
        f"/api/scm/v1/inventario/aperturas/{draft['id']}/resolver",
        headers={
            "X-Actor-Id": str(approver_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={
            "version": submitted["version"], "decision": "APROBAR",
            "motivo_resolucion": "Conteo revisado contra hojas fisicas",
        },
    )
    assert approved_response.status_code == 200
    approved = approved_response.get_json()
    assert approved["estado"] == "APLICADO"
    assert all(line["movimiento_id"] for line in approved["lineas"])

    balances_response = client.get(
        "/api/scm/v1/inventario/saldos",
        headers={"X-Actor-Id": str(preparer_id)},
    )
    assert balances_response.status_code == 200
    balance_payload = balances_response.get_json()
    balances = {
        item["articulo"]["codigo"]: item
        for item in balance_payload["items"] + balance_payload["materiales"]
    }
    assert balances["PZ-APERTURA-LIB"]["cantidad_libre"] == "12.000"
    assert balances["MP-APERTURA-PEN"]["cantidad_fisica"] == "25.500"
    assert balances["MP-APERTURA-PEN"]["cantidad_no_disponible"] == "25.500"
    assert balances["MP-APERTURA-PEN"]["cantidad_libre"] == "0.000"


def test_carga_tabular_de_apertura_es_exclusiva_de_gerencia(
    app, client, scm_config,
):
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        warehouse_actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        warehouse_actor.roles.append(
            RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        )
        db.session.commit()
        actor_id = warehouse_actor.id

    response = client.post(
        "/api/scm/v1/inventario/aperturas",
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
        json={
            "fecha_corte": "2026-08-20",
            "motivo": "Intento tabular sin autorización excepcional",
            "metodo": "TABULAR_CONTINGENCIA",
            "lineas": [],
        },
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == (
        "INVENTORY_OPENING_CONTINGENCY_FORBIDDEN"
    )


def test_apertura_fisica_deriva_saldo_de_bolsas_pesadas_con_qr(
    app, client, scm_config,
):
    from app.models.scm_catalogos import ScmCategoriaRecepcion, ScmMaterial
    from app.models.scm_inventory import ScmUbicacionInventario
    from app.models.scm_inventory_operations import ScmAlmacen
    from app.models.trabajador import RolOperativo, Trabajador
    from app.services.scm_inventory_opening_service import capture_physical_opening_unit

    with app.app_context():
        preparer = Trabajador.query.filter_by(codigo="TRB-01").one()
        preparer.roles.append(RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one())
        approver = Trabajador(codigo="TRB-FISICO-JP", nombres="Jefe", apellidos="Fisico", activo=True)
        db.session.add(approver)
        approver.roles.append(RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one())
        category = ScmCategoriaRecepcion.query.filter_by(codigo="LEGACY_POR_CONFIGURAR").one()
        material = ScmMaterial(
            codigo="MP-FISICA-QR", nombre="Resina de apertura física",
            clase="MATERIA_PRIMA", unidad_base="KG", categoria_recepcion_id=category.id,
        )
        warehouse = ScmAlmacen(
            codigo="A-FIS", nombre="Almacén físico", tipo="MATERIAS_PRIMAS", activo=True,
        )
        location = ScmUbicacionInventario(
            codigo="A-FIS-B01", nombre="Bloque físico 1", almacen=warehouse, activo=True,
        )
        db.session.add_all([approver, material, warehouse, location])
        db.session.commit()
        preparer_id, approver_id, material_id = preparer.id, approver.id, material.id

    created = client.post(
        "/api/scm/v1/inventario/aperturas",
        headers={"X-Actor-Id": str(preparer_id), "Idempotency-Key": str(uuid4())},
        json={
            "fecha_corte": "2026-08-20", "motivo": "Conteo pesado de bolsas",
            "metodo": "CONTEO_FISICO_QR", "lineas": [],
        },
    )
    assert created.status_code == 201
    opening = created.get_json()
    assert opening["total_lineas"] == 0
    assert opening["creado_por"]["id"] == preparer_id
    assert opening["creado_por"]["nombre"]

    with app.app_context():
        first = capture_physical_opening_unit(
            db.session, station_id="PESAJE-UAT", operation_id=uuid4(), data={
                "opening_id": opening["id"], "material_scm_id": material_id,
                "ubicacion_codigo": "A-FIS-B01", "peso_bruto_kg": "25.100",
                "tara_kg": "0.100", "estado_calidad": "PENDIENTE",
                "pesado_por_id": preparer_id, "reading_stable": True,
            },
        )
        second = capture_physical_opening_unit(
            db.session, station_id="PESAJE-UAT", operation_id=uuid4(), data={
                "opening_id": opening["id"], "material_scm_id": material_id,
                "ubicacion_codigo": "A-FIS-B01", "peso_bruto_kg": "25.100",
                "tara_kg": "0.100", "estado_calidad": "PENDIENTE",
                "pesado_por_id": preparer_id, "reading_stable": True,
            },
        )
    assert first["lineas"][0]["cantidad"] == "25.000"
    assert second["lineas"][0]["cantidad"] == "50.000"
    assert second["total_unidades_logisticas"] == 2
    assert all(item["qr_value"].startswith("SCM:UL:") for item in second["unidades_logisticas"])

    submitted = client.post(
        f"/api/scm/v1/inventario/aperturas/{opening['id']}/enviar",
        headers={"X-Actor-Id": str(preparer_id), "Idempotency-Key": str(uuid4())},
        json={"version": second["version"]},
    ).get_json()
    approved = client.post(
        f"/api/scm/v1/inventario/aperturas/{opening['id']}/resolver",
        headers={"X-Actor-Id": str(approver_id), "Idempotency-Key": str(uuid4())},
        json={
            "version": submitted["version"], "decision": "APROBAR",
            "motivo_resolucion": "Dos bolsas y pesos revisados",
        },
    )
    assert approved.status_code == 200
    assert {item["estado"] for item in approved.get_json()["unidades_logisticas"]} == {"BLOQUEADA"}
