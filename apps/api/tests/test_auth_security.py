from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from app import security
from app import deps
from app.deps import SESSION_COOKIE, get_current_user
from app.routes import auth
from app.security import make_session_cookie
from lumen_core.schemas import LoginIn, SignupByokIn, SignupIn


class _Orig:
    def __init__(self, message: str, *, constraint_name: str | None = None) -> None:
        self.message = message
        if constraint_name is not None:
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    def __str__(self) -> str:
        return self.message


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


class _PasswordResetRedis:
    def __init__(
        self,
        token_key: str,
        *,
        user_id: str = "user-1",
        ttl_ms: int = 120_000,
    ) -> None:
        self.tokens = {token_key: user_id}
        self.token_ttls = {token_key: ttl_ms}
        self.claims: dict[str, dict[str, str | int]] = {}
        self.events: list[str] = []
        self.claim_calls = 0
        self.fail_consume_claim = False
        self._lock = asyncio.Lock()

    async def eval(self, script, _num_keys, *args):
        if script not in {
            auth._CLAIM_PASSWORD_RESET_TOKEN_LUA,
            auth._RESTORE_PASSWORD_RESET_TOKEN_LUA,
            auth._CONSUME_PASSWORD_RESET_CLAIM_LUA,
        }:
            return [1, 0]

        async with self._lock:
            if script == auth._CLAIM_PASSWORD_RESET_TOKEN_LUA:
                self.claim_calls += 1
                token_key, claim_key, owner = args
                user_id = self.tokens.get(token_key)
                ttl_ms = self.token_ttls.get(token_key, -2)
                if user_id is None or ttl_ms <= 0:
                    return [0, ""]
                if claim_key in self.claims:
                    return [2, ""]
                self.claims[claim_key] = {
                    "owner": owner,
                    "user_id": user_id,
                    "ttl_ms": ttl_ms,
                }
                self.tokens.pop(token_key)
                self.token_ttls.pop(token_key)
                self.events.append("redis.claim")
                return [1, user_id]

            if script == auth._RESTORE_PASSWORD_RESET_TOKEN_LUA:
                token_key, claim_key, owner = args
                claim = self.claims.get(claim_key)
                if claim is None or claim["owner"] != owner:
                    return 0
                if token_key in self.tokens or int(claim["ttl_ms"]) <= 0:
                    return -1
                self.tokens[token_key] = str(claim["user_id"])
                self.token_ttls[token_key] = int(claim["ttl_ms"])
                self.claims.pop(claim_key)
                self.events.append("redis.restore")
                return 1

            if self.fail_consume_claim:
                raise RuntimeError("redis unavailable")
            claim_key, owner = args
            claim = self.claims.get(claim_key)
            if claim is None or claim["owner"] != owner:
                return 0
            self.claims.pop(claim_key)
            self.events.append("redis.consume_claim")
            return 1


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
async def test_runtime_defaults_include_navigation_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "generation.fast_default": "0",
        "ui.nav.studio_visible": "1",
        "ui.nav.video_visible": "0",
        "ui.nav.projects_visible": "1",
        "ui.nav.assets_visible": "0",
        "canvas.enabled": "1",
    }

    async def fake_get_setting(_db, spec):
        return values.get(spec.key)

    monkeypatch.setattr(auth, "get_setting", fake_get_setting)

    defaults = await auth._runtime_defaults(object())  # noqa: SLF001

    assert defaults.fast is False
    assert defaults.nav_visibility.studio is True
    assert defaults.nav_visibility.video is False
    assert defaults.nav_visibility.projects is True
    assert defaults.nav_visibility.assets is False
    assert defaults.canvas_enabled is True


@pytest.mark.asyncio
async def test_get_current_user_rejects_soft_deleted_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def allow_failed_session_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        deps.SESSION_VALIDATION_FAILURE_LIMITER, "check", allow_failed_session_record
    )

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
async def test_login_checks_admin_limiter_before_db_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_keys: list[str] = []

    class Db:
        async def execute(self, _stmt):
            raise AssertionError("admin login limiter must run before user lookup")

    async def fake_check(_redis, key: str, cost: int = 1) -> None:
        seen_keys.append(key)
        raise auth._bad("rate_limited", "too many login attempts", 429)

    monkeypatch.setattr(auth, "get_redis", lambda: object())
    monkeypatch.setattr(auth.AUTH_ADMIN_LOGIN_LIMITER, "check", fake_check)

    with pytest.raises(Exception) as excinfo:
        await auth.login(
            LoginIn(email="member@example.com", password="bad-password"),
            _request(method="POST"),
            Response(),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 429
    assert len(seen_keys) == 1
    assert seen_keys[0].startswith("rl:auth:admin_login:127.0.0.1:")


@pytest.mark.asyncio
async def test_signup_integrity_error_returns_email_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConflictDb(_Db):
        async def flush(self):
            raise IntegrityError(
                "insert users",
                {},
                _Orig(
                    'duplicate key value violates unique constraint "uq_users_email_active"',
                    constraint_name="uq_users_email_active",
                ),
            )

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
async def test_signup_integrity_error_does_not_misclassify_non_email_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConflictDb(_Db):
        async def flush(self):
            raise IntegrityError(
                "insert sessions",
                {},
                _Orig(
                    'duplicate key value violates unique constraint "auth_sessions_pkey"',
                    constraint_name="auth_sessions_pkey",
                ),
            )

    monkeypatch.setattr(auth, "hash_password", lambda _plain: "hashed")
    db = ConflictDb(results=[None, SimpleNamespace(email="new@example.com")])

    with pytest.raises(Exception) as excinfo:
        await auth.signup(
            SignupIn(email="new@example.com", password="password123"),
            _request(method="POST"),
            Response(),
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 503
    assert excinfo.value.detail["error"]["code"] == "signup_unavailable"
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_signup_byok_integrity_error_returns_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_byok_settings(_db):
        return SimpleNamespace(
            mode_enabled=True,
            byok_signup_enabled=True,
            byok_signup_bypasses_allowlist=False,
        )

    async def no_audit(*_args, **_kwargs):
        return True

    class ConflictDb(_Db):
        async def flush(self):
            raise IntegrityError(
                "insert users",
                {},
                _Orig(
                    'duplicate key value violates unique constraint "uq_users_email_active"',
                    constraint_name="uq_users_email_active",
                ),
            )

    pending = SimpleNamespace(
        consumed_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        supplier_id="supplier-1",
        key_ciphertext="cipher",
        key_hash="hash",
        key_hint="sk-test",
        verified_at=datetime.now(timezone.utc),
    )
    allow = SimpleNamespace(email="race-byok@example.com")
    db = ConflictDb(results=[None, pending, allow])
    monkeypatch.setattr(auth, "read_byok_settings", fake_byok_settings)
    monkeypatch.setattr(auth, "hash_password", lambda _plain: "hashed")
    monkeypatch.setattr(auth, "write_audit_isolated", no_audit)

    with pytest.raises(Exception) as excinfo:
        await auth.signup_byok(
            SignupByokIn(
                email="race-byok@example.com",
                password="password123",
                verification_token="verify-token",
            ),
            _request(method="POST"),
            Response(),
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_verification_token"
    assert pending.consumed_at is None
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
    # Keep exercising the route-level error envelope even when the shared
    # schema rejects short passwords during request parsing.
    weak_signup = SignupIn.model_construct(
        email="new@example.com",
        password="short",
        display_name="",
        invite_token=None,
    )
    with pytest.raises(Exception) as excinfo:
        await auth.signup(
            weak_signup,
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
    token_key = auth._password_reset_key("reset-token")
    claim_key = auth._password_reset_claim_key("reset-token")
    redis = _PasswordResetRedis(token_key)

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
            assert token_key not in redis.tokens
            redis.events.append("db.commit")
            self.committed = True

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
    assert token_key not in redis.tokens
    assert claim_key not in redis.claims
    assert redis.events == ["redis.claim", "db.commit", "redis.consume_claim"]
    assert "FOR UPDATE" in str(db.user_select).upper()


@pytest.mark.asyncio
async def test_password_reset_confirm_restores_token_when_execute_fails_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_key = auth._password_reset_key("reset-token")
    claim_key = auth._password_reset_claim_key("reset-token")
    redis = _PasswordResetRedis(token_key, ttl_ms=87_654)

    class Db:
        def __init__(self) -> None:
            self.user = SimpleNamespace(
                id="user-1", deleted_at=None, password_hash="old"
            )
            self.committed = False
            self.rolled_back = False

        async def execute(self, stmt):
            if "auth_sessions" in str(stmt).lower():
                raise RuntimeError("database unavailable before commit")
            return _ScalarResult(self.user)

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            self.rolled_back = True
            redis.events.append("db.rollback")

    db = Db()
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth, "hash_password", lambda plain: f"hashed:{plain}")

    with pytest.raises(RuntimeError, match="database unavailable before commit"):
        await auth.password_reset_confirm(
            auth.PasswordResetConfirmIn(
                token="reset-token", new_password="new-password"
            ),
            _request(method="POST"),
            db,  # type: ignore[arg-type]
        )

    assert db.committed is False
    assert db.rolled_back is True
    assert redis.tokens[token_key] == "user-1"
    assert redis.token_ttls[token_key] == 87_654
    assert claim_key not in redis.claims
    assert redis.events == [
        "redis.claim",
        "db.rollback",
        "redis.restore",
    ]


@pytest.mark.asyncio
async def test_password_reset_confirm_does_not_restore_token_when_commit_applies_then_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token_key = auth._password_reset_key("reset-token")
    claim_key = auth._password_reset_claim_key("reset-token")
    redis = _PasswordResetRedis(token_key, ttl_ms=87_654)

    class Db:
        def __init__(self) -> None:
            self.user = SimpleNamespace(
                id="user-1", deleted_at=None, password_hash="old"
            )
            self.sessions_revoked = False
            self.persisted_password_hash = "old"
            self.persisted_sessions_revoked = False
            self.rolled_back = False

        async def execute(self, stmt):
            if "auth_sessions" not in str(stmt).lower():
                return _ScalarResult(self.user)
            self.sessions_revoked = True
            return None

        async def commit(self) -> None:
            self.persisted_password_hash = self.user.password_hash
            self.persisted_sessions_revoked = self.sessions_revoked
            redis.events.append("db.commit_applied")
            raise RuntimeError("commit acknowledgement lost")

        async def rollback(self) -> None:
            self.rolled_back = True
            redis.events.append("db.rollback")

    db = Db()
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth, "hash_password", lambda plain: f"hashed:{plain}")

    with pytest.raises(HTTPException) as excinfo:
        await auth.password_reset_confirm(
            auth.PasswordResetConfirmIn(
                token="reset-token", new_password="new-password"
            ),
            _request(method="POST"),
            db,  # type: ignore[arg-type]
        )

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"]["code"] == "reset_outcome_uncertain"
    assert db.persisted_password_hash == "hashed:new-password"
    assert db.persisted_sessions_revoked is True
    assert db.rolled_back is True
    assert token_key not in redis.tokens
    assert claim_key not in redis.claims
    assert redis.events == [
        "redis.claim",
        "db.commit_applied",
        "db.rollback",
        "redis.consume_claim",
    ]
    assert "password_reset_commit_outcome_uncertain" in caplog.text
    assert "reset-token" not in caplog.text


@pytest.mark.asyncio
async def test_password_reset_claim_restore_preserves_ttl_and_owner() -> None:
    token_key = auth._password_reset_key("reset-token")
    claim_key = auth._password_reset_claim_key("reset-token")
    redis = _PasswordResetRedis(token_key, ttl_ms=54_321)

    claimed_user_id = await auth._claim_password_reset_token(  # noqa: SLF001
        redis,
        token_key,
        claim_key,
        owner="right-owner",
    )

    assert claimed_user_id == "user-1"
    assert token_key not in redis.tokens
    assert redis.claims[claim_key] == {
        "owner": "right-owner",
        "user_id": "user-1",
        "ttl_ms": 54_321,
    }
    assert (
        await auth._restore_password_reset_token(  # noqa: SLF001
            redis,
            token_key,
            claim_key,
            owner="wrong-owner",
        )
        is False
    )
    assert token_key not in redis.tokens
    assert claim_key in redis.claims
    assert (
        await auth._restore_password_reset_token(  # noqa: SLF001
            redis,
            token_key,
            claim_key,
            owner="right-owner",
        )
        is True
    )
    assert redis.tokens[token_key] == "user-1"
    assert redis.token_ttls[token_key] == 54_321
    assert claim_key not in redis.claims


@pytest.mark.asyncio
async def test_password_reset_confirm_stays_consumed_when_redis_fails_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token_key = auth._password_reset_key("reset-token")
    claim_key = auth._password_reset_claim_key("reset-token")
    redis = _PasswordResetRedis(token_key)
    redis.fail_consume_claim = True

    class Db:
        def __init__(self) -> None:
            self.user = SimpleNamespace(
                id="user-1", deleted_at=None, password_hash="old"
            )
            self.committed = False

        async def execute(self, stmt):
            if "auth_sessions" not in str(stmt).lower():
                return _ScalarResult(self.user)
            return None

        async def commit(self) -> None:
            self.committed = True

    db = Db()
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth, "hash_password", lambda plain: f"hashed:{plain}")

    out = await auth.password_reset_confirm(
        auth.PasswordResetConfirmIn(token="reset-token", new_password="new-password"),
        _request(method="POST"),
        db,  # type: ignore[arg-type]
    )

    assert out.ok is True
    assert db.committed is True
    assert token_key not in redis.tokens
    assert claim_key in redis.claims
    assert "password_reset_claim_consume_failed" in caplog.text
    assert "reset-token" not in caplog.text


@pytest.mark.asyncio
async def test_password_reset_confirm_concurrent_submit_commits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_key = auth._password_reset_key("reset-token")
    redis = _PasswordResetRedis(token_key)
    commit_count = 0

    class Db:
        def __init__(self) -> None:
            self.user = SimpleNamespace(
                id="user-1", deleted_at=None, password_hash="old"
            )

        async def execute(self, stmt):
            if "auth_sessions" not in str(stmt).lower():
                return _ScalarResult(self.user)
            return None

        async def commit(self) -> None:
            nonlocal commit_count
            commit_count += 1

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth, "hash_password", lambda plain: f"hashed:{plain}")

    async def confirm(db: Db):
        return await auth.password_reset_confirm(
            auth.PasswordResetConfirmIn(
                token="reset-token", new_password="new-password"
            ),
            _request(method="POST"),
            db,  # type: ignore[arg-type]
        )

    results = await asyncio.gather(
        confirm(Db()),
        confirm(Db()),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, auth.OkOut)]
    failures = [result for result in results if isinstance(result, HTTPException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].detail["error"]["code"] == "invalid_token"
    assert commit_count == 1
    assert token_key not in redis.tokens


@pytest.mark.asyncio
async def test_password_reset_confirm_restores_token_after_user_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_key = auth._password_reset_key("reset-token")
    claim_key = auth._password_reset_claim_key("reset-token")
    redis = _PasswordResetRedis(token_key)

    async def reject_user(_redis, _key):
        raise HTTPException(status_code=429, detail={"error": {"code": "rate_limit"}})

    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(auth._PASSWORD_RESET_CONFIRM_USER_LIMITER, "check", reject_user)

    with pytest.raises(HTTPException) as excinfo:
        await auth.password_reset_confirm(
            auth.PasswordResetConfirmIn(
                token="reset-token", new_password="new-password"
            ),
            _request(method="POST"),
            _Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 429
    assert redis.tokens[token_key] == "user-1"
    assert claim_key not in redis.claims


@pytest.mark.asyncio
async def test_password_reset_confirm_token_limiter_runs_before_atomic_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_token(_redis, _key):
        raise HTTPException(status_code=429, detail={"error": {"code": "rate_limit"}})

    token_key = auth._password_reset_key("reset-token")
    redis = _PasswordResetRedis(token_key)
    monkeypatch.setattr(auth, "get_redis", lambda: redis)
    monkeypatch.setattr(
        auth._PASSWORD_RESET_CONFIRM_TOKEN_LIMITER, "check", reject_token
    )

    with pytest.raises(HTTPException) as excinfo:
        await auth.password_reset_confirm(
            auth.PasswordResetConfirmIn(
                token="reset-token", new_password="new-password"
            ),
            _request(method="POST"),
            _Db(),  # type: ignore[arg-type]
        )

    assert excinfo.value.status_code == 429
    assert redis.claim_calls == 0
    assert redis.tokens[token_key] == "user-1"


@pytest.mark.asyncio
async def test_password_reset_confirm_claim_conflict_does_not_consume_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_key = auth._password_reset_key("reset-token")
    claim_key = auth._password_reset_claim_key("reset-token")
    redis = _PasswordResetRedis(token_key)
    redis.claims[claim_key] = {
        "owner": "other-owner",
        "user_id": "user-1",
        "ttl_ms": 120_000,
    }
    monkeypatch.setattr(auth, "get_redis", lambda: redis)

    with pytest.raises(Exception) as excinfo:
        await auth.password_reset_confirm(
            auth.PasswordResetConfirmIn(
                token="reset-token", new_password="new-password"
            ),
            _request(method="POST"),
            _Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_token"
    assert redis.tokens[token_key] == "user-1"
    assert redis.claims[claim_key]["owner"] == "other-owner"


@pytest.mark.asyncio
async def test_failed_session_validation_limiter_propagates_http_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_429(*_args, **_kwargs):
        raise HTTPException(status_code=429, detail={"error": {"code": "rate_limit"}})

    monkeypatch.setattr(deps.SESSION_VALIDATION_FAILURE_LIMITER, "check", raise_429)

    with pytest.raises(HTTPException) as excinfo:
        await deps._record_failed_session_validation(_request())

    assert excinfo.value.status_code == 429


@pytest.mark.asyncio
async def test_bot_auth_failure_limiter_propagates_http_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_429(*_args, **_kwargs):
        raise HTTPException(status_code=429, detail={"error": {"code": "rate_limit"}})

    monkeypatch.setattr(deps.BOT_TOKEN_FAILURE_LIMITER, "check", raise_429)

    with pytest.raises(HTTPException) as excinfo:
        await deps._record_bot_auth_failure(_request())

    assert excinfo.value.status_code == 429


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
