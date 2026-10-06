import importlib.util
from pathlib import Path
from io import StringIO
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def load_migration():
    path=Path(__file__).resolve().parents[1]/'migrations/versions/fd4e5f607182_assistant_proposals.py'
    spec=importlib.util.spec_from_file_location('assistant_migration',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_local_upgrade_downgrade_preserves_existing_data(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'migration.db'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE trabajador (id INTEGER PRIMARY KEY)'))
        conn.execute(text('INSERT INTO trabajador VALUES (1)'))
        migration=load_migration();migration.op=Operations(MigrationContext.configure(conn))
        migration.upgrade()
        assert {'scm_assistant_proposal','scm_assistant_proposal_revision'} <= set(inspect(conn).get_table_names())
        migration.downgrade()
        assert inspect(conn).get_table_names()==['trabajador']
        assert conn.scalar(text('SELECT count(*) FROM trabajador'))==1


def test_postgres_ddl_is_additive_only():
    output=StringIO()
    migration=load_migration()
    migration.op=Operations(MigrationContext.configure(dialect_name='postgresql',opts={'as_sql':True,'output_buffer':output}))
    migration.upgrade()
    sql=output.getvalue()
    assert migration.down_revision=='fc3d4e5f6071'
    assert sql.count('CREATE TABLE')==2
    assert 'ALTER TABLE' not in sql and 'UPDATE ' not in sql and 'INSERT ' not in sql
    assert 'uq_assistant_proposal_revision' in sql
