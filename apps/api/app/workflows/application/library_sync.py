"""Explicit workflow library synchronization command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class WorkflowLibrarySyncPort(Protocol):
    async def sync(
        self,
        *,
        user_id: str,
        force: bool,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class SyncWorkflowLibrary:
    sync_port: WorkflowLibrarySyncPort

    async def execute(
        self,
        *,
        user_id: str,
        force: bool = False,
    ) -> Mapping[str, Any]:
        return await self.sync_port.sync(user_id=user_id, force=force)


__all__ = ["SyncWorkflowLibrary", "WorkflowLibrarySyncPort"]
