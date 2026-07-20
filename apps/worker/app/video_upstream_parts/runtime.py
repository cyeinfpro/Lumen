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


_runtime_factory: Callable[[], AdapterRuntime] | None = None


def set_runtime_factory(factory: Callable[[], AdapterRuntime]) -> None:
    global _runtime_factory
    _runtime_factory = factory


def current_runtime() -> AdapterRuntime:
    if _runtime_factory is None:
        raise RuntimeError("video upstream runtime factory is not initialized")
    return _runtime_factory()
