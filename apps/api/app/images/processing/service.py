from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image as PILImage, UnidentifiedImageError

from lumen_core.image_reference import (
    DEFAULT_REFERENCE_MAX_SIDE,
    ImageReferenceError,
    MaskPreflightError,
    validate_mask_preflight,
)

from ..domain.artifact import ArtifactIdentity
from ..domain.resource_estimate import (
    ImageResourceEstimate,
    estimate_image_resources,
)
from .mask import analyze_mask_file
from .metadata import image_mime_type, sha256_file
from .normalize import normalize_to_jpeg
from .reference_variant import (
    REFERENCE_VARIANT_MIME,
    make_reference_variant_file,
)


class ProcessingError(ValueError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ImageInspection:
    mime: str
    output_mime: str
    mode: str
    width: int
    height: int
    estimate: ImageResourceEstimate


@dataclass(frozen=True)
class PreparedUpload:
    original_path: Path
    mime: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    metadata: dict[str, Any]
    normalized_ref_path: Path
    normalized_ref_meta: dict[str, Any]

    @property
    def original_identity(self) -> ArtifactIdentity:
        return ArtifactIdentity(
            sha256=self.sha256,
            size_bytes=self.size_bytes,
        )

    @property
    def normalized_ref_identity(self) -> ArtifactIdentity:
        return ArtifactIdentity(
            sha256=str(self.normalized_ref_meta["sha256"]),
            size_bytes=int(self.normalized_ref_meta["bytes"]),
        )


def _too_many_pixels(max_pixels: int) -> ProcessingError:
    return ProcessingError(
        "too_many_pixels",
        f"image exceeds safe pixel limit ({max_pixels} pixels)",
        413,
    )


def _enforce_dimensions(
    size: tuple[int, int],
    *,
    max_pixels: int,
    max_long_side: int,
) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise ProcessingError("invalid_image", "invalid image size", 400)
    if width * height > max_pixels:
        raise _too_many_pixels(max_pixels)
    if max(width, height) > max_long_side:
        raise ProcessingError(
            "too_large",
            f"image long side exceeds {max_long_side}px",
            413,
        )


def _reserve_file_bytes(staged: Any, path: Path, *, max_bytes: int) -> int:
    size = path.stat().st_size
    if size > max_bytes:
        raise ProcessingError(
            "too_large",
            f"file exceeds {max_bytes // (1024 * 1024)}MB",
            413,
        )
    lease = getattr(staged, "lease", None)
    if lease is not None:
        lease.reserve_bytes(size)
    return size


class ImageProcessor:
    def inspect(
        self,
        source_path: Path,
        *,
        upload_bytes: int,
        allowed_mime: set[str],
        normalizable_mime: set[str],
        max_pixels: int,
        max_long_side: int,
        mime_resolver: Callable[[PILImage.Image], str] = image_mime_type,
    ) -> ImageInspection:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", PILImage.DecompressionBombWarning)
                with PILImage.open(source_path) as image:
                    width, height = image.size
                    _enforce_dimensions(
                        (width, height),
                        max_pixels=max_pixels,
                        max_long_side=max_long_side,
                    )
                    mime = mime_resolver(image)
                    mode = image.mode
                    image.verify()
            if mime in allowed_mime:
                output_mime = mime
            elif mime in normalizable_mime:
                output_mime = "image/jpeg"
            else:
                raise ProcessingError(
                    "unsupported_mime",
                    f"mime not allowed: {mime}",
                    400,
                )
            return ImageInspection(
                mime=mime,
                output_mime=output_mime,
                mode=mode,
                width=width,
                height=height,
                estimate=estimate_image_resources(
                    width=width,
                    height=height,
                    mode=mode,
                    upload_bytes=upload_bytes,
                ),
            )
        except ProcessingError:
            raise
        except (
            PILImage.DecompressionBombError,
            PILImage.DecompressionBombWarning,
        ) as exc:
            raise _too_many_pixels(max_pixels) from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ProcessingError("invalid_image", "unreadable image", 400) from exc

    def process(
        self,
        staged: Any,
        filename: str | None,
        *,
        allowed_mime: set[str],
        normalizable_mime: set[str],
        max_bytes: int,
        max_pixels: int,
        max_long_side: int,
        mask_requested: bool = False,
        reference_size: tuple[int, int] | None = None,
        metadata_reader: Callable[[PILImage.Image, str | None], dict[str, Any]]
        | None = None,
        mime_resolver: Callable[[PILImage.Image], str] = image_mime_type,
    ) -> PreparedUpload:
        inspection = self.inspect(
            Path(staged.path),
            upload_bytes=int(staged.size_bytes),
            allowed_mime=allowed_mime,
            normalizable_mime=normalizable_mime,
            max_pixels=max_pixels,
            max_long_side=max_long_side,
            mime_resolver=mime_resolver,
        )
        lease = getattr(staged, "lease", None)
        if lease is not None:
            lease.reserve_pixels(inspection.width * inspection.height)
        try:
            with PILImage.open(staged.path) as image:
                metadata = (
                    metadata_reader(image, filename)
                    if metadata_reader is not None
                    else {}
                )

            original_path = Path(staged.path)
            size_bytes = int(staged.size_bytes)
            sha256 = str(staged.sha256)
            mime = inspection.mime
            width = inspection.width
            height = inspection.height
            if mime in normalizable_mime:
                original_path = staged.new_temp_path(suffix=".normalized.jpg")
                width, height = normalize_to_jpeg(Path(staged.path), original_path)
                size_bytes = _reserve_file_bytes(
                    staged,
                    original_path,
                    max_bytes=max_bytes,
                )
                sha256 = sha256_file(original_path)
                metadata["upload_normalized"] = {
                    "source_mime": mime,
                    "target_mime": "image/jpeg",
                    "reason": "unsupported_upload_mime",
                }
                mime = "image/jpeg"

            normalized_ref_path = staged.new_temp_path(suffix=".ref.webp")
            normalized_width, normalized_height = make_reference_variant_file(
                original_path,
                normalized_ref_path,
                max_side=DEFAULT_REFERENCE_MAX_SIDE,
            )
            normalized_size = _reserve_file_bytes(
                staged,
                normalized_ref_path,
                max_bytes=max_bytes,
            )
            normalized_sha256 = sha256_file(normalized_ref_path)
            mask_preflight = analyze_mask_file(
                original_path,
                reference_size=reference_size,
            )
            if mask_requested:
                validate_mask_preflight(mask_preflight)
        except ProcessingError:
            raise
        except (
            PILImage.DecompressionBombError,
            PILImage.DecompressionBombWarning,
        ) as exc:
            raise _too_many_pixels(max_pixels) from exc
        except MaskPreflightError as exc:
            raise ProcessingError(exc.code, exc.message, exc.status_code) from exc
        except ImageReferenceError as exc:
            raise ProcessingError(exc.code, exc.message, exc.status_code) from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ProcessingError("invalid_image", "unreadable image", 400) from exc

        metadata = {
            **metadata,
            "mask_preflight": mask_preflight.to_metadata(),
        }
        return PreparedUpload(
            original_path=original_path,
            mime=mime,
            width=width,
            height=height,
            size_bytes=size_bytes,
            sha256=sha256,
            metadata=metadata,
            normalized_ref_path=normalized_ref_path,
            normalized_ref_meta={
                "mime": REFERENCE_VARIANT_MIME,
                "width": normalized_width,
                "height": normalized_height,
                "sha256": normalized_sha256,
                "bytes": normalized_size,
                "max_side": DEFAULT_REFERENCE_MAX_SIDE,
            },
        )

    def rebuild_reference(
        self,
        source_path: Path,
        output_path: Path,
    ) -> ArtifactIdentity:
        make_reference_variant_file(
            source_path,
            output_path,
            max_side=DEFAULT_REFERENCE_MAX_SIDE,
        )
        return ArtifactIdentity(
            sha256=sha256_file(output_path),
            size_bytes=output_path.stat().st_size,
        )
