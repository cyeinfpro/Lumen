"""metrics_upstream 单元测试。

直接读取 prometheus_client 的样本值断言，避免依赖 /metrics scrape。
"""

from __future__ import annotations

import pytest

from lumen_core import metrics_upstream as mu


def _counter_value(counter, **labels) -> float:
    """从 Counter 取出某组 label 当前累计值。"""
    return counter.labels(**labels)._value.get()


def _histogram_count(histogram, endpoint: str) -> float:
    """从 Histogram metric collect 出来的 _count sample。"""
    metric_name = histogram._name + "_count"
    for metric in histogram.collect():
        for sample in metric.samples:
            if sample.name == metric_name and sample.labels.get("endpoint") == endpoint:
                return sample.value
    return 0.0


def _gauge_value(gauge) -> float:
    return gauge._value.get()


# ---------- record_upstream_tokens ----------

@pytest.mark.parametrize("kind", ["input", "output", "cached", "reasoning"])
def test_record_upstream_tokens_accepts_valid_kinds(kind: str) -> None:
    before = _counter_value(mu.UPSTREAM_TOKENS_TOTAL, kind=kind)
    mu.record_upstream_tokens(kind, 3)
    after = _counter_value(mu.UPSTREAM_TOKENS_TOTAL, kind=kind)
    assert after - before == pytest.approx(3.0)


def test_record_upstream_tokens_ignores_invalid_kind() -> None:
    # 不该新增 label 也不该报错
    mu.record_upstream_tokens("garbage", 10)
    # 通过私有 _metrics 字典判断没注册新 label
    assert ("garbage",) not in mu.UPSTREAM_TOKENS_TOTAL._metrics


def test_record_upstream_tokens_ignores_non_positive() -> None:
    before = _counter_value(mu.UPSTREAM_TOKENS_TOTAL, kind="input")
    mu.record_upstream_tokens("input", 0)
    mu.record_upstream_tokens("input", -5)
    after = _counter_value(mu.UPSTREAM_TOKENS_TOTAL, kind="input")
    assert after == before


# ---------- record_upstream_duration ----------

def test_record_upstream_duration_observes() -> None:
    before = _histogram_count(mu.UPSTREAM_DURATION_SECONDS, "responses")
    mu.record_upstream_duration(1.25, "responses")
    after = _histogram_count(mu.UPSTREAM_DURATION_SECONDS, "responses")
    assert after - before == 1


def test_record_upstream_duration_ignores_negative() -> None:
    before = _histogram_count(mu.UPSTREAM_DURATION_SECONDS, "probe")
    mu.record_upstream_duration(-0.5, "probe")
    after = _histogram_count(mu.UPSTREAM_DURATION_SECONDS, "probe")
    assert after == before


# ---------- record_upstream_request ----------

@pytest.mark.parametrize(
    "status_code,bucket",
    [
        (200, "2xx"),
        (201, "2xx"),
        (299, "2xx"),
        (400, "4xx"),
        (404, "4xx"),
        (499, "4xx"),
        (500, "5xx"),
        (503, "5xx"),
        (599, "5xx"),
        (100, "error"),
        (302, "error"),
        (999, "error"),
        (-1, "error"),
    ],
)
def test_record_upstream_request_buckets(status_code: int, bucket: str) -> None:
    endpoint = f"test_{status_code}"
    before = _counter_value(mu.UPSTREAM_REQUEST_TOTAL, endpoint=endpoint, status=bucket)
    mu.record_upstream_request(status_code, endpoint)
    after = _counter_value(mu.UPSTREAM_REQUEST_TOTAL, endpoint=endpoint, status=bucket)
    assert after - before == 1


# ---------- record_used_percent ----------

@pytest.mark.parametrize("p", [0, 1, 50, 99, 100])
def test_record_used_percent_in_range(p: int) -> None:
    mu.record_used_percent(p)
    assert _gauge_value(mu.UPSTREAM_USED_PERCENT) == pytest.approx(float(p))


@pytest.mark.parametrize("p", [-1, 101, 1000, -100])
def test_record_used_percent_out_of_range_ignored(p: int) -> None:
    mu.record_used_percent(42)  # 先设一个已知值
    mu.record_used_percent(p)   # 越界值应被忽略
    assert _gauge_value(mu.UPSTREAM_USED_PERCENT) == pytest.approx(42.0)
