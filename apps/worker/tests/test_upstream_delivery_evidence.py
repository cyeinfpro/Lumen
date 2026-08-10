from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from lumen_core.upstream_billing import (
    UPSTREAM_DISPATCH_PROVEN_NO_COST,
    UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
    UPSTREAM_RESPONSE_HTTP_ATTEMPTS,
    UPSTREAM_RESPONSE_REQUEST_ID,
    UPSTREAM_RESPONSE_STATUS_CODE,
    UPSTREAM_RESPONSE_TRACE_ID,
)

from app.provider_runtime.errors import UpstreamError
from app.tasks.generation_parts import retry_state, runner_dispatch_phase
from app.upstream_clients.image_job_client import ImageJobClientError
from app.upstream_parts import direct_requests, image_job_failover, image_jobs
from app.upstream_parts.image_execution import ImageExecutionRequest
from app.upstream_parts.upstream_impl import build_image_upstream_runtime


TEST_RUNTIME = build_image_upstream_runtime()
TEST_SERVICES = TEST_RUNTIME.services


def _image_request(**changes: Any) -> ImageExecutionRequest:
    values: dict[str, Any] = {
        "action": "generate",
        "prompt": "test",
        "size": "1024x1024",
        "images": None,
        "mask": None,
        "n": 1,
        "quality": "high",
        "output_format": "png",
        "output_compression": None,
        "background": "auto",
        "moderation": "auto",
        "model": None,
        "progress_callback": None,
        "provider_override": None,
        "user_id": None,
        "upstream_runtime": TEST_RUNTIME,
    }
    values.update(changes)
    return ImageExecutionRequest(**values)


async def _fixed_image_timeout(
    _size: str,
    *,
    runtime: object | None = None,
) -> tuple[httpx.Timeout, float]:
    _ = runtime
    return httpx.Timeout(5.0), 5.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout],
)
async def test_direct_generate_connect_failures_keep_proven_undelivered(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.HTTPError],
) -> None:
    events: list[dict[str, Any]] = []

    async def get_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fail_post(**kwargs: Any) -> httpx.Response:
        await kwargs["before_attempt"](1)
        raise error_type("request was not delivered")

    monkeypatch.setattr(
        TEST_SERVICES.lifecycle,
        "get_images_client",
        get_client,
    )
    monkeypatch.setattr(TEST_SERVICES.core, "post_with_retry", fail_post)
    monkeypatch.setattr(
        direct_requests,
        "_image_request_timeout",
        _fixed_image_timeout,
    )

    with pytest.raises(UpstreamError) as exc_info:
        await TEST_SERVICES.direct.direct_generate_image_once(
            _image_request(progress_callback=events.append),
            base_url_override="https://provider.example/v1",
            api_key_override="sk-test",
        )

    error = exc_info.value
    assert error.error_code == "direct_image_request_failed"
    assert error.status_code == 0
    assert error.payload["receipt_reason"] == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    assert error.payload.get("upstream_result_unknown") is not True
    assert [event["type"] for event in events] == ["dispatch_ready"]


@pytest.mark.asyncio
async def test_direct_edit_connect_failure_keeps_proven_undelivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []

    async def fail_curl(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        await kwargs["on_dispatch_ready"]()
        raise httpx.ConnectError("connection refused before upload")

    monkeypatch.setattr(TEST_SERVICES.transport, "curl_post_multipart", fail_curl)
    monkeypatch.setattr(
        direct_requests,
        "_image_request_timeout",
        _fixed_image_timeout,
    )

    with pytest.raises(UpstreamError) as exc_info:
        await TEST_SERVICES.direct.direct_edit_image_once(
            _image_request(
                action="edit",
                images=[b"\x89PNG\r\n\x1a\n" + b"\x00" * 32],
                progress_callback=events.append,
            ),
            base_url_override="https://provider.example/v1",
            api_key_override="sk-test",
        )

    error = exc_info.value
    assert error.error_code == "direct_image_request_failed"
    assert error.payload["receipt_reason"] == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    assert [event["type"] for event in events] == ["dispatch_ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [6, 7])
async def test_curl_dns_and_connect_exit_codes_are_connect_errors(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    class Process:
        pid = 0

        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"curl could not connect"

    async def create_process(*_args: Any, **_kwargs: Any) -> Process:
        return Process()

    monkeypatch.setattr(
        TEST_SERVICES.infrastructure.asyncio,
        "create_subprocess_exec",
        create_process,
    )

    with pytest.raises(httpx.ConnectError, match=f"rc={returncode}"):
        await TEST_SERVICES.transport.curl_post_multipart_using_paths(
            url="https://provider.example/v1/images/edits",
            data={"prompt": "test"},
            staged_files=[],
            headers={},
            timeout_s=30,
        )


@pytest.mark.parametrize(
    "cause_type",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout],
)
def test_image_job_connect_failures_keep_proven_undelivered(
    cause_type: type[httpx.HTTPError],
) -> None:
    client_error = ImageJobClientError(
        "image job submit failed before delivery",
        operation="submit",
        transient=True,
        result_unknown=True,
    )
    client_error.__cause__ = cause_type("request was not delivered")

    mapped = image_jobs._map_image_job_client_error(
        client_error,
        method="POST",
        url="https://jobs.example/v1/image-jobs",
        runtime=TEST_RUNTIME,
    )

    assert mapped.error_code == "direct_image_request_failed"
    assert mapped.payload["receipt_reason"] == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED
    assert not image_job_failover._upstream_cost_already_incurred(
        mapped,
        runtime=TEST_RUNTIME,
    )


@pytest.mark.parametrize("status_code", [302, 400, 408, 425, 429])
def test_image_job_explicit_submit_rejection_keeps_proven_no_cost(
    status_code: int,
) -> None:
    client_error = ImageJobClientError(
        "image job submit rejected",
        operation="submit",
        status_code=status_code,
        payload={"error": {"message": "sidecar rejected request"}},
        transient=status_code in {408, 425, 429},
        result_unknown=True,
    )

    mapped = image_jobs._map_image_job_client_error(
        client_error,
        method="POST",
        url="https://jobs.example/v1/image-jobs",
        runtime=TEST_RUNTIME,
    )

    assert not image_job_failover.submit_failure_result_unknown(client_error)
    assert mapped.error_code != "image_job_result_unknown"
    assert mapped.status_code == status_code
    assert mapped.payload["receipt_reason"] == UPSTREAM_DISPATCH_PROVEN_NO_COST
    assert mapped.payload.get("upstream_result_unknown") is not True


def _receipt_error(reason: str, *, status_code: int) -> UpstreamError:
    error = UpstreamError(
        "provider attempt failed without upstream cost",
        status_code=status_code,
        error_code="direct_image_request_failed",
        payload={"receipt_reason": reason},
    )
    error.upstream_receipt_reason = reason
    return error


def test_multi_provider_merge_preserves_all_undelivered_evidence() -> None:
    merged = TEST_SERVICES.retry.merge_fallback_errors(
        [
            _receipt_error(
                UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
                status_code=0,
            ),
            _receipt_error(
                UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
                status_code=0,
            ),
        ],
        error_code="all_direct_image_providers_failed",
        message="all providers failed",
        runtime=TEST_RUNTIME,
    )

    assert merged.status_code == 0
    assert merged.payload["receipt_reason"] == UPSTREAM_DISPATCH_PROVEN_UNDELIVERED


def test_multi_provider_merge_promotes_mixed_no_cost_evidence() -> None:
    merged = TEST_SERVICES.retry.merge_fallback_errors(
        [
            _receipt_error(
                UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
                status_code=0,
            ),
            _receipt_error(
                UPSTREAM_DISPATCH_PROVEN_NO_COST,
                status_code=429,
            ),
        ],
        error_code="all_direct_image_providers_failed",
        message="all providers failed",
        runtime=TEST_RUNTIME,
    )

    assert merged.payload["receipt_reason"] == UPSTREAM_DISPATCH_PROVEN_NO_COST


def test_multi_provider_merge_drops_receipt_when_any_attempt_is_unknown() -> None:
    merged = TEST_SERVICES.retry.merge_fallback_errors(
        [
            _receipt_error(
                UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
                status_code=0,
            ),
            UpstreamError(
                "provider may have accepted the request",
                status_code=503,
                error_code="upstream_error",
            ),
        ],
        error_code="all_direct_image_providers_failed",
        message="all providers failed",
        runtime=TEST_RUNTIME,
    )

    assert "receipt_reason" not in merged.payload
    assert not hasattr(merged, "upstream_receipt_reason")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_marker"),
    [
        (
            UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
            (False, True, False),
        ),
        (
            UPSTREAM_DISPATCH_PROVEN_NO_COST,
            (False, False, True),
        ),
    ],
)
async def test_runner_prioritizes_dispatch_evidence_over_positive_status(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected_marker: tuple[bool, bool, bool],
) -> None:
    markers: list[tuple[bool, bool, bool]] = []
    state = SimpleNamespace(
        image_iter=object(),
        gen_upstream_request_snapshot={
            "upstream_dispatch_started_at": "2026-08-04T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 3,
        },
        generation=SimpleNamespace(execution_epoch=3),
        attempt=1,
    )

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
        proven_no_cost: bool = False,
    ) -> None:
        markers.append((response_received, proven_undelivered, proven_no_cost))

    monkeypatch.setattr(
        runner_dispatch_phase,
        "record_generation_upstream_marker",
        record_marker,
    )
    error = UpstreamError(
        "merged provider failures",
        status_code=200,
        error_code="all_direct_image_providers_failed",
        payload={"receipt_reason": reason},
    )
    error.upstream_receipt_reason = reason

    await runner_dispatch_phase._raise_dispatch_failure(  # noqa: SLF001
        state,
        error,
    )

    assert markers == [expected_marker]


@pytest.mark.asyncio
async def test_runner_keeps_mixed_provider_merge_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[tuple[bool, bool, bool]] = []
    state = SimpleNamespace(
        image_iter=object(),
        gen_upstream_request_snapshot={
            "upstream_dispatch_started_at": "2026-08-04T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 3,
        },
        generation=SimpleNamespace(execution_epoch=3),
        attempt=1,
    )

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
        proven_no_cost: bool = False,
    ) -> None:
        markers.append((response_received, proven_undelivered, proven_no_cost))

    monkeypatch.setattr(
        runner_dispatch_phase,
        "record_generation_upstream_marker",
        record_marker,
    )

    await runner_dispatch_phase._raise_dispatch_failure(  # noqa: SLF001
        state,
        UpstreamError(
            "mixed provider outcomes",
            status_code=200,
            error_code="all_direct_image_providers_failed",
        ),
    )

    assert markers == [(True, False, False)]


@pytest.mark.asyncio
async def test_runner_rejects_forged_payload_receipt_for_result_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[tuple[bool, bool, bool]] = []
    state = SimpleNamespace(
        image_iter=object(),
        gen_upstream_request_snapshot={
            "upstream_dispatch_started_at": "2026-08-04T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 3,
        },
        generation=SimpleNamespace(execution_epoch=3),
        attempt=1,
    )

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
        proven_no_cost: bool = False,
    ) -> None:
        markers.append((response_received, proven_undelivered, proven_no_cost))

    monkeypatch.setattr(
        runner_dispatch_phase,
        "record_generation_upstream_marker",
        record_marker,
    )

    await runner_dispatch_phase._raise_dispatch_failure(  # noqa: SLF001
        state,
        UpstreamError(
            "gateway failed after accepting the request",
            status_code=503,
            error_code="direct_image_result_unknown",
            payload={
                "receipt_reason": UPSTREAM_DISPATCH_PROVEN_UNDELIVERED,
                "upstream_result_unknown": True,
            },
        ),
    )

    assert markers == []


class _ResponseMarkerResult:
    def __init__(self, current: object) -> None:
        self.current = current

    def scalar_one_or_none(self) -> object:
        return self.current


class _ResponseMarkerSession:
    def __init__(self, current: object) -> None:
        self.current = current
        self.commits = 0

    async def __aenter__(self) -> _ResponseMarkerSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, _statement: object) -> _ResponseMarkerResult:
        return _ResponseMarkerResult(self.current)

    async def commit(self) -> None:
        self.commits += 1


class _NoopProgressPublisher:
    async def __call__(self, _event: dict[str, Any]) -> None:
        return None

    def pop_provider_used_event(self) -> dict[str, str]:
        return {}


@pytest.mark.asyncio
async def test_response_received_event_persists_allowlisted_diagnostics() -> None:
    current = SimpleNamespace(upstream_request={})
    session = _ResponseMarkerSession(current)
    state = SimpleNamespace(
        services=SimpleNamespace(
            store=SimpleNamespace(session=lambda: session),
            events=object(),
            provider=object(),
        ),
        generation=SimpleNamespace(execution_epoch=4),
        task_id="gen-response-receipt",
        user_id="user-1",
        attempt=2,
        gen_upstream_request_snapshot={},
    )
    publisher = runner_dispatch_phase._EpochGuardedProgressPublisher(  # noqa: SLF001
        state,
        _NoopProgressPublisher(),
    )

    await publisher(
        {
            "type": "response_received",
            UPSTREAM_RESPONSE_STATUS_CODE: 503,
            UPSTREAM_RESPONSE_REQUEST_ID: " req-final\r\n",
            UPSTREAM_RESPONSE_TRACE_ID: "trace-final",
            UPSTREAM_RESPONSE_HTTP_ATTEMPTS: 2,
            "authorization": "Bearer must-not-persist",
            "response_body": "must-not-persist",
        }
    )

    request = current.upstream_request
    assert session.commits == 1
    assert request["upstream_response_received_at"]
    assert request[UPSTREAM_RESPONSE_STATUS_CODE] == 503
    assert request[UPSTREAM_RESPONSE_REQUEST_ID] == "req-final"
    assert request[UPSTREAM_RESPONSE_TRACE_ID] == "trace-final"
    assert request[UPSTREAM_RESPONSE_HTTP_ATTEMPTS] == 2
    assert "authorization" not in request
    assert "response_body" not in request
    assert state.gen_upstream_request_snapshot == request


@pytest.mark.asyncio
async def test_direct_result_unknown_code_reaches_persistent_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def capture_finalizer(
        _state: object,
        *,
        status: str,
        code: str,
        error_message: str,
        allow_cancel_requested: bool,
    ) -> None:
        captured.update(
            {
                "status": status,
                "code": code,
                "error_message": error_message,
                "allow_cancel_requested": allow_cancel_requested,
            }
        )

    monkeypatch.setattr(
        retry_state,
        "_finalize_generation_unknown",
        capture_finalizer,
    )

    await retry_state.finalize_generation_result_unknown(
        object(),
        UpstreamError(
            "final direct result is unknown",
            status_code=503,
            error_code="direct_image_result_unknown",
        ),
    )

    assert captured["status"] == "failed"
    assert captured["code"] == "direct_image_result_unknown"
    assert captured["error_message"] == "final direct result is unknown"
    assert captured["allow_cancel_requested"] is False
