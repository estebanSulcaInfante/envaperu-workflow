"""Cold-read coverage for the opt-in compact assembly-order list."""

import json
from uuid import uuid4

from sqlalchemy import event

from app import db
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.services.scm_assembly_order_service import (
    get_assembly_order,
    list_assembly_orders,
)
from app.services.scm_service_support import ScmServiceError


def test_summary_is_compact_and_detail_contract_is_preserved(app, scm_config):
    from test_scm_inline_assembly import _seed_concurrent_wip_flow

    with app.app_context():
        actor, order, _center, _work, _article, _component = _seed_concurrent_wip_flow()
        db.session.expire_all()
        summary = list_assembly_orders(
            db.session,
            actor_id=actor.id,
            view="resumen",
            q=order.codigo,
            estado="LIBERADA",
            pagina=1,
            tamano=25,
        )
        detail = get_assembly_order(db.session, actor_id=actor.id, order_id=order.id)

    assert summary["page"] == {"pagina": 1, "tamano": 25, "total": 1, "paginas": 1}
    item = summary["items"][0]
    assert item["codigo"] == order.codigo
    assert item["salida"]["cantidad_objetivo"] == "10.000"
    assert "entradas_planificadas" not in item
    assert "cierre_kg" not in item
    assert "lote_salida" not in item
    assert detail["entradas_planificadas"]
    assert "cierre_kg" in detail


def test_summary_does_not_invoke_full_serializer(app, scm_config, monkeypatch):
    from test_scm_inline_assembly import _seed_concurrent_wip_flow

    with app.app_context():
        actor, order, *_rest = _seed_concurrent_wip_flow()

        def fail_full_serializer(*_args, **_kwargs):
            raise AssertionError("compact OA summary invoked the full serializer")

        monkeypatch.setattr(
            "app.services.scm_assembly_order_service._serialize",
            fail_full_serializer,
        )
        payload = list_assembly_orders(
            db.session,
            actor_id=actor.id,
            view="resumen",
            q=order.codigo,
        )
        wildcard = list_assembly_orders(
            db.session,
            actor_id=actor.id,
            view="resumen",
            q="%",
        )

    assert payload["items"][0]["codigo"] == order.codigo
    assert wildcard["page"]["total"] == 0


def test_summary_paginates_valid_order_ids_before_output_join(app, scm_config):
    from test_scm_inline_assembly import _seed_concurrent_wip_flow

    with app.app_context():
        actor, order, *_rest = _seed_concurrent_wip_flow()
        output = order.salidas[0]
        db.session.add(ScmOrdenOperacionSalida(
            orden_operacion_id=order.id,
            articulo_scm_id=output.articulo_scm_id,
            cantidad_objetivo=output.cantidad_objetivo,
        ))
        db.session.commit()
        db.session.expire_all()
        try:
            list_assembly_orders(
                db.session,
                actor_id=actor.id,
                view="resumen",
                pagina=1,
                tamano=25,
            )
        except ScmServiceError as error:
            assert error.code == "OA_OUTPUT_INVALID"
            assert error.status_code == 409
            assert error.details["order_id"] == str(order.id)
        else:
            raise AssertionError("OA con dos salidas debe conservar el 409 de detalle")

        try:
            get_assembly_order(db.session, actor_id=actor.id, order_id=order.id)
        except ScmServiceError as error:
            assert error.code == "OA_OUTPUT_INVALID"
            assert error.status_code == 409
        else:
            raise AssertionError("El detalle de OA debe conservar el 409 de cardinalidad")


def test_summary_requires_oa_view_permission(app, client, scm_config):
    response = client.get(
        "/api/scm/v1/ordenes-armado",
        query_string={"vista": "resumen"},
        headers={"X-Actor-Id": "999999"},
    )

    assert response.status_code == 403


def test_summary_validates_filters_and_page_size(app, scm_config):
    from test_scm_inline_assembly import _seed_concurrent_wip_flow

    with app.app_context():
        actor, _order, *_rest = _seed_concurrent_wip_flow()
        for kwargs, code in [
            ({"view": "compacto"}, "OA_VIEW_INVALID"),
            ({"view": "resumen", "tamano": 20}, "OA_PAGINATION_INVALID"),
            ({"view": "resumen", "estado": "NO_EXISTE"}, "OA_STATE_INVALID"),
        ]:
            try:
                list_assembly_orders(db.session, actor_id=actor.id, **kwargs)
            except ScmServiceError as error:
                assert error.code == code
            else:
                raise AssertionError(f"expected {code}")


def test_summary_route_forwards_server_filters_and_pagination(app, client, scm_config):
    from test_scm_inline_assembly import _seed_concurrent_wip_flow

    with app.app_context():
        actor, order, *_rest = _seed_concurrent_wip_flow()
        response = client.get(
            "/api/scm/v1/ordenes-armado",
            headers={"X-Actor-Id": str(actor.id)},
            query_string={
                "vista": "resumen",
                "q": order.codigo,
                "estado": "LIBERADA",
                "pagina": "1",
                "tamano": "25",
            },
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["page"] == {"pagina": 1, "tamano": 25, "total": 1, "paginas": 1}
    assert payload["items"][0]["codigo"] == order.codigo


def test_summary_sql_and_bytes_stay_bounded_for_cold_1_25_100_orders(
    app, scm_config,
):
    from test_scm_inline_assembly import _seed_concurrent_wip_flow

    with app.app_context():
        actor, prototype, _center, _work, _article, _component = _seed_concurrent_wip_flow()
        metrics = {}
        for amount in (1, 25, 100):
            extras = []
            for index in range(amount - 1):
                extra = ScmOrdenOperacion(
                    codigo=f"OA-COLD-{amount:03d}-{index:03d}",
                    tipo="ENSAMBLE",
                    origen_demanda="REPOSICION_WIP",
                    estado="LIBERADA",
                    operacion_ruta_revision_id=prototype.operacion_ruta_revision_id,
                    operacion_ruta_hash=prototype.operacion_ruta_hash,
                    created_by_id=actor.id,
                    salidas=[ScmOrdenOperacionSalida(
                        articulo_scm_id=prototype.salidas[0].articulo_scm_id,
                        cantidad_objetivo=10,
                    )],
                )
                extras.append(extra)
            db.session.add_all(extras)
            db.session.commit()
            db.session.expire_all()

            query_count = 0

            def count_query(*_args):
                nonlocal query_count
                query_count += 1

            event.listen(db.engine, "before_cursor_execute", count_query)
            try:
                summary = list_assembly_orders(
                    db.session,
                    actor_id=actor.id,
                    view="resumen",
                    pagina=1,
                    tamano=100,
                )
            finally:
                event.remove(db.engine, "before_cursor_execute", count_query)
            legacy = list_assembly_orders(db.session, actor_id=actor.id)
            metrics[amount] = {
                "summary_queries": query_count,
                "summary_bytes": len(json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str)),
                "legacy_bytes": len(json.dumps(legacy, sort_keys=True, separators=(",", ":"), default=str)),
            }
            assert summary["page"]["total"] == amount
            for extra in extras:
                db.session.delete(extra)
            db.session.commit()

        assert metrics[100]["summary_queries"] <= metrics[1]["summary_queries"] + 4
        assert metrics[100]["summary_queries"] <= metrics[25]["summary_queries"] + 4
        print({"assembly_summary_cold_1_25_100": metrics})
