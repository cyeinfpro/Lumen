from __future__ import annotations

from app.provider_runtime.errors import UpstreamError
from app.provider_runtime.upstream_services import upstream_services


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

    merged = upstream_services().retry.merge_fallback_errors(
        [wrapped],
        error_code="fallback_lanes_failed",
        message="both lanes failed",
    )

    assert merged.error_code == "moderation_blocked"
    assert merged.status_code == 200
