from decimal import Decimal
from urllib.parse import urlencode

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models.producto import PiezaColor, ProductoTerminado
from app.models.scm_articulos import (
    CLASE_PIEZA_COLOR,
    CLASE_PRODUCTO_TERMINADO,
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
    ScmDefinicionWip,
)
from app.models.scm_empaque import (
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_estructuras import (
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_catalogos import ScmCapacidad
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_configuration import ensure_initial_scm_configuration


def _actor():
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
        codigo=f"B1H_{capability}",
        nombre=f"Solo {capability}",
    )
    capacity = next(
        capacity
        for capacity in ScmCapacidad.query.all()
        if capacity.codigo == capability
    )
    role.capacidades.append(capacity)
    actor = Trabajador(
        codigo=f"TRB-B1H-{capability}",
        nombres="B1H",
        apellidos="Parcial",
        activo=True,
        roles=[role],
    )
    db.session.add(actor)
    db.session.commit()
    return actor


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


def _seed_catalog(actor_id):
    pieces = [
        PiezaColor(
            sku=f"PC-B1H-{index:03d}",
            linea_id=1,
            familia_id=1,
            piezas=("Árbol 100%_A" if index == 0 else f"Pieza Color {index}"),
        )
        for index in range(30)
    ]
    products = [
        ProductoTerminado(
            cod_sku_pt=f"PT-B1H-{index:03d}",
            linea_id=1,
            familia_id=1,
            producto=f"Producto terminado {index}",
            um="UN",
        )
        for index in range(30)
    ]
    wips = [
        ScmArticulo(
            codigo=f"WIP-B1H-{index:03d}",
            nombre=("Balde Azul" if index == 0 else f"WIP Balde {index}"),
            clase=CLASE_SUBENSAMBLE_WIP,
            definicion_wip=ScmDefinicionWip(
                descripcion=f"Descripcion WIP {index}",
                requiere_calidad=index % 2 == 0,
            ),
        )
        for index in range(30)
    ]
    db.session.add_all([*pieces, *products])
    db.session.flush()
    articles = db.session.query(ScmArticulo).filter(
        ScmArticulo.codigo.in_([piece.sku for piece in pieces] + [product.cod_sku_pt for product in products])
    ).all()
    db.session.add_all(wips)
    db.session.flush()
    articles.extend(wips)
    articles_by_code = {article.codigo: article for article in articles}
    articles_by_code["PC-B1H-000"].activo = False
    db.session.commit()
    return articles_by_code


def _seed_structures(actor_id, articles_by_code):
    result = articles_by_code["WIP-B1H-000"]
    component = articles_by_code["PC-B1H-001"]
    revisions = []
    for index in range(30):
        article = articles_by_code[f"WIP-B1H-{index:03d}"]
        revisions.append(
            ScmEstructuraRevision(
                articulo_resultado_id=article.id,
                numero_revision=1,
                estado="APROBADA" if index < 25 else "BORRADOR",
                creada_por_id=actor_id,
            )
        )
    db.session.add_all(revisions)
    db.session.flush()
    for revision in revisions:
        revision.componentes.append(ScmEstructuraComponente(
            secuencia=1,
            articulo_componente_id=component.id,
            cantidad=Decimal("1"),
            unidad="UN",
        ))
    historical = ScmEstructuraRevision(
        articulo_resultado_id=result.id,
        numero_revision=2,
        estado="RETIRADA",
        creada_por_id=actor_id,
    )
    historical.componentes.append(ScmEstructuraComponente(
        secuencia=1,
        articulo_componente_id=component.id,
        cantidad=Decimal("2"),
        unidad="UN",
    ))
    db.session.add(historical)
    db.session.commit()
    return revisions, historical


def _seed_rules(actor_id):
    profiles = [
        ScmPerfilEmpacable(
            codigo=f"PERF-B1H-{index:03d}",
            nombre=f"Perfil B1H {index}",
            descripcion_fisica=("Balde seguro" if index == 0 else "Perfil prueba"),
        )
        for index in range(30)
    ]
    containers = [
        ScmTipoContenedor(
            codigo=f"CONT-B1H-{index:03d}",
            clase="MANGA",
            nombre=f"Contenedor B1H {index}",
            material="PE",
            dimensiones_json={"ancho_mm": "500"},
            tara_nominal_g=100,
            tolerancia_tara_g=10,
            peso_bruto_max_kg=10,
        )
        for index in range(30)
    ]
    db.session.add_all([*profiles, *containers])
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
    revisions = []
    for index, rule in enumerate(rules):
        revisions.append(ScmReglaEmpaqueRevision(
            regla_id=rule.id,
            numero_revision=1,
            estado="APROBADA" if index % 3 == 0 else "BORRADOR",
            medicion_fisica_probada=True,
            cantidad_objetivo_un=8,
            cantidad_maxima_probada_un=10,
            peso_neto_operativo_max_kg="9",
            margen_seguridad_kg="0.5",
            tolerancia_peso_abs_g="50",
            tolerancia_peso_pct="2.5",
            creada_por_id=actor_id,
        ))
    db.session.add_all(revisions)
    db.session.flush()
    historical = ScmReglaEmpaqueRevision(
        regla_id=rules[0].id,
        numero_revision=2,
        estado="RETIRADA",
        medicion_fisica_probada=True,
        cantidad_objetivo_un=8,
        cantidad_maxima_probada_un=10,
        peso_neto_operativo_max_kg="9",
        margen_seguridad_kg="0.5",
        tolerancia_peso_abs_g="50",
        tolerancia_peso_pct="2.5",
        creada_por_id=actor_id,
    )
    db.session.add(historical)
    db.session.commit()
    return profiles, containers, revisions, historical


def test_b1h_resolver_returns_inactive_and_missing_without_page_filter(app, client):
    with app.app_context():
        actor = _actor()
        articles = _seed_catalog(actor.id)
        ids = [articles["PC-B1H-000"].id, articles["WIP-B1H-000"].id, 999999]
        actor_id = actor.id
    response = _get(
        client,
        actor_id,
        "/api/scm/v1/articulos/resolver?ids=" + ",".join(map(str, ids)),
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert [item["id"] for item in payload["items"]] == sorted(ids[:2])
    assert payload["items"][0]["activo"] is False
    assert payload["missing_ids"] == [999999]
    assert {"wip", "subtipo"}.issubset(payload["items"][-1])


def test_b1h_structure_hydration_returns_current_and_exact_history(app, client):
    with app.app_context():
        actor = _actor()
        articles = _seed_catalog(actor.id)
        revisions, historical = _seed_structures(actor.id, articles)
        actor_id = actor.id
        article_id = articles["WIP-B1H-000"].id
        historical_id = historical.id
    response = _get(
        client,
        actor_id,
        f"/api/scm/v1/estructuras/hidratacion?articulo_ids={article_id},999999&revision_ids={historical_id},999999",
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert [item["estado"] for item in payload["approved"]] == ["APROBADA"]
    assert payload["approved"][0]["componentes"][0]["articulo"]["codigo"] == "PC-B1H-001"
    assert [item["id"] for item in payload["referenced"]] == [historical.id]
    assert payload["referenced"][0]["estado"] == "RETIRADA"
    assert payload["missing_article_ids"] == [999999]
    assert payload["missing_revision_ids"] == [999999]


def test_b1h_structure_hydration_keeps_sql_budget_for_many_ids(app, client):
    with app.app_context():
        actor = _actor()
        articles = _seed_catalog(actor.id)
        _seed_structures(actor.id, articles)
        article_ids = [
            articles[f"WIP-B1H-{index:03d}"].id
            for index in range(25)
        ]
        actor_id = actor.id
    one, one_count = _counted_get(
        client,
        actor_id,
        f"/api/scm/v1/estructuras/hidratacion?articulo_ids={article_ids[0]}",
    )
    many, many_count = _counted_get(
        client,
        actor_id,
        "/api/scm/v1/estructuras/hidratacion?articulo_ids="
        + ",".join(map(str, article_ids)),
    )
    assert one.status_code == many.status_code == 200
    assert many_count <= one_count + 3
    assert len(many.get_json()["approved"]) == 25


def test_c1_article_page_filters_aliases_terms_and_literal_search(app, client):
    with app.app_context():
        actor = _actor()
        _seed_catalog(actor.id)
        actor_id = actor.id
    response = _get(
        client,
        actor_id,
        "/api/scm/v1/articulos/pagina?q=wip%20balde&clases=SUBENSAMBLE_WIP&activo=all&limite=100",
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["total"] == 30
    assert all(item["clase"] == "SUBENSAMBLE_WIP" for item in payload["items"])
    for alias, expected_class in (
        ("piezacolor", "PIEZA_COLOR"),
        ("subensamblewip", "SUBENSAMBLE_WIP"),
    ):
        alias_page = _get(
            client, actor_id,
            f"/api/scm/v1/articulos/pagina?q={alias}&limite=100",
        )
        assert alias_page.status_code == 200, alias_page.get_json()
        assert alias_page.get_json()["total"] == 30
        assert all(
            item["clase"] == expected_class
            for item in alias_page.get_json()["items"]
        )
    literal = _get(
        client,
        actor_id,
        "/api/scm/v1/articulos/pagina?q=arbol%20100%25_A&limite=100",
    )
    assert literal.status_code == 200
    assert literal.get_json()["total"] == 1
    inactive = _get(
        client,
        actor_id,
        "/api/scm/v1/articulos/pagina?clases=PIEZA_COLOR&activo=false&limite=100",
    )
    assert inactive.status_code == 200
    assert inactive.get_json()["total"] == 1


def test_c1_article_page_cursor_and_sql_budget(app, client):
    with app.app_context():
        actor = _actor()
        _seed_catalog(actor.id)
        actor_id = actor.id
    first, first_count = _counted_get(
        client,
        actor_id,
        "/api/scm/v1/articulos/pagina?limite=1",
    )
    many, many_count = _counted_get(
        client,
        actor_id,
        "/api/scm/v1/articulos/pagina?limite=100",
    )
    assert first.status_code == many.status_code == 200
    assert many_count <= first_count + 3
    assert len(many.get_json()["items"]) == 90
    cursor = first.get_json()["next_cursor"]
    second = _get(
        client,
        actor_id,
        f"/api/scm/v1/articulos/pagina?limite=1&cursor={cursor}",
    )
    assert second.status_code == 200
    assert second.get_json()["total"] == 90
    mismatch = _get(
        client,
        actor_id,
        f"/api/scm/v1/articulos/pagina?limite=2&cursor={cursor}",
    )
    assert mismatch.status_code == 400


def test_c2_packaging_page_current_history_filters_and_cursor(app, client):
    with app.app_context():
        actor = _actor()
        profiles, containers, _revisions, historical = _seed_rules(actor.id)
        actor_id = actor.id
        historical_id = historical.id
        profile_id = profiles[0].id
        container_id = containers[0].id
    current = _get(
        client,
        actor_id,
        "/api/scm/v1/reglas-empaque/pagina?vista=actual&limite=100",
    )
    assert current.status_code == 200, current.get_json()
    current_payload = current.get_json()
    assert current_payload["total"] == 30
    assert all(item["estado"] in {"BORRADOR", "APROBADA"} for item in current_payload["items"])
    history = _get(
        client,
        actor_id,
        "/api/scm/v1/reglas-empaque/pagina?vista=historial&limite=100",
    )
    assert history.status_code == 200
    assert [item["revision_id"] for item in history.get_json()["items"]] == [historical_id]
    filtered = _get(
        client,
        actor_id,
        f"/api/scm/v1/reglas-empaque/pagina?perfil_empacable_id={profile_id}&tipo_contenedor_id={container_id}",
    )
    assert filtered.status_code == 200
    assert filtered.get_json()["total"] == 1
    searched = _get(
        client,
        actor_id,
        "/api/scm/v1/reglas-empaque/pagina?q=balde%20seguro&limite=100",
    )
    assert searched.status_code == 200
    assert searched.get_json()["total"] == 1


def test_c2_packaging_page_keeps_sql_budget_for_many_rows(app, client):
    with app.app_context():
        actor = _actor()
        _seed_rules(actor.id)
        actor_id = actor.id
    one, one_count = _counted_get(
        client,
        actor_id,
        "/api/scm/v1/reglas-empaque/pagina?limite=1",
    )
    many, many_count = _counted_get(
        client,
        actor_id,
        "/api/scm/v1/reglas-empaque/pagina?limite=100",
    )
    assert one.status_code == many.status_code == 200
    assert many_count <= one_count + 1
    assert len(many.get_json()["items"]) == 30


@pytest.mark.parametrize(
    "path",
    [
        "/api/scm/v1/articulos/resolver?ids=",
        "/api/scm/v1/articulos/resolver?ids=1," + ",".join(str(i) for i in range(2, 103)),
        "/api/scm/v1/estructuras/hidratacion",
        "/api/scm/v1/reglas-empaque/pagina?vista=bad",
        "/api/scm/v1/articulos/pagina?limite=101",
        "/api/scm/v1/articulos/pagina?cursor=!!!!",
        "/api/scm/v1/reglas-empaque/pagina?cursor=!!!!",
    ],
)
def test_new_readers_reject_invalid_parameters(app, client, path):
    with app.app_context():
        actor = _actor()
        actor_id = actor.id
    response = _get(client, actor_id, path)
    assert response.status_code == 400, response.get_json()


def test_new_readers_keep_capabilities_and_cursor_revocation(app, client):
    with app.app_context():
        actor = _actor()
        articles = _seed_catalog(actor.id)
        actor_id = actor.id
        partial = _actor_with_only("ARTICULO_VER")
        partial_id = partial.id
        article_id = articles["WIP-B1H-000"].id
    response = _get(
        client,
        partial_id,
        "/api/scm/v1/articulos/pagina",
    )
    assert response.status_code == 200
    resolver = _get(
        client,
        partial_id,
        f"/api/scm/v1/articulos/resolver?ids={article_id}",
    )
    assert resolver.status_code == 200
    hydration = _get(
        client,
        partial_id,
        f"/api/scm/v1/estructuras/hidratacion?articulo_ids={article_id}",
    )
    assert hydration.status_code == 403
    packaging = _get(
        client,
        partial_id,
        "/api/scm/v1/reglas-empaque/pagina",
    )
    assert packaging.status_code == 403


@pytest.mark.parametrize("endpoint,code", [
    ("articulos/pagina", "INVALID_ARTICLE_PAGE"),
    ("reglas-empaque/pagina", "INVALID_PACKAGING_RULE_PAGE"),
])
@pytest.mark.parametrize("limit", ["0", "101", "bad"])
def test_page_limit_errors_identify_the_requested_reader(app, client, endpoint, code, limit):
    with app.app_context():
        actor_id = _actor().id
    response = _get(client, actor_id, f"/api/scm/v1/{endpoint}?limite={limit}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == code


def test_packaging_keyset_exhausts_multiple_revisions_in_stable_order(app, client):
    with app.app_context():
        actor_id = _actor().id
        _, _, revisions, _ = _seed_rules(actor_id)
        for revision in revisions[:3]:
            for number in range(3, 15):
                db.session.add(ScmReglaEmpaqueRevision(
                    regla_id=revision.regla_id, numero_revision=number,
                    estado="BORRADOR", creada_por_id=actor_id,
                    cantidad_objetivo_un=8, cantidad_maxima_probada_un=10,
                    peso_neto_operativo_max_kg=9, margen_seguridad_kg=0.5,
                    tolerancia_peso_abs_g=50, tolerancia_peso_pct=2.5,
                ))
        db.session.commit()
        expected = [r.id for r in db.session.query(ScmReglaEmpaqueRevision)
            .join(ScmReglaEmpaque).join(ScmPerfilEmpacable).join(
                ScmTipoContenedor, ScmTipoContenedor.id == ScmReglaEmpaque.tipo_contenedor_id)
            .filter(ScmReglaEmpaqueRevision.estado.in_(["APROBADA", "BORRADOR"]))
            .order_by(ScmPerfilEmpacable.codigo, ScmTipoContenedor.codigo,
                ScmReglaEmpaqueRevision.numero_revision.desc(), ScmReglaEmpaqueRevision.id.desc()).all()]
    seen, cursor = [], None
    for _ in range(20):
        params = {"limite": 5}
        if cursor:
            params["cursor"] = cursor
        response = _get(client, actor_id, "/api/scm/v1/reglas-empaque/pagina?" + urlencode(params))
        assert response.status_code == 200, response.get_json()
        page = response.get_json()
        assert page["total"] == len(expected)
        seen.extend(item["revision_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            assert cursor is None
            break
    assert seen == expected
    assert len(seen) == len(set(seen)) == 66


@pytest.mark.parametrize("endpoint,capability,changes", [
    ("articulos/pagina", "ARTICULO_VER", [{"q": "balde"}, {"clases": "SUBENSAMBLE_WIP"}, {"activo": "true"}, {"limite": 2}]),
    ("reglas-empaque/pagina", "EMPAQUE_VER", [{"vista": "historial"}, {"q": "balde"}, {"perfil_empacable_id": 1}, {"tipo_contenedor_id": 1}, {"limite": 2}]),
])
def test_page_cursor_rejects_filter_actor_changes_and_revoked_read(app, client, endpoint, capability, changes):
    with app.app_context():
        actor_id = _actor().id
        _seed_catalog(actor_id)
        _seed_rules(actor_id)
        partial_id = _actor_with_only(capability).id
    base = "/api/scm/v1/" + endpoint
    response = _get(client, partial_id, base + "?limite=1")
    assert response.status_code == 200
    cursor = response.get_json()["next_cursor"]
    assert cursor
    params = {"limite": 1, "cursor": cursor}
    for change in changes:
        assert _get(client, partial_id, base + "?" + urlencode({**params, **change})).status_code == 400
    assert _get(client, actor_id, base + "?" + urlencode(params)).status_code == 400
    with app.app_context():
        partial = db.session.get(Trabajador, partial_id)
        for role in partial.roles:
            role.capacidades = [item for item in role.capacidades if item.codigo != capability]
        db.session.commit()
    assert _get(client, partial_id, base + "?" + urlencode(params)).status_code == 403
