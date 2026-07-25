"""Public worker upstream facade.

Implementation and runtime composition live below ``app.upstream_parts``.
Only stable worker entrypoints are exported from this module.
"""

from __future__ import annotations

from .provider_runtime.errors import UpstreamError
from .upstream_parts.upstream_impl import (
    close_client,
    edit_image,
    generate_image,
    responses_call,
    stream_completion,
    validate_effective_image_job_configuration,
)

__all__ = [
    "UpstreamError",
    "generate_image",
    "edit_image",
    "stream_completion",
    "responses_call",
    "close_client",
    "validate_effective_image_job_configuration",
]
