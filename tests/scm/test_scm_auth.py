from uuid import uuid4

import jwt
from sqlalchemy import event

from app.extensions import db
from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import ScmCapacidad
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_service_support import load_actor


class FakeVerifier:
    def __init__(self, claims=None, error=None):
        self.claims = claims or {}
        self.error = error

    def verify(self, _token):
        if self.error:
            raise self.error
        return self.claims


def enable_auth(app, verifier):
    app.config["SCM_AUTH_MODE"] = "supabase"
    app.extensions["scm_token_verifier"] = verifier


def test_auth_requiere_bearer_token(app, client):
    enable_auth(app, FakeVerifier())

    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "AUTH_REQUIRED"


def test_auth_rechaza_token_invalido(app, client):
    enable_auth(app, FakeVerifier(error=jwt.InvalidTokenError()))

    response = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer invalido"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "AUTH_TOKEN_INVALID"


def test_auth_exige_vinculo_con_trabajador(app, client):
    enable_auth(app, FakeVerifier({"sub": str(uuid4()), "email": "uat@example.com"}))

    response = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer valido"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "AUTH_WORKER_NOT_LINKED"


def test_auth_deriva_actor_del_token_e_ignora_actor_header(app, client):
    auth_user_id = uuid4()
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.auth_user_id = auth_user_id
        actor_id = actor.id
        db.session.commit()

    enable_auth(app, FakeVerifier({
        "sub": str(auth_user_id),
        "email": "juan@example.com",
    }))

    response = client.get(
        "/api/auth/me",
        headers={
            "Authorization": "Bearer valido",
            "X-Actor-Id": "999999",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["id"] == actor_id
    assert response.get_json()["correo"] == "juan@example.com"
    assert response.headers["Cache-Control"] == "private, no-store"


def test_auth_no_hidrata_roles_en_lectura_que_no_los_necesita(app, client):
    auth_user_id = uuid4()
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.auth_user_id = auth_user_id
        db.session.commit()
        engine = db.engine

    enable_auth(app, FakeVerifier({"sub": str(auth_user_id)}))
    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        response = client.get(
            "/api/catalogo/maquinas",
            headers={"Authorization": "Bearer valido"},
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    authorization_relationship_queries = [
        statement
        for statement in statements
        if "scm_rol_capacidad" in statement
        or (
            "trabajador_rol" in statement
            and "join rol_operativo" in statement
        )
    ]
    assert authorization_relationship_queries == []


def test_capability_check_queries_existence_without_catalog_hydration(app):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        capability = ScmCapacidad(
            codigo="CAPABILITY_EGRESS_TEST",
            nombre="Capacidad de prueba egress",
        )
        actor.roles[0].capacidades.append(capability)
        db.session.commit()
        actor_id = actor.id
        db.session.expire_all()
        engine = db.engine
        statements = []

        def record_statement(
            _conn, _cursor, statement, _parameters, _context, _many
        ):
            statements.append(statement.lower())

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            loaded = load_actor(
                db.session,
                actor_id,
                capability="CAPABILITY_EGRESS_TEST",
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

        assert loaded.id == actor_id
        capability_queries = [
            statement
            for statement in statements
            if "scm_rol_capacidad" in statement
        ]
        assert len(capability_queries) == 1
        assert "scm_capacidad.nombre" not in capability_queries[0]
        assert "scm_capacidad.descripcion" not in capability_queries[0]


def test_auth_bloquea_inmediatamente_trabajador_inactivo(app, client):
    auth_user_id = uuid4()
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.auth_user_id = auth_user_id
        actor.activo = False
        db.session.commit()

    enable_auth(app, FakeVerifier({"sub": str(auth_user_id)}))

    response = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer valido"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "AUTH_WORKER_INACTIVE"


def test_gestor_maestros_no_puede_escribir_operacion_productiva(app, client):
    auth_user_id = uuid4()
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.auth_user_id = auth_user_id
        actor.roles.append(RolOperativo(
            codigo="GESTOR_MAESTROS",
            nombre="Gestor de datos maestros",
        ))
        db.session.commit()

    enable_auth(app, FakeVerifier({"sub": str(auth_user_id)}))

    response = client.post(
        "/api/ordenes",
        headers={"Authorization": "Bearer valido"},
        json={},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "MASTER_STEWARD_SCOPE"


def test_salud_e_imagenes_no_exigen_sesion(app, client):
    enable_auth(app, FakeVerifier(error=jwt.InvalidTokenError()))

    health = client.get("/api/health")
    missing_image = client.get("/api/productos/NO-EXISTE/imagen")

    assert health.status_code == 200
    assert missing_image.status_code == 404
    assert missing_image.get_json().get("error") != {
        "code": "AUTH_REQUIRED",
        "message": "Inicia sesión para continuar.",
    }


def test_supabase_non_admin_is_denied_on_authorization_catalog_prefixes(
    app,
    client,
):
    auth_user_id = uuid4()
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.auth_user_id = auth_user_id
        role_id = actor.roles[0].id
        db.session.commit()

    enable_auth(app, FakeVerifier({"sub": str(auth_user_id)}))
    headers = {"Authorization": "Bearer valido"}

    responses = (
        client.get("/api/catalogo/capacidades", headers=headers),
        client.get("/api/catalogo/roles-operativos", headers=headers),
        client.put(
            f"/api/catalogo/roles-operativos/{role_id}",
            headers=headers,
            json={"nombre": "No autorizado", "expected_version": 1},
        ),
    )
    for response in responses:
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == (
            "CAPABILITY_REQUIRED"
        )


def test_supabase_primary_role_ignores_spoofed_actor_header(app, client):
    auth_user_id = uuid4()
    with app.app_context():
        admin = Trabajador.query.filter_by(codigo="TRB-01").one()
        admin.auth_user_id = auth_user_id
        capability = ScmCapacidad(
            codigo="AUTORIZACION_SCM_ADMINISTRAR",
            nombre="Administrar autorizaciones SCM",
        )
        admin.roles[0].capacidades.append(capability)
        target_role = RolOperativo(
            codigo="TARGET_ROLE",
            nombre="Rol objetivo",
        )
        target = Trabajador(
            codigo="TRB-TARGET",
            nombres="Target",
            apellidos="Worker",
            roles=[target_role],
        )
        db.session.add_all([capability, target])
        db.session.commit()
        admin_id = admin.id
        target_id = target.id
        target_role_id = target_role.id

    enable_auth(app, FakeVerifier({"sub": str(auth_user_id)}))
    response = client.patch(
        f"/api/catalogo/trabajadores/{target_id}/rol-principal",
        headers={
            "Authorization": "Bearer valido",
            "X-Actor-Id": str(target_id),
        },
        json={"rol_operativo_id": target_role_id},
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["rol_principal"]["id"] == target_role_id
    with app.app_context():
        event = ScmEvento.query.filter_by(
            aggregate_type="TRABAJADOR_ROL_PRINCIPAL",
            aggregate_id=str(target_id),
        ).one()
        assert event.actor_id == admin_id
        assert event.actor_id != target_id
