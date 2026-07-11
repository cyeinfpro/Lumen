from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from app import observability, sse_publish


class FakeRedis:
    def __init__(
        self,
        *,
        xadd_failures: int = 0,
        publish_failures: int = 0,
        dlq_failures: int = 0,
    ) -> None:
        self.xadd_failures = xadd_failures
        self.publish_failures = publish_failures
        self.dlq_failures = dlq_failures
        self.xadd_calls = 0
        self.publish_calls = 0
        self.dlq_calls = 0
        self.published: list[tuple[str, str]] = []
        self.dlq: list[tuple[str, str]] = []
        self.dedupe: dict[str, str] = {}
        self.stream_entries: list[tuple[str, dict]] = []
        self.expirations: list[tuple[str, int]] = []

    async def xadd(self, key, fields, **_kwargs):
        self.xadd_calls += 1
        if self.xadd_calls <= self.xadd_failures:
            raise RuntimeError("redis unavailable")
        self.stream_entries.append((key, dict(fields)))
        return "1710000000000-0"

    async def eval(
        self,
        _lua: str,
        _num_keys: int,
        stream_key: str,
        _dedupe_key: str,
        event_id: str,
        event_name: str,
        payload_json: str,
        *_args: str,
    ):
        self.xadd_calls += 1
        if self.xadd_calls <= self.xadd_failures:
            raise RuntimeError("redis unavailable")
        existing = self.dedupe.get(event_id)
        if existing is not None:
            return existing
        stream_id = f"1710000000000-{len(self.stream_entries)}"
        self.stream_entries.append(
            (
                stream_key,
                {"event": event_name, "data": payload_json, "event_id": event_id},
            )
        )
        self.dedupe[event_id] = stream_id
        return stream_id

    async def publish(self, channel: str, payload: str):
        self.publish_calls += 1
        if self.publish_calls <= self.publish_failures:
            raise RuntimeError("publish unavailable")
        self.published.append((channel, payload))
        return 1

    async def lpush(self, key: str, payload: str):
        self.dlq_calls += 1
        if self.dlq_calls <= self.dlq_failures:
            raise RuntimeError("dlq unavailable")
        self.dlq.append((key, payload))
        return 1

    async def ltrim(self, *_args):
        return 1

    async def expire(self, key: str, ttl: int) -> int:
        self.expirations.append((key, ttl))
        return 1


def test_metrics_server_closes_bound_socket_when_thread_start_fails(monkeypatch):
    class FakeHttpd:
        closed = False

        def serve_forever(self) -> None:
            raise AssertionError("thread should not run target")

        def server_close(self) -> None:
            self.closed = True

    class BrokenThread:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread failed")

    httpd = FakeHttpd()
    monkeypatch.setattr(observability, "_metrics_server_started", False)
    monkeypatch.setattr(observability, "_metrics_httpd", None)
    monkeypatch.setattr(observability, "_metrics_thread", None)
    monkeypatch.setattr(
        observability,
        "make_server",
        lambda *_args, **_kwargs: httpd,
    )
    monkeypatch.setattr(observability.threading, "Thread", BrokenThread)

    with pytest.raises(RuntimeError, match="metrics server failed"):
        observability.start_metrics_server(9101)

    assert httpd.closed is True
    assert observability._metrics_httpd is None
    assert observability._metrics_server_started is False


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
    assert redis.expirations == [
        ("events:user:user-1", sse_publish.EVENTS_STREAM_TTL_SECONDS)
    ]
    assert len(redis.published) == 1

    channel, payload_json = redis.published[0]
    payload = json.loads(payload_json)
    assert channel == "user:user-1"
    assert payload["event"] == "generation.requeued"
    assert payload["sse_id"] == "1710000000000-0"
    assert isinstance(payload["event_id"], str)


@pytest.mark.asyncio
async def test_publish_event_preserves_falsy_payload_event_id() -> None:
    redis = FakeRedis()

    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.started",
        {"generation_id": "gen-1", "event_id": 0},
    )

    assert redis.stream_entries[0][1]["event_id"] == "0"
    payload = json.loads(redis.published[0][1])
    assert payload["event_id"] == "0"
    assert payload["data"]["event_id"] == 0


class AcceptedThenRaisedRedis(FakeRedis):
    async def eval(
        self,
        _lua: str,
        _num_keys: int,
        stream_key: str,
        _dedupe_key: str,
        event_id: str,
        event_name: str,
        payload_json: str,
        *_args: str,
    ):
        self.xadd_calls += 1
        existing = self.dedupe.get(event_id)
        if existing is not None:
            return existing
        stream_id = "1710000000000-42"
        self.stream_entries.append(
            (
                stream_key,
                {"event": event_name, "data": payload_json, "event_id": event_id},
            )
        )
        self.dedupe[event_id] = stream_id
        raise RuntimeError("connection dropped after server accepted xadd")


class GarnetLuaXaddRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.kv: dict[str, str] = {}
        self.deleted: list[str] = []

    async def eval(
        self,
        _lua: str,
        _num_keys: int,
        _stream_key: str,
        dedupe_key: str,
        *_args: str,
    ):
        self.xadd_calls += 1
        self.kv[dedupe_key] = ""
        raise RuntimeError("Unknown Redis command called from script")

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int | None = None,
    ) -> bool:
        _ = ex
        if nx and key in self.kv:
            return False
        if xx and key not in self.kv:
            return False
        self.kv[key] = value
        return True

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1 if self.kv.pop(key, None) is not None else 0


class GarnetNoStreamRedis(GarnetLuaXaddRedis):
    async def xadd(self, *_args, **_kwargs):
        self.xadd_calls += 1
        raise RuntimeError("unknown command")


class FallbackStoreFailureRedis(FakeRedis):
    eval = None

    def __init__(self) -> None:
        super().__init__()
        self.kv: dict[str, str] = {}
        self.store_failures = 1

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int | None = None,
    ) -> bool:
        _ = ex
        if nx and key in self.kv:
            return False
        if xx and key not in self.kv:
            return False
        if xx and self.store_failures:
            self.store_failures -= 1
            raise RuntimeError("connection dropped while storing dedupe stream id")
        self.kv[key] = value
        return True

    async def xrevrange(self, key: str, *, count: int):
        _ = count
        return [
            (f"1710000000000-{idx}", fields)
            for idx, (stream_key, fields) in reversed(
                list(enumerate(self.stream_entries))
            )
            if stream_key == key
        ]


@pytest.mark.asyncio
async def test_publish_event_xadd_retry_is_idempotent_after_accepted_exception(
    monkeypatch,
):
    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    redis = AcceptedThenRaisedRedis()

    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.progress",
        {"generation_id": "gen-1"},
    )

    assert redis.xadd_calls == 2
    assert len(redis.stream_entries) == 1
    payload = json.loads(redis.published[0][1])
    assert payload["sse_id"] == "1710000000000-42"


@pytest.mark.asyncio
async def test_publish_event_fallback_recovers_orphaned_empty_dedupe_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_DEDUPE_RESERVATION_WAIT_SECONDS", 0.0)
    redis = FallbackStoreFailureRedis()

    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.progress",
        {"generation_id": "gen-1", "event_id": "evt-stable"},
    )

    assert redis.xadd_calls == 1
    assert len(redis.stream_entries) == 1
    dedupe_key = "events:user:user-1:dedupe:evt-stable"
    assert redis.kv[dedupe_key] == "1710000000000-0"
    payload = json.loads(redis.published[0][1])
    assert payload["sse_id"] == "1710000000000-0"


@pytest.mark.asyncio
async def test_publish_event_falls_back_when_lua_cannot_xadd() -> None:
    redis = GarnetLuaXaddRedis()

    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.progress",
        {"generation_id": "gen-1"},
    )

    dedupe_key = next(iter(redis.kv))
    assert redis.xadd_calls == 2
    assert redis.deleted == [dedupe_key]
    assert redis.kv[dedupe_key] == "1710000000000-0"
    assert len(redis.stream_entries) == 1
    payload = json.loads(redis.published[0][1])
    assert payload["sse_id"] == "1710000000000-0"


@pytest.mark.asyncio
async def test_publish_event_omits_sse_id_when_stream_commands_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: dict = {}

    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_persist_sse_dlq(**kwargs) -> bool:
        persisted.update(kwargs)
        return True

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_persist_sse_dlq", fake_persist_sse_dlq)

    redis = GarnetNoStreamRedis()

    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.progress",
        {"generation_id": "gen-1"},
    )

    dedupe_key = next(iter(redis.kv))
    assert redis.xadd_calls == 6
    assert redis.deleted == [dedupe_key, dedupe_key, dedupe_key]
    assert redis.kv[dedupe_key] == ""
    assert redis.stream_entries == []
    assert len(redis.dlq) == 1
    dlq_payload = json.loads(redis.dlq[0][1])
    assert "sse_id" not in dlq_payload
    assert dlq_payload["recoverable"] is False
    assert dlq_payload["dlq_id"].startswith("dlq-")
    assert persisted["payload"]["envelope"]["dlq_id"] == dlq_payload["dlq_id"]
    payload = json.loads(redis.published[0][1])
    assert "sse_id" not in payload
    assert payload["recoverable"] is False
    assert payload["dlq_id"] == dlq_payload["dlq_id"]


@pytest.mark.asyncio
async def test_publish_event_dlq_payload_uses_non_recoverable_dlq_id(monkeypatch):
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
    assert "sse_id" not in payload
    assert payload["dlq_id"].startswith("dlq-")
    assert payload["recoverable"] is False
    assert persisted["payload"]["envelope"]["dlq_id"] == payload["dlq_id"]
    assert len(payload["dlq_id"].split("-")) >= 3


@pytest.mark.asyncio
async def test_publish_event_raises_when_all_durable_sinks_fail(monkeypatch):
    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_persist_sse_dlq(**_kwargs) -> bool:
        return False

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_persist_sse_dlq", fake_persist_sse_dlq)

    redis = FakeRedis(xadd_failures=3, dlq_failures=1)

    with pytest.raises(RuntimeError, match="no durable sink"):
        await sse_publish.publish_event(
            redis,
            "user-1",
            "user:user-1",
            "generation.failed",
            {"generation_id": "gen-1"},
        )

    assert redis.publish_calls == 0


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
