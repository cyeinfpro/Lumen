from __future__ import annotations

import sys
from pathlib import Path

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app.config import Settings
from app import main


def test_settings_allow_db_managed_bot_token() -> None:
    settings = Settings(telegram_bot_shared_secret="s" * 32, telegram_bot_token="")

    assert settings.telegram_bot_token == ""
    assert settings.telegram_bot_shared_secret == "s" * 32


@pytest.mark.asyncio
async def test_main_exits_before_api_client_without_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "telegram_bot_shared_secret", "")
    monkeypatch.setattr(
        main,
        "LumenApi",
        lambda: pytest.fail("LumenApi must not be constructed without shared secret"),
    )

    await main._amain()
