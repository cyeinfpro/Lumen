"""Manual conversation compaction, queueing, and status services."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    messages_token_count,
    would_exceed_budget,
)
from lumen_core.models import Conversation, Message

from ...arq_pool import get_arq_pool
from .cursor import message_alive_filters
from .context import (
    CIRCUIT_BREAKER_KEY,
    COMPACTION_MESSAGE_LOAD_LIMIT,
    MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS,
    SUMMARY_MODEL_DEFAULT,
    SUMMARY_TARGET_DEFAULT_TOKENS,
    circuit_breaker_retry_after,
    load_prompt_content,
    manual_compact_cooldown_key,
    setting_float,
    setting_int,
    setting_str,
    simple_structured_system_prompt,
    trace_id,
)
from .contracts import ManualCompactIn


MANUAL_COMPACT_JOB_TTL_SECONDS = 24 * 3600
MANUAL_COMPACT_ACTIVE_TTL_SECONDS = 30 * 60
MANUAL_COMPACT_RETRY_AFTER_SECONDS = 2
logger = logging.getLogger(__name__)


def not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "not_found", "message": "conversation not found"}},
    )


def service_unavailable(
    reason: str, *, request: Request | None = None
) -> HTTPException:
    error: dict[str, Any] = {
        "code": "compression_unavailable",
        "message": "compression unavailable",
        "reason": reason,
        "details": {"reason": reason},
    }
    if request is not None:
        tid = getattr(getattr(request, "state", None), "request_id", None)
        if not isinstance(tid, str) or not tid:
            tid = trace_id()
        error["trace_id"] = tid
        error["details"]["trace_id"] = tid
    return HTTPException(status_code=503, detail={"error": error})


async def check_manual_compact_cooldown(
    redis: Any,
    *,
    user_id: str,
    conv_id: str,
    cooldown_seconds: int,
) -> tuple[int, int]:
    if cooldown_seconds <= 0:
        return 1, 0
    key = manual_compact_cooldown_key(user_id=user_id, conv_id=conv_id)
    lua = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])
local ok = redis.call('SET', key, '1', 'EX', ttl, 'NX')
if ok then
  return {1, ttl}
end
local existing_ttl = redis.call('TTL', key)
if existing_ttl < 0 then
  redis.call('EXPIRE', key, ttl)
  existing_ttl = ttl
end
return {0, existing_ttl}
"""
    try:
        result = await redis.eval(lua, 1, key, str(cooldown_seconds))
        allowed = int(result[0])
        raw_reset_seconds = int(result[1])
        reset_seconds = raw_reset_seconds if raw_reset_seconds > 0 else cooldown_seconds
    except Exception as exc:
        tid = trace_id()
        logger.error(
            "manual compact cooldown limiter unavailable",
            exc_info=True,
            extra={"trace_id": tid, "user_id": user_id, "conv_id": conv_id},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "cooldown_limiter_unavailable",
                    "message": "manual compact cooldown limiter unavailable",
                    "trace_id": tid,
                }
            },
            headers={"Retry-After": "1"},
        ) from exc
    if allowed:
        return 0, reset_seconds
    cooldown_minutes = max(1, round(cooldown_seconds / 60))
    raise HTTPException(
        status_code=429,
        detail={
            "error": {
                "code": "manual_compact_cooldown",
                "message": f"同一会话 {cooldown_minutes} 分钟内只能手动压缩一次",
                "rate_limit_remaining": 0,
                "rate_limit_reset_seconds": reset_seconds,
                "details": {
                    "rate_limit_remaining": 0,
                    "rate_limit_reset_seconds": reset_seconds,
                },
            }
        },
        headers={"Retry-After": str(max(1, reset_seconds))},
    )


async def load_messages_for_compaction(
    db: AsyncSession,
    conv_id: str,
) -> list[Message]:
    rows = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conv_id,
                    *message_alive_filters(),
                )
                .order_by(desc(Message.created_at), desc(Message.id))
                .limit(COMPACTION_MESSAGE_LOAD_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


def manual_compact_job_id(
    *,
    user_id: str,
    conv_id: str,
    boundary_id: str,
    extra_instruction: str | None,
    target_tokens: int,
    input_budget: int,
    summary_timeout_s: float,
    model: str,
) -> str:
    raw = json.dumps(
        {
            "user_id": user_id,
            "conv_id": conv_id,
            "boundary_id": boundary_id,
            "extra_instruction": extra_instruction or "",
            "target_tokens": target_tokens,
            "input_budget": input_budget,
            "summary_timeout_s": summary_timeout_s,
            "model": model,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def manual_compact_job_key(
    *,
    user_id: str,
    conv_id: str,
    job_id: str,
) -> str:
    return f"context:manual_compact:job:{user_id}:{conv_id}:{job_id}"


def manual_compact_active_key(*, user_id: str, conv_id: str) -> str:
    return f"context:manual_compact:active:{user_id}:{conv_id}"


async def redis_get_json(redis: Any, key: str) -> dict[str, Any] | None:
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.warning("redis_get_json invalid payload key=%s", key)
        return None
    return value if isinstance(value, dict) else None


async def redis_set_json(
    redis: Any,
    key: str,
    value: dict[str, Any],
    ttl: int,
) -> None:
    raw = json.dumps(value, separators=(",", ":"), default=str)
    setter = getattr(redis, "set", None)
    if setter is not None:
        await setter(key, raw, ex=ttl)
        return
    await redis.setex(key, ttl, raw)


async def redis_set_nx_json(
    redis: Any,
    key: str,
    value: dict[str, Any],
    ttl: int,
) -> bool:
    setter = getattr(redis, "set", None)
    if setter is None:
        return False
    raw = json.dumps(value, separators=(",", ":"), default=str)
    return bool(await setter(key, raw, ex=ttl, nx=True))


def compact_pending_payload(
    *,
    job_id: str,
    status: str = "pending",
    retry_after_seconds: int = MANUAL_COMPACT_RETRY_AFTER_SECONDS,
) -> dict[str, Any]:
    return {
        "status": status,
        "compacted": False,
        "reason": "pending",
        "job_id": job_id,
        "retry_after_seconds": retry_after_seconds,
    }


def compact_payload_from_job(
    job: dict[str, Any] | None,
    *,
    job_id: str,
) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    status = str(job.get("status") or "")
    if status == "succeeded":
        response = job.get("response")
        return response if isinstance(response, dict) else None
    if status in {"queued", "running"}:
        return compact_pending_payload(job_id=job_id, status="pending")
    if status == "failed":
        return {
            "status": "failed",
            "compacted": False,
            "reason": job.get("reason") or "upstream_error",
            "job_id": job_id,
        }
    return None


def classify_compact_failure(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "lock_busy"
    status = str(result.get("status") or "")
    if status in {"circuit_open", "circuit_breaker"}:
        return "circuit_open"
    if status in {"failed", "summary_failed", "cas_failed", "upstream_error"}:
        return "upstream_error"
    if status in {"lock_busy", "lock_wait_timeout"}:
        return "lock_busy"
    return "upstream_error"


def build_compact_summary_payload(
    *,
    result: dict[str, Any],
    conv: Conversation,
) -> dict[str, Any]:
    summary_jsonb = (
        conv.summary_jsonb
        if isinstance(getattr(conv, "summary_jsonb", None), dict)
        else {}
    )
    compressed_at = summary_jsonb.get("compressed_at")
    summary_tokens = int(result.get("summary_tokens") or 0)
    source_token_estimate = int(result.get("source_token_estimate") or 0)
    raw_tokens_freed = result.get("tokens_freed")
    tokens_freed = (
        int(raw_tokens_freed)
        if raw_tokens_freed is not None
        else max(0, source_token_estimate - summary_tokens)
    )
    return {
        "summary_created": bool(result.get("summary_created")),
        "summary_used": bool(result.get("summary_used", True)),
        "summary_up_to_message_id": result.get("summary_up_to_message_id"),
        "summary_up_to_created_at": result.get("summary_up_to_created_at"),
        "tokens": summary_tokens,
        "source_message_count": int(result.get("source_message_count") or 0),
        "source_token_estimate": source_token_estimate,
        "image_caption_count": int(result.get("image_caption_count") or 0),
        "tokens_freed": tokens_freed,
        "fallback_reason": result.get("fallback_reason"),
        "compressed_at": compressed_at,
        "status": result.get("status"),
    }


async def enqueue_manual_compact_job(
    *,
    user_id: str,
    conv_id: str,
    boundary_id: str,
    extra_instruction: str | None,
    target_tokens: int,
    input_budget: int,
    summary_timeout_s: float,
    model: str,
    redis: Any,
    cooldown_seconds: int,
    get_arq_pool_fn: Callable[[], Awaitable[Any]] = get_arq_pool,
) -> dict[str, Any]:
    job_id = manual_compact_job_id(
        user_id=user_id,
        conv_id=conv_id,
        boundary_id=boundary_id,
        extra_instruction=extra_instruction,
        target_tokens=target_tokens,
        input_budget=input_budget,
        summary_timeout_s=summary_timeout_s,
        model=model,
    )
    job_key = manual_compact_job_key(
        user_id=user_id,
        conv_id=conv_id,
        job_id=job_id,
    )
    active_key = manual_compact_active_key(user_id=user_id, conv_id=conv_id)
    cooldown_key = manual_compact_cooldown_key(user_id=user_id, conv_id=conv_id)
    existing = await _safe_redis_get(redis, job_key, "manual compact job status read")
    payload = compact_payload_from_job(existing, job_id=job_id)
    if payload is not None:
        return payload
    active = await _safe_redis_get(redis, active_key, "manual compact active read")
    active_job_id = active.get("job_id") if isinstance(active, dict) else None
    if isinstance(active_job_id, str) and active_job_id:
        return compact_pending_payload(job_id=active_job_id)

    active_payload = {
        "job_id": job_id,
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        locked = await redis_set_nx_json(
            redis,
            active_key,
            active_payload,
            MANUAL_COMPACT_ACTIVE_TTL_SECONDS,
        )
    except Exception as exc:
        raise service_unavailable("upstream_error") from exc
    if not locked:
        active = await _safe_redis_get(
            redis, active_key, "manual compact active recheck"
        )
        active_job_id = active.get("job_id") if isinstance(active, dict) else None
        return compact_pending_payload(
            job_id=active_job_id if isinstance(active_job_id, str) else job_id
        )
    try:
        await check_manual_compact_cooldown(
            redis,
            user_id=user_id,
            conv_id=conv_id,
            cooldown_seconds=cooldown_seconds,
        )
    except HTTPException:
        await _delete_quietly(redis, active_key)
        raise

    now = datetime.now(timezone.utc).isoformat()
    job_payload = {
        "status": "queued",
        "job_id": job_id,
        "user_id": user_id,
        "conv_id": conv_id,
        "boundary_id": boundary_id,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await redis_set_json(
            redis, job_key, job_payload, MANUAL_COMPACT_JOB_TTL_SECONDS
        )
        pool = await get_arq_pool_fn()
        await pool.enqueue_job(
            "manual_compact_conversation",
            user_id,
            conv_id,
            boundary_id,
            job_id,
            extra_instruction,
            target_tokens,
            input_budget,
            summary_timeout_s,
            model,
        )
    except Exception as exc:
        await _delete_quietly(redis, active_key)
        await _delete_quietly(redis, job_key)
        await _delete_quietly(redis, cooldown_key)
        raise service_unavailable("upstream_error") from exc
    return compact_pending_payload(job_id=job_id)


async def _safe_redis_get(
    redis: Any,
    key: str,
    operation: str,
) -> dict[str, Any] | None:
    try:
        return await redis_get_json(redis, key)
    except Exception:
        logger.warning("%s failed", operation, exc_info=True)
        return None


async def _delete_quietly(redis: Any, key: str) -> None:
    try:
        await redis.delete(key)
    except Exception:
        logger.debug("redis cleanup failed key=%s", key, exc_info=True)


def import_worker_context_summary() -> Any | None:
    module_name = "apps.worker.app.tasks.context_summary"
    try:
        return import_module(module_name)
    except ModuleNotFoundError:
        pass
    except Exception:
        logger.warning("worker context summary import failed", exc_info=True)
        return None
    project_root = str(Path(__file__).resolve().parents[5])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    try:
        return import_module(module_name)
    except ModuleNotFoundError:
        logger.warning("worker context summary module not found")
        return None
    except Exception:
        logger.warning("worker context summary import failed", exc_info=True)
        return None


def import_ensure_context_summary() -> Any | None:
    module = import_worker_context_summary()
    return getattr(module, "ensure_context_summary", None) if module else None


async def compact_conversation(
    conv_id: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    body: ManualCompactIn | None,
    *,
    get_redis_fn: Callable[[], Any],
    get_arq_pool_fn: Callable[[], Awaitable[Any]] = get_arq_pool,
    import_ensure_fn: Callable[[], Any | None] = import_ensure_context_summary,
    get_owned_conv_fn: Callable[..., Awaitable[Any]],
) -> dict[str, Any]:
    body = body or ManualCompactIn()
    conv = await get_owned_conv_fn(db, conv_id, user.id)
    boundary = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conv.id,
                *message_alive_filters(),
                Message.role.in_(("user", "assistant")),
            )
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if boundary is None:
        raise HTTPException(status_code=409, detail="no messages to compact")
    if not body.force:
        short_circuit = await _budget_short_circuit(
            db,
            conv=conv,
            user=user,
            body=body,
        )
        if short_circuit is not None:
            return short_circuit

    redis = get_redis_fn()
    target_tokens = await setting_int(
        db,
        "context.summary_target_tokens",
        SUMMARY_TARGET_DEFAULT_TOKENS,
    )
    input_budget = await setting_int(db, "context.summary_input_budget", 80_000)
    summary_timeout_s = await setting_float(
        db,
        "context.summary_http_timeout_s",
        120.0,
    )
    model = await setting_str(db, "context.summary_model", SUMMARY_MODEL_DEFAULT)
    cooldown_seconds = await setting_int(
        db,
        "context.manual_compact_cooldown_seconds",
        MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS,
    )
    retry_after = await circuit_breaker_retry_after(redis)
    if retry_after is not None:
        raise _circuit_open_error(retry_after)
    if body.background:
        return await enqueue_manual_compact_job(
            user_id=user.id,
            conv_id=conv.id,
            boundary_id=boundary.id,
            extra_instruction=body.extra_instruction,
            target_tokens=target_tokens,
            input_budget=input_budget,
            summary_timeout_s=summary_timeout_s,
            model=model,
            redis=redis,
            cooldown_seconds=cooldown_seconds,
            get_arq_pool_fn=get_arq_pool_fn,
        )
    await check_manual_compact_cooldown(
        redis,
        user_id=user.id,
        conv_id=conv.id,
        cooldown_seconds=cooldown_seconds,
    )
    ensure = import_ensure_fn()
    if ensure is None:
        raise service_unavailable("upstream_error", request=request)
    runtime_settings = {
        "context.summary_target_tokens": target_tokens,
        "context.summary_input_budget": input_budget,
        "context.summary_http_timeout_s": summary_timeout_s,
        "context.summary_model": model,
        "redis": redis,
    }
    try:
        result = await ensure(
            db,
            conv,
            boundary,
            runtime_settings,
            force=True,
            extra_instruction=body.extra_instruction,
            trigger="manual",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise service_unavailable("upstream_error", request=request) from exc
    if (
        result is None
        or not isinstance(result, dict)
        or "failed" in str(result.get("status") or "")
    ):
        raise service_unavailable(
            classify_compact_failure(result),
            request=request,
        )
    return {
        "status": "ok",
        "compacted": True,
        "summary": build_compact_summary_payload(result=result, conv=conv),
    }


async def _budget_short_circuit(
    db: AsyncSession,
    *,
    conv: Conversation,
    user: Any,
    body: ManualCompactIn,
) -> dict[str, Any] | None:
    messages = await load_messages_for_compaction(db, conv.id)
    conversation_prompt = await load_prompt_content(
        db,
        user_id=user.id,
        prompt_id=conv.default_system_prompt_id,
    )
    global_prompt = await load_prompt_content(
        db,
        user_id=user.id,
        prompt_id=user.default_system_prompt_id,
    )
    system_prompt = (
        simple_structured_system_prompt(
            global_prompt=global_prompt,
            conversation_prompt=conversation_prompt,
            legacy_conversation_prompt=conv.default_system,
        )
        or ""
    )
    safety_margin = body.safety_margin if body.safety_margin is not None else 4096
    used_tokens = messages_token_count(messages, system_prompt=system_prompt)
    if would_exceed_budget(
        messages,
        system_prompt=system_prompt,
        budget=CONTEXT_INPUT_TOKEN_BUDGET,
        safety_margin=safety_margin,
    ):
        return None
    return {
        "status": "ok",
        "compacted": False,
        "reason": "below_budget",
        "estimated_input_tokens": used_tokens,
        "input_budget_tokens": CONTEXT_INPUT_TOKEN_BUDGET,
        "safety_margin": safety_margin,
    }


def _circuit_open_error(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": {
                "code": "compression_unavailable",
                "message": "compression unavailable",
                "reason": "circuit_open",
                "details": {"reason": "circuit_open"},
            }
        },
        headers={"Retry-After": str(max(1, retry_after))},
    )


async def get_compact_conversation_status(
    conv_id: str,
    job_id: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    *,
    get_redis_fn: Callable[[], Any],
    get_owned_visible_conv_fn: Callable[..., Awaitable[Any]],
) -> dict[str, Any]:
    await get_owned_visible_conv_fn(db, conv_id, user)
    redis = get_redis_fn()
    job_key = manual_compact_job_key(
        user_id=user.id,
        conv_id=conv_id,
        job_id=job_id,
    )
    try:
        job = await redis_get_json(redis, job_key)
    except Exception as exc:
        raise service_unavailable("upstream_error", request=request) from exc
    payload = compact_payload_from_job(job, job_id=job_id)
    if payload is None:
        raise not_found()
    if payload.get("status") == "failed":
        raise service_unavailable(
            str(payload.get("reason") or "upstream_error"),
            request=request,
        )
    return payload


__all__ = [
    "CIRCUIT_BREAKER_KEY",
    "COMPACTION_MESSAGE_LOAD_LIMIT",
    "MANUAL_COMPACT_ACTIVE_TTL_SECONDS",
    "MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS",
    "MANUAL_COMPACT_JOB_TTL_SECONDS",
    "MANUAL_COMPACT_RETRY_AFTER_SECONDS",
    "check_manual_compact_cooldown",
    "classify_compact_failure",
    "compact_conversation",
    "compact_payload_from_job",
    "compact_pending_payload",
    "build_compact_summary_payload",
    "enqueue_manual_compact_job",
    "get_compact_conversation_status",
    "import_ensure_context_summary",
    "import_worker_context_summary",
    "load_messages_for_compaction",
    "manual_compact_active_key",
    "manual_compact_job_id",
    "manual_compact_job_key",
    "redis_get_json",
    "redis_set_json",
    "redis_set_nx_json",
]
