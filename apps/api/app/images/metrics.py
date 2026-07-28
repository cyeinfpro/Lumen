from __future__ import annotations

from typing import Any

from ..config import settings


class _NoopMetric:
    def labels(self, *_args: Any, **_kwargs: Any) -> "_NoopMetric":
        return self

    def inc(self, _amount: float = 1) -> None:
        return None

    def observe(self, _amount: float) -> None:
        return None


def _counter(
    name: str,
    documentation: str,
    *,
    labelnames: tuple[str, ...],
) -> Any:
    if not settings.metrics_enabled:
        return _NoopMetric()

    from prometheus_client import REGISTRY, Counter

    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    return Counter(name, documentation, labelnames=labelnames)


def _histogram(name: str, documentation: str) -> Any:
    if not settings.metrics_enabled:
        return _NoopMetric()

    from prometheus_client import REGISTRY, Histogram

    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    return Histogram(name, documentation)


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

_upload_bytes = _histogram(
    "image_upload_bytes",
    "Uploaded image source bytes accepted by the artifact writer.",
)

_upload_writer_queue_wait_seconds = _histogram(
    "image_upload_writer_queue_wait_seconds",
    "Time image upload producers wait for writer queue capacity.",
)

_upload_writer_duration_seconds = _histogram(
    "image_upload_writer_duration_seconds",
    "Duration of the single blocking image upload writer lifecycle.",
)

_capacity_reservation_ratio = _histogram(
    "image_capacity_reservation_ratio",
    "Ratio of final image storage reservation bytes to actual transient bytes.",
)


def record_publish_conflict(backend: str) -> None:
    _publish_conflicts_total.labels(backend=backend).inc()


def record_publish_idempotent_winner(backend: str) -> None:
    _publish_idempotent_winners_total.labels(backend=backend).inc()


def record_staged_sweep_failure(reason: str) -> None:
    _staged_sweep_failures_total.labels(reason=reason).inc()


def record_staged_sweep_tombstone() -> None:
    _staged_sweep_tombstones_total.inc()


def record_upload_writer(
    *,
    upload_bytes: int,
    queue_wait_seconds: float,
    duration_seconds: float,
) -> None:
    _upload_bytes.observe(max(0, upload_bytes))
    _upload_writer_queue_wait_seconds.observe(max(0.0, queue_wait_seconds))
    _upload_writer_duration_seconds.observe(max(0.0, duration_seconds))


def record_capacity_reservation_ratio(
    *,
    reserved_bytes: int,
    actual_bytes: int,
) -> None:
    if actual_bytes <= 0:
        return
    _capacity_reservation_ratio.observe(max(0, reserved_bytes) / actual_bytes)
