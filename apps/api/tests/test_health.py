from __future__ import annotations

import pytest

import app.main as main
from app.main import readyz


class OkRedis:
    async def ping(self) -> bool:
        return True


class BadRedis:
    async def ping(self) -> bool:
        raise RuntimeError("redis down")


class OkConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return None


class OkEngine:
    def connect(self):
        return OkConn()


class BadConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        raise RuntimeError("db down")


class BadEngine:
    def connect(self):
        return BadConn()


@pytest.mark.asyncio
async def test_readyz_reports_ok_when_dependencies_respond(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "engine", OkEngine())
    assert await readyz(redis=OkRedis()) == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_fails_when_redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "engine", OkEngine())
    with pytest.raises(Exception) as excinfo:
        await readyz(redis=BadRedis())
    assert getattr(excinfo.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_readyz_fails_when_db_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "engine", BadEngine())
    with pytest.raises(Exception) as excinfo:
        await readyz(redis=OkRedis())
    assert getattr(excinfo.value, "status_code", None) == 503
