"""Durable Agent Provider-dispatch evidence shared by API and Worker."""

from __future__ import annotations

from typing import Any


PROVIDER_DISPATCH_AUTHORIZED_COUNT_KEY = "provider_dispatch_authorized_count"


def _nonnegative_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def provider_dispatch_evidence_count(dispatch: dict[str, Any]) -> int:
    """Return the largest durable proof that a Provider call may have started."""
    return max(
        provider_dispatch_checkpointed_count(dispatch),
        provider_dispatch_authorized_count(dispatch),
    )


def provider_dispatch_checkpointed_count(dispatch: dict[str, Any]) -> int:
    return _nonnegative_count(dispatch.get("provider_dispatch_count"))


def provider_dispatch_authorized_count(dispatch: dict[str, Any]) -> int:
    return _nonnegative_count(dispatch.get(PROVIDER_DISPATCH_AUTHORIZED_COUNT_KEY))


def mark_provider_dispatch_authorized(
    dispatch: dict[str, Any],
    ordinal: int,
) -> None:
    if isinstance(ordinal, bool) or ordinal < 1:
        raise ValueError("provider dispatch ordinal must be positive")
    dispatch[PROVIDER_DISPATCH_AUTHORIZED_COUNT_KEY] = max(
        _nonnegative_count(dispatch.get(PROVIDER_DISPATCH_AUTHORIZED_COUNT_KEY)),
        ordinal,
    )


__all__ = [
    "PROVIDER_DISPATCH_AUTHORIZED_COUNT_KEY",
    "mark_provider_dispatch_authorized",
    "provider_dispatch_authorized_count",
    "provider_dispatch_checkpointed_count",
    "provider_dispatch_evidence_count",
]
