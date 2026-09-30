import json
from contextlib import nullcontext
from io import StringIO
from uuid import uuid4

import pytest

from app.services.scm_service_support import ScmServiceError
from scripts import run_kg009_recovery_manifest as launcher


def _manifest(**overrides):
    source_id = str(uuid4())
    values = {
        "actor_id": 7,
        "article_ids": [233],
        "reason": "Recuperación KG009 autorizada",
        "operation_id": str(uuid4()),
        "source_pesaje_ids": [source_id],
        "source_snapshot_hashes": {source_id: "a" * 64},
    }
    values.update(overrides)
    return values


class _FakeSession:
    def __init__(self):
        self.removed = False

    def remove(self):
        self.removed = True


class _FakeDb:
    def __init__(self, session):
        self.session = session


class _FakeApp:
    config = {
        "SQLALCHEMY_DATABASE_URI": (
            "postgresql://postgres.swsovpdcbomvfhomplnc:secret@"
            "aws-0-us-east-1.pooler.supabase.com:6543/postgres"
        )
    }

    def app_context(self):
        return nullcontext()


def test_load_manifest_rejects_unknown_fields(tmp_path):
    path = tmp_path / "manifest.json"
    payload = _manifest(unexpected="no")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(launcher.ManifestError, match="unknown fields"):
        launcher.load_manifest(path)


def test_main_applies_fixed_manifest_and_prints_redacted_summary(tmp_path):
    path = tmp_path / "manifest.json"
    payload = _manifest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    session = _FakeSession()
    captured = {}

    def fake_recovery(session_arg, **kwargs):
        captured["session"] = session_arg
        captured["kwargs"] = kwargs
        return {
            "mode": "APPLIED",
            "operation_id": payload["operation_id"],
            "articles": [
                {"article_id": 233, "codigo": "PC-000206", "unidad_after": "KG"}
            ],
            "items": [
                {"delta_kg": "1.250", "status": "APPLIED"},
                {"delta_kg": "0.500", "status": "ALREADY_APPLIED"},
            ],
        }

    output = StringIO()
    errors = StringIO()
    status = launcher.main(
        [str(path)],
        app_factory=lambda: _FakeApp(),
        recovery_fn=fake_recovery,
        db_obj=_FakeDb(session),
        output=output,
        error=errors,
    )

    assert status == 0
    assert captured["session"] is session
    assert captured["kwargs"]["actor_id"] == 7
    assert captured["kwargs"]["article_ids"] == [233]
    assert [str(value) for value in captured["kwargs"]["source_pesaje_ids"]] == payload[
        "source_pesaje_ids"
    ]
    assert captured["kwargs"]["operation_id"] == launcher.UUID(payload["operation_id"])
    assert captured["kwargs"]["source_snapshot_hashes"] == payload[
        "source_snapshot_hashes"
    ]
    assert "apply" not in captured["kwargs"]
    assert session.removed is True

    summary = json.loads(output.getvalue())
    assert summary == {
        "articles": [{"article_id": 233, "codigo": "PC-000206", "unidad_after": "KG"}],
            "operation_delta_kg_total": "1.750",
        "mode": "APPLIED",
        "operation_id": payload["operation_id"],
            "response_applied_count": 1,
            "response_semantics": "ORIGINAL_OPERATION_RESPONSE; MAY_BE_IDEMPOTENT_REPLAY",
            "response_source_count": 2,
    }
    assert errors.getvalue() == ""
    assert "a" * 64 not in output.getvalue()


def test_main_reports_service_error_without_traceback(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    output = StringIO()
    errors = StringIO()

    def fail(*_args, **_kwargs):
        raise ScmServiceError(
            "KG_RECOVERY_CONFLICT",
            "Fuente no reconciliable",
            status_code=409,
        )

    status = launcher.main(
        [str(path)],
        app_factory=lambda: _FakeApp(),
        recovery_fn=fail,
        db_obj=_FakeDb(_FakeSession()),
        output=output,
        error=errors,
    )

    assert status == 1
    assert output.getvalue() == ""
    assert json.loads(errors.getvalue()) == {
        "status": "ERROR",
        "code": "KG_RECOVERY_CONFLICT",
        "message": "Fuente no reconciliable",
    }
    assert "Traceback" not in errors.getvalue()


def test_main_rejects_non_productive_database_target(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    output = StringIO()
    errors = StringIO()

    class LocalApp(_FakeApp):
        config = {
            "SQLALCHEMY_DATABASE_URI": "postgresql://user@127.0.0.1:5432/local"
        }

    status = launcher.main(
        [str(path)],
        app_factory=LocalApp,
        recovery_fn=lambda *_args, **_kwargs: pytest.fail("must not call recovery"),
        db_obj=_FakeDb(_FakeSession()),
        output=output,
        error=errors,
    )

    assert status == 2
    assert json.loads(errors.getvalue())["code"] == "KG009_TARGET_MISMATCH"


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://postgres.swsovpdcbomvfhomplnc:secret@"
        "aws-0-us-east-1.pooler.supabase.com:5432/postgres",
        "postgresql://postgres.swsovpdcbomvfhomplnc:secret@"
        "aws-0-us-east-1.pooler.supabase.com:6543/postgres",
        "postgresql://postgres:secret@db.swsovpdcbomvfhomplnc.supabase.co:5432/postgres",
    ],
)
def test_target_guard_accepts_only_known_project_connection_shapes(database_url):
    class ConfiguredApp:
        config = {"SQLALCHEMY_DATABASE_URI": database_url}

    launcher.assert_productive_target(ConfiguredApp())


def test_main_accepts_manifest_from_environment_without_echoing_json(
    monkeypatch,
):
    payload = _manifest()
    raw = json.dumps(payload)
    monkeypatch.setenv("KG009_RECOVERY_MANIFEST", raw)
    session = _FakeSession()
    output = StringIO()
    errors = StringIO()
    captured = {}

    def fake_recovery(session_arg, **kwargs):
        captured["session"] = session_arg
        captured["kwargs"] = kwargs
        return {
            "mode": "APPLIED",
            "operation_id": payload["operation_id"],
            "articles": [],
            "items": [],
        }

    status = launcher.main(
        ["--manifest-env", "KG009_RECOVERY_MANIFEST"],
        app_factory=lambda: _FakeApp(),
        recovery_fn=fake_recovery,
        db_obj=_FakeDb(session),
        output=output,
        error=errors,
    )

    assert status == 0
    assert captured["session"] is session
    assert captured["kwargs"]["operation_id"] == launcher.UUID(
        payload["operation_id"]
    )
    assert raw not in output.getvalue()
    assert raw not in errors.getvalue()


def test_main_requires_exactly_one_manifest_source(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    output = StringIO()
    errors = StringIO()

    neither = launcher.main([], output=output, error=errors)
    assert neither == 2
    assert json.loads(errors.getvalue())["code"] == "MANIFEST_SOURCE_INVALID"

    output.seek(0)
    output.truncate(0)
    errors.seek(0)
    errors.truncate(0)
    monkeypatch.setenv("KG009_RECOVERY_MANIFEST", "{}")
    both = launcher.main(
        [str(path), "--manifest-env", "KG009_RECOVERY_MANIFEST"],
        output=output,
        error=errors,
    )
    assert both == 2
    assert json.loads(errors.getvalue())["code"] == "MANIFEST_SOURCE_INVALID"
