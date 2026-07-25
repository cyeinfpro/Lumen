"""Typed request outcomes for the image-job control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ImageJobHandle:
    job_id: str
    upstream_api_key: str = field(repr=False, compare=False)
    status_code: int = 202


@dataclass(frozen=True)
class ImageJobStatus:
    payload: dict[str, Any]
    status_code: int


@dataclass(frozen=True)
class UploadedReference:
    url: str


__all__ = ["ImageJobHandle", "ImageJobStatus", "UploadedReference"]
