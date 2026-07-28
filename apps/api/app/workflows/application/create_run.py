"""Create workflow run commands."""

from __future__ import annotations

from dataclasses import dataclass

from ..ports.run_creation import (
    CreateApparelRunCommand,
    CreatePosterRunCommand,
    WorkflowRunCreated,
    WorkflowRunCreationPort,
)
from .errors import WorkflowRequestError


def _require_user_id(user_id: str) -> None:
    if not user_id.strip():
        raise WorkflowRequestError(
            status_code=401,
            code="invalid_user",
            message="user id is required",
        )


@dataclass(frozen=True, slots=True)
class CreateWorkflowRun:
    port: WorkflowRunCreationPort

    async def create_apparel(
        self,
        command: CreateApparelRunCommand,
    ) -> WorkflowRunCreated:
        _require_user_id(command.user_id)
        if not command.product_image_ids:
            raise WorkflowRequestError(
                status_code=422,
                code="missing_product_images",
                message="at least one product image is required",
            )
        return await self.port.create_apparel(command)

    async def create_poster(
        self,
        command: CreatePosterRunCommand,
    ) -> WorkflowRunCreated:
        _require_user_id(command.user_id)
        if not command.copy_text.strip():
            raise WorkflowRequestError(
                status_code=422,
                code="missing_copy_text",
                message="copy_text is required",
            )
        return await self.port.create_poster(command)


__all__ = ["CreateWorkflowRun"]
