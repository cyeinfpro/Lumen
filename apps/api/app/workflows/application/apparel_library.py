"""Application use cases for browsing, synchronizing, and deleting apparel models."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from lumen_core.schemas import (
    ApparelModelLibraryListOut,
    ApparelModelLibrarySyncOut,
)

from ..domain.apparel_library import (
    MODEL_LIBRARY_AGE_SEGMENTS,
    MODEL_LIBRARY_APPEARANCES,
    MODEL_LIBRARY_SOURCES,
)
from ..ports.apparel_library import (
    ApparelLibraryDeletePort,
    ApparelLibraryQueryPort,
    ApparelLibrarySyncPort,
    ApparelLibraryUser,
)
from ..ports.runtime_state import AsyncLockPort
from .errors import WorkflowRequestError
from .values import dedupe_nonempty


def _request_error(
    *,
    status_code: int,
    code: str,
    message: str,
) -> WorkflowRequestError:
    return WorkflowRequestError(
        status_code=status_code,
        code=code,
        message=message,
    )


@dataclass(frozen=True, slots=True)
class ApparelLibraryBatchDeleteResult:
    deleted: int
    not_found: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ListApparelModelLibrary:
    port: ApparelLibraryQueryPort

    async def execute(
        self,
        *,
        user: ApparelLibraryUser,
        age_segment: object = "all",
        source: str = "all",
        appearance: str = "all",
        query: str = "",
    ) -> ApparelModelLibraryListOut:
        normalized_source = source.strip() or "all"
        if normalized_source not in MODEL_LIBRARY_SOURCES:
            raise _request_error(
                status_code=422,
                code="invalid_source",
                message="invalid model library source",
            )
        normalized_age = str(age_segment)
        if normalized_age not in MODEL_LIBRARY_AGE_SEGMENTS:
            raise _request_error(
                status_code=422,
                code="invalid_age_segment",
                message="invalid model library age segment",
            )
        normalized_appearance = appearance.strip() or "all"
        if normalized_appearance not in MODEL_LIBRARY_APPEARANCES:
            raise _request_error(
                status_code=422,
                code="invalid_appearance",
                message="invalid model library appearance",
            )
        combined_items, migrated_legacy = await self.port.combined_items(
            user_id=user.id
        )
        items = self.port.filter_items(
            combined_items,
            source=normalized_source,
            age_segment=normalized_age,
            appearance=normalized_appearance,
            query=query,
        )
        if migrated_legacy:
            await self.port.commit()
        return ApparelModelLibraryListOut(
            items=[self.port.item_out(item) for item in items],
            sync=self.port.sync_state_out(user),
        )


@dataclass(frozen=True, slots=True)
class SyncApparelModelLibraryPresets:
    port: ApparelLibrarySyncPort

    async def execute(
        self,
        *,
        user: ApparelLibraryUser,
        sync_lock: AsyncLockPort,
    ) -> ApparelModelLibrarySyncOut:
        if not self.port.can_sync(user):
            raise _request_error(
                status_code=403,
                code="forbidden",
                message="model library preset sync is not allowed",
            )
        proxy_url = await self.port.resolve_sync_proxy()
        await self.port.close_request_transaction()
        return await self.port.sync_presets(
            contents_url=self.port.github_contents_url(),
            sync_lock=sync_lock,
            proxy_url=proxy_url,
        )


@dataclass(frozen=True, slots=True)
class DeleteApparelModelLibraryItems:
    port: ApparelLibraryDeletePort

    async def delete_one(self, *, user_id: str, item_id: str) -> dict[str, bool]:
        await self.port.ensure_legacy_migrated(user_id=user_id)
        if not await self._delete_item(user_id=user_id, item_id=item_id):
            raise _request_error(
                status_code=404,
                code="not_found",
                message="model library item not found",
            )
        await self.port.commit()
        return {"ok": True}

    async def delete_many(
        self,
        *,
        user_id: str,
        item_ids: Iterable[object],
    ) -> ApparelLibraryBatchDeleteResult:
        await self.port.ensure_legacy_migrated(user_id=user_id)
        deleted = 0
        not_found: list[str] = []
        for item_id in dedupe_nonempty(item_ids):
            if await self._delete_item(user_id=user_id, item_id=item_id):
                deleted += 1
            else:
                not_found.append(item_id)
        await self.port.commit()
        return ApparelLibraryBatchDeleteResult(
            deleted=deleted,
            not_found=tuple(not_found),
        )

    async def _delete_item(self, *, user_id: str, item_id: str) -> bool:
        if item_id.startswith("user:"):
            removed_legacy = self.port.remove_legacy_private_item(
                user_id=user_id,
                item_id=item_id,
            )
            removed_row = await self.port.delete_private_row(
                user_id=user_id,
                item_id=item_id,
            )
            return removed_legacy or removed_row

        item = await self.port.find_item(user_id=user_id, item_id=item_id)
        if item is None or item.get("source") != "preset":
            return False
        await self.port.hide_preset(user_id=user_id, item_id=item_id)
        return True


__all__ = [
    "ApparelLibraryBatchDeleteResult",
    "DeleteApparelModelLibraryItems",
    "ListApparelModelLibrary",
    "SyncApparelModelLibraryPresets",
]
