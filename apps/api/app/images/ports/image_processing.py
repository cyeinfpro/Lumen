from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol


if TYPE_CHECKING:
    from ..processing.service import ImageInspection, PreparedUpload


@dataclass(frozen=True)
class ImageProcessingRequest:
    source_path: Path
    source_size_bytes: int
    source_sha256: str
    filename: str | None
    allowed_mime: frozenset[str]
    normalizable_mime: frozenset[str]
    max_bytes: int
    max_pixels: int
    max_long_side: int
    mask_requested: bool
    reference_size: tuple[int, int] | None
    metadata_profile: str | None
    output_paths: tuple[Path, ...]


@dataclass(frozen=True)
class ImageVariantProcessingRequest:
    source_path: Path
    output_path: Path
    variant: Literal["display_webp", "video_reference_jpeg"]
    max_pixels: int
    max_side: int


@dataclass(frozen=True)
class PreparedImageVariant:
    output_path: Path
    mime: str
    width: int
    height: int
    size_bytes: int
    sha256: str


class ImageProcessingExecutorPort(Protocol):
    async def inspect(
        self,
        source_path: Path,
        *,
        upload_bytes: int,
        allowed_mime: set[str],
        normalizable_mime: set[str],
        max_pixels: int,
        max_long_side: int,
    ) -> ImageInspection: ...

    async def process(
        self,
        request: ImageProcessingRequest,
    ) -> PreparedUpload: ...

    async def render_variant(
        self,
        request: ImageVariantProcessingRequest,
    ) -> PreparedImageVariant: ...

    async def aclose(self) -> None: ...
