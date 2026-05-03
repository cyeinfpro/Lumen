from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from aiogram.types import Message

from app.handlers._helpers import require_message


@pytest.mark.asyncio
async def test_require_message_returns_message_when_present() -> None:
    cb = MagicMock()
    msg = MagicMock(spec=Message)
    cb.message = msg
    cb.answer = AsyncMock()

    out = await require_message(cb)

    assert out is msg
    cb.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_require_message_alerts_and_returns_none_when_message_missing() -> None:
    cb = MagicMock()
    cb.message = None
    cb.answer = AsyncMock()

    out = await require_message(cb)

    assert out is None
    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "过期" in text
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_require_message_alerts_when_message_is_inaccessible() -> None:
    """非 Message 实例（如 InaccessibleMessage 在 aiogram3）也应被拒。"""
    cb = MagicMock()
    cb.message = object()  # 不是 Message 实例
    cb.answer = AsyncMock()

    out = await require_message(cb)

    assert out is None
    cb.answer.assert_awaited_once()
