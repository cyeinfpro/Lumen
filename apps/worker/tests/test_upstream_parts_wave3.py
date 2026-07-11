from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app import upstream
from app.upstream_parts import (
    direct_failover,
    image_dispatch,
    image_job_failover,
    image_race,
    provider_selection,
    retry_policy,
)


def test_wave3_modules_are_exposed_through_upstream_facade() -> None:
    exports = {
        retry_policy: (
            "_summarize_exception",
            "_truncate_lane_summary",
            "_is_retryable_fallback_exception",
            "_fallback_retry_backoff_seconds",
            "_max_attempts_for_exception",
            "_retry_after_seconds",
            "_merge_fallback_errors",
            "_provider_error_details",
            "_mentions_safety_policy",
            "_should_continue_image_provider_failover",
            "_merge_image_path_errors",
            "_responses_image_stream_with_retry",
        ),
        provider_selection: (
            "_provider_pool_redis",
            "_pool_acquire_inflight",
            "_pool_release_inflight",
            "_is_byok_provider",
            "_provider_attempt_context",
            "_pool_report_image_success",
            "_pool_report_image_failure",
            "_provider_endpoint_locked_error",
            "_provider_capability_error",
            "_provider_endpoint_unavailable_error",
            "_provider_allows_image_endpoint",
            "_pool_select_compat",
            "_is_image_rate_limit_error",
            "_is_quota_accounting_unavailable",
            "_provider_has_image_quota",
            "_reserve_admin_image_call",
            "_image_request_attempt_claim",
            "_release_unused_image_reservation",
            "_image_quota_claim",
            "_record_admin_image_call_or_raise",
        ),
        direct_failover: (
            "_direct_generate_image_with_failover",
            "_direct_edit_image_with_failover",
            "_responses_image_stream_with_failover",
        ),
        image_job_failover: (
            "_image_jobs_endpoint_fallback_chain",
            "_image_job_error_class",
            "_should_continue_image_job_failover",
            "_image_job_run_once",
            "_image_job_with_failover",
        ),
        image_race: (
            "_drain_task_group_result",
            "_cancel_and_wait_tasks",
            "_race_responses_image",
            "_dual_race_image_action",
            "_dual_race_image_jobs_action",
        ),
        image_dispatch: (
            "_image_jobs_endpoint_for_engine",
            "_provider_supports_image_jobs",
            "_should_use_image_jobs",
            "_image_endpoint_kind_for_engine",
            "_image_dispatch_candidates",
            "_run_image_once_for_provider",
            "_dispatch_image",
            "generate_image",
            "edit_image",
        ),
    }

    for module, names in exports.items():
        for name in names:
            assert getattr(upstream, name) is getattr(module, name)


def test_wave3_facade_and_modules_stay_below_line_limits() -> None:
    upstream_source = Path(upstream.__file__).read_text()
    assert len(upstream_source.splitlines()) < 3000

    for module in (
        retry_policy,
        provider_selection,
        direct_failover,
        image_job_failover,
        image_race,
        image_dispatch,
    ):
        source = Path(module.__file__).read_text()
        assert len(source.splitlines()) < 800, module.__name__


@pytest.mark.parametrize(
    ("status_code", "expected_attempts"),
    [
        (503, 3),
        (429, 5),
        (422, 1),
    ],
)
def test_retry_budget_classification_survives_extraction(
    status_code: int,
    expected_attempts: int,
) -> None:
    exc = upstream.UpstreamError(
        "classified upstream failure",
        status_code=status_code,
        error_code="classified_error",
    )

    assert upstream._max_attempts_for_exception(exc) == expected_attempts


def test_quota_accounting_unavailable_stops_all_failover() -> None:
    exc = upstream.UpstreamError(
        "quota reservation unavailable",
        status_code=503,
        error_code=upstream.EC.QUOTA_ACCOUNTING_UNAVAILABLE.value,
    )

    assert not upstream._should_continue_image_provider_failover(
        exc,
        retriable=True,
    )
    assert not upstream._should_continue_image_job_failover(
        exc,
        retriable=True,
    )


@pytest.mark.asyncio
async def test_race_cancel_timeout_is_read_from_late_bound_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable: Any, *, timeout: float) -> Any:
        observed_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=1.0)

    async def pending_lane() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(upstream, "_RACE_CANCEL_WAIT_S", 1.25)
    monkeypatch.setattr(upstream.asyncio, "wait_for", fake_wait_for)

    task = asyncio.create_task(pending_lane())
    await upstream._cancel_and_wait_tasks([task], label="wave3 test")

    assert observed_timeouts == [1.25]
    assert task.cancelled()
