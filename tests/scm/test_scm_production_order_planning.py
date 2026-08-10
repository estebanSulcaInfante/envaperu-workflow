from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app import db
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
)
from app.services.scm_production_order_service import (
    adjust_production_plan_targets,
    calculate_production_plan,
    confirm_production_plan,
)
from app.services.scm_fabrication_order_service import (
    list_fabrication_orders,
    release_fabrication_order,
    update_fabrication_order,
)
from app.services.scm_assembly_order_service import (
    list_assembly_orders,
    transition_assembly_order,
)
from app.services.scm_service_support import ScmServiceError


def test_demand_order_can_be_covered_by_fabrication_output(app):
    from app.models.producto import ProductoTerminado
    from app.models.scm_articulos import ScmArticuloProducto

    with app.app_context():
        product = ProductoTerminado(
            cod_sku_pt="PT-PLAN-001",
            producto="Producto de prueba",
            linea_id=1,
            familia_id=1,
        )
        db.session.add(product)
        db.session.flush()
        article_link = ScmArticuloProducto.query.filter_by(
            producto_terminado_id=product.cod_sku_pt,
        ).one()

        demand = ScmOrdenProduccion(
            codigo="OP-000001",
            origen="PLANIFICACION",
            fecha_necesidad=date(2026, 8, 10),
            created_by_id=1,
        )
        line = ScmOrdenProduccionLinea(
            producto_terminado_id=product.cod_sku_pt,
            cantidad_solicitada=Decimal("100.000"),
        )
        demand.lineas.append(line)

        operation = ScmOrdenOperacion(
            codigo="OF-000001",
            tipo="FABRICACION",
            origen_demanda="ORDEN_PRODUCCION",
            created_by_id=1,
        )
        fabrication = ScmOrdenFabricacion(orden_operacion=operation)
        run = ScmCorridaFabricacion(
            codigo="OF-000001-C01",
            secuencia=1,
        )
        fabrication.corridas.append(run)
        output = ScmOrdenOperacionSalida(
            orden_operacion=operation,
            corrida_fabricacion=run,
            articulo_scm_id=article_link.articulo_id,
            cantidad_objetivo=Decimal("100.000"),
        )
        allocation = ScmAsignacionDemandaSuministro(
            orden_produccion_linea=line,
            fuente_tipo="SALIDA_ORDEN",
            orden_operacion_salida=output,
            cantidad_planificada=Decimal("100.000"),
        )

        db.session.add_all([demand, operation, allocation])
        db.session.commit()

        assert demand.lineas == [line]
        assert operation.fabricacion is fabrication
        assert fabrication.corridas == [run]
        assert line.asignaciones == [allocation]
        assert allocation.orden_operacion_salida is output
        assert allocation.cantidad_planificada == Decimal("100.000")


def test_create_demand_order_api_uses_pt_lines_and_idempotency(
    app,
    client,
    scm_config,
):
    from app.models.producto import ProductoTerminado
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        planner = Trabajador.query.filter_by(codigo="TRB-01").one()
        planner.roles.append(
            RolOperativo.query.filter_by(codigo="PLANIFICACION").one()
        )
        db.session.add(ProductoTerminado(
            cod_sku_pt="PT-PLAN-API",
            producto="Producto API",
            linea_id=1,
            familia_id=1,
        ))
        db.session.commit()
        actor_id = planner.id

    operation_id = str(uuid4())
    payload = {
        "origen": "PLANIFICACION",
        "referencia_origen": "UAT-010P",
        "fecha_necesidad": "2026-08-10",
        "lineas": [{
            "producto_terminado_id": "PT-PLAN-API",
            "cantidad_solicitada": 120,
        }],
    }
    headers = {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": operation_id,
    }
    created = client.post(
        "/api/scm/v1/ordenes-produccion",
        json=payload,
        headers=headers,
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["codigo"] == "OP-000001"
    assert body["estado"] == "BORRADOR"
    assert body["lineas"][0]["cantidad_solicitada"] == "120.000"

    replay = client.post(
        "/api/scm/v1/ordenes-produccion",
        json=payload,
        headers=headers,
    )
    assert replay.status_code == 201
    assert replay.get_json() == body

    listed = client.get(
        "/api/scm/v1/ordenes-produccion",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.get_json()["items"]] == [body["id"]]


def test_create_and_release_exceptional_fabrication_order(
    app,
    client,
    scm_config,
):
    from app.models.molde import Molde
    from app.models.producto import (
        ColorBase,
        ColorProduccion,
        FamiliaColor,
        ProductoTerminado,
    )
    from app.models.scm_articulos import ScmArticuloProducto
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        )
        base = ColorBase(nombre="NATURAL")
        family = FamiliaColor(nombre="TRANSPARENTE")
        product = ProductoTerminado(
            cod_sku_pt="PT-OF-API",
            producto="Producto terminal de molde",
            linea_id=1,
            familia_id=1,
        )
        db.session.add_all([
            base,
            family,
            product,
            Molde(
                codigo="ML-OF-API",
                nombre="Molde API",
                peso_tiro_gr=100,
                tiempo_ciclo_std=30,
            ),
        ])
        db.session.flush()
        color = ColorProduccion(
            color_base_id=base.id,
            familia_color_id=family.id,
        )
        db.session.add(color)
        db.session.flush()
        article_id = ScmArticuloProducto.query.filter_by(
            producto_terminado_id=product.cod_sku_pt,
        ).one().articulo_id
        actor_id = actor.id
        color_id = color.id
        db.session.commit()

    headers = {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(uuid4()),
    }
    created = client.post(
        "/api/scm/v1/ordenes-fabricacion/excepcionales",
        headers=headers,
        json={
            "motivo": "Prueba controlada UAT",
            "molde_id": "ML-OF-API",
            "maquina_prevista_id": 1,
            "snapshot_tiempo_ciclo_seg": 30,
            "snapshot_horas_turno": 24,
            "snapshot_peso_colada_gr": 10,
            "corridas": [{
                "color_produccion_id": color_id,
                "ciclos_objetivo": 50,
                "salidas": [{
                    "articulo_scm_id": article_id,
                    "cantidad_por_ciclo": 2,
                    "peso_unitario_g": 45,
                }],
            }],
        },
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["codigo"] == "OF-000001"
    assert body["fecha_necesidad"] is None
    assert body["fecha_necesidad_fuente"] is None
    assert body["rango_fechas_ot"] == {
        "desde": None,
        "hasta": None,
        "cantidad": 0,
    }
    assert body["programacion_estado"] == "SIN_PROGRAMAR"
    assert body["contexto_temporal"] == {
        "fecha_necesidad_min": None,
        "fecha_necesidad_max": None,
        "fecha_necesidad_motivo": "SIN_DEMANDA_FECHADA",
        "fecha_ot_primera": None,
        "fecha_ot_ultima": None,
        "programacion_estado": "SIN_JORNADA",
        "cantidad_ot": 0,
    }
    assert body["started_at"] is None
    assert body["closed_at"] is None
    assert body["corridas"][0]["salidas"][0]["cantidad_objetivo"] == (
        "100.000"
    )
    assert body["corridas"][0]["salidas"][0]["kg_estandar_objetivo"] == (
        "4.500000"
    )

    released = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{body['id']}/liberar",
        headers={
            "X-Actor-Id": str(actor_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={"version": body["version"]},
    )
    assert released.status_code == 200
    assert released.get_json()["estado"] == "LIBERADA"
    assert released.get_json()["corridas"][0]["estado"] == "LIBERADA"


def test_calcular_y_confirmar_plan_crea_of_y_oa_en_borrador(app, scm_config):
    from app.models.maquina import Maquina, TipoMaquina
    from app.models.molde import Molde, MoldePieza, Pieza
    from app.models.producto import (
        ColorBase,
        ColorProduccion,
        Familia,
        FamiliaColor,
        Linea,
        PiezaColor,
        ProductoTerminado,
    )
    from app.models.scm_articulos import (
        ScmArticuloPiezaColor,
        ScmArticuloProducto,
    )
    from app.models.scm_estructuras import (
        ScmEstructuraComponente,
        ScmEstructuraRevision,
    )
    from app.models.scm_rutas import (
        ScmCentroTrabajo,
        ScmOperacionPrecedencia,
        ScmOperacionRuta,
        ScmRutaRevision,
    )
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        planner = Trabajador.query.filter_by(codigo="TRB-01").one()
        planner.roles.append(
            RolOperativo.query.filter_by(codigo="PLANIFICACION").one()
        )
        planner.roles.append(
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        )
        line_master = Linea.query.first()
        family = Familia.query.first()
        piece = Pieza(
            codigo="PZ-PLAN-001",
            nombre="Pieza planificada",
            linea_id=line_master.id,
            familia_id=family.id,
            peso_nominal_gr=50,
        )
        color_base = ColorBase(nombre="AZUL PLAN")
        color_family = FamiliaColor(nombre="SOLIDO PLAN")
        color = ColorProduccion(
            color_base_rel=color_base,
            familia_color_rel=color_family,
        )
        piece_color = PiezaColor(
            sku="PC-PLAN-001",
            pieza_rel=piece,
            piezas="Pieza azul",
            color_produccion_rel=color,
            linea_id=line_master.id,
            familia_id=family.id,
            peso=50,
        )
        product = ProductoTerminado(
            cod_sku_pt="PT-PLAN-FLOW",
            producto="Producto armado",
            linea_id=line_master.id,
            familia_id=family.id,
        )
        db.session.add_all([
            piece,
            color_base,
            color_family,
            color,
            piece_color,
            product,
        ])
        db.session.flush()
        db.session.add_all([
            Molde(
                codigo="ML-PLAN-FLOW",
                nombre="Molde de la OF planificada",
                peso_tiro_gr=120,
                tiempo_ciclo_std=30,
            ),
            MoldePieza(
                molde_id="ML-PLAN-FLOW",
                pieza_id=piece.id,
                cavidades=2,
                peso_unitario_gr=50,
            ),
        ])
        piece_article = ScmArticuloPiezaColor.query.filter_by(
            pieza_color_sku=piece_color.sku
        ).one().articulo
        product_article = ScmArticuloProducto.query.filter_by(
            producto_terminado_id=product.cod_sku_pt
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
                articulo_componente_id=piece_article.id,
                cantidad=2,
                unidad="UN",
            )],
        )
        center = ScmCentroTrabajo(
            codigo="CTR-PLAN",
            nombre="Centro planificación",
            tipo="INYECCION",
        )
        route = ScmRutaRevision(
            articulo_objetivo_id=product_article.id,
            numero_revision=1,
            estado="APROBADA",
            content_hash="b" * 64,
            creada_por_id=planner.id,
            aprobada_por_id=planner.id,
        )
        db.session.add_all([structure, center, route])
        db.session.flush()
        fabrication_step = ScmOperacionRuta(
            ruta=route,
            clave="FABRICAR",
            secuencia_visible=1,
            nombre="Fabricar pieza",
            tipo="INYECCION",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=piece_article.id,
        )
        assembly_step = ScmOperacionRuta(
            ruta=route,
            clave="ENSAMBLAR",
            secuencia_visible=2,
            nombre="Armar producto",
            tipo="ENSAMBLE",
            executor_kind="ORDEN_OPERACION",
            centro_trabajo_id=center.id,
            articulo_salida_id=product_article.id,
            estructura_revision_id=structure.id,
        )
        db.session.add_all([fabrication_step, assembly_step])
        db.session.flush()
        db.session.add(ScmOperacionPrecedencia(
            ruta_id=route.id,
            operacion_anterior_id=fabrication_step.id,
            operacion_siguiente_id=assembly_step.id,
        ))
        order = ScmOrdenProduccion(
            codigo="OP-000900",
            origen="PLANIFICACION",
            fecha_necesidad=date(2026, 8, 10),
            estado="APROBADA",
            created_by_id=planner.id,
            approved_by_id=planner.id,
        )
        demand_line = ScmOrdenProduccionLinea(
            producto_terminado_id=product.cod_sku_pt,
            cantidad_solicitada=10,
            estructura_revision_id=structure.id,
            estructura_hash=structure.content_hash,
            ruta_revision_id=route.id,
            ruta_hash=route.content_hash,
        )
        order.lineas.append(demand_line)
        db.session.add(order)
        db.session.commit()

        calculated = calculate_production_plan(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            order_id=order.id,
            expected_resource_version=order.version,
        )
        documents = calculated["plan"]["propuesta"]["documentos"]
        assert [
            (item["tipo"], item["cantidad_objetivo"])
            for item in documents
        ] == [("FABRICACION", "20"), ("ENSAMBLE", "10.000")]
        assert calculated["plan"]["propuesta"]["bloqueos"] == []

        adjusted = adjust_production_plan_targets(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            order_id=order.id,
            data={
                "version": order.version,
                "plan_id": calculated["plan"]["id"],
                "content_hash": calculated["plan"]["content_hash"],
                "motivo": "Ajuste de metas para UAT",
                "ajustes": [
                    {
                        "clave": documents[0]["clave"],
                        "cantidad_objetivo": 18,
                    },
                    {
                        "clave": documents[1]["clave"],
                        "cantidad_objetivo": 9,
                    },
                ],
            },
        )
        assert adjusted["plan"]["revision"] == 2
        assert [
            (
                item["cantidad_calculada"],
                item["cantidad_objetivo"],
            )
            for item in adjusted["plan"]["propuesta"]["documentos"]
        ] == [("20", "18.000"), ("10.000", "9.000")]
        assert adjusted["plan"]["propuesta"]["ajustes"][0]["motivo"] == (
            "Ajuste de metas para UAT"
        )

        calculated = calculate_production_plan(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            order_id=order.id,
            expected_resource_version=order.version,
        )

        from app.models.scm_inventory import (
            ScmSaldoInventario,
            ScmUbicacionInventario,
        )

        location = ScmUbicacionInventario(
            codigo="ALMACEN_UAT",
            nombre="Almacen UAT",
        )
        inventory = ScmSaldoInventario(
            articulo_scm_id=piece_article.id,
            ubicacion=location,
            cantidad_fisica=4,
        )
        db.session.add_all([location, inventory])
        db.session.commit()
        with_stock = calculate_production_plan(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            order_id=order.id,
            expected_resource_version=order.version,
        )
        assert [
            (item["tipo"], item["cantidad_objetivo"])
            for item in with_stock["plan"]["propuesta"]["documentos"]
        ] == [("FABRICACION", "16.000"), ("ENSAMBLE", "10.000")]
        assert with_stock["plan"]["propuesta"]["politica_stock"] == (
            "KARDEX_NORMALIZADO"
        )
        assert with_stock["plan"]["propuesta"]["reservas_stock"][0][
            "cantidad"
        ] == "4.000"

        calculated = with_stock
        confirmed = confirm_production_plan(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            order_id=order.id,
            data={
                "version": order.version,
                "plan_id": calculated["plan"]["id"],
                "content_hash": calculated["plan"]["content_hash"],
            },
        )
        assert confirmed["orden"]["estado"] == "PLANIFICADA"
        assert {item["tipo"] for item in confirmed["documentos"]} == {
            "FABRICACION",
            "ENSAMBLE",
        }
        assert {item["estado"] for item in confirmed["documentos"]} == {
            "BORRADOR"
        }
        assert ScmOrdenFabricacion.query.count() == 1
        fabrication_order = ScmOrdenOperacion.query.filter_by(
            tipo="FABRICACION",
        ).one()
        assert str(fabrication_order.plan_produccion_id) == (
            calculated["plan"]["id"]
        )
        assert fabrication_order.propuesta_clave
        # La fecha efectiva de la linea prevalece sobre la cabecera OP y debe
        # propagarse tambien hacia una OF intermedia del plan multinivel.
        demand_line.fecha_necesidad = date(2026, 8, 12)
        from app.models.registro import RegistroDiarioProduccion

        db.session.add_all([
            RegistroDiarioProduccion(
                codigo_ot="OT-PROJ-OF-01",
                codigo_ot_sintetico=False,
                estado="CERRADA",
                tipo_ot="FABRICACION",
                orden_operacion_id=fabrication_order.id,
                maquina_id=1,
                fecha=date(2026, 8, 8),
                turno="DIA",
                created_by_id=planner.id,
            ),
            RegistroDiarioProduccion(
                codigo_ot="OT-PROJ-OF-02",
                codigo_ot_sintetico=False,
                estado="PLANIFICADA",
                tipo_ot="FABRICACION",
                orden_operacion_id=fabrication_order.id,
                maquina_id=1,
                fecha=date(2026, 8, 9),
                turno="DIA",
                created_by_id=planner.id,
            ),
            # Las anuladas no forman parte de la ventana programada vigente.
            RegistroDiarioProduccion(
                codigo_ot="OT-PROJ-OF-ANU",
                codigo_ot_sintetico=False,
                estado="ANULADA",
                tipo_ot="FABRICACION",
                orden_operacion_id=fabrication_order.id,
                maquina_id=1,
                fecha=date(2026, 8, 7),
                turno="DIA",
                created_by_id=planner.id,
            ),
        ])
        db.session.commit()
        serialized_of = list_fabrication_orders(
            db.session,
            actor_id=planner.id,
        )["items"][0]
        assert serialized_of["fecha_necesidad"] == "2026-08-12"
        assert serialized_of["fecha_necesidad_fuente"] == {
            "tipo": "OP",
            "id": str(order.id),
            "codigo": "OP-000900",
        }
        assert serialized_of["rango_fechas_ot"] == {
            "desde": "2026-08-08",
            "hasta": "2026-08-09",
            "cantidad": 2,
        }
        assert serialized_of["programacion_estado"] == "PROGRAMADA"
        assert serialized_of["contexto_temporal"] == {
            "fecha_necesidad_min": "2026-08-12",
            "fecha_necesidad_max": "2026-08-12",
            "fecha_necesidad_motivo": None,
            "fecha_ot_primera": "2026-08-08",
            "fecha_ot_ultima": "2026-08-09",
            "programacion_estado": "PROGRAMADA",
            "cantidad_ot": 2,
        }
        planned_ot = RegistroDiarioProduccion.query.filter_by(
            codigo_ot="OT-PROJ-OF-02",
        ).one()
        planned_ot.estado = "EN_EJECUCION"
        db.session.commit()
        assert list_fabrication_orders(
            db.session,
            actor_id=planner.id,
        )["items"][0]["contexto_temporal"]["programacion_estado"] == (
            "EN_EJECUCION"
        )
        planned_ot.estado = "CERRADA"
        db.session.commit()
        assert list_fabrication_orders(
            db.session,
            actor_id=planner.id,
        )["items"][0]["contexto_temporal"]["programacion_estado"] == (
            "CERRADA"
        )
        assert "started_at" in serialized_of
        assert "closed_at" in serialized_of
        assert serialized_of["proceso_requerido"] == "INYECCION"
        assert serialized_of["corridas"][0]["salidas"][0]["articulo"][
            "pieza_id"
        ] == piece.id
        run = fabrication_order.fabricacion.corridas[0]
        output = run.salidas[0]

        blowing_type = TipoMaquina(
            codigo="SOPLADO-UAT",
            nombre="Sopladora UAT",
            proceso="SOPLADO",
        )
        db.session.add(blowing_type)
        db.session.flush()
        incompatible_machine = Maquina(
            codigo="MQ-SOP-UAT",
            nombre="Sopladora incompatible para inyeccion",
            tipo_maquina_id=blowing_type.id,
            estado="OPERATIVA",
            activo=True,
        )
        db.session.add(incompatible_machine)
        db.session.commit()

        with pytest.raises(ScmServiceError) as incompatible:
            update_fabrication_order(
                db.session,
                actor_id=planner.id,
                operation_id=uuid4(),
                operation_order_id=fabrication_order.id,
                data={
                    "version": fabrication_order.version,
                    "molde_id": "ML-PLAN-FLOW",
                    "maquina_prevista_id": incompatible_machine.id,
                    "snapshot_horas_turno": 8,
                    "corridas": [{
                        "id": str(run.id),
                        "salidas": [{"id": str(output.id)}],
                    }],
                },
            )
        assert incompatible.value.code == "MACHINE_PROCESS_INCOMPATIBLE"
        assert incompatible.value.details["proceso_requerido"] == "INYECCION"

        fabrication_order = db.session.get(
            ScmOrdenOperacion,
            fabrication_order.id,
        )
        run = fabrication_order.fabricacion.corridas[0]
        output = run.salidas[0]
        configured = update_fabrication_order(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            operation_order_id=fabrication_order.id,
            data={
                "version": fabrication_order.version,
                "molde_id": "ML-PLAN-FLOW",
                "maquina_prevista_id": 1,
                "snapshot_horas_turno": 8,
                "corridas": [{
                    "id": str(run.id),
                    "salidas": [{"id": str(output.id)}],
                }],
            },
        )
        configured_run = configured["corridas"][0]
        configured_output = configured_run["salidas"][0]
        assert Decimal(configured["snapshot_tiempo_ciclo_seg"]) == Decimal("30")
        assert Decimal(configured["snapshot_peso_colada_gr"]) == Decimal("20")
        assert configured_run["ciclos_objetivo"] == 8
        assert configured_output["cantidad_por_ciclo_snapshot"] == "2.0000"
        assert configured_output["cantidad_objetivo"] == "16.000"
        assert configured_output["excedente_objetivo"] == "0.000"
        assert configured_output["kg_estandar_objetivo"] == "0.800000"

        released = release_fabrication_order(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            operation_order_id=fabrication_order.id,
            expected_resource_version=configured["version"],
        )
        assert released["estado"] == "LIBERADA"
        assembly_order = ScmOrdenOperacion.query.filter_by(
            tipo="ENSAMBLE",
        ).one()
        assembly = transition_assembly_order(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            order_id=assembly_order.id,
            action="liberar",
            data={"version": assembly_order.version},
        )
        assert assembly["estado"] == "LIBERADA"
        assert assembly["entradas_planificadas"][0][
            "cantidad_planificada"
        ] == "20"
        assembly = transition_assembly_order(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            order_id=assembly_order.id,
            action="iniciar",
            data={"version": assembly["version"]},
        )
        assert assembly["estado"] == "EN_EJECUCION"
        assembly = transition_assembly_order(
            db.session,
            actor_id=planner.id,
            operation_id=uuid4(),
            order_id=assembly_order.id,
            action="cerrar",
            data={
                "version": assembly["version"],
                "cantidad_real": 10,
                "cantidad_rechazada": 0,
            },
        )
        assert assembly["estado"] == "CERRADA"
        assert Decimal(assembly["salida"]["cantidad_real"]) == Decimal("10")
        assert Decimal(
            assembly["lote_salida"]["cantidad_acreditada"]
        ) == Decimal("10")
        assert ScmAsignacionDemandaSuministro.query.count() == 1
        allocation = ScmAsignacionDemandaSuministro.query.one()
        assert allocation.cantidad_planificada == Decimal("10.000")
        assert allocation.orden_operacion_salida.orden_operacion.tipo == (
            "ENSAMBLE"
        )
        assert allocation.estado == "SATISFECHA"
        later_demand_line = ScmOrdenProduccionLinea(
            orden_produccion=order,
            producto_terminado_id=product.cod_sku_pt,
            cantidad_solicitada=1,
            fecha_necesidad=date(2026, 8, 15),
            estructura_revision_id=structure.id,
            estructura_hash=structure.content_hash,
            ruta_revision_id=route.id,
            ruta_hash=route.content_hash,
        )
        db.session.add(later_demand_line)
        db.session.flush()
        db.session.add(ScmAsignacionDemandaSuministro(
            orden_produccion_linea=later_demand_line,
            fuente_tipo="SALIDA_ORDEN",
            orden_operacion_salida=assembly_order.salidas[0],
            cantidad_planificada=1,
            operation_id=uuid4(),
        ))
        db.session.add_all([
            RegistroDiarioProduccion(
                codigo_ot="OT-PROJ-OA-01",
                codigo_ot_sintetico=False,
                estado="CERRADA",
                tipo_ot="ENSAMBLE",
                modo_ejecucion_ensamble="MESA",
                orden_operacion_id=assembly_order.id,
                centro_trabajo_id=center.id,
                responsable_id=planner.id,
                cantidad_objetivo=5,
                fecha=date(2026, 8, 9),
                turno="DIA",
                created_by_id=planner.id,
            ),
            RegistroDiarioProduccion(
                codigo_ot="OT-PROJ-OA-02",
                codigo_ot_sintetico=False,
                estado="PLANIFICADA",
                tipo_ot="ENSAMBLE",
                modo_ejecucion_ensamble="MESA",
                orden_operacion_id=assembly_order.id,
                centro_trabajo_id=center.id,
                responsable_id=planner.id,
                cantidad_objetivo=5,
                fecha=date(2026, 8, 10),
                turno="DIA",
                created_by_id=planner.id,
            ),
        ])
        db.session.commit()
        serialized_oa = list_assembly_orders(
            db.session,
            actor_id=planner.id,
        )["items"][0]
        assert serialized_oa["fecha_necesidad"] == "2026-08-12"
        assert serialized_oa["fecha_necesidad_fuente"] == {
            "tipo": "OP",
            "id": str(order.id),
            "codigo": "OP-000900",
        }
        assert serialized_oa["rango_fechas_ot"] == {
            "desde": "2026-08-09",
            "hasta": "2026-08-10",
            "cantidad": 2,
        }
        assert serialized_oa["programacion_estado"] == "PROGRAMADA"
        assert serialized_oa["contexto_temporal"] == {
            "fecha_necesidad_min": "2026-08-12",
            "fecha_necesidad_max": "2026-08-15",
            "fecha_necesidad_motivo": None,
            "fecha_ot_primera": "2026-08-09",
            "fecha_ot_ultima": "2026-08-10",
            "programacion_estado": "PROGRAMADA",
            "cantidad_ot": 2,
        }
        from app.models.scm_inventory import ScmReservaInventario

        reservation = ScmReservaInventario.query.one()
        assert reservation.cantidad == Decimal("4.000")
        assert reservation.uso == "INPUT_OPERACION"
        assert inventory.cantidad_reservada == Decimal("4.000")
