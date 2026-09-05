"""Concurrency verification on an isolated localhost-only schema, not migrations."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app import create_app, db
from app.config import Config
from app.models.producto import Familia, Linea, LineaFamilia
from app.models.trabajador import Trabajador
from app.models.scm_production_orders import ScmOrdenOperacion
from app.services.scm_configuration import ensure_initial_scm_configuration
from tests.scm.test_scm_fabrication_replacement import _seed_released_order, _replace

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("same_key", [False, True])
def test_parallel_replacement_creates_one_successor(same_key):
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL required")
    url = make_url(raw)
    assert url.host in {"localhost", "127.0.0.1"}
    assert url.database == "envaperu_test"
    schema = "of_replacement_" + uuid4().hex
    engine = create_engine(url)
    previous = Config.SQLALCHEMY_DATABASE_URI
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    app = None
    try:
        Config.SQLALCHEMY_DATABASE_URI = url.set(query={"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
        app = create_app()
        app.config["TESTING"] = True
        with app.app_context():
            db.create_all()
            ensure_initial_scm_configuration()
            line = Linea(codigo=1, nombre="TEST")
            family = Familia(codigo=1, nombre="TEST")
            db.session.add_all([line, family])
            db.session.flush()
            db.session.add(LineaFamilia(linea_id=line.id, familia_id=family.id))
            db.session.add(Trabajador(codigo="TRB-01", nombres="Test", apellidos="Concurrente", activo=True))
            db.session.commit()
        order_id, actor_id, _, _ = _seed_released_order(app)
        barrier = Barrier(2)
        shared_key = uuid4()

        def attempt(_):
            with app.test_client() as client:
                barrier.wait(timeout=10)
                response = _replace(client, order_id, actor_id, key=shared_key if same_key else uuid4())
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        assert sorted(status for status, _ in results) == ([201, 201] if same_key else [201, 409]), results
        if same_key:
            assert results[0][1] == results[1][1]
        with app.app_context():
            assert ScmOrdenOperacion.query.filter_by(propuesta_clave=f"REEMPLAZO:{order_id}").count() == 1
    finally:
        if app:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        Config.SQLALCHEMY_DATABASE_URI = previous
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()
