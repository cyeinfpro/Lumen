from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lumen_core.context_window import estimate_text_tokens
from lumen_core.models import Message

from .common import SummaryCoverage, SummarySegment


@dataclass(frozen=True)
class SummaryFallbackRuntime:
    message_to_line: Callable[..., str]
    call_upstream: Callable[..., Awaitable[str | None]]
    compose_input: Callable[[str | None, Sequence[str]], str]
    plan_segments: Callable[[Sequence[str], int], list[SummarySegment]]
    bound_segments: Callable[
        [Sequence[SummarySegment]], tuple[list[SummarySegment], str | None]
    ]
    set_partial: Callable[[Any, str, str, int], Awaitable[None]]
    logger: logging.Logger
    max_segments: int


async def segment_and_summarize(
    *,
    conv_id: str,
    messages: Sequence[Message],
    previous_summary: str | None,
    target_tokens: int,
    model: str,
    input_budget: int,
    timeout_s: float,
    extra_instruction: str | None,
    image_captions: Mapping[str, str] | None,
    redis: Any,
    progress_callback: Callable[[int, int], Awaitable[None]] | None,
    coverage: SummaryCoverage | None,
    runtime: SummaryFallbackRuntime,
) -> str | None:
    lines = [
        runtime.message_to_line(message, image_captions=image_captions)
        for message in messages
    ]
    if not lines and not previous_summary:
        return None
    if _fits_input_budget(lines, previous_summary, input_budget):
        return await _summarize_single_input(
            lines,
            messages=messages,
            previous_summary=previous_summary,
            target_tokens=target_tokens,
            model=model,
            timeout_s=timeout_s,
            extra_instruction=extra_instruction,
            coverage=coverage,
            runtime=runtime,
        )
    return await _summarize_segments(
        conv_id=conv_id,
        messages=messages,
        lines=lines,
        previous_summary=previous_summary,
        target_tokens=target_tokens,
        model=model,
        input_budget=input_budget,
        timeout_s=timeout_s,
        extra_instruction=extra_instruction,
        redis=redis,
        progress_callback=progress_callback,
        coverage=coverage,
        runtime=runtime,
    )


def _fits_input_budget(
    lines: Sequence[str],
    previous_summary: str | None,
    input_budget: int,
) -> bool:
    line_tokens = sum(estimate_text_tokens(line) for line in lines)
    if previous_summary:
        line_tokens += estimate_text_tokens(previous_summary)
    return line_tokens <= input_budget


async def _summarize_single_input(
    lines: Sequence[str],
    *,
    messages: Sequence[Message],
    previous_summary: str | None,
    target_tokens: int,
    model: str,
    timeout_s: float,
    extra_instruction: str | None,
    coverage: SummaryCoverage | None,
    runtime: SummaryFallbackRuntime,
) -> str | None:
    result = await runtime.call_upstream(
        runtime.compose_input(previous_summary, lines),
        target_tokens,
        model,
        extra_instruction=extra_instruction,
        timeout_s=timeout_s,
    )
    if result and coverage is not None:
        coverage.covered_message_count = len(messages)
    return result


async def _summarize_segments(
    *,
    conv_id: str,
    messages: Sequence[Message],
    lines: Sequence[str],
    previous_summary: str | None,
    target_tokens: int,
    model: str,
    input_budget: int,
    timeout_s: float,
    extra_instruction: str | None,
    redis: Any,
    progress_callback: Callable[[int, int], Awaitable[None]] | None,
    coverage: SummaryCoverage | None,
    runtime: SummaryFallbackRuntime,
) -> str | None:
    all_segments = runtime.plan_segments(lines, max(1, input_budget // 2))
    segments, bounded_reason = runtime.bound_segments(all_segments)
    if bounded_reason:
        runtime.logger.warning(
            "context_summary.too_many_segments conv=%s segments=%s planned=%s max=%s",
            conv_id,
            len(all_segments),
            len(segments),
            runtime.max_segments,
        )

    current_summary = previous_summary
    last_committable_summary: str | None = None
    for idx, segment in enumerate(segments, start=1):
        current_summary = await runtime.call_upstream(
            runtime.compose_input(current_summary, segment.lines),
            target_tokens,
            model,
            extra_instruction=extra_instruction,
            timeout_s=timeout_s,
        )
        if not current_summary:
            return _partial_segment_result(
                conv_id,
                idx=idx,
                total=len(segments),
                last_committable_summary=last_committable_summary,
                coverage=coverage,
                logger=runtime.logger,
            )
        await runtime.set_partial(redis, conv_id, current_summary, idx)
        if segment.ends_at_message_boundary:
            last_committable_summary = current_summary
            if coverage is not None:
                coverage.covered_message_count = segment.covered_message_count
        await _report_progress(
            progress_callback,
            conv_id=conv_id,
            current=idx,
            total=len(segments),
            logger=runtime.logger,
        )

    if coverage is not None:
        coverage.partial_reason = bounded_reason
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
