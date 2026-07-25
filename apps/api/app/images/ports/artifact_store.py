from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from ..domain.artifact import (
    ArtifactIdentity,
    ArtifactKey,
    PublishedArtifact,
    StagedArtifact,
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

    async def list_staged(self) -> list[StagedArtifact]: ...

    def processing_path(self, key: ArtifactKey) -> Path: ...
