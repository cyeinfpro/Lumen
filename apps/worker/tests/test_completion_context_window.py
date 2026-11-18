from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

import pytest

os.environ.setdefault(
    "STORAGE_ROOT", str(Path(tempfile.gettempdir()) / "lumen-worker-test-storage")
)

from app.provider_runtime.upstream_services import ImageUpstreamRuntime
from app.tasks.completion_parts import context_loading
from app.tasks.completion_parts import default_runtime as completion
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

    legacy_payload = json.dumps(
        input_list,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    request_payload = json.dumps(
        {
            "input": input_list,
            "instructions": instructions,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
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


@pytest.mark.asyncio
async def test_context_loading_batches_sql_commits_before_parallel_io_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    execute_calls = 0
    tx_open = True
    tx_closed_before_reads: list[bool] = []
    read_threads: set[int] = set()
    main_thread_id = threading.get_ident()
    active_reads = 0
    max_active_reads = 0
    read_keys: list[str] = []

    rows = [
        (
            "img-1",
            "orig/1.png",
            "image/png",
            hashlib.sha256(b"preview-1").hexdigest(),
            "variants/1-preview.webp",
        ),
        (
            "img-2",
            "orig/2.png",
            "image/png",
            hashlib.sha256(b"orig-2").hexdigest(),
            None,
        ),
        (
            "img-3",
            "orig/3.jpg",
            "image/jpeg",
            hashlib.sha256(b"preview-3").hexdigest(),
            "variants/3-preview.webp",
        ),
        (
            "img-4",
            "orig/4.png",
            "image/png",
            hashlib.sha256(b"preview-4").hexdigest(),
            "variants/4-preview.webp",
        ),
        (
            "img-5",
            "orig/5.png",
            "image/png",
            hashlib.sha256(b"preview-5").hexdigest(),
            "variants/5-preview.webp",
        ),
    ]

    class _BatchResult:
        def all(self) -> list[tuple[Any, ...]]:
            return list(rows)

    class _BatchSession:
        async def get(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("per-image session.get must not run in batch mode")

        async def execute(self, _stmt: Any) -> _BatchResult:
            nonlocal execute_calls, tx_open
            execute_calls += 1
            tx_open = True
            events.append("sql")
            return _BatchResult()

        async def commit(self) -> None:
            nonlocal tx_open
            tx_open = False
            events.append("commit")

    payloads = {
        "variants/1-preview.webp": b"webp-bytes-1",
        "orig/2.png": b"png-bytes-2",
        "variants/3-preview.webp": b"webp-bytes-3",
        "variants/4-preview.webp": b"webp-bytes-4",
        "variants/5-preview.webp": b"webp-bytes-5",
    }

    def fake_get_bytes(key: str) -> bytes:
        nonlocal active_reads, max_active_reads
        tx_closed_before_reads.append(not tx_open)
        read_threads.add(threading.get_ident())
        read_keys.append(key)
        active_reads += 1
        max_active_reads = max(max_active_reads, active_reads)
        try:
            time.sleep(0.02)
            return payloads[key]
        finally:
            active_reads -= 1

    monkeypatch.setattr(completion.storage, "get_bytes", fake_get_bytes)

    msg1 = _message(1, "first turn")
    msg1.content = {
        "text": "first turn",
        "attachments": [
            {"image_id": "img-1"},
            {"image_id": "img-deleted"},
            {"image_id": "img-2"},
        ],
    }
    msg2 = _message(2, "assistant reply", Role.ASSISTANT.value)
    msg3 = _message(3, "second turn")
    msg3.content = {
        "text": "second turn",
        "attachments": [
            {"image_id": "img-1"},
            {"image_id": "img-3"},
            {"image_id": "img-4"},
            {"image_id": "img-5"},
        ],
    }

    packed = completion.PackedContext(
        input_list=[],
        estimated_tokens=100,
        summary_used=False,
        summary_created=False,
        summary_up_to_message_id=None,
        sticky_used=False,
        included_messages_count=3,
        truncated_without_summary=False,
        fallback_reason=None,
        _recent_rows=(msg1, msg2, msg3),
    )

    input_list = await completion._build_input_from_packed_context(
        _BatchSession(),
        packed,
    )

    assert execute_calls == 1
    assert events == ["sql", "commit"]
    assert tx_closed_before_reads and all(tx_closed_before_reads)
    assert len(read_keys) == 5
    assert 1 < max_active_reads <= 4
    assert read_threads and main_thread_id not in read_threads

    first_images = [
        part["image_url"]
        for part in input_list[0]["content"]
        if part["type"] == "input_image"
    ]
    second_images = [
        part["image_url"]
        for part in input_list[2]["content"]
        if part["type"] == "input_image"
    ]
    assert first_images == [
        f"data:image/webp;base64,{base64.b64encode(b'webp-bytes-1').decode('ascii')}",
        f"data:image/png;base64,{base64.b64encode(b'png-bytes-2').decode('ascii')}",
    ]
    assert second_images == [
        f"data:image/webp;base64,{base64.b64encode(b'webp-bytes-1').decode('ascii')}",
        f"data:image/webp;base64,{base64.b64encode(b'webp-bytes-3').decode('ascii')}",
        f"data:image/webp;base64,{base64.b64encode(b'webp-bytes-4').decode('ascii')}",
        f"data:image/webp;base64,{base64.b64encode(b'webp-bytes-5').decode('ascii')}",
    ]


@pytest.mark.asyncio
async def test_completion_runtime_attachment_lru_cache_zero_disk_reread_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_upstream = ImageUpstreamRuntime(services=object())  # type: ignore[arg-type]
    runtime_a = completion.build_completion_runtime(
        image_upstream_runtime=fake_upstream
    )
    runtime_b = completion.build_completion_runtime(
        image_upstream_runtime=fake_upstream
    )
    cache_a = getattr(runtime_a.runner, "attachment_cache", None)
    cache_b = getattr(runtime_b.runner, "attachment_cache", None)
    assert isinstance(cache_a, context_loading.AttachmentDataUrlCache)
    assert isinstance(cache_b, context_loading.AttachmentDataUrlCache)
    assert cache_a is not cache_b
    assert cache_a.max_bytes == 32 * 1024 * 1024
    assert cache_a.ttl_seconds == 300.0

    sha_1 = hashlib.sha256(b"cached-image-1").hexdigest()
    sha_2 = hashlib.sha256(b"cached-image-2").hexdigest()
    disk_reads: list[str] = []

    class _TurnSession:
        def __init__(
            self,
            *,
            target: Message,
            messages: list[Message],
            image_rows: list[tuple[Any, ...]],
        ) -> None:
            self._target = target
            self._batches = [list(reversed(messages))]
            self._image_rows = image_rows
            self.commits = 0

        async def get(self, model: Any, object_id: str) -> Any:
            if model is Message and object_id == self._target.id:
                return self._target
            return None

        async def execute(self, stmt: Any) -> Any:
            sql_text = str(stmt)
            if "images" in sql_text:
                rows = list(self._image_rows)

                class _ImgResult:
                    def all(self) -> list[tuple[Any, ...]]:
                        return rows

                return _ImgResult()
            batch = self._batches.pop(0) if self._batches else []
            return _Result(batch)

        async def commit(self) -> None:
            self.commits += 1

    async def _disable_compression(key: str, default: int) -> int:
        if key == "context.compression_enabled":
            return 0
        return default

    monkeypatch.setattr(
        completion.runtime_settings, "resolve_int", _disable_compression
    )
    monkeypatch.setattr(
        completion.storage,
        "get_bytes",
        lambda key: (disk_reads.append(key), f"bytes:{key}".encode("ascii"))[1],
    )

    u1 = _message(1, "look at image 1")
    u1.content = {"text": "look at image 1", "attachments": [{"image_id": "img-c1"}]}
    t1 = _message(2, "", Role.ASSISTANT.value)
    t1.parent_message_id = u1.id

    pack_fn = runtime_a.services.context_builder.adapter.context._pack_recent_history  # type: ignore[attr-defined]
    packed_turn_1 = await pack_fn(
        _TurnSession(
            target=t1,
            messages=[u1, t1],
            image_rows=[("img-c1", "orig/c1.png", "image/png", sha_1, "prev/c1.webp")],
        ),
        conversation_id="conv-1",
        up_to_message_id=t1.id,
        system_prompt=None,
    )
    assert disk_reads == ["prev/c1.webp"]
    assert len(packed_turn_1.input_list) == 1

    a1 = _message(2, "saw image 1", Role.ASSISTANT.value)
    u2 = _message(3, "compare with image 2")
    u2.content = {
        "text": "compare with image 2",
        "attachments": [{"image_id": "img-c2"}],
    }
    t2 = _message(4, "", Role.ASSISTANT.value)
    t2.parent_message_id = u2.id

    packed_turn_2 = await pack_fn(
        _TurnSession(
            target=t2,
            messages=[u1, a1, u2, t2],
            image_rows=[
                ("img-c1", "orig/c1.png", "image/png", sha_1, "prev/c1.webp"),
                ("img-c2", "orig/c2.png", "image/png", sha_2, "prev/c2.webp"),
            ],
        ),
        conversation_id="conv-1",
        up_to_message_id=t2.id,
        system_prompt=None,
    )
    # Turn 2 hits SHA-256 LRU cache for img-c1 -> zero repeat disk read!
    assert disk_reads == ["prev/c1.webp", "prev/c2.webp"]
    assert len(packed_turn_2.input_list) == 3

    # Direct verification of byte-cap LRU eviction and TTL expiration.
    now_clock = 100.0
    bounded = context_loading.AttachmentDataUrlCache(
        max_bytes=60,
        ttl_seconds=300.0,
        clock=lambda: now_clock,
    )
    bounded.put("sha-a", "a" * 30, mime="image/webp", is_preview=True)
    bounded.put("sha-b", "b" * 30, mime="image/webp", is_preview=True)
    assert bounded.get("sha-a", mime="image/webp", is_preview=True) == "a" * 30
    bounded.put("sha-c", "c" * 30, mime="image/webp", is_preview=True)
    assert bounded.get("sha-b", mime="image/webp", is_preview=True) is None
    assert bounded.get("sha-a", mime="image/webp", is_preview=True) == "a" * 30
    assert bounded.get("sha-c", mime="image/webp", is_preview=True) == "c" * 30

    now_clock = 401.0
    assert bounded.get("sha-a", mime="image/webp", is_preview=True) is None
    assert bounded.current_bytes == 0
