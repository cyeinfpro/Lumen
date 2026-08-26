from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import lumen_core.providers as provider_mod
from lumen_core.providers_parts import proxy_runtime
from lumen_core.providers_parts.definitions import DEFAULT_PROVIDER_PURPOSES
from lumen_core.providers_parts.selection import route_to_purpose
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    ProviderProxyDefinition,
    RoundRobinState,
    build_effective_provider_config,
    build_effective_providers,
    endpoint_kind_allowed,
    has_embedding_purpose,
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


_PROVIDER_PROXY_RUNTIME = proxy_runtime.ProviderProxyRuntime()


async def _resolve_provider_proxy_url(
    proxy: ProviderProxyDefinition,
    *,
    bind_host: str = "127.0.0.1",
    advertise_host: str | None = None,
) -> str | None:
    return await provider_mod.resolve_provider_proxy_url(
        proxy,
        runtime=_PROVIDER_PROXY_RUNTIME,
        bind_host=bind_host,
        advertise_host=advertise_host,
    )


async def _close_provider_proxy_tunnels() -> None:
    await provider_mod.close_provider_proxy_tunnels(
        runtime=_PROVIDER_PROXY_RUNTIME,
    )


def _write_known_hosts(tmp_path: Path) -> str:
    path = tmp_path / "known_hosts"
    path.write_text(
        "[203.0.113.10]:22 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return str(path)


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
                lambda _: (
                    weighted_priority_order_and_advance(
                        providers,
                        state,
                    )[0].name
                ),
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
            "image_streaming_enabled": "true",
            "image_edit_input_transport": " file ",
            "agent_models": [" gpt-5.6-sol ", "gpt-5.6-sol", "gpt-5.6-mini"],
            "agent_thinking_level_map": {"xhigh": "xhigh", "max": "max"},
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
    assert provider.image_streaming_enabled is True
    assert provider.image_edit_input_transport == "file"
    assert provider.agent_models == ("gpt-5.6-sol", "gpt-5.6-mini")
    assert provider.agent_context_window == 272_000
    assert provider.agent_thinking_level_map == {"xhigh": "xhigh", "max": "max"}
    assert provider.purposes == DEFAULT_PROVIDER_PURPOSES


def test_mixed_agent_model_catalog_keeps_conservative_context_default() -> None:
    provider = parse_provider_item(
        {
            "base_url": "https://upstream.example",
            "api_key": "sk-test",
            "agent_models": ["gpt-5.4", "gpt-5.6-sol"],
        },
        index=0,
    )
    assert provider.agent_context_window == 128_000


def test_parse_provider_item_rejects_invalid_agent_models() -> None:
    with pytest.raises(ValueError, match="agent_models must be a list"):
        parse_provider_item(
            {
                "base_url": "https://upstream.example",
                "api_key": "sk-test",
                "agent_models": "gpt-5.6-sol",
            },
            index=0,
        )
    with pytest.raises(ValueError, match="unsupported level"):
        parse_provider_item(
            {
                "base_url": "https://upstream.example",
                "api_key": "sk-test",
                "agent_thinking_level_map": {"extreme": "extreme"},
            },
            index=0,
        )


def test_parse_provider_item_parses_string_booleans_without_truthy_coercion():
    provider = parse_provider_item(
        {
            "base_url": "https://upstream.example",
            "api_key": "sk-test",
            "enabled": "false",
            "image_jobs_enabled": "0",
        },
        index=0,
    )

    assert provider.enabled is False
    assert provider.image_jobs_enabled is False
    assert provider.image_streaming_enabled is False

    enabled_provider = parse_provider_item(
        {
            "base_url": "https://upstream.example",
            "api_key": "sk-test",
            "enabled": "yes",
            "image_jobs_enabled": "true",
        },
        index=0,
    )

    assert enabled_provider.enabled is True
    assert enabled_provider.image_jobs_enabled is True


def test_parse_provider_item_normalizes_purposes() -> None:
    provider = parse_provider_item(
        {
            "base_url": "https://upstream.example",
            "api_key": "sk-test",
            "purposes": [" embedding ", "chat", "chat"],
        },
        index=0,
    )

    assert provider.purposes == ("embedding", "chat")


def test_route_to_purpose_preserves_legacy_route_aliases() -> None:
    assert route_to_purpose("text") == "chat"
    assert route_to_purpose("image_jobs") == "image"
    assert route_to_purpose("embedding") == "embedding"
    assert route_to_purpose(None) == "chat"


def test_parse_provider_item_defaults_unknown_edit_transport_to_url():
    provider = parse_provider_item(
        {
            "base_url": "https://upstream.example",
            "api_key": "sk-test",
            "image_edit_input_transport": "auto",
        },
        index=0,
    )

    assert provider.image_edit_input_transport == "url"


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


def test_provider_proxy_default_password_and_replace_preserve_public_contract():
    default_proxy = ProviderProxyDefinition(
        name="default",
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
    )
    secret_proxy = ProviderProxyDefinition(
        name="secret",
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
        password="keep-me",
    )

    assert default_proxy.password is None
    assert replace(secret_proxy, name="copy").password == "keep-me"


def test_parse_proxy_item_parses_string_enabled_without_truthy_coercion():
    proxy = parse_proxy_item(
        {
            "name": "egress",
            "type": "socks5",
            "host": "127.0.0.1",
            "enabled": "false",
        },
        index=0,
    )

    assert proxy.enabled is False


def test_parse_ssh_proxy_accepts_managed_host_key_trust_aliases() -> None:
    fingerprint = f"SHA256:{'A' * 43}"
    proxy = parse_proxy_item(
        {
            "name": "ssh-hop",
            "type": "ssh",
            "host": "ssh.example.com",
            "known_hosts_file": " /run/secrets/lumen_known_hosts ",
            "fingerprint": fingerprint,
        },
        index=0,
    )

    assert proxy.known_hosts_path == "/run/secrets/lumen_known_hosts"
    assert proxy.known_hosts_file == proxy.known_hosts_path
    assert proxy.host_key_fingerprint == fingerprint
    assert proxy.fingerprint == fingerprint


def test_parse_ssh_proxy_rejects_conflicting_or_invalid_trust_material() -> None:
    base = {
        "name": "ssh-hop",
        "type": "ssh",
        "host": "ssh.example.com",
    }
    with pytest.raises(ValueError, match="aliases disagree"):
        parse_proxy_item(
            {
                **base,
                "known_hosts_path": "/etc/ssh/known_hosts",
                "known_hosts_file": "/run/secrets/known_hosts",
            },
            index=0,
        )
    with pytest.raises(ValueError, match="SHA256"):
        parse_proxy_item(
            {**base, "host_key_fingerprint": "md5:invalid"},
            index=0,
        )


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


def test_build_effective_provider_config_reports_disabled_named_proxy():
    raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "egress",
                    "type": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                    "enabled": False,
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

    assert [p.name for p in proxies] == ["egress"]
    assert providers[0].proxy is None
    assert errors == ["provider primary: proxy egress is disabled"]


def test_build_effective_provider_config_allows_disabled_provider_stale_proxy():
    raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "egress",
                    "type": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                    "enabled": False,
                }
            ],
            "providers": [
                {
                    "name": "parked",
                    "base_url": "https://upstream.example",
                    "api_key": "",
                    "enabled": False,
                    "proxy": "egress",
                }
            ],
        }
    )

    providers, _proxies, errors = build_effective_provider_config(
        raw_providers=raw,
        legacy_base_url=None,
        legacy_api_key=None,
    )

    assert errors == []
    assert providers[0].enabled is False
    assert providers[0].proxy is None


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
    tmp_path: Path,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    captured: dict[str, object] = {}
    source_known_hosts = _write_known_hosts(tmp_path)

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
        known_hosts_option = next(
            item for item in cmd if item.startswith("UserKnownHostsFile=")
        )
        known_hosts_path = known_hosts_option.split("=", 1)[1]
        captured["known_hosts_path"] = known_hosts_path
        captured["known_hosts_at_spawn"] = Path(known_hosts_path).read_text(
            encoding="utf-8"
        )
        return _FakeSshProcess()

    async def fake_local_port_accepts(host: str, port: int) -> bool:
        captured["probe_host"] = host
        captured["port"] = port
        return True

    def fake_free_local_port(bind_host: str) -> int:
        captured["bind_host"] = bind_host
        return 41555

    monkeypatch.setattr(proxy_runtime.shutil, "which", fake_which)
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(proxy_runtime, "_free_local_port", fake_free_local_port)
    monkeypatch.setattr(
        proxy_runtime,
        "_local_port_accepts",
        fake_local_port_accepts,
    )

    url = await _resolve_provider_proxy_url(
        ProviderProxyDefinition(
            name="ssh-cn",
            protocol="ssh",
            host="203.0.113.10",
            port=22,
            username="root",
            password="secret-password",
            known_hosts_path=source_known_hosts,
        )
    )

    cmd = captured["cmd"]
    env = captured["env"]
    assert url == "socks5h://127.0.0.1:41555"
    assert isinstance(cmd, tuple)
    assert cmd[0] == "/usr/bin/ssh"
    assert cmd[cmd.index("-D") + 1] == "127.0.0.1:41555"
    assert captured["bind_host"] == "127.0.0.1"
    assert captured["probe_host"] == "127.0.0.1"
    assert "-g" not in cmd
    assert "BatchMode=no" in cmd
    assert "PasswordAuthentication=yes" in cmd
    assert "StrictHostKeyChecking=yes" in cmd
    assert "StrictHostKeyChecking=accept-new" not in cmd
    assert any(
        item.startswith("UserKnownHostsFile=") for item in cmd if isinstance(item, str)
    )
    known_hosts_path = captured["known_hosts_path"]
    assert isinstance(known_hosts_path, str)
    assert known_hosts_path != source_known_hosts
    assert captured["known_hosts_at_spawn"] == Path(source_known_hosts).read_text(
        encoding="utf-8"
    )
    assert not os.path.exists(known_hosts_path)
    assert os.path.exists(source_known_hosts)
    assert f"GlobalKnownHostsFile={os.devnull}" in cmd
    assert "root@203.0.113.10" in cmd
    assert isinstance(env, dict)
    assert "LUMEN_SSH_PASSWORD" not in env
    assert "SSHPASS" not in env
    assert isinstance(env["LUMEN_SSH_PASSWORD_FILE"], str)
    assert not os.path.exists(env["LUMEN_SSH_PASSWORD_FILE"])
    assert isinstance(env["SSH_ASKPASS"], str)
    assert not os.path.exists(env["SSH_ASKPASS"])

    await _close_provider_proxy_tunnels()


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_keeps_loopback_and_container_tunnels_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    bind_hosts: list[str] = []
    probes: list[tuple[str, int]] = []
    commands: list[tuple[str, ...]] = []
    processes: list[_FakeSshProcess] = []
    ports = iter((41559, 41560))

    async def fake_create_subprocess_exec(
        *cmd: str,
        **_kwargs: object,
    ) -> _FakeSshProcess:
        process = _FakeSshProcess()
        commands.append(cmd)
        processes.append(process)
        return process

    def fake_free_local_port(bind_host: str) -> int:
        bind_hosts.append(bind_host)
        return next(ports)

    async def fake_local_port_accepts(host: str, port: int) -> bool:
        probes.append((host, port))
        return True

    monkeypatch.setattr(
        proxy_runtime.shutil,
        "which",
        lambda name: "/usr/bin/ssh" if name == "ssh" else None,
    )
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(proxy_runtime, "_free_local_port", fake_free_local_port)
    monkeypatch.setattr(
        proxy_runtime,
        "_local_port_accepts",
        fake_local_port_accepts,
    )

    proxy = ProviderProxyDefinition(
        name="ssh-shared",
        protocol="ssh",
        host="203.0.113.10",
        port=22,
        username="root",
        known_hosts_path=_write_known_hosts(tmp_path),
    )

    loopback_url = await _resolve_provider_proxy_url(proxy)
    container_url = await _resolve_provider_proxy_url(
        proxy,
        bind_host="0.0.0.0",
        advertise_host="api",
    )
    reused_loopback_url = await _resolve_provider_proxy_url(proxy)

    assert loopback_url == reused_loopback_url == "socks5h://127.0.0.1:41559"
    assert container_url == "socks5h://api:41560"
    assert bind_hosts == ["127.0.0.1", "0.0.0.0"]
    assert probes == [("127.0.0.1", 41559), ("127.0.0.1", 41560)]
    assert [cmd[cmd.index("-D") + 1] for cmd in commands] == [
        "127.0.0.1:41559",
        "0.0.0.0:41560",
    ]
    assert "-g" not in commands[0]
    assert "-g" in commands[1]
    assert len(_PROVIDER_PROXY_RUNTIME.tunnels) == 2
    assert all(process.returncode is None for process in processes)

    await _close_provider_proxy_tunnels()


@pytest.mark.asyncio
async def test_close_waits_for_inflight_ssh_start_and_terminates_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = proxy_runtime.ProviderProxyRuntime()
    process = _FakeSshProcess()
    spawned = asyncio.Event()
    allow_ready = asyncio.Event()

    async def fake_create_subprocess_exec(
        *_cmd: str,
        **_kwargs: object,
    ) -> _FakeSshProcess:
        spawned.set()
        return process

    async def fake_local_port_accepts(_host: str, _port: int) -> bool:
        await allow_ready.wait()
        return True

    monkeypatch.setattr(
        proxy_runtime.shutil,
        "which",
        lambda name: "/usr/bin/ssh" if name == "ssh" else None,
    )
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        proxy_runtime,
        "_local_port_accepts",
        fake_local_port_accepts,
    )
    monkeypatch.setattr(proxy_runtime, "_free_local_port", lambda _host: 41561)

    proxy = ProviderProxyDefinition(
        name="ssh-close-race",
        protocol="ssh",
        host="203.0.113.10",
        port=22,
        known_hosts_path=_write_known_hosts(tmp_path),
    )
    resolve_task = asyncio.create_task(
        provider_mod.resolve_provider_proxy_url(proxy, runtime=runtime)
    )
    await spawned.wait()
    close_task = asyncio.create_task(
        provider_mod.close_provider_proxy_tunnels(runtime=runtime)
    )
    await asyncio.sleep(0)
    assert close_task.done() is False

    allow_ready.set()
    assert await resolve_task == "socks5h://127.0.0.1:41561"
    await close_task

    assert process.returncode == 0
    assert runtime.tunnels == {}
    assert runtime.closed is True
    with pytest.raises(RuntimeError, match="runtime is closed"):
        await provider_mod.resolve_provider_proxy_url(proxy, runtime=runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_kind", ["disabled", "socks5"])
async def test_non_ssh_refresh_retires_named_ssh_tunnels(
    replacement_kind: str,
) -> None:
    runtime = proxy_runtime.ProviderProxyRuntime()
    stale_process = _FakeSshProcess()
    other_process = _FakeSshProcess()
    old_proxy = ProviderProxyDefinition(
        name="shared-proxy",
        protocol="ssh",
        host="203.0.113.10",
        port=22,
    )
    other_proxy = ProviderProxyDefinition(
        name="other-proxy",
        protocol="ssh",
        host="203.0.113.11",
        port=22,
    )
    runtime.tunnels[proxy_runtime._ssh_tunnel_key(old_proxy, "127.0.0.1")] = (  # noqa: SLF001
        proxy_runtime._SshTunnel(  # noqa: SLF001
            proxy_name=old_proxy.name,
            proxy_identity=proxy_runtime._ssh_proxy_identity(old_proxy),  # noqa: SLF001
            bind_host="127.0.0.1",
            local_port=41562,
            process=stale_process,
        )
    )
    runtime.tunnels[proxy_runtime._ssh_tunnel_key(other_proxy, "127.0.0.1")] = (  # noqa: SLF001
        proxy_runtime._SshTunnel(  # noqa: SLF001
            proxy_name=other_proxy.name,
            proxy_identity=proxy_runtime._ssh_proxy_identity(other_proxy),  # noqa: SLF001
            bind_host="127.0.0.1",
            local_port=41563,
            process=other_process,
        )
    )
    replacement = (
        ProviderProxyDefinition(
            name=old_proxy.name,
            protocol="ssh",
            host=old_proxy.host,
            port=old_proxy.port,
            enabled=False,
        )
        if replacement_kind == "disabled"
        else ProviderProxyDefinition(
            name=old_proxy.name,
            protocol="socks5",
            host="127.0.0.1",
            port=1080,
        )
    )

    resolved = await provider_mod.resolve_provider_proxy_url(
        replacement,
        runtime=runtime,
    )

    assert resolved == (
        None if replacement_kind == "disabled" else "socks5h://127.0.0.1:1080"
    )
    assert stale_process.returncode == 0
    assert other_process.returncode is None
    assert all(
        tunnel.proxy_name != old_proxy.name for tunnel in runtime.tunnels.values()
    )
    assert any(
        tunnel.proxy_name == other_proxy.name for tunnel in runtime.tunnels.values()
    )
    await provider_mod.close_provider_proxy_tunnels(runtime=runtime)


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_rejects_symlink_known_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    source = Path(_write_known_hosts(tmp_path))
    link = tmp_path / "known_hosts-link"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlinks are unavailable")
    launched = False

    async def fake_create_subprocess_exec(
        *_cmd: str,
        **_kwargs: object,
    ) -> _FakeSshProcess:
        nonlocal launched
        launched = True
        return _FakeSshProcess()

    monkeypatch.setattr(
        proxy_runtime.shutil,
        "which",
        lambda name: "/usr/bin/ssh" if name == "ssh" else None,
    )
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        await _resolve_provider_proxy_url(
            ProviderProxyDefinition(
                name="ssh-symlinked",
                protocol="ssh",
                host="203.0.113.10",
                port=22,
                known_hosts_path=str(link),
            )
        )

    assert launched is False


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_rejects_missing_host_key_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    launched = False

    def fake_which(name: str) -> str | None:
        return "/usr/bin/ssh" if name == "ssh" else None

    async def fake_create_subprocess_exec(
        *_cmd: str,
        **_kwargs: object,
    ) -> _FakeSshProcess:
        nonlocal launched
        launched = True
        return _FakeSshProcess()

    monkeypatch.setattr(proxy_runtime.shutil, "which", fake_which)
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="refusing unknown host key"):
        await _resolve_provider_proxy_url(
            ProviderProxyDefinition(
                name="ssh-untrusted",
                protocol="ssh",
                host="203.0.113.10",
                port=22,
                username="root",
            )
        )

    assert launched is False


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_rejects_writable_known_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    known_hosts_path = _write_known_hosts(tmp_path)
    os.chmod(known_hosts_path, 0o666)
    monkeypatch.setattr(
        proxy_runtime.shutil,
        "which",
        lambda name: "/usr/bin/ssh" if name == "ssh" else None,
    )

    with pytest.raises(RuntimeError, match="group/world writable"):
        await _resolve_provider_proxy_url(
            ProviderProxyDefinition(
                name="ssh-unmanaged",
                protocol="ssh",
                host="203.0.113.10",
                port=22,
                known_hosts_path=known_hosts_path,
            )
        )


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_pins_configured_host_key_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    captured: dict[str, object] = {}
    key_blob = b"synthetic-ed25519-host-key"
    encoded_key = base64.b64encode(key_blob).decode("ascii")
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(key_blob).digest()
    ).decode("ascii").rstrip("=")
    keyscan_line = f"[203.0.113.10]:22 ssh-ed25519 {encoded_key}"

    def fake_which(name: str) -> str | None:
        if name == "ssh":
            return "/usr/bin/ssh"
        if name == "ssh-keyscan":
            return "/usr/bin/ssh-keyscan"
        return None

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["keyscan_cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"{keyscan_line}\n",
            stderr="",
        )

    async def fake_create_subprocess_exec(
        *cmd: str,
        **_kwargs: object,
    ) -> _FakeSshProcess:
        captured["cmd"] = cmd
        known_hosts_option = next(
            item for item in cmd if item.startswith("UserKnownHostsFile=")
        )
        path = known_hosts_option.split("=", 1)[1]
        captured["known_hosts_path"] = path
        captured["known_hosts_at_spawn"] = Path(path).read_text(encoding="utf-8")
        return _FakeSshProcess()

    async def fake_local_port_accepts(_host: str, _port: int) -> bool:
        return True

    monkeypatch.setattr(proxy_runtime.shutil, "which", fake_which)
    monkeypatch.setattr(proxy_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        proxy_runtime,
        "_local_port_accepts",
        fake_local_port_accepts,
    )
    monkeypatch.setattr(proxy_runtime, "_free_local_port", lambda _host: 41558)

    url = await _resolve_provider_proxy_url(
        ProviderProxyDefinition(
            name="ssh-pinned",
            protocol="ssh",
            host="203.0.113.10",
            port=22,
            username="root",
            host_key_fingerprint=fingerprint,
        )
    )

    assert url == "socks5h://127.0.0.1:41558"
    assert captured["keyscan_cmd"] == [
        "/usr/bin/ssh-keyscan",
        "-T",
        "5",
        "-p",
        "22",
        "--",
        "203.0.113.10",
    ]
    assert captured["known_hosts_at_spawn"] == f"{keyscan_line}\n"
    known_hosts_path = captured["known_hosts_path"]
    assert isinstance(known_hosts_path, str)
    assert not os.path.exists(known_hosts_path)

    await _close_provider_proxy_tunnels()


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_rejects_host_key_fingerprint_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    launched = False
    expected_blob = b"expected-host-key"
    presented_blob = b"attacker-host-key"
    expected_fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(expected_blob).digest()
    ).decode("ascii").rstrip("=")
    presented_key = base64.b64encode(presented_blob).decode("ascii")

    def fake_which(name: str) -> str | None:
        if name == "ssh":
            return "/usr/bin/ssh"
        if name == "ssh-keyscan":
            return "/usr/bin/ssh-keyscan"
        return None

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"[203.0.113.10]:22 ssh-ed25519 {presented_key}\n",
            stderr="",
        )

    async def fake_create_subprocess_exec(
        *_cmd: str,
        **_kwargs: object,
    ) -> _FakeSshProcess:
        nonlocal launched
        launched = True
        return _FakeSshProcess()

    monkeypatch.setattr(proxy_runtime.shutil, "which", fake_which)
    monkeypatch.setattr(proxy_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        await _resolve_provider_proxy_url(
            ProviderProxyDefinition(
                name="ssh-mismatch",
                protocol="ssh",
                host="203.0.113.10",
                port=22,
                username="root",
                host_key_fingerprint=expected_fingerprint,
            )
        )

    assert launched is False


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_terminates_failed_password_process_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    captured: dict[str, object] = {}
    proc = _FakeSshProcess()

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
        return proc

    async def fake_local_port_accepts(_host: str, _port: int) -> bool:
        return False

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(proxy_runtime.shutil, "which", fake_which)
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(proxy_runtime.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(proxy_runtime, "_free_local_port", lambda _host: 41556)
    monkeypatch.setattr(
        proxy_runtime,
        "_local_port_accepts",
        fake_local_port_accepts,
    )
    monkeypatch.setattr(proxy_runtime, "_SSH_TUNNEL_READY_CHECKS", 1)
    monkeypatch.setattr(proxy_runtime, "_SSH_TUNNEL_START_ATTEMPTS", 1)

    with pytest.raises(RuntimeError, match="failed to start"):
        await _resolve_provider_proxy_url(
            ProviderProxyDefinition(
                name="ssh-cn",
                protocol="ssh",
                host="203.0.113.10",
                port=22,
                username="root",
                password="secret-password",
                known_hosts_path=_write_known_hosts(tmp_path),
            )
        )

    env = captured["env"]
    assert isinstance(env, dict)
    assert proc.returncode == 0
    assert isinstance(env["LUMEN_SSH_PASSWORD_FILE"], str)
    assert not os.path.exists(env["LUMEN_SSH_PASSWORD_FILE"])
    assert isinstance(env["SSH_ASKPASS"], str)
    assert not os.path.exists(env["SSH_ASKPASS"])


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_cancel_stops_process_before_secret_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _PROVIDER_PROXY_RUNTIME.clear()
    captured: dict[str, object] = {}
    proc = _FakeSshProcess()
    unlink_events: list[tuple[str, int | None, bool]] = []
    original_unlink = proxy_runtime._unlink_quietly

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
        env = captured["env"]
        if isinstance(env, dict):
            captured["password_file"] = env.get("LUMEN_SSH_PASSWORD_FILE")
            captured["askpass_path"] = env.get("SSH_ASKPASS")
        return proc

    async def fake_local_port_accepts(_host: str, _port: int) -> bool:
        return False

    async def fake_sleep(_delay: float) -> None:
        raise asyncio.CancelledError()

    def tracking_unlink(path: str | None) -> None:
        if path:
            if path == captured.get("password_file"):
                label = "password"
            elif path == captured.get("askpass_path"):
                label = "askpass"
            else:
                label = "other"
            unlink_events.append((label, proc.returncode, os.path.exists(path)))
        original_unlink(path)

    monkeypatch.setattr(proxy_runtime.shutil, "which", fake_which)
    monkeypatch.setattr(
        proxy_runtime.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(proxy_runtime.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(proxy_runtime, "_free_local_port", lambda _host: 41557)
    monkeypatch.setattr(
        proxy_runtime,
        "_local_port_accepts",
        fake_local_port_accepts,
    )
    monkeypatch.setattr(proxy_runtime, "_unlink_quietly", tracking_unlink)
    monkeypatch.setattr(proxy_runtime, "_SSH_TUNNEL_START_ATTEMPTS", 1)

    with pytest.raises(asyncio.CancelledError):
        await _resolve_provider_proxy_url(
            ProviderProxyDefinition(
                name="ssh-cn",
                protocol="ssh",
                host="203.0.113.10",
                port=22,
                username="root",
                password="secret-password",
                known_hosts_path=_write_known_hosts(tmp_path),
            )
        )

    env = captured["env"]
    assert isinstance(env, dict)
    assert proc.returncode == 0
    assert ("password", 0, True) in unlink_events
    assert ("askpass", 0, True) in unlink_events
    assert isinstance(env["LUMEN_SSH_PASSWORD_FILE"], str)
    assert not os.path.exists(env["LUMEN_SSH_PASSWORD_FILE"])
    assert isinstance(env["SSH_ASKPASS"], str)
    assert not os.path.exists(env["SSH_ASKPASS"])


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


def test_parse_provider_item_rejects_non_integral_priority():
    base = {"base_url": "https://upstream.example", "api_key": "sk-test"}
    for value in ("5.5", "high", True):
        with pytest.raises(ValueError, match="priority"):
            parse_provider_item({**base, "priority": value}, index=0)


def test_parse_provider_item_rejects_invalid_boolean_strings():
    base = {"base_url": "https://upstream.example", "api_key": "sk-test"}
    for field in ("enabled", "image_jobs_enabled", "image_streaming_enabled"):
        with pytest.raises(ValueError, match=f"{field} must be a boolean"):
            parse_provider_item({**base, field: "sometimes"}, index=0)


def test_parse_proxy_item_rejects_invalid_enabled_string():
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        parse_proxy_item(
            {
                "type": "socks5",
                "host": "127.0.0.1",
                "enabled": "sometimes",
            },
            index=0,
        )


def test_parse_provider_item_requires_locked_image_endpoint_to_be_explicit():
    base = {"base_url": "https://upstream.example", "api_key": "sk-test"}
    with pytest.raises(ValueError, match="image_jobs_endpoint_lock"):
        parse_provider_item(
            {**base, "image_jobs_endpoint": "auto", "image_jobs_endpoint_lock": True},
            index=0,
        )

    provider = parse_provider_item(
        {
            **base,
            "image_jobs_endpoint": "generations",
            "image_jobs_endpoint_lock": "true",
        },
        index=0,
    )
    assert provider.image_jobs_endpoint == "generations"
    assert provider.image_jobs_endpoint_lock is True


def test_endpoint_kind_allowed_parses_dict_lock_without_truthy_coercion():
    unlocked = {
        "image_jobs_endpoint": "generations",
        "image_jobs_endpoint_lock": "false",
    }
    locked = {
        "image_jobs_endpoint": "generations",
        "image_jobs_endpoint_lock": "true",
    }

    assert endpoint_kind_allowed(unlocked, "responses") is True
    assert endpoint_kind_allowed(locked, "responses") is False


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


def test_parse_provider_json_allows_disabled_provider_without_api_key():
    raw = json.dumps(
        [
            {
                "name": "disabled",
                "base_url": "https://disabled.example",
                "api_key": "",
                "enabled": False,
            }
        ]
    )

    providers, errors = parse_provider_json(raw)

    assert errors == []
    assert len(providers) == 1
    assert providers[0].enabled is False
    assert providers[0].api_key == ""


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


def test_has_embedding_purpose_requires_enabled_provider_with_embedding() -> None:
    chat_only = ProviderDefinition(
        name="chat",
        base_url="https://chat.example",
        api_key="sk",
        purposes=("chat", "image"),
        enabled=True,
    )
    embed_disabled = ProviderDefinition(
        name="embed-off",
        base_url="https://embed.example",
        api_key="sk",
        purposes=("embedding",),
        enabled=False,
    )
    embed_enabled = ProviderDefinition(
        name="embed",
        base_url="https://embed.example",
        api_key="sk",
        purposes=("embedding",),
        enabled=True,
    )

    assert has_embedding_purpose([]) is False
    assert has_embedding_purpose([chat_only]) is False
    assert has_embedding_purpose([chat_only, embed_disabled]) is False
    assert has_embedding_purpose([chat_only, embed_enabled]) is True


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
