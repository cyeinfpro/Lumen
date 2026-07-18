"""Durable-enough Redis receipt cache for video submission recovery."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from .video_upstream import SubmitResult


logger = logging.getLogger(__name__)

SUBMIT_RESULT_CACHE_TTL_S = 7 * 24 * 60 * 60
_SUBMIT_RESULT_CACHE_PREFIX = "video:submit_result:"


@dataclass(frozen=True)
class CachedSubmitResult:
    result: SubmitResult
    provider_name: str | None = None
    provider_kind: str | None = None


def _submit_result_cache_key(task_id: str) -> str:
    return f"{_SUBMIT_RESULT_CACHE_PREFIX}{task_id}"


async def store_submit_result(
    redis: Any,
    task_id: str,
    result: SubmitResult,
    *,
    provider_name: str | None = None,
    provider_kind: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "provider_task_id": result.provider_task_id,
        "raw": result.raw,
    }
    if provider_name:
        payload["provider_name"] = provider_name
    if provider_kind:
        payload["provider_kind"] = provider_kind
    await redis.set(
        _submit_result_cache_key(task_id),
        json.dumps(payload, separators=(",", ":")),
        ex=SUBMIT_RESULT_CACHE_TTL_S,
    )


async def load_submit_result(
    redis: Any,
    task_id: str,
) -> CachedSubmitResult | None:
    raw = await redis.get(_submit_result_cache_key(task_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.debug(
                "video submit cache is not UTF-8 task=%s",
                task_id,
                exc_info=True,
            )
            return None
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("video submit cache decode failed task=%s", task_id, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    provider_task_id = payload.get("provider_task_id")
    raw_result = payload.get("raw")
    provider_name = payload.get("provider_name")
    provider_kind = payload.get("provider_kind")
    if not isinstance(provider_task_id, str) or not provider_task_id:
        return None
    if not isinstance(raw_result, dict):
        return None
    return CachedSubmitResult(
        result=SubmitResult(provider_task_id=provider_task_id, raw=raw_result),
        provider_name=(
            provider_name if isinstance(provider_name, str) and provider_name else None
        ),
        provider_kind=(
            provider_kind if isinstance(provider_kind, str) and provider_kind else None
        ),
    )


def cached_submit_result(cached: Any) -> SubmitResult:
    if isinstance(cached, CachedSubmitResult):
        return cached.result
    return SubmitResult(
        provider_task_id=str(getattr(cached, "provider_task_id")),
        raw=dict(getattr(cached, "raw")),
    )


def cached_submit_provider_name(cached: Any) -> str | None:
    value = getattr(cached, "provider_name", None)
    return value if isinstance(value, str) and value else None


def cached_submit_provider_kind(cached: Any) -> str | None:
    value = getattr(cached, "provider_kind", None)
    return value if isinstance(value, str) and value else None
