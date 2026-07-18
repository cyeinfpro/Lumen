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
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    DEFAULT_PROVIDER_PURPOSES,
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
    route_to_purpose,
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
            "image_edit_input_transport": " file ",
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
    assert provider.image_edit_input_transport == "file"
    assert provider.purposes == DEFAULT_PROVIDER_PURPOSES


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
    provider_mod._SSH_TUNNELS.clear()
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

    async def fake_local_port_accepts(port: int) -> bool:
        captured["port"] = port
        return True

    monkeypatch.setattr(provider_mod.shutil, "which", fake_which)
    monkeypatch.setattr(
        provider_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
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
            known_hosts_path=source_known_hosts,
        )
    )

    cmd = captured["cmd"]
    env = captured["env"]
    assert url == "socks5h://127.0.0.1:41555"
    assert isinstance(cmd, tuple)
    assert cmd[0] == "/usr/bin/ssh"
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

    await provider_mod.close_provider_proxy_tunnels()


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_rejects_symlink_known_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_mod._SSH_TUNNELS.clear()
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
        provider_mod.shutil,
        "which",
        lambda name: "/usr/bin/ssh" if name == "ssh" else None,
    )
    monkeypatch.setattr(
        provider_mod.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        await provider_mod.resolve_provider_proxy_url(
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
    provider_mod._SSH_TUNNELS.clear()
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

    monkeypatch.setattr(provider_mod.shutil, "which", fake_which)
    monkeypatch.setattr(
        provider_mod.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="refusing unknown host key"):
        await provider_mod.resolve_provider_proxy_url(
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
    provider_mod._SSH_TUNNELS.clear()
    known_hosts_path = _write_known_hosts(tmp_path)
    os.chmod(known_hosts_path, 0o666)
    monkeypatch.setattr(
        provider_mod.shutil,
        "which",
        lambda name: "/usr/bin/ssh" if name == "ssh" else None,
    )

    with pytest.raises(RuntimeError, match="group/world writable"):
        await provider_mod.resolve_provider_proxy_url(
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
    provider_mod._SSH_TUNNELS.clear()
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

    async def fake_local_port_accepts(_port: int) -> bool:
        return True

    monkeypatch.setattr(provider_mod.shutil, "which", fake_which)
    monkeypatch.setattr(provider_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        provider_mod.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        provider_mod,
        "_local_port_accepts",
        fake_local_port_accepts,
    )
    monkeypatch.setattr(provider_mod, "_free_local_port", lambda: 41558)

    url = await provider_mod.resolve_provider_proxy_url(
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

    await provider_mod.close_provider_proxy_tunnels()


@pytest.mark.asyncio
async def test_resolve_ssh_proxy_rejects_host_key_fingerprint_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_mod._SSH_TUNNELS.clear()
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

    monkeypatch.setattr(provider_mod.shutil, "which", fake_which)
    monkeypatch.setattr(provider_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        provider_mod.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        await provider_mod.resolve_provider_proxy_url(
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
    provider_mod._SSH_TUNNELS.clear()
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

    async def fake_local_port_accepts(_port: int) -> bool:
        return False

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(provider_mod.shutil, "which", fake_which)
    monkeypatch.setattr(
        provider_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(provider_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(provider_mod, "_free_local_port", lambda: 41556)
    monkeypatch.setattr(provider_mod, "_local_port_accepts", fake_local_port_accepts)
    monkeypatch.setattr(provider_mod, "_SSH_TUNNEL_READY_CHECKS", 1)
    monkeypatch.setattr(provider_mod, "_SSH_TUNNEL_START_ATTEMPTS", 1)

    with pytest.raises(RuntimeError, match="failed to start"):
        await provider_mod.resolve_provider_proxy_url(
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
    provider_mod._SSH_TUNNELS.clear()
    captured: dict[str, object] = {}
    proc = _FakeSshProcess()
    unlink_events: list[tuple[str, int | None, bool]] = []
    original_unlink = provider_mod._unlink_quietly

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

    async def fake_local_port_accepts(_port: int) -> bool:
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

    monkeypatch.setattr(provider_mod.shutil, "which", fake_which)
    monkeypatch.setattr(
        provider_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(provider_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(provider_mod, "_free_local_port", lambda: 41557)
    monkeypatch.setattr(provider_mod, "_local_port_accepts", fake_local_port_accepts)
    monkeypatch.setattr(provider_mod, "_unlink_quietly", tracking_unlink)
    monkeypatch.setattr(provider_mod, "_SSH_TUNNEL_START_ATTEMPTS", 1)

    with pytest.raises(asyncio.CancelledError):
        await provider_mod.resolve_provider_proxy_url(
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
    for field in ("enabled", "image_jobs_enabled"):
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
