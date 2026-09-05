from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4
import pytest

from app import db
from app.models.producto import ColorBase, ColorProduccion, FamiliaColor, ProductoTerminado
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_auditoria import ScmEvento
from app.models.scm_articulos import ScmArticuloProducto
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
from app.models.trabajador import RolOperativo, Trabajador


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
