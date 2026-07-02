from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request

from app import deps
from app.routes import telegram


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/telegram/bind",
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/telegram/me",
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in headers.items()
            ],
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


def test_telegram_bool_option_treats_string_false_as_disabled() -> None:
    assert telegram._bool_option("false") is False
    assert telegram._bool_option("0") is False
    assert telegram._bool_option("true") is True


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
            _request(headers=[(b"x-telegram-user-id", b"tg-123")]),
            telegram.BindIn(chat_id="chat-1", code="bad-code"),
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert limiter.keys == ["rl:telegram:bind:127.0.0.1"]


@pytest.mark.asyncio
async def test_bind_db_failure_releases_claim_without_deleting_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.values = {telegram._link_code_key("code-1"): "user-1"}  # noqa: SLF001
            self.deleted: list[str] = []

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
            self.values[key] = value
            return True

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            self.values.pop(key, None)
            return 1

    class Result:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Db:
        def __init__(self) -> None:
            self.results = [
                SimpleNamespace(
                    id="user-1",
                    email="u@example.com",
                    display_name="User",
                    deleted_at=None,
                ),
                None,
                None,
            ]
            self.rolled_back = False

        async def execute(self, _stmt: Any) -> Result:
            return Result(self.results.pop(0))

        def add(self, _value: Any) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            raise RuntimeError("deadlock")

        async def rollback(self) -> None:
            self.rolled_back = True

    redis = Redis()
    monkeypatch.setattr(telegram, "get_redis", lambda: redis)

    db = Db()
    with pytest.raises(RuntimeError):
        await telegram.bind_telegram(
            _request(),
            telegram.BindIn(chat_id="chat-1", code="code-1", tg_user_id="tg-123"),
            db,  # type: ignore[arg-type]
        )

    assert db.rolled_back is True
    assert telegram._link_code_key("code-1") in redis.values  # noqa: SLF001
    assert telegram._link_code_claim_key("code-1") in redis.deleted  # noqa: SLF001
    assert telegram._link_code_key("code-1") not in redis.deleted  # noqa: SLF001


@pytest.mark.asyncio
async def test_bind_records_tg_user_id_from_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.values = {telegram._link_code_key("code-1"): "user-1"}  # noqa: SLF001

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
            self.values[key] = value
            return True

        async def delete(self, *_keys: str) -> int:
            return 1

    class Result:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Db:
        def __init__(self) -> None:
            self.results = [
                SimpleNamespace(
                    id="user-1",
                    email="u@example.com",
                    display_name="User",
                    deleted_at=None,
                ),
                None,
                None,
            ]
            self.added: list[Any] = []

        async def execute(self, _stmt: Any) -> Result:
            return Result(self.results.pop(0))

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def commit(self) -> None:
            return None

    monkeypatch.setattr(telegram, "get_redis", lambda: Redis())
    db = Db()

    out = await telegram.bind_telegram(
        _request(headers=[(b"x-telegram-user-id", b"tg-123")]),
        telegram.BindIn(chat_id="chat-1", code="code-1"),
        db,  # type: ignore[arg-type]
    )

    assert out.user_id == "user-1"
    assert db.added[0].tg_user_id == "tg-123"


@pytest.mark.asyncio
async def test_bind_rejects_tg_user_id_header_body_mismatch() -> None:
    with pytest.raises(Exception) as excinfo:
        await telegram.bind_telegram(
            _request(headers=[(b"x-telegram-user-id", b"tg-123")]),
            telegram.BindIn(chat_id="chat-1", code="code-1", tg_user_id="tg-456"),
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "telegram_user_mismatch"


@pytest.mark.asyncio
async def test_bind_rejects_missing_tg_user_id() -> None:
    with pytest.raises(Exception) as excinfo:
        await telegram.bind_telegram(
            _request(),
            telegram.BindIn(chat_id="chat-1", code="code-1"),
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "missing_telegram_user_id"


@pytest.mark.asyncio
async def test_release_link_code_claim_only_deletes_matching_owner() -> None:
    class Redis:
        def __init__(self) -> None:
            self.values = {
                telegram._link_code_claim_key("code-1"): "chat:new"  # noqa: SLF001
            }
            self.deleted: list[str] = []

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            self.values.pop(key, None)
            return 1

    redis = Redis()

    await telegram._release_link_code_claim(  # noqa: SLF001
        redis,
        "code-1",
        owner="chat:old",
    )

    assert telegram._link_code_claim_key("code-1") in redis.values  # noqa: SLF001
    assert redis.deleted == []

    await telegram._release_link_code_claim(  # noqa: SLF001
        redis,
        "code-1",
        owner="chat:new",
    )

    assert telegram._link_code_claim_key("code-1") not in redis.values  # noqa: SLF001
    assert redis.deleted == [telegram._link_code_claim_key("code-1")]  # noqa: SLF001


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


@pytest.mark.asyncio
async def test_get_bot_user_requires_bound_tg_user_id_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "secret")

    failures: list[str] = []

    async def fake_record_failure(_request: Request) -> None:
        failures.append("failed")

    monkeypatch.setattr(deps, "_record_bot_auth_failure", fake_record_failure)

    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(chat_id="100", tg_user_id="200", user_id="user-1"),
                SimpleNamespace(id="user-1", deleted_at=None),
            )

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    ok = await deps.get_bot_user(
        _request_with_headers(
            {
                "X-Bot-Token": "secret",
                "X-Telegram-Chat-Id": "100",
                "X-Telegram-User-Id": "200",
            }
        ),
        Db(),  # type: ignore[arg-type]
    )
    assert ok.id == "user-1"

    with pytest.raises(Exception) as excinfo:
        await deps.get_bot_user(
            _request_with_headers(
                {
                    "X-Bot-Token": "secret",
                    "X-Telegram-Chat-Id": "100",
                    "X-Telegram-User-Id": "201",
                }
            ),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 403
    assert excinfo.value.detail["error"]["code"] == "telegram_user_mismatch"
    assert failures == ["failed"]


@pytest.mark.asyncio
async def test_get_bot_user_rejects_missing_tg_user_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "secret")

    failures: list[str] = []

    async def fake_record_failure(_request: Request) -> None:
        failures.append("failed")

    monkeypatch.setattr(deps, "_record_bot_auth_failure", fake_record_failure)

    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(chat_id="100", tg_user_id="200", user_id="user-1"),
                SimpleNamespace(id="user-1", deleted_at=None),
            )

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    with pytest.raises(Exception) as excinfo:
        await deps.get_bot_user(
            _request_with_headers(
                {
                    "X-Bot-Token": "secret",
                    "X-Telegram-Chat-Id": "100",
                }
            ),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "missing_telegram_user_id"
    assert failures == ["failed"]


@pytest.mark.asyncio
async def test_get_bot_user_accepts_backfilled_legacy_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "secret")

    failures: list[str] = []

    async def fake_record_failure(_request: Request) -> None:
        failures.append("failed")

    monkeypatch.setattr(deps, "_record_bot_auth_failure", fake_record_failure)

    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(chat_id="100", tg_user_id="100", user_id="user-1"),
                SimpleNamespace(id="user-1", deleted_at=None),
            )

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    user = await deps.get_bot_user(
        _request_with_headers(
            {
                "X-Bot-Token": "secret",
                "X-Telegram-Chat-Id": "100",
                "X-Telegram-User-Id": "100",
            }
        ),
        Db(),  # type: ignore[arg-type]
    )

    assert user.id == "user-1"
    assert failures == []


@pytest.mark.asyncio
async def test_get_bot_user_rejects_corrupt_binding_without_tg_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "secret")

    failures: list[str] = []

    async def fake_record_failure(_request: Request) -> None:
        failures.append("failed")

    monkeypatch.setattr(deps, "_record_bot_auth_failure", fake_record_failure)

    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(chat_id="100", tg_user_id=None, user_id="user-1"),
                SimpleNamespace(id="user-1", deleted_at=None),
            )

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    with pytest.raises(Exception) as excinfo:
        await deps.get_bot_user(
            _request_with_headers(
                {
                    "X-Bot-Token": "secret",
                    "X-Telegram-Chat-Id": "100",
                    "X-Telegram-User-Id": "100",
                }
            ),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 403
    assert excinfo.value.detail["error"]["code"] == "telegram_rebind_required"
    assert failures == ["failed"]
