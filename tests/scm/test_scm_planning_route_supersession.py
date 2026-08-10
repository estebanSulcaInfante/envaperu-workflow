from datetime import date
from uuid import uuid4

from app import db
from app.models.producto import ProductoTerminado
from app.models.scm_articulos import ScmArticulo, ScmArticuloProducto
from app.models.scm_estructuras import (
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_auditoria import ScmEvento
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
    ScmPlanProduccion,
)
from app.models.scm_rutas import (
    ScmCentroTrabajo,
    ScmOperacionPrecedencia,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_production_order_service import (
    calculate_production_plan,
    confirm_production_plan,
    refresh_production_order_routes,
)


def _operation(
    *,
    route,
    center,
    key,
    sequence,
    output,
    executor_kind,
    structure=None,
):
    return ScmOperacionRuta(
        ruta=route,
        clave=key,
        secuencia_visible=sequence,
        nombre=(
            "Soplar pieza intermedia"
            if executor_kind == "OP_OT"
            else "Armar producto terminado"
        ),
        tipo="SOPLADO" if executor_kind == "OP_OT" else "ENSAMBLE",
        executor_kind=executor_kind,
        centro_trabajo_id=center.id,
        articulo_salida_id=output.id,
        estructura_revision_id=structure.id if structure else None,
    )


def _add_complete_route(*, route, center, intermediate, product, structure):
    fabrication = _operation(
        route=route,
        center=center,
        key="SOPLAR",
        sequence=1,
        output=intermediate,
        executor_kind="OP_OT",
    )
    assembly = _operation(
        route=route,
        center=center,
        key="ARMAR",
        sequence=2,
        output=product,
        executor_kind="ORDEN_OPERACION",
        structure=structure,
    )
    db.session.add_all([fabrication, assembly])
    db.session.flush()
    db.session.add(ScmOperacionPrecedencia(
        ruta_id=route.id,
        operacion_anterior_id=fabrication.id,
        operacion_siguiente_id=assembly.id,
    ))
    return fabrication, assembly


def _add_terminal_only_route(*, route, center, product, structure):
    assembly = _operation(
        route=route,
        center=center,
        key="ARMAR",
        sequence=1,
        output=product,
        executor_kind="ORDEN_OPERACION",
        structure=structure,
    )
    db.session.add(assembly)
    db.session.flush()
    return assembly


def _scenario(*, active_route_is_complete):
    planner = Trabajador.query.filter_by(codigo="TRB-01").one()
    planner.roles.extend([
        RolOperativo.query.filter_by(codigo="PLANIFICACION").one(),
        RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one(),
    ])
    product = ProductoTerminado(
        cod_sku_pt=(
            "PT-ROUTE-SUPERSESSION"
            if active_route_is_complete
            else "PT-ROUTE-REAL-BLOCKER"
        ),
        producto="Producto con ruta reemplazada",
        linea_id=1,
        familia_id=1,
    )
    intermediate = ScmArticulo(
        codigo=(
            "PC-ROUTE-SUPERSESSION"
            if active_route_is_complete
            else "PC-ROUTE-REAL-BLOCKER"
        ),
        nombre="Pieza color requerida por el PT",
        clase="PIEZA_COLOR",
    )
    db.session.add_all([product, intermediate])
    db.session.flush()
    product_article = ScmArticuloProducto.query.filter_by(
        producto_terminado_id=product.cod_sku_pt,
    ).one().articulo
    structure = ScmEstructuraRevision(
        articulo_resultado_id=product_article.id,
        numero_revision=1,
        estado="APROBADA",
        content_hash="a" * 64,
        creada_por_id=planner.id,
        aprobada_por_id=planner.id,
        componentes=[ScmEstructuraComponente(
            secuencia=1,
            articulo_componente_id=intermediate.id,
            cantidad=2,
            unidad="UN",
        )],
    )
    center = ScmCentroTrabajo(
        codigo=(
            "CT-ROUTE-SUPERSESSION"
            if active_route_is_complete
            else "CT-ROUTE-REAL-BLOCKER"
        ),
        nombre="Soplado y armado de prueba",
        tipo="SOPLADO",
    )
    retired = ScmRutaRevision(
        articulo_objetivo_id=product_article.id,
        numero_revision=1,
        estado="RETIRADA",
        content_hash="b" * 64,
        creada_por_id=planner.id,
        aprobada_por_id=planner.id,
        retirada_por_id=planner.id,
    )
    active = ScmRutaRevision(
        articulo_objetivo_id=product_article.id,
        numero_revision=2,
        estado="APROBADA",
        content_hash="c" * 64,
        creada_por_id=planner.id,
        aprobada_por_id=planner.id,
    )
    db.session.add_all([structure, center, retired, active])
    db.session.flush()
    if active_route_is_complete:
        _add_terminal_only_route(
            route=retired,
            center=center,
            product=product_article,
            structure=structure,
        )
        active_operations = _add_complete_route(
            route=active,
            center=center,
            intermediate=intermediate,
            product=product_article,
            structure=structure,
        )
    else:
        _add_complete_route(
            route=retired,
            center=center,
            intermediate=intermediate,
            product=product_article,
            structure=structure,
        )
        active_operations = (_add_terminal_only_route(
            route=active,
            center=center,
            product=product_article,
            structure=structure,
        ),)
    order = ScmOrdenProduccion(
        codigo=(
            "OP-ROUTE-SUPERSESSION"
            if active_route_is_complete
            else "OP-ROUTE-REAL-BLOCKER"
        ),
        origen="PLANIFICACION",
        fecha_necesidad=date(2026, 8, 18),
        estado="APROBADA",
        created_by_id=planner.id,
        approved_by_id=planner.id,
    )
    line = ScmOrdenProduccionLinea(
        producto_terminado_id=product.cod_sku_pt,
        cantidad_solicitada=10,
        estructura_revision_id=structure.id,
        estructura_hash=structure.content_hash,
        # La OP fue aprobada cuando revision 1 aun era la vigente.
        ruta_revision_id=retired.id,
        ruta_hash=retired.content_hash,
    )
    order.lineas.append(line)
    db.session.add(order)
    db.session.commit()
    return {
        "planner": planner,
        "order": order,
        "line": line,
        "intermediate": intermediate,
        "retired": retired,
        "active": active,
        "active_operations": active_operations,
    }


def test_recalculation_uses_active_multistep_route_and_is_idempotent(
    app,
    scm_config,
):
    with app.app_context():
        data = _scenario(active_route_is_complete=True)
        # Recalcular por si solo es reproducible: conserva la revision 1 que
        # se congelo al aprobar la OP, aunque despues haya sido retirada.
        first = calculate_production_plan(
            db.session,
            actor_id=data["planner"].id,
            operation_id=uuid4(),
            order_id=data["order"].id,
            expected_resource_version=data["order"].version,
        )
        frozen_proposal = first["plan"]["propuesta"]
        assert frozen_proposal["inputs"]["lineas"][0][
            "ruta_revision_id"
        ] == data["retired"].id
        assert frozen_proposal["bloqueos"][0]["articulo_codigo"] == (
            data["intermediate"].codigo
        )

        refresh_operation_id = uuid4()
        refreshed = refresh_production_order_routes(
            db.session,
            actor_id=data["planner"].id,
            operation_id=refresh_operation_id,
            order_id=data["order"].id,
            expected_resource_version=data["order"].version,
        )
        refresh_replay = refresh_production_order_routes(
            db.session,
            actor_id=data["planner"].id,
            operation_id=refresh_operation_id,
            order_id=data["order"].id,
            expected_resource_version=data["order"].version - 1,
        )
        assert refresh_replay == refreshed
        assert refreshed["orden"]["version"] == 2
        assert refreshed["cambios"] == [{
            "linea_id": str(data["line"].id),
            "producto_terminado_id": data["line"].producto_terminado_id,
            "ruta_anterior": {
                "id": data["retired"].id,
                "codigo": (
                    f"{data['retired'].articulo_objetivo.codigo}-R1"
                ),
                "revision": 1,
                "estado": "RETIRADA",
                "content_hash": data["retired"].content_hash,
            },
            "ruta_nueva": {
                "id": data["active"].id,
                "codigo": (
                    f"{data['active'].articulo_objetivo.codigo}-R2"
                ),
                "revision": 2,
                "estado": "APROBADA",
                "content_hash": data["active"].content_hash,
            },
        }]
        assert refreshed["planes_superados"] == [first["plan"]["id"]]
        audit_event = ScmEvento.query.filter_by(
            aggregate_type="ORDEN_PRODUCCION",
            aggregate_id=str(data["order"].id),
            tipo="PRODUCTION_ORDER_ROUTES_REFRESHED",
        ).one()
        assert audit_event.actor_id == data["planner"].id
        assert audit_event.before_json["rutas"][0]["ruta"]["revision"] == 1
        assert audit_event.after_json["cambios"][0]["ruta_nueva"][
            "revision"
        ] == 2
        db.session.refresh(data["line"])
        assert data["line"].ruta_revision_id == data["active"].id

        second_operation_id = uuid4()
        second = calculate_production_plan(
            db.session,
            actor_id=data["planner"].id,
            operation_id=second_operation_id,
            order_id=data["order"].id,
            expected_resource_version=refreshed["orden"]["version"],
        )
        proposal = second["plan"]["propuesta"]
        assert proposal["bloqueos"] == []
        assert [
            (item["tipo"], item["articulo"]["codigo"], item["cantidad_objetivo"])
            for item in proposal["documentos"]
        ] == [
            ("FABRICACION", data["intermediate"].codigo, "20"),
            ("ENSAMBLE", data["line"].producto_terminado_id, "10.000"),
        ]
        assert {
            item["ruta_revision_id"] for item in proposal["documentos"]
        } == {data["active"].id}
        assert proposal["inputs"]["lineas"][0]["ruta_revision_id"] == (
            data["active"].id
        )
        assert all(
            item["operacion_ruta_id"]
            in {operation.id for operation in data["active_operations"]}
            for item in proposal["documentos"]
        )
        assert proposal["asignaciones_demanda"] == [{
            "linea_id": str(data["line"].id),
            "propuesta_clave": (
                f"R{data['active'].id}-O{data['active_operations'][1].id}"
            ),
            "cantidad": "10.000",
        }]
        replay = calculate_production_plan(
            db.session,
            actor_id=data["planner"].id,
            operation_id=second_operation_id,
            order_id=data["order"].id,
            expected_resource_version=refreshed["orden"]["version"],
        )
        assert second["plan"]["revision"] == 2
        assert replay["plan"]["id"] == second["plan"]["id"]
        assert ScmPlanProduccion.query.count() == 2

        confirmation_id = uuid4()
        confirmed = confirm_production_plan(
            db.session,
            actor_id=data["planner"].id,
            operation_id=confirmation_id,
            order_id=data["order"].id,
            data={
                "version": refreshed["orden"]["version"],
                "plan_id": second["plan"]["id"],
                "content_hash": second["plan"]["content_hash"],
            },
        )
        confirmed_replay = confirm_production_plan(
            db.session,
            actor_id=data["planner"].id,
            operation_id=confirmation_id,
            order_id=data["order"].id,
            data={
                "version": refreshed["orden"]["version"],
                "plan_id": second["plan"]["id"],
                "content_hash": second["plan"]["content_hash"],
            },
        )
        assert confirmed_replay == confirmed
        assert ScmOrdenOperacion.query.count() == 2
        assert ScmOrdenFabricacion.query.count() == 1
        assert ScmAsignacionDemandaSuministro.query.count() == 1
        assert format(
            ScmAsignacionDemandaSuministro.query.one().cantidad_planificada,
            "f",
        ) == "10.000"
        assert sorted(
            format(output.cantidad_objetivo, "f")
            for order in ScmOrdenOperacion.query.all()
            for output in order.salidas
        ) == ["10.000", "20.000"]
        assert {
            item.operacion_ruta_revision_id
            for item in ScmOrdenOperacion.query.all()
        } == {operation.id for operation in data["active_operations"]}


def test_active_route_with_real_missing_output_still_blocks(app, scm_config):
    with app.app_context():
        data = _scenario(active_route_is_complete=False)

        frozen = calculate_production_plan(
            db.session,
            actor_id=data["planner"].id,
            operation_id=uuid4(),
            order_id=data["order"].id,
            expected_resource_version=data["order"].version,
        )
        # La ruta historica completa permanece reproducible hasta que el
        # planificador acepta explicitamente la revision nueva.
        assert frozen["plan"]["propuesta"]["bloqueos"] == []
        assert {
            item["ruta_revision_id"]
            for item in frozen["plan"]["propuesta"]["documentos"]
        } == {data["retired"].id}

        refreshed = refresh_production_order_routes(
            db.session,
            actor_id=data["planner"].id,
            operation_id=uuid4(),
            order_id=data["order"].id,
            expected_resource_version=data["order"].version,
        )
        calculated = calculate_production_plan(
            db.session,
            actor_id=data["planner"].id,
            operation_id=uuid4(),
            order_id=data["order"].id,
            expected_resource_version=refreshed["orden"]["version"],
        )

        proposal = calculated["plan"]["propuesta"]
        assert proposal["bloqueos"] == [{
            "codigo": "ROUTE_OUTPUT_MISSING",
            "linea_id": str(data["line"].id),
            "producto_terminado_id": data["line"].producto_terminado_id,
            "ruta_revision_id": data["active"].id,
            "articulo_id": data["intermediate"].id,
            "articulo_codigo": data["intermediate"].codigo,
            "articulo": {
                "codigo": data["intermediate"].codigo,
                "nombre": data["intermediate"].nombre,
                "clase": data["intermediate"].clase,
            },
            "cantidad": "20",
            "motivo_codigo": "SIN_OPERACION_DE_RUTA",
            "motivo": (
                "La BOM requiere este articulo, pero la ruta aprobada no "
                "incluye una operacion cuya salida sea este articulo."
            ),
        }]
        assert {
            item["ruta_revision_id"] for item in proposal["documentos"]
        } == {data["active"].id}
        retired_operation_ids = {
            item.id for item in data["retired"].operaciones
        }
        assert all(
            item["operacion_ruta_id"] not in retired_operation_ids
            for item in proposal["documentos"]
        )
        assert ScmOrdenOperacion.query.count() == 0


def test_refresh_routes_api_exposes_governed_idempotent_command(
    app,
    client,
    scm_config,
):
    with app.app_context():
        data = _scenario(active_route_is_complete=True)
        actor_id = data["planner"].id
        order_id = data["order"].id
        version = data["order"].version
        operation_id = uuid4()

        response = client.post(
            f"/api/scm/v1/ordenes-produccion/{order_id}/actualizar-rutas",
            headers={
                "X-Actor-Id": str(actor_id),
                "Idempotency-Key": str(operation_id),
            },
            json={"version": version},
        )
        replay = client.post(
            f"/api/scm/v1/ordenes-produccion/{order_id}/actualizar-rutas",
            headers={
                "X-Actor-Id": str(actor_id),
                "Idempotency-Key": str(operation_id),
            },
            json={"version": version},
        )

        assert response.status_code == 200
        assert replay.status_code == 200
        assert replay.get_json() == response.get_json()
        assert response.get_json()["orden"]["version"] == version + 1
        assert response.get_json()["cambios"][0]["ruta_anterior"][
            "revision"
        ] == 1
        assert response.get_json()["cambios"][0]["ruta_nueva"][
            "revision"
        ] == 2
