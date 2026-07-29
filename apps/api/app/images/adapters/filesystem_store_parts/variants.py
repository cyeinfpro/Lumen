from __future__ import annotations

from pathlib import Path

from ...domain.artifact import ArtifactKey


class FileSystemVariantsMixin:
    def processing_path(self, key: ArtifactKey) -> Path:
        return self._path(key)
