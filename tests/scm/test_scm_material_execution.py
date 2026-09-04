from decimal import Decimal
from uuid import uuid4

from app import db
from app.models.materiales import Colorante, MateriaPrima
from app.models.receta_color import RecetaColorLinea, RecetaColorMaestra
from app.models.scm_articulos import ScmArticulo
from app.models.scm_catalogos import ScmCategoriaRecepcion, ScmMaterial
from app.models.scm_inventory import ScmSaldoMaterialInventario, ScmUbicacionInventario
from app.models.scm_material_execution import ScmLotePremezcla
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.models.trabajador import RolOperativo, Trabajador


def _headers(actor_id, *, idempotent=False):
    result = {"X-Actor-Id": str(actor_id)}
    if idempotent:
        result["Idempotency-Key"] = str(uuid4())
    return result


def _seed_us010b():
    planner = Trabajador.query.filter_by(codigo="TRB-01").one()
    planner.roles.append(RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one())
    warehouse = Trabajador(
        codigo="TRB-US010B-ALM", nombres="Ana", apellidos="Almacen", activo=True,
    )
    db.session.add(warehouse)
    warehouse.roles.append(RolOperativo.query.filter_by(codigo="ALMACEN_RECEPCION").one())
    category = ScmCategoriaRecepcion.query.filter_by(codigo="LEGACY_POR_CONFIGURAR").one()
    resin = ScmMaterial(
        codigo="MP-US010B-PP", nombre="PP virgen US010B", clase="MATERIA_PRIMA",
        categoria_recepcion_id=category.id,
    )
    pigment = ScmMaterial(
        codigo="COL-US010B-AM", nombre="Amarillo US010B", clase="COLORANTE",
        categoria_recepcion_id=category.id,
    )
    db.session.add_all([warehouse, resin, pigment])
    db.session.flush()
    db.session.add_all([
        MateriaPrima(nombre=resin.nombre, tipo="VIRGEN", scm_material_id=resin.id),
        Colorante(nombre=pigment.nombre, tipo="COLORANTE", scm_material_id=pigment.id),
    ])
    from app.models.producto import (
        ColorBase,
        ColorProduccion,
        FamiliaColor,
        PiezaColor,
    )
    color_base = ColorBase.query.first()
    if color_base is None:
        color_base = ColorBase(nombre="AMARILLO US010B")
        db.session.add(color_base)
        db.session.flush()
    color_family = FamiliaColor.query.first()
    if color_family is None:
        color_family = FamiliaColor(nombre="SOLIDO US010B")
        db.session.add(color_family)
        db.session.flush()
    color = ColorProduccion(
        color_base_id=color_base.id, familia_color_id=color_family.id,
    )
    db.session.add(color)
    db.session.flush()
    recipe = RecetaColorMaestra(
        color_produccion_id=color.id, producto_scope="*",
        nombre_variante="US010B", revision=1, estado="APROBADA",
        base_virgen_kg=25,
    )
    recipe.lineas = [
        RecetaColorLinea(
            material_id=resin.id, tipo_componente="MATERIA_PRIMA",
            cantidad=1, unidad="FRACCION", orden=1,
        ),
        RecetaColorLinea(
            material_id=pigment.id, tipo_componente="COLORANTE",
            cantidad=100, unidad="GRAMOS", base_kg=25, orden=2,
        ),
    ]
    db.session.add(recipe)
    db.session.flush()
    piece_color = PiezaColor(
        sku="PC-US010B",
        piezas="Pieza US010B",
        color_produccion_id=color.id,
        peso=100,
    )
    db.session.add(piece_color)
    db.session.flush()
    article = ScmArticulo.query.filter_by(codigo="PC-US010B").one()
    order = ScmOrdenOperacion(
        codigo="OF-US010B", tipo="FABRICACION", origen_demanda="EXCEPCIONAL",
        estado="LIBERADA", created_by_id=planner.id, released_by_id=planner.id,
    )
    fabrication = ScmOrdenFabricacion(
        orden_operacion=order, snapshot_tiempo_ciclo_seg=20,
        snapshot_horas_turno=8, snapshot_peso_colada_gr=10,
    )
    run = ScmCorridaFabricacion(
        orden_fabricacion=fabrication, codigo="OF-US010B-C01", secuencia=1,
        color_produccion_id=color.id, receta_revision_id=recipe.id,
        ciclos_objetivo=100, estado="LIBERADA",
    )
    output = ScmOrdenOperacionSalida(
        orden_operacion=order, corrida_fabricacion=run,
        articulo=article, cantidad_por_ciclo_snapshot=1,
        peso_unitario_snapshot_g=100, cantidad_objetivo=100,
        kg_estandar_objetivo=10,
    )
    location = ScmUbicacionInventario(
        codigo="ALMACEN_MP_US010B", nombre="Almacen MP US010B",
        clases_articulo_json=["MATERIA_PRIMA", "COLORANTE"],
    )
    db.session.add_all([order, fabrication, run, output, location])
    db.session.flush()
    db.session.add_all([
        ScmSaldoMaterialInventario(
            material_id=resin.id, ubicacion_id=location.id, cantidad_fisica_kg=20,
        ),
        ScmSaldoMaterialInventario(
            material_id=pigment.id, ubicacion_id=location.id, cantidad_fisica_kg=2,
        ),
    ])
    db.session.commit()
    return planner.id, warehouse.id, order.id, run.id, resin.id, pigment.id


def test_requerir_reservar_emitir_y_devolver_sin_consumir(app, client, scm_config):
    with app.app_context():
        planner_id, warehouse_id, order_id, run_id, resin_id, pigment_id = _seed_us010b()

    generated = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/requerimientos-material/generar",
        headers=_headers(planner_id, idempotent=True), json={},
    )
    assert generated.status_code == 201, generated.get_json()
    items = generated.get_json()["items"]
    quantities = {item["material"]["id"]: item["cantidad_plan_kg"] for item in items}
    # 10 kg de salida + 1 kg de runner; 100 g / 25 kg virgen.
    assert quantities[resin_id] == "11.000"
    assert quantities[pigment_id] == "0.044"

    reserved = client.post(
        f"/api/scm/v1/corridas-fabricacion/{run_id}/materiales/reservar",
        headers=_headers(planner_id, idempotent=True), json={},
    )
    assert reserved.status_code == 200, reserved.get_json()
    resin_requirement = next(
        item for item in reserved.get_json()["requerimientos"]
        if item["material"]["id"] == resin_id
    )
    reservation = resin_requirement["reservas"][0]
    assert reservation["cantidad_kg"] == "11.000"

    emitted = client.post(
        f"/api/scm/v1/reservas-material/{reservation['id']}/emitir",
        headers=_headers(warehouse_id, idempotent=True),
        json={"cantidad_kg": "5.000", "motivo": "Entrega parcial a preparacion"},
    )
    assert emitted.status_code == 201, emitted.get_json()
    emission = emitted.get_json()
    assert emission["cantidad_neta_kg"] == "5.000"

    returned = client.post(
        f"/api/scm/v1/emisiones-material/{emission['id']}/devolver",
        headers=_headers(warehouse_id, idempotent=True),
        json={"cantidad_kg": "2.000", "motivo": "Remanente no utilizado"},
    )
    assert returned.status_code == 201, returned.get_json()
    assert returned.get_json()["cantidad_neta_kg"] == "3.000"

    balances = client.get(
        "/api/scm/v1/inventario/saldos", headers=_headers(warehouse_id),
    )
    assert balances.status_code == 200
    resin_balances = [
        value for value in balances.get_json()["materiales"]
        if value["material_scm_id"] == resin_id
    ]
    by_location = {value["ubicacion"]["codigo"]: value for value in resin_balances}
    assert by_location["ALMACEN_MP_US010B"]["cantidad_fisica"] == "17.000"
    assert by_location["ALMACEN_MP_US010B"]["cantidad_reservada"] == "8.000"
    assert by_location["PREPARACION_PRODUCCION"]["cantidad_fisica"] == "3.000"
    assert by_location["PREPARACION_PRODUCCION"]["cantidad_reservada"] == "3.000"
    # Emision y devolucion trasladan custodia; no crean CONSUMO.
    assert all(value["tipo"] != "CONSUMO" for value in balances.get_json().get("movimientos", []))


def test_reserva_es_atomica_si_falta_un_componente(app, client, scm_config):
    with app.app_context():
        planner_id, _warehouse_id, order_id, run_id, resin_id, pigment_id = _seed_us010b()
        pigment_balance = ScmSaldoMaterialInventario.query.filter_by(material_id=pigment_id).one()
        pigment_balance.cantidad_fisica_kg = Decimal("0.001")
        db.session.commit()

    assert client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/requerimientos-material/generar",
        headers=_headers(planner_id, idempotent=True), json={},
    ).status_code == 201
    failed = client.post(
        f"/api/scm/v1/corridas-fabricacion/{run_id}/materiales/reservar",
        headers=_headers(planner_id, idempotent=True), json={},
    )
    assert failed.status_code == 409
    assert failed.get_json()["error"]["code"] == "INSUFFICIENT_MATERIAL_STOCK"
    with app.app_context():
        resin_balance = ScmSaldoMaterialInventario.query.filter_by(material_id=resin_id).one()
        assert Decimal(resin_balance.cantidad_reservada_kg) == 0


def test_premezcla_consume_emisiones_y_crea_wip_genealogico(app, client, scm_config):
    with app.app_context():
        planner_id, warehouse_id, order_id, run_id, _resin_id, _pigment_id = _seed_us010b()

    generated = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/requerimientos-material/generar",
        headers=_headers(planner_id, idempotent=True), json={},
    )
    assert generated.status_code == 201, generated.get_json()
    reserved = client.post(
        f"/api/scm/v1/corridas-fabricacion/{run_id}/materiales/reservar",
        headers=_headers(planner_id, idempotent=True), json={},
    )
    assert reserved.status_code == 200, reserved.get_json()
    emission_ids = []
    for requirement in reserved.get_json()["requerimientos"]:
        for reservation in requirement["reservas"]:
            emitted = client.post(
                f"/api/scm/v1/reservas-material/{reservation['id']}/emitir",
                headers=_headers(warehouse_id, idempotent=True),
                json={
                    "cantidad_kg": reservation["cantidad_kg"],
                    "motivo": "Entrega completa para mezcla US010B",
                },
            )
            assert emitted.status_code == 201, emitted.get_json()
            emission_ids.append(emitted.get_json()["id"])

    premixed = client.post(
        f"/api/scm/v1/corridas-fabricacion/{run_id}/premezclas",
        headers=_headers(planner_id, idempotent=True),
        json={"motivo": "Tanda completa UAT", "genealogia_tipo": "EXACTA"},
    )
    assert premixed.status_code == 201, premixed.get_json()
    body = premixed.get_json()
    assert body["codigo"] == "LMP-OF-US010B-C01-001"
    assert body["cantidad_kg"] == "11.044"
    assert {value["emision_id"] for value in body["inputs"]} == set(emission_ids)

    workspace = client.get(
        "/api/scm/v1/materiales-ejecucion", headers=_headers(planner_id),
    )
    assert workspace.status_code == 200, workspace.get_json()
    run = workspace.get_json()["items"][0]
    assert run["premezclas"][0]["estado"] == "DISPONIBLE_MAQUINA"
    assert all(
        Decimal(requirement["cantidad_emitida_neta_kg"]) == 0
        and Decimal(requirement["cantidad_consumida_preparacion_kg"]) > 0
        for requirement in run["requerimientos"]
    )

    rejected_return = client.post(
        f"/api/scm/v1/emisiones-material/{emission_ids[0]}/devolver",
        headers=_headers(warehouse_id, idempotent=True),
        json={"cantidad_kg": "0.001", "motivo": "No debe permitirse"},
    )
    assert rejected_return.status_code == 409


def test_cutover_impide_premezcla_legacy_si_corrida_ya_usa_opm_canonica(
    app, client, scm_config,
):
    with app.app_context():
        planner_id, _warehouse_id, _order_id, run_id, _resin_id, _pigment_id = (
            _seed_us010b()
        )

    canonical = client.post(
        "/api/scm/v1/requerimientos-preparacion/calcular",
        headers=_headers(planner_id, idempotent=True),
        json={"corrida_fabricacion_id": str(run_id)},
    )
    assert canonical.status_code == 200, canonical.get_json()
    legacy = client.post(
        f"/api/scm/v1/corridas-fabricacion/{run_id}/premezclas",
        headers=_headers(planner_id, idempotent=True),
        json={"motivo": "No crear doble realidad", "genealogia_tipo": "EXACTA"},
    )
    assert legacy.status_code == 409, legacy.get_json()
    assert (
        legacy.get_json()["error"]["code"]
        == "CANONICAL_PREPARED_MATERIAL_ALREADY_ACTIVE"
    )


def test_cutover_exige_migracion_si_corrida_ya_tiene_premezcla_legacy(
    app, client, scm_config,
):
    with app.app_context():
        planner_id, _warehouse_id, _order_id, run_id, _resin_id, _pigment_id = (
            _seed_us010b()
        )
        legacy = ScmLotePremezcla(
            codigo="LMP-LEGACY-CUTOVER-001",
            corrida_fabricacion_id=run_id,
            secuencia=1,
            cantidad_kg=Decimal("11.044"),
            genealogia_tipo="EXACTA",
            estado="DISPONIBLE_MAQUINA",
            ubicacion_codigo="PREPARACION_PRODUCCION",
            motivo="Historico legacy preservado",
            actor_id=planner_id,
            operation_id=uuid4(),
        )
        db.session.add(legacy)
        db.session.commit()

    canonical = client.post(
        "/api/scm/v1/requerimientos-preparacion/calcular",
        headers=_headers(planner_id, idempotent=True),
        json={"corrida_fabricacion_id": str(run_id)},
    )
    assert canonical.status_code == 409, canonical.get_json()
    assert (
        canonical.get_json()["error"]["code"]
        == "PREPARED_MATERIAL_LEGACY_MIGRATION_REQUIRED"
    )
    with app.app_context():
        assert ScmLotePremezcla.query.filter_by(
            corrida_fabricacion_id=run_id,
        ).count() == 1
