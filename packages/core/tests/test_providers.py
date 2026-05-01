from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

import lumen_core.providers as provider_mod
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    ProviderProxyDefinition,
    RoundRobinState,
    build_effective_provider_config,
    build_effective_providers,
    parse_provider_item,
    parse_proxy_item,
    parse_provider_json,
    socks_proxy_url,
    weighted_priority_order,
    weighted_priority_order_and_advance,
)


class _FakeSshProcess:
    returncode: int | None = None
    stderr = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def _provider(name: str, *, weight: int = 1) -> ProviderDefinition:
    return ProviderDefinition(
        name=name,
        base_url=f"https://{name}.example.com",
        api_key="key",
        priority=10,
        weight=weight,
    )


def test_weighted_priority_order_advances_shared_counter_serially():
    providers = [_provider("a"), _provider("b"), _provider("c")]
    state = RoundRobinState()

    with ThreadPoolExecutor(max_workers=16) as pool:
        orders = list(
            pool.map(
                lambda _: weighted_priority_order_and_advance(
                    providers,
                    state,
                )[0].name,
                range(64),
            )
        )

    assert state.counters[10] == 64
    assert set(orders) == {"a", "b", "c"}


def test_weighted_priority_order_uses_explicit_independent_state():
    providers = [_provider("a"), _provider("b")]
    first = RoundRobinState()
    second = RoundRobinState()

    assert weighted_priority_order_and_advance(providers, first)[0].name == "a"
    assert weighted_priority_order_and_advance(providers, first)[0].name == "b"
    assert weighted_priority_order_and_advance(providers, second)[0].name == "a"
    assert first.counters == {10: 2}
    assert second.counters == {10: 1}


def test_weighted_priority_order_compatibility_alias_still_advances_counter():
    providers = [_provider("a"), _provider("b")]
    counters: dict[int, int] = {}

    assert weighted_priority_order(providers, counters)[0].name == "a"
    assert weighted_priority_order(providers, counters)[0].name == "b"
    assert counters == {10: 2}


def test_parse_provider_item_defaults_and_normalizes_fields():
    provider = parse_provider_item(
        {
            "name": "  primary  ",
            "base_url": "https://upstream.example/v1/ ",
            "api_key": " sk-test ",
            "priority": "5",
            "weight": "2.9",
            "proxy": " proxy-us ",
            "image_rate_limit": " 5/min ",
            "image_daily_quota": "10",
            "image_jobs_enabled": True,
        },
        index=0,
    )

    assert provider.name == "primary"
    assert provider.base_url == "https://upstream.example/v1"
    assert provider.api_key == "sk-test"
    assert provider.priority == 5
    assert provider.weight == 2
    assert provider.proxy_name == "proxy-us"
    assert provider.image_rate_limit == "5/min"
    assert provider.image_daily_quota == 10
    assert provider.image_jobs_enabled is True


def test_parse_proxy_item_normalizes_s5_alias_and_hides_password_in_repr():
    proxy = parse_proxy_item(
        {
            "name": " us ",
            "type": "s5",
            "host": "127.0.0.1",
            "port": "1080",
            "username": " user ",
            "password": " secret ",
        },
        index=0,
    )

    assert proxy.name == "us"
    assert proxy.protocol == "socks5"
    assert proxy.host == "127.0.0.1"
    assert proxy.port == 1080
    assert proxy.username == "user"
    assert proxy.password == "secret"
    assert "secret" not in repr(proxy)


def test_build_effective_provider_config_attaches_named_proxy():
    raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "egress",
                    "type": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                }
            ],
            "providers": [
                {
                    "name": "primary",
                    "base_url": "https://upstream.example",
                    "api_key": "sk-test",
                    "proxy": "egress",
                }
            ],
        }
    )

    providers, proxies, errors = build_effective_provider_config(
        raw_providers=raw,
        legacy_base_url=None,
        legacy_api_key=None,
    )

    assert errors == []
    assert [p.name for p in proxies] == ["egress"]
    assert providers[0].proxy_name == "egress"
    assert providers[0].proxy is proxies[0]


def test_socks_proxy_url_quotes_credentials():
    proxy = ProviderProxyDefinition(
        name="p",
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
        username="u ser",
        password="p@ss",
    )

    assert socks_proxy_url(proxy) == "socks5h://u%20ser:p%40ss@127.0.0.1:1080"


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_supports_password_auth_with_askpass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_mod._SSH_TUNNELS.clear()
    captured: dict[str, object] = {}

    def fake_which(name: str) -> str | None:
        if name == "ssh":
            return "/usr/bin/ssh"
        return None

    async def fake_create_subprocess_exec(
        *cmd: str,
        **kwargs: object,
    ) -> _FakeSshProcess:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeSshProcess()

    async def fake_local_port_accepts(port: int) -> bool:
        captured["port"] = port
        return True

    monkeypatch.setattr(provider_mod.shutil, "which", fake_which)
    monkeypatch.setattr(provider_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(provider_mod, "_free_local_port", lambda: 41555)
    monkeypatch.setattr(provider_mod, "_local_port_accepts", fake_local_port_accepts)

    url = await provider_mod.resolve_provider_proxy_url(
        ProviderProxyDefinition(
            name="ssh-cn",
            protocol="ssh",
            host="203.0.113.10",
            port=22,
            username="root",
            password="secret-password",
        )
    )

    cmd = captured["cmd"]
    env = captured["env"]
    assert url == "socks5h://127.0.0.1:41555"
    assert isinstance(cmd, tuple)
    assert cmd[0] == "/usr/bin/ssh"
    assert "BatchMode=no" in cmd
    assert "PasswordAuthentication=yes" in cmd
    assert "root@203.0.113.10" in cmd
    assert isinstance(env, dict)
    assert env["LUMEN_SSH_PASSWORD"] == "secret-password"
    assert "SSHPASS" not in env
    assert isinstance(env["SSH_ASKPASS"], str)
    assert not os.path.exists(env["SSH_ASKPASS"])

    await provider_mod.close_provider_proxy_tunnels()


def test_parse_provider_item_uses_index_name_when_name_is_blank():
    provider = parse_provider_item(
        {"name": "", "base_url": "https://upstream.example", "api_key": "sk-test"},
        index=3,
    )

    assert provider.name == "provider-3"


def test_parse_provider_item_clamps_extreme_float_weight_values():
    base = {"base_url": "https://upstream.example", "api_key": "sk-test"}

    assert parse_provider_item({**base, "weight": "1e309"}, index=0).weight == 1
    assert parse_provider_item({**base, "weight": "nan"}, index=0).weight == 1
    assert parse_provider_item({**base, "weight": "0"}, index=0).weight == 1
    assert parse_provider_item({**base, "weight": "2500"}, index=0).weight == 1000


def test_parse_provider_json_accumulates_item_errors():
    raw = json.dumps(
        [
            {
                "name": "ok",
                "base_url": "https://ok.example",
                "api_key": "sk-ok",
            },
            "not-object",
            {"name": "missing-base", "api_key": "sk-test"},
            {"name": "missing-key", "base_url": "https://bad.example"},
        ]
    )

    providers, errors = parse_provider_json(raw)

    assert [p.name for p in providers] == ["ok"]
    assert errors == [
        "providers[1] is not an object",
        "providers[2] invalid: provider missing-base: base_url is required",
        "providers[3] invalid: provider missing-key: api_key is required",
    ]


def test_parse_provider_json_reports_malformed_json():
    providers, errors = parse_provider_json("[")

    assert providers == []
    assert len(errors) == 1
    assert errors[0].startswith("providers JSON parse failed")


def test_parse_provider_json_ignores_absent_or_empty_arrays():
    assert parse_provider_json(None) == ([], [])
    assert parse_provider_json("[]") == ([], [])


def test_build_effective_providers_uses_legacy_fallback_when_pool_absent():
    providers, errors = build_effective_providers(
        raw_providers=None,
        legacy_base_url="https://legacy.example/",
        legacy_api_key=" sk-legacy ",
    )

    assert errors == []
    assert len(providers) == 1
    assert providers[0].name == "default"
    assert providers[0].base_url == "https://legacy.example"
    assert providers[0].api_key == "sk-legacy"


def test_build_effective_providers_defaults_legacy_base_url():
    providers, errors = build_effective_providers(
        raw_providers=None,
        legacy_base_url="",
        legacy_api_key="sk-legacy",
    )

    assert errors == []
    assert len(providers) == 1
    assert providers[0].base_url == DEFAULT_LEGACY_PROVIDER_BASE_URL


def test_build_effective_providers_does_not_merge_legacy_when_pool_exists():
    raw = json.dumps(
        [
            {
                "name": "configured",
                "base_url": "https://configured.example",
                "api_key": "sk-configured",
            }
        ]
    )

    providers, errors = build_effective_providers(
        raw_providers=raw,
        legacy_base_url="https://legacy.example",
        legacy_api_key="sk-legacy",
    )

    assert errors == []
    assert [p.name for p in providers] == ["configured"]
