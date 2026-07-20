"""Late-bound access to the video generation compatibility facade."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class VideoGenerationFacade:
    """Resolve historical task globals at call time.

    Video generation has a broad private compatibility surface used by tests
    and sibling worker modules. The late-bound resolver keeps those monkeypatch
    points effective after the implementation is split into focused modules.
    """

    def __init__(self) -> None:
        self._resolver: Callable[[], Mapping[str, Any]] | None = None

    def bind(self, resolver: Callable[[], Mapping[str, Any]]) -> None:
        self._resolver = resolver

    def __getattr__(self, name: str) -> Any:
        resolver = self._resolver
        if resolver is None:
            raise RuntimeError("video generation facade is not bound")
        namespace = resolver()
        try:
            return namespace[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


_g = VideoGenerationFacade()
bind_video_generation_facade = _g.bind

__all__ = ["VideoGenerationFacade", "_g", "bind_video_generation_facade"]
