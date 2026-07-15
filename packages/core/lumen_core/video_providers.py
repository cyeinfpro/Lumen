"""Video provider configuration parsing.

Video generation intentionally has its own provider pool because Seedance/Veo
APIs are asynchronous task APIs, not OpenAI-compatible responses endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any
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
VIDEO_REFERENCE_MEDIA_LIMITS: dict[str, dict[str, int]] = {
    "volcano": {"image": 9, "video": 3},
    "volcano_third_party": {"image": 9, "video": 3},
    "volcano_newapi": {"image": 4, "video": 3, "audio": 1},
    "dashscope": {"image": 9},
    "omni_flash": {"image": 9},
    "fake": {"image": 9, "video": 3},
}
_VOLCANO_DOMESTIC_MODEL_ALIASES = {
    "dreamina-seedance-2-0-mini-260615": "doubao-seedance-2-0-mini-260615",
}


@dataclass(frozen=True)
class VideoProviderDefinition:
    name: str
    kind: str
    base_url: str
    api_key: str
    enabled: bool = True
    priority: int = 0
    weight: int = 1
    concurrency: int = 1
    supports_idempotency: bool = False
    models: dict[str, str] | None = None
    proxy_name: str | None = None
    proxy: ProviderProxyDefinition | None = None

    def upstream_model_for(self, model: str, action: str) -> str | None:
        mapping = self.models or {}
        return mapping.get(f"{model}:{action}") or mapping.get(model)

    def supports(self, model: str, action: str) -> bool:
        return (
            self.enabled
            and action in VIDEO_ACTIONS
            and self.upstream_model_for(model, action) is not None
        )


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


def parse_video_provider_config_json(
    raw: str | None,
    *,
    shared_provider_raw: str | None = None,
    allow_missing_proxy: bool = False,
) -> tuple[list[VideoProviderDefinition], list[ProviderProxyDefinition], list[str]]:
    provider_items, proxy_items, errors = _split_video_config(raw)
    if errors:
        return [], [], errors
    shared_proxies: list[ProviderProxyDefinition] = []
    if shared_provider_raw:
        _providers, shared_proxies, shared_errors = parse_provider_config_json(
            shared_provider_raw
        )
        errors.extend(f"shared providers: {err}" for err in shared_errors)
    proxies = list(shared_proxies)
    seen_proxy_names = {p.name for p in proxies}
    for i, item in enumerate(proxy_items):
        if not isinstance(item, dict):
            errors.append(f"video.providers.proxies[{i}] must be an object")
            continue
        try:
            parsed_proxy = parse_proxy_item(item, index=i)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"video.providers.proxies[{i}] invalid: {exc}")
            continue
        if parsed_proxy.name in seen_proxy_names:
            errors.append(
                f"video.providers.proxies[{i}].name is duplicated: {parsed_proxy.name}"
            )
            continue
        seen_proxy_names.add(parsed_proxy.name)
        proxies.append(parsed_proxy)

    providers: list[VideoProviderDefinition] = []
    provider_names: set[str] = set()
    for i, item in enumerate(provider_items):
        if not isinstance(item, dict):
            errors.append(f"video.providers[{i}] must be an object")
            continue
        try:
            provider = parse_video_provider_item(item, index=i)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"video.providers[{i}] invalid: {exc}")
            continue
        if provider.name in provider_names:
            errors.append(f"video.providers[{i}].name is duplicated: {provider.name}")
            continue
        provider_names.add(provider.name)
        providers.append(provider)

    proxy_by_name = {p.name: p for p in proxies}
    attached: list[VideoProviderDefinition] = []
    for provider in providers:
        attached_proxy: ProviderProxyDefinition | None = None
        if provider.proxy_name:
            attached_proxy = proxy_by_name.get(provider.proxy_name)
            if (
                attached_proxy is None
                and provider.enabled
                and not allow_missing_proxy
            ):
                errors.append(
                    f"provider {provider.name}: proxy {provider.proxy_name} not found"
                )
            elif attached_proxy is not None and not attached_proxy.enabled:
                if provider.enabled:
                    errors.append(
                        f"provider {provider.name}: proxy {provider.proxy_name} is disabled"
                    )
                attached_proxy = None
        attached.append(replace(provider, proxy=attached_proxy))
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
    "VIDEO_ACTIONS",
    "VIDEO_PROVIDER_KINDS",
    "VIDEO_REFERENCE_MEDIA_LIMITS",
    "VideoProviderDefinition",
    "ordered_video_providers",
    "parse_video_provider_config_json",
    "parse_video_provider_item",
    "select_video_provider",
    "validate_video_providers",
    "video_reference_media_limits",
]
