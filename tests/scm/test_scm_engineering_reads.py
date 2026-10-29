import base64
import json

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_empaque import (
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_estructuras import ScmEstructuraRevision
from app.models.scm_rutas import ScmRutaRevision
from app.models.scm_catalogos import ScmCapacidad
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_configuration import ensure_initial_scm_configuration


def _authorized_actor():
    ensure_initial_scm_configuration()
    actor = Trabajador.query.filter_by(codigo="TRB-01").one()
    role = RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
    if role not in actor.roles:
        actor.roles.append(role)
    db.session.commit()
    return actor


def _actor_with_only(capability):
    ensure_initial_scm_configuration()
    role = RolOperativo(
        codigo=f"B1_{capability[:12]}",
        nombre=f"B1 {capability}",
    )
    capacity = ScmCapacidad.query.filter_by(codigo=capability).one()
    role.capacidades.append(capacity)
    actor = Trabajador(
        codigo=f"TRB-B1-{capability[:8]}",
        nombres="B1",
        apellidos="Lectura",
        activo=True,
        roles=[role],
    )
    db.session.add(actor)
    db.session.commit()
    return actor


def _another_authorized_actor():
    role = RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
    actor = Trabajador(
        codigo="TRB-B1-OTHER",
        nombres="B1",
        apellidos="Otro",
        activo=True,
        roles=[role],
    )
    db.session.add(actor)
    db.session.commit()
    return actor


def _seed_approval_rows(actor_id):
    articles = [
        ScmArticulo(
            codigo=f"B1-ART-{index:03d}",
            nombre=(
                "Árbol 100%_A"
                if index == 0
                else "Arbol 100XXA"
                if index == 1
                else "Arbol 100\\A"
                if index == 2
                else f"Artículo B1 {index}"
            ),
            clase="SUBENSAMBLE_WIP",
        )
        for index in range(30)
    ]
    db.session.add_all(articles)
    db.session.flush()

    structures = [
        ScmEstructuraRevision(
            articulo_resultado_id=article.id,
            numero_revision=1,
            estado=("PENDIENTE_APROBACION" if index < 26 else "BORRADOR"),
            creada_por_id=actor_id,
        )
        for index, article in enumerate(articles)
    ]
    structures.append(ScmEstructuraRevision(
        articulo_resultado_id=articles[29].id,
        numero_revision=2,
        estado="APROBADA",
        creada_por_id=actor_id,
    ))
    routes = [
        ScmRutaRevision(
            articulo_objetivo_id=article.id,
            numero_revision=1,
            estado=("BORRADOR" if index < 26 else "APROBADA"),
            creada_por_id=actor_id,
        )
        for index, article in enumerate(articles)
    ]
    profiles = [
        ScmPerfilEmpacable(
            codigo=f"B1-PERF-{index:03d}",
            nombre=f"Perfil B1 {index}",
            descripcion_fisica="Perfil de prueba",
        )
        for index in range(30)
    ]
    containers = [
        ScmTipoContenedor(
            codigo=f"B1-TMG-{index:03d}",
            clase="MANGA",
            nombre=f"Contenedor B1 {index}",
            material="PE",
            dimensiones_json={"ancho_mm": "500"},
            tara_nominal_g=100,
            tolerancia_tara_g=10,
            peso_bruto_max_kg=10,
        )
        for index in range(30)
    ]
    db.session.add_all([*structures, *routes, *profiles, *containers])
    db.session.flush()
    rules = [
        ScmReglaEmpaque(
            perfil_empacable_id=profiles[index].id,
            tipo_contenedor_id=containers[index].id,
        )
        for index in range(30)
    ]
    db.session.add_all(rules)
    db.session.flush()
    revisions = [
        ScmReglaEmpaqueRevision(
            regla_id=rule.id,
            numero_revision=1,
            estado=("BORRADOR" if index < 26 else "APROBADA"),
            medicion_fisica_probada=True,
            cantidad_objetivo_un=8,
            cantidad_maxima_probada_un=10,
            peso_neto_operativo_max_kg="9",
            margen_seguridad_kg="0.5",
            tolerancia_peso_abs_g="50",
            tolerancia_peso_pct="2.5",
            creada_por_id=actor_id,
        )
        for index, rule in enumerate(rules)
    ]
    db.session.add_all(revisions)
    db.session.commit()
    return articles


def _get(client, actor_id, path):
    db.session.remove()
    return client.get(path, headers={"X-Actor-Id": str(actor_id)})


def _counted_get(client, actor_id, path):
    count = 0

    def before(_conn, _cursor, statement, _params, _context, _many):
        nonlocal count
        if statement.lstrip().upper().startswith("SELECT"):
            count += 1

    event.listen(db.engine, "before_cursor_execute", before)
    try:
        response = _get(client, actor_id, path)
    finally:
        event.remove(db.engine, "before_cursor_execute", before)
    return response, count


def _walk_pages(client, actor_id, domain):
    cursor = None
    ids = []
    first = None
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        response = _get(
            client,
            actor_id,
            f"/api/scm/v1/ingenieria/aprobaciones?dominio={domain}&limite=1{suffix}",
        )
        assert response.status_code == 200, response.get_json()
        payload = response.get_json()
        first = payload if first is None else first
        ids.extend(item["revision_id"] for item in payload["items"])
        if not payload["has_more"]:
            assert payload["next_cursor"] is None
            assert len(ids) == payload["total"]
            return first, ids
        cursor = payload["next_cursor"]


def test_global_approval_pages_are_complete_by_domain_and_state(app, client):
    with app.app_context():
        actor = _authorized_actor()
        _seed_approval_rows(actor.id)
        actor_id = actor.id

    for domain, expected_count, expected_state in (
        ("ESTRUCTURA", 26, "PENDIENTE_APROBACION"),
        ("RUTA", 26, "BORRADOR"),
        ("EMPAQUE", 26, "BORRADOR"),
    ):
        first, ids = _walk_pages(client, actor_id, domain)
        assert first["scope"] == {
            "dominio": domain,
            "estado": expected_state,
            "capacidad": f"{domain}_VER",
        }
        assert len(ids) == expected_count
        assert len(ids) == len(set(ids))
        response = _get(
            client,
            actor_id,
            f"/api/scm/v1/ingenieria/aprobaciones?dominio={domain}&limite=100",
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["total"] == expected_count
        assert all(item["estado"] == expected_state for item in payload["items"])
        if domain == "EMPAQUE":
            assert {"perfil", "contenedor"}.issubset(payload["items"][0])
            assert "componentes" not in payload["items"][0]
        else:
            assert "objetivo" in payload["items"][0]
            assert "operaciones" not in payload["items"][0]


def test_approval_search_is_normalized_and_literal(app, client):
    with app.app_context():
        actor = _authorized_actor()
        _seed_approval_rows(actor.id)
        actor_id = actor.id

    response = _get(
        client,
        actor_id,
        "/api/scm/v1/ingenieria/aprobaciones?dominio=ESTRUCTURA&q=arbol%20100%25_A",
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 1
    assert payload["items"][0]["objetivo"]["nombre"] == "Árbol 100%_A"
    backslash = _get(
        client,
        actor_id,
        "/api/scm/v1/ingenieria/aprobaciones?dominio=ESTRUCTURA&q=arbol%20100%5CA",
    )
    assert backslash.status_code == 200
    backslash_payload = backslash.get_json()
    assert backslash_payload["total"] == 1
    assert backslash_payload["items"][0]["objetivo"]["nombre"] == "Arbol 100\\A"
    for query in ("", "   "):
        empty = _get(
            client,
            actor_id,
            f"/api/scm/v1/ingenieria/aprobaciones?dominio=ESTRUCTURA&q={query}",
        )
        unfiltered = _get(
            client,
            actor_id,
            "/api/scm/v1/ingenieria/aprobaciones?dominio=ESTRUCTURA",
        )
        assert empty.status_code == unfiltered.status_code == 200
        assert empty.get_json()["total"] == unfiltered.get_json()["total"]


def test_approval_cursor_is_bound_to_actor_domain_and_filter(app, client):
    with app.app_context():
        actor = _authorized_actor()
        _seed_approval_rows(actor.id)
        actor_id = actor.id
        other = _another_authorized_actor()
        other_id = other.id

    first = _get(
        client,
        actor_id,
        "/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&limite=1",
    ).get_json()
    cursor = first["next_cursor"]
    assert cursor
    mismatch_actor = _get(
        client,
        other_id,
        f"/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&limite=1&cursor={cursor}",
    )
    assert mismatch_actor.status_code == 400
    mismatch_domain = _get(
        client,
        actor_id,
        f"/api/scm/v1/ingenieria/aprobaciones?dominio=EMPAQUE&limite=1&cursor={cursor}",
    )
    assert mismatch_domain.status_code == 400
    mismatch_query = _get(
        client,
        actor_id,
        f"/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&limite=1&q=otro&cursor={cursor}",
    )
    assert mismatch_query.status_code == 400
    mismatch_limit = _get(
        client,
        actor_id,
        f"/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&limite=2&cursor={cursor}",
    )
    assert mismatch_limit.status_code == 400
    empty_query = _get(
        client,
        actor_id,
        f"/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&limite=1&q=&cursor={cursor}",
    )
    assert empty_query.status_code == 200
    malformed = _get(
        client,
        actor_id,
        "/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&cursor=not-base64",
    )
    assert malformed.status_code == 400


@pytest.mark.parametrize("raw_cursor", [[], None, "scalar"])
def test_approval_cursor_rejects_non_object_json(app, client, raw_cursor):
    with app.app_context():
        actor = _authorized_actor()
        _seed_approval_rows(actor.id)
        actor_id = actor.id
    encoded = base64.urlsafe_b64encode(
        json.dumps(raw_cursor).encode()
    ).decode().rstrip("=")
    response = _get(
        client,
        actor_id,
        f"/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&cursor={encoded}",
    )
    assert response.status_code == 400


@pytest.mark.parametrize("cursor", ["!!!!", "a" * 2049])
def test_approval_cursor_rejects_invalid_encoding_or_size(app, client, cursor):
    with app.app_context():
        actor = _authorized_actor()
        _seed_approval_rows(actor.id)
        actor_id = actor.id
    response = _get(
        client,
        actor_id,
        f"/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&cursor={cursor}",
    )
    assert response.status_code == 400


def test_approval_requires_each_domain_capability_and_revocation(app, client):
    with app.app_context():
        actor = _authorized_actor()
        _seed_approval_rows(actor.id)
        route_only = _actor_with_only("RUTA_VER")
        actor_id = actor.id
        route_only_id = route_only.id

    assert _get(
        client,
        route_only_id,
        "/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA",
    ).status_code == 200
    assert _get(
        client,
        route_only_id,
        "/api/scm/v1/ingenieria/aprobaciones?dominio=ESTRUCTURA",
    ).status_code == 403
    assert _get(
        client,
        route_only_id,
        "/api/scm/v1/ingenieria/aprobaciones?dominio=EMPAQUE",
    ).status_code == 403

    first = _get(
        client,
        actor_id,
        "/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&limite=1",
    ).get_json()
    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        for role in actor.roles:
            role.capacidades = [
                capacity
                for capacity in role.capacidades
                if capacity.codigo != "RUTA_VER"
            ]
        db.session.commit()
    revoked = _get(
        client,
        actor_id,
        f"/api/scm/v1/ingenieria/aprobaciones?dominio=RUTA&cursor={first['next_cursor']}",
    )
    assert revoked.status_code == 403


def test_approval_invalid_page_parameters_and_sql_budget(app, client):
    with app.app_context():
        actor = _authorized_actor()
        _seed_approval_rows(actor.id)
        actor_id = actor.id

    for query in (
        "dominio=NOPE",
        "dominio=RUTA&limite=0",
        "dominio=RUTA&limite=101",
        "dominio=RUTA&limite=abc",
    ):
        response = _get(
            client,
            actor_id,
            f"/api/scm/v1/ingenieria/aprobaciones?{query}",
        )
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == (
            "INVALID_ENGINEERING_APPROVAL_PAGE"
        )

    for domain in ("ESTRUCTURA", "RUTA", "EMPAQUE"):
        one, one_count = _counted_get(
            client,
            actor_id,
            f"/api/scm/v1/ingenieria/aprobaciones?dominio={domain}&limite=1",
        )
        many, many_count = _counted_get(
            client,
            actor_id,
            f"/api/scm/v1/ingenieria/aprobaciones?dominio={domain}&limite=100",
        )
        assert one.status_code == many.status_code == 200
        assert many_count <= one_count + 4
        assert len(many.get_json()["items"]) == 26
