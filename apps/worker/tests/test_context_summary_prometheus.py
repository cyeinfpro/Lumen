"""Prometheus counter / histogram coverage for context compaction (P1-3).

Why a dedicated file: prometheus_client uses a process-wide registry — counters /
histograms accumulate across tests. We snapshot label values **before** each
exercise and assert deltas, instead of expecting absolute values. This avoids
flakiness when other test files also touch the same metrics (or when this file
runs together with the broader worker suite).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.observability import (
    context_compaction_duration_seconds,
    context_compaction_total,
)
from app.tasks import context_summary
from lumen_core.constants import Role
from lumen_core.context_window import SUMMARY_KIND, SUMMARY_VERSION
from lumen_core.models import Conversation, Message


# ---------- Helpers --------------------------------------------------------


def _counter_value(reason: str, trigger: str, outcome: str) -> float:
    sample = context_compaction_total.labels(
        reason=reason, trigger=trigger, outcome=outcome
    )
    return float(sample._value.get())


def _hist_sum(reason: str, outcome: str) -> float:
    sample = context_compaction_duration_seconds.labels(reason=reason, outcome=outcome)
    return float(sample._sum.get())


def _hist_count(reason: str, outcome: str) -> float:
    """Total observation count = sum of all per-bucket counters.

    Why: prometheus_client stores each bucket as a non-cumulative counter (count
    of observations falling exactly in that le range). Total observations =
    sum of all buckets. The +Inf bucket alone only contains values larger than
    the largest finite bucket.
    """
    sample = context_compaction_duration_seconds.labels(reason=reason, outcome=outcome)
    return sum(float(b.get()) for b in sample._buckets)


def _message(index: int, text: str = "hello", role: str = Role.USER.value) -> Message:
    return Message(
        id=f"msg-prom-{index:03d}",
        conversation_id="conv-prom-1",
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
    """Minimal redis stub that supports the lock + publish + metrics calls."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        if kwargs.get("nx") and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def delete(self, key: str) -> None:
        self.kv.pop(key, None)

    async def publish(self, _channel: str, _payload: str) -> None:
        return None

    async def hincrby(self, key: str, field: str, value: int) -> None:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + value

    async def expire(self, _key: str, _ttl: int) -> None:
        return None

    async def lpush(self, _key: str, _value: str) -> None:
        return None

    async def ltrim(self, _key: str, _start: int, _stop: int) -> None:
        return None

    async def lrange(self, _key: str, _start: int, _stop: int) -> list[str]:
        return []


# ---------- Tests ----------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_counter_increments_on_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-prom-1", user_id="user-1", summary_jsonb=None)
    redis = _FakeRedis()

    async def fake_load(*_args: Any, **_kwargs: Any) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages(
            [_message(1), _message(2)], 2, 1000, 0
        )

    async def fake_segment(**_kwargs: Any) -> str:
        return "## Earlier Context Summary\nfacts"

    async def fake_cas(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fake_segment)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)

    before_counter = _counter_value("token_limit", "auto", "ok")
    before_hist_count = _hist_count("token_limit", "ok")
    before_hist_sum = _hist_sum("token_limit", "ok")

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": redis},
        trigger="auto",
    )

    assert result is not None
    assert result["status"] == "created"
    assert _counter_value("token_limit", "auto", "ok") == before_counter + 1.0
    # Histogram should have one new observation; _sum must grow (>= 0)
    assert _hist_count("token_limit", "ok") == before_hist_count + 1.0
    assert _hist_sum("token_limit", "ok") >= before_hist_sum


@pytest.mark.asyncio
async def test_compaction_counter_increments_on_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-prom-1", user_id="user-1", summary_jsonb=None)
    redis = _FakeRedis()

    async def fake_load(*_args: Any, **_kwargs: Any) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages([_message(1)], 1, 100, 0)

    async def fake_segment(**_kwargs: Any) -> None:
        # Why: returning None / "" simulates the upstream failure path inside
        # ensure_context_summary -> outcome="failed".
        return None

    async def fake_cas(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_caption(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fake_segment)
    monkeypatch.setattr(context_summary, "_cas_write_summary", fake_cas)
    monkeypatch.setattr(context_summary, "_caption_images_for_summary", fake_caption)

    before_counter = _counter_value("token_limit", "auto", "ok")
    before_hist_count = _hist_count("token_limit", "ok")

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": redis},
        trigger="auto",
    )

    assert isinstance(result, dict)
    assert result["status"] == "created_local_fallback"
    assert _counter_value("token_limit", "auto", "ok") == before_counter + 1.0
    assert _hist_count("token_limit", "ok") == before_hist_count + 1.0


@pytest.mark.asyncio
async def test_compaction_histogram_excludes_lock_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _message(3)
    conv = Conversation(id="conv-prom-1", user_id="user-1", summary_jsonb=None)

    # When lock is busy AND there's no usable cached summary covering the boundary,
    # ensure_context_summary records outcome="lock_busy" and returns None — without
    # calling upstream. Histogram MUST NOT be sampled (would pollute p50/p99).
    async def fake_acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_read(*_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
        return None  # no cached summary -> lock_busy outcome

    async def fake_sleep(_seconds: float) -> None:
        return None

    async def fake_load(*_args: Any, **_kwargs: Any) -> context_summary.LoadedSummaryMessages:
        return context_summary.LoadedSummaryMessages([_message(1)], 1, 20, 0)

    async def fake_segment(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("lock_busy must not call upstream")

    monkeypatch.setattr(context_summary, "_acquire_summary_lock", fake_acquire)
    monkeypatch.setattr(context_summary, "_read_current_summary", fake_read)
    monkeypatch.setattr(context_summary.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(context_summary, "_load_messages_for_summary", fake_load)
    monkeypatch.setattr(context_summary, "_segment_and_summarize", fake_segment)

    before_counter = _counter_value("token_limit", "auto", "lock_busy")
    before_hist_count_ok = _hist_count("token_limit", "ok")
    before_hist_count_failed = _hist_count("token_limit", "failed")
    # Snapshot any potentially-existing lock_busy histogram series too, in case
    # earlier code paths ever observed under that label name.
    before_hist_count_lock = _hist_count("token_limit", "lock_busy")
    before_hist_sum_lock = _hist_sum("token_limit", "lock_busy")

    result = await context_summary.ensure_context_summary(
        _FakeSession(),
        conv,
        boundary,
        {"redis": _FakeRedis()},
    )

    assert result is None
    # Counter still increments (we want to count the busy event)
    assert _counter_value("token_limit", "auto", "lock_busy") == before_counter + 1.0
    # Histogram for lock_busy must remain unchanged
    assert _hist_count("token_limit", "lock_busy") == before_hist_count_lock
    assert _hist_sum("token_limit", "lock_busy") == before_hist_sum_lock
    # And we must not have polluted ok / failed histograms either
    assert _hist_count("token_limit", "ok") == before_hist_count_ok
    assert _hist_count("token_limit", "failed") == before_hist_count_failed


@pytest.mark.asyncio
async def test_compaction_counter_uses_manual_reason_when_trigger_manual() -> None:
    """record_summary_metrics with trigger='manual' must label reason='manual'."""
    redis = _FakeRedis()

    before = _counter_value("manual", "manual", "ok")
    await context_summary.record_summary_metrics(
        redis,
        conv_id="conv-prom-1",
        trigger="manual",
        outcome="ok",
    )
    assert _counter_value("manual", "manual", "ok") == before + 1.0


@pytest.mark.asyncio
async def test_compaction_counter_increments_on_lock_busy_via_record_metrics() -> None:
    """Direct call with outcome='lock_busy' should still bump the counter."""
    redis = _FakeRedis()

    before = _counter_value("token_limit", "auto", "lock_busy")
    await context_summary.record_summary_metrics(
        redis,
        conv_id="conv-prom-1",
        trigger="auto",
        outcome="lock_busy",
    )
    assert _counter_value("token_limit", "auto", "lock_busy") == before + 1.0


def test_metrics_are_registered_with_expected_names() -> None:
    """Sanity: P1-3 spec says new metric names must use the lumen_context_* namespace."""
    # Why: contract test — if someone renames the metric, dashboards / alerts break.
    # Note: prometheus_client Counter strips the '_total' suffix from _name (it
    # is appended automatically when exposed at /metrics); Histogram keeps the
    # full name. Source: prometheus_client/metrics.py Counter.__init__.
    assert context_compaction_total._name == "lumen_context_compaction"
    assert (
        context_compaction_duration_seconds._name
        == "lumen_context_compaction_duration_seconds"
    )
    # And label sets must match what the design doc specifies.
    assert tuple(context_compaction_total._labelnames) == ("reason", "trigger", "outcome")
    assert tuple(context_compaction_duration_seconds._labelnames) == ("reason", "outcome")
    # Sanity-check that at least one bucket lands in the typical 5 s range —
    # this is what we expect average compactions to land near, so it must exist
    # in the configured buckets.
    upper_bounds = getattr(context_compaction_duration_seconds, "_upper_bounds", ())
    assert 5.0 in tuple(upper_bounds)


def test_summary_models_unchanged() -> None:
    """Guard: SUMMARY_KIND / SUMMARY_VERSION imports remain so tests stay aligned."""
    # Why: keep the import non-dead so any future refactor that drops these
    # constants surfaces here loud and early.
    assert isinstance(SUMMARY_KIND, str)
    assert isinstance(SUMMARY_VERSION, int)
