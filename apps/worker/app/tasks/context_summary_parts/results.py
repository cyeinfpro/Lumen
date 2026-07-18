"""Context-summary request/result value objects and serializers."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .common import LoadedSummaryMessages, SummaryCoverage, utc_now
from .messages import loaded_summary_prefix


@dataclass(frozen=True)
class SummaryRequest:
    conv_id: str
    boundary: Any
    boundary_id: str
    boundary_dt: datetime
    settings: Any
    target_tokens: int
    input_budget: int
    summary_timeout_s: float
    model: str
    circuit_threshold: int
    extra_instruction: str | None
    extra_hash: str | None
    existing_summary: dict[str, Any] | None
    previous_summary_text: str | None
    loaded: LoadedSummaryMessages
    trigger: str
    force: bool


@dataclass(frozen=True)
class SummaryTiming:
    started_at: datetime
    started_monotonic: float


@dataclass(frozen=True)
class SummaryGenerationResult:
    text: str
    loaded: LoadedSummaryMessages
    coverage: SummaryCoverage
    fallback_reason: str | None


def summary_event_payload(
    request: SummaryRequest,
    timing: SummaryTiming,
    *,
    phase: str,
    ok: bool | None,
    fallback_reason: str | None,
    progress: tuple[int, int] | None = None,
    public: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conversation_id": request.conv_id,
        "phase": phase,
        "trigger": request.trigger,
        "started_at": timing.started_at.isoformat(),
        "completed_at": utc_now().isoformat() if phase == "completed" else None,
        "elapsed_ms": (
            int((time.monotonic() - timing.started_monotonic) * 1000)
            if phase == "completed"
            else None
        ),
        "ok": ok,
        "fallback_reason": fallback_reason,
    }
    if progress is not None:
        payload["progress"] = {
            "current_segment": progress[0],
            "total_segments": progress[1],
        }
    if public is not None:
        payload["stats"] = {
            "summary_tokens": public["summary_tokens"],
            "source_message_count": public["source_message_count"],
            "source_token_estimate": public["source_token_estimate"],
            "image_caption_count": public["image_caption_count"],
            "tokens_freed": public["tokens_freed"],
            "summary_up_to_message_id": public["summary_up_to_message_id"],
        }
    return payload


def normalize_summary_coverage(
    summary_text: str | None,
    coverage: SummaryCoverage,
    loaded: LoadedSummaryMessages,
) -> None:
    # Older monkeypatches return a successful string without the coverage object.
    if (
        summary_text
        and coverage.covered_message_count == 0
        and coverage.partial_reason is None
    ):
        coverage.covered_message_count = len(loaded.messages)


def effective_summary_window(
    request: SummaryRequest,
    generated: SummaryGenerationResult,
) -> tuple[LoadedSummaryMessages, str, datetime] | None:
    covered_count = generated.coverage.covered_message_count
    if covered_count >= len(generated.loaded.messages):
        return generated.loaded, request.boundary_id, request.boundary_dt

    effective_loaded = loaded_summary_prefix(
        generated.loaded,
        covered_count,
    )
    if not effective_loaded.messages:
        return None
    covered_boundary = effective_loaded.messages[-1]
    return effective_loaded, str(covered_boundary.id), covered_boundary.created_at


def worker_compact_summary_payload(
    *,
    result: dict[str, Any],
    conv: Any,
) -> dict[str, Any]:
    summary = conv.summary_jsonb if isinstance(conv.summary_jsonb, dict) else {}
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
        "compressed_at": summary.get("compressed_at"),
        "status": result.get("status"),
    }
