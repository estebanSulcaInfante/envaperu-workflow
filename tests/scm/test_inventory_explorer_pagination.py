from decimal import Decimal

from app import db


def _headers(actor_id):
    return {"X-Actor-Id": str(actor_id)}


def test_explorador_kardex_pagina_y_filtra_en_servidor(
    app, client, scm_config,
):
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import (
        ScmSaldoInventario,
        ScmUbicacionInventario,
    )
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
        )
        location = ScmUbicacionInventario(
            codigo="PZ-EXPLORADOR-A1",
            nombre="Posicion piezas A1",
        )
        db.session.add(location)
        db.session.flush()
        for index in range(63):
            article = ScmArticulo(
                codigo=f"PC-EXPLORADOR-{index:04d}",
                nombre=f"Pieza explorador {index:04d}",
                clase="PIEZA_COLOR",
            )
            db.session.add(article)
            db.session.flush()
            db.session.add(ScmSaldoInventario(
                articulo_scm_id=article.id,
                ubicacion_id=location.id,
                cantidad_fisica=Decimal(index + 1),
                cantidad_reservada=Decimal("1") if index % 2 else Decimal("0"),
            ))
        finished = ScmArticulo(
            codigo="PT-EXPLORADOR-0001",
            nombre="Producto terminado fuera del Kardex activo",
            clase="PRODUCTO_TERMINADO",
        )
        db.session.add(finished)
        db.session.flush()
        db.session.add(ScmSaldoInventario(
            articulo_scm_id=finished.id,
            ubicacion_id=location.id,
            cantidad_fisica=Decimal("99"),
        ))
        db.session.commit()
        actor_id = actor.id

    first = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(actor_id),
        query_string={
            "kardex": "PIEZAS_WIP",
            "limite": 25,
            "ordenar": "CODIGO",
        },
    )
    assert first.status_code == 200
    payload = first.get_json()
    assert len(payload["items"]) == 25
    assert payload["page"] == {
        "has_more": True,
        "limit": 25,
        "next_cursor": payload["page"]["next_cursor"],
        "total": 63,
    }
    assert payload["page"]["next_cursor"]
    assert all(
        item["articulo"]["clase"] in {"PIEZA_COLOR", "SUBENSAMBLE_WIP"}
        for item in payload["items"]
    )

    second = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(actor_id),
        query_string={
            "kardex": "PIEZAS_WIP",
            "limite": 25,
            "ordenar": "CODIGO",
            "cursor": payload["page"]["next_cursor"],
        },
    )
    assert second.status_code == 200
    second_payload = second.get_json()
    assert len(second_payload["items"]) == 25
    assert {
        item["id"] for item in payload["items"]
    }.isdisjoint({item["id"] for item in second_payload["items"]})

    searched = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(actor_id),
        query_string={
            "kardex": "PIEZAS_WIP",
            "q": "0059",
            "limite": 25,
        },
    )
    assert searched.status_code == 200
    searched_payload = searched.get_json()
    assert searched_payload["page"]["total"] == 1
    assert searched_payload["items"][0]["articulo"]["codigo"] == (
        "PC-EXPLORADOR-0059"
    )


def test_explorador_rechaza_cursor_de_otro_kardex(
    app, client, scm_config,
):
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import (
        ScmSaldoInventario,
        ScmUbicacionInventario,
    )
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
        )
        location = ScmUbicacionInventario(
            codigo="PZ-CURSOR-A1", nombre="Posicion cursor A1",
        )
        db.session.add(location)
        db.session.flush()
        for index in range(2):
            article = ScmArticulo(
                codigo=f"PC-CURSOR-{index:04d}",
                nombre=f"Pieza cursor {index}",
                clase="PIEZA_COLOR",
            )
            db.session.add(article)
            db.session.flush()
            db.session.add(ScmSaldoInventario(
                articulo_scm_id=article.id,
                ubicacion_id=location.id,
                cantidad_fisica=1,
            ))
        db.session.commit()
        actor_id = actor.id

    page = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(actor_id),
        query_string={"kardex": "PIEZAS_WIP", "limite": 1},
    ).get_json()
    response = client.get(
        "/api/scm/v1/inventario/explorador",
        headers=_headers(actor_id),
        query_string={
            "kardex": "PRODUCTO_TERMINADO",
            "limite": 1,
            "cursor": page["page"]["next_cursor"],
        },
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "INVALID_INVENTORY_CURSOR"
