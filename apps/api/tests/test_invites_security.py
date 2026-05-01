from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Request

from app.routes import invites


class _Rows:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _Db:
    def __init__(self, row):
        self.row = row

    async def execute(self, _stmt):
        return _Rows(self.row)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/invite/token",
            "headers": [],
            "client": ("203.0.113.10", 12345),
        }
    )


@pytest.mark.asyncio
async def test_invite_preview_rejects_deleted_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop_check(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(invites.PUBLIC_PREVIEW_LIMITER, "check", noop_check)
    monkeypatch.setattr(invites, "get_redis", lambda: object())

    inv = SimpleNamespace(
        token="token",
        email=None,
        role="member",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        revoked_at=None,
        used_at=None,
    )
    creator = SimpleNamespace(deleted_at=datetime.now(timezone.utc))

    out = await invites.preview_invite("token", _request(), _Db((inv, creator)))  # type: ignore[arg-type]

    assert out.valid is False
    assert out.invalid_reason == "creator_deleted"
