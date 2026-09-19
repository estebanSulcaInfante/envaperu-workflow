"""Deterministic UN/KG marker race against a legacy UN write."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.scm.test_scm_kg_postgres import (
    F94,
    _article,
    _drop_isolated_schema,
    _isolated_postgres_url,
    _location,
    _run_flask_db,
)


pytestmark = pytest.mark.postgres


def _race_schema():
    admin_engine, schema, schema_url = _isolated_postgres_url()
    _run_flask_db(schema_url, "upgrade", F94)
    return admin_engine, schema, schema_url


def _run_race(*, marker_first):
    admin_engine, schema, schema_url = _race_schema()
    setup_engine = create_engine(schema_url)
    try:
        with setup_engine.begin() as connection:
            article_id = _article(
                connection,
                code=f"KG-RACE-{uuid4().hex[:10].upper()}",
                unit="UN",
            )
            location_id = _location(
                connection,
                code=f"KG-RACE-LOC-{uuid4().hex[:10].upper()}",
            )

        marker_ready = Event()
        un_ready = Event()
        marker_attempt = Event()
        un_attempt = Event()

        def marker_transaction():
            engine = create_engine(schema_url)
            try:
                with engine.connect() as connection:
                    transaction = connection.begin()
                    try:
                        if not marker_first:
                            if not un_ready.wait(timeout=30):
                                return "marker_timeout"
                            marker_attempt.set()
                        connection.execute(text("""
                            UPDATE scm_articulo
                            SET unidad_inventario = 'KG'
                            WHERE id = :article_id
                        """), {"article_id": article_id})
                        marker_ready.set()
                        if marker_first:
                            if not un_attempt.wait(timeout=30):
                                return "marker_timeout"
                        transaction.commit()
                        return "marker"
                    except DBAPIError:
                        transaction.rollback()
                        return "marker_error"
                    finally:
                        marker_ready.set()
                        marker_attempt.set()
                        un_attempt.set()
                        un_ready.set()
            finally:
                engine.dispose()

        def un_transaction():
            engine = create_engine(schema_url)
            try:
                with engine.connect() as connection:
                    transaction = connection.begin()
                    try:
                        if marker_first:
                            if not marker_ready.wait(timeout=30):
                                return "un_timeout"
                            un_attempt.set()
                        connection.execute(text("""
                            INSERT INTO scm_saldo_inventario (
                                id, articulo_scm_id, ubicacion_id,
                                cantidad_fisica, cantidad_reservada,
                                cantidad_no_disponible, version
                            ) VALUES (:id, :article_id, :location_id, 1, 0, 1, 1)
                        """), {
                            "id": str(uuid4()), "article_id": article_id,
                            "location_id": location_id,
                        })
                        un_ready.set()
                        if not marker_first:
                            if not marker_attempt.wait(timeout=30):
                                return "un_timeout"
                        transaction.commit()
                        return "un"
                    except DBAPIError:
                        transaction.rollback()
                        return "un_error"
                    finally:
                        marker_ready.set()
                        marker_attempt.set()
                        un_attempt.set()
                        un_ready.set()
            finally:
                engine.dispose()

        with ThreadPoolExecutor(max_workers=2) as executor:
            marker_future = executor.submit(marker_transaction)
            un_future = executor.submit(un_transaction)
            outcomes = {marker_future.result(), un_future.result()}

        with setup_engine.connect() as connection:
            marker = connection.execute(text(
                "SELECT unidad_inventario FROM scm_articulo WHERE id = :id"
            ), {"id": article_id}).scalar_one()
            legacy_count = connection.execute(text(
                "SELECT count(*) FROM scm_saldo_inventario WHERE articulo_scm_id = :id"
            ), {"id": article_id}).scalar_one()
            kg_count = connection.execute(text(
                "SELECT count(*) FROM scm_saldo_inventario_kg WHERE articulo_scm_id = :id"
            ), {"id": article_id}).scalar_one()
        return outcomes, marker, legacy_count, kg_count
    finally:
        setup_engine.dispose()
        _drop_isolated_schema(admin_engine, schema)


@pytest.mark.parametrize("marker_first", [True, False])
def test_opt_in_marker_and_legacy_un_write_have_one_deterministic_winner(marker_first):
    outcomes, marker, legacy_count, kg_count = _run_race(marker_first=marker_first)
    assert outcomes == (
        {"marker", "un_error"} if marker_first else {"marker_error", "un"}
    )
    assert (marker, legacy_count, kg_count) == (
        ("KG", 0, 0) if marker_first else ("UN", 1, 0)
    )
