"""Prometheus metrics shared by the Telegram stream delivery path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from prometheus_client import Counter, Gauge


class Metric(Protocol):
    def labels(self, *args: object, **kwargs: object) -> Metric: ...

    def inc(self, amount: float = 1) -> None: ...

    def set(self, value: float) -> None: ...


class _NoopMetric:
    def labels(self, *args: object, **kwargs: object) -> _NoopMetric:
        return self

    def inc(self, amount: float = 1) -> None:
        return None

    def set(self, value: float) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ListenerMetrics:
    dispatch_retry: Metric
    quarantine_total: Metric
    quarantine_depth: Metric
    terminal_notified: Metric
    delivery_lease: Metric
    delivery_result_unknown: Metric


def _counter(
    name: str,
    documentation: str,
    labelnames: tuple[str, ...] = (),
) -> Metric:
    try:
        return Counter(name, documentation, labelnames)
    except ValueError:
        return _NoopMetric()


def _gauge(name: str, documentation: str) -> Metric:
    try:
        return Gauge(name, documentation)
    except ValueError:
        return _NoopMetric()


metrics = ListenerMetrics(
    dispatch_retry=_counter(
        "tgbot_stream_dispatch_retry_total",
        "Telegram stream dispatch retries.",
        ("event", "reason"),
    ),
    quarantine_total=_counter(
        "tgbot_delivery_quarantine_total",
        "Telegram stream entries committed to durable quarantine.",
        ("event", "reason"),
    ),
    quarantine_depth=_gauge(
        "tgbot_delivery_quarantine_depth",
        "Most recently observed Telegram delivery quarantine depth.",
    ),
    terminal_notified=_counter(
        "tgbot_terminal_notified_total",
        "Owner-fenced Telegram terminal delivery receipts.",
        ("event",),
    ),
    delivery_lease=_counter(
        "tgbot_delivery_lease_total",
        "Telegram terminal delivery lease outcomes.",
        ("outcome",),
    ),
    delivery_result_unknown=_counter(
        "tgbot_delivery_result_unknown_total",
        "Telegram image deliveries whose external result is unknown.",
        ("reason",),
    ),
)
