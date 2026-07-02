"""Reference-image helpers for apparel model library generation."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, User
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    build_effective_provider_config,
    endpoint_kind_allowed,
    weighted_priority_order,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.vision_tagging import (
    DEFAULT_TAGGING_MODEL,
    MODEL_LIBRARY_TAGGING_INSTRUCTIONS,
    AutoTagResult,
    VisionTaggingUpstreamError,
    call_vision_tagging_upstream_one,
    image_record_to_data_url,
    parse_model_library_tagging_payload,
)

from ..config import settings
from ..runtime_settings import get_setting

logger = logging.getLogger(__name__)

_REFERENCE_EXTRACT_TOTAL_TIMEOUT_S = 30.0
_REFERENCE_STORAGE_MISSING_NOTE = (
    "参考图文件仍在保存中或缺少存储路径，请稍后重试或重新上传。"
)
_REFERENCE_STORAGE_READ_FAILED_NOTE = "参考图文件读取失败，请重新上传后再试。"


@dataclass(slots=True)
class ReferenceProfile:
    age_segment: str | None = None
    gender: str | None = None
    appearance_direction: str | None = None
    style_tags: list[str] = field(default_factory=list)
    notes: str | None = None

    @classmethod
    def from_auto_tag(cls, result: AutoTagResult) -> "ReferenceProfile":
        return cls(
            age_segment=result.age_segment,
            gender=result.gender,
            appearance_direction=result.appearance_direction,
            style_tags=list(result.style_tags or []),
            notes=result.notes,
        )

    def __bool__(self) -> bool:
        return bool(
            self.age_segment
            or self.gender
            or self.appearance_direction
            or self.style_tags
            or self.notes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "age_segment": self.age_segment,
            "gender": self.gender,
            "appearance_direction": self.appearance_direction,
            "style_tags": list(self.style_tags or []),
            "notes": self.notes,
        }


def _storage_path(storage_key: str) -> Path:
    root = Path(settings.storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise ValueError("invalid storage path")
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise ValueError("absolute storage paths are not allowed")
    path = (root / key_path).resolve()
    path.relative_to(root)
    return path


async def _image_data_url(image: Image) -> str | None:
    storage_key = (image.storage_key or "").strip()
    if not storage_key:
        logger.info(
            "model_library reference extract: image has empty storage_key image_id=%s",
            image.id,
        )
        return None
    try:
        raw = await asyncio.to_thread(_storage_path(storage_key).read_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "model_library reference extract: read image failed image_id=%s key=%s err=%s",
            image.id,
            storage_key,
            exc,
        )
        return None
    return image_record_to_data_url(image, raw)


async def _ordered_response_providers(db: AsyncSession) -> list[Any]:
    spec_providers = get_spec("providers")
    raw_providers = await get_setting(db, spec_providers) if spec_providers else None
    providers, _proxies, _errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    providers = [p for p in providers if endpoint_kind_allowed(p, "responses")]
    counters: dict[int, int] = {}
    return weighted_priority_order(providers, counters)


async def auto_tag_owned_model_library_image(
    db: AsyncSession,
    *,
    user_id: str,
    image_id: str,
    model: str = DEFAULT_TAGGING_MODEL,
) -> AutoTagResult:
    """Run model-library vision tagging in the API process.

    The API and worker share the same prompt/parser/request builder; this
    function only handles DB ownership, local storage bytes, and provider order.
    Expected upstream/storage failures return an empty result.
    """
    if not image_id or not user_id:
        return AutoTagResult(image_id=image_id or "")
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        return AutoTagResult(image_id=image_id)
    storage_key = (image.storage_key or "").strip()
    if not storage_key:
        return AutoTagResult(
            image_id=image_id,
            notes=_REFERENCE_STORAGE_MISSING_NOTE,
        )
    image_url = await _image_data_url(image)
    if image_url is None:
        return AutoTagResult(
            image_id=image_id,
            notes=_REFERENCE_STORAGE_READ_FAILED_NOTE,
        )
    providers = await _ordered_response_providers(db)
    if not providers:
        return AutoTagResult(image_id=image_id)

    last_err: str | None = None
    try:
        async with asyncio.timeout(_REFERENCE_EXTRACT_TOTAL_TIMEOUT_S):
            for provider in providers:
                try:
                    raw = await call_vision_tagging_upstream_one(
                        image_id=image_id,
                        image_url=image_url,
                        model=model,
                        base_url=provider.base_url,
                        api_key=provider.api_key,
                        proxy=getattr(provider, "proxy", None),
                        purpose="model_library_tagging",
                        instructions=MODEL_LIBRARY_TAGGING_INSTRUCTIONS,
                    )
                except VisionTaggingUpstreamError as exc:
                    last_err = f"{exc.error_code}:{exc.status_code}:{exc}"
                    continue
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)
                    continue
                return parse_model_library_tagging_payload(image_id, raw)
    except TimeoutError:
        logger.info("model_library reference extract timed out image_id=%s", image_id)
        return AutoTagResult(image_id=image_id)

    if last_err is not None:
        logger.info(
            "model_library reference extract all providers failed image_id=%s err=%s",
            image_id,
            last_err,
        )
    return AutoTagResult(image_id=image_id)


async def extract_reference_profile(
    *,
    db: AsyncSession,
    user: User,
    image_id: str,
) -> ReferenceProfile:
    """Synchronously extract model-library profile fields from a reference image."""
    result = await auto_tag_owned_model_library_image(
        db,
        user_id=user.id,
        image_id=image_id,
    )
    return ReferenceProfile.from_auto_tag(result)


__all__ = [
    "ReferenceProfile",
    "auto_tag_owned_model_library_image",
    "extract_reference_profile",
]
