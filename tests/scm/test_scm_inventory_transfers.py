from decimal import Decimal
from uuid import uuid4

from app import db


def _headers(actor_id, key=None):
    return {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(key or uuid4()),
    }


def test_pickup_multi_qr_mueve_custodia_sin_consumir(app, client, scm_config):
    from app.models.scm_articulos import ScmArticulo
    from app.models.scm_inventory import (
        ScmMovimientoInventario, ScmSaldoInventario, ScmUbicacionInventario,
    )
    from app.models.scm_ot import ScmManga
    from app.models.scm_warehouse import ScmExistenciaManga
    from app.models.trabajador import RolOperativo, Trabajador

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one())
        db.session.commit()
        actor_id = actor.id

    warehouse = client.post(
        "/api/scm/v1/almacenes", headers=_headers(actor_id),
        json={"codigo": "ALM-QR", "nombre": "Almacen QR", "tipo": "PIEZAS_WIP"},
    ).get_json()
    origin = client.post(
        f"/api/scm/v1/almacenes/{warehouse['id']}/ubicaciones",
        headers=_headers(actor_id),
        json={"codigo": "PICK-QR", "nombre": "Picking QR", "tipo": "POSICION", "clases_articulo": ["PIEZA_COLOR"]},
    ).get_json()
    destination = client.post(
        f"/api/scm/v1/almacenes/{warehouse['id']}/ubicaciones",
        headers=_headers(actor_id),
        json={"codigo": "MESA-QR", "nombre": "Mesa QR", "tipo": "PUNTO_PRODUCCION", "clases_articulo": ["PIEZA_COLOR"]},
    ).get_json()

    with app.app_context():
        article = ScmArticulo(codigo="PC-QR-018", nombre="Pieza QR", clase="PIEZA_COLOR")
        db.session.add(article)
        db.session.flush()
        balance = ScmSaldoInventario(
            articulo_scm_id=article.id, ubicacion_id=origin["id"], cantidad_fisica=20,
            cantidad_reservada=20,
        )
        db.session.add(balance)
        db.session.flush()
        for index in (1, 2):
            manga = ScmManga(
                codigo=f"MANGA-QR-{index}", ot_id=900 + index,
                plan_linea_id=900 + index, lote_articulo_id=900 + index,
                secuencia_ot=index, estado="RECIBIDA",
                cantidad_planificada_un=10, cantidad_asignada_un=10,
                cantidad_confirmada_un=10, cantidad_contenida_un=10,
                maquinista_previsto_id=actor_id,
                articulo_codigo_snapshot=article.codigo,
                articulo_nombre_snapshot=article.nombre,
                regla_revision_id_snapshot=900 + index,
                regla_hash_snapshot="a" * 64,
                tipo_contenedor_codigo_snapshot="MANGA",
                tipo_contenedor_nombre_snapshot="Manga",
                peso_unitario_snapshot_g=10,
                tara_nominal_g_snapshot=100,
                tolerancia_tara_g_snapshot=5,
                peso_bruto_max_kg_snapshot=20,
                created_by_id=actor_id,
            )
            db.session.add(manga)
            db.session.flush()
            db.session.add(ScmExistenciaManga(
                manga_id=manga.id, etiqueta_resuelta_id=900 + index,
                articulo_scm_id=article.id, saldo_id=balance.id,
                ubicacion_id=origin["id"], movimiento_ingreso_id=uuid4(),
                operation_id=uuid4(), resuelta_por="QR_FINAL",
                estado_logistico="RESERVADA", estado_calidad="LIBERADA",
                cantidad_fisica=10, cantidad_reservada=10,
                peso_neto_snapshot_kg=1, recibida_por_id=actor_id,
            ))
        db.session.commit()

    opened = client.post(
        "/api/scm/v1/operaciones-almacen/sesiones",
        headers=_headers(actor_id),
        json={
            "tipo": "TRANSFERENCIA", "modalidad": "PICKUP",
            "origen_ubicacion_id": origin["id"],
            "destino_ubicacion_id": destination["id"],
        },
    )
    assert opened.status_code == 201
    session = opened.get_json()
    for code in ("MANGA-QR-1", "MANGA-QR-2"):
        scanned = client.post(
            f"/api/scm/v1/operaciones-almacen/sesiones/{session['id']}/escanear",
            headers=_headers(actor_id), json={"codigo": code},
        )
        assert scanned.status_code == 200
        session = scanned.get_json()
    assert len(session["items"]) == 2

    removed = client.delete(
        f"/api/scm/v1/operaciones-almacen/sesiones/{session['id']}/items/{session['items'][1]['id']}",
        headers=_headers(actor_id), json={"version": session["version"]},
    )
    assert removed.status_code == 200
    session = removed.get_json()
    assert [item["manga_codigo"] for item in session["items"]] == ["MANGA-QR-1"]
    rescanned = client.post(
        f"/api/scm/v1/operaciones-almacen/sesiones/{session['id']}/escanear",
        headers=_headers(actor_id), json={"codigo": "MANGA-QR-2"},
    )
    assert rescanned.status_code == 200
    session = rescanned.get_json()

    operation_id = uuid4()
    confirmed = client.post(
        f"/api/scm/v1/operaciones-almacen/sesiones/{session['id']}/confirmar",
        headers=_headers(actor_id, operation_id),
        json={"version": session["version"], "custodio_id": actor_id},
    )
    assert confirmed.status_code == 201
    transfer = confirmed.get_json()
    assert transfer["estado"] == "CERRADA"
    assert len(transfer["items"]) == 2

    replay = client.post(
        f"/api/scm/v1/operaciones-almacen/sesiones/{session['id']}/confirmar",
        headers=_headers(actor_id, operation_id),
        json={"version": session["version"], "custodio_id": actor_id},
    )
    assert replay.status_code == 201
    assert replay.get_json() == transfer

    trace = client.get(
        "/api/scm/v1/unidades-logisticas/MANGA-QR-1/trazabilidad",
        headers={"X-Actor-Id": str(actor_id)},
    )
    assert trace.status_code == 200
    trace_payload = trace.get_json()
    assert trace_payload["estado_logistico"] == "EN_STAGING_ARMADO"
    assert trace_payload["transferencias"][0]["codigo"] == transfer["codigo"]
    assert {movement["tipo"] for movement in trace_payload["movimientos"]} == {
        "TRASLADO_SALIDA", "TRASLADO_ENTRADA",
    }

    with app.app_context():
        balances = ScmSaldoInventario.query.all()
        assert sum(Decimal(item.cantidad_fisica) for item in balances) == Decimal("20")
        assert ScmMovimientoInventario.query.filter_by(tipo="TRASLADO_SALIDA").count() == 2
        assert ScmMovimientoInventario.query.filter_by(tipo="TRASLADO_ENTRADA").count() == 2
        assert {item.estado_logistico for item in ScmExistenciaManga.query.all()} == {"EN_STAGING_ARMADO"}

    prepared_return = client.post(
        f"/api/scm/v1/transferencias/{transfer['id']}/retorno",
        headers=_headers(actor_id), json={"existencia_ids": [transfer["items"][0]["existencia_id"]]},
    )
    assert prepared_return.status_code == 201
    return_session = prepared_return.get_json()
    assert return_session["tipo"] == "RETORNO"
    assert return_session["estado"] == "LISTA"
    assert len(return_session["items"]) == 1
    with app.app_context():
        assert ScmMovimientoInventario.query.count() == 4
        states = {item.manga.codigo: item.estado_logistico for item in ScmExistenciaManga.query.all()}
        assert states["MANGA-QR-1"] == "PENDIENTE_RETORNO"
