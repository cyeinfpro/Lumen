"""Optional callbacks used by provider probes.

ProviderPool must not import the high-level upstream facade. The composition
root registers the image probe callback during worker startup.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar


ImageProbe = Callable[..., Awaitable[tuple[str, str | None]]]
_IMAGE_PROBE: ContextVar[ImageProbe | None] = ContextVar(
    "provider_image_probe",
    default=None,
)


def set_image_probe(callback: ImageProbe) -> None:
    _IMAGE_PROBE.set(callback)


def get_image_probe() -> ImageProbe:
    callback = _IMAGE_PROBE.get()
    if callback is not None:
        return callback
    raise RuntimeError("upstream image probe callback has not been registered")


__all__ = ["get_image_probe", "set_image_probe"]
