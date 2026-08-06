from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from lumen_core.providers_parts.definitions import ProviderProxyDefinition

from ..proxy_pool import ProxyStateUnavailable


ProxyPicker = Callable[
    ...,
    Awaitable[ProviderProxyDefinition | None],
]


class RuntimeProxySelectionError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


async def select_runtime_proxy(
    redis: Any,
    candidates: list[ProviderProxyDefinition],
    *,
    strategy: str,
    avoid: Iterable[str],
    picker: ProxyPicker,
) -> ProviderProxyDefinition | None:
    try:
        picked = await picker(
            redis,
            candidates,
            strategy=strategy,
            avoid=avoid,
        )
    except ProxyStateUnavailable as exc:
        raise RuntimeProxySelectionError(
            "proxy_state_unavailable",
            "proxy cooldown state could not be verified",
            503,
        ) from exc
    if candidates and picked is None:
        raise RuntimeProxySelectionError(
            "proxy_pool_exhausted",
            "all configured proxies are cooling down",
            503,
        )
    return picked


__all__ = [
    "RuntimeProxySelectionError",
    "select_runtime_proxy",
]
