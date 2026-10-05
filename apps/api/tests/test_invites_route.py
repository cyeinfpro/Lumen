from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from pydantic import ValidationError

from app.routes import invites


class _Result:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def all(self) -> list[Any]:
        return self.value if isinstance(self.value, list) else []


class _Db:
    def __init__(self, value: Any = None) -> None:
        self.value = value
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result(self.value)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/admin/invite_links/invite-1",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_admin_invite_requires_bound_email() -> None:
    with pytest.raises(ValidationError):
        invites._CreateInviteIn(role="admin")  # noqa: SLF001


@pytest.mark.asyncio
async def test_create_invite_link_checks_per_admin_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Limiter:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def check(self, _redis: Any, key: str) -> None:
            self.keys.append(key)
            raise invites._http("rate_limited", "too many invites", 429)  # noqa: SLF001

    limiter = Limiter()
    monkeypatch.setattr(invites, "ADMIN_INVITE_CREATE_LIMITER", limiter)
    monkeypatch.setattr(invites, "get_redis", lambda: object())

    with pytest.raises(Exception) as excinfo:
        await invites.create_invite_link(
            invites._CreateInviteIn(),  # noqa: SLF001
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            object(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 429
    assert limiter.keys == ["rl:admin:invite_links:create:admin-1"]


@pytest.mark.asyncio
async def test_revoke_invite_uses_admin_scope_not_creator_scope() -> None:
    db = _Db(None)

    with pytest.raises(Exception) as excinfo:
        await invites.revoke_invite_link(
            "invite-1",
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            db,  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 404
    rendered = str(db.statements[0])
    assert "invite_links.id" in rendered
    assert "WHERE invite_links.id = :id_1" in rendered
    assert "invite_links.created_by = " not in rendered


@pytest.mark.asyncio
async def test_list_invites_uses_admin_scope_not_creator_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_base_url(_request: Request, _db: Any) -> str:
        return "https://lumen.example"

    monkeypatch.setattr(invites, "resolve_public_base_url", fake_base_url)
    db = _Db([])

    out = await invites.list_invite_links(
        SimpleNamespace(id="admin-1", email="admin@example.test"),
        _request(),
        db,  # type: ignore[arg-type]
    )

    assert out == {"items": []}
    rendered = str(db.statements[0])
    assert "WHERE invite_links.created_by" not in rendered


@pytest.mark.asyncio
async def test_list_invites_redacts_tokens_but_creation_snapshot_can_reveal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_base_url(_request: Request, _db: Any) -> str:
        return "https://lumen.example"

    monkeypatch.setattr(invites, "resolve_public_base_url", fake_base_url)
    inv = SimpleNamespace(
        id="invite-1", token="secret-invite-token", email=None, role="member",
        expires_at=None, used_at=None, revoked_at=None,
        created_at=datetime.now(timezone.utc),
    )
    out = await invites.list_invite_links(
        SimpleNamespace(id="admin-1", email="admin@example.test"),
        _request(), _Db([(inv, None)]),  # type: ignore[arg-type]
    )
    listed = out["items"][0]
    assert listed.token == "redacted"
    assert "secret-invite-token" not in listed.model_dump_json()
    created = invites._to_out(inv, None, public_base_url="https://lumen.example")
    assert created.url == "https://lumen.example/invite/secret-invite-token"
