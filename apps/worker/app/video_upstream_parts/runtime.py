"""Runtime hooks injected by the public video upstream facade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class AdapterRuntime:
    """Dependencies whose facade-level monkeypatches must remain observable."""

    httpx: Any
    settings: Any
    socks_proxy_url: Callable[[Any], str]
    pinned_async_http_transport: Callable[[Any], Any]
    download_video_url: Callable[..., Awaitable[Any]]
    downloaded_video_bytes: Callable[..., Awaitable[bytes]]
    fetch_image_url_as_data_url: Callable[..., Awaitable[str]]
    image_data_url: Callable[..., str]
    seedance_content: Callable[..., list[dict[str, Any]]]


class AdapterRuntimePort:
    """Explicit owner for adapter runtime composition."""

    def __init__(self) -> None:
        self._factory: Callable[[], AdapterRuntime] | None = None

    def install(self, factory: Callable[[], AdapterRuntime]) -> None:
        if self._factory is not None and self._factory is not factory:
            raise RuntimeError("video adapter runtime factory is already installed")
        self._factory = factory

    def current(self) -> AdapterRuntime:
        if self._factory is None:
            raise RuntimeError("video upstream runtime factory is not initialized")
        return self._factory()


_ADAPTER_RUNTIME_PORT = AdapterRuntimePort()


def set_runtime_factory(factory: Callable[[], AdapterRuntime]) -> None:
    _ADAPTER_RUNTIME_PORT.install(factory)


def current_runtime() -> AdapterRuntime:
    return _ADAPTER_RUNTIME_PORT.current()


__all__ = [
    "AdapterRuntime",
    "AdapterRuntimePort",
    "current_runtime",
    "set_runtime_factory",
]
