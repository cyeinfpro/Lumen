from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
TS = "20260519-010203"


def _load_runner() -> ModuleType:
    path = ROOT / "scripts" / "restore_runner.py"
    spec = importlib.util.spec_from_file_location("lumen_restore_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage_trusted_runner(tmp_path: Path) -> tuple[Path, Path, Path]:
    trusted_dir = tmp_path / "trusted-release" / "scripts"
    trusted_dir.mkdir(parents=True)
    trusted_runner = trusted_dir / "restore_runner.py"
    trusted_script = trusted_dir / "restore.sh"
    trusted_helper = trusted_dir / "restore_journal.py"
    trusted_runner.write_text("# runner marker\n", encoding="utf-8")
    trusted_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    shutil.copy2(ROOT / "scripts" / "restore_journal.py", trusted_helper)
    return trusted_runner, trusted_script, trusted_helper


def _write_backup_pair(
    backup_root: Path,
    *,
    operation_id: str = "backup-op-1",
) -> dict[str, object]:
    pg_path = backup_root / "pg" / f"{TS}.pg.dump.gz"
    redis_path = backup_root / "redis" / f"{TS}.redis.tgz"
    marker_path = backup_root / f".backup-pair.{TS}.json"
    pg_path.parent.mkdir(parents=True)
    redis_path.parent.mkdir()
    pg_path.write_bytes(b"postgres-payload")
    redis_path.write_bytes(b"redis-payload")
    binding = {
        "backup_operation_id": operation_id,
        "backup_pair_marker": str(marker_path),
        "pg_backup_path": str(pg_path),
        "redis_backup_path": str(redis_path),
        "pg_backup_size": pg_path.stat().st_size,
        "redis_backup_size": redis_path.stat().st_size,
        "pg_backup_sha256": hashlib.sha256(pg_path.read_bytes()).hexdigest(),
        "redis_backup_sha256": hashlib.sha256(redis_path.read_bytes()).hexdigest(),
    }
    marker_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "operation_id": operation_id,
                "timestamp": TS,
                "pg": {
                    "name": pg_path.name,
                    "size": binding["pg_backup_size"],
                    "sha256": binding["pg_backup_sha256"],
                },
                "redis": {
                    "name": redis_path.name,
                    "size": binding["redis_backup_size"],
                    "sha256": binding["redis_backup_sha256"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return binding


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
    trusted_runner, trusted_script, _trusted_helper = _stage_trusted_runner(tmp_path)
    trigger = tmp_path / "restore.trigger"
    trigger.write_text(f"{TS}\n", encoding="ascii")
    journal = tmp_path / "restore-state" / "active.json"
    backup_root = tmp_path / "backup"
    binding = _write_backup_pair(backup_root)
    attacker = tmp_path / "attacker.sh"
    attacker.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")

    monkeypatch.setattr(runner, "__file__", str(trusted_runner))
    monkeypatch.setenv("LUMEN_RESTORE_SCRIPT", str(attacker))
    monkeypatch.setenv("BASH_ENV", str(attacker))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "python-inject"))
    monkeypatch.setenv("LUMEN_RESTORE_JOURNAL_FILE", str(journal))
    monkeypatch.setenv("BACKUP_ROOT", str(backup_root))
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
        TS,
    ]
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "LUMEN_RESTORE_SCRIPT" not in child_env
    assert "BASH_ENV" not in child_env
    assert "PYTHONPATH" not in child_env
    assert child_env["PATH"].startswith("/usr/local/sbin:")
    assert child_env["BACKUP_ROOT"] == str(backup_root)
    persisted = json.loads(journal.read_text(encoding="utf-8"))
    assert persisted["phase"] == "request_pending"
    assert persisted["timestamp"] == TS
    for field, value in binding.items():
        assert persisted[field] == value


def test_restore_runner_consumes_journal_without_api_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    trusted_runner, trusted_script, _trusted_helper = _stage_trusted_runner(tmp_path)
    journal = tmp_path / "restore-state" / "active.json"
    journal.parent.mkdir()
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "restore-recovery",
                "timestamp": "20260519-010203",
                "phase": "redis_applying",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(runner, "__file__", str(trusted_runner))
    monkeypatch.setenv("LUMEN_RESTORE_JOURNAL_FILE", str(journal))
    monkeypatch.setattr(
        runner.os,
        "execve",
        lambda executable, argv, env: captured.update(
            executable=executable,
            argv=argv,
            env=env,
        ),
    )

    assert runner.main([str(tmp_path / "missing.trigger")]) == 127
    assert captured["argv"] == [
        "/bin/bash",
        str(trusted_script.resolve()),
        "--recover-only",
    ]


def test_restore_runner_retries_durable_pending_request_without_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    trusted_runner, trusted_script, _trusted_helper = _stage_trusted_runner(tmp_path)
    journal = tmp_path / "restore-state" / "active.json"
    backup_root = tmp_path / "backup"
    binding = _write_backup_pair(backup_root)
    journal.parent.mkdir()
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "restore-pending",
                "timestamp": TS,
                "phase": "request_pending",
                **binding,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(runner, "__file__", str(trusted_runner))
    monkeypatch.setenv("LUMEN_RESTORE_JOURNAL_FILE", str(journal))
    monkeypatch.setenv("BACKUP_ROOT", str(backup_root))
    monkeypatch.setattr(
        runner.os,
        "execve",
        lambda executable, argv, env: captured.update(
            executable=executable,
            argv=argv,
            env=env,
        ),
    )

    assert runner.main([str(tmp_path / "missing.trigger")]) == 127
    assert captured["argv"] == [
        "/bin/bash",
        str(trusted_script.resolve()),
        TS,
    ]


def test_restore_runner_rejects_missing_pair_marker_before_pending_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _load_runner()
    trusted_runner, _trusted_script, _trusted_helper = _stage_trusted_runner(tmp_path)
    backup_root = tmp_path / "backup"
    binding = _write_backup_pair(backup_root)
    Path(str(binding["backup_pair_marker"])).unlink()
    trigger = tmp_path / "restore.trigger"
    trigger.write_text(f"{TS}\n", encoding="ascii")
    journal = tmp_path / "restore-state" / "active.json"
    monkeypatch.setattr(runner, "__file__", str(trusted_runner))
    monkeypatch.setenv("BACKUP_ROOT", str(backup_root))
    monkeypatch.setenv("LUMEN_RESTORE_JOURNAL_FILE", str(journal))
    monkeypatch.setattr(
        runner.os,
        "execve",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exec")),
    )

    assert runner.main([str(trigger)]) == 2

    assert "backup pair marker or payload is invalid" in capsys.readouterr().err
    assert not journal.exists()


def test_restore_runner_rejects_pending_pair_operation_identity_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _load_runner()
    trusted_runner, _trusted_script, _trusted_helper = _stage_trusted_runner(tmp_path)
    backup_root = tmp_path / "backup"
    binding = _write_backup_pair(backup_root)
    journal = tmp_path / "restore-state" / "active.json"
    journal.parent.mkdir()
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "restore-pending",
                "timestamp": TS,
                "phase": "request_pending",
                **binding,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    marker_path = Path(str(binding["backup_pair_marker"]))
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["operation_id"] = "backup-op-replaced"
    marker_path.write_text(json.dumps(marker) + "\n", encoding="utf-8")
    monkeypatch.setattr(runner, "__file__", str(trusted_runner))
    monkeypatch.setenv("BACKUP_ROOT", str(backup_root))
    monkeypatch.setenv("LUMEN_RESTORE_JOURNAL_FILE", str(journal))
    monkeypatch.setattr(
        runner.os,
        "execve",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exec")),
    )

    assert runner.main([str(tmp_path / "missing.trigger")]) == 2

    assert "no longer matches durable request" in capsys.readouterr().err
    assert journal.exists()


def test_restore_runner_unit_uses_fixed_interpreters_and_trigger_path() -> None:
    unit = (ROOT / "deploy/systemd/lumen-restore-runner.service").read_text(
        encoding="utf-8"
    )

    assert "EnvironmentFile=" not in unit
    assert "Environment=LUMEN_ENV_FILE=/opt/lumen/shared/.env" in unit
    assert "LUMEN_RESTORE_SCRIPT" not in unit
    assert "/usr/bin/python3 -I /opt/lumen/current/scripts/restore_runner.py" in unit
    assert "/opt/lumendata/backup/.restore.trigger" in unit
    assert "LUMEN_RESTORE_JOURNAL_FILE=/var/lib/lumen/restore/active.json" in unit
    assert "Restart=on-failure" in unit
    assert 'if [ "$SERVICE_RESULT" = success ]' in unit
    assert "restore request retained after service result=$SERVICE_RESULT" in unit
    assert "/usr/bin/env timeout" not in unit

    path_unit = (ROOT / "deploy/systemd/lumen-restore.path").read_text(encoding="utf-8")
    assert "PathExists=/var/lib/lumen/restore/active.json" in path_unit
