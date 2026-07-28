from __future__ import annotations

from ...upstream_parts.errors import UpstreamCancelled


class TaskCancelled(UpstreamCancelled):
    """The user cancelled work owned by this generation attempt."""


class LeaseLost(UpstreamCancelled):
    """The worker no longer owns the generation lease."""


class StaleGenerationAttempt(Exception):
    """The persisted attempt epoch no longer belongs to this worker."""
