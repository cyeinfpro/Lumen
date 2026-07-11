from __future__ import annotations

import inspect
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO, Callable, Generator, Iterator, cast

import pytest
from fastapi import HTTPException

from app.routes import images, shares
from app.services import storage_files


RouteIterator = Callable[[BinaryIO], Iterator[bytes]]
ROUTE_MODULES = (images, shares)
STREAM_ITERATORS = (
    storage_files.iter_open_file_and_close,
    images._iter_open_file_and_close,
    shares._iter_open_file_and_close,
)


class _RecordingFile(BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_sizes: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


def _assert_storage_error(exc: HTTPException, *, status_code: int = 400) -> None:
    detail = cast(dict[str, Any], exc.detail)
    assert exc.status_code == status_code
    assert detail["error"]["code"] == (
        "invalid_path" if status_code == 400 else "not_found"
    )


@pytest.mark.parametrize("route_module", ROUTE_MODULES)
def test_route_storage_facades_keep_private_signatures(
    route_module: ModuleType,
) -> None:
    assert tuple(inspect.signature(route_module._fs_path).parameters) == (
        "storage_key",
    )
    assert tuple(
        inspect.signature(route_module._iter_open_file_and_close).parameters
    ) == ("f",)


@pytest.mark.parametrize("route_module", ROUTE_MODULES)
def test_route_storage_path_facade_delegates_with_route_error_factory(
    route_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = tmp_path / "resolved.png"
    calls: list[tuple[str | Path, str, object]] = []

    def fake_resolve(
        storage_root: str | Path,
        storage_key: str,
        *,
        error_factory,
    ) -> Path:
        calls.append((storage_root, storage_key, error_factory))
        return expected

    monkeypatch.setattr(route_module.settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(storage_files, "resolve_storage_path", fake_resolve)

    assert route_module._fs_path("u/user/image.png") == expected
    assert calls == [(str(tmp_path), "u/user/image.png", route_module._http)]


@pytest.mark.parametrize("route_module", ROUTE_MODULES)
@pytest.mark.parametrize(
    "storage_key",
    ("", "bad\x00key.png", "../escape.png", "/absolute/escape.png"),
)
def test_route_storage_path_facades_reject_unsafe_keys(
    route_module: ModuleType,
    storage_key: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(route_module.settings, "storage_root", str(tmp_path))

    with pytest.raises(HTTPException) as exc_info:
        route_module._fs_path(storage_key)

    _assert_storage_error(exc_info.value)


@pytest.mark.parametrize("route_module", ROUTE_MODULES)
def test_route_storage_path_facades_reject_parent_symlink_escape(
    route_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(route_module.settings, "storage_root", str(root))

    with pytest.raises(HTTPException) as exc_info:
        route_module._fs_path("linked/secret.png")

    _assert_storage_error(exc_info.value)


@pytest.mark.parametrize("route_module", ROUTE_MODULES)
def test_route_missing_storage_file_remains_not_found(
    route_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(route_module.settings, "storage_root", str(tmp_path))

    with pytest.raises(HTTPException) as exc_info:
        if route_module is images:
            route_module._open_regular_file_no_symlink(
                route_module._fs_path("missing.png")
            )
        else:
            route_module._open_storage_file_safe("missing.png")

    _assert_storage_error(exc_info.value, status_code=404)


@pytest.mark.parametrize("iterator", STREAM_ITERATORS)
def test_storage_iterators_stream_bounded_chunks_and_close(
    iterator: RouteIterator,
) -> None:
    chunk_size = storage_files.FILE_STREAM_CHUNK_SIZE
    payload = b"x" * (chunk_size * 2 + 17)
    opened = _RecordingFile(payload)

    chunks = list(iterator(opened))

    assert b"".join(chunks) == payload
    assert [len(chunk) for chunk in chunks] == [chunk_size, chunk_size, 17]
    assert opened.read_sizes == [chunk_size, chunk_size, chunk_size, chunk_size]
    assert opened.closed is True


@pytest.mark.parametrize("iterator", STREAM_ITERATORS)
def test_storage_iterators_close_after_partial_consumption(
    iterator: RouteIterator,
) -> None:
    opened = _RecordingFile(b"x" * (storage_files.FILE_STREAM_CHUNK_SIZE + 1))
    stream = cast(Generator[bytes, None, None], iterator(opened))

    assert len(next(stream)) == storage_files.FILE_STREAM_CHUNK_SIZE
    stream.close()

    assert opened.closed is True
