"""Prometheus metrics for the outbox pipeline."""

from prometheus_client import Counter

from .. import observability

outbox_events_total = observability._metric(  # noqa: SLF001
    Counter,
    "lumen_worker_outbox_events_total",
    "Outbox events handled by kind and outcome.",
    labelnames=("kind", "outcome"),
)

outbox_dlq_total = observability._metric(  # noqa: SLF001
    Counter,
    "lumen_worker_outbox_dlq_total",
    "Outbox dead-letter records by event kind and reason.",
    labelnames=("kind", "reason"),
)

outbox_retry_total = observability._metric(  # noqa: SLF001
    Counter,
    "lumen_worker_outbox_retry_total",
    "Outbox retry counter operations by outcome.",
    labelnames=("outcome",),
)
