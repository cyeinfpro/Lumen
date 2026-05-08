from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Request

from app.routes import admin_telegram


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/telegram/restart",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.asyncio
async def test_restart_bot_reports_publish_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Redis:
        async def publish(self, *_args):
            raise RuntimeError("redis down")

    class Db:
        def __init__(self) -> None:
            self.committed = False

        async def commit(self) -> None:
            self.committed = True

    async def fake_audit(*_args, **_kwargs) -> None:
        return None

    db = Db()
    monkeypatch.setattr(admin_telegram, "get_redis", lambda: Redis())
    monkeypatch.setattr(admin_telegram, "write_admin_audit", fake_audit)

    out = await admin_telegram.restart_bot(
        _request(),
        SimpleNamespace(id="admin-1"),
        db,  # type: ignore[arg-type]
    )

    assert out.ok is False
    assert out.receivers == 0
    assert out.error == "publish_failed"
    assert db.committed is True
