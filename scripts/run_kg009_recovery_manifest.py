"""Apply one reviewed KG009 recovery manifest before the API starts.

The launcher deliberately has no discovery, preview, hash recalculation or
production bypass.  The manifest is the complete authorization boundary and
is handed to ``apply_kg_recovery`` unchanged after structural validation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO
from uuid import UUID

from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.services.scm_kg_recovery_service import (  # noqa: E402
    apply_kg_recovery,
)
from app.services.scm_service_support import ScmServiceError  # noqa: E402


MANIFEST_FIELDS = frozenset(
    {
        "actor_id",
        "article_ids",
        "reason",
        "operation_id",
        "source_pesaje_ids",
        "source_snapshot_hashes",
    }
)
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
PRODUCTIVE_PROJECT = "swsovpdcbomvfhomplnc"


class ManifestError(ValueError):
    """A safe, operator-facing validation error for the fixed manifest."""


class TargetError(ManifestError):
    """The configured database is not the approved production target."""


class SourceError(ManifestError):
    """The launcher received zero or multiple manifest sources."""


def _parse_uuid(value: Any, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ManifestError(f"{field} must be a valid UUID") from error


def _parse_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestError(f"{field} must be a positive integer")
    return value


def _parse_id_list(value: Any, field: str, *, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise ManifestError(f"{field} must contain 1 to {maximum} explicit values")
    if len(set(map(str, value))) != len(value):
        raise ManifestError(f"{field} must not contain duplicates")
    return value


def _validate_manifest_payload(payload: Any) -> dict[str, Any]:
    """Validate decoded JSON from either the path or environment source."""
    if not isinstance(payload, dict):
        raise ManifestError("manifest root must be a JSON object")
    unknown = sorted(set(payload) - MANIFEST_FIELDS)
    missing = sorted(MANIFEST_FIELDS - set(payload))
    if unknown:
        raise ManifestError(f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise ManifestError(f"missing fields: {', '.join(missing)}")

    actor_id = _parse_positive_int(payload["actor_id"], "actor_id")
    article_values = _parse_id_list(payload["article_ids"], "article_ids", maximum=100)
    article_ids = [_parse_positive_int(value, "article_ids") for value in article_values]
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise ManifestError("reason must be non-empty and at most 500 characters")

    operation_id = _parse_uuid(payload["operation_id"], "operation_id")
    source_values = _parse_id_list(
        payload["source_pesaje_ids"], "source_pesaje_ids", maximum=200
    )
    source_ids = [_parse_uuid(value, "source_pesaje_ids") for value in source_values]

    hashes = payload["source_snapshot_hashes"]
    if not isinstance(hashes, dict):
        raise ManifestError("source_snapshot_hashes must be an object")
    canonical_hashes: dict[str, str] = {}
    for raw_key, digest in hashes.items():
        source_id = str(_parse_uuid(raw_key, "source_snapshot_hashes key"))
        if source_id in canonical_hashes:
            raise ManifestError("source_snapshot_hashes must not contain duplicate UUID keys")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ManifestError("source_snapshot_hashes values must be SHA-256 hex digests")
        canonical_hashes[source_id] = digest
    expected_hashes = {str(source_id) for source_id in source_ids}
    if set(canonical_hashes) != expected_hashes:
        raise ManifestError(
            "source_snapshot_hashes must contain exactly the selected source UUIDs"
        )

    return {
        "actor_id": actor_id,
        "article_ids": article_ids,
        "reason": reason.strip(),
        "operation_id": operation_id,
        "source_pesaje_ids": source_ids,
        "source_snapshot_hashes": canonical_hashes,
    }


def load_manifest_json(raw: str) -> dict[str, Any]:
    """Decode and validate JSON supplied through the approved env variable."""
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ManifestError("cannot read manifest JSON") from error
    return _validate_manifest_payload(payload)


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate the exact operator-approved manifest shape."""
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ManifestError("cannot read manifest JSON") from error
    return load_manifest_json(raw)


def _decimal_delta(items: list[Mapping[str, Any]]) -> Decimal:
    total = Decimal("0")
    for item in items:
        try:
            total += Decimal(str(item.get("delta_kg", "0")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ManifestError("recovery returned an invalid delta") from error
    return total.quantize(Decimal("0.001"))


def recovery_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep operator output small and exclude hashes/details from stdout."""
    items = result.get("items") or []
    if not isinstance(items, list):
        raise ManifestError("recovery returned an invalid items list")
    articles = result.get("articles") or []
    if not isinstance(articles, list):
        raise ManifestError("recovery returned an invalid articles list")
    article_summary = [
        {
            "article_id": item.get("article_id"),
            "codigo": item.get("codigo"),
            "unidad_after": item.get("unidad_after"),
        }
        for item in articles
    ]
    delta = _decimal_delta(items)
    applied_count = sum(1 for item in items if item.get("status") != "ALREADY_APPLIED")
    return {
        "mode": result.get("mode"),
        "operation_id": result.get("operation_id"),
        "articles": article_summary,
        # The service may return the original response on an idempotent
        # replay.  These names deliberately describe response contents, not
        # a newly observed mutation.
        "response_source_count": len(items),
        "response_applied_count": applied_count,
        "operation_delta_kg_total": format(delta, "f"),
        "response_semantics": "ORIGINAL_OPERATION_RESPONSE; MAY_BE_IDEMPOTENT_REPLAY",
    }


def assert_productive_target(app: Any) -> None:
    """Fail closed unless the configured database is the approved project."""
    config = getattr(app, "config", None)
    database_url = config.get("SQLALCHEMY_DATABASE_URI") if config else None
    if not database_url:
        raise TargetError("productive database target is not configured")
    try:
        target = make_url(str(database_url))
    except Exception as error:
        raise TargetError("productive database target is invalid") from error
    pooler_target = (
        bool(target.host)
        and target.host.endswith(".pooler.supabase.com")
        and target.username == f"postgres.{PRODUCTIVE_PROJECT}"
        and target.port in {5432, 6543}
    )
    direct_target = (
        target.host == f"db.{PRODUCTIVE_PROJECT}.supabase.co"
        and target.username == "postgres"
        and target.port == 5432
    )
    if target.database != "postgres" or not (pooler_target or direct_target):
        raise TargetError("database target is not the approved production project")


def _error_payload(code: str, message: str) -> dict[str, str]:
    return {"status": "ERROR", "code": code, "message": message}


def run_manifest(
    manifest: Mapping[str, Any],
    *,
    app_factory: Callable[[], Any],
    recovery_fn: Callable[..., Mapping[str, Any]],
    db_obj: Any,
) -> Mapping[str, Any]:
    """Run the approved apply path with an injectable app/session for tests."""
    app = app_factory()
    with app.app_context():
        try:
            assert_productive_target(app)
            return recovery_fn(
                db_obj.session,
                actor_id=manifest["actor_id"],
                article_ids=manifest["article_ids"],
                reason=manifest["reason"],
                operation_id=manifest["operation_id"],
                source_pesaje_ids=manifest["source_pesaje_ids"],
                source_snapshot_hashes=manifest["source_snapshot_hashes"],
            )
        finally:
            db_obj.session.remove()


def main(
    argv: list[str] | None = None,
    *,
    app_factory: Callable[[], Any] | None = None,
    recovery_fn: Callable[..., Mapping[str, Any]] | None = None,
    db_obj: Any | None = None,
    output: TextIO | None = None,
    error: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Aplica un manifiesto explícito de recuperación KG009 y termina."
    )
    parser.add_argument("manifest", nargs="?", type=Path)
    parser.add_argument(
        "--manifest-env",
        metavar="ENV_NAME",
        help="Nombre de la variable que contiene el JSON cerrado del manifiesto.",
    )
    args = parser.parse_args(argv)
    output = sys.stdout if output is None else output
    error = sys.stderr if error is None else error
    app_factory = create_app if app_factory is None else app_factory
    recovery_fn = apply_kg_recovery if recovery_fn is None else recovery_fn
    db_obj = db if db_obj is None else db_obj

    try:
        if args.manifest is None and args.manifest_env is None:
            raise SourceError("exactly one manifest source is required")
        if args.manifest is not None and args.manifest_env is not None:
            raise SourceError("manifest path and environment source are mutually exclusive")
        if args.manifest_env is not None:
            raw_manifest = os.environ.get(args.manifest_env)
            if raw_manifest is None:
                raise ManifestError("manifest environment variable is not set")
            manifest = load_manifest_json(raw_manifest)
        else:
            manifest = load_manifest(args.manifest)
        result = run_manifest(
            manifest,
            app_factory=app_factory,
            recovery_fn=recovery_fn,
            db_obj=db_obj,
        )
        print(json.dumps(recovery_summary(result), ensure_ascii=False, sort_keys=True), file=output)
        return 0
    except TargetError as exc:
        print(json.dumps(_error_payload("KG009_TARGET_MISMATCH", str(exc)), ensure_ascii=False), file=error)
        return 2
    except SourceError as exc:
        print(json.dumps(_error_payload("MANIFEST_SOURCE_INVALID", str(exc)), ensure_ascii=False), file=error)
        return 2
    except ManifestError as exc:
        print(json.dumps(_error_payload("MANIFEST_INVALID", str(exc)), ensure_ascii=False), file=error)
        return 2
    except ScmServiceError as exc:
        print(json.dumps(_error_payload(exc.code, exc.message), ensure_ascii=False), file=error)
        return 1
    except Exception:
        # Never expose DSNs, tracebacks or SQL details from a startup command.
        print(
            json.dumps(
                _error_payload("KG009_RECOVERY_LAUNCHER_ERROR", "recovery failed"),
                ensure_ascii=False,
            ),
            file=error,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
