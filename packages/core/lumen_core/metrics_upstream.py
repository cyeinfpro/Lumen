"""上游 API 调用维度的 Prometheus metrics。

Agent 1 的 upstream.py 通过下面 4 个 record_* 函数把数据打进 registry：
- record_upstream_tokens(kind, n)        ：累计上游消耗的 token，按 kind 分类
- record_upstream_duration(seconds, ep)  ：单次上游 HTTP 请求耗时
- record_upstream_request(status, ep)    ：上游请求总数，按 2xx/4xx/5xx/error 分桶
- record_used_percent(p)                  ：x-codex-primary-used-percent 最新值

设计要点：
- 输入做基本守门（非法值直接忽略），避免坏指标污染 registry。
- 不依赖 settings.metrics_enabled —— 模块被 observability 触发 import 后即注册到全局
  prometheus registry；如果 metrics 未挂 /metrics，这些 Counter 也只是常驻内存而已。
- 不在此文件做任何业务逻辑 / IO；纯指标。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# token 累计：input / output / cached / reasoning 四种语义
UPSTREAM_TOKENS_TOTAL = Counter(
    "lumen_upstream_tokens_total",
    "Tokens consumed by upstream calls",
    ["kind"],
)

# 上游 HTTP 请求耗时分布；bucket 覆盖 0.1s ~ 600s（长流式响应也能落桶）
UPSTREAM_DURATION_SECONDS = Histogram(
    "lumen_upstream_request_duration_seconds",
    "Upstream HTTP request duration",
    ["endpoint"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
)

# 上游请求计数，按状态码桶 + endpoint 拆分
UPSTREAM_REQUEST_TOTAL = Counter(
    "lumen_upstream_request_total",
    "Upstream HTTP requests by status bucket",
    ["endpoint", "status"],
)

# 上游配额使用百分比（取自响应 header），Gauge 只保留最新值
UPSTREAM_USED_PERCENT = Gauge(
    "lumen_upstream_used_percent",
    "Last reported x-codex-primary-used-percent header from upstream",
)


# 合法 token kind 集合（避免任意字符串污染 label cardinality）
_VALID_TOKEN_KINDS = ("input", "output", "cached", "reasoning")


def record_upstream_tokens(kind: str, n: int) -> None:
    """累加上游消耗的 token；非法 kind / 非正数 n 静默忽略。"""
    if n <= 0 or kind not in _VALID_TOKEN_KINDS:
        return
    UPSTREAM_TOKENS_TOTAL.labels(kind=kind).inc(n)


def record_upstream_duration(seconds: float, endpoint: str) -> None:
    """记录一次上游请求的耗时；负值（例如 monotonic 异常）忽略。"""
    if seconds < 0:
        return
    UPSTREAM_DURATION_SECONDS.labels(endpoint=endpoint).observe(seconds)


def record_upstream_request(status_code: int, endpoint: str) -> None:
    """按状态码段（2xx/4xx/5xx/error）累加请求数，便于 alert 写错误率。"""
    if 200 <= status_code < 300:
        bucket = "2xx"
    elif 400 <= status_code < 500:
        bucket = "4xx"
    elif 500 <= status_code < 600:
        bucket = "5xx"
    else:
        # 1xx / 3xx / 异常 sentinel（如 -1 表示连接失败）统一归为 error
        bucket = "error"
    UPSTREAM_REQUEST_TOTAL.labels(endpoint=endpoint, status=bucket).inc()


def record_used_percent(p: int) -> None:
    """更新上游配额使用百分比 Gauge；越界值忽略。"""
    if not (0 <= p <= 100):
        return
    UPSTREAM_USED_PERCENT.set(float(p))


__all__ = [
    "UPSTREAM_TOKENS_TOTAL",
    "UPSTREAM_DURATION_SECONDS",
    "UPSTREAM_REQUEST_TOTAL",
    "UPSTREAM_USED_PERCENT",
    "record_upstream_tokens",
    "record_upstream_duration",
    "record_upstream_request",
    "record_used_percent",
]
