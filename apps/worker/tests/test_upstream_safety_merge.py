from __future__ import annotations

from app.provider_runtime.errors import UpstreamError
from app.upstream_parts.upstream_impl import build_image_upstream_runtime


TEST_UPSTREAM_RUNTIME = build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services


def test_fallback_merge_promotes_wrapped_safety_block_to_terminal_code() -> None:
    wrapped = UpstreamError(
        "all 1 image job providers failed",
        status_code=200,
        error_code="all_direct_image_providers_failed",
        payload={
            "upstream_body": (
                "event: response.failed\n"
                'data: {"error":{"code":"moderation_blocked",'
                '"message":"safety_violations=[sexual]"}}'
            )
        },
    )

    merged = TEST_UPSTREAM_SERVICES.retry.merge_fallback_errors(
        [wrapped],
        error_code="fallback_lanes_failed",
        message="both lanes failed",
    )

    assert merged.error_code == "moderation_blocked"
    assert merged.status_code == 200
