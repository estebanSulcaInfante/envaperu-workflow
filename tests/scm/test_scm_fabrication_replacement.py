from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4
import pytest
from sqlalchemy import select

from app import db
from app.models.maquina import Maquina, TipoMaquina
from app.models.molde import Molde
from app.models.producto import ColorBase, ColorProduccion, FamiliaColor, ProductoTerminado
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_auditoria import ScmEvento
from app.models.scm_articulos import ScmArticulo, ScmArticuloProducto
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
    ScmPlanProduccion,
    utc_now,
)
from app.models.scm_rutas import ScmCentroTrabajo, ScmOperacionRuta, ScmRutaRevision
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_ot_service import (
    create_fabrication_ot,
    recalculate_fabrication_manga_plan,
)
from app.services.scm_fabrication_order_service import release_fabrication_order
from app.services.scm_packaging_service import (
    approve_packaging_rule,
    assign_article_profiles,
    create_container_type,
    create_packable_profile,
    create_packaging_rule,
)
from app.services.scm_route_service import _content_hash


def _seed_released_order(app, *, direct_assignment=False):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one())
        base = ColorBase(nombre=f"BASE-{uuid4().hex[:8]}")
        family = FamiliaColor(nombre=f"FAMILY-{uuid4().hex[:8]}")
        product = ProductoTerminado(
            cod_sku_pt=f"PT-REP-{uuid4().hex[:8].upper()}", producto="Producto reemplazo",
            linea_id=1, familia_id=1,
        )
        db.session.add_all([base, family, product])
        db.session.flush()
        color = ColorProduccion(color_base_id=base.id, familia_color_id=family.id)
        recipe = RecetaColorMaestra(
            color_produccion=color, nombre_variante="Receta corregida",
            revision=1, estado="APROBADA", es_default=False, base_virgen_kg=25,
        )
        db.session.add_all([color, recipe])
        db.session.flush()
        article = ScmArticuloProducto.query.filter_by(
            producto_terminado_id=product.cod_sku_pt,
        ).one()
        demand = ScmOrdenProduccion(
            codigo=f"OP-REP-{uuid4().hex[:8].upper()}", origen="PLANIFICACION",
            fecha_necesidad=date(2026, 9, 5), created_by_id=actor.id,
        )
        line = ScmOrdenProduccionLinea(
            producto_terminado_id=product.cod_sku_pt, cantidad_solicitada=120,
        )
        demand.lineas.append(line)
        proposal_key = f"OF-ORIGINAL-{uuid4().hex[:8].upper()}"
        plan = ScmPlanProduccion(
            orden_produccion=demand, revision=1, estado="CONFIRMADO",
            input_hash="a" * 64, content_hash="b" * 64,
            propuesta_json={"documentos": [{
                "clave": proposal_key, "tipo": "FABRICACION",
                "operacion_ruta_id": None, "ruta_hash": None,
                "articulo_scm_id": article.articulo_id,
            }]}, calculado_por_id=actor.id,
            operation_id=uuid4(), confirmado_por_id=actor.id, confirmado_at=utc_now(),
        )
        old = ScmOrdenOperacion(
            codigo=f"OF-REP-{uuid4().hex[:8].upper()}", tipo="FABRICACION",
            origen_demanda="ORDEN_PRODUCCION", plan_produccion=plan,
            propuesta_clave=proposal_key, estado="LIBERADA",
            created_by_id=actor.id, released_by_id=actor.id, released_at=utc_now(),
        )
        fabrication = ScmOrdenFabricacion(orden_operacion=old)
        run = ScmCorridaFabricacion(
            codigo=f"{old.codigo}-C01", secuencia=1,
            color_produccion_id=color.id, receta_revision_id=recipe.id,
            receta_hash="c" * 64, ciclos_objetivo=12, estado="LIBERADA",
            meta_kg_legacy=Decimal("18.000000"),
        )
        fabrication.corridas.append(run)
        output = ScmOrdenOperacionSalida(
            orden_operacion=old, corrida_fabricacion=run,
            articulo_scm_id=article.articulo_id,
            cantidad_por_ciclo_snapshot=Decimal("10.0000"),
            peso_unitario_snapshot_g=Decimal("15.0000"),
            cantidad_objetivo=Decimal("120.000"),
            kg_estandar_objetivo=Decimal("1.800000"),
            excedente_objetivo=Decimal("0.000"),
        )
        db.session.add_all([demand, plan, old, output])
        if direct_assignment:
            db.session.add(ScmAsignacionDemandaSuministro(
                orden_produccion_linea=line, fuente_tipo="SALIDA_ORDEN",
                orden_operacion_salida=output, cantidad_planificada=120,
            ))
        db.session.commit()
        return str(old.id), actor.id, str(run.id), recipe.id


def _replace(client, order_id, actor_id, *, version=1, reason="Corregir receta", key=None):
    return client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/reemplazar",
        json={"version": version, "motivo": reason},
        headers={
            "X-Actor-Id": str(actor_id),
            "Idempotency-Key": str(key or uuid4()),
        },
    )


def test_replacement_is_atomic_and_preserves_snapshots(app, client, scm_config):
    order_id, actor_id, run_id, recipe_id = _seed_released_order(app)
    key = uuid4()
    response = _replace(client, order_id, actor_id, key=key)
    assert response.status_code == 201, response.json
    body = response.get_json()
    assert body["anterior"]["estado"] == "ANULADA"
    assert body["sucesora"]["estado"] == "BORRADOR"
    assert body["sucesora"]["id"] != order_id
    assert body["sucesora"]["reemplazo"]["anterior"]["id"] == order_id
    assert body["sucesora"]["corridas"][0]["receta_revision_id"] is None
    assert body["sucesora"]["corridas"][0]["salidas"][0]["cantidad_objetivo"] == "120.000"
    assert body["sucesora"]["corridas"][0]["salidas"][0]["kg_estandar_objetivo"] == "1.800000"
    replay = _replace(client, order_id, actor_id, version=1, key=key)
    assert replay.status_code == 201
    assert replay.get_json() == body
    duplicate = _replace(client, order_id, actor_id, version=1)
    assert duplicate.status_code == 409  # a different key cannot create another successor
    with app.app_context():
        old = db.session.get(ScmOrdenOperacion, UUID(order_id))
        assert old.estado == "ANULADA"
        assert old.fabricacion.corridas[0].receta_revision_id == recipe_id
        assert ScmEvento.query.filter_by(aggregate_id=order_id, tipo="OF_REPLACED").count() == 1


def test_replacement_blocks_direct_demand_assignment(app, client, scm_config):
    order_id, actor_id, _, _ = _seed_released_order(app, direct_assignment=True)
    response = _replace(client, order_id, actor_id)
    assert response.status_code == 409, response.json
    assert response.get_json()["error"]["code"] == "DIRECT_DEMAND_ASSIGNMENT"
    with app.app_context():
        assert db.session.get(ScmOrdenOperacion, UUID(order_id)).estado == "LIBERADA"


def test_replacement_recipe_update_is_explicit_and_does_not_recalculate(app, client, scm_config):
    order_id, actor_id, _, recipe_id = _seed_released_order(app)
    created = _replace(client, order_id, actor_id).get_json()
    successor = created["sucesora"]
    run = successor["corridas"][0]
    update = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{successor['id']}",
        json={"version": successor["version"], "corridas": [{"id": run["id"], "receta_revision_id": recipe_id}]},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert update.status_code == 200, update.json
    assert update.get_json()["corridas"][0]["receta_revision_id"] == recipe_id
    assert update.get_json()["corridas"][0]["salidas"][0]["cantidad_objetivo"] == "120.000"
    altered = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{successor['id']}",
        json={"version": update.get_json()["version"], "corridas": [{"id": run["id"], "receta_revision_id": recipe_id, "ciclos_objetivo": 99}]},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert altered.status_code == 400


def test_replacement_patch_rejects_duplicate_run_ids_atomically(app, client, scm_config):
    order_id, actor_id, _, recipe_id = _seed_released_order(app)
    created = _replace(client, order_id, actor_id).get_json()
    successor = created["sucesora"]
    run = successor["corridas"][0]
    rejected = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{successor['id']}",
        json={"version": successor["version"], "corridas": [
            {"id": run["id"], "receta_revision_id": recipe_id},
            {"id": run["id"], "receta_revision_id": recipe_id},
        ]},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert rejected.status_code == 422
    assert rejected.get_json()["error"]["code"] == "OF_CORRIDA_MISMATCH"
    with app.app_context():
        persisted = db.session.get(ScmOrdenOperacion, UUID(successor["id"]))
        assert persisted.version == successor["version"]
        assert persisted.fabricacion.corridas[0].receta_revision_id is None


def test_replacement_clones_process_machine_and_releases_after_recipe(app, client, scm_config):
    order_id, actor_id, _, recipe_id = _seed_released_order(app)
    with app.app_context():
        old = db.session.get(ScmOrdenOperacion, UUID(order_id))
        suffix = uuid4().hex[:8].upper()
        machine_type = TipoMaquina(
            codigo=f"REP-INJ-{suffix}", nombre="Inyeccion reemplazo", proceso="INYECCION"
        )
        machine = Maquina(
            codigo=f"MQ-REP-{suffix}", nombre="Maquina reemplazo",
            tipo_maquina=machine_type, estado="OPERATIVA", activo=True,
        )
        mold = Molde(
            codigo=f"ML-REP-{suffix}", nombre="Molde reemplazo",
            peso_tiro_gr=20, tiempo_ciclo_std=30,
        )
        db.session.add_all([machine, mold])
        db.session.flush()
        old.fabricacion.molde_id = mold.codigo
        old.fabricacion.maquina_prevista_id = machine.id
        old.fabricacion.snapshot_tiempo_ciclo_seg = 30
        old.fabricacion.snapshot_horas_turno = 8
        old.fabricacion.snapshot_peso_colada_gr = 10
        old.fabricacion.snapshot_proceso = "INYECCION"
        old.fabricacion.fuente_proceso = "EXPLICITO"
        db.session.commit()

    created = _replace(client, order_id, actor_id).get_json()
    successor = created["sucesora"]
    run = successor["corridas"][0]
    selected = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{successor['id']}",
        json={"version": successor["version"], "corridas": [
            {"id": run["id"], "receta_revision_id": recipe_id}
        ]},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert selected.status_code == 200, selected.get_json()
    released = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{successor['id']}/liberar",
        json={"version": selected.get_json()["version"]},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert released.status_code == 200, released.get_json()
    assert released.get_json()["estado"] == "LIBERADA"
    assert released.get_json()["snapshot_proceso"] == "INYECCION"
    with app.app_context():
        old = db.session.get(ScmOrdenOperacion, UUID(order_id))
        successor_model = db.session.get(ScmOrdenOperacion, UUID(successor["id"]))
        assert old.estado == "ANULADA"
        assert old.fabricacion.snapshot_proceso == "INYECCION"
        assert successor_model.fabricacion.maquina_prevista_id == old.fabricacion.maquina_prevista_id


@pytest.mark.parametrize("legacy_null", [False, True])
def test_replacement_soplado_ot_rejects_injection_machine_and_accepts_sopladora(
    app, client, scm_config, legacy_null
):
    order_id, actor_id, _, recipe_id = _seed_released_order(app)
    with app.app_context():
        old = db.session.get(ScmOrdenOperacion, UUID(order_id))
        suffix = uuid4().hex[:8].upper()
        soplado_type = TipoMaquina(
            codigo=f"REP-SOP-{suffix}", nombre="Soplado reemplazo", proceso="SOPLADO"
        )
        soplado = Maquina(
            codigo=f"MQ-REP-SOP-{suffix}", nombre="Sopladora reemplazo",
            tipo_maquina=soplado_type, estado="OPERATIVA", activo=True,
        )
        injection_type = TipoMaquina(
            codigo=f"REP-INJ-OT-{suffix}", nombre="Inyeccion OT", proceso="INYECCION"
        )
        injection = Maquina(
            codigo=f"MQ-REP-INJ-{suffix}", nombre="Inyectora OT",
            tipo_maquina=injection_type, estado="OPERATIVA", activo=True,
        )
        mold = Molde(
            codigo=f"ML-REP-SOP-{suffix}", nombre="Molde soplado reemplazo",
            peso_tiro_gr=20, tiempo_ciclo_std=30,
        )
        worker = Trabajador(
            codigo=f"TRB-REP-MQ-{suffix}", nombres="Operador", apellidos="Soplado",
            activo=True,
            roles=[RolOperativo.query.filter_by(codigo="MAQUINISTA").one()],
        )
        db.session.add_all([soplado, injection, mold, worker])
        db.session.flush()
        old.fabricacion.molde_id = mold.codigo
        old.fabricacion.maquina_prevista_id = soplado.id
        old.fabricacion.snapshot_tiempo_ciclo_seg = 30
        old.fabricacion.snapshot_horas_turno = 8
        old.fabricacion.snapshot_peso_colada_gr = 10
        old.fabricacion.snapshot_proceso = "SOPLADO"
        old.fabricacion.fuente_proceso = "EXPLICITO"
        if legacy_null:
            center = db.session.scalar(select(ScmCentroTrabajo))
            if center is None:
                center = ScmCentroTrabajo(
                    codigo=f"CTR-A3-{suffix}", nombre="Centro A3", tipo="SOPLADO"
                )
                db.session.add(center)
                db.session.flush()
            route = ScmRutaRevision(
                articulo_objetivo_id=old.salidas[0].articulo_scm_id,
                numero_revision=1,
                estado="APROBADA",
                creada_por_id=actor_id,
                aprobada_por_id=actor_id,
            )
            route.operaciones.append(ScmOperacionRuta(
                clave=f"A3-SOP-{suffix}", secuencia_visible=10,
                nombre="Ruta SOPLADO A3", tipo="SOPLADO", executor_kind="OP_OT",
                centro_trabajo_id=center.id,
                articulo_salida_id=old.salidas[0].articulo_scm_id,
            ))
            db.session.add(route)
            db.session.flush()
            route.content_hash = _content_hash(route)
            old.fabricacion.snapshot_proceso = None
            old.fabricacion.fuente_proceso = None
            old.operacion_ruta_revision_id = route.operaciones[0].id
            old.operacion_ruta_hash = route.content_hash
            proposal = dict(old.plan_produccion.propuesta_json)
            proposal["documentos"] = [{
                **proposal["documentos"][0],
                "operacion_ruta_id": route.operaciones[0].id,
                "ruta_hash": route.content_hash,
            }]
            old.plan_produccion.propuesta_json = proposal
        article_id = old.salidas[0].articulo_scm_id
        container = create_container_type(
            db.session,
            actor_id=actor_id,
            data={
                "clase": "MANGA", "nombre": f"Manga A3 {suffix}", "material": "PE",
                "dimensiones": {"ancho_mm": "500", "largo_mm": "800"},
                "tara_nominal_g": "100", "tolerancia_tara_g": "10",
                "peso_bruto_max_kg": "100",
            },
        )
        profile = create_packable_profile(
            db.session,
            actor_id=actor_id,
            data={"nombre": f"Perfil A3 {suffix}", "descripcion_fisica": "Fixture A3"},
        )
        article = db.session.get(ScmArticulo, article_id)
        assign_article_profiles(
            db.session,
            actor_id=actor_id,
            article_id=article.id,
            data={
                "version": article.version,
                "perfiles": [{
                    "perfil_empacable_id": profile["id"],
                    "es_predeterminado": True,
                    "activo": True,
                }],
            },
        )
        rule = create_packaging_rule(
            db.session,
            actor_id=actor_id,
            data={
                "perfil_empacable_id": profile["id"],
                "tipo_contenedor_id": container["id"],
                "medicion_fisica_probada": True,
                "cantidad_objetivo_un": 100,
                "cantidad_maxima_probada_un": 100,
                "peso_neto_operativo_max_kg": "99",
                "margen_seguridad_kg": "0",
                "tolerancia_peso_abs_g": "20",
                "tolerancia_peso_pct": "1",
            },
        )
        approver = Trabajador(
            codigo=f"TRB-REP-AP-{suffix}", nombres="Aprobador", apellidos="Empaque",
            activo=True,
            roles=[RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()],
        )
        db.session.add(approver)
        db.session.flush()
        approve_packaging_rule(
            db.session,
            actor_id=approver.id,
            revision_id=rule["revision_id"],
            operation_id=uuid4(),
            data={"version": rule["version"]},
        )
        db.session.commit()

    created = _replace(client, order_id, actor_id).get_json()
    successor = created["sucesora"]
    run = successor["corridas"][0]
    if legacy_null:
        inspected = client.get(
            f"/api/scm/v1/ordenes-fabricacion/{successor['id']}",
            headers={"X-Actor-Id": str(actor_id)},
        )
        assert inspected.status_code == 200, inspected.get_json()
        assert inspected.get_json()["snapshot_proceso"] is None
        assert inspected.get_json()["fuente_proceso"] is None
        assert inspected.get_json()["proceso_requerido"] == "SOPLADO"
    selected = client.patch(
        f"/api/scm/v1/ordenes-fabricacion/{successor['id']}",
        json={"version": successor["version"], "corridas": [
            {"id": run["id"], "receta_revision_id": recipe_id}
        ]},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert selected.status_code == 200, selected.get_json()
    released = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{successor['id']}/liberar",
        json={"version": selected.get_json()["version"]},
        headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": str(uuid4())},
    )
    assert released.status_code == 200, released.get_json()
    if legacy_null:
        assert released.get_json()["fuente_proceso"] == "RUTA_CABECERA"
    with app.app_context():
        plan = recalculate_fabrication_manga_plan(
            db.session,
            actor_id=actor_id,
            order_id=UUID(successor["id"]),
            operation_id=uuid4(),
            data={},
        )
        line = plan["plan"]["lineas"][0]
        worker = db.session.scalar(
            select(Trabajador).where(Trabajador.codigo.like("TRB-REP-MQ-%")).order_by(Trabajador.id.desc())
        )
        injection = db.session.scalar(
            select(Maquina).where(Maquina.codigo.like("MQ-REP-INJ-%")).order_by(Maquina.id.desc())
        )
        soplado = db.session.scalar(
            select(Maquina).where(Maquina.codigo.like("MQ-REP-SOP-%")).order_by(Maquina.id.desc())
        )
        data = {
            "corrida_fabricacion_id": run["id"],
            "fecha_operativa": "2026-09-29",
            "turno": "DIA",
            "maquinista_id": worker.id,
            "asignaciones": [{
                "plan_linea_id": line["id"],
                "cantidad_un": line["cantidad_objetivo_un"],
            }],
        }
        with pytest.raises(Exception) as error:
            create_fabrication_ot(
                db.session,
                actor_id=actor_id,
                order_id=UUID(successor["id"]),
                operation_id=uuid4(),
                data={**data, "maquina_id": injection.id},
            )
        assert getattr(error.value, "code", None) == "MACHINE_PROCESS_INCOMPATIBLE"
        accepted = create_fabrication_ot(
            db.session,
            actor_id=actor_id,
            order_id=UUID(successor["id"]),
            operation_id=uuid4(),
            data={**data, "maquina_id": soplado.id},
        )
        assert accepted["trabajo_color"]["corrida_fabricacion_id"] == run["id"]


def test_release_header_route_fill_rolls_back_on_machine_failure(app, scm_config):
    order_id, actor_id, _, _ = _seed_released_order(app)
    with app.app_context():
        old = db.session.get(ScmOrdenOperacion, UUID(order_id))
        suffix = uuid4().hex[:8].upper()
        injection_type = TipoMaquina(
            codigo=f"REP-ROLL-INJ-{suffix}", nombre="Inyectora rollback", proceso="INYECCION"
        )
        injection = Maquina(
            codigo=f"MQ-ROLL-INJ-{suffix}", nombre="Inyectora rollback",
            tipo_maquina=injection_type, estado="OPERATIVA", activo=True,
        )
        center = db.session.scalar(select(ScmCentroTrabajo))
        if center is None:
            center = ScmCentroTrabajo(
                codigo=f"CTR-ROLL-{suffix}", nombre="Centro rollback", tipo="SOPLADO"
            )
            db.session.add(center)
            db.session.flush()
        article_id = old.salidas[0].articulo_scm_id
        route = ScmRutaRevision(
            articulo_objetivo_id=article_id,
            numero_revision=1,
            estado="APROBADA",
            creada_por_id=actor_id,
            aprobada_por_id=actor_id,
        )
        route.operaciones.append(ScmOperacionRuta(
            clave=f"ROLLBACK-{suffix}", secuencia_visible=10,
            nombre="Ruta soplado rollback", tipo="SOPLADO", executor_kind="OP_OT",
            centro_trabajo_id=center.id, articulo_salida_id=article_id,
        ))
        db.session.add_all([injection, route])
        db.session.flush()
        route.content_hash = _content_hash(route)
        old.estado = "BORRADOR"
        old.fabricacion.snapshot_proceso = None
        old.fabricacion.fuente_proceso = None
        old.fabricacion.maquina_prevista_id = injection.id
        old.operacion_ruta_revision_id = route.operaciones[0].id
        old.operacion_ruta_hash = route.content_hash
        db.session.commit()

        with pytest.raises(Exception) as error:
            release_fabrication_order(
                db.session,
                actor_id=actor_id,
                operation_id=uuid4(),
                operation_order_id=UUID(order_id),
                expected_resource_version=1,
            )
        assert getattr(error.value, "code", None) == "MACHINE_PROCESS_INCOMPATIBLE"
        db.session.expire_all()
        rolled_back = db.session.get(ScmOrdenOperacion, UUID(order_id))
        assert rolled_back.fabricacion.snapshot_proceso is None
        assert rolled_back.fabricacion.fuente_proceso is None


@pytest.mark.parametrize("state", ["BORRADOR", "PROGRAMADA", "EN_EJECUCION", "CERRADA", "ANULADA"])
def test_replacement_rejects_other_states_without_changes(app, client, scm_config, state):
    order_id, actor_id, _, _ = _seed_released_order(app)
    with app.app_context():
        old = db.session.get(ScmOrdenOperacion, UUID(order_id))
        old.estado = state
        db.session.commit()
    response = _replace(client, order_id, actor_id)
    assert response.status_code == 409
    with app.app_context():
        assert db.session.get(ScmOrdenOperacion, UUID(order_id)).estado == state
        assert ScmEvento.query.filter_by(tipo="OF_REPLACED").count() == 0


@pytest.mark.parametrize("field", ["started_at", "closed_at"])
def test_replacement_rejects_historical_activity(app, client, scm_config, field):
    order_id, actor_id, _, _ = _seed_released_order(app)
    with app.app_context():
        setattr(db.session.get(ScmOrdenOperacion, UUID(order_id)), field, utc_now())
        db.session.commit()
    assert _replace(client, order_id, actor_id).status_code == 409


def test_replacement_rejects_stock_reservation_for_proposal(app, client, scm_config):
    order_id, actor_id, _, _ = _seed_released_order(app)
    with app.app_context():
        old = db.session.get(ScmOrdenOperacion, UUID(order_id))
        plan = old.plan_produccion
        plan.propuesta_json = {**plan.propuesta_json, "reservas_stock": [
            {"propuesta_consumidora_clave": old.propuesta_clave, "cantidad": "10"}
        ]}
        db.session.commit()
    response = _replace(client, order_id, actor_id)
    assert response.status_code == 409, response.json
    with app.app_context():
        assert db.session.get(ScmOrdenOperacion, UUID(order_id)).estado == "LIBERADA"
