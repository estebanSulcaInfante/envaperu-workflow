from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from app.extensions import db
from app.models.scm_catalogos import ScmCapacidad
from app.models.trabajador import RolOperativo, Trabajador


ASSERTION_KEY = "a" * 48
BRIDGE_CREDENTIAL = "b" * 48


class FakeVerifier:
    def __init__(self, claims=None, error=None):
        self.claims = claims or {}
        self.error = error

    def verify(self, _token):
        if self.error:
            raise self.error
        return self.claims


def configure(app, claims, *, enabled=True):
    app.config.update(
        SCM_AUTH_MODE="supabase",
        SCM_PERSONAL_AGENT_ACCESS_ENABLED=enabled,
        SCM_PERSONAL_AGENT_BRIDGE_ORIGIN="https://openclaw.example.test",
        SCM_PERSONAL_AGENT_ASSERTION_SECRET=ASSERTION_KEY,
        SCM_PERSONAL_AGENT_BRIDGE_CREDENTIAL=BRIDGE_CREDENTIAL,
        SCM_PERSONAL_AGENT_MAX_SESSION_SECONDS=3600,
    )
    app.extensions["scm_token_verifier"] = FakeVerifier(claims)


def actor_with_access():
    auth_user_id = uuid4()
    actor = Trabajador.query.filter_by(codigo="TRB-01").one()
    actor.auth_user_id = auth_user_id
    capability = ScmCapacidad(
        codigo="ASISTENTE_PRODUCCION_USAR",
        nombre="Usar agente SCM",
    )
    actor.roles[0].capacidades.append(capability)
    db.session.commit()
    return actor, auth_user_id


def test_access_issues_short_handoff_without_raw_supabase_token(app, client):
    with app.app_context():
        actor, auth_user_id = actor_with_access()
        actor_id = actor.id
        claims = {
            "sub": str(auth_user_id),
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()),
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "aud": "authenticated",
        }
        actor_id = actor.id
    configure(app, claims)

    response = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase-secret"},
    )

    assert response.status_code == 200, response.get_json()
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.get_json()
    assert body["exchange_url"] == "https://openclaw.example.test/_scm/exchange"
    assert body["agent_id"] == f"scm-{auth_user_id.hex}"
    assert "supabase-secret" not in response.get_data(as_text=True)
    assertion = jwt.decode(
        body["assertion"],
        ASSERTION_KEY,
        algorithms=["HS256"],
        audience="scm-agent-bridge",
        issuer="api-scm",
    )
    assert assertion["sub"] == str(auth_user_id)
    assert assertion["actor_id"] == actor_id
    assert assertion["agent_id"] == f"scm-{auth_user_id.hex}"
    assert assertion["exp"] <= claims["exp"]
    assert assertion["handoff_exp"] <= assertion["iat"] + 30


def test_bridge_authorization_uses_verified_actor_display_name_and_safe_fallback(app, client):
    with app.app_context():
        actor, auth_user_id = actor_with_access()
        actor_id = actor.id
        actor.nombre_corto = "  Ana <b>\n Operadora\t"
        claims = {
            "sub": str(auth_user_id),
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()),
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "aud": "authenticated",
            "display_name": "Nombre no confiable de claims",
        }
        db.session.commit()
    configure(app, claims)

    issued = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    ).get_json()["assertion"]
    response = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        json={"assertion": issued, "phase": "exchange"},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    display_name = body["identity"]["display_name"]
    assert display_name == "Ana <b> Operadora"
    assert len(display_name) <= 120
    assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in display_name)
    assert "claims" not in display_name
    assert response.content_type.startswith("application/json")

    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        actor.nombre_corto = "N" * 200
        db.session.commit()
    limited = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        json={"assertion": issued, "phase": "session"},
    )
    assert limited.status_code == 200, limited.get_json()
    assert limited.get_json()["identity"]["display_name"] == "N" * 120

    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        actor.nombre_corto = "   "
        actor.nombres = ""
        actor.apellidos = ""
        db.session.commit()
    fallback = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        json={"assertion": issued, "phase": "session"},
    )
    assert fallback.status_code == 200, fallback.get_json()
    assert fallback.get_json()["identity"]["display_name"] == "Usuario SCM"


def test_internal_authorization_reloads_capability_and_rejects_revocation(
    app, client
):
    with app.app_context():
        actor, auth_user_id = actor_with_access()
        actor_id = actor.id
        exp = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp())
        claims = {
            "sub": str(auth_user_id),
            "exp": exp,
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "aud": "authenticated",
        }
    configure(app, claims)
    issued = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    ).get_json()["assertion"]

    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        actor.roles[0].capacidades[0].activo = False
        db.session.commit()

    response = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        json={"assertion": issued, "phase": "exchange"},
    )
    assert response.status_code == 403


def test_internal_endpoint_is_the_only_auth_bypass(app, client):
    configure(app, {}, enabled=False)
    assert client.get("/api/auth/me").status_code == 401
    response = client.post(
        "/api/scm/v1/agente/autorizacion",
        json={"assertion": "bad", "phase": "exchange"},
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
    )
    assert response.status_code in (403, 503)


def test_canonical_false_flag_wins_over_true_alias(app, client):
    with app.app_context():
        _actor, auth_user_id = actor_with_access()
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            "sub": str(auth_user_id),
            "exp": now + 600,
            "iat": now,
            "aud": "authenticated",
        }
    configure(app, claims)
    app.config["SCM_PERSONAL_AGENT_ACCESS_ENABLED"] = "false"
    app.config["SCM_PERSONAL_AGENT_ENABLED"] = "true"
    response = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "AGENT_ACCESS_DISABLED"


def test_local_actor_mode_cannot_mint_personal_agent_access(app, client):
    with app.app_context():
        _actor, auth_user_id = actor_with_access()
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"sub": str(auth_user_id), "exp": now + 600, "iat": now}
    configure(app, claims)
    app.config["SCM_AUTH_MODE"] = "local_actor"
    response = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    )
    assert response.status_code == 401


def test_access_rejects_client_identity_and_oversized_bridge_payload(app, client):
    with app.app_context():
        _actor, auth_user_id = actor_with_access()
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            "sub": str(auth_user_id),
            "exp": now + 600,
            "iat": now,
            "aud": "authenticated",
        }
    configure(app, claims)
    extra = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
        json={"auth_user_id": str(auth_user_id)},
    )
    assert extra.status_code == 400

    oversized = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        data="x" * (8 * 1024 + 1),
        content_type="application/json",
    )
    assert oversized.status_code == 413


def test_union_of_active_roles_survives_single_role_revocation(app, client):
    with app.app_context():
        actor, auth_user_id = actor_with_access()
        first_role = actor.roles[0]
        second_role = RolOperativo(codigo="AGENTE_SEGUNDO", nombre="Segundo")
        second_role.capacidades.append(first_role.capacidades[0])
        db.session.add(second_role)
        actor.roles.append(second_role)
        actor_id = actor.id
        db.session.commit()
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"sub": str(auth_user_id), "exp": now + 600, "iat": now}
    configure(app, claims)
    issued = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    ).get_json()["assertion"]

    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        actor.roles[0].activo = False
        db.session.commit()
    retained = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        json={"assertion": issued, "phase": "exchange"},
    )
    assert retained.status_code == 200, retained.get_json()

    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        for role in actor.roles:
            role.activo = False
        db.session.commit()
    denied = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        json={"assertion": issued, "phase": "session"},
    )
    assert denied.status_code == 403


def test_inactive_actor_and_unlinked_identity_are_denied(app, client):
    with app.app_context():
        actor, auth_user_id = actor_with_access()
        actor.activo = False
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"sub": str(auth_user_id), "exp": now + 600, "iat": now}
        db.session.commit()
    configure(app, claims)
    inactive = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    )
    assert inactive.status_code == 403

    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.activo = True
        actor.auth_user_id = None
        db.session.commit()
    unlinked = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    )
    assert unlinked.status_code == 403


def test_admin_capability_does_not_bypass_agent_capability(app, client):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        auth_user_id = uuid4()
        actor.auth_user_id = auth_user_id
        admin = ScmCapacidad(
            codigo="AUTORIZACION_SCM_ADMINISTRAR",
            nombre="Administrar autorizaciones SCM",
        )
        actor.roles[0].capacidades.append(admin)
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"sub": str(auth_user_id), "exp": now + 600, "iat": now}
        db.session.commit()
    configure(app, claims)
    response = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    )
    assert response.status_code == 403


def test_assertion_rejects_wrong_signature_audience_expiry_and_session_cap(
    app, client
):
    with app.app_context():
        _actor, auth_user_id = actor_with_access()
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {"sub": str(auth_user_id), "exp": now + 600, "iat": now}
    configure(app, claims)
    issued = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    ).get_json()["assertion"]
    # Change a significant signature byte, not unused trailing base64 bits.
    signing_input, signature = issued.rsplit(".", 1)
    bad_signature = signing_input + "." + ("a" if signature[0] != "a" else "b") + signature[1:]
    for assertion in (bad_signature,):
        response = client.post(
            "/api/scm/v1/agente/autorizacion",
            headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
            json={"assertion": assertion, "phase": "exchange"},
        )
        assert response.status_code == 403

    payload = jwt.decode(issued, ASSERTION_KEY, algorithms=["HS256"], options={"verify_signature": False})
    wrong_aud = dict(payload, aud="wrong")
    wrong_aud_token = jwt.encode(wrong_aud, ASSERTION_KEY, algorithm="HS256")
    response = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        json={"assertion": wrong_aud_token, "phase": "exchange"},
    )
    assert response.status_code == 403

    expired = dict(payload, exp=payload["iat"], handoff_exp=payload["iat"])
    expired_token = jwt.encode(expired, ASSERTION_KEY, algorithm="HS256")
    response = client.post(
        "/api/scm/v1/agente/autorizacion",
        headers={"Authorization": f"Bearer {BRIDGE_CREDENTIAL}"},
        json={"assertion": expired_token, "phase": "session"},
    )
    assert response.status_code == 403

    app.config["SCM_PERSONAL_AGENT_MAX_SESSION_SECONDS"] = 30
    capped = client.post(
        "/api/scm/v1/agente/acceso",
        headers={"Authorization": "Bearer supabase"},
    ).get_json()
    capped_payload = jwt.decode(
        capped["assertion"], ASSERTION_KEY, algorithms=["HS256"], options={"verify_signature": False}
    )
    assert capped_payload["exp"] - capped_payload["iat"] <= 30
