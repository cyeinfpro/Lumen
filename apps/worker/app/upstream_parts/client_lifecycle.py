"""HTTP client construction, caching, retirement, and shutdown lifecycle."""

from __future__ import annotations

from ..provider_runtime.upstream_services import upstream_services

import asyncio
import math
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx


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
        return upstream_services().lifecycle.TrackedStreamContext(
            self, super().stream(*args, **kwargs)
        )


async def _resolve_timeout_config() -> _TimeoutConfig:

    async def _resolve_float(spec_key: str, fallback: float) -> float:
        try:
            raw = await upstream_services().infrastructure.resolve(spec_key)
        except Exception as exc:  # noqa: BLE001
            upstream_services().infrastructure.logger.debug(
                "runtime timeout setting fallback key=%s err=%s", spec_key, exc
            )
            return fallback
        if raw is None:
            return fallback
        try:
            value = float(raw)
        except (TypeError, ValueError):
            upstream_services().infrastructure.logger.warning(
                "invalid runtime timeout setting key=%s value=%r", spec_key, raw
            )
            return fallback
        if not math.isfinite(value) or value <= 0:
            upstream_services().infrastructure.logger.warning(
                "invalid runtime timeout setting key=%s value=%r", spec_key, raw
            )
            return fallback
        return value

    settings = upstream_services().infrastructure.settings
    return upstream_services().lifecycle.TimeoutConfig(
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
    settings = upstream_services().infrastructure.settings
    timeout_config = timeout_config or upstream_services().lifecycle.TimeoutConfig(
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
        client_kwargs["transport"] = (
            upstream_services().infrastructure.pinned_async_http_transport(
                pinned_target
            )
        )
    return upstream_services().lifecycle.TrackedAsyncClient(
        **client_kwargs,
    )


def _build_images_client(
    timeout_config: _TimeoutConfig | None = None,
    *,
    proxy_url: str | None = None,
    pinned_target: Any | None = None,
) -> httpx.AsyncClient:
    """Build the Images API client without a default content-type header."""
    settings = upstream_services().infrastructure.settings
    timeout_config = timeout_config or upstream_services().lifecycle.TimeoutConfig(
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
        client_kwargs["transport"] = (
            upstream_services().infrastructure.pinned_async_http_transport(
                pinned_target
            )
        )
    return upstream_services().lifecycle.TrackedAsyncClient(**client_kwargs)


def _cache_proxied_client(
    cache: OrderedDict[tuple[_TimeoutConfig, str], httpx.AsyncClient],
    key: tuple[_TimeoutConfig, str],
    client: httpx.AsyncClient,
) -> list[httpx.AsyncClient]:
    cache[key] = client
    cache.move_to_end(key)
    evicted: list[httpx.AsyncClient] = []
    while len(cache) > upstream_services().core.PROXIED_CLIENT_CACHE_MAX:
        _old_key, old_client = cache.popitem(last=False)
        evicted.append(old_client)
    return evicted


async def _delayed_aclose(
    client: httpx.AsyncClient, *, delay: float | None = None
) -> None:
    try:
        await asyncio.sleep(
            upstream_services().core.PROXIED_CLIENT_CLOSE_DELAY_SECONDS
            if delay is None
            else delay
        )
        wait_until_idle = getattr(client, "_wait_until_idle", None)
        if callable(wait_until_idle):
            try:
                await wait_until_idle(
                    upstream_services().core.PROXIED_CLIENT_IDLE_CLOSE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                upstream_services().infrastructure.logger.warning(
                    "timed out waiting for retired upstream client to idle"
                )
        await upstream_services().lifecycle.aclose_client_cancel_safe(client)
    except Exception:  # noqa: BLE001
        upstream_services().infrastructure.logger.warning(
            "delayed proxied client close failed", exc_info=True
        )


async def _aclose_client_cancel_safe(client: httpx.AsyncClient) -> None:
    close_task = asyncio.create_task(client.aclose())
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        with suppress(Exception, asyncio.CancelledError):
            await close_task
        raise


def _schedule_delayed_aclose(client: httpx.AsyncClient) -> asyncio.Task[None]:
    upstream_services().core.retired_clients.add(client)
    task = asyncio.create_task(upstream_services().lifecycle.delayed_aclose(client))
    upstream_services().core.retired_client_close_tasks.add(task)

    def _discard_retired_client(done: asyncio.Task[None]) -> None:
        upstream_services().core.retired_client_close_tasks.discard(done)
        upstream_services().core.retired_clients.discard(client)

    task.add_done_callback(_discard_retired_client)
    return task


async def _close_retired_clients_now() -> None:
    tasks = list(upstream_services().core.retired_client_close_tasks)
    clients = list(upstream_services().core.retired_clients)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    upstream_services().core.retired_client_close_tasks.difference_update(tasks)
    upstream_services().core.retired_clients.difference_update(clients)
    for client in clients:
        await upstream_services().lifecycle.aclose_client_cancel_safe(client)


async def _get_client(
    proxy_url: str | None = None,
    *,
    pinned_target: Any | None = None,
) -> httpx.AsyncClient:
    timeout_config = await upstream_services().lifecycle.resolve_timeout_config()
    if proxy_url is not None and pinned_target is not None:
        raise ValueError("proxy and pinned target are mutually exclusive")
    if proxy_url:
        key = (timeout_config, proxy_url)
        evicted: list[httpx.AsyncClient] = []
        async with upstream_services().core.client_lock:
            client = upstream_services().core.proxied_clients.get(key)
            if client is not None:
                upstream_services().core.proxied_clients.move_to_end(key)
                return client
            client = upstream_services().lifecycle.build_client(
                timeout_config, proxy_url=proxy_url
            )
            evicted = upstream_services().lifecycle.cache_proxied_client(
                upstream_services().core.proxied_clients,
                key,
                client,
            )
        for evicted_client in evicted:
            upstream_services().lifecycle.schedule_delayed_aclose(evicted_client)
        return client
    if pinned_target is not None:
        key = _pinned_client_key(timeout_config, pinned_target)
        evicted = []
        async with upstream_services().core.client_lock:
            client = _pinned_clients.get(key)
            if client is not None:
                _pinned_clients.move_to_end(key)
                return client
            client = upstream_services().lifecycle.build_client(
                timeout_config,
                pinned_target=pinned_target,
            )
            evicted = upstream_services().lifecycle.cache_proxied_client(
                _pinned_clients,
                key,
                client,
            )
        for evicted_client in evicted:
            upstream_services().lifecycle.schedule_delayed_aclose(evicted_client)
        return client
    if (
        upstream_services().core.client is None
        or upstream_services().core.client_timeout_config != timeout_config
    ):
        async with upstream_services().core.client_lock:
            if (
                upstream_services().core.client is None
                or upstream_services().core.client_timeout_config != timeout_config
            ):
                retired_client = upstream_services().core.client
                upstream_services().core.client = (
                    upstream_services().lifecycle.build_client(timeout_config)
                )
                upstream_services().core.client_timeout_config = timeout_config
                if retired_client is not None:
                    upstream_services().lifecycle.schedule_delayed_aclose(
                        retired_client
                    )
    shared_client = upstream_services().core.client
    assert shared_client is not None
    return shared_client


async def _get_images_client(
    proxy_url: str | None = None,
    *,
    pinned_target: Any | None = None,
) -> httpx.AsyncClient:
    timeout_config = await upstream_services().lifecycle.resolve_timeout_config()
    if proxy_url is not None and pinned_target is not None:
        raise ValueError("proxy and pinned target are mutually exclusive")
    if proxy_url:
        key = (timeout_config, proxy_url)
        evicted: list[httpx.AsyncClient] = []
        async with upstream_services().core.images_client_lock:
            client = upstream_services().core.proxied_images_clients.get(key)
            if client is not None:
                upstream_services().core.proxied_images_clients.move_to_end(key)
                return client
            client = upstream_services().lifecycle.build_images_client(
                timeout_config,
                proxy_url=proxy_url,
            )
            evicted = upstream_services().lifecycle.cache_proxied_client(
                upstream_services().core.proxied_images_clients,
                key,
                client,
            )
        for evicted_client in evicted:
            upstream_services().lifecycle.schedule_delayed_aclose(evicted_client)
        return client
    if pinned_target is not None:
        key = _pinned_client_key(timeout_config, pinned_target)
        evicted = []
        async with upstream_services().core.images_client_lock:
            client = _pinned_images_clients.get(key)
            if client is not None:
                _pinned_images_clients.move_to_end(key)
                return client
            client = upstream_services().lifecycle.build_images_client(
                timeout_config,
                pinned_target=pinned_target,
            )
            evicted = upstream_services().lifecycle.cache_proxied_client(
                _pinned_images_clients,
                key,
                client,
            )
        for evicted_client in evicted:
            upstream_services().lifecycle.schedule_delayed_aclose(evicted_client)
        return client
    if (
        upstream_services().core.images_client is None
        or upstream_services().core.images_client_timeout_config != timeout_config
    ):
        async with upstream_services().core.images_client_lock:
            if (
                upstream_services().core.images_client is None
                or upstream_services().core.images_client_timeout_config
                != timeout_config
            ):
                retired_client = upstream_services().core.images_client
                upstream_services().core.images_client = (
                    upstream_services().lifecycle.build_images_client(timeout_config)
                )
                upstream_services().core.images_client_timeout_config = timeout_config
                if retired_client is not None:
                    upstream_services().lifecycle.schedule_delayed_aclose(
                        retired_client
                    )
    shared_client = upstream_services().core.images_client
    assert shared_client is not None
    return shared_client


async def close_client() -> None:
    """Close shared, proxied, retired, and provider-proxy resources."""
    await upstream_services().lifecycle.close_retired_clients_now()

    async with upstream_services().core.client_lock:
        clients: list[httpx.AsyncClient] = []
        if upstream_services().core.client is not None:
            clients.append(upstream_services().core.client)
            upstream_services().core.client = None
            upstream_services().core.client_timeout_config = None
        clients.extend(upstream_services().core.proxied_clients.values())
        upstream_services().core.proxied_clients.clear()
        clients.extend(_pinned_clients.values())
        _pinned_clients.clear()
    for client in clients:
        await upstream_services().lifecycle.aclose_client_cancel_safe(client)

    async with upstream_services().core.images_client_lock:
        image_clients: list[httpx.AsyncClient] = []
        if upstream_services().core.images_client is not None:
            image_clients.append(upstream_services().core.images_client)
            upstream_services().core.images_client = None
            upstream_services().core.images_client_timeout_config = None
        image_clients.extend(upstream_services().core.proxied_images_clients.values())
        upstream_services().core.proxied_images_clients.clear()
        image_clients.extend(_pinned_images_clients.values())
        _pinned_images_clients.clear()
    for client in image_clients:
        await upstream_services().lifecycle.aclose_client_cancel_safe(client)
    await upstream_services().infrastructure.close_provider_proxy_tunnels()


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
