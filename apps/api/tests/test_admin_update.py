from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.routes import admin_update


def test_update_paths_resolve_from_lumen_scripts_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "backup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "restore.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "update.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_update.settings, "lumen_scripts_dir", str(scripts_dir))

    assert admin_update._update_script() == scripts_dir / "update.sh"
    assert admin_update._update_log_path() == backup_root / ".update.log"
    assert admin_update._update_marker_path() == backup_root / ".update.running"


def test_proxy_env_is_replaced_for_update_process() -> None:
    env = {
        "HTTP_PROXY": "http://old",
        "https_proxy": "http://old-lower",
        "KEEP": "1",
    }

    admin_update._clean_proxy_env(env)
    admin_update._apply_proxy_env(env, "socks5h://127.0.0.1:1080")

    assert env["KEEP"] == "1"
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        assert env[key] == "socks5h://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_resolve_update_proxy_uses_named_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_setting(_db: Any, spec: Any) -> str | None:
        values = {
            "update.use_proxy_pool": "1",
            "update.proxy_name": "egress",
        }
        return values.get(spec.key)

    async def fake_load_proxies(_db: Any):
        from lumen_core.providers import ProviderProxyDefinition

        return [
            ProviderProxyDefinition(
                name="egress",
                protocol="socks5",
                host="127.0.0.1",
                port=1080,
                enabled=True,
            )
        ]

    async def fake_resolve(proxy):
        assert proxy.name == "egress"
        return "socks5h://127.0.0.1:1080"

    monkeypatch.setattr(admin_update, "get_setting", fake_get_setting)
    monkeypatch.setattr(admin_update, "_load_proxies", fake_load_proxies)
    monkeypatch.setattr(admin_update, "resolve_provider_proxy_url", fake_resolve)

    proxy, proxy_url = await admin_update._resolve_update_proxy(object())  # type: ignore[arg-type]

    assert proxy is not None
    assert proxy.name == "egress"
    assert proxy_url == "socks5h://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_resolve_update_proxy_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_setting(_db: Any, spec: Any) -> str | None:
        return "0" if spec.key == "update.use_proxy_pool" else None

    monkeypatch.setattr(admin_update, "get_setting", fake_get_setting)

    proxy, proxy_url = await admin_update._resolve_update_proxy(object())  # type: ignore[arg-type]

    assert proxy is None
    assert proxy_url is None
