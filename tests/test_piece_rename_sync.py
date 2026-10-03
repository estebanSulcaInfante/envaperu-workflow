"""Contrato de renombrado del maestro y propagación condicional al catálogo."""

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models.molde import Pieza
from app.models.orden import SnapshotComposicionMolde
from app.models.producto import PiezaColor
from app.models.scm_articulos import (
    ScmArticulo,
    ScmArticuloPiezaColor,
)
from app.services.catalog_name_service import nombre_pieza_color


def _crear_pieza_con_variantes(client, nombre="Tapa ámbar"):
    pieza_resp = client.post("/api/piezas", json={
        "nombre": nombre,
        "peso_nominal_gr": 11.5,
        "linea_id": 1,
        "familia_id": 1,
    })
    assert pieza_resp.status_code == 201, pieza_resp.get_json()
    pieza = pieza_resp.get_json()
    molde_resp = client.post("/api/moldes", json={
        "nombre": f"Molde {nombre}",
        "peso_tiro_gr": 80,
    })
    assert molde_resp.status_code == 201, molde_resp.get_json()
    forma_resp = client.post(
        f"/api/moldes/{molde_resp.get_json()['codigo']}/formas",
        json={
            "pieza_id": pieza["id"],
            "cavidades": 4,
            "peso_unitario_gr": 11.5,
        },
    )
    assert forma_resp.status_code == 201, forma_resp.get_json()
    forma_id = forma_resp.get_json()["id"]
    color_resp = client.post("/api/colores", json={"nombre": "AZUL ÁRTICO"})
    assert color_resp.status_code in (200, 201), color_resp.get_json()
    variant_resp = client.post(
        f"/api/formas/{forma_id}/colores",
        json={"color_id": color_resp.get_json()["id"]},
    )
    assert variant_resp.status_code == 201, variant_resp.get_json()
    return pieza, variant_resp.get_json()


def _articulo_de_sku(sku):
    link = ScmArticuloPiezaColor.query.filter_by(pieza_color_sku=sku).one()
    return db.session.get(ScmArticulo, link.articulo_id)


def test_composicion_conserva_sufijo_unicode_mojibake_literal():
    assert nombre_pieza_color("Pieza", "S\u00c3\u201cLIDO") == "Pieza S\u00c3\u201cLIDO"


def test_put_pieza_color_incrementa_version_con_actualizacion_cas(client, app):
    pieza, variant = _crear_pieza_con_variantes(client)
    response = client.put(
        f"/api/piezas-color/{variant['sku']}",
        json={"nombre": "Nombre manual"},
    )
    assert response.status_code == 200, response.get_json()
    with app.app_context():
        updated = db.session.get(PiezaColor, variant["sku"])
        assert updated.piezas == "Nombre manual"
        assert updated.version == 2


def test_renombrar_pieza_sincroniza_solo_nombres_generados_y_conserva_snapshots(
    client,
    app,
):
    pieza, variant = _crear_pieza_con_variantes(client)
    with app.app_context():
        pc = db.session.get(PiezaColor, variant["sku"])
        articulo = _articulo_de_sku(variant["sku"])
        pc.peso = 13.25
        pc.cod_extru = 42
        pc.estado_revision = "VERIFICADO"
        articulo.version = 3
        db.session.add(SnapshotComposicionMolde(
            orden_id="OP-RENOMBRE-HISTORICO",
            pieza_id=pieza["id"],
            pieza_codigo_snapshot=pieza["codigo"],
            pieza_nombre_snapshot=pieza["nombre"],
            cavidades=4,
            peso_unit_gr=11.5,
        ))
        db.session.commit()
        pc_version, articulo_version = pc.version, articulo.version

    response = client.put(f"/api/piezas/{pieza['id']}", json={
        "version": pieza["version"],
        "nombre": "Tapa ámbar nueva",
        "peso_nominal_gr": 12,
    })

    assert response.status_code == 200, response.get_json()
    with app.app_context():
        pc = db.session.get(PiezaColor, variant["sku"])
        articulo = _articulo_de_sku(variant["sku"])
        snapshot = SnapshotComposicionMolde.query.one()
        color_suffix = variant["nombre"][len(pieza["nombre"]) + 1:]
        assert pc.piezas == f"Tapa \u00e1mbar nueva {color_suffix}"
        assert pc.version == pc_version + 1
        assert (pc.peso, pc.cod_extru, pc.estado_revision) == (13.25, 42, "VERIFICADO")
        assert articulo.nombre == pc.piezas
        assert articulo.version == articulo_version + 1
        assert snapshot.pieza_nombre_snapshot == "Tapa ámbar"
        assert snapshot.pieza_codigo_snapshot == pieza["codigo"]


def test_renombrar_pieza_preserva_pc_personalizado_y_articulo_personalizado(
    client,
    app,
):
    pieza, variant = _crear_pieza_con_variantes(client, "Tapa manual")
    with app.app_context():
        pc = db.session.get(PiezaColor, variant["sku"])
        pc.piezas = "Nombre manual de variante"
        articulo = _articulo_de_sku(variant["sku"])
        articulo.nombre = "Nombre manual de artículo"
        db.session.commit()
        pc_version = pc.version
        articulo_version = articulo.version

    response = client.put(f"/api/piezas/{pieza['id']}", json={
        "version": pieza["version"],
        "nombre": "Tapa renombrada",
        "peso_nominal_gr": 11.5,
    })

    assert response.status_code == 200, response.get_json()
    with app.app_context():
        pc = db.session.get(PiezaColor, variant["sku"])
        articulo = _articulo_de_sku(variant["sku"])
        assert pc.piezas == "Nombre manual de variante"
        assert pc.version == pc_version
        assert articulo.nombre == "Nombre manual de artículo"
        assert articulo.version == articulo_version


def test_renombrado_repetido_y_pieza_no_relacionada_son_idempotentes(client, app):
    pieza_a, variant_a = _crear_pieza_con_variantes(client, "Tapa A")
    pieza_b, variant_b = _crear_pieza_con_variantes(client, "Tapa B")
    first = client.put(f"/api/piezas/{pieza_a['id']}", json={
        "version": pieza_a["version"], "nombre": "Tapa A nueva",
        "peso_nominal_gr": 11.5,
    })
    assert first.status_code == 200, first.get_json()
    repeated = client.put(f"/api/piezas/{pieza_a['id']}", json={
        "version": first.get_json()["version"], "nombre": "Tapa A nueva",
        "peso_nominal_gr": 11.5,
    })
    assert repeated.status_code == 200, repeated.get_json()
    with app.app_context():
        assert db.session.get(PiezaColor, variant_a["sku"]).version == 2
        assert db.session.get(PiezaColor, variant_b["sku"]).piezas == variant_b["nombre"]


def test_renombrado_pieza_rechaza_version_obsoleta_sin_mutar_relaciones(client, app):
    pieza, variant = _crear_pieza_con_variantes(client)
    response = client.put(f"/api/piezas/{pieza['id']}", json={
        "version": pieza["version"] + 1,
        "nombre": "No debe persistir",
        "peso_nominal_gr": 99,
    })
    assert response.status_code == 409
    assert response.get_json()["codigo"] == "VERSION_CONFLICT"
    with app.app_context():
        assert db.session.get(Pieza, pieza["id"]).nombre == pieza["nombre"]
        assert db.session.get(PiezaColor, variant["sku"]).piezas == variant["nombre"]


@pytest.mark.parametrize("longitud", [201])
def test_renombrado_rechaza_base_y_derivado_mayor_a_200_sin_mutar(
    client,
    app,
    longitud,
):
    pieza, variant = _crear_pieza_con_variantes(client, "P" * 180)
    response = client.put(f"/api/piezas/{pieza['id']}", json={
        "version": pieza["version"],
        "nombre": "N" * longitud,
        "peso_nominal_gr": 11.5,
    })
    assert response.status_code == 400
    assert response.get_json()["codigo"] == "NOMBRE_DEMASIADO_LARGO"
    with app.app_context():
        assert db.session.get(Pieza, pieza["id"]).nombre == pieza["nombre"]
        assert db.session.get(PiezaColor, variant["sku"]).piezas == variant["nombre"]


def test_renombrado_rechaza_derivado_demasiado_largo_antes_de_mutar(client, app):
    pieza, variant = _crear_pieza_con_variantes(client, "Pieza")
    nuevo_nombre = "N" * 195
    response = client.put(f"/api/piezas/{pieza['id']}", json={
        "version": pieza["version"],
        "nombre": nuevo_nombre,
        "peso_nominal_gr": 11.5,
    })
    assert response.status_code == 400
    assert response.get_json()["codigo"] == "NOMBRE_DERIVADO_DEMASIADO_LARGO"
    with app.app_context():
        assert db.session.get(Pieza, pieza["id"]).nombre == pieza["nombre"]
        assert db.session.get(PiezaColor, variant["sku"]).piezas == variant["nombre"]


def test_fallo_tardio_en_articulo_revierte_pieza_y_variante(client, app):
    pieza, variant = _crear_pieza_con_variantes(client)
    with app.app_context():
        db.session.execute(text("""
            CREATE TRIGGER fail_article_rename
            BEFORE UPDATE OF nombre ON scm_articulo
            BEGIN SELECT RAISE(ABORT, 'forced article rename failure'); END
        """))
        db.session.commit()

    response = client.put(f"/api/piezas/{pieza['id']}", json={
        "version": pieza["version"], "nombre": "Tapa fallida",
        "peso_nominal_gr": 11.5,
    })

    assert response.status_code == 400
    with app.app_context():
        assert db.session.get(Pieza, pieza["id"]).nombre == pieza["nombre"]
        assert db.session.get(PiezaColor, variant["sku"]).piezas == variant["nombre"]
        assert _articulo_de_sku(variant["sku"]).nombre == variant["nombre"]


def test_articulo_personalizado_se_preserva_aunque_pc_sea_generado(client, app):
    pieza, variant = _crear_pieza_con_variantes(client)
    with app.app_context():
        articulo = _articulo_de_sku(variant['sku'])
        articulo.nombre = 'Articulo personalizado'
        db.session.commit()
        article_version = articulo.version
    response = client.put(f"/api/piezas/{pieza['id']}", json={
        'version': pieza['version'], 'nombre': 'Nueva base',
    })
    assert response.status_code == 200, response.get_json()
    with app.app_context():
        pc = db.session.get(PiezaColor, variant['sku'])
        assert pc.piezas == nombre_pieza_color('Nueva base', pc.color_produccion_rel.nombre)
        articulo = _articulo_de_sku(variant['sku'])
        assert articulo.nombre == 'Articulo personalizado'
        assert articulo.version == article_version
