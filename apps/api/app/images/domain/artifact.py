from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from types import MappingProxyType


class ArtifactStatus(StrEnum):
    STAGING = "staging"
    PROCESSING = "processing"
    PUBLISHING = "publishing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


_ALLOWED_TRANSITIONS = MappingProxyType(
    {
        ArtifactStatus.STAGING: frozenset(
            {ArtifactStatus.PROCESSING, ArtifactStatus.FAILED}
        ),
        ArtifactStatus.PROCESSING: frozenset(
            {ArtifactStatus.PUBLISHING, ArtifactStatus.FAILED}
        ),
        ArtifactStatus.PUBLISHING: frozenset(
            {ArtifactStatus.READY, ArtifactStatus.FAILED}
        ),
        ArtifactStatus.READY: frozenset(
            {ArtifactStatus.DELETING, ArtifactStatus.FAILED}
        ),
        ArtifactStatus.FAILED: frozenset(
            {ArtifactStatus.PROCESSING, ArtifactStatus.DELETING}
        ),
        ArtifactStatus.DELETING: frozenset(
            {ArtifactStatus.DELETED, ArtifactStatus.FAILED}
        ),
        ArtifactStatus.DELETED: frozenset(),
    }
)


class InvalidArtifactTransition(ValueError):
    pass


def ensure_artifact_transition(
    current: str | ArtifactStatus,
    target: str | ArtifactStatus,
) -> None:
    current_status = ArtifactStatus(current)
    target_status = ArtifactStatus(target)
    if target_status not in _ALLOWED_TRANSITIONS[current_status]:
        raise InvalidArtifactTransition(
            f"invalid image artifact transition: {current_status} -> {target_status}"
        )


@dataclass(frozen=True)
class ArtifactKey:
    value: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.value)
        if (
            not self.value
            or path.is_absolute()
            or ".." in path.parts
            or "." in path.parts
            or "\\" in self.value
        ):
            raise ValueError("artifact key must be a safe relative POSIX path")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class UploadTicket:
    value: str

    def __post_init__(self) -> None:
        if not self.value or "/" in self.value or "\\" in self.value:
            raise ValueError("invalid upload ticket")


@dataclass(frozen=True)
class ArtifactIdentity:
    sha256: str
    size_bytes: int
    device: int | None = None
    inode: int | None = None

    def matches(self, other: "ArtifactIdentity") -> bool:
        if self.sha256 != other.sha256 or self.size_bytes != other.size_bytes:
            return False
        if self.device is not None and self.device != other.device:
            return False
        return self.inode is None or self.inode == other.inode

    def to_json(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "device": self.device,
            "inode": self.inode,
        }

    @classmethod
    def from_json(cls, value: Any) -> "ArtifactIdentity | None":
        if not isinstance(value, dict):
            return None
        sha256 = value.get("sha256")
        size_bytes = value.get("size_bytes")
        if not isinstance(sha256, str) or not isinstance(size_bytes, int):
            return None
        device = value.get("device")
        inode = value.get("inode")
        return cls(
            sha256=sha256,
            size_bytes=size_bytes,
            device=device if isinstance(device, int) else None,
            inode=inode if isinstance(inode, int) else None,
        )


@dataclass(frozen=True)
class StagedArtifact:
    ticket: UploadTicket
    path: str
    identity: ArtifactIdentity
    modified_at: float | None = None


@dataclass(frozen=True)
class PublishedArtifact:
    key: ArtifactKey
    identity: ArtifactIdentity
    created: bool


@dataclass(frozen=True)
class ArtifactManifestItem:
    key: ArtifactKey
    identity: ArtifactIdentity
    mime: str
    required: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "storage_key": self.key.value,
            "identity": self.identity.to_json(),
            "mime": self.mime,
            "required": self.required,
        }

    @classmethod
    def from_json(cls, value: Any) -> "ArtifactManifestItem | None":
        if not isinstance(value, dict):
            return None
        key = value.get("storage_key")
        mime = value.get("mime")
        identity = ArtifactIdentity.from_json(value.get("identity"))
        if not isinstance(key, str) or not isinstance(mime, str) or identity is None:
            return None
        try:
            artifact_key = ArtifactKey(key)
        except ValueError:
            return None
        return cls(
            key=artifact_key,
            identity=identity,
            mime=mime,
            required=value.get("required") is not False,
        )
