"""Versioned parsing for durable completion checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumen_core.pricing import parse_canonical_nonnegative_int
from lumen_core.upstream_billing import (
    UPSTREAM_RESPONSE_ATTEMPT,
    UPSTREAM_RESPONSE_EXECUTION_EPOCH,
    has_upstream_response_receipt,
)

COMPLETION_EXECUTION_EPOCH_KEY = "execution_epoch"
COMPLETION_USAGE_EXECUTION_EPOCH_KEY = "completion_usage_execution_epoch"
COMPLETION_USAGE_ATTEMPT_EPOCH_KEY = "completion_usage_attempt_epoch"
COMPLETION_CHECKPOINT_LEGACY_VERSION = 1
COMPLETION_CHECKPOINT_VERSION = 2
COMPLETION_CHECKPOINT_VERSION_KEY = "completion_checkpoint_version"
COMPLETION_CHECKPOINT_EXECUTION_EPOCH_KEY = "completion_checkpoint_execution_epoch"
COMPLETION_CHECKPOINT_ATTEMPT_EPOCH_KEY = "completion_checkpoint_attempt_epoch"
COMPLETION_CHECKPOINT_RESPONSE_ID_KEY = "completion_checkpoint_response_id"
COMPLETION_CHECKPOINT_USAGE_COMPLETE_KEY = "completion_checkpoint_usage_complete"
COMPLETION_CHECKPOINT_USAGE_KEY = "completion_checkpoint_usage"
COMPLETION_CHECKPOINT_AT_KEY = "completion_checkpoint_at"
COMPLETION_CHECKPOINT_STATE_KEY = "completion_checkpoint_state"
COMPLETION_CHECKPOINT_USAGE_EXACT_KEY = "completion_checkpoint_usage_exact"
COMPLETION_CHECKPOINT_IMAGES_KEY = "completion_checkpoint_images"
COMPLETION_CHECKPOINT_CONTAINER_KEY = "completion_checkpoint"

_SUPPORTED_VERSIONS = (
    COMPLETION_CHECKPOINT_LEGACY_VERSION,
    COMPLETION_CHECKPOINT_VERSION,
)
_REQUIRED_EXACT_USAGE_PATH_GROUPS = (
    (
        ("input_tokens",),
        ("prompt_tokens",),
        ("promptTokenCount",),
    ),
    (
        ("output_tokens",),
        ("completion_tokens",),
        ("candidatesTokenCount",),
    ),
)
_OPTIONAL_EXACT_USAGE_PATHS = (
    ("total_tokens",),
    ("totalTokenCount",),
    ("cache_read_input_tokens",),
    ("cache_read_tokens",),
    ("cache_creation_input_tokens",),
    ("cache_creation_tokens",),
    ("cache_creation", "ephemeral_5m_input_tokens"),
    ("cache_creation_5m_input_tokens",),
    ("cache_creation_5m_tokens",),
    ("cache_creation", "ephemeral_1h_input_tokens"),
    ("cache_creation_1h_input_tokens",),
    ("cache_creation_1h_tokens",),
    ("input_tokens_details", "cached_tokens"),
    ("prompt_tokens_details", "cached_tokens"),
    ("cached_tokens",),
    ("cachedContentTokenCount",),
    ("output_tokens_details", "reasoning_tokens"),
    ("completion_tokens_details", "reasoning_tokens"),
    ("reasoning_tokens",),
    ("output_tokens_details", "image_tokens"),
    ("completion_tokens_details", "image_tokens"),
    ("image_output_tokens",),
    ("image_tokens",),
)
_V2_FIELD_KEYS = (
    ("execution_epoch", COMPLETION_CHECKPOINT_EXECUTION_EPOCH_KEY),
    ("attempt", COMPLETION_CHECKPOINT_ATTEMPT_EPOCH_KEY),
    ("attempt_epoch", COMPLETION_CHECKPOINT_ATTEMPT_EPOCH_KEY),
    ("response_id", COMPLETION_CHECKPOINT_RESPONSE_ID_KEY),
    ("usage_exact", COMPLETION_CHECKPOINT_USAGE_EXACT_KEY),
    ("usage_complete", COMPLETION_CHECKPOINT_USAGE_COMPLETE_KEY),
    ("usage", COMPLETION_CHECKPOINT_USAGE_KEY),
    ("at", COMPLETION_CHECKPOINT_AT_KEY),
    ("state", COMPLETION_CHECKPOINT_STATE_KEY),
    ("images", COMPLETION_CHECKPOINT_IMAGES_KEY),
)


@dataclass(frozen=True, slots=True)
class CompletionCheckpointParse:
    request: dict[str, Any] | None = None
    error: str | None = None
    stale: bool = False


def _nonnegative_int(value: Any) -> int | None:
    return parse_canonical_nonnegative_int(value)


def _completion_request(completion: Any) -> dict[str, Any]:
    request = getattr(completion, "upstream_request", None)
    return dict(request) if isinstance(request, dict) else {}


def _has_checkpoint_fields(request: dict[str, Any]) -> bool:
    return COMPLETION_CHECKPOINT_CONTAINER_KEY in request or any(
        key.startswith("completion_checkpoint_") for key in request
    )


def _canonical_checkpoint_request(
    request: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    canonical = dict(request)
    nested = request.get(COMPLETION_CHECKPOINT_CONTAINER_KEY)
    version = request.get(COMPLETION_CHECKPOINT_VERSION_KEY)
    if not isinstance(nested, dict):
        return canonical, version
    version = nested.get("version", version)
    for source, target in _V2_FIELD_KEYS:
        if source in nested:
            canonical[target] = nested[source]
    canonical.pop(COMPLETION_CHECKPOINT_CONTAINER_KEY, None)
    return canonical, version


def _checkpoint_is_stale(
    completion: Any,
    request: dict[str, Any],
) -> tuple[bool, str | None]:
    checkpoint_epoch = _nonnegative_int(
        request.get(COMPLETION_CHECKPOINT_EXECUTION_EPOCH_KEY)
    )
    checkpoint_attempt = _nonnegative_int(
        request.get(COMPLETION_CHECKPOINT_ATTEMPT_EPOCH_KEY)
    )
    if checkpoint_epoch is None or checkpoint_attempt is None:
        return False, "completion checkpoint execution identity is invalid"
    execution_epoch = _nonnegative_int(getattr(completion, "execution_epoch", None))
    attempt = _nonnegative_int(getattr(completion, "attempt", None))
    if execution_epoch is None or attempt is None:
        return False, "completion execution identity is invalid"
    return (
        checkpoint_epoch != execution_epoch or checkpoint_attempt != attempt,
        None,
    )


def _checkpoint_receipts_are_valid(
    completion: Any,
    request: dict[str, Any],
) -> bool:
    execution_epoch = _nonnegative_int(getattr(completion, "execution_epoch", None))
    attempt = _nonnegative_int(getattr(completion, "attempt", None))
    usage_epoch = _nonnegative_int(request.get(COMPLETION_USAGE_EXECUTION_EPOCH_KEY))
    usage_attempt = _nonnegative_int(request.get(COMPLETION_USAGE_ATTEMPT_EPOCH_KEY))
    response_id = request.get(COMPLETION_CHECKPOINT_RESPONSE_ID_KEY)
    response_attempt = _nonnegative_int(request.get(UPSTREAM_RESPONSE_ATTEMPT))
    response_epoch = _nonnegative_int(request.get(UPSTREAM_RESPONSE_EXECUTION_EPOCH))
    return bool(
        execution_epoch is not None
        and attempt is not None
        and usage_epoch == execution_epoch
        and usage_attempt == attempt
        and isinstance(response_id, str)
        and response_id.strip()
        and response_attempt == attempt
        and response_epoch == execution_epoch
        and has_upstream_response_receipt(
            request,
            execution_epoch=execution_epoch,
        )
    )


def parse_completion_checkpoint(completion: Any) -> CompletionCheckpointParse:
    raw_request = _completion_request(completion)
    if not _has_checkpoint_fields(raw_request):
        return CompletionCheckpointParse()
    request, raw_version = _canonical_checkpoint_request(raw_request)
    version = _nonnegative_int(raw_version)
    if version is None:
        return CompletionCheckpointParse(
            error="completion checkpoint version is invalid"
        )
    request[COMPLETION_CHECKPOINT_VERSION_KEY] = version
    if version not in _SUPPORTED_VERSIONS:
        return CompletionCheckpointParse(
            error=f"unsupported completion checkpoint version: {version}"
        )
    stale, identity_error = _checkpoint_is_stale(completion, request)
    if identity_error is not None:
        return CompletionCheckpointParse(error=identity_error)
    if stale:
        return CompletionCheckpointParse(stale=True)
    if not _checkpoint_receipts_are_valid(completion, request):
        return CompletionCheckpointParse(
            error="completion checkpoint response or usage receipt is invalid"
        )
    return CompletionCheckpointParse(request=request)


def completed_usage_has_exact_totals(raw_usage: Any) -> bool:
    if not isinstance(raw_usage, dict):
        return False

    malformed = object()

    def path_value(path: tuple[str, ...]) -> tuple[bool, Any]:
        value: Any = raw_usage
        for key in path:
            if value is None:
                return False, None
            if not isinstance(value, dict):
                return True, malformed
            if key not in value:
                return False, None
            value = value[key]
        return True, value

    def exact_alias_present(paths: tuple[tuple[str, ...], ...]) -> bool:
        for path in paths:
            present, value = path_value(path)
            if not present or value is None:
                continue
            return parse_canonical_nonnegative_int(value) is not None
        return False

    if not all(
        exact_alias_present(paths) for paths in _REQUIRED_EXACT_USAGE_PATH_GROUPS
    ):
        return False
    for path in _OPTIONAL_EXACT_USAGE_PATHS:
        present, value = path_value(path)
        if (
            present
            and value is not None
            and parse_canonical_nonnegative_int(value) is None
        ):
            return False
    return True


__all__ = [
    "COMPLETION_CHECKPOINT_AT_KEY",
    "COMPLETION_CHECKPOINT_ATTEMPT_EPOCH_KEY",
    "COMPLETION_CHECKPOINT_CONTAINER_KEY",
    "COMPLETION_CHECKPOINT_EXECUTION_EPOCH_KEY",
    "COMPLETION_CHECKPOINT_IMAGES_KEY",
    "COMPLETION_CHECKPOINT_LEGACY_VERSION",
    "COMPLETION_CHECKPOINT_RESPONSE_ID_KEY",
    "COMPLETION_CHECKPOINT_STATE_KEY",
    "COMPLETION_CHECKPOINT_USAGE_COMPLETE_KEY",
    "COMPLETION_CHECKPOINT_USAGE_EXACT_KEY",
    "COMPLETION_CHECKPOINT_USAGE_KEY",
    "COMPLETION_CHECKPOINT_VERSION",
    "COMPLETION_CHECKPOINT_VERSION_KEY",
    "COMPLETION_EXECUTION_EPOCH_KEY",
    "COMPLETION_USAGE_ATTEMPT_EPOCH_KEY",
    "COMPLETION_USAGE_EXECUTION_EPOCH_KEY",
    "CompletionCheckpointParse",
    "completed_usage_has_exact_totals",
    "parse_completion_checkpoint",
]
