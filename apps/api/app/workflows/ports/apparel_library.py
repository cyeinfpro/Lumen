"""Typed ports for apparel model-library application use cases."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from lumen_core.schemas import (
    ApparelModelLibraryItemOut,
    ApparelModelLibrarySyncOut,
    ApparelModelLibrarySyncStateOut,
)

from ..domain.json_types import JsonObject
from .runtime_state import AsyncLockPort


class ApparelLibraryUser(Protocol):
    id: str
    role: str


class ApparelLibraryQueryPort(Protocol):
    async def combined_items(
        self,
        *,
        user_id: str,
    ) -> tuple[list[JsonObject], bool]: ...

    def filter_items(
        self,
        items: Sequence[JsonObject],
        *,
        source: str,
        age_segment: str,
        appearance: str,
        query: str,
    ) -> list[JsonObject]: ...

    def item_out(self, item: JsonObject) -> ApparelModelLibraryItemOut: ...

    def sync_state_out(
        self,
        user: ApparelLibraryUser,
    ) -> ApparelModelLibrarySyncStateOut: ...

    async def commit(self) -> None: ...


class ApparelLibrarySyncPort(Protocol):
    def can_sync(self, user: ApparelLibraryUser) -> bool: ...

    def github_contents_url(self) -> str: ...

    async def resolve_sync_proxy(self) -> str | None: ...

    async def close_request_transaction(self) -> None: ...

    async def sync_presets(
        self,
        *,
        contents_url: str,
        sync_lock: AsyncLockPort,
        proxy_url: str | None,
    ) -> ApparelModelLibrarySyncOut: ...


class ApparelLibraryDeletePort(Protocol):
    async def ensure_legacy_migrated(self, *, user_id: str) -> None: ...

    def remove_legacy_private_item(self, *, user_id: str, item_id: str) -> bool: ...

    async def delete_private_row(self, *, user_id: str, item_id: str) -> bool: ...

    async def find_item(
        self,
        *,
        user_id: str,
        item_id: str,
    ) -> JsonObject | None: ...

    async def hide_preset(self, *, user_id: str, item_id: str) -> None: ...

    async def commit(self) -> None: ...


__all__ = [
    "ApparelLibraryDeletePort",
    "ApparelLibraryQueryPort",
    "ApparelLibrarySyncPort",
    "ApparelLibraryUser",
]
