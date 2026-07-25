"""Identity values kept separate from provider credentials."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CallerIdentity:
    service_id: str
    owner_hash: str
    authorization: str = field(repr=False)
    legacy: bool = False


@dataclass(frozen=True)
class UpstreamCredential:
    authorization: str = field(repr=False)
