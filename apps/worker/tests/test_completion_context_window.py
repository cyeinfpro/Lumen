from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from typing import Any

import pytest

os.environ.setdefault(
    "STORAGE_ROOT", str(Path(tempfile.gettempdir()) / "lumen-worker-test-storage")
)

from app.tasks import completion
from lumen_core.constants import Role
from lumen_core.context_window import SUMMARY_KIND, SUMMARY_VERSION
from lumen_core.models import Conversation, Message


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

    monkeypatch.setattr(completion.runtime_settings, "resolve_int", _disable_compression)
    # Default system instructions are now budgeted with the same doubled
    # overhead as production requests, so keep this high enough for the two
    # newest messages while still forcing older history to be truncated.
    monkeypatch.setattr(completion, "CONTEXT_INPUT_TOKEN_BUDGET", 140)
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
async def test_compression_injects_sticky_and_existing_summary(
    enable_compression: None,
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
    assert completion.compose_summary_guardrail() in joined
    assert "old detail " not in joined


@pytest.mark.asyncio
async def test_compression_calls_summary_service_when_missing(
    enable_compression: None,
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
async def test_compression_failure_fallback_still_keeps_current_user_message(
    enable_compression: None,
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
