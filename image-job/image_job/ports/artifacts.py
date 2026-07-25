"""Artifact persistence port."""

from __future__ import annotations

from typing import Protocol


class ArtifactStore(Protocol):
    async def put_reference(
        self,
        *,
        owner_hash: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, object]: ...

    async def readiness_probe(self) -> bool: ...
