from __future__ import annotations

import errno
from pathlib import Path

import pytest

import app.storage as storage_mod
from app.storage import LocalStorage, StorageDiskFullError


def test_path_for_rejects_resolved_escape(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(ValueError):
        storage.path_for("../escape.png")
    with pytest.raises(ValueError):
        storage.path_for(str(tmp_path / "escape.png"))


def test_put_bytes_rejects_conflicting_existing_key(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    assert storage.put_bytes("u/user/g/gen/orig.png", b"first") == len(b"first")
    assert storage.put_bytes("u/user/g/gen/orig.png", b"first") == len(b"first")
    with pytest.raises(FileExistsError):
        storage.put_bytes("u/user/g/gen/orig.png", b"second")
    assert storage.get_bytes("u/user/g/gen/orig.png") == b"first"


def test_put_bytes_falls_back_when_hardlink_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalStorage(tmp_path)

    def raise_eperm(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(storage_mod.os, "link", raise_eperm)

    result = storage.put_bytes_result("u/user/g/gen/orig.png", b"first")

    assert result.size == len(b"first")
    assert result.created is True
    assert storage.get_bytes("u/user/g/gen/orig.png") == b"first"
    assert storage.put_bytes_result("u/user/g/gen/orig.png", b"first").created is False
    with pytest.raises(FileExistsError):
        storage.put_bytes("u/user/g/gen/orig.png", b"second")


def test_put_bytes_fallback_retries_transient_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalStorage(tmp_path)

    def raise_eperm(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(errno.EPERM, "operation not permitted")

    calls = 0
    original_write = LocalStorage._write_bytes_exclusive

    def flaky_write(path: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileExistsError(path)
        original_write(path, data)

    monkeypatch.setattr(storage_mod.os, "link", raise_eperm)
    monkeypatch.setattr(
        LocalStorage, "_write_bytes_exclusive", staticmethod(flaky_write)
    )

    result = storage.put_bytes_result("u/user/g/gen/orig.png", b"first")

    assert result.created is True
    assert calls == 2
    assert storage.get_bytes("u/user/g/gen/orig.png") == b"first"


@pytest.mark.asyncio
async def test_async_put_get_and_delete_bytes(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    assert await storage.aput_bytes("u/user/g/gen/orig.png", b"image") == len(b"image")
    assert await storage.aget_bytes("u/user/g/gen/orig.png") == b"image"
    assert storage.delete("u/user/g/gen/orig.png") is True
    assert storage.delete("u/user/g/gen/orig.png") is False


def test_put_bytes_translates_enospc_to_storage_disk_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalStorage(tmp_path)

    def raise_enospc(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(storage_mod.os, "open", raise_enospc)

    with pytest.raises(StorageDiskFullError) as exc_info:
        storage.put_bytes("u/user/g/gen/orig.png", b"image")

    assert exc_info.value.error_code == "disk_full"
    assert exc_info.value.errno == errno.ENOSPC
