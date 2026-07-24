from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from redis.exceptions import WatchError

from app import db as worker_db
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


class SimplePipeline:
    def __init__(self, redis) -> None:
        self.redis = redis
        self.watched: dict[str, str | None] = {}
        self.commands: list[tuple[str, tuple, dict]] = []

    async def watch(self, *keys: str) -> None:
        self.watched = {key: await self.redis.get(key) for key in keys}

    async def get(self, key: str):
        return await self.redis.get(key)

    def multi(self) -> None:
        return None

    def delete(self, key: str) -> None:
        self.commands.append(("delete", (key,), {}))

    def set(self, key: str, value: str, **kwargs) -> None:
        self.commands.append(("set", (key, value), kwargs))

    def xadd(self, key: str, fields: dict, **kwargs) -> None:
        self.commands.append(("xadd", (key, fields), kwargs))

    def expire(self, key: str, ttl: int) -> None:
        self.commands.append(("expire", (key, ttl), {}))

    async def execute(self) -> list:
        for key, watched_value in self.watched.items():
            if await self.redis.get(key) != watched_value:
                raise WatchError("watched owner changed")
        return [
            await getattr(self.redis, command)(*args, **kwargs)
            for command, args, kwargs in self.commands
        ]

    async def reset(self) -> None:
        return None


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
        existing = self.kv.get(dedupe_key)
        if existing is not None:
            if sse_publish._has_stream_id(existing):
                return existing
            raise RuntimeError(sse_publish._DEDUPE_RESERVATION_PENDING_ERROR)
        self.kv[dedupe_key] = _args[-1]
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

    def pipeline(self, *, transaction: bool = True) -> SimplePipeline:
        assert transaction is True
        return SimplePipeline(self)


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

    def pipeline(self, *, transaction: bool = True) -> SimplePipeline:
        assert transaction is True
        return SimplePipeline(self)


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
    assert redis.deleted == []
    assert redis.kv[dedupe_key] == "1710000000000-0"
    assert len(redis.stream_entries) == 1
    payload = json.loads(redis.published[0][1])
    assert payload["sse_id"] == "1710000000000-0"


@pytest.mark.asyncio
async def test_publish_event_raises_when_xadd_fails_even_if_dlq_succeeds(
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

    with pytest.raises(sse_publish.SSEPublishRetryableError) as exc_info:
        await sse_publish.publish_event(
            redis,
            "user-1",
            "user:user-1",
            "generation.progress",
            {"generation_id": "gen-1"},
        )

    dedupe_key = next(iter(redis.kv))
    assert redis.xadd_calls == 6
    assert redis.deleted == [dedupe_key, dedupe_key]
    assert redis.kv[dedupe_key].startswith(sse_publish._DEDUPE_RESERVATION_PREFIX)
    assert redis.stream_entries == []
    assert len(redis.dlq) == 1
    dlq_payload = json.loads(redis.dlq[0][1])
    assert "sse_id" not in dlq_payload
    assert dlq_payload["recoverable"] is False
    assert dlq_payload["dlq_id"].startswith("dlq-")
    assert persisted["payload"]["envelope"]["dlq_id"] == dlq_payload["dlq_id"]
    assert redis.publish_calls == 0
    assert exc_info.value.stream_key == "events:user:user-1"
    assert exc_info.value.event_id == dlq_payload["event_id"]
    assert exc_info.value.diagnostic_dlq_persisted is True


def test_lua_xadd_establishes_stream_ttl_before_returning_stream_id() -> None:
    lua = " ".join(sse_publish._XADD_IDEMPOTENT_LUA.split())

    xadd_index = lua.index("local stream_id = redis.call( 'XADD'")
    expire_index = lua.index("local ttl_set = redis.call('EXPIRE'")
    store_index = lua.index("redis.call('SET', KEYS[2], stream_id")

    assert xadd_index < expire_index < store_index
    assert "redis.call('SET', KEYS[2], ARGV[8], 'NX', 'EX'" in lua
    assert "redis.call('XDEL', KEYS[1], stream_id)" in lua


class TransactionFallbackRedis(FakeRedis):
    eval = None

    def __init__(self) -> None:
        super().__init__()
        self.kv: dict[str, str] = {}

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
        return 1 if self.kv.pop(key, None) is not None else 0

    async def pttl(self, _key: str) -> int:
        return (
            sse_publish._EVENTS_DEDUPE_TTL_SECONDS * 1000
            - int(sse_publish._DEDUPE_RESERVATION_STALE_SECONDS * 1000)
            - 1
        )

    async def xrevrange(self, key: str, *, count: int):
        _ = count
        return [
            (f"1710000000000-{idx}", fields)
            for idx, (stream_key, fields) in reversed(
                list(enumerate(self.stream_entries))
            )
            if stream_key == key
        ]

    def pipeline(self, *, transaction: bool = True) -> SimplePipeline:
        assert transaction is True
        return SimplePipeline(self)


@pytest.mark.asyncio
async def test_fallback_recovers_atomic_xadd_ttl_after_response_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AcceptedThenRaisedPipeline(SimplePipeline):
        async def execute(self) -> list:
            results = await super().execute()
            if any(command == "xadd" for command, _args, _kwargs in self.commands):
                if not self.redis.response_lost:
                    self.redis.response_lost = True
                    raise RuntimeError("connection dropped after EXEC")
            return results

    class Redis(TransactionFallbackRedis):
        def __init__(self) -> None:
            super().__init__()
            self.response_lost = False

        def pipeline(self, *, transaction: bool = True) -> SimplePipeline:
            assert transaction is True
            return AcceptedThenRaisedPipeline(self)

    monkeypatch.setattr(sse_publish, "_DEDUPE_RESERVATION_WAIT_SECONDS", 0.0)
    redis = Redis()
    kwargs = {
        "stream_key": "events:user:user-1",
        "event_name": "generation.progress",
        "event_id": "evt-atomic",
        "payload_json": json.dumps({"event_id": "evt-atomic"}),
    }

    with pytest.raises(RuntimeError, match="connection dropped after EXEC"):
        await sse_publish._xadd_event_without_lua(
            redis,
            reservation_token="pending:first-owner",
            **kwargs,
        )

    stream_id = await sse_publish._xadd_event_without_lua(
        redis,
        reclaim_empty_reservation=True,
        reservation_token="pending:retry-owner",
        **kwargs,
    )

    assert stream_id == "1710000000000-0"
    assert len(redis.stream_entries) == 1
    assert (
        "events:user:user-1",
        sse_publish.EVENTS_STREAM_TTL_SECONDS,
    ) in redis.expirations


@pytest.mark.asyncio
async def test_stale_reclaim_compare_delete_preserves_new_owner_and_skips_xadd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OwnerSwitchPipeline(SimplePipeline):
        async def get(self, key: str):
            value = await super().get(key)
            if not self.redis.owner_switched:
                self.redis.owner_switched = True
                self.redis.kv[key] = "pending:new-owner"
            return value

    class Redis(TransactionFallbackRedis):
        def __init__(self) -> None:
            super().__init__()
            self.owner_switched = False

        def pipeline(self, *, transaction: bool = True) -> SimplePipeline:
            assert transaction is True
            return OwnerSwitchPipeline(self)

    monkeypatch.setattr(sse_publish, "_DEDUPE_RESERVATION_WAIT_SECONDS", 0.0)
    redis = Redis()
    dedupe_key = "events:user:user-1:dedupe:evt-race"
    redis.kv[dedupe_key] = "pending:stale-owner"

    with pytest.raises(RuntimeError, match="reservation has no stream id"):
        await sse_publish._xadd_event_without_lua(
            redis,
            stream_key="events:user:user-1",
            event_name="generation.progress",
            event_id="evt-race",
            payload_json=json.dumps({"event_id": "evt-race"}),
            reclaim_empty_reservation=True,
        )

    assert redis.kv[dedupe_key] == "pending:new-owner"
    assert redis.xadd_calls == 0


class FakeDlqSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.added: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def begin(self):
        return self

    async def execute(self, _stmt):
        rows = self.rows

        class Result:
            def scalars(self):
                return self

            def all(self):
                return rows

        return Result()

    def add(self, row) -> None:
        self.added.append(row)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "new_identity",
    [
        ("evt-distinct", "user-1", "user:user-1"),
        ("evt-existing", "user-2", "user:user-1"),
        ("evt-existing", "user-1", "task:task-2"),
    ],
)
async def test_pg_dlq_dedupe_uses_event_id_user_and_channel(
    monkeypatch: pytest.MonkeyPatch,
    new_identity: tuple[str, str, str],
) -> None:
    existing = SimpleNamespace(
        payload={
            "user_id": "user-1",
            "channel": "user:user-1",
            "envelope": {
                "event": "generation.failed",
                "event_id": "evt-existing",
                "ts_ms": 1234,
            },
        }
    )
    session = FakeDlqSession([existing])
    monkeypatch.setattr(worker_db, "SessionLocal", lambda: session)
    event_id, user_id, channel = new_identity

    persisted = await sse_publish._persist_sse_dlq(
        event_name="generation.failed",
        payload={
            "user_id": user_id,
            "channel": channel,
            "envelope": {
                "event": "generation.failed",
                "event_id": event_id,
                "ts_ms": 1234,
            },
        },
        error_class="XADDFailed",
        error_message="failed",
    )

    assert persisted is True
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_pg_dlq_dedupe_skips_only_exact_stable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "user_id": "user-1",
        "channel": "user:user-1",
        "envelope": {
            "event": "generation.failed",
            "event_id": "evt-same",
            "ts_ms": 1234,
        },
    }
    session = FakeDlqSession([SimpleNamespace(payload=payload)])
    monkeypatch.setattr(worker_db, "SessionLocal", lambda: session)

    persisted = await sse_publish._persist_sse_dlq(
        event_name="generation.failed",
        payload=payload,
        error_class="XADDFailed",
        error_message="failed",
    )

    assert persisted is True
    assert session.added == []


@pytest.mark.asyncio
async def test_publish_event_records_diagnostic_dlq_before_retryable_failure(
    monkeypatch,
):
    persisted: dict = {}

    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_persist_sse_dlq(**kwargs) -> bool:
        persisted.update(kwargs)
        return True

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_persist_sse_dlq", fake_persist_sse_dlq)

    redis = FakeRedis(xadd_failures=3)

    with pytest.raises(sse_publish.SSEPublishRetryableError) as exc_info:
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
    assert redis.publish_calls == 0
    assert exc_info.value.diagnostic_dlq_persisted is True


@pytest.mark.asyncio
async def test_publish_event_reports_when_diagnostic_sinks_also_fail(monkeypatch):
    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_persist_sse_dlq(**_kwargs) -> bool:
        return False

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_persist_sse_dlq", fake_persist_sse_dlq)

    redis = FakeRedis(xadd_failures=3, dlq_failures=1)

    with pytest.raises(sse_publish.SSEPublishRetryableError) as exc_info:
        await sse_publish.publish_event(
            redis,
            "user-1",
            "user:user-1",
            "generation.failed",
            {"generation_id": "gen-1"},
        )

    assert redis.publish_calls == 0
    assert exc_info.value.diagnostic_dlq_persisted is False


@pytest.mark.asyncio
async def test_publish_event_retry_reuses_event_id_and_stream_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_persist_sse_dlq(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_persist_sse_dlq", fake_persist_sse_dlq)
    redis = FakeRedis(xadd_failures=3)
    data = {"generation_id": "gen-1", "event_id": "evt-stable"}

    with pytest.raises(sse_publish.SSEPublishRetryableError):
        await sse_publish.publish_event(
            redis,
            "user-1",
            "user:user-1",
            "generation.progress",
            data,
        )

    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.progress",
        data,
    )
    await sse_publish.publish_event(
        redis,
        "user-1",
        "user:user-1",
        "generation.progress",
        data,
    )

    assert len(redis.stream_entries) == 1
    assert redis.stream_entries[0][1]["event_id"] == "evt-stable"
    successful_payloads = [json.loads(payload) for _channel, payload in redis.published]
    assert [payload["event_id"] for payload in successful_payloads] == [
        "evt-stable",
        "evt-stable",
    ]
    assert successful_payloads[0]["sse_id"] == successful_payloads[1]["sse_id"]


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
                "X-Lumen-Upstream-Authorization": "Bearer provider-secret",
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
    assert (
        scrubbed["request"]["headers"]["X-Lumen-Upstream-Authorization"]
        == "[redacted]"
    )
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
