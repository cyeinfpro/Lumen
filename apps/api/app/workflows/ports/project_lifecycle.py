"""Typed ports for workflow project lifecycle use cases."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol


class ProjectRunRecord(Protocol):
    id: str
    type: str
    title: str
    conversation_id: str | None
    current_step: str
    deleted_at: datetime | None


class ProjectLifecycleRepository(Protocol):
    async def get_owned_run(
        self,
        *,
        user_id: str,
        run_id: str,
        for_update: bool = False,
    ) -> ProjectRunRecord: ...

    async def rename_active_conversation(
        self,
        *,
        conversation_id: str,
        user_id: str,
        title: str,
    ) -> None: ...

    async def mark_active_conversation_deleted(
        self,
        *,
        conversation_id: str,
        user_id: str,
        deleted_at: datetime,
    ) -> None: ...

    async def commit(self) -> None: ...


class ProjectOutputPort(Protocol):
    async def sync_standard_outputs(self, run: ProjectRunRecord) -> None: ...

    async def sync_poster_outputs(self, run: ProjectRunRecord) -> None: ...

    async def build_run_out(self, run: ProjectRunRecord) -> object: ...

    async def soft_delete_generated_images(
        self,
        *,
        run: ProjectRunRecord,
        deleted_at: datetime,
        cancel_message: str,
        account_mode: str,
    ) -> object: ...

    async def post_commit_generated_cleanup(
        self,
        *,
        user_id: str,
        cleanup: object,
    ) -> None: ...


class ProjectAssetPort(Protocol):
    async def attach_assets(
        self,
        *,
        run: ProjectRunRecord,
        user_id: str,
        image_ids: Sequence[str],
        asset_type: str,
        source_step_key: str,
        label: str | None,
    ) -> None: ...


__all__ = [
    "ProjectAssetPort",
    "ProjectLifecycleRepository",
    "ProjectOutputPort",
    "ProjectRunRecord",
]
