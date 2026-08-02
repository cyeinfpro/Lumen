"""Context-summary orchestration with explicit facade-provided dependencies."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from lumen_core.constants import Role
from lumen_core.context_window import (
    SUMMARY_KIND,
    SUMMARY_VERSION,
    estimate_text_tokens,
    is_summary_usable,
)
from lumen_core.model_entities import (
    Conversation,
    Message,
)

from . import planning
from .common import (
    LoadedSummaryMessages,
    SummaryCoverage,
    SummaryLock,
    boundary_created_at,
    boundary_id,
    extra_instruction_hash,
    iso,
    public_summary_result,
    settings_float,
    settings_int,
    settings_str,
    summary_satisfies_request,
    truncate,
    utc_now,
)
from .results import (
    SegmentSummaryExecution,
    SummaryGenerationResult,
    SummaryRequest,
    SummaryTiming,
    effective_summary_window,
    normalize_summary_coverage,
    summary_event_payload,
)
from .text import build_local_fallback_summary


@dataclass(frozen=True, slots=True)
class SummaryServiceDependencies:
    load_messages: Callable[..., Awaitable[LoadedSummaryMessages]]
    load_position: Callable[..., Awaitable[tuple[datetime, str] | None]]
    caption_images: Callable[..., Awaitable[dict[str, str]]]
    segment_and_summarize: Callable[..., Awaitable[str | None]]
    segment_message_to_line: Callable[..., str]
    record_circuit_sample: Callable[..., Awaitable[None]]
    record_metrics: Callable[..., Awaitable[None]]
    acquire_lock: Callable[..., Awaitable[SummaryLock | None]]
    release_lock: Callable[..., Awaitable[None]]
    renew_lock: Callable[..., Awaitable[None]]
    read_summary: Callable[..., Awaitable[dict[str, Any] | None]]
    write_summary: Callable[..., Awaitable[bool]]
    lock_active_context: Callable[..., Awaitable[bool]]
    release_transaction: Callable[..., Awaitable[None]]
    delete_partial: Callable[..., Awaitable[None]]
    publish_event: Callable[..., Awaitable[None]]
    is_circuit_open: Callable[..., Awaitable[bool]]
    sleep: Callable[[float], Awaitable[Any]]
    logger: logging.Logger
    context_compaction_duration_seconds: Any
    target_tokens: int
    input_budget: int
    timeout_s: float
    lock_wait_s: float
    model: str


async def attach_summary_image_captions(
    session: Any,
    request: SummaryRequest,
    deps: SummaryServiceDependencies,
) -> LoadedSummaryMessages:
    image_captions = await deps.caption_images(
        session,
        request.loaded.messages,
        request.settings,
    )
    if not image_captions:
        return request.loaded
    return LoadedSummaryMessages(
        request.loaded.messages,
        request.loaded.source_message_count,
        request.loaded.source_token_estimate,
        request.loaded.image_caption_count + len(image_captions),
        image_captions,
    )


def observe_compaction_duration(
    *,
    trigger: str,
    outcome: str,
    elapsed_s: float,
    deps: SummaryServiceDependencies,
) -> None:
    try:
        reason = "manual" if trigger == "manual" else "token_limit"
        deps.context_compaction_duration_seconds.labels(
            reason=reason,
            outcome=outcome,
        ).observe(max(0.0, elapsed_s))
    except Exception as exc:  # noqa: BLE001
        deps.logger.debug("context_summary.prom_hist_failed err=%r", exc)


async def report_summary_generation_failure(
    request: SummaryRequest,
    timing: SummaryTiming,
    redis: Any,
    *,
    circuit_open: bool,
    deps: SummaryServiceDependencies,
) -> None:
    observe_compaction_duration(
        trigger=request.trigger,
        outcome="failed",
        elapsed_s=time.monotonic() - timing.started_monotonic,
        deps=deps,
    )
    await deps.record_metrics(
        redis,
        conv_id=request.conv_id,
        trigger=request.trigger,
        outcome="failed",
        circuit_threshold_percent=(None if circuit_open else request.circuit_threshold),
    )
    await deps.publish_event(
        redis,
        request.conv_id,
        summary_event_payload(
            request,
            timing,
            phase="completed",
            ok=False,
            fallback_reason="summary_failed",
        ),
    )


async def generate_summary_result(
    session: Any,
    request: SummaryRequest,
    timing: SummaryTiming,
    redis: Any,
    *,
    circuit_open: bool,
    progress_callback: Callable[[int, int], Awaitable[None]],
    image_upstream_runtime: Any,
    deps: SummaryServiceDependencies,
) -> SummaryGenerationResult | None:
    async def can_dispatch_upstream() -> bool:
        active = await deps.lock_active_context(
            session,
            request.conv_id,
            user_id=request.user_id,
        )
        if not active:
            deps.logger.info(
                "context_summary.dispatch_fenced_out conv=%s",
                request.conv_id,
            )
            return False
        await deps.release_transaction(session)
        return True

    if not await can_dispatch_upstream():
        return None
    loaded = await attach_summary_image_captions(session, request, deps)
    coverage = SummaryCoverage()
    summary_text: str | None = None
    if not circuit_open:
        summary_text = await deps.segment_and_summarize(
            conv_id=request.conv_id,
            messages=loaded.messages,
            previous_summary=request.previous_summary_text,
            target_tokens=request.target_tokens,
            model=request.model,
            input_budget=request.input_budget,
            timeout_s=request.summary_timeout_s,
            extra_instruction=request.extra_instruction,
            image_captions=loaded.image_captions,
            redis=redis,
            progress_callback=progress_callback,
            execution=SegmentSummaryExecution(
                coverage=coverage,
                image_upstream_runtime=image_upstream_runtime,
                before_upstream=can_dispatch_upstream,
            ),
        )
    normalize_summary_coverage(summary_text, coverage, loaded)

    fallback_reason = coverage.partial_reason if summary_text else None
    if fallback_reason == "partial_segment_failure":
        await deps.record_circuit_sample(
            redis,
            success=False,
            threshold_percent=request.circuit_threshold,
        )
    if summary_text:
        return SummaryGenerationResult(
            summary_text,
            loaded,
            coverage,
            fallback_reason,
        )

    if not circuit_open and coverage.partial_reason != "segment_limit":
        await deps.record_circuit_sample(
            redis,
            success=False,
            threshold_percent=request.circuit_threshold,
        )
    summary_text, fallback_covered_count = build_local_fallback_summary(
        previous_summary=request.previous_summary_text,
        messages=loaded.messages,
        target_tokens=request.target_tokens,
        extra_instruction=request.extra_instruction,
        image_captions=loaded.image_captions,
        message_to_line=deps.segment_message_to_line,
        truncate_fn=truncate,
    )
    coverage.covered_message_count = fallback_covered_count
    fallback_reason = (
        "circuit_open_local_fallback" if circuit_open else "local_fallback"
    )
    if not summary_text:
        await report_summary_generation_failure(
            request,
            timing,
            redis,
            circuit_open=circuit_open,
            deps=deps,
        )
        return None
    deps.logger.warning(
        "context_summary.local_fallback_used conv=%s source_messages=%d",
        request.conv_id,
        loaded.source_message_count,
    )
    return SummaryGenerationResult(
        summary_text,
        loaded,
        coverage,
        fallback_reason,
    )


def normalize_summary_output(
    request: SummaryRequest,
    summary_text: str,
    *,
    logger: logging.Logger,
) -> tuple[str, int]:
    summary_tokens = estimate_text_tokens(summary_text)
    if summary_tokens <= request.target_tokens * 2:
        return summary_text, summary_tokens
    max_chars = max(1000, int(request.target_tokens * 1.5 * 4))
    summary_text = truncate(summary_text, max_chars)
    summary_tokens = estimate_text_tokens(summary_text)
    logger.warning(
        "context_summary.output_truncated conv=%s tokens=%s",
        request.conv_id,
        summary_tokens,
    )
    return summary_text, summary_tokens


async def first_summary_user_message_id(
    session: Any,
    conv_id: str,
    fallback_id: str,
    *,
    logger: logging.Logger,
) -> str:
    first_user_message_id = None
    try:
        first_user_message_id = (
            await session.execute(
                select(Message.id)
                .where(
                    Message.conversation_id == conv_id,
                    Message.deleted_at.is_(None),
                    Message.role == Role.USER.value,
                )
                .order_by(Message.created_at.asc(), Message.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "context_summary.first_user_lookup_failed conv=%s err=%r",
            conv_id,
            exc,
        )
    return str(first_user_message_id or fallback_id)


def build_summary_jsonb(
    request: SummaryRequest,
    generated: SummaryGenerationResult,
    effective_loaded: LoadedSummaryMessages,
    *,
    summary_boundary_id: str,
    summary_boundary_dt: datetime,
    first_user_message_id: str,
    summary_text: str,
    summary_tokens: int,
) -> dict[str, Any]:
    previous_runs = (
        int(request.existing_summary.get("compression_runs") or 0)
        if request.existing_summary is not None
        else 0
    )
    return {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": summary_boundary_id,
        "up_to_created_at": iso(summary_boundary_dt),
        "first_user_message_id": first_user_message_id,
        "text": summary_text,
        "tokens": summary_tokens,
        "source_message_count": effective_loaded.source_message_count,
        "source_token_estimate": effective_loaded.source_token_estimate,
        "model": request.model,
        "image_caption_count": effective_loaded.image_caption_count,
        "extra_instruction_hash": request.extra_hash,
        "compressed_at": utc_now().isoformat(),
        "compression_runs": previous_runs + 1,
        "last_quality_signal": generated.fallback_reason,
        "fallback_reason": generated.fallback_reason,
    }


async def handle_lost_summary_lock(
    session: Any,
    request: SummaryRequest,
    timing: SummaryTiming,
    redis: Any,
    lock: SummaryLock,
    deps: SummaryServiceDependencies,
) -> dict[str, Any] | None:
    latest = await deps.read_summary(session, request.conv_id)
    if summary_satisfies_request(latest, request.boundary, request.extra_hash):
        return public_summary_result(
            latest,
            created=False,
            status="cached_after_lock_lost",
        )
    observe_compaction_duration(
        trigger=request.trigger,
        outcome="lock_lost",
        elapsed_s=time.monotonic() - timing.started_monotonic,
        deps=deps,
    )
    await deps.record_metrics(
        redis,
        conv_id=request.conv_id,
        trigger=request.trigger,
        outcome="lock_lost",
    )
    await deps.publish_event(
        redis,
        request.conv_id,
        summary_event_payload(
            request,
            timing,
            phase="completed",
            ok=False,
            fallback_reason=f"lock_{lock.lost_reason}",
        ),
    )
    return None


async def persist_summary_result(
    session: Any,
    request: SummaryRequest,
    timing: SummaryTiming,
    redis: Any,
    lock: SummaryLock,
    generated: SummaryGenerationResult,
    deps: SummaryServiceDependencies,
) -> dict[str, Any] | None:
    window = effective_summary_window(request, generated)
    if window is None:
        return {"status": "summary_failed"}
    effective_loaded, summary_boundary_id, summary_boundary_dt = window
    summary_text, summary_tokens = normalize_summary_output(
        request,
        generated.text,
        logger=deps.logger,
    )
    first_user_message_id = await first_summary_user_message_id(
        session,
        request.conv_id,
        summary_boundary_id,
        logger=deps.logger,
    )
    summary_jsonb = build_summary_jsonb(
        request,
        generated,
        effective_loaded,
        summary_boundary_id=summary_boundary_id,
        summary_boundary_dt=summary_boundary_dt,
        first_user_message_id=first_user_message_id,
        summary_text=summary_text,
        summary_tokens=summary_tokens,
    )

    if lock.lost_reason:
        return await handle_lost_summary_lock(
            session,
            request,
            timing,
            redis,
            lock,
            deps,
        )
    wrote = await deps.write_summary(
        session,
        request.conv_id,
        summary_jsonb,
        lock=lock,
        allow_equal_boundary_refresh=request.force,
    )
    if not wrote:
        latest = await deps.read_summary(session, request.conv_id)
        if summary_satisfies_request(latest, request.boundary, request.extra_hash):
            return public_summary_result(
                latest,
                created=False,
                status="cas_reused",
            )
        observe_compaction_duration(
            trigger=request.trigger,
            outcome="cas_failed",
            elapsed_s=time.monotonic() - timing.started_monotonic,
            deps=deps,
        )
        await deps.record_metrics(
            redis,
            conv_id=request.conv_id,
            trigger=request.trigger,
            outcome="cas_failed",
        )
        return None

    await deps.delete_partial(redis, request.conv_id)
    public_status = (
        "created_local_fallback"
        if generated.fallback_reason
        in {"circuit_open_local_fallback", "local_fallback"}
        else "created"
    )
    public = public_summary_result(
        summary_jsonb,
        created=True,
        status=public_status,
    )
    observe_compaction_duration(
        trigger=request.trigger,
        outcome="ok",
        elapsed_s=time.monotonic() - timing.started_monotonic,
        deps=deps,
    )
    await deps.record_metrics(
        redis,
        conv_id=request.conv_id,
        trigger=request.trigger,
        outcome="ok",
        source_tokens=effective_loaded.source_token_estimate,
        summary_tokens=summary_tokens,
        circuit_threshold_percent=(
            request.circuit_threshold
            if generated.fallback_reason in {None, "segment_limit"}
            else None
        ),
    )
    await deps.publish_event(
        redis,
        request.conv_id,
        summary_event_payload(
            request,
            timing,
            phase="completed",
            ok=True,
            fallback_reason=generated.fallback_reason,
            public=public,
        ),
    )
    return public


async def run_locked_context_summary(
    session: Any,
    request: SummaryRequest,
    redis: Any,
    lock: SummaryLock,
    *,
    circuit_open: bool,
    image_upstream_runtime: Any,
    deps: SummaryServiceDependencies,
) -> dict[str, Any] | None:
    renew_task: asyncio.Task[None] | None = None
    try:
        timing = SummaryTiming(utc_now(), time.monotonic())

        async def progress(current_segment: int, total_segments: int) -> None:
            await deps.publish_event(
                redis,
                request.conv_id,
                summary_event_payload(
                    request,
                    timing,
                    phase="progress",
                    ok=None,
                    fallback_reason=None,
                    progress=(current_segment, total_segments),
                ),
            )

        await deps.publish_event(
            redis,
            request.conv_id,
            summary_event_payload(
                request,
                timing,
                phase="started",
                ok=None,
                fallback_reason=None,
            ),
        )
        if redis is not None and lock.kind == "redis" and lock.token is not None:
            renew_task = asyncio.create_task(
                deps.renew_lock(redis, request.conv_id, lock)
            )

        generated = await generate_summary_result(
            session,
            request,
            timing,
            redis,
            circuit_open=circuit_open,
            progress_callback=progress,
            image_upstream_runtime=image_upstream_runtime,
            deps=deps,
        )
        if generated is None:
            return {"status": "summary_failed"}
        return await persist_summary_result(
            session,
            request,
            timing,
            redis,
            lock,
            generated,
            deps,
        )
    finally:
        await stop_summary_lock_renewal(renew_task)
        await deps.release_lock(redis, request.conv_id, lock)


async def stop_summary_lock_renewal(
    renew_task: asyncio.Task[None] | None,
) -> None:
    if renew_task is None:
        return
    renew_task.cancel()
    try:
        await renew_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def summary_dry_run_result(request: SummaryRequest) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "dry_run": True,
        "would_call_upstream": (
            request.loaded.source_message_count > 0
            or bool(request.previous_summary_text)
        ),
        "summary_created": False,
        "summary_used": False,
        "summary_up_to_message_id": request.boundary_id,
        "summary_up_to_created_at": iso(request.boundary_dt),
        "source_message_count": request.loaded.source_message_count,
        "source_token_estimate": request.loaded.source_token_estimate,
        "image_caption_count": request.loaded.image_caption_count,
        "extra_instruction_hash": request.extra_hash,
    }


async def wait_for_summary_lock(
    session: Any,
    request: SummaryRequest,
    redis: Any,
    deps: SummaryServiceDependencies,
) -> dict[str, Any] | None:
    await deps.sleep(deps.lock_wait_s)
    latest = await deps.read_summary(session, request.conv_id)
    if summary_satisfies_request(latest, request.boundary, request.extra_hash):
        return public_summary_result(
            latest,
            created=False,
            status="cached_after_lock_wait",
        )
    await deps.record_metrics(
        redis,
        conv_id=request.conv_id,
        trigger=request.trigger,
        outcome="lock_busy",
    )
    return None


async def ensure_context_summary(
    session: Any,
    conv: Conversation,
    boundary: Any,
    settings: Any,
    *,
    force: bool,
    extra_instruction: str | None,
    dry_run: bool,
    trigger: str,
    image_upstream_runtime: Any,
    deps: SummaryServiceDependencies,
) -> dict[str, Any] | None:
    target_tokens = settings_int(
        settings,
        "context.summary_target_tokens",
        deps.target_tokens,
    )
    input_budget = settings_int(
        settings,
        "context.summary_input_budget",
        deps.input_budget,
    )
    summary_timeout_s = settings_float(
        settings,
        "context.summary_http_timeout_s",
        deps.timeout_s,
    )
    model = settings_str(settings, "context.summary_model", deps.model)
    circuit_threshold = settings_int(
        settings,
        "context.compression_circuit_breaker_threshold",
        60,
    )
    plan = await planning.build_summary_plan(
        session,
        conv,
        boundary,
        settings,
        force=force,
        extra_instruction=extra_instruction,
        dry_run=dry_run,
        trigger=trigger,
        target_tokens=target_tokens,
        input_budget=input_budget,
        summary_timeout_s=summary_timeout_s,
        model=model,
        circuit_threshold=circuit_threshold,
        load_messages=deps.load_messages,
        load_position=deps.load_position,
        boundary_id_fn=boundary_id,
        boundary_created_at_fn=boundary_created_at,
        extra_instruction_hash_fn=extra_instruction_hash,
        is_summary_usable_fn=is_summary_usable,
        summary_satisfies_request_fn=summary_satisfies_request,
        public_summary_result_fn=public_summary_result,
    )
    if plan.handled:
        return plan.immediate_result
    request = plan.request
    if request is None:
        return None
    if dry_run:
        return summary_dry_run_result(request)

    redis = (
        settings.get("redis") or settings.get("_redis")
        if isinstance(settings, dict)
        else getattr(settings, "redis", None) or getattr(settings, "_redis", None)
    )
    circuit_open = await deps.is_circuit_open(redis)
    if circuit_open:
        await deps.record_metrics(
            redis,
            conv_id=request.conv_id,
            trigger=trigger,
            outcome="circuit_open",
        )
    lock = await deps.acquire_lock(session, redis, request.conv_id)
    if lock is None:
        return await wait_for_summary_lock(session, request, redis, deps)
    return await run_locked_context_summary(
        session,
        request,
        redis,
        lock,
        circuit_open=circuit_open,
        image_upstream_runtime=image_upstream_runtime,
        deps=deps,
    )
