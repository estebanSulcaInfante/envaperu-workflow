from uuid import uuid4

import pytest

from app.extensions import db
from app.models.producto import Familia, Linea, ProductoTerminado
from app.models.scm_articulos import ScmArticulo, ScmArticuloProducto
from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import ScmCapacidad
from app.models.scm_production_orders import ScmOrdenOperacion
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_article_service import create_wip_article
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_structure_service import (
    approve_structure,
    create_structure,
    send_structure_for_approval,
)
from app.services.scm_route_service import (
    approve_route,
    create_article_route,
    create_route,
    create_work_center,
    list_work_centers,
    publish_route_directly,
    retire_route,
    update_route,
)
from app.services.scm_service_support import ScmServiceError


def _actors():
    ensure_initial_scm_configuration()
    creator = Trabajador.query.filter_by(codigo="TRB-01").one()
    creator.roles.append(
        RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
    )
    approver = Trabajador(
        codigo="TRB-R3-JP",
        nombres="Julia",
        apellidos="Produccion",
        activo=True,
        roles=[
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        ],
    )
    db.session.add(approver)
    db.session.commit()
    return creator, approver


def _product(code, name):
    line = Linea.query.first()
    family = Familia.query.first()
    product = ProductoTerminado(
        cod_sku_pt=code,
        producto=name,
        linea_id=line.id,
        familia_id=family.id,
        um="UN",
    )
    db.session.add(product)
    db.session.commit()
    link = ScmArticuloProducto.query.filter_by(
        producto_terminado_id=code
    ).one()
    return product, db.session.get(ScmArticulo, link.articulo_id)


def _wip(actor, name):
    payload = create_wip_article(
        db.session,
        actor_id=actor.id,
        data={"nombre": name, "requiere_calidad": False},
    )
    return payload["id"]


def _center(actor, suffix, *, operation_type="INYECCION"):
    return create_work_center(
        db.session,
        actor_id=actor.id,
        data={
            "nombre": f"Centro {suffix}",
            "tipo": operation_type,
        },
    )


def _operation(
    key,
    sequence,
    center_id,
    output_id,
    *,
    executor_kind="OP_OT",
    structure_id=None,
    operation_type="INYECCION",
):
    payload = {
        "clave": key,
        "secuencia_visible": sequence,
        "nombre": f"Operacion {key}",
        "tipo": operation_type,
        "executor_kind": executor_kind,
        "centro_trabajo_id": center_id,
        "articulo_salida_id": output_id,
        "permite_concurrente": False,
    }
    if structure_id is not None:
        payload["estructura_revision_id"] = structure_id
    return payload


def _grant_exceptional_assembly_capability(actor):
    capability = ScmCapacidad.query.filter_by(
        codigo="OA_EXCEPCIONAL_CREAR"
    ).one_or_none()
    if capability is None:
        capability = ScmCapacidad(
            codigo="OA_EXCEPCIONAL_CREAR",
            nombre="Crear orden de armado excepcional",
        )
        db.session.add(capability)
        db.session.flush()
    role = actor.roles[0]
    if capability not in role.capacidades:
        role.capacidades.append(capability)
    db.session.commit()


def _approved_wip_route(creator, approver, suffix):
    target_id = _wip(creator, f"WIP objetivo {suffix}")
    component_id = _wip(creator, f"WIP componente {suffix}")
    center = _center(
        creator,
        f"OA-{suffix}",
        operation_type="ENSAMBLE",
    )
    structure = create_structure(
        db.session,
        actor_id=creator.id,
        article_id=target_id,
        data={
            "componentes": [
                {"articulo_id": component_id, "cantidad": "1"},
            ],
        },
    )
    structure = send_structure_for_approval(
        db.session,
        actor_id=creator.id,
        structure_id=structure["id"],
        operation_id=uuid4(),
        data={"version": structure["version"]},
    )
    structure = approve_structure(
        db.session,
        actor_id=approver.id,
        structure_id=structure["id"],
        operation_id=uuid4(),
        data={"version": structure["version"]},
    )
    operation = _operation(
        "ARMAR_WIP",
        10,
        center["id"],
        target_id,
        executor_kind="ORDEN_OPERACION",
        structure_id=structure["id"],
        operation_type="ENSAMBLE",
    )
    operation["permite_concurrente"] = True
    route = create_article_route(
        db.session,
        actor_id=creator.id,
        article_id=target_id,
        data={"operaciones": [operation], "precedencias": []},
    )
    route = approve_route(
        db.session,
        actor_id=approver.id,
        route_id=route["id"],
        operation_id=uuid4(),
        data={"version": route["version"]},
    )
    return target_id, structure, route


def _exceptional_assembly_payload(target_id, structure, route):
    return {
        "origen_demanda": "REPOSICION_WIP",
        "motivo": "Reposicion sintetica para F3",
        "articulo_salida_id": target_id,
        "operacion_ruta_revision_id": route["operaciones"][0]["id"],
        "estructura_revision_id": structure["id"],
        "cantidad_objetivo": "20",
        "versiones": {
            "ruta": route["version"],
            "estructura": structure["version"],
        },
    }


def test_centro_trabajo_genera_codigo_automatico(app):
    with app.app_context():
        creator, _approver = _actors()
        first = _center(creator, "AUTO-A")
        second = _center(creator, "AUTO-B")

        assert first["codigo"].startswith("CT-")
        assert len(first["codigo"]) == 9
        assert second["codigo"].startswith("CT-")
        assert first["codigo"] != second["codigo"]


def test_actor_con_ot_ver_puede_consultar_centros_sin_administrarlos(
    app,
    client,
):
    with app.app_context():
        creator, _approver = _actors()
        center = _center(creator, "TABLERO-OT")
        operator = Trabajador(
            codigo="TRB-R3-OT",
            nombres="Oscar",
            apellidos="Turno",
            activo=True,
            roles=[RolOperativo.query.filter_by(codigo="MAQUINISTA").one()],
        )
        db.session.add(operator)
        db.session.commit()

        listed = list_work_centers(
            db.session,
            actor_id=operator.id,
            active=True,
        )["items"]
        assert center["id"] in {item["id"] for item in listed}
        response = client.get(
            "/api/scm/v1/centros-trabajo",
            query_string={"activo": "true"},
            headers={"X-Actor-Id": str(operator.id)},
        )
        assert response.status_code == 200
        assert center["id"] in {
            item["id"] for item in response.get_json()["items"]
        }

        with pytest.raises(ScmServiceError) as error:
            create_work_center(
                db.session,
                actor_id=operator.id,
                data={"nombre": "No autorizado", "tipo": "INYECCION"},
            )
        assert error.value.code == "CAPABILITY_REQUIRED"


def test_soplado_es_operacion_de_fabricacion_valida(app):
    with app.app_context():
        _creator, leader = _actors()
        product, target = _product(
            "PT-R3-SOPLADO",
            "Alcancia fabricada por soplado",
        )
        center = _center(
            leader,
            "SOPLADO",
            operation_type="SOPLADO",
        )
        route = create_route(
            db.session,
            actor_id=leader.id,
            product_id=product.cod_sku_pt,
            data={
                "operaciones": [
                    _operation(
                        "SOPLAR",
                        10,
                        center["id"],
                        target.id,
                        operation_type="SOPLADO",
                    ),
                ],
                "precedencias": [],
            },
        )

        published = publish_route_directly(
            db.session,
            actor_id=leader.id,
            route_id=route["id"],
            operation_id=uuid4(),
            data={"version": route["version"]},
        )

        assert center["tipo"] == "SOPLADO"
        assert published["estado"] == "APROBADA"
        assert published["operaciones"][0]["tipo"] == "SOPLADO"
        assert published["operaciones"][0]["executor_kind"] == "OP_OT"


def test_jefatura_publica_su_propia_ruta_directamente(app):
    with app.app_context():
        _creator, leader = _actors()
        product, target = _product("PT-R3-DIRECT", "Producto ruta directa")
        center = _center(leader, "DIRECT")
        route = create_route(
            db.session,
            actor_id=leader.id,
            product_id=product.cod_sku_pt,
            data={
                "operaciones": [
                    _operation("OP1", 1, center["id"], target.id),
                ],
                "precedencias": [],
            },
        )

        published = publish_route_directly(
            db.session,
            actor_id=leader.id,
            route_id=route["id"],
            operation_id=uuid4(),
            data={"version": route["version"]},
        )

        assert published["estado"] == "APROBADA"
        assert published["aprobada_por_id"] == leader.id


def test_aprobacion_rechaza_ciclo_aunque_secuencia_visual_parezca_valida(app):
    with app.app_context():
        creator, approver = _actors()
        product, target = _product("PT-R3-CYCLE", "Producto ciclo")
        intermediate_a = _wip(creator, "WIP ruta A")
        intermediate_b = _wip(creator, "WIP ruta B")
        center = _center(creator, "CYCLE")

        route = create_route(
            db.session,
            actor_id=creator.id,
            product_id=product.cod_sku_pt,
            data={
                "operaciones": [
                    _operation(
                        "A", 10, center["id"], intermediate_a
                    ),
                    _operation(
                        "B", 20, center["id"], intermediate_b
                    ),
                    _operation("C", 30, center["id"], target.id),
                ],
                "precedencias": [
                    {"anterior_clave": "A", "siguiente_clave": "B"},
                    {"anterior_clave": "B", "siguiente_clave": "C"},
                    {"anterior_clave": "C", "siguiente_clave": "A"},
                ],
            },
        )

        with pytest.raises(ScmServiceError) as error:
            approve_route(
                db.session,
                actor_id=approver.id,
                route_id=route["id"],
                operation_id=uuid4(),
                data={"version": route["version"]},
            )

        assert error.value.code == "ROUTE_CYCLE"


def test_terminal_debe_ser_unico_y_producir_producto_objetivo(app):
    with app.app_context():
        creator, approver = _actors()
        product, target = _product("PT-R3-TERM", "Producto terminal")
        intermediate = _wip(creator, "WIP terminal incorrecto")
        center = _center(creator, "TERM")

        route = create_route(
            db.session,
            actor_id=creator.id,
            product_id=product.cod_sku_pt,
            data={
                "operaciones": [
                    _operation(
                        "INYECCION",
                        10,
                        center["id"],
                        intermediate,
                    ),
                    _operation(
                        "SALIDA",
                        20,
                        center["id"],
                        intermediate,
                    ),
                ],
                "precedencias": [
                    {
                        "anterior_clave": "INYECCION",
                        "siguiente_clave": "SALIDA",
                    }
                ],
            },
        )

        with pytest.raises(ScmServiceError) as error:
            approve_route(
                db.session,
                actor_id=approver.id,
                route_id=route["id"],
                operation_id=uuid4(),
                data={"version": route["version"]},
            )

        assert error.value.code == "OUTPUT_ARTICLE_INCOMPATIBLE"
        assert error.value.details["target_article_id"] == target.id


def test_executor_kind_es_unico_y_compatible_con_estructura(app):
    with app.app_context():
        creator, _approver = _actors()
        product, target = _product("PT-R3-AUTH", "Producto autoridad")
        center = _center(creator, "AUTH")

        ambiguous = _operation(
            "UNICA",
            10,
            center["id"],
            target.id,
            structure_id=999,
        )
        with pytest.raises(ScmServiceError) as error:
            create_route(
                db.session,
                actor_id=creator.id,
                product_id=product.cod_sku_pt,
                data={
                    "operaciones": [ambiguous],
                    "precedencias": [],
                },
            )

        assert error.value.code == "EXECUTOR_KIND_INCOMPATIBLE"

        missing = _operation(
            "UNICA",
            10,
            center["id"],
            target.id,
        )
        del missing["executor_kind"]
        with pytest.raises(ScmServiceError) as missing_error:
            create_route(
                db.session,
                actor_id=creator.id,
                product_id=product.cod_sku_pt,
                data={
                    "operaciones": [missing],
                    "precedencias": [],
                },
            )
        assert missing_error.value.code == "EXECUTOR_KIND_INCOMPATIBLE"


def test_ruta_aprobada_es_inmutable_y_nueva_revision_retira_anterior(app):
    with app.app_context():
        creator, approver = _actors()
        product, target = _product("PT-R3-OK", "Producto directo")
        center = _center(creator, "OK")
        route = create_route(
            db.session,
            actor_id=creator.id,
            product_id=product.cod_sku_pt,
            data={
                "notas": "Salida directa desde produccion",
                "operaciones": [
                    _operation(
                        "INYECCION_FINAL",
                        10,
                        center["id"],
                        target.id,
                    )
                ],
                "precedencias": [],
            },
        )
        approval_key = uuid4()
        approved = approve_route(
            db.session,
            actor_id=approver.id,
            route_id=route["id"],
            operation_id=approval_key,
            data={"version": route["version"]},
        )
        replay = approve_route(
            db.session,
            actor_id=approver.id,
            route_id=route["id"],
            operation_id=approval_key,
            data={"version": route["version"]},
        )
        assert replay == approved
        assert approved["estado"] == "APROBADA"
        assert approved["content_hash"]
        assert approved["operaciones"][0]["executor_kind"] == "OP_OT"

        with pytest.raises(ScmServiceError) as error:
            update_route(
                db.session,
                actor_id=creator.id,
                route_id=route["id"],
                data={
                    "version": approved["version"],
                    "notas": "No editable",
                    "operaciones": approved["operaciones"],
                    "precedencias": [],
                },
            )
        assert error.value.code == "ROUTE_NOT_EDITABLE"

        replacement = create_route(
            db.session,
            actor_id=creator.id,
            product_id=product.cod_sku_pt,
            data={
                "operaciones": [
                    _operation(
                        "INYECCION_FINAL",
                        10,
                        center["id"],
                        target.id,
                    )
                ],
                "precedencias": [],
            },
        )
        replacement = approve_route(
            db.session,
            actor_id=approver.id,
            route_id=replacement["id"],
            operation_id=uuid4(),
            data={"version": replacement["version"]},
        )
        assert replacement["numero_revision"] == 2

        retired = retire_route(
            db.session,
            actor_id=approver.id,
            route_id=replacement["id"],
            operation_id=uuid4(),
            data={"version": replacement["version"]},
        )
        assert retired["estado"] == "RETIRADA"


def test_orden_operacion_usa_bom_aprobada_compatible_sin_copiar_cantidades(
    app,
):
    with app.app_context():
        creator, approver = _actors()
        product, target = _product(
            "PT-R3-BOM",
            "Producto armado",
        )
        component_id = _wip(creator, "WIP componente de armado")
        center = _center(creator, "BOM")

        structure = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=target.id,
            data={
                "componentes": [
                    {"articulo_id": component_id, "cantidad": "2"}
                ]
            },
        )
        structure = send_structure_for_approval(
            db.session,
            actor_id=creator.id,
            structure_id=structure["id"],
            operation_id=uuid4(),
            data={"version": structure["version"]},
        )
        structure = approve_structure(
            db.session,
            actor_id=approver.id,
            structure_id=structure["id"],
            operation_id=uuid4(),
            data={"version": structure["version"]},
        )
        assembly_operation = _operation(
            "ENSAMBLAR",
            10,
            center["id"],
            target.id,
            executor_kind="ORDEN_OPERACION",
            structure_id=structure["id"],
        )
        assembly_operation["tipo"] = "ENSAMBLE"

        route = create_route(
            db.session,
            actor_id=creator.id,
            product_id=product.cod_sku_pt,
            data={
                "operaciones": [assembly_operation],
                "precedencias": [],
            },
        )
        approved = approve_route(
            db.session,
            actor_id=approver.id,
            route_id=route["id"],
            operation_id=uuid4(),
            data={"version": route["version"]},
        )

        operation = approved["operaciones"][0]
        assert operation["estructura_revision_id"] == structure["id"]
        assert "cantidad" not in operation
        assert "componentes" not in operation


def test_api_expone_flujo_ruta_y_aprobacion_idempotente(app, client):
    with app.app_context():
        creator, approver = _actors()
        product, target = _product("PT-R3-API", "Producto API")
        center = _center(creator, "API")
        creator_id = creator.id
        approver_id = approver.id
        product_id = product.cod_sku_pt
        target_id = target.id
        center_id = center["id"]

    response = client.post(
        f"/api/scm/v1/productos/{product_id}/rutas",
        headers={"X-Actor-Id": str(creator_id)},
        json={
            "operaciones": [
                _operation(
                    "INYECCION_FINAL",
                    10,
                    center_id,
                    target_id,
                )
            ],
            "precedencias": [],
        },
    )
    assert response.status_code == 201
    route = response.get_json()

    missing_key = client.post(
        f"/api/scm/v1/rutas/{route['id']}/aprobar",
        headers={"X-Actor-Id": str(approver_id)},
        json={"version": route["version"]},
    )
    assert missing_key.status_code == 400
    assert missing_key.get_json()["error"]["code"] == (
        "IDEMPOTENCY_KEY_REQUIRED"
    )

    approved_response = client.post(
        f"/api/scm/v1/rutas/{route['id']}/aprobar",
        headers={
            "X-Actor-Id": str(approver_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={"version": route["version"]},
    )
    assert approved_response.status_code == 200
    assert approved_response.get_json()["estado"] == "APROBADA"

    listed = client.get(
        f"/api/scm/v1/productos/{product_id}/rutas",
        headers={"X-Actor-Id": str(creator_id)},
    )
    assert listed.status_code == 200
    assert listed.get_json()["items"][0]["articulo_objetivo_id"] == target_id


def test_create_and_approve_route_with_wip_target(app, client):
    with app.app_context():
        creator, approver = _actors()
        target_id = _wip(creator, "Tapa armada WIP")
        component_id = _wip(creator, "Componente de tapa WIP")
        center = _center(
            creator,
            "WIP-API",
            operation_type="ENSAMBLE",
        )
        structure = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=target_id,
            data={
                "componentes": [
                    {"articulo_id": component_id, "cantidad": "1"},
                ],
            },
        )
        structure = send_structure_for_approval(
            db.session,
            actor_id=creator.id,
            structure_id=structure["id"],
            operation_id=uuid4(),
            data={"version": structure["version"]},
        )
        structure = approve_structure(
            db.session,
            actor_id=approver.id,
            structure_id=structure["id"],
            operation_id=uuid4(),
            data={"version": structure["version"]},
        )
        creator_id = creator.id
        approver_id = approver.id
        center_id = center["id"]
        structure_id = structure["id"]

    operation = _operation(
        "ARMAR_WIP",
        10,
        center_id,
        target_id,
        executor_kind="ORDEN_OPERACION",
        structure_id=structure_id,
        operation_type="ENSAMBLE",
    )
    operation["permite_concurrente"] = True
    created = client.post(
        f"/api/scm/v1/articulos/{target_id}/rutas",
        headers={"X-Actor-Id": str(creator_id)},
        json={"operaciones": [operation], "precedencias": []},
    )

    assert created.status_code == 201
    route = created.get_json()
    assert route["articulo_objetivo"]["clase"] == "SUBENSAMBLE_WIP"
    assert route["producto_id"] is None

    approved = client.post(
        f"/api/scm/v1/rutas/{route['id']}/aprobar",
        headers={
            "X-Actor-Id": str(approver_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={"version": route["version"]},
    )
    assert approved.status_code == 200
    assert approved.get_json()["estado"] == "APROBADA"

    listed = client.get(
        f"/api/scm/v1/articulos/{target_id}/rutas",
        headers={"X-Actor-Id": str(creator_id)},
    )
    assert listed.status_code == 200
    assert listed.get_json()["items"][0]["articulo_objetivo_id"] == target_id


def test_api_crea_oa_excepcional_wip_idempotente_y_gobernada(app, client):
    with app.app_context():
        creator, approver = _actors()
        target_id, structure, route = _approved_wip_route(
            creator,
            approver,
            "CREATE",
        )
        _grant_exceptional_assembly_capability(approver)
        payload = _exceptional_assembly_payload(target_id, structure, route)
        unauthorized_id = creator.id
        authorized_id = approver.id

    forbidden = client.post(
        "/api/scm/v1/ordenes-armado/excepcionales",
        headers={
            "X-Actor-Id": str(unauthorized_id),
            "Idempotency-Key": str(uuid4()),
        },
        json=payload,
    )
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"]["code"] == (
        "EXCEPTIONAL_ASSEMBLY_AUTHORIZATION_REQUIRED"
    )

    idempotency_key = str(uuid4())
    created = client.post(
        "/api/scm/v1/ordenes-armado/excepcionales",
        headers={
            "X-Actor-Id": str(authorized_id),
            "Idempotency-Key": idempotency_key,
        },
        json=payload,
    )
    assert created.status_code == 201
    order = created.get_json()
    assert order["tipo"] == "ENSAMBLE"
    assert order["estado"] == "BORRADOR"
    assert order["origen_demanda"] == "REPOSICION_WIP"
    assert order["motivo"] == payload["motivo"]
    assert order["plan_produccion_id"] is None
    assert order["salida"]["articulo_scm_id"] == target_id
    assert order["salida"]["clase"] == "SUBENSAMBLE_WIP"
    assert order["salida"]["cantidad_objetivo"] == "20"
    assert order["operacion"]["id"] == payload[
        "operacion_ruta_revision_id"
    ]
    assert order["operacion"]["estructura_revision_id"] == structure["id"]

    replay = client.post(
        "/api/scm/v1/ordenes-armado/excepcionales",
        headers={
            "X-Actor-Id": str(authorized_id),
            "Idempotency-Key": idempotency_key,
        },
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == order

    conflict = client.post(
        "/api/scm/v1/ordenes-armado/excepcionales",
        headers={
            "X-Actor-Id": str(authorized_id),
            "Idempotency-Key": idempotency_key,
        },
        json={**payload, "motivo": "Otro motivo con la misma clave"},
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    with app.app_context():
        assert ScmOrdenOperacion.query.filter_by(
            tipo="ENSAMBLE",
            origen_demanda="REPOSICION_WIP",
        ).count() == 1
        assert ScmEvento.query.filter_by(
            aggregate_type="ORDEN_ARMADO",
            tipo="EXCEPTIONAL_ASSEMBLY_ORDER_CREATED",
        ).count() == 1


def test_api_rechaza_oa_excepcional_con_ruta_o_bom_incompatible(app, client):
    with app.app_context():
        creator, approver = _actors()
        target_id, structure, route = _approved_wip_route(
            creator,
            approver,
            "MISMATCH",
        )
        other_target_id = _wip(creator, "Otro WIP no compatible")
        _grant_exceptional_assembly_capability(approver)
        payload = _exceptional_assembly_payload(target_id, structure, route)
        actor_id = approver.id

    wrong_output = {
        **payload,
        "articulo_salida_id": other_target_id,
    }
    output_response = client.post(
        "/api/scm/v1/ordenes-armado/excepcionales",
        headers={
            "X-Actor-Id": str(actor_id),
            "Idempotency-Key": str(uuid4()),
        },
        json=wrong_output,
    )
    assert output_response.status_code == 422
    assert output_response.get_json()["error"]["code"] == (
        "ASSEMBLY_ENGINEERING_NOT_READY"
    )

    wrong_structure = {
        **payload,
        "estructura_revision_id": structure["id"] + 999999,
    }
    structure_response = client.post(
        "/api/scm/v1/ordenes-armado/excepcionales",
        headers={
            "X-Actor-Id": str(actor_id),
            "Idempotency-Key": str(uuid4()),
        },
        json=wrong_structure,
    )
    assert structure_response.status_code == 422
    assert structure_response.get_json()["error"]["code"] == (
        "ASSEMBLY_ENGINEERING_NOT_READY"
    )

    with app.app_context():
        assert ScmOrdenOperacion.query.filter_by(
            tipo="ENSAMBLE",
            origen_demanda="REPOSICION_WIP",
        ).count() == 0
