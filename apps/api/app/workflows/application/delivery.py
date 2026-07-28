"""Application values for workflow binary delivery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkflowBinaryFile:
    path: Path
    media_type: str
    sha256: str
    size: int


__all__ = ["WorkflowBinaryFile"]
