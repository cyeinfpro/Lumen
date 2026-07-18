"""Completion Worker——DESIGN §6.5.a + §22.2 + §22.8。

`run_completion(ctx, task_id)` 是 arq 任务入口。流程概览：

1. 幂等读 Completion；终态直接 return
2. UPDATE status=streaming, started_at, attempt++
3. 读同会话最近 N=20 条消息 → 转成 input 列表（§22.2）
4. 组 body（按用户开关可挂 web_search tool）+ stream=True
5. 消费 SSE，每次 delta 累加、每 N 个 token 落一次 PG、publish delta
6. `response.completed` → 记录 tokens，status=succeeded；publish succeeded
7. 失败：按 §6.4 分类重试；超限 → failed
8. 流中断恢复：arq retry 策略会让另一 Worker 重跑；重跑时清空 text 并 publish restarted
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from functools import partial
from typing import Any

import httpx
from PIL import Image as PILImage
from sqlalchemy import select, text as sa_text, update

from .. import billing as worker_billing
from lumen_core import billing as billing_core
from lumen_core.constants import (
    DEFAULT_CHAT_INSTRUCTIONS,
    DEFAULT_CHAT_MODEL,
    EV_COMP_DELTA,
    EV_COMP_FAILED,
    EV_COMP_IMAGE,
    EV_COMP_PROGRESS,
    EV_COMP_RESTARTED,
    EV_COMP_STARTED,
    EV_COMP_SUCCEEDED,
    EV_COMP_THINKING_DELTA,
    CompletionStage,
    CompletionStatus,
    GenerationErrorCode as EC,
    MessageStatus,
    RETRY_BACKOFF_SECONDS,
    task_channel,
)
from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    compose_summary_guardrail as compose_summary_guardrail,
    count_tokens,
    estimate_system_prompt_tokens,
    estimate_text_tokens,
    get_input_budget,
)
from lumen_core.chat_tools import (
    ToolStatus,
    normalize_tool_idle_timeout_seconds,
)
from lumen_core.byok_retention import (
    BYOK_DEFAULT_DELETE_ENABLED,
    ByokRetentionPolicy,
    applies_to_account_mode as byok_retention_applies_to_account_mode,
    cutoffs as byok_retention_cutoffs,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Image,
    ImageVariant,
    Message,
    User,
    new_uuid7,
)
from lumen_core.pricing import parse_usage
from lumen_core.queue_metadata import completion_queue_metadata, merge_queue_metadata

from .. import completion_billing, runtime_settings
from ..db import SessionLocal, affected_rows
from ..byok_runtime import (
    byok_error_message,
    byok_error_to_generation_code,
    classify_user_credential_error,
    record_user_credential_runtime_error,
    resolve_user_credential_runtime,
)
from ..observability import (
    completion_cancel_check_errors_total,
    get_tracer,
    safe_outcome,
    task_duration_seconds,
    upstream_calls_total,
)
from ..image_artifacts import (
    _compute_blurhash as _generation_compute_blurhash,
    _make_display,
    _make_preview,
    _make_thumb,
    _sha256,
)
from ..retry import RetryDecision, is_retriable
from ..sse_publish import publish_event as _publish_sse_event
from ..storage import storage
from ..upstream import UpstreamError, stream_completion
from ..upstream import (
    _extract_response_image_b64,
    _extract_response_revised_prompt,
)
from .state import is_completion_terminal
from .completion_parts.context import (
    PackedContext as PackedContext,
    _estimated_summary_source as _estimated_summary_source,
    _fallback_pack as _fallback_pack,
    _make_quality_probes as _make_quality_probes,
    _pack_with_existing_summary as _pack_with_existing_summary,
    _packed_with_input as _packed_with_input,
)
from .completion_parts.citation_text import (
    _apply_url_citations as _apply_url_citations,
    _extract_completed_output_text as _extract_completed_output_text,
    _extract_url_citations as _extract_url_citations,
    _finalize_completion_text as _finalize_completion_text,
    _markdown_link as _markdown_link,
)
from .completion_parts.tool_state import (
    _CODE_INTERPRETER_TOOL_TYPE as _CODE_INTERPRETER_TOOL_TYPE,
    _CompletionToolTracker as _CompletionToolTracker,
    _FILE_SEARCH_TOOL_TYPE as _FILE_SEARCH_TOOL_TYPE,
    _IMAGE_GENERATION_TOOL_TYPE as _IMAGE_GENERATION_TOOL_TYPE,
    _ToolCallState as _ToolCallState,
    _WEB_SEARCH_TOOL_TYPE as _WEB_SEARCH_TOOL_TYPE,
    _extract_tool_call_update as _extract_tool_call_update,
    _first_str as _first_str,
    _merge_tool_call_state as _merge_tool_call_state,
    _normalize_tool_status as _normalize_tool_status,
    _normalize_tool_type as _normalize_tool_type,
    _summarize_tool_error as _summarize_tool_error,
    _tool_display_label as _tool_display_label,
    _tool_status_rank as _tool_status_rank,
)
from .completion_parts.history import (
    _STICKY_TEXT_CHAR_LIMIT as _STICKY_TEXT_CHAR_LIMIT,
    _SummaryBoundary as _SummaryBoundary,
    _instructions_with_summary_guardrail as _instructions_with_summary_guardrail,
    _message_after_summary as _message_after_summary,
    _message_created_at as _message_created_at,
    _role_eq as _role_eq,
    _sticky_text_from_message as _sticky_text_from_message,
    _summary_age_seconds as _summary_age_seconds,
    _summary_compressed_at as _summary_compressed_at,
    _summary_covers_boundary as _summary_covers_boundary,
    _summary_created_at as _summary_created_at,
    _truncate_sticky_text as _truncate_sticky_text,
    _with_summary_guardrail as _with_summary_guardrail,
)
from .completion_parts.request_metadata import (
    _completion_upstream_provider_event as _completion_upstream_provider_event,
    _content_str_list as _content_str_list,
    _merge_completion_upstream_metadata as _merge_completion_upstream_metadata,
    _normalize_reasoning_effort_for_upstream as _normalize_reasoning_effort_for_upstream,
    _split_csv_ids as _split_csv_ids,
)
from .completion_parts import history as _completion_history
from .completion_parts import context_loading as _completion_context_loading
from .completion_parts import stream as _completion_stream
from .completion_parts import tool_images as _completion_tool_images
from .completion_parts.context_loading import (
    _context_circuit_open as _context_circuit_open,
    _pick_current_user as _pick_current_user,
    _pick_first_user as _pick_first_user,
)
from .completion_parts.stream import (
    _LeaseLost as _LeaseLost,
    _TaskCancelled as _TaskCancelled,
    _ToolIdleTimeout as _ToolIdleTimeout,
    _extract_reasoning_delta as _extract_reasoning_delta,
    _extract_reasoning_text_from_item as _extract_reasoning_text_from_item,
    _extract_reasoning_text_from_response as _extract_reasoning_text_from_response,
    _next_completion_stream_event as _next_completion_stream_event,
    _raise_for_terminal_response_event as _raise_for_terminal_response_event,
)
from .completion_parts.tool_images import (
    _CompletionUsageAccumulator as _CompletionUsageAccumulator,
    _decode_upstream_image_b64 as _decode_upstream_image_b64,
    _completion_event_payload as _completion_event_payload,
    _estimate_completion_request_input_tokens as _estimate_completion_request_input_tokens,
    _estimate_completion_tool_output_tokens as _estimate_completion_tool_output_tokens,
    _extract_image_events_from_response as _extract_image_events_from_response,
    _fallback_completion_usage_tokens as _fallback_completion_usage_tokens,
    _settle_cancelled_completion_billing as _settle_cancelled_completion_billing,
    _tool_image_dedupe_key as _tool_image_dedupe_key,
)
from . import outbox as _completion_outbox

from .generation import (
    _cleanup_storage_on_error,
    _write_generation_files,
)

logger = logging.getLogger(__name__)
_tracer = get_tracer("lumen.worker.completion")
_fallback_completion_tool_image_tokens = (
    completion_billing.fallback_completion_tool_image_tokens
)
_image_output_tokens_for_budget = completion_billing.image_output_tokens_for_budget


async def _resolve_byok_retention_policy() -> ByokRetentionPolicy:
    return ByokRetentionPolicy(
        hide_enabled=bool(
            await runtime_settings.resolve_int("byok.retention_hide_enabled", 1)
        ),
        delete_enabled=bool(
            await runtime_settings.resolve_int(
                "byok.retention_delete_enabled",
                int(BYOK_DEFAULT_DELETE_ENABLED),
            )
        ),
        hide_days=await runtime_settings.resolve_int("byok.retention_hide_days", 3),
        delete_days=await runtime_settings.resolve_int(
            "byok.retention_delete_days",
            7,
        ),
    ).normalized()


async def _message_retention_filter_for_account(account_mode: str | None):
    if not byok_retention_applies_to_account_mode(account_mode):
        return None
    policy = await _resolve_byok_retention_policy()
    if not policy.hide_enabled:
        return None
    return Message.created_at >= byok_retention_cutoffs(policy=policy).visible_after


try:
    from . import context_summary
except Exception:  # noqa: BLE001
    context_summary = None  # type: ignore[assignment]

try:
    from . import memory_extraction
except Exception:  # noqa: BLE001
    memory_extraction = None  # type: ignore[assignment]


_LEASE_TTL_S = 300
_LEASE_RENEW_S = 30
_MAX_ATTEMPTS = 3
_PG_FLUSH_EVERY_CHARS = 128  # 每累计 ~128 字符 flush 一次到 PG
_PG_FLUSH_RETRIES = 3
_PG_FLUSH_BACKOFF_S = 0.2
_CONTEXT_COMPRESSION_ENABLED_DEFAULT = 1
_CONTEXT_COMPRESSION_TRIGGER_PERCENT_DEFAULT = 80
_CONTEXT_SUMMARY_TARGET_TOKENS_DEFAULT = 1200
_CONTEXT_SUMMARY_MIN_RECENT_MESSAGES_DEFAULT = 16
_CONTEXT_SUMMARY_MIN_INTERVAL_SECONDS_DEFAULT = 30
_CHAT_TOOL_VECTOR_STORE_SETTING = "chat.file_search_vector_store_ids"
_CHAT_IMAGE_TOOL_SIZE = "1024x1024"
# GEN-P1-4: 用户点取消后 API 在 Redis 设 task:{id}:cancel=1。worker 在 SSE 循环
# 里每隔若干次 delta 检查一次（控制 Redis 调用频率），命中后立即终止流并标 cancelled。
_CANCEL_CHECK_EVERY_DELTAS = 4
_CANCEL_POLL_INTERVAL_S = 0.1
_MAX_TOOL_INVOCATIONS_DEFAULT = 8
_TOOL_IDLE_TIMEOUT_S_DEFAULT = 30.0
_CHAT_TOOL_IMAGE_BUDGET_SETTING = "chat.tool_image_generation_micro"
_TOOL_LIMIT_FALLBACK_TEXT = (
    "Tool invocation limit reached. Continue with the information already "
    "available and do not call any tools."
)


class _CompletionToolInsufficientBalance(UpstreamError):
    """Wallet balance fell below the image tool budget before publishing output."""


def _count_message_tokens(role: str, content: dict[str, Any] | None) -> int:
    return _completion_history._count_message_tokens_with_counter(
        role,
        content,
        token_counter=count_tokens,
    )


def _estimate_system_prompt_tokens_once(system_prompt: str | None) -> int:
    prompt = system_prompt or DEFAULT_CHAT_INSTRUCTIONS
    estimated = estimate_system_prompt_tokens(prompt)
    if estimated <= 0:
        return 0
    return max(0, estimated - estimate_text_tokens(prompt))


async def _record_completion_upstream_metadata(
    *,
    task_id: str,
    attempt_epoch: int,
    provider_event: dict[str, str],
    fast_mode: bool,
) -> None:
    if not provider_event:
        return
    try:
        async with SessionLocal() as session:
            comp = await session.get(Completion, task_id)
            if comp is None or comp.attempt != attempt_epoch:
                return
            if comp.status not in _RUNNING_COMPLETION_STATUSES:
                return
            comp.upstream_request = _merge_completion_upstream_metadata(
                dict(comp.upstream_request or {}),
                provider_event=provider_event,
                fast_mode=fast_mode,
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "completion upstream metadata write failed task=%s attempt=%s",
            task_id,
            attempt_epoch,
            exc_info=True,
        )


async def _chat_tools_from_content(
    content: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    content = content or {}
    tools: list[dict[str, Any]] = []
    if content.get("web_search") is True:
        tools.append({"type": _WEB_SEARCH_TOOL_TYPE})

    if content.get("file_search") is True:
        vector_store_ids = _content_str_list(content, "vector_store_ids")
        if not vector_store_ids:
            try:
                vector_store_ids = _split_csv_ids(
                    await runtime_settings.resolve(_CHAT_TOOL_VECTOR_STORE_SETTING)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "file_search vector store setting resolve failed: %s", exc
                )
                vector_store_ids = []
        if vector_store_ids:
            tools.append(
                {
                    "type": _FILE_SEARCH_TOOL_TYPE,
                    "vector_store_ids": vector_store_ids,
                }
            )
        else:
            raise UpstreamError(
                "file_search requested but no vector_store_ids are configured",
                error_code="FILE_SEARCH_NOT_CONFIGURED",
                status_code=400,
            )

    if content.get("code_interpreter") is True:
        tools.append(
            {
                "type": _CODE_INTERPRETER_TOOL_TYPE,
                "container": {"type": "auto"},
            }
        )

    if content.get("image_generation") is True:
        tools.append(
            {
                "type": _IMAGE_GENERATION_TOOL_TYPE,
                "model": "gpt-image-2",
                "size": _CHAT_IMAGE_TOOL_SIZE,
                "quality": "medium",
                "output_format": "png",
                "background": "auto",
            }
        )

    return tools


def _configure_chat_tools(body: dict[str, Any], tools: list[dict[str, Any]]) -> None:
    if not tools:
        return
    body["tools"] = tools
    body["tool_choice"] = "auto"
    body["parallel_tool_calls"] = False


async def _settle_failed_completion_billing(
    session: Any,
    completion: Completion,
    *,
    usage_values: tuple[Any, ...],
    reason: str,
) -> None:
    if any(int(value or 0) > 0 for value in usage_values):
        await worker_billing.charge_completion(session, completion)
        return
    await worker_billing.release_completion(
        session,
        completion,
        reason=reason,
    )


async def _publish_completion_tool_progress(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    tool_call: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> None:
    await publish_event(
        redis,
        user_id,
        channel,
        EV_COMP_PROGRESS,
        {
            "completion_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "attempt_epoch": attempt_epoch,
            "stage": "tool_call",
            "tool_call": tool_call,
            "tool_calls": tool_calls,
        },
    )


_COMPLETION_EVENT_HOOKS = _completion_tool_images.CompletionEventHooks(
    session_factory=lambda: SessionLocal(),
    stage_outbox_event=_completion_outbox._stage_outbox_event,
    raw_publish_event=lambda *args, **kwargs: _publish_sse_event(*args, **kwargs),
    new_event_id=new_uuid7,
    user_model=User,
    conversation_model=Conversation,
    logger=logger,
)

_stage_completion_event = partial(
    _completion_tool_images._stage_completion_event,
    hooks=_COMPLETION_EVENT_HOOKS,
)
publish_event = partial(
    _completion_tool_images._publish_completion_event,
    hooks=_COMPLETION_EVENT_HOOKS,
)


async def _deliver_completion_event(
    redis: Any,
    delivery: tuple[str, str, dict[str, Any]],
) -> None:
    await _completion_outbox._deliver_staged_outbox_events(redis, [delivery])


async def _publish_completion_tool_updates(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    tool_tracker: _CompletionToolTracker,
    updates: list[dict[str, Any]],
) -> None:
    tool_calls = tool_tracker.content()
    if len(updates) > 1:
        await publish_event(
            redis,
            user_id,
            channel,
            EV_COMP_PROGRESS,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
                "stage": "tool_call",
                "tool_call": updates[-1],
                "tool_call_updates": updates,
                "tool_calls": tool_calls,
            },
        )
        return
    for tool_call in updates:
        await _publish_completion_tool_progress(
            redis=redis,
            user_id=user_id,
            channel=channel,
            task_id=task_id,
            message_id=message_id,
            attempt=attempt,
            attempt_epoch=attempt_epoch,
            tool_call=tool_call,
            tool_calls=tool_calls,
        )


def _tool_limited_completion_body(body: dict[str, Any]) -> dict[str, Any]:
    fallback = dict(body)
    fallback.pop("tools", None)
    fallback["tool_choice"] = "none"
    fallback["parallel_tool_calls"] = False
    input_items = list(body.get("input") or [])
    input_items.append(
        {
            "role": "user",
            "content": [{"type": "input_text", "text": _TOOL_LIMIT_FALLBACK_TEXT}],
        }
    )
    fallback["input"] = input_items
    return fallback


def _compute_blurhash(img: PILImage.Image) -> str | None:
    return _completion_tool_images._compute_blurhash(
        img,
        compute_blurhash=_generation_compute_blurhash,
    )


def _image_format_and_meta(
    raw_image: bytes,
) -> tuple[
    str,
    str,
    int,
    int,
    str | None,
    bytes,
    tuple[int, int],
    bytes,
    tuple[int, int],
    bytes,
    tuple[int, int],
]:
    return _completion_tool_images._image_format_and_meta(
        raw_image,
        hooks=_completion_tool_images.ToolImageFormatHooks(
            compute_blurhash=_compute_blurhash,
            make_display=_make_display,
            make_preview=_make_preview,
            make_thumb=_make_thumb,
            upstream_error_type=UpstreamError,
            bad_response_error_code=EC.BAD_RESPONSE.value,
        ),
    )


async def _store_completion_tool_image(
    *,
    session: Any,
    task_id: str,
    attempt_epoch: int,
    user_id: str,
    message_id: str,
    raw_image: bytes,
    revised_prompt: str | None,
    billing_budget_micro: int,
) -> dict[str, Any]:
    return await _completion_tool_images._store_completion_tool_image(
        session=session,
        task_id=task_id,
        attempt_epoch=attempt_epoch,
        user_id=user_id,
        message_id=message_id,
        raw_image=raw_image,
        revised_prompt=revised_prompt,
        billing_budget_micro=billing_budget_micro,
        hooks=_completion_tool_images.ToolImageStorageHooks(
            image_format_and_meta=_image_format_and_meta,
            new_uuid7=new_uuid7,
            sha256=_sha256,
            write_generation_files=_write_generation_files,
            cleanup_storage_on_error=_cleanup_storage_on_error,
            record_image_usage=partial(
                _completion_tool_images._record_completion_tool_image_usage,
                hooks=_completion_tool_images.ToolImageUsageHooks(
                    acquire_lock=_acquire_completion_xact_lock,
                    completion_model=Completion,
                    running_statuses=_RUNNING_COMPLETION_STATUSES,
                    superseded_error_type=_CompletionEpochSuperseded,
                    fallback_image_tokens=_fallback_completion_tool_image_tokens,
                ),
            ),
            image_model=Image,
            image_variant_model=ImageVariant,
            message_model=Message,
            storage_public_url=storage.public_url,
        ),
    )


async def _ensure_completion_tool_image_wallet_budget(
    *,
    user_id: str,
    task_id: str,
    reserved_micro: int = 0,
) -> int:
    return await _completion_tool_images._ensure_completion_tool_image_wallet_budget(
        user_id=user_id,
        task_id=task_id,
        reserved_micro=reserved_micro,
        hooks=_completion_tool_images.ToolImageBudgetHooks(
            runtime_settings=runtime_settings,
            session_factory=SessionLocal,
            completion_model=Completion,
            worker_billing=worker_billing,
            billing_core=billing_core,
            insufficient_balance_error_type=_CompletionToolInsufficientBalance,
            budget_setting=_CHAT_TOOL_IMAGE_BUDGET_SETTING,
        ),
    )


async def _store_and_publish_completion_tool_image(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    b64_image: str,
    revised_prompt: str | None,
    reserved_tool_image_micro: int = 0,
) -> tuple[dict[str, Any] | None, int]:
    return await _completion_tool_images._store_and_publish_completion_tool_image(
        redis=redis,
        user_id=user_id,
        channel=channel,
        task_id=task_id,
        message_id=message_id,
        attempt=attempt,
        attempt_epoch=attempt_epoch,
        b64_image=b64_image,
        revised_prompt=revised_prompt,
        reserved_tool_image_micro=reserved_tool_image_micro,
        hooks=_completion_tool_images.ToolImagePublishHooks(
            ensure_wallet_budget=_ensure_completion_tool_image_wallet_budget,
            decode_upstream_image_b64=_decode_upstream_image_b64,
            session_factory=SessionLocal,
            store_tool_image=_store_completion_tool_image,
            publish_event=publish_event,
            upstream_error_type=UpstreamError,
            bad_response_error_code=EC.BAD_RESPONSE.value,
            image_event=EV_COMP_IMAGE,
        ),
    )


async def _is_cancelled(redis: Any, task_id: str) -> bool:
    return await _completion_stream._is_cancelled(
        redis,
        task_id,
        hooks=_completion_stream.CancellationCheckHooks(
            cancel_check_errors_total=completion_cancel_check_errors_total,
            logger=logger,
        ),
    )


async def _raise_if_completion_cancelled(
    redis: Any,
    task_id: str,
    reason: str,
) -> None:
    await _completion_stream._raise_if_completion_cancelled(
        redis,
        task_id,
        reason,
        is_cancelled=_is_cancelled,
    )


async def _watch_completion_cancel(
    redis: Any,
    task_id: str,
    *,
    cancel_requested: asyncio.Event,
    stop_requested: asyncio.Event,
    poll_interval_s: float = _CANCEL_POLL_INTERVAL_S,
) -> None:
    await _completion_stream._watch_completion_cancel(
        redis,
        task_id,
        cancel_requested=cancel_requested,
        stop_requested=stop_requested,
        poll_interval_s=poll_interval_s,
        is_cancelled=_is_cancelled,
    )


async def _iter_completion_stream_with_abort(
    stream: Any,
    *,
    cancel_requested: asyncio.Event,
    lease_lost: asyncio.Event,
    tool_tracker: _CompletionToolTracker,
    tool_idle_timeout_s: float,
) -> Any:
    async for event in _completion_stream._iter_completion_stream_with_abort(
        stream,
        cancel_requested=cancel_requested,
        lease_lost=lease_lost,
        tool_tracker=tool_tracker,
        tool_idle_timeout_s=tool_idle_timeout_s,
        next_event=_next_completion_stream_event,
    ):
        yield event


class _CompletionEpochSuperseded(RuntimeError):
    """Raised when another worker has advanced this completion attempt epoch."""


def _completion_lock_key(completion_id: str) -> int:
    """GEN-P0-6: stable 63-bit int key for pg_advisory_xact_lock。

    pg_advisory_xact_lock 接收 bigint。把 completion UUID 哈希成 63-bit 整数
    （第 64 位留给 PG 的符号位），不同 worker 在同一 completion 上竞争时自动排队；
    事务结束自动释放。这是 `attempt` 作 CAS epoch 之外的第二层保险——即使 CAS
    检查与 UPDATE 之间有客户端重试路径，advisory lock 也能保证互斥。
    """
    import hashlib

    h = hashlib.sha256(completion_id.encode("utf-8", errors="replace")).digest()
    # 取前 8 字节，mask 到 63 bit 正整数
    return int.from_bytes(h[:8], byteorder="big", signed=False) & ((1 << 63) - 1)


async def _acquire_completion_xact_lock(session: Any, completion_id: str) -> None:
    """Best-effort: 在当前事务内拿 pg_advisory_xact_lock。非 Postgres 后端静默跳过。"""
    try:
        key = _completion_lock_key(completion_id)
        await session.execute(
            sa_text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=key)
        )
    except Exception as exc:  # noqa: BLE001
        # SQLite 单测 / 非 PG 环境没有 pg_advisory_xact_lock；退化到 CAS 级保护。
        logger.debug("pg_advisory_xact_lock unavailable: %s", exc)


# ---------------------------------------------------------------------------
# Lease helpers
# ---------------------------------------------------------------------------

_RELEASE_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""

_RUNNING_COMPLETION_STATUSES = (CompletionStatus.STREAMING.value,)


async def _acquire_lease(redis: Any, task_id: str, worker_token: str) -> None:
    ok = await redis.set(
        f"task:{task_id}:lease",
        worker_token,
        ex=_LEASE_TTL_S,
        nx=True,
    )
    if not ok:
        raise _LeaseLost(f"lease already held task={task_id}")


async def _release_lease(redis: Any, task_id: str, worker_token: str) -> None:
    try:
        await redis.eval(
            _RELEASE_LEASE_LUA,
            1,
            f"task:{task_id}:lease",
            worker_token,
        )
    except Exception:  # noqa: BLE001
        logger.debug("completion lease release failed task=%s", task_id, exc_info=True)


async def _lease_renewer(
    redis: Any,
    task_id: str,
    worker_token: str,
    lease_lost: asyncio.Event | None = None,
) -> None:
    """每 30s owner-CAS 续租一次；连续 3 次 Redis 失败或 owner 丢失即退出。"""
    consecutive_failures = 0
    try:
        while True:
            await asyncio.sleep(_LEASE_RENEW_S)
            try:
                ok = await redis.eval(
                    _RENEW_LEASE_LUA,
                    1,
                    f"task:{task_id}:lease",
                    worker_token,
                    str(_LEASE_TTL_S),
                )
                if int(ok or 0) != 1:
                    if lease_lost is not None:
                        lease_lost.set()
                    logger.warning(
                        "completion lease ownership lost task=%s worker=%s",
                        task_id,
                        worker_token,
                    )
                    return
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                logger.warning(
                    "lease renew failed task=%s err=%s streak=%d",
                    task_id,
                    exc,
                    consecutive_failures,
                )
                if consecutive_failures >= 3:
                    if lease_lost is not None:
                        lease_lost.set()
                    logger.error(
                        "lease renewer giving up task=%s failures=%d",
                        task_id,
                        consecutive_failures,
                    )
                    return
    except asyncio.CancelledError:
        raise


async def _cleanup_completion_runtime(
    *,
    redis: Any,
    task_id: str,
    lease_token: str,
    lease_acquired: bool,
    renewer: asyncio.Task[None] | None,
    cancel_stop_requested: asyncio.Event | None,
    cancel_watcher: asyncio.Task[None] | None,
    stream_span_cm: Any | None,
    task_start: float,
    task_outcome: str,
) -> None:
    """Stop background work and release the owned lease even under cancellation."""
    if cancel_stop_requested is not None:
        cancel_stop_requested.set()

    async def _critical_cleanup() -> None:
        for label, task in (
            ("cancel watcher", cancel_watcher),
            ("lease renewer", renewer),
        ):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException:  # noqa: BLE001
                logger.debug(
                    "completion %s cleanup failed task=%s",
                    label,
                    task_id,
                    exc_info=True,
                )
        if lease_acquired:
            await _release_lease(redis, task_id, lease_token)

    cleanup_future = asyncio.ensure_future(_critical_cleanup())
    cancel_during_cleanup = False
    try:
        await asyncio.shield(cleanup_future)
    except asyncio.CancelledError:
        cancel_during_cleanup = True

        def _consume_late_cleanup(task: asyncio.Task[None]) -> None:
            with suppress(BaseException):
                task.result()
            logger.debug("completion late cleanup finished task=%s", task_id)

        cleanup_future.add_done_callback(_consume_late_cleanup)
    finally:
        if stream_span_cm is not None:
            with suppress(BaseException):
                stream_span_cm.__exit__(None, None, None)
        try:
            duration = asyncio.get_event_loop().time() - task_start
            task_duration_seconds.labels(
                kind="completion",
                outcome=safe_outcome(task_outcome),
            ).observe(duration)
        except Exception:  # noqa: BLE001
            pass

    if cancel_during_cleanup:
        raise asyncio.CancelledError()


# ---------------------------------------------------------------------------
# History packing (§22.2)
# ---------------------------------------------------------------------------


async def _attachment_to_data_url(session: Any, image_id: str) -> str | None:
    return await _completion_context_loading._attachment_to_data_url(
        session,
        image_id,
        storage_get_bytes=storage.get_bytes,
        logger=logger,
    )


async def _message_to_input_item(session: Any, m: Message) -> dict[str, Any] | None:
    return await _completion_context_loading._message_to_input_item(
        session,
        m,
        attachment_to_data_url=_attachment_to_data_url,
    )


async def _build_input_from_packed_context(
    session: Any,
    packed: PackedContext,
) -> list[dict[str, Any]]:
    return await _completion_context_loading._build_input_from_packed_context(
        session,
        packed,
        message_to_input_item=_message_to_input_item,
    )


async def _load_rows_desc(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    budget_tokens: int | None,
    system_prompt: str | None,
    retention_filter: Any | None = None,
) -> tuple[list[Message], int, bool]:
    return await _completion_context_loading._load_rows_desc(
        session,
        conversation_id=conversation_id,
        target=target,
        budget_tokens=budget_tokens,
        system_prompt=system_prompt,
        retention_filter=retention_filter,
        count_message_tokens=_count_message_tokens,
        estimate_system_prompt_tokens=_estimate_system_prompt_tokens_once,
    )


async def _load_rows_desc_after_summary(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    summary: dict[str, Any],
    retention_filter: Any | None = None,
) -> list[Message]:
    return await _completion_context_loading._load_rows_desc_after_summary(
        session,
        conversation_id=conversation_id,
        target=target,
        summary=summary,
        retention_filter=retention_filter,
    )


async def _get_message(session: Any, message_id: str | None) -> Message | None:
    return await _completion_context_loading._get_message(
        session,
        message_id,
        logger=logger,
    )


async def _pick_first_user_from_summary(
    session: Any,
    summary: dict[str, Any],
) -> Message | None:
    return await _completion_context_loading._pick_first_user_from_summary(
        session,
        summary,
        get_message=_get_message,
    )


async def _pick_current_user_with_lookup(
    session: Any,
    rows_desc: list[Message],
    target: Message,
    summary: dict[str, Any] | None = None,
) -> Message | None:
    return await _completion_context_loading._pick_current_user_with_lookup(
        session,
        rows_desc,
        target,
        summary,
        get_message=_get_message,
    )


async def _resolve_summary_model() -> str:
    return await _completion_context_loading._resolve_summary_model(
        runtime_settings=runtime_settings,
        logger=logger,
    )


async def _resolve_int_setting(spec_key: str, default: int) -> int:
    return await _completion_context_loading._resolve_int_setting(
        spec_key,
        default,
        runtime_settings=runtime_settings,
        logger=logger,
    )


async def _ensure_context_summary(
    session: Any,
    conv: Conversation,
    boundary: _SummaryBoundary,
    *,
    target_tokens: int,
    model: str,
    redis: Any | None,
) -> dict[str, Any] | None:
    return await _completion_context_loading._ensure_context_summary(
        session,
        conv,
        boundary,
        target_tokens=target_tokens,
        model=model,
        redis=redis,
        service=context_summary,
        logger=logger,
    )


def _context_loading_hooks() -> _completion_context_loading.ContextLoadingHooks:
    return _completion_context_loading.ContextLoadingHooks(
        count_message_tokens=_count_message_tokens,
        count_tokens=count_tokens,
        estimate_system_prompt_tokens=_estimate_system_prompt_tokens_once,
        get_input_budget=get_input_budget,
        message_retention_filter_for_account=_message_retention_filter_for_account,
        resolve_summary_model=_resolve_summary_model,
        resolve_int_setting=_resolve_int_setting,
        ensure_context_summary=_ensure_context_summary,
        build_input_from_packed_context=_build_input_from_packed_context,
        load_rows_desc=_load_rows_desc,
        load_rows_desc_after_summary=_load_rows_desc_after_summary,
        pick_first_user_from_summary=_pick_first_user_from_summary,
        pick_current_user_with_lookup=_pick_current_user_with_lookup,
        pick_first_user=_pick_first_user,
        pick_current_user=_pick_current_user,
        context_circuit_open=_context_circuit_open,
        input_token_budget=CONTEXT_INPUT_TOKEN_BUDGET,
        compression_enabled_default=_CONTEXT_COMPRESSION_ENABLED_DEFAULT,
        compression_trigger_percent_default=(
            _CONTEXT_COMPRESSION_TRIGGER_PERCENT_DEFAULT
        ),
        summary_target_tokens_default=_CONTEXT_SUMMARY_TARGET_TOKENS_DEFAULT,
        summary_min_recent_messages_default=(
            _CONTEXT_SUMMARY_MIN_RECENT_MESSAGES_DEFAULT
        ),
        summary_min_interval_seconds_default=(
            _CONTEXT_SUMMARY_MIN_INTERVAL_SECONDS_DEFAULT
        ),
        logger=logger,
    )


async def _pack_recent_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
    redis: Any | None = None,
    chat_model: str | None = None,
    account_mode: str | None = None,
) -> PackedContext:
    return await _completion_context_loading._pack_recent_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
        redis=redis,
        chat_model=chat_model,
        account_mode=account_mode,
        hooks=_context_loading_hooks(),
    )


async def _build_input_from_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
) -> list[dict[str, Any]]:
    return await _completion_context_loading._build_input_from_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
        pack_recent_history=_pack_recent_history,
    )


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_exception(exc: BaseException, has_partial: bool) -> RetryDecision:
    if isinstance(exc, UpstreamError):
        return is_retriable(
            exc.error_code, exc.status_code, has_partial, error_message=str(exc)
        )
    if isinstance(exc, billing_core.BillingError):
        return is_retriable(
            exc.code,
            exc.status_code,
            has_partial,
            error_message=exc.message,
        )
    if isinstance(
        exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)
    ):
        return is_retriable(
            "stream_interrupted" if has_partial else "upstream_error",
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, httpx.HTTPError):
        return is_retriable("upstream_error", None, has_partial, error_message=str(exc))
    return RetryDecision(False, f"unhandled {type(exc).__name__}")


def _bounded_next_attempt(current_attempt: int | None) -> tuple[int, bool]:
    """Return capped next attempt and whether it may run upstream."""
    next_attempt = min((current_attempt or 0) + 1, _MAX_ATTEMPTS + 1)
    return next_attempt, next_attempt <= _MAX_ATTEMPTS


def _context_metadata(packed: PackedContext) -> dict[str, Any]:
    return {
        "estimated_input_tokens": packed.estimated_tokens,
        "included_messages_count": packed.included_messages_count,
        "summary_used": packed.summary_used,
        "summary_created": packed.summary_created,
        "sticky_used": packed.sticky_used,
        "summary_up_to_message_id": packed.summary_up_to_message_id,
        "fallback_reason": packed.fallback_reason,
        "compressor_model": packed.compressor_model,
        "image_caption_count": packed.image_caption_count,
        "quality_probes": packed.quality_probes or _make_quality_probes(packed),
    }


def _append_text_to_first_system(
    input_list: list[dict[str, Any]],
    text: str,
) -> None:
    if not text:
        return
    for item in input_list:
        if item.get("role") != "system":
            continue
        content = item.get("content")
        if not isinstance(content, list) or not content:
            continue
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "input_text":
            old = first.get("text") if isinstance(first.get("text"), str) else ""
            first["text"] = f"{old.rstrip()}\n\n{text}" if old else text
            return
    input_list.insert(
        0,
        {"role": "system", "content": [{"type": "input_text", "text": text}]},
    )


def _insert_user_context_after_summary(
    input_list: list[dict[str, Any]],
    text: str,
) -> None:
    if not text:
        return
    item = {"role": "user", "content": [{"type": "input_text", "text": text}]}
    insert_at = 1
    for idx, existing in enumerate(input_list):
        content = existing.get("content")
        if not isinstance(content, list):
            continue
        joined = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"input_text", "output_text"}
        )
        if "CONVERSATION SUMMARY" in joined or "会话摘要" in joined:
            insert_at = idx + 1
    input_list.insert(insert_at, item)


async def _inject_user_memory_context(
    session: Any,
    *,
    input_list: list[dict[str, Any]],
    user_id: str,
    conversation_id: str | None,
    parent_user_message_id: str | None,
    redis: Any | None = None,
) -> dict[str, Any]:
    if (
        memory_extraction is None
        or conversation_id is None
        or not parent_user_message_id
    ):
        return {"used_memory_ids": [], "used_memory_summary": []}
    parent = await session.get(Message, parent_user_message_id)
    if parent is None:
        return {"used_memory_ids": [], "used_memory_summary": []}
    parent_content = parent.content if isinstance(parent.content, dict) else {}
    user_text = parent_content.get("text") if isinstance(parent_content, dict) else ""
    if not isinstance(user_text, str):
        user_text = ""
    assembled = await memory_extraction.assemble_user_memory_prompt(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        user_text=user_text,
        redis=redis,
        parent_user_message_id=parent_user_message_id,
    )
    head_sections = "\n\n".join(
        section
        for section in (
            assembled.scope_hint_text,
            assembled.profile_text,
            assembled.constraints_text,
            assembled.confirmation_instruction,
        )
        if section
    )
    if head_sections:
        _append_text_to_first_system(input_list, head_sections)
    if assembled.context_text:
        _insert_user_context_after_summary(input_list, assembled.context_text)
    return {
        "used_memory_ids": assembled.used_memory_ids,
        "used_memory_summary": assembled.used_memory_summary,
        "confirmation_candidate_id": assembled.confirmation_candidate_id,
    }


async def _record_completion_context_metadata(
    session: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    packed: PackedContext,
) -> None:
    if not packed.compression_enabled:
        return
    comp = await session.get(Completion, task_id)
    if comp is None or comp.attempt != attempt_epoch:
        return
    upstream_request = dict(comp.upstream_request or {})
    upstream_request["context"] = _context_metadata(packed)
    comp.upstream_request = upstream_request
    await session.commit()


async def _flush_completion_text(
    task_id: str,
    text: str,
    *,
    attempt_epoch: int,
    retries: int = _PG_FLUSH_RETRIES,
) -> None:
    """Flush streamed text to PG, retrying transient commit/update failures.

    The attempt guard is the minimal epoch contract: an older worker must never
    overwrite text once a newer run has advanced Completion.attempt.
    """
    last_exc: BaseException | None = None
    for idx in range(retries):
        try:
            async with SessionLocal() as session:
                res = await session.execute(
                    update(Completion)
                    .where(
                        Completion.id == task_id,
                        Completion.attempt == attempt_epoch,
                        Completion.status == CompletionStatus.STREAMING.value,
                    )
                    .values(text=text)
                )
                if affected_rows(res) == 0:
                    raise _CompletionEpochSuperseded(
                        f"completion epoch superseded task={task_id} "
                        f"attempt_epoch={attempt_epoch}"
                    )
                await session.commit()
                return
        except _CompletionEpochSuperseded:
            # Keep stale-worker fencing strict: run_completion catches this
            # sentinel and exits without writing terminal state.
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "completion text flush failed task=%s attempt_epoch=%s "
                "try=%d/%d err=%s",
                task_id,
                attempt_epoch,
                idx + 1,
                retries,
                exc,
            )
            if idx + 1 < retries:
                await asyncio.sleep(_PG_FLUSH_BACKOFF_S * (2**idx))

    raise UpstreamError(
        "completion text flush failed after retries",
        error_code=EC.UPSTREAM_ERROR.value,
        status_code=None,
    ) from last_exc


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def _completion_preflight_failure(
    session: Any,
    completion: Completion,
) -> tuple[int, tuple[str, str] | None]:
    window_failure = await worker_billing.completion_window_rate_limit_failure(
        session,
        completion,
    )
    if window_failure is not None:
        return int(completion.attempt or 0), window_failure
    attempt, may_run = _bounded_next_attempt(completion.attempt)
    if may_run:
        return attempt, None
    return (
        attempt,
        (
            "max_attempts_exceeded",
            f"completion exceeded max attempts ({_MAX_ATTEMPTS})",
        ),
    )


async def run_completion(ctx: dict[str, Any], task_id: str) -> None:  # noqa: PLR0915, PLR0912
    redis = ctx["redis"]
    worker_id = str(ctx.get("worker_id") or ctx.get("job_id") or "worker")
    lease_token = f"{worker_id}:{new_uuid7()}"
    _task_start = asyncio.get_event_loop().time()
    _task_outcome = "unknown"
    attempt = 0
    attempt_epoch = 0
    user_api_credential_id: str | None = None
    account_mode = "wallet"
    runtime_override: Any | None = None
    queue_metadata_payload: dict[str, Any] = {}
    lease_lost = asyncio.Event()
    lease_acquired = False
    renewer: asyncio.Task[None] | None = None
    cancel_stop_requested: asyncio.Event | None = None
    cancel_watcher: asyncio.Task[None] | None = None
    _stream_span_cm = None
    setup_complete = False

    try:
        # Redis lease is the first ownership fence. No completion row mutation
        # may happen until this invocation owns its unique token.
        await _acquire_lease(redis, task_id, lease_token)
        lease_acquired = True
        renewer = asyncio.create_task(
            _lease_renewer(redis, task_id, lease_token, lease_lost)
        )

        # --- 1. claim completion row after lease ownership ---
        async with SessionLocal() as session:
            # The PG lock serializes claim transitions among lease owners and
            # protects against mixed-version workers during rolling updates.
            await _acquire_completion_xact_lock(session, task_id)

            comp: Completion | None = (
                await session.execute(
                    select(Completion).where(Completion.id == task_id).with_for_update()
                )
            ).scalar_one_or_none()
            if comp is None:
                logger.warning("completion not found task_id=%s", task_id)
                _task_outcome = "not_found"
                return
            if is_completion_terminal(comp.status):
                logger.info(
                    "completion terminal task_id=%s status=%s", task_id, comp.status
                )
                _task_outcome = "terminal"
                return
            if lease_lost.is_set():
                raise _LeaseLost("lease lost before completion claim")

            # 判断是否是"被接管重跑"——attempt > 0 且 text 非空 ⇒ 上一个 worker 挂了
            was_restarted = (comp.attempt or 0) > 0 and bool(comp.text)

            user_id = comp.user_id
            message_id = comp.message_id
            system_prompt = comp.system_prompt
            user_api_credential_id = getattr(comp, "user_api_credential_id", None)
            user_row = await session.get(User, user_id)
            account_mode = getattr(user_row, "account_mode", "wallet")
            # 关键：chat 走 /v1/responses 但要用聊天模型（gpt-5.5 等），
            # 而 UPSTREAM_MODEL 是图像模型 gpt-image-2，不能跨用
            chat_model = comp.model or DEFAULT_CHAT_MODEL

            attempt, preflight_failure = await _completion_preflight_failure(
                session,
                comp,
            )
            attempt_epoch = attempt
            if lease_lost.is_set():
                raise _LeaseLost("lease lost during completion preflight")
            if preflight_failure is not None:
                err_code, err_msg = preflight_failure
                comp.status = CompletionStatus.FAILED.value
                comp.progress_stage = CompletionStage.FINALIZING
                comp.attempt = attempt
                comp.finished_at = datetime.now(timezone.utc)
                comp.error_code = err_code
                comp.error_message = err_msg
                msg_failed = await session.get(Message, message_id)
                if (
                    msg_failed is not None
                    and msg_failed.status != MessageStatus.CANCELED
                ):
                    msg_failed.status = MessageStatus.FAILED
                comp_failed = await session.get(Completion, task_id)
                if comp_failed is not None:
                    await worker_billing.release_completion(
                        session,
                        comp_failed,
                        reason=err_code,
                    )
                if lease_lost.is_set():
                    raise _LeaseLost("lease lost before preflight failure commit")
                failed_delivery = _stage_completion_event(
                    session,
                    user_id,
                    task_channel(task_id),
                    EV_COMP_FAILED,
                    _completion_event_payload(
                        task_id,
                        message_id,
                        attempt,
                        attempt_epoch,
                        code=err_code,
                        message=err_msg,
                        retriable=False,
                    ),
                )
                await session.commit()
                await worker_billing.flush_balance_cache_refreshes(session)
                await _deliver_completion_event(redis, failed_delivery)
                _task_outcome = "failed"
                return

            comp.status = CompletionStatus.STREAMING.value
            comp.progress_stage = CompletionStage.STREAMING
            started_at = datetime.now(timezone.utc)
            comp.started_at = started_at
            comp.attempt = attempt
            upstream_request = dict(comp.upstream_request or {})
            queue_metadata_payload = completion_queue_metadata(
                upstream_request=upstream_request,
                created_at=comp.created_at,
                started_at=started_at,
                finished_at=comp.finished_at,
                now=started_at,
            )
            comp.upstream_request = merge_queue_metadata(
                upstream_request,
                queue_metadata_payload,
            )
            # 流中断恢复：清空已写 text（§6.9 策略 1）
            if was_restarted:
                comp.text = ""
            if lease_lost.is_set():
                raise _LeaseLost("lease lost before completion claim commit")
            await session.commit()

            # 查 conversation_id（通过 message）
            msg = await session.get(Message, message_id)
            conversation_id = msg.conversation_id if msg is not None else None

        channel = task_channel(task_id)

        accumulated_text = ""
        accumulated_thinking = ""
        flushed_len = 0
        has_partial = False
        tool_images: list[dict[str, Any]] = []
        stored_image_call_ids: set[str] = set()
        reserved_tool_image_budget_micro = 0
        tool_tracker = _CompletionToolTracker()
        usage_totals = _CompletionUsageAccumulator()
        round_text_start = 0
        round_thinking_start = 0
        request_sent = False
        upstream_provider_event: dict[str, str] | None = None

        # 观测：整个 upstream 流式阶段一层 span；手动 enter/exit 以免嵌套大块改缩进
        try:
            span_cm = _tracer.start_as_current_span("upstream.stream_completion")
            stream_span = span_cm.__enter__()
            _stream_span_cm = span_cm
            stream_span.set_attribute("lumen.task_id", task_id)
        except Exception:  # noqa: BLE001
            if _stream_span_cm is not None:
                with suppress(BaseException):
                    _stream_span_cm.__exit__(None, None, None)
                _stream_span_cm = None

        setup_complete = True
    except _LeaseLost as exc:
        _task_outcome = "lease_lost"
        logger.info("completion lease unavailable task=%s err=%s", task_id, exc)
        return
    finally:
        if not setup_complete:
            await _cleanup_completion_runtime(
                redis=redis,
                task_id=task_id,
                lease_token=lease_token,
                lease_acquired=lease_acquired,
                renewer=renewer,
                cancel_stop_requested=cancel_stop_requested,
                cancel_watcher=cancel_watcher,
                stream_span_cm=_stream_span_cm,
                task_start=_task_start,
                task_outcome=_task_outcome,
            )

    try:
        if lease_lost.is_set():
            raise _LeaseLost("lease lost before completion start event")
        await publish_event(
            redis,
            user_id,
            channel,
            EV_COMP_RESTARTED if was_restarted else EV_COMP_STARTED,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
                **queue_metadata_payload,
            },
        )
        if lease_lost.is_set():
            raise _LeaseLost("lease lost during completion start event")

        if user_api_credential_id:
            async with SessionLocal() as session:
                runtime_override = await resolve_user_credential_runtime(
                    session,
                    user_api_credential_id,
                )
            # purpose 守卫：completion (chat / responses) 任务要求 supplier purposes
            # 包含 "chat"。不命中直接抛 byok_purpose_mismatch，由外层 except 走
            # record_user_credential_runtime_error + 任务失败路径，不污染 admin pool。
            if "chat" not in (getattr(runtime_override, "purposes", ()) or ()):
                raise UpstreamError(
                    "user API key supplier does not allow chat purpose",
                    status_code=403,
                    error_code="byok_purpose_mismatch",
                    payload={"credential_id": user_api_credential_id},
                )
        # --- 4. 组 body ---
        reasoning_effort: str | None = None
        fast_mode = False
        chat_tools: list[dict[str, Any]] = []
        memory_meta_for_event: dict[str, Any] = {
            "used_memory_ids": [],
            "used_memory_summary": [],
        }
        # instructions 必须保持稳定（prompt cache 命中前提）。
        # 上游 cache 按请求前缀逐字节比对，instructions 是头部字段；这里只允许：
        # ① comp.system_prompt（DB 持久化的用户/会话级 prompt，按消息固定）
        # ② DEFAULT_CHAT_INSTRUCTIONS 常量
        # ③ _instructions_with_summary_guardrail() 追加的 SUMMARY_GUARDRAIL 常量
        # 严禁在此注入 datetime.now() / time.time() / uuid / random / user.name /
        # session_id / IP 等动态字段；如有动态信息需要给模型，请塞到 input_list 的
        # user message 里（不参与 cache key）。
        instructions = system_prompt or DEFAULT_CHAT_INSTRUCTIONS
        async with SessionLocal() as session:
            target_msg = await session.get(Message, message_id)
            if conversation_id is None:
                input_list = []
            else:
                packed = await _pack_recent_history(
                    session,
                    conversation_id=conversation_id,
                    up_to_message_id=message_id,
                    system_prompt=system_prompt,
                    redis=redis,
                    chat_model=chat_model,
                    account_mode=account_mode,
                )
                # _pack_recent_history 可能跑数百 ms（多张图 base64、history scan）；
                # 期间 lease 可能因 redis 抖动丢掉。下面还有 db 写入，先卡一道。
                if lease_lost.is_set():
                    raise _LeaseLost("lease lost after history pack")
                input_list = packed.input_list
                instructions = _instructions_with_summary_guardrail(
                    system_prompt,
                    enabled=packed.summary_used or packed.sticky_used,
                )
                memory_meta = await _inject_user_memory_context(
                    session,
                    input_list=input_list,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    parent_user_message_id=(
                        getattr(target_msg, "parent_message_id", None)
                        if target_msg is not None
                        else None
                    ),
                    redis=redis,
                )
                memory_meta_for_event = memory_meta
                await _record_completion_context_metadata(
                    session,
                    task_id=task_id,
                    attempt_epoch=attempt_epoch,
                    packed=packed,
                )
                if memory_meta.get("used_memory_ids"):
                    comp_row = await session.get(Completion, task_id)
                    if comp_row is not None and comp_row.attempt == attempt_epoch:
                        upstream_request = dict(comp_row.upstream_request or {})
                        upstream_request["memory"] = memory_meta
                        comp_row.upstream_request = upstream_request
                        await session.commit()

            # assistant.parent_message_id → user message.content.{reasoning_effort, fast, tools}
            if target_msg is not None and target_msg.parent_message_id:
                parent = await session.get(Message, target_msg.parent_message_id)
                if parent is not None and isinstance(parent.content, dict):
                    effort = parent.content.get("reasoning_effort")
                    if effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
                        reasoning_effort = effort
                    if parent.content.get("fast") is True:
                        fast_mode = True
                    chat_tools = await _chat_tools_from_content(parent.content)
        reasoning_effort = _normalize_reasoning_effort_for_upstream(reasoning_effort)

        # body 字段顺序稳定 + tools 数组排序由 upstream._iter_sse 兜底（见 upstream.py 顶部
        # prompt-cache 注释）；这里维持固定字面量字典字面量顺序：model → input → instructions →
        # stream → store。reasoning / service_tier 在末尾按需追加，不会插入到稳定前缀里。
        body: dict[str, Any] = {
            "model": chat_model,
            "input": input_list,
            # 上游现在强制要求 `instructions` 顶层字段；无自定义 system_prompt 时用默认
            "instructions": instructions,
            "stream": True,
            "store": True,
            # Tools are added below only when the parent user message opted in.
        }
        _configure_chat_tools(body, chat_tools)
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
        if fast_mode:
            # Fast 模式 = OpenAI Priority 处理通道（Codex fast 语义同源）。
            # 上游若不支持会原样忽略；若账号/项目未开 Priority，服务端会降级到 default 但仍处理。
            body["service_tier"] = "priority"
        max_tool_invocations = max(
            1,
            await runtime_settings.resolve_int(
                "chat.max_tool_invocations", _MAX_TOOL_INVOCATIONS_DEFAULT
            ),
        )
        cancel_poll_interval_s = max(
            0.05,
            (
                await runtime_settings.resolve_int(
                    "chat.cancel_poll_interval_ms",
                    int(_CANCEL_POLL_INTERVAL_S * 1000),
                )
            )
            / 1000,
        )
        tool_idle_timeout_s = normalize_tool_idle_timeout_seconds(
            await runtime_settings.resolve_int(
                "chat.tool_status_idle_timeout_s",
                int(_TOOL_IDLE_TIMEOUT_S_DEFAULT),
            ),
            default=_TOOL_IDLE_TIMEOUT_S_DEFAULT,
        )

        # --- 5. 消费 SSE ---
        delta_counter = 0
        # GEN-P1-4: 进入 SSE 循环前检查一次 cancel——已经取消就直接走终态。
        if await _is_cancelled(redis, task_id):
            raise _TaskCancelled("cancelled before stream start")
        if lease_lost.is_set():
            raise _LeaseLost("lease lost before stream start")
        completed_response: dict[str, Any] | None = None
        cancel_requested = asyncio.Event()
        cancel_stop_requested = asyncio.Event()
        cancel_watcher = asyncio.create_task(
            _watch_completion_cancel(
                redis,
                task_id,
                cancel_requested=cancel_requested,
                stop_requested=cancel_stop_requested,
                poll_interval_s=cancel_poll_interval_s,
            )
        )
        tool_loop_truncated = False
        request_sent = True
        round_text_start = len(accumulated_text)
        round_thinking_start = len(accumulated_thinking)
        usage_totals.start_round(
            input_fallback_tokens=_estimate_completion_request_input_tokens(
                input_list,
                instructions=instructions,
            ),
            tool_output_tokens=_estimate_completion_tool_output_tokens(
                tool_tracker.content()
            ),
        )
        async for ev in _iter_completion_stream_with_abort(
            stream_completion(body, runtime_override=runtime_override),
            cancel_requested=cancel_requested,
            lease_lost=lease_lost,
            tool_tracker=tool_tracker,
            tool_idle_timeout_s=tool_idle_timeout_s,
        ):
            if lease_lost.is_set():
                raise _LeaseLost("lease lost during stream")
            ev_type = ev.get("type", "")
            if ev_type == "provider_used":
                provider_event = _completion_upstream_provider_event(ev)
                if provider_event:
                    upstream_provider_event = provider_event
                    await _record_completion_upstream_metadata(
                        task_id=task_id,
                        attempt_epoch=attempt_epoch,
                        provider_event=provider_event,
                        fast_mode=fast_mode,
                    )
                continue
            tool_call = tool_tracker.update(ev)
            if tool_call is not None:
                await _publish_completion_tool_progress(
                    redis=redis,
                    user_id=user_id,
                    channel=channel,
                    task_id=task_id,
                    message_id=message_id,
                    attempt=attempt,
                    attempt_epoch=attempt_epoch,
                    tool_call=tool_call,
                    tool_calls=tool_tracker.content(),
                )
                if tool_tracker.invocation_count > max_tool_invocations:
                    await _publish_completion_tool_updates(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        tool_tracker=tool_tracker,
                        updates=tool_tracker.finalize_active(
                            ToolStatus.FAILED.value,
                            error="tool invocation limit exceeded",
                        ),
                    )
                    await publish_event(
                        redis,
                        user_id,
                        channel,
                        EV_COMP_PROGRESS,
                        {
                            "completion_id": task_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "attempt_epoch": attempt_epoch,
                            "stage": "tool_loop_truncated",
                            "max_tool_invocations": max_tool_invocations,
                        },
                    )
                    tool_loop_truncated = True
                    break
            thinking_delta = _extract_reasoning_delta(ev)
            if thinking_delta:
                if not accumulated_thinking.endswith(thinking_delta):
                    accumulated_thinking += thinking_delta
                if thinking_delta:
                    await publish_event(
                        redis,
                        user_id,
                        channel,
                        EV_COMP_THINKING_DELTA,
                        {
                            "completion_id": task_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "attempt_epoch": attempt_epoch,
                            "thinking_delta": thinking_delta,
                        },
                    )

            image_b64 = _extract_response_image_b64(ev)
            if image_b64:
                image_dedupe_key = _tool_image_dedupe_key(ev, image_b64)
                if image_dedupe_key not in stored_image_call_ids:
                    has_partial = True
                    # 图片存储是 base64 解码 + PIL 处理 + 多 storage 写 + DB
                    # insert，最长可达数秒；进入前再卡一道 lease，避免 lease 已
                    # 被接管 worker 抢走时这边继续写图。
                    if lease_lost.is_set():
                        raise _LeaseLost("lease lost before tool image store")
                    (
                        image_payload,
                        image_budget_micro,
                    ) = await _store_and_publish_completion_tool_image(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        b64_image=image_b64,
                        revised_prompt=_extract_response_revised_prompt(ev),
                        reserved_tool_image_micro=reserved_tool_image_budget_micro,
                    )
                    if image_payload is not None:
                        tool_images.append(image_payload)
                        stored_image_call_ids.add(image_dedupe_key)
                        reserved_tool_image_budget_micro += image_budget_micro

            if ev_type == "response.output_text.delta":
                delta = ev.get("delta") or ""
                if not delta:
                    continue
                has_partial = True
                accumulated_text += delta
                # GEN-P1-4: 每 N 个 delta 检查 cancel；命中跳出。
                delta_counter += 1
                if delta_counter % _CANCEL_CHECK_EVERY_DELTAS == 0:
                    if lease_lost.is_set():
                        raise _LeaseLost("lease lost during stream")
                    if await _is_cancelled(redis, task_id):
                        raise _TaskCancelled("cancelled during stream")

                # 按块 flush 到 PG，避免每 token 一个 UPDATE
                total_len = len(accumulated_text)
                if total_len - flushed_len >= _PG_FLUSH_EVERY_CHARS:
                    flushed_len = total_len
                    await _flush_completion_text(
                        task_id,
                        accumulated_text,
                        attempt_epoch=attempt_epoch,
                    )

                # 实时推给前端
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_COMP_DELTA,
                    {
                        "completion_id": task_id,
                        "message_id": message_id,
                        "attempt": attempt,
                        "attempt_epoch": attempt_epoch,
                        "text_delta": delta,
                    },
                )
            elif ev_type == "response.completed":
                has_partial = True
                raw_resp = ev.get("response")
                resp = raw_resp if isinstance(raw_resp, dict) else {}
                completed_response = resp
                raw_usage = resp.get("usage")
                usage_totals.record_usage(
                    parse_usage(
                        chat_model,
                        raw_usage if isinstance(raw_usage, dict) else None,
                    ),
                    raw_usage=raw_usage if isinstance(raw_usage, dict) else None,
                )
                # 同时抄一下 output_text（兜底：某些网关只在 completed 里给完整文本）
                if not accumulated_text:
                    accumulated_text = _extract_completed_output_text(resp)
                if not accumulated_thinking:
                    reasoning_text = _extract_reasoning_text_from_response(resp)
                    if reasoning_text:
                        accumulated_thinking = reasoning_text
                        await publish_event(
                            redis,
                            user_id,
                            channel,
                            EV_COMP_THINKING_DELTA,
                            {
                                "completion_id": task_id,
                                "message_id": message_id,
                                "attempt": attempt,
                                "attempt_epoch": attempt_epoch,
                                "thinking_delta": reasoning_text,
                            },
                        )
                for image_event in _extract_image_events_from_response(resp):
                    image_b64 = _extract_response_image_b64(image_event)
                    if not image_b64:
                        continue
                    image_dedupe_key = _tool_image_dedupe_key(image_event, image_b64)
                    if image_dedupe_key in stored_image_call_ids:
                        continue
                    (
                        image_payload,
                        image_budget_micro,
                    ) = await _store_and_publish_completion_tool_image(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        b64_image=image_b64,
                        revised_prompt=_extract_response_revised_prompt(image_event),
                        reserved_tool_image_micro=reserved_tool_image_budget_micro,
                    )
                    if image_payload is not None:
                        tool_images.append(image_payload)
                        stored_image_call_ids.add(image_dedupe_key)
                        reserved_tool_image_budget_micro += image_budget_micro
                await _publish_completion_tool_updates(
                    redis=redis,
                    user_id=user_id,
                    channel=channel,
                    task_id=task_id,
                    message_id=message_id,
                    attempt=attempt,
                    attempt_epoch=attempt_epoch,
                    tool_tracker=tool_tracker,
                    updates=tool_tracker.update_from_response(resp),
                )
            elif ev_type in {
                "response.failed",
                "response.incomplete",
                "response.cancelled",
                "response.canceled",
            }:
                raw_resp = ev.get("response")
                resp = raw_resp if isinstance(raw_resp, dict) else {}
                await _publish_completion_tool_updates(
                    redis=redis,
                    user_id=user_id,
                    channel=channel,
                    task_id=task_id,
                    message_id=message_id,
                    attempt=attempt,
                    attempt_epoch=attempt_epoch,
                    tool_tracker=tool_tracker,
                    updates=tool_tracker.update_from_response(resp),
                )
                terminal_status = (
                    ToolStatus.CANCELLED.value
                    if ev_type in {"response.cancelled", "response.canceled"}
                    else ToolStatus.FAILED.value
                )
                await _publish_completion_tool_updates(
                    redis=redis,
                    user_id=user_id,
                    channel=channel,
                    task_id=task_id,
                    message_id=message_id,
                    attempt=attempt,
                    attempt_epoch=attempt_epoch,
                    tool_tracker=tool_tracker,
                    updates=tool_tracker.finalize_active(
                        terminal_status,
                        error=_summarize_tool_error(
                            resp.get("error")
                            or resp.get("incomplete_details")
                            or ev.get("error")
                        ),
                    ),
                )
                _raise_for_terminal_response_event(ev_type, resp, ev.get("error"))
            # 其他事件（content_part.added 等）忽略

        if tool_loop_truncated:
            usage_totals.finish_round(
                output_text=accumulated_text[round_text_start:],
                reasoning_text=accumulated_thinking[round_thinking_start:],
                tool_output_tokens=_estimate_completion_tool_output_tokens(
                    tool_tracker.content()
                ),
            )
            fallback_body = _tool_limited_completion_body(body)
            round_text_start = len(accumulated_text)
            round_thinking_start = len(accumulated_thinking)
            usage_totals.start_round(
                input_fallback_tokens=_estimate_completion_request_input_tokens(
                    fallback_body["input"],
                    instructions=fallback_body.get("instructions"),
                ),
                tool_output_tokens=_estimate_completion_tool_output_tokens(
                    tool_tracker.content()
                ),
            )
            async for ev in _iter_completion_stream_with_abort(
                stream_completion(fallback_body, runtime_override=runtime_override),
                cancel_requested=cancel_requested,
                lease_lost=lease_lost,
                tool_tracker=tool_tracker,
                tool_idle_timeout_s=tool_idle_timeout_s,
            ):
                if lease_lost.is_set():
                    raise _LeaseLost("lease lost during tool-limit fallback")
                ev_type = ev.get("type", "")
                if ev_type == "provider_used":
                    provider_event = _completion_upstream_provider_event(ev)
                    if provider_event:
                        upstream_provider_event = provider_event
                        await _record_completion_upstream_metadata(
                            task_id=task_id,
                            attempt_epoch=attempt_epoch,
                            provider_event=provider_event,
                            fast_mode=fast_mode,
                        )
                    continue
                thinking_delta = _extract_reasoning_delta(ev)
                if thinking_delta:
                    if not accumulated_thinking.endswith(thinking_delta):
                        accumulated_thinking += thinking_delta
                    await publish_event(
                        redis,
                        user_id,
                        channel,
                        EV_COMP_THINKING_DELTA,
                        {
                            "completion_id": task_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "attempt_epoch": attempt_epoch,
                            "thinking_delta": thinking_delta,
                        },
                    )

                image_b64 = _extract_response_image_b64(ev)
                if image_b64:
                    image_dedupe_key = _tool_image_dedupe_key(ev, image_b64)
                    if image_dedupe_key not in stored_image_call_ids:
                        has_partial = True
                        if lease_lost.is_set():
                            raise _LeaseLost("lease lost before tool image store")
                        (
                            image_payload,
                            image_budget_micro,
                        ) = await _store_and_publish_completion_tool_image(
                            redis=redis,
                            user_id=user_id,
                            channel=channel,
                            task_id=task_id,
                            message_id=message_id,
                            attempt=attempt,
                            attempt_epoch=attempt_epoch,
                            b64_image=image_b64,
                            revised_prompt=_extract_response_revised_prompt(ev),
                            reserved_tool_image_micro=reserved_tool_image_budget_micro,
                        )
                        if image_payload is not None:
                            tool_images.append(image_payload)
                            stored_image_call_ids.add(image_dedupe_key)
                            reserved_tool_image_budget_micro += image_budget_micro

                if ev_type == "response.output_text.delta":
                    delta = ev.get("delta") or ""
                    if not delta:
                        continue
                    has_partial = True
                    accumulated_text += delta
                    delta_counter += 1
                    if delta_counter % _CANCEL_CHECK_EVERY_DELTAS == 0:
                        if lease_lost.is_set():
                            raise _LeaseLost("lease lost during fallback stream")
                        if await _is_cancelled(redis, task_id):
                            raise _TaskCancelled("cancelled during fallback stream")
                    total_len = len(accumulated_text)
                    if total_len - flushed_len >= _PG_FLUSH_EVERY_CHARS:
                        flushed_len = total_len
                        await _flush_completion_text(
                            task_id,
                            accumulated_text,
                            attempt_epoch=attempt_epoch,
                        )
                    await publish_event(
                        redis,
                        user_id,
                        channel,
                        EV_COMP_DELTA,
                        {
                            "completion_id": task_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "attempt_epoch": attempt_epoch,
                            "text_delta": delta,
                        },
                    )
                elif ev_type == "response.completed":
                    has_partial = True
                    raw_resp = ev.get("response")
                    resp = raw_resp if isinstance(raw_resp, dict) else {}
                    completed_response = resp
                    raw_usage = resp.get("usage")
                    usage_totals.record_usage(
                        parse_usage(
                            chat_model,
                            raw_usage if isinstance(raw_usage, dict) else None,
                        ),
                        raw_usage=(raw_usage if isinstance(raw_usage, dict) else None),
                    )
                    completed_text = _extract_completed_output_text(resp)
                    if completed_text and not accumulated_text.endswith(completed_text):
                        accumulated_text = (
                            f"{accumulated_text}\n\n{completed_text}"
                            if accumulated_text
                            else completed_text
                        )
                    if not accumulated_thinking:
                        reasoning_text = _extract_reasoning_text_from_response(resp)
                        if reasoning_text:
                            accumulated_thinking = reasoning_text
                            await publish_event(
                                redis,
                                user_id,
                                channel,
                                EV_COMP_THINKING_DELTA,
                                {
                                    "completion_id": task_id,
                                    "message_id": message_id,
                                    "attempt": attempt,
                                    "attempt_epoch": attempt_epoch,
                                    "thinking_delta": reasoning_text,
                                },
                            )
                    for image_event in _extract_image_events_from_response(resp):
                        image_b64 = _extract_response_image_b64(image_event)
                        if not image_b64:
                            continue
                        image_dedupe_key = _tool_image_dedupe_key(
                            image_event,
                            image_b64,
                        )
                        if image_dedupe_key in stored_image_call_ids:
                            continue
                        (
                            image_payload,
                            image_budget_micro,
                        ) = await _store_and_publish_completion_tool_image(
                            redis=redis,
                            user_id=user_id,
                            channel=channel,
                            task_id=task_id,
                            message_id=message_id,
                            attempt=attempt,
                            attempt_epoch=attempt_epoch,
                            b64_image=image_b64,
                            revised_prompt=_extract_response_revised_prompt(
                                image_event
                            ),
                            reserved_tool_image_micro=reserved_tool_image_budget_micro,
                        )
                        if image_payload is not None:
                            tool_images.append(image_payload)
                            stored_image_call_ids.add(image_dedupe_key)
                            reserved_tool_image_budget_micro += image_budget_micro
                    await _publish_completion_tool_updates(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        tool_tracker=tool_tracker,
                        updates=tool_tracker.update_from_response(resp),
                    )
                    await _publish_completion_tool_updates(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        tool_tracker=tool_tracker,
                        updates=tool_tracker.finalize_active(
                            ToolStatus.SUCCEEDED.value
                        ),
                    )
                elif ev_type in {
                    "response.failed",
                    "response.incomplete",
                    "response.cancelled",
                    "response.canceled",
                }:
                    raw_resp = ev.get("response")
                    resp = raw_resp if isinstance(raw_resp, dict) else {}
                    await _publish_completion_tool_updates(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        tool_tracker=tool_tracker,
                        updates=tool_tracker.update_from_response(resp),
                    )
                    terminal_status = (
                        ToolStatus.CANCELLED.value
                        if ev_type in {"response.cancelled", "response.canceled"}
                        else ToolStatus.FAILED.value
                    )
                    await _publish_completion_tool_updates(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        tool_tracker=tool_tracker,
                        updates=tool_tracker.finalize_active(
                            terminal_status,
                            error=_summarize_tool_error(
                                resp.get("error")
                                or resp.get("incomplete_details")
                                or ev.get("error")
                            ),
                        ),
                    )
                    _raise_for_terminal_response_event(ev_type, resp, ev.get("error"))

        usage_totals.finish_round(
            output_text=accumulated_text[round_text_start:],
            reasoning_text=accumulated_thinking[round_thinking_start:],
            tool_output_tokens=_estimate_completion_tool_output_tokens(
                tool_tracker.content()
            ),
        )
        if tool_loop_truncated and accumulated_text:
            final_text = _apply_url_citations(
                accumulated_text,
                _extract_url_citations(completed_response or {}),
            )
        else:
            final_text = _finalize_completion_text(accumulated_text, completed_response)
        if not final_text and tool_images:
            final_text = "已生成图片。"
        if not final_text:
            raise UpstreamError(
                "upstream returned empty completion",
                error_code=EC.NO_TEXT_RETURNED.value,
                status_code=200,
            )
        # --- 6. 成功态 ---
        # 写终态 db 前最后一道 lease 检查：如果 stream 期间 lease 丢失但事件循环
        # 没立刻 raise（lease_lost.set() + 当前 await 不在 stream 循环里），
        # 这里是写 SUCCEEDED 行前最后机会，否则会出现"lease 已被别人接管 +
        # 我又写了 SUCCEEDED"的双写。
        if lease_lost.is_set():
            raise _LeaseLost("lease lost before success commit")
        await _raise_if_completion_cancelled(
            redis,
            task_id,
            "cancelled before success commit",
        )
        await _publish_completion_tool_updates(
            redis=redis,
            user_id=user_id,
            channel=channel,
            task_id=task_id,
            message_id=message_id,
            attempt=attempt,
            attempt_epoch=attempt_epoch,
            tool_tracker=tool_tracker,
            updates=tool_tracker.finalize_active(ToolStatus.SUCCEEDED.value),
        )
        async with SessionLocal() as session:
            comp_for_usage = await session.get(Completion, task_id)
            if (
                comp_for_usage is not None
                and comp_for_usage.attempt == attempt_epoch
                and comp_for_usage.status in _RUNNING_COMPLETION_STATUSES
                and tool_images
                and usage_totals.image_output_tokens <= 0
                and reserved_tool_image_budget_micro > 0
            ):
                usage_totals.image_output_tokens = (
                    await _fallback_completion_tool_image_tokens(
                        session,
                        comp_for_usage,
                        budget_micro=reserved_tool_image_budget_micro,
                    )
                )
                usage_totals.tokens_out = max(
                    usage_totals.tokens_out,
                    usage_totals.image_output_tokens,
                )
            res = await session.execute(
                update(Completion)
                .where(
                    Completion.id == task_id,
                    Completion.attempt == attempt_epoch,
                    Completion.status.in_(_RUNNING_COMPLETION_STATUSES),
                )
                .values(
                    status=CompletionStatus.SUCCEEDED.value,
                    progress_stage=CompletionStage.FINALIZING,
                    text=final_text,
                    **usage_totals.model_values(),
                    finished_at=datetime.now(timezone.utc),
                    error_code=None,
                    error_message=None,
                )
            )
            if affected_rows(res) == 0:
                raise _CompletionEpochSuperseded(
                    f"completion epoch superseded before success task={task_id} "
                    f"attempt_epoch={attempt_epoch}"
                )
            msg = await session.get(Message, message_id)
            if msg is not None and msg.status != MessageStatus.CANCELED:
                content = dict(msg.content or {})
                content["text"] = final_text
                if accumulated_thinking:
                    content["thinking"] = accumulated_thinking
                tool_calls = tool_tracker.content()
                if tool_calls:
                    content["tool_calls"] = tool_calls
                if memory_meta_for_event.get("used_memory_ids"):
                    content["used_memory_ids"] = memory_meta_for_event.get(
                        "used_memory_ids", []
                    )
                    content["used_memory_summary"] = memory_meta_for_event.get(
                        "used_memory_summary", []
                    )
                    if memory_meta_for_event.get("confirmation_candidate_id"):
                        content["confirmation_candidate_id"] = (
                            memory_meta_for_event.get("confirmation_candidate_id")
                        )
                msg.content = content
                msg.status = MessageStatus.SUCCEEDED
            comp_for_billing = await session.get(Completion, task_id)
            if comp_for_billing is not None:
                upstream_request = dict(comp_for_billing.upstream_request or {})
                upstream_request = _merge_completion_upstream_metadata(
                    upstream_request,
                    provider_event=upstream_provider_event,
                    fast_mode=fast_mode,
                )
                comp_for_billing.upstream_request = upstream_request or None
                usage_totals.apply_to(comp_for_billing)
                await _raise_if_completion_cancelled(
                    redis,
                    task_id,
                    "cancelled before billing settle",
                )
                await worker_billing.charge_completion(session, comp_for_billing)
                await _raise_if_completion_cancelled(
                    redis,
                    task_id,
                    "cancelled before success commit",
                )
            success_delivery = _stage_completion_event(
                session,
                user_id,
                channel,
                EV_COMP_SUCCEEDED,
                _completion_event_payload(
                    task_id,
                    message_id,
                    attempt,
                    attempt_epoch,
                    text=final_text,
                    tokens_in=usage_totals.tokens_in,
                    tokens_out=usage_totals.tokens_out,
                    tool_calls=tool_tracker.content(),
                    tool_loop_truncated=tool_loop_truncated,
                    used_memory_ids=memory_meta_for_event.get(
                        "used_memory_ids",
                        [],
                    ),
                    used_memory_summary=memory_meta_for_event.get(
                        "used_memory_summary",
                        [],
                    ),
                    confirmation_candidate_id=memory_meta_for_event.get(
                        "confirmation_candidate_id"
                    ),
                ),
            )
            memory_delivery = (
                await _completion_tool_images._stage_completion_memory_extract(
                    session,
                    feature_enabled=memory_extraction is not None,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    source_message_id=(
                        getattr(msg, "parent_message_id", None)
                        if msg is not None
                        else None
                    ),
                    assistant_message_id=message_id,
                    hooks=_COMPLETION_EVENT_HOOKS,
                )
            )
            await session.commit()
            await worker_billing.flush_balance_cache_refreshes(session)

        await _deliver_completion_event(redis, success_delivery)
        if memory_delivery is not None:
            await _deliver_completion_event(redis, memory_delivery)
        _task_outcome = "succeeded"
        upstream_calls_total.labels(kind="completion", outcome="ok").inc()

        # 自动起会话标题（第一轮对话完成后触发；内部幂等）
        if conversation_id:
            from .auto_title import maybe_enqueue_auto_title

            await maybe_enqueue_auto_title(redis, conversation_id)

    except _LeaseLost as exc:
        logger.warning(
            "completion lease lost task=%s attempt=%s err=%s",
            task_id,
            attempt,
            exc,
        )
        _task_outcome = "lease_lost"
        return

    except _CompletionEpochSuperseded as exc:
        logger.info("completion worker superseded task=%s err=%s", task_id, exc)
        _task_outcome = "superseded"
        return

    except _TaskCancelled as exc:
        # GEN-P1-4: 用户主动取消——标 cancelled 并 publish failed(retriable=false)。
        logger.info("completion cancelled by user task=%s reason=%s", task_id, exc)
        usage_totals.finish_round(
            output_text=accumulated_text[round_text_start:],
            reasoning_text=accumulated_thinking[round_thinking_start:],
            tool_output_tokens=_estimate_completion_tool_output_tokens(
                tool_tracker.content()
            ),
        )
        await _publish_completion_tool_updates(
            redis=redis,
            user_id=user_id,
            channel=channel,
            task_id=task_id,
            message_id=message_id,
            attempt=attempt,
            attempt_epoch=attempt_epoch,
            tool_tracker=tool_tracker,
            updates=tool_tracker.finalize_active(ToolStatus.CANCELLED.value),
        )
        cancel_delivery: tuple[str, str, dict[str, Any]] | None = None
        try:
            async with SessionLocal() as session:
                res = await session.execute(
                    update(Completion)
                    .where(
                        Completion.id == task_id,
                        Completion.attempt == attempt_epoch,
                        Completion.status.in_(_RUNNING_COMPLETION_STATUSES),
                    )
                    .values(
                        status=CompletionStatus.CANCELED.value,
                        progress_stage=CompletionStage.FINALIZING,
                        finished_at=datetime.now(timezone.utc),
                        error_code=EC.CANCELLED.value,
                        error_message="cancelled by user",
                    )
                )
                if affected_rows(res) == 0:
                    raise _CompletionEpochSuperseded(
                        f"completion cancel superseded task={task_id} "
                        f"attempt_epoch={attempt_epoch}"
                    )
                msg_c = await session.get(Message, message_id)
                if msg_c is not None and msg_c.status not in (
                    MessageStatus.SUCCEEDED,
                    MessageStatus.FAILED,
                    MessageStatus.CANCELED,
                ):
                    tool_calls = tool_tracker.content()
                    if tool_calls:
                        content = dict(msg_c.content or {})
                        content["tool_calls"] = tool_calls
                        msg_c.content = content
                    msg_c.status = MessageStatus.FAILED
                comp_cancel = await session.get(Completion, task_id)
                if comp_cancel is not None:
                    await _settle_cancelled_completion_billing(
                        session,
                        comp_cancel,
                        has_partial=has_partial,
                        input_list=(
                            input_list
                            if request_sent and "input_list" in locals()
                            else None
                        ),
                        instructions=(
                            instructions
                            if request_sent and "instructions" in locals()
                            else None
                        ),
                        usage_is_finalized=True,
                        accumulated_text=accumulated_text,
                        tokens_in=usage_totals.tokens_in,
                        tokens_out=usage_totals.tokens_out,
                        cache_read_tokens=usage_totals.cache_read_tokens,
                        cache_creation_tokens=usage_totals.cache_creation_tokens,
                        cache_creation_5m_tokens=usage_totals.cache_creation_5m_tokens,
                        cache_creation_1h_tokens=usage_totals.cache_creation_1h_tokens,
                        reasoning_tokens=usage_totals.reasoning_tokens,
                        image_output_tokens=usage_totals.image_output_tokens,
                        tool_images=tool_images,
                        reserved_tool_image_budget_micro=reserved_tool_image_budget_micro,
                        reason=EC.CANCELLED.value,
                    )
                staged_cancel_delivery = _stage_completion_event(
                    session,
                    user_id,
                    channel,
                    EV_COMP_FAILED,
                    _completion_event_payload(
                        task_id,
                        message_id,
                        attempt,
                        attempt_epoch,
                        code="cancelled",
                        message="cancelled by user",
                        retriable=False,
                    ),
                )
                await session.commit()
                cancel_delivery = staged_cancel_delivery
                await worker_billing.flush_balance_cache_refreshes(session)
        except _CompletionEpochSuperseded as stale_exc:
            logger.info(
                "completion cancel skipped by newer epoch task=%s "
                "attempt_epoch=%s err=%s",
                task_id,
                attempt_epoch,
                stale_exc,
            )
            _task_outcome = "superseded"
            return
        except Exception as db_exc:  # noqa: BLE001
            logger.warning(
                "completion cancel DB update failed task=%s err=%s",
                task_id,
                db_exc,
            )
        if cancel_delivery is not None:
            await _deliver_completion_event(redis, cancel_delivery)
        _task_outcome = "failed"
        return

    except Exception as exc:  # noqa: BLE001
        if has_partial or tool_loop_truncated:
            usage_totals.finish_round(
                output_text=accumulated_text[round_text_start:],
                reasoning_text=accumulated_thinking[round_thinking_start:],
                tool_output_tokens=_estimate_completion_tool_output_tokens(
                    tool_tracker.content()
                ),
            )
        if isinstance(exc, _ToolIdleTimeout):
            await _publish_completion_tool_updates(
                redis=redis,
                user_id=user_id,
                channel=channel,
                task_id=task_id,
                message_id=message_id,
                attempt=attempt,
                attempt_epoch=attempt_epoch,
                tool_tracker=tool_tracker,
                updates=tool_tracker.finalize_active(
                    ToolStatus.TIMED_OUT.value,
                    error="tool call idle timeout",
                ),
            )
            exc = UpstreamError(
                "tool call idle timeout",
                error_code=EC.TIMEOUT.value,
                status_code=200,
            )
        upstream_calls_total.labels(kind="completion", outcome="error").inc()
        decision = _classify_exception(exc, has_partial)
        _byok_terminal, byok_error = classify_user_credential_error(exc)
        if user_api_credential_id and byok_error:
            await record_user_credential_runtime_error(user_api_credential_id, exc)
            decision = RetryDecision(False, f"byok {byok_error}")
        _err_code_log = (
            getattr(exc, "error_code", None)
            or getattr(exc, "code", None)
            or type(exc).__name__
        )
        _http_status_log = getattr(exc, "status_code", None)
        logger.warning(
            "completion failed task=%s attempt=%s retriable=%s reason=%s "
            "error_code=%s http_status=%s",
            task_id,
            attempt,
            decision.retriable,
            decision.reason,
            _err_code_log,
            _http_status_log,
        )
        logger.debug("completion exc trace task=%s", task_id, exc_info=True)

        err_code = (
            byok_error_to_generation_code(byok_error)
            if user_api_credential_id and byok_error
            else (
                getattr(exc, "error_code", None)
                or getattr(exc, "code", None)
                or type(exc).__name__
            )
        )
        err_msg = (
            byok_error_message(byok_error)
            if user_api_credential_id and byok_error
            else str(getattr(exc, "message", None) or exc)[:2000]
        )
        _task_outcome = (
            "retry" if (decision.retriable and attempt < _MAX_ATTEMPTS) else "failed"
        )

        if decision.retriable and attempt < _MAX_ATTEMPTS:
            idx = min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)
            delay = RETRY_BACKOFF_SECONDS[idx]

            async with SessionLocal() as session:
                res = await session.execute(
                    update(Completion)
                    .where(
                        Completion.id == task_id,
                        Completion.attempt == attempt_epoch,
                        Completion.status.in_(_RUNNING_COMPLETION_STATUSES),
                    )
                    .values(
                        status=CompletionStatus.QUEUED.value,
                        progress_stage=CompletionStage.QUEUED,
                        error_code=err_code,
                        error_message=err_msg,
                    )
                )
                await session.commit()
                if affected_rows(res) == 0:
                    logger.info(
                        "completion retry skipped by newer epoch task=%s "
                        "attempt_epoch=%s",
                        task_id,
                        attempt_epoch,
                    )
                    _task_outcome = "superseded"
                    return

            try:
                await redis.enqueue_job(
                    "run_completion", task_id, _defer_by=delay, _job_try=attempt + 1
                )
            except Exception as enq_exc:  # noqa: BLE001
                logger.error("re-enqueue failed task=%s err=%s", task_id, enq_exc)
                enqueue_err = "retry_enqueue_failed"
                enqueue_msg = f"failed to enqueue retry: {enq_exc}"[:2000]
                await _publish_completion_tool_updates(
                    redis=redis,
                    user_id=user_id,
                    channel=channel,
                    task_id=task_id,
                    message_id=message_id,
                    attempt=attempt,
                    attempt_epoch=attempt_epoch,
                    tool_tracker=tool_tracker,
                    updates=tool_tracker.finalize_active(
                        ToolStatus.FAILED.value,
                        error=enqueue_msg,
                    ),
                )
                async with SessionLocal() as session:
                    res = await session.execute(
                        update(Completion)
                        .where(
                            Completion.id == task_id,
                            Completion.attempt == attempt_epoch,
                            Completion.status == CompletionStatus.QUEUED.value,
                        )
                        .values(
                            status=CompletionStatus.FAILED.value,
                            progress_stage=CompletionStage.FINALIZING,
                            finished_at=datetime.now(timezone.utc),
                            error_code=enqueue_err,
                            error_message=enqueue_msg,
                        )
                    )
                    if affected_rows(res) == 0:
                        await session.commit()
                        logger.info(
                            "completion retry enqueue failure skipped by newer epoch task=%s "
                            "attempt_epoch=%s",
                            task_id,
                            attempt_epoch,
                        )
                        _task_outcome = "superseded"
                        return
                    msg = await session.get(Message, message_id)
                    if msg is not None and msg.status != MessageStatus.CANCELED:
                        msg.status = MessageStatus.FAILED
                    comp_failed = await session.get(Completion, task_id)
                    if comp_failed is not None:
                        await worker_billing.release_completion(
                            session,
                            comp_failed,
                            reason=enqueue_err,
                        )
                    enqueue_failure_delivery = _stage_completion_event(
                        session,
                        user_id,
                        channel,
                        EV_COMP_FAILED,
                        _completion_event_payload(
                            task_id,
                            message_id,
                            attempt,
                            attempt_epoch,
                            code=enqueue_err,
                            message=enqueue_msg,
                            retriable=False,
                        ),
                    )
                    await session.commit()
                    await worker_billing.flush_balance_cache_refreshes(session)
                await _deliver_completion_event(redis, enqueue_failure_delivery)
                _task_outcome = "failed"
            return

        # terminal
        await _publish_completion_tool_updates(
            redis=redis,
            user_id=user_id,
            channel=channel,
            task_id=task_id,
            message_id=message_id,
            attempt=attempt,
            attempt_epoch=attempt_epoch,
            tool_tracker=tool_tracker,
            updates=tool_tracker.finalize_active(
                ToolStatus.FAILED.value,
                error=err_msg,
            ),
        )
        async with SessionLocal() as session:
            res = await session.execute(
                update(Completion)
                .where(
                    Completion.id == task_id,
                    Completion.attempt == attempt_epoch,
                    Completion.status.in_(_RUNNING_COMPLETION_STATUSES),
                )
                .values(
                    status=CompletionStatus.FAILED.value,
                    progress_stage=CompletionStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=err_code,
                    error_message=err_msg,
                )
            )
            if affected_rows(res) == 0:
                await session.commit()
                logger.info(
                    "completion failure skipped by newer epoch task=%s "
                    "attempt_epoch=%s",
                    task_id,
                    attempt_epoch,
                )
                _task_outcome = "superseded"
                return
            msg = await session.get(Message, message_id)
            if msg is not None and msg.status != MessageStatus.CANCELED:
                tool_calls = tool_tracker.content()
                if tool_calls:
                    content = dict(msg.content or {})
                    content["tool_calls"] = tool_calls
                    msg.content = content
                msg.status = MessageStatus.FAILED
            # Why: partial-stream or completed-response failures already consumed
            # upstream work. Preserve parsed usage buckets when available, and
            # estimate a minimum input/text/image usage only when the provider
            # never sent a usage frame.
            if has_partial or tool_loop_truncated or any(usage_totals.values()):
                comp_partial = await session.get(Completion, task_id)
                if comp_partial is not None:
                    if (
                        tool_images
                        and usage_totals.image_output_tokens <= 0
                        and reserved_tool_image_budget_micro > 0
                    ):
                        usage_totals.image_output_tokens = (
                            await _fallback_completion_tool_image_tokens(
                                session,
                                comp_partial,
                                budget_micro=reserved_tool_image_budget_micro,
                            )
                        )
                        usage_totals.tokens_out = max(
                            usage_totals.tokens_out,
                            usage_totals.image_output_tokens,
                        )
                    usage_totals.apply_to(comp_partial)
                    await _settle_failed_completion_billing(
                        session,
                        comp_partial,
                        usage_values=usage_totals.values(),
                        reason=str(err_code),
                    )
            else:
                comp_failed = await session.get(Completion, task_id)
                if comp_failed is not None:
                    await worker_billing.release_completion(
                        session,
                        comp_failed,
                        reason=str(err_code),
                    )
            failure_delivery = _stage_completion_event(
                session,
                user_id,
                channel,
                EV_COMP_FAILED,
                _completion_event_payload(
                    task_id,
                    message_id,
                    attempt,
                    attempt_epoch,
                    code=err_code,
                    message=err_msg,
                    retriable=False,
                ),
            )
            await session.commit()
            await worker_billing.flush_balance_cache_refreshes(session)

        await _deliver_completion_event(redis, failure_delivery)

    finally:
        await _cleanup_completion_runtime(
            redis=redis,
            task_id=task_id,
            lease_token=lease_token,
            lease_acquired=lease_acquired,
            renewer=renewer,
            cancel_stop_requested=cancel_stop_requested,
            cancel_watcher=cancel_watcher,
            stream_span_cm=_stream_span_cm,
            task_start=_task_start,
            task_outcome=_task_outcome,
        )


__all__ = ["run_completion"]
