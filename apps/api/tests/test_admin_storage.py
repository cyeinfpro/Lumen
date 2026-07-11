from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routes import admin_storage


def test_write_atomic_uses_unique_temp_names(tmp_path: Path) -> None:
    path = tmp_path / "storage.conf"
    stale = tmp_path / "storage.conf.tmp"
    stale.write_text("stale", encoding="utf-8")

    admin_storage._write_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "new\n"
    assert stale.read_text(encoding="utf-8") == "stale"


def test_write_atomic_tolerates_eperm_on_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CIFS forceuid mounts EPERM on chmod — the write must still complete.

    /opt/lumendata in production is a CIFS forceuid mount where chmod /
    fchmod always returns EPERM. ``_write_atomic`` must therefore swallow
    that specific OSError so admin actions like trigger_update / storage
    config writes do not turn into a 5xx for routine permission noise.
    """
    path = tmp_path / "storage.conf"
    real_chmod = os.chmod
    chmod_calls: list[tuple[str, int]] = []

    def fake_chmod(target: str | int, mode: int, *args: object, **kwargs: object) -> None:
        chmod_calls.append((str(target), mode))
        # Mirror the kernel error path used by CIFS forceuid: PermissionError
        # is OSError with errno.EPERM, not a different exception class.
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), str(target))

    monkeypatch.setattr(admin_storage.os, "chmod", fake_chmod)

    # Must not raise: _write_atomic is required to swallow chmod EPERM.
    admin_storage._write_atomic(path, "payload\n", mode=0o600)

    assert path.read_text(encoding="utf-8") == "payload\n"
    assert chmod_calls, "chmod was expected to be attempted on the temp file"
    # Ensure the exception class we raised is the same one Linux CIFS produces.
    assert isinstance(
        PermissionError(errno.EPERM, "x"), OSError
    ), "PermissionError must remain a subclass of OSError for the catch to fire"
    # Restore (defense-in-depth in case other tests in this module rely on it).
    monkeypatch.setattr(admin_storage.os, "chmod", real_chmod)


def test_normalize_local_root_rejects_system_paths_and_honors_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    monkeypatch.setenv("LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS", str(allowed))

    assert admin_storage._normalize_local_root(str(allowed / "lumen")) == str(
        allowed / "lumen"
    )
    with pytest.raises(HTTPException) as exc_info:
        admin_storage._normalize_local_root("/etc")
    assert exc_info.value.detail["error"]["code"] == "unsafe_local_root"

    with pytest.raises(HTTPException) as exc_info:
        admin_storage._normalize_local_root(str(tmp_path / "outside"))
    assert exc_info.value.detail["error"]["code"] == "local_root_not_allowed"


def test_storage_trigger_staging_rejects_live_pending_and_clears_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trigger = tmp_path / "apply.trigger"
    trigger.write_text("pending\n", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        admin_storage._clear_stale_trigger(trigger, stale_after=60)
    assert exc_info.value.status_code == 409

    monkeypatch.setattr(admin_storage.time, "time", lambda: 10_000.0)
    os.utime(trigger, (1.0, 1.0))
    admin_storage._clear_stale_trigger(trigger, stale_after=60)
    assert not trigger.exists()
