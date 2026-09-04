from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import event

from app import db
from app.models.materiales import MateriaPrima
from app.models.maquina import Maquina
from app.models.receta_color import RecetaColorLinea, RecetaColorMaestra
from app.models.scm_articulos import ScmArticulo
from app.models.scm_catalogos import (
    ScmCapacidad,
    ScmCategoriaRecepcion,
    ScmMaterial,
)
from app.models.scm_inventory import (
    ScmSaldoMaterialInventario,
    ScmUbicacionInventario,
)
from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
from app.models.scm_prepared_material import (
    ScmAsignacionRequerimientoPreparacion,
    ScmBolsaMaterialPreparado,
    ScmLecturaPesoPreparacion,
    ScmLoteMaterialPreparado,
    ScmMovimientoMaterialPreparado,
    ScmOrdenPreparacionMaterial,
    ScmRequerimientoMaterialPreparado,
    ScmReservaMaterialPreparado,
    ScmEmisionMaterialPreparado,
    ScmSaldoMaterialPreparado,
)
from app.models.scm_ot import ScmTrabajoColor, ScmTrabajoOt
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.models.registro import RegistroDiarioProduccion


def _headers(actor_id):
    return {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(uuid4()),
    }


def _seed_two_jar_runs_with_one_recipe():
    planner = Trabajador.query.filter_by(codigo="TRB-01").one()
    planner.roles.append(
        RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
    )
    category = ScmCategoriaRecepcion.query.filter_by(
        codigo="LEGACY_POR_CONFIGURAR"
    ).one()
    resin = ScmMaterial(
        codigo="MP-PP-JARRA-6L",
        nombre="Polipropileno virgen para Jarra 6L",
        clase="MATERIA_PRIMA",
        categoria_recepcion_id=category.id,
    )
    db.session.add(resin)
    db.session.flush()
    db.session.add(
        MateriaPrima(
            nombre=resin.nombre,
            tipo="VIRGEN",
            scm_material_id=resin.id,
        )
    )

    from app.models.producto import (
        ColorBase,
        ColorProduccion,
        FamiliaColor,
        PiezaColor,
    )

    color_base = ColorBase.query.first()
    if color_base is None:
        color_base = ColorBase(nombre="TRANSPARENTE")
        db.session.add(color_base)
        db.session.flush()
    color_family = FamiliaColor.query.first()
    if color_family is None:
        color_family = FamiliaColor(nombre="TRANSPARENTE")
        db.session.add(color_family)
        db.session.flush()
    color = ColorProduccion(
        color_base_id=color_base.id,
        familia_color_id=color_family.id,
    )
    db.session.add(color)
    db.session.flush()
    recipe = RecetaColorMaestra(
        color_produccion_id=color.id,
        producto_scope="*",
        nombre_variante="JARRA 6L TRANSPARENTE",
        revision=1,
        estado="APROBADA",
        base_virgen_kg=25,
    )
    recipe.lineas = [
        RecetaColorLinea(
            material_id=resin.id,
            tipo_componente="MATERIA_PRIMA",
            cantidad=1,
            unidad="FRACCION",
            orden=1,
        )
    ]
    db.session.add(recipe)
    db.session.flush()

    piece_color = PiezaColor(
        sku="PC-JARRA-6L-TRANSPARENTE",
        piezas="Jarra 6L transparente",
        color_produccion_id=color.id,
        peso=100,
    )
    db.session.add(piece_color)
    db.session.flush()
    article = ScmArticulo.query.filter_by(
        codigo="PC-JARRA-6L-TRANSPARENTE"
    ).one()

    run_ids = []
    for sequence in (1, 2):
        order = ScmOrdenOperacion(
            codigo=f"OF-JARRA-6L-{sequence:02d}",
            tipo="FABRICACION",
            origen_demanda="EXCEPCIONAL",
            estado="LIBERADA",
            created_by_id=planner.id,
            released_by_id=planner.id,
        )
        fabrication = ScmOrdenFabricacion(
            orden_operacion=order,
            snapshot_tiempo_ciclo_seg=20,
            snapshot_horas_turno=8,
            snapshot_peso_colada_gr=10,
        )
        run = ScmCorridaFabricacion(
            orden_fabricacion=fabrication,
            codigo=f"OF-JARRA-6L-{sequence:02d}-C01",
            secuencia=1,
            color_produccion_id=color.id,
            receta_revision_id=recipe.id,
            ciclos_objetivo=100,
            estado="LIBERADA",
        )
        output = ScmOrdenOperacionSalida(
            orden_operacion=order,
            corrida_fabricacion=run,
            articulo=article,
            cantidad_por_ciclo_snapshot=1,
            peso_unitario_snapshot_g=100,
            cantidad_objetivo=100,
            kg_estandar_objetivo=10,
        )
        db.session.add_all([order, fabrication, run, output])
        db.session.flush()
        run_ids.append(run.id)

    db.session.commit()
    return planner.id, recipe.id, run_ids, resin.id


def _seed_l1_actors_and_stock(*, planner_id, resin_id):
    planner = db.session.get(Trabajador, planner_id)
    preparer_role = RolOperativo.query.filter_by(codigo="PREPARADOR_MATERIAL").one()
    supervisor_role = RolOperativo.query.filter_by(codigo="SUPERVISOR").one()
    quality_role = RolOperativo.query.filter_by(codigo="CALIDAD").one()
    warehouse_role = RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
    preparer = Trabajador(
        codigo="TRB-OPM-PREP", nombres="Carlos", apellidos="Medina", activo=True,
    )
    preparer.roles.extend([
        preparer_role, supervisor_role, quality_role,
    ])
    confirmer = Trabajador(
        codigo="TRB-OPM-4OJOS", nombres="Mario", apellidos="Confirmador", activo=True,
    )
    confirmer.roles.append(supervisor_role)
    warehouse_actor = Trabajador(
        codigo="TRB-OPM-ALM", nombres="Ana", apellidos="Almacen", activo=True,
    )
    warehouse_actor.roles.append(warehouse_role)
    quality_actor = Trabajador(
        codigo="TRB-OPM-CAL", nombres="Rosa", apellidos="Calidad", activo=True,
    )
    quality_actor.roles.append(quality_role)
    warehouse = ScmAlmacen(
        codigo="A-ENVA-MP", nombre="Almacen de materias primas",
        tipo="MATERIAS_PRIMAS",
    )
    source = ScmUbicacionInventario(
        almacen=warehouse, codigo="A-ENVA-MP-GEN", nombre="Stock general MP",
        tipo="ZONA", permite_saldo_libre=True,
        clases_articulo_json=["MATERIA_PRIMA", "COLORANTE", "ADITIVO"],
    )
    staging = ScmUbicacionInventario(
        almacen=warehouse, codigo="A-ENVA-MP-PREP", nombre="Preparacion de material",
        tipo="STAGING", permite_saldo_libre=False,
        clases_articulo_json=["MATERIA_PRIMA", "COLORANTE", "ADITIVO"],
    )
    prepared_storage = ScmUbicacionInventario(
        almacen=warehouse, codigo="A-ENVA-MP-MEZ", nombre="Material preparado",
        tipo="ZONA", permite_saldo_libre=True,
        clases_articulo_json=["MATERIAL_PREPARADO"],
    )
    production_point = ScmUbicacionInventario(
        almacen_id=None, codigo="P-ENVA-INY-01",
        nombre="Punto de consumo Inyectora 01",
        tipo="PUNTO_PRODUCCION", permite_saldo_libre=False,
        clases_articulo_json=["MATERIAL_PREPARADO"],
    )
    db.session.add_all([
        preparer, confirmer, warehouse_actor, quality_actor,
        warehouse, source, staging, prepared_storage, production_point,
    ])
    db.session.flush()
    for worker in (preparer, warehouse_actor):
        db.session.add(ScmAlmacenTrabajador(
            almacen_id=warehouse.id,
            trabajador_id=worker.id,
            clases_articulo_json=[
                "MATERIA_PRIMA", "COLORANTE", "ADITIVO", "MATERIAL_PREPARADO"
            ],
            asignado_por_id=planner.id,
        ))
    db.session.add(ScmSaldoMaterialInventario(
        material_id=resin_id,
        ubicacion_id=source.id,
        cantidad_fisica_kg=100,
    ))
    db.session.commit()
    return {
        "preparer_id": preparer.id,
        "confirmer_id": confirmer.id,
        "warehouse_id": warehouse_actor.id,
        "quality_id": quality_actor.id,
        "source_id": source.id,
        "staging_id": staging.id,
        "prepared_storage_id": prepared_storage.id,
        "production_point_id": production_point.id,
    }


def _calculate_and_propose(client, *, planner_id, run_ids):
    requirement_ids = []
    for run_id in run_ids:
        response = client.post(
            "/api/scm/v1/requerimientos-preparacion/calcular",
            headers=_headers(planner_id),
            json={"corrida_fabricacion_id": str(run_id)},
        )
        assert response.status_code == 200, response.get_json()
        requirement_ids.append(response.get_json()["id"])
    response = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planner_id),
        json={
            "motivo": "Preparacion compartida Jarra 6L",
            "coberturas": [
                {"requerimiento_id": value, "cantidad_kg": "11.000"}
                for value in requirement_ids
            ],
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _seed_free_prepared_bag(
    *, planner_id, recipe_id, composition_hash, location_id, quantity="10.000",
):
    net = Decimal(quantity)
    order = ScmOrdenPreparacionMaterial(
        codigo=f"OPM-STOCK-{str(uuid4())[:8].upper()}",
        receta_revision_id=recipe_id,
        composicion_hash=composition_hash,
        cantidad_objetivo_kg=net,
        estado="CERRADA",
        motivo="Lote disponible previo",
        created_by_id=planner_id,
        closed_by_id=planner_id,
        operation_id=uuid4(),
    )
    reading = ScmLecturaPesoPreparacion(
        orden=order, tipo_uso="BOLSA_SALIDA",
        peso_bruto_kg=net + Decimal("0.100"), tara_kg=Decimal("0.100"),
        peso_neto_kg=net, metodo="CONTINGENCIA_MANUAL",
        evidencia_ref="SEED-STOCK-LMP-01", motivo="Bolsa disponible",
        estado="UTILIZADA", created_by_id=planner_id, operation_id=uuid4(),
    )
    lot = ScmLoteMaterialPreparado(
        orden=order, codigo=f"LMP-STOCK-{str(uuid4())[:8].upper()}",
        receta_revision_id=recipe_id, cantidad_kg=net,
        estado="DISPONIBLE", created_by_id=planner_id,
    )
    bag = ScmBolsaMaterialPreparado(
        orden=order, lote=lot, lectura=reading,
        codigo=f"BMP-STOCK-{str(uuid4())[:8].upper()}", secuencia=1,
        peso_bruto_kg=net + Decimal("0.100"), tara_kg=Decimal("0.100"),
        peso_neto_kg=net, metodo="CONTINGENCIA_MANUAL",
        evidencia_ref="SEED-STOCK-LMP-01", motivo="Bolsa disponible",
        estado="DISPONIBLE", ubicacion_id=location_id,
        created_by_id=planner_id, confirmed_by_id=planner_id,
        operation_id=uuid4(),
    )
    balance = ScmSaldoMaterialPreparado.query.filter_by(
        receta_revision_id=recipe_id, ubicacion_id=location_id,
    ).one_or_none()
    if balance is None:
        balance = ScmSaldoMaterialPreparado(
            receta_revision_id=recipe_id, ubicacion_id=location_id,
            cantidad_fisica_kg=net, cantidad_reservada_kg=Decimal("0.000"),
            cantidad_no_disponible_kg=Decimal("0.000"),
        )
        db.session.add(balance)
    else:
        balance.cantidad_fisica_kg = Decimal(balance.cantidad_fisica_kg) + net
        balance.version += 1
    db.session.add_all([order, reading, lot, bag])
    db.session.flush()
    return bag.id


def _seed_work_color(*, planner_id, run_id):
    run = db.session.get(ScmCorridaFabricacion, run_id)
    machine = Maquina.query.first()
    work_order = RegistroDiarioProduccion(
        codigo_ot=f"OT-OPM-{str(uuid4())[:8].upper()}",
        codigo_ot_sintetico=False,
        estado="PLANIFICADA",
        tipo_ot="FABRICACION",
        fecha=date(2026, 8, 17),
        turno="DIA",
        maquina_id=machine.id,
        responsable_id=planner_id,
        maquinista_previsto_id=planner_id,
        orden_operacion_id=run.orden_fabricacion.orden_operacion_id,
        corrida_fabricacion_id=run.id,
        created_by_id=planner_id,
    )
    db.session.add(work_order)
    db.session.flush()
    work = ScmTrabajoOt(
        orden_trabajo_id=work_order.id,
        codigo=f"TC-OPM-{str(uuid4())[:8].upper()}",
        secuencia=1,
        estado="EN_EJECUCION",
        orden_operacion_id=run.orden_fabricacion.orden_operacion_id,
        cantidad_objetivo_un=100,
        created_by_id=planner_id,
    )
    db.session.add(work)
    db.session.flush()
    db.session.add(ScmTrabajoColor(
        trabajo_ot_id=work.id,
        corrida_fabricacion_id=run.id,
        receta_revision_id_snapshot=run.receta_revision_id,
        receta_hash_snapshot=run.receta_hash,
    ))
    db.session.flush()
    return work.id


def _post_with_replay(client, path, *, actor_id, body):
    headers = _headers(actor_id)
    first = client.post(path, headers=headers, json=body)
    replay = client.post(path, headers=headers, json=body)
    assert replay.status_code == first.status_code, replay.get_json()
    assert replay.get_json() == first.get_json()
    return first


def _propose_explicit_output_plan(
    app, client, *, planner_id, run_ids, quantities,
):
    requirement_ids = []
    for run_id, quantity in zip(run_ids, quantities):
        generated = client.post(
            "/api/scm/v1/requerimientos-preparacion/calcular",
            headers=_headers(planner_id),
            json={"corrida_fabricacion_id": str(run_id)},
        )
        assert generated.status_code == 200, generated.get_json()
        requirement_id = UUID(generated.get_json()["id"])
        with app.app_context():
            requirement = db.session.get(
                ScmRequerimientoMaterialPreparado, requirement_id,
            )
            requirement.cantidad_requerida_kg = Decimal(quantity)
            db.session.commit()
        requirement_ids.append(requirement_id)
    proposed = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planner_id),
        json={
            "motivo": "Plan explicito de bolsas completas",
            "coberturas": [
                {
                    "requerimiento_id": str(requirement_id),
                    "cantidad_kg": quantity,
                }
                for requirement_id, quantity in zip(
                    requirement_ids, quantities
                )
            ],
        },
    )
    assert proposed.status_code == 201, proposed.get_json()
    with app.app_context():
        order = db.session.get(
            ScmOrdenPreparacionMaterial, UUID(proposed.get_json()["id"]),
        )
        order.estado = "EN_PREPARACION"
        order.version += 1
        for assignment in order.asignaciones:
            assignment.estado = "COMPROMETIDA"
            assignment.cantidad_comprometida_kg = (
                assignment.cantidad_planificada_kg
            )
        db.session.commit()
    return client.get(
        f"/api/scm/v1/ordenes-preparacion-material/{proposed.get_json()['id']}",
        headers={"X-Actor-Id": str(planner_id)},
    ).get_json()


def _record_output_reading(
    client, *, actor_id, order, assignment_id, net, evidence,
):
    net_value = Decimal(net)
    return client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order['id']}/lecturas",
        headers=_headers(actor_id),
        json={
            "version": order["version"],
            "tipo_uso": "BOLSA_SALIDA",
            "metodo": "CONTINGENCIA_MANUAL",
            "bruto_kg": format(net_value + Decimal("0.100"), "f"),
            "tara_kg": "0.100",
            "neto_kg": format(net_value, "f"),
            "motivo": "Bolsa completa con destino explicito",
            "evidencia_ref": evidence,
            "asignacion_requerimiento_id": assignment_id,
        },
    )


def test_lecturas_salida_25_15_exigen_destino_y_admiten_multiples_bolsas(
    app, client, scm_config,
):
    with app.app_context():
        planner_id, _recipe_id, run_ids, _resin_id = (
            _seed_two_jar_runs_with_one_recipe()
        )
    order = _propose_explicit_output_plan(
        app, client,
        planner_id=planner_id,
        run_ids=run_ids,
        quantities=("25.000", "15.000"),
    )
    omitted = _record_output_reading(
        client,
        actor_id=planner_id,
        order=order,
        assignment_id=None,
        net="1.000",
        evidence="OUT-NO-ASSIGNMENT",
    )
    assert omitted.status_code == 422
    assert omitted.get_json()["error"]["code"] == "OPM_OUTPUT_ASSIGNMENT_REQUIRED"

    assignments = sorted(
        order["asignaciones"],
        key=lambda value: Decimal(value["cantidad_planificada_kg"]),
        reverse=True,
    )
    for sequence, (assignment, net) in enumerate((
        (assignments[0], "14.000"),
        (assignments[0], "11.000"),
        (assignments[1], "15.000"),
    ), start=1):
        order = client.get(
            f"/api/scm/v1/ordenes-preparacion-material/{order['id']}",
            headers={"X-Actor-Id": str(planner_id)},
        ).get_json()
        recorded = _record_output_reading(
            client,
            actor_id=planner_id,
            order=order,
            assignment_id=assignment["id"],
            net=net,
            evidence=f"OUT-25-15-{sequence}",
        )
        assert recorded.status_code == 201, recorded.get_json()
        assert (
            recorded.get_json()["asignacion_requerimiento_id"]
            == assignment["id"]
        )


def test_bolsa_15_no_cabe_en_planes_10_10_y_bolsa_5_si(
    app, client, scm_config,
):
    with app.app_context():
        planner_id, _recipe_id, run_ids, _resin_id = (
            _seed_two_jar_runs_with_one_recipe()
        )
    order = _propose_explicit_output_plan(
        app, client,
        planner_id=planner_id,
        run_ids=run_ids,
        quantities=("10.000", "10.000"),
    )
    assignment_id = order["asignaciones"][0]["id"]
    oversized = _record_output_reading(
        client,
        actor_id=planner_id,
        order=order,
        assignment_id=assignment_id,
        net="15.000",
        evidence="OUT-ADVERSE-15",
    )
    assert oversized.status_code == 409
    assert (
        oversized.get_json()["error"]["code"]
        == "OPM_OUTPUT_BAG_EXCEEDS_ASSIGNMENT"
    )
    accepted = _record_output_reading(
        client,
        actor_id=planner_id,
        order=order,
        assignment_id=assignment_id,
        net="5.000",
        evidence="OUT-ADVERSE-5",
    )
    assert accepted.status_code == 201, accepted.get_json()


def test_opm_consolida_dos_corridas_compatibles_sin_sobrecobertura(
    app, client, scm_config
):
    with app.app_context():
        planner_id, recipe_id, run_ids, _resin_id = _seed_two_jar_runs_with_one_recipe()

    requirement_ids = []
    for run_id in run_ids:
        generated = client.post(
            "/api/scm/v1/requerimientos-preparacion/calcular",
            headers=_headers(planner_id),
            json={"corrida_fabricacion_id": str(run_id)},
        )
        assert generated.status_code == 200, generated.get_json()
        body = generated.get_json()
        assert body["estado"] == "PENDIENTE"
        assert body["receta_revision_id"] == recipe_id
        assert body["cantidad_requerida_kg"] == "11.000"
        assert body["pendiente_kg"] == "11.000"
        requirement_ids.append(body["id"])

        recalculated = client.post(
            "/api/scm/v1/requerimientos-preparacion/calcular",
            headers=_headers(planner_id),
            json={"corrida_fabricacion_id": str(run_id)},
        )
        assert recalculated.status_code == 200, recalculated.get_json()
        assert recalculated.get_json()["id"] == body["id"]

    created = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planner_id),
        json={
            "motivo": "Preparar juntas dos corridas de Jarra 6L",
            "coberturas": [
                {"requerimiento_id": value, "cantidad_kg": "11.000"}
                for value in requirement_ids
            ],
        },
    )
    assert created.status_code == 201, created.get_json()
    body = created.get_json()
    assert body["estado"] == "BORRADOR"
    assert body["receta_revision_id"] == recipe_id
    assert body["cantidad_objetivo_kg"] == "22.000"
    assert [
        item["cantidad_planificada_kg"] for item in body["asignaciones"]
    ] == [
        "11.000",
        "11.000",
    ]

    duplicated = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planner_id),
        json={
            "motivo": "No debe duplicar cobertura",
            "coberturas": [
                {
                    "requerimiento_id": requirement_ids[0],
                    "cantidad_kg": "0.001",
                }
            ],
        },
    )
    assert duplicated.status_code == 409
    assert duplicated.get_json()["error"]["code"] == "PREPARED_REQUIREMENT_ALREADY_COVERED"


def test_colas_cursor_y_destino_operacional_global_son_canónicos(
    app, client, scm_config
):
    with app.app_context():
        planner_id, _, run_ids, resin_id = _seed_two_jar_runs_with_one_recipe()
        actors = _seed_l1_actors_and_stock(
            planner_id=planner_id, resin_id=resin_id,
        )

    eligible_page_1 = client.get(
        "/api/scm/v1/corridas-fabricacion/elegibles-preparacion?limit=1",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert eligible_page_1.status_code == 200, eligible_page_1.get_json()
    page_1 = eligible_page_1.get_json()
    assert len(page_1["items"]) == 1
    assert page_1["items"][0]["tipo"] == "CORRIDA_ELEGIBLE"
    assert page_1["has_more"] is True
    assert page_1["next_cursor"]
    eligible_page_2 = client.get(
        "/api/scm/v1/corridas-fabricacion/elegibles-preparacion",
        query_string={"limit": 1, "cursor": page_1["next_cursor"]},
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert eligible_page_2.status_code == 200, eligible_page_2.get_json()
    assert eligible_page_2.get_json()["items"][0]["id"] != page_1["items"][0]["id"]

    order = _calculate_and_propose(
        client, planner_id=planner_id, run_ids=run_ids,
    )
    requirements_page_1 = client.get(
        "/api/scm/v1/requerimientos-preparacion?limit=1",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert requirements_page_1.status_code == 200, requirements_page_1.get_json()
    req_page_1 = requirements_page_1.get_json()
    assert len(req_page_1["items"]) == 1
    assert "composicion" not in req_page_1["items"][0]
    assert "asignaciones" not in req_page_1["items"][0]
    assert req_page_1["has_more"] is True
    requirements_page_2 = client.get(
        "/api/scm/v1/requerimientos-preparacion",
        query_string={"limit": 1, "cursor": req_page_1["next_cursor"]},
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert requirements_page_2.status_code == 200, requirements_page_2.get_json()
    assert (
        requirements_page_2.get_json()["items"][0]["id"]
        != req_page_1["items"][0]["id"]
    )
    statements = []
    with app.app_context():
        engine = db.engine

    def capture_statement(
        _connection, _cursor, statement, _parameters, _context, _executemany,
    ):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        orders = client.get(
            "/api/scm/v1/ordenes-preparacion-material?limit=25",
            headers={"X-Actor-Id": str(planner_id)},
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)
    assert orders.status_code == 200, orders.get_json()
    assert len(statements) <= 8
    assert orders.get_json()["items"][0]["id"] == order["id"]
    order_summary = orders.get_json()["items"][0]
    assert len(order_summary["asignaciones"]) == 2
    assert set(order_summary["asignaciones"][0]) == {
        "id",
        "requerimiento_id",
        "tipo_fuente",
        "cantidad_planificada_kg",
        "cantidad_comprometida_kg",
        "cantidad_consumida_kg",
        "estado",
    }
    assert "requerimiento" not in order_summary["asignaciones"][0]

    destinations = client.get(
        "/api/scm/v1/ubicaciones-material-preparado/destinos",
        headers={"X-Actor-Id": str(actors["preparer_id"])},
    )
    assert destinations.status_code == 200, destinations.get_json()
    by_code = {value["codigo"]: value for value in destinations.get_json()["items"]}
    assert by_code["P-ENVA-INY-01"] == {
        "id": actors["production_point_id"],
        "codigo": "P-ENVA-INY-01",
        "nombre": "Punto de consumo Inyectora 01",
        "tipo": "PUNTO_PRODUCCION",
        "almacen": None,
        "permite_saldo_libre": False,
        "seleccionable_como_stock": False,
        "usos": ["ENTREGA_PRODUCCION"],
    }
    assert by_code["A-ENVA-MP-MEZ"]["seleccionable_como_stock"] is True


def test_stock_lmp_compatible_se_compromete_antes_de_proponer_opm(
    app, client, scm_config
):
    with app.app_context():
        planner_id, recipe_id, run_ids, resin_id = _seed_two_jar_runs_with_one_recipe()
        actors = _seed_l1_actors_and_stock(
            planner_id=planner_id, resin_id=resin_id,
        )
    generated = client.post(
        "/api/scm/v1/requerimientos-preparacion/calcular",
        headers=_headers(planner_id),
        json={"corrida_fabricacion_id": str(run_ids[0])},
    )
    assert generated.status_code == 200, generated.get_json()
    requirement_id = generated.get_json()["id"]
    with app.app_context():
        requirement = db.session.get(
            ScmRequerimientoMaterialPreparado, UUID(requirement_id),
        )
        requirement.cantidad_requerida_kg = Decimal("25.000")
        requirement.version += 1
        bag_id = _seed_free_prepared_bag(
            planner_id=planner_id, recipe_id=recipe_id,
            composition_hash=requirement.composicion_hash,
            location_id=actors["prepared_storage_id"], quantity="10.000",
        )
        planning_only_role = RolOperativo(
            codigo="PLAN-OPM-SIN-STOCK",
            nombre="Planificador OPM sin autoridad de stock",
        )
        planning_only_role.capacidades = ScmCapacidad.query.filter(
            ScmCapacidad.codigo.in_(("OPM_VER", "OPM_CREAR"))
        ).all()
        planning_only = Trabajador(
            codigo="TRB-OPM-PLAN-ONLY",
            nombres="Planificador",
            apellidos="Sin Reserva",
            activo=True,
        )
        planning_only.roles.append(planning_only_role)
        db.session.add_all([planning_only_role, planning_only])
        db.session.commit()
        planning_only_id = planning_only.id

    blocked = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planning_only_id),
        json={
            "motivo": "No reservar stock de forma silenciosa",
            "coberturas": [{
                "requerimiento_id": requirement_id,
                "cantidad_kg": "25.000",
            }],
        },
    )
    assert blocked.status_code == 409, blocked.get_json()
    assert (
        blocked.get_json()["error"]["code"]
        == "PREPARED_STOCK_DECISION_REQUIRED"
    )
    with app.app_context():
        bag = db.session.get(ScmBolsaMaterialPreparado, bag_id)
        balance = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
        ).one()
        requirement = db.session.get(
            ScmRequerimientoMaterialPreparado, UUID(requirement_id),
        )
        assert bag.estado == "DISPONIBLE"
        assert Decimal(balance.cantidad_reservada_kg) == Decimal("0.000")
        requirement_version = requirement.version

    forbidden_assignment = client.post(
        f"/api/scm/v1/requerimientos-preparacion/{requirement_id}/asignaciones-stock",
        headers=_headers(planning_only_id),
        json={
            "version": requirement_version,
            "bolsa_ids": [str(bag_id)],
            "motivo": "No debe reservar sin capacidad",
        },
    )
    assert forbidden_assignment.status_code == 403
    with app.app_context():
        bag = db.session.get(ScmBolsaMaterialPreparado, bag_id)
        balance = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
        ).one()
        assert bag.estado == "DISPONIBLE"
        assert Decimal(balance.cantidad_reservada_kg) == Decimal("0.000")

    candidates = client.get(
        f"/api/scm/v1/requerimientos-preparacion/{requirement_id}/stock-compatible",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert candidates.status_code == 200, candidates.get_json()
    assert [value["id"] for value in candidates.get_json()["items"]] == [
        str(bag_id)
    ]
    assigned = client.post(
        f"/api/scm/v1/requerimientos-preparacion/{requirement_id}/asignaciones-stock",
        headers=_headers(planner_id),
        json={
            "version": requirement_version,
            "bolsa_ids": [str(bag_id)],
            "motivo": "Asignacion explicita de una bolsa completa",
        },
    )
    assert assigned.status_code == 201, assigned.get_json()
    assert (
        assigned.get_json()["asignaciones"][0]["cantidad_comprometida_kg"]
        == "10.000"
    )
    assert (
        assigned.get_json()["requerimiento"]["pendiente_planificacion_kg"]
        == "15.000"
    )
    hidden_while_active = client.get(
        f"/api/scm/v1/requerimientos-preparacion/{requirement_id}/stock-compatible",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert hidden_while_active.status_code == 200
    assert hidden_while_active.get_json()["items"] == []

    proposed = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planner_id),
        json={
            "motivo": "Preparar solo el saldo no cubierto por stock",
            "coberturas": [{
                "requerimiento_id": requirement_id,
                "cantidad_kg": "15.000",
            }],
        },
    )
    assert proposed.status_code == 201, proposed.get_json()
    order = proposed.get_json()
    assert order["cantidad_objetivo_kg"] == "15.000"
    assert order["asignaciones"][0]["cantidad_planificada_kg"] == "15.000"
    with app.app_context():
        bag = db.session.get(ScmBolsaMaterialPreparado, bag_id)
        balance = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
        ).one()
        assignments = ScmAsignacionRequerimientoPreparacion.query.filter_by(
            requerimiento_id=UUID(requirement_id),
        ).all()
        assert bag.estado == "DISPONIBLE"
        assert Decimal(balance.cantidad_reservada_kg) == Decimal("0.000")
        assert sorted(value.tipo_fuente for value in assignments) == [
            "LOTE_PREPARADO_STOCK", "OPM_ESPERADA",
        ]

    duplicate = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planner_id),
        json={
            "motivo": "No prometer dos veces la misma necesidad",
            "coberturas": [{
                "requerimiento_id": requirement_id,
                "cantidad_kg": "0.001",
            }],
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"]["code"] == "PREPARED_REQUIREMENT_ALREADY_COVERED"

    release_headers = _headers(planner_id)
    released_stock = client.post(
        "/api/scm/v1/asignaciones-stock-material-preparado/"
        f"{assigned.get_json()['asignaciones'][0]['id']}/liberar",
        headers=release_headers,
        json={"motivo": "La bolsa sera destinada a otra corrida"},
    )
    assert released_stock.status_code == 200, released_stock.get_json()
    assert released_stock.get_json()["asignacion"]["estado"] == "LIBERADA"
    replay_release = client.post(
        "/api/scm/v1/asignaciones-stock-material-preparado/"
        f"{assigned.get_json()['asignaciones'][0]['id']}/liberar",
        headers=release_headers,
        json={"motivo": "La bolsa sera destinada a otra corrida"},
    )
    assert replay_release.status_code == 200
    visible_after_release = client.get(
        f"/api/scm/v1/requerimientos-preparacion/{requirement_id}/stock-compatible",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert visible_after_release.status_code == 200
    assert [
        value["id"] for value in visible_after_release.get_json()["items"]
    ] == [str(bag_id)]
    with app.app_context():
        bag = db.session.get(ScmBolsaMaterialPreparado, bag_id)
        balance = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
        ).one()
        assert bag.estado == "DISPONIBLE"
        assert Decimal(balance.cantidad_reservada_kg) == Decimal("0.000")


def test_l2_bolsa_completa_custodia_consumo_retorno_y_reuso_sin_doble_saldo(
    app, client, scm_config,
):
    with app.app_context():
        planner_id, recipe_id, run_ids, resin_id = _seed_two_jar_runs_with_one_recipe()
        actors = _seed_l1_actors_and_stock(
            planner_id=planner_id, resin_id=resin_id,
        )
    generated = client.post(
        "/api/scm/v1/requerimientos-preparacion/calcular",
        headers=_headers(planner_id),
        json={"corrida_fabricacion_id": str(run_ids[0])},
    )
    assert generated.status_code == 200, generated.get_json()
    requirement_id = generated.get_json()["id"]
    with app.app_context():
        requirement = db.session.get(
            ScmRequerimientoMaterialPreparado, UUID(requirement_id),
        )
        requirement.cantidad_requerida_kg = Decimal("20.000")
        requirement.version += 1
        bag_ids = [
            _seed_free_prepared_bag(
                planner_id=planner_id,
                recipe_id=recipe_id,
                composition_hash=requirement.composicion_hash,
                location_id=actors["prepared_storage_id"],
                quantity="10.000",
            )
            for _value in range(2)
        ]
        work_id = _seed_work_color(planner_id=planner_id, run_id=run_ids[0])
        return_work_id = _seed_work_color(
            planner_id=planner_id, run_id=run_ids[0],
        )
        foreign_work_id = _seed_work_color(
            planner_id=planner_id, run_id=run_ids[1],
        )
        active_work = db.session.get(ScmTrabajoOt, work_id)
        active_machine = db.session.get(
            Maquina, active_work.orden_trabajo.maquina_id,
        )
        production_point = db.session.get(
            ScmUbicacionInventario, actors["production_point_id"],
        )
        production_point.maquina_id = active_machine.id
        machine_code = active_machine.codigo
        other_machine = Maquina(
            codigo="INY-02", nombre="Inyectora 2",
            tipo_maquina_id=active_machine.tipo_maquina_id,
            estado="OPERATIVA", activo=True,
        )
        db.session.add(other_machine)
        db.session.flush()
        db.session.add(ScmUbicacionInventario(
            almacen_id=None, maquina_id=other_machine.id,
            codigo="P-ENVA-INY-02", nombre="Punto de consumo Inyectora 02",
            tipo="PUNTO_PRODUCCION", permite_saldo_libre=False,
            clases_articulo_json=["MATERIAL_PREPARADO"],
        ))
        requirement_version = requirement.version
        db.session.commit()

    assigned = client.post(
        f"/api/scm/v1/requerimientos-preparacion/{requirement_id}/asignaciones-stock",
        headers=_headers(planner_id),
        json={
            "version": requirement_version,
            "bolsa_ids": [str(value) for value in bag_ids],
            "motivo": "Cobertura de dos bolsas completas",
        },
    )
    assert assigned.status_code == 201, assigned.get_json()
    assignments = assigned.get_json()["asignaciones"]
    assert len(assignments) == 2
    assignment_by_bag = {
        value["bolsa_id"]: value for value in assignments
    }
    first_assignment = assignment_by_bag[str(bag_ids[0])]
    second_assignment = assignment_by_bag[str(bag_ids[1])]

    foreign = client.post(
        f"/api/scm/v1/trabajos-color/{foreign_work_id}/reservas-material-preparado",
        headers=_headers(planner_id),
        json={
            "asignacion_id": first_assignment["id"],
            "bolsa_id": str(bag_ids[0]),
            "motivo": "No corresponde a esta corrida",
        },
    )
    assert foreign.status_code == 409, foreign.get_json()
    assert foreign.get_json()["error"]["code"] == "PREPARED_ASSIGNMENT_NOT_ELIGIBLE"

    reserved = _post_with_replay(
        client,
        f"/api/scm/v1/trabajos-color/{work_id}/reservas-material-preparado",
        actor_id=planner_id,
        body={
            "asignacion_id": first_assignment["id"],
            "bolsa_id": str(bag_ids[0]),
            "motivo": "Reservar bolsa completa para el trabajo",
        },
    )
    assert reserved.status_code == 201, reserved.get_json()
    reservation = reserved.get_json()
    with app.app_context():
        active_work = db.session.get(ScmTrabajoOt, work_id)
        active_work_version = active_work.version
    blocked_completion = client.post(
        f"/api/scm/v1/trabajos-color/{work_id}/completar",
        headers=_headers(planner_id),
        json={"version": active_work_version},
    )
    assert blocked_completion.status_code == 409
    assert (
        blocked_completion.get_json()["error"]["code"]
        == "WORK_HAS_ACTIVE_PREPARED_MATERIAL"
    )
    with app.app_context():
        source = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
        ).one()
        assert Decimal(source.cantidad_fisica_kg) == Decimal("20.000")
        assert Decimal(source.cantidad_reservada_kg) == Decimal("10.000")

    prepared = client.post(
        "/api/scm/v1/reservas-material-preparado/"
        f"{reservation['id']}/preparar-entrega",
        headers=_headers(actors["warehouse_id"]),
        json={
            "version": reservation["version"],
            "ubicacion_destino_id": actors["production_point_id"],
            "motivo": "Preparar entrega al punto de maquina",
        },
    )
    assert prepared.status_code == 201, prepared.get_json()
    delivery = prepared.get_json()["entrega"]

    dispatched = _post_with_replay(
        client,
        f"/api/scm/v1/entregas-material-preparado/{delivery['id']}/despachar",
        actor_id=actors["warehouse_id"],
        body={
            "version": delivery["version"],
            "motivo": "Bolsa sale bajo custodia hacia la maquina",
        },
    )
    assert dispatched.status_code == 200, dispatched.get_json()
    delivery = dispatched.get_json()["entrega"]
    assert delivery["estado"] == "EN_TRANSITO"
    with app.app_context():
        source = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
        ).one()
        destination = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["production_point_id"],
        ).one()
        assert Decimal(source.cantidad_fisica_kg) == Decimal("10.000")
        assert Decimal(source.cantidad_reservada_kg) == Decimal("0.000")
        assert Decimal(destination.cantidad_fisica_kg) == Decimal("10.000")
        assert Decimal(destination.cantidad_reservada_kg) == Decimal("10.000")

    before_receipt = client.post(
        f"/api/scm/v1/trabajos-color/{work_id}/consumos-material-preparado",
        headers=_headers(planner_id),
        json={
            "entrega_id": delivery["id"],
            "version": delivery["version"],
            "motivo": "No se puede consumir antes de recibir",
        },
    )
    assert before_receipt.status_code == 409, before_receipt.get_json()
    assert before_receipt.get_json()["error"]["code"] == "LMP_DELIVERY_NOT_RECEIVED"

    invalid_bag_qr = client.post(
        "/api/scm/v1/recepciones-material-preparado/resolver-qr",
        headers={"X-Actor-Id": str(planner_id)},
        json={
            "maquina_qr": f"SCM:MAQUINA:{machine_code}:V1",
            "bolsa_qr": "SCM:BMP:no-es-un-uuid",
        },
    )
    assert invalid_bag_qr.status_code == 404, invalid_bag_qr.get_json()
    assert (
        invalid_bag_qr.get_json()["error"]["code"]
        == "PREPARED_BAG_QR_NOT_FOUND"
    )

    mismatch = client.post(
        "/api/scm/v1/recepciones-material-preparado/resolver-qr",
        headers={"X-Actor-Id": str(planner_id)},
        json={
            "maquina_qr": "SCM:MAQUINA:INY-02:V1",
            "bolsa_qr": reserved.get_json()["bolsa"]["qr_value"],
        },
    )
    assert mismatch.status_code == 409, mismatch.get_json()
    assert (
        mismatch.get_json()["error"]["code"]
        == "PREPARED_DELIVERY_MACHINE_MISMATCH"
    )
    with app.app_context():
        unchanged = db.session.get(
            ScmEmisionMaterialPreparado, UUID(delivery["id"]),
        )
        assert unchanged.estado == "EN_TRANSITO"
        assert unchanged.recepcion_metodo is None

    resolved = client.post(
        "/api/scm/v1/recepciones-material-preparado/resolver-qr",
        headers={"X-Actor-Id": str(planner_id)},
        json={
            "maquina_qr": f"SCM:MAQUINA:{machine_code}:V1",
            "bolsa_qr": reserved.get_json()["bolsa"]["qr_value"],
        },
    )
    assert resolved.status_code == 200, resolved.get_json()
    assert resolved.get_json()["maquina"]["codigo"] == machine_code
    assert (
        resolved.get_json()["bolsa"]["qr_value"]
        == reserved.get_json()["bolsa"]["qr_value"]
    )
    assert resolved.get_json()["acciones_permitidas"]["recibir_y_consumir"] is True

    received = client.post(
        "/api/scm/v1/recepciones-material-preparado/confirmar-qr",
        headers=_headers(planner_id),
        json={
            "maquina_qr": f"SCM:MAQUINA:{machine_code}:V1",
            "bolsa_qr": reserved.get_json()["bolsa"]["qr_value"],
            "entrega_id": delivery["id"],
            "expected_version": delivery["version"],
            "accion": "RECIBIR_Y_CONSUMIR",
            "motivo": "Maquinista recibe e incorpora la bolsa completa",
        },
    )
    assert received.status_code == 200, received.get_json()
    delivery = received.get_json()["entrega"]
    assert delivery["recepcion_qr"]["maquina"]["codigo"] == machine_code
    assert (
        delivery["recepcion_qr"]["bolsa_qr"]
        == reserved.get_json()["bolsa"]["qr_value"]
    )
    assert received.get_json()["accion"] == "RECIBIR_Y_CONSUMIR"
    assert received.get_json()["reserva"]["estado"] == "CONSUMIDA"
    assert delivery["estado"] == "CERRADA"
    with app.app_context():
        persisted_delivery = db.session.get(
            ScmEmisionMaterialPreparado, UUID(delivery["id"]),
        )
        assert (
            persisted_delivery.bolsa_qr_snapshot
            == reserved.get_json()["bolsa"]["qr_value"]
        )
        destination = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["production_point_id"],
        ).one()
        assert Decimal(destination.cantidad_fisica_kg) == Decimal("0.000")
        assert Decimal(destination.cantidad_reservada_kg) == Decimal("0.000")

    with app.app_context():
        completed_work = db.session.get(ScmTrabajoOt, work_id)
        completed_work_version = completed_work.version
    completed = client.post(
        f"/api/scm/v1/trabajos-color/{work_id}/completar",
        headers=_headers(planner_id),
        json={"version": completed_work_version},
    )
    assert completed.status_code == 200, completed.get_json()
    assert completed.get_json()["trabajo_color"]["estado"] == "COMPLETADO"
    completed_reservation = client.post(
        f"/api/scm/v1/trabajos-color/{work_id}/reservas-material-preparado",
        headers=_headers(planner_id),
        json={
            "asignacion_id": second_assignment["id"],
            "bolsa_id": str(bag_ids[1]),
            "motivo": "No reservar sobre trabajo completado",
        },
    )
    assert completed_reservation.status_code == 409
    assert (
        completed_reservation.get_json()["error"]["code"]
        == "WORK_COLOR_NOT_RESERVABLE"
    )

    second_reserve = client.post(
        f"/api/scm/v1/trabajos-color/{return_work_id}/reservas-material-preparado",
        headers=_headers(planner_id),
        json={
            "asignacion_id": second_assignment["id"],
            "bolsa_id": str(bag_ids[1]),
            "motivo": "Segunda bolsa para probar retorno",
        },
    )
    assert second_reserve.status_code == 201, second_reserve.get_json()
    second_reservation = second_reserve.get_json()
    second_prepared = client.post(
        "/api/scm/v1/reservas-material-preparado/"
        f"{second_reservation['id']}/preparar-entrega",
        headers=_headers(actors["warehouse_id"]),
        json={
            "version": second_reservation["version"],
            "ubicacion_destino_id": actors["production_point_id"],
            "motivo": "Preparar segunda entrega",
        },
    )
    second_delivery = second_prepared.get_json()["entrega"]
    second_dispatched = client.post(
        f"/api/scm/v1/entregas-material-preparado/{second_delivery['id']}/despachar",
        headers=_headers(actors["warehouse_id"]),
        json={"version": second_delivery["version"], "motivo": "Despachar"},
    )
    assert second_dispatched.status_code == 200, second_dispatched.get_json()
    second_delivery = second_dispatched.get_json()["entrega"]
    with app.app_context():
        return_work = db.session.get(ScmTrabajoOt, return_work_id)
        return_work_version = return_work.version
    paused = client.post(
        f"/api/scm/v1/trabajos-color/{return_work_id}/pausar",
        headers=_headers(planner_id),
        json={
            "version": return_work_version,
            "motivo": "Pausa para evaluar anulacion con bolsa en custodia",
        },
    )
    assert paused.status_code == 200, paused.get_json()
    rejected_annulment = client.post(
        f"/api/scm/v1/trabajos-color/{return_work_id}/anular",
        headers=_headers(planner_id),
        json={
            "version": paused.get_json()["trabajo_color"]["version"],
            "motivo": "No debe anular una entrega en transito",
        },
    )
    assert rejected_annulment.status_code == 409
    assert (
        rejected_annulment.get_json()["error"]["code"]
        == "PREPARED_RESERVATION_RETURN_REQUIRED"
    )
    with app.app_context():
        persisted_reservation = db.session.get(
            ScmReservaMaterialPreparado, UUID(second_reservation["id"]),
        )
        persisted_delivery = db.session.get(
            ScmEmisionMaterialPreparado, UUID(second_delivery["id"]),
        )
        destination = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["production_point_id"],
        ).one()
        assert persisted_reservation.estado == "ACTIVA"
        assert persisted_delivery.estado == "EN_TRANSITO"
        assert Decimal(destination.cantidad_reservada_kg) == Decimal("10.000")
    second_received = client.post(
        "/api/scm/v1/recepciones-material-preparado/confirmar-qr",
        headers=_headers(planner_id),
        json={
            "maquina_qr": f"SCM:MAQUINA:{machine_code}:V1",
            "bolsa_qr": second_reservation["bolsa"]["codigo"],
            "entrega_id": second_delivery["id"],
            "expected_version": second_delivery["version"],
            "accion": "RECIBIR",
            "motivo": "Recibir para conservar custodia sin consumo",
        },
    )
    assert second_received.status_code == 200, second_received.get_json()
    second_delivery = second_received.get_json()["entrega"]
    assert (
        second_delivery["recepcion_qr"]["bolsa_qr"]
        == second_reservation["bolsa"]["codigo"]
    )
    returned = client.post(
        f"/api/scm/v1/entregas-material-preparado/{second_delivery['id']}/retornar",
        headers=_headers(actors["warehouse_id"]),
        json={
            "version": second_delivery["version"],
            "ubicacion_retorno_id": actors["prepared_storage_id"],
            "motivo": "Bolsa integra no consumida retorna al almacen",
        },
    )
    assert returned.status_code == 200, returned.get_json()
    assert returned.get_json()["reserva"]["estado"] == "DEVUELTA"
    assert returned.get_json()["reserva"]["bolsa"]["estado"] == "DISPONIBLE"

    released = client.post(
        "/api/scm/v1/reservas-material-preparado/"
        f"{second_reservation['id']}/liberar",
        headers=_headers(planner_id),
        json={
            "version": returned.get_json()["reserva"]["version"],
            "motivo": "Cerrar reserva retornada sin alterar cobertura",
        },
    )
    assert released.status_code == 200, released.get_json()
    assert released.get_json()["reserva"]["estado"] == "LIBERADA"
    assert released.get_json()["asignacion"]["estado"] == "COMPROMETIDA"

    reused = client.post(
        f"/api/scm/v1/trabajos-color/{return_work_id}/reservas-material-preparado",
        headers=_headers(planner_id),
        json={
            "asignacion_id": second_assignment["id"],
            "bolsa_id": str(bag_ids[1]),
            "motivo": "La misma cobertura vuelve a reservar su bolsa",
        },
    )
    assert reused.status_code == 201, reused.get_json()
    with app.app_context():
        source = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
        ).one()
        assert Decimal(source.cantidad_fisica_kg) == Decimal("10.000")
        assert Decimal(source.cantidad_reservada_kg) == Decimal("10.000")
        return_work = db.session.get(ScmTrabajoOt, return_work_id)
        return_work_version = return_work.version
    annulled = client.post(
        f"/api/scm/v1/trabajos-color/{return_work_id}/anular",
        headers=_headers(planner_id),
        json={
            "version": return_work_version,
            "motivo": "Cancelar trabajo antes de despachar la bolsa reservada",
        },
    )
    assert annulled.status_code == 200, annulled.get_json()
    assert annulled.get_json()["trabajo_color"]["estado"] == "ANULADO"
    with app.app_context():
        reused_reservation = db.session.get(
            ScmReservaMaterialPreparado, UUID(reused.get_json()["id"]),
        )
        bag = db.session.get(ScmBolsaMaterialPreparado, bag_ids[1])
        source = ScmSaldoMaterialPreparado.query.filter_by(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
        ).one()
        assert reused_reservation.estado == "CANCELADA"
        assert bag.estado == "DISPONIBLE"
        assert Decimal(source.cantidad_reservada_kg) == Decimal("0.000")

    consumed_genealogy = client.get(
        f"/api/scm/v1/lotes-material-preparado/{first_assignment['lote_id']}",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert consumed_genealogy.status_code == 200, consumed_genealogy.get_json()
    consumed_graph = consumed_genealogy.get_json()["genealogia"]
    consumed_bag_graph = consumed_graph["bolsas"][0]
    assert consumed_bag_graph["reservas_y_entregas"][0]["resultado"] == "CONSUMO"
    assert (
        consumed_bag_graph["reservas_y_entregas"][0]["recepcion_maquina"]
        is not None
    )
    assert {
        value["tipo"] for value in consumed_bag_graph["movimientos"]
    } >= {"RESERVA", "EMISION_SALIDA", "EMISION_ENTRADA", "CONSUMO"}

    return_genealogy = client.get(
        f"/api/scm/v1/lotes-material-preparado/{second_assignment['lote_id']}",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert return_genealogy.status_code == 200, return_genealogy.get_json()
    return_movements = {
        value["tipo"]
        for value in return_genealogy.get_json()["genealogia"]["movimientos"]
    }
    assert return_movements >= {
        "RETORNO_SALIDA", "RETORNO_ENTRADA", "LIBERACION_RESERVA",
    }

    reservations_page = client.get(
        f"/api/scm/v1/trabajos-color/{return_work_id}/"
        "reservas-material-preparado?limit=1",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert reservations_page.status_code == 200, reservations_page.get_json()
    reservations_page_body = reservations_page.get_json()
    assert len(reservations_page_body["items"]) == 1
    assert reservations_page_body["has_more"] is True
    assert reservations_page_body["next_cursor"]
    assert "decisiones_calidad" not in reservations_page_body["items"][0]["bolsa"]
    second_page = client.get(
        f"/api/scm/v1/trabajos-color/{return_work_id}/"
        "reservas-material-preparado?limit=1&cursor="
        f"{reservations_page_body['next_cursor']}",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert second_page.status_code == 200, second_page.get_json()
    assert len(second_page.get_json()["items"]) == 1


def test_anular_opm_libera_plan_y_reservas_raw_sin_borrar_trazabilidad(
    app, client, scm_config,
):
    with app.app_context():
        planner_id, _recipe_id, run_ids, resin_id = _seed_two_jar_runs_with_one_recipe()
        actors = _seed_l1_actors_and_stock(
            planner_id=planner_id, resin_id=resin_id,
        )
    draft = _calculate_and_propose(
        client, planner_id=planner_id, run_ids=run_ids,
    )
    cancel_headers = _headers(planner_id)
    cancel_body = {
        "version": draft["version"],
        "motivo": "Plan duplicado detectado antes de liberar",
    }
    cancelled = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{draft['id']}/anular",
        headers=cancel_headers,
        json=cancel_body,
    )
    assert cancelled.status_code == 200, cancelled.get_json()
    assert cancelled.get_json()["estado"] == "ANULADA"
    replay = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{draft['id']}/anular",
        headers=cancel_headers,
        json=cancel_body,
    )
    assert replay.status_code == 200
    assert replay.get_json()["id"] == draft["id"]

    replacement = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planner_id),
        json={
            "motivo": "Replanificacion posterior a anulacion",
            "coberturas": [
                {
                    "requerimiento_id": value["requerimiento_id"],
                    "cantidad_kg": value["cantidad_planificada_kg"],
                }
                for value in draft["asignaciones"]
            ],
        },
    )
    assert replacement.status_code == 201, replacement.get_json()
    replacement_body = replacement.get_json()
    released = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{replacement_body['id']}/liberar",
        headers=_headers(planner_id),
        json={
            "version": replacement_body["version"],
            "motivo": "Liberar plan corregido",
        },
    )
    assert released.status_code == 200, released.get_json()
    reserved = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{replacement_body['id']}/reservar-insumos",
        headers=_headers(actors["preparer_id"]),
        json={
            "version": released.get_json()["version"],
            "ubicacion_origen_ids": [actors["source_id"]],
        },
    )
    assert reserved.status_code == 200, reserved.get_json()
    with app.app_context():
        raw_balance = ScmSaldoMaterialInventario.query.filter_by(
            material_id=resin_id,
            ubicacion_id=actors["source_id"],
        ).one()
        assert Decimal(raw_balance.cantidad_reservada_kg) == Decimal("22.000")

    cancelled_released = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{replacement_body['id']}/anular",
        headers=_headers(planner_id),
        json={
            "version": reserved.get_json()["version"],
            "motivo": "Cancelar antes de incorporar material",
        },
    )
    assert cancelled_released.status_code == 200, cancelled_released.get_json()
    assert cancelled_released.get_json()["estado"] == "ANULADA"
    with app.app_context():
        raw_balance = ScmSaldoMaterialInventario.query.filter_by(
            material_id=resin_id,
            ubicacion_id=actors["source_id"],
        ).one()
        assert Decimal(raw_balance.cantidad_reservada_kg) == Decimal("0.000")
        cancelled_assignments = ScmAsignacionRequerimientoPreparacion.query.filter_by(
            orden_preparacion_id=UUID(replacement_body["id"]),
        ).all()
        assert {value.estado for value in cancelled_assignments} == {"CANCELADA"}


def test_rechazo_de_bolsa_reabre_solo_su_asignacion_sin_borrar_plan(
    app, client, scm_config
):
    with app.app_context():
        planner_id, recipe_id, run_ids, resin_id = _seed_two_jar_runs_with_one_recipe()
        actors = _seed_l1_actors_and_stock(
            planner_id=planner_id, resin_id=resin_id,
        )
    order_body = _calculate_and_propose(
        client, planner_id=planner_id, run_ids=run_ids,
    )
    released = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_body['id']}/liberar",
        headers=_headers(planner_id),
        json={
            "version": order_body["version"],
            "motivo": "Comprometer asignaciones para prueba de rechazo",
        },
    )
    assert released.status_code == 200, released.get_json()
    with app.app_context():
        order = db.session.get(
            ScmOrdenPreparacionMaterial, UUID(order_body["id"]),
        )
        order.estado = "CERRADA"
        order.closed_by_id = planner_id
        order.version += 1
        order_assignments = (
            ScmAsignacionRequerimientoPreparacion.query
            .filter_by(orden_preparacion_id=order.id)
            .order_by(ScmAsignacionRequerimientoPreparacion.id)
            .all()
        )
        lot = ScmLoteMaterialPreparado(
            orden=order, codigo=f"LMP-RECH-{str(uuid4())[:8].upper()}",
            receta_revision_id=recipe_id, cantidad_kg=Decimal("22.000"),
            estado="PENDIENTE_CALIDAD", created_by_id=planner_id,
        )
        bags = []
        for sequence in (1, 2):
            reading = ScmLecturaPesoPreparacion(
                orden=order, tipo_uso="BOLSA_SALIDA",
                peso_bruto_kg=Decimal("11.100"), tara_kg=Decimal("0.100"),
                peso_neto_kg=Decimal("11.000"), metodo="CONTINGENCIA_MANUAL",
                evidencia_ref=f"RECHAZO-PARCIAL-{sequence}",
                motivo="Bolsa para decision parcial", estado="UTILIZADA",
                created_by_id=planner_id, operation_id=uuid4(),
            )
            bag = ScmBolsaMaterialPreparado(
                orden=order, lote=lot, lectura=reading,
                codigo=f"BMP-RECH-{str(uuid4())[:8].upper()}",
                secuencia=sequence,
                peso_bruto_kg=Decimal("11.100"), tara_kg=Decimal("0.100"),
                peso_neto_kg=Decimal("11.000"), metodo="CONTINGENCIA_MANUAL",
                evidencia_ref=f"RECHAZO-PARCIAL-{sequence}",
                motivo="Bolsa para decision parcial",
                estado="PENDIENTE_CALIDAD",
                ubicacion_id=actors["prepared_storage_id"],
                created_by_id=planner_id, confirmed_by_id=planner_id,
                operation_id=uuid4(),
                asignacion_requerimiento=order_assignments[sequence - 1],
            )
            db.session.add_all([reading, bag])
            bags.append(bag)
        balance = ScmSaldoMaterialPreparado(
            receta_revision_id=recipe_id,
            ubicacion_id=actors["prepared_storage_id"],
            cantidad_fisica_kg=Decimal("22.000"),
            cantidad_reservada_kg=Decimal("0.000"),
            cantidad_no_disponible_kg=Decimal("22.000"),
        )
        db.session.add_all([lot, balance])
        db.session.commit()
        lot_id = lot.id
        rejected_bag_id = bags[0].id
        accepted_bag_id = bags[1].id

    rejected = client.post(
        f"/api/scm/v1/lotes-material-preparado/{lot_id}/"
        f"bolsas/{rejected_bag_id}/calidad",
        headers=_headers(actors["quality_id"]),
        json={"decision": "RECHAZAR", "motivo": "Bolsa no conforme"},
    )
    assert rejected.status_code == 200, rejected.get_json()
    accepted = client.post(
        f"/api/scm/v1/lotes-material-preparado/{lot_id}/"
        f"bolsas/{accepted_bag_id}/calidad",
        headers=_headers(actors["quality_id"]),
        json={"decision": "LIBERAR", "motivo": "Segunda bolsa conforme"},
    )
    assert accepted.status_code == 200, accepted.get_json()
    with app.app_context():
        assignments = (
            ScmAsignacionRequerimientoPreparacion.query
            .filter_by(orden_preparacion_id=UUID(order_body["id"]))
            .order_by(ScmAsignacionRequerimientoPreparacion.id)
            .all()
        )
        assert [Decimal(value.cantidad_planificada_kg) for value in assignments] == [
            Decimal("11.000"), Decimal("11.000"),
        ]
        assert sorted(
            Decimal(value.cantidad_comprometida_kg) for value in assignments
        ) == [Decimal("0.000"), Decimal("11.000")]
        requirements = [value.requerimiento for value in assignments]
        assert sum(
            Decimal(value.cantidad_requerida_kg)
            - sum(Decimal(item.cantidad_comprometida_kg) for item in value.asignaciones)
            for value in requirements
        ) == Decimal("11.000")
        assert sorted(value.estado for value in requirements) == [
            "CUBIERTA", "PENDIENTE",
        ]

    replanned = client.post(
        "/api/scm/v1/ordenes-preparacion-material/proponer",
        headers=_headers(planner_id),
        json={
            "motivo": "Reponer exactamente la bolsa rechazada",
            "coberturas": [{
                "requerimiento_id": str(value.requerimiento_id),
                "cantidad_kg": "11.000",
            } for value in assignments if value.estado == "LIBERADA"],
        },
    )
    assert replanned.status_code == 201, replanned.get_json()
    assert replanned.get_json()["cantidad_objetivo_kg"] == "11.000"


def test_l1_cuatro_ojos_cierre_recepcion_y_calidad_sin_doble_credito(
    app, client, scm_config
):
    with app.app_context():
        planner_id, recipe_id, run_ids, resin_id = _seed_two_jar_runs_with_one_recipe()
        actors = _seed_l1_actors_and_stock(
            planner_id=planner_id, resin_id=resin_id,
        )

    order = _calculate_and_propose(
        client, planner_id=planner_id, run_ids=run_ids,
    )
    order_id = order["id"]

    released = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/liberar",
        headers=_headers(planner_id),
        json={"version": order["version"], "motivo": "Liberar receta aprobada"},
    )
    assert released.status_code == 200, released.get_json()
    order = released.get_json()

    reserved = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/reservar-insumos",
        headers=_headers(actors["preparer_id"]),
        json={
            "version": order["version"],
            "ubicacion_origen_ids": [actors["source_id"]],
        },
    )
    assert reserved.status_code == 200, reserved.get_json()
    order = reserved.get_json()
    reservation = order["requerimientos_insumo"][0]["reservas"][0]

    emitted = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/"
        f"reservas-insumo/{reservation['id']}/emitir",
        headers=_headers(actors["warehouse_id"]),
        json={
            "version": order["version"],
            "ubicacion_destino_id": actors["staging_id"],
            "cantidad_kg": reservation["cantidad_kg"],
            "motivo": "Entregar resina a preparacion",
        },
    )
    assert emitted.status_code == 201, emitted.get_json()
    emission_id = emitted.get_json()["emision"]["id"]
    order = emitted.get_json()["orden_preparacion"]

    started = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/iniciar",
        headers=_headers(actors["preparer_id"]),
        json={"version": order["version"], "motivo": "Iniciar tanda"},
    )
    assert started.status_code == 200, started.get_json()
    order = started.get_json()

    out_of_range_weight = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/lecturas",
        headers=_headers(actors["preparer_id"]),
        json={
            "version": order["version"], "tipo_uso": "APORTE",
            "metodo": "CONTINGENCIA_MANUAL", "bruto_kg": "1e100",
            "tara_kg": "0.120", "neto_kg": "22.000",
            "motivo": "Valor fuera del rango Numeric(15,3)",
            "evidencia_ref": "UAT-OPM-L1-RANGO-INVALIDO",
        },
    )
    assert out_of_range_weight.status_code == 422
    assert out_of_range_weight.get_json()["error"]["code"] == "INVALID_QUANTITY"

    input_reading = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/lecturas",
        headers=_headers(actors["preparer_id"]),
        json={
            "version": order["version"], "tipo_uso": "APORTE",
            "metodo": "CONTINGENCIA_MANUAL", "bruto_kg": "22.120",
            "tara_kg": "0.120", "neto_kg": "22.000",
            "motivo": "Balanza dedicada pendiente",
            "evidencia_ref": "UAT-OPM-L1-APORTE-01",
        },
    )
    assert input_reading.status_code == 201, input_reading.get_json()
    reading = input_reading.get_json()

    self_approval = client.post(
        f"/api/scm/v1/lecturas-preparacion/{reading['id']}/confirmar-segundo-actor",
        headers=_headers(actors["preparer_id"]),
        json={
            "version": reading["version"], "bruto_kg": "22.120",
            "tara_kg": "0.120", "neto_kg": "22.000",
            "motivo": "No debe autoaprobarse",
        },
    )
    assert self_approval.status_code == 403
    assert self_approval.get_json()["error"]["code"] == "OPM_SELF_APPROVAL_FORBIDDEN"

    confirmed = client.post(
        f"/api/scm/v1/lecturas-preparacion/{reading['id']}/confirmar-segundo-actor",
        headers=_headers(actors["confirmer_id"]),
        json={
            "version": reading["version"], "bruto_kg": "22.120",
            "tara_kg": "0.120", "neto_kg": "22.000",
            "motivo": "Peso verificado por segundo actor",
        },
    )
    assert confirmed.status_code == 200, confirmed.get_json()

    detail = client.get(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}",
        headers={"X-Actor-Id": str(actors["preparer_id"])},
    )
    assert detail.status_code == 200
    order = detail.get_json()
    incorporated = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/aportes",
        headers=_headers(actors["preparer_id"]),
        json={
            "version": order["version"], "lectura_id": reading["id"],
            "emision_id": emission_id, "motivo": "Incorporacion real completa",
        },
    )
    assert incorporated.status_code == 201, incorporated.get_json()

    detail = client.get(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}",
        headers={"X-Actor-Id": str(actors["preparer_id"])},
    ).get_json()
    for sequence in (1, 2):
        reading_response = client.post(
            f"/api/scm/v1/ordenes-preparacion-material/{order_id}/lecturas",
            headers=_headers(actors["preparer_id"]),
            json={
                "version": detail["version"], "tipo_uso": "BOLSA_SALIDA",
                "metodo": "CONTINGENCIA_MANUAL", "bruto_kg": "10.120",
                "tara_kg": "0.120", "neto_kg": "10.000",
                "motivo": f"Bolsa completa {sequence}",
                "evidencia_ref": f"UAT-OPM-L1-BOLSA-{sequence:02d}",
                "asignacion_requerimiento_id": detail["asignaciones"][
                    sequence - 1
                ]["id"],
            },
        )
        assert reading_response.status_code == 201, reading_response.get_json()
        bag_reading = reading_response.get_json()
        confirmation = client.post(
            f"/api/scm/v1/lecturas-preparacion/{bag_reading['id']}/"
            "confirmar-segundo-actor",
            headers=_headers(actors["confirmer_id"]),
            json={
                "version": bag_reading["version"], "bruto_kg": "10.120",
                "tara_kg": "0.120", "neto_kg": "10.000",
                "motivo": "Bolsa verificada",
            },
        )
        assert confirmation.status_code == 200, confirmation.get_json()
        detail = client.get(
            f"/api/scm/v1/ordenes-preparacion-material/{order_id}",
            headers={"X-Actor-Id": str(actors["preparer_id"])},
        ).get_json()

    erroneous = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/lecturas",
        headers=_headers(actors["preparer_id"]),
        json={
            "version": detail["version"], "tipo_uso": "BOLSA_SALIDA",
            "metodo": "CONTINGENCIA_MANUAL", "bruto_kg": "1.100",
            "tara_kg": "0.100", "neto_kg": "1.000",
            "motivo": "Lectura equivocada que debe conservarse invalidada",
            "evidencia_ref": "UAT-OPM-L1-ERROR-01",
            "asignacion_requerimiento_id": detail["asignaciones"][0]["id"],
        },
    )
    assert erroneous.status_code == 201, erroneous.get_json()
    erroneous_reading = erroneous.get_json()
    detail = client.get(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}",
        headers={"X-Actor-Id": str(actors["preparer_id"])},
    ).get_json()
    blocked_reconciliation = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/conciliar",
        headers=_headers(planner_id),
        json={
            "version": detail["version"], "perdida_kg": "0.000",
            "muestra_kg": "0.000", "remanente_equipo_kg": "0.000",
            "motivo": "No debe ignorar una lectura pendiente",
        },
    )
    assert blocked_reconciliation.status_code == 409
    assert (
        blocked_reconciliation.get_json()["error"]["code"]
        == "OPM_PENDING_WEIGHT_CONFIRMATIONS"
    )
    invalidated = client.post(
        f"/api/scm/v1/lecturas-preparacion/{erroneous_reading['id']}/invalidar",
        headers=_headers(actors["confirmer_id"]),
        json={
            "version": erroneous_reading["version"],
            "motivo": "Registro equivocado; se recaptura si fuera necesario",
        },
    )
    assert invalidated.status_code == 200, invalidated.get_json()
    assert invalidated.get_json()["estado"] == "INVALIDADA"
    detail = client.get(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}",
        headers={"X-Actor-Id": str(actors["preparer_id"])},
    ).get_json()

    reconciled = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/conciliar",
        headers=_headers(planner_id),
        json={
            "version": detail["version"], "perdida_kg": "2.000",
            "muestra_kg": "0.000", "remanente_equipo_kg": "0.000",
            "motivo": "Balance con dos kilos de perdida declarada",
        },
    )
    assert reconciled.status_code == 200, reconciled.get_json()
    assert reconciled.get_json()["balance"]["diferencia_kg"] == "0.000"

    close_headers = _headers(planner_id)
    close_payload = {
        "version": reconciled.get_json()["version"],
        "motivo": "Cerrar tanda conciliada",
    }
    closed = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/cerrar",
        headers=close_headers,
        json=close_payload,
    )
    assert closed.status_code == 201, closed.get_json()
    close_body = closed.get_json()
    assert close_body["kardex_acreditado"] is False
    lot = close_body["lote"]
    assert lot["estado"] == "PENDIENTE_RECEPCION"
    assert len(lot["bolsas"]) == 2
    assert all(
        bag["asignacion_requerimiento_id"] is not None
        for bag in lot["bolsas"]
    )
    with app.app_context():
        requirements = ScmRequerimientoMaterialPreparado.query.filter(
            ScmRequerimientoMaterialPreparado.corrida_fabricacion_id.in_(run_ids)
        ).all()
        assert sum(
            Decimal(value.cantidad_requerida_kg)
            - sum(
                Decimal(item.cantidad_comprometida_kg)
                for item in value.asignaciones
                if item.estado in ("COMPROMETIDA", "SATISFECHA")
            )
            for value in requirements
        ) == Decimal("2.000")
    close_replay = client.post(
        f"/api/scm/v1/ordenes-preparacion-material/{order_id}/cerrar",
        headers=close_headers,
        json=close_payload,
    )
    assert close_replay.status_code == 201, close_replay.get_json()
    assert close_replay.get_json()["lote"]["id"] == lot["id"]
    with app.app_context():
        assert ScmSaldoMaterialPreparado.query.count() == 0
        assert ScmMovimientoMaterialPreparado.query.count() == 0

    invalid_stock_receipt = client.post(
        f"/api/scm/v1/lotes-material-preparado/{lot['id']}/"
        f"bolsas/{lot['bolsas'][0]['id']}/recibir",
        headers=_headers(actors["warehouse_id"]),
        json={
            "ubicacion_id": actors["production_point_id"],
            "motivo": "Un punto de maquina no es almacen libre",
        },
    )
    assert invalid_stock_receipt.status_code == 422
    assert (
        invalid_stock_receipt.get_json()["error"]["code"]
        == "CANONICAL_LOCATION_REQUIRED"
    )

    with app.app_context():
        wrong_scope_actor = Trabajador(
            codigo="TRB-OPM-ALM-PIEZAS",
            nombres="Almacen",
            apellidos="Solo Piezas",
            activo=True,
        )
        wrong_scope_actor.roles.append(
            RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one()
        )
        db.session.add(wrong_scope_actor)
        db.session.flush()
        prepared_location = db.session.get(
            ScmUbicacionInventario, actors["prepared_storage_id"],
        )
        db.session.add(ScmAlmacenTrabajador(
            almacen_id=prepared_location.almacen_id,
            trabajador_id=wrong_scope_actor.id,
            clases_articulo_json=["PIEZA_COLOR"],
            asignado_por_id=planner_id,
        ))
        db.session.commit()
        wrong_scope_actor_id = wrong_scope_actor.id
    wrong_class_receipt = client.post(
        f"/api/scm/v1/lotes-material-preparado/{lot['id']}/"
        f"bolsas/{lot['bolsas'][0]['id']}/recibir",
        headers=_headers(wrong_scope_actor_id),
        json={
            "ubicacion_id": actors["prepared_storage_id"],
            "motivo": "La asignacion de Piezas no autoriza material preparado",
        },
    )
    assert wrong_class_receipt.status_code == 403
    assert (
        wrong_class_receipt.get_json()["error"]["code"]
        == "WAREHOUSE_ARTICLE_CLASS_SCOPE_REQUIRED"
    )
    with app.app_context():
        assert ScmSaldoMaterialPreparado.query.count() == 0
        assert ScmMovimientoMaterialPreparado.query.count() == 0

    for bag in lot["bolsas"]:
        received = client.post(
            f"/api/scm/v1/lotes-material-preparado/{lot['id']}/"
            f"bolsas/{bag['id']}/recibir",
            headers=_headers(actors["warehouse_id"]),
            json={
                "ubicacion_id": actors["prepared_storage_id"],
                "motivo": "Ingreso fisico de bolsa cerrada",
            },
        )
        assert received.status_code == 200, received.get_json()
        assert received.get_json()["bolsa"]["estado"] == "PENDIENTE_CALIDAD"

    retry_receipt = client.post(
        f"/api/scm/v1/lotes-material-preparado/{lot['id']}/"
        f"bolsas/{lot['bolsas'][0]['id']}/recibir",
        headers=_headers(actors["warehouse_id"]),
        json={
            "ubicacion_id": actors["prepared_storage_id"],
            "motivo": "Ingreso fisico de bolsa cerrada",
        },
    )
    assert retry_receipt.status_code == 200, retry_receipt.get_json()
    with app.app_context():
        balance = ScmSaldoMaterialPreparado.query.one()
        assert Decimal(balance.cantidad_fisica_kg) == Decimal("20.000")
        assert Decimal(balance.cantidad_no_disponible_kg) == Decimal("20.000")
        assert ScmMovimientoMaterialPreparado.query.count() == 2

    quality_as_participant = client.post(
        f"/api/scm/v1/lotes-material-preparado/{lot['id']}/"
        f"bolsas/{lot['bolsas'][0]['id']}/calidad",
        headers=_headers(actors["preparer_id"]),
        json={"decision": "LIBERAR", "motivo": "No debe autoliberar"},
    )
    assert quality_as_participant.status_code == 403
    assert (
        quality_as_participant.get_json()["error"]["code"]
        == "QUALITY_SEGREGATION_REQUIRED"
    )
    with app.app_context():
        warehouse_actor = db.session.get(Trabajador, actors["warehouse_id"])
        warehouse_actor.roles.append(
            RolOperativo.query.filter_by(codigo="CALIDAD").one()
        )
        db.session.commit()
    quality_as_receiver = client.post(
        f"/api/scm/v1/lotes-material-preparado/{lot['id']}/"
        f"bolsas/{lot['bolsas'][0]['id']}/calidad",
        headers=_headers(actors["warehouse_id"]),
        json={"decision": "LIBERAR", "motivo": "Receptor multirol no libera"},
    )
    assert quality_as_receiver.status_code == 403
    assert (
        quality_as_receiver.get_json()["error"]["code"]
        == "QUALITY_SEGREGATION_REQUIRED"
    )

    for bag in lot["bolsas"]:
        quality = client.post(
            f"/api/scm/v1/lotes-material-preparado/{lot['id']}/"
            f"bolsas/{bag['id']}/calidad",
            headers=_headers(actors["quality_id"]),
            json={"decision": "LIBERAR", "motivo": "Material conforme"},
        )
        assert quality.status_code == 200, quality.get_json()
        assert quality.get_json()["bolsa"]["estado"] == "DISPONIBLE"

    retry_quality = client.post(
        f"/api/scm/v1/lotes-material-preparado/{lot['id']}/"
        f"bolsas/{lot['bolsas'][0]['id']}/calidad",
        headers=_headers(actors["quality_id"]),
        json={"decision": "LIBERAR", "motivo": "Material conforme"},
    )
    assert retry_quality.status_code == 200, retry_quality.get_json()
    with app.app_context():
        balance = ScmSaldoMaterialPreparado.query.one()
        assert Decimal(balance.cantidad_fisica_kg) == Decimal("20.000")
        assert Decimal(balance.cantidad_no_disponible_kg) == Decimal("0.000")
        assert Decimal(balance.cantidad_reservada_kg) == Decimal("0.000")
        assert ScmMovimientoMaterialPreparado.query.count() == 4
    genealogy = client.get(
        f"/api/scm/v1/lotes-material-preparado/{lot['id']}",
        headers={"X-Actor-Id": str(planner_id)},
    )
    assert genealogy.status_code == 200, genealogy.get_json()
    genealogy_body = genealogy.get_json()["genealogia"]
    assert len(genealogy_body["entradas"]["requerimientos_insumo"]) == 1
    assert len(genealogy_body["entradas"]["aportes"]) == 1
    assert len(genealogy_body["bolsas"]) == 2
    assert {value["tipo"] for value in genealogy_body["movimientos"]} == {
        "RECEPCION", "LIBERACION_CALIDAD",
    }
