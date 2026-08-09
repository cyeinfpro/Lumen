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
        claim_payload: dict[str, object] | None = None,
        claim_status: str = "running",
        acquired: bool = True,
        prepare_action: str = "execute",
        fail_ack: bool = False,
        fail_finish: bool = False,
        protocol_version: int = 1,
    ) -> None:
        self.claim_payload = dict(claim_payload or {})
        self.claim_status = claim_status
        self.acquired = acquired
        self.prepare_action = prepare_action
        self.fail_ack = fail_ack
        self.fail_finish = fail_finish
        self.protocol_version = protocol_version
        self.events: list[tuple[object, ...]] = []
        self.quarantines: list[dict[str, object]] = []

    async def control_capabilities(self) -> dict[str, object]:
        self.events.append(("capabilities",))
        return {"effect_protocol_version": self.protocol_version}

    async def claim_control_effect(
        self,
        command_id: str,
        *,
        command: str,
        owner: str,
    ) -> dict[str, object]:
        self.events.append(("claim", command_id, command, owner))
        return {
            "command": command,
            "status": self.claim_status,
            "acquired": self.acquired,
            "owner": owner if self.acquired else None,
            "fence": 7,
            "lease_seconds": 6,
            "payload": dict(self.claim_payload),
        }

    async def renew_control_effect(
        self,
        command_id: str,
        *,
        command: str,
        owner: str,
        fence: int,
    ) -> dict[str, object]:
        self.events.append(("renew", command_id, command, owner, fence))
        return {"renewed": True, "fence": fence, "lease_seconds": 6}

    async def prepare_control_redrive_effect(
        self,
        command_id: str,
        *,
        owner: str,
        fence: int,
    ) -> dict[str, object]:
        self.events.append(("prepare", command_id, owner, fence))
        return {
            "action": self.prepare_action,
            "fence": fence,
            "idempotency_key": "telegram-quarantine-redrive:q-1",
        }

    async def commit_control_restart_intent(
        self,
        command_id: str,
        *,
        owner: str,
        fence: int,
        generation: str,
    ) -> dict[str, object]:
        self.events.append(("restart_intent", command_id, owner, fence, generation))
        return {
            "action": "stop_current_generation",
            "fence": fence,
            "requested_generation": generation,
        }

    async def finish_control_effect(
        self,
        command_id: str,
        *,
        command: str,
        owner: str,
        fence: int,
        status: str,
        error: str | None = None,
        generation: str | None = None,
    ) -> dict[str, object]:
        self.events.append(
            (
                "finish",
                command_id,
                command,
                owner,
                fence,
                status,
                error,
                generation,
            )
        )
        if self.fail_finish:
            raise RuntimeError("effect fence lost")
        return {"status": status}

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
            "newly_accepted": status == "accepted",
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
        return [(control_consumer.CONTROL_STREAM.encode(), entries)] if entries else []

    async def expire(self, *args: object) -> bool:
        self.events.append(("expire", *args))
        return True

    async def xack(self, *args: object) -> int:
        self.events.append(("xack", *args))
        return 1

    async def xdel(self, *args: object) -> int:
        self.events.append(("xdel", *args))
        return 1

    async def xtrim(self, *args: object, **kwargs: object) -> int:
        self.events.append(("xtrim", *args, kwargs))
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
async def test_restart_commits_intent_before_stop_without_terminal_ack() -> None:
    redis = RecordingRedis()
    api = RecordingApi()
    stop_event = asyncio.Event()

    restarted = await control_consumer.process_control_entry(
        redis,
        api,  # type: ignore[arg-type]
        stop_event,
        stream_id_raw=_entry()[0],
        fields_raw=_entry()[1],
        bot=None,
        generation="generation-old",
    )

    assert restarted is True
    assert stop_event.is_set()
    assert [event[0] for event in api.events] == ["claim", "restart_intent"]
    assert redis.events == []


@pytest.mark.asyncio
async def test_new_generation_completes_restart_and_then_removes_transport() -> None:
    redis = RecordingRedis()
    api = RecordingApi(
        claim_payload={
            control_consumer.TELEGRAM_CONTROL_RESTART_INTENT_KEY: {
                "state": "stop_intent_committed",
                "requested_generation": "generation-old",
            }
        }
    )
    stop_event = asyncio.Event()
    restart_ready = asyncio.Event()
    restart_ready.set()

    restarted = await control_consumer.process_control_entry(
        redis,
        api,  # type: ignore[arg-type]
        stop_event,
        stream_id_raw=_entry()[0],
        fields_raw=_entry()[1],
        bot=None,
        generation="generation-new",
        restart_ready=restart_ready,
    )

    assert restarted is False
    assert not stop_event.is_set()
    assert [event[0] for event in api.events] == ["claim", "finish", "ack"]
    finish = api.events[1]
    assert finish[5] == "succeeded"
    assert finish[7] == "generation-new"
    assert [event[0] for event in redis.events] == [
        "expire",
        "xack",
        "xdel",
        "xtrim",
    ]
    assert redis.events[0][2] == 90 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_new_generation_waits_for_runtime_readiness_before_restart_ack() -> None:
    redis = RecordingRedis()
    api = RecordingApi(
        claim_payload={
            control_consumer.TELEGRAM_CONTROL_RESTART_INTENT_KEY: {
                "state": "stop_intent_committed",
                "requested_generation": "generation-old",
            }
        }
    )
    stop_event = asyncio.Event()
    restart_ready = asyncio.Event()

    task = asyncio.create_task(
        control_consumer.process_control_entry(
            redis,
            api,  # type: ignore[arg-type]
            stop_event,
            stream_id_raw=_entry()[0],
            fields_raw=_entry()[1],
            bot=None,
            generation="generation-new",
            restart_ready=restart_ready,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert [event[0] for event in api.events] == ["claim"]
    assert redis.events == []

    restart_ready.set()
    assert await asyncio.wait_for(task, timeout=1) is False
    assert [event[0] for event in api.events] == ["claim", "finish", "ack"]
    assert [event[0] for event in redis.events] == [
        "expire",
        "xack",
        "xdel",
        "xtrim",
    ]


@pytest.mark.asyncio
async def test_effect_api_is_required_and_legacy_client_fails_closed() -> None:
    class LegacyApi:
        async def ack_control_command(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            pytest.fail("legacy ack path must not run")

    redis = RecordingRedis()
    with pytest.raises(AttributeError, match="claim_control_effect"):
        await control_consumer.process_control_entry(
            redis,
            LegacyApi(),  # type: ignore[arg-type]
            asyncio.Event(),
            stream_id_raw=_entry()[0],
            fields_raw=_entry()[1],
            bot=None,
            generation="generation-old",
        )
    assert redis.events == []


@pytest.mark.asyncio
async def test_ack_failure_keeps_stream_entry_pending() -> None:
    redis = RecordingRedis()
    api = RecordingApi(
        claim_status="succeeded",
        acquired=False,
        fail_ack=True,
    )

    with pytest.raises(RuntimeError, match="API unavailable"):
        await control_consumer.process_control_entry(
            redis,
            api,  # type: ignore[arg-type]
            asyncio.Event(),
            stream_id_raw=_entry()[0],
            fields_raw=_entry()[1],
            bot=None,
            generation="generation-new",
        )

    assert redis.events == []


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
    assert [event[0] for event in redis.events] == ["xack", "xdel", "xtrim"]


@pytest.mark.asyncio
async def test_redrive_failure_keeps_unknown_effect_and_transport_nonterminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = RecordingRedis()
    api = RecordingApi(
        claim_payload={
            "payload_raw": "{}",
            "stream_user_id": "user-1",
        }
    )

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
        generation="generation-1",
    )

    assert [event[0] for event in api.events] == [
        "claim",
        "prepare",
        "finish",
    ]
    assert api.events[2][5] == "outcome_unknown"
    assert redis.events == []


@pytest.mark.asyncio
async def test_existing_unknown_redrive_waits_for_manual_reconciliation() -> None:
    redis = RecordingRedis()
    api = RecordingApi(
        claim_payload={
            "payload_raw": "{}",
            "stream_user_id": "user-1",
        },
        prepare_action="outcome_unknown",
    )
    entry = _entry(
        command="redrive_quarantine",
        payload='{"payload_raw":"{}","stream_user_id":"user-1"}',
    )

    restarted = await control_consumer.process_control_entry(
        redis,
        api,  # type: ignore[arg-type]
        asyncio.Event(),
        stream_id_raw=entry[0],
        fields_raw=entry[1],
        bot=object(),  # type: ignore[arg-type]
        generation="generation-1",
    )

    assert restarted is False
    assert [event[0] for event in api.events] == ["claim", "prepare"]
    assert redis.events == []


@pytest.mark.asyncio
async def test_lease_loss_cancels_redrive_and_blocks_terminal_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = RecordingRedis()
    api = RecordingApi(
        claim_payload={
            "payload_raw": "{}",
            "stream_user_id": "user-1",
        },
        fail_finish=True,
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_redrive(*_args: object, **_kwargs: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def lose_lease(*_args: object, **_kwargs: object) -> None:
        await started.wait()
        raise control_consumer.ControlEffectLeaseLost("lease lost")

    monkeypatch.setattr(
        control_consumer,
        "redrive_quarantined_event",
        blocked_redrive,
    )
    monkeypatch.setattr(control_consumer, "_effect_heartbeat", lose_lease)
    entry = _entry(
        command="redrive_quarantine",
        payload='{"payload_raw":"{}","stream_user_id":"user-1"}',
    )

    with pytest.raises(control_consumer.ControlEffectLeaseLost, match="lease lost"):
        await control_consumer.process_control_entry(
            redis,
            api,  # type: ignore[arg-type]
            asyncio.Event(),
            stream_id_raw=entry[0],
            fields_raw=entry[1],
            bot=object(),  # type: ignore[arg-type]
            generation="generation-1",
        )

    assert cancelled.is_set()
    assert [event[0] for event in api.events] == [
        "claim",
        "prepare",
    ]
    assert redis.events == []


@pytest.mark.asyncio
async def test_effect_heartbeat_renews_and_fails_closed_on_rejection() -> None:
    api = RecordingApi()

    async def reject_renewal(
        command_id: str,
        *,
        command: str,
        owner: str,
        fence: int,
    ) -> dict[str, object]:
        api.events.append(("renew", command_id, command, owner, fence))
        return {"renewed": False, "fence": fence, "lease_seconds": 6}

    async def no_sleep(_delay: float) -> None:
        return None

    api.renew_control_effect = reject_renewal  # type: ignore[method-assign]
    with pytest.raises(control_consumer.ControlEffectLeaseLost, match="lease"):
        await control_consumer._effect_heartbeat(  # noqa: SLF001
            api,  # type: ignore[arg-type]
            command_id="command-1",
            command="redrive_quarantine",
            owner="owner-1",
            fence=7,
            lease_seconds=6,
            sleep=no_sleep,
        )
    assert api.events == [("renew", "command-1", "redrive_quarantine", "owner-1", 7)]


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
    assert [event[0] for event in redis.events] == ["group", "claim"]
    assert [event[0] for event in api.events] == [
        "capabilities",
        "claim",
        "restart_intent",
    ]
    assert redis.closed is True


@pytest.mark.asyncio
async def test_listener_rejects_incompatible_effect_protocol() -> None:
    redis = RecordingRedis()
    api = RecordingApi(protocol_version=0)

    async def no_sleep(_stop: asyncio.Event, _delay: float) -> bool:
        pytest.fail("listener must fail before opening Redis")

    with pytest.raises(RuntimeError, match="incompatible Telegram"):
        await control_consumer.run_control_listener(
            asyncio.Event(),
            api=api,  # type: ignore[arg-type]
            sleep_or_stop=no_sleep,
            redis_factory=lambda *_args, **_kwargs: redis,
        )
    assert redis.events == []


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
