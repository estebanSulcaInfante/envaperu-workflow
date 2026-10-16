"""Install the reviewed PIEZA/WIP KG inventory policy after catalog recovery.

The migration is serialized with the global KG runner and fails closed if
any of the 124 approved catalog articles is still UN. The downgrade removes
only the exact v1 trigger, constraint, and function; it does not revert data.

Embedded SQL SHA-256: upgrade=B4CC7A22061182B8EE20D8EC5CCFD73658852F6B0CF98E1D396F8ED877711201; downgrade=D1AE5592C33AF25A2EC1B9CDF0972AA58F09CF2614D5C30EA688D24DAF03BF2B.
"""

from hashlib import sha256
from time import monotonic

from alembic import op

revision = "b7f31e9c120a"
down_revision = "f074de2410ac"
branch_labels = None
depends_on = None

UPGRADE_SQL_SHA256 = "B4CC7A22061182B8EE20D8EC5CCFD73658852F6B0CF98E1D396F8ED877711201"
UPGRADE_SQL = "-- Versioned future policy for the reviewed Alembic migration.\n-- Run only after all 124 baseline PIEZA_COLOR/SUBENSAMBLE_WIP articles are KG.\n-- This file is an artifact; it has not been applied to any database.\n\nDO $precondition$\nBEGIN\n    IF to_regclass('public.scm_articulo') IS NULL THEN\n        RAISE EXCEPTION 'KG_ARTICLE_TABLE_MISSING';\n    END IF;\n    IF EXISTS (\n        SELECT 1 FROM public.scm_articulo\n         WHERE clase IN ('PIEZA_COLOR', 'SUBENSAMBLE_WIP')\n           AND (unidad_inventario IS DISTINCT FROM 'KG'\n                OR unidad_base IS DISTINCT FROM 'UN')\n    ) THEN\n        RAISE EXCEPTION 'KG_CATALOG_NOT_FULLY_RECLASSIFIED';\n    END IF;\n    IF EXISTS (\n        SELECT 1 FROM pg_catalog.pg_constraint\n         WHERE conrelid='public.scm_articulo'::regclass\n           AND conname='ck_scm_articulo_kg_inventory_policy_v1'\n    ) OR to_regprocedure('public.scm_kg_article_insert_guard_v1()') IS NOT NULL\n       OR EXISTS (\n           SELECT 1 FROM pg_catalog.pg_trigger\n            WHERE tgrelid='public.scm_articulo'::regclass\n              AND tgname='scm_kg_article_insert_guard_v1'\n              AND NOT tgisinternal\n       ) THEN\n        RAISE EXCEPTION 'KG_ARTICLE_POLICY_V1_ALREADY_EXISTS';\n    END IF;\nEND\n$precondition$;\n\nALTER TABLE public.scm_articulo\n    ADD CONSTRAINT ck_scm_articulo_kg_inventory_policy_v1\n    CHECK (\n        clase NOT IN ('PIEZA_COLOR', 'SUBENSAMBLE_WIP')\n        OR (unidad_inventario = 'KG' AND unidad_base = 'UN')\n    ) NOT VALID;\n\nALTER TABLE public.scm_articulo\n    VALIDATE CONSTRAINT ck_scm_articulo_kg_inventory_policy_v1;\n\nCREATE FUNCTION public.scm_kg_article_insert_guard_v1()\nRETURNS trigger\nLANGUAGE plpgsql\nSET search_path TO 'pg_catalog', 'public'\nAS $function$\nBEGIN\n    IF NEW.clase IN ('PIEZA_COLOR', 'SUBENSAMBLE_WIP') THEN\n        IF NEW.unidad_base IS DISTINCT FROM 'UN' THEN\n            RAISE EXCEPTION 'KG_BASE_UNIT_MUST_REMAIN_UN';\n        END IF;\n        NEW.unidad_inventario := 'KG';\n    ELSIF NEW.clase = 'PRODUCTO_TERMINADO' THEN\n        -- Product finished goods retain the existing UN policy.\n        NEW.unidad_inventario := 'UN';\n    ELSIF NEW.unidad_inventario = 'KG' THEN\n        RAISE EXCEPTION 'KG_CLASS_NOT_ALLOWED';\n    END IF;\n    RETURN NEW;\nEND\n$function$;\n\nCREATE TRIGGER scm_kg_article_insert_guard_v1\nBEFORE INSERT ON public.scm_articulo\nFOR EACH ROW EXECUTE FUNCTION public.scm_kg_article_insert_guard_v1();\n"

DOWNGRADE_SQL_SHA256 = "D1AE5592C33AF25A2EC1B9CDF0972AA58F09CF2614D5C30EA688D24DAF03BF2B"
DOWNGRADE_SQL = "-- Exact rollback for future_policy.sql. Not applied.\n-- Fails closed if the named v1 objects are missing or owned differently.\n\nDO $precondition$\nDECLARE\n    trigger_function oid;\nBEGIN\n    IF to_regclass('public.scm_articulo') IS NULL THEN\n        RAISE EXCEPTION 'KG_ARTICLE_TABLE_MISSING';\n    END IF;\n    IF NOT EXISTS (\n        SELECT 1 FROM pg_catalog.pg_constraint\n         WHERE conrelid='public.scm_articulo'::regclass\n           AND conname='ck_scm_articulo_kg_inventory_policy_v1'\n    ) THEN\n        RAISE EXCEPTION 'KG_ARTICLE_POLICY_V1_CONSTRAINT_MISSING';\n    END IF;\n    trigger_function := to_regprocedure('public.scm_kg_article_insert_guard_v1()');\n    IF trigger_function IS NULL OR NOT EXISTS (\n        SELECT 1 FROM pg_catalog.pg_trigger\n         WHERE tgrelid='public.scm_articulo'::regclass\n           AND tgname='scm_kg_article_insert_guard_v1'\n           AND tgfoid=trigger_function\n           AND NOT tgisinternal\n    ) THEN\n        RAISE EXCEPTION 'KG_ARTICLE_POLICY_V1_TRIGGER_OR_FUNCTION_MISMATCH';\n    END IF;\nEND\n$precondition$;\n\nDROP TRIGGER scm_kg_article_insert_guard_v1 ON public.scm_articulo;\nALTER TABLE public.scm_articulo\n    DROP CONSTRAINT ck_scm_articulo_kg_inventory_policy_v1;\nDROP FUNCTION public.scm_kg_article_insert_guard_v1();\n"

ADVISORY_LOCK_KEY = 20260919005
MAX_TRANSACTION_SECONDS = 3.0
POSTGRES_TRANSACTION_TIMEOUT_MIN_VERSION = 170000


def _supports_transaction_timeout(bind):
    """PostgreSQL added transaction_timeout in version 17."""
    version = bind.exec_driver_sql("SHOW server_version_num").scalar_one()
    return int(version) >= POSTGRES_TRANSACTION_TIMEOUT_MIN_VERSION


def _render_policy_sql(bind, sql):
    """Render the reviewed SQL into Alembic's active PostgreSQL schema."""
    schema = bind.exec_driver_sql("SELECT current_schema()").scalar_one()
    quoted_schema = bind.dialect.identifier_preparer.quote_schema(schema)
    rendered = sql.replace("public.", f"{quoted_schema}.")
    return rendered.replace(
        "SET search_path TO 'pg_catalog', 'public'",
        f"SET search_path TO pg_catalog, {quoted_schema}",
    )


def _run_policy_sql(sql, expected_sha256):
    if sha256(sql.encode("utf-8")).hexdigest().upper() != expected_sha256:
        raise RuntimeError("KG_ARTICLE_POLICY_SQL_HASH_MISMATCH")
    bind = op.get_bind()
    started = monotonic()
    bind.exec_driver_sql("SET LOCAL lock_timeout = '500ms'")
    bind.exec_driver_sql("SET LOCAL statement_timeout = '3s'")
    if _supports_transaction_timeout(bind):
        bind.exec_driver_sql("SET LOCAL transaction_timeout = '3s'")
    bind.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout = '3s'")
    bind.exec_driver_sql(f"SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY})")
    if monotonic() - started > MAX_TRANSACTION_SECONDS:
        raise RuntimeError("KG_ARTICLE_POLICY_LOCK_DEADLINE_EXCEEDED")
    bind.exec_driver_sql(_render_policy_sql(bind, sql))
    if monotonic() - started > MAX_TRANSACTION_SECONDS:
        raise RuntimeError("KG_ARTICLE_POLICY_TRANSACTION_DEADLINE_EXCEEDED")

def upgrade():
    _run_policy_sql(UPGRADE_SQL, UPGRADE_SQL_SHA256)

def downgrade():
    _run_policy_sql(DOWNGRADE_SQL, DOWNGRADE_SQL_SHA256)
