from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import Request, Response


@dataclass(frozen=True)
class DeliverySpec:
    path: Path
    storage_key: str | None
    media_type: str
    etag: str
    cache_control: str
    inline_filename: str | None = None


def deliver_artifact(
    spec: DeliverySpec,
    *,
    request: Request | None,
    response_builder: Callable[..., Response],
) -> Response:
    return response_builder(
        spec.path,
        media_type=spec.media_type,
        etag=spec.etag,
        cache_control=spec.cache_control,
        storage_key=spec.storage_key,
        request=request,
        inline_filename=spec.inline_filename,
    )
