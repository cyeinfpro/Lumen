from __future__ import annotations

import json
from typing import Any, Callable

from lumen_core.schemas import ProviderProxyOut, VideoProviderItemOut

from .presentation import mask_key, mask_secret


def parse_video_raw_config(raw: str | None) -> tuple[list[dict], list[dict]]:
    if not raw:
        return [], []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [], []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)], []
    if not isinstance(value, dict):
        return [], []
    providers = value.get("providers", [])
    proxies = value.get("proxies", [])
    if not isinstance(providers, list):
        providers = []
    if not isinstance(proxies, list):
        proxies = []
    return (
        [item for item in providers if isinstance(item, dict)],
        [item for item in proxies if isinstance(item, dict)],
    )


def video_provider_out(provider: Any) -> VideoProviderItemOut:
    is_volcano = provider.kind == "volcano"
    return VideoProviderItemOut(
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key_hint=mask_key(provider.api_key),
        access_key_id_hint=(mask_key(provider.access_key_id) if is_volcano else None),
        secret_access_key_hint=(
            mask_secret(provider.secret_access_key) if is_volcano else None
        ),
        project_name=provider.project_name if is_volcano else None,
        region=provider.region if is_volcano else None,
        asset_management_ready=provider.asset_management_ready,
        enabled=provider.enabled,
        priority=provider.priority,
        weight=provider.weight,
        concurrency=provider.concurrency,
        supports_idempotency=provider.supports_idempotency,
        proxy=provider.proxy_name,
        models=dict(provider.models or {}),
    )


def video_proxy_options(
    raw_video: str | None,
    raw_shared: str | None,
    *,
    parse_shared: Callable[[str], tuple[list[dict], list[dict]]],
    present_proxy: Callable[[dict, int], ProviderProxyOut],
) -> list[ProviderProxyOut]:
    _shared_items, shared_proxies = parse_shared(raw_shared or "")
    _video_items, video_proxies = parse_video_raw_config(raw_video)
    items = [*shared_proxies, *video_proxies]
    output: list[ProviderProxyOut] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        proxy = present_proxy(item, index)
        if proxy.name in seen:
            continue
        seen.add(proxy.name)
        output.append(proxy)
    return output
