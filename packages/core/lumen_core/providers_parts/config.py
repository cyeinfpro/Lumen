from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

from ..immutables import immutable_mapping
from .definitions import (
    DEFAULT_IMAGE_EDIT_INPUT_TRANSPORT,
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    DEFAULT_PROVIDER_PURPOSES,
    IMAGE_EDIT_INPUT_TRANSPORT_VALUES,
    IMAGE_JOBS_ENDPOINT_VALUES,
    MAX_PROVIDER_WEIGHT,
    PROVIDER_PURPOSE_VALUES,
    SSH_HOST_KEY_FINGERPRINT_RE,
    ProviderDefinition,
    ProviderProxyDefinition,
)


_PROXY_PROTOCOL_ALIASES = immutable_mapping(
    {
        "s5": "socks5",
        "socks": "socks5",
        "socks5": "socks5",
        "socks5h": "socks5",
        "ssh": "ssh",
    }
)


def normalize_provider_purposes(raw: Any) -> tuple[str, ...]:
    """Parse provider purposes with backward-compatible defaults."""
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


def _parse_weight(raw: Any) -> int:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1
    if not math.isfinite(value):
        return 1
    return max(1, min(int(value), MAX_PROVIDER_WEIGHT))


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


def parse_optional_bool(raw: Any) -> bool | None:
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


def _parse_bool(raw: Any, *, default: bool, field: str) -> bool:
    if raw is None or raw == "":
        return default
    parsed = parse_optional_bool(raw)
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
    parsed_lock = parse_optional_bool(raw_lock)
    if raw_lock not in (None, "") and parsed_lock is None:
        raise ValueError("image_jobs_endpoint_lock must be a boolean")
    if parsed_lock and endpoint == "auto":
        raise ValueError(
            "image_jobs_endpoint_lock requires image_jobs_endpoint to be "
            "responses or generations"
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
        responses_supported=parse_optional_bool(item.get("responses_supported")),
        image_generations_supported=parse_optional_bool(
            item.get("image_generations_supported")
        ),
        image_responses_supported=parse_optional_bool(
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
    if host_key_fingerprint and not SSH_HOST_KEY_FINGERPRINT_RE.fullmatch(
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
                        f"provider {provider.name}: proxy "
                        f"{provider.proxy_name} is disabled"
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
    """Build the compatibility provider from legacy environment variables."""
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
