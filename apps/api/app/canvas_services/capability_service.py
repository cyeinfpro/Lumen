"""One public capability view for Canvas model preflight."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas import canonical_hash
from lumen_core.canvas_capabilities import image_capability
from lumen_core.image_models import DEFAULT_IMAGE_MODEL, IMAGE_MODELS

from ..services.video.options import get_video_options


async def canvas_capability_catalog(user: Any, db: AsyncSession) -> dict[str, Any]:
    video = (await get_video_options(user, db)).model_dump(mode="json")
    catalog = {
        "schema_version": 1,
        "default_image_model": DEFAULT_IMAGE_MODEL,
        "image_models": [image_capability(model) for model in sorted(IMAGE_MODELS)],
        "video": video,
    }
    return {**catalog, "version": canonical_hash(catalog)}
