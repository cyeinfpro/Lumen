"""Optional callbacks used by provider probes.

ProviderPool must not import the high-level upstream facade.  The facade
registers the image probe callback at import time; the lazy fallback keeps
standalone cron/test imports compatible with older startup ordering.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable


ImageProbe = Callable[..., Awaitable[tuple[str, str | None]]]
_image_probe: ImageProbe | None = None


def set_image_probe(callback: ImageProbe) -> None:
    global _image_probe
    _image_probe = callback


def get_image_probe() -> ImageProbe:
    callback = _image_probe
    if callback is not None:
        return callback
    upstream = importlib.import_module("app.upstream")
    callback = getattr(upstream, "_responses_image_stream")
    if not callable(callback):
        raise RuntimeError("upstream image probe callback is not callable")
    return callback


__all__ = ["get_image_probe", "set_image_probe"]
