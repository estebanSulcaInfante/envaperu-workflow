"""Real PostgreSQL endpoint transactions under a non-owner runtime role."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from app import create_app
from app.config import Config
from app.extensions import db
from app.models.molde import Pieza
from app.models.producto import ColorBase, ColorProduccion, FamiliaColor, PiezaColor

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_catalog(monkeypatch):
    raw = os.getenv('TEST_DATABASE_URL')
    if not raw:
        pytest.skip('TEST_DATABASE_URL is required')
    url = make_url(raw)
    assert url.host in {'localhost', '127.0.0.1'}
    assert url.database == 'envaperu_test'
    schema = 'rename_' + uuid4().hex[:12]
    role = schema + '_runtime'
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA {schema}'))
        conn.execute(text(f'CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS'))
        conn.execute(text(f'GRANT {role} TO CURRENT_USER'))
    query = dict(url.query, options=f'-csearch_path={schema}')
    scoped_url = url.set(query=query)
    monkeypatch.setattr(Config, 'SQLALCHEMY_DATABASE_URI', scoped_url)
    monkeypatch.setattr(Config, 'SCM_AUTH_MODE', 'local_actor')
    app = create_app()
    app.config['TESTING'] = True
    owner_engine = None
    runtime_engine = None
    try:
        with app.app_context():
            owner_engine = db.engine
            db.create_all()
            piece = Pieza(codigo='PZ-TEST-RENAME', nombre='CABINA', peso_nominal_gr=148)
            base = ColorBase(nombre='AMARILLO MINERO')
            family = FamiliaColor(nombre='S\u00c3\u201cLIDO')
            db.session.add_all([piece, base, family])
            db.session.flush()
            color = ColorProduccion(color_base_id=base.id, familia_color_id=family.id)
            db.session.add(color)
            db.session.flush()
            old_name = f'{piece.nombre} {color.nombre}'
            pc = PiezaColor(sku='PC-TEST-RENAME', pieza_id=piece.id,
                            color_produccion_id=color.id, piezas=old_name,
                            peso=148, estado_revision='EN_REVISION')
            db.session.add(pc)
            db.session.commit()
            piece_id = piece.id
            db.session.remove()
            with owner_engine.begin() as conn:
                conn.execute(text(f'GRANT USAGE ON SCHEMA {schema} TO {role}'))
                conn.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}'))
                conn.execute(text(f'GRANT UPDATE ON pieza, pieza_color, scm_articulo TO {role}'))
            runtime_url = scoped_url.set(query=dict(query, options=f'-csearch_path={schema} -crole={role}'))
            runtime_engine = create_engine(runtime_url)
            db.engines[None] = runtime_engine
            with runtime_engine.connect() as conn:
                assert conn.scalar(text('SELECT current_user')) == role
                assert not conn.scalar(text("SELECT has_table_privilege(current_user, 'scm_articulo_pieza_color', 'UPDATE')"))
            yield app, owner_engine, runtime_engine, piece_id, old_name
    finally:
        with app.app_context():
            db.session.remove()
        if runtime_engine is not None:
            runtime_engine.dispose()
        if owner_engine is not None:
            owner_engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            conn.execute(text(f'DROP ROLE {role}'))
        admin.dispose()


def _rename(app, piece_id, name='CABINA NUEVA'):
    with app.test_client() as client:
        response = client.put(f'/api/piezas/{piece_id}', json={'version': 1, 'nombre': name})
        return response.status_code, response.get_json()


def _names(owner):
    with owner.connect() as conn:
        return conn.execute(text('SELECT p.nombre, p.version, pc.piezas, pc.version, a.nombre, a.version FROM pieza p JOIN pieza_color pc ON pc.pieza_id=p.id JOIN scm_articulo_pieza_color ap ON ap.pieza_color_sku=pc.sku JOIN scm_articulo a ON a.id=ap.articulo_id')).one()


def test_pg_runtime_role_renames_without_update_permission_on_subtype(pg_catalog):
    app, owner, _, piece_id, old_name = pg_catalog
    status, body = _rename(app, piece_id)
    assert status == 200, body
    new_name = old_name.replace('CABINA ', 'CABINA NUEVA ', 1)
    assert tuple(_names(owner)) == ('CABINA NUEVA', 2, new_name, 2, new_name, 2)


def test_pg_concurrent_master_renames_have_one_winner(pg_catalog):
    app, owner, _, piece_id, _ = pg_catalog
    barrier = Barrier(2)
    def rename(name):
        barrier.wait(timeout=10)
        return _rename(app, piece_id, name)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(rename, ['CABINA UNO', 'CABINA DOS']))
    assert sorted(status for status, _ in results) == [200, 409], results
    row = _names(owner)
    assert row[1] == row[3] == row[5] == 2
    assert row[2] == row[4]
    assert row[2].startswith(row[0] + ' ')


def test_pg_manual_name_committed_while_master_waits_is_preserved(pg_catalog):
    app, owner, runtime, piece_id, old_name = pg_catalog
    waiting = Event()
    def before_execute(conn, cursor, statement, parameters, context, many):
        if statement.startswith('SELECT pieza_color.') and 'FOR UPDATE' in statement:
            waiting.set()
    event.listen(runtime, 'before_cursor_execute', before_execute)
    try:
        with owner.connect() as writer, ThreadPoolExecutor(max_workers=1) as pool:
            transaction = writer.begin()
            writer.execute(text("UPDATE pieza_color SET piezas='CUSTOM CONCURRENT', version=version+1"))
            future = pool.submit(_rename, app, piece_id)
            assert waiting.wait(10), 'rename did not reach child lock'
            assert not future.done()
            transaction.commit()
            status, body = future.result(timeout=10)
            assert status == 200, body
        row = _names(owner)
        assert tuple(row) == ('CABINA NUEVA', 2, 'CUSTOM CONCURRENT', 2, old_name, 1)
    finally:
        event.remove(runtime, 'before_cursor_execute', before_execute)


def test_pg_late_article_error_rolls_back_entire_rename(pg_catalog):
    app, owner, _, piece_id, old_name = pg_catalog
    with owner.begin() as conn:
        conn.execute(text("CREATE FUNCTION fail_rename() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced rename rollback'; END $$"))
        conn.execute(text('CREATE TRIGGER fail_rename BEFORE UPDATE ON scm_articulo FOR EACH ROW EXECUTE FUNCTION fail_rename()'))
    status, body = _rename(app, piece_id)
    assert status == 400, body
    assert tuple(_names(owner)) == ('CABINA', 1, old_name, 1, old_name, 1)
