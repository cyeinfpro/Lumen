"""admin_proxies PUT 回归测试：重命名保留凭据 + 共享行写锁（sqlite 上为 no-op）。

覆盖 update_proxies 的两个 P2 修复：
- 重命名代理时按「host+port+username」端点身份回退，保留已存 password/private_key
- 读-改-写前拿共享行咨询锁（lock_providers_config，PostgreSQL 上才生效）
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen_core.models import Base, SystemSetting
from lumen_core.schemas import ProviderProxyIn, ProvidersUpdateIn
from app.services.admin_model_cache import AdminModelCache


SEED_CONFIG = {
    "providers": [
        {
            "name": "manual",
            "base_url": "https://upstream.example/v1",
            "api_key": "sk-secret",
        }
    ],
    "proxies": [
        {
            "name": "egress-old",
            "type": "socks5",
            "host": "10.0.0.1",
            "port": 1080,
            "username": "alice",
            "password": "s3cret-pass",
            "private_key_path": "/keys/egress.pem",
            "enabled": True,
        }
    ],
}


class _StubPipe:
    def __init__(self) -> None:
        self.calls = 0

    def hgetall(self, _key: str) -> "_StubPipe":
        self.calls += 1
        return self

    def exists(self, _key: str) -> "_StubPipe":
        self.calls += 1
        return self

    async def execute(self) -> list[dict[str, Any]]:
        return [{} for _ in range(self.calls)]


class _StubRedis:
    def pipeline(self, transaction: bool = False) -> _StubPipe:  # noqa: FBT001,FBT002
        return _StubPipe()

    async def exists(self, _key: str) -> int:
        return 0

    async def hgetall(self, _key: str) -> dict[str, Any]:
        return {}


def _admin_request() -> Request:
    cache = AdminModelCache()
    runtime = SimpleNamespace(admin_models=lambda: cache)
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/proxies",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "app": SimpleNamespace(state=SimpleNamespace(runtime=runtime)),
        }
    )


@asynccontextmanager
async def _session_scope() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[SystemSetting.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(session: AsyncSession) -> None:
    session.add(
        SystemSetting(key="providers", value=json.dumps(SEED_CONFIG, ensure_ascii=False))
    )
    await session.commit()


async def _stored_config(session: AsyncSession) -> dict[str, Any]:
    row = (
        await session.execute(
            select(SystemSetting.value).where(SystemSetting.key == "providers")
        )
    ).scalar_one()
    return json.loads(row)


async def _run_update(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    items: list[ProviderProxyIn],
) -> None:
    from app.routes import admin_proxies

    monkeypatch.setattr(admin_proxies, "get_redis", lambda: _StubRedis())
    monkeypatch.setattr(admin_proxies, "write_admin_audit", _async_noop_audit)
    admin = SimpleNamespace(id="admin-1", email="admin@example.com")
    await admin_proxies.update_proxies(
        body=admin_proxies.ProxiesUpdateIn(items=items),
        request=_admin_request(),
        admin=admin,
        db=session,
    )


@pytest.mark.asyncio
async def test_update_proxies_rename_preserves_password_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session_scope() as session:
        await _seed(session)

        # 重命名：name 变了，password/private_key 留空 → 应按端点身份保留旧凭据
        await _run_update(
            monkeypatch,
            session,
            [
                ProviderProxyIn(
                    name="egress-new",
                    type="socks5",
                    host="10.0.0.1",
                    port=1080,
                    username="alice",
                    password="",
                    private_key_path=None,
                )
            ],
        )

        config = await _stored_config(session)
        assert [p["name"] for p in config["proxies"]] == ["egress-new"]
        assert config["proxies"][0]["password"] == "s3cret-pass"
        assert config["proxies"][0]["private_key_path"] == "/keys/egress.pem"
        # providers items 不动
        assert config["providers"] == SEED_CONFIG["providers"]


@pytest.mark.asyncio
async def test_update_proxies_same_name_empty_password_keeps_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session_scope() as session:
        await _seed(session)

        await _run_update(
            monkeypatch,
            session,
            [
                ProviderProxyIn(
                    name="egress-old",
                    type="socks5",
                    host="10.0.0.1",
                    port=1080,
                    username="alice",
                    password="",
                    private_key_path=None,
                )
            ],
        )

        config = await _stored_config(session)
        assert config["proxies"][0]["password"] == "s3cret-pass"
        assert config["proxies"][0]["private_key_path"] == "/keys/egress.pem"


@pytest.mark.asyncio
async def test_update_proxies_explicit_password_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session_scope() as session:
        await _seed(session)

        await _run_update(
            monkeypatch,
            session,
            [
                ProviderProxyIn(
                    name="egress-old",
                    type="socks5",
                    host="10.0.0.1",
                    port=1080,
                    username="alice",
                    password="new-pass",
                    private_key_path="/keys/other.pem",
                )
            ],
        )

        config = await _stored_config(session)
        assert config["proxies"][0]["password"] == "new-pass"
        assert config["proxies"][0]["private_key_path"] == "/keys/other.pem"


@pytest.mark.asyncio
async def test_update_proxies_brand_new_proxy_gets_no_old_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session_scope() as session:
        await _seed(session)

        # 全新代理（端点与旧代理完全不同）→ 不应捡到旧代理的密码
        await _run_update(
            monkeypatch,
            session,
            [
                ProviderProxyIn(
                    name="brand-new",
                    type="socks5",
                    host="203.0.113.9",
                    port=1080,
                    username=None,
                    password="",
                    private_key_path=None,
                )
            ],
        )

        config = await _stored_config(session)
        assert config["proxies"][0]["password"] == ""
        assert config["proxies"][0]["private_key_path"] is None


@pytest.mark.asyncio
async def test_lock_providers_config_noop_on_sqlite() -> None:
    from app.services.provider_config import lock_providers_config

    async with _session_scope() as session:
        # sqlite 上咨询锁是 no-op，不报错即可
        await lock_providers_config(session)


def test_provider_proxy_rows_rename_fallback_keeps_password() -> None:
    from app.routes import providers

    old_rows = [
        {
            "name": "egress-old",
            "type": "socks5",
            "host": "10.0.0.1",
            "port": 1080,
            "username": "alice",
            "password": "s3cret-pass",
            "enabled": True,
        }
    ]
    body = ProvidersUpdateIn(
        items=[],
        proxies=[
            ProviderProxyIn(
                name="egress-new",
                type="socks5",
                host="10.0.0.1",
                port=1080,
                username="alice",
                password="",
            )
        ],
    )
    rows = providers._provider_proxy_rows(body, {}, old_rows)
    assert rows[0]["name"] == "egress-new"
    assert rows[0]["password"] == "s3cret-pass"


def test_provider_proxy_rows_brand_new_proxy_keeps_empty_password() -> None:
    from app.routes import providers

    old_rows = [
        {
            "name": "egress-old",
            "type": "socks5",
            "host": "10.0.0.1",
            "port": 1080,
            "username": "alice",
            "password": "s3cret-pass",
            "enabled": True,
        }
    ]
    body = ProvidersUpdateIn(
        items=[],
        proxies=[
            ProviderProxyIn(
                name="brand-new",
                type="socks5",
                host="203.0.113.9",
                port=1080,
                username=None,
                password="",
            )
        ],
    )
    rows = providers._provider_proxy_rows(body, {}, old_rows)
    assert rows[0]["password"] == ""


async def _async_noop_audit(*_args: Any, **_kwargs: Any) -> None:
    assert _kwargs["autocommit"] is False
    return None
