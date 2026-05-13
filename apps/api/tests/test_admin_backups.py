from __future__ import annotations

from pathlib import Path

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
