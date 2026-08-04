from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "restore_journal.py"
SPEC = importlib.util.spec_from_file_location("restore_journal", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RESTORE_JOURNAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESTORE_JOURNAL)
TS = "20260803-010203"
PG_HASH = "1" * 64
REDIS_HASH = "2" * 64


def _binding_arguments() -> list[str]:
    return [
        "--backup-operation-id",
        "backup-op-1",
        "--backup-pair-marker",
        f"/opt/lumendata/backup/.backup-pair.{TS}.json",
        "--pg-backup-path",
        f"/opt/lumendata/backup/pg/{TS}.pg.dump.gz",
        "--redis-backup-path",
        f"/opt/lumendata/backup/redis/{TS}.redis.tgz",
        "--pg-backup-size",
        "123",
        "--redis-backup-size",
        "456",
        "--pg-backup-sha256",
        PG_HASH,
        "--redis-backup-sha256",
        REDIS_HASH,
    ]


def _write_command(
    path: Path,
    *,
    phase: str = "redis_applying",
    redis_state: str = "applying",
    pg_swap_in_progress: str = "0",
    pg_promoted: str = "0",
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "write",
        str(path),
        "--operation-id",
        "restore-op-1",
        "--timestamp",
        TS,
        "--phase",
        phase,
        "--pg-db",
        "lumen",
        "--pg-container",
        "lumen-pg",
        "--redis-container",
        "lumen-redis",
        "--pg-temp-db",
        "lumen_restore_20260803010203_1",
        "--redis-host-dir",
        "/var/lib/docker/volumes/lumen-redis/_data",
        "--redis-backup-dir",
        "/var/lib/docker/volumes/lumen-redis/_data/.lumen-restore-old.1",
        "--redis-original-manifest",
        (
            "/var/lib/docker/volumes/lumen-redis/_data/"
            ".lumen-restore-old.1/.original-items"
        ),
        "--redis-state",
        redis_state,
        *_binding_arguments(),
        "--services-stopped",
        "1",
        "--redis-needs-start",
        "1",
        "--pg-swap-in-progress",
        pg_swap_in_progress,
        "--pg-promoted",
        pg_promoted,
        "--service",
        "api",
        "--service",
        "worker",
        "--service",
        "tgbot",
    ]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_request_command(path: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "request-write",
        str(path),
        "--operation-id",
        "restore-request-1",
        "--timestamp",
        TS,
        *_binding_arguments(),
    ]


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
    pg_hash = hashlib.sha256(pg_path.read_bytes()).hexdigest()
    redis_hash = hashlib.sha256(redis_path.read_bytes()).hexdigest()
    marker_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "operation_id": operation_id,
                "timestamp": TS,
                "pg": {
                    "name": pg_path.name,
                    "size": pg_path.stat().st_size,
                    "sha256": pg_hash,
                },
                "redis": {
                    "name": redis_path.name,
                    "size": redis_path.stat().st_size,
                    "sha256": redis_hash,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "backup_operation_id": operation_id,
        "backup_pair_marker": str(marker_path),
        "pg_backup_path": str(pg_path),
        "redis_backup_path": str(redis_path),
        "pg_backup_size": pg_path.stat().st_size,
        "redis_backup_size": redis_path.stat().st_size,
        "pg_backup_sha256": pg_hash,
        "redis_backup_sha256": redis_hash,
    }


def test_pending_request_journal_survives_preoperational_recovery(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "state" / "active.json"
    written = subprocess.run(
        _write_request_command(journal),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["phase"] == "request_pending"
    assert payload["timestamp"] == "20260803-010203"

    helper = ROOT / "scripts" / "lib" / "restore_journal.sh"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            SCRIPT_DIR={shlex.quote(str(ROOT / "scripts"))}
            TS=20260803-010203
            PG_DB=lumen
            PG_CONTAINER=lumen-pg
            REDIS_CONTAINER=lumen-redis
            REDIS_BACKUP_PREFIX=.lumen-restore-old.
            ACTIVE_WRITER_SERVICES=()
            ACTIVE_SITE_SERVICES=()
            log() {{ :; }}
            LUMEN_RESTORE_JOURNAL_FILE={shlex.quote(str(journal))}
            LUMEN_RESTORE_JOURNAL_HELPER={shlex.quote(str(SCRIPT))}
            . {shlex.quote(str(helper))}
            lumen_restore_recover_interrupted
            test "$RESTORE_JOURNAL_ACTIVE" -eq 0
            test "$RESTORE_OPERATION_ID" = restore-request-1
            test -f {shlex.quote(str(journal))}
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_write_load_and_clear_use_private_atomic_state(tmp_path: Path) -> None:
    journal = tmp_path / "state" / "active.json"

    written = subprocess.run(
        _write_command(journal),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert written.returncode == 0, written.stderr
    assert stat.S_IMODE(journal.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    document = json.loads(journal.read_text(encoding="utf-8"))
    assert document["phase"] == "redis_applying"
    assert document["active_writer_services"] == ["api", "worker", "tgbot"]
    assert document["backup_operation_id"] == "backup-op-1"
    assert document["pg_backup_size"] == 123
    assert document["redis_backup_sha256"] == REDIS_HASH
    assert not list(journal.parent.glob("*.tmp"))

    loaded = _run("load-shell", str(journal))
    assert loaded.returncode == 0, loaded.stderr
    assert "RESTORE_JOURNAL_PHASE=redis_applying" in loaded.stdout
    assert "RESTORE_JOURNAL_SERVICES_STOPPED=1" in loaded.stdout
    assert "RESTORE_JOURNAL_ACTIVE_WRITER_SERVICES='api worker tgbot'" in loaded.stdout
    assert "RESTORE_JOURNAL_BACKUP_OPERATION_ID=backup-op-1" in loaded.stdout
    assert "RESTORE_JOURNAL_PG_BACKUP_SIZE=123" in loaded.stdout

    cleared = _run("clear", str(journal))
    assert cleared.returncode == 0, cleared.stderr
    assert not journal.exists()


def test_write_rejects_insecure_parent_without_changing_mode(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o755)
    os.chmod(state_dir, 0o755)
    journal = state_dir / "active.json"

    result = subprocess.run(
        _write_command(journal),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "directory mode must be 0700" in result.stderr
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o755
    assert not journal.exists()


def test_write_rejects_journal_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    target = tmp_path / "outside"
    target.write_text("keep\n", encoding="utf-8")
    journal = state_dir / "active.json"
    journal.symlink_to(target)

    result = subprocess.run(
        _write_command(journal),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert journal.is_symlink()


def test_load_and_clear_reject_insecure_journal_mode(tmp_path: Path) -> None:
    journal = tmp_path / "state" / "active.json"
    written = subprocess.run(
        _write_command(journal),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    journal.chmod(0o644)

    loaded = _run("load-shell", str(journal))
    cleared = _run("clear", str(journal))

    assert loaded.returncode == 2
    assert cleared.returncode == 2
    assert journal.exists()


def test_load_rejects_malformed_owned_json(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    journal = state_dir / "active.json"
    journal.write_text("{not-json\n", encoding="utf-8")
    journal.chmod(0o600)

    loaded = _run("load-shell", str(journal))

    assert loaded.returncode == 2
    assert "restore journal error" in loaded.stderr


def test_missing_journal_uses_distinct_load_status(tmp_path: Path) -> None:
    journal = tmp_path / "state" / "active.json"

    loaded = _run("load-shell", str(journal))
    cleared = _run("clear", str(journal))

    assert loaded.returncode == 3
    assert cleared.returncode == 0


def test_unknown_restore_phase_is_rejected_without_clearing_journal(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "state" / "active.json"
    written = subprocess.run(
        _write_command(journal),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    payload = json.loads(journal.read_text(encoding="utf-8"))
    payload["phase"] = "unknown"
    journal.write_text(json.dumps(payload), encoding="utf-8")
    journal.chmod(0o600)

    loaded = _run("load-shell", str(journal))

    assert loaded.returncode == 2
    assert journal.exists()


def test_committed_phase_requires_promoted_pg_and_committed_redis(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "state" / "active.json"

    invalid = subprocess.run(
        _write_command(journal, phase="committed"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert invalid.returncode == 2
    assert "requires promoted postgres and committed redis" in invalid.stderr
    assert not journal.exists()

    valid = subprocess.run(
        _write_command(
            journal,
            phase="committed",
            redis_state="committed",
            pg_promoted="1",
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert valid.returncode == 0, valid.stderr
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["phase"] == "committed"
    assert payload["pg_promoted"] is True
    assert payload["redis_state"] == "committed"


def test_pg_rolled_back_phase_rejects_promoted_or_named_rollback(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "state" / "active.json"
    invalid = _write_command(
        journal,
        phase="pg_rolled_back",
        pg_promoted="1",
    )

    result = subprocess.run(
        invalid,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "requires the pre-restore postgres state" in result.stderr

    valid = subprocess.run(
        _write_command(journal, phase="pg_rolled_back"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert valid.returncode == 0, valid.stderr
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["phase"] == "pg_rolled_back"
    assert payload["pg_promoted"] is False
    assert payload["pg_rollback_db"] == ""


def test_backup_pair_binding_requires_marker_and_matches_payloads(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    binding = _write_backup_pair(backup_root)

    result = _run("backup-pair-bind-json", str(backup_root), TS)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == binding

    (backup_root / f".backup-pair.{TS}.json").unlink()
    missing = _run("backup-pair-bind-json", str(backup_root), TS)
    assert missing.returncode == 2
    assert "cannot stat JSON state" in missing.stderr


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("timestamp", "20260803-010204", "identity"),
        ("operation_id", "", "operation identity"),
        ("pg.size", 999, "size or type"),
        ("redis.sha256", "0" * 64, "hash"),
    ],
)
def test_backup_pair_binding_rejects_marker_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    backup_root = tmp_path / "backup"
    _write_backup_pair(backup_root)
    marker_path = backup_root / f".backup-pair.{TS}.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    target = marker
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    marker_path.write_text(json.dumps(marker) + "\n", encoding="utf-8")

    result = _run("backup-pair-bind-json", str(backup_root), TS)

    assert result.returncode == 2
    assert error in result.stderr


def test_bound_backup_pair_rejects_operation_identity_change(tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    binding = _write_backup_pair(backup_root)
    command = [
        "backup-pair-verify-bound",
        str(backup_root),
        TS,
        str(binding["pg_backup_path"]),
        str(binding["redis_backup_path"]),
        str(binding["pg_backup_size"]),
        str(binding["redis_backup_size"]),
        str(binding["pg_backup_sha256"]),
        str(binding["redis_backup_sha256"]),
        "--operation-id",
        "different-backup-operation",
        "--pair-marker",
        str(binding["backup_pair_marker"]),
    ]

    result = _run(*command)

    assert result.returncode == 2
    assert "marker identity is invalid" in result.stderr


def test_redis_state_fsync_covers_files_and_parent_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_dir = tmp_path / "redis-host"
    backup_dir = host_dir / ".lumen-restore-old.test"
    manifest = backup_dir / ".original-items"
    (host_dir / "appendonlydir").mkdir(parents=True)
    (backup_dir / "appendonlydir").mkdir(parents=True)
    (host_dir / "dump.rdb").write_bytes(b"new")
    (host_dir / "appendonlydir" / "part.aof").write_bytes(b"new-aof")
    (backup_dir / "dump.rdb").write_bytes(b"old")
    (backup_dir / "appendonlydir" / "part.aof").write_bytes(b"old-aof")
    manifest.write_text("dump.rdb\nappendonlydir\n", encoding="utf-8")
    expected = {
        path.stat().st_ino
        for path in (
            host_dir,
            host_dir / "dump.rdb",
            host_dir / "appendonlydir",
            host_dir / "appendonlydir" / "part.aof",
            backup_dir,
            backup_dir / "dump.rdb",
            backup_dir / "appendonlydir",
            backup_dir / "appendonlydir" / "part.aof",
            manifest,
        )
    }
    fsynced: set[int] = set()
    real_fsync = RESTORE_JOURNAL.os.fsync

    def record_fsync(descriptor: int) -> None:
        fsynced.add(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(RESTORE_JOURNAL.os, "fsync", record_fsync)

    RESTORE_JOURNAL._fsync_redis_state(
        SimpleNamespace(
            phase="redis_applied",
            host_dir=host_dir,
            backup_dir=backup_dir,
            manifest=manifest,
        )
    )

    assert expected <= fsynced


@pytest.mark.parametrize(
    "phase",
    [
        "redis_stashing",
        "redis_stashed",
        "redis_applying",
        "redis_applied",
        "redis_rolling_back",
        "redis_rolled_back",
    ],
)
def test_redis_phase_fsync_precedes_journal_write(
    tmp_path: Path,
    phase: str,
) -> None:
    helper = ROOT / "scripts" / "lib" / "restore_journal.sh"
    calls = tmp_path / "calls"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            SCRIPT_DIR={shlex.quote(str(ROOT / "scripts"))}
            TS={TS}
            PG_DB=lumen
            PG_CONTAINER=lumen-pg
            REDIS_CONTAINER=lumen-redis
            PG_TEMP_DB=
            PG_ROLLBACK_DB=
            REDIS_HOST_DIR=/var/lib/redis
            REDIS_BACKUP_DIR=/var/lib/redis/.lumen-restore-old.test
            REDIS_ORIGINAL_MANIFEST="$REDIS_BACKUP_DIR/.original-items"
            REDIS_RESTORE_STATE=applying
            SERVICES_STOPPED=1
            REDIS_NEEDS_START=1
            PG_SWAP_IN_PROGRESS=0
            PG_PROMOTED=0
            ACTIVE_WRITER_SERVICES=(api)
            ACTIVE_SITE_SERVICES=(web)
            LUMEN_RESTORE_JOURNAL_FILE=/var/lib/lumen/restore/active.json
            LUMEN_RESTORE_JOURNAL_HELPER=/trusted/restore_journal.py
            log() {{ :; }}
            python3() {{ printf '%s\\n' "$2" >> {shlex.quote(str(calls))}; }}
            . {shlex.quote(str(helper))}
            lumen_restore_journal_write {shlex.quote(phase)}
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "redis-state-fsync",
        "write",
    ]


def test_redis_phase_fsync_failure_blocks_journal_advance(tmp_path: Path) -> None:
    helper = ROOT / "scripts" / "lib" / "restore_journal.sh"
    calls = tmp_path / "calls"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            SCRIPT_DIR={shlex.quote(str(ROOT / "scripts"))}
            TS={TS}
            PG_DB=lumen
            PG_CONTAINER=lumen-pg
            REDIS_CONTAINER=lumen-redis
            REDIS_HOST_DIR=/var/lib/redis
            REDIS_BACKUP_DIR=/var/lib/redis/.lumen-restore-old.test
            REDIS_ORIGINAL_MANIFEST="$REDIS_BACKUP_DIR/.original-items"
            ACTIVE_WRITER_SERVICES=()
            ACTIVE_SITE_SERVICES=()
            LUMEN_RESTORE_JOURNAL_FILE=/var/lib/lumen/restore/active.json
            LUMEN_RESTORE_JOURNAL_HELPER=/trusted/restore_journal.py
            log() {{ :; }}
            python3() {{
                printf '%s\\n' "$2" >> {shlex.quote(str(calls))}
                [ "$2" != redis-state-fsync ]
            }}
            . {shlex.quote(str(helper))}
            if lumen_restore_journal_write redis_stashed; then
                exit 99
            fi
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert calls.read_text(encoding="utf-8").splitlines() == ["redis-state-fsync"]
