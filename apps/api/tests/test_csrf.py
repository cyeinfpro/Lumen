from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import Request
from starlette.responses import Response

from app.deps import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, verify_csrf
from app.routes.auth import refresh_csrf
from app.security import make_csrf_token, make_session_cookie, verify_csrf_token


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Db:
    async def execute(self, _stmt):
        session = SimpleNamespace(
            revoked_at=None,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        user = SimpleNamespace(deleted_at=None)
        return _ScalarResult((session, user))


def _request(*, session_id: str = "session-1", csrf: str | None = None, header: str | None = None) -> Request:
    csrf = csrf if csrf is not None else make_csrf_token(session_id)
    raw_session = make_session_cookie(session_id)
    cookies = f"{SESSION_COOKIE}={raw_session}; {CSRF_COOKIE}={csrf}"
    headers = [(b"cookie", cookies.encode())]
    if header is not None:
        headers.append((CSRF_HEADER.lower().encode(), header.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


@pytest.mark.asyncio
async def test_verify_csrf_accepts_token_bound_to_session() -> None:
    token = make_csrf_token("session-1")
    await verify_csrf(_request(session_id="session-1", csrf=token, header=token), _Db())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_verify_csrf_accepts_valid_header_when_cookie_is_stale() -> None:
    token = make_csrf_token("session-1")
    await verify_csrf(_request(session_id="session-1", csrf="stale-cookie", header=token), _Db())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_verify_csrf_rejects_missing_header() -> None:
    request = _request(session_id="session-1", header=None)
    try:
        await verify_csrf(request, _Db())  # type: ignore[arg-type]
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        raise AssertionError("expected CSRF failure")


@pytest.mark.asyncio
async def test_verify_csrf_rejects_mismatched_token() -> None:
    request = _request(session_id="session-1", header="not-the-cookie")
    try:
        await verify_csrf(request, _Db())  # type: ignore[arg-type]
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        raise AssertionError("expected CSRF failure")


@pytest.mark.asyncio
async def test_verify_csrf_rejects_token_from_another_session() -> None:
    token_from_other_session = make_csrf_token("session-2")
    request = _request(
        session_id="session-1",
        csrf=token_from_other_session,
        header=token_from_other_session,
    )
    try:
        await verify_csrf(request, _Db())  # type: ignore[arg-type]
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        raise AssertionError("expected CSRF failure")


def _csrf_refresh_request(session_id: str | None = "session-1") -> Request:
    headers = []
    if session_id is not None:
        headers.append(
            (
                b"cookie",
                f"{SESSION_COOKIE}={make_session_cookie(session_id)}".encode(),
            )
        )
    return Request(
        {"type": "http", "method": "GET", "path": "/auth/csrf", "headers": headers}
    )


@pytest.mark.asyncio
async def test_refresh_csrf_returns_session_bound_token_and_cookie() -> None:
    response = Response()

    out = await refresh_csrf(_csrf_refresh_request("session-1"), response, _Db())

    assert verify_csrf_token("session-1", out.csrf_token)
    set_cookie_headers = [
        value for name, value in response.raw_headers if name == b"set-cookie"
    ]
    assert any(
        value.startswith(f"{CSRF_COOKIE}={out.csrf_token}".encode())
        for value in set_cookie_headers
    )


@pytest.mark.asyncio
async def test_refresh_csrf_rejects_missing_session_cookie() -> None:
    with pytest.raises(Exception) as excinfo:
        await refresh_csrf(_csrf_refresh_request(None), Response(), _Db())

    assert getattr(excinfo.value, "status_code", None) == 401
