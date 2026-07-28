"""Application-owned workflow project lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..domain.models import WorkflowKind
from ..ports.project_lifecycle import (
    ProjectAssetPort,
    ProjectLifecycleRepository,
    ProjectOutputPort,
    ProjectRunRecord,
)
from .errors import WorkflowRequestError


def _invalid_title() -> WorkflowRequestError:
    return WorkflowRequestError(
        status_code=422,
        code="invalid_title",
        message="title cannot be empty",
    )


@dataclass(frozen=True, slots=True)
class ProjectLifecycle:
    repository: ProjectLifecycleRepository
    outputs: ProjectOutputPort
    assets: ProjectAssetPort
    now: Callable[[], datetime]

    async def get(self, *, user_id: str, run_id: str) -> object:
        run = await self.repository.get_owned_run(
            user_id=user_id,
            run_id=run_id,
        )
        return await self.outputs.build_run_out(run)

    async def reconcile(self, *, user_id: str, run_id: str) -> object:
        run = await self.repository.get_owned_run(
            user_id=user_id,
            run_id=run_id,
            for_update=True,
        )
        await self._sync_outputs(run)
        result = await self.outputs.build_run_out(run)
        await self.repository.commit()
        return result

    async def patch_title(
        self,
        *,
        user_id: str,
        run_id: str,
        title: str | None,
    ) -> object:
        run = await self.repository.get_owned_run(
            user_id=user_id,
            run_id=run_id,
            for_update=True,
        )
        if title is not None:
            normalized_title = title.strip()
            if not normalized_title:
                raise _invalid_title()
            run.title = normalized_title
            if run.conversation_id:
                await self.repository.rename_active_conversation(
                    conversation_id=run.conversation_id,
                    user_id=user_id,
                    title=normalized_title,
                )
        result = await self.outputs.build_run_out(run)
        await self.repository.commit()
        return result

    async def delete(
        self,
        *,
        user_id: str,
        run_id: str,
        account_mode: str,
    ) -> dict[str, bool]:
        run = await self.repository.get_owned_run(
            user_id=user_id,
            run_id=run_id,
            for_update=True,
        )
        deleted_at = self.now()
        cleanup = await self.outputs.soft_delete_generated_images(
            run=run,
            deleted_at=deleted_at,
            cancel_message="workflow deleted",
            account_mode=account_mode,
        )
        run.deleted_at = deleted_at
        if run.conversation_id:
            await self.repository.mark_active_conversation_deleted(
                conversation_id=run.conversation_id,
                user_id=user_id,
                deleted_at=deleted_at,
            )
        await self.repository.commit()
        await self.outputs.post_commit_generated_cleanup(
            user_id=user_id,
            cleanup=cleanup,
        )
        return {"ok": True}

    async def add_assets(
        self,
        *,
        user_id: str,
        run_id: str,
        image_ids: Sequence[str],
        asset_type: str,
        source_step_key: str | None,
        label: str | None,
    ) -> object:
        run = await self.repository.get_owned_run(
            user_id=user_id,
            run_id=run_id,
            for_update=True,
        )
        await self._sync_outputs(run)
        effective_step_key = (source_step_key or run.current_step or "").strip()
        await self.assets.attach_assets(
            run=run,
            user_id=user_id,
            image_ids=image_ids,
            asset_type=asset_type,
            source_step_key=effective_step_key,
            label=label,
        )
        result = await self.outputs.build_run_out(run)
        await self.repository.commit()
        return result

    async def _sync_outputs(self, run: ProjectRunRecord) -> None:
        if run.type == WorkflowKind.POSTER_DESIGN.value:
            await self.outputs.sync_poster_outputs(run)
            return
        await self.outputs.sync_standard_outputs(run)


__all__ = ["ProjectLifecycle"]
