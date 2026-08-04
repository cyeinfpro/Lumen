from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import proxy_manager  # noqa: E402


def test_normalize_proxy_url_preserves_compose_service_hostname() -> None:
    assert (
        proxy_manager.normalize_proxy_url("socks5h://api:41560")
        == "socks5://api:41560"
    )


class FakeApi:
    def __init__(self, names: list[str] | None = None) -> None:
        self.names = list(names or [])
        self.avoids: list[list[str]] = []
        self.reports: list[tuple[str, bool]] = []

    async def report_proxy(self, name: str, *, success: bool = False) -> dict[str, Any]:
        self.reports.append((name, success))
        return {}

    async def get_runtime_config(
        self, avoid: list[str] | None = None
    ) -> dict[str, Any]:
        self.avoids.append(list(avoid or []))
        name = self.names.pop(0)
        return {"proxy": {"name": name, "url": f"socks5://{name}"}}


class EndpointRefreshApi(FakeApi):
    def __init__(self, refreshed_url: str) -> None:
        super().__init__()
        self.refreshed_url = refreshed_url

    async def get_runtime_config(
        self, avoid: list[str] | None = None
    ) -> dict[str, Any]:
        requested_avoid = list(avoid or [])
        self.avoids.append(requested_avoid)
        if requested_avoid:
            return {"proxy": None}
        return {
            "proxy": {
                "name": "proxy-a",
                "url": self.refreshed_url,
            }
        }


class ProxySessionSpy:
    def __init__(self, proxy: str) -> None:
        self._proxy = proxy
        self.assignments: list[str] = []

    @property
    def proxy(self) -> str:
        return self._proxy

    @proxy.setter
    def proxy(self, value: str) -> None:
        self.assignments.append(value)
        self._proxy = value


@pytest.mark.asyncio
async def test_failed_proxy_names_expire_from_local_avoid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(proxy_manager.time, "monotonic", lambda: now)
    monkeypatch.setattr(proxy_manager, "_FAILED_NAME_COOLDOWN_SEC", 10.0)

    api = FakeApi(["proxy-b", "proxy-c", "proxy-a"])
    mgr = proxy_manager.ProxyManager(api)  # type: ignore[arg-type]
    mgr.current_name = "proxy-a"

    assert await mgr.failover()
    assert api.avoids[-1] == ["proxy-a"]
    assert mgr.current_name == "proxy-b"

    now = 105.0
    assert await mgr.failover()
    assert api.avoids[-1] == ["proxy-a", "proxy-b"]
    assert mgr.current_name == "proxy-c"

    now = 111.0
    assert await mgr.failover()
    assert api.avoids[-1] == ["proxy-b", "proxy-c"]
    assert mgr.current_name == "proxy-a"


@pytest.mark.asyncio
async def test_report_success_clears_current_failed_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(proxy_manager.time, "monotonic", lambda: now)

    api = FakeApi()
    mgr = proxy_manager.ProxyManager(api)  # type: ignore[arg-type]
    mgr.current_name = "proxy-a"
    mgr._failed_names = {"proxy-a": 200.0, "expired": 90.0}

    await mgr.report_success()

    assert mgr._failed_names == {}
    assert api.reports == [("proxy-a", True)]


@pytest.mark.asyncio
async def test_pool_exhaustion_retries_without_avoid_and_refreshes_endpoint() -> None:
    session = ProxySessionSpy("socks5://proxy.internal:41000")
    api = EndpointRefreshApi("socks5h://proxy.internal:42000")
    mgr = proxy_manager.ProxyManager(api)  # type: ignore[arg-type]
    mgr.current_name = "proxy-a"
    mgr.current_url = "socks5://proxy.internal:41000"
    mgr._session = session  # type: ignore[assignment]

    assert await mgr.failover() is True
    assert mgr.current_name == "proxy-a"
    assert mgr.current_url == "socks5://proxy.internal:42000"
    assert session.proxy == "socks5://proxy.internal:42000"
    assert session.assignments == ["socks5://proxy.internal:42000"]
    assert api.avoids == [["proxy-a"], []]
    assert api.reports == [("proxy-a", False)]


@pytest.mark.asyncio
async def test_pool_exhaustion_does_not_retry_same_endpoint() -> None:
    session = ProxySessionSpy("socks5://proxy.internal:41000")
    api = EndpointRefreshApi("socks5h://proxy.internal:41000")
    mgr = proxy_manager.ProxyManager(api)  # type: ignore[arg-type]
    mgr.current_name = "proxy-a"
    mgr.current_url = "socks5://proxy.internal:41000"
    mgr._session = session  # type: ignore[assignment]

    assert await mgr.failover() is False
    assert mgr.current_name == "proxy-a"
    assert mgr.current_url == "socks5://proxy.internal:41000"
    assert session.proxy == "socks5://proxy.internal:41000"
    assert session.assignments == []
    assert api.avoids == [["proxy-a"], []]
    assert api.reports == [("proxy-a", False)]
