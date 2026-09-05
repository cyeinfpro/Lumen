from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException

from app import deps, redis_client, security
from app.db import get_db
from app.routes import auth, telegram


INVALID_SIGNATURES = [
    "",
    "a" * 63,
    "0" * 65,
    "g" * 64,
    "A" * 64,
    "\u00e9",
    "\u4e2d",
    "a" * 63 + "\n",
]


@pytest.mark.parametrize("signature", INVALID_SIGNATURES)
def test_session_and_csrf_reject_malformed_signatures(signature: str) -> None:
    cookie_payload = security.make_session_cookie("session-1").rsplit(".", 1)[0]
    csrf_nonce = security.make_csrf_token("session-1").rsplit(".", 1)[0]

    assert security.parse_session_cookie(f"{cookie_payload}.{signature}") is None
    assert not security.verify_csrf_token("session-1", f"{csrf_nonce}.{signature}")


def test_session_and_csrf_signature_match_and_mismatch() -> None:
    cookie = security.make_session_cookie("session-1")
    csrf = security.make_csrf_token("session-1")

    assert security.parse_session_cookie(cookie) == "session-1"
    assert security.verify_csrf_token("session-1", csrf)
    assert (
        security.parse_session_cookie(cookie.replace("session-1", "session-2")) is None
    )
    assert not security.verify_csrf_token("session-2", csrf)


class _Db:
    def __init__(self) -> None:
        self.queries = 0
        self.info: dict[str, str] = {}

    async def execute(self, _stmt):
        self.queries += 1
        session = SimpleNamespace(
            revoked_at=None,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        user = SimpleNamespace(id="user-1", deleted_at=None)
        return SimpleNamespace(first=lambda: (session, user))


@pytest.fixture
def auth_app():
    app = FastAPI()
    db = _Db()
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(auth.router, prefix="/auth")
    app.include_router(telegram.router_bot)

    @app.get("/session-user")
    async def session_user(user=Depends(deps.get_current_user)):
        return {"id": user.id}

    @app.post("/checked-csrf", dependencies=[Depends(deps.verify_csrf)])
    @app.post("/checked-csrf-session", dependencies=[Depends(deps.verify_csrf_session)])
    async def checked_csrf():
        return {"ok": True}

    return app, db


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/session-user", "/auth/csrf"])
@pytest.mark.parametrize("signature", ["short", "g" * 64, "\u00e9", "\u4e2d"])
async def test_session_cookie_routes_reject_malformed_signatures(
    auth_app, path, signature
):
    app, db = auth_app
    payload = security.make_session_cookie("session-1").rsplit(".", 1)[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            path,
            headers=[
                (b"cookie", f"session={payload}.{signature}".encode("utf-8")),
            ],
        )

    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "unauthenticated"
    assert db.queries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/session-user", "/auth/csrf"])
async def test_session_cookie_routes_accept_valid_signature(auth_app, path):
    app, db = auth_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            path,
            headers={
                "cookie": f"session={security.make_session_cookie('session-1')}",
            },
        )

    assert response.status_code == 200
    assert db.queries == 1
    if path == "/auth/csrf":
        assert security.verify_csrf_token("session-1", response.json()["csrf_token"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/checked-csrf", "/checked-csrf-session", "/auth/logout"]
)
@pytest.mark.parametrize("signature", ["short", "g" * 64, "\u00e9", "\u4e2d"])
async def test_csrf_routes_reject_malformed_signatures(auth_app, path, signature):
    app, db = auth_app
    nonce = security.make_csrf_token("session-1").rsplit(".", 1)[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            headers=[
                (
                    b"cookie",
                    f"session={security.make_session_cookie('session-1')}".encode(),
                ),
                (b"x-csrf-token", f"{nonce}.{signature}".encode("utf-8")),
            ],
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "csrf_failed"
    assert db.queries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/checked-csrf", "/checked-csrf-session"])
async def test_csrf_routes_accept_valid_signature(auth_app, path):
    app, db = auth_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            headers={
                "cookie": f"session={security.make_session_cookie('session-1')}",
                "x-csrf-token": security.make_csrf_token("session-1"),
            },
        )

    assert response.status_code == 200
    assert db.queries == 1
    assert db.info["lumen.durable_session_id"] == "session-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provided",
    [b"", b"wrong", b"\xe9", "\u4e2d".encode("utf-8"), b"\xa0" + b"s" * 32 + b"\xa0"],
)
@pytest.mark.parametrize("limited", [False, True])
async def test_bot_route_rejects_malformed_token_through_failure_limiter(
    auth_app,
    monkeypatch,
    provided,
    limited,
):
    app, db = auth_app
    calls: list[str] = []

    async def check(_redis, key):
        calls.append(key)
        if limited:
            raise HTTPException(
                status_code=429, detail={"error": {"code": "rate_limit"}}
            )

    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "s" * 32)
    monkeypatch.setattr(redis_client, "get_redis", lambda: object())
    monkeypatch.setattr(deps.BOT_TOKEN_FAILURE_LIMITER, "check", check)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/telegram/access-config", headers=[(b"x-bot-token", provided)]
        )

    assert response.status_code == (429 if limited else 401)
    assert response.json()["detail"]["error"]["code"] == (
        "rate_limit" if limited else "bot_unauthorized"
    )
    assert calls == ["rl:botauth:failed:127.0.0.1"]
    assert db.queries == 0


@pytest.mark.asyncio
async def test_bot_route_accepts_ascii_token_without_recording_failure(
    auth_app, monkeypatch
):
    app, _db = auth_app

    async def no_failure(*_args):
        pytest.fail("valid bot credentials must not consume the failure budget")

    async def setting(_db, _key, default=""):
        return default

    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "s" * 32)
    monkeypatch.setattr(deps.BOT_TOKEN_FAILURE_LIMITER, "check", no_failure)
    monkeypatch.setattr(telegram, "_get_setting_str", setting)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/telegram/access-config", headers={"x-bot-token": "  " + "s" * 32 + "  "}
        )

    assert response.status_code == 200
    assert response.json() == {"bot_enabled": True, "allowed_user_ids": ""}
