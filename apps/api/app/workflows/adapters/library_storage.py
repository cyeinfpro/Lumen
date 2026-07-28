"""Atomic indexes and binary storage for the apparel model library."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from ..domain.apparel_library import (
    MODEL_LIBRARY_MAX_BINARY_BYTES,
    MODEL_LIBRARY_MAX_INDEX_BYTES,
    MODEL_LIBRARY_SCHEMA_VERSION,
)
from .serialization import dedupe_nonempty as _dedupe_nonempty  # noqa: F401
from .serialization import http as _http  # noqa: F401
from .serialization import iso_now as _iso_now  # noqa: F401
from .serialization import storage_path as _storage_path  # noqa: F401


MODEL_LIBRARY_ROOT_KEY = "apparel-model-library"


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
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
        _fsync_dir(path.parent)
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
    try:
        raw = _read_file_bytes_bounded(
            path,
            MODEL_LIBRARY_MAX_INDEX_BYTES,
        )
        data = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return dict(default)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise _http(
            "invalid_index", f"invalid model library index: {path.name}", 500
        ) from exc
    if not isinstance(data, dict):
        raise _http(
            "invalid_index",
            f"invalid model library index: {path.name}",
            500,
        )
    return data


def _library_root() -> Path:
    return _storage_path(MODEL_LIBRARY_ROOT_KEY)


def _library_index_path() -> Path:
    return _library_root() / "index.json"


def _library_sync_state_path() -> Path:
    return _library_root() / "sync-state.json"


def _library_sync_lock_path() -> Path:
    return _library_root() / ".sync-state.lock"


def _library_user_index_path(user_id: str) -> Path:
    return _library_root() / "users" / user_id / "index.json"


def _default_library_index() -> dict[str, Any]:
    return {
        "schema_version": MODEL_LIBRARY_SCHEMA_VERSION,
        "updated_at": None,
        "preset_items": [],
    }


def _default_user_library_index() -> dict[str, Any]:
    return {
        "schema_version": MODEL_LIBRARY_SCHEMA_VERSION,
        "updated_at": None,
        "hidden_preset_ids": [],
        "items": [],
    }


def _default_sync_state() -> dict[str, Any]:
    return {
        "schema_version": MODEL_LIBRARY_SCHEMA_VERSION,
        "last_success_at": None,
        "last_error": None,
        "last_attempt_at": None,
        "last_result": None,
        "sync_lease": None,
    }


def _load_global_library_index() -> dict[str, Any]:
    return _read_json_file(
        _library_index_path(),
        _default_library_index(),
    )


def _load_user_library_index(user_id: str) -> dict[str, Any]:
    """Read the legacy per-user JSON index.

    Kept for cutover safety: routes call ``_ensure_legacy_user_library_migrated``
    before DB reads so users do not lose visibility of old saved models when
    the new tables exist but the one-off backfill has not been run yet.
    """
    return _read_json_file(
        _library_user_index_path(user_id),
        _default_user_library_index(),
    )


def _save_global_library_index(index: dict[str, Any]) -> None:
    index["schema_version"] = MODEL_LIBRARY_SCHEMA_VERSION
    index["updated_at"] = _iso_now()
    _write_json_atomic(_library_index_path(), index)


def _save_user_library_index(user_id: str, index: dict[str, Any]) -> None:
    """Legacy file writer kept for migration tests and deletion tombstoning.

    Creation/update routes write through ORM; delete still updates this file
    so lazy migration cannot re-create rows the user already removed.
    """
    index["schema_version"] = MODEL_LIBRARY_SCHEMA_VERSION
    index["updated_at"] = _iso_now()
    _write_json_atomic(_library_user_index_path(user_id), index)


def _remove_user_library_item_from_legacy_index(user_id: str, item_id: str) -> bool:
    """Keep lazy JSON migration from resurrecting a DB-deleted user item."""
    index_path = _library_user_index_path(user_id)
    if not index_path.is_file():
        return False
    index = _load_user_library_index(user_id)
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
    _save_user_library_index(user_id, index)
    return True


def _hide_preset_in_legacy_user_library_index(user_id: str, preset_id: str) -> bool:
    """Mirror preset hides into the legacy index while lazy migration exists."""
    index_path = _library_user_index_path(user_id)
    if not index_path.is_file():
        return False
    index = _load_user_library_index(user_id)
    hidden_ids = _dedupe_nonempty(index.get("hidden_preset_ids") or [])
    if preset_id in hidden_ids:
        return False
    index["hidden_preset_ids"] = [*hidden_ids, preset_id]
    _save_user_library_index(user_id, index)
    return True


def _save_sync_state(state: dict[str, Any]) -> None:
    state["schema_version"] = MODEL_LIBRARY_SCHEMA_VERSION
    _write_json_atomic(_library_sync_state_path(), state)


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
    path = _storage_path(storage_key)
    if not path.is_file():
        raise _http("not_found", "library binary missing", 404)
    size = path.stat().st_size
    if size > MODEL_LIBRARY_MAX_BINARY_BYTES:
        raise _http(
            "library_binary_too_large",
            f"library binary exceeds {MODEL_LIBRARY_MAX_BINARY_BYTES} bytes",
            413,
        )
    sha = _sha256_file_bounded(
        path,
        MODEL_LIBRARY_MAX_BINARY_BYTES,
    )
    if sha is None:
        raise _http(
            "library_binary_too_large",
            f"library binary exceeds {MODEL_LIBRARY_MAX_BINARY_BYTES} bytes",
            413,
        )
    return path, _guess_mime(path), sha


def _stream_file(path: Path) -> Iterable[bytes]:
    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            yield chunk


def _preset_storage_key(preset_id: str, version: int, image_path: str) -> str:
    suffix = Path(image_path).suffix.lower() or ".webp"
    return f"{MODEL_LIBRARY_ROOT_KEY}/presets/{preset_id}/v{version}{suffix}"


def _preset_thumb_storage_key(
    preset_id: str, thumb_path: str | None, image_key: str
) -> str:
    if not thumb_path:
        return image_key
    suffix = Path(thumb_path).suffix.lower() or ".webp"
    return f"{MODEL_LIBRARY_ROOT_KEY}/presets/{preset_id}/thumb{suffix}"


def _write_bytes_replace(path: Path, data: bytes) -> None:
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
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


# Public workflow contracts.
default_library_index = _default_library_index
default_sync_state = _default_sync_state
default_user_library_index = _default_user_library_index
fsync_dir = _fsync_dir
guess_mime = _guess_mime
hide_preset_in_legacy_user_library_index = _hide_preset_in_legacy_user_library_index
library_index_path = _library_index_path
library_root = _library_root
library_sync_lock_path = _library_sync_lock_path
library_sync_state_path = _library_sync_state_path
library_user_index_path = _library_user_index_path
load_global_library_index = _load_global_library_index
load_user_library_index = _load_user_library_index
open_library_storage_file = _open_library_storage_file
preset_storage_key = _preset_storage_key
preset_thumb_storage_key = _preset_thumb_storage_key
read_file_bytes_bounded = _read_file_bytes_bounded
read_json_file = _read_json_file
remove_user_library_item_from_legacy_index = _remove_user_library_item_from_legacy_index
save_global_library_index = _save_global_library_index
save_sync_state = _save_sync_state
save_user_library_index = _save_user_library_index
sha256_file_bounded = _sha256_file_bounded
stream_file = _stream_file
write_bytes_replace = _write_bytes_replace
write_json_atomic = _write_json_atomic
