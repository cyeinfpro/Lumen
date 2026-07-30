"""Logging and metrics helpers for upstream calls."""

from __future__ import annotations

import logging
from typing import Any

try:
    from lumen_core.metrics_upstream import (
        record_upstream_duration,
        record_upstream_request,
        record_upstream_tokens,
        record_used_percent,
    )
except Exception:

    def record_upstream_tokens(kind: str, n: int) -> None:
        return None

    def record_upstream_duration(seconds: float, endpoint: str) -> None:
        return None

    def record_upstream_request(status_code: int, endpoint: str) -> None:
        return None

    def record_used_percent(p: int) -> None:
        return None


logger = logging.getLogger(__name__)


def extract_response_meta_headers(
    response_headers: Any,
) -> dict[str, Any]:
    """从上游响应 headers（dict / httpx.Headers）里抽 lumen 关心的元信息。

    缺失字段以 None 占位，方便统一打日志结构化字段。
    """
    if response_headers is None:
        return {"x_request_id": None, "x_codex_primary_used_percent": None}
    try:
        x_req_id = response_headers.get("x-request-id")
    except Exception:  # noqa: BLE001
        x_req_id = None
    try:
        used_pct = response_headers.get("x-codex-primary-used-percent")
    except Exception:  # noqa: BLE001
        used_pct = None
    used_pct_int: int | None = None
    if isinstance(used_pct, str) and used_pct.strip():
        try:
            used_pct_int = int(float(used_pct))
        except (TypeError, ValueError):
            used_pct_int = None
    return {
        "x_request_id": x_req_id if isinstance(x_req_id, str) else None,
        "x_codex_primary_used_percent": used_pct_int,
    }


def log_upstream_call(
    *,
    endpoint: str,
    status: int,
    duration_ms: float,
    trace_id: str,
    response_headers: Any = None,
) -> None:
    """统一的上游 HTTP 调用元信息日志 + Prometheus 埋点。

    endpoint 取值受 prom label 约束：当前固定 `responses` / `responses_compact` /
    `images_generations` / `images_edits`。新增端点请同步更新 metrics_upstream 文档。
    """
    meta = extract_response_meta_headers(response_headers)
    used_pct = meta.get("x_codex_primary_used_percent")
    logger.info(
        "upstream.call endpoint=%s status=%s duration_ms=%.1f trace_id=%s "
        "x_request_id=%s x_codex_primary_used_percent=%s",
        endpoint,
        status,
        duration_ms,
        trace_id,
        meta.get("x_request_id"),
        used_pct,
    )
    try:
        record_upstream_request(status_code=status, endpoint=endpoint)
        record_upstream_duration(
            seconds=max(0.0, duration_ms / 1000.0), endpoint=endpoint
        )
        if isinstance(used_pct, int):
            record_used_percent(p=used_pct)
    except Exception:  # noqa: BLE001
        # metrics 埋点不允许影响主链路；任何异常都吞掉。
        logger.debug("metrics record failed", exc_info=True)


def record_usage(usage: Any) -> None:
    """从上游 `usage` 字段提取 token 计数并写入 Prometheus + 日志。

    上游字段路径（响应或 SSE response.completed.response.usage）：
    - input_tokens / output_tokens / total_tokens
    - input_tokens_details.cached_tokens
    - output_tokens_details.reasoning_tokens
    """
    if not isinstance(usage, dict):
        return
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    in_details = usage.get("input_tokens_details")
    out_details = usage.get("output_tokens_details")
    cached_tokens = (
        in_details.get("cached_tokens") if isinstance(in_details, dict) else None
    )
    reasoning_tokens = (
        out_details.get("reasoning_tokens") if isinstance(out_details, dict) else None
    )

    def _as_int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return max(0, n)

    inp = _as_int(input_tokens)
    outp = _as_int(output_tokens)
    cached = _as_int(cached_tokens)
    reasoning = _as_int(reasoning_tokens)
    total = _as_int(total_tokens)
    logger.info(
        "upstream.usage input_tokens=%s output_tokens=%s cached_tokens=%s "
        "reasoning_tokens=%s total_tokens=%s",
        inp,
        outp,
        cached,
        reasoning,
        total,
    )
    try:
        if inp is not None:
            record_upstream_tokens(kind="input", n=inp)
        if outp is not None:
            record_upstream_tokens(kind="output", n=outp)
        if cached is not None:
            record_upstream_tokens(kind="cached", n=cached)
        if reasoning is not None:
            record_upstream_tokens(kind="reasoning", n=reasoning)
    except Exception:  # noqa: BLE001
        logger.debug("metrics tokens record failed", exc_info=True)
