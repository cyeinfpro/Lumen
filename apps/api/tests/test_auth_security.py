from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Request
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from app import security
from app.deps import SESSION_COOKIE, get_current_user
from app.routes import auth
from app.security import make_session_cookie
from lumen_core.schemas import LoginIn, SignupIn


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def first(self):
        return self.value


class _Db:
    def __init__(self, results=()):
        self.results = list(results)
        self.added = []
        self.rolled_back = False

    async def execute(self, _stmt):
        return _ScalarResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        self.rolled_back = True


def _request(method: str = "GET", session_id: str | None = None) -> Request:
    headers = []
    if session_id is not None:
        headers.append((b"cookie", f"{SESSION_COOKIE}={make_session_cookie(session_id)}".encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
        }
    )


def _request_with_forwarded_for() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"198.51.100.10")],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_verify_password_propagates_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_hashed: str, _plain: str) -> bool:
        raise RuntimeError("argon2 unavailable")

    monkeypatch.setattr(security, "_ph", SimpleNamespace(verify=boom))

    with pytest.raises(RuntimeError):
        security.verify_password("hash", "pw")


def test_auth_cookies_are_samesite_strict_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "app_env", "prod")
    response = Response()

    auth._set_auth_cookies(response, "session-1", "csrf-1")

    cookies = [value.decode().lower() for name, value in response.raw_headers if name == b"set-cookie"]
    assert cookies
    assert all("samesite=strict" in cookie for cookie in cookies)


@pytest.mark.asyncio
async def test_get_current_user_rejects_soft_deleted_user() -> None:
    session = SimpleNamespace(
        revoked_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    user = SimpleNamespace(deleted_at=datetime.now(timezone.utc))
    db = _Db(results=[(session, user)])

    with pytest.raises(Exception) as excinfo:
        await get_current_user(_request(session_id="session-1"), db)  # type: ignore[arg-type]

    assert getattr(excinfo.value, "status_code", None) == 401
    assert excinfo.value.detail["error"]["code"] == "user_deleted"


@pytest.mark.asyncio
async def test_login_runs_dummy_verify_when_user_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_hashes: list[str] = []

    def fake_verify(hashed: str, _plain: str) -> bool:
        seen_hashes.append(hashed)
        return False

    monkeypatch.setattr(auth, "verify_password", fake_verify)

    with pytest.raises(Exception) as excinfo:
        await auth.login(
            LoginIn(email="missing@example.com", password="pw"),
            _request(method="POST"),
            Response(),
            _Db(results=[None]),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 401
    assert seen_hashes == [auth._DUMMY_PASSWORD_HASH]


@pytest.mark.asyncio
async def test_signup_integrity_error_returns_email_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    class ConflictDb(_Db):
        async def flush(self):
            raise IntegrityError("insert users", {}, Exception("duplicate"))

    monkeypatch.setattr(auth, "hash_password", lambda _plain: "hashed")
    db = ConflictDb(results=[None, SimpleNamespace(email="new@example.com")])

    with pytest.raises(Exception) as excinfo:
        await auth.signup(
            SignupIn(email="new@example.com", password="password123"),
            _request(method="POST"),
            Response(),
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert excinfo.value.detail["error"]["code"] == "email_taken"
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_signup_rejects_weak_password() -> None:
    with pytest.raises(Exception) as excinfo:
        await auth.signup(
            SignupIn(email="new@example.com", password="short"),
            _request(method="POST"),
            Response(),
            _Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "weak_password"


@pytest.mark.asyncio
async def test_password_reset_confirm_rejects_weak_password_before_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoomRedis:
        async def get(self, _key: str):
            raise AssertionError("weak passwords should fail before redis lookup")

    monkeypatch.setattr(auth, "get_redis", lambda: BoomRedis())

    with pytest.raises(Exception) as excinfo:
        await auth.password_reset_confirm(
            auth.PasswordResetConfirmIn(token="token", new_password="short"),
            _request(method="POST"),
            _Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "weak_password"


@pytest.mark.asyncio
async def test_password_reset_request_does_not_reveal_missing_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        async def eval(self, *_args):
            return [1, 0]

    monkeypatch.setattr(auth, "get_redis", lambda: Redis())

    out = await auth.password_reset_request(
        auth.PasswordResetRequestIn(email="missing@example.com"),
        _request(method="POST"),
        _Db(results=[None]),  # type: ignore[arg-type]
    )

    assert out.ok is True


@pytest.mark.asyncio
async def test_password_reset_request_stores_hashed_token_for_existing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.calls = []

        async def eval(self, *_args):
            return [1, 0]

        async def set(self, key: str, value: str, *, ex: int) -> None:
            self.calls.append((key, value, ex))

    redis = Redis()
    user = SimpleNamespace(id="user-1", deleted_at=None)

    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: "reset-token")

    out = await auth.password_reset_request(
        auth.PasswordResetRequestIn(email="user@example.com"),
        _request(method="POST"),
        _Db(results=[user]),  # type: ignore[arg-type]
    )

    assert out.ok is True
    assert redis.calls == [
        (
            auth._password_reset_key("reset-token"),
            "user-1",
            auth._PASSWORD_RESET_TTL_SECONDS,
        )
    ]


@pytest.mark.asyncio
async def test_password_reset_request_reports_redis_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        async def eval(self, *_args):
            return [1, 0]

        async def set(self, *_args, **_kwargs) -> None:
            raise RuntimeError("redis down")

    user = SimpleNamespace(id="user-1", deleted_at=None)
    monkeypatch.setattr(auth, "get_redis", lambda: Redis())

    with pytest.raises(Exception) as excinfo:
        await auth.password_reset_request(
            auth.PasswordResetRequestIn(email="user@example.com"),
            _request(method="POST"),
            _Db(results=[user]),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 503
    assert excinfo.value.detail["error"]["code"] == "reset_unavailable"


@pytest.mark.asyncio
async def test_password_reset_confirm_updates_password_and_revokes_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.deleted = []

        async def eval(self, *_args):
            return [1, 0]

        async def get(self, _key: str) -> str:
            return "user-1"

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

        async def getdel(self, key: str) -> str:
            # 路由切换到原子 GETDEL（Redis 6.2+）；mock 返回模拟值并记录 delete。
            self.deleted.append(key)
            return "user-1"

    class Db:
        def __init__(self) -> None:
            self.user = SimpleNamespace(id="user-1", deleted_at=None, password_hash="old")
            self.revoked_sessions = False
            self.committed = False
            self.user_select = None

        async def execute(self, stmt):
            if self.user_select is None:
                self.user_select = stmt
                return _ScalarResult(self.user)
            self.revoked_sessions = True
            return None

        async def commit(self) -> None:
            self.committed = True

    redis = Redis()
    db = Db()

    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth, "hash_password", lambda plain: f"hashed:{plain}")

    out = await auth.password_reset_confirm(
        auth.PasswordResetConfirmIn(token="reset-token", new_password="new-password"),
        _request(method="POST"),
        db,  # type: ignore[arg-type]
    )

    assert out.ok is True
    assert db.user.password_hash == "hashed:new-password"
    assert db.revoked_sessions is True
    assert db.committed is True
    assert redis.deleted == [auth._password_reset_key("reset-token")]
    assert "FOR UPDATE" in str(db.user_select).upper()


@pytest.mark.asyncio
async def test_create_session_uses_trusted_forwarded_for(monkeypatch: pytest.MonkeyPatch) -> None:
    old = auth.settings.trusted_proxies
    auth.settings.trusted_proxies = "127.0.0.1/32"

    class Db:
        def __init__(self) -> None:
            self.added = None

        def add(self, value):
            self.added = value

        async def flush(self) -> None:
            return None

    db = Db()
    try:
        await auth._create_session(
            db,  # type: ignore[arg-type]
            SimpleNamespace(id="user-1"),
            _request_with_forwarded_for(),
        )
    finally:
        auth.settings.trusted_proxies = old

    assert db.added.ip == "198.51.100.10"
