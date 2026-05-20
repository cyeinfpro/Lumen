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

    def scalar_one_or_none(self):
        return self.row


class _Db:
    def __init__(self, row):
        self.row = row
        self.committed = False

    async def execute(self, _stmt):
        return _Rows(self.row)

    async def commit(self) -> None:
        self.committed = True


class _RevokeDb:
    def __init__(self, row):
        self.row = row
        self.statement = None
        self.committed = False

    async def execute(self, stmt):
        self.statement = stmt
        return _Rows(self.row)

    async def commit(self) -> None:
        self.committed = True


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


@pytest.mark.asyncio
async def test_revoke_invite_allows_any_admin_to_manage_invite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_write_audit(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(invites, "write_audit", fake_write_audit)
    monkeypatch.setattr(invites, "request_ip_hash", lambda _request: "ip-hash")

    inv = SimpleNamespace(id="invite-1", revoked_at=None)
    db = _RevokeDb(inv)
    admin = SimpleNamespace(id="admin-other", email="other@example.com")

    await invites.revoke_invite_link(
        "invite-1",
        _request(),
        admin,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert "WHERE invite_links.id = :id_1" in str(db.statement)
    assert "invite_links.created_by = " not in str(db.statement)
    assert inv.revoked_at is not None
    assert db.committed is True
