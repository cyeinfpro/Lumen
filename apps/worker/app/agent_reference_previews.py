"""Bounded, cached Agent reference preview assembly."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
from datetime import datetime
from typing import Any

from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_image_tokens import estimate_agent_image_tokens
from lumen_core.byok_retention import (
    ByokRetentionPolicy,
    applies_to_account_mode as byok_retention_applies,
    cutoffs as byok_retention_cutoffs,
)
from lumen_core.model_entities import AgentRunReference, Image, ImageVariant, Message

from . import runtime_settings
from .agent_context_errors import AgentContextError
from .agent_runtime_client import AgentRuntimeReference
from .config import settings
from .storage import storage


logger = logging.getLogger(__name__)
_REFERENCE_SOURCE_MAX_BYTES = 32 * 1024 * 1024
_REFERENCE_MAX_PIXELS = 50_000_000


def current_turn_reference_rows(
    current_user: Message,
    reference_rows: list[AgentRunReference],
) -> list[AgentRunReference]:
    content = current_user.content if isinstance(current_user.content, dict) else {}
    attachments = content.get("attachments")
    if not isinstance(attachments, list):
        return []
    by_image = {reference.image_id: reference for reference in reference_rows}
    selected: list[AgentRunReference] = []
    seen: set[str] = set()
    for attachment in attachments[:16]:
        if not isinstance(attachment, dict):
            continue
        image_id = attachment.get("image_id")
        if not isinstance(image_id, str) or image_id in seen:
            continue
        reference = by_image.get(image_id)
        if reference is None:
            raise AgentContextError("agent_snapshot_incomplete")
        selected.append(reference)
        seen.add(image_id)
    return selected


def encode_reference_preview_with_dimensions(
    raw: bytes, maximum: int
) -> tuple[bytes, int, int]:
    if len(raw) > _REFERENCE_SOURCE_MAX_BYTES:
        raise AgentContextError("agent_reference_too_large")
    try:
        with PILImage.open(io.BytesIO(raw)) as source:
            source.load()
            if source.width * source.height > _REFERENCE_MAX_PIXELS:
                raise AgentContextError("agent_reference_too_large")
            image = source.convert("RGBA" if "A" in source.getbands() else "RGB")
    except AgentContextError:
        raise
    except Exception as exc:
        raise AgentContextError("agent_reference_preview_invalid") from exc
    image.thumbnail((1024, 1024), PILImage.Resampling.LANCZOS)
    for quality in (82, 72, 60, 48):
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=quality, method=4)
        value = output.getvalue()
        if len(value) <= maximum:
            return value, image.width, image.height
        image.thumbnail(
            (max(256, int(image.width * 0.8)), max(256, int(image.height * 0.8))),
            PILImage.Resampling.LANCZOS,
        )
    raise AgentContextError("agent_reference_preview_too_large")


def encode_reference_preview(raw: bytes, maximum: int) -> bytes:
    encoded, _width, _height = encode_reference_preview_with_dimensions(raw, maximum)
    return encoded


async def reference_visible_after(account_mode: str | None) -> datetime | None:
    if not byok_retention_applies(account_mode):
        return None
    hide_enabled = bool(
        await runtime_settings.resolve_int("byok.retention_hide_enabled", 1)
    )
    if not hide_enabled:
        return None
    policy = ByokRetentionPolicy(
        hide_enabled=True,
        hide_days=await runtime_settings.resolve_int("byok.retention_hide_days", 3),
    ).normalized()
    return byok_retention_cutoffs(policy=policy).visible_after


async def reference_previews(
    db: AsyncSession,
    references: list[AgentRunReference],
    *,
    run_user_id: str,
    visible_after: datetime | None,
    provider_api: str,
    redis: Any,
) -> list[AgentRuntimeReference]:
    if not references:
        return []
    image_ids = [reference.image_id for reference in references]
    image_statement = select(Image).where(
        Image.id.in_(image_ids),
        Image.user_id == run_user_id,
        Image.deleted_at.is_(None),
        Image.artifact_status == "ready",
    )
    if visible_after is not None:
        image_statement = image_statement.where(Image.created_at >= visible_after)
    images = list((await db.execute(image_statement)).scalars().all())
    images_by_id = {image.id: image for image in images}
    variants = list(
        (
            await db.execute(
                select(ImageVariant).where(
                    ImageVariant.image_id.in_(image_ids),
                    ImageVariant.kind == "preview1024",
                )
            )
        )
        .scalars()
        .all()
    )
    variants_by_image = {variant.image_id: variant for variant in variants}
    if any(
        reference.user_id != run_user_id or reference.image_id not in images_by_id
        for reference in references
    ):
        raise AgentContextError("agent_reference_not_found")

    semaphore = asyncio.Semaphore(4)

    async def load(reference: AgentRunReference) -> AgentRuntimeReference:
        image = images_by_id[reference.image_id]
        preview = variants_by_image.get(reference.image_id)
        storage_key = preview.storage_key if preview is not None else image.storage_key
        cache_version = hashlib.sha256(
            (
                f"{reference.image_id}\n{storage_key}\n"
                f"{settings.agent_reference_preview_max_bytes}\nagent-preview-v1"
            ).encode("utf-8")
        ).hexdigest()[:32]
        cache_key = f"agent:preview:{cache_version}"
        try:
            cached = await redis.get(cache_key) if redis is not None else None
            if isinstance(cached, bytes):
                cached = cached.decode("utf-8")
            if isinstance(cached, str):
                payload = json.loads(cached)
                encoded = base64.b64decode(payload["data_base64"], validate=True)
                width = int(payload["width"])
                height = int(payload["height"])
                if (
                    0 < len(encoded) <= settings.agent_reference_preview_max_bytes
                    and 0 < width <= 1024
                    and 0 < height <= 1024
                ):
                    estimate = estimate_agent_image_tokens(provider_api, width, height)
                    return AgentRuntimeReference(
                        reference_label=reference.reference_label,
                        role=reference.role,
                        display_label=reference.display_label,
                        mime_type="image/webp",
                        data_base64=payload["data_base64"],
                        width=width,
                        height=height,
                        estimated_input_tokens=estimate.upper,
                        token_policy=estimate.policy_version,
                    )
        except Exception:
            logger.debug("Agent reference preview cache miss", exc_info=True)
        try:
            async with semaphore:
                raw = await asyncio.wait_for(
                    storage.aget_bytes(storage_key), timeout=30
                )
                encoded, width, height = await asyncio.to_thread(
                    encode_reference_preview_with_dimensions,
                    raw,
                    settings.agent_reference_preview_max_bytes,
                )
        except AgentContextError:
            raise
        except Exception as exc:
            raise AgentContextError("agent_reference_preview_unavailable") from exc
        estimate = estimate_agent_image_tokens(provider_api, width, height)
        encoded_base64 = base64.b64encode(encoded).decode("ascii")
        if redis is not None:
            try:
                await redis.set(
                    cache_key,
                    json.dumps(
                        {
                            "data_base64": encoded_base64,
                            "width": width,
                            "height": height,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    ex=3600,
                )
            except Exception:
                logger.debug("Agent reference preview cache write failed")
        return AgentRuntimeReference(
            reference_label=reference.reference_label,
            role=reference.role,
            display_label=reference.display_label,
            mime_type="image/webp",
            data_base64=encoded_base64,
            width=width,
            height=height,
            estimated_input_tokens=estimate.upper,
            token_policy=estimate.policy_version,
        )

    return list(await asyncio.gather(*(load(reference) for reference in references)))


__all__ = [
    "current_turn_reference_rows",
    "encode_reference_preview",
    "reference_previews",
    "reference_visible_after",
]
