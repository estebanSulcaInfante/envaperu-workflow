import copy
from uuid import UUID, uuid4

from app.extensions import db
from app.models.scm_auditoria import ScmEvento
from app.models.scm_product_onboarding import ScmAltaProductoSesion
from app.models.producto import (
    ColorProduccion,
    FamiliaColor,
    PiezaColor,
    ProductoTerminado,
)
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_catalogos import (
    ScmCapacidad,
    ScmCategoriaRecepcion,
    ScmMaterial,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.models.scm_commercial import ScmPresentacionComercial


STEP_CODES = [
    "IDENTIDAD",
    "COMPONENTES",
    "COLORES",
    "ESTRUCTURA",
    "RUTA_EMPAQUE",
    "REVISION",
]


def _catalog_admin(app):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        capability = ScmCapacidad.query.filter_by(
            codigo="ARTICULO_ADMINISTRAR"
        ).one_or_none()
        if capability is None:
            capability = ScmCapacidad(
                codigo="ARTICULO_ADMINISTRAR",
                nombre="Administrar articulos SCM",
            )
        actor.roles[0].capacidades.append(capability)
        db.session.add(capability)
        db.session.commit()
        return actor.id


def _headers(actor_id, operation_id=None):
    headers = {"X-Actor-Id": str(actor_id)}
    if operation_id is not None:
        headers["Idempotency-Key"] = str(operation_id)
    return headers


def _create_session(client, actor_id, **overrides):
    body = {
        "titulo": "Alta guiada Colador #3",
        "data": {
            "modo": "NUEVO",
            "producto": {"producto": "COLADOR #3"},
            "procedencia": {
                "tipo": "EXCEL",
                "referencia": "SKU PIEZAS 2026",
                "hoja": "SKU PIEZAS HOGAR",
            },
        },
    }
    body.update(overrides)
    response = client.post(
        "/api/scm/v1/altas-producto",
        headers=_headers(actor_id, uuid4()),
        json=body,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _put_step(client, actor_id, session, step, data, state="COMPLETADO"):
    response = client.put(
        f"/api/scm/v1/altas-producto/{session['id']}/pasos/{step}",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": session["version"],
            "data": data,
            "estado_paso": state,
        },
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _materialize_identity(client, actor_id, session, *, name="PT ALTA GUIADA"):
    response = client.post(
        f"/api/scm/v1/altas-producto/{session['id']}"
        "/pasos/IDENTIDAD/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": session["version"],
            "application_key": "identity-prerequisite-v1",
            "data": {
                "modo": "NUEVO",
                "producto": {
                    "producto": name,
                    "linea_id": 1,
                    "familia_id": 1,
                    "um": "UN",
                },
            },
        },
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_session_create_resume_list_and_idempotency(app, client):
    actor_id = _catalog_admin(app)
    operation_id = uuid4()
    payload = {
        "titulo": "Alta guiada Portavajillas",
        "data": {
            "modo": "NUEVO",
            "producto": {"producto": "PORTAVAJILLAS"},
            "procedencia": {
                "tipo": "CONSULTA_PLANTA",
                "referencia": "Jefe de produccion",
            },
        },
    }

    created_response = client.post(
        "/api/scm/v1/altas-producto",
        headers=_headers(actor_id, operation_id),
        json=payload,
    )
    assert created_response.status_code == 201, created_response.get_json()
    created = created_response.get_json()
    assert created["estado"] == "BORRADOR"
    assert created["version"] == 1
    assert created["paso_actual"] == "IDENTIDAD"
    assert [step["codigo"] for step in created["pasos"]] == STEP_CODES
    assert created["pasos"][0]["estado"] == "EN_PROGRESO"
    assert created["pasos"][0]["data"] == payload["data"]
    assert created["fuentes"]["IDENTIDAD"]["procedencia"]["tipo"] == (
        "CONSULTA_PLANTA"
    )

    replay = client.post(
        "/api/scm/v1/altas-producto",
        headers=_headers(actor_id, operation_id),
        json=payload,
    )
    assert replay.status_code == 201
    assert replay.get_json() == created

    conflict = client.post(
        "/api/scm/v1/altas-producto",
        headers=_headers(actor_id, operation_id),
        json={"titulo": "La misma clave con otro comando"},
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    fetched = client.get(
        f"/api/scm/v1/altas-producto/{created['id']}",
        headers=_headers(actor_id),
    )
    assert fetched.status_code == 200
    assert fetched.get_json() == created

    listed = client.get(
        "/api/scm/v1/altas-producto?estado=BORRADOR",
        headers=_headers(actor_id),
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.get_json()] == [created["id"]]

    with app.app_context():
        events = ScmEvento.query.filter_by(
            aggregate_type="ALTA_PRODUCTO_SESION",
            aggregate_id=created["id"],
        ).all()
        assert [event.tipo for event in events] == [
            "ALTA_PRODUCTO_INICIADA"
        ]
        assert events[0].actor_id == actor_id
        assert events[0].operation_id == operation_id


def test_revision_draft_allows_blank_optional_notes_for_save_and_exit(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _create_session(client, actor_id, data={})

    response = client.put(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/REVISION",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "data": {
                "confirmaciones": {
                    "datos_fuente_revisados": False,
                    "entiende_que_no_crea_op": False,
                    "pendientes_aceptados": False,
                },
                "pasos_revisados": STEP_CODES[:-1],
                "revisiones_revisadas": [],
                "notas": "",
            },
        },
    )

    assert response.status_code == 200, response.get_json()
    saved = response.get_json()
    revision = next(
        step for step in saved["pasos"] if step["codigo"] == "REVISION"
    )
    assert revision["estado"] == "EN_PROGRESO"
    assert revision["data"]["notas"] == ""


def test_step_save_is_opaque_versioned_and_invalidates_descendants(app, client):
    actor_id = _catalog_admin(app)
    with app.app_context():
        db.session.add(ProductoTerminado(
            cod_sku_pt="PT-000321",
            producto="COLADOR #3",
            linea_id=1,
            familia_id=1,
        ))
        db.session.commit()
    session = _create_session(client, actor_id, data={})

    step_data = {
        "IDENTIDAD": {
            "modo": "NUEVO",
            "producto_ref": "PT-000321",
            "producto": {
                "cod_sku_pt": "PT-000321",
                "producto": "COLADOR #3",
                "linea_id": 1,
                "familia_id": 1,
            },
            "procedencia": {
                "tipo": "EXCEL",
                "referencia": "SKU PIEZAS 2026",
                "hoja": "SKU PIEZAS HOGAR",
                "fila": 580,
            },
        },
        "COMPONENTES": {"piezas": [{"pieza_ref": "PZ-000001"}]},
        "COLORES": {"variantes": [{"color_ref": "COL-000001"}]},
        "ESTRUCTURA": {"estructura_ref": "EST-000001"},
        "RUTA_EMPAQUE": {
            "ruta_ref": "RUT-000001",
            "perfil_empaque_ref": "PER-000001",
        },
        "REVISION": {
            "confirmaciones": {
                "datos_fuente_revisados": True,
                "entiende_que_no_crea_op": True,
                "pendientes_aceptados": True,
            },
            "pasos_revisados": STEP_CODES[:-1],
            "revisiones_revisadas": [],
        },
    }
    for code in STEP_CODES:
        session = _put_step(
            client, actor_id, session, code, step_data[code]
        )

    assert session["version"] == 7
    assert session["referencias"]["IDENTIDAD"] == {
        "producto_ref": "PT-000321"
    }
    assert session["referencias"]["RUTA_EMPAQUE"] == {
        "perfil_empaque_ref": "PER-000001",
        "ruta_ref": "RUT-000001",
    }

    operation_id = uuid4()
    changed_data = {"variantes": [{"color_ref": "COL-000002"}]}
    changed_response = client.put(
        f"/api/scm/v1/altas-producto/{session['id']}/pasos/COLORES",
        headers=_headers(actor_id, operation_id),
        json={
            "expected_version": 7,
            "data": changed_data,
            "estado_paso": "COMPLETADO",
        },
    )
    assert changed_response.status_code == 200, changed_response.get_json()
    changed = changed_response.get_json()
    assert changed["version"] == 8
    assert changed["invalidated_steps"] == [
        "ESTRUCTURA",
        "RUTA_EMPAQUE",
        "REVISION",
    ]
    assert [step["estado"] for step in changed["pasos"][3:]] == [
        "INVALIDADO",
        "INVALIDADO",
        "INVALIDADO",
    ]
    # La invalidacion no borra evidencia ni datos que el usuario debe revisar.
    assert changed["pasos"][3]["data"] == step_data["ESTRUCTURA"]

    replay = client.put(
        f"/api/scm/v1/altas-producto/{session['id']}/pasos/COLORES",
        headers=_headers(actor_id, operation_id),
        json={
            "expected_version": 7,
            "data": changed_data,
            "estado_paso": "COMPLETADO",
        },
    )
    assert replay.status_code == 200
    assert replay.get_json() == changed

    stale = client.put(
        f"/api/scm/v1/altas-producto/{session['id']}/pasos/COLORES",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": 7,
            "data": {"variantes": []},
            "estado_paso": "EN_PROGRESO",
        },
    )
    assert stale.status_code == 409
    error = stale.get_json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"]["received"] == 7
    assert error["details"]["expected"] == 8
    assert error["details"]["current_session"]["version"] == 8


def test_validation_never_treats_completed_checklist_as_canonical_readiness(
    app, client
):
    actor_id = _catalog_admin(app)
    session = _create_session(client, actor_id, data={})

    blocked_response = client.post(
        f"/api/scm/v1/altas-producto/{session['id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": session["version"]},
    )
    assert blocked_response.status_code == 200
    blocked = blocked_response.get_json()
    assert blocked["estado"] == "CON_BLOQUEOS"
    assert blocked["readiness"]["lista_para_finalizar"] is False
    assert {item["paso"] for item in blocked["readiness"]["bloqueos"]} == (
        set(STEP_CODES)
    )

    for code in STEP_CODES:
        step_payload = {"capturado": True}
        if code == "REVISION":
            step_payload = {
                "confirmaciones": {
                    "datos_fuente_revisados": True,
                    "entiende_que_no_crea_op": True,
                    "pendientes_aceptados": True,
                },
                "pasos_revisados": STEP_CODES[:-1],
                "revisiones_revisadas": [],
            }
        session = _put_step(
            client,
            actor_id,
            blocked if code == STEP_CODES[0] else session,
            code,
            step_payload,
        )
        blocked = session

    validated_response = client.post(
        f"/api/scm/v1/altas-producto/{session['id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": session["version"]},
    )
    assert validated_response.status_code == 200
    validated = validated_response.get_json()
    assert validated["estado"] == "CON_BLOQUEOS"
    assert validated["readiness"]["status"] == "BLOCKED"
    assert validated["readiness"]["lista_para_finalizar"] is False
    assert {
        item["code"] for item in validated["readiness"]["items"]
    } >= {
        "PRODUCT_NOT_RESOLVED",
        "STRUCTURE_NOT_RESOLVED",
        "ROUTE_NOT_RESOLVED",
    }

    finalized_response = client.post(
        f"/api/scm/v1/altas-producto/{session['id']}/finalizar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": validated["version"]},
    )
    assert finalized_response.status_code == 422
    assert finalized_response.get_json()["error"]["code"] == (
        "SESSION_NOT_READY"
    )


def test_incomplete_finalize_persists_blockers_without_completing_steps(
    app, client
):
    actor_id = _catalog_admin(app)
    session = _create_session(client, actor_id, data={})
    response = client.post(
        f"/api/scm/v1/altas-producto/{session['id']}/finalizar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": session["version"]},
    )
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "SESSION_NOT_READY"
    current = error["details"]["current_session"]
    assert current["estado"] == "CON_BLOQUEOS"
    assert all(step["estado"] == "PENDIENTE" for step in current["pasos"])

    resumed = client.get(
        f"/api/scm/v1/altas-producto/{session['id']}",
        headers=_headers(actor_id),
    ).get_json()
    assert resumed["estado"] == "CON_BLOQUEOS"
    assert resumed["version"] == session["version"] + 1


def test_session_requires_catalog_administration_capability(app, client):
    with app.app_context():
        actor_id = Trabajador.query.filter_by(codigo="TRB-01").one().id

    response = client.post(
        "/api/scm/v1/altas-producto",
        headers=_headers(actor_id, uuid4()),
        json={"titulo": "No autorizado"},
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"


def test_identity_autosave_names_placeholder_session_without_overwriting_custom_title(
    app, client
):
    actor_id = _catalog_admin(app)
    session = _create_session(
        client,
        actor_id,
        titulo="Nuevo Producto Terminado",
        data={},
    )
    named = _put_step(
        client,
        actor_id,
        session,
        "IDENTIDAD",
        {"producto": {"producto": "PORTAVAJILLAS PREMIUM"}},
        state="EN_PROGRESO",
    )
    assert named["titulo"] == "PORTAVAJILLAS PREMIUM"

    renamed_draft = _put_step(
        client,
        actor_id,
        named,
        "IDENTIDAD",
        {"producto": {"producto": "OTRO NOMBRE DE BORRADOR"}},
        state="EN_PROGRESO",
    )
    assert renamed_draft["titulo"] == "PORTAVAJILLAS PREMIUM"


def test_unfiltered_list_keeps_all_resumable_workflow_states(app, client):
    actor_id = _catalog_admin(app)
    first = _create_session(client, actor_id, titulo="Borrador", data={})
    second = _create_session(client, actor_id, titulo="Bloqueada", data={})
    third = _create_session(client, actor_id, titulo="Lista", data={})
    with app.app_context():
        db.session.get(
            ScmAltaProductoSesion, UUID(second["id"])
        ).estado = "CON_BLOQUEOS"
        db.session.get(
            ScmAltaProductoSesion, UUID(third["id"])
        ).estado = "LISTA_PARA_PUBLICAR"
        db.session.commit()

    listed = client.get(
        "/api/scm/v1/altas-producto",
        headers=_headers(actor_id),
    )
    assert listed.status_code == 200
    states = {
        item["id"]: item["estado"] for item in listed.get_json()
    }
    assert states[first["id"]] == "BORRADOR"
    assert states[second["id"]] == "CON_BLOQUEOS"
    assert states[third["id"]] == "LISTA_PARA_PUBLICAR"


def test_identity_links_object_product_reference_and_rejects_unknown_pt(
    app, client
):
    actor_id = _catalog_admin(app)
    with app.app_context():
        db.session.add(ProductoTerminado(
            cod_sku_pt="PT-OBJECT-001",
            producto="Producto enlazado",
            linea_id=1,
            familia_id=1,
        ))
        db.session.commit()

    session = _create_session(client, actor_id, data={})
    linked = _put_step(
        client,
        actor_id,
        session,
        "IDENTIDAD",
        {
            "modo": "NUEVO",
            "producto_ref": {
                "cod_sku_pt": "PT-OBJECT-001",
                "producto": "Producto enlazado",
            },
            "referencias": {
                "producto_terminado_id": "PT-OBJECT-001",
            },
            "producto": {
                "cod_sku_pt": "PT-OBJECT-001",
                "producto": "Producto enlazado",
            },
        },
    )
    assert linked["producto_terminado_id"] == "PT-OBJECT-001"
    assert linked["referencias"]["IDENTIDAD"][
        "producto_terminado_id"
    ] == "PT-OBJECT-001"
    with app.app_context():
        persisted = db.session.get(
            ScmAltaProductoSesion, UUID(session["id"])
        )
        assert persisted.producto_terminado_id == "PT-OBJECT-001"

    unknown = client.put(
        f"/api/scm/v1/altas-producto/{session['id']}/pasos/IDENTIDAD",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": linked["version"],
            "estado_paso": "COMPLETADO",
            "data": {
                "producto_ref": {"cod_sku_pt": "PT-NO-EXISTE"},
                "referencias": {
                    "producto_terminado_id": "PT-NO-EXISTE",
                },
            },
        },
    )
    assert unknown.status_code == 404
    assert unknown.get_json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    resumed = client.get(
        f"/api/scm/v1/altas-producto/{session['id']}",
        headers=_headers(actor_id),
    ).get_json()
    assert resumed["version"] == linked["version"]
    assert resumed["producto_terminado_id"] == "PT-OBJECT-001"


def test_apply_identity_materializes_product_atomically_and_replays(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _create_session(client, actor_id, data={})
    operation_id = uuid4()
    payload = {
        "expected_version": onboarding["version"],
        "application_key": "identity-portavajillas-v1",
        "data": {
            "modo": "NUEVO",
            "producto": {
                "producto": "PORTAVAJILLAS PREMIUM",
                "linea_id": 1,
                "familia_id": 1,
                "peso_g": 850,
                "marca": "ENVAPERU",
            },
            "procedencia": {"tipo": "CONSULTA_PLANTA"},
        },
    }
    response = client.post(
        (
            f"/api/scm/v1/altas-producto/{onboarding['id']}"
            "/pasos/IDENTIDAD/aplicar"
        ),
        headers=_headers(actor_id, operation_id),
        json=payload,
    )
    assert response.status_code == 200, response.get_json()
    applied = response.get_json()
    product_id = applied["producto_terminado_id"]
    assert applied["estado"] == "BORRADOR"
    assert applied["version"] == onboarding["version"] + 1
    assert applied["pasos"][0]["estado"] == "COMPLETADO"
    assert applied["application_results"] == {
        "application_key": "identity-portavajillas-v1",
        "paso": "IDENTIDAD",
        "status": "APPLIED",
        "created": [{"type": "PRODUCTO_TERMINADO", "id": product_id}],
        "reused": [],
        "pending": [],
        "resolved_references": {
            "producto_terminado_id": product_id,
        },
    }
    assert applied["referencias"]["IDENTIDAD"] == {
        "producto_terminado_id": product_id,
        "producto_ref": product_id,
    }
    assert applied["pasos"][0]["application_status"] == {
        "status": "APPLIED",
        "application_key": "identity-portavajillas-v1",
        "paso": "IDENTIDAD",
        "created": [{"type": "PRODUCTO_TERMINADO", "id": product_id}],
        "reused": [],
        "pending": [],
        "resolved_references": {
            "producto_terminado_id": product_id,
        },
    }
    with app.app_context():
        assert db.session.get(ProductoTerminado, product_id) is not None
        assert ScmPresentacionComercial.query.filter_by(
            producto_terminado_id=product_id,
            predeterminada=True,
        ).count() == 1

    replay = client.post(
        (
            f"/api/scm/v1/altas-producto/{onboarding['id']}"
            "/pasos/IDENTIDAD/aplicar"
        ),
        headers=_headers(actor_id, operation_id),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == applied

    with app.app_context():
        assert ProductoTerminado.query.filter_by(
            producto="PORTAVAJILLAS PREMIUM"
        ).count() == 1
        assert ScmPresentacionComercial.query.filter_by(
            producto_terminado_id=product_id,
        ).count() == 1

    changed = copy.deepcopy(payload)
    changed["expected_version"] = applied["version"]
    changed["application_key"] = "identity-portavajillas-v2"
    changed["data"]["producto"]["producto"] = "OTRO PRODUCTO SILENCIOSO"
    rejected = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/IDENTIDAD/aplicar",
        headers=_headers(actor_id, uuid4()),
        json=changed,
    )
    assert rejected.status_code == 422
    assert rejected.get_json()["error"]["code"] == (
        "ONBOARDING_STEP_ALREADY_APPLIED"
    )
    with app.app_context():
        assert ProductoTerminado.query.count() == 1


def test_onboarding_mutations_require_idempotency_key(app, client):
    actor_id = _catalog_admin(app)
    response = client.post(
        "/api/scm/v1/altas-producto",
        headers=_headers(actor_id),
        json={"titulo": "Sin clave"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == (
        "IDEMPOTENCY_KEY_REQUIRED"
    )


def test_apply_components_creates_unclassified_pieces_and_mold_links(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _create_session(client, actor_id, data={})
    onboarding = _materialize_identity(client, actor_id, onboarding)
    payload = {
        "expected_version": onboarding["version"],
        "application_key": "components-colador-v1",
        "data": {
            "molde": {
                "modo": "NUEVO",
                "nombre": "MOLDE COLADOR #3",
                "peso_tiro_gr": 140,
                "tiempo_ciclo_std": 35,
            },
            "piezas": [{
                "client_id": "cesto",
                "modo": "NUEVA",
                "nombre": "CESTO COLADOR #3",
                "cavidades": 1,
                "peso_unitario_gr": 120,
            }],
            "imagenes": {"estado": "PENDIENTE_NO_SOPORTADO"},
        },
    }
    operation_id = uuid4()
    response = client.post(
        (
            f"/api/scm/v1/altas-producto/{onboarding['id']}"
            "/pasos/COMPONENTES/aplicar"
        ),
        headers=_headers(actor_id, operation_id),
        json=payload,
    )
    assert response.status_code == 200, response.get_json()
    applied = response.get_json()
    refs = applied["application_results"]["resolved_references"]
    assert refs["molde_ref"].startswith("ML-")
    assert refs["piezas"][0]["client_id"] == "cesto"
    assert applied["pasos"][1]["estado"] == "COMPLETADO"
    with app.app_context():
        piece = db.session.get(Pieza, refs["piezas"][0]["pieza_ref"])
        assert piece.linea_id is None
        assert piece.familia_id is None
        assert db.session.get(Molde, refs["molde_ref"]) is not None
        assert db.session.get(
            MoldePieza, refs["piezas"][0]["molde_pieza_ref"]
        ) is not None

    replay = client.post(
        (
            f"/api/scm/v1/altas-producto/{onboarding['id']}"
            "/pasos/COMPONENTES/aplicar"
        ),
        headers=_headers(actor_id, operation_id),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == applied

    changed = copy.deepcopy(payload)
    changed["expected_version"] = applied["version"]
    changed["application_key"] = "components-colador-v2"
    changed["data"]["molde"]["nombre"] = "MOLDE DUPLICADO"
    rejected = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json=changed,
    )
    assert rejected.status_code == 422
    assert rejected.get_json()["error"]["code"] == (
        "ONBOARDING_STEP_ALREADY_APPLIED"
    )
    with app.app_context():
        assert Molde.query.count() == 1
        assert Pieza.query.count() == 1


def test_apply_components_supports_multiple_molds_in_one_product(app, client):
    actor_id = _catalog_admin(app)
    onboarding = _materialize_identity(
        client, actor_id, _create_session(client, actor_id, data={})
    )
    payload = {
        "expected_version": onboarding["version"],
        "application_key": "components-portavajilla-multimolde-v1",
        "data": {
            "moldes": [
                {
                    "client_id": "molde-tapa",
                    "molde": {
                        "modo": "NUEVO",
                        "nombre": "MOLDE TAPA PORTAVAJILLA",
                        "peso_tiro_gr": 390,
                    },
                    "piezas": [{
                        "client_id": "pieza-tapa",
                        "modo": "NUEVA",
                        "nombre": "TAPA PORTAVAJILLA",
                        "cavidades": 1,
                        "peso_unitario_gr": 380,
                    }],
                },
                {
                    "client_id": "molde-base",
                    "molde": {
                        "modo": "NUEVO",
                        "nombre": "MOLDE BASE PORTAVAJILLA",
                        "peso_tiro_gr": 395,
                    },
                    "piezas": [{
                        "client_id": "pieza-base",
                        "modo": "NUEVA",
                        "nombre": "BASE PORTAVAJILLA",
                        "cavidades": 1,
                        "peso_unitario_gr": 380,
                    }],
                },
            ],
        },
    }
    operation_id = uuid4()
    response = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, operation_id),
        json=payload,
    )
    assert response.status_code == 200, response.get_json()
    applied = response.get_json()
    refs = applied["application_results"]["resolved_references"]
    assert "molde_ref" not in refs
    assert {item["client_id"] for item in refs["moldes"]} == {
        "molde-tapa", "molde-base"
    }
    assert len(refs["piezas"]) == 2
    assert {item["molde_client_id"] for item in refs["piezas"]} == {
        "molde-tapa", "molde-base"
    }
    with app.app_context():
        assert Molde.query.count() == 2
        assert Pieza.query.count() == 2
        assert MoldePieza.query.count() == 2

    replay = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, operation_id),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == applied

    with app.app_context():
        family = FamiliaColor(codigo=9921, nombre="TRANSPARENTE MULTIMOLDE")
        db.session.add(family)
        db.session.commit()
        family_id = family.id
    colors = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": applied["version"],
            "application_key": "colors-portavajilla-multimolde-v1",
            "data": {
                "colores": [{
                    "client_id": "transparente",
                    "modo": "NUEVO",
                    "nombre": "TRANSPARENTE PORTAVAJILLA",
                    "familia_color_id": family_id,
                    "hex": "#FFFFFF",
                }],
                "matriz": [{
                    "pieza_client_id": piece_client_id,
                    "color_client_id": "transparente",
                    "seleccionada": True,
                } for piece_client_id in ("pieza-tapa", "pieza-base")],
                "formulaciones": [{
                    "color_client_id": "transparente",
                    "tipo": "PENDIENTE",
                    "motivo_pendiente": "Receta por validar",
                }],
            },
        },
    )
    assert colors.status_code == 200, colors.get_json()
    matrix = colors.get_json()["application_results"][
        "resolved_references"
    ]["matriz"]
    assert {item["pieza_ref"] for item in matrix} == {
        item["pieza_ref"] for item in refs["piezas"]
    }


def test_applied_legacy_components_can_add_a_second_mold_explicitly(app, client):
    actor_id = _catalog_admin(app)
    onboarding = _materialize_identity(
        client, actor_id, _create_session(client, actor_id, data={})
    )
    first_key = "components-legacy-one-mold-v1"
    first = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "application_key": first_key,
            "data": {
                "molde": {
                    "modo": "NUEVO", "nombre": "MOLDE TAPA LEGACY",
                    "peso_tiro_gr": 390,
                },
                "piezas": [{
                    "client_id": "pieza-tapa", "modo": "NUEVA",
                    "nombre": "TAPA LEGACY", "cavidades": 1,
                    "peso_unitario_gr": 380,
                }],
            },
        },
    ).get_json()
    first_refs = first["referencias"]["COMPONENTES"]
    second = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": first["version"],
            "application_key": "components-add-base-v2",
            "supersedes_application_key": first_key,
            "data": {
                "moldes": [{
                    "client_id": "molde-inicial",
                    "molde": {"modo": "REUTILIZAR", "ref": first_refs["molde_ref"]},
                    "piezas": [{
                        "client_id": "pieza-tapa", "modo": "REUTILIZAR",
                        "ref": first_refs["piezas"][0]["pieza_ref"],
                        "cavidades": 1, "peso_unitario_gr": 380,
                    }],
                }, {
                    "client_id": "molde-base",
                    "molde": {
                        "modo": "NUEVO", "nombre": "MOLDE BASE AGREGADO",
                        "peso_tiro_gr": 395,
                    },
                    "piezas": [{
                        "client_id": "pieza-base", "modo": "NUEVA",
                        "nombre": "BASE AGREGADA", "cavidades": 1,
                        "peso_unitario_gr": 380,
                    }],
                }],
            },
        },
    )
    assert second.status_code == 200, second.get_json()
    refs = second.get_json()["referencias"]["COMPONENTES"]
    assert len(refs["moldes"]) == 2
    assert {item["client_id"] for item in refs["piezas"]} == {
        "pieza-tapa", "pieza-base"
    }
    with app.app_context():
        assert Molde.query.count() == 2
        assert Pieza.query.count() == 2


def test_apply_steps_require_materialized_predecessors_without_side_effects(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _create_session(client, actor_id, data={})
    components = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "application_key": "components-out-of-order-v1",
            "data": {
                "molde": {
                    "modo": "NUEVO",
                    "nombre": "NO DEBE CREARSE",
                    "peso_tiro_gr": 100,
                },
                "piezas": [{
                    "client_id": "fuera-orden",
                    "modo": "NUEVA",
                    "nombre": "NO DEBE CREARSE",
                    "cavidades": 1,
                    "peso_unitario_gr": 90,
                }],
            },
        },
    )
    assert components.status_code == 422
    assert components.get_json()["error"]["details"]["required_step"] == (
        "IDENTIDAD"
    )
    onboarding = _materialize_identity(client, actor_id, onboarding)
    colors = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "application_key": "colors-out-of-order-v1",
            "data": {"colores": [], "matriz": [], "formulaciones": []},
        },
    )
    assert colors.status_code == 422
    assert colors.get_json()["error"]["details"]["required_step"] == (
        "COMPONENTES"
    )
    with app.app_context():
        assert Molde.query.count() == 0
        assert Pieza.query.count() == 0
        assert PiezaColor.query.count() == 0
        assert RecetaColorMaestra.query.count() == 0


def test_color_formulation_cannot_reference_undeclared_color(app, client):
    actor_id = _catalog_admin(app)
    onboarding = _materialize_identity(
        client,
        actor_id,
        _create_session(client, actor_id, data={}),
    )
    components = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "application_key": "components-formula-scope-v1",
            "data": {
                "molde": {
                    "modo": "NUEVO",
                    "nombre": "MOLDE FORMULA SCOPE",
                    "peso_tiro_gr": 100,
                },
                "piezas": [{
                    "client_id": "pieza-a",
                    "modo": "NUEVA",
                    "nombre": "PIEZA A",
                    "cavidades": 1,
                    "peso_unitario_gr": 90,
                }],
            },
        },
    ).get_json()
    with app.app_context():
        before = (
            PiezaColor.query.count(),
            RecetaColorMaestra.query.count(),
        )
    response = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": components["version"],
            "application_key": "colors-formula-scope-v1",
            "data": {
                "colores": [{
                    "client_id": "declarado",
                    "modo": "REUTILIZAR",
                    "color_ref": 1,
                }],
                "matriz": [],
                "formulaciones": [{
                    "color_ref": 999999,
                    "tipo": "PENDIENTE",
                    "motivo_pendiente": "Color externo",
                }],
            },
        },
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == (
        "FORMULATION_REFERENCE_OUT_OF_SCOPE"
    )
    with app.app_context():
        assert (
            PiezaColor.query.count(),
            RecetaColorMaestra.query.count(),
        ) == before


def test_apply_colors_supports_pigment_free_and_pending_without_fake_recipe(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _create_session(client, actor_id, data={})
    onboarding = _materialize_identity(client, actor_id, onboarding)
    components_response = client.post(
        (
            f"/api/scm/v1/altas-producto/{onboarding['id']}"
            "/pasos/COMPONENTES/aplicar"
        ),
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "application_key": "components-colors-v1",
            "data": {
                "molde": {
                    "modo": "NUEVO",
                    "nombre": "MOLDE TRANSPARENTE",
                    "peso_tiro_gr": 100,
                },
                "piezas": [{
                    "client_id": "tapa",
                    "modo": "NUEVA",
                    "nombre": "TAPA TRANSPARENTE",
                    "cavidades": 1,
                    "peso_unitario_gr": 90,
                }],
            },
        },
    )
    assert components_response.status_code == 200
    components = components_response.get_json()
    with app.app_context():
        family = FamiliaColor(
            nombre="TRANSPARENTE ALTA GUIADA",
            codigo=9901,
        )
        db.session.add(family)
        db.session.flush()
        category = ScmCategoriaRecepcion.query.first()
        if category is None:
            category = ScmCategoriaRecepcion(
                codigo="CAT-ALTA-GUIADA",
                nombre="Categoria alta guiada",
                modalidad_default="POR_CONFIGURAR",
                recepcion_habilitada=False,
            )
            db.session.add(category)
            db.session.flush()
        resin = ScmMaterial(
            codigo="MP-ALTA-GUIADA-001",
            nombre="PP VIRGEN ALTA GUIADA",
            clase="MATERIA_PRIMA",
            categoria_recepcion_id=category.id,
        )
        db.session.add(resin)
        db.session.commit()
        family_id = family.id
        resin_id = resin.id

    response = client.post(
        (
            f"/api/scm/v1/altas-producto/{onboarding['id']}"
            "/pasos/COLORES/aplicar"
        ),
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": components["version"],
            "application_key": "colors-transparent-v1",
            "data": {
                "colores": [
                    {
                        "client_id": "transparente",
                        "modo": "NUEVO",
                        "nombre": "TRANSPARENTE SIN PIGMENTO",
                        "familia_color_id": family_id,
                        "hex": "#FFFFFF",
                    },
                    {
                        "client_id": "pendiente",
                        "modo": "NUEVO",
                        "nombre": "VERDE PASTO PENDIENTE",
                        "familia_color_id": family_id,
                        "hex": "#2E7D32",
                    },
                ],
                "matriz": [
                    {
                        "pieza_client_id": "tapa",
                        "color_client_id": "transparente",
                        "seleccionada": True,
                    },
                    {
                        "pieza_client_id": "tapa",
                        "color_client_id": "pendiente",
                        "seleccionada": True,
                    },
                ],
                "formulaciones": [
                    {
                        "color_client_id": "transparente",
                        "tipo": "SIN_PIGMENTO",
                        "base_virgen_kg": 25,
                        "componentes": [{
                            "material_id": resin_id,
                            "tipo_componente": "MATERIA_PRIMA",
                            "cantidad": 1,
                        }],
                    },
                    {
                        "color_client_id": "pendiente",
                        "tipo": "PENDIENTE",
                        "motivo_pendiente": "Falta validar receta de planta",
                    },
                ],
            },
        },
    )
    assert response.status_code == 200, response.get_json()
    applied = response.get_json()
    results = applied["application_results"]
    assert results["status"] == "APPLIED"
    assert len(results["pending"]) == 1
    assert applied["pasos"][2]["estado"] == "EN_PROGRESO"
    formulas = results["resolved_references"]["formulaciones"]
    pigment_free = next(
        item for item in formulas if item["tipo"] == "SIN_PIGMENTO"
    )
    pending = next(item for item in formulas if item["tipo"] == "PENDIENTE")
    assert pigment_free["receta_ref"] is not None
    assert pigment_free["estado"] == "SIN_PIGMENTO"
    assert pending["receta_ref"] is None
    assert pending["estado"] == "PENDIENTE"
    with app.app_context():
        recipe = db.session.get(
            RecetaColorMaestra, pigment_free["receta_ref"]
        )
        assert recipe.estado == "BORRADOR"
        assert len(recipe.lineas) == 1
        assert recipe.lineas[0].tipo_componente == "MATERIA_PRIMA"
        pending_color = pending["color_ref"]
        assert RecetaColorMaestra.query.filter_by(
            color_produccion_id=pending_color
        ).count() == 0
        for row in results["resolved_references"]["matriz"]:
            variant = db.session.get(PiezaColor, row["pieza_color_ref"])
            assert variant.linea_id is None
            assert variant.familia_id is None


def test_apply_components_partial_failure_checkpoints_and_resumes_same_key(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _create_session(client, actor_id, data={})
    onboarding = _materialize_identity(client, actor_id, onboarding)
    payload = {
        "expected_version": onboarding["version"],
        "application_key": "components-partial-v1",
        "data": {
            "molde": {
                "modo": "NUEVO",
                "nombre": "MOLDE REANUDABLE",
                "peso_tiro_gr": 180,
            },
            "piezas": [
                {
                    "client_id": "primera",
                    "modo": "NUEVA",
                    "nombre": "PIEZA YA APLICADA",
                    "cavidades": 1,
                    "peso_unitario_gr": 80,
                },
                {
                    "client_id": "segunda",
                    "modo": "REUTILIZAR",
                    "ref": 999,
                    "cavidades": 1,
                    "peso_unitario_gr": 70,
                },
            ],
        },
    }
    response = client.post(
        (
            f"/api/scm/v1/altas-producto/{onboarding['id']}"
            "/pasos/COMPONENTES/aplicar"
        ),
        headers=_headers(actor_id, uuid4()),
        json=payload,
    )
    assert response.status_code == 422, response.get_json()
    error = response.get_json()["error"]
    assert error["code"] == "ONBOARDING_APPLICATION_PARTIAL"
    current = error["details"]["current_session"]
    results = error["details"]["application_results"]
    assert results["status"] == "PARTIAL"
    assert len(results["resolved_references"]["piezas"]) == 1
    first_piece_ref = results["resolved_references"]["piezas"][0][
        "pieza_ref"
    ]
    mold_ref = results["resolved_references"]["molde_ref"]
    with app.app_context():
        assert db.session.get(Pieza, first_piece_ref) is not None

    payload["expected_version"] = current["version"]
    payload["data"]["piezas"][1] = {
        "client_id": "segunda",
        "modo": "NUEVA",
        "nombre": "PIEZA CORREGIDA AL REINTENTAR",
        "cavidades": 1,
        "peso_unitario_gr": 70,
    }
    resumed_response = client.post(
        (
            f"/api/scm/v1/altas-producto/{onboarding['id']}"
            "/pasos/COMPONENTES/aplicar"
        ),
        headers=_headers(actor_id, uuid4()),
        json=payload,
    )
    assert resumed_response.status_code == 200, resumed_response.get_json()
    resumed = resumed_response.get_json()
    assert resumed["application_results"]["status"] == "APPLIED"
    resolved_pieces = resumed["application_results"][
        "resolved_references"
    ]["piezas"]
    assert {item["client_id"] for item in resolved_pieces} == {
        "primera", "segunda"
    }
    with app.app_context():
        assert Pieza.query.filter_by(nombre="PIEZA YA APLICADA").count() == 1
        assert Pieza.query.filter_by(
            nombre="PIEZA CORREGIDA AL REINTENTAR"
        ).count() == 1
        assert Molde.query.filter_by(codigo=mold_ref).count() == 1


def test_partial_application_rejects_changes_to_checkpointed_units(app, client):
    actor_id = _catalog_admin(app)
    onboarding = _create_session(client, actor_id, data={})
    onboarding = _materialize_identity(client, actor_id, onboarding)
    payload = {
        "expected_version": onboarding["version"],
        "application_key": "components-checkpoint-immutable-v1",
        "data": {
            "molde": {
                "modo": "NUEVO",
                "nombre": "MOLDE CHECKPOINT",
                "peso_tiro_gr": 180,
            },
            "piezas": [
                {
                    "client_id": "aplicada",
                    "modo": "NUEVA",
                    "nombre": "PIEZA CHECKPOINT",
                    "cavidades": 1,
                    "peso_unitario_gr": 80,
                },
                {
                    "client_id": "pendiente",
                    "modo": "REUTILIZAR",
                    "ref": 999,
                    "cavidades": 1,
                    "peso_unitario_gr": 70,
                },
            ],
        },
    }
    partial = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json=payload,
    )
    assert partial.status_code == 422
    current = partial.get_json()["error"]["details"]["current_session"]
    payload["expected_version"] = current["version"]
    payload["data"]["piezas"][0]["nombre"] = "NO PUEDE CAMBIAR"

    conflict = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json=payload,
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == (
        "CHECKPOINTED_APPLICATION_DATA_CHANGED"
    )


def test_partial_colors_resume_accepts_normalized_reuse_refs_without_duplicates(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _materialize_identity(
        client,
        actor_id,
        _create_session(client, actor_id, data={}),
    )
    components_response = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "application_key": "components-color-retry-v1",
            "data": {
                "molde": {
                    "modo": "NUEVO",
                    "nombre": "MOLDE COLOR RETRY",
                    "peso_tiro_gr": 100,
                },
                "piezas": [{
                    "client_id": "pieza-retry",
                    "modo": "NUEVA",
                    "nombre": "PIEZA COLOR RETRY",
                    "cavidades": 1,
                    "peso_unitario_gr": 90,
                }],
            },
        },
    )
    assert components_response.status_code == 200
    components = components_response.get_json()
    piece_ref = components["referencias"]["COMPONENTES"]["piezas"][0][
        "pieza_ref"
    ]
    with app.app_context():
        family = FamiliaColor(
            codigo=9910,
            nombre="COLOR RETRY FAMILY",
        )
        db.session.add(family)
        db.session.commit()
        family_id = family.id

    initial_data = {
        "colores": [{
            "client_id": "color-retry",
            "modo": "NUEVO",
            "nombre": "COLOR CHECKPOINT RETRY",
            "familia_color_id": family_id,
            "hex": "#123456",
        }],
        "matriz": [],
        "formulaciones": [{
            "color_client_id": "color-retry",
            "tipo": "PENDIENTE",
            "motivo_pendiente": "Pendiente de planta",
        }],
    }
    partial = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": components["version"],
            "application_key": "colors-normalized-retry-v1",
            "data": initial_data,
        },
    )
    assert partial.status_code == 422, partial.get_json()
    details = partial.get_json()["error"]["details"]
    assert details["application_results"]["status"] == "PARTIAL"
    color_ref = details["application_results"]["resolved_references"][
        "colores"
    ][0]["color_ref"]
    with app.app_context():
        before_count = ColorProduccion.query.count()

    resumed = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": details["current_session"]["version"],
            "application_key": "colors-normalized-retry-v1",
            "data": {
                "colores": [{
                    "client_id": "color-retry",
                    "modo": "REUTILIZAR",
                    "color_ref": color_ref,
                }],
                "matriz": [{
                    "pieza_ref": piece_ref,
                    "color_ref": color_ref,
                    "seleccionada": True,
                }],
                "formulaciones": [{
                    "color_ref": color_ref,
                    "tipo": "PENDIENTE",
                    "motivo_pendiente": "Pendiente de planta",
                }],
            },
        },
    )
    assert resumed.status_code == 200, resumed.get_json()
    assert resumed.get_json()["application_results"]["status"] == "APPLIED"
    with app.app_context():
        assert ColorProduccion.query.count() == before_count
        assert db.session.get(ColorProduccion, color_ref) is not None


def test_partial_colors_resume_reconciles_checkpointed_recipe_as_existing(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _materialize_identity(
        client,
        actor_id,
        _create_session(client, actor_id, data={}),
    )
    components = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "application_key": "components-formula-retry-v1",
            "data": {
                "molde": {
                    "modo": "NUEVO",
                    "nombre": "MOLDE FORMULA RETRY",
                    "peso_tiro_gr": 100,
                },
                "piezas": [{
                    "client_id": "pieza-formula-retry",
                    "modo": "NUEVA",
                    "nombre": "PIEZA FORMULA RETRY",
                    "cavidades": 1,
                    "peso_unitario_gr": 90,
                }],
            },
        },
    ).get_json()
    piece_ref = components["referencias"]["COMPONENTES"]["piezas"][0][
        "pieza_ref"
    ]
    with app.app_context():
        family = FamiliaColor(codigo=9912, nombre="FORMULA RETRY FAMILY")
        category = ScmCategoriaRecepcion(
            codigo="CAT-FORMULA-RETRY",
            nombre="Categoria formula retry",
            modalidad_default="POR_CONFIGURAR",
            recepcion_habilitada=False,
        )
        db.session.add_all([family, category])
        db.session.flush()
        resin = ScmMaterial(
            codigo="MP-FORMULA-RETRY",
            nombre="Resina formula retry",
            clase="MATERIA_PRIMA",
            categoria_recepcion_id=category.id,
        )
        db.session.add(resin)
        db.session.commit()
        family_id = family.id
        resin_id = resin.id

    initial_data = {
        "colores": [
            {
                "client_id": "primero",
                "modo": "NUEVO",
                "nombre": "FORMULA RETRY PRIMERO",
                "familia_color_id": family_id,
                "hex": "#112233",
            },
            {
                "client_id": "segundo",
                "modo": "NUEVO",
                "nombre": "FORMULA RETRY SEGUNDO",
                "familia_color_id": family_id,
                "hex": "#334455",
            },
        ],
        "matriz": [
            {
                "pieza_client_id": "pieza-formula-retry",
                "color_client_id": "primero",
                "seleccionada": True,
            },
            {
                "pieza_client_id": "pieza-formula-retry",
                "color_client_id": "segundo",
                "seleccionada": True,
            },
        ],
        "formulaciones": [
            {
                "color_client_id": "primero",
                "tipo": "SIN_PIGMENTO",
                "componentes": [{
                    "material_id": resin_id,
                    "tipo_componente": "MATERIA_PRIMA",
                    "cantidad": 1,
                }],
            },
            {
                "color_client_id": "segundo",
                "tipo": "SIN_PIGMENTO",
                "componentes": [{
                    "material_id": 999999,
                    "tipo_componente": "MATERIA_PRIMA",
                    "cantidad": 1,
                }],
            },
        ],
    }
    partial = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": components["version"],
            "application_key": "colors-formula-retry-v1",
            "data": initial_data,
        },
    )
    assert partial.status_code == 422, partial.get_json()
    details = partial.get_json()["error"]["details"]
    results = details["application_results"]
    assert results["status"] == "PARTIAL"
    assert len(results["resolved_references"]["formulaciones"]) == 1
    first_formula = results["resolved_references"]["formulaciones"][0]
    first_recipe_ref = first_formula["receta_ref"]
    colors_by_client = {
        item["client_id"]: item["color_ref"]
        for item in results["resolved_references"]["colores"]
    }
    with app.app_context():
        assert RecetaColorMaestra.query.count() == 1

    retry_data = copy.deepcopy(initial_data)
    retry_data["colores"] = [
        {
            "client_id": client_id,
            "modo": "REUTILIZAR",
            "color_ref": color_ref,
        }
        for client_id, color_ref in colors_by_client.items()
    ]
    retry_data["matriz"] = [
        {
            "pieza_ref": piece_ref,
            "color_ref": color_ref,
            "seleccionada": True,
        }
        for color_ref in colors_by_client.values()
    ]
    retry_data["formulaciones"] = [
        {
            "color_ref": colors_by_client["primero"],
            "tipo": "EXISTENTE",
            "receta_ref": first_recipe_ref,
        },
        {
            "color_ref": colors_by_client["segundo"],
            "tipo": "SIN_PIGMENTO",
            "componentes": [{
                "material_id": resin_id,
                "tipo_componente": "MATERIA_PRIMA",
                "cantidad": 1,
            }],
        },
    ]
    resumed = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": details["current_session"]["version"],
            "application_key": "colors-formula-retry-v1",
            "data": retry_data,
        },
    )
    assert resumed.status_code == 200, resumed.get_json()
    formulas = resumed.get_json()["application_results"][
        "resolved_references"
    ]["formulaciones"]
    assert next(
        row for row in formulas
        if row["color_ref"] == colors_by_client["primero"]
    )["receta_ref"] == first_recipe_ref
    with app.app_context():
        assert RecetaColorMaestra.query.count() == 2
        assert db.session.get(RecetaColorMaestra, first_recipe_ref) is not None


def test_colors_with_pending_formula_can_be_superseded_without_orphans(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _materialize_identity(
        client,
        actor_id,
        _create_session(client, actor_id, data={}),
    )
    components_response = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COMPONENTES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": onboarding["version"],
            "application_key": "components-colors-reopen-v1",
            "data": {
                "molde": {
                    "modo": "NUEVO",
                    "nombre": "MOLDE COLORS REOPEN",
                    "peso_tiro_gr": 100,
                },
                "piezas": [{
                    "client_id": "pieza-colors-reopen",
                    "modo": "NUEVA",
                    "nombre": "PIEZA COLORS REOPEN",
                    "cavidades": 1,
                    "peso_unitario_gr": 90,
                }],
            },
        },
    )
    assert components_response.status_code == 200
    components = components_response.get_json()
    piece_ref = components["referencias"]["COMPONENTES"]["piezas"][0][
        "pieza_ref"
    ]
    with app.app_context():
        family = FamiliaColor(codigo=9916, nombre="REOPEN FINISH")
        db.session.add(family)
        db.session.commit()
        family_id = family.id

    first_payload = {
        "colores": [{
            "client_id": "color-original",
            "modo": "NUEVO",
            "nombre": "COLOR ORIGINAL REOPEN",
            "familia_color_id": family_id,
            "hex": "#112233",
        }],
        "matriz": [{
            "pieza_ref": piece_ref,
            "color_client_id": "color-original",
            "seleccionada": True,
        }],
        "formulaciones": [{
            "color_client_id": "color-original",
            "tipo": "PENDIENTE",
            "motivo_pendiente": "Falta validar receta",
        }],
    }
    first_response = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": components["version"],
            "application_key": "colors-pending-original-v1",
            "data": first_payload,
        },
    )
    assert first_response.status_code == 200, first_response.get_json()
    first = first_response.get_json()
    assert next(
        row for row in first["pasos"] if row["codigo"] == "COLORES"
    )["estado"] == "EN_PROGRESO"
    original_color_ref = first["referencias"]["COLORES"]["colores"][0][
        "color_ref"
    ]

    corrected_payload = {
        "colores": [
            {
                "client_id": "color-original",
                "modo": "REUTILIZAR",
                "color_ref": original_color_ref,
            },
            {
                "client_id": "color-added",
                "modo": "NUEVO",
                "nombre": "COLOR ADDED REOPEN",
                "familia_color_id": family_id,
                "hex": "#445566",
            },
        ],
        "matriz": [
            {
                "pieza_ref": piece_ref,
                "color_ref": original_color_ref,
                "seleccionada": True,
            },
            {
                "pieza_ref": piece_ref,
                "color_client_id": "color-added",
                "seleccionada": True,
            },
        ],
        "formulaciones": [
            {
                "color_ref": original_color_ref,
                "tipo": "PENDIENTE",
                "motivo_pendiente": "Receta original por confirmar",
            },
            {
                "color_client_id": "color-added",
                "tipo": "PENDIENTE",
                "motivo_pendiente": "Receta nueva por confirmar",
            },
        ],
    }
    corrected_response = client.post(
        f"/api/scm/v1/altas-producto/{onboarding['id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": first["version"],
            "application_key": "colors-pending-corrected-v2",
            "supersedes_application_key": "colors-pending-original-v1",
            "data": corrected_payload,
        },
    )
    assert corrected_response.status_code == 200, corrected_response.get_json()
    corrected = corrected_response.get_json()
    assert len(corrected["referencias"]["COLORES"]["colores"]) == 2
    assert corrected["pasos"][2]["application_status"][
        "application_key"
    ] == "colors-pending-corrected-v2"
    with app.app_context():
        stored = db.session.get(
            ScmAltaProductoSesion,
            UUID(onboarding["id"]),
        )
        journal = stored.application_journal_json["COLORES"]
        assert journal["colors-pending-original-v1"]["superseded_by"] == (
            "colors-pending-corrected-v2"
        )
        assert ColorProduccion.query.filter(
            ColorProduccion.id == original_color_ref
        ).count() == 1


def test_onboarding_is_private_to_creator_and_admin_can_audit(app, client):
    owner_id = _catalog_admin(app)
    owned = _create_session(client, owner_id, data={})
    with app.app_context():
        article_capability = ScmCapacidad.query.filter_by(
            codigo="ARTICULO_ADMINISTRAR"
        ).one()
        audit_capability = ScmCapacidad.query.filter_by(
            codigo="AUTORIZACION_SCM_ADMINISTRAR"
        ).one_or_none()
        if audit_capability is None:
            audit_capability = ScmCapacidad(
                codigo="AUTORIZACION_SCM_ADMINISTRAR",
                nombre="Administrar autorizaciones SCM",
            )
        foreign_role = RolOperativo(
            codigo="GESTOR_OTRO",
            nombre="Gestor otro",
            activo=True,
        )
        foreign_role.capacidades.append(article_capability)
        foreign_actor = Trabajador(
            codigo="TRB-OTRO",
            nombres="Otro",
            apellidos="Gestor",
            activo=True,
        )
        foreign_actor.roles.append(foreign_role)
        auditor_role = RolOperativo(
            codigo="AUDITOR_ALTAS",
            nombre="Auditor altas",
            activo=True,
        )
        auditor_role.capacidades.extend([
            article_capability,
            audit_capability,
        ])
        auditor = Trabajador(
            codigo="TRB-AUDITOR",
            nombres="Auditor",
            apellidos="SCM",
            activo=True,
        )
        auditor.roles.append(auditor_role)
        db.session.add_all([
            audit_capability,
            foreign_role,
            foreign_actor,
            auditor_role,
            auditor,
        ])
        db.session.commit()
        foreign_id = foreign_actor.id
        auditor_id = auditor.id

    foreign_get = client.get(
        f"/api/scm/v1/altas-producto/{owned['id']}",
        headers=_headers(foreign_id),
    )
    assert foreign_get.status_code == 404
    assert foreign_get.get_json()["error"]["code"] == (
        "ONBOARDING_SESSION_NOT_FOUND"
    )
    foreign_list = client.get(
        "/api/scm/v1/altas-producto",
        headers=_headers(foreign_id),
    )
    assert foreign_list.status_code == 200
    assert foreign_list.get_json() == []
    foreign_write = client.put(
        f"/api/scm/v1/altas-producto/{owned['id']}/pasos/IDENTIDAD",
        headers=_headers(foreign_id, uuid4()),
        json={
            "expected_version": owned["version"],
            "data": {"producto": {"producto": "NO DEBE VERSE"}},
        },
    )
    assert foreign_write.status_code == 404

    audited = client.get(
        f"/api/scm/v1/altas-producto/{owned['id']}",
        headers=_headers(auditor_id),
    )
    assert audited.status_code == 200
    assert audited.get_json()["id"] == owned["id"]
    audit_list = client.get(
        "/api/scm/v1/altas-producto",
        headers=_headers(auditor_id),
    )
    assert owned["id"] in {item["id"] for item in audit_list.get_json()}

    for suffix, method, body in (
        (
            "/pasos/IDENTIDAD",
            client.put,
            {
                "expected_version": owned["version"],
                "data": {"producto": {"producto": "TAKEOVER"}},
            },
        ),
        (
            "/pasos/IDENTIDAD/aplicar",
            client.post,
            {
                "expected_version": owned["version"],
                "application_key": "foreign-takeover-v1",
                "data": {"modo": "REUTILIZAR", "producto_ref": "PT-X"},
            },
        ),
        (
            "/validar",
            client.post,
            {"expected_version": owned["version"]},
        ),
        (
            "/finalizar",
            client.post,
            {"expected_version": owned["version"]},
        ),
    ):
        denied = method(
            f"/api/scm/v1/altas-producto/{owned['id']}{suffix}",
            headers=_headers(auditor_id, uuid4()),
            json=body,
        )
        assert denied.status_code == 404
        assert denied.get_json()["error"]["code"] == (
            "ONBOARDING_SESSION_NOT_FOUND"
        )


def test_application_status_elige_journal_mas_reciente_sin_orden_json(
    app, client
):
    actor_id = _catalog_admin(app)
    onboarding = _create_session(client, actor_id, data={})
    with app.app_context():
        model = ScmAltaProductoSesion.query.one()
        refs = {"producto_terminado_id": "PT-TEMPORAL"}
        model.referencias_json = {"IDENTIDAD": refs}
        model.application_journal_json = {
            "IDENTIDAD": {
                "zzzz-antigua": {
                    "status": "APPLIED",
                    "session_version": 2,
                    "recorded_at": "2026-08-10T08:00:00+00:00",
                    "result": {"resolved_references": refs},
                },
                "aaaa-reciente": {
                    "status": "APPLIED",
                    "session_version": 3,
                    "recorded_at": "2026-08-10T09:00:00+00:00",
                    "result": {"resolved_references": refs},
                },
            },
        }
        db.session.commit()
        db.session.expire_all()

    response = client.get(
        f"/api/scm/v1/altas-producto/{onboarding['id']}",
        headers=_headers(actor_id),
    )
    assert response.status_code == 200
    assert response.get_json()["pasos"][0]["application_status"][
        "application_key"
    ] == "aaaa-reciente"
