from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any

import pytest

from app.tasks import context_summary
from lumen_core.constants import Role
from lumen_core.context_window import SUMMARY_KIND, SUMMARY_VERSION
from lumen_core.models import Conversation, Message


def _message(index: int, text: str = "hello", role: str = Role.USER.value) -> Message:
    return Message(
        id=f"msg-{index:03d}",
        conversation_id="conv-1",
        role=role,
        content={"text": text, "attachments": []},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index),
        deleted_at=None,
    )


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _FakeSession:
    async def execute(self, *_args: Any, **_kwargs: Any) -> _ScalarResult:
        return _ScalarResult("first-user")

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        if kwargs.get("nx") and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.kv.pop(key, None)

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, json.loads(payload)))


class _MetricsRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, int]] = {}
        self.ttls: dict[str, int] = {}
        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}

    async def hincrby(self, key: str, field: str, value: int) -> None:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + value

    async def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        values = self.lists.get(key, [])
        if stop == -1:
            self.lists[key] = values[start:]
        else:
            self.lists[key] = values[start : stop + 1]

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        values = self.lists.get(key, [])
        if stop == -1:
            return values[start:]
        return values[start : stop + 1]

    async def set(self, key: str, value: str, **_kwargs: Any) -> None:
        self.values[key] = value


def test_summarize_text_blob_handles_json_code_plain_and_file_read() -> None:
    json_blob = json.dumps(
        {"z": 1, "a": {"nested": True}, "items": [1, 2, 3], "large": "x" * 1800}
    )
    assert "top-level keys" in context_summary._summarize_text_blob(json_blob)

    code_blob = (
        "```python\n"
        "def build_input(value):\n"
        "    return value\n"
        "```\n"
        + ("x\n" * 900)
    )
    code_summary = context_summary._summarize_text_blob(code_blob)
    assert "def build_input" in code_summary
    assert "lines elided" in code_summary

    plain = "A" * 1800 + "TAIL"
    plain_summary = context_summary._summarize_text_blob(plain)
    assert "[... elided ...]" in plain_summary
    assert plain_summary.endswith("TAIL")

    file_read = "Read /tmp/example.py\n" + ("line\n" * 400)
    assert context_summary._summarize_text_blob(file_read) == (
        "[file read summary: /tmp/example.py - 401 lines]"
    )


def test_message_to_summary_line_serializes_attachments_and_generated_image() -> None:
    msg = _message(1, "describe this", Role.USER.value)
    msg.content = {
        "text": "describe this",
        "attachments": [
            {"kind": "image", "image_id": "img-1", "caption": "A red cube on a desk"},
            {"kind": "file", "name": "brief.pdf", "mime": "application/pdf", "size": 123},
            {"kind": "unknown"},
        ],
    }

    line = context_summary._message_to_summary_line(msg)

    assert "[USER #msg-001" in line
    assert "describe this" in line
    assert "[user_image image_id=img-1]" in line
    assert "A red cube on a desk" in line
    assert "[user_file name='brief.pdf'" in line
    assert "[attachment kind='unknown']" in line

    no_caption = _message(9, "old image", Role.USER.value)
    no_caption.content = {
        "text": "old image",
        "attachments": [{"kind": "image", "image_id": "img-missing-caption"}],
    }
    assert "caption='cached visual caption'" in context_summary._message_to_summary_line(
        no_caption,
        image_captions={"img-missing-caption": "cached visual caption"},
    )

    assistant = _message(2, "", Role.ASSISTANT.value)
    assistant.content = {
        "generation_summary": {
            "image_id": "gen-1",
            "width": 1024,
            "height": 1024,
            "caption": "poster art",
        },
        "images": [
            {"image_id": "gen-1", "width": 1024, "height": 1024, "caption": "poster art"},
            {"image_id": "gen-2", "width": 768, "height": 1024, "caption": "detail crop"},
        ],
    }
    assistant_line = context_summary._message_to_summary_line(assistant)
    assert assistant_line.count("[generated_image image_id=gen-1") == 1
    assert "[generated_image image_id=gen-2" in assistant_line


def test_summary_response_body_uses_gpt54_high_reasoning() -> None:
    body = context_summary._summary_response_body(
        "source",
        target_tokens=300,
        model=context_summary._SUMMARY_MODEL,
        instructions="compress",
    )

    assert body["model"] == "gpt-5.4"
    assert body["reasoning"] == {"effort": "high"}
    assert "max_output_tokens" not in body
    assert body["store"] is False


def test_summary_boundary_uses_message_id_when_timestamps_equal() -> None:
    boundary = _message(10)
    boundary.id = "msg-b"
    same_created_at = boundary.created_at.isoformat()
    base = {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": "msg-a",
        "up_to_created_at": same_created_at,
        "first_user_message_id": "msg-001",
        "text": "old summary",
        "tokens": 10,
    }

    assert context_summary._summary_covers_boundary(base, boundary) is False
    assert (
        context_summary._summary_covers_boundary(
            {**base, "up_to_message_id": "msg-c"},
            boundary,
        )
        is True
    )


def test_worker_compact_summary_payload_preserves_public_stats() -> None:
    conv = Conversation(
        id="conv-1",
        user_id="user-1",
        summary_jsonb={"compressed_at": "2026-04-26T12:00:00+00:00"},
    )

    payload = context_summary._worker_compact_summary_payload(
        result={
            "status": "created",
            "summary_created": True,
            "summary_used": True,
            "summary_up_to_message_id": "msg-3",
            "summary_up_to_created_at": "2026-04-26T00:00:03+00:00",
            "summary_tokens": 200,
            "source_message_count": 4,
            "source_token_estimate": 1200,
            "image_caption_count": 1,
            "fallback_reason": None,
        },
        conv=conv,
    )

    assert payload["tokens_freed"] == 1000
    assert payload["source_token_estimate"] == 1200
    assert payload["image_caption_count"] == 1


@pytest.mark.asyncio
async def test_record_summary_metrics_writes_admin_compatible_fields() -> None:
    redis = _MetricsRedis()

    await context_summary.record_summary_metrics(
        redis,
        conv_id="conv-1",
        trigger="manual",
        outcome="ok",
        source_tokens=100,
        summary_tokens=10,
    )
    await context_summary.record_summary_metrics(
        redis,
        conv_id="conv-1",
        trigger="auto",
        outcome="failed",
    )

    row = next(iter(redis.hashes.values()))
    assert row["manual_compact_calls"] == 1
    assert row["summary_attempts"] == 2
    assert row["summary_successes"] == 1
    assert row["summary_failures"] == 1
    assert row["fallback_reason:summary_failed"] == 1


@pytest.mark.asyncio
async def test_record_summary_metrics_opens_circuit_after_failure_threshold() -> None:
    redis = _MetricsRedis()

    for _ in range(5):
        await context_summary.record_summary_metrics(
            redis,
            conv_id="conv-1",
            trigger="auto",
            outcome="failed",
            circuit_threshold_percent=60,
        )

    assert "context:circuit:breaker:state" in redis.values
    state = json.loads(redis.values["context:circuit:breaker:state"])
    assert state["state"] == "open"
    assert "context:circuit:breaker:until" in redis.values


@pytest.mark.asyncio
async def test_segment_and_summarize_uses_segments_and_partial_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_call(
        input_text: str,
        target_tokens: int,
        model: str,
        *,
        extra_instruction: str | None = None,
    ) -> str:
        calls.append(input_text)
        return f"summary-{len(calls)}"

    progress: list[tuple[int, int]] = []

    async def on_progress(current: int, total: int) -> None:
        progress.append((current, total))

    monkeypatch.setattr(context_summary, "_call_summary_upstream", fake_call)
    redis = _FakeRedis()
    messages = [_message(i, "x" * 3000) for i in range(1, 6)]

    result = await context_summary._segment_and_summarize(
        conv_id="conv-1",
        messages=messages,
        previous_summary=None,
        target_tokens=100,
        model="gpt-test",
        input_budget=80,
        redis=redis,
        progress_callback=on_progress,
    )

    assert result == f"summary-{len(calls)}"
    assert len(calls) > 1
    assert progress[-1] == (len(calls), len(calls))
    assert "context:summary:partial:conv-1" in redis.kv


def test_chunk_lines_by_budget_splits_single_oversized_line() -> None:
    chunks = context_summary._chunk_lines_by_budget(["x" * 80_000], 1000)

    assert len(chunks) > 1
    assert all(
        context_summary.estimate_text_tokens(line) <= 1000
        for chunk in chunks
        for line in chunk
    )


@pytest.mark.asyncio
async def test_ensure_context_summary_dry_run_does_not_call_upstream_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)

    async def fake_load(*_args: Any, **_kwargs: Any) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages([_message(1), _message(2)], 2, 42, 0)

    async def fail_segment(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("dry_run must not call upstream")

    async def fail_cas(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("dry_run must not write")

    async def fail_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise AssertionError("dry_run must not caption images")

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fail_segment)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fail_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fail_caption)

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": _FakeRedis()},
        dry_run=True,
    )

    assert result is not None
    assert result["status"] == "dry_run"
    assert result["source_message_count"] == 2
    assert "text" not in result


@pytest.mark.asyncio
async def test_ensure_context_summary_writes_summary_and_returns_public_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    redis = _FakeRedis()
    written: dict[str, Any] = {}

    async def fake_load(*_args: Any, **_kwargs: Any) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages([_message(1), _message(2)], 2, 1000, 1)

    async def fake_segment(**kwargs: Any) -> str:
        assert kwargs["image_captions"] == {"img-1": "generated caption"}
        return "## Earlier Context Summary\nimportant facts"

    async def fake_cas(_session: Any, _conv_id: str, summary: dict[str, Any]) -> bool:
        written.update(summary)
        return True

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"img-1": "generated caption"}

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fake_segment)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": redis, "context.summary_target_tokens": 300, "context.summary_model": "gpt-test"},
        extra_instruction="keep image ids",
        trigger="manual",
    )

    assert result is not None
    assert result["status"] == "created"
    assert result["summary_created"] is True
    assert result["summary_up_to_message_id"] == boundary.id
    assert "text" not in result
    assert written["text"].startswith("## Earlier Context Summary")
    assert written["kind"] == SUMMARY_KIND
    assert written["version"] == SUMMARY_VERSION
    assert written["extra_instruction_hash"].startswith("sha1:")
    assert written["image_caption_count"] == 2

    phases = [payload["phase"] for _channel, payload in redis.published]
    assert phases == ["started", "completed"]
    assert all("text" not in payload for _channel, payload in redis.published)
    completed = redis.published[-1][1]
    assert completed["ok"] is True
    assert completed["stats"]["tokens_freed"] > 0


@pytest.mark.asyncio
async def test_ensure_context_summary_writes_local_fallback_when_upstream_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    redis = _FakeRedis()
    written: dict[str, Any] = {}

    async def fake_load(*_args: Any, **_kwargs: Any) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages(
            [_message(1, "original goal"), _message(2, "important file /tmp/a.py")],
            2,
            1000,
            0,
        )

    async def fake_segment(**_kwargs: Any) -> None:
        return None

    async def fake_cas(_session: Any, _conv_id: str, summary: dict[str, Any]) -> bool:
        written.update(summary)
        return True

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fake_segment)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": redis, "context.summary_target_tokens": 300},
        trigger="manual",
    )

    assert result is not None
    assert result["status"] == "created_local_fallback"
    assert result["summary_created"] is True
    assert written["fallback_reason"] == "local_fallback"
    assert "original goal" in written["text"]
    assert "important file" in written["text"]
    assert redis.published[-1][1]["ok"] is True
    assert redis.published[-1][1]["fallback_reason"] == "local_fallback"


@pytest.mark.asyncio
async def test_ensure_context_summary_lock_busy_waits_and_reuses_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    latest = {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": boundary.id,
        "up_to_created_at": boundary.created_at.isoformat(),
        "first_user_message_id": "first-user",
        "text": "hidden summary",
        "tokens": 10,
        "source_message_count": 3,
        "source_token_estimate": 100,
        "image_caption_count": 0,
    }

    async def fake_acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_read(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return latest

    async def fake_sleep(_seconds: float) -> None:
        return None

    async def fake_load(*_args: Any, **_kwargs: Any) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages([_message(1)], 1, 20, 0)

    monkeypatch.setattr(context_summary, "_acquire_summary_lock", fake_acquire)
    monkeypatch.setattr(context_summary, "_read_current_summary", fake_read)
    monkeypatch.setattr(context_summary.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": _FakeRedis()},
        force=True,
    )

    assert result is not None
    assert result["status"] == "cached_after_lock_wait"
    assert result["summary_tokens"] == 10
    assert "text" not in result


@pytest.mark.asyncio
async def test_ensure_context_summary_lock_busy_does_not_reuse_mismatched_extra_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    latest = {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": boundary.id,
        "up_to_created_at": boundary.created_at.isoformat(),
        "first_user_message_id": "first-user",
        "text": "hidden summary",
        "tokens": 10,
        "source_message_count": 3,
        "source_token_estimate": 100,
        "image_caption_count": 0,
        "extra_instruction_hash": "sha1:other",
    }

    async def fake_acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_read(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return latest

    async def fake_sleep(_seconds: float) -> None:
        return None

    async def fake_load(*_args: Any, **_kwargs: Any) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages([_message(1)], 1, 20, 0)

    monkeypatch.setattr(context_summary, "_acquire_summary_lock", fake_acquire)
    monkeypatch.setattr(context_summary, "_read_current_summary", fake_read)
    monkeypatch.setattr(context_summary.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": _FakeRedis()},
        extra_instruction="different focus",
    )

    assert result is None
