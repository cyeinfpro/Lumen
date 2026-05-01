"""Lumen Worker 观测层：Sentry / OpenTelemetry / Prometheus。

- `init_sentry(dsn, environment, traces_sample_rate)` — 沿用 API 端签名，dsn 为空 no-op。
- `init_otel(service_name, endpoint)` — 配 OTLP tracer provider + 自动仪表化
  sqlalchemy / httpx / redis。Worker 没 FastAPI 实例所以不做 FastAPI integration。
- `start_metrics_server(port)` — 起 `prometheus_client` 独立 HTTP 端点暴露 Worker 指标。
- 自定义指标：`lumen_worker_task_duration_seconds{kind,outcome}` —— 由 tasks/*.py
  import 后 .labels(...).observe()。
"""

from __future__ import annotations

import logging
import re
from errno import EADDRINUSE
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, start_http_server

logger = logging.getLogger(__name__)


# ---------- Sentry PII 脱敏 ----------

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-csrf-token",
    "x-api-key",
    "x-auth-token",
}
_SENSITIVE_KEY_HINTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "csrf",
    # Worker events can carry full prompts, user text, and image URLs.
    "prompt",
    "instructions",
    "content",
    "text",
    "image_url",
    "data_url",
    "b64",
    "base64",
)


def _redact_string(value: str) -> str:
    return _EMAIL_PATTERN.sub("[email]", value)


def _scrub_value(key: str, value: Any) -> Any:
    lowered = (key or "").lower()
    if any(hint in lowered for hint in _SENSITIVE_KEY_HINTS):
        return "[redacted]"
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {k: _scrub_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(key, v) for v in value]
    return value


def _scrub_headers(headers: Any) -> Any:
    if not isinstance(headers, dict):
        return headers
    out: dict[str, Any] = {}
    for name, value in headers.items():
        if (name or "").lower() in _SENSITIVE_HEADERS:
            out[name] = "[redacted]"
        elif isinstance(value, str):
            out[name] = _redact_string(value)
        else:
            out[name] = value
    return out


def _scrub_request(request: Any) -> Any:
    if not isinstance(request, dict):
        return request
    cleaned = dict(request)
    if "cookies" in cleaned:
        cleaned["cookies"] = "[redacted]"
    if "headers" in cleaned:
        cleaned["headers"] = _scrub_headers(cleaned["headers"])
    if "data" in cleaned:
        cleaned["data"] = _scrub_value("data", cleaned["data"])
    if "query_string" in cleaned and isinstance(cleaned["query_string"], str):
        cleaned["query_string"] = _redact_string(cleaned["query_string"])
    return cleaned


def _sentry_before_send(event: dict, _hint: dict) -> dict:
    if not isinstance(event, dict):
        return event
    if "request" in event:
        event["request"] = _scrub_request(event["request"])
    if "extra" in event and isinstance(event["extra"], dict):
        event["extra"] = {k: _scrub_value(k, v) for k, v in event["extra"].items()}
    if "user" in event and isinstance(event["user"], dict):
        user = dict(event["user"])
        for key in ("email", "username", "ip_address"):
            if key in user:
                user[key] = "[redacted]"
        event["user"] = user
    return event


def _sentry_before_breadcrumb(crumb: dict, _hint: dict) -> dict | None:
    if not isinstance(crumb, dict):
        return crumb
    msg = crumb.get("message")
    if isinstance(msg, str):
        crumb["message"] = _redact_string(msg)
    data = crumb.get("data")
    if isinstance(data, dict):
        crumb["data"] = {k: _scrub_value(k, v) for k, v in data.items()}
    return crumb


# ---------- 业务指标（top-level，tasks 模块直接 import） ----------

task_duration_seconds = Histogram(
    "lumen_worker_task_duration_seconds",
    "Worker task duration in seconds, labeled by kind and outcome.",
    labelnames=("kind", "outcome"),
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

upstream_calls_total = Counter(
    "lumen_worker_upstream_calls_total",
    "Count of upstream API calls, labeled by kind and outcome.",
    labelnames=("kind", "outcome"),
)

# ---- 账号级 image 调度指标（多 provider = 多 OAuth 账号 → 每号一组时序） ----
# 当前 image 路由的状态——每个号每个 state 一个时序；同一时刻一个号只有一个
# state 是 1，其他 state 是 0。state 取自 ProviderHealth：closed / cooldown /
# rate_limited（与 pool.get_status() 的 image.state 字段对齐）。
account_image_state = Gauge(
    "lumen_account_image_state",
    "Per-account image route state (1=in this state, 0=not). "
    "States: closed / cooldown / rate_limited.",
    labelnames=("account", "state"),
)

# 累计调用计数：success / failure / rate_limited 三种 outcome
# success：report_image_success
# failure：report_image_failure（普通 retriable，3 次累计触发 image cooldown）
# rate_limited：report_image_rate_limited（429 / quota）
account_image_calls_total = Counter(
    "lumen_account_image_calls_total",
    "Per-account image generation call count by outcome.",
    labelnames=("account", "outcome"),
)

# 当前已用配额（运维用来对比 image_rate_limit / image_daily_quota 配置）：
# - window=current_window：滑动窗口当前已用次数（来自 Redis ZCARD）
# - window=daily：当日已用次数（来自 Redis daily counter）
account_image_quota_used = Gauge(
    "lumen_account_image_quota_used",
    "Per-account image quota used in current window.",
    labelnames=("account", "window"),
)

# ---- Context compaction 指标（与 record_summary_metrics 的 Redis hash 体系并行） ----
# Why: Redis hash 走 admin/小时聚合方便 ops dashboard，prometheus counter 走 /metrics
# 走时序数据库（Grafana / alertmanager），两者互补；不要替换。
# label 设计：
# - reason: "token_limit"（auto trigger 命中 token 阈值）/ "manual"（用户主动触发）
#           / "truncation_fallback"（暂未使用，保留给后续硬截断回退路径）
# - trigger: "auto" / "manual"（与 record_summary_metrics 现有 trigger 含义对齐，spec
#            里写的 "auto/user" 是笔误，按现有 Redis 体系用 "auto/manual"）
# - outcome: "ok" / "failed" / "circuit_open" / "lock_busy" / "cas_failed"
context_compaction_total = Counter(
    "lumen_context_compaction_total",
    "Conversation context compaction outcomes",
    labelnames=("reason", "trigger", "outcome"),
)

# Why: lock_busy 是没真正干活的快速失败，histogram 不该污染 p50/p99，所以调用方在
# lock_busy 分支不要 observe；只在 ok / failed / cas_failed 等真正跑过 upstream
# 的分支记录耗时（不含 lock 等待）。
context_compaction_duration_seconds = Histogram(
    "lumen_context_compaction_duration_seconds",
    "Time spent producing a context compaction summary (excluding lock wait)",
    labelnames=("reason", "outcome"),
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 60.0, 120.0),
)

# Why: 限制 outcome 标签基数，避免 prometheus 时间序列爆炸（恶意/未知值都映射到 "unknown"）
_ALLOWED_OUTCOMES = frozenset({"succeeded", "retry", "failed", "unknown", "ok", "error"})

# image route 的 outcome 白名单（与 account_image_calls_total 标签对齐）
_ALLOWED_IMAGE_OUTCOMES = frozenset({"success", "failure", "rate_limited"})

# image route state 白名单（与 pool.get_status() 的 image.state 对齐）
_ALLOWED_IMAGE_STATES = frozenset({"closed", "cooldown", "rate_limited"})


def safe_image_outcome(outcome: str | None) -> str:
    """outcome 白名单：未知值映射到 'failure'（保守计入失败而不是丢弃）。"""
    if outcome and outcome in _ALLOWED_IMAGE_OUTCOMES:
        return outcome
    return "failure"


def safe_outcome(outcome: str | None) -> str:
    """把任意 outcome 映射到白名单内，未知值统一为 'unknown'。"""
    if outcome and outcome in _ALLOWED_OUTCOMES:
        return outcome
    return "unknown"


# ---------- Sentry ----------

def init_sentry(
    dsn: str,
    environment: str,
    traces_sample_rate: float = 0.1,
) -> None:
    if not dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            environment=environment or "dev",
            traces_sample_rate=traces_sample_rate,
            send_default_pii=False,
            before_send=_sentry_before_send,
            before_breadcrumb=_sentry_before_breadcrumb,
        )
        logger.info("worker sentry initialized env=%s", environment)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker sentry init failed: %s", exc)


# ---------- OpenTelemetry ----------

def init_otel(service_name: str, endpoint: str) -> None:
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)

        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            logger.warning("otel sqlalchemy instrument failed: %s", exc)

        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            logger.warning("otel httpx instrument failed: %s", exc)

        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            RedisInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            logger.warning("otel redis instrument failed: %s", exc)

        logger.info(
            "worker otel initialized service=%s endpoint=%s", service_name, endpoint
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker otel init failed: %s", exc)


# ---------- Prometheus HTTP server ----------

_metrics_server_started = False


def start_metrics_server(port: int) -> None:
    """在指定端口起一个独立的 prometheus_client HTTP server。幂等。"""
    global _metrics_server_started
    if _metrics_server_started:
        return
    try:
        start_http_server(port)
        _metrics_server_started = True
        logger.info("worker metrics server started on :%d", port)
    except OSError as exc:
        if getattr(exc, "errno", None) == EADDRINUSE:
            logger.error("worker metrics server port already in use: :%d", port)
            raise RuntimeError(
                f"worker metrics server port already in use: {port}"
            ) from exc
        logger.error("worker metrics server could not bind :%d: %s", port, exc)
        raise RuntimeError(
            f"worker metrics server could not bind port {port}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("worker metrics server failed on :%d: %s", port, exc)
        raise RuntimeError(f"worker metrics server failed on port {port}") from exc


def get_tracer(name: str = "lumen.worker"):
    """便捷拿到当前 tracer provider 的 tracer；未初始化也可用（返回 NoOp）。"""
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:  # noqa: BLE001
        class _NoopSpan:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def set_attribute(self, *_args, **_kwargs):
                pass

        class _NoopTracer:
            def start_as_current_span(self, *_args, **_kwargs):
                return _NoopSpan()

        return _NoopTracer()


__all__ = [
    "init_sentry",
    "init_otel",
    "start_metrics_server",
    "get_tracer",
    "task_duration_seconds",
    "upstream_calls_total",
    "safe_outcome",
    "account_image_state",
    "account_image_calls_total",
    "account_image_quota_used",
    "safe_image_outcome",
    "context_compaction_total",
    "context_compaction_duration_seconds",
]
