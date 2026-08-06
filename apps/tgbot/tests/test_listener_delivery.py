from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import listener  # noqa: E402


class ActiveUsersPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def zremrangebyscore(self, *args: object) -> "ActiveUsersPipeline":
        self.calls.append(("zremrangebyscore", *args))
        return self

    def zrangebyscore(self, *args: object) -> "ActiveUsersPipeline":
        self.calls.append(("zrangebyscore", *args))
        return self

    async def execute(self) -> list[object]:
        return [1, [b"user-1", b"user-2", b""]]


class ActiveUsersRedis:
    def __init__(self) -> None:
        self.pipe = ActiveUsersPipeline()

    def pipeline(self, *, transaction: bool) -> ActiveUsersPipeline:
        assert transaction is False
        return self.pipe

    async def set(self, key: str, _value: object, **kwargs: object) -> bool:
        assert key == listener._FALLBACK_SCAN_LEASE_KEY
        assert kwargs == {
            "nx": True,
            "ex": listener._FALLBACK_SCAN_LEASE_SECONDS,
        }
        return False


class EmptyActivePipeline:
    def zremrangebyscore(self, *_args: object) -> "EmptyActivePipeline":
        return self

    def zrangebyscore(self, *_args: object) -> "EmptyActivePipeline":
        return self

    async def execute(self) -> list[object]:
        return [0, []]


class ExistingTrackerPipeline:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def exists(self, key: str) -> "ExistingTrackerPipeline":
        self.keys.append(key)
        return self

    async def execute(self) -> list[object]:
        return [key.endswith("gen-legacy") for key in self.keys]


class FallbackRedis:
    def __init__(self, *, acquire_lease: bool = True) -> None:
        self.acquire_lease = acquire_lease
        self.pipeline_calls = 0
        self.scan_calls: list[dict[str, object]] = []
        self.xrevrange_calls: list[dict[str, object]] = []
        self.cursor_writes: list[tuple[str, object, dict[str, object]]] = []

    def pipeline(self, *, transaction: bool) -> object:
        assert transaction is False
        self.pipeline_calls += 1
        if self.pipeline_calls == 1:
            return EmptyActivePipeline()
        return ExistingTrackerPipeline()

    async def set(
        self,
        key: str,
        value: object,
        **kwargs: object,
    ) -> bool:
        if key == listener._FALLBACK_SCAN_LEASE_KEY:
            assert value == b"1"
            assert kwargs == {
                "nx": True,
                "ex": listener._FALLBACK_SCAN_LEASE_SECONDS,
            }
            return self.acquire_lease
        self.cursor_writes.append((key, value, kwargs))
        return True

    async def get(self, key: str) -> None:
        assert key in {
            listener._FALLBACK_SCAN_CURSOR_KEY,
            listener._fallback_stream_cursor_key("legacy-user"),
        }
        return None

    async def scan(self, **kwargs: object) -> tuple[int, list[bytes]]:
        if not self.acquire_lease:
            raise AssertionError("lease loser must not scan")
        self.scan_calls.append(kwargs)
        return 0, [b"events:user:legacy-user", b"events:user:web-only:dlq"]

    async def xrevrange(self, key: str, **kwargs: object) -> list[object]:
        self.xrevrange_calls.append({"key": key, **kwargs})
        return [
            (
                b"123-0",
                {
                    b"data": json.dumps(
                        {
                            "event": "generation.progress",
                            "data": {"generation_id": "gen-legacy"},
                        }
                    ).encode(),
                },
            )
        ]


class RecoveringTracker:
    def __init__(self) -> None:
        self.refreshes: list[tuple[str, str]] = []

    async def refresh(self, gen_id: str, user_id: str) -> bool:
        self.refreshes.append((gen_id, user_id))
        return True


class RefreshingDispatchTracker:
    def __init__(self) -> None:
        self.refreshes: list[tuple[str, str]] = []
        self.track = SimpleNamespace(
            chat_id=1,
            status_message_id=2,
            prompt="p",
            batch_id="",
        )

    async def get(self, _gen_id: str) -> object:
        return self.track

    async def refresh(self, gen_id: str, user_id: str) -> bool:
        self.refreshes.append((gen_id, user_id))
        return True


class CursorRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict[str, object]]] = []

    async def set(self, key: str, value: object, **kwargs: object) -> bool:
        self.calls.append((key, value, kwargs))
        return True


class WorkerRedis:
    def __init__(
        self,
        stop_event: asyncio.Event,
        responses: list[tuple[str, str]],
        *,
        fail_quarantine: bool = False,
    ) -> None:
        self.stop_event = stop_event
        self.responses = list(responses)
        self.fail_quarantine = fail_quarantine
        self.cursor: str | None = None
        self.cursor_writes: list[str] = []
        self.attempts: dict[str, int] = {}
        self.quarantine: list[dict[str, str]] = []
        self.quarantined: dict[str, str] = {}
        self.eval_calls = 0

    async def get(self, _key: str) -> str | None:
        return self.cursor

    async def xread(self, **_kwargs: object) -> list[object]:
        if not self.responses:
            self.stop_event.set()
            return []
        entry_id, payload = self.responses.pop(0)
        return [
            (
                b"events:user:user-1",
                [(entry_id.encode(), {b"data": payload.encode()})],
            )
        ]

    async def set(
        self,
        key: str,
        value: object,
        **_kwargs: object,
    ) -> bool:
        assert key == listener._cursor_key_v2("user-1")
        self.cursor = str(value)
        self.cursor_writes.append(str(value))
        return True

    async def incr(self, key: str) -> int:
        self.attempts[key] = self.attempts.get(key, 0) + 1
        return self.attempts[key]

    async def expire(self, _key: str, _ttl: int) -> bool:
        return True

    async def eval(
        self,
        script: str,
        numkeys: int,
        *args: object,
    ) -> int:
        assert script == listener._listener_support._QUARANTINE_AND_ADVANCE_LUA
        assert numkeys == 4
        self.eval_calls += 1
        if self.fail_quarantine:
            raise RuntimeError("quarantine unavailable")
        keys = [str(value) for value in args[:numkeys]]
        argv = [str(value) for value in args[numkeys:]]
        record = {
            "stream": keys[0],
            "source_stream": argv[1],
            "source_id": argv[2],
            "user_id": argv[3],
            "event": argv[4],
            "reason": argv[5],
            "attempts": argv[6],
            "payload": argv[7],
        }
        self.quarantine.append(record)
        self.cursor = argv[2]
        self.quarantined[keys[2]] = argv[2]
        return 1

    async def xlen(self, _key: str) -> int:
        return len(self.quarantine)


class QuarantineApi:
    def __init__(self, *, fail_persist: bool = False) -> None:
        self.fail_persist = fail_persist
        self.persisted: list[dict[str, object]] = []
        self.mirrored: list[tuple[str, str]] = []

    async def persist_delivery_quarantine(
        self,
        **payload: object,
    ) -> dict[str, object]:
        if self.fail_persist:
            raise RuntimeError("quarantine unavailable")
        self.persisted.append(payload)
        return {"quarantine_id": "quarantine-1"}

    async def mark_delivery_quarantine_mirrored(
        self,
        quarantine_id: str,
        redis_stream_id: str,
    ) -> None:
        self.mirrored.append((quarantine_id, redis_stream_id))


class BusyTracker:
    async def begin_delivery(self, _gen_id: str) -> object:
        return SimpleNamespace(state="busy", owner_token=None)


class NotifiedTracker:
    async def begin_delivery(self, _gen_id: str) -> object:
        return SimpleNamespace(state="already_notified", owner_token=None)


class ActiveNotifiedTracker(BusyTracker):
    pass


class RenewingTracker:
    def __init__(self, *, lose: bool = False) -> None:
        self.lose = lose
        self.renewals = 0
        self.renewed_twice = asyncio.Event()

    async def begin_delivery(self, _gen_id: str) -> object:
        return SimpleNamespace(state="acquired", owner_token="r" * 32)

    async def renew_delivery(self, _gen_id: str, _owner_token: str) -> bool:
        self.renewals += 1
        if self.renewals >= 2:
            self.renewed_twice.set()
        return not self.lose


class RecordingTracker:
    def __init__(
        self,
        events: list[object],
        *,
        batch_remaining: int | None = 0,
    ) -> None:
        self.events = events
        self.batch_remaining = batch_remaining
        self.owner_token = "o" * 32

    async def begin_delivery(self, gen_id: str) -> object:
        self.events.append(("begin", gen_id))
        return SimpleNamespace(state="acquired", owner_token=self.owner_token)

    async def renew_delivery(self, gen_id: str, owner_token: str) -> bool:
        self.events.append(("renew", gen_id, owner_token))
        return True

    async def is_notified(self, _gen_id: str) -> bool:
        return False

    async def is_delivery_active(self, _gen_id: str) -> bool:
        return False

    async def mark_notified(self, gen_id: str, *, owner_token: str) -> bool:
        self.events.append(("mark", gen_id, owner_token))
        return True

    async def clear_delivery(self, gen_id: str, *, owner_token: str) -> bool:
        self.events.append(("clear", gen_id, owner_token))
        return True

    async def batch_decr(self, batch_id: str, gen_id: str = "") -> int | None:
        self.events.append(("batch_decr", batch_id, gen_id))
        return self.batch_remaining

    async def batch_remove(self, batch_id: str) -> None:
        self.events.append(("batch_remove", batch_id))


class RecordingBot:
    def __init__(self, events: list[object], *, fail_edit: bool = False) -> None:
        self.events = events
        self.fail_edit = fail_edit

    async def edit_message_text(self, **_kwargs) -> None:
        self.events.append("edit")
        if self.fail_edit:
            raise RuntimeError("edit failed")

    async def send_message(self, **_kwargs) -> None:
        self.events.append("send")

    async def delete_message(self, **_kwargs) -> None:
        self.events.append("delete")


class RecordingApi:
    def __init__(
        self,
        events: list[object],
        tmp_path: Path,
        *,
        detail: dict[str, object] | None = None,
        get_error: Exception | None = None,
        download_errors: set[str] | None = None,
        sent: set[str] | None = None,
        fail_delivered_receipt_once: bool = False,
    ) -> None:
        self.events = events
        self.tmp_path = tmp_path
        self.detail = detail
        self.get_error = get_error
        self.download_errors = set(download_errors or ())
        self.delivery_states = {
            image_id: "delivered" for image_id in set(sent or ())
        }
        self.delivery_owners: dict[str, str] = {}
        self.fail_delivered_receipt_once = fail_delivered_receipt_once

    async def get_generation(
        self,
        _chat_id: int,
        gen_id: str,
        *,
        tg_user_id: int,
    ) -> dict[str, object]:
        assert tg_user_id == _chat_id
        self.events.append(("get_generation", gen_id))
        if self.get_error is not None:
            raise self.get_error
        if self.detail is not None:
            return dict(self.detail)
        return {"edit_url": "", "project_url": ""}

    async def download_image_to_file(
        self,
        _chat_id: int,
        image_id: str,
        *,
        tg_user_id: int,
    ) -> tuple[Path, str, int]:
        assert tg_user_id == _chat_id
        self.events.append(("download", image_id))
        if image_id in self.download_errors:
            raise listener.ApiError("download_failed", "boom")
        path = self.tmp_path / f"{image_id}.png"
        path.write_bytes(b"png")
        return path, "image/png", path.stat().st_size

    async def list_delivered_telegram_images(
        self,
        _chat_id: int,
        _generation_id: str,
        *,
        tg_user_id: int,
    ) -> set[str]:
        assert tg_user_id == _chat_id
        return {
            image_id
            for image_id, state in self.delivery_states.items()
            if state == "delivered"
        }

    async def begin_telegram_delivery(
        self,
        chat_id: int,
        *,
        tg_user_id: int,
        generation_id: str,
        image_id: str,
        owner_token: str,
    ) -> dict[str, object]:
        assert tg_user_id == chat_id
        self.events.append(
            ("begin_image", generation_id, image_id, chat_id, owner_token)
        )
        state = self.delivery_states.get(image_id)
        if state == "delivered":
            return {
                "state": "already_delivered",
                "attempt_id": f"attempt-{image_id}",
                "message_id": 101,
            }
        if state in {"dispatching", "delivery_result_unknown"}:
            return {
                "state": "result_unknown",
                "attempt_id": f"attempt-{image_id}",
                "message_id": None,
            }
        self.delivery_states[image_id] = "dispatching"
        self.delivery_owners[image_id] = owner_token
        return {
            "state": "send_allowed",
            "attempt_id": f"attempt-{image_id}",
            "message_id": None,
        }

    async def finish_telegram_delivery(
        self,
        chat_id: int,
        attempt_id: str,
        *,
        tg_user_id: int,
        owner_token: str,
        state: str,
        telegram_message_id: int | None = None,
        error_class: str | None = None,
    ) -> dict[str, object]:
        assert tg_user_id == chat_id
        image_id = attempt_id.removeprefix("attempt-")
        if self.delivery_owners.get(image_id) != owner_token:
            raise RuntimeError("stale image delivery owner")
        self.events.append(
            (
                "finish_image",
                "gen-1",
                image_id,
                owner_token,
                state,
                telegram_message_id,
                error_class,
            )
        )
        if state == "delivered" and self.fail_delivered_receipt_once:
            self.fail_delivered_receipt_once = False
            raise RuntimeError("receipt store unavailable")
        self.delivery_states[image_id] = state
        return {"state": state, "newly_finished": True}


def _close_created_task(coro, *_args, **_kwargs):
    coro.close()
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


@pytest.mark.asyncio
async def test_listener_discovers_only_recent_bot_task_users() -> None:
    redis = ActiveUsersRedis()

    user_ids = await listener._load_active_user_ids(redis)  # type: ignore[arg-type]

    assert user_ids == {"user-1", "user-2"}
    assert redis.pipe.calls[0][0] == "zremrangebyscore"
    assert redis.pipe.calls[1][0] == "zrangebyscore"


@pytest.mark.asyncio
async def test_listener_rebuilds_empty_active_zset_from_legacy_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FallbackRedis()
    recovering_tracker = RecoveringTracker()
    monkeypatch.setattr(listener, "tracker", recovering_tracker)
    monkeypatch.setattr(listener.time, "time", lambda: 200_000.0)

    user_ids = await listener._load_active_user_ids(redis)  # type: ignore[arg-type]

    assert user_ids == {"legacy-user"}
    assert recovering_tracker.refreshes == [("gen-legacy", "legacy-user")]
    assert redis.scan_calls == [
        {
            "cursor": 0,
            "match": "events:user:*",
            "count": listener._FALLBACK_SCAN_COUNT,
        }
    ]
    assert redis.xrevrange_calls == [
        {
            "key": "events:user:legacy-user",
            "max": "+",
            "min": listener._initial_cursor(),
            "count": listener._FALLBACK_EVENTS_PER_STREAM,
        }
    ]
    assert redis.cursor_writes == [
        (
            listener._fallback_stream_cursor_key("legacy-user"),
            "+",
            {"ex": listener._CURSOR_TTL_SECONDS},
        ),
        (
            listener._FALLBACK_SCAN_CURSOR_KEY,
            "0",
            {"ex": listener._CURSOR_TTL_SECONDS},
        ),
    ]


@pytest.mark.asyncio
async def test_listener_fallback_scan_is_cluster_throttled() -> None:
    redis = FallbackRedis(acquire_lease=False)

    user_ids = await listener._load_active_user_ids(redis)  # type: ignore[arg-type]

    assert user_ids == set()
    assert redis.scan_calls == []
    assert redis.pipeline_calls == 1


@pytest.mark.asyncio
async def test_non_terminal_event_refreshes_tracker_and_active_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshing_tracker = RefreshingDispatchTracker()
    progress_calls: list[str] = []
    monkeypatch.setattr(listener, "tracker", refreshing_tracker)
    monkeypatch.setattr(listener, "_should_throttle_progress", lambda _gen_id: False)

    async def fake_on_progress(
        _bot: object, _track: object, data: dict[str, object]
    ) -> None:
        progress_calls.append(str(data["generation_id"]))

    monkeypatch.setattr(listener, "_on_progress", fake_on_progress)

    await listener._dispatch(
        SimpleNamespace(),
        SimpleNamespace(),
        {
            "event": "generation.started",
            "data": {"generation_id": "gen-1"},
        },
        stream_user_id="user-1",
    )

    assert refreshing_tracker.refreshes == [("gen-1", "user-1")]
    assert progress_calls == ["gen-1"]


@pytest.mark.asyncio
async def test_listener_fake_clock_keeps_full_retention_lookback_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 200_000.25
    monkeypatch.setattr(listener.time, "time", lambda: now)
    redis = CursorRedis()

    initial_cursor = listener._initial_cursor()
    await listener._save_cursor(redis, "user-1", "123-0")  # type: ignore[arg-type]

    expected_ms = int(now * 1000) - listener.ACTIVE_USER_STREAM_TTL_SECONDS * 1000
    assert initial_cursor == f"{expected_ms}-0"
    assert listener.ACTIVE_USER_STREAM_TTL_SECONDS >= 48 * 3600
    assert listener._CURSOR_TTL_SECONDS >= 48 * 3600
    assert redis.calls == [
        (
            listener._cursor_key_v2("user-1"),
            "123-0",
            {"ex": listener._CURSOR_TTL_SECONDS},
        )
    ]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("{not-json", "invalid_json"),
        ('{"event":"generation.succeeded","data":[]}', "data_not_object"),
        ('{"data":{}}', "event_missing"),
        (
            '{"event":"generation.succeeded","data":{}}',
            "generation_id_missing",
        ),
        (
            '{"event":"generation.future","data":{"generation_id":"gen-1"}}',
            "unsupported_event",
        ),
    ],
)
@pytest.mark.asyncio
async def test_poison_stream_entry_is_atomically_quarantined(
    payload: str,
    reason: str,
) -> None:
    stop_event = asyncio.Event()
    redis = WorkerRedis(stop_event, [("100-0", payload)])

    await listener._user_worker(
        SimpleNamespace(),
        QuarantineApi(),
        redis,  # type: ignore[arg-type]
        "user-1",
        stop_event,
    )

    assert redis.cursor == "100-0"
    assert redis.cursor_writes == []
    assert redis.eval_calls == 1
    assert len(redis.quarantine) == 1
    assert redis.quarantine[0]["payload"] == payload
    assert reason in redis.quarantine[0]["reason"]
    assert redis.quarantined[
        listener._quarantined_key("user-1", "")
    ] == "100-0"


@pytest.mark.asyncio
async def test_quarantine_failure_never_advances_poison_cursor() -> None:
    stop_event = asyncio.Event()
    redis = WorkerRedis(stop_event, [("100-0", "{not-json")])

    with pytest.raises(RuntimeError, match="quarantine unavailable"):
        await listener._user_worker(
            SimpleNamespace(),
            QuarantineApi(fail_persist=True),
            redis,  # type: ignore[arg-type]
            "user-1",
            stop_event,
        )

    assert redis.cursor is None
    assert redis.cursor_writes == []
    assert redis.quarantine == []
    assert redis.quarantined == {}


@pytest.mark.asyncio
async def test_attached_dispatch_retry_does_not_advance_cursor_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    attached = json.dumps(
        {
            "event": "generation.attached",
            "data": {
                "parent_generation_id": "parent-1",
                "generation_id": "bonus-1",
            },
        }
    )
    succeeded = json.dumps(
        {
            "event": "generation.succeeded",
            "data": {"generation_id": "bonus-1"},
        }
    )
    redis = WorkerRedis(
        stop_event,
        [
            ("100-0", attached),
            ("100-0", attached),
            ("101-0", succeeded),
        ],
    )
    calls: list[str] = []

    async def fake_dispatch(
        _bot: object,
        _api: object,
        envelope: dict[str, object],
        *,
        stream_user_id: str,
    ) -> listener.DispatchDisposition:
        assert stream_user_id == "user-1"
        event = str(envelope["event"])
        calls.append(event)
        if calls == ["generation.attached"]:
            raise RuntimeError("tracker write failed")
        return listener.DispatchDisposition.DELIVERED

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(listener, "_dispatch", fake_dispatch)
    monkeypatch.setattr(listener.asyncio, "sleep", no_sleep)

    await listener._user_worker(
        SimpleNamespace(),
        QuarantineApi(),
        redis,  # type: ignore[arg-type]
        "user-1",
        stop_event,
    )

    assert calls == [
        "generation.attached",
        "generation.attached",
        "generation.succeeded",
    ]
    assert redis.cursor_writes == ["100-0", "101-0"]
    assert redis.quarantine == []


@pytest.mark.asyncio
async def test_terminal_failure_and_notice_failure_quarantine_without_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    payload = json.dumps(
        {
            "event": "generation.succeeded",
            "data": {"generation_id": "gen-1"},
        }
    )
    redis = WorkerRedis(stop_event, [("100-0", payload)])
    notified: list[str] = []

    async def fail_dispatch(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telegram unavailable")

    async def fail_notice(*_args: object, **_kwargs: object) -> object:
        return listener.DropNoticeReceipt(
            delivered=False,
            error="RuntimeError: remediation unavailable",
        )

    class NoNotifiedTracker:
        async def mark_notified(self, gen_id: str, **_kwargs: object) -> bool:
            notified.append(gen_id)
            return True

    monkeypatch.setattr(listener, "_dispatch", fail_dispatch)
    monkeypatch.setattr(listener, "_notify_dispatch_drop", fail_notice)
    monkeypatch.setattr(listener, "tracker", NoNotifiedTracker())
    monkeypatch.setattr(listener, "_DISPATCH_MAX_ATTEMPTS", 1)

    await listener._user_worker(
        SimpleNamespace(),
        QuarantineApi(),
        redis,  # type: ignore[arg-type]
        "user-1",
        stop_event,
    )

    assert notified == []
    assert redis.cursor == "100-0"
    assert redis.cursor_writes == []
    assert "remediation unavailable" in redis.quarantine[0]["reason"]
    assert redis.quarantined[
        listener._quarantined_key("user-1", "gen-1")
    ] == "100-0"


@pytest.mark.asyncio
async def test_dispatch_quarantine_commit_failure_keeps_terminal_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()
    payload = json.dumps(
        {
            "event": "generation.succeeded",
            "data": {"generation_id": "gen-1"},
        }
    )
    redis = WorkerRedis(stop_event, [("100-0", payload)])

    async def fail_dispatch(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telegram unavailable")

    async def no_notice(*_args: object, **_kwargs: object) -> object:
        return listener.DropNoticeReceipt(delivered=False, error="tracker_missing")

    monkeypatch.setattr(listener, "_dispatch", fail_dispatch)
    monkeypatch.setattr(listener, "_notify_dispatch_drop", no_notice)
    monkeypatch.setattr(listener, "_DISPATCH_MAX_ATTEMPTS", 1)

    with pytest.raises(RuntimeError, match="quarantine unavailable"):
        await listener._user_worker(
            SimpleNamespace(),
            QuarantineApi(fail_persist=True),
            redis,  # type: ignore[arg-type]
            "user-1",
            stop_event,
        )

    assert redis.cursor is None
    assert redis.quarantine == []


@pytest.mark.asyncio
async def test_terminal_delivery_busy_does_not_silently_skip_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener, "tracker", BusyTracker())

    with pytest.raises(listener._TerminalDeliveryBusy):
        await listener._on_succeeded(
            SimpleNamespace(),
            SimpleNamespace(),
            "gen-1",
            SimpleNamespace(
                chat_id=1,
                status_message_id=2,
                prompt="p",
                batch_id="",
                is_bonus=False,
            ),
            {},
        )


@pytest.mark.asyncio
async def test_terminal_delivery_busy_does_not_silently_skip_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener, "tracker", BusyTracker())

    with pytest.raises(listener._TerminalDeliveryBusy):
        await listener._on_failed(
            SimpleNamespace(),
            "gen-1",
            SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
            {},
        )


@pytest.mark.asyncio
async def test_terminal_delivery_notified_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener, "tracker", NotifiedTracker())

    await listener._on_failed(
        SimpleNamespace(),
        "gen-1",
        SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
        {},
    )


@pytest.mark.asyncio
async def test_terminal_delivery_notified_replay_with_active_lock_stays_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener, "tracker", ActiveNotifiedTracker())

    with pytest.raises(listener._TerminalDeliveryBusy):
        await listener._on_failed(
            SimpleNamespace(),
            "gen-1",
            SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
            {},
        )


@pytest.mark.asyncio
async def test_delivery_lease_renews_repeatedly_during_slow_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewing = RenewingTracker()
    monkeypatch.setattr(listener, "tracker", renewing)
    monkeypatch.setattr(listener, "DELIVERY_LOCK_MS", 30)

    async with listener._delivery_lease("gen-1") as lease:
        await asyncio.wait_for(renewing.renewed_twice.wait(), timeout=1)
        lease.assert_owned("gen-1")

    assert renewing.renewals >= 2


@pytest.mark.asyncio
async def test_delivery_lease_renewal_loss_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewing = RenewingTracker(lose=True)
    monkeypatch.setattr(listener, "tracker", renewing)
    monkeypatch.setattr(listener, "DELIVERY_LOCK_MS", 30)

    with pytest.raises(listener.DeliveryResultUnknown, match="lease lost"):
        async with listener._delivery_lease("gen-1") as lease:
            while not lease.lost.is_set():
                await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_failed_delivery_marks_notified_after_telegram_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    monkeypatch.setattr(listener.asyncio, "create_task", _close_created_task)

    await listener._on_failed(
        RecordingBot(events),
        "gen-1",
        SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
        {},
    )

    assert events[:3] == [
        ("begin", "gen-1"),
        "edit",
        ("mark", "gen-1", "o" * 32),
    ]
    assert not [event for event in events if event[0] == "clear"]


@pytest.mark.asyncio
async def test_failed_delivery_leaves_no_sent_marker_when_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    with pytest.raises(RuntimeError, match="edit failed"):
        await listener._on_failed(
            RecordingBot(events, fail_edit=True),
            "gen-1",
            SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
            {},
        )

    assert not [event for event in events if event[0] == "mark"]
    assert ("clear", "gen-1", "o" * 32) in events


@pytest.mark.asyncio
async def test_succeeded_delivery_marks_notified_after_all_documents_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def fake_send_document_with_backoff(*_args, **kwargs) -> object:
        events.append(("send_document", kwargs["filename"]))
        return SimpleNamespace(message_id=101)

    async def fake_finish(_bot, gen_id: str, _track) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener, "_send_document_with_backoff", fake_send_document_with_backoff
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", fake_finish)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path),
        "gen-1",
        SimpleNamespace(
            chat_id=1,
            status_message_id=2,
            prompt="p",
            batch_id="",
            is_bonus=False,
        ),
        {"images": [{"image_id": "img-1"}]},
    )

    mark_idx = events.index(("mark", "gen-1", "o" * 32))
    send_idx = next(
        idx
        for idx, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "send_document"
    )
    assert send_idx < mark_idx
    assert not [event for event in events if event[0] == "clear"]
    assert ("finish", "gen-1") in events


@pytest.mark.asyncio
async def test_succeeded_delivery_leaves_no_sent_marker_when_any_document_send_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def fake_send_document_with_backoff(*_args, **kwargs) -> object:
        events.append(("send_document", kwargs["filename"]))
        if kwargs["filename"].endswith("-2.png"):
            raise listener.TelegramDefinitiveReject("telegram rejected")
        return SimpleNamespace(message_id=101)

    async def fake_finish(_bot, gen_id: str, _track) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener, "_send_document_with_backoff", fake_send_document_with_backoff
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", fake_finish)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            RecordingApi(events, tmp_path),
            "gen-1",
            SimpleNamespace(
                chat_id=1,
                status_message_id=2,
                prompt="p",
                batch_id="",
                is_bonus=False,
            ),
            {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
        )

    assert not [event for event in events if event[0] == "mark"]
    assert ("clear", "gen-1", "o" * 32) in events
    assert ("finish", "gen-1") not in events


@pytest.mark.asyncio
async def test_sent_then_receipt_write_failure_never_resends_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    api = RecordingApi(
        events,
        tmp_path,
        fail_delivered_receipt_once=True,
    )
    sends = 0
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def send_once(*_args: object, **_kwargs: object) -> object:
        nonlocal sends
        sends += 1
        return SimpleNamespace(message_id=101)

    monkeypatch.setattr(listener, "_send_document_with_backoff", send_once)

    with pytest.raises(listener.DeliveryResultUnknown, match="receipt write"):
        await listener._on_succeeded(
            RecordingBot(events),
            api,
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}]},
        )
    with pytest.raises(listener.DeliveryResultUnknown, match="result unknown"):
        await listener._on_succeeded(
            RecordingBot(events),
            api,
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}]},
        )

    assert sends == 1
    assert api.delivery_states == {"img-1": "dispatching"}
    assert not [event for event in events if event[0] == "mark"]


@pytest.mark.asyncio
async def test_telegram_timeout_enters_unknown_and_never_auto_resends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    api = RecordingApi(events, tmp_path)
    sends = 0
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def timeout(*_args: object, **_kwargs: object) -> object:
        nonlocal sends
        sends += 1
        raise TimeoutError("response lost")

    monkeypatch.setattr(listener, "_send_document_with_backoff", timeout)

    with pytest.raises(listener.DeliveryResultUnknown):
        await listener._on_succeeded(
            RecordingBot(events),
            api,
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}]},
        )
    with pytest.raises(listener.DeliveryResultUnknown):
        await listener._on_succeeded(
            RecordingBot(events),
            api,
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}]},
        )

    assert sends == 1
    assert api.delivery_states == {
        "img-1": "delivery_result_unknown"
    }


@pytest.mark.asyncio
async def test_definitive_reject_can_be_retried_by_new_delivery_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    api = RecordingApi(events, tmp_path)
    sends = 0
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def reject_then_send(*_args: object, **_kwargs: object) -> object:
        nonlocal sends
        sends += 1
        if sends == 1:
            raise listener.TelegramDefinitiveReject("bad request")
        return SimpleNamespace(message_id=102)

    async def finish(_bot: object, gen_id: str, _track: object) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener,
        "_send_document_with_backoff",
        reject_then_send,
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", finish)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            api,
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}]},
        )
    await listener._on_succeeded(
        RecordingBot(events),
        api,
        "gen-1",
        _succeeded_track(),
        {"images": [{"image_id": "img-1"}]},
    )

    assert sends == 2
    assert api.delivery_states == {"img-1": "delivered"}
    assert ("mark", "gen-1", "o" * 32) in events


def _succeeded_track() -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=1,
        status_message_id=2,
        prompt="p",
        batch_id="",
        is_bonus=False,
    )


def _patch_delivery(monkeypatch: pytest.MonkeyPatch, events: list[object]) -> None:
    async def fake_send_document_with_backoff(*_args, **kwargs) -> object:
        events.append(
            ("send_document", kwargs["filename"], kwargs["caption"] is not None)
        )
        if str(kwargs["filename"]).endswith("-2.png") and kwargs.get("_fail"):
            raise listener.TelegramDefinitiveReject("telegram rejected")
        return SimpleNamespace(message_id=101)

    async def fake_finish(_bot, gen_id: str, _track) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener, "_send_document_with_backoff", fake_send_document_with_backoff
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", fake_finish)


@pytest.mark.asyncio
async def test_succeeded_lookup_failure_is_retryable_not_reported_as_no_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审计 J-3：查不到图 ≠ 没有图。查询失败必须可重投，不能标终态。"""
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            RecordingApi(
                events, tmp_path, get_error=listener.ApiError("upstream", "down")
            ),
            "gen-1",
            _succeeded_track(),
            {},
        )

    # 不能把"没有图片返回"当结论发给用户，也不能 mark_notified 把图判死刑
    assert "edit" not in events
    assert not [event for event in events if event[0] == "mark"]
    assert ("clear", "gen-1", "o" * 32) in events


@pytest.mark.asyncio
async def test_succeeded_falls_back_to_api_when_event_images_unparsable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """images 里没有 image_id 时退回 API 再问一次，而不是直接宣布没有图。"""
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path, detail={"image_ids": ["img-9"]}),
        "gen-1",
        _succeeded_track(),
        {"images": [{"url": "https://example.invalid/x.png"}]},
    )

    assert ("download", "img-9") in events
    assert ("mark", "gen-1", "o" * 32) in events


@pytest.mark.asyncio
async def test_succeeded_reports_no_images_only_when_api_confirms_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path, detail={"image_ids": []}),
        "gen-1",
        _succeeded_track(),
        {},
    )

    assert "edit" in events
    assert ("mark", "gen-1", "o" * 32) in events


@pytest.mark.asyncio
async def test_succeeded_partial_send_records_only_delivered_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审计 J-4：发到一半失败时，只有已送达的图会被记账。"""
    events: list[object] = []
    api = RecordingApi(events, tmp_path)
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def fake_send_document_with_backoff(*_args, **kwargs) -> object:
        events.append(("send_document", kwargs["filename"]))
        if str(kwargs["filename"]).endswith("-2.png"):
            raise listener.TelegramDefinitiveReject("telegram rejected")
        return SimpleNamespace(message_id=101)

    async def fake_finish(_bot, gen_id: str, _track) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener, "_send_document_with_backoff", fake_send_document_with_backoff
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", fake_finish)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            api,
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
        )

    assert api.delivery_states == {
        "img-1": "delivered",
        "img-2": "failed_before_accept",
    }
    assert not [
        event
        for event in events
        if event[:3] == ("finish_image", "gen-1", "img-2")
        and event[4] == "delivered"
    ]


@pytest.mark.asyncio
async def test_succeeded_replay_only_resends_missing_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审计 J-4：重投不能把已经发出去的图再发一遍（也不能再挂一套按钮）。"""
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path, sent={"img-1"}),
        "gen-1",
        _succeeded_track(),
        {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
    )

    assert ("download", "img-1") not in events
    assert ("download", "img-2") in events
    sends = [event for event in events if event[0] == "send_document"]
    # 只补发第二张，序号保持整批原始位置，且不再重复挂 caption / 操作键盘
    assert sends == [("send_document", "gen-1-2.png", False)]
    assert ("mark", "gen-1", "o" * 32) in events


@pytest.mark.asyncio
async def test_succeeded_replay_after_full_delivery_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path, sent={"img-1"}),
        "gen-1",
        _succeeded_track(),
        {"images": [{"image_id": "img-1"}]},
    )

    assert not [event for event in events if event[0] == "send_document"]
    assert ("mark", "gen-1", "o" * 32) in events
    assert ("finish", "gen-1") in events


@pytest.mark.asyncio
async def test_succeeded_download_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """部分图下载失败 = 用户少拿一张已付费的图，必须重投补发。"""
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            RecordingApi(events, tmp_path, download_errors={"img-2"}),
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
        )

    assert not [event for event in events if event[0] == "mark"]


@pytest.mark.asyncio
async def test_succeeded_all_downloads_failed_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """全部图片下载失败 = 和查询失败同类的可重试错误，不能标 delivered。

    旧实现在这里 delivered=True + mark_notified，重投直接跳过，用户永远
    拿不到已付费的图。修复后 delivered 保持 False，走 attempt 计数重投。
    """
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            RecordingApi(events, tmp_path, download_errors={"img-1", "img-2"}),
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
        )

    assert not [event for event in events if event[0] == "mark"]
    assert ("clear", "gen-1", "o" * 32) in events
    # 不提前给用户「下载失败」的结论：重投时同文本 edit 会命中 message not
    # modified → fallback send_message，把失败提示重复发一遍（J-3 同款策略）。
    assert "edit" not in events


@pytest.mark.asyncio
async def test_dispatch_drop_tells_user_the_task_already_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 J-3：放弃重投前必须说明"已完成、别重新生成"，否则用户会重复付费。"""
    events: list[object] = []
    texts: list[str] = []

    class TrackOnlyTracker:
        async def get(self, _gen_id: str) -> object:
            return _succeeded_track()

    monkeypatch.setattr(listener, "tracker", TrackOnlyTracker())

    async def fake_replace_status(
        _bot: object,
        _track: object,
        text: str,
    ) -> int:
        texts.append(text)
        return 2

    monkeypatch.setattr(
        listener,
        "_replace_status_with_receipt",
        fake_replace_status,
    )

    await listener._notify_dispatch_drop(
        RecordingBot(events), "generation.succeeded", "gen-1"
    )
    await listener._notify_dispatch_drop(
        RecordingBot(events), "generation.failed", "gen-2"
    )

    assert len(texts) == 1
    assert "不用重新生成" in texts[0]
    assert "/tasks" in texts[0]


@pytest.mark.asyncio
async def test_batch_finalize_decrements_once_per_generation_and_deletes_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        listener, "tracker", RecordingTracker(events, batch_remaining=0)
    )

    await listener._maybe_finalize_batch(
        RecordingBot(events),
        SimpleNamespace(chat_id=1, status_message_id=2, batch_id="batch-1"),
        "gen-1",
    )

    assert ("batch_decr", "batch-1", "gen-1") in events
    assert "delete" in events
    assert ("batch_remove", "batch-1") in events


@pytest.mark.asyncio
async def test_batch_finalize_skips_delete_when_counter_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        listener, "tracker", RecordingTracker(events, batch_remaining=None)
    )

    await listener._maybe_finalize_batch(
        RecordingBot(events),
        SimpleNamespace(chat_id=1, status_message_id=2, batch_id="batch-1"),
        "gen-1",
    )

    assert ("batch_decr", "batch-1", "gen-1") in events
    assert "delete" not in events
    assert ("batch_remove", "batch-1") not in events


@pytest.mark.asyncio
async def test_attached_registers_bonus_before_send_and_dedups_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 P2：send_message 与 tracker.add 之间崩溃会让重投重复发「🎁 双引擎」。

    修复后先注册后发消息：第一次调用严格按「注册 → send → 补写 message_id」
    顺序；模拟崩溃后的重投命中「已注册」跳过分支，不再发第二条。
    """
    events: list[object] = []
    registered: set[str] = set()

    class BonusTracker(RecordingTracker):
        def __init__(self) -> None:
            super().__init__(events)

        async def get(self, gen_id: str) -> object | None:
            events.append(("get", gen_id))
            if gen_id == "parent-1":
                return SimpleNamespace(
                    chat_id=1,
                    status_message_id=2,
                    prompt="p",
                    params={},
                    is_bonus=False,
                    user_id="user-1",
                )
            if gen_id in registered:
                return SimpleNamespace(chat_id=1, status_message_id=42, prompt="p")
            return None

        async def add(self, gen_id: str, _track: object) -> None:
            events.append(("add", gen_id))
            registered.add(gen_id)

        async def update_status_message(self, gen_id: str, message_id: int) -> None:
            events.append(("update_status_message", gen_id, message_id))

    monkeypatch.setattr(listener, "tracker", BonusTracker())

    class Message:
        def __init__(self) -> None:
            self.message_id = 42

    class Bot:
        async def send_message(self, *, chat_id: int, text: str) -> Message:
            events.append(("send_message", chat_id))
            assert "🎁" in text
            return Message()

    data = {"parent_generation_id": "parent-1", "generation_id": "bonus-1"}

    await listener._on_attached(Bot(), data)

    # 注册必须先于 send；send 成功后补写真实 message_id
    assert events == [
        ("get", "parent-1"),
        ("get", "bonus-1"),
        ("add", "bonus-1"),
        ("send_message", 1),
        ("update_status_message", "bonus-1", 42),
    ]

    # 重投（旧实现里 send 与 add 之间崩溃的场景）：bonus 已注册 → 直接跳过
    events.clear()
    await listener._on_attached(Bot(), data)
    assert events == [("get", "parent-1"), ("get", "bonus-1")]


@pytest.mark.asyncio
async def test_attached_registration_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册失败绝不能先发消息：否则留下「发了 🎁 但 tracker 里没有 bonus」的
    悬挂态，succeeded(bonus_id) 也找不到 chat，用户永远拿不到图。"""
    events: list[object] = []

    class FailingTracker(RecordingTracker):
        def __init__(self) -> None:
            super().__init__(events)

        async def get(self, gen_id: str) -> object | None:
            events.append(("get", gen_id))
            if gen_id == "parent-1":
                return SimpleNamespace(
                    chat_id=1,
                    status_message_id=2,
                    prompt="p",
                    params={},
                    is_bonus=False,
                    user_id="user-1",
                )
            return None

        async def add(self, gen_id: str, _track: object) -> None:
            events.append(("add", gen_id))
            raise RuntimeError("redis down")

    monkeypatch.setattr(listener, "tracker", FailingTracker())

    class Bot:
        async def send_message(self, **_kwargs) -> None:
            events.append("send_message")  # pragma: no cover - 不应被调用

    with pytest.raises(RuntimeError, match="redis down"):
        await listener._on_attached(
            Bot(),
            {"parent_generation_id": "parent-1", "generation_id": "bonus-1"},
        )

    assert "send_message" not in events
    assert ("add", "bonus-1") in events
