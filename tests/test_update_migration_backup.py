from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATE_MODULE_DIR = ROOT / "scripts" / "update"
UPDATE_SOURCE_RELATIVE = (
    "common.sh",
    "backup/restore_points.sh",
    "backup/migration_helpers.sh",
    "recovery/state.sh",
    "backup/preflight.sh",
    "backup/phases.sh",
)


def _update_source() -> str:
    return "\n".join(
        (UPDATE_MODULE_DIR / relative).read_text(encoding="utf-8")
        for relative in UPDATE_SOURCE_RELATIVE
    )


def _function_source(name: str) -> str:
    text = _update_source()
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text)
    assert match is not None, f"{name} not found"
    return match.group(0)


def _run_bash(
    script: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    helper = ROOT / "scripts" / "restore_journal.py"
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"export LUMEN_BACKUP_PAIR_HELPER={shlex.quote(str(helper))}\n{script}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _write_flock_mock(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import fcntl
import sys

operation = sys.argv[1]
descriptor = int(sys.argv[2])
try:
    if operation == "-n":
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif operation == "-u":
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    else:
        raise SystemExit(2)
except BlockingIOError:
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _pair_result(
    backup_root: Path,
    timestamp: str,
    *,
    operation_id: str = "backup-test-operation",
) -> dict[str, object]:
    pg = backup_root / "pg" / f"{timestamp}.pg.dump.gz"
    redis = backup_root / "redis" / f"{timestamp}.redis.tgz"
    pg_hash = hashlib.sha256(pg.read_bytes()).hexdigest()
    redis_hash = hashlib.sha256(redis.read_bytes()).hexdigest()
    marker = backup_root / f".backup-pair.{timestamp}.json"
    marker.write_text(
        json.dumps(
            {
                "schema": 1,
                "operation_id": operation_id,
                "timestamp": timestamp,
                "pg": {
                    "name": pg.name,
                    "size": pg.stat().st_size,
                    "sha256": pg_hash,
                },
                "redis": {
                    "name": redis.name,
                    "size": redis.stat().st_size,
                    "sha256": redis_hash,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "operation_id": operation_id,
        "pair_marker": str(marker),
        "pg_sha256": pg_hash,
        "pg_size": pg.stat().st_size,
        "redis_sha256": redis_hash,
        "redis_size": redis.stat().st_size,
        "timestamp": timestamp,
    }


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
        + json.dumps(_pair_result(backup_root, timestamp))
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
        lumen_update_file_sha256() {{
            python3 - "$1" <<'PY'
import hashlib
import sys
with open(sys.argv[1], "rb") as handle:
    print(hashlib.sha256(handle.read()).hexdigest())
PY
        }}
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


def test_unmarked_backup_pair_cannot_satisfy_update_preflight(
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
    result_payload = _pair_result(backup_root, timestamp)
    Path(str(result_payload["pair_marker"])).unlink()
    output = tmp_path / "backup.out"
    output.write_text(json.dumps(result_payload) + "\n", encoding="utf-8")
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
                "$(( $(date +%s) - 1 ))" \
                {shlex.quote(str(baseline))}; then
            exit 91
        fi
        test -z "$UPDATE_RESTORE_POINT_TIMESTAMP"
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout


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
        json.dumps(_pair_result(backup_root, timestamp))
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
        json.dumps(_pair_result(backup_root, timestamp))
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
    journal_capture = tmp_path / "update-journal-path"
    scripts_dir.mkdir()
    shared_env.write_text("", encoding="utf-8")
    backup_script = scripts_dir / "backup.sh"
    backup_script.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
. "${TEST_LIB:?}"
lumen_verify_borrowed_maintenance_lock "${LUMEN_DEPLOY_ROOT:?}"
timestamp="$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "${BACKUP_ROOT}/pg" "${BACKUP_ROOT}/redis"
pg="${BACKUP_ROOT}/pg/${timestamp}.pg.dump.gz"
redis="${BACKUP_ROOT}/redis/${timestamp}.redis.tgz"
    printf 'new-pg' > "${pg}"
    printf 'new-redis' > "${redis}"
    printf '%s\\n' "${LUMEN_BACKUP_JOURNAL_FILE:?}" > "${TEST_JOURNAL_CAPTURE:?}"
    python3 - \
        "${BACKUP_ROOT}" "${timestamp}" "${pg}" "${redis}" \
        "${LUMEN_BACKUP_OPERATION_ID:?}" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
timestamp = sys.argv[2]
pg = Path(sys.argv[3])
redis = Path(sys.argv[4])
operation_id = sys.argv[5]
pg_hash = hashlib.sha256(pg.read_bytes()).hexdigest()
redis_hash = hashlib.sha256(redis.read_bytes()).hexdigest()
marker = root / f".backup-pair.{timestamp}.json"
marker.write_text(
    json.dumps(
        dict(
            schema=1,
            operation_id=operation_id,
            timestamp=timestamp,
            pg=dict(name=pg.name, size=pg.stat().st_size, sha256=pg_hash),
            redis=dict(
                name=redis.name,
                size=redis.stat().st_size,
                sha256=redis_hash,
            ),
        )
    )
    + "\\n",
    encoding="utf-8",
)
print(
    json.dumps(
        dict(
            timestamp=timestamp,
            operation_id=operation_id,
            pair_marker=str(marker),
            pg_size=pg.stat().st_size,
            redis_size=redis.stat().st_size,
            pg_sha256=pg_hash,
            redis_sha256=redis_hash,
        )
    )
)
PY
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
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_flock_mock(fakebin / "flock")
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}{os.pathsep}{env['PATH']}"

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(ROOT / "scripts" / "lib.sh"))}
        {functions}
        LUMEN_DEPLOY_ROOT={shlex.quote(str(tmp_path / "deploy"))}
        mkdir -p "$LUMEN_DEPLOY_ROOT"
        export LUMEN_DEPLOY_ROOT
        lumen_acquire_lock "$LUMEN_DEPLOY_ROOT" update-preflight-test
        test "$LUMEN_LOCK_KIND" = "flock"
        SCRIPT_DIR={shlex.quote(str(scripts_dir))}
        CURRENT_RELEASE=""
        UPDATE_LOG_DIR={shlex.quote(str(backup_root))}
        OPERATION_ID=test-operation
        SHARED_ENV={shlex.quote(str(shared_env))}
        TEST_JOURNAL_CAPTURE={shlex.quote(str(journal_capture))}
        TEST_LIB={shlex.quote(str(ROOT / "scripts" / "lib.sh"))}
        export TEST_JOURNAL_CAPTURE TEST_LIB
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_RESTORE_POINT_PG=""
        UPDATE_RESTORE_POINT_REDIS=""
        UPDATE_RESTORE_POINT_PG_SIZE=""
        UPDATE_RESTORE_POINT_REDIS_SIZE=""
        lumen_update_file_sha256() {{
            python3 - "$1" <<'PY'
import hashlib
import sys
with open(sys.argv[1], "rb") as handle:
    print(hashlib.sha256(handle.read()).hexdigest())
PY
        }}
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
        """,
        env=env,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "EMIT:backup_preflight:restore_point:" in result.stdout
    update_journal = journal_capture.read_text(encoding="utf-8").strip()
    assert update_journal == str(
        shared_env.parent / ".backup-recovery" / "test-operation.json"
    )
    service = (
        ROOT / "deploy" / "systemd" / "lumen-backup.service"
    ).read_text(encoding="utf-8")
    assert (
        "LUMEN_BACKUP_JOURNAL_FILE=/opt/lumendata/backup/.recovery/backup.json"
        in service
    )
    assert update_journal != "/opt/lumendata/backup/.recovery/backup.json"


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

    text = _update_source()
    migrate_start = text.index("# Phase: migrate_db")
    guard_index = text.index(
        "elif ! guard_migration_restore_point; then", migrate_start
    )
    stop_index = text.index('lumen_compose_in "${NEW_RELEASE}" stop -t', migrate_start)
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


def test_automatic_app_rollback_requires_matching_schema_heads(
    tmp_path: Path,
) -> None:
    old_release = tmp_path / "old"
    new_release = tmp_path / "new"
    old_release.mkdir()
    new_release.mkdir()
    (old_release / ".image-tag").write_text("v1.2.83\n", encoding="utf-8")
    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("guard_automatic_app_rollback_compatibility")}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        target_alembic_head() {{
            test "${{LUMEN_IMAGE_TAG:?}}" = v1.2.83
            printf '%s\\n' "${{ROLLBACK_HEAD:?}}"
        }}
        current_alembic_revision() {{
            test "${{LUMEN_IMAGE_TAG:?}}" = v1.2.84
            printf '%s\\n' "${{DATABASE_HEAD:?}}"
        }}
        UPDATE_MIGRATION_STARTED=1
        UPDATE_MIGRATION_VERIFIED=1
        UPDATE_MIGRATION_HEAD=0052_task_execution_epoch
        PREVIOUS_TAG=ignored-by-release-anchor
        TARGET_TAG=v1.2.84
        ROLLBACK_HEAD=0050_outbox_claim_v2
        DATABASE_HEAD=0052_task_execution_epoch
        if guard_automatic_app_rollback_compatibility \
                {shlex.quote(str(old_release))} \
                {shlex.quote(str(new_release))}; then
            exit 91
        fi
        ROLLBACK_HEAD=0052_task_execution_epoch
        guard_automatic_app_rollback_compatibility \
            {shlex.quote(str(old_release))} \
            {shlex.quote(str(new_release))}
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "拒绝自动应用回滚" in result.stderr


def test_automatic_app_rollback_fails_closed_when_head_is_unknown(
    tmp_path: Path,
) -> None:
    old_release = tmp_path / "old"
    new_release = tmp_path / "new"
    old_release.mkdir()
    new_release.mkdir()
    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("guard_automatic_app_rollback_compatibility")}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        target_alembic_head() {{ printf ''; }}
        current_alembic_revision() {{ printf '0052_task_execution_epoch\\n'; }}
        UPDATE_MIGRATION_STARTED=1
        UPDATE_MIGRATION_VERIFIED=0
        UPDATE_MIGRATION_HEAD=""
        PREVIOUS_TAG=v1.2.83
        TARGET_TAG=v1.2.84
        if guard_automatic_app_rollback_compatibility \
                {shlex.quote(str(old_release))} \
                {shlex.quote(str(new_release))}; then
            exit 92
        fi
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "兼容性无法验证" in result.stderr


def test_automatic_app_rollback_skips_schema_probe_before_migration() -> None:
    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("guard_automatic_app_rollback_compatibility")}
        log_error() {{ exit 90; }}
        target_alembic_head() {{ exit 91; }}
        current_alembic_revision() {{ exit 92; }}
        UPDATE_MIGRATION_STARTED=0
        UPDATE_MIGRATION_VERIFIED=0
        UPDATE_MIGRATION_HEAD=""
        guard_automatic_app_rollback_compatibility "" ""
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_precommit_restore_has_no_side_effects_when_schema_guard_rejects(
    tmp_path: Path,
) -> None:
    side_effect = tmp_path / "side-effect"
    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("restore_uncommitted_update_state")}
        guard_automatic_app_rollback_compatibility() {{ return 1; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        restore_update_symlink_snapshot() {{ : > {shlex.quote(str(side_effect))}; }}
        restore_update_env_snapshot() {{ : > {shlex.quote(str(side_effect))}; }}
        lumen_restore_operations_host_artifacts() {{
            : > {shlex.quote(str(side_effect))}
        }}
        lumen_compose_in() {{ : > {shlex.quote(str(side_effect))}; }}
        lumen_release_remove_unused() {{ : > {shlex.quote(str(side_effect))}; }}
        discard_update_state_snapshot() {{ : > {shlex.quote(str(side_effect))}; }}
        ROOT={shlex.quote(str(tmp_path))}
        CURRENT_ID=old
        NEW_RELEASE={shlex.quote(str(tmp_path / "new"))}
        NEW_ID=new
        UPDATE_ENV_SNAPSHOT=""
        UPDATE_HOST_ARTIFACT_SNAPSHOT=""
        UPDATE_RELEASE_SWITCHED=1
        UPDATE_OLD_SERVICES_STOPPED=1
        if restore_uncommitted_update_state; then
            exit 91
        fi
        test ! -e {shlex.quote(str(side_effect))}
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "schema capability guard" in result.stderr


def test_resume_revalidates_bound_restore_point_before_any_phase(tmp_path: Path) -> None:
    completed = tmp_path / "completed"
    result = _run_bash(
        f"""
        set -euo pipefail
        {_function_source("validate_resumed_update_state")}
        lumen_update_journal_validate_resume() {{ return 0; }}
        validate_bound_update_restore_point() {{ return 1; }}
        lumen_configure_proxy_env() {{ return 1; }}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        UPDATE_RESTORE_POINT_TIMESTAMP=20260803-010203
        UPDATE_MIGRATION_VERIFIED=0
        UPDATE_MIGRATION_HEAD=""
        NEW_RELEASE=""
        SHARED_ENV={shlex.quote(str(tmp_path / "shared.env"))}
        if validate_resumed_update_state; then
            : > {shlex.quote(str(completed))}
        fi
        test ! -e {shlex.quote(str(completed))}
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_bound_restore_point_revalidation_rejects_same_size_tampering(
    tmp_path: Path,
) -> None:
    timestamp = "20260803-010203"
    backup_root = tmp_path / "backup"
    pg = backup_root / "pg" / f"{timestamp}.pg.dump.gz"
    redis = backup_root / "redis" / f"{timestamp}.redis.tgz"
    pg.parent.mkdir(parents=True)
    redis.parent.mkdir(parents=True)
    with gzip.open(pg, "wb") as handle:
        handle.write(b"postgres-restore-point")
    with tarfile.open(redis, "w:gz") as archive:
        payload = b"redis-restore-point"
        info = tarfile.TarInfo("dump.rdb")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    pg_hash = hashlib.sha256(pg.read_bytes()).hexdigest()
    redis_hash = hashlib.sha256(redis.read_bytes()).hexdigest()
    _pair_result(backup_root, timestamp)
    script = f"""
        set -euo pipefail
        {_function_source("validate_bound_update_restore_point")}
        LUMEN_BACKUP_ROOT={shlex.quote(str(backup_root))}
        UPDATE_RESTORE_POINT_TIMESTAMP={timestamp}
        UPDATE_RESTORE_POINT_PG={shlex.quote(str(pg))}
        UPDATE_RESTORE_POINT_REDIS={shlex.quote(str(redis))}
        UPDATE_RESTORE_POINT_PG_SIZE={pg.stat().st_size}
        UPDATE_RESTORE_POINT_REDIS_SIZE={redis.stat().st_size}
        UPDATE_RESTORE_POINT_PG_SHA256={pg_hash}
        UPDATE_RESTORE_POINT_REDIS_SHA256={redis_hash}
        validate_bound_update_restore_point
    """
    valid = _run_bash(script)
    assert valid.returncode == 0, valid.stderr + valid.stdout

    tampered = bytearray(pg.read_bytes())
    tampered[len(tampered) // 2] ^= 0x01
    pg.write_bytes(tampered)
    rejected = _run_bash(script)
    assert rejected.returncode != 0


def test_update_rollback_masks_second_signal_until_restore_finishes(
    tmp_path: Path,
) -> None:
    completed = tmp_path / "completed"
    result = _run_bash(
        f"""
        set -u
        {_function_source("on_err")}
        log_error() {{ :; }}
        discard_release_source_manifest_cache() {{ :; }}
        lumen_step_finalize_failure() {{ :; }}
        log_update_restore_boundary() {{ :; }}
        lumen_update_journal_status() {{ :; }}
        lumen_update_journal_failed() {{ :; }}
        restore_uncommitted_update_state() {{
            kill -TERM "$$"
            : > {shlex.quote(str(completed))}
        }}
        trap 'exit 143' TERM
        UPDATE_ERROR_HANDLED=0
        UPDATE_RESTORE_POINT_TIMESTAMP=""
        UPDATE_MIGRATION_STARTED=0
        ROLLBACK_DONE=0
        UPDATE_STATE_COMMITTED=0
        UPDATE_STATE_COMMIT_UNKNOWN=0
        UPDATE_STATE_SNAPSHOT_READY=1
        UPDATE_SNAPSHOT_LINKS_KNOWN=1
        _UPDATE_LAST_PHASE=test
        on_err 1
        """
    )

    assert result.returncode != 143, result.stderr + result.stdout
    assert completed.exists()
