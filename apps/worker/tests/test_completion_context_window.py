from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import pytest

os.environ.setdefault(
    "STORAGE_ROOT", str(Path(tempfile.gettempdir()) / "lumen-worker-test-storage")
)

from app.tasks import completion
from app.tasks.completion_parts import tool_images
from lumen_core.constants import Role
from lumen_core.context_window import SUMMARY_KIND, SUMMARY_VERSION
from lumen_core.models import Conversation, Message
from lumen_core.pricing import UsageTokens


class _Result:
    def __init__(self, rows: list[Message]) -> None:
        self._rows = rows

    def scalars(self) -> list[Message]:
        return self._rows


class _HistorySession:
    def __init__(self, *, target: Message, batches: list[list[Message]]) -> None:
        self.target = target
        self.batches = batches

    async def get(self, _model: Any, _object_id: str) -> Message:
        return self.target

    async def execute(self, _statement: Any) -> _Result:
        if self.batches:
            return _Result(self.batches.pop(0))
        return _Result([])


class _CompressionSession:
    def __init__(
        self,
        *,
        conversation: Conversation,
        messages: list[Message],
        batches: list[list[Message]],
    ) -> None:
        self.conversation = conversation
        self.messages = {m.id: m for m in messages}
        self.batches = batches

    async def get(self, model: Any, object_id: str) -> Any:
        if model is Conversation:
            return self.conversation if object_id == self.conversation.id else None
        if model is Message:
            return self.messages.get(object_id)
        return None

    async def execute(self, _statement: Any) -> _Result:
        if self.batches:
            return _Result(self.batches.pop(0))
        return _Result([])


def _message(index: int, text: str, role: str = Role.USER.value) -> Message:
    return Message(
        id=f"msg-{index:03d}",
        conversation_id="conv-1",
        role=role,
        content={"text": text, "attachments": []},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index),
        deleted_at=None,
    )


def _conversation(summary_jsonb: dict[str, Any] | None = None) -> Conversation:
    return Conversation(
        id="conv-1",
        user_id="user-1",
        title="",
        summary_jsonb=summary_jsonb,
    )


def _texts(input_list: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for item in input_list:
        for part in item.get("content") or []:
            text = part.get("text")
            if isinstance(text, str):
                out.append(text)
    return out


def _summary(
    *,
    up_to: Message,
    first_user: Message,
    text: str = "compressed old facts",
) -> dict[str, Any]:
    return {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": up_to.id,
        "up_to_created_at": up_to.created_at.isoformat(),
        "first_user_message_id": first_user.id,
        "text": text,
        "tokens": 10,
        "source_message_count": 2,
        "source_token_estimate": 400,
        "model": "summary-model",
        "image_caption_count": 0,
        "compressed_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
    }


def test_completion_summary_boundary_uses_message_id_when_timestamps_equal() -> None:
    first = _message(1, "first")
    boundary = _message(2, "boundary")
    boundary.id = "msg-b"
    summary = _summary(up_to=boundary, first_user=first)
    summary["up_to_message_id"] = "msg-a"

    assert completion._summary_covers_boundary(summary, boundary) is False

    summary["up_to_message_id"] = "msg-c"
    assert completion._summary_covers_boundary(summary, boundary) is True


@pytest.fixture
def enable_compression(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve_int(key: str, default: int) -> int:
        values = {
            "context.compression_enabled": 1,
            "context.compression_trigger_percent": 1,
            "context.summary_target_tokens": 20,
            "context.summary_min_recent_messages": 2,
            "context.summary_min_interval_seconds": 30,
        }
        return values.get(key, default)

    async def fake_resolve(key: str) -> str | None:
        if key == "context.summary_model":
            return "summary-model"
        return None

    monkeypatch.setattr(completion.runtime_settings, "resolve_int", fake_resolve_int)
    monkeypatch.setattr(completion.runtime_settings, "resolve", fake_resolve)


@pytest.mark.asyncio
async def test_context_window_includes_more_than_legacy_20_messages() -> None:
    messages = [_message(i, f"message {i}") for i in range(1, 26)]
    session = _HistorySession(target=messages[-1], batches=[list(reversed(messages))])

    input_list = await completion._build_input_from_history(
        session,
        conversation_id="conv-1",
        up_to_message_id=messages[-1].id,
        system_prompt=None,
    )

    assert len(input_list) == 25
    assert _texts(input_list)[0] == "message 1"
    assert _texts(input_list)[-1] == "message 25"


@pytest.mark.asyncio
async def test_context_window_truncates_by_token_budget_from_oldest_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 这条覆盖压缩关闭时的 legacy 硬截断路径；P0-1 把默认开关翻成 1 后需显式关掉，
    # 避免落入 compression 分支（_HistorySession 没准备 Conversation 桩）。
    async def _disable_compression(key: str, default: int) -> int:
        if key == "context.compression_enabled":
            return 0
        return default

    monkeypatch.setattr(
        completion.runtime_settings, "resolve_int", _disable_compression
    )
    # Keep token counting deterministic across tiktoken cold/warm CI runs.
    monkeypatch.setattr(completion, "count_tokens", lambda text: len(text or "") // 2)
    monkeypatch.setattr(completion, "estimate_system_prompt_tokens", lambda _prompt: 0)
    # High enough for the two newest messages while still forcing older history
    # to be truncated.
    monkeypatch.setattr(completion, "CONTEXT_INPUT_TOKEN_BUDGET", 250)
    messages = [_message(i, f"message {i} " + ("x" * 200)) for i in range(1, 6)]
    session = _HistorySession(target=messages[-1], batches=[list(reversed(messages))])

    input_list = await completion._build_input_from_history(
        session,
        conversation_id="conv-1",
        up_to_message_id=messages[-1].id,
        system_prompt=None,
    )

    texts = _texts(input_list)
    assert len(texts) == 2
    assert texts[0].startswith("message 4 ")
    assert texts[1].startswith("message 5 ")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_compression")
async def test_compression_injects_sticky_and_existing_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion, "CONTEXT_INPUT_TOKEN_BUDGET", 220)
    first = _message(1, "original task")
    old_a = _message(2, "old answer " + ("a" * 300), Role.ASSISTANT.value)
    old_u = _message(3, "old detail " + ("b" * 300))
    recent_a = _message(4, "recent answer", Role.ASSISTANT.value)
    current = _message(5, "current question")
    target = _message(6, "", Role.ASSISTANT.value)
    target.parent_message_id = current.id
    messages = [first, old_a, old_u, recent_a, current, target]
    conv = _conversation(_summary(up_to=old_u, first_user=first))
    session = _CompressionSession(
        conversation=conv,
        messages=messages,
        batches=[list(reversed(messages))],
    )

    input_list = await completion._build_input_from_history(
        session,
        conversation_id="conv-1",
        up_to_message_id=target.id,
        system_prompt="system prompt",
    )

    texts = _texts(input_list)
    joined = "\n".join(texts)
    assert "[ORIGINAL_TASK]" in joined
    assert "original task" in joined
    assert "[EARLIER_CONTEXT_SUMMARY]" in joined
    assert "compressed old facts" in joined
    assert "current question" in joined
    assert "system prompt" not in joined
    instructions = completion._instructions_with_summary_guardrail(
        "system prompt",
        enabled=True,
    )
    assert completion.compose_summary_guardrail() in instructions
    assert "old detail " not in joined


@pytest.mark.asyncio
async def test_custom_system_prompt_is_sent_once_and_estimated_once() -> None:
    prompt = "custom system prompt"
    summary_text = "compressed facts"
    packed = completion.PackedContext(
        input_list=[],
        estimated_tokens=0,
        summary_used=True,
        summary_created=False,
        summary_up_to_message_id="msg-001",
        sticky_used=False,
        included_messages_count=0,
        truncated_without_summary=False,
        fallback_reason=None,
        summary_tokens=completion.count_tokens(summary_text),
        _system_prompt=prompt,
        _summary_text=summary_text,
    )

    input_list = await completion._build_input_from_packed_context(object(), packed)
    instructions = completion._instructions_with_summary_guardrail(prompt, enabled=True)
    body = {"input": input_list, "instructions": instructions}

    assert str(body).count(prompt) == 1
    assert all(item.get("role") != "system" for item in input_list)
    assert completion._estimate_system_prompt_tokens_once(instructions) == (
        completion.estimate_system_prompt_tokens(instructions)
        - completion.estimate_text_tokens(instructions)
    )


def test_fallback_usage_estimate_counts_top_level_instructions_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_images, "count_tokens", len)
    input_list = [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "short request"}],
        }
    ]
    instructions = "Follow this billing-sensitive instruction. " * 200

    input_only_tokens, _ = completion._fallback_completion_usage_tokens(
        input_list,
        "",
        tokens_in=0,
        tokens_out=0,
    )
    request_tokens, _ = completion._fallback_completion_usage_tokens(
        input_list,
        "",
        instructions=instructions,
        tokens_in=0,
        tokens_out=0,
    )

    legacy_payload = json.dumps(input_list, ensure_ascii=False)
    request_payload = json.dumps(
        {
            "input": input_list,
            "instructions": instructions,
        },
        ensure_ascii=False,
    )
    assert input_only_tokens == len(legacy_payload)
    assert request_tokens == len(request_payload)
    assert request_tokens - input_only_tokens == len(request_payload) - len(
        legacy_payload
    )


def _finish_usage_round(
    accumulator: tool_images._CompletionUsageAccumulator,
    *,
    input_floor: int,
    raw_usage: dict[str, Any] | None,
    usage: UsageTokens | None = None,
    output_text: str = "",
    reasoning_text: str = "",
    tool_tokens_before: int = 0,
    tool_tokens_after: int = 0,
) -> None:
    accumulator.start_round(
        input_fallback_tokens=input_floor,
        tool_output_tokens=tool_tokens_before,
    )
    accumulator.record_usage(
        usage or UsageTokens(0, 0),
        raw_usage=raw_usage,
    )
    accumulator.finish_round(
        output_text=output_text,
        reasoning_text=reasoning_text,
        tool_output_tokens=tool_tokens_after,
    )


def _tool_limit_input_floors() -> tuple[int, int]:
    body = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "research this"}],
            }
        ],
        "instructions": "Use tools only when needed.",
        "tools": [{"type": "web_search"}],
    }
    fallback_body = completion._tool_limited_completion_body(body)
    return (
        tool_images._estimate_completion_request_input_tokens(
            body["input"],
            instructions=body["instructions"],
        ),
        tool_images._estimate_completion_request_input_tokens(
            fallback_body["input"],
            instructions=fallback_body["instructions"],
        ),
    )


def test_tool_limit_usage_fills_missing_first_round_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_images, "count_tokens", len)
    first_floor, second_floor = _tool_limit_input_floors()
    usage = tool_images._CompletionUsageAccumulator()

    _finish_usage_round(
        usage,
        input_floor=first_floor,
        raw_usage={},
        output_text="first",
        reasoning_text="think",
        tool_tokens_before=2,
        tool_tokens_after=7,
    )
    _finish_usage_round(
        usage,
        input_floor=second_floor,
        raw_usage={
            "input_tokens": 37,
            "output_tokens": 11,
            "output_tokens_details": {"reasoning_tokens": 3},
        },
        usage=UsageTokens(
            input_tokens=37,
            output_tokens=11,
            cache_read_tokens=5,
            reasoning_tokens=3,
        ),
        output_text="reported-second",
        reasoning_text="reported-reasoning",
        tool_tokens_before=7,
        tool_tokens_after=20,
    )

    assert usage.tokens_in == first_floor + 37
    assert usage.tokens_out == len("first") + len("think") + 5 + 11
    assert usage.cache_read_tokens == 5
    assert usage.reasoning_tokens == len("think") + 3


def test_tool_limit_usage_fills_missing_second_round_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_images, "count_tokens", len)
    first_floor, second_floor = _tool_limit_input_floors()
    usage = tool_images._CompletionUsageAccumulator()

    _finish_usage_round(
        usage,
        input_floor=first_floor,
        raw_usage={
            "input_tokens": 29,
            "output_tokens": 7,
            "reasoning_tokens": 2,
        },
        usage=UsageTokens(
            input_tokens=29,
            output_tokens=7,
            reasoning_tokens=2,
        ),
        output_text="reported-first",
        reasoning_text="reported-thinking",
    )
    _finish_usage_round(
        usage,
        input_floor=second_floor,
        raw_usage={},
        output_text="second",
    )

    assert usage.tokens_in == 29 + second_floor
    assert usage.tokens_out == 7 + len("second")
    assert usage.reasoning_tokens == 2


def test_tool_limit_usage_fills_both_rounds_without_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_images, "count_tokens", len)
    first_floor, second_floor = _tool_limit_input_floors()
    usage = tool_images._CompletionUsageAccumulator()

    _finish_usage_round(
        usage,
        input_floor=first_floor,
        raw_usage={},
        output_text="first",
    )
    _finish_usage_round(
        usage,
        input_floor=second_floor,
        raw_usage=None,
        output_text="second",
    )

    assert usage.tokens_in == first_floor + second_floor
    assert usage.tokens_out == len("first") + len("second")
    assert second_floor > first_floor


@pytest.mark.parametrize(
    (
        "first_raw",
        "first_usage",
        "second_raw",
        "second_usage",
        "expected_input",
        "expected_output",
    ),
    [
        (
            {"input_tokens": 4},
            UsageTokens(4, 0),
            {"output_tokens": 6},
            UsageTokens(0, 6),
            4 + 202,
            len("first") + 6,
        ),
        (
            {"output_tokens": 6},
            UsageTokens(0, 6),
            {"input_tokens": 4},
            UsageTokens(4, 0),
            101 + 4,
            6 + len("second"),
        ),
    ],
)
def test_partial_usage_fields_fallback_per_round_in_both_orders(
    monkeypatch: pytest.MonkeyPatch,
    first_raw: dict[str, Any],
    first_usage: UsageTokens,
    second_raw: dict[str, Any],
    second_usage: UsageTokens,
    expected_input: int,
    expected_output: int,
) -> None:
    monkeypatch.setattr(tool_images, "count_tokens", len)
    usage = tool_images._CompletionUsageAccumulator()
    _finish_usage_round(
        usage,
        input_floor=101,
        raw_usage=first_raw,
        usage=first_usage,
        output_text="first",
    )
    _finish_usage_round(
        usage,
        input_floor=202,
        raw_usage=second_raw,
        usage=second_usage,
        output_text="second",
    )

    assert usage.tokens_in == expected_input
    assert usage.tokens_out == expected_output


def test_explicit_zero_usage_fields_do_not_trigger_same_dimension_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_images, "count_tokens", len)
    usage = tool_images._CompletionUsageAccumulator()

    _finish_usage_round(
        usage,
        input_floor=101,
        raw_usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        output_text="visible",
        reasoning_text="thought",
    )

    assert usage.tokens_in == 0
    assert usage.tokens_out == 0
    assert usage.reasoning_tokens == 0


@pytest.mark.asyncio
async def test_manual_summary_is_used_even_below_auto_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve_int(key: str, default: int) -> int:
        values = {
            "context.compression_enabled": 1,
            "context.compression_trigger_percent": 98,
            "context.summary_target_tokens": 20,
            "context.summary_min_recent_messages": 16,
            "context.summary_min_interval_seconds": 30,
        }
        return values.get(key, default)

    async def fake_resolve(key: str) -> str | None:
        if key == "context.summary_model":
            return "summary-model"
        return None

    monkeypatch.setattr(completion.runtime_settings, "resolve_int", fake_resolve_int)
    monkeypatch.setattr(completion.runtime_settings, "resolve", fake_resolve)
    monkeypatch.setattr(completion, "CONTEXT_INPUT_TOKEN_BUDGET", 20_000)
    first = _message(1, "original task")
    old_a = _message(2, "old answer " + ("a" * 600), Role.ASSISTANT.value)
    old_u = _message(3, "old detail " + ("b" * 600))
    recent = _message(4, "recent question")
    target = _message(5, "", Role.ASSISTANT.value)
    target.parent_message_id = recent.id
    messages = [first, old_a, old_u, recent, target]
    conv = _conversation(_summary(up_to=old_u, first_user=first))
    session = _CompressionSession(
        conversation=conv,
        messages=messages,
        batches=[list(reversed(messages))],
    )

    packed = await completion._pack_recent_history(
        session,
        conversation_id="conv-1",
        up_to_message_id=target.id,
        system_prompt=None,
    )

    assert packed.summary_used is True
    assert packed.summary_up_to_message_id == old_u.id
    texts = _texts(await completion._build_input_from_packed_context(session, packed))
    joined = "\n".join(texts)
    assert "compressed old facts" in joined
    assert "recent question" in joined
    assert "old detail " not in joined


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_compression")
async def test_compression_calls_summary_service_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion, "CONTEXT_INPUT_TOKEN_BUDGET", 220)
    first = _message(1, "original task")
    old_a = _message(2, "old answer " + ("a" * 300), Role.ASSISTANT.value)
    old_u = _message(3, "old detail " + ("b" * 300))
    recent_a = _message(4, "recent answer", Role.ASSISTANT.value)
    current = _message(5, "current question")
    target = _message(6, "", Role.ASSISTANT.value)
    target.parent_message_id = current.id
    messages = [first, old_a, old_u, recent_a, current, target]
    conv = _conversation(None)
    session = _CompressionSession(
        conversation=conv,
        messages=messages,
        batches=[list(reversed(messages))],
    )
    calls: list[Any] = []

    class _SummaryService:
        @staticmethod
        async def ensure_context_summary(
            _session: Any,
            _conv: Conversation,
            boundary: Any,
            _settings: dict[str, Any],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls.append(boundary)
            assert first.id not in boundary.summary_message_ids
            return _summary(
                up_to=session.messages[boundary.up_to_message_id],
                first_user=first,
                text="newly compressed facts",
            )

    monkeypatch.setattr(completion, "context_summary", _SummaryService)

    input_list = await completion._build_input_from_history(
        session,
        conversation_id="conv-1",
        up_to_message_id=target.id,
        system_prompt=None,
    )

    assert len(calls) == 1
    assert "newly compressed facts" in "\n".join(_texts(input_list))


@pytest.mark.asyncio
async def test_summary_service_keeps_commit_ownership_after_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _message(1, "original task")
    boundary_message = _message(2, "old context")
    conversation = _conversation(None)
    boundary = completion._SummaryBoundary(
        conversation_id=conversation.id,
        up_to_message_id=boundary_message.id,
        up_to_created_at=boundary_message.created_at,
        first_user_message_id=first.id,
        recent_message_ids=[],
        summary_message_ids=[boundary_message.id],
        source_message_count=1,
        source_token_estimate=10,
    )
    events: list[str] = []

    class Session:
        async def refresh(self, row: Conversation) -> None:
            events.append("refresh")
            assert row is conversation

        async def commit(self) -> None:
            raise AssertionError(
                "completion context loader must not own summary commit"
            )

    class SummaryService:
        @staticmethod
        async def ensure_context_summary(
            _session: Any,
            conv: Conversation,
            _boundary: Any,
            _settings: dict[str, Any],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            events.append("service")
            conv.summary_jsonb = _summary(
                up_to=boundary_message,
                first_user=first,
                text="service-owned summary",
            )
            return {"summary_created": True}

    monkeypatch.setattr(completion, "context_summary", SummaryService)

    summary = await completion._ensure_context_summary(
        Session(),
        conversation,
        boundary,
        target_tokens=100,
        model="summary-model",
        redis=None,
    )

    assert summary is conversation.summary_jsonb
    assert summary["text"] == "service-owned summary"
    assert events == ["service", "refresh"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_compression")
async def test_compression_failure_fallback_still_keeps_current_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion, "CONTEXT_INPUT_TOKEN_BUDGET", 80)
    first = _message(1, "original task")
    old_a = _message(2, "older context " + ("a" * 200), Role.ASSISTANT.value)
    old_b = _message(3, "old context " + ("b" * 200), Role.ASSISTANT.value)
    current = _message(4, "current question " + ("x" * 600))
    target = _message(5, "", Role.ASSISTANT.value)
    target.parent_message_id = current.id
    messages = [first, old_a, old_b, current, target]
    session = _CompressionSession(
        conversation=_conversation(None),
        messages=messages,
        batches=[list(reversed(messages)), list(reversed(messages))],
    )
    monkeypatch.setattr(completion, "context_summary", None)

    packed = await completion._pack_recent_history(
        session,
        conversation_id="conv-1",
        up_to_message_id=target.id,
        system_prompt=None,
    )

    assert packed.fallback_reason == "summary_failed"
    assert packed.truncated_without_summary is True
    assert "current question " in "\n".join(_texts(packed.input_list))
