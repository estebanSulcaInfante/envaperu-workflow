import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_mesa_review_migration_preserves_history_and_limits_active_tokens():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    table = sa.Table(
        "scm_mesa_inventario_revision",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("codigo", sa.String, nullable=False),
        sa.Column("estado", sa.String, nullable=False),
        sa.UniqueConstraint("codigo", "estado", name="uq_mesa_inv_revision_codigo_estado"),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert(), [
            {"id": "1", "codigo": "MANGA-1", "estado": "INVALIDADA"},
            {"id": "2", "codigo": "MANGA-1", "estado": "CONSUMIDA"},
        ])
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        spec = importlib.util.spec_from_file_location(
            "mesa26011905",
            Path(__file__).parents[2] / "migrations/versions/mesa26011905_unique_active_mesa_review.py",
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.op = operations
        migration.upgrade()

        connection.execute(sa.text(
            "INSERT INTO scm_mesa_inventario_revision (id, codigo, estado) "
            "VALUES ('3', 'MANGA-1', 'ACTIVA')"
        ))
        try:
            connection.execute(sa.text(
                "INSERT INTO scm_mesa_inventario_revision (id, codigo, estado) "
                "VALUES ('4', 'MANGA-1', 'ACTIVA')"
            ))
        except sa.exc.IntegrityError:
            pass
        else:
            raise AssertionError("debe existir un único token ACTIVA por manga")

        connection.execute(sa.text(
            "INSERT INTO scm_mesa_inventario_revision (id, codigo, estado) "
            "VALUES ('5', 'MANGA-1', 'INVALIDADA')"
        ))
        assert connection.execute(sa.text(
            "SELECT count(*) FROM scm_mesa_inventario_revision WHERE codigo = 'MANGA-1'"
        )).scalar_one() == 4
