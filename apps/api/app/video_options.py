"""Video option projection helpers."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from lumen_core.video_providers import (
    select_video_provider,
    video_reference_media_limits,
)


def reference_media_limits_for_model(
    providers: list[Any],
    model: str,
    actions: Collection[str],
) -> dict[str, int]:
    if "reference" not in actions:
        return {}
    provider = select_video_provider(providers, model=model, action="reference")
    return video_reference_media_limits(provider.kind) if provider is not None else {}
