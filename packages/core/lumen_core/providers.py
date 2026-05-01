"""Shared provider-pool parsing helpers.

Both api and worker need the same effective provider list:
- explicit `providers` entries
- legacy `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` only when `providers` is absent
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from collections.abc import MutableMapping
from dataclasses import InitVar, dataclass, field, replace
from typing import Any, Callable
from urllib.parse import quote


@dataclass(frozen=True)
class ProviderProxyDefinition:
    name: str
    protocol: str
    host: str
    port: int
    username: str | None = None
    password: InitVar[str | None] = None
    private_key_path: str | None = None
    enabled: bool = True
    _password: str | None = field(init=False, repr=False, compare=False)

    def __post_init__(self, password: str | None) -> None:
        object.__setattr__(self, "_password", password)

    @property
    def password(self) -> str | None:
        return self._password


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    base_url: str
    api_key: str
    priority: int = 0
    weight: int = 1
    enabled: bool = True
    proxy_name: str | None = None
    proxy: ProviderProxyDefinition | None = field(
        default=None, repr=False, compare=False
    )
    # 账号级生图配额（默认 None=不限速；先不设默认值，运行一段时间后按账号订阅级别再填）。
    # image_rate_limit 形如 "5/min" / "50/h" / "200/d"，空值=不查 Redis 短路。
    image_rate_limit: str | None = None
    image_daily_quota: int | None = None
    image_jobs_enabled: bool = False
    # When this provider is selected for the image_jobs route, decide which
    # upstream endpoint the sidecar should forward to.
    #   "auto"        — Lumen picks per-request, learning from health stats
    #   "generations" — always /v1/images/generations (or /v1/images/edits for edit)
    #   "responses"   — always /v1/responses
    image_jobs_endpoint: str = "auto"
    # 锁定 endpoint：当 image_jobs_endpoint 为 generations / responses 时，
    # True 表示该 provider 只能服务对应的上游 endpoint kind。锁到
    # generations 时不会被用于 /v1/responses 文本、探活或 fallback；锁到
    # responses 时不会被用于 /v1/images/*。auto 时此字段无意义。
    image_jobs_endpoint_lock: bool = False
    # Per-provider sidecar base URL. Empty string ("") means fall back to the
    # global `image.job_base_url` runtime setting. Lets us run multiple
    # sidecars (one per provider region/host) without cross-routing requests.
    image_jobs_base_url: str = ""
    # Per-provider concurrency cap for image generation tasks. Default 1
    # preserves the historical "one in-flight per provider" behaviour. Bump it
    # when a single account can sustain more concurrent generations (e.g. paid
    # plans with higher rate limits, or sidecar-fronted accounts where the
    # bottleneck is server-side queue depth, not the upstream account itself).
    image_concurrency: int = 1


IMAGE_JOBS_ENDPOINT_VALUES = ("auto", "generations", "responses")
DEFAULT_LEGACY_PROVIDER_BASE_URL = "https://api.example.com"
_MAX_PROVIDER_WEIGHT = 1000
_PROXY_PROTOCOL_ALIASES = {
    "s5": "socks5",
    "socks": "socks5",
    "socks5": "socks5",
    "socks5h": "socks5",
    "ssh": "ssh",
}


def endpoint_kind_allowed(provider: Any, endpoint_kind: str | None) -> bool:
    """Return whether a provider may be used for an upstream endpoint kind.

    ``endpoint_kind`` is the actual upstream protocol family: ``responses`` for
    POST /v1/responses, ``generations`` for POST /v1/images/generations or
    /v1/images/edits, and ``models`` for GET /v1/models. Locked providers are
    exclusive to their configured image endpoint and are not used for unrelated
    paths such as model catalog fetches.
    """
    if endpoint_kind not in {"generations", "responses", "models"}:
        return True
    if isinstance(provider, dict):
        locked = bool(provider.get("image_jobs_endpoint_lock", False))
        configured = provider.get("image_jobs_endpoint", "auto")
    else:
        locked = bool(getattr(provider, "image_jobs_endpoint_lock", False))
        configured = getattr(provider, "image_jobs_endpoint", "auto")
    if not locked or configured not in {"generations", "responses"}:
        return True
    if endpoint_kind == "models":
        return False
    return configured == endpoint_kind


@dataclass
class RoundRobinState:
    counters: dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def advance(self, priority: int) -> int:
        with self._lock:
            counter = self.counters.get(priority, 0)
            self.counters[priority] = counter + 1
            return counter


def _parse_weight(raw: Any) -> int:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1
    if not math.isfinite(value):
        return 1
    return max(1, min(int(value), _MAX_PROVIDER_WEIGHT))


def _parse_optional_str(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _parse_proxy_protocol(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return "socks5"
    normalized = raw.strip().lower()
    protocol = _PROXY_PROTOCOL_ALIASES.get(normalized)
    if protocol is None:
        raise ValueError("proxy protocol must be socks5 or ssh")
    return protocol


def _parse_proxy_port(raw: Any, *, default: int) -> int:
    if raw in (None, ""):
        return default
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("proxy port must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("proxy port must be between 1 and 65535")
    return port


def parse_provider_item(item: dict[str, Any], *, index: int) -> ProviderDefinition:
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        name = f"provider-{index}"
    base_url = item.get("base_url", "")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError(f"provider {name}: base_url is required")
    api_key = item.get("api_key", "")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(f"provider {name}: api_key is required")
    priority = int(item.get("priority", 0))
    weight = _parse_weight(item.get("weight", 1))
    enabled = bool(item.get("enabled", True))
    rate_limit_raw = item.get("image_rate_limit")
    image_rate_limit: str | None = None
    if isinstance(rate_limit_raw, str) and rate_limit_raw.strip():
        image_rate_limit = rate_limit_raw.strip()
    quota_raw = item.get("image_daily_quota")
    image_daily_quota: int | None = None
    if isinstance(quota_raw, (int, str)) and str(quota_raw).strip():
        try:
            quota_int = int(quota_raw)
            if quota_int > 0:
                image_daily_quota = quota_int
        except (TypeError, ValueError):
            image_daily_quota = None
    proxy_name = _parse_optional_str(item.get("proxy") or item.get("proxy_name"))
    raw_endpoint = item.get("image_jobs_endpoint")
    if isinstance(raw_endpoint, str):
        normalized_endpoint = raw_endpoint.strip().lower()
    else:
        normalized_endpoint = "auto"
    if normalized_endpoint not in IMAGE_JOBS_ENDPOINT_VALUES:
        normalized_endpoint = "auto"
    raw_lock = item.get("image_jobs_endpoint_lock", False)
    image_jobs_endpoint_lock = bool(raw_lock) if normalized_endpoint != "auto" else False
    raw_base = item.get("image_jobs_base_url")
    image_jobs_base_url = ""
    if isinstance(raw_base, str):
        candidate = raw_base.strip().rstrip("/")
        if candidate:
            image_jobs_base_url = candidate
    raw_conc = item.get("image_concurrency", 1)
    try:
        image_concurrency = max(1, int(raw_conc))
    except (TypeError, ValueError):
        image_concurrency = 1
    return ProviderDefinition(
        name=name.strip(),
        base_url=base_url.strip().rstrip("/"),
        api_key=api_key.strip(),
        priority=priority,
        weight=weight,
        enabled=enabled,
        proxy_name=proxy_name,
        image_rate_limit=image_rate_limit,
        image_daily_quota=image_daily_quota,
        image_jobs_enabled=bool(item.get("image_jobs_enabled", False)),
        image_jobs_endpoint=normalized_endpoint,
        image_jobs_endpoint_lock=image_jobs_endpoint_lock,
        image_jobs_base_url=image_jobs_base_url,
        image_concurrency=image_concurrency,
    )


def parse_proxy_item(item: dict[str, Any], *, index: int) -> ProviderProxyDefinition:
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        name = f"proxy-{index}"
    protocol = _parse_proxy_protocol(item.get("type", item.get("protocol")))
    host = item.get("host", "")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(f"proxy {name}: host is required")
    port = _parse_proxy_port(
        item.get("port"),
        default=22 if protocol == "ssh" else 1080,
    )
    username = _parse_optional_str(item.get("username"))
    password = _parse_optional_str(item.get("password"))
    private_key_path = _parse_optional_str(
        item.get("private_key_path") or item.get("identity_file")
    )
    return ProviderProxyDefinition(
        name=name.strip(),
        protocol=protocol,
        host=host.strip(),
        port=port,
        username=username,
        password=password,
        private_key_path=private_key_path,
        enabled=bool(item.get("enabled", True)),
    )


def _split_provider_config_items(
    value: Any,
) -> tuple[list[Any], list[Any], list[str]]:
    if isinstance(value, list):
        return value, [], []
    if not isinstance(value, dict):
        return [], [], ["providers must be a JSON array or object"]
    provider_items = value.get("providers")
    if not isinstance(provider_items, list):
        return [], [], ["providers.providers must be a non-empty JSON array"]
    proxy_items = value.get("proxies", [])
    if proxy_items is None:
        proxy_items = []
    if not isinstance(proxy_items, list):
        return provider_items, [], ["providers.proxies must be a JSON array"]
    return provider_items, proxy_items, []


def _attach_provider_proxies(
    providers: list[ProviderDefinition],
    proxies: list[ProviderProxyDefinition],
) -> tuple[list[ProviderDefinition], list[str]]:
    proxy_by_name = {p.name: p for p in proxies}
    result: list[ProviderDefinition] = []
    errors: list[str] = []
    for provider in providers:
        proxy = None
        if provider.proxy_name:
            proxy = proxy_by_name.get(provider.proxy_name)
            if proxy is None:
                errors.append(
                    f"provider {provider.name}: proxy {provider.proxy_name} not found"
                )
        result.append(replace(provider, proxy=proxy))
    return result, errors


def parse_provider_config_json(
    raw: str | None,
) -> tuple[list[ProviderDefinition], list[ProviderProxyDefinition], list[str]]:
    if not raw:
        return [], [], []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [], [f"providers JSON parse failed: {exc}"]
    provider_items, proxy_items, errors = _split_provider_config_items(value)
    if errors:
        return [], [], errors
    if not provider_items:
        return [], [], []

    providers: list[ProviderDefinition] = []
    proxies: list[ProviderProxyDefinition] = []
    for i, item in enumerate(proxy_items):
        if not isinstance(item, dict):
            errors.append(f"proxies[{i}] is not an object")
            continue
        try:
            proxies.append(parse_proxy_item(item, index=i))
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"proxies[{i}] invalid: {exc}")
    for i, item in enumerate(provider_items):
        if not isinstance(item, dict):
            errors.append(f"providers[{i}] is not an object")
            continue
        try:
            providers.append(parse_provider_item(item, index=i))
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"providers[{i}] invalid: {exc}")
    providers, attach_errors = _attach_provider_proxies(providers, proxies)
    errors.extend(attach_errors)
    return providers, proxies, errors


def parse_provider_json(raw: str | None) -> tuple[list[ProviderDefinition], list[str]]:
    providers, _proxies, errors = parse_provider_config_json(raw)
    return providers, errors


def parse_proxy_json(raw: str | None) -> tuple[list[ProviderProxyDefinition], list[str]]:
    _providers, proxies, errors = parse_provider_config_json(raw)
    return proxies, errors


def build_legacy_provider(
    *,
    base_url: str | None,
    api_key: str | None,
) -> ProviderDefinition | None:
    """Build the compatibility provider from legacy env vars.

    This is intentionally only a fallback for deployments that have not rewritten
    `.env` to `PROVIDERS` yet. It is not merged into an explicit provider pool.
    """
    key = (api_key or "").strip()
    if not key:
        return None
    base = (base_url or DEFAULT_LEGACY_PROVIDER_BASE_URL).strip().rstrip("/")
    if not base:
        base = DEFAULT_LEGACY_PROVIDER_BASE_URL
    return ProviderDefinition(
        name="default",
        base_url=base,
        api_key=key,
        priority=0,
        weight=1,
        enabled=True,
    )


def build_effective_providers(
    *,
    raw_providers: str | None,
    legacy_base_url: str | None = None,
    legacy_api_key: str | None = None,
) -> tuple[list[ProviderDefinition], list[str]]:
    providers, _proxies, errors = parse_provider_config_json(raw_providers)
    if providers:
        return providers, errors
    legacy = build_legacy_provider(
        base_url=legacy_base_url,
        api_key=legacy_api_key,
    )
    return ([legacy] if legacy else [], errors)


def build_effective_provider_config(
    *,
    raw_providers: str | None,
    legacy_base_url: str | None = None,
    legacy_api_key: str | None = None,
) -> tuple[list[ProviderDefinition], list[ProviderProxyDefinition], list[str]]:
    providers, proxies, errors = parse_provider_config_json(raw_providers)
    if providers:
        return providers, proxies, errors
    legacy = build_legacy_provider(
        base_url=legacy_base_url,
        api_key=legacy_api_key,
    )
    return ([legacy] if legacy else [], [], errors)


def advance_round_robin_counter(
    rr_counters: MutableMapping[int, int] | RoundRobinState,
    priority: int,
) -> int:
    """Return the current counter for `priority`, then advance it."""
    if isinstance(rr_counters, RoundRobinState):
        return rr_counters.advance(priority)
    counter = rr_counters.get(priority, 0)
    rr_counters[priority] = counter + 1
    return counter


def _weighted_priority_order(
    providers: list[ProviderDefinition],
    counter_for_priority: Callable[[int], int] | None,
) -> list[ProviderDefinition]:
    enabled = [p for p in providers if p.enabled]
    by_priority: dict[int, list[ProviderDefinition]] = {}
    for p in enabled:
        by_priority.setdefault(p.priority, []).append(p)

    result: list[ProviderDefinition] = []
    for prio in sorted(by_priority.keys(), reverse=True):
        group = by_priority[prio]
        if len(group) <= 1:
            result.extend(group)
            continue
        total_weight = sum(max(1, p.weight) for p in group)
        counter = counter_for_priority(prio) if counter_for_priority else 0
        offset = counter % max(total_weight, 1)

        seen: set[str] = set()
        accumulated = 0
        for p in group:
            accumulated += max(1, p.weight)
            if accumulated > offset and p.name not in seen:
                seen.add(p.name)
                result.append(p)
        for p in group:
            if p.name not in seen:
                seen.add(p.name)
                result.append(p)
    return result


def weighted_priority_order_and_advance(
    providers: list[ProviderDefinition],
    rr_counters: MutableMapping[int, int] | RoundRobinState,
) -> list[ProviderDefinition]:
    return _weighted_priority_order(
        providers,
        lambda priority: advance_round_robin_counter(rr_counters, priority),
    )


def weighted_priority_order(
    providers: list[ProviderDefinition],
    rr_counters: MutableMapping[int, int] | RoundRobinState | None = None,
) -> list[ProviderDefinition]:
    """Return weighted provider order.

    Passing `rr_counters` is kept for compatibility; new code should use
    `weighted_priority_order_and_advance` when it wants to mutate counters.
    """
    if rr_counters is not None:
        return weighted_priority_order_and_advance(providers, rr_counters)
    return _weighted_priority_order(providers, None)


def _proxy_host_for_url(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def socks_proxy_url(proxy: ProviderProxyDefinition) -> str | None:
    if not proxy.enabled:
        return None
    if proxy.protocol != "socks5":
        return None
    host = _proxy_host_for_url(proxy.host)
    auth = ""
    if proxy.username:
        auth = quote(proxy.username, safe="")
        if proxy.password:
            auth += f":{quote(proxy.password, safe='')}"
        auth += "@"
    return f"socks5h://{auth}{host}:{proxy.port}"


@dataclass
class _SshTunnel:
    url: str
    process: asyncio.subprocess.Process


_SSH_TUNNELS: dict[str, _SshTunnel] = {}
_SSH_TUNNEL_LOCK = asyncio.Lock()
_SSH_TUNNEL_START_ATTEMPTS = 3
_SSH_TUNNEL_READY_CHECKS = 30


def _ssh_tunnel_key(proxy: ProviderProxyDefinition) -> str:
    password_digest = (
        hashlib.sha256(proxy.password.encode("utf-8")).hexdigest()
        if proxy.password
        else ""
    )
    return "\x1f".join(
        [
            proxy.name,
            proxy.host,
            str(proxy.port),
            proxy.username or "",
            password_digest,
            proxy.private_key_path or "",
        ]
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _local_port_accepts(port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(2), timeout=0.3)
        return reply == b"\x05\x00"
    except Exception:
        return False
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _write_ssh_askpass_helper() -> str:
    fd, path = tempfile.mkstemp(prefix="lumen-ssh-askpass-", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
        fh.write("printf '%s\\n' \"$LUMEN_SSH_PASSWORD\"\n")
    os.chmod(path, 0o700)
    return path


def _unlink_quietly(path: str | None) -> None:
    if not path:
        return
    with contextlib.suppress(OSError):
        os.unlink(path)


async def _read_process_stderr(
    proc: asyncio.subprocess.Process,
    *,
    limit: int = 2000,
) -> str:
    if proc.stderr is None:
        return ""
    try:
        raw = await asyncio.wait_for(proc.stderr.read(limit), timeout=0.2)
    except Exception:
        return ""
    return raw.decode("utf-8", errors="replace")


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        return
    except Exception:
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def _ensure_ssh_socks_proxy(proxy: ProviderProxyDefinition) -> str:
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        raise RuntimeError("ssh binary not found; cannot start ssh proxy")
    key = _ssh_tunnel_key(proxy)
    existing = _SSH_TUNNELS.get(key)
    if existing and existing.process.returncode is None:
        return existing.url

    async with _SSH_TUNNEL_LOCK:
        existing = _SSH_TUNNELS.get(key)
        if existing and existing.process.returncode is None:
            return existing.url
        for old_key, tunnel in list(_SSH_TUNNELS.items()):
            if old_key == key:
                continue
            if old_key.startswith(f"{proxy.name}\x1f"):
                _SSH_TUNNELS.pop(old_key, None)
                await _terminate_process(tunnel.process)

        last_error = ""
        for _attempt in range(_SSH_TUNNEL_START_ATTEMPTS):
            local_port = _free_local_port()
            target = f"{proxy.username}@{proxy.host}" if proxy.username else proxy.host
            cmd = [
                ssh_bin,
                "-N",
                "-D",
                f"127.0.0.1:{local_port}",
                "-p",
                str(proxy.port),
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
            ]
            if proxy.password:
                cmd.extend(
                    [
                        "-o",
                        "BatchMode=no",
                        "-o",
                        "PasswordAuthentication=yes",
                        "-o",
                        "KbdInteractiveAuthentication=yes",
                        "-o",
                        "PreferredAuthentications=password,keyboard-interactive,publickey",
                    ]
                )
            else:
                cmd.extend(
                    [
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "PasswordAuthentication=no",
                    ]
                )
            if proxy.private_key_path:
                cmd.extend(["-i", proxy.private_key_path])
            cmd.append(target)

            env = None
            askpass_path = None
            sshpass_bin = shutil.which("sshpass") if proxy.password else None
            if proxy.password and sshpass_bin:
                cmd = [sshpass_bin, "-e", *cmd]
                env = os.environ.copy()
                env["SSHPASS"] = proxy.password
            elif proxy.password:
                askpass_path = _write_ssh_askpass_helper()
                env = os.environ.copy()
                env["SSH_ASKPASS"] = askpass_path
                env["SSH_ASKPASS_REQUIRE"] = "force"
                env.setdefault("DISPLAY", "localhost:0")
                env["LUMEN_SSH_PASSWORD"] = proxy.password

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                for _ in range(_SSH_TUNNEL_READY_CHECKS):
                    if proc.returncode is not None:
                        stderr = await _read_process_stderr(proc)
                        last_error = (
                            f"exited with {proc.returncode}: {stderr}".strip()
                        )
                        break
                    if await _local_port_accepts(local_port):
                        url = f"socks5h://127.0.0.1:{local_port}"
                        _SSH_TUNNELS[key] = _SshTunnel(url=url, process=proc)
                        return url
                    await asyncio.sleep(0.1)
                else:
                    stderr = await _read_process_stderr(proc)
                    last_error = f"timed out waiting for local SOCKS port: {stderr}".strip()
            finally:
                _unlink_quietly(askpass_path)

            await _terminate_process(proc)

        raise RuntimeError(
            f"ssh proxy {proxy.name} failed to start after "
            f"{_SSH_TUNNEL_START_ATTEMPTS} attempts: {last_error}"
        )


async def resolve_provider_proxy_url(
    proxy: ProviderProxyDefinition | None,
) -> str | None:
    if proxy is None or not proxy.enabled:
        return None
    if proxy.protocol == "socks5":
        return socks_proxy_url(proxy)
    if proxy.protocol == "ssh":
        return await _ensure_ssh_socks_proxy(proxy)
    raise RuntimeError(f"unsupported proxy protocol: {proxy.protocol}")


async def close_provider_proxy_tunnels() -> None:
    tunnels = list(_SSH_TUNNELS.values())
    _SSH_TUNNELS.clear()
    for tunnel in tunnels:
        await _terminate_process(tunnel.process)


__all__ = [
    "ProviderDefinition",
    "ProviderProxyDefinition",
    "RoundRobinState",
    "DEFAULT_LEGACY_PROVIDER_BASE_URL",
    "advance_round_robin_counter",
    "build_effective_provider_config",
    "build_effective_providers",
    "build_legacy_provider",
    "close_provider_proxy_tunnels",
    "endpoint_kind_allowed",
    "parse_provider_item",
    "parse_provider_config_json",
    "parse_provider_json",
    "parse_proxy_item",
    "parse_proxy_json",
    "resolve_provider_proxy_url",
    "socks_proxy_url",
    "weighted_priority_order",
    "weighted_priority_order_and_advance",
]
