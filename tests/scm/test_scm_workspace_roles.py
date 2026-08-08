from app.extensions import db
from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import ScmCapacidad
from app.models.trabajador import RolOperativo, Trabajador


ADMIN_CAPABILITY = "AUTORIZACION_SCM_ADMINISTRAR"


def _headers(actor_id):
    return {"X-Actor-Id": str(actor_id)}


def _grant_admin(actor):
    capability = ScmCapacidad(
        codigo=ADMIN_CAPABILITY,
        nombre="Administrar autorizaciones SCM",
    )
    actor.roles[0].capacidades.append(capability)
    db.session.add(capability)
    db.session.commit()
    return capability


def test_capabilities_require_admin_and_expose_catalog_contract(app, client):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id
        capability = ScmCapacidad(
            codigo="INVENTARIO_VER",
            nombre="Consultar inventario SCM",
            descripcion="Consulta saldos y movimientos.",
        )
        db.session.add(capability)
        db.session.commit()

    denied = client.get(
        "/api/catalogo/capacidades",
        headers=_headers(actor_id),
    )
    assert denied.status_code == 403
    assert denied.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"

    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        _grant_admin(actor)
        assert actor.tiene_capacidad(ADMIN_CAPABILITY)

    response = client.get(
        "/api/catalogo/capacidades",
        headers=_headers(actor_id),
    )

    assert response.status_code == 200, response.get_json()
    assert {
        "id",
        "codigo",
        "nombre",
        "descripcion",
        "activo",
    } == set(response.get_json()[0])
    assert {item["codigo"] for item in response.get_json()} == {
        ADMIN_CAPABILITY,
        "INVENTARIO_VER",
    }


def test_worker_catalog_mutations_require_admin_in_local_mode(app, client):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id

    for response in (
        client.post(
            "/api/catalogo/trabajadores",
            headers=_headers(actor_id),
            json={"nombres": "Sin", "apellidos": "Permiso"},
        ),
        client.put(
            f"/api/catalogo/trabajadores/{actor_id}",
            headers=_headers(actor_id),
            json={"nombre_corto": "Sin permiso"},
        ),
        client.patch(
            f"/api/catalogo/trabajadores/{actor_id}/estado",
            headers=_headers(actor_id),
            json={"activo": False},
        ),
    ):
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == (
            "CAPABILITY_REQUIRED"
        )


def test_worker_autoassigns_only_one_active_role_without_resolving_ambiguity(
    app,
    client,
):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id
        _grant_admin(actor)
        active_a = RolOperativo(codigo="ACTIVO_A", nombre="Activo A")
        active_b = RolOperativo(codigo="ACTIVO_B", nombre="Activo B")
        inactive = RolOperativo(
            codigo="INACTIVO_A",
            nombre="Inactivo A",
            activo=False,
        )
        db.session.add_all([active_a, active_b, inactive])
        db.session.commit()
        active_a_id = active_a.id
        active_b_id = active_b.id
        inactive_id = inactive.id

    unambiguous = client.post(
        "/api/catalogo/trabajadores",
        headers=_headers(actor_id),
        json={
            "nombres": "Una",
            "apellidos": "Funcion",
            "roles_ids": [active_a_id, inactive_id],
        },
    )
    assert unambiguous.status_code == 201, unambiguous.get_json()
    assert unambiguous.get_json()["rol_principal"]["id"] == active_a_id
    assert unambiguous.get_json()["rol_principal_pendiente"] is False

    ambiguous = client.post(
        "/api/catalogo/trabajadores",
        headers=_headers(actor_id),
        json={
            "nombres": "Dos",
            "apellidos": "Funciones",
            "roles_ids": [active_a_id, active_b_id],
        },
    )
    assert ambiguous.status_code == 201, ambiguous.get_json()
    assert ambiguous.get_json()["rol_principal"] is None
    assert ambiguous.get_json()["rol_principal_pendiente"] is True

    with app.app_context():
        events = ScmEvento.query.filter_by(
            aggregate_type="TRABAJADOR_ROL_PRINCIPAL",
            aggregate_id=str(unambiguous.get_json()["id"]),
        ).all()
        assert [event.tipo for event in events] == [
            "ROL_PRINCIPAL_AUTOASIGNADO"
        ]


def test_role_workspace_crud_is_versioned_audited_and_preserves_unknown_keys(
    app,
    client,
):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id
        _grant_admin(actor)
        db.session.add_all([
            ScmCapacidad(
                codigo="INVENTARIO_VER",
                nombre="Consultar inventario SCM",
            ),
            ScmCapacidad(
                codigo="OP_VER",
                nombre="Consultar ordenes de produccion",
            ),
        ])
        db.session.commit()

    created = client.post(
        "/api/catalogo/roles-operativos",
        headers=_headers(actor_id),
        json={
            "codigo": "AUDITOR_INVENTARIO",
            "nombre": "Auditor de inventario",
            "activo": True,
            "capacidad_codigos": ["INVENTARIO_VER"],
            "workspace_focus": "Revisar existencias trazables.",
            "workspace_start_feature": "warehouse.kardex",
            "workspace_preferencias": [
                {
                    "feature_key": "warehouse.kardex",
                    "prioridad": 10,
                    "fijada": True,
                },
                {
                    "feature_key": "retired.future.feature",
                    "prioridad": 90,
                    "fijada": False,
                },
            ],
        },
    )

    assert created.status_code == 201, created.get_json()
    role = created.get_json()
    assert role["version"] == 1
    assert role["capacidades"] == ["INVENTARIO_VER"]
    assert role["capacidad_codigos"] == ["INVENTARIO_VER"]
    assert role["workspace_start_feature"] == "warehouse.kardex"
    assert [
        item["feature_key"] for item in role["workspace_preferencias"]
    ] == ["warehouse.kardex", "retired.future.feature"]

    updated = client.put(
        f"/api/catalogo/roles-operativos/{role['id']}",
        headers=_headers(actor_id),
        json={
            "nombre": "Auditor de inventario y kardex",
            "capacidad_codigos": ["OP_VER"],
            "workspace_focus": "Conciliar inventario y movimientos.",
            "workspace_start_feature": "warehouse.kardex",
            "workspace_preferencias": [{
                "feature_key": "warehouse.kardex",
                "prioridad": 5,
                "fijada": True,
            }],
            "expected_version": 1,
        },
    )

    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["version"] == 2
    assert updated.get_json()["capacidad_codigos"] == ["OP_VER"]
    assert updated.get_json()["workspace_preferencias"] == [{
        "feature_key": "warehouse.kardex",
        "prioridad": 5,
        "fijada": True,
    }]

    stale = client.put(
        f"/api/catalogo/roles-operativos/{role['id']}",
        headers=_headers(actor_id),
        json={
            "nombre": "Escritura obsoleta",
            "expected_version": 1,
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "VERSION_CONFLICT"

    with app.app_context():
        events = ScmEvento.query.filter_by(
            aggregate_type="ROL_OPERATIVO",
            aggregate_id=str(role["id"]),
        ).order_by(ScmEvento.id).all()
        assert [event.tipo for event in events] == [
            "ROL_OPERATIVO_CREADO",
            "ROL_OPERATIVO_ACTUALIZADO",
        ]
        assert events[-1].before_json["version"] == 1
        assert events[-1].after_json["version"] == 2


def test_role_rejects_missing_or_inactive_capabilities(app, client):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id
        _grant_admin(actor)
        db.session.add(ScmCapacidad(
            codigo="INACTIVA",
            nombre="Capacidad inactiva",
            activo=False,
        ))
        db.session.commit()

    response = client.post(
        "/api/catalogo/roles-operativos",
        headers=_headers(actor_id),
        json={
            "codigo": "ROL_INVALIDO",
            "nombre": "Rol inválido",
            "capacidad_codigos": ["NO_EXISTE", "INACTIVA"],
            "workspace_preferencias": [],
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "INVALID_CAPABILITY"
    assert response.get_json()["error"]["details"]["codes"] == [
        "INACTIVA",
        "NO_EXISTE",
    ]


def test_role_governance_keeps_assigned_inactive_capability_visible(app):
    with app.app_context():
        capability = ScmCapacidad(
            codigo="SE_DESACTIVA",
            nombre="Capacidad a desactivar",
        )
        role = RolOperativo(
            codigo="VISIBLE_INACTIVA",
            nombre="Gobernanza visible",
            capacidades=[capability],
        )
        db.session.add(role)
        db.session.commit()
        capability.activo = False
        db.session.commit()

        payload = role.to_dict()
        assert payload["capacidades"] == []
        assert payload["capacidad_codigos"] == ["SE_DESACTIVA"]


def test_primary_role_must_be_active_and_assigned_then_auth_me_exposes_it(
    app,
    client,
):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id
        _grant_admin(actor)
        role = RolOperativo(
            codigo="AUDITOR_INV",
            nombre="Auditor inventario",
            workspace_focus="Revisar inventario.",
            workspace_start_feature="warehouse.kardex",
        )
        actor.roles.append(role)
        db.session.add(role)
        db.session.commit()
        role_id = role.id
        original_role_id = next(
            item.id for item in actor.roles if item.id != role_id
        )

    selected = client.patch(
        f"/api/catalogo/trabajadores/{actor_id}/rol-principal",
        headers=_headers(actor_id),
        json={"rol_operativo_id": role_id},
    )

    assert selected.status_code == 200, selected.get_json()
    assert selected.get_json()["rol_principal"]["id"] == role_id
    assert selected.get_json()["rol_principal_pendiente"] is False

    identity = client.get(
        "/api/auth/me",
        headers=_headers(actor_id),
    )
    assert identity.status_code == 200
    assert identity.get_json()["rol_principal"] == {
        "id": role_id,
        "codigo": "AUDITOR_INV",
        "nombre": "Auditor inventario",
        "activo": True,
        "workspace_focus": "Revisar inventario.",
        "workspace_start_feature": "warehouse.kardex",
        "workspace_preferencias": [],
    }
    assert identity.get_json()["rol_principal_pendiente"] is False
    assert identity.headers["Cache-Control"] == "private, no-store"

    deactivated = client.put(
        f"/api/catalogo/roles-operativos/{role_id}",
        headers=_headers(actor_id),
        json={"activo": False, "expected_version": 1},
    )
    assert deactivated.status_code == 200
    inactive_identity = client.get(
        "/api/auth/me",
        headers=_headers(actor_id),
    ).get_json()
    assert inactive_identity["rol_principal"] is None
    assert inactive_identity["rol_principal_pendiente"] is True

    removed = client.put(
        f"/api/catalogo/trabajadores/{actor_id}",
        headers=_headers(actor_id),
        json={"roles_ids": [original_role_id]},
    )
    assert removed.status_code == 200
    assert removed.get_json()["rol_principal"] is None
    assert removed.get_json()["rol_principal_pendiente"] is True

    with app.app_context():
        events = ScmEvento.query.filter_by(
            aggregate_type="TRABAJADOR_ROL_PRINCIPAL",
            aggregate_id=str(actor_id),
        ).all()
        assert [event.tipo for event in events] == [
            "ROL_PRINCIPAL_DEFINIDO",
            "ROL_PRINCIPAL_LIMPIADO",
        ]
        assert events[0].after_json["rol_operativo_id"] == role_id
        assert events[1].after_json["rol_operativo_id"] is None


def test_primary_role_rejects_role_not_assigned_to_worker(app, client):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id
        _grant_admin(actor)
        foreign_role = RolOperativo(
            codigo="NO_ASIGNADO",
            nombre="No asignado",
        )
        db.session.add(foreign_role)
        db.session.commit()
        foreign_role_id = foreign_role.id

    response = client.patch(
        f"/api/catalogo/trabajadores/{actor_id}/rol-principal",
        headers=_headers(actor_id),
        json={"rol_operativo_id": foreign_role_id},
    )

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "PRIMARY_ROLE_NOT_ASSIGNED"


def test_primary_role_rejects_inactive_assigned_role(app, client):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id
        _grant_admin(actor)
        inactive = RolOperativo(
            codigo="INACTIVO",
            nombre="Rol inactivo",
            activo=False,
        )
        actor.roles.append(inactive)
        db.session.add(inactive)
        db.session.commit()
        inactive_id = inactive.id

    response = client.patch(
        f"/api/catalogo/trabajadores/{actor_id}/rol-principal",
        headers=_headers(actor_id),
        json={"rol_operativo_id": inactive_id},
    )

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "PRIMARY_ROLE_INACTIVE"


def test_role_rejects_duplicate_preferences_and_immutable_code(app, client):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor_id = actor.id
        _grant_admin(actor)

    duplicate = client.post(
        "/api/catalogo/roles-operativos",
        headers=_headers(actor_id),
        json={
            "codigo": "DUPLICADO",
            "nombre": "Duplicado",
            "capacidad_codigos": [],
            "workspace_preferencias": [
                {"feature_key": "warehouse.kardex", "prioridad": 1},
                {"feature_key": "warehouse.kardex", "prioridad": 2},
            ],
        },
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["error"]["code"] == (
        "DUPLICATE_WORKSPACE_PREFERENCE"
    )

    created = client.post(
        "/api/catalogo/roles-operativos",
        headers=_headers(actor_id),
        json={
            "codigo": "ESTABLE",
            "nombre": "Codigo estable",
            "capacidad_codigos": [],
            "workspace_preferencias": [],
        },
    ).get_json()
    immutable = client.put(
        f"/api/catalogo/roles-operativos/{created['id']}",
        headers=_headers(actor_id),
        json={
            "codigo": "CAMBIADO",
            "nombre": "Codigo cambiado",
            "expected_version": 1,
        },
    )
    assert immutable.status_code == 422
    assert immutable.get_json()["error"]["code"] == "IMMUTABLE_ROLE_CODE"
