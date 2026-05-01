from __future__ import annotations

from app.retry import is_moderation_block, is_retriable


def test_no_image_returned_is_retriable_even_with_http_200() -> None:
    decision = is_retriable("no_image_returned", 200)
    assert decision.retriable is True


def test_tool_choice_downgrade_is_retriable_even_with_http_200() -> None:
    decision = is_retriable("tool_choice_downgrade", 200)
    assert decision.retriable is True


def test_stream_interrupted_without_partial_is_retriable() -> None:
    decision = is_retriable("stream_interrupted", 200, has_partial=False)
    assert decision.retriable is True


def test_stream_interrupted_with_partial_is_terminal() -> None:
    decision = is_retriable("stream_interrupted", 200, has_partial=True)
    assert decision.retriable is False


def test_stream_interrupted_with_text_partial_is_retriable() -> None:
    decision = is_retriable("stream_interrupted", None, has_partial=True)
    assert decision.retriable is True


def test_sse_curl_failed_without_partial_is_retriable() -> None:
    # curl rc=28（超时）/ rc=7（连接失败）等子进程级故障，没开始出图就重试
    decision = is_retriable("sse_curl_failed", 200, has_partial=False)
    assert decision.retriable is True


def test_sse_curl_failed_with_partial_is_terminal() -> None:
    decision = is_retriable("sse_curl_failed", 200, has_partial=True)
    assert decision.retriable is False


def test_safety_errors_remain_terminal_for_task_retry() -> None:
    # provider failover 会切换其它上游；如果所有上游都拒绝，任务层仍不应反复重放。
    assert is_retriable("moderation_blocked", 200).retriable is False
    assert is_retriable("content_policy_violation", 200).retriable is False
    assert is_retriable("safety_violation", 200).retriable is False


def test_wrapped_safety_error_message_is_terminal() -> None:
    decision = is_retriable(
        "fallback_lanes_failed",
        200,
        error_message=(
            "all image lanes failed: moderation_blocked; "
            "safety_violations=[sexual]"
        ),
    )
    assert decision.retriable is False


def test_invalid_size_message_is_terminal() -> None:
    decision = is_retriable("upstream_error", 502, error_message="invalid size: 9999x9999")
    assert decision.retriable is False


def test_disk_full_is_retriable() -> None:
    decision = is_retriable("disk_full", None)
    assert decision.retriable is True


def test_image_job_provider_wrapper_is_retriable() -> None:
    decision = is_retriable("all_direct_image_providers_failed", 200)
    assert decision.retriable is True


def test_is_moderation_block_by_error_code() -> None:
    assert is_moderation_block("moderation_blocked") is True
    assert is_moderation_block("content_policy_violation") is True
    assert is_moderation_block("safety_violation") is True


def test_is_moderation_block_by_message_keywords() -> None:
    assert is_moderation_block(
        "all_providers_failed",
        "request blocked by upstream safety policy",
    ) is True
    assert is_moderation_block(
        None, "moderation_blocked: prompt rejected"
    ) is True
    assert is_moderation_block(
        "fallback_lanes_failed",
        "all image lanes failed: safety_violations=[sexual]",
    ) is True


def test_is_moderation_block_negative() -> None:
    assert is_moderation_block("rate_limit_error", "too many requests") is False
    assert is_moderation_block(None, "connection reset") is False
    assert is_moderation_block(None, None) is False
