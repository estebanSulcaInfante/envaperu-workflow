from app.extensions import db
from app.models.producto import ColorBase, ColorProduccion, FamiliaColor
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_catalogos import ScmCategoriaRecepcion, ScmMaterial


def _catalog(app, scm_config):
    with app.app_context():
        family = FamiliaColor(nombre="SOLIDO RECETA", codigo=901)
        base = ColorBase(nombre="AMARILLO CAJA ORGANIZADORA")
        db.session.add_all([family, base])
        db.session.flush()
        color = ColorProduccion(
            color_base_id=base.id,
            familia_color_id=family.id,
        )
        category = ScmCategoriaRecepcion.query.first()
        resin = ScmMaterial(
            codigo="MP-RECETA-001",
            nombre="PP VIRGEN RECETA",
            clase="MATERIA_PRIMA",
            categoria_recepcion_id=category.id,
        )
        pigment = ScmMaterial(
            codigo="COL-RECETA-001",
            nombre="AMARILLO RECETA",
            clase="COLORANTE",
            categoria_recepcion_id=category.id,
        )
        db.session.add_all([color, resin, pigment])
        db.session.commit()
        return {
            "color_id": color.id,
            "family_id": family.id,
            "resin_id": resin.id,
            "pigment_id": pigment.id,
        }


def _approved_payload(catalog, *, variant="CAJA ORGANIZADORA", dose=500):
    return {
        "color_produccion_id": catalog["color_id"],
        "nombre_variante": variant,
        "estado": "APROBADA",
        "es_default": True,
        "base_virgen_kg": 25,
        "notas": "Recuperada de una OP controlada",
        "lineas": [
            {
                "material_id": catalog["resin_id"],
                "tipo_componente": "MATERIA_PRIMA",
                "cantidad": 1,
            },
            {
                "material_id": catalog["pigment_id"],
                "tipo_componente": "COLORANTE",
                "cantidad": dose,
                "base_kg": 25,
            },
        ],
    }


def test_color_crud_supports_optional_visual_hex(client, app, scm_config):
    catalog = _catalog(app, scm_config)

    response = client.put(
        f'/api/colores/{catalog["color_id"]}',
        json={
            "version": 1,
            "nombre": "AMARILLO CAJA ORGANIZADORA",
            "familia_color_id": catalog["family_id"],
            "hex_referencia": "#f2c94c",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["hex_referencia"] == "#F2C94C"

    invalid = client.put(
        f'/api/colores/{catalog["color_id"]}',
        json={
            "version": 2,
            "hex_referencia": "amarillo",
        },
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["codigo"] == "HEX_REFERENCIA_INVALIDO"

    deleted = client.delete(
        f'/api/colores/{catalog["color_id"]}?version=2',
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["activo"] is False
    assert all(item["id"] != catalog["color_id"] for item in client.get('/api/colores').get_json())
    all_colors = client.get('/api/colores?include_inactive=true').get_json()
    assert next(item for item in all_colors if item["id"] == catalog["color_id"])["activo"] is False


def test_color_family_full_crud_is_versioned_and_logical(client, app):
    created = client.post('/api/familias-color', json={
        'nombre': 'Pastel prueba',
        'codigo': 9901,
    })
    assert created.status_code == 201
    family = created.get_json()
    assert family == {
        'id': family['id'],
            'nombre': 'PASTEL PRUEBA',
            'codigo': 9901,
            'codigo_display': 'FC-009901',
            'activo': True,
        'version': 1,
    }

    updated = client.put(f"/api/familias-color/{family['id']}", json={
        'version': 1,
        'nombre': 'Pastel especial',
        'codigo': 9901,
    })
    assert updated.status_code == 200
    assert updated.get_json()['nombre'] == 'PASTEL ESPECIAL'
    assert updated.get_json()['version'] == 2

    immutable = client.put(f"/api/familias-color/{family['id']}", json={
        'version': 2,
        'codigo': 9902,
    })
    assert immutable.status_code == 400
    assert immutable.get_json()['codigo'] == 'CODIGO_INMUTABLE'

    conflict = client.put(f"/api/familias-color/{family['id']}", json={
        'version': 1,
        'nombre': 'Nombre obsoleto',
    })
    assert conflict.status_code == 409

    deleted = client.delete(f"/api/familias-color/{family['id']}?version=2")
    assert deleted.status_code == 200
    assert deleted.get_json()['activo'] is False
    assert all(item['id'] != family['id'] for item in client.get('/api/familias-color').get_json())
    all_families = client.get('/api/familias-color?include_inactive=true').get_json()
    assert next(item for item in all_families if item['id'] == family['id'])['activo'] is False


def test_manual_master_recipe_is_crud_versioned_and_prefill_authority(
    client,
    app,
    scm_config,
):
    catalog = _catalog(app, scm_config)
    created = client.post(
        '/api/catalogo/recetas-color',
        json=_approved_payload(catalog),
    )
    assert created.status_code == 201
    recipe = created.get_json()
    assert recipe["revision"] == 1
    assert recipe["estado"] == "APROBADA"
    assert recipe["es_default"] is True
    assert len(recipe["lineas"]) == 2

    prefill = client.get(
        f'/api/catalogo/receta-color?color_produccion_id={catalog["color_id"]}'
        '&kg_virgen_base=70'
    )
    assert prefill.status_code == 200
    prefill_data = prefill.get_json()
    assert prefill_data["fuente"] == "RECETA_MAESTRA"
    assert prefill_data["materias_primas"][0]["fraccion"] == 1.0
    assert prefill_data["pigmentos"][0]["gramos"] == 1400.0

    changed_payload = _approved_payload(catalog, dose=450)
    changed_payload["version"] = recipe["version"]
    changed = client.put(
        f'/api/catalogo/recetas-color/{recipe["id"]}',
        json=changed_payload,
    )
    assert changed.status_code == 200
    revision_two = changed.get_json()
    assert revision_two["id"] != recipe["id"]
    assert revision_two["revision"] == 2
    assert revision_two["reemplaza_receta_id"] == recipe["id"]
    assert revision_two["lineas"][1]["cantidad"] == 450.0

    with app.app_context():
        previous = db.session.get(RecetaColorMaestra, recipe["id"])
        assert previous.estado == "INACTIVA"
        assert previous.es_default is False

    deleted = client.delete(
        f'/api/catalogo/recetas-color/{revision_two["id"]}'
        f'?version={revision_two["version"]}'
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["estado"] == "INACTIVA"


def test_recipe_draft_allows_partial_work_but_approval_requires_full_resin_fraction(
    client,
    app,
    scm_config,
):
    catalog = _catalog(app, scm_config)
    partial = _approved_payload(catalog)
    partial["lineas"][0]["cantidad"] = 0.6

    invalid = client.post('/api/catalogo/recetas-color', json=partial)
    assert invalid.status_code == 400
    assert invalid.get_json()["codigo"] == "FRACCIONES_RECETA_INVALIDAS"

    partial["estado"] = "BORRADOR"
    partial["es_default"] = False
    draft = client.post('/api/catalogo/recetas-color', json=partial)
    assert draft.status_code == 201
    assert draft.get_json()["estado"] == "BORRADOR"


def test_recipe_draft_replaces_existing_lines_without_unique_conflict(
    client,
    app,
    scm_config,
):
    catalog = _catalog(app, scm_config)
    payload = _approved_payload(catalog, dose=80)
    payload["estado"] = "BORRADOR"
    payload["es_default"] = False
    created = client.post('/api/catalogo/recetas-color', json=payload)
    assert created.status_code == 201, created.get_json()
    draft = created.get_json()

    payload["version"] = draft["version"]
    payload["lineas"][1]["cantidad"] = 95
    updated = client.put(
        f'/api/catalogo/recetas-color/{draft["id"]}',
        json=payload,
    )

    assert updated.status_code == 200, updated.get_json()
    result = updated.get_json()
    assert result["id"] == draft["id"]
    assert result["version"] == 2
    assert result["lineas"][1]["cantidad"] == 95.0


def test_only_one_approved_default_exists_per_color_and_product_scope(
    client,
    app,
    scm_config,
):
    catalog = _catalog(app, scm_config)
    first = client.post(
        '/api/catalogo/recetas-color',
        json=_approved_payload(catalog, variant="FORMULA A"),
    ).get_json()
    second_response = client.post(
        '/api/catalogo/recetas-color',
        json=_approved_payload(catalog, variant="FORMULA B", dose=480),
    )
    assert second_response.status_code == 201
    second = second_response.get_json()
    assert second["es_default"] is True

    rows = client.get(
        f'/api/catalogo/recetas-color?color_produccion_id={catalog["color_id"]}'
        '&include_inactive=true'
    ).get_json()["items"]
    defaults = [item for item in rows if item["estado"] == "APROBADA" and item["es_default"]]
    assert [item["id"] for item in defaults] == [second["id"]]
    assert next(item for item in rows if item["id"] == first["id"])["es_default"] is False
