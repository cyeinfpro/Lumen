from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request

from app.routes import events


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/events",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_sanitize_last_event_id_uses_supplied_server_time() -> None:
    assert (
        events._sanitize_last_event_id(  # noqa: SLF001
            "1720000000000-0",
            now_ms=1_720_000_001_000,
        )
        == "1720000000000-0"
    )
    assert (
        events._sanitize_last_event_id(  # noqa: SLF001
            "1720000000000-0",
            now_ms=1_719_999_000_000,
        )
        is None
    )


def test_normalize_recoverable_sse_id_accepts_only_redis_stream_ids() -> None:
    assert (
        events._normalize_recoverable_sse_id("1710000000000-0")  # noqa: SLF001
        == "1710000000000-0"
    )
    assert (
        events._normalize_recoverable_sse_id("live-1710000000000-abc")  # noqa: SLF001
        is None
    )
    assert (
        events._normalize_recoverable_sse_id("dlq-1710000000000-abc")  # noqa: SLF001
        is None
    )
    assert (
        events._normalize_recoverable_sse_id("1710000000000")  # noqa: SLF001
        is None
    )
    assert (
        events._normalize_recoverable_sse_id("1710000000000-x")  # noqa: SLF001
        is None
    )


@pytest.mark.asyncio
async def test_sse_connection_slot_limits_and_releases() -> None:
    class Redis:
        def __init__(self) -> None:
            self.tokens: dict[str, float] = {}
            self.expire_calls = 0

        async def eval(self, script: str, _numkeys: int, key: str, *args: Any) -> int:
            assert key == "sse:connections:user-1"
            if "zcard" in script and "zadd" in script:
                now = float(args[0])
                ttl = int(args[1])
                limit = int(args[2])
                expires_at = float(args[3])
                token = str(args[4])
                assert ttl == 90
                self.tokens = {
                    current_token: expires
                    for current_token, expires in self.tokens.items()
                    if expires > now
                }
                self.expire_calls += 1
                if len(self.tokens) >= limit:
                    return 0
                self.tokens[token] = expires_at
                return 1
            if "zrem" in script:
                token = str(args[0])
                self.tokens.pop(token, None)
                if self.tokens:
                    self.expire_calls += 1
                return 1
            raise AssertionError(f"unexpected script: {script}")

    redis = Redis()

    slot = await events._acquire_sse_connection_slot(redis, "user-1", limit=1)
    assert slot is not None
    assert slot[0] == "sse:connections:user-1"
    assert len(redis.tokens) == 1
    assert redis.expire_calls == 1

    with pytest.raises(HTTPException) as excinfo:
        await events._acquire_sse_connection_slot(redis, "user-1", limit=1)

    assert excinfo.value.status_code == 429
    assert len(redis.tokens) == 1

    await events._release_sse_connection_slot(redis, slot)

    assert redis.tokens == {}


@pytest.mark.asyncio
async def test_sse_connection_slot_refreshes_only_its_own_token() -> None:
    class Redis:
        def __init__(self) -> None:
            self.tokens: dict[str, float] = {"other": time.time() + 60}

        async def eval(self, script: str, _numkeys: int, key: str, *args: Any) -> int:
            assert key == "sse:connections:user-1"
            if "zscore" in script:
                now = float(args[0])
                token = str(args[2])
                expires_at = float(args[3])
                self.tokens = {
                    current_token: expires
                    for current_token, expires in self.tokens.items()
                    if expires > now
                }
                if token not in self.tokens:
                    return 0
                self.tokens[token] = expires_at
                return 1
            if "zrem" in script:
                self.tokens.pop(str(args[0]), None)
                return 1
            raise AssertionError(f"unexpected script: {script}")

    redis = Redis()

    await events._refresh_sse_connection_slot(
        redis,
        ("sse:connections:user-1", "missing"),
    )

    assert set(redis.tokens) == {"other"}

    redis.tokens["mine"] = time.time() + 1
    await events._refresh_sse_connection_slot(redis, ("sse:connections:user-1", "mine"))

    assert set(redis.tokens) == {"other", "mine"}
    assert redis.tokens["mine"] > time.time() + 80


def test_replay_payload_filter_matches_requested_channels() -> None:
    user_channel = "user:user-1"

    assert events._replay_payload_matches_channels(
        {"conversation_id": "conv-1"},
        requested_channels={"conv:conv-1"},
        include_user_channel=False,
        user_channel=user_channel,
    )
    assert not events._replay_payload_matches_channels(
        {"conversation_id": "conv-2"},
        requested_channels={"conv:conv-1"},
        include_user_channel=False,
        user_channel=user_channel,
    )
    assert events._replay_payload_matches_channels(
        {"generation_id": "gen-1"},
        requested_channels={"task:gen-1"},
        include_user_channel=False,
        user_channel=user_channel,
    )
    assert not events._replay_payload_matches_channels(
        {"conversation_id": "conv-2"},
        requested_channels={user_channel},
        include_user_channel=True,
        user_channel=user_channel,
    )
    assert events._replay_payload_matches_channels(
        {"notice": "ok"},
        requested_channels={user_channel},
        include_user_channel=True,
        user_channel=user_channel,
    )
    assert not events._replay_payload_matches_channels(
        {"conversation_id": "conv-1"},
        requested_channels={"conv:conv-1"},
        include_user_channel=False,
        user_channel=user_channel,
        envelope_channel="conv:conv-2",
    )
    assert events._replay_payload_matches_channels(
        {"storyboard_run_id": "storyboard-1"},
        requested_channels={"storyboard:storyboard-1"},
        include_user_channel=False,
        user_channel=user_channel,
    )
    assert not events._replay_payload_matches_channels(
        {"storyboard_run_id": "storyboard-2"},
        requested_channels={"storyboard:storyboard-1"},
        include_user_channel=False,
        user_channel=user_channel,
    )


@pytest.mark.asyncio
async def test_pubsub_event_without_sse_id_is_persisted_for_live_id() -> None:
    class Redis:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def xadd(
            self,
            stream_key: str,
            fields: dict[str, str],
            **kwargs: Any,
        ) -> str:
            self.calls.append(
                {"stream_key": stream_key, "fields": fields, "kwargs": kwargs}
            )
            return "1710000000001-0"

    redis = Redis()

    stream_id = await events._stream_id_for_pubsub_event(  # noqa: SLF001
        redis,
        stream_key="events:user:user-1",
        event_name="generation.completed",
        envelope_event_id="event-1",
        payload={"generation_id": "gen-1"},
    )

    assert stream_id == "1710000000001-0"
    assert redis.calls[0]["stream_key"] == "events:user:user-1"
    assert redis.calls[0]["fields"]["event"] == "generation.completed"
    payload = json.loads(redis.calls[0]["fields"]["data"])
    assert payload["event_id"] == "event-1"
    assert payload["data"]["generation_id"] == "gen-1"


@pytest.mark.asyncio
async def test_pubsub_event_without_sse_id_has_no_id_when_streams_are_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Redis:
        async def xadd(
            self,
            _stream_key: str,
            _fields: dict[str, str],
            **_kwargs: Any,
        ) -> str:
            raise RuntimeError("unknown command")

    with caplog.at_level("WARNING", logger=events.logger.name):
        stream_id = await events._stream_id_for_pubsub_event(  # noqa: SLF001
            Redis(),
            stream_key="events:user:user-1",
            event_name="generation.completed",
            envelope_event_id="event-1",
            payload={"generation_id": "gen-1"},
        )

    assert stream_id is None
    assert "has no recoverable id" in caplog.text
    assert "xadd fallback failed" not in caplog.text


@pytest.mark.asyncio
async def test_pubsub_event_envelope_id_does_not_overwrite_payload_event_id() -> None:
    class Redis:
        def __init__(self) -> None:
            self.fields: dict[str, str] | None = None

        async def xadd(
            self,
            _stream_key: str,
            fields: dict[str, str],
            **_kwargs: Any,
        ) -> str:
            self.fields = fields
            return "1710000000001-0"

    redis = Redis()

    await events._stream_id_for_pubsub_event(  # noqa: SLF001
        redis,
        stream_key="events:user:user-1",
        event_name="generation.completed",
        envelope_event_id="envelope-event",
        payload={"event_id": "payload-event", "generation_id": "gen-1"},
    )

    assert redis.fields is not None
    envelope = json.loads(redis.fields["data"])
    assert envelope["event_id"] == "envelope-event"
    assert envelope["data"]["event_id"] == "payload-event"


@pytest.mark.asyncio
async def test_pubsub_event_fallback_preserves_falsy_envelope_event_id() -> None:
    class Redis:
        def __init__(self) -> None:
            self.fields: dict[str, str] | None = None

        async def xadd(
            self,
            _stream_key: str,
            fields: dict[str, str],
            **_kwargs: Any,
        ) -> str:
            self.fields = fields
            return "1710000000001-0"

    redis = Redis()

    await events._stream_id_for_pubsub_event(  # noqa: SLF001
        redis,
        stream_key="events:user:user-1",
        event_name="generation.completed",
        envelope_event_id=0,  # type: ignore[arg-type]
        payload={"generation_id": "gen-1"},
    )

    assert redis.fields is not None
    envelope = json.loads(redis.fields["data"])
    assert envelope["event_id"] == "0"
    assert redis.fields["event_id"] == "0"


@pytest.mark.asyncio
async def test_events_rejects_too_many_channels_before_subscribing() -> None:
    channels = ",".join(f"task:{i}" for i in range(events.MAX_SSE_CHANNELS + 1))

    with pytest.raises(Exception) as excinfo:
        await events.events(
            _request(),
            SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            channels=channels,
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "too_many_channels"
    assert excinfo.value.detail["error"]["max_channels"] == events.MAX_SSE_CHANNELS
    assert (
        excinfo.value.detail["error"]["requested_count"] == events.MAX_SSE_CHANNELS + 1
    )
    assert (
        excinfo.value.detail["error"]["effective_count"] == events.MAX_SSE_CHANNELS + 1
    )


@pytest.mark.asyncio
async def test_validate_channels_batches_owned_task_queries() -> None:
    class Result:
        def __init__(self, rows: list[str]) -> None:
            self.rows = rows

        def scalars(self):
            return self

        def all(self) -> list[str]:
            return self.rows

    class Db:
        def __init__(self) -> None:
            self.tables: list[str] = []

        async def execute(self, statement):
            sql = str(statement)
            if "FROM conversations" in sql:
                self.tables.append("conversations")
                return Result(["conv-1"])
            if "FROM video_generations" in sql:
                self.tables.append("video_generations")
                return Result(["video-1"])
            if "FROM completions" in sql:
                self.tables.append("completions")
                return Result(["comp-1"])
            if "FROM generations" in sql:
                self.tables.append("generations")
                return Result(["gen-1"])
            return Result([])

    db = Db()

    clean = await events._validate_channels(  # noqa: SLF001
        [
            "user:user-1",
            "conv:conv-1",
            "task:gen-1",
            "task:comp-1",
            "task:video-1",
            "ignored:channel",
        ],
        "user-1",
        db,  # type: ignore[arg-type]
    )

    assert clean == [
        "user:user-1",
        "conv:conv-1",
        "task:gen-1",
        "task:comp-1",
        "task:video-1",
    ]
    assert db.tables == [
        "conversations",
        "generations",
        "completions",
        "video_generations",
    ]


@pytest.mark.asyncio
async def test_validate_channels_accepts_owned_storyboard_run() -> None:
    class Result:
        def __init__(self, rows: list[str]) -> None:
            self.rows = rows

        def scalars(self):
            return self

        def all(self) -> list[str]:
            return self.rows

    class Db:
        def __init__(self) -> None:
            self.tables: list[str] = []

        async def execute(self, statement):
            sql = str(statement)
            if "FROM workflow_runs" in sql:
                self.tables.append("workflow_runs")
                return Result(["storyboard-1"])
            return Result([])

    db = Db()

    clean = await events._validate_channels(  # noqa: SLF001
        ["user:user-1", "storyboard:storyboard-1"],
        "user-1",
        db,  # type: ignore[arg-type]
    )

    assert clean == ["user:user-1", "storyboard:storyboard-1"]
    assert db.tables == ["workflow_runs"]


@pytest.mark.asyncio
async def test_events_logs_replay_failure_and_acloses_pubsub(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class PubSub:
        def __init__(self) -> None:
            self.aclose_called = False
            self.close_called = False

        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            return None

        async def aclose(self) -> None:
            self.aclose_called = True

        async def close(self) -> None:
            self.close_called = True

        async def get_message(self, **_kwargs: Any) -> None:
            return None

    class Redis:
        def __init__(self) -> None:
            self.pubsub_obj = PubSub()

        async def xrevrange(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return [("1710000000000-0", {})]

        async def xread(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("stream unavailable")

        def pubsub(self) -> PubSub:
            return self.pubsub_obj

    # 用当前 ms 构造 Last-Event-ID，否则会被 _sanitize_last_event_id 以
    # "超出 24h 重放窗口" 拒绝，replay 分支被跳过，logger 不会 emit。
    sane_event_id = f"{int(time.time() * 1000)}-0"

    class DisconnectedRequest:
        headers = {"Last-Event-ID": sane_event_id}

        async def is_disconnected(self) -> bool:
            return True

    redis = Redis()
    monkeypatch.setattr(events, "get_redis", lambda: redis)

    response = await events.events(
        DisconnectedRequest(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="",
    )

    with caplog.at_level("WARNING"):
        async for _chunk in response.body_iterator:
            pass

    assert "sse replay failed" in caplog.text
    assert redis.pubsub_obj.aclose_called is True
    assert redis.pubsub_obj.close_called is False


@pytest.mark.asyncio
async def test_live_pubsub_event_preserves_falsy_payload_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PubSub:
        def __init__(self) -> None:
            self.sent = False

        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def get_message(self, **_kwargs: Any) -> dict[str, str] | None:
            if self.sent:
                return None
            self.sent = True
            return {
                "channel": "user:user-1",
                "data": json.dumps(
                    {
                        "event": "generation.completed",
                        "event_id": 0,
                        "data": {"generation_id": "gen-1"},
                    },
                    separators=(",", ":"),
                ),
            }

    class Redis:
        def __init__(self) -> None:
            self.pubsub_obj = PubSub()
            self.xadd_fields: dict[str, str] | None = None

        def pubsub(self) -> PubSub:
            return self.pubsub_obj

        async def xadd(
            self,
            _stream_key: str,
            fields: dict[str, str],
            **_kwargs: Any,
        ) -> str:
            self.xadd_fields = fields
            return "1710000000001-0"

    class Request:
        headers: dict[str, str] = {}
        checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return self.checks > 1

    async def fake_validate_channels(
        channels: list[str],
        _user_id: str,
        _db: Any,
    ) -> list[str]:
        return channels

    redis = Redis()
    monkeypatch.setattr(events, "_validate_channels", fake_validate_channels)
    monkeypatch.setattr(events, "get_redis", lambda: redis)

    response = await events.events(
        Request(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="user:user-1",
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    assert chunks == [
        {
            "id": "1710000000001-0",
            "event": "generation.completed",
            "data": (
                '{"generation_id":"gen-1","msg_id":"1710000000001-0",'
                '"sse_id":"1710000000001-0","event_id":"0"}'
            ),
        }
    ]
    assert redis.xadd_fields is not None
    envelope = json.loads(redis.xadd_fields["data"])
    assert envelope["event_id"] == "0"


@pytest.mark.asyncio
async def test_events_replay_uses_last_event_id_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class PubSub:
        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def get_message(self, **_kwargs: Any) -> None:
            return None

    class Redis:
        async def xrevrange(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return [("1710000000000-0", {})]

        def pubsub(self) -> PubSub:
            return PubSub()

    class RequestWithoutHeader:
        headers: dict[str, str] = {}

        async def is_disconnected(self) -> bool:
            return True

    async def fake_validate_channels(
        channels: list[str],
        _user_id: str,
        _db: Any,
    ) -> list[str]:
        return channels

    async def fake_iter_replay_events(*_args: Any, **kwargs: Any):
        seen["last_event_id"] = kwargs["last_event_id"]
        if False:
            yield {}

    async def fake_acquire_sse_connection_slot(*_args: Any, **_kwargs: Any) -> None:
        return None

    sane_event_id = f"{int(time.time() * 1000)}-0"
    monkeypatch.setattr(events, "_validate_channels", fake_validate_channels)
    monkeypatch.setattr(events, "_iter_replay_events", fake_iter_replay_events)
    monkeypatch.setattr(
        events,
        "_acquire_sse_connection_slot",
        fake_acquire_sse_connection_slot,
    )
    monkeypatch.setattr(events, "get_redis", lambda: Redis())

    response = await events.events(
        RequestWithoutHeader(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="user:user-1",
        last_event_id_query=sane_event_id,
    )

    async for _chunk in response.body_iterator:
        pass

    assert seen == {"last_event_id": sane_event_id}


@pytest.mark.asyncio
async def test_events_replay_subscribes_before_high_water_and_deduplicates_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_id = int(time.time() * 1000)
    last_event_id = f"{base_id - 1}-0"
    replay_id = f"{base_id}-0"
    high_water_id = f"{base_id + 1}-0"
    live_id = f"{base_id + 2}-0"

    def stream_fields(generation_id: str) -> dict[str, str]:
        return {
            "event": "generation.completed",
            "data": json.dumps(
                {
                    "event": "generation.completed",
                    "channel": "user:user-1",
                    "data": {"generation_id": generation_id},
                },
                separators=(",", ":"),
            ),
        }

    def pubsub_payload(stream_id: str, generation_id: str) -> str:
        return json.dumps(
            {
                "event": "generation.completed",
                "channel": "user:user-1",
                "sse_id": stream_id,
                "data": {"generation_id": generation_id},
            },
            separators=(",", ":"),
        )

    class PubSub:
        def __init__(self) -> None:
            self.messages = []
            self.subscribed = False
            self.unsubscribe_called = False
            self.aclose_called = False

        async def subscribe(self, *_channels: str) -> None:
            self.subscribed = True

        async def unsubscribe(self, *_channels: str) -> None:
            self.unsubscribe_called = True

        async def aclose(self) -> None:
            self.aclose_called = True

        async def get_message(self, **_kwargs: Any) -> dict[str, str] | None:
            if self.messages:
                return self.messages.pop(0)
            return None

    class Redis:
        def __init__(self) -> None:
            self.pubsub_obj = PubSub()
            self.calls: list[str] = []

        def pubsub(self) -> PubSub:
            return self.pubsub_obj

        async def xrevrange(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            assert self.pubsub_obj.subscribed is True
            self.calls.append("xrevrange")
            self.pubsub_obj.messages.extend(
                [
                    {
                        "channel": "user:user-1",
                        "data": pubsub_payload(high_water_id, "race"),
                    },
                    {
                        "channel": "user:user-1",
                        "data": pubsub_payload(live_id, "live"),
                    },
                ]
            )
            return [(high_water_id.encode("ascii"), {})]

        async def xread(self, cursor: dict[str, str], **_kwargs: Any) -> list[Any]:
            self.calls.append("xread")
            assert cursor == {"events:user:user-1": last_event_id}
            stream_key = "events:user:user-1"
            return [
                (
                    stream_key,
                    [
                        (replay_id, stream_fields("replay")),
                        (high_water_id, stream_fields("race")),
                        # This entry was created after the captured high water.
                        (live_id, stream_fields("live")),
                    ],
                )
            ]

    class Request:
        headers = {"Last-Event-ID": last_event_id}
        checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return self.checks >= 3

    async def fake_validate_channels(
        channels: list[str],
        _user_id: str,
        _db: Any,
    ) -> list[str]:
        return channels

    async def fake_acquire_sse_connection_slot(
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        return None

    redis = Redis()
    monkeypatch.setattr(events, "_validate_channels", fake_validate_channels)
    monkeypatch.setattr(
        events,
        "_acquire_sse_connection_slot",
        fake_acquire_sse_connection_slot,
    )
    monkeypatch.setattr(events, "get_redis", lambda: redis)

    response = await events.events(
        Request(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="user:user-1",
    )

    chunks = [chunk async for chunk in response.body_iterator]

    assert [chunk["id"] for chunk in chunks] == [replay_id, high_water_id, live_id]
    assert [json.loads(chunk["data"])["generation_id"] for chunk in chunks] == [
        "replay",
        "race",
        "live",
    ]
    assert redis.calls == ["xrevrange", "xread"]
    assert redis.pubsub_obj.unsubscribe_called is True
    assert redis.pubsub_obj.aclose_called is True


@pytest.mark.asyncio
async def test_events_replay_cancellation_still_closes_pubsub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PubSub:
        def __init__(self) -> None:
            self.unsubscribe_called = False
            self.aclose_called = False

        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            self.unsubscribe_called = True

        async def aclose(self) -> None:
            self.aclose_called = True

    class Redis:
        def __init__(self) -> None:
            self.pubsub_obj = PubSub()

        def pubsub(self) -> PubSub:
            return self.pubsub_obj

        async def xrevrange(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return [("1710000000000-0", {})]

        async def xread(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            raise asyncio.CancelledError()

    class Request:
        headers = {"Last-Event-ID": f"{int(time.time() * 1000)}-0"}

        async def is_disconnected(self) -> bool:
            return False

    async def fake_validate_channels(
        channels: list[str],
        _user_id: str,
        _db: Any,
    ) -> list[str]:
        return channels

    async def fake_acquire_sse_connection_slot(
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        return None

    redis = Redis()
    monkeypatch.setattr(events, "_validate_channels", fake_validate_channels)
    monkeypatch.setattr(
        events,
        "_acquire_sse_connection_slot",
        fake_acquire_sse_connection_slot,
    )
    monkeypatch.setattr(events, "get_redis", lambda: redis)

    response = await events.events(
        Request(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="user:user-1",
    )

    with pytest.raises(asyncio.CancelledError):
        await response.body_iterator.__anext__()

    assert redis.pubsub_obj.unsubscribe_called is True
    assert redis.pubsub_obj.aclose_called is True


@pytest.mark.asyncio
async def test_sse_replay_does_not_block_when_cursor_is_latest() -> None:
    class Redis:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None

        async def xread(self, *_args: Any, **kwargs: Any) -> list[Any]:
            self.kwargs = kwargs
            return []

    redis = Redis()
    events_seen = [
        item
        async for item in events._iter_replay_events(
            redis,
            stream_key="events:user:user-1",
            last_event_id="1710000000000-0",
            requested_channels=set(),
            include_user_channel=False,
            user_channel="user:user-1",
        )
    ]

    assert events_seen == []
    assert redis.kwargs is not None
    assert "block" not in redis.kwargs


@pytest.mark.asyncio
async def test_sse_replay_pages_until_stream_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events, "_REPLAY_BATCH_SIZE", 2)

    class Redis:
        def __init__(self) -> None:
            self.calls = 0

        async def xread(self, cursor: dict[str, str], **_kwargs: Any) -> list[Any]:
            self.calls += 1
            stream_key, last_id = next(iter(cursor.items()))
            if last_id == "0-0":
                return [
                    (
                        stream_key,
                        [
                            (
                                "1-0",
                                {
                                    "event": "completion.delta",
                                    "data": '{"data":{"completion_id":"c1","text_delta":"a"}}',
                                },
                            ),
                            (
                                "2-0",
                                {
                                    "event": "completion.delta",
                                    "data": '{"data":{"completion_id":"c1","text_delta":"b"}}',
                                },
                            ),
                        ],
                    )
                ]
            if last_id == "2-0":
                return [
                    (
                        stream_key,
                        [
                            (
                                "3-0",
                                {
                                    "event": "completion.delta",
                                    "data": '{"data":{"completion_id":"c1","text_delta":"c"}}',
                                },
                            )
                        ],
                    )
                ]
            return []

    redis = Redis()
    events_seen = [
        item
        async for item in events._iter_replay_events(
            redis,
            stream_key="events:user:user-1",
            last_event_id="0-0",
            requested_channels={"task:c1"},
            include_user_channel=False,
            user_channel="user:user-1",
        )
    ]

    assert [item["id"] for item in events_seen] == ["1-0", "2-0", "3-0"]
    assert [json.loads(item["data"])["sse_id"] for item in events_seen] == [
        "1-0",
        "2-0",
        "3-0",
    ]
    assert redis.calls == 2


@pytest.mark.asyncio
async def test_sse_replay_truncated_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events, "_REPLAY_BATCH_SIZE", 2)
    monkeypatch.setattr(events, "_REPLAY_MAX_EVENTS", 2)

    class Redis:
        async def xread(self, cursor: dict[str, str], **_kwargs: Any) -> list[Any]:
            stream_key, last_id = next(iter(cursor.items()))
            assert last_id == "0-0"
            return [
                (
                    stream_key,
                    [
                        (
                            "1-0",
                            {
                                "event": "completion.delta",
                                "data": '{"data":{"completion_id":"c1","text_delta":"a"}}',
                            },
                        ),
                        (
                            "2-0",
                            {
                                "event": "completion.delta",
                                "data": '{"data":{"completion_id":"c1","text_delta":"b"}}',
                            },
                        ),
                    ],
                )
            ]

    events_seen = [
        item
        async for item in events._iter_replay_events(
            Redis(),
            stream_key="events:user:user-1",
            last_event_id="0-0",
            requested_channels={"task:c1"},
            include_user_channel=False,
            user_channel="user:user-1",
        )
    ]

    truncated = events_seen[-1]
    assert truncated["event"] == "replay_truncated"
    assert truncated["id"] == "2-0"
    assert json.loads(truncated["data"])["cursor"] == "2-0"


@pytest.mark.asyncio
async def test_events_closes_after_replay_truncation_before_live_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(time.time() * 1000)

    async def fake_validate_channels(
        channels: list[str],
        _user_id: str,
        _db: Any,
    ) -> list[str]:
        return channels

    async def fake_replay_connection_events(*_args: Any, **_kwargs: Any):
        yield {
            "id": f"{now_ms + 1}-0",
            "event": "replay_truncated",
            "data": json.dumps(
                {
                    "reason": "too_many_events",
                    "limit": 2,
                    "cursor": f"{now_ms + 1}-0",
                }
            ),
        }

    async def fake_acquire_sse_connection_slot(*_args: Any, **_kwargs: Any) -> None:
        return None

    class PubSub:
        def __init__(self) -> None:
            self.get_message_calls = 0
            self.unsubscribe_called = False
            self.aclose_called = False

        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            self.unsubscribe_called = True

        async def aclose(self) -> None:
            self.aclose_called = True

        async def get_message(self, **_kwargs: Any) -> None:
            self.get_message_calls += 1
            raise AssertionError("truncated replay must close before live PubSub")

    class Redis:
        def __init__(self) -> None:
            self.pubsub_obj = PubSub()

        async def time(self) -> tuple[int, int]:
            return (now_ms // 1000, (now_ms % 1000) * 1000)

        def pubsub(self) -> PubSub:
            return self.pubsub_obj

    class ConnectedRequest:
        headers = {"Last-Event-ID": f"{now_ms}-0"}

        async def is_disconnected(self) -> bool:
            return False

    redis = Redis()
    monkeypatch.setattr(events, "_validate_channels", fake_validate_channels)
    monkeypatch.setattr(
        events,
        "_replay_connection_events",
        fake_replay_connection_events,
    )
    monkeypatch.setattr(
        events,
        "_acquire_sse_connection_slot",
        fake_acquire_sse_connection_slot,
    )
    monkeypatch.setattr(events, "get_redis", lambda: redis)

    response = await events.events(
        ConnectedRequest(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="user:user-1",
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(chunks) == 1
    assert chunks[0]["event"] == "replay_truncated"
    assert redis.pubsub_obj.get_message_calls == 0
    assert redis.pubsub_obj.unsubscribe_called is True
    assert redis.pubsub_obj.aclose_called is True


@pytest.mark.asyncio
async def test_events_replay_uses_effective_subscription_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_validate_channels(
        channels: list[str],
        _user_id: str,
        _db: Any,
    ) -> list[str]:
        return channels

    async def fake_iter_replay_events(*_args: Any, **kwargs: Any):
        captured.update(kwargs)
        for event in ():
            yield event

    class PubSub:
        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def get_message(self, **_kwargs: Any) -> None:
            return None

    class Redis:
        async def xrevrange(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return [("1710000000000-0", {})]

        def pubsub(self) -> PubSub:
            return PubSub()

    class DisconnectedRequest:
        headers = {"Last-Event-ID": f"{int(time.time() * 1000)}-0"}

        async def is_disconnected(self) -> bool:
            return True

    monkeypatch.setattr(events, "_validate_channels", fake_validate_channels)
    monkeypatch.setattr(events, "_iter_replay_events", fake_iter_replay_events)
    monkeypatch.setattr(events, "get_redis", lambda: Redis())

    response = await events.events(
        DisconnectedRequest(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="conv:conv-1",
    )
    async for _chunk in response.body_iterator:
        pass

    assert captured["requested_channels"] == {"conv:conv-1"}
    assert captured["include_user_channel"] is False


@pytest.mark.asyncio
async def test_events_replay_validates_last_event_id_with_redis_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_validate_channels(
        channels: list[str],
        _user_id: str,
        _db: Any,
    ) -> list[str]:
        return channels

    async def fake_iter_replay_events(*_args: Any, **kwargs: Any):
        captured["last_event_id"] = kwargs["last_event_id"]
        if False:
            yield {}

    async def fake_acquire_sse_connection_slot(*_args: Any, **_kwargs: Any) -> None:
        return None

    class PubSub:
        async def subscribe(self, *_channels: str) -> None:
            return None

        async def unsubscribe(self, *_channels: str) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def get_message(self, **_kwargs: Any) -> None:
            return None

    class Redis:
        async def time(self) -> tuple[int, int]:
            return (1_720_000_001, 0)

        async def xrevrange(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return [("1720000000000-0", {})]

        def pubsub(self) -> PubSub:
            return PubSub()

    class DisconnectedRequest:
        headers = {"Last-Event-ID": "1720000000000-0"}

        async def is_disconnected(self) -> bool:
            return True

    redis = Redis()
    monkeypatch.setattr(events, "_validate_channels", fake_validate_channels)
    monkeypatch.setattr(events, "_iter_replay_events", fake_iter_replay_events)
    monkeypatch.setattr(
        events,
        "_acquire_sse_connection_slot",
        fake_acquire_sse_connection_slot,
    )
    monkeypatch.setattr(events, "get_redis", lambda: redis)
    monkeypatch.setattr(events.time, "time", lambda: 1.0)

    response = await events.events(
        DisconnectedRequest(),  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        channels="conv:conv-1",
    )
    async for _chunk in response.body_iterator:
        pass

    assert captured["last_event_id"] == "1720000000000-0"


@pytest.mark.asyncio
async def test_sse_replay_includes_user_events_without_leaking_other_channels() -> None:
    class Redis:
        async def xread(self, cursor: dict[str, str], **_kwargs: Any) -> list[Any]:
            stream_key, last_id = next(iter(cursor.items()))
            if last_id != "0-0":
                return []
            return [
                (
                    stream_key,
                    [
                        (
                            "1-0",
                            {
                                "event": "wallet.low_balance",
                                "data": (
                                    '{"event":"wallet.low_balance",'
                                    '"channel":"user:user-1",'
                                    '"data":{"notice":"low"}}'
                                ),
                            },
                        ),
                        (
                            "2-0",
                            {
                                "event": "completion.delta",
                                "data": (
                                    '{"event":"completion.delta",'
                                    '"channel":"conv:conv-2",'
                                    '"data":{"conversation_id":"conv-2",'
                                    '"completion_id":"c2"}}'
                                ),
                            },
                        ),
                        (
                            "3-0",
                            {
                                "event": "conv.message.appended",
                                "data": (
                                    '{"event":"conv.message.appended",'
                                    '"channel":"conv:conv-1",'
                                    '"data":{"conversation_id":"conv-1",'
                                    '"message_id":"m1"}}'
                                ),
                            },
                        ),
                    ],
                )
            ]

    events_seen = [
        item
        async for item in events._iter_replay_events(
            Redis(),
            stream_key="events:user:user-1",
            last_event_id="0-0",
            requested_channels={"conv:conv-1", "user:user-1"},
            include_user_channel=True,
            user_channel="user:user-1",
        )
    ]

    assert [item["id"] for item in events_seen] == ["1-0", "3-0"]
    assert [item["event"] for item in events_seen] == [
        "wallet.low_balance",
        "conv.message.appended",
    ]
