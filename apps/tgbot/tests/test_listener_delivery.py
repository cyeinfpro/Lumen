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
            SimpleNamespace(chat_id=1, status_message_id=2, prompt="p"),
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
            SimpleNamespace(chat_id=1, status_message_id=2, prompt="p"),
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
        SimpleNamespace(chat_id=1, status_message_id=2, prompt="p"),
        {},
    )
