from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict, Unpack

from lumen_core.context_window import estimate_text_tokens
from lumen_core.models import Message

from .common import SummaryCoverage, SummarySegment


@dataclass(frozen=True)
class SummaryFallbackRuntime:
    message_to_line: Callable[..., str]
    call_upstream: Callable[..., Awaitable[str | None]]
    before_upstream: Callable[[], Awaitable[bool]] | None
    compose_input: Callable[[str | None, Sequence[str]], str]
    plan_segments: Callable[[Sequence[str], int], list[SummarySegment]]
    bound_segments: Callable[
        [Sequence[SummarySegment]], tuple[list[SummarySegment], str | None]
    ]
    set_partial: Callable[[Any, str, str, int], Awaitable[None]]
    logger: logging.Logger
    max_segments: int


class _SummaryFallbackArgs(TypedDict):
    conv_id: str
    messages: Sequence[Message]
    previous_summary: str | None
    target_tokens: int
    model: str
    input_budget: int
    timeout_s: float
    extra_instruction: str | None
    image_captions: Mapping[str, str] | None
    redis: Any
    progress_callback: Callable[[int, int], Awaitable[None]] | None
    coverage: SummaryCoverage | None
    runtime: SummaryFallbackRuntime


@dataclass(frozen=True)
class _SummaryFallbackRequest:
    conv_id: str
    messages: Sequence[Message]
    previous_summary: str | None
    target_tokens: int
    model: str
    input_budget: int
    timeout_s: float
    extra_instruction: str | None
    image_captions: Mapping[str, str] | None
    redis: Any
    progress_callback: Callable[[int, int], Awaitable[None]] | None
    coverage: SummaryCoverage | None
    runtime: SummaryFallbackRuntime


async def segment_and_summarize(
    **kwargs: Unpack[_SummaryFallbackArgs],
) -> str | None:
    request = _SummaryFallbackRequest(**kwargs)
    lines = [
        request.runtime.message_to_line(
            message,
            image_captions=request.image_captions,
        )
        for message in request.messages
    ]
    if not lines and not request.previous_summary:
        return None
    if _fits_input_budget(
        lines,
        request.previous_summary,
        request.input_budget,
    ):
        if not await _can_call_upstream(request):
            return None
        result = await request.runtime.call_upstream(
            request.runtime.compose_input(request.previous_summary, lines),
            request.target_tokens,
            request.model,
            extra_instruction=request.extra_instruction,
            timeout_s=request.timeout_s,
        )
        if result and request.coverage is not None:
            request.coverage.covered_message_count = len(request.messages)
        return result
    return await _summarize_segments(request, lines)


async def _can_call_upstream(request: _SummaryFallbackRequest) -> bool:
    before_upstream = request.runtime.before_upstream
    return before_upstream is None or await before_upstream()


def _fits_input_budget(
    lines: Sequence[str],
    previous_summary: str | None,
    input_budget: int,
) -> bool:
    line_tokens = sum(estimate_text_tokens(line) for line in lines)
    if previous_summary:
        line_tokens += estimate_text_tokens(previous_summary)
    return line_tokens <= input_budget


async def _summarize_segments(
    request: _SummaryFallbackRequest,
    lines: Sequence[str],
) -> str | None:
    runtime = request.runtime
    all_segments = runtime.plan_segments(lines, max(1, request.input_budget // 2))
    segments, bounded_reason = runtime.bound_segments(all_segments)
    if bounded_reason:
        runtime.logger.warning(
            "context_summary.too_many_segments conv=%s segments=%s planned=%s max=%s",
            request.conv_id,
            len(all_segments),
            len(segments),
            runtime.max_segments,
        )

    current_summary = request.previous_summary
    last_committable_summary: str | None = None
    for idx, segment in enumerate(segments, start=1):
        if not await _can_call_upstream(request):
            return None
        current_summary = await runtime.call_upstream(
            runtime.compose_input(current_summary, segment.lines),
            request.target_tokens,
            request.model,
            extra_instruction=request.extra_instruction,
            timeout_s=request.timeout_s,
        )
        if not current_summary:
            return _partial_segment_result(
                request.conv_id,
                idx=idx,
                total=len(segments),
                last_committable_summary=last_committable_summary,
                coverage=request.coverage,
                logger=runtime.logger,
            )
        await runtime.set_partial(
            request.redis,
            request.conv_id,
            current_summary,
            idx,
        )
        if segment.ends_at_message_boundary:
            last_committable_summary = current_summary
            if request.coverage is not None:
                request.coverage.covered_message_count = segment.covered_message_count
        await _report_progress(
            request.progress_callback,
            conv_id=request.conv_id,
            current=idx,
            total=len(segments),
            logger=runtime.logger,
        )

    if request.coverage is not None:
        request.coverage.partial_reason = bounded_reason
    return last_committable_summary


def _partial_segment_result(
    conv_id: str,
    *,
    idx: int,
    total: int,
    last_committable_summary: str | None,
    coverage: SummaryCoverage | None,
    logger: logging.Logger,
) -> str | None:
    if coverage is not None:
        coverage.partial_reason = "partial_segment_failure"
    if last_committable_summary:
        logger.warning(
            "context_summary.partial_segment_fallback conv=%s done=%d total=%d covered_messages=%d",
            conv_id,
            idx - 1,
            total,
            coverage.covered_message_count if coverage is not None else 0,
        )
    return last_committable_summary


async def _report_progress(
    callback: Callable[[int, int], Awaitable[None]] | None,
    *,
    conv_id: str,
    current: int,
    total: int,
    logger: logging.Logger,
) -> None:
    if callback is None or total <= 1:
        return
    try:
        await callback(current, total)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "context_summary.progress_callback_failed conv=%s err=%r",
            conv_id,
            exc,
        )
