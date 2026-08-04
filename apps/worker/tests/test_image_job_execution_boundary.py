from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.tasks.generation_parts import runner_dispatch_phase
from app.tasks.generation_parts.execution_boundary import (
    SIDECAR_EXECUTIONS_KEY,
    release_or_settle_generation,
    sidecar_execution_from_request,
    sidecar_executions_from_request,
)
from app.tasks.generation_parts.progress import ImageProgressPublisher
from app.upstream_clients.image_job_models import (
    ImageJobCancelOutcome,
    ImageJobCancelResult,
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
    ImageJobHandle,
    ImageJobRecoveryOutcome,
    ImageJobResultState,
    ImageJobStatus,
)
from app.upstream_parts import image_job_recovery, image_jobs, image_race
from app.upstream_parts.image_execution import (
    ImageExecutionRequest,
    ImageRequestContext,
)
from app.upstream_parts.upstream_impl import build_image_upstream_runtime


TEST_RUNTIME = build_image_upstream_runtime()
TEST_SERVICES = TEST_RUNTIME.services


def _execution(
    *,
    job_id: str = "job-accepted",
    provider_id: str = "provider-a",
    endpoint: str = "responses",
    idempotency_key: str = "lumen-image-job-stable",
    result_state: ImageJobResultState = ImageJobResultState.PENDING,
    cost_knowledge: ImageJobCostKnowledge = ImageJobCostKnowledge.UNKNOWN,
    result_artifact: dict[str, Any] | None = None,
) -> ImageJobExecutionHandle:
    return ImageJobExecutionHandle(
        job_id=job_id,
        provider_id=provider_id,
        endpoint=endpoint,
        base_url="https://image-job.example",
        idempotency_key=idempotency_key,
        result_state=result_state,
        cost_knowledge=cost_knowledge,
        sidecar_status=(
            "succeeded" if result_state == ImageJobResultState.SUCCEEDED else "accepted"
        ),
        result_artifact=result_artifact,
    )


def _request(
    execution: ImageJobExecutionHandle | tuple[ImageJobExecutionHandle, ...],
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
    assert sidecar_executions_from_request(
        {"sidecar_execution": pending.to_dict()}
    ) == (pending,)


@pytest.mark.parametrize(
    ("status", "outcome", "outcome_uncertain", "expected_state", "expected_cost"),
    [
        (
            "succeeded",
            ImageJobCancelOutcome.ALREADY_TERMINAL,
            False,
            ImageJobResultState.SUCCEEDED,
            ImageJobCostKnowledge.INCURRED,
        ),
        (
            "unknown",
            ImageJobCancelOutcome.UNCERTAIN,
            True,
            ImageJobResultState.UNCERTAIN,
            ImageJobCostKnowledge.UNKNOWN,
        ),
    ],
)
def test_cancelled_execution_preserves_bonus_cost_knowledge(
    status: str,
    outcome: ImageJobCancelOutcome,
    outcome_uncertain: bool,
    expected_state: ImageJobResultState,
    expected_cost: ImageJobCostKnowledge,
) -> None:
    result = image_job_recovery.execution_after_cancel(
        _execution(endpoint="responses"),
        ImageJobCancelResult(
            job_id="job-accepted",
            outcome=outcome,
            status=status,
            status_code=200,
            outcome_uncertain=outcome_uncertain,
        ),
    )

    assert result.result_state == expected_state
    assert result.cost_knowledge == expected_cost
    assert result.sidecar_status == status
    assert result.cancel_outcome == outcome


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
    ("cancel_result", "expected_state", "expected_cost"),
    [
        (
            ImageJobCancelResult(
                job_id="job-accepted",
                outcome=ImageJobCancelOutcome.ALREADY_TERMINAL,
                status="succeeded",
                status_code=200,
                outcome_uncertain=False,
            ),
            ImageJobResultState.SUCCEEDED,
            ImageJobCostKnowledge.INCURRED,
        ),
        (
            ImageJobCancelResult(
                job_id="job-accepted",
                outcome=ImageJobCancelOutcome.UNCERTAIN,
                status="unknown",
                status_code=None,
                outcome_uncertain=True,
            ),
            ImageJobResultState.UNCERTAIN,
            ImageJobCostKnowledge.UNKNOWN,
        ),
    ],
)
async def test_recovered_loser_cancel_persists_cost_outcome(
    monkeypatch: pytest.MonkeyPatch,
    cancel_result: ImageJobCancelResult,
    expected_state: ImageJobResultState,
    expected_cost: ImageJobCostKnowledge,
) -> None:
    poll_started = asyncio.Event()
    progress_events: list[dict[str, Any]] = []
    cancel_calls = 0

    class RecoveryClient:
        async def poll(
            self,
            _handle: ImageJobHandle,
            *,
            trace_id: str,
        ) -> ImageJobStatus:
            assert trace_id == "trace-1"
            poll_started.set()
            await asyncio.Event().wait()
            raise AssertionError("poll must be cancelled")

        async def cancel(
            self,
            _handle: ImageJobHandle,
            *,
            trace_id: str,
        ) -> ImageJobCancelResult:
            nonlocal cancel_calls
            assert trace_id == "trace-1"
            cancel_calls += 1
            return cancel_result

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "build_image_job_client",
        lambda _base_url: RecoveryClient(),
    )
    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_TIMEOUT_S", 10.0)
    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_POLL_INTERVAL_S", 0.0)

    task = asyncio.create_task(
        TEST_SERVICES.image_jobs.resume_image_job(
            _request(
                _execution(endpoint="responses"),
                progress_callback=progress_events.append,
            )
        )
    )
    await asyncio.wait_for(poll_started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    executions = [
        ImageJobExecutionHandle.from_mapping(event.get("execution"))
        for event in progress_events
        if event.get("type") == "image_job_execution"
    ]
    final = next(execution for execution in reversed(executions) if execution)
    assert cancel_calls == 1
    assert final.result_state == expected_state
    assert final.cost_knowledge == expected_cost
    assert final.cancel_outcome == cancel_result.outcome


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

    secondary_progress = image_race._metadata_only_progress(  # noqa: SLF001
        publisher,
        runtime=TEST_RUNTIME,
    )
    await secondary_progress(
        {
            "type": "image_job_execution",
            "execution": execution.to_dict(),
        }
    )

    assert commits == 1
    assert generation.upstream_request["sidecar_execution"] == execution.to_dict()
    assert SIDECAR_EXECUTIONS_KEY not in generation.upstream_request
    assert state.sidecar_execution == execution
    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "generations.execution_epoch = 6" in sql


@pytest.mark.asyncio
async def test_accepted_execution_db_failure_stops_before_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = SimpleNamespace(
        attempt=2,
        execution_epoch=6,
        status="running",
        upstream_request={"trace_id": "trace-1"},
    )
    commits = 0
    polls = 0
    downloads = 0
    closes = 0

    class Result:
        def scalar_one_or_none(self) -> Any:
            return generation

    class Session:
        async def execute(self, _statement: Any) -> Result:
            return Result()

        async def commit(self) -> None:
            nonlocal commits
            commits += 1
            raise RuntimeError("image execution receipt commit failed")

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

    class Client:
        async def submit(self, *_args: Any, **_kwargs: Any) -> ImageJobHandle:
            return ImageJobHandle(
                job_id="job-db-failure",
                upstream_api_key="sk-provider",
            )

        async def poll(self, *_args: Any, **_kwargs: Any) -> ImageJobStatus:
            nonlocal polls
            polls += 1
            raise AssertionError("accepted job must not poll without a durable receipt")

        async def close(self) -> None:
            nonlocal closes
            closes += 1

    async def download_result(**_kwargs: Any) -> bytes:
        nonlocal downloads
        downloads += 1
        raise AssertionError("accepted job must not succeed without a durable receipt")

    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "build_image_job_client",
        lambda _base_url: Client(),
    )
    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_TIMEOUT_S", 10.0)
    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "download_image_job_result",
        download_result,
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
            progress_callback=publisher,
            request_context=ImageRequestContext.create(
                trace_id="trace-1",
                upstream_runtime=TEST_RUNTIME,
            ),
            runtime=TEST_RUNTIME,
        )

    error = exc_info.value
    assert error.error_code == "image_job_result_unknown"
    assert error.payload["receipt_persist_failed"] is True
    assert error.payload["sidecar_execution_accepted"] is True
    assert error.payload["sidecar_execution"]["job_id"] == "job-db-failure"
    assert error.payload.get("recovery_only") is not True
    assert isinstance(error.__cause__, RuntimeError)
    assert str(error.__cause__) == "image execution receipt commit failed"
    assert commits == 1
    assert polls == 0
    assert downloads == 0
    assert closes == 1
    assert state.sidecar_execution is None
    assert state.gen_upstream_request_snapshot == {"trace_id": "trace-1"}


@pytest.mark.asyncio
async def test_noncritical_image_progress_callback_failure_is_best_effort() -> None:
    async def fail_progress(_event: dict[str, Any]) -> None:
        raise RuntimeError("noncritical progress unavailable")

    await TEST_SERVICES.transport.emit_image_progress(
        fail_progress,
        "fallback_started",
        runtime=TEST_RUNTIME,
        source="image_jobs",
    )


@pytest.mark.asyncio
async def test_two_accepted_dual_race_jobs_recover_without_resubmit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generations_execution = _execution(
        job_id="job-generations",
        endpoint="generations",
        idempotency_key="idem-generations",
    )
    responses_execution = _execution(
        job_id="job-responses",
        endpoint="responses",
        idempotency_key="idem-responses",
    )
    generation = SimpleNamespace(
        attempt=2,
        execution_epoch=6,
        status="running",
        upstream_request={"trace_id": "trace-1"},
    )

    class Result:
        def scalar_one_or_none(self) -> Any:
            return generation

    class Session:
        async def execute(self, _statement: Any) -> Result:
            return Result()

        async def commit(self) -> None:
            return None

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
    secondary_progress = image_race._metadata_only_progress(  # noqa: SLF001
        publisher,
        runtime=TEST_RUNTIME,
    )

    await publisher(
        {
            "type": "image_job_execution",
            "execution": generations_execution.to_dict(),
        }
    )
    await secondary_progress(
        {
            "type": "image_job_execution",
            "execution": responses_execution.to_dict(),
        }
    )

    persisted_request = dict(generation.upstream_request)
    persisted_executions = sidecar_executions_from_request(persisted_request)
    assert persisted_executions == (
        generations_execution,
        responses_execution,
    )
    assert set(persisted_request[SIDECAR_EXECUTIONS_KEY]) == {
        "generations",
        "responses",
    }
    assert sidecar_execution_from_request(persisted_request) == generations_execution

    captured_requests: list[Any] = []
    image_iter = object()

    class Provider:
        def generate(self, request: Any) -> object:
            captured_requests.append(request)
            return image_iter

    restart_state = SimpleNamespace(
        image_request_options={
            "render_quality": "high",
            "output_format": "png",
            "output_compression": None,
            "background": "auto",
            "moderation": "auto",
            "responses_model": "gpt-image",
        },
        is_dual_race=True,
        reserved_provider=None,
        prompt_for_upstream="draw",
        resolved=SimpleNamespace(size="1024x1024"),
        requested_image_count=1,
        progress_publisher=None,
        user_id="user-1",
        trace_id="trace-1",
        attempt=2,
        task_id="generation-1",
        action="generate",
        services=SimpleNamespace(provider=Provider()),
        gen_upstream_request_snapshot=persisted_request,
        sidecar_execution=generations_execution,
    )
    assert runner_dispatch_phase.build_image_iterator(restart_state) is image_iter
    recovered_context = captured_requests[0].context.sidecar_execution
    assert recovered_context == persisted_executions

    submitted = 0
    polled_job_ids: list[str] = []

    class RecoveryClient:
        async def submit(self, *_args: Any, **_kwargs: Any) -> Any:
            nonlocal submitted
            submitted += 1
            raise AssertionError("recovery must not submit another image job")

        async def poll(
            self,
            handle: ImageJobHandle,
            *,
            trace_id: str,
        ) -> ImageJobStatus:
            assert trace_id == "trace-1"
            polled_job_ids.append(handle.job_id)
            endpoint = handle.job_id.removeprefix("job-")
            return ImageJobStatus(
                payload={
                    "job_id": handle.job_id,
                    "status": "succeeded",
                    "endpoint_used": endpoint,
                    "images": [
                        {
                            "url": (f"https://image-job.example/{endpoint}-result.png"),
                            "format": "png",
                        }
                    ],
                },
                status_code=200,
            )

        async def close(self) -> None:
            return None

    async def reject_fresh_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("recovery must not select a fresh provider")

    async def download_result(**kwargs: Any) -> bytes:
        return str(kwargs["image_url"]).encode("ascii")

    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "build_image_job_client",
        lambda _base_url: RecoveryClient(),
    )
    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_TIMEOUT_S", 10.0)
    monkeypatch.setattr(TEST_SERVICES.core, "IMAGE_JOB_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(
        TEST_SERVICES.core,
        "DUAL_RACE_IMAGE_JOBS_BONUS_GRACE_S",
        1.0,
    )
    monkeypatch.setattr(
        TEST_SERVICES.image_jobs,
        "download_image_job_result",
        download_result,
    )
    monkeypatch.setattr(
        TEST_SERVICES.dispatch,
        "image_dispatch_candidates",
        reject_fresh_dispatch,
    )

    results = [
        item
        async for item in TEST_SERVICES.dispatch.dispatch_image(
            _request(recovered_context)
        )
    ]

    assert len(results) == 2
    assert sorted(polled_job_ids) == ["job-generations", "job-responses"]
    assert submitted == 0


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
        # 当前 execution 已持久化 dispatch，provider 可能已经收到并计费。
        (
            {
                "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
                "upstream_dispatch_attempt": 2,
                "upstream_dispatch_execution_epoch": 3,
            },
            3,
            "settle:unknown",
        ),
        # 响应收据同样只能证明请求到达；没有 durable no-cost 证据就结算。
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
            "settle:unknown",
        ),
        # 更早 attempt 的 response 不改变当前 dispatch 的未知成本。
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
        # provider 明确证明送达后未计费，同样允许释放。
        (
            {
                "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
                "upstream_dispatch_attempt": 2,
                "upstream_dispatch_execution_epoch": 3,
                "upstream_dispatch_delivery": "proven_no_cost",
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
