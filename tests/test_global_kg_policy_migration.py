from importlib import import_module

from sqlalchemy.dialects import postgresql


migration = import_module(
    "migrations.versions.b7f31e9c120a_global_kg_inventory_policy"
)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _RecordingBind:
    def __init__(self, server_version_num, schema="public"):
        self.server_version_num = server_version_num
        self.schema = schema
        self.statements = []
        self.dialect = postgresql.dialect()

    def exec_driver_sql(self, statement):
        self.statements.append(statement)
        if statement == "SHOW server_version_num":
            return _ScalarResult(self.server_version_num)
        if statement == "SELECT current_schema()":
            return _ScalarResult(self.schema)
        return _ScalarResult(None)


def _run_with_version(monkeypatch, server_version_num):
    bind = _RecordingBind(server_version_num)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    migration._run_policy_sql(
        migration.UPGRADE_SQL,
        migration.UPGRADE_SQL_SHA256,
    )
    return bind.statements


def test_policy_migration_omits_transaction_timeout_on_postgres_16(monkeypatch):
    statements = _run_with_version(monkeypatch, "160010")

    assert "SET LOCAL transaction_timeout = '3s'" not in statements
    assert "SET LOCAL statement_timeout = '3s'" in statements


def test_policy_migration_uses_transaction_timeout_on_postgres_17(monkeypatch):
    statements = _run_with_version(monkeypatch, "170004")

    assert "SET LOCAL transaction_timeout = '3s'" in statements


def test_policy_migration_targets_alembic_active_schema(monkeypatch):
    bind = _RecordingBind("160010", schema="scm_mig_1234")
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    migration._run_policy_sql(
        migration.UPGRADE_SQL,
        migration.UPGRADE_SQL_SHA256,
    )

    rendered = bind.statements[-1]
    assert "scm_mig_1234.scm_articulo" in rendered
    assert "SET search_path TO pg_catalog, scm_mig_1234" in rendered
    assert "public.scm_articulo" not in rendered
