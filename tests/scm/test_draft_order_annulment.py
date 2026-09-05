from uuid import uuid4

import pytest

from app import db
from app.models.scm_auditoria import ScmEvento
from app.models.scm_production_orders import ScmOrdenOperacion, ScmOrdenFabricacion, ScmCorridaFabricacion
from app.models.trabajador import RolOperativo, Trabajador


@pytest.fixture
def draft(app, scm_config):
    def create(kind, state="BORRADOR", authorized=True):
        with app.app_context():
            actor = Trabajador.query.filter_by(codigo="TRB-01").one()
            if authorized:
                actor.roles.append(RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one())
            order = ScmOrdenOperacion(codigo=f"{kind}-TEST", tipo=(
                "FABRICACION" if kind == "OF" else "ENSAMBLE"
            ), origen_demanda="PRUEBA_TECNICA", estado=state, created_by_id=actor.id)
            if kind == "OF":
                fabrication = ScmOrdenFabricacion(orden_operacion=order)
                fabrication.corridas.append(ScmCorridaFabricacion(codigo="OF-TEST-C01", secuencia=1))
            db.session.add(order)
            db.session.commit()
            return str(order.id), actor.id
    return create


def command(client, kind, order_id, actor_id, payload=None, key=None):
    resource = "ordenes-fabricacion" if kind == "OF" else "ordenes-armado"
    return client.post(f"/api/scm/v1/{resource}/{order_id}/anular", json=(
        payload if payload is not None else {"version": 1, "motivo": "Borrador duplicado"}
    ), headers={"X-Actor-Id": str(actor_id), "Idempotency-Key": key or str(uuid4())})


@pytest.mark.parametrize("kind", ["OF", "OA"])
def test_annul_preserves_order_and_replays(app, client, draft, kind):
    order_id, actor_id = draft(kind)
    key = str(uuid4())
    response = command(client, kind, order_id, actor_id, key=key)
    assert response.status_code == 200, response.json
    assert response.json["estado"] == "ANULADA"
    assert command(client, kind, order_id, actor_id, key=key).json == response.json
    if kind == "OF":
        detail = client.get(f"/api/scm/v1/ordenes-fabricacion/{order_id}", headers={"X-Actor-Id": str(actor_id)})
        assert detail.status_code == 200, detail.json
        assert detail.json["anulacion"]["motivo"] == "Borrador duplicado"
        assert detail.json["corridas"][0]["estado"] == "ANULADA"
    with app.app_context():
        from uuid import UUID
        order = db.session.get(ScmOrdenOperacion, UUID(order_id))
        assert order.version == 2
        assert order.codigo == f"{kind}-TEST"
        events = ScmEvento.query.filter_by(aggregate_id=order_id, tipo=f"{kind}_ANNULLED").all()
        assert len(events) == 1
        assert events[0].before_json["estado"] == "BORRADOR"
        assert events[0].after_json["anulacion"]["motivo"] == "Borrador duplicado"


@pytest.mark.parametrize("kind", ["OF", "OA"])
@pytest.mark.parametrize("state", ["LIBERADA", "PROGRAMADA", "EN_EJECUCION", "CERRADA", "ANULADA"])
def test_non_draft_blocked(client, draft, kind, state):
    order_id, actor_id = draft(kind, state)
    assert command(client, kind, order_id, actor_id).status_code == 409


@pytest.mark.parametrize("kind", ["OF", "OA"])
@pytest.mark.parametrize("payload,status", [
    ({"version": 1, "motivo": "  "}, 400),
    ({"version": 2, "motivo": "Error"}, 409),
    ({"version": 1, "motivo": "x" * 501}, 400),
])
def test_invalid_command(client, draft, kind, payload, status):
    order_id, actor_id = draft(kind)
    assert command(client, kind, order_id, actor_id, payload).status_code == status


@pytest.mark.parametrize("kind", ["OF", "OA"])
def test_permission_required(client, draft, kind):
    order_id, actor_id = draft(kind, authorized=False)
    assert command(client, kind, order_id, actor_id).status_code == 403


@pytest.mark.parametrize("kind", ["OF", "OA"])
@pytest.mark.parametrize("has_credit", [False, True])
def test_only_own_uncredited_allocations_released(app, client, draft, kind, has_credit):
    from datetime import date
    from uuid import UUID
    from app.models.producto import ProductoTerminado
    from app.models.scm_articulos import ScmArticuloProducto
    from app.models.scm_production_orders import (
        ScmOrdenProduccion, ScmOrdenProduccionLinea, ScmOrdenOperacionSalida,
        ScmAsignacionDemandaSuministro,
    )
    order_id, actor_id = draft(kind)
    with app.app_context():
        db.session.add(ProductoTerminado(cod_sku_pt="PT-ANNUL", producto="Test", linea_id=1, familia_id=1))
        db.session.flush()
        article = ScmArticuloProducto.query.filter_by(producto_terminado_id="PT-ANNUL").one()
        demand = ScmOrdenProduccion(codigo="OP-ANNUL", origen="PLANIFICACION", estado="PLANIFICADA",
                                   fecha_necesidad=date(2026, 9, 5), created_by_id=actor_id)
        line = ScmOrdenProduccionLinea(producto_terminado_id="PT-ANNUL", cantidad_solicitada=100)
        demand.lineas.append(line)
        other = ScmOrdenOperacion(codigo="OF-OTHER", tipo="FABRICACION", origen_demanda="PRUEBA_TECNICA")
        db.session.add_all([demand, other])
        owned = ScmOrdenOperacionSalida(orden_operacion_id=UUID(order_id), articulo_scm_id=article.articulo_id, cantidad_objetivo=60)
        untouched = ScmOrdenOperacionSalida(orden_operacion=other, articulo_scm_id=article.articulo_id, cantidad_objetivo=40)
        allocation = ScmAsignacionDemandaSuministro(orden_produccion_linea=line, fuente_tipo="SALIDA_ORDEN",
            orden_operacion_salida=owned, cantidad_planificada=60, cantidad_comprometida=1 if has_credit else 0)
        other_allocation = ScmAsignacionDemandaSuministro(orden_produccion_linea=line, fuente_tipo="SALIDA_ORDEN",
            orden_operacion_salida=untouched, cantidad_planificada=40)
        db.session.add_all([allocation, other_allocation])
        db.session.commit()
        aid, oid = allocation.id, other_allocation.id
    response = command(client, kind, order_id, actor_id)
    assert response.status_code == (409 if has_credit else 200), response.json
    with app.app_context():
        assert db.session.get(ScmAsignacionDemandaSuministro, aid).estado == ("PLANIFICADA" if has_credit else "CANCELADA")
        assert db.session.get(ScmAsignacionDemandaSuministro, oid).estado == "PLANIFICADA"
        assert db.session.get(ScmAsignacionDemandaSuministro, aid).cantidad_planificada == 60
    if not has_credit:
        assert response.json["ordenes_produccion"][0]["estado"] == "PLANIFICADA"


@pytest.mark.parametrize("kind", ["OF", "OA"])
def test_prior_release_blocks_even_if_state_is_draft(app, client, draft, kind):
    from uuid import UUID
    from app.models.scm_production_orders import utc_now
    order_id, actor_id = draft(kind)
    with app.app_context():
        db.session.get(ScmOrdenOperacion, UUID(order_id)).released_at = utc_now()
        db.session.commit()
    assert command(client, kind, order_id, actor_id).status_code == 409


@pytest.mark.parametrize("kind", ["OF", "OA"])
def test_linked_ot_blocks_annulment(app, client, draft, kind):
    from uuid import UUID
    from datetime import date
    from app.models.registro import RegistroDiarioProduccion
    order_id, actor_id = draft(kind)
    with app.app_context():
        db.session.add(RegistroDiarioProduccion(orden_operacion_id=UUID(order_id),
            fecha=date(2026, 9, 5), turno="DIA", estado="PLANIFICADA", maquina_id=1))
        db.session.commit()
    response = command(client, kind, order_id, actor_id)
    assert response.status_code == 409, response.json
    assert response.json["error"]["code"] == "ORDER_HAS_DEPENDENCIES"
