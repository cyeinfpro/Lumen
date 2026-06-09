from __future__ import annotations

import httpx

from app.tasks.video_generation import (
    _is_retryable_video_exception,
    _submit_retry_delay_s,
    _video_exception_code,
    _video_exception_message,
)
from app.video_upstream import VideoUpstreamError


def test_blank_submit_timeout_gets_actionable_error_message() -> None:
    exc = httpx.ReadTimeout("")

    assert _video_exception_code(exc, default="provider_unavailable") == (
        "upstream_timeout"
    )
    assert _video_exception_message(exc, phase="submit") == (
        "video upstream submit failed: upstream_timeout (ReadTimeout)"
    )
    assert _is_retryable_video_exception(exc) is True


def test_retryable_video_upstream_errors_are_transient_only() -> None:
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError("busy", error_code="capacity", status_code=429)
        )
        is True
    )
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError(
                "gateway failed", error_code="provider_error", status_code=502
            )
        )
        is True
    )
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError("bad prompt", error_code="invalid_input", status_code=400)
        )
        is False
    )
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError("bad response", error_code="bad_response")
        )
        is False
    )


def test_submit_retry_delays_are_bounded() -> None:
    assert [_submit_retry_delay_s(attempt) for attempt in range(1, 6)] == [
        8,
        24,
        60,
        60,
        60,
    ]
