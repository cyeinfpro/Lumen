"""Atomic indexes and binary storage for the apparel model library."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from .library_runtime import runtime as _runtime


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    runtime = _runtime()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode(
        "utf-8"
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        runtime._fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
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


def _read_file_bytes_bounded(path: Path, max_bytes: int) -> bytes:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"{path.name} exceeds {max_bytes} bytes")
    payload = bytearray()
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(64 * 1024):
            if len(payload) + len(chunk) > max_bytes:
                raise ValueError(f"{path.name} exceeds {max_bytes} bytes")
            payload.extend(chunk)
    return bytes(payload)


def _read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime()
    try:
        raw = runtime._read_file_bytes_bounded(
            path,
            runtime.MODEL_LIBRARY_MAX_INDEX_BYTES,
        )
        data = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return dict(default)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise runtime._http(
            "invalid_index", f"invalid model library index: {path.name}", 500
        ) from exc
    if not isinstance(data, dict):
        raise runtime._http(
            "invalid_index",
            f"invalid model library index: {path.name}",
            500,
        )
    return data


def _library_root() -> Path:
    runtime = _runtime()
    return runtime._storage_path(runtime.MODEL_LIBRARY_ROOT_KEY)


def _library_index_path() -> Path:
    return _runtime()._library_root() / "index.json"


def _library_sync_state_path() -> Path:
    return _runtime()._library_root() / "sync-state.json"


def _library_sync_lock_path() -> Path:
    return _runtime()._library_root() / ".sync-state.lock"


def _library_user_index_path(user_id: str) -> Path:
    return _runtime()._library_root() / "users" / user_id / "index.json"


def _default_library_index() -> dict[str, Any]:
    runtime = _runtime()
    return {
        "schema_version": runtime.MODEL_LIBRARY_SCHEMA_VERSION,
        "updated_at": None,
        "preset_items": [],
    }


def _default_user_library_index() -> dict[str, Any]:
    runtime = _runtime()
    return {
        "schema_version": runtime.MODEL_LIBRARY_SCHEMA_VERSION,
        "updated_at": None,
        "hidden_preset_ids": [],
        "items": [],
    }


def _default_sync_state() -> dict[str, Any]:
    runtime = _runtime()
    return {
        "schema_version": runtime.MODEL_LIBRARY_SCHEMA_VERSION,
        "last_success_at": None,
        "last_error": None,
        "last_attempt_at": None,
        "last_result": None,
        "sync_lease": None,
    }


def _load_global_library_index() -> dict[str, Any]:
    runtime = _runtime()
    return runtime._read_json_file(
        runtime._library_index_path(),
        runtime._default_library_index(),
    )


def _load_user_library_index(user_id: str) -> dict[str, Any]:
    """Read the legacy per-user JSON index.

    Kept for cutover safety: routes call ``_ensure_legacy_user_library_migrated``
    before DB reads so users do not lose visibility of old saved models when
    the new tables exist but the one-off backfill has not been run yet.
    """
    runtime = _runtime()
    return runtime._read_json_file(
        runtime._library_user_index_path(user_id),
        runtime._default_user_library_index(),
    )


def _save_global_library_index(index: dict[str, Any]) -> None:
    runtime = _runtime()
    index["schema_version"] = runtime.MODEL_LIBRARY_SCHEMA_VERSION
    index["updated_at"] = runtime._iso_now()
    runtime._write_json_atomic(runtime._library_index_path(), index)


def _save_user_library_index(user_id: str, index: dict[str, Any]) -> None:
    """Legacy file writer kept for migration tests and deletion tombstoning.

    Creation/update routes write through ORM; delete still updates this file
    so lazy migration cannot re-create rows the user already removed.
    """
    runtime = _runtime()
    index["schema_version"] = runtime.MODEL_LIBRARY_SCHEMA_VERSION
    index["updated_at"] = runtime._iso_now()
    runtime._write_json_atomic(runtime._library_user_index_path(user_id), index)


def _remove_user_library_item_from_legacy_index(user_id: str, item_id: str) -> bool:
    """Keep lazy JSON migration from resurrecting a DB-deleted user item."""
    runtime = _runtime()
    index_path = runtime._library_user_index_path(user_id)
    if not index_path.is_file():
        return False
    index = runtime._load_user_library_index(user_id)
    raw_items = index.get("items")
    if not isinstance(raw_items, list):
        return False
    next_items: list[Any] = []
    removed = False
    for raw in raw_items:
        raw_id = str(raw.get("id") or "").strip() if isinstance(raw, dict) else ""
        if raw_id == item_id:
            removed = True
            continue
        next_items.append(raw)
    if not removed:
        return False
    index["items"] = next_items
    runtime._save_user_library_index(user_id, index)
    return True


def _hide_preset_in_legacy_user_library_index(user_id: str, preset_id: str) -> bool:
    """Mirror preset hides into the legacy index while lazy migration exists."""
    runtime = _runtime()
    index_path = runtime._library_user_index_path(user_id)
    if not index_path.is_file():
        return False
    index = runtime._load_user_library_index(user_id)
    hidden_ids = runtime._dedupe_nonempty(index.get("hidden_preset_ids") or [])
    if preset_id in hidden_ids:
        return False
    index["hidden_preset_ids"] = [*hidden_ids, preset_id]
    runtime._save_user_library_index(user_id, index)
    return True


def _save_sync_state(state: dict[str, Any]) -> None:
    runtime = _runtime()
    state["schema_version"] = runtime.MODEL_LIBRARY_SCHEMA_VERSION
    runtime._write_json_atomic(runtime._library_sync_state_path(), state)


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


def _sha256_file_bounded(path: Path, max_bytes: int) -> str | None:
    if path.stat().st_size > max_bytes:
        return None
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                return None
            digest.update(chunk)
    return digest.hexdigest()


def _open_library_storage_file(storage_key: str) -> tuple[Path, str, str]:
    runtime = _runtime()
    path = runtime._storage_path(storage_key)
    if not path.is_file():
        raise runtime._http("not_found", "library binary missing", 404)
    size = path.stat().st_size
    if size > runtime.MODEL_LIBRARY_MAX_BINARY_BYTES:
        raise runtime._http(
            "library_binary_too_large",
            f"library binary exceeds {runtime.MODEL_LIBRARY_MAX_BINARY_BYTES} bytes",
            413,
        )
    sha = runtime._sha256_file_bounded(
        path,
        runtime.MODEL_LIBRARY_MAX_BINARY_BYTES,
    )
    if sha is None:
        raise runtime._http(
            "library_binary_too_large",
            f"library binary exceeds {runtime.MODEL_LIBRARY_MAX_BINARY_BYTES} bytes",
            413,
        )
    return path, runtime._guess_mime(path), sha


def _stream_file(path: Path) -> Iterable[bytes]:
    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            yield chunk


def _library_binary_response(storage_key: str, request: Request) -> Response:
    runtime = _runtime()
    path, media_type, sha = runtime._open_library_storage_file(storage_key)
    size = path.stat().st_size
    etag = f'"{sha}"'
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=86400"},
        )
    return StreamingResponse(
        runtime._stream_file(path),
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": etag,
            "Content-Length": str(size),
        },
    )


def _preset_storage_key(preset_id: str, version: int, image_path: str) -> str:
    runtime = _runtime()
    suffix = Path(image_path).suffix.lower() or ".webp"
    return f"{runtime.MODEL_LIBRARY_ROOT_KEY}/presets/{preset_id}/v{version}{suffix}"


def _preset_thumb_storage_key(
    preset_id: str, thumb_path: str | None, image_key: str
) -> str:
    runtime = _runtime()
    if not thumb_path:
        return image_key
    suffix = Path(thumb_path).suffix.lower() or ".webp"
    return f"{runtime.MODEL_LIBRARY_ROOT_KEY}/presets/{preset_id}/thumb{suffix}"


def _write_bytes_replace(path: Path, data: bytes) -> None:
    runtime = _runtime()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        runtime._fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)
