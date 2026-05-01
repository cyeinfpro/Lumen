"""上游 api.example.com 健康探测（每小时第 5 分钟一次）。

目的：常态化验证上游 `/v1/responses` SSE 端点的三件事——
  (a) HTTP 200 / 完整 SSE 流；
  (b) `response.completed` 帧的 schema 字段不消失（id/object/created_at/output/usage）；
  (c) 上游没有静默切换默认 model（response.model 必须等于请求的 model）。

实现：
- 调 `apps.worker.app.upstream.stream_completion`（不直接 httpx，避开 base_url/key 选择
  逻辑的重复实现），消费完整 SSE 流并抽 `response.completed.response`。
- 失败时 `logger.error` + Sentry capture（软导入）。
- Prometheus 指标走两路：
  * 软导入 `lumen_core.metrics_upstream` 的 `record_upstream_request` /
    `record_upstream_duration`（共享包 metrics，失败时只 log）；
  * 本地 Gauge `lumen_upstream_probe_last_ok_timestamp` /
    `lumen_upstream_probe_schema_drift` 直接注册到默认 registry（worker 进程的
    `start_metrics_server` 会把它一起暴露在 :9100/metrics）。

预算：每小时一次最小请求（`Reply with 'ok'`），按 probe report 估算 < 1 美分。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 探针请求体（最小可用 chat 请求 + cache_lookback 标识）
# ---------------------------------------------------------------------------
# 注：model 在 probe_upstream() 入口动态从 settings 读，避免硬编码 gpt-5.4。
def _build_probe_request(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": "Reply with the single word 'ok'.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ping"}],
            }
        ],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "stream": True,  # 上游 HTTP 端忽略 stream:false，强制 SSE，按真实路径走
    }


# `response.completed.response` 必须出现的字段。任何一个缺失都视为 schema drift。
EXPECTED_FIELDS = frozenset({"id", "object", "created_at", "output", "usage"})

# 单次探针的 wall-clock 上限。上游正常 ping 应在 10s 内回完，30s 给慢启动留余量。
_PROBE_TIMEOUT_S: float = 30.0


# ---------------------------------------------------------------------------
# Prometheus 指标（probe 专用 Gauge，本模块局部注册到默认 registry）
# ---------------------------------------------------------------------------
# 用 try/except 是为了让单元测试在没装 prometheus_client 的极端环境下也能 import。
try:
    from prometheus_client import Gauge

    PROBE_LAST_OK = Gauge(
        "lumen_upstream_probe_last_ok_timestamp",
        "Unix ts of last successful upstream probe (0 if never).",
    )
    PROBE_SCHEMA_DRIFT = Gauge(
        "lumen_upstream_probe_schema_drift",
        "1 if last probe detected schema drift (missing fields or model swap), else 0.",
    )
    # 启动初值：未探测前 schema_drift=0、last_ok=0，避免曲线断点。
    PROBE_LAST_OK.set(0)
    PROBE_SCHEMA_DRIFT.set(0)
except Exception:  # noqa: BLE001
    # prometheus_client 缺失时降级——保留同名占位对象，set() 是 no-op。
    class _NoopGauge:
        def set(self, _value: float) -> None:
            return None

    PROBE_LAST_OK = _NoopGauge()  # type: ignore[assignment]
    PROBE_SCHEMA_DRIFT = _NoopGauge()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 软导入 metrics_upstream（位于共享 packages/core）。理论上 lumen_core 一定可用；
# 任何异常情况下不让 cron 崩——只 log 一次。
# ---------------------------------------------------------------------------
def _record_request(status_code: int, endpoint: str = "probe") -> None:
    try:
        from lumen_core.metrics_upstream import record_upstream_request
    except Exception:  # noqa: BLE001
        logger.debug("upstream_probe.metrics_upstream import failed (status=%s)", status_code)
        return
    try:
        record_upstream_request(status_code=status_code, endpoint=endpoint)
    except Exception as exc:  # noqa: BLE001
        logger.debug("upstream_probe.record_upstream_request err=%s", exc)


def _record_duration(seconds: float, endpoint: str = "probe") -> None:
    try:
        from lumen_core.metrics_upstream import record_upstream_duration
    except Exception:  # noqa: BLE001
        return
    try:
        record_upstream_duration(seconds=seconds, endpoint=endpoint)
    except Exception as exc:  # noqa: BLE001
        logger.debug("upstream_probe.record_upstream_duration err=%s", exc)


# ---------------------------------------------------------------------------
# Sentry 软导入：装了就 capture_message / capture_exception，没装就只 log。
# ---------------------------------------------------------------------------
def _sentry_capture(message: str, level: str = "error", **extras: Any) -> None:
    try:
        import sentry_sdk  # type: ignore
    except Exception:  # noqa: BLE001
        return
    try:
        # sentry_sdk 2.x：用 new_scope() 替代已弃用的 push_scope()
        new_scope = getattr(sentry_sdk, "new_scope", None)
        if callable(new_scope):
            with new_scope() as scope:  # type: ignore[misc]
                for k, v in extras.items():
                    scope.set_extra(k, v)
                sentry_sdk.capture_message(message, level=level)  # type: ignore[arg-type]
        else:
            # 极老版本 fallback
            with sentry_sdk.push_scope() as scope:  # type: ignore[attr-defined]
                for k, v in extras.items():
                    scope.set_extra(k, v)
                sentry_sdk.capture_message(message, level=level)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.debug("upstream_probe.sentry_capture err=%s", exc)


# ---------------------------------------------------------------------------
# 默认 model 解析：优先 settings.upstream_default_model（admin 可改），
# 缺失时回落到 lumen_core 常量 DEFAULT_IMAGE_RESPONSES_MODEL。
# 注意：probe 不使用 image probe 的 model（那个会烧配额），用默认 chat / responses
# 文本 model 即可。
# ---------------------------------------------------------------------------
def _resolve_probe_model() -> str:
    try:
        from ..config import settings as _settings

        m = getattr(_settings, "upstream_default_model", "") or ""
        if isinstance(m, str) and m.strip():
            return m.strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("upstream_probe.settings load err=%s", exc)
    try:
        from lumen_core.constants import DEFAULT_IMAGE_RESPONSES_MODEL

        return DEFAULT_IMAGE_RESPONSES_MODEL
    except Exception:  # noqa: BLE001
        # 最后兜底：回归 probe report 同款 model。绝不 raise——cron 必须不崩。
        return "gpt-5.4"


# ---------------------------------------------------------------------------
# 抽 SSE 完成帧：response.completed 的 data.response 是结构化对象。
# 实际上游事件结构（参考 _iter_sse 文档）：
#   { "type": "response.completed", "response": { "id": ..., "model": ..., ... } }
# 兼容部分网关把字段平铺到顶层的边角情况。
# ---------------------------------------------------------------------------
def _extract_completed_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    ev_type = event.get("type")
    if ev_type != "response.completed":
        return None
    resp = event.get("response")
    if isinstance(resp, dict):
        return resp
    # 兜底：极少数代理把 response 的字段平铺
    flat = {k: event.get(k) for k in EXPECTED_FIELDS if k in event}
    if "model" in event:
        flat["model"] = event["model"]
    return flat or None


async def probe_upstream(ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """对上游执行一次最小 SSE 探针。

    返回结构（也是 cron 的返回值，arq 会按 keep_result 落到 Redis）：
        {
            "ok": bool,             # 三项都通过才 True
            "latency_ms": float,    # wall-clock 耗时，无论成败都记
            "status": int,          # HTTP-like 状态：200 = 流读完，5xx = 读失败/超时
            "model": str,           # 期望的 probe model
            "got_model": str | None,# 上游实际回的 model（None = 缺字段）
            "missing_fields": list[str],  # EXPECTED_FIELDS 里缺的字段
            "schema_drift": bool,   # missing_fields 非空 或 model 不匹配
        }

    成功条件（ok=True）：
      - SSE 流读到 `response.completed`；
      - 完成帧含 EXPECTED_FIELDS 全部字段；
      - `response.model == probe_model`。
    任一不满足都标 ok=False；latency 仍然记录便于查"是否慢"。
    """
    probe_model = _resolve_probe_model()
    body = _build_probe_request(probe_model)

    started = time.monotonic()
    completed_payload: dict[str, Any] | None = None
    saw_completed = False
    status_code = 0
    error_repr: str | None = None

    # 延迟 import：避免 worker 启动时无关探针把 upstream 模块拉起来（PIL / httpx 都重）。
    from ..upstream import UpstreamError, stream_completion

    try:
        # 用 asyncio.wait_for 给一个硬上限，避免上游 hang 死拖垮 cron 槽位。
        import asyncio

        async def _consume() -> None:
            nonlocal completed_payload, saw_completed
            async for ev in stream_completion(body):
                payload = _extract_completed_payload(ev)
                if payload is not None:
                    completed_payload = payload
                    saw_completed = True
                    # 拿到完成帧就够了；上游通常不再发别的事件，但保险起见 break。
                    break

        await asyncio.wait_for(_consume(), timeout=_PROBE_TIMEOUT_S)
        status_code = 200 if saw_completed else 599  # 没看到完成帧 → 视作 5xx 类故障
    except asyncio.TimeoutError as exc:  # type: ignore[attr-defined]
        error_repr = f"timeout after {_PROBE_TIMEOUT_S}s"
        status_code = 504
        logger.error("upstream_probe.timeout %s", error_repr)
        _sentry_capture(
            "upstream_probe timeout",
            level="error",
            timeout_s=_PROBE_TIMEOUT_S,
            model=probe_model,
        )
        # 重新抛会让 arq 标记任务失败；探针应"软失败"——记录后正常返回。
        del exc
    except UpstreamError as exc:
        error_repr = repr(exc)
        status_code = exc.status_code or 502
        logger.error(
            "upstream_probe.upstream_error status=%s code=%s msg=%s",
            status_code,
            getattr(exc, "error_code", None),
            exc,
        )
        _sentry_capture(
            "upstream_probe upstream_error",
            level="error",
            status=status_code,
            error_code=getattr(exc, "error_code", None),
            model=probe_model,
        )
    except Exception as exc:  # noqa: BLE001
        error_repr = repr(exc)
        status_code = 500
        logger.error("upstream_probe.unexpected_error %s", error_repr)
        _sentry_capture(
            "upstream_probe unexpected_error",
            level="error",
            error=error_repr,
            model=probe_model,
        )

    elapsed_s = time.monotonic() - started
    latency_ms = elapsed_s * 1000.0

    # ---------- 字段校验 + model swap 检测 ----------
    missing: list[str] = []
    got_model: str | None = None
    schema_drift = False

    if completed_payload is not None:
        for k in EXPECTED_FIELDS:
            v = completed_payload.get(k)
            # 注意：output 允许 [] / usage 允许 {}，但字段必须存在；用 `not in` 判存在。
            if k not in completed_payload:
                missing.append(k)
            elif k == "id" and not isinstance(v, str):
                missing.append(k)
        got_model = completed_payload.get("model") if isinstance(completed_payload, dict) else None
        if missing:
            schema_drift = True
        if got_model is not None and got_model != probe_model:
            schema_drift = True
    else:
        # 没拿到完成帧：算 schema 不可观测，标 drift 让告警拉响。
        missing = sorted(EXPECTED_FIELDS)
        schema_drift = True

    ok = (
        saw_completed
        and not missing
        and got_model == probe_model
        and error_repr is None
    )

    # ---------- 上报指标 ----------
    _record_request(status_code=status_code or (200 if ok else 500))
    _record_duration(seconds=elapsed_s)

    # 本地 Gauge 只反映"最近一次"——不是计数器。
    if ok:
        PROBE_LAST_OK.set(time.time())
    PROBE_SCHEMA_DRIFT.set(1 if schema_drift else 0)

    # ---------- schema drift 单独告警 ----------
    if schema_drift and ok is False and saw_completed:
        # 200 但 schema 漂移：单独发一条 warning，便于运维区分"上游挂"和"上游变"。
        logger.warning(
            "upstream_probe.schema_drift missing=%s got_model=%s want_model=%s",
            missing,
            got_model,
            probe_model,
        )
        _sentry_capture(
            "upstream_probe schema_drift",
            level="warning",
            missing_fields=missing,
            got_model=got_model,
            want_model=probe_model,
        )

    result = {
        "ok": ok,
        "latency_ms": round(latency_ms, 2),
        "status": status_code or (200 if ok else 500),
        "model": probe_model,
        "got_model": got_model,
        "missing_fields": missing,
        "schema_drift": schema_drift,
    }
    logger.info("upstream_probe.done %s", result)
    return result


__all__ = [
    "probe_upstream",
    "PROBE_LAST_OK",
    "PROBE_SCHEMA_DRIFT",
    "EXPECTED_FIELDS",
]
