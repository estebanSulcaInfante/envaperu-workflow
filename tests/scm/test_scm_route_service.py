from uuid import uuid4

import pytest

from app.extensions import db
from app.models.producto import Familia, Linea, ProductoTerminado
from app.models.scm_articulos import ScmArticulo, ScmArticuloProducto
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
    create_route,
    create_work_center,
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


def _center(actor, suffix):
    return create_work_center(
        db.session,
        actor_id=actor.id,
        data={
            "nombre": f"Centro {suffix}",
            "tipo": "INYECCION",
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
):
    payload = {
        "clave": key,
        "secuencia_visible": sequence,
        "nombre": f"Operacion {key}",
        "tipo": "INYECCION",
        "executor_kind": executor_kind,
        "centro_trabajo_id": center_id,
        "articulo_salida_id": output_id,
        "permite_concurrente": False,
    }
    if structure_id is not None:
        payload["estructura_revision_id"] = structure_id
    return payload


def test_centro_trabajo_genera_codigo_automatico(app):
    with app.app_context():
        creator, _approver = _actors()
        first = _center(creator, "AUTO-A")
        second = _center(creator, "AUTO-B")

        assert first["codigo"].startswith("CT-")
        assert len(first["codigo"]) == 9
        assert second["codigo"].startswith("CT-")
        assert first["codigo"] != second["codigo"]


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
            "Producto ensamblado",
        )
        component_id = _wip(creator, "WIP componente de ensamble")
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
