from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATE = ROOT / "scripts" / "update.sh"


def _function_source(name: str) -> str:
    text = UPDATE.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text)
    assert match is not None, f"{name} not found"
    return match.group(0)


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_current_backup_pair_is_recorded_as_the_round_restore_point(
    tmp_path: Path,
) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = tmp_path / "backup"
    pg = backup_root / "pg" / f"{timestamp}.pg.dump.gz"
    redis = backup_root / "redis" / f"{timestamp}.redis.tgz"
    pg.parent.mkdir(parents=True)
    redis.parent.mkdir(parents=True)
    pg.write_bytes(b"current-pg")
    redis.write_bytes(b"current-redis")
    output = tmp_path / "backup.out"
    output.write_text(
        "backup log\n"
        + json.dumps(
            {
                "timestamp": timestamp,
                "pg_size": pg.stat().st_size,
                "redis_size": redis.stat().st_size,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    started_epoch = int(time.time()) - 1

    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("verify_update_restore_point")}
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_RESTORE_POINT_PG=""
        UPDATE_RESTORE_POINT_REDIS=""
        UPDATE_RESTORE_POINT_PG_SIZE=""
        UPDATE_RESTORE_POINT_REDIS_SIZE=""
        verify_update_restore_point \
            {shlex.quote(str(output))} \
            {shlex.quote(str(backup_root))} \
            {started_epoch} \
            {shlex.quote(str(baseline))}
        printf 'timestamp=%s\\npg=%s\\nredis=%s\\n' \
            "$UPDATE_RESTORE_POINT_TIMESTAMP" \
            "$UPDATE_RESTORE_POINT_PG" \
            "$UPDATE_RESTORE_POINT_REDIS"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert f"timestamp={timestamp}" in result.stdout
    assert f"pg={pg}" in result.stdout
    assert f"redis={redis}" in result.stdout


def test_stale_manual_backup_cannot_satisfy_the_current_update_round(
    tmp_path: Path,
) -> None:
    timestamp = "20260101-000000"
    backup_root = tmp_path / "backup"
    pg = backup_root / "pg" / f"{timestamp}.pg.dump.gz"
    redis = backup_root / "redis" / f"{timestamp}.redis.tgz"
    pg.parent.mkdir(parents=True)
    redis.parent.mkdir(parents=True)
    pg.write_bytes(b"old-pg")
    redis.write_bytes(b"old-redis")
    output = tmp_path / "backup.out"
    output.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "pg_size": pg.stat().st_size,
                "redis_size": redis.stat().st_size,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")

    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("verify_update_restore_point")}
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_RESTORE_POINT_PG=""
        UPDATE_RESTORE_POINT_REDIS=""
        UPDATE_RESTORE_POINT_PG_SIZE=""
        UPDATE_RESTORE_POINT_REDIS_SIZE=""
        if verify_update_restore_point \
                {shlex.quote(str(output))} \
                {shlex.quote(str(backup_root))} \
                "$(date +%s)" \
                {shlex.quote(str(baseline))}; then
            exit 91
        fi
        test -z "$UPDATE_RESTORE_POINT_TIMESTAMP"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_unchanged_current_timestamp_files_cannot_masquerade_as_new_backup(
    tmp_path: Path,
) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = tmp_path / "backup"
    pg = backup_root / "pg" / f"{timestamp}.pg.dump.gz"
    redis = backup_root / "redis" / f"{timestamp}.redis.tgz"
    pg.parent.mkdir(parents=True)
    redis.parent.mkdir(parents=True)
    pg.write_bytes(b"manual-pg")
    redis.write_bytes(b"manual-redis")
    output = tmp_path / "backup.out"
    output.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "pg_size": pg.stat().st_size,
                "redis_size": redis.stat().st_size,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.json"

    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("snapshot_update_backup_files")}
        {_function_source("verify_update_restore_point")}
        snapshot_update_backup_files \
            {shlex.quote(str(backup_root))} \
            {shlex.quote(str(baseline))}
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_RESTORE_POINT_PG=""
        UPDATE_RESTORE_POINT_REDIS=""
        UPDATE_RESTORE_POINT_PG_SIZE=""
        UPDATE_RESTORE_POINT_REDIS_SIZE=""
        if verify_update_restore_point \
                {shlex.quote(str(output))} \
                {shlex.quote(str(backup_root))} \
                "$(( $(date +%s) - 1 ))" \
                {shlex.quote(str(baseline))}; then
            exit 92
        fi
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_backup_preflight_pipeline_creates_and_verifies_restore_point(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    backup_root = tmp_path / "backup"
    shared_env = tmp_path / "shared.env"
    scripts_dir.mkdir()
    shared_env.write_text("", encoding="utf-8")
    backup_script = scripts_dir / "backup.sh"
    backup_script.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
timestamp="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "${BACKUP_ROOT}/pg" "${BACKUP_ROOT}/redis"
pg="${BACKUP_ROOT}/pg/${timestamp}.pg.dump.gz"
redis="${BACKUP_ROOT}/redis/${timestamp}.redis.tgz"
printf 'new-pg' > "${pg}"
printf 'new-redis' > "${redis}"
printf '{"timestamp":"%s","pg_size":%s,"redis_size":%s}\\n' \
    "${timestamp}" "$(wc -c < "${pg}")" "$(wc -c < "${redis}")"
""",
        encoding="utf-8",
    )
    backup_script.chmod(0o755)
    functions = "\n".join(
        _function_source(name)
        for name in (
            "snapshot_update_backup_files",
            "verify_update_restore_point",
            "run_update_backup_preflight",
        )
    )

    result = _run_bash(
        f"""
        set -euo pipefail
        {functions}
        SCRIPT_DIR={shlex.quote(str(scripts_dir))}
        CURRENT_RELEASE=""
        UPDATE_LOG_DIR={shlex.quote(str(backup_root))}
        OPERATION_ID=test-operation
        SHARED_ENV={shlex.quote(str(shared_env))}
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_RESTORE_POINT_PG=""
        UPDATE_RESTORE_POINT_REDIS=""
        UPDATE_RESTORE_POINT_PG_SIZE=""
        UPDATE_RESTORE_POINT_REDIS_SIZE=""
        lumen_env_value() {{ printf ''; }}
        log_info() {{ :; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        emit_info() {{ printf 'EMIT:%s:%s:%s\\n' "$1" "$2" "$3"; }}
        run_update_backup_preflight
        test -f "$UPDATE_RESTORE_POINT_PG"
        test -f "$UPDATE_RESTORE_POINT_REDIS"
        test -n "$UPDATE_RESTORE_POINT_TIMESTAMP"
        test -z "$(find {shlex.quote(str(backup_root))} -maxdepth 1 \
            -name '.update-backup*' -print -quit)"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "EMIT:backup_preflight:restore_point:" in result.stdout


def test_noninteractive_fast_update_requires_restore_point_before_stop() -> None:
    functions = "\n".join(
        _function_source(name)
        for name in (
            "lumen_env_truthy",
            "update_requires_migration_restore_point",
            "guard_migration_restore_point",
        )
    )
    result = _run_bash(
        f"""
        set -euo pipefail
        {functions}
        log_info() {{ :; }}
        log_warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        emit_info() {{ :; }}
        emit_warn() {{ printf 'EMIT:%s:%s\\n' "$1" "$2"; }}
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_RESTORE_POINT_PG=""
        UPDATE_RESTORE_POINT_REDIS=""
        LUMEN_UPDATE_NONINTERACTIVE=1
        unset LUMEN_UPDATE_REQUIRE_MIGRATION_BACKUP
        unset LUMEN_UPDATE_SKIP_BACKUP
        rc=0
        guard_migration_restore_point || rc=$?
        printf 'rc=%s\\n' "$rc"
        test "$rc" -eq 1
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "rc=1" in result.stdout
    assert "missing_required_restore_point" in result.stdout
    assert "拒绝停止旧服务或执行 Alembic" in result.stderr

    text = UPDATE.read_text(encoding="utf-8")
    migrate_start = text.index("# Phase: migrate_db")
    guard_index = text.index("elif ! guard_migration_restore_point; then", migrate_start)
    stop_index = text.index(
        'lumen_compose_in "${NEW_RELEASE}" stop -t', migrate_start
    )
    run_index = text.index("UPDATE_MIGRATION_STARTED=1", migrate_start)
    assert guard_index < stop_index < run_index


def test_interactive_fast_override_remains_explicit_and_warned() -> None:
    functions = "\n".join(
        _function_source(name)
        for name in (
            "lumen_env_truthy",
            "update_requires_migration_restore_point",
            "guard_migration_restore_point",
        )
    )
    result = _run_bash(
        f"""
        set -euo pipefail
        {functions}
        log_info() {{ :; }}
        log_warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        emit_info() {{ :; }}
        emit_warn() {{ printf 'EMIT:%s:%s\\n' "$1" "$2"; }}
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_RESTORE_POINT_PG=""
        UPDATE_RESTORE_POINT_REDIS=""
        unset LUMEN_UPDATE_NONINTERACTIVE
        unset LUMEN_UPDATE_REQUIRE_MIGRATION_BACKUP
        unset LUMEN_UPDATE_SKIP_BACKUP
        guard_migration_restore_point
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "missing_restore_point_explicit_override" in result.stdout
    assert "按显式 fast/skip 语义继续" in result.stderr


def test_failure_log_names_restore_point_and_database_rollback_boundary() -> None:
    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("log_update_restore_boundary")}
        log_warn() {{ printf 'WARN:%s\\n' "$*" >&2; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        UPDATE_RESTORE_BOUNDARY_LOGGED=0
        UPDATE_RESTORE_POINT_TIMESTAMP=20260718-010203
        UPDATE_RESTORE_POINT_PG=/backup/pg/20260718-010203.pg.dump.gz
        UPDATE_RESTORE_POINT_REDIS=/backup/redis/20260718-010203.redis.tgz
        UPDATE_MIGRATION_STARTED=1
        UPDATE_MIGRATION_VERIFIED=0
        log_update_restore_boundary migrate_db
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "本轮恢复点：timestamp=20260718-010203" in result.stderr
    assert "数据库可能已部分变更" in result.stderr
    assert "自动回滚仅覆盖 release/env/服务" in result.stderr
