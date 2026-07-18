from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
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


class _CasSession:
    def __init__(self, conv: Conversation) -> None:
        self.conv = conv
        self.commits = 0
        self.rollbacks = 0
        self.execute_calls = 0
        self.after_execute: Any | None = None
        self.statements: list[Any] = []

    async def execute(self, *args: Any, **_kwargs: Any) -> _ScalarResult:
        self.execute_calls += 1
        if args:
            self.statements.append(args[0])
        if self.after_execute is not None:
            self.after_execute()
        return _ScalarResult(self.conv)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.eval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.expirations: dict[str, int] = {}

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

    async def expire(self, key: str, ttl: int) -> None:
        self.expirations[key] = ttl

    async def eval(
        self,
        script: str,
        _numkeys: int,
        *args: Any,
    ) -> int:
        self.eval_calls.append((script, args))
        key = str(args[0])
        token = str(args[1])
        if self.kv.get(key) != token:
            return 0
        if "DEL" in script:
            await self.delete(key)
            return 1
        if "EXPIRE" in script:
            self.expirations[key] = int(args[2])
            return 1
        raise AssertionError("unexpected Lua script")

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


def _half_open_text_pool() -> tuple[Any, Any]:
    from app.provider_pool import ProviderConfig, ProviderHealth, ProviderPool

    pool = ProviderPool()
    provider = ProviderConfig(
        name="summary-provider",
        base_url="https://summary.example",
        api_key="sk-summary",
    )
    health = ProviderHealth(
        consecutive_failures=3,
        cooldown_until=time.monotonic() - 1.0,
    )
    pool._providers = [provider]
    pool._health = {provider.name: health}
    pool._config_loaded_at = time.monotonic() + 60.0
    return pool, health


@pytest.mark.asyncio
async def test_call_summary_upstream_reports_actual_success_and_closes_half_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool, upstream

    pool, health = _half_open_text_pool()

    async def fake_get_pool() -> Any:
        return pool

    async def fake_responses_call(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"output_text": "summary text"}

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "responses_call", fake_responses_call)

    result = await context_summary._call_summary_upstream(
        "input",
        100,
        "gpt-test",
    )

    assert result == "summary text"
    assert health.consecutive_failures == 0
    assert health.cooldown_until is None
    assert health.half_open_probe_inflight is False
    assert health.half_open_probe_token is None
    assert health.total_requests == 1
    assert health.successful_requests == 1
    assert health.failed_requests == 0


@pytest.mark.asyncio
async def test_call_summary_upstream_reports_actual_failure_and_reopens_half_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool, upstream

    pool, health = _half_open_text_pool()

    async def fake_get_pool() -> Any:
        return pool

    async def fail_responses_call(*_args: Any, **_kwargs: Any) -> None:
        raise context_summary.UpstreamError(
            "upstream unavailable",
            error_code=context_summary.EC.SERVICE_UNAVAILABLE.value,
            status_code=503,
        )

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "responses_call", fail_responses_call)

    result = await context_summary._call_summary_upstream(
        "input",
        100,
        "gpt-test",
    )

    assert result is None
    assert health.consecutive_failures == 4
    assert health.cooldown_until is not None
    assert health.cooldown_until > time.monotonic()
    assert health.half_open_probe_inflight is False
    assert health.half_open_probe_token is None
    assert health.total_requests == 1
    assert health.successful_requests == 0
    assert health.failed_requests == 1


@pytest.mark.asyncio
async def test_call_summary_upstream_cancellation_only_releases_half_open_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool, upstream

    pool, health = _half_open_text_pool()

    async def fake_get_pool() -> Any:
        return pool

    async def cancel_responses_call(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "responses_call", cancel_responses_call)

    with pytest.raises(asyncio.CancelledError):
        await context_summary._call_summary_upstream(
            "input",
            100,
            "gpt-test",
        )

    assert health.consecutive_failures == 3
    assert health.half_open_probe_inflight is False
    assert health.half_open_probe_token is None
    assert health.total_requests == 0
    assert health.successful_requests == 0
    assert health.failed_requests == 0

    provider = (await pool.select(route="text"))[0]
    with pool.text_attempt(provider):
        pass


@pytest.mark.asyncio
async def test_call_summary_upstream_local_parse_error_is_not_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool, upstream

    pool, health = _half_open_text_pool()

    async def fake_get_pool() -> Any:
        return pool

    async def fake_responses_call(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"output_text": "valid upstream response"}

    def fail_local_parse(_payload: Any) -> tuple[str, dict[str, Any]]:
        raise ValueError("local parser failed")

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "responses_call", fake_responses_call)
    monkeypatch.setattr(context_summary, "_parse_response_dict", fail_local_parse)

    result = await context_summary._call_summary_upstream(
        "input",
        100,
        "gpt-test",
    )

    assert result is None
    assert health.consecutive_failures == 0
    assert health.cooldown_until is None
    assert health.half_open_probe_inflight is False
    assert health.half_open_probe_token is None
    assert health.total_requests == 1
    assert health.successful_requests == 1
    assert health.failed_requests == 0


def test_summarize_text_blob_handles_json_code_plain_and_file_read() -> None:
    json_blob = json.dumps(
        {"z": 1, "a": {"nested": True}, "items": [1, 2, 3], "large": "x" * 1800}
    )
    assert "top-level keys" in context_summary._summarize_text_blob(json_blob)

    code_blob = "```python\ndef build_input(value):\n    return value\n```\n" + (
        "x\n" * 900
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
            {
                "kind": "file",
                "name": "brief.pdf",
                "mime": "application/pdf",
                "size": 123,
            },
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
    assert (
        "caption='cached visual caption'"
        in context_summary._message_to_summary_line(
            no_caption,
            image_captions={"img-missing-caption": "cached visual caption"},
        )
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
            {
                "image_id": "gen-1",
                "width": 1024,
                "height": 1024,
                "caption": "poster art",
            },
            {
                "image_id": "gen-2",
                "width": 768,
                "height": 1024,
                "caption": "detail crop",
            },
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


@pytest.mark.asyncio
async def test_ensure_context_summary_uses_message_id_when_timestamps_equal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(10)
    boundary.id = "msg-b"
    same_created_at = boundary.created_at.isoformat()
    base_summary = {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_created_at": same_created_at,
        "first_user_message_id": "msg-001",
        "text": "old summary",
        "tokens": 10,
    }

    async def fail_load(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("newer equal-timestamp summary must be reused")

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fail_load)
    cached = await context_summary.ensure_context_summary(
        _FakeSession(),
        Conversation(
            id="conv-1",
            user_id="user-1",
            summary_jsonb={**base_summary, "up_to_message_id": "msg-c"},
        ),
        boundary,
        {},
    )
    assert cached is not None
    assert cached["status"] == "cached"

    load_calls = 0

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
        nonlocal load_calls
        load_calls += 1
        return context_summary.LoadedSummaryMessages([], 0, 0, 0)

    async def fake_acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_read(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_acquire_summary_lock", fake_acquire)
    monkeypatch.setattr(context_summary, "_read_current_summary", fake_read)
    monkeypatch.setattr(context_summary.asyncio, "sleep", fake_sleep)

    stale = await context_summary.ensure_context_summary(
        _FakeSession(),
        Conversation(
            id="conv-1",
            user_id="user-1",
            summary_jsonb={**base_summary, "up_to_message_id": "msg-a"},
        ),
        boundary,
        {},
    )

    assert stale is None
    assert load_calls == 1


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
async def test_record_summary_metrics_circuit_open_is_not_another_failure() -> None:
    redis = _MetricsRedis()

    await context_summary.record_summary_metrics(
        redis,
        conv_id="conv-1",
        trigger="auto",
        outcome="circuit_open",
    )

    row = next(iter(redis.hashes.values()))
    assert row["fallback_reason:circuit_open"] == 1
    assert "summary_attempts" not in row
    assert "summary_failures" not in row


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


@pytest.mark.asyncio
async def test_segment_and_summarize_limits_safe_prefix_instead_of_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_call(
        input_text: str,
        _target_tokens: int,
        _model: str,
        *,
        extra_instruction: str | None = None,
        timeout_s: float,
    ) -> str:
        _ = (extra_instruction, timeout_s)
        calls.append(input_text)
        return f"summary-{len(calls)}"

    monkeypatch.setattr(context_summary, "_call_summary_upstream", fake_call)
    monkeypatch.setattr(
        context_summary,
        "_message_to_summary_line",
        lambda message, **_kwargs: f"{message.id} " + ("x" * 3000),
    )
    coverage = context_summary._SummaryCoverage()
    messages = [_message(i, "x" * 3000) for i in range(1, 10)]

    result = await context_summary._segment_and_summarize(
        conv_id="conv-1",
        messages=messages,
        previous_summary=None,
        target_tokens=100,
        model="gpt-test",
        input_budget=80,
        coverage=coverage,
    )

    assert result == "summary-8"
    assert len(calls) == context_summary._SUMMARY_MAX_SEGMENTS
    assert "msg-001" in calls[0]
    assert "msg-008" in calls[-1]
    assert all("msg-009" not in call for call in calls)
    assert coverage.covered_message_count == 8
    assert coverage.partial_reason == "segment_limit"


@pytest.mark.asyncio
async def test_segment_limit_does_not_cross_cap_for_one_oversized_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_call(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError(
            "no complete message boundary exists within the segment cap"
        )

    monkeypatch.setattr(context_summary, "_call_summary_upstream", fail_call)
    monkeypatch.setattr(
        context_summary,
        "_message_to_summary_line",
        lambda message, **_kwargs: f"{message.id} " + ("x" * 100_000),
    )
    coverage = context_summary._SummaryCoverage()

    result = await context_summary._segment_and_summarize(
        conv_id="conv-1",
        messages=[_message(1)],
        previous_summary=None,
        target_tokens=100,
        model="gpt-test",
        input_budget=80,
        coverage=coverage,
    )

    assert result is None
    assert coverage.covered_message_count == 0
    assert coverage.partial_reason == "segment_limit"


@pytest.mark.asyncio
async def test_segment_failure_returns_last_complete_message_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_call(
        input_text: str,
        _target_tokens: int,
        _model: str,
        *,
        extra_instruction: str | None = None,
        timeout_s: float,
    ) -> str | None:
        _ = (extra_instruction, timeout_s)
        calls.append(input_text)
        if len(calls) == 3:
            return None
        return f"summary-{len(calls)}"

    monkeypatch.setattr(context_summary, "_call_summary_upstream", fake_call)
    monkeypatch.setattr(
        context_summary,
        "_message_to_summary_line",
        lambda message, **_kwargs: (
            f"{message.id} " + ("x" * (500 if message.id == "msg-001" else 20_000))
        ),
    )
    coverage = context_summary._SummaryCoverage()

    result = await context_summary._segment_and_summarize(
        conv_id="conv-1",
        messages=[_message(1), _message(2)],
        previous_summary=None,
        target_tokens=100,
        model="gpt-test",
        input_budget=80,
        coverage=coverage,
    )

    assert len(calls) == 3
    assert result == "summary-1"
    assert coverage.covered_message_count == 1
    assert coverage.partial_reason == "partial_segment_failure"


@pytest.mark.asyncio
async def test_partial_segment_commit_resumes_after_covered_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3, "x" * 3000)
    messages = [_message(i, "x" * 3000) for i in range(1, 4)]
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    redis = _FakeRedis()
    load_after_ids: list[str | None] = []
    written: list[dict[str, Any]] = []

    async def fake_load(
        _session: Any,
        _conv_id: str,
        after_message_id: str | None,
        _before_boundary_id: str,
    ) -> context_summary.LoadedSummaryMessages:
        load_after_ids.append(after_message_id)
        selected = messages if after_message_id is None else messages[1:]
        return context_summary.LoadedSummaryMessages(
            selected,
            len(selected),
            sum(
                context_summary.estimate_message_tokens(msg.role, msg.content)
                for msg in selected
            ),
            0,
        )

    async def fake_segment(**kwargs: Any) -> str:
        coverage = kwargs["coverage"]
        current_messages = kwargs["messages"]
        if len(load_after_ids) == 1:
            coverage.covered_message_count = 1
            coverage.partial_reason = "partial_segment_failure"
            return "summary-through-msg-001"
        coverage.covered_message_count = len(current_messages)
        return "summary-through-msg-003"

    async def fake_cas(
        _session: Any,
        _conv_id: str,
        summary: dict[str, Any],
        **_kwargs: Any,
    ) -> bool:
        written.append(summary)
        conv.summary_jsonb = summary
        return True

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fake_segment)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)

    first = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": redis},
    )
    second = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": redis},
    )

    assert first is not None
    assert first["summary_up_to_message_id"] == "msg-001"
    assert first["source_message_count"] == 1
    assert written[0]["up_to_message_id"] == "msg-001"
    assert second is not None
    assert second["summary_up_to_message_id"] == "msg-003"
    assert load_after_ids == [None, "msg-001"]


def test_chunk_lines_by_budget_splits_single_oversized_line() -> None:
    chunks = context_summary._chunk_lines_by_budget(["x" * 80_000], 1000)

    assert len(chunks) > 1
    assert all(
        context_summary.estimate_text_tokens(line) <= 1000
        for chunk in chunks
        for line in chunk
    )


@pytest.mark.asyncio
async def test_renew_summary_lock_marks_lock_lost_when_key_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FakeRedis()
    lock = context_summary._SummaryLock("redis", "token-1")

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(context_summary.asyncio, "sleep", fake_sleep)

    await context_summary._renew_summary_lock_loop(
        redis,
        "conv-1",
        lock,
        interval_s=1,
    )

    assert lock.lost_reason == "expired"


@pytest.mark.asyncio
async def test_redis_summary_lock_release_does_not_delete_new_owner() -> None:
    class SwitchingRedis(_FakeRedis):
        async def eval(
            self,
            script: str,
            numkeys: int,
            *args: Any,
        ) -> int:
            self.kv[str(args[0])] = "token-new"
            return await super().eval(script, numkeys, *args)

    redis = SwitchingRedis()
    key = "context:summary:lock:conv-1"
    redis.kv[key] = "token-old"

    await context_summary._release_summary_lock(
        redis,
        "conv-1",
        context_summary._SummaryLock("redis", "token-old"),
    )

    assert redis.kv[key] == "token-new"
    assert key not in redis.deleted
    assert len(redis.eval_calls) == 1


@pytest.mark.asyncio
async def test_manual_compact_active_release_uses_atomic_job_owner_cas() -> None:
    key = "context:manual_compact:active:user-1:conv-1"

    class Redis:
        def __init__(self) -> None:
            self.value = json.dumps({"job_id": "job-new"})
            self.calls: list[tuple[Any, ...]] = []

        async def eval(self, *args: Any) -> int:
            self.calls.append(args)
            _script, _numkeys, eval_key, job_id = args
            payload = json.loads(self.value)
            if eval_key == key and payload.get("job_id") == job_id:
                self.value = ""
                return 1
            return 0

    redis = Redis()
    await context_summary._safe_release_manual_compact_active(
        redis,
        user_id="user-1",
        conv_id="conv-1",
        job_id="job-old",
    )

    assert json.loads(redis.value)["job_id"] == "job-new"
    assert redis.calls[0][1:] == (1, key, "job-old")
    assert "cjson.decode" in redis.calls[0][0]


@pytest.mark.asyncio
async def test_redis_summary_lock_renew_does_not_expire_new_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SwitchingRedis(_FakeRedis):
        async def eval(
            self,
            script: str,
            numkeys: int,
            *args: Any,
        ) -> int:
            self.kv[str(args[0])] = "token-new"
            return await super().eval(script, numkeys, *args)

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(context_summary.asyncio, "sleep", fake_sleep)
    redis = SwitchingRedis()
    key = "context:summary:lock:conv-1"
    redis.kv[key] = "token-old"
    lock = context_summary._SummaryLock("redis", "token-old")

    await context_summary._renew_summary_lock_loop(
        redis,
        "conv-1",
        lock,
        interval_s=1,
    )

    assert redis.kv[key] == "token-new"
    assert key not in redis.expirations
    assert lock.lost_reason == "stolen"


@pytest.mark.asyncio
async def test_cas_write_refuses_equal_boundary_fallback_downgrade() -> None:
    boundary_at = "2026-01-01T00:00:03+00:00"
    current_summary = {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": "msg-003",
        "up_to_created_at": boundary_at,
        "first_user_message_id": "msg-001",
        "text": "upstream summary",
        "tokens": 20,
        "compression_runs": 2,
        "compressed_at": "2026-01-01T00:00:10+00:00",
        "fallback_reason": None,
    }
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=current_summary)
    session = _CasSession(conv)
    fallback_summary = {
        **current_summary,
        "text": "local fallback",
        "tokens": 25,
        "compressed_at": "2026-01-01T00:00:20+00:00",
        "fallback_reason": "local_fallback",
        "last_quality_signal": "local_fallback",
    }

    wrote = await context_summary._cas_write_summary(
        session,
        "conv-1",
        fallback_summary,
    )

    assert wrote is False
    assert conv.summary_jsonb == current_summary
    assert session.commits == 0
    assert session.rollbacks == 0
    assert session.statements[0].get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
async def test_cas_write_skips_db_when_redis_lease_already_lost() -> None:
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    session = _CasSession(conv)
    lock = context_summary._SummaryLock("redis", "token-1", lost_reason="stolen")

    wrote = await context_summary._cas_write_summary(
        session,
        "conv-1",
        {
            "version": SUMMARY_VERSION,
            "kind": SUMMARY_KIND,
            "up_to_message_id": "msg-003",
            "up_to_created_at": "2026-01-01T00:00:03+00:00",
            "first_user_message_id": "msg-001",
            "text": "summary",
            "tokens": 10,
        },
        lock=lock,
    )

    assert wrote is False
    assert session.execute_calls == 0
    assert session.commits == 0


@pytest.mark.asyncio
async def test_cas_write_rolls_back_when_lease_lost_after_row_lock() -> None:
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    session = _CasSession(conv)
    lock = context_summary._SummaryLock("redis", "token-1")

    def lose_lock() -> None:
        lock.lost_reason = "expired"

    session.after_execute = lose_lock

    wrote = await context_summary._cas_write_summary(
        session,
        "conv-1",
        {
            "version": SUMMARY_VERSION,
            "kind": SUMMARY_KIND,
            "up_to_message_id": "msg-003",
            "up_to_created_at": "2026-01-01T00:00:03+00:00",
            "first_user_message_id": "msg-001",
            "text": "summary",
            "tokens": 10,
        },
        lock=lock,
    )

    assert wrote is False
    assert session.execute_calls == 1
    assert session.rollbacks == 1
    assert session.commits == 0
    assert conv.summary_jsonb is None


@pytest.mark.asyncio
async def test_ensure_context_summary_dry_run_does_not_call_upstream_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages(
            [_message(1), _message(2)], 2, 42, 0
        )

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

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages(
            [_message(1), _message(2)], 2, 1000, 1
        )

    async def fake_segment(**kwargs: Any) -> str:
        assert kwargs["image_captions"] == {"img-1": "generated caption"}
        return "## Earlier Context Summary\nimportant facts"

    async def fake_cas(
        _session: Any,
        _conv_id: str,
        summary: dict[str, Any],
        **_kwargs: Any,
    ) -> bool:
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
        {
            "redis": redis,
            "context.summary_target_tokens": 300,
            "context.summary_model": "gpt-test",
        },
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

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages(
            [_message(1, "original goal"), _message(2, "important file /tmp/a.py")],
            2,
            1000,
            0,
        )

    async def fake_segment(**_kwargs: Any) -> None:
        return None

    async def fake_cas(
        _session: Any,
        _conv_id: str,
        summary: dict[str, Any],
        **_kwargs: Any,
    ) -> bool:
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
async def test_local_fallback_advances_only_contiguous_source_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(4)
    messages = [_message(index, f"message {index}") for index in range(1, 5)]
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    load_after_ids: list[str | None] = []
    written: list[dict[str, Any]] = []

    async def fake_load(
        _session: Any,
        _conv_id: str,
        after_message_id: str | None,
        _before_boundary_id: str,
    ) -> context_summary.LoadedSummaryMessages:
        load_after_ids.append(after_message_id)
        start = 0 if after_message_id is None else int(after_message_id[-3:])
        selected = messages[start:]
        return context_summary.LoadedSummaryMessages(
            selected,
            len(selected),
            1000,
            0,
        )

    async def fake_segment(**_kwargs: Any) -> None:
        return None

    async def fake_cas(
        _session: Any,
        _conv_id: str,
        summary: dict[str, Any],
        **_kwargs: Any,
    ) -> bool:
        written.append(summary)
        conv.summary_jsonb = summary
        return True

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fake_segment)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)
    monkeypatch.setattr(
        context_summary,
        "_message_to_summary_line",
        lambda message, **_kwargs: f"{message.id} " + ("x" * 700),
    )

    first = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": _FakeRedis(), "context.summary_target_tokens": 300},
    )
    second = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": _FakeRedis(), "context.summary_target_tokens": 300},
    )
    third = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": _FakeRedis(), "context.summary_target_tokens": 300},
    )

    assert first is not None and second is not None and third is not None
    assert first["summary_up_to_message_id"] == "msg-002"
    assert second["summary_up_to_message_id"] == "msg-003"
    assert third["summary_up_to_message_id"] == "msg-004"
    assert load_after_ids == [None, "msg-002", "msg-003"]
    assert "msg-001" in written[0]["text"]
    assert "msg-002" in written[0]["text"]
    assert "msg-003" not in written[0]["text"]


@pytest.mark.asyncio
async def test_open_circuit_local_fallback_does_not_record_failure_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    redis = _FakeRedis()
    metric_outcomes: list[str] = []

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages(
            [_message(1, "goal"), _message(2, "decision")],
            2,
            100,
            0,
        )

    async def fail_segment(**_kwargs: Any) -> str:
        raise AssertionError("open circuit must not call upstream summarization")

    async def fail_circuit_sample(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("open circuit fallback must not refresh failure TTL")

    async def fake_metrics(
        _redis: Any,
        *,
        conv_id: str,
        trigger: str,
        outcome: str,
        **_kwargs: Any,
    ) -> None:
        _ = (conv_id, trigger)
        metric_outcomes.append(outcome)

    async def fake_cas(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    async def circuit_open(_redis: Any) -> bool:
        return True

    monkeypatch.setattr(context_summary, "_is_circuit_open", circuit_open)
    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fail_segment)
    monkeypatch.setattr(context_summary, "_record_circuit_sample", fail_circuit_sample)
    monkeypatch.setattr(context_summary, "record_summary_metrics", fake_metrics)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": redis},
    )

    assert result is not None
    assert result["status"] == "created_local_fallback"
    assert result["fallback_reason"] == "circuit_open_local_fallback"
    assert metric_outcomes == ["circuit_open", "ok"]


@pytest.mark.asyncio
async def test_segment_limit_local_fallback_does_not_record_failure_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(2)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    redis = _FakeRedis()

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages([_message(1)], 1, 20, 0)

    async def segment_limited(**kwargs: Any) -> None:
        kwargs["coverage"].partial_reason = "segment_limit"
        return None

    async def fail_circuit_sample(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("local segment cap must not count as an upstream failure")

    async def fake_metrics(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_cas(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", segment_limited)
    monkeypatch.setattr(context_summary, "_record_circuit_sample", fail_circuit_sample)
    monkeypatch.setattr(context_summary, "record_summary_metrics", fake_metrics)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": redis},
    )

    assert result is not None
    assert result["status"] == "created_local_fallback"


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

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
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

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
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


@pytest.mark.asyncio
async def test_postgres_summary_lock_uses_dedicated_session_lock_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Connection:
        commits = 0
        closed = False

        async def execute(self, statement: Any, _params: Any) -> _ScalarResult:
            statements.append(str(statement))
            return _ScalarResult(True)

        async def commit(self) -> None:
            self.commits += 1

        async def close(self) -> None:
            self.closed = True

        async def invalidate(self) -> None:
            raise AssertionError("valid unlock must not invalidate connection")

    connection = Connection()

    class Engine:
        async def connect(self) -> Connection:
            return connection

    monkeypatch.setattr(context_summary, "engine", Engine())

    lock = await context_summary._acquire_summary_lock(  # noqa: SLF001
        object(),
        None,
        "conv-pg-lock",
    )

    assert lock is not None
    assert lock.kind == "pg"
    assert "pg_try_advisory_lock" in statements[0]
    assert "pg_try_advisory_xact_lock" not in statements[0]
    await context_summary._release_summary_lock(  # noqa: SLF001
        None,
        "conv-pg-lock",
        lock,
    )
    assert "pg_advisory_unlock" in statements[1]
    assert connection.commits == 2
    assert connection.closed is True


@pytest.mark.asyncio
async def test_redis_summary_lock_failure_does_not_consume_postgres_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Engine:
        async def connect(self) -> None:
            raise AssertionError("postgres fallback must stay unused")

    class BrokenRedis:
        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(context_summary, "engine", Engine())

    lock = await context_summary._acquire_summary_lock(  # noqa: SLF001
        object(),
        BrokenRedis(),
        "conv-redis-down",
    )

    assert lock is None


@pytest.mark.asyncio
async def test_summary_releases_business_transaction_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-1", user_id="user-1", summary_jsonb=None)
    session = _FakeSession()
    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1

    session.commit = commit  # type: ignore[method-assign]

    async def fake_load(
        *_args: Any, **_kwargs: Any
    ) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages([_message(1)], 1, 20, 0)

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        assert commits >= 1
        return {}

    async def fake_segment(**_kwargs: Any) -> str:
        assert commits >= 1
        return "summary"

    async def fake_cas(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fake_segment)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)

    result = await context_summary.ensure_context_summary(
        session,
        conv,
        boundary,
        {"redis": _FakeRedis()},
        force=True,
    )

    assert result is not None
    assert commits >= 1
