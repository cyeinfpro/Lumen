"""Result lookup and ownership checks."""

from __future__ import annotations

import hmac
from typing import Any

from ..contracts import ArtifactCorrupt
from ..domain.identity import CallerIdentity, UpstreamCredential
from .auth import credential_hash


class ResultFailure(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ResultService:
    def __init__(self, repository: Any, persistence: Any) -> None:
        self.repository = repository
        self.persistence = persistence

    async def response_for_row(self, row: Any) -> dict[str, Any]:
        try:
            return await self.persistence.validated_response(row)
        except ArtifactCorrupt as exc:
            await self.persistence.mark_artifact_corrupt(
                str(row["job_id"]),
                reason=exc.reason,
            )
            current = await self.repository.one(
                "SELECT * FROM jobs WHERE job_id = ?",
                (row["job_id"],),
            )
            if current is None:
                raise ResultFailure(404, "image job not found") from exc
            return self.persistence.row_to_response(current)

    async def get(
        self,
        job_id: str,
        caller: CallerIdentity,
        upstream: UpstreamCredential | None = None,
    ) -> dict[str, Any]:
        row = await self.repository.one(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        )
        if row is None:
            raise ResultFailure(404, "image job not found")
        candidates = [caller.owner_hash]
        if upstream is not None:
            candidates.append(credential_hash(upstream.authorization))
        if not any(
            hmac.compare_digest(str(row["auth_hash"]), candidate)
            for candidate in candidates
        ):
            raise ResultFailure(403, "image job belongs to a different key")
        return await self.response_for_row(row)
