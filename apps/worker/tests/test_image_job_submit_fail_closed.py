from __future__ import annotations

import httpx
import pytest

from app.provider_runtime.contracts import ImageJobEndpoint
from app.upstream_clients.image_job_client import ImageJobClient, ImageJobClientError
from app.upstream_parts import image_job_failover, image_jobs
from app.upstream_parts.upstream_impl import build_image_upstream_runtime


TEST_RUNTIME = build_image_upstream_runtime()
TEST_SERVICES = TEST_RUNTIME.services


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body_kind"),
    [
        (502, "non_json"),
        (503, "empty"),
        (500, "json"),
    ],
)
async def test_submit_5xx_is_result_unknown_and_blocks_failover(
    status_code: int,
    body_kind: str,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if body_kind == "json":
            return httpx.Response(
                status_code,
                json={"error": {"message": "sidecar failed after dispatch"}},
            )
        if body_kind == "empty":
            return httpx.Response(status_code, content=b"")
        return httpx.Response(
            status_code,
            headers={"content-type": "text/html"},
            content=b"<html>bad gateway</html>",
        )

    client = ImageJobClient(
        ImageJobEndpoint(
            base_url="https://jobs.test",
            service_token="service-token",
        ),
        timeout=_timeout(),
        transport=httpx.MockTransport(handler),
        post_with_retry=TEST_SERVICES.core.post_with_retry,
    )
    try:
        with pytest.raises(ImageJobClientError) as raw_error:
            await client.submit(
                {"request_type": "responses"},
                upstream_api_key="provider-key",
                trace_id="trace-submit",
            )
    finally:
        await client.close()

    client_error = raw_error.value
    assert client_error.status_code == status_code
    assert client_error.transient is True
    assert client_error.result_unknown is True
    if body_kind == "json":
        assert client_error.payload == {
            "error": {"message": "sidecar failed after dispatch"}
        }
    else:
        assert client_error.payload == {}

    mapped = image_jobs._map_image_job_client_error(
        client_error,
        method="POST",
        url="https://jobs.test/v1/image-jobs",
        runtime=TEST_RUNTIME,
    )

    assert len(requests) == 1
    assert (
        mapped.error_code
        == TEST_SERVICES.infrastructure.EC.IMAGE_JOB_RESULT_UNKNOWN.value
    )
    assert mapped.status_code == status_code
    assert mapped.payload["upstream_result_unknown"] is True
    assert image_job_failover._upstream_cost_already_incurred(
        mapped,
        runtime=TEST_RUNTIME,
    )
    assert not image_job_failover._should_continue_image_job_failover(
        mapped,
        retriable=True,
        runtime=TEST_RUNTIME,
    )


def test_submit_5xx_without_client_marker_still_fails_closed() -> None:
    error = ImageJobClientError(
        "legacy image job submit returned invalid JSON",
        operation="submit",
        status_code=502,
    )

    assert image_job_failover.submit_failure_result_unknown(error)
    assert image_job_failover._upstream_cost_already_incurred(
        error,
        runtime=TEST_RUNTIME,
    )
