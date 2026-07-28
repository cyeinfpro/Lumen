"""Public poster-style preset lookup for workflow consumers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    POSTER_STYLE_LIBRARY_FOLDER,
    POSTER_STYLE_SCHEMA_VERSION,
)
from lumen_core.models import PosterStyleHiddenPreset

from ...config import settings
from . import library
from . import storage
from .sync import sync_lease_owner

_INDEX_MAX_BYTES = 32 * 1024 * 1024


def _index_path() -> Path:
    root = storage.resolve_storage_root(settings.storage_root)
    return root / POSTER_STYLE_LIBRARY_FOLDER / "index.json"


def _sync_state_path() -> Path:
    root = storage.resolve_storage_root(settings.storage_root)
    return root / POSTER_STYLE_LIBRARY_FOLDER / "sync-state.json"


def _sync_lock_path() -> Path:
    root = storage.resolve_storage_root(settings.storage_root)
    return root / POSTER_STYLE_LIBRARY_FOLDER / ".sync-state.lock"


def _default_index() -> dict[str, Any]:
    return {
        "schema_version": POSTER_STYLE_SCHEMA_VERSION,
        "updated_at": None,
        "preset_items": [],
    }


def _default_sync_state() -> dict[str, Any]:
    return {
        "schema_version": POSTER_STYLE_SCHEMA_VERSION,
        "last_success_at": None,
        "last_error": None,
        "last_attempt_at": None,
        "last_result": None,
        "sync_lease": None,
    }


def _load_index() -> dict[str, Any]:
    index = storage.read_json_file(
        _index_path(),
        _default_index(),
        max_bytes=_INDEX_MAX_BYTES,
    )
    if not isinstance(index.get("preset_items"), list):
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "code": "invalid_index",
                    "message": f"invalid poster style index: {_index_path().name}",
                }
            },
        )
    return index


def _save_index(index: dict[str, Any]) -> None:
    index["schema_version"] = POSTER_STYLE_SCHEMA_VERSION
    index["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    storage.write_json_atomic(_index_path(), index, max_bytes=_INDEX_MAX_BYTES)


def _local_presets_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "assets" / "poster-style-presets"
        if candidate.is_dir():
            return candidate
    return None


def _local_preset_entries() -> list[dict[str, Any]]:
    root = _local_presets_root()
    if root is None:
        return []
    entries: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for parsed in library.scan_local_presets(root):
        entries.append(
            {
                "id": library.preset_item_id(
                    parsed["preset_id"],
                    parsed["version"],
                ),
                "source": "preset",
                "preset_id": parsed["preset_id"],
                "version": parsed["version"],
                "title": parsed["title"],
                "category": parsed["category"],
                "library_folder": parsed["library_folder"],
                "mood": parsed["mood"],
                "prompt_template": parsed["prompt_template"],
                "palette": parsed["palette"],
                "recommended_aspects": parsed["recommended_aspects"],
                "style_tags": parsed["style_tags"],
                "samples": [],
                "created_at": now,
                "updated_at": now,
            }
        )
    return entries


def _publish_local_presets_if_empty(items: list[dict[str, Any]]) -> bool:
    with library.poster_style_sync_file_lock(_sync_lock_path()):
        state = storage.read_json_file(
            _sync_state_path(),
            _default_sync_state(),
            max_bytes=_INDEX_MAX_BYTES,
        )
        owner = sync_lease_owner(state)
        if owner is not None and owner[1] > datetime.now(timezone.utc):
            return False
        index = _load_index()
        if index.get("preset_items"):
            return False
        index["preset_items"] = items
        _save_index(index)
        return True


async def bootstrap_local_presets_if_empty() -> None:
    if (await asyncio.to_thread(_load_index)).get("preset_items"):
        return
    items = await asyncio.to_thread(_local_preset_entries)
    if not items:
        return
    await asyncio.to_thread(_publish_local_presets_if_empty, items)


async def find_preset_item(
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> dict[str, Any] | None:
    if not item_id.startswith("preset:"):
        return None
    hidden = {
        value
        for value in (
            (
                await db.execute(
                    select(PosterStyleHiddenPreset.preset_id).where(
                        PosterStyleHiddenPreset.user_id == user_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if isinstance(value, str)
    }
    if item_id in hidden:
        return None
    index = await asyncio.to_thread(_load_index)
    for item in index.get("preset_items") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == item_id:
            return dict(item)
    return None
