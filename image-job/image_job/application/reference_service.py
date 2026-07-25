"""Reference upload service."""

from __future__ import annotations

from typing import Any

from ..domain.identity import CallerIdentity


class ReferenceFailure(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ReferenceService:
    def __init__(self, artifacts: Any, max_bytes: int) -> None:
        self.artifacts = artifacts
        self.max_bytes = max_bytes

    async def upload(
        self,
        *,
        caller: CallerIdentity,
        content_type: str,
        data: bytes,
    ) -> dict[str, object]:
        if not data:
            raise ReferenceFailure(400, "empty body")
        if len(data) > self.max_bytes:
            raise ReferenceFailure(
                413,
                f"request body exceeds {self.max_bytes} bytes",
            )
        return await self.artifacts.put_reference(
            owner_hash=caller.owner_hash,
            content_type=content_type,
            data=data,
        )
