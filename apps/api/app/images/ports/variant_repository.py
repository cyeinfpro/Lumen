from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class VariantSource:
    image_id: str
    user_id: str
    storage_key: str
    sha256: str
    size_bytes: int
    width: int
    height: int


@dataclass(frozen=True)
class VariantRecord:
    image_id: str
    kind: str
    storage_key: str
    width: int
    height: int


@dataclass(frozen=True)
class VariantLookup:
    source: VariantSource | None
    variant: VariantRecord | None


@dataclass(frozen=True)
class VariantClaim:
    image_id: str
    kind: str
    token: str
    source_key: str
    source_sha256: str


class VariantRepositoryPort(Protocol):
    async def lookup(
        self,
        image_id: str,
        kind: str,
        *,
        expected_user_id: str | None = None,
    ) -> VariantLookup: ...

    async def try_claim(
        self,
        source: VariantSource,
        kind: str,
        *,
        token: str,
        lease_until: datetime,
        now: datetime,
    ) -> VariantClaim | None: ...

    async def renew_claim(
        self,
        claim: VariantClaim,
        *,
        lease_until: datetime,
        now: datetime,
    ) -> bool: ...

    async def finalize(
        self,
        claim: VariantClaim,
        variant: VariantRecord,
        *,
        now: datetime,
    ) -> VariantRecord | None: ...

    async def fail(
        self,
        claim: VariantClaim,
        *,
        error_code: str,
        retry_at: datetime,
        now: datetime,
    ) -> None: ...
