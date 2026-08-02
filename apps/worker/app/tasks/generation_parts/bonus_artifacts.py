from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

from PIL import Image as PILImage

from lumen_core.model_base import new_uuid7

from ...upstream_parts import (
    GeneratedPayloadInput,
    cleanup_owned_generated_payload,
    materialize_generated_payload,
)
from .image_artifact_contracts import sha256
from .image_metadata import (
    maybe_embed_model_image_metadata_bytes,
    model_image_metadata_from_request,
)
from .services import RunGenerationDeps


logger = logging.getLogger(f"{__package__}.persistence")


@dataclass(slots=True)
class BonusGenerationContext:
    services: RunGenerationDeps
    redis: Any
    user_id: str
    channel: str
    parent_task_id: str
    execution_epoch: int
    attempt: int
    parent_idempotency_key: str
    parent_upstream_request: dict[str, Any] | None
    message_id: str
    action: str
    model: str
    prompt: str
    size_requested: str
    aspect_ratio: str
    input_image_ids: list[str]
    primary_input_image_id: str | None
    references: list[tuple[str, bytes]]
    image_request_options: dict[str, Any]
    b64_result: GeneratedPayloadInput
    revised_prompt: str | None
    upstream_provider: str | None
    upstream_actual_route: str | None
    upstream_actual_source: str | None
    upstream_actual_endpoint: str | None
    billing_meta: dict[str, Any] | None
    idempotency_suffix: str
    extra_upstream_fields: dict[str, Any] | None
    record_model_library_candidate: bool
    settle_billing: bool
    log_label: str


@dataclass(slots=True)
class BonusImageArtifact:
    bonus_generation_id: str
    image_id: str
    raw_image: bytes
    sha256: str
    orig_mime: str
    width: int
    height: int
    blurhash: str | None
    display_bytes: bytes
    display_size: tuple[int, int]
    preview_bytes: bytes
    preview_size: tuple[int, int]
    thumb_bytes: bytes
    thumb_size: tuple[int, int]
    transparent_alpha_recovered: bool
    transparent_qc_payload: dict[str, Any] | None
    transparent_provider: str | None
    image_metadata: dict[str, Any]
    billing_meta: dict[str, Any]
    key_orig: str
    key_display: str
    key_preview: str
    key_thumb: str


async def prepare_bonus_artifact(
    context: BonusGenerationContext,
) -> BonusImageArtifact | None:
    if context.b64_result is None:
        return None
    raw_image = _decode_bonus_image(context)
    if raw_image is None or _bonus_sha_echoed(context, raw_image):
        return None
    processed = await _postprocess_bonus_image(context, raw_image)
    if processed is None:
        return None
    billing_meta = _bonus_billing_meta(context)
    if billing_meta is None:
        return None
    return _build_bonus_artifact(context, processed, billing_meta)


def _decode_bonus_image(
    context: BonusGenerationContext,
) -> bytes | None:
    try:
        return materialize_generated_payload(context.b64_result)
    except (TypeError, ValueError):
        logger.warning(
            "%s image payload decode failed parent=%s",
            context.log_label,
            context.parent_task_id,
        )
        return None
    finally:
        if not isinstance(context.b64_result, str):
            cleanup_owned_generated_payload(context.b64_result)


def _bonus_sha_echoed(
    context: BonusGenerationContext,
    raw_image: bytes,
) -> bool:
    if not context.references:
        return False
    sha = sha256(raw_image)
    echoed = any(sha == reference_sha for reference_sha, _raw in context.references)
    if echoed:
        logger.info(
            "%s sha echoed reference parent=%s; skip",
            context.log_label,
            context.parent_task_id,
        )
    return echoed


async def _postprocess_bonus_image(
    context: BonusGenerationContext,
    raw_image: bytes,
) -> Any | None:
    try:
        return await context.services.provider.postprocess(
            raw_image,
            prompt=context.prompt,
            transparent_requested=(
                context.image_request_options.get("background") == "transparent"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s pillow decode failed parent=%s err=%r",
            context.log_label,
            context.parent_task_id,
            exc,
        )
        return None


def _bonus_billing_meta(
    context: BonusGenerationContext,
) -> dict[str, Any] | None:
    result = (
        dict(context.billing_meta)
        if context.billing_meta is not None
        else {
            "is_dual_race_bonus": True,
            "billing_free": False,
            "billing_label": "billable",
            "billing_policy": "dual_race_loser_settled_separately",
        }
    )
    if result.get("billing_free") is not True and not context.settle_billing:
        logger.warning(
            "%s missing settle_billing for billable image parent=%s",
            context.log_label,
            context.parent_task_id,
        )
        return None
    return result


def _build_bonus_artifact(
    context: BonusGenerationContext,
    processed: Any,
    billing_meta: dict[str, Any],
) -> BonusImageArtifact:
    bonus_generation_id = new_uuid7()
    image_id = new_uuid7()
    extension, mime = _bonus_format(processed.orig_format)
    model_metadata = model_image_metadata_from_request(
        image_id=image_id,
        mime=mime,
        request=context.parent_upstream_request,
        prompt=context.prompt,
    )
    raw_image, sha = _embed_bonus_metadata(
        context,
        processed.raw_image,
        processed.sha256,
        processed.orig_format,
        model_metadata,
    )
    return BonusImageArtifact(
        bonus_generation_id=bonus_generation_id,
        image_id=image_id,
        raw_image=raw_image,
        sha256=sha,
        orig_mime=mime,
        width=processed.width,
        height=processed.height,
        blurhash=processed.blurhash,
        display_bytes=processed.display.bytes,
        display_size=processed.display.size,
        preview_bytes=processed.preview.bytes,
        preview_size=processed.preview.size,
        thumb_bytes=processed.thumb.bytes,
        thumb_size=processed.thumb.size,
        transparent_alpha_recovered=processed.transparent_alpha_recovered,
        transparent_qc_payload=processed.transparent_qc_payload,
        transparent_provider=processed.transparent_provider,
        image_metadata={**model_metadata, **billing_meta},
        billing_meta=billing_meta,
        key_orig=(f"u/{context.user_id}/g/{bonus_generation_id}/orig.{extension}"),
        key_display=(f"u/{context.user_id}/g/{bonus_generation_id}/display2048.webp"),
        key_preview=(f"u/{context.user_id}/g/{bonus_generation_id}/preview1024.webp"),
        key_thumb=(f"u/{context.user_id}/g/{bonus_generation_id}/thumb256.jpg"),
    )


def _bonus_format(orig_format: str) -> tuple[str, str]:
    return (
        {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}[orig_format],
        {
            "PNG": "image/png",
            "WEBP": "image/webp",
            "JPEG": "image/jpeg",
        }[orig_format],
    )


def _embed_bonus_metadata(
    context: BonusGenerationContext,
    raw_image: bytes,
    sha: str,
    orig_format: str,
    model_metadata: dict[str, Any],
) -> tuple[bytes, str]:
    if not model_metadata:
        return raw_image, sha
    try:
        with PILImage.open(io.BytesIO(raw_image)) as image:
            image.load()
            raw_image = maybe_embed_model_image_metadata_bytes(
                image=image,
                fmt=orig_format,
                raw_image=raw_image,
                metadata=model_metadata,
            )
        return raw_image, sha256(raw_image)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "%s model metadata embed skipped parent=%s err=%s",
            context.log_label,
            context.parent_task_id,
            exc,
        )
        return raw_image, sha
