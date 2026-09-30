"""RED characterization for KG009 recovery query amplification.

This test is intentionally RED until the approved performance package replaces
per-source ORM/eager-loading queries with a bounded batch plan.  It measures
the dry-run only; no KG or UN projection is mutated.
"""

from uuid import uuid4

from sqlalchemy import event

from app import db
from app.services.scm_kg_recovery_service import apply_kg_recovery, preview_kg_recovery
from test_scm_kg009_recovery import _seed_recovery_service_fixture


def test_kg009_preview_has_bounded_sql_shape_after_batch_optimization(app):
    """RED: one-source preview currently triggers eager relationship loads."""
    with app.app_context():
        ctx = _seed_recovery_service_fixture(
            app, station_code=f"PESAJE-KG009-PERF-{uuid4().hex[:8]}"
        )
        statements = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", record)
        try:
            preview = preview_kg_recovery(
                db.session,
                actor_id=ctx["creator"].id,
                article_ids=[ctx["article"].id],
                reason="Caracterizacion RED KG009",
                source_pesaje_ids=[ctx["weighing"].public_id],
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", record)

        assert preview["mode"] == "DRY_RUN"
        assert len(statements) <= 30, (
            "RED query budget exceeded: "
            f"{len(statements)} statements; "
            + " | ".join(statement.splitlines()[0][:120] for statement in statements)
        )


def test_kg009_apply_has_bounded_sql_shape_after_batch_optimization(app):
    """GREEN characterization for one source; 78-source cap is external."""
    with app.app_context():
        ctx = _seed_recovery_service_fixture(
            app, station_code=f"PESAJE-KG009-PERF-APPLY-{uuid4().hex[:8]}"
        )
        source_id = ctx["weighing"].public_id
        preview = preview_kg_recovery(
            db.session,
            actor_id=ctx["creator"].id,
            article_ids=[ctx["article"].id],
            reason="Caracterizacion RED GREEN KG009",
            source_pesaje_ids=[source_id],
        )
        statements = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", record)
        try:
            apply_kg_recovery(
                db.session,
                actor_id=ctx["creator"].id,
                article_ids=[ctx["article"].id],
                reason="Caracterizacion RED GREEN KG009",
                operation_id=uuid4(),
                source_pesaje_ids=[source_id],
                source_snapshot_hashes={
                    str(source_id): preview["sources"][0]["source_snapshot_hash"]
                },
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", record)

        assert len(statements) <= 100, (
            "GREEN per-source query budget exceeded: "
            f"{len(statements)} statements"
        )
