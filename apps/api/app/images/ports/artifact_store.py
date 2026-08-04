from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Set
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from ..domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    PublishedArtifact,
    StagedArtifact,
    StagedSweepBudget,
    StagedSweepResult,
    UploadTicket,
)


class ArtifactStorePort(Protocol):
    async def stage(
        self,
        ticket: UploadTicket,
        source: AsyncIterator[bytes],
        *,
        max_bytes: int,
    ) -> StagedArtifact: ...

    async def publish(
        self,
        staged: StagedArtifact,
        key: ArtifactKey,
    ) -> PublishedArtifact: ...

    async def publish_path(
        self,
        source: Path,
        key: ArtifactKey,
        *,
        expected: ArtifactIdentity,
    ) -> PublishedArtifact: ...

    def artifact_lifecycle_fence(
        self,
        key: ArtifactKey,
        *,
        timeout_seconds: float | None = None,
    ) -> AbstractAsyncContextManager[None]: ...

    async def identity(self, key: ArtifactKey) -> ArtifactIdentity | None: ...

    async def exists(self, key: ArtifactKey) -> bool: ...

    async def open(self, key: ArtifactKey) -> AsyncIterator[bytes]: ...

    async def delete(
        self,
        key: ArtifactKey,
        expected: ArtifactIdentity | None = None,
    ) -> bool: ...

    async def delete_staged(
        self,
        staged: StagedArtifact,
    ) -> bool: ...

    async def sweep_staged(
        self,
        *,
        active_tickets: Set[str] | None,
        stale_before: float,
        budget: StagedSweepBudget,
        load_active_tickets: Callable[[Set[str]], Awaitable[Set[str]]] | None = None,
        before_delete: Callable[[], Awaitable[None]] | None = None,
    ) -> StagedSweepResult: ...

    def processing_path(self, key: ArtifactKey) -> Path: ...
