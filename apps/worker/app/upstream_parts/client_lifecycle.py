"""HTTP client construction, caching, retirement, and shutdown lifecycle."""

from __future__ import annotations

import asyncio
import importlib
import math
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

_UPSTREAM_MODULE_NAME = __name__.rsplit(".upstream_parts.", 1)[0] + ".upstream"


def _facade() -> Any:
    """Resolve the compatibility module at call time for monkeypatch visibility."""
    return importlib.import_module(_UPSTREAM_MODULE_NAME)


@dataclass(frozen=True)
class _TimeoutConfig:
    connect: float
    read: float
    write: float

    def to_httpx(self, *, read: float | None = None) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read if read is None else read,
            write=self.write,
            pool=self.connect,
        )


_pinned_clients: OrderedDict[
    tuple[_TimeoutConfig, str, tuple[str, ...]],
    httpx.AsyncClient,
] = OrderedDict()
_pinned_images_clients: OrderedDict[
    tuple[_TimeoutConfig, str, tuple[str, ...]],
    httpx.AsyncClient,
] = OrderedDict()


def _pinned_client_key(
    timeout_config: _TimeoutConfig,
    target: Any,
) -> tuple[_TimeoutConfig, str, tuple[str, ...]]:
    return (
        timeout_config,
        str(target.url),
        tuple(str(ip) for ip in target.resolved_ips),
    )


class _TrackedStreamContext:
    def __init__(self, client: "_TrackedAsyncClient", inner: Any) -> None:
        self._client = client
        self._inner = inner
        self._entered = False

    async def __aenter__(self) -> httpx.Response:
        self._client._acquire_request()
        try:
            response = await self._inner.__aenter__()
        except BaseException:
            self._client._release_request()
            raise
        self._entered = True
        return response

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self._inner.__aexit__(exc_type, exc, tb)
        finally:
            if self._entered:
                self._entered = False
                self._client._release_request()


class _TrackedAsyncClient(httpx.AsyncClient):
    """httpx client that can defer retirement until active calls drain."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._active_requests = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def _acquire_request(self) -> None:
        self._active_requests += 1
        self._idle.clear()

    def _release_request(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)
        if self._active_requests == 0:
            self._idle.set()

    async def _wait_until_idle(self, timeout: float) -> None:
        if self._active_requests == 0:
            return
        await asyncio.wait_for(self._idle.wait(), timeout=timeout)

    async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
        self._acquire_request()
        try:
            return await super().request(*args, **kwargs)
        finally:
            self._release_request()

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        facade = _facade()
        return facade._TrackedStreamContext(self, super().stream(*args, **kwargs))


async def _resolve_timeout_config() -> _TimeoutConfig:
    facade = _facade()

    async def _resolve_float(spec_key: str, fallback: float) -> float:
        try:
            raw = await facade.resolve(spec_key)
        except Exception as exc:  # noqa: BLE001
            facade.logger.debug(
                "runtime timeout setting fallback key=%s err=%s", spec_key, exc
            )
            return fallback
        if raw is None:
            return fallback
        try:
            value = float(raw)
        except (TypeError, ValueError):
            facade.logger.warning(
                "invalid runtime timeout setting key=%s value=%r", spec_key, raw
            )
            return fallback
        if not math.isfinite(value) or value <= 0:
            facade.logger.warning(
                "invalid runtime timeout setting key=%s value=%r", spec_key, raw
            )
            return fallback
        return value

    settings = facade.settings
    return facade._TimeoutConfig(
        connect=await _resolve_float(
            "upstream.connect_timeout_s", settings.upstream_connect_timeout_s
        ),
        read=await _resolve_float(
            "upstream.read_timeout_s", settings.upstream_read_timeout_s
        ),
        write=await _resolve_float(
            "upstream.write_timeout_s", settings.upstream_write_timeout_s
        ),
    )


def _build_client(
    timeout_config: _TimeoutConfig | None = None,
    *,
    proxy_url: str | None = None,
    pinned_target: Any | None = None,
) -> httpx.AsyncClient:
    """Build the shared JSON client without base URL or authorization state."""
    facade = _facade()
    settings = facade.settings
    timeout_config = timeout_config or facade._TimeoutConfig(
        connect=settings.upstream_connect_timeout_s,
        read=settings.upstream_read_timeout_s,
        write=settings.upstream_write_timeout_s,
    )
    if proxy_url is not None and pinned_target is not None:
        raise ValueError("proxy and pinned target are mutually exclusive")
    client_kwargs: dict[str, Any] = {
        "timeout": timeout_config.to_httpx(),
        "headers": {"content-type": "application/json"},
        "proxy": proxy_url,
        "follow_redirects": False,
        "trust_env": False,
    }
    if pinned_target is not None:
        client_kwargs["transport"] = facade.pinned_async_http_transport(pinned_target)
    return facade._TrackedAsyncClient(
        **client_kwargs,
    )


def _build_images_client(
    timeout_config: _TimeoutConfig | None = None,
    *,
    proxy_url: str | None = None,
    pinned_target: Any | None = None,
) -> httpx.AsyncClient:
    """Build the Images API client without a default content-type header."""
    facade = _facade()
    settings = facade.settings
    timeout_config = timeout_config or facade._TimeoutConfig(
        connect=settings.upstream_connect_timeout_s,
        read=settings.upstream_read_timeout_s,
        write=settings.upstream_write_timeout_s,
    )
    if proxy_url is not None and pinned_target is not None:
        raise ValueError("proxy and pinned target are mutually exclusive")
    client_kwargs: dict[str, Any] = {
        "timeout": timeout_config.to_httpx(),
        "proxy": proxy_url,
        "follow_redirects": False,
        "trust_env": False,
    }
    if pinned_target is not None:
        client_kwargs["transport"] = facade.pinned_async_http_transport(pinned_target)
    return facade._TrackedAsyncClient(**client_kwargs)


def _cache_proxied_client(
    cache: OrderedDict[tuple[_TimeoutConfig, str], httpx.AsyncClient],
    key: tuple[_TimeoutConfig, str],
    client: httpx.AsyncClient,
) -> list[httpx.AsyncClient]:
    facade = _facade()
    cache[key] = client
    cache.move_to_end(key)
    evicted: list[httpx.AsyncClient] = []
    while len(cache) > facade._PROXIED_CLIENT_CACHE_MAX:
        _old_key, old_client = cache.popitem(last=False)
        evicted.append(old_client)
    return evicted


async def _delayed_aclose(
    client: httpx.AsyncClient, *, delay: float | None = None
) -> None:
    facade = _facade()
    try:
        await asyncio.sleep(
            facade._PROXIED_CLIENT_CLOSE_DELAY_SECONDS if delay is None else delay
        )
        wait_until_idle = getattr(client, "_wait_until_idle", None)
        if callable(wait_until_idle):
            try:
                await wait_until_idle(facade._PROXIED_CLIENT_IDLE_CLOSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                facade.logger.warning(
                    "timed out waiting for retired upstream client to idle"
                )
        await facade._aclose_client_cancel_safe(client)
    except Exception:  # noqa: BLE001
        facade.logger.warning("delayed proxied client close failed", exc_info=True)


async def _aclose_client_cancel_safe(client: httpx.AsyncClient) -> None:
    close_task = asyncio.create_task(client.aclose())
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        with suppress(Exception, asyncio.CancelledError):
            await close_task
        raise


def _schedule_delayed_aclose(client: httpx.AsyncClient) -> asyncio.Task[None]:
    facade = _facade()
    facade._retired_clients.add(client)
    task = asyncio.create_task(facade._delayed_aclose(client))
    facade._retired_client_close_tasks.add(task)

    def _discard_retired_client(done: asyncio.Task[None]) -> None:
        facade._retired_client_close_tasks.discard(done)
        facade._retired_clients.discard(client)

    task.add_done_callback(_discard_retired_client)
    return task


async def _close_retired_clients_now() -> None:
    facade = _facade()
    tasks = list(facade._retired_client_close_tasks)
    clients = list(facade._retired_clients)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    facade._retired_client_close_tasks.difference_update(tasks)
    facade._retired_clients.difference_update(clients)
    for client in clients:
        await facade._aclose_client_cancel_safe(client)


async def _get_client(
    proxy_url: str | None = None,
    *,
    pinned_target: Any | None = None,
) -> httpx.AsyncClient:
    facade = _facade()
    timeout_config = await facade._resolve_timeout_config()
    if proxy_url is not None and pinned_target is not None:
        raise ValueError("proxy and pinned target are mutually exclusive")
    if proxy_url:
        key = (timeout_config, proxy_url)
        evicted: list[httpx.AsyncClient] = []
        async with facade._client_lock:
            client = facade._proxied_clients.get(key)
            if client is not None:
                facade._proxied_clients.move_to_end(key)
                return client
            client = facade._build_client(timeout_config, proxy_url=proxy_url)
            evicted = facade._cache_proxied_client(
                facade._proxied_clients,
                key,
                client,
            )
        for evicted_client in evicted:
            facade._schedule_delayed_aclose(evicted_client)
        return client
    if pinned_target is not None:
        key = _pinned_client_key(timeout_config, pinned_target)
        evicted = []
        async with facade._client_lock:
            client = _pinned_clients.get(key)
            if client is not None:
                _pinned_clients.move_to_end(key)
                return client
            client = facade._build_client(
                timeout_config,
                pinned_target=pinned_target,
            )
            evicted = facade._cache_proxied_client(
                _pinned_clients,
                key,
                client,
            )
        for evicted_client in evicted:
            facade._schedule_delayed_aclose(evicted_client)
        return client
    if facade._client is None or facade._client_timeout_config != timeout_config:
        async with facade._client_lock:
            if (
                facade._client is None
                or facade._client_timeout_config != timeout_config
            ):
                retired_client = facade._client
                facade._client = facade._build_client(timeout_config)
                facade._client_timeout_config = timeout_config
                if retired_client is not None:
                    facade._schedule_delayed_aclose(retired_client)
    shared_client = facade._client
    assert shared_client is not None
    return shared_client


async def _get_images_client(
    proxy_url: str | None = None,
    *,
    pinned_target: Any | None = None,
) -> httpx.AsyncClient:
    facade = _facade()
    timeout_config = await facade._resolve_timeout_config()
    if proxy_url is not None and pinned_target is not None:
        raise ValueError("proxy and pinned target are mutually exclusive")
    if proxy_url:
        key = (timeout_config, proxy_url)
        evicted: list[httpx.AsyncClient] = []
        async with facade._images_client_lock:
            client = facade._proxied_images_clients.get(key)
            if client is not None:
                facade._proxied_images_clients.move_to_end(key)
                return client
            client = facade._build_images_client(
                timeout_config,
                proxy_url=proxy_url,
            )
            evicted = facade._cache_proxied_client(
                facade._proxied_images_clients,
                key,
                client,
            )
        for evicted_client in evicted:
            facade._schedule_delayed_aclose(evicted_client)
        return client
    if pinned_target is not None:
        key = _pinned_client_key(timeout_config, pinned_target)
        evicted = []
        async with facade._images_client_lock:
            client = _pinned_images_clients.get(key)
            if client is not None:
                _pinned_images_clients.move_to_end(key)
                return client
            client = facade._build_images_client(
                timeout_config,
                pinned_target=pinned_target,
            )
            evicted = facade._cache_proxied_client(
                _pinned_images_clients,
                key,
                client,
            )
        for evicted_client in evicted:
            facade._schedule_delayed_aclose(evicted_client)
        return client
    if (
        facade._images_client is None
        or facade._images_client_timeout_config != timeout_config
    ):
        async with facade._images_client_lock:
            if (
                facade._images_client is None
                or facade._images_client_timeout_config != timeout_config
            ):
                retired_client = facade._images_client
                facade._images_client = facade._build_images_client(timeout_config)
                facade._images_client_timeout_config = timeout_config
                if retired_client is not None:
                    facade._schedule_delayed_aclose(retired_client)
    shared_client = facade._images_client
    assert shared_client is not None
    return shared_client


async def close_client() -> None:
    """Close shared, proxied, retired, and provider-proxy resources."""
    facade = _facade()
    await facade._close_retired_clients_now()

    async with facade._client_lock:
        clients: list[httpx.AsyncClient] = []
        if facade._client is not None:
            clients.append(facade._client)
            facade._client = None
            facade._client_timeout_config = None
        clients.extend(facade._proxied_clients.values())
        facade._proxied_clients.clear()
        clients.extend(_pinned_clients.values())
        _pinned_clients.clear()
    for client in clients:
        await facade._aclose_client_cancel_safe(client)

    async with facade._images_client_lock:
        image_clients: list[httpx.AsyncClient] = []
        if facade._images_client is not None:
            image_clients.append(facade._images_client)
            facade._images_client = None
            facade._images_client_timeout_config = None
        image_clients.extend(facade._proxied_images_clients.values())
        facade._proxied_images_clients.clear()
        image_clients.extend(_pinned_images_clients.values())
        _pinned_images_clients.clear()
    for client in image_clients:
        await facade._aclose_client_cancel_safe(client)
    await facade.close_provider_proxy_tunnels()


__all__ = [
    "_TimeoutConfig",
    "_TrackedAsyncClient",
    "_TrackedStreamContext",
    "_aclose_client_cancel_safe",
    "_build_client",
    "_build_images_client",
    "_cache_proxied_client",
    "_close_retired_clients_now",
    "_delayed_aclose",
    "_get_client",
    "_get_images_client",
    "_resolve_timeout_config",
    "_schedule_delayed_aclose",
    "close_client",
]
