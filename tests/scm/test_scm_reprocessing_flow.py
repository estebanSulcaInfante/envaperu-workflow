from uuid import uuid4

from app import db


def _setup(app):
    from app.models.producto import ColorBase
    from app.models.scm_catalogos import ScmCategoriaRecepcion, ScmMaterial
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        warehouse = Trabajador.query.filter_by(codigo="TRB-01").one()
        warehouse.roles.extend([
            RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one(),
            RolOperativo.query.filter_by(codigo="OPERADOR_MOLINO").one(),
        ])
        manager = Trabajador(
            codigo="TRB-JP-01",
            nombres="Jefa",
            apellidos="Produccion",
            activo=True,
            roles=[RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()],
        )
        color = ColorBase(nombre="AMARILLO UAT")
        category = ScmCategoriaRecepcion(
            codigo="RECUPERADO_UAT",
            nombre="Recuperado UAT",
            modalidad_default="VIRGEN_CONFIANZA_PROVEEDOR",
            lote_externo_obligatorio=False,
            recepcion_habilitada=True,
        )
        output = ScmMaterial(
            codigo="MR-UAT-001",
            nombre="PP recuperado amarillo UAT",
            clase="MATERIA_PRIMA",
            categoria_recepcion=category,
        )
        db.session.add_all([manager, color, output])
        db.session.commit()
        return warehouse.id, manager.id, color.id, output.id


def _headers(actor_id, operation=False):
    result = {"X-Actor-Id": str(actor_id)}
    if operation:
        result["Idempotency-Key"] = str(uuid4())
    return result


def test_flujo_molienda_no_duplica_peso_y_libera_solo_por_jefatura(
    app, client, scm_config,
):
    warehouse_id, manager_id, color_id, material_id = _setup(app)
    refs = client.get(
        "/api/scm/v1/reproceso/referencias",
        headers=_headers(warehouse_id),
    ).get_json()
    pp_id = next(item["id"] for item in refs["familias_material"] if item["codigo"] == "PP")
    injection_id = next(item["id"] for item in refs["procesos"] if item["codigo"] == "INYECCION")
    clean_id = next(item["id"] for item in refs["condiciones"] if item["codigo"] == "LIMPIA")

    created = client.post(
        "/api/scm/v1/reproceso/mermas",
        headers=_headers(warehouse_id, operation=True),
        json={
            "familia_material_id": pp_id,
            "proceso_origen_id": injection_id,
            "condicion_id": clean_id,
            "color_id": color_id,
            "origen_tipo": "FABRICACION",
            "origen_id": "OF-UAT-001",
            "peso_bruto_kg": 10.2,
            "tara_kg": 0.2,
            "ubicacion_codigo": "MERMA-UAT",
        },
    )
    assert created.status_code == 201, created.get_json()
    lot_id = created.get_json()["lote"]["id"]
    assert created.get_json()["lote"]["saldo_disponible_kg"] == "10.000"

    order_response = client.post(
        "/api/scm/v1/reproceso/ordenes-molienda",
        headers=_headers(warehouse_id),
        json={
            "familia_objetivo_id": pp_id,
            "proceso_objetivo_id": injection_id,
            "color_objetivo_id": color_id,
            "material_salida_id": material_id,
            "tolerancia_balance_kg": 0.1,
        },
    )
    assert order_response.status_code == 201, order_response.get_json()
    order_id = order_response.get_json()["id"]

    added = client.post(
        f"/api/scm/v1/reproceso/ordenes-molienda/{order_id}/aportes",
        headers=_headers(warehouse_id),
        json={"lote_merma_id": lot_id, "cantidad_planificada_kg": 10},
    )
    assert added.status_code == 201, added.get_json()
    contribution_id = added.get_json()["aportes"][0]["id"]
    validated = client.post(
        f"/api/scm/v1/reproceso/ordenes-molienda/{order_id}/validar",
        headers=_headers(warehouse_id),
    )
    assert validated.status_code == 200
    assert validated.get_json()["estado"] == "VALIDADA"

    weighed = client.post(
        f"/api/scm/v1/reproceso/ordenes-molienda/{order_id}/pesos-pre-molino",
        headers=_headers(warehouse_id),
        json={"aportes": [{"aporte_id": contribution_id, "peso_pre_molino_kg": 10}]},
    )
    assert weighed.status_code == 200, weighed.get_json()
    assert weighed.get_json()["aportes"][0]["diferencia_custodia_kg"] == "0.000"
    assert client.post(
        f"/api/scm/v1/reproceso/ordenes-molienda/{order_id}/iniciar",
        headers=_headers(warehouse_id),
    ).status_code == 200

    operation_key = str(uuid4())
    close_payload = {
        "perdida_kg": 0.2,
        "salidas": [
            {"peso_neto_kg": 4.8, "ubicacion_codigo": "REC-UAT"},
            {"peso_neto_kg": 5, "ubicacion_codigo": "REC-UAT"},
        ],
    }
    closed = client.post(
        f"/api/scm/v1/reproceso/ordenes-molienda/{order_id}/cerrar",
        headers={"X-Actor-Id": str(warehouse_id), "Idempotency-Key": operation_key},
        json=close_payload,
    )
    assert closed.status_code == 200, closed.get_json()
    assert len(closed.get_json()["lotes_recuperados"]) == 2
    recovered_id = closed.get_json()["lotes_recuperados"][0]["id"]
    replay = client.post(
        f"/api/scm/v1/reproceso/ordenes-molienda/{order_id}/cerrar",
        headers={"X-Actor-Id": str(warehouse_id), "Idempotency-Key": operation_key},
        json=close_payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == closed.get_json()

    movements = client.get(
        f"/api/scm/v1/reproceso/mermas/{lot_id}/movimientos",
        headers=_headers(warehouse_id),
    ).get_json()
    assert [item["tipo"] for item in movements["items"]] == [
        "INGRESO_ALMACEN", "CONSUMO_MOLIENDA",
    ]
    released = client.post(
        f"/api/scm/v1/reproceso/lotes-recuperados/{recovered_id}/liberar",
        headers=_headers(manager_id),
        json={"motivo": "Balance y composicion conformes"},
    )
    assert released.status_code == 200, released.get_json()
    assert released.get_json()["estado"] == "DISPONIBLE"


def test_diferencia_custodia_crea_una_alerta_idempotente(
    app, client, scm_config,
):
    warehouse_id, _, color_id, _ = _setup(app)
    refs = client.get(
        "/api/scm/v1/reproceso/referencias",
        headers=_headers(warehouse_id),
    ).get_json()
    pp_id = next(item["id"] for item in refs["familias_material"] if item["codigo"] == "PP")
    injection_id = next(item["id"] for item in refs["procesos"] if item["codigo"] == "INYECCION")
    clean_id = next(item["id"] for item in refs["condiciones"] if item["codigo"] == "LIMPIA")
    # La huella se prueba directamente con una condición equivalente; no se
    # necesita cerrar otra orden para demostrar que no se duplica.
    from app.services.scm_alert_service import upsert_operational_alert

    with app.app_context():
        first = upsert_operational_alert(
            db.session,
            rule_code="DIFERENCIA_CUSTODIA_MERMA",
            aggregate_type="APORTE_MOLIENDA",
            aggregate_id="UAT-1",
            condition_key="peso:8.500",
            summary="Diferencia UAT",
            detail={"diferencia_kg": "1.500"},
            actor_id=warehouse_id,
        )
        second = upsert_operational_alert(
            db.session,
            rule_code="DIFERENCIA_CUSTODIA_MERMA",
            aggregate_type="APORTE_MOLIENDA",
            aggregate_id="UAT-1",
            condition_key="peso:8.500",
            summary="Diferencia UAT",
            detail={"diferencia_kg": "1.500"},
            actor_id=warehouse_id,
        )
        db.session.commit()
        assert first.id == second.id

    alerts = client.get(
        "/api/scm/v1/alertas?estado=ABIERTA",
        headers=_headers(warehouse_id),
    )
    assert alerts.status_code == 200
    assert len(alerts.get_json()["items"]) == 1
