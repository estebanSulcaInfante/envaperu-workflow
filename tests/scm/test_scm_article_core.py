from app.extensions import db
from app.models.producto import PiezaColor, ProductoTerminado
from app.models.scm_articulos import (
    CLASE_PIEZA_COLOR,
    CLASE_PRODUCTO_TERMINADO,
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
    ScmArticuloPiezaColor,
    ScmArticuloProducto,
    ScmDefinicionWip,
)
from app.models.scm_catalogos import ScmCapacidad
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_article_service import sync_catalog_articles
from app.services.scm_configuration import ensure_initial_scm_configuration


def _legacy_catalog_rows():
    pieza = PiezaColor(
        sku="PC-R1-000001",
        linea_id=1,
        familia_id=1,
        piezas="Asa de balde",
    )
    producto = ProductoTerminado(
        cod_sku_pt="PT-R1-000001",
        linea_id=1,
        familia_id=1,
        producto="Balde terminado",
        um="UN",
    )
    db.session.add_all([pieza, producto])
    db.session.commit()
    return pieza, producto


def test_catalogos_hacen_dual_write_1_a_1_y_sync_es_idempotente(app):
    with app.app_context():
        pieza, producto = _legacy_catalog_rows()

        first = sync_catalog_articles(db.session)
        db.session.commit()
        second = sync_catalog_articles(db.session)
        db.session.commit()

        assert first.articulos_creados == 0
        assert first.subtipos_creados == 0
        assert second.articulos_creados == 0
        assert second.subtipos_creados == 0

        pieza_link = ScmArticuloPiezaColor.query.one()
        producto_link = ScmArticuloProducto.query.one()
        assert pieza_link.pieza_color_sku == pieza.sku
        assert pieza_link.articulo.clase == CLASE_PIEZA_COLOR
        assert pieza_link.articulo.codigo == pieza.sku
        assert producto_link.producto_terminado_id == producto.cod_sku_pt
        assert producto_link.articulo.clase == CLASE_PRODUCTO_TERMINADO
        assert producto_link.articulo.codigo == producto.cod_sku_pt
        assert ScmDefinicionWip.query.count() == 0


def test_api_crea_wip_con_codigo_correlativo_y_autorizacion_server_side(
    app,
    client,
):
    with app.app_context():
        ensure_initial_scm_configuration()
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        ingenieria = RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
        actor.roles.append(ingenieria)
        db.session.commit()
        actor_id = actor.id

    response = client.post(
        "/api/scm/v1/articulos/wip",
        headers={"X-Actor-Id": str(actor_id)},
        json={
            "nombre": "Balde con asa prearmada",
            "descripcion": "WIP normalizado previo al ensamble final",
            "requiere_calidad": False,
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["codigo"] == "WIP-000001"
    assert payload["clase"] == CLASE_SUBENSAMBLE_WIP
    assert payload["unidad_base"] == "UN"
    assert payload["wip"]["requiere_calidad"] is False

    with app.app_context():
        article = ScmArticulo.query.filter_by(codigo="WIP-000001").one()
        assert article.definicion_wip.descripcion.startswith("WIP normalizado")


def test_api_rechaza_alta_wip_sin_capacidad(app, client):
    with app.app_context():
        ensure_initial_scm_configuration()
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        assert actor.tiene_capacidad("ARTICULO_ADMINISTRAR") is False
        actor_id = actor.id

    response = client.post(
        "/api/scm/v1/articulos/wip",
        headers={"X-Actor-Id": str(actor_id)},
        json={"nombre": "WIP no autorizado"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"
    with app.app_context():
        assert ScmDefinicionWip.query.count() == 0


def test_api_edita_inactiva_y_reactiva_wip_con_version_optimista(
    app,
    client,
):
    with app.app_context():
        ensure_initial_scm_configuration()
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
        )
        db.session.commit()
        actor_id = actor.id

    created = client.post(
        "/api/scm/v1/articulos/wip",
        headers={"X-Actor-Id": str(actor_id)},
        json={"nombre": "Prearmado inicial"},
    ).get_json()
    edited = client.patch(
        f"/api/scm/v1/articulos/wip/{created['id']}",
        headers={"X-Actor-Id": str(actor_id)},
        json={
            "version": created["version"],
            "nombre": "Prearmado corregido",
            "descripcion": "Balde con asa colocada",
            "requiere_calidad": True,
        },
    )

    assert edited.status_code == 200, edited.get_json()
    edited_payload = edited.get_json()
    assert edited_payload["nombre"] == "Prearmado corregido"
    assert edited_payload["wip"] == {
        "descripcion": "Balde con asa colocada",
        "requiere_calidad": True,
    }
    assert edited_payload["version"] == created["version"] + 1

    stale = client.patch(
        f"/api/scm/v1/articulos/wip/{created['id']}",
        headers={"X-Actor-Id": str(actor_id)},
        json={"version": created["version"], "activo": False},
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "STALE_VERSION"

    inactive = client.patch(
        f"/api/scm/v1/articulos/wip/{created['id']}",
        headers={"X-Actor-Id": str(actor_id)},
        json={"version": edited_payload["version"], "activo": False},
    )
    assert inactive.status_code == 200
    assert inactive.get_json()["activo"] is False

    reactivated = client.patch(
        f"/api/scm/v1/articulos/wip/{created['id']}",
        headers={"X-Actor-Id": str(actor_id)},
        json={
            "version": inactive.get_json()["version"],
            "activo": True,
        },
    )
    assert reactivated.status_code == 200
    assert reactivated.get_json()["activo"] is True


def test_seed_r_core_completa_roles_existentes_sin_asignar_personas(app):
    with app.app_context():
        trabajador = Trabajador.query.filter_by(codigo="TRB-01").one()
        roles_antes = {rol.codigo for rol in trabajador.roles}
        assert roles_antes == {"MAQUINISTA"}

        first = ensure_initial_scm_configuration()
        relaciones_primera = first.relaciones_creadas
        second = ensure_initial_scm_configuration()

        db.session.refresh(trabajador)
        roles_despues = {rol.codigo for rol in trabajador.roles}
        maquinista = RolOperativo.query.filter_by(codigo="MAQUINISTA").one()
        capacidades_maquinista = {
            capacidad.codigo for capacidad in maquinista.capacidades
        }

        assert roles_despues == roles_antes
        assert {"MANGA_PESAR", "MANGA_PESAJE_VER", "WIP_VER"} <= (
            capacidades_maquinista
        )
        assert ScmCapacidad.query.filter_by(
            codigo="AUTORIZACION_SCM_ADMINISTRAR"
        ).one()
        assert relaciones_primera > 0
        assert second.capacidades_creadas == 0
        assert second.roles_creados == 0
        assert second.relaciones_creadas == 0
