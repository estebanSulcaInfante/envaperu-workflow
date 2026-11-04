from types import SimpleNamespace

import pytest
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm.attributes import NO_VALUE

from app.services.scm_engineering_reads import (
    OUTPUT_ROUTE_OPERATIONS_ENDPOINT,
    _page_cursor_encode,
    list_output_route_operations,
)
from app.services.scm_process_resolution import route_operation_dto
from app.services import scm_fabrication_order_service as fabrication_service
from app.services.scm_service_support import ScmServiceError


def test_output_route_reader_requires_route_read_capability(monkeypatch):
    seen = {}

    def load_actor(_session, _actor_id, *, capability):
        seen["capability"] = capability
        raise ScmServiceError(
            "ACTOR_CAPABILITY_REQUIRED",
            "capability required",
            status_code=403,
        )

    monkeypatch.setattr(
        "app.services.scm_engineering_reads.load_actor",
        load_actor,
    )

    with pytest.raises(ScmServiceError) as error:
        list_output_route_operations(
            SimpleNamespace(),
            actor_id=7,
            output_article_id=42,
        )

    assert seen["capability"] == "RUTA_VER"
    assert error.value.status_code == 403


def test_route_operation_dto_does_not_trigger_lazy_owner_load():
    class RouteWithoutLoadedOwner:
        id = 91
        numero_revision = 3
        content_hash = "a" * 64

        @property
        def articulo_objetivo(self):
            raise AssertionError("owner route target must already be eager-loaded")

    output = SimpleNamespace(
        id=17,
        codigo="PC-017",
        nombre="Pieza 017",
        clase="PIEZA_COLOR",
    )
    operation = SimpleNamespace(
        id=33,
        clave="INYECTAR_PC",
        nombre="Inyectar pieza",
        tipo="INYECCION",
        executor_kind="OP_OT",
        ruta=RouteWithoutLoadedOwner(),
        articulo_salida=output,
        articulo_salida_id=output.id,
    )

    payload = route_operation_dto(operation)

    assert payload["ruta_revision_id"] == 91
    assert "owner_route_target_article" not in payload


def test_output_route_reader_filters_exact_output_and_separates_owner(
    app,
    client,
    monkeypatch,
):
    from app.extensions import db
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_catalogos import ScmCapacidad
    from app.models.scm_rutas import (
        ScmCentroTrabajo,
        ScmOperacionRuta,
        ScmRutaRevision,
    )
    from app.models.trabajador import Trabajador
    from app.services.scm_service_support import load_actor as authorize_actor

    monkeypatch.setattr(
        "app.services.scm_engineering_reads.load_actor",
        lambda session, actor_id, *, capability: Trabajador.query.get(actor_id),
    )

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        capability = ScmCapacidad.query.filter_by(codigo="RUTA_VER").one_or_none()
        if capability is None:
            capability = ScmCapacidad(
                codigo="RUTA_VER",
                nombre="Leer rutas",
            )
            db.session.add(capability)
            db.session.flush()
        if capability not in actor.roles[0].capacidades:
            actor.roles[0].capacidades.append(capability)
        output = ScmArticulo(
            codigo="PC-OUTPUT-01",
            nombre="Pieza azul de salida",
            clase="PIEZA_COLOR",
            unidad_base="UN",
            unidad_inventario="KG",
            activo=True,
        )
        owner = ScmArticulo(
            codigo="WIP-OWNER-01",
            nombre="WIP propietario ruta",
            clase="SUBENSAMBLE_WIP",
            unidad_base="UN",
            unidad_inventario="KG",
            activo=True,
        )
        wrong_output = ScmArticulo(
            codigo="PC-OTHER-01",
            nombre="Otra salida",
            clase="PIEZA_COLOR",
            unidad_base="UN",
            unidad_inventario="KG",
            activo=True,
        )
        pt_owner = ScmArticulo(
            codigo="PT-OWNER-01",
            nombre="PT propietario ruta",
            clase="PRODUCTO_TERMINADO",
            unidad_base="UN",
            unidad_inventario="UN",
            activo=True,
        )
        wip_output = ScmArticulo(
            codigo="WIP-OUTPUT-01",
            nombre="Salida WIP no elegible para OF",
            clase="SUBENSAMBLE_WIP",
            unidad_base="UN",
            unidad_inventario="KG",
            activo=True,
        )
        pt_output = ScmArticulo(
            codigo="PT-OUTPUT-01",
            nombre="Salida PT no elegible para OF",
            clase="PRODUCTO_TERMINADO",
            unidad_base="UN",
            unidad_inventario="UN",
            activo=True,
        )
        inactive_owner = ScmArticulo(
            codigo="WIP-INACTIVE-01",
            nombre="Owner inactivo",
            clase="SUBENSAMBLE_WIP",
            unidad_base="UN",
            unidad_inventario="KG",
            activo=False,
        )
        center = ScmCentroTrabajo(
            codigo="CT-READER-01",
            nombre="Inyeccion lector",
            tipo="INYECCION",
            activo=True,
        )
        db.session.add_all([
            output, owner, wrong_output, pt_owner, wip_output, pt_output,
            inactive_owner, center,
        ])
        db.session.flush()
        route = ScmRutaRevision(
            articulo_objetivo_id=owner.id,
            numero_revision=1,
            estado="APROBADA",
            content_hash="a" * 64,
            creada_por_id=actor.id,
        )
        db.session.add(route)
        db.session.flush()
        db.session.add_all([
            ScmOperacionRuta(
                ruta_id=route.id,
                clave="INYECTAR_PC",
                secuencia_visible=10,
                nombre="Inyectar pieza azul",
                tipo="INYECCION",
                executor_kind="OP_OT",
                centro_trabajo_id=center.id,
                articulo_salida_id=output.id,
            ),
            ScmOperacionRuta(
                ruta_id=route.id,
                clave="OTRA_SALIDA",
                secuencia_visible=20,
                nombre="No debe salir",
                tipo="INYECCION",
                executor_kind="OP_OT",
                centro_trabajo_id=center.id,
                articulo_salida_id=wrong_output.id,
            ),
            ScmOperacionRuta(
                ruta_id=route.id,
                clave="WIP_NO_OF",
                secuencia_visible=30,
                nombre="Salida WIP no elegible",
                tipo="INYECCION",
                executor_kind="OP_OT",
                centro_trabajo_id=center.id,
                articulo_salida_id=wip_output.id,
            ),
            ScmOperacionRuta(
                ruta_id=route.id,
                clave="OA_NO_OF",
                secuencia_visible=40,
                nombre="Operacion de armado",
                tipo="PREARMADO",
                executor_kind="OP_OT",
                centro_trabajo_id=center.id,
                articulo_salida_id=output.id,
            ),
        ])
        inactive_route = ScmRutaRevision(
            articulo_objetivo_id=inactive_owner.id,
            numero_revision=1,
            estado="APROBADA",
            content_hash="c" * 64,
            creada_por_id=actor.id,
        )
        db.session.add(inactive_route)
        db.session.flush()
        db.session.add(ScmOperacionRuta(
            ruta_id=inactive_route.id,
            clave="INACTIVE_OWNER",
            secuencia_visible=10,
            nombre="Owner inactivo",
            tipo="INYECCION",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=output.id,
        ))
        pt_route = ScmRutaRevision(
            articulo_objetivo_id=pt_owner.id,
            numero_revision=1,
            estado="APROBADA",
            content_hash="b" * 64,
            creada_por_id=actor.id,
        )
        db.session.add(pt_route)
        db.session.flush()
        db.session.add(ScmOperacionRuta(
            ruta_id=pt_route.id,
            clave="PT_OWNER_ROUTE",
            secuencia_visible=10,
            nombre="Ruta propietaria PT",
            tipo="INYECCION",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=output.id,
        ))
        db.session.commit()

        statements = []
        monkeypatch.setattr(
            "app.services.scm_engineering_reads.load_actor",
            authorize_actor,
        )
        db.session.expire_all()

        def record_statement(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", record_statement)
        try:
            result = list_output_route_operations(
                db.session,
                actor_id=actor.id,
                output_article_id=output.id,
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", record_statement)

        assert result["total"] == 2
        assert len(result["items"]) == 2
        assert {item["owner_route_target_article"]["clase"] for item in result["items"]} == {
            "SUBENSAMBLE_WIP", "PRODUCTO_TERMINADO",
        }
        assert len(statements) == 5
        item = next(item for item in result["items"] if item["owner_route_target_article"]["id"] == owner.id)
        assert item["operation_id"] == item["operation"]["id"]
        assert item["route_revision_id"] == route.id
        assert item["output_article"]["id"] == output.id
        assert item["owner_route_target_article"]["id"] == owner.id
        assert item["route_content_hash"] == "a" * 64
        first_page = list_output_route_operations(
            db.session,
            actor_id=actor.id,
            output_article_id=output.id,
            limit=1,
        )
        assert first_page["has_more"] is True
        second_page = list_output_route_operations(
            db.session,
            actor_id=actor.id,
            output_article_id=output.id,
            limit=1,
            cursor=first_page["next_cursor"],
        )
        assert {
            first_page["items"][0]["operation_id"],
            second_page["items"][0]["operation_id"],
        } == {item["operation_id"] for item in result["items"]}
        for cursor_kwargs in (
            {"actor_id": actor.id + 1},
            {"capability": "ARTICULO_VER"},
            {"filters": {"output_article_id": output.id, "q": "otro", "limit": 1}},
            {"filters": {"output_article_id": output.id, "q": None, "limit": 2}},
            {"order": "operation_id_desc"},
        ):
            cursor_payload = {
                "endpoint": OUTPUT_ROUTE_OPERATIONS_ENDPOINT,
                "actor_id": actor.id,
                "capability": "RUTA_VER",
                "filters": {"output_article_id": output.id, "q": None, "limit": 1},
                "order": "route_id_desc_operation_id_desc",
                "last": [first_page["items"][0]["route_revision_id"], first_page["items"][0]["operation_id"]],
                "code": "INVALID_OUTPUT_ROUTE_PAGE",
            }
            cursor_payload.update(cursor_kwargs)
            with pytest.raises(ScmServiceError) as cursor_error:
                list_output_route_operations(
                    db.session,
                    actor_id=actor.id,
                    output_article_id=output.id,
                    limit=1,
                    cursor=_page_cursor_encode(**cursor_payload),
                )
            assert cursor_error.value.status_code == 400
        with pytest.raises(ScmServiceError) as limit_error:
            list_output_route_operations(
                db.session,
                actor_id=actor.id,
                output_article_id=output.id,
                limit=0,
            )
        assert limit_error.value.status_code == 400
        route.estado = "BORRADOR"
        pt_route.estado = "BORRADOR"
        db.session.commit()
        assert list_output_route_operations(
            db.session,
            actor_id=actor.id,
            output_article_id=output.id,
        )["total"] == 0
        route.estado = "APROBADA"
        pt_route.estado = "APROBADA"
        route.content_hash = None
        pt_route.content_hash = None
        db.session.commit()
        assert list_output_route_operations(
            db.session,
            actor_id=actor.id,
            output_article_id=output.id,
        )["total"] == 0
        route.content_hash = "a" * 64
        pt_route.content_hash = "b" * 64
        db.session.commit()
        assert list_output_route_operations(
            db.session,
            actor_id=actor.id,
            output_article_id=wip_output.id,
        )["total"] == 0
        assert list_output_route_operations(
            db.session,
            actor_id=actor.id,
            output_article_id=pt_output.id,
        )["total"] == 0
        assert list_output_route_operations(
            db.session,
            actor_id=actor.id,
            output_article_id=output.id,
            query="%",
        )["total"] == 0
        assert list_output_route_operations(
            db.session,
            actor_id=actor.id,
            output_article_id=output.id,
            query="pieza azul",
        )["total"] == 1

        monkeypatch.setattr("app.api.rutas_scm._actor_id", lambda: actor.id)
        response = client.get(
            f"/api/scm/v1/articulos/{output.id}/operaciones-ruta?limite=25"
        )
        assert response.status_code == 200
        assert response.get_json()["items"][0]["output_article"]["id"] == output.id

        cursor = _page_cursor_encode(
            endpoint=OUTPUT_ROUTE_OPERATIONS_ENDPOINT,
            actor_id=actor.id,
            capability="RUTA_VER",
            filters={"output_article_id": output.id, "q": None, "limit": 25},
            order="route_id_desc_operation_id_desc",
            last=[route.id, item["operation_id"]],
            code="INVALID_OUTPUT_ROUTE_PAGE",
        )
        with pytest.raises(ScmServiceError) as error:
            list_output_route_operations(
                db.session,
                actor_id=actor.id,
                output_article_id=wrong_output.id,
                cursor=cursor,
            )
        assert error.value.status_code == 400


def test_fabrication_list_eager_loads_route_owners_for_multiple_runs(
    app,
    monkeypatch,
):
    from app import db
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_production_orders import (
        ScmCorridaFabricacion,
        ScmOrdenFabricacion,
        ScmOrdenOperacion,
    )
    from app.models.scm_rutas import (
        ScmCentroTrabajo,
        ScmOperacionRuta,
        ScmRutaRevision,
    )
    from app.models.trabajador import Trabajador

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        output = ScmArticulo(
            codigo="PC-OF-LIST",
            nombre="Salida OF",
            clase="PIEZA_COLOR",
            unidad_base="UN",
            unidad_inventario="KG",
            activo=True,
        )
        owners = [
            ScmArticulo(
                codigo=f"WIP-OF-OWNER-{index}",
                nombre=f"Owner OF {index}",
                clase="SUBENSAMBLE_WIP",
                unidad_base="UN",
                unidad_inventario="KG",
                activo=True,
            )
            for index in (1, 2)
        ]
        center = ScmCentroTrabajo(
            codigo="CT-OF-LIST",
            nombre="Centro OF",
            tipo="INYECCION",
            activo=True,
        )
        db.session.add_all([output, *owners, center])
        db.session.flush()
        runs = []
        for index, owner in enumerate(owners, start=1):
            route = ScmRutaRevision(
                articulo_objetivo=owner,
                numero_revision=1,
                estado="APROBADA",
                content_hash=f"{index:064d}",
                creada_por_id=actor.id,
            )
            route_operation = ScmOperacionRuta(
                ruta=route,
                clave=f"OF_LIST_{index}",
                secuencia_visible=10,
                nombre=f"Operacion OF {index}",
                tipo="INYECCION",
                executor_kind="OP_OT",
                centro_trabajo=center,
                articulo_salida=output,
            )
            runs.append(ScmCorridaFabricacion(
                codigo=f"RUN-OF-LIST-{index}",
                secuencia=index,
                estado="BORRADOR",
                operacion_ruta=route_operation,
                operacion_ruta_hash=route.content_hash,
            ))
        order = ScmOrdenOperacion(
            codigo="OF-LIST-EAGER",
            tipo="FABRICACION",
            origen_demanda="PRUEBA_SINTETICA",
            motivo="N+1 guard",
            estado="BORRADOR",
            created_by_id=actor.id,
        )
        fabrication = ScmOrdenFabricacion(
            orden_operacion=order,
            snapshot_proceso="INYECCION",
            fuente_proceso="EXPLICITO",
            corridas=runs,
        )
        db.session.add(fabrication)
        db.session.commit()
        db.session.expire_all()

        monkeypatch.setattr(fabrication_service, "load_actor", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            fabrication_service,
            "operation_schedule_projections",
            lambda session, operations: {operation.id: {} for operation in operations},
        )

        def compact_serializer(_session, operation, **_kwargs):
            return {
                "owner_loaded": [
                    sa_inspect(run.operacion_ruta.ruta)
                    .attrs.articulo_objetivo.loaded_value is not NO_VALUE
                    for run in operation.fabricacion.corridas
                ],
            }

        monkeypatch.setattr(fabrication_service, "_serialize", compact_serializer)

        result = fabrication_service.list_fabrication_orders(
            db.session,
            actor_id=actor.id,
        )

        assert result["items"] == [{"owner_loaded": [True, True]}]
