"""Rolling context-summary facade and task entrypoints."""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from sqlalchemy import select

from lumen_core.constants import GenerationErrorCode as EC, Role
from lumen_core.context_window import (
    SUMMARY_KIND,
    SUMMARY_VERSION,
    compare_message_position,
    estimate_message_tokens,
    estimate_text_tokens,
    is_summary_usable,
)
from lumen_core.models import Conversation, Image, Message

from ..db import SessionLocal, engine
from ..observability import (
    context_compaction_duration_seconds,
    context_compaction_total,
)
from ..provider_runtime.errors import UpstreamError
from ..provider_runtime.upstream_services import ImageUpstreamRuntime
from .context_summary_parts import (
    events as _events,
    facade_upstream as _facade_upstream,
    fallback as _fallback,
    manual as _manual,
    persistence as _persistence,
    selection as _selection,
    service as _service,
    upstream as _upstream,
)
from .context_summary_parts.common import (
    LoadedSummaryMessages,
    SummaryCoverage as _SummaryCoverage,
    SummaryLock as _SummaryLock,
    SummarySegment as _SummarySegment,
    current_summary_wins_equal_boundary as _current_summary_wins_equal_boundary,
    iso as _iso,
    settings_int as _settings_int,
    settings_str as _settings_str,
    truncate as _truncate,
    utc_now as _utc_now,
)
from .context_summary_parts import config as _config
from .context_summary_parts.results import (
    SegmentSummaryExecution as _SegmentSummaryExecution,
    SummaryRequest as _SummaryRequest,
    worker_compact_summary_payload as _worker_compact_summary_payload,
)
from .context_summary_parts.segments import (
    bounded_summary_segments as _bounded_summary_segments_impl,
    chunk_lines_by_budget as _chunk_lines_by_budget,
    summary_segments_by_budget as _summary_segments_by_budget,
)
from .context_summary_parts.text import (
    local_fallback_summary_text as _local_fallback_summary_text_impl,
    message_to_summary_line as _message_to_summary_line_impl,
    summarize_text_blob as _summarize_text_blob,
)
from .context_summary_parts.upstream_payloads import (
    compose_summary_input as _compose_summary_input,
    parse_response_dict as _parse_response_dict,
    summary_provider_kwargs as _summary_provider_kwargs,
    summary_response_body as _summary_response_body_impl,
)

logger = logging.getLogger(__name__)
_SUMMARY_MODEL = _config.SUMMARY_MODEL
_SUMMARY_MAX_SEGMENTS = _config.SUMMARY_MAX_SEGMENTS
def _message_to_summary_line(
    msg: Message,
    image_captions: Mapping[str, str] | None = None,
) -> str:
    return _message_to_summary_line_impl(
        msg,
        image_captions,
        iso_fn=_iso,
        truncate_fn=_truncate,
        summarize_text_fn=_summarize_text_blob,
    )
async def _message_position(
    session: Any, message_id: str
) -> tuple[Any, str] | None:
    return await _selection.message_position(session, message_id)
async def _load_messages_for_summary(
    session: Any,
    conv_id: str,
    after_message_id: str | None,
    before_boundary_id: str,
) -> LoadedSummaryMessages:
    return await _selection.load_messages_for_summary(
        session,
        conv_id,
        after_message_id,
        before_boundary_id,
        position_loader=_message_position,
    )
async def _release_business_transaction(session: Any) -> None:
    await _persistence.release_business_transaction(session)
async def _caption_images_for_summary(
    session: Any,
    messages: Sequence[Message],
    settings: Any,
) -> dict[str, str]:
    return await _selection.caption_images_for_summary(
        session,
        messages,
        settings,
        settings_int=_settings_int,
        settings_str=_settings_str,
        release_business_transaction=_release_business_transaction,
        logger=logger,
    )
def _summary_response_body(
    input_text: str,
    *,
    target_tokens: int,
    model: str,
    instructions: str,
) -> dict[str, Any]:
    return _summary_response_body_impl(
        input_text,
        target_tokens=target_tokens,
        model=model,
        instructions=instructions,
        reasoning_effort=_config.SUMMARY_REASONING_EFFORT,
    )
def _summary_upstream_runtime(
    *,
    get_pool: Callable[[], Awaitable[Any]],
    classify_retriable: Callable[..., Any],
    responses_call: Callable[..., Awaitable[Any]],
) -> _upstream.SummaryUpstreamRuntime:
    return _facade_upstream.build_runtime(
        get_pool=get_pool,
        classify_retriable=classify_retriable,
        responses_call=responses_call,
        response_body=_summary_response_body,
        parse_response=_parse_response_dict,
        provider_kwargs=_summary_provider_kwargs,
        empty_output_error=lambda: UpstreamError(
            "context summary empty output",
            error_code=EC.EMPTY_OUTPUT.value,
            status_code=502,
        ),
        logger=logger,
        retry_attempts=_config.PER_PROVIDER_RETRY_ATTEMPTS,
        retry_backoff_s=_config.PER_PROVIDER_RETRY_BACKOFF_S,
    )
async def _call_summary_upstream(
    input_text: str,
    target_tokens: int,
    model: str,
    *,
    extra_instruction: str | None = None,
    timeout_s: float = _config.SUMMARY_HTTP_TIMEOUT_S,
    image_upstream_runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    return await _facade_upstream.call_summary_upstream(
        input_text,
        target_tokens,
        model,
        instructions=_config.SUMMARY_INSTRUCTIONS,
        extra_instruction=extra_instruction,
        timeout_s=timeout_s,
        image_upstream_runtime=image_upstream_runtime,
        runtime_factory=_summary_upstream_runtime,
    )
def _local_fallback_summary_text(
    *,
    previous_summary: str | None,
    messages: Sequence[Message],
    target_tokens: int,
    extra_instruction: str | None = None,
    image_captions: Mapping[str, str] | None = None,
) -> str | None:
    return _local_fallback_summary_text_impl(
        previous_summary=previous_summary,
        messages=messages,
        target_tokens=target_tokens,
        extra_instruction=extra_instruction,
        image_captions=image_captions,
        message_to_line=_message_to_summary_line,
        truncate_fn=_truncate,
    )
async def _call_summary_upstream_compatible(
    input_text: str,
    target_tokens: int,
    model: str,
    *,
    extra_instruction: str | None,
    timeout_s: float,
    image_upstream_runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    runtime_kwargs = (
        {"image_upstream_runtime": image_upstream_runtime}
        if image_upstream_runtime is not None
        else {}
    )
    try:
        return await _call_summary_upstream(
            input_text,
            target_tokens,
            model,
            extra_instruction=extra_instruction,
            timeout_s=timeout_s,
            **runtime_kwargs,
        )
    except TypeError as exc:
        if "timeout_s" not in str(exc):
            raise
        return await _call_summary_upstream(
            input_text,
            target_tokens,
            model,
            extra_instruction=extra_instruction,
            **runtime_kwargs,
        )
def _bounded_summary_segments(
    segments: Sequence[_SummarySegment],
) -> tuple[list[_SummarySegment], str | None]:
    return _bounded_summary_segments_impl(segments, _SUMMARY_MAX_SEGMENTS)


async def _safe_set_partial(
    redis: Any, conv_id: str, text: str, segment_index: int
) -> None:
    await _events.safe_set_partial(
        redis,
        conv_id,
        text,
        segment_index,
        ttl_s=_config.PARTIAL_TTL_S,
        logger=logger,
    )


async def _safe_delete_partial(redis: Any, conv_id: str) -> None:
    await _events.safe_delete_partial(redis, conv_id, logger=logger)


def _manual_compact_job_key(*, user_id: str, conv_id: str, job_id: str) -> str:
    return _events.manual_compact_job_key(
        user_id=user_id,
        conv_id=conv_id,
        job_id=job_id,
    )


def _manual_compact_active_key(*, user_id: str, conv_id: str) -> str:
    return _events.manual_compact_active_key(user_id=user_id, conv_id=conv_id)


async def _safe_set_job_status(
    redis: Any,
    key: str,
    payload: dict[str, Any],
    *,
    ttl: int = _config.MANUAL_COMPACT_JOB_TTL_S,
) -> None:
    await _events.safe_set_job_status(
        redis,
        key,
        payload,
        ttl=ttl,
        logger=logger,
    )


async def _safe_release_manual_compact_active(
    redis: Any,
    *,
    user_id: str,
    conv_id: str,
    job_id: str,
) -> None:
    await _events.safe_release_manual_compact_active(
        redis,
        user_id=user_id,
        conv_id=conv_id,
        job_id=job_id,
        script=_config.RELEASE_MANUAL_COMPACT_ACTIVE_LUA,
        logger=logger,
    )


async def _segment_and_summarize(
    *,
    conv_id: str,
    messages: Sequence[Message],
    previous_summary: str | None,
    target_tokens: int,
    model: str,
    input_budget: int,
    timeout_s: float = _config.SUMMARY_HTTP_TIMEOUT_S,
    extra_instruction: str | None = None,
    image_captions: Mapping[str, str] | None = None,
    redis: Any = None,
    progress_callback: Callable[[int, int], Awaitable[None]] | None = None,
    execution: _SegmentSummaryExecution | None = None,
) -> str | None:
    execution = execution or _SegmentSummaryExecution()
    return await _fallback.segment_and_summarize(
        conv_id=conv_id,
        messages=messages,
        previous_summary=previous_summary,
        target_tokens=target_tokens,
        model=model,
        input_budget=input_budget,
        timeout_s=timeout_s,
        extra_instruction=extra_instruction,
        image_captions=image_captions,
        redis=redis,
        progress_callback=progress_callback,
        coverage=execution.coverage,
        runtime=_fallback.SummaryFallbackRuntime(
            message_to_line=_message_to_summary_line,
            call_upstream=partial(
                _call_summary_upstream_compatible,
                image_upstream_runtime=execution.image_upstream_runtime,
            ),
            compose_input=_compose_summary_input,
            plan_segments=_summary_segments_by_budget,
            bound_segments=_bounded_summary_segments,
            set_partial=_safe_set_partial,
            logger=logger,
            max_segments=_SUMMARY_MAX_SEGMENTS,
        ),
    )


async def _publish_compaction_event(
    redis: Any, conv_id: str, payload: dict[str, Any]
) -> None:
    await _events.publish_compaction_event(redis, conv_id, payload, logger=logger)


_redis_text = _events.redis_text


async def _is_circuit_open(redis: Any) -> bool:
    return await _events.is_circuit_open(
        redis,
        state_key=_config.CIRCUIT_STATE_KEY,
        logger=logger,
    )


async def _record_circuit_sample(
    redis: Any,
    *,
    success: bool,
    threshold_percent: int,
) -> None:
    await _events.record_circuit_sample(
        redis,
        success=success,
        threshold_percent=threshold_percent,
        samples_key=_config.CIRCUIT_SAMPLES_KEY,
        state_key=_config.CIRCUIT_STATE_KEY,
        until_key=_config.CIRCUIT_UNTIL_KEY,
        sample_window=_config.CIRCUIT_SAMPLE_WINDOW,
        min_samples=_config.CIRCUIT_MIN_SAMPLES,
        ttl_s=_config.CIRCUIT_TTL_S,
        utc_now=_utc_now,
        logger=logger,
    )


async def record_summary_metrics(
    redis: Any,
    *,
    conv_id: str,
    trigger: str,
    outcome: str,
    source_tokens: int = 0,
    summary_tokens: int = 0,
    circuit_threshold_percent: int | None = None,
) -> None:
    await _events.record_summary_metrics(
        redis,
        conv_id=conv_id,
        trigger=trigger,
        outcome=outcome,
        source_tokens=source_tokens,
        summary_tokens=summary_tokens,
        circuit_threshold_percent=circuit_threshold_percent,
        utc_now=_utc_now,
        record_circuit_sample=_record_circuit_sample,
        context_compaction_total=context_compaction_total,
        logger=logger,
    )


def _observe_compaction_duration(
    *, trigger: str, outcome: str, elapsed_s: float
) -> None:
    _events.observe_compaction_duration(
        trigger=trigger,
        outcome=outcome,
        elapsed_s=elapsed_s,
        context_compaction_duration_seconds=context_compaction_duration_seconds,
        logger=logger,
    )


_get_redis_from_settings = _events.get_redis_from_settings


async def _acquire_summary_lock(
    session: Any,
    redis: Any,
    conv_id: str,
) -> _SummaryLock | None:
    return await _persistence.acquire_summary_lock(
        session,
        redis,
        conv_id,
        engine=engine,
        ttl_s=_config.SUMMARY_LOCK_TTL_S,
        lock_factory=_SummaryLock,
        logger=logger,
    )


async def _release_summary_lock(
    redis: Any, conv_id: str, lock: _SummaryLock | None
) -> None:
    await _persistence.release_summary_lock(
        redis,
        conv_id,
        lock,
        release_script=_config.RELEASE_SUMMARY_LOCK_LUA,
        logger=logger,
    )


async def _renew_summary_lock_loop(
    redis: Any,
    conv_id: str,
    lock: _SummaryLock,
    *,
    interval_s: float = _config.SUMMARY_LOCK_RENEW_INTERVAL_S,
) -> None:
    await _persistence.renew_summary_lock_loop(
        redis,
        conv_id,
        lock,
        interval_s=interval_s,
        ttl_s=_config.SUMMARY_LOCK_TTL_S,
        renew_script=_config.RENEW_SUMMARY_LOCK_LUA,
        logger=logger,
    )


async def _read_current_summary(
    session: Any, conv_id: str
) -> dict[str, Any] | None:
    return await _persistence.read_current_summary(session, conv_id, logger=logger)


async def _cas_write_summary(
    session: Any,
    conv_id: str,
    summary: dict[str, Any],
    *,
    lock: _SummaryLock | None = None,
    allow_equal_boundary_refresh: bool = False,
) -> bool:
    return await _persistence.cas_write_summary(
        session,
        conv_id,
        summary,
        lock=lock,
        allow_equal_boundary_refresh=allow_equal_boundary_refresh,
        current_summary_wins_equal_boundary=_current_summary_wins_equal_boundary,
        logger=logger,
    )


def _service_dependencies() -> _service.SummaryServiceDependencies:
    return _service.SummaryServiceDependencies(
        load_messages=_load_messages_for_summary,
        load_position=_message_position,
        caption_images=_caption_images_for_summary,
        segment_and_summarize=_segment_and_summarize,
        segment_message_to_line=_message_to_summary_line,
        record_circuit_sample=_record_circuit_sample,
        record_metrics=record_summary_metrics,
        acquire_lock=_acquire_summary_lock,
        release_lock=_release_summary_lock,
        renew_lock=_renew_summary_lock_loop,
        read_summary=_read_current_summary,
        write_summary=_cas_write_summary,
        release_transaction=_release_business_transaction,
        delete_partial=_safe_delete_partial,
        publish_event=_publish_compaction_event,
        is_circuit_open=_is_circuit_open,
        sleep=asyncio.sleep,
        logger=logger,
        context_compaction_duration_seconds=context_compaction_duration_seconds,
        target_tokens=_config.SUMMARY_TARGET_TOKENS,
        input_budget=_config.SUMMARY_INPUT_BUDGET,
        timeout_s=_config.SUMMARY_HTTP_TIMEOUT_S,
        lock_wait_s=_config.SUMMARY_LOCK_WAIT_S,
        model=_SUMMARY_MODEL,
    )


async def ensure_context_summary(
    session: Any,
    conv: Conversation,
    boundary: Any,
    settings: Any,
    *,
    force: bool = False,
    extra_instruction: str | None = None,
    dry_run: bool = False,
    trigger: str = "auto",
    image_upstream_runtime: ImageUpstreamRuntime | None = None,
) -> dict[str, Any] | None:
    return await _service.ensure_context_summary(
        session,
        conv,
        boundary,
        settings,
        force=force,
        extra_instruction=extra_instruction,
        dry_run=dry_run,
        trigger=trigger,
        image_upstream_runtime=image_upstream_runtime,
        deps=_service_dependencies(),
    )


async def manual_compact_conversation(
    ctx: dict[str, Any],
    user_id: str,
    conv_id: str,
    boundary_id: str,
    job_id: str,
    extra_instruction: str | None,
    target_tokens: int,
    input_budget: int,
    summary_timeout_s: float,
    model: str,
) -> dict[str, Any]:
    return await _manual.manual_compact_conversation(
        ctx,
        user_id,
        conv_id,
        boundary_id,
        job_id,
        extra_instruction,
        target_tokens,
        input_budget,
        summary_timeout_s,
        model,
        deps=_manual.ManualCompactDependencies(
            session_factory=SessionLocal,
            ensure_summary=ensure_context_summary,
            job_key=_manual_compact_job_key,
            set_job_status=_safe_set_job_status,
            release_active=_safe_release_manual_compact_active,
            compact_payload=_worker_compact_summary_payload,
            utc_now=_utc_now,
            logger=logger,
        ),
    )


__all__ = [
    "_call_summary_upstream",
    "_load_messages_for_summary",
    "_message_to_summary_line",
    "_segment_and_summarize",
    "_summarize_text_blob",
    "ensure_context_summary",
    "manual_compact_conversation",
    "record_summary_metrics",
]
