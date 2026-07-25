"""Prometheus metrics for reconciliation decisions."""

from prometheus_client import Counter

from .. import observability

reconciliation_rows_total = observability._metric(  # noqa: SLF001
    Counter,
    "lumen_worker_reconciliation_rows_total",
    "Reconciliation row decisions by domain and action.",
    labelnames=("domain", "action"),
)

reconciliation_runs_total = observability._metric(  # noqa: SLF001
    Counter,
    "lumen_worker_reconciliation_runs_total",
    "Reconciliation coordinator runs by coordinator and outcome.",
    labelnames=("coordinator", "outcome"),
)

reconciliation_lease_state_total = observability._metric(  # noqa: SLF001
    Counter,
    "lumen_worker_reconciliation_lease_state_total",
    "Task lease observations by domain and state.",
    labelnames=("domain", "state"),
)
