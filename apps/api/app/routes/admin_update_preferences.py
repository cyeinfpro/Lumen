"""Runtime update preferences and proxy selection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from lumen_core.providers import ProviderProxyDefinition


async def resolve_update_proxy(
    db,
    *,
    get_spec: Callable[[str], object | None],
    get_setting: Callable[[object, object], Awaitable[object]],
    load_proxies: Callable[[object], Awaitable[list[ProviderProxyDefinition]]],
    resolve_proxy_url: Callable[
        [ProviderProxyDefinition],
        Awaitable[str | None],
    ],
    http_error: Callable[[str, str, int], Exception],
) -> tuple[ProviderProxyDefinition | None, str | None]:
    use_spec = get_spec("update.use_proxy_pool")
    name_spec = get_spec("update.proxy_name")
    use_raw = await get_setting(db, use_spec) if use_spec is not None else None
    if str(use_raw or "0").strip() != "1":
        return None, None

    proxies = [proxy for proxy in await load_proxies(db) if proxy.enabled]
    if not proxies:
        raise http_error(
            "proxy_unavailable",
            "update proxy pool is enabled but has no enabled proxies",
            409,
        )
    name_raw = await get_setting(db, name_spec) if name_spec is not None else None
    target_name = str(name_raw or "").strip()
    if target_name:
        proxy = next((item for item in proxies if item.name == target_name), None)
        if proxy is None:
            raise http_error(
                "proxy_not_found",
                f"update proxy '{target_name}' not found or disabled",
                409,
            )
    else:
        proxy = proxies[0]
    proxy_url = await resolve_proxy_url(proxy)
    if not proxy_url:
        raise http_error(
            "proxy_resolve_failed",
            f"update proxy '{proxy.name}' could not be resolved",
            409,
        )
    return proxy, proxy_url


async def update_channel(
    db,
    *,
    get_spec: Callable[[str], object | None],
    get_setting: Callable[[object, object], Awaitable[Any]],
) -> str:
    spec = get_spec("update.channel")
    if spec is None:
        return "stable"
    raw = await get_setting(db, spec)
    value = (raw or "stable").strip().lower()
    return (
        value if value in {"stable", "main", "pinned", "minor", "major"} else "stable"
    )


async def update_check_ttl(
    db,
    *,
    get_spec: Callable[[str], object | None],
    get_setting: Callable[[object, object], Awaitable[Any]],
) -> int:
    spec = get_spec("update.check_ttl_sec")
    if spec is None:
        return 1200
    raw = await get_setting(db, spec)
    try:
        return max(0, int(raw)) if raw is not None else 1200
    except ValueError:
        return 1200


async def update_allow_prerelease(
    db,
    *,
    get_spec: Callable[[str], object | None],
    get_setting: Callable[[object, object], Awaitable[Any]],
) -> bool:
    spec = get_spec("update.allow_prerelease")
    if spec is None:
        return False
    raw = await get_setting(db, spec)
    return str(raw or "0").strip() in {"1", "true", "yes", "on"}
