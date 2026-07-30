from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    Image,
    ImageVariant,
)
from lumen_core.schema_models import ImageOut

from ...domain.variants import (
    DISPLAY_VARIANT,
    PREVIEW_VARIANT,
    THUMB_VARIANT,
    deterministic_variant_key,
)


def image_url(image_id: str) -> str:
    return f"/api/images/{image_id}/binary"


def variant_url(image_id: str, kind: str) -> str:
    return f"/api/images/{image_id}/variants/{kind}"


def variant_key_for_image(img: Image, kind: str) -> str:
    return deterministic_variant_key(
        image_id=img.id,
        source_key=img.storage_key,
        kind=kind,
    )


async def image_out(db: AsyncSession, img: Image) -> ImageOut:
    variants = (
        (await db.execute(select(ImageVariant).where(ImageVariant.image_id == img.id)))
        .scalars()
        .all()
    )
    kinds = {variant.kind for variant in variants}
    return ImageOut(
        id=img.id,
        source=img.source,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
        width=img.width,
        height=img.height,
        mime=img.mime,
        blurhash=img.blurhash,
        url=image_url(img.id),
        display_url=variant_url(img.id, DISPLAY_VARIANT),
        preview_url=(
            variant_url(img.id, PREVIEW_VARIANT) if PREVIEW_VARIANT in kinds else None
        ),
        thumb_url=(
            variant_url(img.id, THUMB_VARIANT) if THUMB_VARIANT in kinds else None
        ),
        metadata_jsonb=img.metadata_jsonb or {},
    )
