from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    FALLBACK_INPUT_TOKEN_BUDGET,
    IMAGE_INPUT_ESTIMATED_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    MODEL_INPUT_BUDGETS,
    SYSTEM_PROMPT_DUPLICATION_FACTOR,
    SYSTEM_PROMPT_OVERHEAD_TOKENS,
    compose_summary_guardrail,
    get_input_budget,
    estimate_message_tokens,
    estimate_summary_tokens,
    estimate_system_prompt_tokens,
    estimate_text_tokens,
    format_sticky_input_text,
    format_summary_input_text,
    is_summary_usable,
)
from lumen_core.constants import DEFAULT_CHAT_INSTRUCTIONS, Role


def _summary(**overrides):
    data = {
        "version": 2,
        "kind": "rolling_conversation_summary",
        "up_to_message_id": "msg-123",
        "up_to_created_at": "2026-04-26T10:00:00+00:00",
        "first_user_message_id": "msg-001",
        "text": "用户正在实现上下文压缩。",
        "tokens": 42,
    }
    data.update(overrides)
    return data


def test_is_summary_usable_accepts_v2_summary():
    assert is_summary_usable(_summary()) is True


def test_is_summary_usable_rejects_missing_or_legacy_fields():
    assert is_summary_usable(None) is False
    assert is_summary_usable(_summary(version=1)) is False
    assert is_summary_usable(_summary(up_to_message_id="")) is False
    assert is_summary_usable(_summary(first_user_message_id=None)) is False
    assert is_summary_usable(_summary(text="")) is False


def test_estimate_summary_tokens_prefers_stored_token_count():
    assert estimate_summary_tokens(_summary(tokens=42)) == 42


def test_estimate_summary_tokens_falls_back_to_text_estimate():
    assert estimate_summary_tokens(_summary(tokens=None, text="abcd")) == 1


def test_estimate_text_tokens_uses_three_point_five_ascii_ratio():
    assert estimate_text_tokens("abc") == 1
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("abcdefg") == 2
    assert estimate_text_tokens("a" * 35) == 10
    assert estimate_text_tokens("你好") == 2
    assert estimate_text_tokens("hello 世界") == 4


def test_summary_and_sticky_formatters_wrap_content():
    summary = format_summary_input_text("earlier facts")
    assert summary.startswith("[EARLIER_CONTEXT_SUMMARY]\n")
    assert "仅作为历史上下文" in summary
    assert "earlier facts" in summary
    assert summary.endswith("\n[/EARLIER_CONTEXT_SUMMARY]")

    sticky = format_sticky_input_text("original task")
    assert sticky == "[ORIGINAL_TASK]\noriginal task\n[/ORIGINAL_TASK]"


def test_compose_summary_guardrail_mentions_both_blocks():
    guardrail = compose_summary_guardrail()
    assert "[EARLIER_CONTEXT_SUMMARY]" in guardrail
    assert "[ORIGINAL_TASK]" in guardrail
    assert "higher-priority system instructions" in guardrail


def test_estimate_system_prompt_tokens_uses_same_overhead_for_fallback():
    explicit = estimate_system_prompt_tokens("system prompt")
    assert explicit == (
        SYSTEM_PROMPT_OVERHEAD_TOKENS
        + SYSTEM_PROMPT_DUPLICATION_FACTOR * estimate_text_tokens("system prompt")
    )

    fallback = estimate_system_prompt_tokens(None)
    assert fallback == (
        SYSTEM_PROMPT_OVERHEAD_TOKENS
        + SYSTEM_PROMPT_DUPLICATION_FACTOR
        * estimate_text_tokens(DEFAULT_CHAT_INSTRUCTIONS)
    )


def test_estimate_message_tokens_counts_system_role_text(monkeypatch):
    import lumen_core.context_window as cw

    monkeypatch.setattr(cw, "_TIKTOKEN_ENCODING", None)
    monkeypatch.setattr(cw, "_TIKTOKEN_INIT_ATTEMPTED", True)

    assert cw.estimate_message_tokens(
        Role.SYSTEM.value,
        {"text": "system message"},
    ) == MESSAGE_OVERHEAD_TOKENS + cw.estimate_text_tokens("system message")


def test_estimate_message_tokens_ignores_empty_system_role():
    assert estimate_message_tokens(Role.SYSTEM.value, {"text": ""}) == 0


def test_estimate_message_tokens_ignores_non_list_attachments(monkeypatch):
    import lumen_core.context_window as cw

    monkeypatch.setattr(cw, "_TIKTOKEN_ENCODING", None)
    monkeypatch.setattr(cw, "_TIKTOKEN_INIT_ATTEMPTED", True)

    assert cw.estimate_message_tokens(
        Role.USER.value,
        {"text": "", "attachments": ({"image_id": "img-1"},)},
    ) == 0

    assert cw.estimate_message_tokens(
        Role.USER.value,
        {"text": "abc", "attachments": {"image_id": "img-1"}},
    ) == MESSAGE_OVERHEAD_TOKENS + cw.estimate_text_tokens("abc")


def test_estimate_message_tokens_counts_only_list_attachments(monkeypatch):
    import lumen_core.context_window as cw

    monkeypatch.setattr(cw, "_TIKTOKEN_ENCODING", None)
    monkeypatch.setattr(cw, "_TIKTOKEN_INIT_ATTEMPTED", True)

    assert cw.estimate_message_tokens(
        Role.USER.value,
        {"attachments": [{"image_id": "img-1"}, {"image_id": ""}, "bad"]},
    ) == MESSAGE_OVERHEAD_TOKENS + IMAGE_INPUT_ESTIMATED_TOKENS


def test_get_input_budget_uses_tested_model_budget_and_conservative_fallback():
    assert MODEL_INPUT_BUDGETS["gpt-5.4"] == CONTEXT_INPUT_TOKEN_BUDGET
    assert get_input_budget("gpt-5.4") == CONTEXT_INPUT_TOKEN_BUDGET
    assert get_input_budget("unknown-model") == FALLBACK_INPUT_TOKEN_BUDGET
    assert get_input_budget(None) == FALLBACK_INPUT_TOKEN_BUDGET
    assert FALLBACK_INPUT_TOKEN_BUDGET < CONTEXT_INPUT_TOKEN_BUDGET
