from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.tasks.generation_parts.execution_boundary import (
    release_or_settle_generation,
)
from app.tasks.generation_parts.progress import ImageProgressPublisher
from app.upstream_clients.image_job_models import (
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
    ImageJobHandle,
    ImageJobRecoveryOutcome,
    ImageJobResultState,
    ImageJobStatus,
)
from app.upstream_parts import image_jobs
from app.upstream_parts.image_execution import (
    ImageExecutionRequest,
    ImageRequestContext,
)
from app.upstream_parts.upstream_impl import build_image_upstream_runtime


TEST_RUNTIME = build_image_upstream_runtime()
TEST_SERVICES = TEST_RUNTIME.services


def _execution(
    *,
    result_state: ImageJobResultState = ImageJobResultState.PENDING,
    cost_knowledge: ImageJobCostKnowledge = ImageJobCostKnowledge.UNKNOWN,
    result_artifact: dict[str, Any] | None = None,
) -> ImageJobExecutionHandle:
    return ImageJobExecutionHandle(
        job_id="job-accepted",
        provider_id="provider-a",
        endpoint="responses",
        base_url="https://image-job.example",
        idempotency_key="lumen-image-job-stable",
        result_state=result_state,
        cost_knowledge=cost_knowledge,
        sidecar_status=(
            "succeeded" if result_state == ImageJobResultState.SUCCEEDED else "accepted"
        ),
        result_artifact=result_artifact,
    )


def _request(
    execution: ImageJobExecutionHandle,
    *,
    progress_callback: Any = None,
) -> ImageExecutionRequest:
    return ImageExecutionRequest(
        action="generate",
        prompt="draw",
        size="1024x1024",
        images=None,
        mask=None,
        n=1,
        quality="high",
        output_format="png",
        output_compression=None,
        background="auto",
        moderation="auto",
        model="gpt-image",
        progress_callback=progress_callback,
        provider_override=SimpleNamespace(name="provider-a", api_key="sk-provider"),
        user_id="user-1",
        request_context=ImageRequestContext.create(
            trace_id="trace-1",
            sidecar_execution=execution,
            upstream_runtime=TEST_RUNTIME,
        ),
        upstream_runtime=TEST_RUNTIME,
    )


def test_execution_handle_has_typed_recovery_outcomes() -> None:
    pending = _execution()
    succeeded = _execution(
        result_state=ImageJobResultState.SUCCEEDED,
        cost_knowledge=ImageJobCostKnowledge.INCURRED,
        result_artifact={"url": "https://image-job.example/result.png"},
    )
    failed = _execution(
        result_state=ImageJobResultState.FAILED,
        cost_knowledge=ImageJobCostKnowledge.NONE,
    )

    assert pending.recovery_outcome == ImageJobRecoveryOutcome.POLL
    assert succeeded.recovery_outcome == ImageJobRecoveryOutcome.DELIVER
    assert failed.recovery_outcome == ImageJobRecoveryOutcome.TERMINAL
    assert ImageJobExecutionHandle.from_mapping(succeeded.to_dict()) == succeeded


def test_sidecar_payload_is_clamped_to_one_upstream_image() -> None:
    body = image_jobs._image_job_body_base(
        prompt="draw",
        size="1024x1024",
        n=7,
        quality="high",
        output_format="png",
        output_compression=None,
        background="auto",
        moderation="auto",
        runtime=TEST_RUNTIME,
    )

    assert body["n"] == 1


@pytest.mark.asyncio
async def test_accepted_timeout_recovers_original_job_without_second_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = 0
    recovery_polls = 0
    progress_events: list[dict[str, Any]] = []

    class SubmitClient:
        async def submit(self, *_args: Any, **_kwargs: Any) -> ImageJobHandle:
            nonlocal submitted
            submitted += 1
            return ImageJobHandle(
                job_id="job-accepted",
                upstream_api_key="sk-provider",
            )

        async def close(self) -> None:
            return None

    class RecoveryClient:
        async def submit(self, *_args: Any, **_kwargs: Any) -> ImageJobHandle:
            raise AssertionError("recovery must never submit another image job")

        async def poll(
            self,
            _handle: ImageJobHandle,
            *,
            trace_id: str,
        ) -> ImageJobStatus:
            nonlocal recovery_polls
            assert trace_id == "trace-1"
            recovery_polls += 1
            return ImageJobStatus(
                payload={
                    "job_id": "job-accepted",
                    "status": "succeeded",
                    "endpoint_used": "responses",
                    "images": [
                        {
                            "url": "https://image-job.example/result.png",
                            "format": "png",
                        }
                    ],
                },
                status_code=200,
            )

        async def close(self) -> None:
            return None

    clients = iter([SubmitClient(), RecoveryClient()])
    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "build_image_job_client",
        lambda _base_url: next(clients),
    )
    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_TIMEOUT_S", 0.0)
    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "download_image_job_result",
        lambda **_kwargs: _return_bytes(b"recovered"),
    )

    with pytest.raises(TEST_SERVICES.infrastructure.UpstreamError) as exc_info:
        await TEST_SERVICES.image_jobs.submit_and_wait_image_job(
            payload={
                "request_type": "responses",
                "endpoint": "/v1/responses",
                "body": {},
            },
            base_url="https://image-job.example",
            api_key="sk-provider",
            provider_id="provider-a",
            endpoint="responses",
            proxy=None,
            progress_callback=progress_events.append,
            request_context=ImageRequestContext.create(
                trace_id="trace-1",
                upstream_runtime=TEST_RUNTIME,
            ),
            runtime=TEST_RUNTIME,
        )

    assert submitted == 1
    assert exc_info.value.error_code == "image_job_result_unknown"
    assert exc_info.value.payload["recovery_only"] is True
    accepted = ImageJobExecutionHandle.from_mapping(
        exc_info.value.payload["sidecar_execution"]
    )
    assert accepted is not None
    assert accepted.recovery_outcome == ImageJobRecoveryOutcome.POLL

    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_TIMEOUT_S", 10.0)
    result = await TEST_SERVICES.image_jobs.resume_image_job(
        _request(accepted, progress_callback=progress_events.append)
    )

    assert result[0] == "cmVjb3ZlcmVk"
    assert submitted == 1
    assert recovery_polls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_failure",
    ["timeout", "http_404", "empty", "over_limit"],
)
async def test_succeeded_retry_is_delivery_only(
    monkeypatch: pytest.MonkeyPatch,
    first_failure: str,
) -> None:
    execution = _execution(
        result_state=ImageJobResultState.SUCCEEDED,
        cost_knowledge=ImageJobCostKnowledge.INCURRED,
        result_artifact={"url": "https://image-job.example/result.png"},
    )
    download_attempts = 0

    class DeliveryClient:
        async def submit(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("delivery recovery must not submit")

        async def poll(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("delivery recovery must not poll")

        async def close(self) -> None:
            return None

    async def download(**_kwargs: Any) -> bytes:
        nonlocal download_attempts
        download_attempts += 1
        if download_attempts == 1:
            if first_failure == "timeout":
                raise httpx.ReadTimeout("artifact timeout")
            if first_failure == "http_404":
                raise TEST_SERVICES.infrastructure.UpstreamError(
                    "artifact not found",
                    status_code=404,
                    error_code="direct_image_request_failed",
                )
            if first_failure == "over_limit":
                raise OSError("artifact exceeds configured byte limit")
            return b""
        return b"delivered"

    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "build_image_job_client",
        lambda _base_url: DeliveryClient(),
    )
    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "download_image_job_result",
        download,
    )

    with pytest.raises(TEST_SERVICES.infrastructure.UpstreamError) as exc_info:
        await TEST_SERVICES.image_jobs.resume_image_job(_request(execution))

    assert exc_info.value.error_code == "image_job_result_unknown"
    assert exc_info.value.payload["delivery_only"] is True
    assert exc_info.value.payload["recovery_only"] is True

    result = await TEST_SERVICES.image_jobs.resume_image_job(_request(execution))
    assert result[0] == "ZGVsaXZlcmVk"
    assert download_attempts == 2


@pytest.mark.asyncio
async def test_progress_persists_accepted_execution_before_polling() -> None:
    execution = _execution()
    generation = SimpleNamespace(
        attempt=2,
        execution_epoch=6,
        status="running",
        upstream_request={"trace_id": "trace-1"},
    )
    commits = 0
    statements: list[Any] = []

    class Result:
        def scalar_one_or_none(self) -> Any:
            return generation

    class Session:
        async def execute(self, statement: Any) -> Result:
            statements.append(statement)
            return Result()

        async def commit(self) -> None:
            nonlocal commits
            commits += 1

    class Store:
        @asynccontextmanager
        async def session(self) -> Any:
            yield Session()

    state = SimpleNamespace(
        task_id="generation-1",
        attempt=2,
        generation=SimpleNamespace(execution_epoch=6),
        sidecar_execution=None,
        gen_upstream_request_snapshot={"trace_id": "trace-1"},
    )
    publisher = ImageProgressPublisher(
        state,
        SimpleNamespace(store=Store()),
    )

    await publisher(
        {
            "type": "image_job_execution",
            "execution": execution.to_dict(),
        }
    )

    assert commits == 1
    assert generation.upstream_request["sidecar_execution"] == execution.to_dict()
    assert state.sidecar_execution == execution
    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "generations.execution_epoch = 6" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("knowledge", "expected_call"),
    [
        (ImageJobCostKnowledge.NONE, "release"),
        (ImageJobCostKnowledge.UNKNOWN, "settle:unknown"),
        (ImageJobCostKnowledge.INCURRED, "settle:incurred"),
    ],
)
async def test_billing_never_releases_unknown_or_incurred_sidecar_cost(
    knowledge: ImageJobCostKnowledge,
    expected_call: str,
) -> None:
    execution = _execution(cost_knowledge=knowledge)
    generation = SimpleNamespace(
        upstream_request={"sidecar_execution": execution.to_dict()}
    )
    calls: list[str] = []

    class Billing:
        async def release(self, *_args: Any, **_kwargs: Any) -> None:
            calls.append("release")

        async def settle_unknown_upstream(
            self,
            *_args: Any,
            knowledge: str,
            **_kwargs: Any,
        ) -> None:
            calls.append(f"settle:{knowledge}")

    await release_or_settle_generation(
        Billing(),
        object(),
        generation,
        reason="terminal",
    )

    assert calls == [expected_call]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_payload", "execution_epoch", "expected_call"),
    [
        # 直接引擎：请求已派发、结果不可知 → 必须结算而不是释放（纯转嫁铁律）。
        (
            {
                "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
                "upstream_dispatch_attempt": 2,
                "upstream_dispatch_execution_epoch": 3,
            },
            3,
            "settle:unknown",
        ),
        # 已派发且已收到同 attempt 的明确应答（如 400 内容政策拒绝）→ 失败语义
        # 交给决策表（PROVEN_ABSENT → release），不得按 dispatch 收据结算，
        # 否则把用户 hold 全额扣掉（多收钱）。
        (
            {
                "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
                "upstream_dispatch_attempt": 2,
                "upstream_dispatch_execution_epoch": 3,
                "upstream_response_received_at": "2026-07-30T00:00:01+00:00",
                "upstream_response_attempt": 2,
                "upstream_response_execution_epoch": 3,
            },
            3,
            "release",
        ),
        # 应答收据属于更早 attempt（同 epoch 重试后当前 dispatch 仍无应答）→
        # 当前请求结果仍不可知，必须结算。
        (
            {
                "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
                "upstream_dispatch_attempt": 3,
                "upstream_dispatch_execution_epoch": 3,
                "upstream_response_received_at": "2026-07-30T00:00:01+00:00",
                "upstream_response_attempt": 2,
                "upstream_response_execution_epoch": 3,
            },
            3,
            "settle:unknown",
        ),
        # 已派发但可证明未送达（proven_undelivered）→ 允许释放。
        (
            {
                "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
                "upstream_dispatch_attempt": 2,
                "upstream_dispatch_execution_epoch": 3,
                "upstream_dispatch_delivery": "proven_undelivered",
            },
            3,
            "release",
        ),
        # 收据属于更早的执行纪元（手动重试已推进）→ 释放当前纪元的 hold。
        (
            {
                "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
                "upstream_dispatch_attempt": 2,
                "upstream_dispatch_execution_epoch": 3,
            },
            4,
            "release",
        ),
        # 从未派发 → 允许释放。
        ({}, 3, "release"),
    ],
)
async def test_release_respects_direct_engine_dispatch_receipt(
    request_payload: dict[str, Any],
    execution_epoch: int,
    expected_call: str,
) -> None:
    generation = SimpleNamespace(
        execution_epoch=execution_epoch,
        upstream_request=request_payload,
    )
    calls: list[str] = []

    class Billing:
        async def release(self, *_args: Any, **_kwargs: Any) -> None:
            calls.append("release")

        async def settle_unknown_upstream(
            self,
            *_args: Any,
            knowledge: str,
            **_kwargs: Any,
        ) -> None:
            calls.append(f"settle:{knowledge}")

    await release_or_settle_generation(
        Billing(),
        object(),
        generation,
        reason="terminal",
    )

    assert calls == [expected_call]


async def _return_bytes(value: bytes) -> bytes:
    return value
