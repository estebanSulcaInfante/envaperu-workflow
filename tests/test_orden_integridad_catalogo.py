from app.extensions import db
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.orden import OrdenProduccion
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    Familia,
    FamiliaColor,
    Linea,
    LineaFamilia,
    PiezaColor,
    ProductoPieza,
    ProductoTerminado,
)


def _seed_catalog(app, *, second_piece=False):
    with app.app_context():
        linea = Linea.query.first()
        familia = Familia.query.first()

        family_color = FamiliaColor(codigo=70, nombre="SOLIDO TEST OP")
        red_base = ColorBase(nombre="ROJO TEST OP")
        blue_base = ColorBase(nombre="AZUL TEST OP")
        db.session.add_all([family_color, red_base, blue_base])
        db.session.flush()
        red = ColorProduccion(
            color_base_id=red_base.id,
            familia_color_id=family_color.id,
        )
        blue = ColorProduccion(
            color_base_id=blue_base.id,
            familia_color_id=family_color.id,
        )

        mold = Molde(
            codigo="ML-OP-INTEGRIDAD",
            nombre="Molde integridad OP",
            peso_tiro_gr=25,
            tiempo_ciclo_std=12,
            activo=True,
        )
        piece = Pieza(
            codigo="PZ-OP-INTEGRIDAD",
            nombre="Pieza integridad OP",
            linea_id=linea.id,
            familia_id=familia.id,
            peso_nominal_gr=10,
            activo=True,
        )
        relation = MoldePieza(
            molde=mold,
            pieza=piece,
            cavidades=2,
            peso_unitario_gr=10,
            activo=True,
        )
        db.session.add_all([red, blue, mold, piece, relation])

        other_piece = None
        other_mold = None
        other_relation = None
        if second_piece:
            other_mold = Molde(
                codigo="ML-OP-OTRO",
                nombre="Molde otra pieza",
                peso_tiro_gr=9,
                tiempo_ciclo_std=10,
                activo=True,
            )
            other_piece = Pieza(
                codigo="PZ-OP-OTRA",
                nombre="Otra pieza",
                linea_id=linea.id,
                familia_id=familia.id,
                peso_nominal_gr=8,
                activo=True,
            )
            other_relation = MoldePieza(
                molde=other_mold,
                pieza=other_piece,
                cavidades=1,
                peso_unitario_gr=8,
                activo=True,
            )
            db.session.add_all([other_mold, other_piece, other_relation])

        db.session.commit()
        return {
            "linea_id": linea.id,
            "familia_id": familia.id,
            "red_id": red.id,
            "blue_id": blue.id,
            "mold_id": mold.codigo,
            "piece_id": piece.id,
            "relation_id": relation.id,
            "other_mold_id": other_mold.codigo if other_mold else None,
            "other_piece_id": other_piece.id if other_piece else None,
            "other_relation_id": other_relation.id if other_relation else None,
        }


def _payload(catalog, numero_op, *, colors=(), product_sku=None):
    return {
        "numero_op": numero_op,
        "maquina_id": 1,
        "producto_sku": product_sku,
        "molde_id": catalog["mold_id"],
        "snapshot_tiempo_ciclo": 12,
        "snapshot_horas_turno": 8,
        "snapshot_peso_colada_gr": 5,
        "auto_snapshot_molde": True,
        "snapshot_composicion": [],
        "lotes": [
            {
                "color_id": color_id,
                "meta_kg": 100,
                "personas": 1,
                "materiales": [],
                "pigmentos": [],
            }
            for color_id in colors
        ],
    }


def _create_variant(catalog, *, sku, piece_id=None, color_id=None, familia_id=None):
    piece = db.session.get(Pieza, piece_id or catalog["piece_id"])
    variant = PiezaColor(
        sku=sku,
        piezas=sku,
        pieza_id=piece.id,
        linea_id=piece.linea_id,
        familia_id=familia_id or piece.familia_id,
        color_produccion_id=color_id,
        peso=piece.peso_nominal_gr,
    )
    db.session.add(variant)
    db.session.flush()
    return variant


def test_prevalidation_reports_variant_and_post_creates_it_once(client, app):
    catalog = _seed_catalog(app)

    preview = client.get(
        "/api/validar-orden-prereq",
        query_string={
            "molde_id": catalog["mold_id"],
            "color_ids": str(catalog["red_id"]),
        },
    )
    assert preview.status_code == 200
    preview_data = preview.get_json()
    assert preview_data["valid"] is True
    assert preview_data["variantes_por_crear"] == [
        {
            "pieza_id": catalog["piece_id"],
            "pieza_codigo": "PZ-OP-INTEGRIDAD",
            "pieza_nombre": "Pieza integridad OP",
            "color_id": catalog["red_id"],
        }
    ]
    with app.app_context():
        assert PiezaColor.query.count() == 0

    first = client.post(
        "/api/ordenes",
        json=_payload(catalog, "OP-INTEGRIDAD-001", colors=[catalog["red_id"]]),
    )
    assert first.status_code == 201, first.get_json()
    generated = first.get_json()["catalogo_autocreado"]["piezas_color"]
    assert len(generated) == 1
    assert generated[0]["pieza_sku"].startswith("PC-")

    second = client.post(
        "/api/ordenes",
        json=_payload(catalog, "OP-INTEGRIDAD-002", colors=[catalog["red_id"]]),
    )
    assert second.status_code == 201, second.get_json()
    assert "catalogo_autocreado" not in second.get_json()
    with app.app_context():
        variants = PiezaColor.query.filter_by(
            pieza_id=catalog["piece_id"],
            color_produccion_id=catalog["red_id"],
        ).all()
        assert len(variants) == 1
        snapshots = [
            order.snapshot_composicion[0]
            for order in OrdenProduccion.query.order_by(OrdenProduccion.numero_op)
        ]
        assert [snapshot.pieza_id for snapshot in snapshots] == [
            catalog["piece_id"],
            catalog["piece_id"],
        ]
        assert [snapshot.pieza_codigo_snapshot for snapshot in snapshots] == [
            "PZ-OP-INTEGRIDAD",
            "PZ-OP-INTEGRIDAD",
        ]
        assert [snapshot.pieza_nombre_snapshot for snapshot in snapshots] == [
            "Pieza integridad OP",
            "Pieza integridad OP",
        ]
        assert all(snapshot.pieza_sku_legacy is None for snapshot in snapshots)


def test_manual_sku_from_another_mold_is_rejected(client, app):
    catalog = _seed_catalog(app, second_piece=True)
    with app.app_context():
        other = _create_variant(
            catalog,
            sku="PC-OTRA-ROJA",
            piece_id=catalog["other_piece_id"],
            color_id=catalog["red_id"],
        )
        db.session.commit()
        other_sku = other.sku

    payload = _payload(catalog, "OP-MOLDE-DIVERGENTE")
    payload["auto_snapshot_molde"] = False
    payload["snapshot_composicion"] = [{
        "pieza_sku": other_sku,
        "cavidades": 1,
        "peso_unit_gr": 8,
    }]
    response = client.post("/api/ordenes", json=payload)
    assert response.status_code == 409
    assert response.get_json()["codigo"] == "PIEZA_NO_PERTENECE_MOLDE"
    with app.app_context():
        assert db.session.get(OrdenProduccion, "OP-MOLDE-DIVERGENTE") is None


def test_divergent_piece_color_classification_is_rejected(client, app):
    catalog = _seed_catalog(app)
    with app.app_context():
        other_family = Familia(codigo=991, nombre="FAMILIA DIVERGENTE OP")
        db.session.add(other_family)
        db.session.flush()
        db.session.add(LineaFamilia(
            linea_id=catalog["linea_id"],
            familia_id=other_family.id,
        ))
        bad = _create_variant(
            catalog,
            sku="PC-CLASIFICACION-MAL",
            color_id=catalog["red_id"],
            familia_id=other_family.id,
        )
        db.session.commit()
        bad_sku = bad.sku

    payload = _payload(catalog, "OP-CLASIFICACION-MAL", colors=[catalog["red_id"]])
    payload["auto_snapshot_molde"] = False
    payload["snapshot_composicion"] = [{
        "molde_pieza_id": catalog["relation_id"],
        "pieza_sku": bad_sku,
        "cavidades": 2,
        "peso_unit_gr": 10,
    }]
    response = client.post("/api/ordenes", json=payload)
    assert response.status_code == 409
    assert response.get_json()["codigo"] == "CLASIFICACION_PIEZA_DIVERGENTE"


def test_inactive_line_family_pair_is_rejected(client, app):
    catalog = _seed_catalog(app)
    with app.app_context():
        relation = LineaFamilia.query.filter_by(
            linea_id=catalog["linea_id"],
            familia_id=catalog["familia_id"],
        ).one()
        relation.activo = False
        db.session.commit()

    response = client.post(
        "/api/ordenes",
        json=_payload(catalog, "OP-CLASIFICACION-INACTIVA"),
    )
    assert response.status_code == 409
    assert response.get_json()["codigo"] == "LINEA_FAMILIA_NO_ASOCIADA"


def test_explicit_legacy_sku_is_evidence_and_does_not_override_lot_color(client, app):
    catalog = _seed_catalog(app)
    with app.app_context():
        red = _create_variant(
            catalog,
            sku="PC-ROJA-EXPLICITA",
            color_id=catalog["red_id"],
        )
        db.session.commit()
        red_sku = red.sku

    payload = _payload(catalog, "OP-COLOR-DIVERGENTE", colors=[catalog["blue_id"]])
    payload["auto_snapshot_molde"] = False
    payload["snapshot_composicion"] = [{
        "molde_pieza_id": catalog["relation_id"],
        "pieza_sku": red_sku,
        "cavidades": 2,
        "peso_unit_gr": 10,
    }]
    response = client.post("/api/ordenes", json=payload)
    assert response.status_code == 201, response.get_json()
    with app.app_context():
        snapshot = db.session.get(
            OrdenProduccion,
            "OP-COLOR-DIVERGENTE",
        ).snapshot_composicion[0]
        assert snapshot.pieza_id == catalog["piece_id"]
        assert snapshot.pieza_sku_legacy == red_sku
        assert PiezaColor.query.filter_by(
            pieza_id=catalog["piece_id"],
            color_produccion_id=catalog["blue_id"],
        ).count() == 1


def test_product_bom_matches_by_abstract_piece_and_allows_inline_color(client, app):
    catalog = _seed_catalog(app)
    with app.app_context():
        generic = _create_variant(catalog, sku="PC-GENERICA-BOM")
        product = ProductoTerminado(
            cod_sku_pt="PT-OP-COMPATIBLE",
            producto="Producto compatible",
            linea_id=catalog["linea_id"],
            familia_id=catalog["familia_id"],
        )
        db.session.add(product)
        db.session.flush()
        db.session.add(ProductoPieza(
            producto_terminado_id=product.cod_sku_pt,
            pieza_sku=generic.sku,
            cantidad=1,
        ))
        db.session.commit()

    response = client.post(
        "/api/ordenes",
        json=_payload(
            catalog,
            "OP-PRODUCTO-COMPATIBLE",
            colors=[catalog["red_id"]],
            product_sku="PT-OP-COMPATIBLE",
        ),
    )
    assert response.status_code == 201, response.get_json()
    with app.app_context():
        assert PiezaColor.query.filter_by(
            pieza_id=catalog["piece_id"],
            color_produccion_id=catalog["red_id"],
        ).count() == 1


def test_product_and_mold_piece_sets_must_match(client, app):
    catalog = _seed_catalog(app, second_piece=True)
    with app.app_context():
        other_generic = _create_variant(
            catalog,
            sku="PC-OTRA-BOM",
            piece_id=catalog["other_piece_id"],
        )
        product = ProductoTerminado(
            cod_sku_pt="PT-OP-INCOMPATIBLE",
            producto="Producto de otro molde",
            linea_id=catalog["linea_id"],
            familia_id=catalog["familia_id"],
        )
        db.session.add(product)
        db.session.flush()
        db.session.add(ProductoPieza(
            producto_terminado_id=product.cod_sku_pt,
            pieza_sku=other_generic.sku,
            cantidad=1,
        ))
        db.session.commit()

    response = client.post(
        "/api/ordenes",
        json=_payload(
            catalog,
            "OP-PRODUCTO-INCOMPATIBLE",
            product_sku="PT-OP-INCOMPATIBLE",
        ),
    )
    assert response.status_code == 409
    body = response.get_json()
    assert body["codigo"] == "PRODUCTO_MOLDE_INCOMPATIBLE"
    assert body["details"]["piezas_bom_faltantes_en_molde"] == [
        catalog["other_piece_id"]
    ]
    assert body["details"]["piezas_molde_fuera_bom"] == [catalog["piece_id"]]


def test_multicolor_snapshot_uses_abstract_piece_and_creates_each_output(client, app):
    catalog = _seed_catalog(app)

    response = client.post(
        "/api/ordenes",
        json=_payload(
            catalog,
            "OP-MULTICOLOR",
            colors=[catalog["red_id"], catalog["blue_id"]],
        ),
    )
    assert response.status_code == 201, response.get_json()
    with app.app_context():
        order = db.session.get(OrdenProduccion, "OP-MULTICOLOR")
        snapshot = order.snapshot_composicion[0]
        assert snapshot.pieza_id == catalog["piece_id"]
        assert snapshot.pieza_sku_legacy is None
        assert {
            item.color_produccion_id
            for item in PiezaColor.query.filter_by(pieza_id=catalog["piece_id"])
        } == {catalog["red_id"], catalog["blue_id"]}


def test_invalid_operational_values_and_non_operational_machine_are_rejected(client, app):
    catalog = _seed_catalog(app)
    payload = _payload(catalog, "OP-CAVIDAD-CERO")
    payload["auto_snapshot_molde"] = False
    payload["snapshot_composicion"] = [{
        "molde_pieza_id": catalog["relation_id"],
        "cavidades": 0,
        "peso_unit_gr": 10,
    }]
    invalid_cavity = client.post("/api/ordenes", json=payload)
    assert invalid_cavity.status_code == 400
    assert invalid_cavity.get_json()["codigo"] == "VALOR_INVALIDO"

    invalid_lot_payload = _payload(
        catalog,
        "OP-LOTE-CERO",
        colors=[catalog["red_id"]],
    )
    invalid_lot_payload["lotes"][0]["meta_kg"] = 0
    invalid_lot = client.post("/api/ordenes", json=invalid_lot_payload)
    assert invalid_lot.status_code == 400
    assert invalid_lot.get_json()["details"]["field"] == "lotes[0].meta_kg"

    with app.app_context():
        from app.models.maquina import Maquina

        machine = db.session.get(Maquina, 1)
        machine.estado = "MANTENIMIENTO"
        db.session.commit()
    non_operational = client.post(
        "/api/ordenes",
        json=_payload(catalog, "OP-MAQUINA-MANTENIMIENTO"),
    )
    assert non_operational.status_code == 409
    assert non_operational.get_json()["codigo"] == "MAQUINA_NO_OPERATIVA"

    preview = client.get(
        "/api/validar-orden-prereq",
        query_string={
            "molde_id": catalog["mold_id"],
            "maquina_id": 1,
            "numero_op": "OP-PREVIEW-MAQUINA",
        },
    )
    assert preview.status_code == 200
    assert preview.get_json()["valid"] is False
    assert preview.get_json()["issues"][0]["codigo"] == "MAQUINA_NO_OPERATIVA"


def test_technical_parameters_people_and_duplicate_order_are_rejected(client, app):
    catalog = _seed_catalog(app)
    invalid_cases = [
        ("snapshot_tiempo_ciclo", 0),
        ("snapshot_horas_turno", 0),
        ("snapshot_peso_colada_gr", -1),
    ]
    for index, (field, value) in enumerate(invalid_cases):
        payload = _payload(catalog, f"OP-TECNICO-{index}")
        payload[field] = value
        response = client.post("/api/ordenes", json=payload)
        assert response.status_code == 400
        assert response.get_json()["codigo"] == "VALOR_INVALIDO"
        assert response.get_json()["details"]["field"] == field

    people_payload = _payload(
        catalog,
        "OP-PERSONAS-CERO",
        colors=[catalog["red_id"]],
    )
    people_payload["lotes"][0]["personas"] = 0
    people = client.post("/api/ordenes", json=people_payload)
    assert people.status_code == 400
    assert people.get_json()["details"]["field"] == "lotes[0].personas"

    existing = client.post(
        "/api/ordenes",
        json=_payload(catalog, "OP-YA-EXISTE"),
    )
    assert existing.status_code == 201
    preview = client.get(
        "/api/validar-orden-prereq",
        query_string={
            "numero_op": "OP-YA-EXISTE",
            "maquina_id": 1,
            "molde_id": catalog["mold_id"],
        },
    )
    assert preview.status_code == 200
    assert preview.get_json()["valid"] is False
    issue = preview.get_json()["issues"][0]
    assert issue["codigo"] == "ORDEN_YA_EXISTE"
    assert issue["status"] == 409


def test_integer_fields_reject_fraction_but_accept_integral_float(client, app):
    catalog = _seed_catalog(app)
    fractional = _payload(
        catalog,
        "OP-PERSONAS-DECIMAL",
        colors=[catalog["red_id"]],
    )
    fractional["lotes"][0]["personas"] = 1.5
    rejected = client.post("/api/ordenes", json=fractional)
    assert rejected.status_code == 400
    assert rejected.get_json()["codigo"] == "VALOR_INVALIDO"
    assert rejected.get_json()["details"]["field"] == "lotes[0].personas"

    integral = _payload(
        catalog,
        "OP-PERSONAS-ENTERO-FLOAT",
        colors=[catalog["red_id"]],
    )
    integral["lotes"][0]["personas"] = 1.0
    accepted = client.post("/api/ordenes", json=integral)
    assert accepted.status_code == 201, accepted.get_json()
    with app.app_context():
        order = db.session.get(OrdenProduccion, "OP-PERSONAS-ENTERO-FLOAT")
        assert order.lotes[0].personas == 1


def test_manual_catalog_composition_must_match_current_mold_piece_values(client, app):
    catalog = _seed_catalog(app)

    wrong_cavities = _payload(catalog, "OP-CAVIDADES-DIVERGENTES")
    wrong_cavities["auto_snapshot_molde"] = False
    wrong_cavities["snapshot_composicion"] = [{
        "molde_pieza_id": catalog["relation_id"],
        "cavidades": 1,
        "peso_unit_gr": 10,
    }]
    cavities_response = client.post("/api/ordenes", json=wrong_cavities)
    assert cavities_response.status_code == 409
    cavities_issue = cavities_response.get_json()
    assert cavities_issue["codigo"] == "COMPOSICION_MOLDE_VALORES_DIVERGENTES"
    assert cavities_issue["details"]["esperado"] == {
        "cavidades": 2,
        "peso_unit_gr": 10.0,
    }
    assert cavities_issue["details"]["recibido"] == {
        "cavidades": 1,
        "peso_unit_gr": 10.0,
    }

    wrong_weight = _payload(catalog, "OP-PESO-DIVERGENTE")
    wrong_weight["auto_snapshot_molde"] = False
    wrong_weight["snapshot_composicion"] = [{
        "molde_pieza_id": catalog["relation_id"],
        "cavidades": 2,
        "peso_unit_gr": 10.5,
    }]
    weight_response = client.post("/api/ordenes", json=wrong_weight)
    assert weight_response.status_code == 409
    assert weight_response.get_json()["codigo"] == "COMPOSICION_MOLDE_VALORES_DIVERGENTES"
    assert weight_response.get_json()["details"]["recibido"]["peso_unit_gr"] == 10.5

    matching = _payload(catalog, "OP-COMPOSICION-COINCIDENTE")
    matching["auto_snapshot_molde"] = False
    matching["snapshot_composicion"] = [{
        "molde_pieza_id": catalog["relation_id"],
        "cavidades": 2,
        "peso_unit_gr": 10.0,
    }]
    accepted = client.post("/api/ordenes", json=matching)
    assert accepted.status_code == 201, accepted.get_json()


def test_legacy_manual_snapshot_without_mold_keeps_free_positive_values(client, app):
    _seed_catalog(app)
    payload = {
        "numero_op": "OP-LEGACY-SIN-MOLDE",
        "maquina_id": 1,
        "snapshot_tiempo_ciclo": 15,
        "snapshot_horas_turno": 8,
        "snapshot_peso_colada_gr": 1,
        "auto_snapshot_molde": False,
        "snapshot_composicion": [{
            "cavidades": 3,
            "peso_unit_gr": 7.25,
        }],
        "lotes": [],
    }
    response = client.post("/api/ordenes", json=payload)
    assert response.status_code == 201, response.get_json()
