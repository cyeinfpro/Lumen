from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from app import observability, sse_publish


class FakeRedis:
    def __init__(self, *, xadd_failures: int = 0, publish_failures: int = 0) -> None:
        self.xadd_failures = xadd_failures
        self.publish_failures = publish_failures
        self.xadd_calls = 0
        self.publish_calls = 0
        self.published: list[tuple[str, str]] = []
        self.dlq: list[tuple[str, str]] = []

    async def xadd(self, key, fields, **_kwargs):
        self.xadd_calls += 1
        if self.xadd_calls <= self.xadd_failures:
            raise RuntimeError("redis unavailable")
        return "1710000000000-0"

    async def publish(self, channel: str, payload: str):
        self.publish_calls += 1
        if self.publish_calls <= self.publish_failures:
            raise RuntimeError("publish unavailable")
        self.published.append((channel, payload))
        return 1

    async def lpush(self, key: str, payload: str):
        self.dlq.append((key, payload))
        return 1

    async def ltrim(self, *_args):
        return 1


@pytest.mark.asyncio
async def test_publish_event_xadd_retries_use_seconds_not_milliseconds(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def fake_persist_sse_dlq(**_kwargs) -> None:
        raise AssertionError("DLQ persistence should not run after a retry succeeds")

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_persist_sse_dlq", fake_persist_sse_dlq)

    redis = FakeRedis(xadd_failures=2)

    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.requeued",
        {"generation_id": "gen-1"},
    )

    assert sleeps == [0.5, 2.0]
    assert redis.xadd_calls == 3
    assert redis.dlq == []
    assert len(redis.published) == 1

    channel, payload_json = redis.published[0]
    payload = json.loads(payload_json)
    assert channel == "user:user-1"
    assert payload["event"] == "generation.requeued"
    assert payload["sse_id"] == "1710000000000-0"


@pytest.mark.asyncio
async def test_publish_event_dlq_payload_has_fallback_sse_id(monkeypatch):
    persisted: dict = {}

    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_persist_sse_dlq(**kwargs) -> None:
        persisted.update(kwargs)

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_persist_sse_dlq", fake_persist_sse_dlq)

    redis = FakeRedis(xadd_failures=3)

    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.failed",
        {"generation_id": "gen-1"},
    )

    assert len(redis.dlq) == 1
    payload = json.loads(redis.dlq[0][1])
    assert payload["sse_id"].startswith("dlq-")
    assert persisted["payload"]["envelope"]["sse_id"] == payload["sse_id"]


@pytest.mark.asyncio
async def test_publish_event_publish_retry_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    redis = FakeRedis(publish_failures=1)

    with caplog.at_level("WARNING", logger=sse_publish.logger.name):
        await sse_publish.publish_event(
            redis,
            "user-1",
            "user:user-1",
            "generation.progress",
            {"generation_id": "gen-1"},
        )

    assert redis.publish_calls == 2
    assert "PUBLISH retry" in caplog.text


def test_worker_sentry_before_send_scrubs_worker_pii() -> None:
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer secret",
                "X-Requester": "user@example.com",
            },
            "cookies": {"session": "secret"},
            "data": {
                "prompt": "draw user@example.com",
                "image_url": "https://cdn.example.test/private.png",
                "nested": {"api_key": "sk-test", "note": "owner@example.com"},
            },
            "query_string": "email=user@example.com",
        },
        "extra": {
            "system_prompt": "private instructions",
            "data_url": "data:image/png;base64,abc",
            "safe_note": "contact user@example.com",
        },
        "user": {
            "id": "user-1",
            "email": "user@example.com",
            "username": "alice",
            "ip_address": "127.0.0.1",
        },
    }

    scrubbed = observability._sentry_before_send(event, {})

    assert scrubbed["request"]["headers"]["Authorization"] == "[redacted]"
    assert scrubbed["request"]["headers"]["X-Requester"] == "[email]"
    assert scrubbed["request"]["cookies"] == "[redacted]"
    assert scrubbed["request"]["data"]["prompt"] == "[redacted]"
    assert scrubbed["request"]["data"]["image_url"] == "[redacted]"
    assert scrubbed["request"]["data"]["nested"]["api_key"] == "[redacted]"
    assert scrubbed["request"]["data"]["nested"]["note"] == "[email]"
    assert scrubbed["request"]["query_string"] == "email=[email]"
    assert scrubbed["extra"]["system_prompt"] == "[redacted]"
    assert scrubbed["extra"]["data_url"] == "[redacted]"
    assert scrubbed["extra"]["safe_note"] == "contact [email]"
    assert scrubbed["user"] == {
        "id": "user-1",
        "email": "[redacted]",
        "username": "[redacted]",
        "ip_address": "[redacted]",
    }


def test_worker_init_sentry_disables_default_pii(monkeypatch) -> None:
    init_kwargs: dict = {}
    fake_sentry = SimpleNamespace(init=lambda **kwargs: init_kwargs.update(kwargs))

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sentry)

    observability.init_sentry("https://example.invalid/1", "prod", 0.25)

    assert init_kwargs["send_default_pii"] is False
    assert init_kwargs["before_send"] is observability._sentry_before_send
    assert init_kwargs["before_breadcrumb"] is observability._sentry_before_breadcrumb
