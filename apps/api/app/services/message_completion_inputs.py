"""Completion input policy: fast defaults, credential models and private SVG sources."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import DEFAULT_CHAT_MODEL, Intent
from lumen_core.model_entities import Image
from lumen_core.schema_models import ChatParamsIn

from .message_submission_billing import http_error as _http
from .message_submission_prompting import TaskCredentialPin


def chat_params_with_fast_default(
    chat_params: ChatParamsIn,
    fast_default: bool,
) -> ChatParamsIn:
    if chat_params.fast is not None:
        return chat_params
    return chat_params.model_copy(update={"fast": fast_default})


def select_chat_task_model(
    credential_pin: TaskCredentialPin | None,
    chat_params: ChatParamsIn,
) -> str:
    return (
        credential_pin.fast_chat_model
        if credential_pin and chat_params.fast and credential_pin.fast_chat_model
        else credential_pin.default_chat_model
        if credential_pin
        else DEFAULT_CHAT_MODEL
    )


async def validate_vision_svg_source(
    db: AsyncSession,
    *,
    user_id: str,
    attachment_ids: list[str],
    intent: Intent,
) -> Image:
    if intent != Intent.VISION_QA or len(attachment_ids) != 1:
        raise _http(
            "invalid_vision_svg_source",
            "Vision SVG requires exactly one source image",
            422,
        )
    image = await db.get(Image, attachment_ids[0])
    if image is None or image.user_id != user_id or image.deleted_at is not None:
        raise _http(
            "invalid_attachment", "source image is not owned or was deleted", 400
        )
    meta = image.metadata_jsonb if isinstance(image.metadata_jsonb, dict) else {}
    frames = meta.get("frame_count", 1)
    if (
        image.artifact_status != "ready"
        or image.mime not in {"image/png", "image/jpeg", "image/webp"}
        or meta.get("is_animated")
        or frames not in (None, 1)
    ):
        raise _http(
            "invalid_vision_svg_source",
            "Vision SVG requires a ready static PNG/JPEG/WebP",
            422,
        )
    if (
        image.width <= 0
        or image.height <= 0
        or image.width * image.height > 16_000_000
        or image.size_bytes <= 0
        or image.size_bytes > 20 * 1024 * 1024
    ):
        raise _http(
            "vision_svg_source_too_large",
            "Vision SVG source exceeds 16 MP or 20 MiB",
            422,
        )
    return image
