from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, Request
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from app import security
from app.deps import SESSION_COOKIE, get_current_user
from app.routes import auth
from app.security import make_session_cookie
from lumen_core.schemas import LoginIn, SignupByokIn, SignupIn


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
        self.results_seen = []
        self.added = []
        self.rolled_back = False

    async def execute(self, stmt):
        self.results_seen.append(stmt)
        value = self.results.pop(0) if self.results else None
        sql = str(stmt).upper()
        if (
            getattr(value, "deleted_at", None) is not None
            and "DELETED_AT IS NULL" in sql
        ):
            value = None
        return _ScalarResult(value)

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
        headers.append(
            (b"cookie", f"{SESSION_COOKIE}={make_session_cookie(session_id)}".encode())
        )
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


def _assert_active_user_filter(stmt) -> None:
    sql = str(stmt).upper()
    assert "DELETED_AT IS NULL" in sql


def test_verify_password_propagates_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_hashed: str, _plain: str) -> bool:
        raise RuntimeError("argon2 unavailable")

    monkeypatch.setattr(security, "_ph", SimpleNamespace(verify=boom))

    with pytest.raises(RuntimeError):
        security.verify_password("hash", "pw")


def test_auth_cookies_are_samesite_strict_outside_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth.settings, "app_env", "prod")
    response = Response()

    auth._set_auth_cookies(response, "session-1", "csrf-1")

    cookies = [
        value.decode().lower()
        for name, value in response.raw_headers
        if name == b"set-cookie"
    ]
    assert cookies
    assert all("samesite=strict" in cookie for cookie in cookies)


def test_auth_cookies_use_lax_for_all_dev_env_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env in ("dev", "development", "local", "test"):
        monkeypatch.setattr(auth.settings, "app_env", env)
        response = Response()

        auth._set_auth_cookies(response, "session-1", "csrf-1")

        cookies = [
            value.decode().lower()
            for name, value in response.raw_headers
            if name == b"set-cookie"
        ]
        assert cookies
        assert all("samesite=lax" in cookie for cookie in cookies)


def test_clear_auth_cookies_matches_cookie_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth.settings, "app_env", "production")
    response = Response()

    auth._clear_auth_cookies(response)

    cookies = [
        value.decode().lower()
        for name, value in response.raw_headers
        if name == b"set-cookie"
    ]
    session_cookie = next(cookie for cookie in cookies if cookie.startswith("session="))
    csrf_cookie = next(cookie for cookie in cookies if cookie.startswith("csrf="))
    assert "secure" in session_cookie
    assert "httponly" in session_cookie
    assert "samesite=strict" in session_cookie
    assert "secure" in csrf_cookie
    assert "httponly" not in csrf_cookie
    assert "samesite=strict" in csrf_cookie


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
async def test_login_runs_dummy_verify_when_user_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_hashes: list[str] = []
    seen_admin_limit_keys: list[str] = []

    def fake_verify(hashed: str, _plain: str) -> bool:
        seen_hashes.append(hashed)
        return False

    async def fake_admin_check(_redis, key: str, cost: int = 1) -> None:
        seen_admin_limit_keys.append(key)

    monkeypatch.setattr(auth, "get_redis", lambda: object())
    monkeypatch.setattr(auth.AUTH_ADMIN_LOGIN_LIMITER, "check", fake_admin_check)
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
    assert len(seen_admin_limit_keys) == 1


@pytest.mark.asyncio
async def test_admin_login_uses_dedicated_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_keys: list[str] = []
    user = SimpleNamespace(
        id="admin-1",
        email="admin@example.com",
        role="admin",
        password_hash="hash",
        deleted_at=None,
    )

    async def fake_check(_redis, key: str, cost: int = 1) -> None:
        seen_keys.append(key)

    monkeypatch.setattr(auth, "get_redis", lambda: object())
    monkeypatch.setattr(auth.AUTH_ADMIN_LOGIN_LIMITER, "check", fake_check)
    monkeypatch.setattr(auth, "verify_password", lambda *_args: False)

    with pytest.raises(Exception) as excinfo:
        await auth.login(
            LoginIn(email="admin@example.com", password="bad-password"),
            _request(method="POST"),
            Response(),
            _Db(results=[user]),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 401
    assert len(seen_keys) == 1
    assert seen_keys[0].startswith("rl:auth:admin_login:127.0.0.1:")


@pytest.mark.asyncio
async def test_signup_integrity_error_returns_email_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
async def test_signup_email_check_ignores_soft_deleted_users() -> None:
    soft_deleted = SimpleNamespace(
        id="old-user",
        email="reuse@example.com",
        deleted_at=datetime.now(timezone.utc),
    )
    db = _Db(results=[soft_deleted])

    with pytest.raises(Exception):
        await auth.signup(
            SignupIn(email="reuse@example.com", password="password123"),
            _request(method="POST"),
            Response(),
            db,  # type: ignore[arg-type]
        )

    _assert_active_user_filter(db.results_seen[0])


@pytest.mark.asyncio
async def test_signup_byok_email_check_ignores_soft_deleted_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_byok_settings(_db):
        return SimpleNamespace(mode_enabled=True, byok_signup_enabled=True)

    soft_deleted = SimpleNamespace(
        id="old-byok-user",
        email="reuse-byok@example.com",
        deleted_at=datetime.now(timezone.utc),
    )
    db = _Db(results=[soft_deleted])
    monkeypatch.setattr(auth, "read_byok_settings", fake_byok_settings)

    with pytest.raises(Exception):
        await auth.signup_byok(
            SignupByokIn(
                email="reuse-byok@example.com",
                password="password123",
                verification_token="verify-token",
            ),
            _request(method="POST"),
            Response(),
            db,  # type: ignore[arg-type]
        )

    _assert_active_user_filter(db.results_seen[0])


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
        def __init__(self) -> None:
            self.calls = []

        async def eval(self, *_args):
            return [1, 0]

        async def set(self, key: str, value: str, *, ex: int) -> None:
            self.calls.append((key, value, ex))

    redis = Redis()
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: "reset-token")

    out = await auth.password_reset_request(
        auth.PasswordResetRequestIn(email="missing@example.com"),
        _request(method="POST"),
        BackgroundTasks(),
        _Db(results=[None]),  # type: ignore[arg-type]
    )

    assert out.ok is True
    assert redis.calls == []


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
        BackgroundTasks(),
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
async def test_password_reset_request_sends_reset_email_for_existing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        async def eval(self, *_args):
            return [1, 0]

        async def set(self, *_args, **_kwargs) -> None:
            return None

    sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_send_password_reset_email(*args: object, **kwargs: object) -> None:
        sent.append((args, kwargs))

    user = SimpleNamespace(id="user-1", deleted_at=None)
    monkeypatch.setattr(auth, "get_redis", lambda: Redis())
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: "reset-token")
    monkeypatch.setattr(
        auth,
        "send_password_reset_email",
        fake_send_password_reset_email,
        raising=False,
    )
    background = BackgroundTasks()

    out = await auth.password_reset_request(
        auth.PasswordResetRequestIn(email="User@Example.COM"),
        _request(method="POST"),
        background,
        _Db(results=[user]),  # type: ignore[arg-type]
    )
    await background()

    assert out.ok is True
    payload = repr(sent)
    assert "user@example.com" in payload
    assert "reset-token" in payload


@pytest.mark.asyncio
async def test_password_reset_request_hides_redis_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        async def eval(self, *_args):
            return [1, 0]

        async def set(self, *_args, **_kwargs) -> None:
            raise RuntimeError("redis down")

    user = SimpleNamespace(id="user-1", deleted_at=None)
    monkeypatch.setattr(auth, "get_redis", lambda: Redis())

    out = await auth.password_reset_request(
        auth.PasswordResetRequestIn(email="user@example.com"),
        _request(method="POST"),
        BackgroundTasks(),
        _Db(results=[user]),  # type: ignore[arg-type]
    )

    assert out.ok is True


@pytest.mark.asyncio
async def test_password_reset_request_deletes_token_when_email_delivery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.deleted = []

        async def eval(self, *_args):
            return [1, 0]

        async def set(self, *_args, **_kwargs) -> None:
            return None

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    async def fail_send(**_kwargs) -> None:
        raise auth.EmailDeliveryError("smtp down")

    async def fake_public_base_url(_request, _db) -> str:
        return "https://lumen.example"

    redis = Redis()
    user = SimpleNamespace(id="user-1", deleted_at=None)
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: "reset-token")
    monkeypatch.setattr(auth, "send_password_reset_email", fail_send)
    monkeypatch.setattr(auth, "resolve_public_base_url", fake_public_base_url)

    background = BackgroundTasks()
    out = await auth.password_reset_request(
        auth.PasswordResetRequestIn(email="user@example.com"),
        _request(method="POST"),
        background,
        _Db(results=[user]),  # type: ignore[arg-type]
    )
    await background()

    assert out.ok is True
    assert redis.deleted == [auth._password_reset_key("reset-token")]


@pytest.mark.asyncio
async def test_password_reset_request_deletes_token_when_email_task_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.deleted = []

        async def eval(self, *_args):
            return [1, 0]

        async def set(self, *_args, **_kwargs) -> None:
            return None

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    async def fail_send(**_kwargs) -> None:
        raise RuntimeError("smtp crashed")

    async def fake_public_base_url(_request, _db) -> str:
        return "https://lumen.example"

    redis = Redis()
    user = SimpleNamespace(id="user-1", deleted_at=None)
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: "reset-token")
    monkeypatch.setattr(auth, "send_password_reset_email", fail_send)
    monkeypatch.setattr(auth, "resolve_public_base_url", fake_public_base_url)

    background = BackgroundTasks()
    out = await auth.password_reset_request(
        auth.PasswordResetRequestIn(email="user@example.com"),
        _request(method="POST"),
        background,
        _Db(results=[user]),  # type: ignore[arg-type]
    )
    await background()

    assert out.ok is True
    assert redis.deleted == [auth._password_reset_key("reset-token")]


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
            self.user = SimpleNamespace(
                id="user-1", deleted_at=None, password_hash="old"
            )
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
async def test_password_reset_confirm_consumes_token_before_user_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.deleted = []

        async def eval(self, _lua, _keys, key: str, *_args):
            if key == "rl:pwd_reset_confirm:user:user-1":
                return [0, 1000]
            return [1, 0]

        async def get(self, _key: str) -> str:
            return "user-1"

        async def getdel(self, key: str) -> str:
            self.deleted.append(key)
            return "user-1"

    redis = Redis()
    monkeypatch.setattr(auth, "get_redis", lambda: redis)

    with pytest.raises(Exception) as excinfo:
        await auth.password_reset_confirm(
            auth.PasswordResetConfirmIn(
                token="reset-token", new_password="new-password"
            ),
            _request(method="POST"),
            _Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 429
    assert redis.deleted == [auth._password_reset_key("reset-token")]


@pytest.mark.asyncio
async def test_create_session_uses_trusted_forwarded_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
