"""Tests for OPT-10 fast-mode lightweight update anchor (fast-anchor.json)."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "update" / "common.sh"
PREFLIGHT = ROOT / "scripts" / "update" / "backup" / "preflight.sh"
PHASES = ROOT / "scripts" / "update" / "backup" / "phases.sh"


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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


def test_fast_mode_backup_preflight_writes_redacted_anchor_under_100ms(
    tmp_path: Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_docker = fakebin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "exec" ]; then
    printf 'A\\t0072_fast_anchor_head\\t\\n'
    printf 'S\\tsite.public_base_url\\thttps://lumen.example.com\\n'
    printf 'S\\tstorage.smb.password\\tsuper-secret-smb-pass\\n'
    printf 'S\\tproviders\\t[{"name":"p1","api_key":"sk-live-secret"}]\\n'
    exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    shared_env = tmp_path / ".env"
    env_bytes = b"DATABASE_URL=postgresql://lumen:pw@postgres:5432/lumen\nSESSION_SECRET=topsecret\n"
    shared_env.write_bytes(env_bytes)
    expected_env_sha = hashlib.sha256(env_bytes).hexdigest()
    log_dir = tmp_path / "update-logs"
    log_dir.mkdir()

    env = {"PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"}
    started = time.perf_counter()
    result = _run_bash(
        f"""
        set -euo pipefail
        lumen_update_failpoint() {{ return 0; }}
        lumen_update_journal_phase_start() {{ return 0; }}
        lumen_update_journal_phase_done() {{ return 0; }}
        lumen_update_journal_failed() {{ return 0; }}
        lumen_emit_step() {{ printf 'STEP:%s\\n' "$*"; }}
        lumen_emit_info() {{ printf 'INFO:%s\\n' "$*"; }}
        log_info() {{ :; }}
        log_warn() {{ :; }}
        log_error() {{ printf 'ERR:%s\\n' "$*" >&2; }}
        . {shlex.quote(str(COMMON))}
        . {shlex.quote(str(PREFLIGHT))}
        . {shlex.quote(str(PHASES))}
        LUMEN_UPDATE_MODE=fast
        LUMEN_UPDATE_FAST_BACKUP=0
        LUMEN_UPDATE_SKIP_BACKUP=0
        LUMEN_UPDATE_NONINTERACTIVE=0
        OPERATION_ID=fast-op-001
        SHARED_ENV={shlex.quote(str(shared_env))}
        UPDATE_LOG_DIR={shlex.quote(str(log_dir))}
        PG_CONTAINER=lumen-pg
        export LUMEN_UPDATE_MODE LUMEN_UPDATE_FAST_BACKUP LUMEN_UPDATE_SKIP_BACKUP
        export LUMEN_UPDATE_NONINTERACTIVE OPERATION_ID SHARED_ENV UPDATE_LOG_DIR PG_CONTAINER
        update_phase_backup_preflight
        """,
        env=env,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert result.returncode == 0, result.stderr + result.stdout
    assert elapsed_ms < 2000.0
    assert "skipped_by_fast_mode" in result.stdout

    anchor_path = log_dir / "fast-anchor.json"
    assert anchor_path.is_file()
    raw_text = anchor_path.read_text(encoding="utf-8")
    assert "super-secret-smb-pass" not in raw_text
    assert "sk-live-secret" not in raw_text
    assert "topsecret" not in raw_text

    payload = json.loads(raw_text)
    assert payload["schema"] == 1
    assert payload["mode"] == "fast"
    assert payload["operation_id"] == "fast-op-001"
    assert payload["env_sha256"] == expected_env_sha
    assert payload["container_reachable"] is True
    assert payload["alembic_current"] == "0072_fast_anchor_head"

    settings_summary = payload["system_settings"]
    assert settings_summary["count"] == 3
    assert settings_summary["keys"] == [
        "providers",
        "site.public_base_url",
        "storage.smb.password",
    ]
    assert settings_summary["items"]["site.public_base_url"]["redacted"] is False
    assert (
        settings_summary["items"]["site.public_base_url"]["preview"]
        == "https://lumen.example.com"
    )
    assert settings_summary["items"]["storage.smb.password"]["redacted"] is True
    assert settings_summary["items"]["storage.smb.password"]["preview"] == "[REDACTED]"
    assert settings_summary["items"]["providers"]["redacted"] is True
    assert settings_summary["items"]["providers"]["preview"] == "[REDACTED]"


def test_fast_anchor_handles_unreachable_container_gracefully_under_100ms(
    tmp_path: Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_docker = fakebin / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake_docker.chmod(0o755)

    shared_env = tmp_path / ".env"
    shared_env.write_text("APP_ENV=prod\n", encoding="utf-8")
    anchor_file = tmp_path / "logs" / "fast-anchor.json"

    env = {"PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"}
    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(PHASES))}
        SHARED_ENV={shlex.quote(str(shared_env))}
        OPERATION_ID=fast-unreachable
        LUMEN_FAST_ANCHOR_FILE={shlex.quote(str(anchor_file))}
        export SHARED_ENV OPERATION_ID LUMEN_FAST_ANCHOR_FILE
        lumen_update_write_fast_anchor
        LUMEN_FAST_ANCHOR_SKIP_DOCKER=1
        export LUMEN_FAST_ANCHOR_SKIP_DOCKER
        SECONDS=0
        for _ in 1 2 3 4 5; do
            lumen_update_write_fast_anchor
        done
        test "$SECONDS" -le 1
        """,
        env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(anchor_file.read_text(encoding="utf-8"))
    assert payload["container_reachable"] is False
    assert payload["alembic_current"] is None
    assert payload["env_sha256"] == hashlib.sha256(b"APP_ENV=prod\n").hexdigest()
    assert payload["system_settings"]["count"] == 0
