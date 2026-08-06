from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from redis.exceptions import ResponseError

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import control_consumer  # noqa: E402


class RecordingApi:
    def __init__(
        self,
        *,
        newly_accepted: bool = True,
        fail_ack: bool = False,
    ) -> None:
        self.newly_accepted = newly_accepted
        self.fail_ack = fail_ack
        self.events: list[tuple[object, ...]] = []
        self.quarantines: list[dict[str, object]] = []

    async def ack_control_command(
        self,
        command_id: str,
        *,
        command: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, object]:
        self.events.append(("ack", command_id, command, status, error))
        if self.fail_ack:
            raise RuntimeError("API unavailable")
        return {
            "command": command,
            "status": status,
            "newly_accepted": self.newly_accepted and status == "accepted",
        }

    async def persist_delivery_quarantine(
        self,
        **payload: object,
    ) -> dict[str, object]:
        self.quarantines.append(payload)
        return {"quarantine_id": "quarantine-1"}


class RecordingRedis:
    def __init__(
        self,
        *,
        claimed: list[tuple[object, object]] | None = None,
        fresh: list[tuple[object, object]] | None = None,
    ) -> None:
        self.claimed = list(claimed or [])
        self.fresh = list(fresh or [])
        self.events: list[tuple[object, ...]] = []
        self.closed = False

    async def xgroup_create(self, *args: object, **kwargs: object) -> None:
        self.events.append(("group", *args, kwargs))

    async def xautoclaim(self, *args: object, **kwargs: object) -> object:
        self.events.append(("claim", *args, kwargs))
        entries, self.claimed = self.claimed, []
        return ("0-0", entries, [])

    async def xreadgroup(self, *args: object, **kwargs: object) -> object:
        self.events.append(("read", *args, kwargs))
        entries, self.fresh = self.fresh, []
        return (
            [(control_consumer.CONTROL_STREAM.encode(), entries)]
            if entries
            else []
        )

    async def xack(self, *args: object) -> int:
        self.events.append(("xack", *args))
        return 1

    async def aclose(self) -> None:
        self.closed = True


def _entry(
    *,
    command_id: str = "command-1",
    command: str = "restart",
    payload: str = "{}",
) -> tuple[bytes, dict[bytes, bytes]]:
    return (
        b"100-0",
        {
            b"command_id": command_id.encode(),
            b"command": command.encode(),
            b"payload": payload.encode(),
        },
    )


@pytest.mark.asyncio
async def test_restart_durable_ack_precedes_xack_and_clean_stop() -> None:
    redis = RecordingRedis()
    api = RecordingApi(newly_accepted=True)
    stop_event = asyncio.Event()

    restarted = await control_consumer.process_control_entry(
        redis,
        api,  # type: ignore[arg-type]
        stop_event,
        stream_id_raw=_entry()[0],
        fields_raw=_entry()[1],
        bot=None,
    )

    assert restarted is True
    assert stop_event.is_set()
    assert api.events == [
        ("ack", "command-1", "restart", "accepted", None)
    ]
    assert redis.events == [
        (
            "xack",
            control_consumer.CONTROL_STREAM,
            control_consumer.CONTROL_GROUP,
            "100-0",
        )
    ]


@pytest.mark.asyncio
async def test_duplicate_restart_is_xacked_without_second_stop() -> None:
    redis = RecordingRedis()
    api = RecordingApi(newly_accepted=False)
    stop_event = asyncio.Event()

    restarted = await control_consumer.process_control_entry(
        redis,
        api,  # type: ignore[arg-type]
        stop_event,
        stream_id_raw=_entry()[0],
        fields_raw=_entry()[1],
        bot=None,
    )

    assert restarted is False
    assert not stop_event.is_set()
    assert [event[0] for event in redis.events] == ["xack"]


@pytest.mark.asyncio
async def test_ack_failure_keeps_stream_entry_pending() -> None:
    redis = RecordingRedis()
    api = RecordingApi(fail_ack=True)

    with pytest.raises(RuntimeError, match="API unavailable"):
        await control_consumer.process_control_entry(
            redis,
            api,  # type: ignore[arg-type]
            asyncio.Event(),
            stream_id_raw=_entry()[0],
            fields_raw=_entry()[1],
            bot=None,
        )

    assert not [event for event in redis.events if event[0] == "xack"]


@pytest.mark.asyncio
async def test_malformed_entry_is_durably_quarantined_before_xack() -> None:
    redis = RecordingRedis()
    api = RecordingApi()

    await control_consumer.process_control_entry(
        redis,
        api,  # type: ignore[arg-type]
        asyncio.Event(),
        stream_id_raw=b"101-0",
        fields_raw={b"command": b"restart", b"payload": b"{"},
        bot=None,
    )

    assert api.quarantines[0]["source_id"] == "101-0"
    assert api.quarantines[0]["source_stream"] == control_consumer.CONTROL_STREAM
    assert [event[0] for event in redis.events] == ["xack"]


@pytest.mark.asyncio
async def test_redrive_failure_is_recorded_before_xack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = RecordingRedis()
    api = RecordingApi()

    async def fail_redrive(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("tracker unavailable")

    monkeypatch.setattr(
        control_consumer,
        "redrive_quarantined_event",
        fail_redrive,
    )
    entry = _entry(
        command="redrive_quarantine",
        payload='{"payload_raw":"{}","stream_user_id":"user-1"}',
    )

    await control_consumer.process_control_entry(
        redis,
        api,  # type: ignore[arg-type]
        asyncio.Event(),
        stream_id_raw=entry[0],
        fields_raw=entry[1],
        bot=object(),  # type: ignore[arg-type]
    )

    assert api.events == [
        (
            "ack",
            "command-1",
            "redrive_quarantine",
            "failed",
            "RuntimeError: tracker unavailable",
        )
    ]
    assert [event[0] for event in redis.events] == ["xack"]


@pytest.mark.asyncio
async def test_listener_reclaims_pending_before_reading_new_entries() -> None:
    redis = RecordingRedis(claimed=[_entry()])
    api = RecordingApi()
    stop_event = asyncio.Event()

    async def no_sleep(_stop: asyncio.Event, _delay: float) -> bool:
        pytest.fail("reconnect backoff should not run")

    await control_consumer.run_control_listener(
        stop_event,
        api=api,  # type: ignore[arg-type]
        sleep_or_stop=no_sleep,
        redis_factory=lambda *_args, **_kwargs: redis,
    )

    assert stop_event.is_set()
    assert [event[0] for event in redis.events] == [
        "group",
        "claim",
        "xack",
    ]
    assert redis.closed is True


@pytest.mark.asyncio
async def test_group_creation_only_ignores_busygroup() -> None:
    class Redis:
        def __init__(self, message: str) -> None:
            self.message = message

        async def xgroup_create(self, *_args: object, **_kwargs: object) -> None:
            raise ResponseError(self.message)

    await control_consumer.ensure_control_group(Redis("BUSYGROUP already exists"))
    with pytest.raises(ResponseError, match="permission denied"):
        await control_consumer.ensure_control_group(Redis("permission denied"))
