"""Bounded poster-style storage primitives."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def resolve_storage_root(value: str) -> Path:
    return Path(value).resolve()


def resolve_storage_path(storage_key: str, *, root: Path) -> Path:
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise _http("invalid_path", "storage path escapes root", 400) from exc
    return path


def fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_bytes_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def write_json_atomic(
    path: Path,
    data: dict[str, Any],
    *,
    max_bytes: int,
) -> None:
    payload = json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise ValueError(f"{path.name} exceeds {max_bytes} bytes")
    write_bytes_replace(path, payload)


def read_json_file(
    path: Path,
    default: dict[str, Any],
    *,
    max_bytes: int,
) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError(f"{path.name} exceeds {max_bytes} bytes")
        data = json.loads(payload.decode("utf-8"))
    except FileNotFoundError:
        return dict(default)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise _http(
            "invalid_index",
            f"invalid poster style index: {path.name}",
            500,
        ) from exc
    if not isinstance(data, dict):
        raise _http(
            "invalid_index",
            f"invalid poster style index: {path.name}",
            500,
        )
    return data


def guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"
