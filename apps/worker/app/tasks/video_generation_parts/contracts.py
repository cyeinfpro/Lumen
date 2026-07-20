"""Shared video generation task contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumen_core.models import Video


@dataclass(frozen=True)
class StoredVideo:
    video: Video
    diagnostics: dict[str, Any]
    created_storage_keys: tuple[str, ...] = ()


class VideoLeaseLost(RuntimeError):
    """Raised when a worker loses its distributed task lease."""


__all__ = ["StoredVideo", "VideoLeaseLost"]
