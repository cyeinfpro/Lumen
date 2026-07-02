from __future__ import annotations

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


class BusyTracker:
    async def begin_delivery(self, _gen_id: str) -> bool:
        return False

    async def is_notified(self, _gen_id: str) -> bool:
        return False


class NotifiedTracker:
    async def begin_delivery(self, _gen_id: str) -> bool:
        return False

    async def is_notified(self, _gen_id: str) -> bool:
        return True

    async def is_delivery_active(self, _gen_id: str) -> bool:
        return False


class ActiveNotifiedTracker(NotifiedTracker):
    async def is_delivery_active(self, _gen_id: str) -> bool:
        return True


class RecordingTracker:
    def __init__(self, events: list[object], *, batch_remaining: int | None = 0) -> None:
        self.events = events
        self.batch_remaining = batch_remaining

    async def begin_delivery(self, gen_id: str) -> bool:
        self.events.append(("begin", gen_id))
        return True

    async def is_notified(self, _gen_id: str) -> bool:
        return False

    async def is_delivery_active(self, _gen_id: str) -> bool:
        return False

    async def mark_notified(self, gen_id: str, *, release_lock: bool = True) -> bool:
        self.events.append(("mark", gen_id, release_lock))
        return True

    async def clear_delivery(self, gen_id: str) -> None:
        self.events.append(("clear", gen_id))

    async def clear_terminal_delivery(self, gen_id: str) -> None:
        self.events.append(("clear_terminal", gen_id))

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
    def __init__(self, events: list[object], tmp_path: Path) -> None:
        self.events = events
        self.tmp_path = tmp_path

    async def get_generation(self, _chat_id: int, gen_id: str) -> dict[str, str]:
        self.events.append(("get_generation", gen_id))
        return {"edit_url": "", "project_url": ""}

    async def download_image_to_file(
        self, _chat_id: int, image_id: str
    ) -> tuple[Path, str, int]:
        self.events.append(("download", image_id))
        path = self.tmp_path / f"{image_id}.png"
        path.write_bytes(b"png")
        return path, "image/png", path.stat().st_size


def _close_created_task(coro, *_args, **_kwargs):
    coro.close()
    return SimpleNamespace(cancel=lambda: None)


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
async def test_failed_delivery_marks_notified_before_irreversible_edit(
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
        ("mark", "gen-1", False),
        "edit",
    ]
    assert ("clear", "gen-1") in events


@pytest.mark.asyncio
async def test_failed_delivery_clears_terminal_marker_when_send_fails(
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

    assert ("mark", "gen-1", False) in events
    assert ("clear_terminal", "gen-1") in events


@pytest.mark.asyncio
async def test_succeeded_delivery_marks_notified_before_document_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def fake_send_document_with_backoff(*_args, **kwargs) -> None:
        events.append(("send_document", kwargs["filename"]))

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

    mark_idx = events.index(("mark", "gen-1", False))
    send_idx = next(
        idx
        for idx, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "send_document"
    )
    assert mark_idx < send_idx
    assert ("clear", "gen-1") in events
    assert ("finish", "gen-1") in events


@pytest.mark.asyncio
async def test_succeeded_delivery_clears_terminal_marker_when_any_document_send_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def fake_send_document_with_backoff(*_args, **kwargs) -> None:
        events.append(("send_document", kwargs["filename"]))
        if kwargs["filename"].endswith("-2.png"):
            raise RuntimeError("telegram send failed")

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

    assert ("mark", "gen-1", False) in events
    assert ("clear_terminal", "gen-1") in events
    assert ("finish", "gen-1") not in events


@pytest.mark.asyncio
async def test_batch_finalize_decrements_once_per_generation_and_deletes_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events, batch_remaining=0))

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
