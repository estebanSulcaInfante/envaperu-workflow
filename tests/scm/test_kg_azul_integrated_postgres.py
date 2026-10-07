"""Integrated KG/AZUL checks in guarded, disposable local PostgreSQL schemas."""
from decimal import Decimal
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import DropSchema
from tests.scm.test_scm_prepared_material_postgres import _isolated_postgres_schema
from tests.scm.test_scm_corrida_colada_snapshot_migration import _migration_module
from tests.scm.test_scm_kg009_concurrency_postgres import (
    kg009_schema_url, postgres_kg009_app, _reset_dedicated_data,
)
from tests.scm import test_scm_material_execution as material_cases
from tests.scm import test_scm_prepared_material as prepared_cases
from tests.scm import test_scm_ot_service as ot_cases

pytestmark = pytest.mark.postgres


def test_colada_migration_preserves_history_and_zero_postgres():
    admin, schema, url = _isolated_postgres_schema()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE scm_corrida_fabricacion (id VARCHAR(36) PRIMARY KEY)'))
            connection.execute(text('CREATE TABLE scm_trabajo_color (id VARCHAR(36) PRIMARY KEY, peso_colada_snapshot_g NUMERIC(15,4) NOT NULL)'))
            connection.execute(text("INSERT INTO scm_corrida_fabricacion VALUES ('old')"))
            connection.execute(text("INSERT INTO scm_trabajo_color VALUES ('old-work', 2.0000)"))
            with Operations.context(MigrationContext.configure(connection)):
                _migration_module().upgrade()
            assert connection.execute(text("SELECT snapshot_peso_colada_gr FROM scm_corrida_fabricacion WHERE id='old'")).scalar_one() is None
            assert connection.execute(text("SELECT peso_colada_snapshot_g FROM scm_trabajo_color WHERE id='old-work'")).scalar_one() == Decimal('2')
            column = next(c for c in inspect(connection).get_columns('scm_corrida_fabricacion') if c['name'] == 'snapshot_peso_colada_gr')
            assert column['nullable'] and column['type'].precision == 12 and column['type'].scale == 4
            connection.execute(text("INSERT INTO scm_corrida_fabricacion VALUES ('zero', 0)"))
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(text("INSERT INTO scm_corrida_fabricacion VALUES ('negative', -0.0001)"))
            assert connection.execute(text("SELECT snapshot_peso_colada_gr FROM scm_corrida_fabricacion WHERE id='zero'")).scalar_one() == 0
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.mark.parametrize('consumer', ['materials', 'prepared', 'ot'])
def test_colada_consumers_on_migrated_postgres(postgres_kg009_app, consumer):
    app = postgres_kg009_app
    _reset_dedicated_data(app)
    if consumer == 'materials':
        material_cases.test_requerimientos_usan_override_de_colada_cero_por_corrida(app, app.test_client(), None)
    elif consumer == 'prepared':
        prepared_cases.test_preparacion_usa_override_cero_y_fallback_de_cabecera_por_corrida(app, app.test_client(), None)
    else:
        ot_cases.test_ot_maquina_contiene_varios_trabajos_color_y_ejecucion_exclusiva(app)
