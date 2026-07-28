"""Filesystem-backed workflow binary delivery."""

from __future__ import annotations

from ..application.delivery import WorkflowBinaryFile
from .library_storage import open_library_storage_file


def workflow_binary_file(storage_key: str) -> WorkflowBinaryFile:
    path, media_type, sha256 = open_library_storage_file(storage_key)
    return WorkflowBinaryFile(
        path=path,
        media_type=media_type,
        sha256=sha256,
        size=path.stat().st_size,
    )


__all__ = ["workflow_binary_file"]
