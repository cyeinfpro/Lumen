from __future__ import annotations

from typing import Any

from ..config import settings


class _NoopCounter:
    def labels(self, *_args: Any, **_kwargs: Any) -> "_NoopCounter":
        return self

    def inc(self, _amount: float = 1) -> None:
        return None


def _counter(
    name: str,
    documentation: str,
    *,
    labelnames: tuple[str, ...],
) -> Any:
    if not settings.metrics_enabled:
        return _NoopCounter()

    from prometheus_client import REGISTRY, Counter

    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    return Counter(name, documentation, labelnames=labelnames)


_publish_conflicts_total = _counter(
    "image_publish_conflicts_total",
    "Number of image publish conflicts, labeled by storage backend.",
    labelnames=("backend",),
)

_publish_idempotent_winners_total = _counter(
    "image_publish_idempotent_winners_total",
    "Number of image publishes resolved to an identical concurrent winner.",
    labelnames=("backend",),
)

_staged_sweep_failures_total = _counter(
    "image_staged_sweep_failures_total",
    "Number of staged image sweep failures, labeled by stable reason.",
    labelnames=("reason",),
)

_staged_sweep_tombstones_total = _counter(
    "image_staged_sweep_tombstones_total",
    "Number of staged metadata entries deleted before sweep loading.",
    labelnames=(),
)


def record_publish_conflict(backend: str) -> None:
    _publish_conflicts_total.labels(backend=backend).inc()


def record_publish_idempotent_winner(backend: str) -> None:
    _publish_idempotent_winners_total.labels(backend=backend).inc()


def record_staged_sweep_failure(reason: str) -> None:
    _staged_sweep_failures_total.labels(reason=reason).inc()


def record_staged_sweep_tombstone() -> None:
    _staged_sweep_tombstones_total.inc()
