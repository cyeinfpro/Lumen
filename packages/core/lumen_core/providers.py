"""Shared provider-pool parsing helpers.

Both api and worker need the same effective provider list:
- explicit `providers` entries
- legacy `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` only when `providers` is absent
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
from collections.abc import MutableMapping
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol, TypeVar
from urllib.parse import quote


IMAGE_EDIT_INPUT_TRANSPORT_VALUES = ("url", "file")
DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT = "url"
PROVIDER_PURPOSE_VALUES = ("chat", "image", "embedding")
DEFAULT_PROVIDER_PURPOSES = ("chat", "image")


class _WeightedProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def priority(self) -> int: ...

    @property
    def weight(self) -> int: ...

    @property
    def enabled(self) -> bool: ...


_WeightedProviderT = TypeVar("_WeightedProviderT", bound=_WeightedProvider)


@dataclass(frozen=True)
class ProviderProxyDefinition:
    name: str
    protocol: str
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False, compare=False)
    private_key_path: str | None = None
    enabled: bool = True
    known_hosts_path: str | None = None
    host_key_fingerprint: str | None = field(
        default=None,
        repr=False,
    )

    @property
    def known_hosts_file(self) -> str | None:
        return self.known_hosts_path

    @property
    def known_hosts(self) -> str | None:
        return self.known_hosts_path

    @property
    def fingerprint(self) -> str | None:
        return self.host_key_fingerprint


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    base_url: str
    api_key: str
    priority: int = 0
    weight: int = 1
    enabled: bool = True
    purposes: tuple[str, ...] = DEFAULT_PROVIDER_PURPOSES
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
    # Controls how image-job forwards reference images for /v1/images/edits:
    #   "url"  — JSON body with images[].image_url, used by sub2api-style gateways
    #   "file" — multipart/form-data image[] files, used by OpenAI/new-api-style gateways
    # Direct non-image-job /v1/images/edits is already multipart and ignores this.
    image_edit_input_transport: str = DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT
    # Per-provider concurrency cap for image generation tasks. Default 1
    # preserves the historical "one in-flight per provider" behaviour. Bump it
    # when a single account can sustain more concurrent generations (e.g. paid
    # plans with higher rate limits, or sidecar-fronted accounts where the
    # bottleneck is server-side queue depth, not the upstream account itself).
    image_concurrency: int = 1
    # Capability flags (image-stability-hardening-plan §P2). 三态语义：
    #   True  — 已确认支持
    #   False — 已确认不支持，路由时排除（避免无意义尝试 + 烧配额）
    #   None  — 未知，按现有 endpoint_lock / 健康度 / 失败学习行为处理
    # 字段缺失（旧配置）默认 None，保证向后兼容。
    responses_supported: bool | None = None
    image_generations_supported: bool | None = None
    image_responses_supported: bool | None = None


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
_SSH_HOST_KEY_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?$")


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
        locked = _parse_optional_bool(provider.get("image_jobs_endpoint_lock")) is True
        configured = provider.get("image_jobs_endpoint", "auto")
    else:
        locked = (
            _parse_optional_bool(getattr(provider, "image_jobs_endpoint_lock", False))
            is True
        )
        configured = getattr(provider, "image_jobs_endpoint", "auto")
    if not locked or configured not in {"generations", "responses"}:
        return True
    if endpoint_kind == "models":
        return False
    return configured == endpoint_kind


def _provider_capability(provider: Any, attr: str) -> bool | None:
    """Read a capability tri-state from either a dataclass or a dict shape."""
    if isinstance(provider, dict):
        value = provider.get(attr)
    else:
        value = getattr(provider, attr, None)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def provider_supports_route(
    provider: Any,
    *,
    route: str,
    endpoint_kind: str | None,
) -> bool:
    """Capability gate for provider selection (image-stability-hardening §P2).

    True  → allowed; False → explicitly disabled (skip this provider entirely).
    Unknown capability (None) → allowed, fall back to runtime health learning.

    ``route`` is the high-level pool route: ``image`` for image generation /
    edit, ``text`` for general /v1/responses traffic, ``models`` for catalog
    fetches. ``endpoint_kind`` further narrows which upstream URL we'll hit
    (``responses`` vs ``generations`` for the image route).
    """
    if route == "models":
        # Catalog probes hit /v1/models; only honor the responses_supported flag
        # because the catalog is served by the responses-style endpoint family.
        return _provider_capability(provider, "responses_supported") is not False

    if route != "image":
        # Text / completion / other routes use the responses endpoint family.
        return _provider_capability(provider, "responses_supported") is not False

    # route == "image"
    if endpoint_kind == "responses":
        if _provider_capability(provider, "image_responses_supported") is False:
            return False
        if _provider_capability(provider, "responses_supported") is False:
            return False
        return True
    if endpoint_kind == "generations":
        return (
            _provider_capability(provider, "image_generations_supported") is not False
        )
    # endpoint_kind unknown / "auto" → allow if neither image capability is
    # explicitly disabled (still need at least one viable path).
    img_resp = _provider_capability(provider, "image_responses_supported")
    img_gen = _provider_capability(provider, "image_generations_supported")
    if img_resp is False and img_gen is False:
        return False
    return True


def route_to_purpose(route: str | None) -> str:
    """Map legacy high-level provider routes to account-level purposes."""
    if route in {
        "image",
        "image_jobs",
        "image2",
        "image2_direct",
        "image2_edit_direct",
    }:
        return "image"
    if route == "embedding":
        return "embedding"
    return "chat"


def has_embedding_purpose(providers: list[ProviderDefinition]) -> bool:
    """Return True iff at least one enabled provider exposes the embedding purpose.

    Memory writes/retrieval depend on a real text-embedding-3-large provider —
    without one we cannot compute usable cosine similarity, so the entire
    feature must short-circuit instead of writing deterministic placeholders
    that won't match anything at retrieval time.
    """
    return any(p.enabled and "embedding" in p.purposes for p in providers)


def normalize_provider_purposes(raw: Any) -> tuple[str, ...]:
    """Parse provider purposes with backward-compatible defaults.

    Missing/empty values on legacy providers mean "chat + image"; explicit
    values must be a non-empty subset of PROVIDER_PURPOSE_VALUES.
    """
    if raw is None or raw == "":
        return DEFAULT_PROVIDER_PURPOSES
    if not isinstance(raw, list | tuple):
        raise ValueError("provider purposes must be an array")
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("provider purposes entries must be strings")
        value = item.strip().lower()
        if value not in PROVIDER_PURPOSE_VALUES:
            raise ValueError(
                "provider purposes entries must be one of "
                + ", ".join(PROVIDER_PURPOSE_VALUES)
            )
        if value not in seen:
            seen.append(value)
    if not seen:
        raise ValueError("provider purposes must contain at least one value")
    return tuple(seen)


@dataclass
class RoundRobinState:
    counters: dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

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


def _parse_priority(raw: Any) -> int:
    if raw in (None, ""):
        return 0
    if isinstance(raw, bool):
        raise ValueError("provider priority must be an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        value = raw.strip()
        if value and value.lstrip("+-").isdigit():
            return int(value)
    raise ValueError("provider priority must be an integer")


def _parse_optional_str(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _parse_optional_bool(raw: Any) -> bool | None:
    """Parse capability tri-state. None / 缺失 / 空字符串 → None（未知）。

    显式 ``"true"`` / ``"false"`` / ``true`` / ``false`` 才映射 bool；其它（含 0/1 这类
    歧义值）一律按未知处理，避免老配置里残留的 truthy/falsy 字段被误判成显式 capability。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        return None
    return None


def _parse_bool(raw: Any, *, default: bool, field: str) -> bool:
    if raw is None or raw == "":
        return default
    parsed = _parse_optional_bool(raw)
    if parsed is None:
        raise ValueError(f"{field} must be a boolean")
    return parsed


def parse_provider_bool(raw: Any, *, default: bool = False) -> bool:
    return _parse_bool(raw, default=default, field="provider boolean")


def normalize_image_edit_input_transport(raw: Any) -> str:
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in IMAGE_EDIT_INPUT_TRANSPORT_VALUES:
            return value
    return DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT


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


def _parse_proxy_alias_string(
    item: dict[str, Any],
    *,
    keys: tuple[str, ...],
    field_name: str,
    proxy_name: str,
) -> str | None:
    values = [
        (key, value)
        for key in keys
        if (value := _parse_optional_str(item.get(key))) is not None
    ]
    if len({value for _key, value in values}) > 1:
        raise ValueError(f"proxy {proxy_name}: {field_name} aliases disagree")
    return values[0][1] if values else None


def _provider_name(item: dict[str, Any], index: int) -> str:
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        return f"provider-{index}"
    return name.strip()


def _provider_api_key(
    item: dict[str, Any],
    *,
    provider_name: str,
    enabled: bool,
) -> str:
    api_key = item.get("api_key", "")
    if not isinstance(api_key, str):
        raise ValueError(f"provider {provider_name}: api_key must be a string")
    api_key = api_key.strip()
    if enabled and not api_key:
        raise ValueError(f"provider {provider_name}: api_key is required")
    return api_key


def _positive_optional_int(raw: Any) -> int | None:
    if not isinstance(raw, (int, str)) or not str(raw).strip():
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _image_jobs_endpoint(item: dict[str, Any]) -> tuple[str, bool]:
    raw_endpoint = item.get("image_jobs_endpoint")
    endpoint = raw_endpoint.strip().lower() if isinstance(raw_endpoint, str) else "auto"
    if endpoint not in IMAGE_JOBS_ENDPOINT_VALUES:
        endpoint = "auto"
    raw_lock = item.get("image_jobs_endpoint_lock", False)
    parsed_lock = _parse_optional_bool(raw_lock)
    if raw_lock not in (None, "") and parsed_lock is None:
        raise ValueError("image_jobs_endpoint_lock must be a boolean")
    if parsed_lock and endpoint == "auto":
        raise ValueError(
            "image_jobs_endpoint_lock requires image_jobs_endpoint to be responses or generations"
        )
    return endpoint, bool(parsed_lock)


def _normalized_optional_base_url(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip().rstrip("/")


def _image_concurrency(raw: Any) -> int:
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def parse_provider_item(item: dict[str, Any], *, index: int) -> ProviderDefinition:
    name = _provider_name(item, index)
    base_url = item.get("base_url", "")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError(f"provider {name}: base_url is required")
    enabled = _parse_bool(item.get("enabled"), default=True, field="enabled")
    api_key = _provider_api_key(item, provider_name=name, enabled=enabled)
    priority = _parse_priority(item.get("priority", 0))
    weight = _parse_weight(item.get("weight", 1))
    purposes = normalize_provider_purposes(item.get("purposes"))
    rate_limit_raw = item.get("image_rate_limit")
    image_rate_limit = (
        rate_limit_raw.strip()
        if isinstance(rate_limit_raw, str) and rate_limit_raw.strip()
        else None
    )
    image_daily_quota = _positive_optional_int(item.get("image_daily_quota"))
    proxy_name = _parse_optional_str(item.get("proxy") or item.get("proxy_name"))
    normalized_endpoint, image_jobs_endpoint_lock = _image_jobs_endpoint(item)
    image_jobs_base_url = _normalized_optional_base_url(item.get("image_jobs_base_url"))
    image_edit_input_transport = normalize_image_edit_input_transport(
        item.get("image_edit_input_transport")
    )
    image_concurrency = _image_concurrency(item.get("image_concurrency", 1))
    return ProviderDefinition(
        name=name,
        base_url=base_url.strip().rstrip("/"),
        api_key=api_key,
        priority=priority,
        weight=weight,
        enabled=enabled,
        purposes=purposes,
        proxy_name=proxy_name,
        image_rate_limit=image_rate_limit,
        image_daily_quota=image_daily_quota,
        image_jobs_enabled=_parse_bool(
            item.get("image_jobs_enabled"),
            default=False,
            field="image_jobs_enabled",
        ),
        image_jobs_endpoint=normalized_endpoint,
        image_jobs_endpoint_lock=image_jobs_endpoint_lock,
        image_jobs_base_url=image_jobs_base_url,
        image_edit_input_transport=image_edit_input_transport,
        image_concurrency=image_concurrency,
        responses_supported=_parse_optional_bool(item.get("responses_supported")),
        image_generations_supported=_parse_optional_bool(
            item.get("image_generations_supported")
        ),
        image_responses_supported=_parse_optional_bool(
            item.get("image_responses_supported")
        ),
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
    known_hosts_path = _parse_proxy_alias_string(
        item,
        keys=("known_hosts_path", "known_hosts_file", "known_hosts"),
        field_name="known_hosts_path",
        proxy_name=name,
    )
    host_key_fingerprint = _parse_proxy_alias_string(
        item,
        keys=("host_key_fingerprint", "fingerprint"),
        field_name="host_key_fingerprint",
        proxy_name=name,
    )
    if host_key_fingerprint and not _SSH_HOST_KEY_FINGERPRINT_RE.fullmatch(
        host_key_fingerprint
    ):
        raise ValueError(
            f"proxy {name}: host_key_fingerprint must use SHA256:... format"
        )
    return ProviderProxyDefinition(
        name=name.strip(),
        protocol=protocol,
        host=host.strip(),
        port=port,
        username=username,
        password=password,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
        host_key_fingerprint=host_key_fingerprint,
        enabled=_parse_bool(item.get("enabled"), default=True, field="enabled"),
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
            if proxy is None and provider.enabled:
                errors.append(
                    f"provider {provider.name}: proxy {provider.proxy_name} not found"
                )
            elif proxy is not None and not proxy.enabled:
                if not provider.enabled:
                    proxy = None
                else:
                    errors.append(
                        f"provider {provider.name}: proxy {provider.proxy_name} is disabled"
                    )
                    proxy = None
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


def parse_proxy_json(
    raw: str | None,
) -> tuple[list[ProviderProxyDefinition], list[str]]:
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
        purposes=DEFAULT_PROVIDER_PURPOSES,
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
    providers: list[_WeightedProviderT],
    counter_for_priority: Callable[[int], int] | None,
) -> list[_WeightedProviderT]:
    enabled = [p for p in providers if p.enabled]
    by_priority: dict[int, list[_WeightedProviderT]] = {}
    for p in enabled:
        by_priority.setdefault(p.priority, []).append(p)

    result: list[_WeightedProviderT] = []
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
    providers: list[_WeightedProviderT],
    rr_counters: MutableMapping[int, int] | RoundRobinState,
) -> list[_WeightedProviderT]:
    return _weighted_priority_order(
        providers,
        lambda priority: advance_round_robin_counter(rr_counters, priority),
    )


def weighted_priority_order(
    providers: list[_WeightedProviderT],
    rr_counters: MutableMapping[int, int] | RoundRobinState | None = None,
) -> list[_WeightedProviderT]:
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
            proxy.known_hosts_path or "",
            proxy.host_key_fingerprint or "",
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


def _secret_dir() -> str:
    """Pick the safest writable directory for SSH secret/askpass files.

    Preference order:
      1. ``$XDG_RUNTIME_DIR``  — tmpfs scoped to the user, gone on logout
      2. ``/run/user/<uid>``    — same place even when the env var is missing
      3. ``tempfile.gettempdir()`` — last resort (typically /tmp; world-readable)

    Anything before /tmp is preferred because /tmp is shared between users on
    multi-tenant hosts and persists across re-login.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return xdg
    try:
        run_user = f"/run/user/{os.getuid()}"
    except AttributeError:  # pragma: no cover — non-POSIX
        run_user = ""
    if run_user and os.path.isdir(run_user):
        return run_user
    return tempfile.gettempdir()


def _atomic_secret_open(prefix: str, mode: int) -> tuple[int, str]:
    """Atomically create a 0o600/0o700 secret file in the runtime dir.

    Uses ``O_CREAT|O_EXCL|O_WRONLY`` so the file is created with the requested
    mode in one syscall; this eliminates the mkstemp+chmod race where a
    privileged reader could open the file between creation (default 0600 from
    mkstemp on Linux but not guaranteed cross-platform) and our explicit chmod.
    """
    base_dir = _secret_dir()
    for _ in range(8):
        suffix = secrets.token_hex(8)
        path = os.path.join(base_dir, f"{prefix}{suffix}")
        try:
            fd = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                mode,
            )
        except FileExistsError:
            continue
        return fd, path
    raise RuntimeError("failed to allocate unique secret filename")


def _write_secret_file(value: str) -> str:
    # Atomic 0o600 create in $XDG_RUNTIME_DIR; eliminates the previous
    # mkstemp-then-chmod race, and keeps the secret off /tmp on systems with
    # a per-user runtime tmpfs. Path is still returned for sshpass -f / askpass
    # consumption — readers must consume it promptly and unlink afterwards
    # (see `_unlink_quietly`); the path leaks via /proc/<pid>/environ for the
    # lifetime of the spawned ssh process, which is unavoidable as long as
    # askpass needs to dereference it from env.
    fd, path = _atomic_secret_open("lumen-ssh-secret-", 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
        fh.write("\n")
    return path


def _write_ssh_askpass_helper() -> str:
    fd, path = _atomic_secret_open("lumen-ssh-askpass-", 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
        fh.write('cat "$LUMEN_SSH_PASSWORD_FILE"\n')
    return path


def _normalize_ssh_fingerprint(value: str) -> str:
    return value.strip().rstrip("=")


def _ssh_key_fingerprint(key_blob: bytes) -> str:
    encoded = base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii")
    return f"SHA256:{encoded.rstrip('=')}"


def _known_hosts_file_error(
    proxy: ProviderProxyDefinition,
    path: str,
    detail: str,
) -> RuntimeError:
    return RuntimeError(f"ssh proxy {proxy.name} known_hosts {detail}: {path}")


def _open_known_hosts_file(
    proxy: ProviderProxyDefinition,
    path: str,
) -> tuple[int, os.stat_result]:
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise _known_hosts_file_error(proxy, path, "file is unavailable") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise _known_hosts_file_error(proxy, path, "path must not be a symlink")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = -1
    try:
        source_fd = os.open(path, flags)
        file_stat = os.fstat(source_fd)
    except OSError as exc:
        if source_fd >= 0:
            os.close(source_fd)
        if exc.errno == errno.ELOOP:
            raise _known_hosts_file_error(
                proxy,
                path,
                "path must not be a symlink",
            ) from exc
        raise _known_hosts_file_error(proxy, path, "file is unavailable") from exc
    if (path_stat.st_dev, path_stat.st_ino) != (
        file_stat.st_dev,
        file_stat.st_ino,
    ):
        os.close(source_fd)
        raise _known_hosts_file_error(
            proxy,
            path,
            "path changed during validation",
        )
    return source_fd, file_stat


def _validate_known_hosts_file(
    proxy: ProviderProxyDefinition,
    path: str,
    file_stat: os.stat_result,
) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise _known_hosts_file_error(proxy, path, "path is not a regular file")
    if not (file_stat.st_mode & 0o444):
        raise _known_hosts_file_error(proxy, path, "file is not readable")
    if file_stat.st_mode & 0o022:
        raise _known_hosts_file_error(
            proxy,
            path,
            "file is group/world writable",
        )
    if file_stat.st_size <= 0:
        raise _known_hosts_file_error(proxy, path, "file is empty")


def _copy_file_descriptor(source_fd: int, target_fd: int) -> int:
    copied = 0
    while True:
        chunk = os.read(source_fd, 64 * 1024)
        if not chunk:
            return copied
        view = memoryview(chunk)
        while view:
            written = os.write(target_fd, view)
            if written <= 0:
                raise OSError("known_hosts snapshot write returned no progress")
            copied += written
            view = view[written:]


def _known_hosts_stat_signature(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _copy_known_hosts_snapshot(
    proxy: ProviderProxyDefinition,
    path: str,
    source_fd: int,
    source_stat: os.stat_result,
) -> str:
    snapshot_fd, snapshot_path = _atomic_secret_open(
        "lumen-ssh-known-hosts-",
        0o600,
    )
    try:
        copied = _copy_file_descriptor(source_fd, snapshot_fd)
        final_stat = os.fstat(source_fd)
        if copied != source_stat.st_size or _known_hosts_stat_signature(
            final_stat
        ) != _known_hosts_stat_signature(source_stat):
            raise _known_hosts_file_error(
                proxy,
                path,
                "file changed during snapshot",
            )
    except BaseException:
        os.close(snapshot_fd)
        _unlink_quietly(snapshot_path)
        raise
    else:
        os.close(snapshot_fd)
        return snapshot_path


def _validated_known_hosts_path(proxy: ProviderProxyDefinition) -> str | None:
    raw_path = (proxy.known_hosts_path or "").strip()
    if not raw_path:
        return None
    path = os.path.abspath(os.path.expanduser(raw_path))
    source_fd, file_stat = _open_known_hosts_file(proxy, path)
    try:
        _validate_known_hosts_file(proxy, path, file_stat)
        return _copy_known_hosts_snapshot(
            proxy,
            path,
            source_fd,
            file_stat,
        )
    finally:
        os.close(source_fd)


def _parse_ssh_keyscan_output(
    output: str,
    *,
    expected_fingerprint: str,
) -> str | None:
    expected = _normalize_ssh_fingerprint(expected_fingerprint)
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            key_blob = base64.b64decode(fields[2], validate=True)
        except (TypeError, ValueError):
            continue
        if hmac.compare_digest(_ssh_key_fingerprint(key_blob), expected):
            return line
    return None


async def _scan_ssh_host_key(
    proxy: ProviderProxyDefinition,
    *,
    fingerprint: str,
) -> str:
    keyscan_bin = shutil.which("ssh-keyscan")
    if not keyscan_bin:
        raise RuntimeError(
            f"ssh proxy {proxy.name} requires ssh-keyscan for fingerprint verification"
        )
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                keyscan_bin,
                "-T",
                "5",
                "-p",
                str(proxy.port),
                "--",
                proxy.host,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"ssh proxy {proxy.name} host key scan failed: {type(exc).__name__}"
        ) from None
    output = result.stdout if isinstance(result.stdout, str) else ""
    matched = _parse_ssh_keyscan_output(
        output,
        expected_fingerprint=fingerprint,
    )
    if matched is None:
        detail = result.stderr.strip() if isinstance(result.stderr, str) else ""
        suffix = f": {detail[:200]}" if detail else ""
        raise RuntimeError(
            f"ssh proxy {proxy.name} host key fingerprint mismatch{suffix}"
        )
    return matched


def _write_known_hosts_line(line: str) -> str:
    fd, path = _atomic_secret_open("lumen-ssh-known-hosts-", 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
    except BaseException:
        _unlink_quietly(path)
        raise
    return path


async def _prepare_ssh_host_key_verification(
    proxy: ProviderProxyDefinition,
) -> tuple[str, str | None]:
    fingerprint = (proxy.host_key_fingerprint or "").strip()
    if fingerprint and not _SSH_HOST_KEY_FINGERPRINT_RE.fullmatch(fingerprint):
        raise RuntimeError(
            f"ssh proxy {proxy.name} has an invalid host key fingerprint"
        )
    if fingerprint:
        matched_line = await _scan_ssh_host_key(proxy, fingerprint=fingerprint)
        temporary_path = _write_known_hosts_line(matched_line)
        return temporary_path, temporary_path

    known_hosts_path = _validated_known_hosts_path(proxy)
    if known_hosts_path is None:
        raise RuntimeError(
            f"ssh proxy {proxy.name} requires known_hosts_path or "
            "host_key_fingerprint; refusing unknown host key"
        )
    return known_hosts_path, known_hosts_path


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


def _running_ssh_tunnel(key: str) -> _SshTunnel | None:
    tunnel = _SSH_TUNNELS.get(key)
    if tunnel is None or tunnel.process.returncode is not None:
        return None
    return tunnel


async def _close_stale_ssh_tunnels(
    proxy: ProviderProxyDefinition,
    current_key: str,
) -> None:
    for old_key, tunnel in list(_SSH_TUNNELS.items()):
        if old_key == current_key or not old_key.startswith(f"{proxy.name}\x1f"):
            continue
        _SSH_TUNNELS.pop(old_key, None)
        await _terminate_process(tunnel.process)


def _ssh_tunnel_command(
    ssh_bin: str,
    proxy: ProviderProxyDefinition,
    *,
    local_port: int,
    known_hosts_path: str,
) -> list[str]:
    target = f"{proxy.username}@{proxy.host}" if proxy.username else proxy.host
    command = [
        ssh_bin,
        "-N",
        "-D",
        f"127.0.0.1:{local_port}",
        "-p",
        str(proxy.port),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        f"GlobalKnownHostsFile={os.devnull}",
        "-o",
        "UpdateHostkeys=no",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if proxy.password:
        command.extend(
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
        command.extend(
            [
                "-o",
                "BatchMode=yes",
                "-o",
                "PasswordAuthentication=no",
            ]
        )
    if proxy.private_key_path:
        command.extend(["-i", proxy.private_key_path])
    command.extend(["--", target])
    return command


def _ssh_password_command(
    command: list[str],
    proxy: ProviderProxyDefinition,
) -> tuple[list[str], dict[str, str] | None, str | None, str | None]:
    if not proxy.password:
        return command, None, None, None
    password_file = _write_secret_file(proxy.password)
    sshpass_bin = shutil.which("sshpass")
    if sshpass_bin:
        return (
            [sshpass_bin, "-f", password_file, *command],
            os.environ.copy(),
            None,
            password_file,
        )
    askpass_path = _write_ssh_askpass_helper()
    env = os.environ.copy()
    env["SSH_ASKPASS"] = askpass_path
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.setdefault("DISPLAY", "localhost:0")
    env["LUMEN_SSH_PASSWORD_FILE"] = password_file
    return command, env, askpass_path, password_file


async def _wait_for_ssh_tunnel(
    proc: asyncio.subprocess.Process,
    local_port: int,
) -> tuple[str | None, str]:
    for _ in range(_SSH_TUNNEL_READY_CHECKS):
        if proc.returncode is not None:
            stderr = await _read_process_stderr(proc)
            return None, f"exited with {proc.returncode}: {stderr}".strip()
        if await _local_port_accepts(local_port):
            return f"socks5h://127.0.0.1:{local_port}", ""
        await asyncio.sleep(0.1)
    stderr = await _read_process_stderr(proc)
    return None, f"timed out waiting for local SOCKS port: {stderr}".strip()


async def _start_ssh_tunnel_attempt(
    proxy: ProviderProxyDefinition,
    *,
    ssh_bin: str,
    key: str,
) -> tuple[str | None, str]:
    (
        known_hosts_path,
        temporary_known_hosts_path,
    ) = await _prepare_ssh_host_key_verification(proxy)
    proc: asyncio.subprocess.Process | None = None
    tunnel_started = False
    askpass_path = None
    password_file = None
    try:
        local_port = _free_local_port()
        command = _ssh_tunnel_command(
            ssh_bin,
            proxy,
            local_port=local_port,
            known_hosts_path=known_hosts_path,
        )
        command, env, askpass_path, password_file = _ssh_password_command(
            command,
            proxy,
        )
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        url, error = await _wait_for_ssh_tunnel(proc, local_port)
        if url is not None:
            _SSH_TUNNELS[key] = _SshTunnel(url=url, process=proc)
            tunnel_started = True
        return url, error
    finally:
        if proc is not None and not tunnel_started:
            await _terminate_process(proc)
        _unlink_quietly(askpass_path)
        _unlink_quietly(password_file)
        _unlink_quietly(temporary_known_hosts_path)


async def _ensure_ssh_socks_proxy(proxy: ProviderProxyDefinition) -> str:
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        raise RuntimeError("ssh binary not found; cannot start ssh proxy")
    key = _ssh_tunnel_key(proxy)
    existing = _running_ssh_tunnel(key)
    if existing is not None:
        return existing.url

    async with _SSH_TUNNEL_LOCK:
        existing = _running_ssh_tunnel(key)
        if existing is not None:
            return existing.url
        await _close_stale_ssh_tunnels(proxy, key)

        last_error = ""
        for _attempt in range(_SSH_TUNNEL_START_ATTEMPTS):
            url, last_error = await _start_ssh_tunnel_attempt(
                proxy,
                ssh_bin=ssh_bin,
                key=key,
            )
            if url is not None:
                return url

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
    "DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT",
    "IMAGE_EDIT_INPUT_TRANSPORT_VALUES",
    "advance_round_robin_counter",
    "build_effective_provider_config",
    "build_effective_providers",
    "build_legacy_provider",
    "close_provider_proxy_tunnels",
    "endpoint_kind_allowed",
    "has_embedding_purpose",
    "parse_provider_item",
    "parse_provider_config_json",
    "parse_provider_json",
    "parse_provider_bool",
    "parse_proxy_item",
    "parse_proxy_json",
    "normalize_image_edit_input_transport",
    "resolve_provider_proxy_url",
    "socks_proxy_url",
    "weighted_priority_order",
    "weighted_priority_order_and_advance",
]
