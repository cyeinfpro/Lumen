"""Video provider configuration parsing.

Video generation intentionally has its own provider pool because Seedance/Veo
APIs are asynchronous task APIs, not OpenAI-compatible responses endpoints.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlsplit

from .providers import (
    ProviderProxyDefinition,
    parse_provider_bool,
    parse_provider_config_json,
    parse_proxy_item,
    weighted_priority_order,
)

VIDEO_PROVIDER_KINDS = (
    "volcano",
    "volcano_third_party",
    "volcano_newapi",
    "dashscope",
    "veo",
    "omni_flash",
    "fake",
)
VIDEO_ACTIONS = ("t2v", "i2v", "reference")
DEFAULT_VOLCANO_PROJECT_NAME = "default"
DEFAULT_VOLCANO_REGION = "cn-beijing"
SEEDANCE_20_MIN_DURATION_S = 4
SEEDANCE_20_MAX_DURATION_S = 15
SEEDANCE_20_SMART_DURATION_S = -1
SEEDANCE_20_STANDARD_RESOLUTIONS = ("480p", "720p", "1080p", "4k")
SEEDANCE_20_FAST_RESOLUTIONS = ("480p", "720p")
VIDEO_REFERENCE_MEDIA_LIMITS: dict[str, dict[str, int]] = {
    "volcano": {"image": 9, "video": 3, "audio": 3},
    "volcano_third_party": {"image": 9, "video": 3},
    "volcano_newapi": {"image": 4, "video": 3, "audio": 1},
    "dashscope": {"image": 9},
    "omni_flash": {"image": 9},
    "fake": {"image": 9, "video": 3},
}
_VOLCANO_DOMESTIC_MODEL_ALIASES = {
    "dreamina-seedance-2-0-mini-260615": "doubao-seedance-2-0-mini-260615",
}
Seedance20Variant = Literal["standard", "fast", "mini"]
_VOLCANO_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEEDANCE_20_VARIANT_PATTERNS: tuple[
    tuple[Seedance20Variant, re.Pattern[str]],
    ...,
] = (
    (
        "fast",
        re.compile(
            r"(?<![a-z0-9-])"
            r"(?:(?:(?:doubao|dreamina)-)?seedance-2-0-fast|video-ds-2-0-fast)"
            r"(?=$|-[0-9]{6}(?:$|[^a-z0-9-])|[^a-z0-9-])"
        ),
    ),
    (
        "mini",
        re.compile(
            r"(?<![a-z0-9-])"
            r"(?:(?:(?:doubao|dreamina)-)?seedance-2-0-mini|video-ds-2-0-mini)"
            r"(?=$|-[0-9]{6}(?:$|[^a-z0-9-])|[^a-z0-9-])"
        ),
    ),
    (
        "standard",
        re.compile(
            r"(?<![a-z0-9-])"
            r"(?:(?:(?:doubao|dreamina)-)?seedance-2-0|video-ds-2-0)"
            r"(?=$|-[0-9]{6}(?:$|[^a-z0-9-])|[^a-z0-9-])"
        ),
    ),
)


@dataclass(frozen=True)
class VideoProviderDefinition:
    name: str
    kind: str
    base_url: str
    api_key: str = field(repr=False)
    enabled: bool = True
    priority: int = 0
    weight: int = 1
    concurrency: int = 1
    supports_idempotency: bool = False
    models: dict[str, str] | None = None
    proxy_name: str | None = None
    proxy: ProviderProxyDefinition | None = None
    access_key_id: str = field(default="", repr=False)
    secret_access_key: str = field(default="", repr=False)
    project_name: str = DEFAULT_VOLCANO_PROJECT_NAME
    region: str = DEFAULT_VOLCANO_REGION

    def upstream_model_for(self, model: str, action: str) -> str | None:
        mapping = self.models or {}
        return mapping.get(f"{model}:{action}") or mapping.get(model)

    def supports(self, model: str, action: str) -> bool:
        return (
            self.enabled
            and action in VIDEO_ACTIONS
            and self.upstream_model_for(model, action) is not None
        )

    @property
    def asset_management_ready(self) -> bool:
        return (
            self.kind == "volcano"
            and bool(self.access_key_id)
            and bool(self.secret_access_key)
        )


def video_provider_binding_fingerprint(
    provider: VideoProviderDefinition,
) -> str:
    proxy = provider.proxy
    binding = {
        "name": provider.name,
        "kind": provider.kind,
        "base_url": provider.base_url,
        "api_key": provider.api_key,
        "access_key_id": provider.access_key_id,
        "secret_access_key": provider.secret_access_key,
        "project_name": provider.project_name,
        "region": provider.region,
        "supports_idempotency": provider.supports_idempotency,
        "models": provider.models or {},
        "proxy_name": provider.proxy_name,
        "proxy": (
            {
                "name": proxy.name,
                "protocol": proxy.protocol,
                "host": proxy.host,
                "port": proxy.port,
                "username": proxy.username,
                "password": proxy.password,
                "private_key_path": proxy.private_key_path,
                "enabled": proxy.enabled,
            }
            if proxy is not None
            else None
        ),
    }
    canonical = json.dumps(
        binding,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"lumen-video-provider-binding-v1\0" + canonical).hexdigest()


def _parse_int(raw: Any, *, default: int, minimum: int, maximum: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be an integer") from exc
    return max(minimum, min(value, maximum))


def _parse_weight(raw: Any) -> int:
    return _parse_int(raw, default=1, minimum=1, maximum=1000)


def _parse_priority(raw: Any) -> int:
    if raw in (None, ""):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("priority must be an integer") from exc


def _parse_optional_string(
    raw: Any,
    *,
    field: str,
    default: str = "",
    maximum: int | None = None,
) -> str:
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    value = raw.strip()
    if not value:
        return default
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field} must not exceed {maximum} characters")
    return value


def _normalize_base_url(raw: Any, *, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} is required")
    value = raw.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"{field} must use http or https")
    if not parts.hostname:
        raise ValueError(f"{field} must include a hostname")
    if parts.username or parts.password:
        raise ValueError(f"{field} must not include credentials")
    return value


def _parse_models(raw: Any, *, provider_name: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"provider {provider_name}: models must be a non-empty object")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"provider {provider_name}: model key must be non-empty")
        if ":" in key:
            model, action = key.rsplit(":", 1)
            if action not in VIDEO_ACTIONS:
                raise ValueError(
                    f"provider {provider_name}: model action must be one of {', '.join(VIDEO_ACTIONS)}"
                )
            if not model.strip():
                raise ValueError(f"provider {provider_name}: model key is invalid")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"provider {provider_name}: upstream model id must be non-empty"
            )
        result[key.strip()] = value.strip()
    return result


def _normalize_volcano_models(models: dict[str, str], *, kind: str) -> dict[str, str]:
    if kind != "volcano":
        return models
    return {
        key: _VOLCANO_DOMESTIC_MODEL_ALIASES.get(value, value)
        for key, value in models.items()
    }


def seedance_20_variant(*identifiers: str | None) -> Seedance20Variant | None:
    normalized = [
        re.sub(
            r"-+", "-", identifier.strip().lower().replace("_", "-").replace(".", "-")
        )
        for identifier in identifiers
        if isinstance(identifier, str) and identifier.strip()
    ]
    for variant, pattern in _SEEDANCE_20_VARIANT_PATTERNS:
        if any(pattern.search(value) is not None for value in normalized):
            return variant
    return None


def seedance_20_allowed_resolutions(
    *identifiers: str | None,
) -> tuple[str, ...] | None:
    variant = seedance_20_variant(*identifiers)
    if variant is None:
        return None
    if variant in {"fast", "mini"}:
        return SEEDANCE_20_FAST_RESOLUTIONS
    return SEEDANCE_20_STANDARD_RESOLUTIONS


def seedance_20_duration_is_valid(
    duration_s: int,
    *identifiers: str | None,
) -> bool:
    if seedance_20_variant(*identifiers) is None:
        return True
    return duration_s == SEEDANCE_20_SMART_DURATION_S or (
        SEEDANCE_20_MIN_DURATION_S <= duration_s <= SEEDANCE_20_MAX_DURATION_S
    )


def parse_video_provider_item(
    item: dict[str, Any],
    *,
    index: int,
) -> VideoProviderDefinition:
    raw_name = item.get("name")
    name = (
        raw_name.strip()
        if isinstance(raw_name, str) and raw_name.strip()
        else f"video-provider-{index}"
    )
    raw_kind = item.get("kind", "volcano")
    kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
    if kind not in VIDEO_PROVIDER_KINDS:
        raise ValueError(
            f"provider {name}: kind must be one of {', '.join(VIDEO_PROVIDER_KINDS)}"
        )
    enabled = parse_provider_bool(item.get("enabled"), default=True)
    api_key = item.get("api_key", "")
    if not isinstance(api_key, str):
        raise ValueError(f"provider {name}: api_key must be a string")
    api_key = api_key.strip()
    if enabled and kind != "fake" and not api_key:
        raise ValueError(f"provider {name}: api_key is required")
    access_key_id = ""
    secret_access_key = ""
    project_name = DEFAULT_VOLCANO_PROJECT_NAME
    region = DEFAULT_VOLCANO_REGION
    if kind == "volcano":
        access_key_id = _parse_optional_string(
            item.get("access_key_id"),
            field=f"provider {name}.access_key_id",
            maximum=256,
        )
        secret_access_key = _parse_optional_string(
            item.get("secret_access_key"),
            field=f"provider {name}.secret_access_key",
            maximum=256,
        )
        project_name = _parse_optional_string(
            item.get("project_name"),
            field=f"provider {name}.project_name",
            default=DEFAULT_VOLCANO_PROJECT_NAME,
            maximum=128,
        )
        region = _parse_optional_string(
            item.get("region"),
            field=f"provider {name}.region",
            default=DEFAULT_VOLCANO_REGION,
            maximum=64,
        )
        if not _VOLCANO_REGION_RE.fullmatch(region):
            raise ValueError(
                f"provider {name}.region must use lowercase letters, digits, and hyphens"
            )
    proxy_name = item.get("proxy", item.get("proxy_name"))
    models = _normalize_volcano_models(
        _parse_models(item.get("models"), provider_name=name),
        kind=kind,
    )
    return VideoProviderDefinition(
        name=name,
        kind=kind,
        base_url=_normalize_base_url(
            item.get("base_url"), field=f"provider {name}.base_url"
        ),
        api_key=api_key,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        project_name=project_name,
        region=region,
        enabled=enabled,
        priority=_parse_priority(item.get("priority", 0)),
        weight=_parse_weight(item.get("weight", 1)),
        concurrency=_parse_int(
            item.get("concurrency", 1), default=1, minimum=1, maximum=32
        ),
        supports_idempotency=parse_provider_bool(
            item.get("supports_idempotency"),
            default=False,
        ),
        models=models,
        proxy_name=proxy_name.strip()
        if isinstance(proxy_name, str) and proxy_name.strip()
        else None,
    )


def _split_video_config(raw: str | None) -> tuple[list[Any], list[Any], list[str]]:
    if not raw:
        return [], [], []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [], [f"video.providers JSON parse failed: {exc}"]
    if isinstance(parsed, list):
        return parsed, [], []
    if not isinstance(parsed, dict):
        return [], [], ["video.providers must be a JSON array or object"]
    providers = parsed.get("providers")
    proxies = parsed.get("proxies", [])
    if not isinstance(providers, list):
        return [], [], ["video.providers.providers must be a JSON array"]
    if proxies is None:
        proxies = []
    if not isinstance(proxies, list):
        return providers, [], ["video.providers.proxies must be a JSON array"]
    return providers, proxies, []


def _shared_video_proxies(
    shared_provider_raw: str | None,
    errors: list[str],
) -> list[ProviderProxyDefinition]:
    if not shared_provider_raw:
        return []
    _providers, shared_proxies, shared_errors = parse_provider_config_json(
        shared_provider_raw
    )
    errors.extend(f"shared providers: {error}" for error in shared_errors)
    return shared_proxies


def _video_proxy_items(
    items: list[Any],
    shared_proxies: list[ProviderProxyDefinition],
    errors: list[str],
) -> list[ProviderProxyDefinition]:
    proxies = list(shared_proxies)
    seen_names = {proxy.name for proxy in proxies}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"video.providers.proxies[{index}] must be an object")
            continue
        try:
            proxy = parse_proxy_item(item, index=index)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"video.providers.proxies[{index}] invalid: {exc}")
            continue
        if proxy.name in seen_names:
            errors.append(
                f"video.providers.proxies[{index}].name is duplicated: {proxy.name}"
            )
            continue
        seen_names.add(proxy.name)
        proxies.append(proxy)
    return proxies


def _video_provider_items(
    items: list[Any],
    errors: list[str],
) -> list[VideoProviderDefinition]:
    providers: list[VideoProviderDefinition] = []
    seen_names: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"video.providers[{index}] must be an object")
            continue
        try:
            provider = parse_video_provider_item(item, index=index)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"video.providers[{index}] invalid: {exc}")
            continue
        if provider.name in seen_names:
            errors.append(
                f"video.providers[{index}].name is duplicated: {provider.name}"
            )
            continue
        seen_names.add(provider.name)
        providers.append(provider)
    return providers


def _attached_video_providers(
    providers: list[VideoProviderDefinition],
    proxies: list[ProviderProxyDefinition],
    errors: list[str],
    *,
    allow_missing_proxy: bool,
) -> list[VideoProviderDefinition]:
    proxy_by_name = {proxy.name: proxy for proxy in proxies}
    attached: list[VideoProviderDefinition] = []
    for provider in providers:
        proxy = proxy_by_name.get(provider.proxy_name) if provider.proxy_name else None
        if provider.proxy_name and proxy is None:
            if provider.enabled and not allow_missing_proxy:
                errors.append(
                    f"provider {provider.name}: proxy {provider.proxy_name} not found"
                )
        elif proxy is not None and not proxy.enabled:
            if provider.enabled:
                errors.append(
                    f"provider {provider.name}: proxy {provider.proxy_name} is disabled"
                )
            proxy = None
        attached.append(replace(provider, proxy=proxy))
    return attached


def parse_video_provider_config_json(
    raw: str | None,
    *,
    shared_provider_raw: str | None = None,
    allow_missing_proxy: bool = False,
) -> tuple[list[VideoProviderDefinition], list[ProviderProxyDefinition], list[str]]:
    provider_items, proxy_items, errors = _split_video_config(raw)
    if errors:
        return [], [], errors
    shared_proxies = _shared_video_proxies(shared_provider_raw, errors)
    proxies = _video_proxy_items(proxy_items, shared_proxies, errors)
    providers = _video_provider_items(provider_items, errors)
    attached = _attached_video_providers(
        providers,
        proxies,
        errors,
        allow_missing_proxy=allow_missing_proxy,
    )
    return attached, proxies, errors


def validate_video_providers(
    raw: str,
    *,
    shared_provider_raw: str | None = None,
    allow_missing_proxy: bool = False,
) -> str:
    providers, _proxies, errors = parse_video_provider_config_json(
        raw,
        shared_provider_raw=shared_provider_raw,
        allow_missing_proxy=allow_missing_proxy,
    )
    if errors:
        raise ValueError("; ".join(errors))
    if not providers:
        raise ValueError("video.providers must include at least one provider")
    return raw.strip()


def ordered_video_providers(
    providers: list[VideoProviderDefinition],
) -> list[VideoProviderDefinition]:
    return weighted_priority_order(providers)


def select_video_provider(
    providers: list[VideoProviderDefinition],
    *,
    model: str,
    action: str,
) -> VideoProviderDefinition | None:
    for provider in ordered_video_providers(providers):
        if provider.supports(model, action):
            return provider
    return None


def video_reference_media_limits(provider_kind: str) -> dict[str, int]:
    return dict(VIDEO_REFERENCE_MEDIA_LIMITS.get(provider_kind, {}))


__all__ = [
    "DEFAULT_VOLCANO_PROJECT_NAME",
    "DEFAULT_VOLCANO_REGION",
    "SEEDANCE_20_FAST_RESOLUTIONS",
    "SEEDANCE_20_MAX_DURATION_S",
    "SEEDANCE_20_MIN_DURATION_S",
    "SEEDANCE_20_SMART_DURATION_S",
    "SEEDANCE_20_STANDARD_RESOLUTIONS",
    "VIDEO_ACTIONS",
    "VIDEO_PROVIDER_KINDS",
    "VIDEO_REFERENCE_MEDIA_LIMITS",
    "VideoProviderDefinition",
    "ordered_video_providers",
    "parse_video_provider_config_json",
    "parse_video_provider_item",
    "seedance_20_allowed_resolutions",
    "seedance_20_duration_is_valid",
    "seedance_20_variant",
    "select_video_provider",
    "validate_video_providers",
    "video_provider_binding_fingerprint",
    "video_reference_media_limits",
]
