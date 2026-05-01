from __future__ import annotations

from lumen_core.context_window import (
    SUMMARY_BLOCK_FOOTER,
    SUMMARY_BLOCK_HEADER,
    STICKY_BLOCK_FOOTER,
    STICKY_BLOCK_HEADER,
    estimate_text_tokens,
    format_sticky_input_text,
    format_summary_input_text,
    is_summary_usable,
)


def test_estimate_text_tokens_handles_mixed_chinese_and_ascii_stably() -> None:
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("你好") == 2
    assert estimate_text_tokens("hello 世界") == 4


def test_is_summary_usable_requires_version_kind_boundary_and_text() -> None:
    valid = {
        "version": 2,
        "kind": "rolling_conversation_summary",
        "up_to_message_id": "msg-10",
        "up_to_created_at": "2026-04-26T10:00:00+00:00",
        "first_user_message_id": "msg-1",
        "text": "User asked for a landing page; assistant drafted it.",
    }

    assert is_summary_usable(valid) is True
    assert is_summary_usable({**valid, "version": 1}) is False
    assert is_summary_usable({**valid, "kind": "legacy"}) is False
    assert is_summary_usable({**valid, "up_to_message_id": ""}) is False
    assert is_summary_usable({**valid, "text": "   "}) is False
    assert is_summary_usable(None) is False


def test_summary_and_sticky_formatters_wrap_with_distinct_markers() -> None:
    summary = format_summary_input_text("compressed facts")
    sticky = format_sticky_input_text("original task")

    assert summary.startswith(f"{SUMMARY_BLOCK_HEADER}\n")
    assert summary.endswith(f"\n{SUMMARY_BLOCK_FOOTER}")
    assert "compressed facts" in summary
    assert "not a new user instruction" not in summary.lower()

    assert sticky.startswith(f"{STICKY_BLOCK_HEADER}\n")
    assert sticky.endswith(f"\n{STICKY_BLOCK_FOOTER}")
    assert "original task" in sticky
