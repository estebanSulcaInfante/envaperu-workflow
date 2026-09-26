"""Exercise RPC migration guards with real SQL on isolated test databases.

The narrow predecessor schema isolates this revision. Full-chain migration
coverage remains in test_scm_migrations_postgres and the release evidence.
"""
import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.scm.test_scm_migrations_postgres import (
    _drop_isolated_schema,
    _isolated_postgres_url,
)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def migrated_rpc_connection(request, monkeypatch):
    admin = schema = None
    if request.param == "postgres":
        admin, schema, url = _isolated_postgres_url()
    else:
        url = "sqlite:///:memory:"
    engine = create_engine(url)
    if request.param == "sqlite":
        @event.listens_for(engine, "connect")
        def foreign_keys_on(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    migration = importlib.import_module(
        "migrations.versions.fa1b2c3d4e50_add_of_process_and_run_route_snapshots"
    )
    try:
        with engine.begin() as connection:
            for table in ("scm_orden_fabricacion", "scm_corrida_fabricacion", "scm_operacion_ruta"):
                connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO scm_orden_fabricacion (id) VALUES (1)"))
            connection.execute(text("INSERT INTO scm_corrida_fabricacion (id) VALUES (1)"))
            connection.execute(text("INSERT INTO scm_operacion_ruta (id) VALUES (1)"))
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            yield connection, migration
    finally:
        engine.dispose()
        if admin is not None:
            _drop_isolated_schema(admin, schema)


def _reject(connection, sql, values):
    # A savepoint also recovers PostgreSQL's aborted statement transaction.
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(text(sql), values)


def test_process_and_source_must_be_valid_and_paired(migrated_rpc_connection):
    connection, _ = migrated_rpc_connection
    sql = "UPDATE scm_orden_fabricacion SET snapshot_proceso=:process, fuente_proceso=:source WHERE id=1"
    for process, source in (("SOPLADO", None), (None, "EXPLICITO"),
                            ("EMPAQUE", "EXPLICITO"), ("SOPLADO", "MAQUINA")):
        _reject(connection, sql, {"process": process, "source": source})
    assert connection.execute(text(
        "SELECT snapshot_proceso, fuente_proceso FROM scm_orden_fabricacion WHERE id=1"
    )).one() == (None, None)
    connection.execute(text(sql), {"process": "SOPLADO", "source": "RUTA_OBJETIVOS"})
    assert connection.execute(text("SELECT snapshot_proceso FROM scm_orden_fabricacion WHERE id=1")).scalar_one() == "SOPLADO"


def test_route_reference_requires_existing_operation_and_full_hash(migrated_rpc_connection):
    connection, _ = migrated_rpc_connection
    sql = "UPDATE scm_corrida_fabricacion SET operacion_ruta_revision_id=:operation, operacion_ruta_hash=:hash WHERE id=1"
    for operation, digest in ((1, None), (None, "a" * 64), (1, "a" * 63), (999, "a" * 64)):
        _reject(connection, sql, {"operation": operation, "hash": digest})
    assert connection.execute(text(
        "SELECT operacion_ruta_revision_id, operacion_ruta_hash FROM scm_corrida_fabricacion WHERE id=1"
    )).one() == (None, None)
    connection.execute(text(sql), {"operation": 1, "hash": "a" * 64})
    _reject(connection, "DELETE FROM scm_operacion_ruta WHERE id=:id", {"id": 1})
    assert connection.execute(text("SELECT count(*) FROM scm_operacion_ruta WHERE id=1")).scalar_one() == 1
    assert connection.execute(text("SELECT operacion_ruta_revision_id FROM scm_corrida_fabricacion WHERE id=1")).scalar_one() == 1


def test_roundtrip_preserves_legacy_rows_without_inferred_defaults(migrated_rpc_connection):
    connection, migration = migrated_rpc_connection
    for _ in range(2):
        assert connection.execute(text("SELECT snapshot_proceso, fuente_proceso FROM scm_orden_fabricacion WHERE id=1")).one() == (None, None)
        assert connection.execute(text("SELECT operacion_ruta_revision_id, operacion_ruta_hash FROM scm_corrida_fabricacion WHERE id=1")).one() == (None, None)
        migration.downgrade()
        assert "snapshot_proceso" not in {column["name"] for column in inspect(connection).get_columns("scm_orden_fabricacion")}
        assert connection.execute(text("SELECT id FROM scm_corrida_fabricacion")).scalar_one() == 1
        migration.upgrade()
