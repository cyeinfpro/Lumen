from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routes import admin_backups


def test_backup_paths_resolve_from_settings_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "backup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "restore.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(admin_backups.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_backups.settings, "lumen_scripts_dir", str(scripts_dir))

    assert admin_backups._backup_root() == backup_root
    assert admin_backups._backup_script() == scripts_dir / "backup.sh"
    assert admin_backups._restore_script() == scripts_dir / "restore.sh"


def test_backup_trigger_mode_accepts_only_numeric_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUMEN_BACKUP_VIA_TRIGGER", "true")
    assert admin_backups._backup_trigger_only_mode() is False
    monkeypatch.setenv("LUMEN_BACKUP_VIA_TRIGGER", "1")
    assert admin_backups._backup_trigger_only_mode() is True


def test_chmod_tolerate_eperm_swallows_only_eperm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CIFS mount in production pins file_mode + uid via mount options;

    every chmod from any caller returns EPERM. The helper has to swallow
    EPERM but still propagate every other OSError so genuine faults
    (ENOSPC, EBADF, EROFS, EIO, ...) still fail fast.
    """
    target = tmp_path / "marker.tmp"
    target.write_text("ok\n", encoding="utf-8")

    # EPERM is swallowed.
    def fake_chmod_eperm(path: object, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(admin_backups.os, "chmod", fake_chmod_eperm)
    admin_backups._chmod_tolerate_eperm(target, 0o600)  # must not raise

    # Other OSErrors still propagate.
    def fake_chmod_enospc(path: object, mode: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(admin_backups.os, "chmod", fake_chmod_enospc)
    with pytest.raises(OSError) as exc:
        admin_backups._chmod_tolerate_eperm(target, 0o600)
    assert exc.value.errno == 28


def test_try_write_pid_marker_replaces_corrupt_empty_marker(tmp_path: Path) -> None:
    marker = tmp_path / ".backup.running"
    marker.write_text("", encoding="utf-8")
    started_at = datetime(2026, 7, 2, tzinfo=timezone.utc)

    assert admin_backups._try_write_pid_marker(marker, 12345, started_at) is True

    raw = marker.read_text(encoding="utf-8")
    assert "pid=12345\n" in raw
    assert f"started_at={started_at.isoformat()}\n" in raw


def test_open_private_append_tolerates_fchmod_eperm_for_non_owner_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """In docker-compose deploys lumen-api appends to .update.log that

    lumen-update-runner.service writes from the host. Container uid (e.g.
    LUMEN_APP_UID=10001) won't match the host owner, so non-owner fchmod
    returns EPERM. Before this fix the trigger_update endpoint crashed with
    a 500 (PermissionError [Errno 1] Operation not permitted) before even
    writing the trigger file. _open_private_append must swallow EPERM —
    O_CREAT already enforces 0o600 on fresh files; existing-file mode is
    the host's responsibility.
    """
    log_path = tmp_path / ".update.log"
    log_path.write_text("preexisting content\n", encoding="utf-8")

    calls: list[tuple[int, int]] = []

    def fake_fchmod(fd: int, mode: int) -> None:
        calls.append((fd, mode))
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(admin_backups.os, "fchmod", fake_fchmod)

    fh = admin_backups._open_private_append(log_path)
    try:
        fh.write("appended by lumen-api\n")
    finally:
        fh.close()

    assert calls and calls[0][1] == 0o600, "expected fchmod attempt with 0o600"
    text = log_path.read_text(encoding="utf-8")
    assert text.startswith("preexisting content")
    assert "appended by lumen-api" in text


def test_open_private_append_re_raises_other_oserrors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only EPERM from a non-owner fchmod is benign — every other OSError
    (ENOSPC, EBADF, EIO, ...) must still surface so the caller fails fast.
    """
    log_path = tmp_path / ".update.log"

    def fake_fchmod(fd: int, mode: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(admin_backups.os, "fchmod", fake_fchmod)

    with pytest.raises(OSError) as exc_info:
        admin_backups._open_private_append(log_path)
    assert exc_info.value.errno == 28


@pytest.mark.asyncio
async def test_run_script_uses_async_subprocess(tmp_path: Path) -> None:
    script = tmp_path / "backup.sh"
    script.write_text("printf 'backup ok'\n", encoding="utf-8")

    result = await admin_backups._run_script(script, timeout=5)

    assert result.returncode == 0
    assert result.stdout == "backup ok"
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_backup_now_unlinks_marker_before_releasing_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    backup_script = scripts_dir / "backup.sh"
    backup_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(admin_backups.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_backups.settings, "lumen_scripts_dir", str(scripts_dir))

    release_marker_states: list[bool] = []

    class FakeLockService:
        def __init__(self, *, fallback_busy):
            self.fallback_busy = fallback_busy

        async def acquire(self, **_kwargs):
            return object()

        async def release(self, *_args, **_kwargs) -> None:
            marker = admin_backups._maintenance_marker_path(
                admin_backups._BACKUP_RUNNING_MARKER
            )
            release_marker_states.append(marker.exists())

    async def timeout_run_script(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(admin_backups, "SystemOperationLockService", FakeLockService)
    monkeypatch.setattr(admin_backups, "_run_script", timeout_run_script)

    with pytest.raises(Exception) as exc_info:
        await admin_backups.backup_now(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(id="admin-1", email="admin@example.test"),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 504
    assert release_marker_states == [False]


def test_timestamp_from_backup_stdout_accepts_json_and_legacy_lines() -> None:
    started_at = datetime(2026, 5, 19, tzinfo=timezone.utc)

    assert (
        admin_backups._timestamp_from_backup_stdout(
            '[backup 2026-05-19T00:00:00Z] done\n'
            '{"timestamp":"20260519-010203","pg_size":1,"redis_size":2}\n',
            started_at,
        )
        == "20260519-010203"
    )
    assert (
        admin_backups._timestamp_from_backup_stdout(
            "[backup 2026-05-19T00:00:00Z] backup 20260519-020304 complete\n",
            started_at,
        )
        == "20260519-020304"
    )
    assert (
        admin_backups._timestamp_from_backup_stdout(
            "backup complete: timestamp=20260519-030405\n",
            started_at,
        )
        == "20260519-030405"
    )


def test_backup_script_was_skipped_detects_maintenance_skip() -> None:
    assert admin_backups._backup_script_was_skipped(
        "[backup now] skipped: maintenance lock held; next timer cycle will retry\n"
    )
    assert not admin_backups._backup_script_was_skipped(
        "[backup now] backup 20260519-010203 complete\n"
    )


@pytest.mark.asyncio
async def test_find_latest_paired_backup_after_uses_filesystem_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    pg_dir = backup_root / "pg"
    redis_dir = backup_root / "redis"
    pg_dir.mkdir(parents=True)
    redis_dir.mkdir()
    monkeypatch.setattr(admin_backups.settings, "backup_root", str(backup_root))

    started_at = datetime.now(timezone.utc)
    ts = "20260519-010203"
    (pg_dir / f"{ts}.pg.dump.gz").write_bytes(b"pg")
    (redis_dir / f"{ts}.redis.tgz").write_bytes(b"redis")

    assert await admin_backups._find_latest_paired_backup_after(started_at) == ts


def test_backup_pair_for_timestamp_rejects_symlinked_backup_leaf(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    pg_dir = backup_root / "pg"
    redis_dir = backup_root / "redis"
    pg_dir.mkdir(parents=True)
    redis_dir.mkdir()
    ts = "20260519-010203"
    real_pg = pg_dir / "real.pg.dump.gz"
    real_pg.write_bytes(b"pg")
    (pg_dir / f"{ts}.pg.dump.gz").symlink_to(real_pg)
    (redis_dir / f"{ts}.redis.tgz").write_bytes(b"redis")

    with pytest.raises(ValueError):
        admin_backups._backup_pair_for_timestamp(backup_root.resolve(), ts)


@pytest.mark.asyncio
async def test_list_backups_skips_symlinked_backup_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    pg_dir = backup_root / "pg"
    redis_dir = backup_root / "redis"
    pg_dir.mkdir(parents=True)
    redis_dir.mkdir()
    monkeypatch.setattr(admin_backups.settings, "backup_root", str(backup_root))
    ts = "20260519-010203"
    real_pg = pg_dir / "real.pg.dump.gz"
    real_pg.write_bytes(b"pg")
    (pg_dir / f"{ts}.pg.dump.gz").symlink_to(real_pg)
    (redis_dir / f"{ts}.redis.tgz").write_bytes(b"redis")

    out = await admin_backups.list_backups(SimpleNamespace())  # type: ignore[arg-type]

    assert out.items == []
    assert out.total == 0


@pytest.mark.asyncio
async def test_backup_now_trigger_mode_writes_trigger_and_waits_for_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "backup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "restore.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(admin_backups.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_backups.settings, "lumen_scripts_dir", str(scripts_dir))
    monkeypatch.setattr(admin_backups, "_backup_trigger_only_mode", lambda: True)

    release_calls: list[dict[str, object]] = []

    class FakeLockService:
        def __init__(self, *, fallback_busy):
            self.fallback_busy = fallback_busy

        async def acquire(self, **_kwargs):
            return object()

        async def release(self, *_args, **kwargs) -> None:
            release_calls.append(kwargs)

    async def fake_wait_for_log_append(*_args, **_kwargs) -> bool:
        return True

    async def fake_wait_for_latest(*_args, **_kwargs) -> str:
        return "20260519-010203"

    async def fake_audit(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(admin_backups, "SystemOperationLockService", FakeLockService)
    monkeypatch.setattr(admin_backups, "_wait_for_log_append", fake_wait_for_log_append)
    monkeypatch.setattr(
        admin_backups,
        "_wait_for_latest_paired_backup_after",
        fake_wait_for_latest,
    )
    monkeypatch.setattr(admin_backups, "write_admin_audit_isolated", fake_audit)

    out = await admin_backups.backup_now(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(id="admin-1", email="admin@example.test"),  # type: ignore[arg-type]
    )

    assert out.ok is True
    assert out.timestamp == "20260519-010203"
    assert (backup_root / ".backup.trigger").is_file()
    assert not (backup_root / ".backup.running").exists()
    assert release_calls[-1] == {"succeeded": True, "reason": "backup_complete"}


@pytest.mark.asyncio
async def test_backup_now_refuses_existing_marker_before_writing_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "backup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "restore.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(admin_backups.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_backups.settings, "lumen_scripts_dir", str(scripts_dir))
    monkeypatch.setattr(admin_backups, "_backup_trigger_only_mode", lambda: True)
    backup_root.mkdir()
    (backup_root / ".backup.running").write_text(
        "pid=0\n"
        f"started_at={datetime.now(timezone.utc).isoformat()}\n"
        "unit=lumen-backup-running.service\n",
        encoding="utf-8",
    )

    release_calls: list[dict[str, object]] = []

    class FakeLockService:
        def __init__(self, *, fallback_busy):
            self.fallback_busy = fallback_busy

        async def acquire(self, **_kwargs):
            return object()

        async def release(self, *_args, **kwargs) -> None:
            release_calls.append(kwargs)

    async def unexpected_wait_for_log_append(*_args, **_kwargs) -> bool:
        raise AssertionError("trigger should not be written when marker exists")

    monkeypatch.setattr(admin_backups, "SystemOperationLockService", FakeLockService)
    monkeypatch.setattr(
        admin_backups,
        "_wait_for_log_append",
        unexpected_wait_for_log_append,
    )

    with pytest.raises(Exception) as exc_info:
        await admin_backups.backup_now(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(id="admin-1", email="admin@example.test"),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert release_calls == [{"succeeded": False, "reason": "maintenance_busy"}]
    assert not (backup_root / ".backup.trigger").exists()


@pytest.mark.asyncio
async def test_backup_now_reports_skipped_script_as_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "backup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "restore.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(admin_backups.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_backups.settings, "lumen_scripts_dir", str(scripts_dir))
    monkeypatch.setattr(admin_backups, "_backup_trigger_only_mode", lambda: False)

    class FakeLockService:
        def __init__(self, *, fallback_busy):
            self.fallback_busy = fallback_busy

        async def acquire(self, **_kwargs):
            return object()

        async def release(self, *_args, **_kwargs) -> None:
            return None

    async def fake_run_script(*_args, **_kwargs):
        return admin_backups._ScriptResult(
            0,
            "[backup now] skipped: maintenance lock held; next timer cycle will retry\n",
            "",
        )

    audit_events: list[str] = []

    async def fake_audit(*_args, event_type: str, **_kwargs) -> None:
        audit_events.append(event_type)

    monkeypatch.setattr(admin_backups, "SystemOperationLockService", FakeLockService)
    monkeypatch.setattr(admin_backups, "_run_script", fake_run_script)
    monkeypatch.setattr(admin_backups, "write_admin_audit_isolated", fake_audit)

    with pytest.raises(Exception) as exc_info:
        await admin_backups.backup_now(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(id="admin-1", email="admin@example.test"),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert audit_events == ["admin.backup.create.skipped"]
    assert not (backup_root / ".backup.trigger").exists()
