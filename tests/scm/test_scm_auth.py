from uuid import uuid4

import jwt

from app.extensions import db
from app.models.trabajador import RolOperativo, Trabajador


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
