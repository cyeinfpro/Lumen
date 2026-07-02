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


class FakeApi:
    def __init__(self, names: list[str] | None = None) -> None:
        self.names = list(names or [])
        self.avoids: list[list[str]] = []
        self.reports: list[tuple[str, bool]] = []

    async def report_proxy(self, name: str, *, success: bool = False) -> dict[str, Any]:
        self.reports.append((name, success))
        return {}

    async def get_runtime_config(self, avoid: list[str] | None = None) -> dict[str, Any]:
        self.avoids.append(list(avoid or []))
        name = self.names.pop(0)
        return {"proxy": {"name": name, "url": f"socks5://{name}"}}


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
