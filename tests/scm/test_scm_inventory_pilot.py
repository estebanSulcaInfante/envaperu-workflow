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
