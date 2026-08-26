"""Pure video poll-result normalization helpers."""

from __future__ import annotations

from ...video_artifacts import InvalidVideoArtifactError
from ...video_upstream_service import PollResult
from .runtime import video_ports


def cancelled_poll_during_finalization(poll: PollResult) -> PollResult:
    raw = {
        **(poll.raw or {}),
        "reason": "cancel_requested_during_finalization",
        "provider_status": poll.status,
    }
    if poll.usage_total_tokens is None and poll.upstream_billable is None:
        raw["upstream_cost_ambiguous"] = True
    return PollResult(
        status="cancelled",
        failure_class="canceled",
        usage_total_tokens=poll.usage_total_tokens,
        upstream_billable=poll.upstream_billable,
        raw=raw,
    )


def invalid_video_artifact_poll(
    poll: PollResult,
    exc: InvalidVideoArtifactError,
) -> PollResult:
    raw = {
        **(poll.raw or {}),
        "reason": video_ports().policy._INVALID_VIDEO_ARTIFACT_REASON,
        "phase": "artifact_validation",
        "provider_status": poll.status,
        "error": str(exc)[:1000],
        "error_code": exc.error_code,
        "artifact_diagnostics": exc.diagnostics,
    }
    if poll.usage_total_tokens is None and poll.upstream_billable is None:
        raw["upstream_cost_ambiguous"] = True
    return PollResult(
        status="failed",
        failure_class=exc.error_code,
        usage_total_tokens=poll.usage_total_tokens,
        upstream_billable=poll.upstream_billable,
        raw=raw,
    )


__all__ = ["cancelled_poll_during_finalization", "invalid_video_artifact_poll"]
