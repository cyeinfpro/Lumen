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
import threading
from collections.abc import Callable
from dataclasses import dataclass
from errno import EADDRINUSE
from socketserver import ThreadingMixIn
from typing import TYPE_CHECKING, Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, make_wsgi_app

if TYPE_CHECKING:
    from sentry_sdk.types import Event

logger = logging.getLogger(__name__)


# ---------- Sentry PII 脱敏 ----------

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-csrf-token",
        "x-api-key",
        "x-auth-token",
        "x-lumen-upstream-authorization",
    }
)
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


def _sentry_before_send(
    event: Event,
    _hint: dict[str, Any],
) -> Event | None:
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


def _registered_collector(name: str) -> Any | None:
    names_to_collectors = getattr(REGISTRY, "_names_to_collectors", {})
    base = name[:-6] if name.endswith("_total") else name
    for candidate in (
        name,
        base,
        f"{base}_total",
        f"{base}_created",
        f"{base}_bucket",
        f"{base}_count",
        f"{base}_sum",
    ):
        collector = names_to_collectors.get(candidate)
        if collector is not None:
            return collector
    return None


def _metric(factory: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    existing = _registered_collector(name)
    if existing is not None:
        return existing
    try:
        return factory(name, *args, **kwargs)
    except ValueError:
        existing = _registered_collector(name)
        if existing is not None:
            return existing
        raise


task_duration_seconds = _metric(
    Histogram,
    "lumen_worker_task_duration_seconds",
    "Worker task duration in seconds, labeled by kind and outcome.",
    labelnames=("kind", "outcome"),
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

upstream_calls_total = _metric(
    Counter,
    "lumen_worker_upstream_calls_total",
    "Count of upstream API calls, labeled by kind and outcome.",
    labelnames=("kind", "outcome"),
)

sse_live_publish_total = _metric(
    Counter,
    "lumen_sse_live_publish_total",
    "SSE live fanout attempts by channel kind and outcome.",
    labelnames=("channel_kind", "outcome"),
)

sse_live_publish_bytes_total = _metric(
    Counter,
    "lumen_sse_live_publish_bytes_total",
    "SSE live fanout payload bytes by channel kind.",
    labelnames=("channel_kind",),
)

sse_live_publish_duration_seconds = _metric(
    Histogram,
    "lumen_sse_live_publish_duration_seconds",
    "SSE live fanout duration by channel kind and outcome.",
    labelnames=("channel_kind", "outcome"),
)

wallet_overdrawn_total = _metric(
    Counter,
    "wallet_overdrawn_total",
    "Number of wallet billing operations that produced an overdrawn adjustment.",
    labelnames=("kind",),
)

wallet_charge_lost_total = _metric(
    Counter,
    "wallet_charge_lost_total",
    "Number of completion charge attempts that failed after upstream work completed.",
)

billing_cost_micro_total = _metric(
    Counter,
    "lumen_billing_cost_micro_total",
    "Completion billing cost by cost bucket, in micro RMB.",
    labelnames=("kind",),
)

billing_pricing_source_total = _metric(
    Counter,
    "lumen_billing_pricing_source_total",
    "Completion pricing source decisions.",
    labelnames=("source",),
)

billing_rate_limit_block_total = _metric(
    Counter,
    "lumen_billing_rate_limit_block_total",
    "Billing window rate-limit blocks.",
    labelnames=("window",),
)

billing_idempotency_replay_total = _metric(
    Counter,
    "lumen_billing_idempotency_replay_total",
    "Billing ledger idempotency replays.",
)

completion_cancel_check_errors_total = _metric(
    Counter,
    "lumen_completion_cancel_check_errors_total",
    "Redis errors while checking chat completion cancellation.",
)

task_reconcile_lease_unknown_total = _metric(
    Counter,
    "lumen_task_reconcile_lease_unknown_total",
    "Task reconciler lease reads that could not determine ownership.",
    labelnames=("kind",),
)

# ---- 账号级 image 调度指标（多 provider = 多 OAuth 账号 → 每号一组时序） ----
# 当前 image 路由的状态——每个号每个 state 一个时序；同一时刻一个号只有一个
# state 是 1，其他 state 是 0。state 取自 ProviderHealth：closed / cooldown /
# rate_limited（与 pool.get_status() 的 image.state 字段对齐）。
account_image_state = _metric(
    Gauge,
    "lumen_account_image_state",
    "Per-account image route state (1=in this state, 0=not). "
    "States: closed / cooldown / rate_limited.",
    labelnames=("account", "state"),
)

# 累计调用计数：success / failure / rate_limited 三种 outcome
# success：report_image_success
# failure：report_image_failure（普通 retriable，3 次累计触发 image cooldown）
# rate_limited：report_image_rate_limited（429 / quota）
account_image_calls_total = _metric(
    Counter,
    "lumen_account_image_calls_total",
    "Per-account image generation call count by outcome.",
    labelnames=("account", "outcome"),
)

# 当前已用配额（运维用来对比 image_rate_limit / image_daily_quota 配置）：
# - window=current_window：滑动窗口当前已用次数（来自 Redis ZCARD）
# - window=daily：当日已用次数（来自 Redis daily counter）
account_image_quota_used = _metric(
    Gauge,
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
context_compaction_total = _metric(
    Counter,
    "lumen_context_compaction_total",
    "Conversation context compaction outcomes",
    labelnames=("reason", "trigger", "outcome"),
)

# Why: lock_busy 是没真正干活的快速失败，histogram 不该污染 p50/p99，所以调用方在
# lock_busy 分支不要 observe；只在 ok / failed / cas_failed 等真正跑过 upstream
# 的分支记录耗时（不含 lock 等待）。
context_compaction_duration_seconds = _metric(
    Histogram,
    "lumen_context_compaction_duration_seconds",
    "Time spent producing a context compaction summary (excluding lock wait)",
    labelnames=("reason", "outcome"),
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 60.0, 120.0),
)

# ---- DB 连接池指标 ----
# Why: worker 的每一条上游路径都要先拿 DB 连接；池被占满时症状是「任务全都变慢」，
# 但没有指标就只能靠 PG 侧 pg_stat_activity 猜。用 set_function 在 scrape 时现采样，
# 不需要额外的 cron，也不会在没人抓 /metrics 时产生任何开销。
# 刻意不做审计里同时提到的 Redis 连接池指标：redis-py 只在私有属性
# （_created_connections / _available_connections / _in_use_connections）上暴露池状态，
# 且 ConnectionPool 与 BlockingConnectionPool 字段并不一致；arq 还会在 worker 里另建
# 自己的池，导出的数字既脆弱又对不上真实并发，代价大于收益。
db_pool_connections = _metric(
    Gauge,
    "lumen_worker_db_pool_connections",
    "SQLAlchemy connection pool state. "
    "States: size / checked_in / checked_out / overflow.",
    labelnames=("state",),
)

# (gauge 的 state 标签, SQLAlchemy Pool 上的方法名)；用元组而不是 dict，
# 模块级可变状态会被 check_architecture 的 runtime-coupling 规则拦下。
_DB_POOL_ACCESSORS: tuple[tuple[str, str], ...] = (
    ("size", "size"),
    ("checked_in", "checkedin"),
    ("checked_out", "checkedout"),
    ("overflow", "overflow"),
)


def _db_pool_sampler(engine: Any, accessor: str) -> Callable[[], float]:
    def sample() -> float:
        try:
            pool = engine.pool
            value = getattr(pool, accessor)()
        except Exception:  # noqa: BLE001
            # 指标采样发生在 scrape 线程里：任何异常都会让整个 /metrics 变 500，
            # 把一个观测项升级成「监控全瞎」。取不到就报 -1（明确的哨兵值）。
            return -1.0
        return float(value) if isinstance(value, (int, float)) else -1.0

    return sample


def bind_db_pool_metrics(engine: Any) -> None:
    """把连接池状态挂到 gauge 上（幂等，重复调用只是覆盖同一个采样函数）。"""
    for state, accessor in _DB_POOL_ACCESSORS:
        db_pool_connections.labels(state=state).set_function(
            _db_pool_sampler(engine, accessor)
        )


# Why: 限制 outcome 标签基数，避免 prometheus 时间序列爆炸（恶意/未知值都映射到 "unknown"）
_ALLOWED_OUTCOMES = frozenset(
    {"succeeded", "retry", "failed", "unknown", "ok", "error"}
)

# image route 的 outcome 白名单（与 account_image_calls_total 标签对齐）
_ALLOWED_IMAGE_OUTCOMES = frozenset({"success", "failure", "rate_limited"})

# image route state 白名单（与 pool.get_status() 的 image.state 对齐）
IMAGE_ROUTE_STATES = frozenset({"closed", "cooldown", "rate_limited"})


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
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
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


@dataclass(slots=True)
class MetricsServerRuntime:
    started: bool = False
    httpd: Any | None = None
    thread: threading.Thread | None = None


_METRICS_SERVER = MetricsServerRuntime()


class _ThreadingMetricsWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _SilentMetricsHandler(WSGIRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _start_metrics_wsgi_server(host: str, port: int) -> tuple[Any, threading.Thread]:
    httpd = make_server(
        host,
        port,
        make_wsgi_app(REGISTRY),
        _ThreadingMetricsWSGIServer,
        handler_class=_SilentMetricsHandler,
    )
    try:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
    except Exception:
        httpd.server_close()
        raise
    return httpd, thread


# Prometheus scrapes this listener over the private container network.
def start_metrics_server(
    port: int,
    host: str = "0.0.0.0",  # nosec B104
) -> None:
    """在指定端口起一个独立的 prometheus_client HTTP server。幂等。"""
    if _METRICS_SERVER.started:
        return
    # Empty configuration retains the container-network listener.
    bind_host = host.strip() or "0.0.0.0"  # nosec B104
    try:
        httpd, thread = _start_metrics_wsgi_server(bind_host, port)
        _METRICS_SERVER.httpd = httpd
        _METRICS_SERVER.thread = thread
        _METRICS_SERVER.started = True
        logger.info("worker metrics server started on %s:%d", bind_host, port)
    except OSError as exc:
        if getattr(exc, "errno", None) == EADDRINUSE:
            logger.error(
                "worker metrics server port already in use: %s:%d",
                bind_host,
                port,
            )
            raise RuntimeError(
                f"worker metrics server port already in use: {bind_host}:{port}"
            ) from exc
        logger.error(
            "worker metrics server could not bind %s:%d: %s",
            bind_host,
            port,
            exc,
        )
        raise RuntimeError(
            f"worker metrics server could not bind {bind_host}:{port}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("worker metrics server failed on %s:%d: %s", bind_host, port, exc)
        raise RuntimeError(
            f"worker metrics server failed on {bind_host}:{port}"
        ) from exc


def stop_metrics_server() -> None:
    """Stop the prometheus HTTP server if startup later fails."""
    httpd = _METRICS_SERVER.httpd
    _METRICS_SERVER.httpd = None
    _METRICS_SERVER.thread = None
    _METRICS_SERVER.started = False
    if httpd is None:
        return
    try:
        httpd.shutdown()
    finally:
        httpd.server_close()


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
    "stop_metrics_server",
    "get_tracer",
    "task_duration_seconds",
    "upstream_calls_total",
    "wallet_overdrawn_total",
    "IMAGE_ROUTE_STATES",
    "wallet_charge_lost_total",
    "billing_cost_micro_total",
    "billing_pricing_source_total",
    "billing_rate_limit_block_total",
    "billing_idempotency_replay_total",
    "completion_cancel_check_errors_total",
    "task_reconcile_lease_unknown_total",
    "safe_outcome",
    "account_image_state",
    "account_image_calls_total",
    "account_image_quota_used",
    "safe_image_outcome",
    "context_compaction_total",
    "context_compaction_duration_seconds",
    "db_pool_connections",
    "bind_db_pool_metrics",
]
