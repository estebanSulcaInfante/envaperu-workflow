import pytest

from app.extensions import db
from app.models.correlativo_catalogo import CorrelativoCatalogo
from app.services.catalog_code_generator import (
    CatalogCodeKeyError,
    generar_codigo_catalogo,
    generar_numero_catalogo,
)


def test_generates_independent_six_digit_catalog_codes(app):
    with app.app_context():
        assert generar_codigo_catalogo("pieza") == "PZ-000001"
        assert generar_codigo_catalogo(" PIEZA ") == "PZ-000002"
        assert generar_codigo_catalogo("pieza_color") == "PC-000001"
        assert generar_codigo_catalogo("producto_terminado") == "PT-000001"
        assert generar_codigo_catalogo("molde") == "ML-000001"
        db.session.commit()

        counters = {
            row.clave: row.siguiente_valor
            for row in CorrelativoCatalogo.query.all()
        }
        assert counters == {
            "PIEZA": 3,
            "PIEZA_COLOR": 2,
            "PRODUCTO_TERMINADO": 2,
            "MOLDE": 2,
        }


def test_counter_reservation_participates_in_caller_transaction(app):
    with app.app_context():
        assert generar_codigo_catalogo("PIEZA") == "PZ-000001"
        db.session.rollback()

        assert generar_codigo_catalogo("PIEZA") == "PZ-000001"
        db.session.commit()


def test_recreates_a_missing_counter_with_atomic_upsert(app):
    with app.app_context():
        assert CorrelativoCatalogo.query.count() == 0

        assert generar_codigo_catalogo("MOLDE") == "ML-000001"
        db.session.commit()

        counter = db.session.get(CorrelativoCatalogo, "MOLDE")
        assert counter.prefijo == "ML"
        assert counter.ancho == 6
        assert counter.siguiente_valor == 2


def test_rejects_unknown_counter_key_before_touching_database(app):
    with app.app_context():
        with pytest.raises(CatalogCodeKeyError, match="desconocida"):
            generar_codigo_catalogo("ORDEN_COMPRA")

        assert CorrelativoCatalogo.query.count() == 0


def test_width_is_a_minimum_and_does_not_truncate_large_values(app):
    with app.app_context():
        db.session.add(CorrelativoCatalogo(
            clave="PIEZA",
            prefijo="PZ",
            siguiente_valor=1_000_000,
            ancho=6,
        ))
        db.session.commit()

        assert generar_codigo_catalogo("PIEZA") == "PZ-1000000"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("MATERIA_PRIMA", "MP-000001"),
        ("COLORANTE", "COL-000001"),
        ("ADITIVO", "ADT-000001"),
        ("PROVEEDOR", "PRV-000001"),
        ("CATEGORIA_RECEPCION", "CAT-000001"),
        ("TRABAJADOR", "TRB-000001"),
        ("MAQUINA", "MAQ-000001"),
        ("TIPO_MAQUINA", "TMQ-000001"),
    ],
)
def test_generates_descriptive_codes_for_extended_catalogs(app, key, expected):
    with app.app_context():
        assert generar_codigo_catalogo(key) == expected


@pytest.mark.parametrize("key", ["LINEA", "FAMILIA", "FAMILIA_COLOR"])
def test_numeric_bridge_reserves_the_same_transactional_counter(app, key):
    with app.app_context():
        assert generar_numero_catalogo(key) == 1
        assert generar_numero_catalogo(key) == 2
