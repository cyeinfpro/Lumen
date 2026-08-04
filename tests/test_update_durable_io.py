from __future__ import annotations

import errno
import importlib.util
import os
from pathlib import Path
import stat
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update" / "durable_io.py"
SPEC = importlib.util.spec_from_file_location("lumen_update_durable_io", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
DURABLE_IO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DURABLE_IO
SPEC.loader.exec_module(DURABLE_IO)


def test_copy_file_durable_replaces_regular_file_and_sets_private_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new-state")
    target.write_bytes(b"old-state")

    DURABLE_IO.copy_file_durable(source, target)

    assert target.read_bytes() == b"new-state"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".target.*.tmp")) == []


def test_directory_fsync_unsupported_uses_syncfs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[int] = []

    def unsupported(_fd: int) -> None:
        raise OSError(errno.EINVAL, "directory fsync unsupported")

    monkeypatch.setattr(DURABLE_IO.os, "fsync", unsupported)
    monkeypatch.setattr(
        DURABLE_IO,
        "_sync_filesystem",
        lambda fd: calls.append(fd),
    )

    DURABLE_IO.fsync_directory(tmp_path)

    assert len(calls) == 1


def test_directory_syncfs_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unsupported(_fd: int) -> None:
        raise OSError(errno.ENOTSUP, "directory fsync unsupported")

    def fail_syncfs(_fd: int) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(DURABLE_IO.os, "fsync", unsupported)
    monkeypatch.setattr(DURABLE_IO, "_sync_filesystem", fail_syncfs)

    with pytest.raises(OSError) as exc_info:
        DURABLE_IO.fsync_directory(tmp_path)

    assert exc_info.value.errno == errno.EIO
