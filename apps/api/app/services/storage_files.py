"""Shared storage-path validation and bounded file streaming."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterator


FILE_STREAM_CHUNK_SIZE = 64 * 1024
StoragePathErrorFactory = Callable[[str, str, int], Exception]


def resolve_storage_path(
    storage_root: str | Path,
    storage_key: str,
    *,
    error_factory: StoragePathErrorFactory,
) -> Path:
    root = Path(storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise error_factory("invalid_path", "invalid storage path", 400)
    key_path = PurePosixPath(storage_key)
    if key_path.is_absolute():
        raise error_factory(
            "invalid_path",
            "absolute storage paths are not allowed",
            400,
        )
    parts = key_path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise error_factory("invalid_path", "storage path escapes root", 400)
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            if current.is_symlink():
                raise error_factory(
                    "invalid_path",
                    "symlink storage paths are not allowed",
                    400,
                )
        except OSError as exc:
            raise error_factory("invalid_path", "invalid storage path", 400) from exc
    return root.joinpath(*parts)


def iter_open_file_and_close(file: BinaryIO) -> Iterator[bytes]:
    try:
        while True:
            chunk = file.read(FILE_STREAM_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        file.close()
