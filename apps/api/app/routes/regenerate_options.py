"""Pure option normalization for message regeneration."""

from __future__ import annotations

from typing import Any

from lumen_core.providers import parse_provider_bool
from lumen_core.schemas import ChatParamsIn


IMAGE_RENDER_QUALITY_VALUES = frozenset(("auto", "low", "medium", "high"))
IMAGE_OUTPUT_FORMAT_VALUES = frozenset(("png", "jpeg", "webp"))
IMAGE_BACKGROUND_VALUES = frozenset(("auto", "opaque", "transparent"))
IMAGE_MODERATION_VALUES = frozenset(("auto", "low"))


def chat_params_from_user_content(content: dict[str, Any]) -> ChatParamsIn:
    effort = content.get("reasoning_effort")
    if effort not in ("none", "minimal", "low", "medium", "high", "xhigh"):
        effort = None
    vector_store_ids = content.get("vector_store_ids")
    if not isinstance(vector_store_ids, list):
        vector_store_ids = []
    return ChatParamsIn(
        reasoning_effort=effort,
        fast=content.get("fast") is True,
        web_search=content.get("web_search") is True,
        file_search=content.get("file_search") is True,
        vector_store_ids=[v for v in vector_store_ids if isinstance(v, str)],
        code_interpreter=content.get("code_interpreter") is True,
        image_generation=content.get("image_generation") is True,
    )


def str_option(value: Any, allowed: set[str] | frozenset[str], default: str | None) -> str | None:
    return value if isinstance(value, str) and value in allowed else default


def bool_option(value: Any, default: bool = False) -> bool:
    try:
        return parse_provider_bool(value, default=default)
    except ValueError:
        return default


def compression_option(value: Any) -> int | None:
    if value is None:
        return None
    try:
        compression = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= compression <= 100:
        return compression
    return None
