"""API compatibility exports for shared Volcano asset media helpers."""

from __future__ import annotations

from lumen_core import volcano_asset_media as _shared
from lumen_core.volcano_asset_media import *  # noqa: F403


def __getattr__(name: str):
    return getattr(_shared, name)


__all__ = _shared.__all__
