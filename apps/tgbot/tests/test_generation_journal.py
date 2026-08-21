from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import generation_journal  # noqa: E402
from app.generation_state import (  # noqa: E402
    SubmissionJournalConflict,
    SubmissionJournalStatus,
)
from app.tracker import Tracker  # noqa: E402


class JournalStore:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}


class JournalRedis:
    def __init__(self, store: JournalStore) -> None:
        self.store = store

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {
            field.encode(): value.encode()
            for field, value in self.store.hashes.get(key, {}).items()
        }

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.store.strings.pop(key, None) is not None)
            deleted += int(self.store.hashes.pop(key, None) is not None)
        return deleted

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        assert numkeys == 3
        keys = [str(value) for value in args[:3]]
        argv = [str(value) for value in args[3:]]
        if script == generation_journal._STAGE_LUA:  # noqa: SLF001
            return self._stage(keys, argv)
        if script == generation_journal._FINISH_LUA:  # noqa: SLF001
            return self._finish(keys, argv)
        raise AssertionError("unexpected journal script")

    def _stage(self, keys: list[str], argv: list[str]) -> list[bytes]:
        update_key, pending_key, operation_key = keys
        update_operation = self.store.strings.get(update_key)
        if update_operation is not None:
            return [b"update", update_operation.encode()]
        pending_operation = self.store.strings.get(pending_key)
        if pending_operation is not None:
            self.store.strings[update_key] = pending_operation
            return [b"pending", pending_operation.encode()]
        (
            operation_id,
            identity_hash,
            update_token,
            request_fingerprint,
            idempotency_key,
            payload,
            status,
            _ttl,
        ) = argv
        self.store.hashes[operation_key] = {
            "operation_id": operation_id,
            "identity_hash": identity_hash,
            "update_token": update_token,
            "request_fingerprint": request_fingerprint,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "status": status,
        }
        self.store.strings[update_key] = operation_id
        self.store.strings[pending_key] = operation_id
        return [b"created", operation_id.encode()]

    def _finish(self, keys: list[str], argv: list[str]) -> int:
        operation_key, _update_key, pending_key = keys
        request_fingerprint, status, clear_pending, _ttl, operation_id = argv
        record = self.store.hashes.get(operation_key)
        if record is None:
            return 0
        if record.get("request_fingerprint") != request_fingerprint:
            return -1
        record["status"] = status
        if clear_pending == "1":
            if self.store.strings.get(pending_key) == operation_id:
                self.store.strings.pop(pending_key, None)
        else:
            pending_operation = self.store.strings.get(pending_key)
            if pending_operation is None or pending_operation == operation_id:
                self.store.strings[pending_key] = operation_id
        return 1


def _payload(prompt: str = "cat") -> dict[str, object]:
    return {
        "idempotency_key": "volatile-key",
        "prompt": prompt,
        "aspect_ratio": "1:1",
        "render_quality": "high",
        "count": 1,
        "resolution": "2k",
        "output_format": "jpeg",
    }


def _tracker(store: JournalStore) -> Tracker:
    tracker = Tracker()
    tracker._redis = JournalRedis(store)  # type: ignore[assignment]
    return tracker


@pytest.mark.asyncio
async def test_journal_survives_update_redelivery_and_redis_client_restart() -> None:
    store = JournalStore()
    first_tracker = _tracker(store)
    first = await first_tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:10",
        payload=_payload(),
    )
    await first_tracker.finish_generation_submission(
        first,
        SubmissionJournalStatus.AMBIGUOUS,
    )

    redelivery = await first_tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:10",
        payload=_payload(),
    )
    recovered_tracker = _tracker(store)
    recovered = await recovered_tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:11",
        payload=_payload(),
    )

    assert redelivery.operation_id == first.operation_id
    assert recovered.operation_id == first.operation_id
    assert redelivery.idempotency_key == recovered.idempotency_key
    assert recovered.status is SubmissionJournalStatus.AMBIGUOUS
    assert generation_journal.SUBMISSION_TTL_SECONDS > 3600


@pytest.mark.asyncio
async def test_terminal_submission_allows_intentional_new_key() -> None:
    tracker = _tracker(JournalStore())
    first = await tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:20",
        payload=_payload(),
    )
    await tracker.finish_generation_submission(
        first,
        SubmissionJournalStatus.ACCEPTED,
    )

    redelivery = await tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:20",
        payload=_payload(),
    )
    confirmed_new = await tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:21",
        payload=_payload(),
    )

    assert redelivery.operation_id == first.operation_id
    assert redelivery.status is SubmissionJournalStatus.ACCEPTED
    assert confirmed_new.operation_id != first.operation_id
    assert confirmed_new.idempotency_key != first.idempotency_key


@pytest.mark.asyncio
async def test_same_update_cannot_change_generation_payload() -> None:
    tracker = _tracker(JournalStore())
    await tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:30",
        payload=_payload("cat"),
    )

    with pytest.raises(SubmissionJournalConflict):
        await tracker.stage_generation_submission(
            chat_id=42,
            tg_user_id=42,
            update_token="message:30",
            payload=_payload("dog"),
        )


@pytest.mark.asyncio
async def test_late_ambiguous_finish_does_not_replace_new_pending_operation() -> None:
    tracker = _tracker(JournalStore())
    first = await tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:40",
        payload=_payload(),
    )
    await tracker.finish_generation_submission(
        first,
        SubmissionJournalStatus.ACCEPTED,
    )
    second = await tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:41",
        payload=_payload(),
    )

    await tracker.finish_generation_submission(
        first,
        SubmissionJournalStatus.AMBIGUOUS,
    )
    retry = await tracker.stage_generation_submission(
        chat_id=42,
        tg_user_id=42,
        update_token="message:42",
        payload=_payload(),
    )

    assert retry.operation_id == second.operation_id
