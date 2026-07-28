"""Upsert workflow project metadata."""

from __future__ import annotations

from dataclasses import dataclass

from ..ports.project_lifecycle import (
    ProjectLifecycleRepository,
    ProjectOutputPort,
    ProjectRunView,
)
from .errors import WorkflowRequestError


@dataclass(frozen=True, slots=True)
class UpsertWorkflowProjectCommand:
    user_id: str
    run_id: str
    title: str | None


@dataclass(frozen=True, slots=True)
class UpsertWorkflowProject:
    repository: ProjectLifecycleRepository
    outputs: ProjectOutputPort

    async def upsert_project(
        self,
        command: UpsertWorkflowProjectCommand,
    ) -> ProjectRunView:
        run = await self.repository.get_owned_run(
            user_id=command.user_id,
            run_id=command.run_id,
            for_update=True,
        )
        if command.title is not None:
            normalized_title = command.title.strip()
            if not normalized_title:
                raise WorkflowRequestError(
                    status_code=422,
                    code="invalid_title",
                    message="title cannot be empty",
                )
            run.title = normalized_title
            if run.conversation_id:
                await self.repository.rename_active_conversation(
                    conversation_id=run.conversation_id,
                    user_id=command.user_id,
                    title=normalized_title,
                )
        result = await self.outputs.build_run_out(run)
        await self.repository.commit()
        return result


__all__ = ["UpsertWorkflowProject", "UpsertWorkflowProjectCommand"]
