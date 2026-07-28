"""Typed payload contracts for generated images."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, TypeAlias
from urllib.parse import urlsplit

DEFAULT_MAX_GENERATED_IMAGE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_GENERATED_BATCH_BYTES = 200 * 1024 * 1024
_MAX_BASE64_HEADER_CHARS = 4096


@dataclass(frozen=True, slots=True)
class InlineImageBytes:
    """Generated image bytes already resident in memory."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("inline image payload must contain bytes")


@dataclass(frozen=True, slots=True)
class StagedImageFile:
    """Generated image stored in an explicitly managed staging file."""

    path: Path
    size: int
    sha256: str
    owned: bool

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("staged image path must be a pathlib.Path")
        if self.size < 0:
            raise ValueError("staged image size must be non-negative")
        normalized_hash = self.sha256.lower()
        if len(normalized_hash) != 64 or any(
            char not in "0123456789abcdef" for char in normalized_hash
        ):
            raise ValueError("staged image sha256 must be a 64-character hex digest")
        object.__setattr__(self, "sha256", normalized_hash)


@dataclass(frozen=True, slots=True)
class RemoteImageResult:
    """Remote result awaiting the controlled streaming-download boundary."""

    url: str
    expected_mime: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("remote image result URL must be non-empty")
        if self.expected_mime is not None and not self.expected_mime:
            raise ValueError("remote image expected MIME must be non-empty or None")


GeneratedPayload: TypeAlias = InlineImageBytes | StagedImageFile | RemoteImageResult
GeneratedPayloadInput: TypeAlias = GeneratedPayload | str
GeneratedImageResult: TypeAlias = tuple[GeneratedPayload, str | None]


def coerce_generated_payload(value: GeneratedPayload | bytes) -> GeneratedPayload:
    """Adapt legacy byte-returning downloaders without copying their body."""
    if isinstance(value, (InlineImageBytes, StagedImageFile, RemoteImageResult)):
        return value
    if isinstance(value, bytes):
        return InlineImageBytes(value)
    raise TypeError(f"unsupported generated payload type: {type(value).__name__}")


def generated_payload_size(payload: GeneratedPayload) -> int | None:
    """Return known local bytes; remote results are accounted after streaming."""
    if isinstance(payload, InlineImageBytes):
        return len(payload.data)
    if isinstance(payload, StagedImageFile):
        return payload.size
    return None


def validate_remote_image_url(
    url: str,
    *,
    allowed_http_hosts: Collection[str] = (),
) -> None:
    """Require HTTPS unless an HTTP host is explicitly allowlisted."""
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        return
    allowed_hosts = {host.casefold() for host in allowed_http_hosts}
    if (
        parsed.scheme == "http"
        and parsed.hostname
        and parsed.hostname.casefold() in allowed_hosts
    ):
        return
    raise ValueError("generated image URL must use HTTPS or an allowlisted HTTP host")


def decode_inline_image_base64(
    value: str,
    *,
    max_bytes: int = DEFAULT_MAX_GENERATED_IMAGE_BYTES,
) -> InlineImageBytes:
    """Decode an inline upstream result after a bounded output-size estimate."""
    if not isinstance(value, str):
        raise TypeError("inline image base64 must be a string")
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    raw = value.strip()
    max_encoded_chars = ((max_bytes + 2) // 3) * 4
    if len(raw) > max_encoded_chars + _MAX_BASE64_HEADER_CHARS:
        raise ValueError("inline image base64 input exceeds size limit")
    if raw[:5].lower() == "data:" and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    if len(raw) > max_encoded_chars:
        raise ValueError("inline image base64 exceeds size limit")

    padding = len(raw) - len(raw.rstrip("="))
    estimated_size = (len(raw) // 4) * 3 - min(padding, 2)
    if estimated_size > max_bytes:
        raise ValueError("inline image decoded bytes exceed size limit")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("inline image contains invalid base64") from exc
    if len(decoded) > max_bytes:
        raise ValueError("inline image decoded bytes exceed size limit")
    return InlineImageBytes(decoded)


def read_generated_payload_bytes(payload: GeneratedPayload) -> bytes:
    """Materialize local payloads for compatibility-only callers."""
    if isinstance(payload, InlineImageBytes):
        return payload.data
    if isinstance(payload, StagedImageFile):
        data = payload.path.read_bytes()
        if len(data) != payload.size:
            raise ValueError("staged image size does not match payload metadata")
        digest = hashlib.sha256(data).hexdigest()
        if digest != payload.sha256:
            raise ValueError("staged image sha256 does not match payload metadata")
        return data
    raise ValueError("remote image result must be streamed before materialization")


def generated_payload_to_base64(payload: GeneratedPayload) -> str:
    """Legacy adapter; new internal callers should retain the tagged payload."""
    return base64.b64encode(read_generated_payload_bytes(payload)).decode("ascii")


def materialize_generated_payload(payload: GeneratedPayloadInput) -> bytes:
    """Materialize the compatibility union without a bytes-to-base64 round trip."""
    if isinstance(payload, str):
        return decode_inline_image_base64(payload).data
    return read_generated_payload_bytes(payload)


def cleanup_owned_generated_payload(payload: GeneratedPayload) -> None:
    """Delete only staging files whose ownership was transferred to the caller."""
    if isinstance(payload, StagedImageFile) and payload.owned:
        payload.path.unlink(missing_ok=True)


__all__ = [
    "DEFAULT_MAX_GENERATED_BATCH_BYTES",
    "DEFAULT_MAX_GENERATED_IMAGE_BYTES",
    "GeneratedImageResult",
    "GeneratedPayload",
    "GeneratedPayloadInput",
    "InlineImageBytes",
    "RemoteImageResult",
    "StagedImageFile",
    "cleanup_owned_generated_payload",
    "coerce_generated_payload",
    "decode_inline_image_base64",
    "generated_payload_size",
    "generated_payload_to_base64",
    "materialize_generated_payload",
    "read_generated_payload_bytes",
    "validate_remote_image_url",
]
