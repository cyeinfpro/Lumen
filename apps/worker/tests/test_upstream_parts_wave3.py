from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from app.upstream_parts import upstream_impl as upstream
from app.upstream_parts import (
    direct_failover,
    image_dispatch,
    image_job_failover,
    image_jobs,
    image_race,
    provider_selection,
    retry_policy,
)


TEST_UPSTREAM_RUNTIME = upstream.build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services


def _assert_runtime_bound(actual: Any, expected: Any) -> None:
    if "runtime" not in inspect.signature(expected).parameters:
        assert actual is expected
        return
    assert actual.func is expected
    assert actual.keywords["runtime"] is TEST_UPSTREAM_RUNTIME


def test_wave3_modules_are_owned_by_public_service_groups() -> None:
    exports = [
        (
            TEST_UPSTREAM_SERVICES.retry,
            retry_policy,
            (
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
        ),
        (
            TEST_UPSTREAM_SERVICES.providers,
            provider_selection,
            (
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
        ),
        (
            TEST_UPSTREAM_SERVICES.direct,
            direct_failover,
            (
                "_direct_generate_image_with_failover",
                "_direct_edit_image_with_failover",
                "_responses_image_stream_with_failover",
            ),
        ),
        (
            TEST_UPSTREAM_SERVICES.image_jobs,
            image_job_failover,
            (
                "_image_jobs_endpoint_fallback_chain",
                "_image_job_error_class",
                "_should_continue_image_job_failover",
                "_image_job_run_once",
                "_image_job_with_failover",
            ),
        ),
        (
            TEST_UPSTREAM_SERVICES.race,
            image_race,
            (
                "_drain_task_group_result",
                "_cancel_and_wait_tasks",
                "_race_responses_image",
                "_dual_race_image_action",
                "_dual_race_image_jobs_action",
            ),
        ),
        (
            TEST_UPSTREAM_SERVICES.dispatch,
            image_dispatch,
            (
                "_image_jobs_endpoint_for_engine",
                "_provider_supports_image_jobs",
                "_should_use_image_jobs",
                "_image_endpoint_kind_for_engine",
                "_image_dispatch_candidates",
                "_run_image_once_for_provider",
                "_dispatch_image",
            ),
        ),
    ]

    for service, module, names in exports:
        for name in names:
            _assert_runtime_bound(
                getattr(service, name.lstrip("_")),
                getattr(module, name),
            )

    assert upstream.generate_image is image_dispatch.generate_image
    assert upstream.edit_image is image_dispatch.edit_image


def test_wave3_facade_and_modules_stay_below_line_limits() -> None:
    public_source = Path(__file__).parents[1] / "app" / "upstream.py"
    assert len(public_source.read_text().splitlines()) < 200
    assert len(Path(upstream.__file__).read_text().splitlines()) < 3000

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

    assert (
        TEST_UPSTREAM_SERVICES.retry.max_attempts_for_exception(exc) == expected_attempts
    )


def test_quota_accounting_unavailable_stops_all_failover() -> None:
    exc = upstream.UpstreamError(
        "quota reservation unavailable",
        status_code=503,
        error_code=TEST_UPSTREAM_SERVICES.infrastructure.EC.QUOTA_ACCOUNTING_UNAVAILABLE.value,
    )

    assert not TEST_UPSTREAM_SERVICES.retry.should_continue_image_provider_failover(
        exc,
        retriable=True,
    )
    assert not TEST_UPSTREAM_SERVICES.image_jobs.should_continue_image_job_failover(
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

    monkeypatch.setattr(TEST_UPSTREAM_SERVICES.core, "RACE_CANCEL_WAIT_S", 1.25)
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure.asyncio, "wait_for", fake_wait_for
    )

    task = asyncio.create_task(pending_lane())
    await TEST_UPSTREAM_SERVICES.race.cancel_and_wait_tasks([task], label="wave3 test")

    assert observed_timeouts == [1.25]
    assert task.cancelled()


async def _finish_job_with_status(status: str) -> Exception:
    """跑 `_finish_image_job` 并把它抛出的错误交回来断言。"""
    with pytest.raises(upstream.UpstreamError) as excinfo:
        await image_jobs._finish_image_job(
            client=None,
            job={"job_id": "job-1", "status": status, "error": "sidecar said so"},
            status_code=200,
            payload={"endpoint": "/v1/images/generations"},
            base_url="http://sidecar.invalid",
            proxy_url=None,
            job_id="job-1",
            progress_callback=None,
            runtime=TEST_UPSTREAM_RUNTIME,
        )
    return excinfo.value


@pytest.mark.asyncio
async def test_uncertain_image_job_raises_result_unknown_code() -> None:
    """sidecar 的 uncertain 终态必须映射到「上游结果不可知」码。

    这个码同时驱动两件事：禁止换 provider 重试（避免二次上游成本），以及让
    计费侧走 settle 而不是 release（纯转嫁，平台不吸收上游成本）。
    """
    exc = await _finish_job_with_status("uncertain")

    assert (
        exc.error_code
        == TEST_UPSTREAM_SERVICES.infrastructure.EC.IMAGE_JOB_RESULT_UNKNOWN.value
    )
    assert exc.payload["upstream_result_unknown"] is True
    assert TEST_UPSTREAM_SERVICES.direct.is_direct_image_result_unknown(exc) is True


@pytest.mark.asyncio
async def test_failed_image_job_stays_refundable() -> None:
    # failed 代表 sidecar 能判定上游未交付且未扣费，行为保持不变（可退款）。
    exc = await _finish_job_with_status("failed")

    assert (
        exc.error_code
        != TEST_UPSTREAM_SERVICES.infrastructure.EC.IMAGE_JOB_RESULT_UNKNOWN.value
    )
    assert TEST_UPSTREAM_SERVICES.direct.is_direct_image_result_unknown(exc) is False


@pytest.mark.asyncio
async def test_unknown_image_job_status_is_bad_response() -> None:
    # 既不是终态也不是 uncertain 的状态仍按协议错误处理。
    exc = await _finish_job_with_status("weird")

    assert exc.error_code == TEST_UPSTREAM_SERVICES.infrastructure.EC.BAD_RESPONSE.value
