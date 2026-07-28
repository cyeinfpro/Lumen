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

from .contracts import CompletionCommand, CompletionResult
from .legacy_adapter import (
    CompletionBillingAdapter,
    CompletionContextAdapter,
    CompletionEventAdapter,
    CompletionLeaseRetryAdapter,
    CompletionRepositoryAdapter,
    CompletionToolAdapter,
    CompletionUpstreamAdapter,
    LegacyCompletionAdapter,
)
from .runtime import CompletionRuntime
from .artifact_codec import (
    compute_blurhash as _generation_compute_blurhash,
    make_display as _make_display,
    make_preview as _make_preview,
    make_thumb as _make_thumb,
    sha256 as _sha256,
)
from .artifact_storage import (
    cleanup_completion_image_files_on_error as _cleanup_storage_on_error,
    write_completion_image_files as _write_generation_files,
)
from .image_storage_runtime import (
    CompletionToolImageBudget,
    CompletionToolImageCodec,
    CompletionToolImageEvents,
    CompletionToolImageRepository,
    CompletionToolImageService,
    CompletionToolImageStorage,
)

# This module assembles the explicit completion runtime and its adapters.

import asyncio
import logging
from contextlib import suppress
from functools import partial
from typing import Any

import httpx
from PIL import Image as PILImage
from sqlalchemy import select, text as sa_text, update

from ... import billing as worker_billing
from lumen_core import billing as billing_core
from lumen_core.constants import (
    DEFAULT_CHAT_INSTRUCTIONS,
    DEFAULT_CHAT_MODEL,
    EV_COMP_IMAGE,
    EV_COMP_PROGRESS,
    CompletionStatus,
    GenerationErrorCode as EC,
    RETRY_BACKOFF_SECONDS,
)
from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    compose_summary_guardrail as compose_summary_guardrail,
    count_tokens,
    estimate_system_prompt_tokens,
    estimate_text_tokens,
    get_input_budget,
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

from ... import completion_billing, runtime_settings
from ...db import SessionLocal, affected_rows
from ...byok_runtime import (
    byok_error_message,
    byok_error_to_generation_code,
    classify_user_credential_error,
    record_user_credential_runtime_error,
    resolve_user_credential_runtime,
)
from ...observability import (
    completion_cancel_check_errors_total,
    get_tracer,
    safe_outcome,
    task_duration_seconds,
    upstream_calls_total,
)
from ...retry import RetryDecision, is_retriable
from ...sse_publish import publish_event as _publish_sse_event
from ...storage import storage
from ...storage_writes import StorageWriteCoordinator
from ...provider_runtime.errors import UpstreamError
from ...provider_runtime.upstream_services import ImageUpstreamRuntime
from ...upstream_parts.responses import (
    extract_response_image_b64 as _extract_response_image_b64,
    extract_response_revised_prompt as _extract_response_revised_prompt,
)
from ...upstream_parts.responses_client import stream_completion
from ..state import is_completion_terminal
from .context import (
    PackedContext as PackedContext,
    estimated_summary_source as _estimated_summary_source,
    fallback_pack as _fallback_pack,
    make_quality_probes as _make_quality_probes,
    pack_with_existing_summary as _pack_with_existing_summary,
    packed_with_input as _packed_with_input,
)
from .context_enrichment import (
    inject_user_memory_context,
    record_completion_context_metadata,
)
from .citation_text import (
    apply_url_citations as _apply_url_citations,
    extract_completed_output_text as _extract_completed_output_text,
    extract_url_citations as _extract_url_citations,
    finalize_completion_text as _finalize_completion_text,
    markdown_link as _markdown_link,
)
from .tool_state import (
    CODE_INTERPRETER_TOOL_TYPE as _CODE_INTERPRETER_TOOL_TYPE,
    CompletionToolTracker as _CompletionToolTracker,
    FILE_SEARCH_TOOL_TYPE as _FILE_SEARCH_TOOL_TYPE,
    IMAGE_GENERATION_TOOL_TYPE as _IMAGE_GENERATION_TOOL_TYPE,
    ToolCallState as _ToolCallState,
    WEB_SEARCH_TOOL_TYPE as _WEB_SEARCH_TOOL_TYPE,
    extract_tool_call_update as _extract_tool_call_update,
    first_str as _first_str,
    merge_tool_call_state as _merge_tool_call_state,
    normalize_tool_status as _normalize_tool_status,
    normalize_tool_type as _normalize_tool_type,
    summarize_tool_error as _summarize_tool_error,
    tool_display_label as _tool_display_label,
    tool_status_rank as _tool_status_rank,
)
from .history import (
    STICKY_TEXT_CHAR_LIMIT as _STICKY_TEXT_CHAR_LIMIT,
    SummaryBoundary as _SummaryBoundary,
    instructions_with_summary_guardrail as _instructions_with_summary_guardrail,
    message_after_summary as _message_after_summary,
    message_created_at as _message_created_at,
    role_eq as _role_eq,
    sticky_text_from_message as _sticky_text_from_message,
    summary_age_seconds as _summary_age_seconds,
    summary_compressed_at as _summary_compressed_at,
    summary_covers_boundary as _summary_covers_boundary,
    summary_created_at as _summary_created_at,
    truncate_sticky_text as _truncate_sticky_text,
    with_summary_guardrail as _with_summary_guardrail,
)
from .request_metadata import (
    completion_upstream_provider_event as _completion_upstream_provider_event,
    content_str_list as _content_str_list,
    merge_completion_upstream_metadata as _merge_completion_upstream_metadata,
    normalize_reasoning_effort_for_upstream as _normalize_reasoning_effort_for_upstream,
    split_csv_ids as _split_csv_ids,
)
from . import history as _completion_history
from . import context_loading as _completion_context_loading
from . import stream as _completion_stream
from . import tool_images as _completion_tool_images
from .runner import run_completion as _run_completion
from .. import context_summary, memory_extraction
from .context_loading import (
    context_circuit_open as _context_circuit_open,
    pick_current_user as _pick_current_user,
    pick_first_user as _pick_first_user,
)
from .stream import (
    LeaseLost as _LeaseLost,
    TaskCancelled as _TaskCancelled,
    ToolIdleTimeout as _ToolIdleTimeout,
    extract_reasoning_delta as _extract_reasoning_delta,
    extract_reasoning_text_from_item as _extract_reasoning_text_from_item,
    extract_reasoning_text_from_response as _extract_reasoning_text_from_response,
    next_completion_stream_event as _next_completion_stream_event,
    raise_for_terminal_response_event as _raise_for_terminal_response_event,
)
from .tool_images import (
    CompletionUsageAccumulator as _CompletionUsageAccumulator,
    completion_event_payload as _completion_event_payload,
    decode_upstream_image_b64 as _decode_upstream_image_b64,
    estimate_completion_request_input_tokens as _estimate_completion_request_input_tokens,
    estimate_completion_tool_output_tokens as _estimate_completion_tool_output_tokens,
    extract_image_events_from_response as _extract_image_events_from_response,
    fallback_completion_usage_tokens as _fallback_completion_usage_tokens,
    settle_cancelled_completion_billing as _settle_cancelled_completion_billing,
    tool_image_dedupe_key as _tool_image_dedupe_key,
)
from .. import outbox as _completion_outbox

__all__ = [
    "_CODE_INTERPRETER_TOOL_TYPE",
    "_CompletionToolTracker",
    "_FILE_SEARCH_TOOL_TYPE",
    "_IMAGE_GENERATION_TOOL_TYPE",
    "_LeaseLost",
    "_STICKY_TEXT_CHAR_LIMIT",
    "_SummaryBoundary",
    "_TaskCancelled",
    "_ToolCallState",
    "_ToolIdleTimeout",
    "_WEB_SEARCH_TOOL_TYPE",
    "_apply_url_citations",
    "_context_circuit_open",
    "_decode_upstream_image_b64",
    "_estimated_summary_source",
    "_extract_completed_output_text",
    "_extract_image_events_from_response",
    "_extract_reasoning_delta",
    "_extract_reasoning_text_from_item",
    "_extract_reasoning_text_from_response",
    "_extract_tool_call_update",
    "_extract_url_citations",
    "_fallback_completion_usage_tokens",
    "_fallback_pack",
    "_finalize_completion_text",
    "_first_str",
    "_instructions_with_summary_guardrail",
    "_make_quality_probes",
    "_markdown_link",
    "_merge_tool_call_state",
    "_message_after_summary",
    "_message_created_at",
    "_normalize_tool_status",
    "_normalize_tool_type",
    "_pack_with_existing_summary",
    "_packed_with_input",
    "_role_eq",
    "_sticky_text_from_message",
    "_summarize_tool_error",
    "_summary_age_seconds",
    "_summary_compressed_at",
    "_summary_covers_boundary",
    "_summary_created_at",
    "_tool_display_label",
    "_tool_image_dedupe_key",
    "_tool_status_rank",
    "_truncate_sticky_text",
    "_with_summary_guardrail",
    "PackedContext",
    "run_completion",
]

logger = logging.getLogger(__name__)
_tracer = get_tracer("lumen.worker.completion")


_fallback_completion_tool_image_tokens = (
    completion_billing.fallback_completion_tool_image_tokens
)
_image_output_tokens_for_budget = completion_billing.image_output_tokens_for_budget
run_completion = _run_completion


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


def _build_completion_tool_image_service(
    storage_writes: StorageWriteCoordinator | None = None,
) -> CompletionToolImageService:
    write_files = (
        _write_generation_files
        if storage_writes is None
        else storage_writes.write_files
    )
    cleanup_on_error = (
        _cleanup_storage_on_error
        if storage_writes is None
        else storage_writes.cleanup_on_error
    )
    return CompletionToolImageService(
        budget=CompletionToolImageBudget(
            reserve=_ensure_completion_tool_image_wallet_budget,
        ),
        codec=CompletionToolImageCodec(
            decode=_decode_upstream_image_b64,
            format_and_meta=_image_format_and_meta,
            sha256=_sha256,
            upstream_error_type=UpstreamError,
            bad_response_error_code=EC.BAD_RESPONSE.value,
        ),
        repository=CompletionToolImageRepository(
            session_factory=SessionLocal,
            new_id=new_uuid7,
            record_usage=partial(
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
            public_url=storage.public_url,
        ),
        storage=CompletionToolImageStorage(
            write_files=write_files,
            cleanup_on_error=cleanup_on_error,
        ),
        events=CompletionToolImageEvents(
            publish=publish_event,
            image_event=EV_COMP_IMAGE,
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


async def _inject_user_memory_context(
    session: Any,
    *,
    input_list: list[dict[str, Any]],
    user_id: str,
    conversation_id: str | None,
    parent_user_message_id: str | None,
    redis: Any | None = None,
) -> dict[str, Any]:
    return await inject_user_memory_context(
        session,
        input_list=input_list,
        user_id=user_id,
        conversation_id=conversation_id,
        parent_user_message_id=parent_user_message_id,
        memory_extraction=memory_extraction,
        message_model=Message,
        redis=redis,
    )


async def _record_completion_context_metadata(
    session: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    packed: PackedContext,
) -> None:
    await record_completion_context_metadata(
        session,
        task_id=task_id,
        attempt_epoch=attempt_epoch,
        packed=packed,
        completion_model=Completion,
    )


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


def _build_completion_context_ports() -> CompletionContextAdapter:
    return CompletionContextAdapter(
        DEFAULT_CHAT_MODEL=DEFAULT_CHAT_MODEL,
        _inject_user_memory_context=_inject_user_memory_context,
        _instructions_with_summary_guardrail=_instructions_with_summary_guardrail,
        _pack_recent_history=_pack_recent_history,
        _record_completion_context_metadata=_record_completion_context_metadata,
        runtime_settings=runtime_settings,
    )


def _build_completion_tools_ports(
    storage_writes: StorageWriteCoordinator | None = None,
) -> CompletionToolAdapter:
    tool_image_service = _build_completion_tool_image_service(storage_writes)
    return CompletionToolAdapter(
        _CompletionToolTracker=_CompletionToolTracker,
        _CompletionUsageAccumulator=_CompletionUsageAccumulator,
        _chat_tools_from_content=_chat_tools_from_content,
        _completion_tool_images=_completion_tool_images,
        _configure_chat_tools=_configure_chat_tools,
        _estimate_completion_request_input_tokens=_estimate_completion_request_input_tokens,
        _estimate_completion_tool_output_tokens=_estimate_completion_tool_output_tokens,
        _extract_image_events_from_response=_extract_image_events_from_response,
        _extract_response_image_b64=_extract_response_image_b64,
        _extract_response_revised_prompt=_extract_response_revised_prompt,
        _publish_completion_tool_progress=_publish_completion_tool_progress,
        _publish_completion_tool_updates=_publish_completion_tool_updates,
        tool_image_service=tool_image_service,
        _summarize_tool_error=_summarize_tool_error,
        _tool_image_dedupe_key=_tool_image_dedupe_key,
        _tool_limited_completion_body=_tool_limited_completion_body,
    )


def _build_completion_persistence_ports() -> CompletionRepositoryAdapter:
    return CompletionRepositoryAdapter(
        Completion=Completion,
        Message=Message,
        SessionLocal=SessionLocal,
        User=User,
        _acquire_completion_xact_lock=_acquire_completion_xact_lock,
        _cleanup_completion_runtime=_cleanup_completion_runtime,
        _flush_completion_text=_flush_completion_text,
        affected_rows=affected_rows,
        is_completion_terminal=is_completion_terminal,
        new_uuid7=new_uuid7,
        select=select,
        update=update,
    )


def _build_completion_upstream_ports(
    image_upstream_runtime: ImageUpstreamRuntime,
) -> CompletionUpstreamAdapter:
    return CompletionUpstreamAdapter(
        UpstreamError=UpstreamError,
        _apply_url_citations=_apply_url_citations,
        _completion_upstream_provider_event=_completion_upstream_provider_event,
        _extract_completed_output_text=_extract_completed_output_text,
        _extract_reasoning_delta=_extract_reasoning_delta,
        _extract_reasoning_text_from_response=_extract_reasoning_text_from_response,
        _extract_url_citations=_extract_url_citations,
        _finalize_completion_text=_finalize_completion_text,
        _merge_completion_upstream_metadata=_merge_completion_upstream_metadata,
        _normalize_reasoning_effort_for_upstream=_normalize_reasoning_effort_for_upstream,
        _raise_for_terminal_response_event=_raise_for_terminal_response_event,
        _record_completion_upstream_metadata=_record_completion_upstream_metadata,
        stream_completion=partial(
            stream_completion,
            runtime=image_upstream_runtime,
        ),
    )


def _build_completion_billing_ports() -> CompletionBillingAdapter:
    return CompletionBillingAdapter(
        _fallback_completion_tool_image_tokens=_fallback_completion_tool_image_tokens,
        _settle_cancelled_completion_billing=_settle_cancelled_completion_billing,
        _settle_failed_completion_billing=_settle_failed_completion_billing,
        byok_error_message=byok_error_message,
        byok_error_to_generation_code=byok_error_to_generation_code,
        classify_user_credential_error=classify_user_credential_error,
        parse_usage=parse_usage,
        record_user_credential_runtime_error=record_user_credential_runtime_error,
        resolve_user_credential_runtime=resolve_user_credential_runtime,
        worker_billing=worker_billing,
    )


def _build_completion_events_ports() -> CompletionEventAdapter:
    return CompletionEventAdapter(
        _COMPLETION_EVENT_HOOKS=_COMPLETION_EVENT_HOOKS,
        _completion_event_payload=_completion_event_payload,
        _deliver_completion_event=_deliver_completion_event,
        _stage_completion_event=_stage_completion_event,
        _tracer=_tracer,
        logger=logger,
        memory_extraction=memory_extraction,
        publish_event=publish_event,
        upstream_calls_total=upstream_calls_total,
    )


def _build_completion_retry_ports() -> CompletionLeaseRetryAdapter:
    return CompletionLeaseRetryAdapter(
        RETRY_BACKOFF_SECONDS=RETRY_BACKOFF_SECONDS,
        RetryDecision=RetryDecision,
        _CANCEL_CHECK_EVERY_DELTAS=_CANCEL_CHECK_EVERY_DELTAS,
        _CANCEL_POLL_INTERVAL_S=_CANCEL_POLL_INTERVAL_S,
        _CompletionEpochSuperseded=_CompletionEpochSuperseded,
        _LeaseLost=_LeaseLost,
        _MAX_ATTEMPTS=_MAX_ATTEMPTS,
        _MAX_TOOL_INVOCATIONS_DEFAULT=_MAX_TOOL_INVOCATIONS_DEFAULT,
        _PG_FLUSH_EVERY_CHARS=_PG_FLUSH_EVERY_CHARS,
        _RUNNING_COMPLETION_STATUSES=_RUNNING_COMPLETION_STATUSES,
        _TOOL_IDLE_TIMEOUT_S_DEFAULT=_TOOL_IDLE_TIMEOUT_S_DEFAULT,
        _TaskCancelled=_TaskCancelled,
        _ToolIdleTimeout=_ToolIdleTimeout,
        _acquire_lease=_acquire_lease,
        _classify_exception=_classify_exception,
        _completion_preflight_failure=_completion_preflight_failure,
        _is_cancelled=_is_cancelled,
        _iter_completion_stream_with_abort=_iter_completion_stream_with_abort,
        _lease_renewer=_lease_renewer,
        _raise_if_completion_cancelled=_raise_if_completion_cancelled,
        _watch_completion_cancel=_watch_completion_cancel,
        completion_queue_metadata=completion_queue_metadata,
        merge_queue_metadata=merge_queue_metadata,
    )


def build_completion_runtime(
    *,
    image_upstream_runtime: ImageUpstreamRuntime,
    storage_writes: StorageWriteCoordinator | None = None,
) -> CompletionRuntime:
    adapter = LegacyCompletionAdapter(
        context=_build_completion_context_ports(),
        tools=_build_completion_tools_ports(storage_writes),
        persistence=_build_completion_persistence_ports(),
        upstream=_build_completion_upstream_ports(image_upstream_runtime),
        billing=_build_completion_billing_ports(),
        events=_build_completion_events_ports(),
        retry=_build_completion_retry_ports(),
    )
    services = adapter.services()

    async def execute(command: CompletionCommand) -> CompletionResult:
        return await _run_completion(command, adapter, services)

    return CompletionRuntime(
        services=services,
        runner=execute,
        image_upstream_runtime=image_upstream_runtime,
    )
