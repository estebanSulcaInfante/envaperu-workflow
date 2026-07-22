from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.extensions import db
from app.models.materiales import Colorante, MateriaPrima
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_catalogos import ScmMaterial
from app.models.trabajador import RolOperativo, Trabajador
from app.services import scm_material_catalog_service


API_BASE = "/api/scm/v1"
CATEGORIES_URL = f"{API_BASE}/config/categorias-recepcion"
MATERIALS_URL = f"{API_BASE}/materiales"


@dataclass(frozen=True)
class ScmCatalogActors:
    config_id: int
    compras_id: int
    gerencia_id: int
    sin_permiso_id: int


@pytest.fixture
def scm_catalog_actors(app, scm_config):
    del scm_config

    with app.app_context():
        roles = {
            role.codigo: role
            for role in db.session.scalars(db.select(RolOperativo))
        }
        config_actor = Trabajador(
            codigo="TRB-CONFIG-API",
            nombres="Claudia",
            apellidos="Configuracion",
            activo=True,
            roles=[roles["CONFIGURACION_SCM"]],
        )
        compras_actor = Trabajador(
            codigo="TRB-COM-CAT",
            nombres="Maria",
            apellidos="Compras",
            activo=True,
            roles=[roles["COMPRAS"]],
        )
        gerencia_actor = Trabajador(
            codigo="TRB-GER-CAT",
            nombres="Gabriela",
            apellidos="Gerencia",
            activo=True,
            roles=[roles["GERENCIA"]],
        )
        sin_permiso = Trabajador(
            codigo="TRB-AUD-CAT",
            nombres="Ada",
            apellidos="Auditoria",
            activo=True,
            roles=[roles["AUDITORIA_CONSULTA"]],
        )
        db.session.add_all([
            config_actor,
            compras_actor,
            gerencia_actor,
            sin_permiso,
        ])
        db.session.commit()
        return ScmCatalogActors(
            config_id=config_actor.id,
            compras_id=compras_actor.id,
            gerencia_id=gerencia_actor.id,
            sin_permiso_id=sin_permiso.id,
        )


def _headers(actor_id, *, idempotency_key=None):
    headers = {"X-Actor-Id": str(actor_id)}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = str(idempotency_key)
    return headers


def _assert_error(response, status, code):
    assert response.status_code == status, response.get_json()
    payload = response.get_json()
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]
    return payload["error"]


def _create_pending_category(client, actor_id, *, suffix="01"):
    response = client.post(
        CATEGORIES_URL,
        headers=_headers(actor_id),
        json={
            "codigo": f"CAT-PENDIENTE-{suffix}",
            "nombre": f"Categoria pendiente {suffix}",
            "modalidad_default": "POR_CONFIGURAR",
            "lote_externo_obligatorio": False,
            "recepcion_habilitada": False,
            "activo": True,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _category_by_code(client, actor_id, code):
    response = client.get(
        CATEGORIES_URL,
        headers=_headers(actor_id),
    )
    assert response.status_code == 200, response.get_json()
    return next(
        item
        for item in response.get_json()["items"]
        if item["codigo"] == code
    )


def _create_material(
    client,
    actor_id,
    *,
    code,
    name,
    material_class,
    category_id,
):
    response = client.post(
        MATERIALS_URL,
        headers=_headers(actor_id),
        json={
            "codigo": code,
            "nombre": name,
            "clase": material_class,
            "categoria_recepcion_id": category_id,
            "unidad_base": "KG",
            "activo": True,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _create_provider(client, actor_id):
    response = client.post(
        f"{API_BASE}/proveedores",
        headers=_headers(actor_id),
        json={
            "codigo": "PROV-MAT-API",
            "razon_social": "Proveedor para catalogo material",
            "ruc": "20524360366",
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_catalogos_scm_autogeneran_prefijos_descriptivos(
    client,
    scm_catalog_actors,
):
    actors = scm_catalog_actors
    category_response = client.post(
        CATEGORIES_URL,
        headers=_headers(actors.config_id),
        json={
            "nombre": "Categoria autogenerada",
            "modalidad_default": "POR_CONFIGURAR",
            "lote_externo_obligatorio": False,
            "recepcion_habilitada": False,
            "activo": True,
        },
    )
    assert category_response.status_code == 201, category_response.get_json()
    category = category_response.get_json()
    assert category["codigo"] == "CAT-000001"

    material_payload = {
        "nombre": "PP automatico",
        "clase": "MATERIA_PRIMA",
        "categoria_recepcion_id": category["id"],
        "unidad_base": "KG",
        "activo": True,
    }
    raw = client.post(
        MATERIALS_URL,
        headers=_headers(actors.config_id),
        json=material_payload,
    )
    assert raw.status_code == 201, raw.get_json()
    assert raw.get_json()["codigo"] == "MP-000001"

    colorant = client.post(
        MATERIALS_URL,
        headers=_headers(actors.config_id),
        json={
            **material_payload,
            "nombre": "Amarillo automatico",
            "clase": "COLORANTE",
            "tipo_colorante": "COLORANTE",
        },
    )
    assert colorant.status_code == 201, colorant.get_json()
    assert colorant.get_json()["codigo"] == "COL-000001"

    additive = client.post(
        MATERIALS_URL,
        headers=_headers(actors.config_id),
        json={
            **material_payload,
            "nombre": "UV automatico",
            "clase": "COLORANTE",
            "tipo_colorante": "ADITIVO",
        },
    )
    assert additive.status_code == 201, additive.get_json()
    assert additive.get_json()["codigo"] == "ADT-000001"

    provider = client.post(
        f"{API_BASE}/proveedores",
        headers=_headers(actors.compras_id),
        json={"razon_social": "Proveedor automatico", "ruc": "20524360367"},
    )
    assert provider.status_code == 201, provider.get_json()
    assert provider.get_json()["codigo"] == "PRV-000001"


def test_categoria_recepcion_crud_logico_exige_config_y_version(
    app,
    client,
    scm_catalog_actors,
):
    actors = scm_catalog_actors
    payload = {
        "codigo": "CAT-PENDIENTE-01",
        "nombre": "Categoria pendiente 01",
        "modalidad_default": "POR_CONFIGURAR",
        "lote_externo_obligatorio": False,
        "recepcion_habilitada": False,
        "activo": True,
    }

    denied = client.post(
        CATEGORIES_URL,
        headers=_headers(actors.sin_permiso_id),
        json=payload,
    )
    error = _assert_error(denied, 403, "CAPABILITY_REQUIRED")
    assert error["details"] == {
        "capability": "CONFIG_RECEPCION_ADMINISTRAR"
    }

    invalid_create = client.post(
        CATEGORIES_URL,
        headers=_headers(actors.config_id),
        json={
            **payload,
            "codigo": "CAT-PENDIENTE-INVALIDA",
            "recepcion_habilitada": True,
        },
    )
    _assert_error(invalid_create, 422, "CATEGORY_NOT_CONFIGURED")

    created = client.post(
        CATEGORIES_URL,
        headers=_headers(actors.config_id),
        json=payload,
    )
    assert created.status_code == 201, created.get_json()
    category = created.get_json()
    assert category["codigo"] == "CAT-PENDIENTE-01"
    assert category["modalidad_default"] == "POR_CONFIGURAR"
    assert category["recepcion_habilitada"] is False
    assert category["activo"] is True
    assert category["version"] == 1

    denied_patch = client.patch(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.sin_permiso_id),
        json={"version": 1, "nombre": "Cambio no autorizado"},
    )
    _assert_error(denied_patch, 403, "CAPABILITY_REQUIRED")

    listed = client.get(
        CATEGORIES_URL,
        headers=_headers(actors.config_id),
    )
    assert listed.status_code == 200, listed.get_json()
    assert category["id"] in {
        item["id"] for item in listed.get_json()["items"]
    }

    detail = client.get(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
    )
    assert detail.status_code == 200, detail.get_json()
    assert detail.get_json() == category

    cannot_enable = client.patch(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
        json={"version": 1, "recepcion_habilitada": True},
    )
    _assert_error(cannot_enable, 422, "CATEGORY_NOT_CONFIGURED")

    updated = client.patch(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
        json={
            "version": 1,
            "nombre": "Categoria pendiente actualizada",
            "activo": False,
        },
    )
    assert updated.status_code == 200, updated.get_json()
    updated_payload = updated.get_json()
    assert updated_payload["nombre"] == "Categoria pendiente actualizada"
    assert updated_payload["activo"] is False
    assert updated_payload["recepcion_habilitada"] is False
    assert updated_payload["version"] == 2

    stale = client.patch(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
        json={"version": 1, "nombre": "Cambio obsoleto"},
    )
    stale_error = _assert_error(stale, 409, "STALE_VERSION")
    assert stale_error["details"] == {"expected": 2, "received": 1}

    immutable_code = client.patch(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
        json={"version": 2, "codigo": "CAT-RENOMBRADA"},
    )
    _assert_error(
        immutable_code,
        422,
        "IMMUTABLE_CATEGORY_CODE",
    )

    typo = client.patch(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
        json={"version": 2, "nobre": "Campo mal escrito"},
    )
    _assert_error(typo, 400, "UNKNOWN_FIELDS")

    no_change = client.patch(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
        json={"version": 2, "nombre": "Categoria pendiente actualizada"},
    )
    _assert_error(no_change, 400, "NO_CHANGES")

    non_integer_version = client.patch(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
        json={"version": 2.5, "nombre": "Version invalida"},
    )
    _assert_error(non_integer_version, 400, "VERSION_REQUIRED")

    final_detail = client.get(
        f"{CATEGORIES_URL}/{category['id']}",
        headers=_headers(actors.config_id),
    )
    assert final_detail.status_code == 200
    assert final_detail.get_json()["codigo"] == "CAT-PENDIENTE-01"
    assert final_detail.get_json()["version"] == 2

    inactive = client.get(
        f"{CATEGORIES_URL}?activo=false",
        headers=_headers(actors.config_id),
    )
    assert inactive.status_code == 200, inactive.get_json()
    assert [item["id"] for item in inactive.get_json()["items"]] == [
        category["id"]
    ]

    inactive_assignment = client.post(
        MATERIALS_URL,
        headers=_headers(actors.config_id),
        json={
            "codigo": "MP-CATEGORIA-INACTIVA",
            "nombre": "No debe usar categoria inactiva",
            "clase": "MATERIA_PRIMA",
            "categoria_recepcion_id": category["id"],
            "unidad_base": "KG",
        },
    )
    _assert_error(inactive_assignment, 422, "CATEGORY_INACTIVE")

    with app.app_context():
        events = ScmEvento.query.filter_by(
            aggregate_type="SCM_CATEGORIA_RECEPCION",
            aggregate_id=category["id"],
        ).order_by(ScmEvento.id).all()
        assert [event.tipo for event in events] == [
            "CATEGORIA_RECEPCION_CREADA",
            "CATEGORIA_RECEPCION_ACTUALIZADA",
        ]
        assert events[-1].before_json["version"] == 1
        assert events[-1].after_json["version"] == 2
        assert events[-1].actor_snapshot == {
            "id": actors.config_id,
            "codigo": "TRB-CONFIG-API",
            "nombre": "Claudia Configuracion",
        }


def test_material_crud_dual_write_es_atomico_y_sincroniza_legacy(
    app,
    client,
    scm_catalog_actors,
):
    actors = scm_catalog_actors
    pending_category = _create_pending_category(client, actors.config_id)
    virgin_category = _category_by_code(
        client,
        actors.config_id,
        "RESINA_VIRGEN",
    )

    denied = client.post(
        MATERIALS_URL,
        headers=_headers(actors.sin_permiso_id),
        json={
            "codigo": "MP-DENEGADA",
            "nombre": "Materia prima denegada",
            "clase": "MATERIA_PRIMA",
            "categoria_recepcion_id": virgin_category["id"],
        },
    )
    _assert_error(denied, 403, "CAPABILITY_REQUIRED")

    raw_material = _create_material(
        client,
        actors.config_id,
        code="MP-API-0001",
        name="PP virgen API",
        material_class="MATERIA_PRIMA",
        category_id=virgin_category["id"],
    )
    colorant = _create_material(
        client,
        actors.config_id,
        code="COL-API-0001",
        name="Masterbatch azul API",
        material_class="COLORANTE",
        category_id=pending_category["id"],
    )
    assert raw_material["version"] == 1
    assert raw_material["unidad_base"] == "KG"
    assert colorant["version"] == 1
    assert colorant["categoria_recepcion_id"] == pending_category["id"]

    denied_patch = client.patch(
        f"{MATERIALS_URL}/{raw_material['id']}",
        headers=_headers(actors.sin_permiso_id),
        json={"version": 1, "nombre": "Cambio no autorizado"},
    )
    _assert_error(denied_patch, 403, "CAPABILITY_REQUIRED")

    listed = client.get(
        MATERIALS_URL,
        headers=_headers(actors.config_id),
    )
    assert listed.status_code == 200, listed.get_json()
    assert {item["codigo"] for item in listed.get_json()["items"]} == {
        "COL-API-0001",
        "MP-API-0001",
    }

    for material in (raw_material, colorant):
        detail = client.get(
            f"{MATERIALS_URL}/{material['id']}",
            headers=_headers(actors.config_id),
        )
        assert detail.status_code == 200, detail.get_json()
        assert detail.get_json() == material

    with app.app_context():
        raw_identity = db.session.get(ScmMaterial, raw_material["id"])
        colorant_identity = db.session.get(ScmMaterial, colorant["id"])
        assert raw_identity.materia_prima is not None
        assert raw_identity.colorante is None
        assert raw_identity.materia_prima.nombre == "PP virgen API"
        assert raw_identity.materia_prima.tipo == "VIRGEN"
        assert colorant_identity.colorante is not None
        assert colorant_identity.materia_prima is None
        assert colorant_identity.colorante.nombre == "Masterbatch azul API"
        counts_before_conflict = (
            ScmMaterial.query.count(),
            MateriaPrima.query.count(),
            Colorante.query.count(),
        )

    duplicate = client.post(
        MATERIALS_URL,
        headers=_headers(actors.config_id),
        json={
            "codigo": "MP-API-0001",
            "nombre": "No debe dejar fila legacy",
            "clase": "COLORANTE",
            "categoria_recepcion_id": pending_category["id"],
        },
    )
    _assert_error(duplicate, 409, "MATERIAL_CODE_CONFLICT")
    with app.app_context():
        assert (
            ScmMaterial.query.count(),
            MateriaPrima.query.count(),
            Colorante.query.count(),
        ) == counts_before_conflict

    updated = client.patch(
        f"{MATERIALS_URL}/{raw_material['id']}",
        headers=_headers(actors.config_id),
        json={
            "version": 1,
            "nombre": "PP virgen API actualizado",
            "categoria_recepcion_id": pending_category["id"],
            "activo": False,
        },
    )
    assert updated.status_code == 200, updated.get_json()
    updated_payload = updated.get_json()
    assert updated_payload["nombre"] == "PP virgen API actualizado"
    assert updated_payload["categoria_recepcion_id"] == pending_category["id"]
    assert updated_payload["activo"] is False
    assert updated_payload["version"] == 2

    with app.app_context():
        raw_identity = db.session.get(ScmMaterial, raw_material["id"])
        assert raw_identity.materia_prima.tipo is None

    category_reclassified = client.patch(
        f"{CATEGORIES_URL}/{pending_category['id']}",
        headers=_headers(actors.config_id),
        json={
            "version": 1,
            "modalidad_default": "SEGUNDA_PESAJE_BOLSA",
        },
    )
    assert category_reclassified.status_code == 200, (
        category_reclassified.get_json()
    )
    assert category_reclassified.get_json()["version"] == 2

    stale = client.patch(
        f"{MATERIALS_URL}/{raw_material['id']}",
        headers=_headers(actors.config_id),
        json={"version": 1, "nombre": "Nombre obsoleto"},
    )
    _assert_error(stale, 409, "STALE_VERSION")

    immutable_code = client.patch(
        f"{MATERIALS_URL}/{raw_material['id']}",
        headers=_headers(actors.config_id),
        json={"version": 2, "codigo": "MP-API-RENOMBRADA"},
    )
    _assert_error(immutable_code, 422, "IMMUTABLE_MATERIAL_CODE")

    immutable_class = client.patch(
        f"{MATERIALS_URL}/{raw_material['id']}",
        headers=_headers(actors.config_id),
        json={"version": 2, "clase": "COLORANTE"},
    )
    _assert_error(immutable_class, 422, "IMMUTABLE_MATERIAL_CLASS")

    colorant_renamed = client.patch(
        f"{MATERIALS_URL}/{colorant['id']}",
        headers=_headers(actors.config_id),
        json={"version": 1, "nombre": "Masterbatch azul actualizado"},
    )
    assert colorant_renamed.status_code == 200, colorant_renamed.get_json()
    assert colorant_renamed.get_json()["version"] == 2

    material_no_change = client.patch(
        f"{MATERIALS_URL}/{colorant['id']}",
        headers=_headers(actors.config_id),
        json={"version": 2, "nombre": "Masterbatch azul actualizado"},
    )
    _assert_error(material_no_change, 400, "NO_CHANGES")

    inactive = client.get(
        f"{MATERIALS_URL}?activo=false",
        headers=_headers(actors.config_id),
    )
    assert inactive.status_code == 200, inactive.get_json()
    assert [item["id"] for item in inactive.get_json()["items"]] == [
        raw_material["id"]
    ]

    with app.app_context():
        raw_identity = db.session.get(ScmMaterial, raw_material["id"])
        colorant_identity = db.session.get(ScmMaterial, colorant["id"])
        assert raw_identity.codigo == "MP-API-0001"
        assert raw_identity.clase == "MATERIA_PRIMA"
        assert raw_identity.nombre == "PP virgen API actualizado"
        assert raw_identity.materia_prima.nombre == (
            "PP virgen API actualizado"
        )
        assert raw_identity.materia_prima.tipo == "SEGUNDA"
        assert raw_identity.categoria_recepcion_id == pending_category["id"]
        assert raw_identity.activo is False
        assert colorant_identity.nombre == "Masterbatch azul actualizado"
        assert colorant_identity.colorante.nombre == (
            "Masterbatch azul actualizado"
        )

        events = ScmEvento.query.filter_by(
            aggregate_type="SCM_MATERIAL",
        ).order_by(ScmEvento.id).all()
        assert [event.tipo for event in events] == [
            "MATERIAL_CREADO",
            "MATERIAL_CREADO",
            "MATERIAL_ACTUALIZADO",
            "MATERIAL_ACTUALIZADO",
        ]
        assert events[-1].actor_snapshot["id"] == actors.config_id


def test_colorante_scm_distingue_colorante_y_aditivo(
    client,
    scm_catalog_actors,
):
    actors = scm_catalog_actors
    category = _create_pending_category(client, actors.config_id, suffix="ADITIVO")
    created = client.post(
        MATERIALS_URL,
        headers=_headers(actors.config_id),
        json={
            "codigo": "ADT-API-0001",
            "nombre": "Aditivo UV",
            "clase": "COLORANTE",
            "tipo_colorante": "ADITIVO",
            "categoria_recepcion_id": category["id"],
            "unidad_base": "KG",
            "activo": True,
        },
    )
    assert created.status_code == 201, created.get_json()
    material = created.get_json()
    assert material["tipo_colorante"] == "ADITIVO"

    updated = client.patch(
        f"{MATERIALS_URL}/{material['id']}",
        headers=_headers(actors.config_id),
        json={"version": 1, "tipo_colorante": "COLORANTE"},
    )
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["tipo_colorante"] == "COLORANTE"


def test_material_dual_write_revierte_si_falla_despues_del_flush(
    app,
    client,
    scm_catalog_actors,
    monkeypatch,
):
    actors = scm_catalog_actors
    virgin_category = _category_by_code(
        client,
        actors.config_id,
        "RESINA_VIRGEN",
    )

    with app.app_context():
        counts_before = (
            ScmMaterial.query.count(),
            MateriaPrima.query.count(),
            Colorante.query.count(),
            ScmEvento.query.count(),
        )

        def fail_after_flush(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("fallo inyectado despues del dual-write")

        monkeypatch.setattr(
            scm_material_catalog_service,
            "_material_event",
            fail_after_flush,
        )
        with pytest.raises(RuntimeError, match="fallo inyectado"):
            scm_material_catalog_service.create_material(
                db.session,
                actor_id=actors.config_id,
                data={
                    "codigo": "MP-ROLLBACK-POST-FLUSH",
                    "nombre": "No debe persistir",
                    "clase": "MATERIA_PRIMA",
                    "categoria_recepcion_id": virgin_category["id"],
                    "unidad_base": "KG",
                    "activo": True,
                },
            )

        assert (
            ScmMaterial.query.count(),
            MateriaPrima.query.count(),
            Colorante.query.count(),
            ScmEvento.query.count(),
        ) == counts_before


def test_aprobacion_revalida_material_y_proveedor_despues_del_envio(
    app,
    client,
    scm_catalog_actors,
):
    actors = scm_catalog_actors
    virgin_category = _category_by_code(
        client,
        actors.config_id,
        "RESINA_VIRGEN",
    )
    material = _create_material(
        client,
        actors.config_id,
        code="MP-REVALIDAR-APROBACION",
        name="Material para revalidar aprobacion",
        material_class="MATERIA_PRIMA",
        category_id=virgin_category["id"],
    )
    provider = _create_provider(client, actors.compras_id)
    order = client.post(
        f"{API_BASE}/ordenes-compra-material",
        headers=_headers(actors.compras_id),
        json={
            "proveedor_id": provider["id"],
            "lineas": [{
                "numero_linea": 1,
                "material_id": material["id"],
                "cantidad_autorizada_kg": "100.000",
            }],
        },
    ).get_json()
    sent = client.post(
        (
            f"{API_BASE}/ordenes-compra-material/{order['id']}"
            "/enviar-aprobacion"
        ),
        headers=_headers(actors.compras_id, idempotency_key=uuid4()),
        json={"version": 1, "revision_numero": 1},
    )
    assert sent.status_code == 200, sent.get_json()

    deactivated_material = client.patch(
        f"{MATERIALS_URL}/{material['id']}",
        headers=_headers(actors.config_id),
        json={"version": 1, "activo": False},
    )
    assert deactivated_material.status_code == 200
    invalid_material_approval = client.post(
        f"{API_BASE}/ordenes-compra-material/{order['id']}/aprobar",
        headers=_headers(actors.gerencia_id, idempotency_key=uuid4()),
        json={"version": 2, "revision_numero": 1},
    )
    _assert_error(
        invalid_material_approval,
        422,
        "MATERIAL_NOT_RECEIVABLE",
    )

    reactivated_material = client.patch(
        f"{MATERIALS_URL}/{material['id']}",
        headers=_headers(actors.config_id),
        json={"version": 2, "activo": True},
    )
    assert reactivated_material.status_code == 200
    deactivated_provider = client.patch(
        f"{API_BASE}/proveedores/{provider['id']}",
        headers=_headers(actors.compras_id),
        json={"version": 1, "activo": False},
    )
    assert deactivated_provider.status_code == 200
    invalid_provider_approval = client.post(
        f"{API_BASE}/ordenes-compra-material/{order['id']}/aprobar",
        headers=_headers(actors.gerencia_id, idempotency_key=uuid4()),
        json={"version": 2, "revision_numero": 1},
    )
    _assert_error(invalid_provider_approval, 422, "PROVIDER_INACTIVE")

    detail = client.get(
        f"{API_BASE}/ordenes-compra-material/{order['id']}",
        headers=_headers(actors.compras_id),
    )
    assert detail.status_code == 200, detail.get_json()
    assert detail.get_json()["version"] == 2
    assert detail.get_json()["revision_actual"]["estado"] == (
        "PENDIENTE_APROBACION"
    )
    with app.app_context():
        assert ScmOperacion.query.count() == 1


def test_oc_acepta_material_no_recibible_en_borrador_pero_no_lo_envia(
    app,
    client,
    scm_catalog_actors,
):
    actors = scm_catalog_actors
    pending_category = _create_pending_category(
        client,
        actors.config_id,
        suffix="OC",
    )
    pending_material = _create_material(
        client,
        actors.config_id,
        code="COL-OC-PENDIENTE",
        name="Colorante pendiente para OC",
        material_class="COLORANTE",
        category_id=pending_category["id"],
    )
    provider = _create_provider(client, actors.compras_id)

    created = client.post(
        f"{API_BASE}/ordenes-compra-material",
        headers=_headers(actors.compras_id),
        json={
            "proveedor_id": provider["id"],
            "lineas": [{
                "numero_linea": 1,
                "material_id": pending_material["id"],
                "cantidad_autorizada_kg": "1250.000",
            }],
        },
    )
    assert created.status_code == 201, created.get_json()
    order = created.get_json()
    assert order["version"] == 1
    assert order["revision_actual"]["estado"] == "BORRADOR"
    assert order["revision_actual"]["lineas"][0]["material_id"] == (
        pending_material["id"]
    )

    send = client.post(
        (
            f"{API_BASE}/ordenes-compra-material/{order['id']}"
            "/enviar-aprobacion"
        ),
        headers=_headers(actors.compras_id, idempotency_key=uuid4()),
        json={"version": 1, "revision_numero": 1},
    )
    error = _assert_error(send, 422, "MATERIAL_NOT_RECEIVABLE")
    assert error["details"] == {"material_id": pending_material["id"]}

    detail = client.get(
        f"{API_BASE}/ordenes-compra-material/{order['id']}",
        headers=_headers(actors.compras_id),
    )
    assert detail.status_code == 200, detail.get_json()
    assert detail.get_json()["version"] == 1
    assert detail.get_json()["revision_actual"]["estado"] == "BORRADOR"

    with app.app_context():
        assert ScmOperacion.query.count() == 0
