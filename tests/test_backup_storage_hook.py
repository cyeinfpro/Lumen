"""Tests for OPT-10 storage backup hook, media watermark receipt, and restore drift warning."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKUP_JOURNAL_SH = ROOT / "scripts" / "lib" / "backup_journal.sh"


def _load_restore_runner() -> ModuleType:
    path = ROOT / "scripts" / "restore_runner.py"
    spec = importlib.util.spec_from_file_location("lumen_restore_runner_opt10", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_journal_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_storage_watermark_counts_regular_files_and_skips_quarantine(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    (storage_root / "u" / "user-1" / "uploads").mkdir(parents=True)
    (storage_root / "u" / "user-1" / "uploads" / "a.png").write_bytes(b"12345")
    (storage_root / "u" / "user-1" / "uploads" / "b.webp").write_bytes(b"1234567")
    (storage_root / ".quarantine" / "u" / "user-1").mkdir(parents=True)
    (storage_root / ".quarantine" / "u" / "user-1" / "old.png").write_bytes(b"ignored")

    res = _run_journal_bash(
        f"""
        set -euo pipefail
        BACKUP_ROOT={shlex.quote(str(tmp_path / "backup"))}
        SCRIPT_DIR={shlex.quote(str(ROOT / "scripts"))}
        log() {{ printf '%s\\n' "$*" >&2; }}
        . {shlex.quote(str(BACKUP_JOURNAL_SH))}
        lumen_backup_capture_storage_watermark {shlex.quote(str(storage_root))}
        """
    )
    assert res.returncode == 0, res.stderr + res.stdout
    payload = json.loads(res.stdout.strip())
    assert payload == {
        "exists": True,
        "file_count": 2,
        "root": str(storage_root),
        "total_bytes": 12,
    }


def test_storage_backup_hook_executes_safe_script_and_rejects_unsafe_paths(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    storage_root = tmp_path / "storage"
    backup_root.mkdir()
    storage_root.mkdir()
    hook_out = tmp_path / "hook-env.json"
    safe_hook = tmp_path / "media-hook.sh"
    safe_hook.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY' > {shlex.quote(str(hook_out))}
import json, os
print(json.dumps({{
    "ts": os.environ["LUMEN_BACKUP_TIMESTAMP"],
    "op": os.environ["LUMEN_BACKUP_OPERATION_ID"],
    "storage": os.environ["LUMEN_STORAGE_ROOT"],
    "backup": os.environ["LUMEN_BACKUP_ROOT"],
}}))
PY
""",
        encoding="utf-8",
    )
    safe_hook.chmod(0o750)

    ok = _run_journal_bash(
        f"""
        set -euo pipefail
        BACKUP_ROOT={shlex.quote(str(backup_root))}
        STORAGE_ROOT={shlex.quote(str(storage_root))}
        SCRIPT_DIR={shlex.quote(str(ROOT / "scripts"))}
        TS=20261118-020000
        BACKUP_OPERATION_ID=op-hook-1
        LUMEN_STORAGE_BACKUP_HOOK={shlex.quote(str(safe_hook))}
        log() {{ printf '%s\\n' "$*"; }}
        . {shlex.quote(str(BACKUP_JOURNAL_SH))}
        lumen_backup_run_storage_hook
        printf 'STATUS=%s RC=%s\\n' "$BACKUP_STORAGE_HOOK_STATUS" "$BACKUP_STORAGE_HOOK_RC"
        """
    )
    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert "STATUS=succeeded RC=0" in ok.stdout
    captured = json.loads(hook_out.read_text(encoding="utf-8"))
    assert captured == {
        "ts": "20261118-020000",
        "op": "op-hook-1",
        "storage": str(storage_root),
        "backup": str(backup_root),
    }

    # World-writable hook must be rejected as unsafe; required=1 makes it fail closed.
    unsafe_hook = tmp_path / "world-writable-hook.sh"
    unsafe_hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    unsafe_hook.chmod(0o777)
    rejected = _run_journal_bash(
        f"""
        set -euo pipefail
        BACKUP_ROOT={shlex.quote(str(backup_root))}
        STORAGE_ROOT={shlex.quote(str(storage_root))}
        SCRIPT_DIR={shlex.quote(str(ROOT / "scripts"))}
        LUMEN_STORAGE_BACKUP_HOOK={shlex.quote(str(unsafe_hook))}
        LUMEN_STORAGE_BACKUP_HOOK_REQUIRED=1
        log() {{ printf '%s\\n' "$*"; }}
        . {shlex.quote(str(BACKUP_JOURNAL_SH))}
        lumen_backup_run_storage_hook
        """
    )
    assert rejected.returncode != 0
    assert "rejected_unsafe" in rejected.stdout


def test_restore_runner_warns_when_storage_watermark_drifts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _load_restore_runner()
    data_root = tmp_path / "lumendata"
    backup_root = data_root / "backup"
    storage_root = data_root / "storage"
    backup_root.mkdir(parents=True)
    (storage_root / "u" / "u1").mkdir(parents=True)
    (storage_root / "u" / "u1" / "img1.jpg").write_bytes(b"1234567890")

    ts = "20261118-010203"
    (backup_root / ".backup.last-success.json").write_text(
        json.dumps(
            {
                "completed_at": ts,
                "operation_id": "backup-1",
                "pair_marker": f".backup-pair.{ts}.json",
                "storage_file_count": 1,
                "storage_total_bytes": 10,
                "storage_watermark": {
                    "exists": True,
                    "file_count": 1,
                    "root": str(storage_root),
                    "total_bytes": 10,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Matching watermark -> no warning
    aligned = runner.check_storage_watermark_drift(
        backup_root,
        ts,
        {"STORAGE_ROOT": str(storage_root)},
    )
    assert aligned is not None and aligned["drift"] is False
    assert capsys.readouterr().err == ""

    # Add an extra file after backup -> drift warning emitted
    (storage_root / "u" / "u1" / "img2.jpg").write_bytes(b"extra")
    drifted = runner.check_storage_watermark_drift(
        backup_root,
        ts,
        {"STORAGE_ROOT": str(storage_root)},
    )
    err = capsys.readouterr().err
    assert drifted is not None and drifted["drift"] is True
    assert drifted["delta_files"] == 1
    assert drifted["delta_bytes"] == 5
    assert "WARN: storage media watermark drift detected" in err
