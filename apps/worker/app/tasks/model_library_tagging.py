"""模特库 vision 自动打标签 worker 薄壳。

核心 prompt、JSON 解析和单 provider Responses 调用在
``lumen_core.vision_tagging``；worker 只负责读存储、走 provider_pool failover，
并保留原有 public helper 签名。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from lumen_core.models import Image
from lumen_core.providers import ProviderProxyDefinition
from lumen_core.vision_tagging import (
    DEFAULT_TAGGING_MODEL,
    MODEL_LIBRARY_TAGGING_INSTRUCTIONS,
    PER_PROVIDER_RETRY_ATTEMPTS,
    PER_PROVIDER_RETRY_BACKOFF_S,
    TAGGING_TOTAL_TIMEOUT_S,
    AutoTagResult,
    VisionTaggingUpstreamError,
    _clean_style_tags,
    _normalize_age_segment,
    _normalize_gender,
    _strip_markdown_fences,
    call_vision_tagging_upstream_one,
    image_record_to_data_url,
    parse_model_library_tagging_payload,
)

from ..storage import storage
from ..upstream import UpstreamError, _auth_headers

logger = logging.getLogger(__name__)

_DEFAULT_TAGGING_MODEL = DEFAULT_TAGGING_MODEL
_PER_PROVIDER_RETRY_ATTEMPTS = PER_PROVIDER_RETRY_ATTEMPTS
_PER_PROVIDER_RETRY_BACKOFF_S = PER_PROVIDER_RETRY_BACKOFF_S
_TAGGING_TOTAL_TIMEOUT_S = TAGGING_TOTAL_TIMEOUT_S
_TAGGING_INSTRUCTIONS = MODEL_LIBRARY_TAGGING_INSTRUCTIONS
_parse_tagging_payload = parse_model_library_tagging_payload


async def _image_data_url(image_record: Any) -> str | None:
    storage_key = getattr(image_record, "storage_key", None)
    if not isinstance(storage_key, str) or not storage_key:
        return None
    try:
        raw = await storage.aget_bytes(storage_key)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "model_library_tagging skipped: cannot read image id=%s key=%s err=%s",
            getattr(image_record, "id", None),
            storage_key,
            exc,
        )
        return None
    return image_record_to_data_url(image_record, raw)


async def _call_upstream_one(
    image_record: Any,
    image_url: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    proxy: ProviderProxyDefinition | None = None,
) -> str:
    try:
        return await call_vision_tagging_upstream_one(
            image_id=str(getattr(image_record, "id", "")),
            image_url=image_url,
            model=model,
            base_url=base_url,
            api_key=api_key,
            proxy=proxy,
            purpose="model_library_tagging",
            instructions=_TAGGING_INSTRUCTIONS,
            auth_headers=_auth_headers(api_key),
        )
    except VisionTaggingUpstreamError as exc:
        raise UpstreamError(
            str(exc),
            error_code=exc.error_code,
            status_code=exc.status_code,
        ) from exc


async def _call_upstream(
    image_record: Any,
    image_url: str,
    *,
    model: str,
) -> str | None:
    from ..provider_pool import get_pool, text_provider_attempt
    from ..retry import is_retriable as classify_retriable

    pool = await get_pool()
    providers = await pool.select(route="text")
    last_exc: BaseException | None = None
    attempted_providers: list[str] = []

    for provider in providers:
        attempted_providers.append(provider.name)
        for attempt in range(_PER_PROVIDER_RETRY_ATTEMPTS):
            try:
                api_key = str(getattr(provider, "api_key"))
                kwargs: dict[str, Any] = {
                    "model": model,
                    "base_url": provider.base_url,
                    "api_key": api_key,
                }
                proxy = getattr(provider, "proxy", None)
                if proxy is not None:
                    kwargs["proxy"] = proxy
                with text_provider_attempt(pool, provider) as provider_attempt:
                    try:
                        result = await _call_upstream_one(
                            image_record,
                            image_url,
                            **kwargs,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        provider_attempt.report_exception(exc)
                        raise
                    else:
                        provider_attempt.report_success()
                return result
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                if not decision.retriable:
                    logger.info(
                        "model_library_tagging terminal upstream failure "
                        "image_id=%s provider=%s reason=%s",
                        getattr(image_record, "id", None),
                        provider.name,
                        decision.reason,
                    )
                    return None
                if attempt + 1 < _PER_PROVIDER_RETRY_ATTEMPTS:
                    await asyncio.sleep(_PER_PROVIDER_RETRY_BACKOFF_S * (2**attempt))

    if last_exc is not None:
        logger.info(
            "model_library_tagging upstream failed image_id=%s providers=%s err=%.300s",
            getattr(image_record, "id", None),
            ",".join(attempted_providers) or "<none>",
            str(last_exc),
        )
    return None


async def auto_tag_image_record(
    image_record: Any,
    *,
    model: str = _DEFAULT_TAGGING_MODEL,
) -> AutoTagResult:
    """对一张已加载的 Image ORM 行做 vision tagging。"""
    image_id = str(getattr(image_record, "id", "") or "")
    if not image_id:
        return AutoTagResult(image_id="")
    image_url = await _image_data_url(image_record)
    if image_url is None:
        return AutoTagResult(image_id=image_id)
    try:
        async with asyncio.timeout(_TAGGING_TOTAL_TIMEOUT_S):
            raw = await _call_upstream(image_record, image_url, model=model)
    except (TimeoutError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info("model_library_tagging failed image_id=%s err=%s", image_id, exc)
        return AutoTagResult(image_id=image_id)
    if not raw:
        return AutoTagResult(image_id=image_id)
    return _parse_tagging_payload(image_id, raw)


async def auto_tag_model_image(
    session: Any,
    *,
    image_id: str,
    user_id: str,
    model: str = _DEFAULT_TAGGING_MODEL,
) -> AutoTagResult:
    """从 session 拉 image 行，调 vision，返回结构化字段。"""
    if not image_id or not user_id:
        return AutoTagResult(image_id=image_id or "")
    record = (
        await session.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if record is None:
        return AutoTagResult(image_id=image_id)
    return await auto_tag_image_record(record, model=model)


__all__ = [
    "AutoTagResult",
    "UpstreamError",
    "_clean_style_tags",
    "_normalize_age_segment",
    "_normalize_gender",
    "_parse_tagging_payload",
    "_strip_markdown_fences",
    "auto_tag_image_record",
    "auto_tag_model_image",
]
