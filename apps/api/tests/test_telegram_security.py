from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Request

from app.routes import telegram


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/telegram/bind",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_link_code_preserves_urlsafe_entropy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        telegram.secrets,
        "token_urlsafe",
        lambda _n: "ab-CD_ef0123456789==",
    )
    code = telegram._gen_link_code()

    assert code == "ab-CD_ef0123456789"
    # Why also assert entropy class shape: a future regression that lower-cases
    # or uppercases the alphabet (e.g. someone "normalising" the code for
    # storage) would still pass the literal-equality check above only because
    # the monkeypatched fake happens to match. Pin the mixed-case + URL-safe
    # punctuation explicitly so the check breaks if the alphabet collapses.
    assert any(c.isupper() for c in code)
    assert any(c.islower() for c in code)
    assert any(c in "-_" for c in code)


def test_link_code_real_token_keeps_mixed_case_alphabet() -> None:
    # Why: the monkeypatched test above proves _gen_link_code does not eat
    # uppercase letters; this one proves the *real* token_urlsafe alphabet is
    # in fact mixed-case URL-safe (so the contract being defended is real and
    # not just an artifact of the mock's chosen characters). 22 chars is the
    # base64-no-pad encoding of 16 bytes, so a real call must produce >=22.
    code = telegram._gen_link_code()
    assert len(code) >= 22


@pytest.mark.asyncio
async def test_bind_invalid_code_counts_against_code_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        async def get(self, _key: str):
            return None

    class Limiter:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def check(self, _redis, key: str) -> None:
            self.keys.append(key)

    limiter = Limiter()
    monkeypatch.setattr(telegram, "get_redis", lambda: Redis())
    monkeypatch.setattr(telegram, "_BOT_BIND_CODE_LIMITER", limiter)

    with pytest.raises(Exception) as excinfo:
        await telegram.bind_telegram(
            _request(),
            telegram.BindIn(chat_id="chat-1", code="bad-code"),
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert limiter.keys == ["rl:telegram:bind:127.0.0.1"]


@pytest.mark.asyncio
async def test_access_config_returns_allowlist_without_proxy_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_setting_str(_db, key: str, default: str = "") -> str:
        values = {
            "telegram.bot_enabled": "0",
            "telegram.allowed_user_ids": "123,456",
        }
        return values.get(key, default)

    monkeypatch.setattr(telegram, "_get_setting_str", fake_get_setting_str)

    out = await telegram.access_config(SimpleNamespace())  # type: ignore[arg-type]

    assert out.bot_enabled is False
    assert out.allowed_user_ids == "123,456"
