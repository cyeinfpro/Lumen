"""Explicit typed wrappers for worker upstream operations and lifecycle."""

from __future__ import annotations

from typing import Any

from ..provider_runtime.errors import UpstreamError
from ..provider_runtime.upstream_services import ImageUpstreamRuntime
from . import upstream_impl as _impl


async def close_client(*, runtime: ImageUpstreamRuntime | None = None) -> None:
    await _impl.close_client(runtime=runtime)


def validate_effective_image_job_configuration(*args: Any, **kwargs: Any) -> Any:
    return _impl.validate_effective_image_job_configuration(*args, **kwargs)


edit_image = _impl.edit_image
generate_image = _impl.generate_image
responses_call = _impl.responses_call
stream_completion = _impl.stream_completion

__all__ = [
    "UpstreamError",
    "close_client",
    "edit_image",
    "generate_image",
    "responses_call",
    "stream_completion",
    "validate_effective_image_job_configuration",
]
