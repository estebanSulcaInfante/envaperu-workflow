"""Exercise the new ledger CHECK migration against an isolated SQLite table."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_batch_migration_upgrade_and_history_preserving_downgrade():
    path = next((Path(__file__).parents[2] / "migrations" / "versions").glob("fc3d4e5f6071_*.py"))
    spec = importlib.util.spec_from_file_location("kg_batch_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "CREATE TABLE scm_movimiento_inventario_kg (id INTEGER PRIMARY KEY, tipo TEXT NOT NULL, "
                "CONSTRAINT ck_scm_movimiento_inventario_kg_tipo CHECK (tipo IN ("
                + migration._LEGACY_MOVEMENT_TYPES + ")))"
            ))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                connection.execute(sa.text(
                    "INSERT INTO scm_movimiento_inventario_kg (id, tipo) VALUES (1, 'RETIRO_ARMADO')"
                ))
                with pytest.raises(RuntimeError, match="RETIRO_ARMADO"):
                    migration.downgrade()
                assert connection.execute(sa.text(
                    "SELECT tipo FROM scm_movimiento_inventario_kg WHERE id=1"
                )).scalar_one() == "RETIRO_ARMADO"
                # Only this private in-memory fixture is cleared to exercise
                # downgrade with no existing withdrawal history.
                connection.execute(sa.text("DELETE FROM scm_movimiento_inventario_kg"))
                migration.downgrade()
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(sa.text(
                        "INSERT INTO scm_movimiento_inventario_kg (id, tipo) VALUES (2, 'RETIRO_ARMADO')"
                    ))
    finally:
        engine.dispose()
