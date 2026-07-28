"""Typed persistence/upstream port for model-library auto-tagging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class ModelLibraryTagItem:
    item_id: str
    image_id: str
    style_tags: tuple[str, ...]
    appearance_direction: str | None
    age_segment: str | None
    gender: str | None


@dataclass(frozen=True, slots=True)
class ModelLibraryTagUpdate:
    style_tags: tuple[str, ...] | None
    appearance_direction: str | None
    age_segment: str | None
    gender: str | None
    notes: str | None


class ModelLibraryTaggingPort(Protocol):
    async def ensure_legacy_migrated(self, *, user_id: str) -> bool: ...

    async def load_item(
        self,
        *,
        user_id: str,
        item_id: str,
    ) -> ModelLibraryTagItem | None: ...

    async def fetch_tags(
        self,
        *,
        user_id: str,
        image_id: str,
    ) -> Mapping[str, object]: ...

    async def save_update(
        self,
        *,
        user_id: str,
        item_id: str,
        update: ModelLibraryTagUpdate,
    ) -> None: ...

    async def commit_migration(self) -> None: ...
