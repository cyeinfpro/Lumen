"""Durable workflow publication port."""

from __future__ import annotations

from typing import Protocol

from ..domain.models import WorkflowRunSnapshot


class WorkflowQueuePort(Protocol):
    async def publish_created(self, run: WorkflowRunSnapshot) -> None: ...

    async def publish_cancelled(self, run: WorkflowRunSnapshot) -> None: ...


__all__ = ["WorkflowQueuePort"]
