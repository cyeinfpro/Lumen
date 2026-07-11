from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_runner() -> ModuleType:
    path = ROOT / "scripts" / "restore_runner.py"
    spec = importlib.util.spec_from_file_location("lumen_restore_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restore_runner_accepts_only_fresh_timestamp_regular_file(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    trigger = tmp_path / "restore.trigger"
    trigger.write_text("20260519-010203\n", encoding="ascii")

    assert runner.load_timestamp(trigger) == "20260519-010203"

    trigger.write_text("../../etc/passwd\n", encoding="ascii")
    with pytest.raises(runner.RestoreTriggerError, match="invalid"):
        runner.load_timestamp(trigger)


def test_restore_runner_rejects_stale_and_symlinked_triggers(tmp_path: Path) -> None:
    runner = _load_runner()
    real = tmp_path / "real.trigger"
    real.write_text("20260519-010203\n", encoding="ascii")
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    os.utime(real, (stale.timestamp(), stale.timestamp()))
    with pytest.raises(runner.RestoreTriggerError, match="stale"):
        runner.load_timestamp(real)

    os.utime(real, None)
    link = tmp_path / "restore.trigger"
    link.symlink_to(real)
    with pytest.raises(runner.RestoreTriggerError, match="cannot open"):
        runner.load_timestamp(link)


def test_restore_runner_ignores_env_script_injection_and_uses_trusted_sibling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    trusted_dir = tmp_path / "trusted-release" / "scripts"
    trusted_dir.mkdir(parents=True)
    trusted_runner = trusted_dir / "restore_runner.py"
    trusted_script = trusted_dir / "restore.sh"
    trusted_runner.write_text("# runner marker\n", encoding="utf-8")
    trusted_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    trigger = tmp_path / "restore.trigger"
    trigger.write_text("20260519-010203\n", encoding="ascii")
    attacker = tmp_path / "attacker.sh"
    attacker.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")

    monkeypatch.setattr(runner, "__file__", str(trusted_runner))
    monkeypatch.setenv("LUMEN_RESTORE_SCRIPT", str(attacker))
    monkeypatch.setenv("BASH_ENV", str(attacker))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "python-inject"))
    captured: dict[str, object] = {}

    def fake_execve(
        executable: str,
        argv: list[str],
        env: dict[str, str],
    ) -> None:
        captured.update(executable=executable, argv=argv, env=env)

    monkeypatch.setattr(runner.os, "execve", fake_execve)

    assert runner.main([str(trigger)]) == 127
    assert captured["executable"] == "/bin/bash"
    assert captured["argv"] == [
        "/bin/bash",
        str(trusted_script.resolve()),
        "20260519-010203",
    ]
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "LUMEN_RESTORE_SCRIPT" not in child_env
    assert "BASH_ENV" not in child_env
    assert "PYTHONPATH" not in child_env
    assert child_env["PATH"].startswith("/usr/local/sbin:")


def test_restore_runner_unit_uses_fixed_interpreters_and_trigger_path() -> None:
    unit = (ROOT / "deploy/systemd/lumen-restore-runner.service").read_text(
        encoding="utf-8"
    )

    assert "EnvironmentFile=-/opt/lumen/shared/.env" in unit
    assert "LUMEN_RESTORE_SCRIPT" not in unit
    assert "/usr/bin/python3 -I /opt/lumen/current/scripts/restore_runner.py" in unit
    assert "/opt/lumendata/backup/.restore.trigger" in unit
    assert "/usr/bin/env timeout" not in unit
