import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import migrate_station_monitoring as startup


def test_recovery_startup_gate_is_off_by_default_without_spawning():
    calls = []

    attempted = startup.run_kg009_recovery_on_startup(
        environ={}, runner=lambda *args, **kwargs: calls.append((args, kwargs))
    )

    assert attempted is False
    assert calls == []


def test_recovery_startup_uses_exact_manifest_env_command(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    attempted = startup.run_kg009_recovery_on_startup(
        environ={"KG009_RECOVERY_ON_STARTUP": "1"}, runner=fake_run
    )

    assert attempted is True
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        sys.executable,
        str(startup.PROJECT_ROOT / "scripts" / "run_kg009_recovery_manifest.py"),
        "--manifest-env",
        "KG009_RECOVERY_MANIFEST",
    ]
    assert kwargs == {"check": False, "timeout": 60}


@pytest.mark.parametrize(
    "failure",
    [
        SimpleNamespace(returncode=1),
        subprocess.TimeoutExpired(cmd="kg009", timeout=60),
        OSError("launcher unavailable"),
    ],
)
def test_recovery_launcher_failures_are_safe_and_do_not_block_startup(
    failure, capsys
):
    def fake_run(*_args, **_kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    attempted = startup.run_kg009_recovery_on_startup(
        environ={"KG009_RECOVERY_ON_STARTUP": "1"}, runner=fake_run
    )

    assert attempted is True
    output = capsys.readouterr().out
    assert "KG009" in output
    assert "API startup continues" in output
    assert "OSError" not in output
    assert "Traceback" not in output
    if isinstance(failure, subprocess.TimeoutExpired):
        assert "KG009_RECOVERY_LAUNCHER_TIMEOUT" in output
    elif isinstance(failure, OSError):
        assert "KG009_RECOVERY_LAUNCHER_ERROR" in output
    else:
        assert "KG009_RECOVERY_LAUNCHER_EXIT_1" in output


def test_main_propagates_migration_error_without_running_recovery(monkeypatch):
    class FakeApp:
        def app_context(self):
            from contextlib import nullcontext

            return nullcontext()

    calls = []
    monkeypatch.setattr(startup, "create_app", lambda: FakeApp())
    monkeypatch.setattr(
        startup,
        "upgrade",
        lambda: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )
    monkeypatch.setattr(startup, "run_kg009_recovery_on_startup", calls.append)

    with pytest.raises(RuntimeError, match="migration failed"):
        startup.main()

    assert calls == []


def test_main_runs_recovery_hook_only_after_successful_migration(monkeypatch):
    from contextlib import nullcontext

    class FakeApp:
        def app_context(self):
            return nullcontext()

    calls = []
    monkeypatch.setattr(startup, "create_app", lambda: FakeApp())
    monkeypatch.setattr(startup, "upgrade", lambda: None)
    monkeypatch.setattr(startup, "db", SimpleNamespace(engine=object()))
    monkeypatch.setattr(startup, "create_station_monitoring_tables", lambda _engine: [])
    monkeypatch.setattr(startup, "run_kg009_recovery_on_startup", lambda: calls.append(True))

    startup.main()

    assert calls == [True]
