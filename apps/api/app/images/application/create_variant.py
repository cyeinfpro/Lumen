from __future__ import annotations

import asyncio
import io
import os
import tempfile
from pathlib import Path

from PIL import Image as PILImage, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, ImageVariant

from ..domain.artifact import ArtifactIdentity, ArtifactKey, ArtifactStatus
from ..domain.variants import DISPLAY_VARIANT, deterministic_variant_key
from ..ports.artifact_store import ArtifactStorePort
from ..processing.metadata import sha256_file


class VariantError(ValueError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def make_display_variant(
    path: Path,
    *,
    max_pixels: int,
    max_side: int = 2048,
) -> tuple[bytes, tuple[int, int]]:
    try:
        image_context = PILImage.open(path)
    except PILImage.DecompressionBombError as exc:
        raise VariantError(
            "too_many_pixels", "image exceeds safe pixel limit", 413
        ) from exc
    except UnidentifiedImageError as exc:
        raise VariantError("invalid_image", "unreadable image", 400) from exc
    with image_context as image:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > max_pixels:
            raise VariantError("too_many_pixels", "image exceeds safe pixel limit", 413)
        image.load()
        image.thumbnail((max_side, max_side))
        output = io.BytesIO()
        with image.convert("RGB") as rgb:
            rgb.save(output, format="WEBP", quality=86, method=4)
        return output.getvalue(), image.size


class CreateVariantService:
    def __init__(
        self,
        *,
        artifacts: ArtifactStorePort,
        max_pixels: int,
    ) -> None:
        self.artifacts = artifacts
        self.max_pixels = max_pixels

    async def ensure_display_variant(
        self,
        db: AsyncSession,
        image: Image,
    ) -> ImageVariant:
        locked = (
            await db.execute(
                select(Image)
                .where(
                    Image.id == image.id,
                    Image.user_id == image.user_id,
                    Image.deleted_at.is_(None),
                    Image.artifact_status == ArtifactStatus.READY.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked is None:
            raise VariantError("not_found", "image not found", 404)
        existing = (
            await db.execute(
                select(ImageVariant).where(
                    ImageVariant.image_id == locked.id,
                    ImageVariant.kind == DISPLAY_VARIANT,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        source_key = ArtifactKey(locked.storage_key)
        if await self.artifacts.identity(source_key) is None:
            raise VariantError("not_found", "binary missing", 404)
        source_path = self.artifacts.processing_path(source_key)
        data, size = await asyncio.to_thread(
            make_display_variant,
            source_path,
            max_pixels=self.max_pixels,
        )
        destination_key = ArtifactKey(
            deterministic_variant_key(
                image_id=locked.id,
                source_key=locked.storage_key,
                kind=DISPLAY_VARIANT,
            )
        )
        fd, name = tempfile.mkstemp(
            prefix=f".{locked.id}.{DISPLAY_VARIANT}-",
            suffix=".webp",
            dir=str(source_path.parent),
        )
        temp_path = Path(name)
        try:
            with open(fd, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            expected = ArtifactIdentity(
                sha256=sha256_file(temp_path),
                size_bytes=temp_path.stat().st_size,
            )
            await self.artifacts.publish_path(
                temp_path,
                destination_key,
                expected=expected,
            )
        finally:
            temp_path.unlink(missing_ok=True)
        variant = ImageVariant(
            image_id=locked.id,
            kind=DISPLAY_VARIANT,
            storage_key=destination_key.value,
            width=size[0],
            height=size[1],
        )
        db.add(variant)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            winner = (
                await db.execute(
                    select(ImageVariant).where(
                        ImageVariant.image_id == locked.id,
                        ImageVariant.kind == DISPLAY_VARIANT,
                    )
                )
            ).scalar_one_or_none()
            if winner is not None:
                return winner
            raise
        return variant
