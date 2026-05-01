"""Lumen API 观测层：Sentry / OpenTelemetry / Prometheus 统一初始化。

设计要点：
- 所有 init 函数在对应配置为空串/禁用时**静默 no-op**，方便本地 dev 不装 collector 也能跑。
- Prometheus 通过 `prometheus-fastapi-instrumentator` 挂 /metrics；metrics 启用时额外
  暴露两个自定义指标给业务代码直接 inc()：
    * `lumen_tasks_enqueued_total{kind}`   —— 入队成功数，kind in {generation, completion}
    * `lumen_http_errors_total{code}`      —— 结构化错误计数，code 沿用 error.code
- 本模块**只负责**初始化；不 import routes、不碰业务逻辑。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import FastAPI

from .config import settings

logger = logging.getLogger(__name__)


class _NoopCounter:
    def __init__(self, name: str) -> None:
        self._name = name

    def labels(self, *_args: Any, **_kwargs: Any) -> "_NoopCounter":
        return self

    def inc(
        self,
        _amount: float = 1,
        _exemplar: dict[str, str] | None = None,
    ) -> None:
        return None


def _counter(
    name: str,
    documentation: str,
    *,
    labelnames: tuple[str, ...],
) -> Any:
    if not settings.metrics_enabled:
        return _NoopCounter(name)

    from prometheus_client import Counter

    return Counter(name, documentation, labelnames=labelnames)


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
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "csrf",
)


def _redact_string(value: str) -> str:
    return _EMAIL_PATTERN.sub("[email]", value)


def _scrub_value(key: str, value: Any) -> Any:
    lowered = (key or "").lower()
    parts = {part for part in re.split(r"[^a-z0-9]+", lowered) if part}
    if lowered in _SENSITIVE_KEY_HINTS or any(hint in parts for hint in _SENSITIVE_KEY_HINTS):
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
    # Why: emails / cookies / auth headers must never reach Sentry.
    if not isinstance(event, dict):
        return event
    if "request" in event:
        event["request"] = _scrub_request(event["request"])
    if "extra" in event and isinstance(event["extra"], dict):
        event["extra"] = {k: _scrub_value(k, v) for k, v in event["extra"].items()}
    if "user" in event and isinstance(event["user"], dict):
        u = dict(event["user"])
        for k in ("email", "username", "ip_address"):
            if k in u:
                u[k] = "[redacted]"
        event["user"] = u
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


# ---------- 业务侧复用的自定义指标（metrics 禁用时为 no-op） ----------

tasks_enqueued_total = _counter(
    "lumen_tasks_enqueued_total",
    "Number of tasks enqueued to arq, labeled by kind.",
    labelnames=("kind",),
)

task_publish_errors_total = _counter(
    "lumen_task_publish_errors_total",
    "Number of best-effort task publish failures, labeled by task kind.",
    labelnames=("kind",),
)

http_errors_total = _counter(
    "lumen_http_errors_total",
    "Number of structured error responses returned, labeled by error.code.",
    labelnames=("code",),
)

audit_write_failures_total = _counter(
    "lumen_audit_write_failures_total",
    "Number of audit log write failures, labeled by write mode.",
    labelnames=("mode",),
)


# ---------- Sentry ----------

def init_sentry(
    dsn: str,
    environment: str,
    traces_sample_rate: float = 0.1,
) -> None:
    """若 `dsn` 非空则初始化 sentry_sdk，带 FastAPI integration；否则 no-op。"""
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=environment or "dev",
            traces_sample_rate=traces_sample_rate,
            send_default_pii=False,
            integrations=[
                StarletteIntegration(),
                FastApiIntegration(),
            ],
            before_send=_sentry_before_send,
            before_breadcrumb=_sentry_before_breadcrumb,
        )
        logger.info("sentry initialized env=%s", environment)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"sentry init failed: {exc}") from exc


# ---------- OpenTelemetry ----------

def init_otel(
    service_name: str,
    endpoint: str,
    app: FastAPI | None = None,
) -> None:
    """若 `endpoint` 非空则配置 OTLP tracer provider，instrument FastAPI + SQLAlchemy
    + httpx + redis。`app` 可选：FastAPI 已创建时传入即可挂 FastAPIInstrumentor。

    endpoint 形如 `http://otel-collector:4318/v1/traces`。
    """
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

        # Instrument libraries（每个 import 都 try 独立，避免缺包时整体失败）
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            if app is not None:
                FastAPIInstrumentor.instrument_app(app)
        except Exception as exc:  # noqa: BLE001
            logger.error("OBS DISABLED: otel fastapi instrument failed: %s", exc, exc_info=exc)

        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            logger.error("OBS DISABLED: otel sqlalchemy instrument failed: %s", exc, exc_info=exc)

        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            logger.error("OBS DISABLED: otel httpx instrument failed: %s", exc, exc_info=exc)

        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            RedisInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            logger.error("OBS DISABLED: otel redis instrument failed: %s", exc, exc_info=exc)

        logger.info("otel initialized service=%s endpoint=%s", service_name, endpoint)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"otel init failed: {exc}") from exc


# ---------- Prometheus ----------

def setup_prometheus(app: FastAPI) -> None:
    """挂 /metrics 端点并绑定默认 HTTP 指标。自定义 Counter 已在模块 top-level 注册。"""
    if not settings.metrics_enabled:
        return

    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator(
            should_group_status_codes=True,
            should_ignore_untemplated=True,
            should_respect_env_var=False,
            excluded_handlers=["/metrics", "/healthz"],
        ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
        logger.info("prometheus /metrics mounted")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"prometheus setup failed: {exc}") from exc


__all__ = [
    "init_sentry",
    "init_otel",
    "setup_prometheus",
    "tasks_enqueued_total",
    "http_errors_total",
]


# 避免未使用导入告警
_ = Any

from lumen_core import metrics_upstream as _metrics_upstream  # noqa: F401,E402  # 注册上游 metrics
