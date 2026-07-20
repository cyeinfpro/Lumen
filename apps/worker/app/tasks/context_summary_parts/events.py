from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any


async def safe_set_partial(
    redis: Any,
    conv_id: str,
    text: str,
    segment_index: int,
    *,
    ttl_s: int,
    logger: logging.Logger,
) -> None:
    if redis is None:
        return
    try:
        await redis.set(
            f"context:summary:partial:{conv_id}",
            json.dumps(
                {"segment_index": segment_index, "text": text},
                ensure_ascii=False,
            ),
            ex=ttl_s,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_summary.partial_set_failed conv=%s err=%r", conv_id, exc)


async def safe_delete_partial(
    redis: Any,
    conv_id: str,
    *,
    logger: logging.Logger,
) -> None:
    if redis is None:
        return
    try:
        await redis.delete(f"context:summary:partial:{conv_id}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "context_summary.partial_delete_failed conv=%s err=%r",
            conv_id,
            exc,
        )


def manual_compact_job_key(*, user_id: str, conv_id: str, job_id: str) -> str:
    return f"context:manual_compact:job:{user_id}:{conv_id}:{job_id}"


def manual_compact_active_key(*, user_id: str, conv_id: str) -> str:
    return f"context:manual_compact:active:{user_id}:{conv_id}"


async def safe_set_job_status(
    redis: Any,
    key: str,
    payload: dict[str, Any],
    *,
    ttl: int,
    logger: logging.Logger,
) -> None:
    if redis is None:
        return
    try:
        await redis.set(
            key,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            ex=ttl,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("manual_compact.job_status_write_failed key=%s err=%r", key, exc)


async def safe_release_manual_compact_active(
    redis: Any,
    *,
    user_id: str,
    conv_id: str,
    job_id: str,
    script: str,
    logger: logging.Logger,
) -> None:
    if redis is None:
        return
    key = manual_compact_active_key(user_id=user_id, conv_id=conv_id)
    try:
        await redis.eval(script, 1, key, job_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("manual_compact.active_release_failed key=%s err=%r", key, exc)


async def publish_compaction_event(
    redis: Any,
    conv_id: str,
    payload: dict[str, Any],
    *,
    logger: logging.Logger,
) -> None:
    if redis is None:
        return
    try:
        await redis.publish(
            f"lumen:events:conversation:{conv_id}",
            json.dumps(
                {"kind": "context.compaction", **payload},
                ensure_ascii=False,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("compaction.event.publish_failed", extra={"err": repr(exc)})


def redis_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def is_circuit_open(
    redis: Any,
    *,
    state_key: str,
    logger: logging.Logger,
) -> bool:
    if redis is None:
        return False
    try:
        raw = await redis.get(state_key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_summary.circuit_read_failed err=%r", exc)
        return False
    if raw is None:
        return False
    text = redis_text(raw).strip()
    if not text or text.lower() in {"0", "closed", "false"}:
        return False
    try:
        data = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text.lower() == "open"
    return isinstance(data, dict) and str(data.get("state") or "").lower() == "open"


async def record_circuit_sample(
    redis: Any,
    *,
    success: bool,
    threshold_percent: int,
    samples_key: str,
    state_key: str,
    until_key: str,
    sample_window: int,
    min_samples: int,
    ttl_s: int,
    utc_now: Callable[[], datetime],
    logger: logging.Logger,
) -> None:
    if redis is None:
        return
    threshold_percent = min(100, max(1, int(threshold_percent)))
    try:
        await redis.lpush(samples_key, "1" if success else "0")
        await redis.ltrim(samples_key, 0, sample_window - 1)
        await redis.expire(samples_key, ttl_s)
        raw_samples = await redis.lrange(samples_key, 0, -1)
        samples = [redis_text(item) for item in raw_samples or []]
        if len(samples) < min_samples:
            return
        failures = sum(1 for item in samples if item == "0")
        if failures * 100 < len(samples) * threshold_percent:
            return
        until = utc_now() + timedelta(seconds=ttl_s)
        state = json.dumps(
            {"state": "open", "until": until.isoformat()},
            separators=(",", ":"),
        )
        await redis.set(state_key, state, ex=ttl_s)
        await redis.set(until_key, until.isoformat(), ex=ttl_s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_summary.circuit_update_failed err=%r", exc)


async def record_summary_metrics(
    redis: Any,
    *,
    conv_id: str,
    trigger: str,
    outcome: str,
    source_tokens: int,
    summary_tokens: int,
    circuit_threshold_percent: int | None,
    utc_now: Callable[[], datetime],
    record_circuit_sample: Callable[..., Awaitable[None]],
    context_compaction_total: Any,
    logger: logging.Logger,
) -> None:
    if redis is None:
        return
    try:
        hour = utc_now().strftime("%Y%m%d%H")
        key = f"context:metrics:hourly:{hour}"
        pipe = redis.pipeline(transaction=False) if hasattr(redis, "pipeline") else None
        fields = _metric_fields(
            trigger=trigger,
            outcome=outcome,
            source_tokens=source_tokens,
            summary_tokens=summary_tokens,
        )
        if pipe is not None:
            for field, value in fields.items():
                pipe.hincrby(key, field, value)
            pipe.expire(key, 3 * 24 * 3600)
            await pipe.execute()
        else:
            for field, value in fields.items():
                await redis.hincrby(key, field, value)
            await redis.expire(key, 3 * 24 * 3600)
        if circuit_threshold_percent is not None and outcome in {"ok", "failed"}:
            await record_circuit_sample(
                redis,
                success=outcome == "ok",
                threshold_percent=circuit_threshold_percent,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_summary.metrics_failed conv=%s err=%r", conv_id, exc)

    try:
        reason = "manual" if trigger == "manual" else "token_limit"
        context_compaction_total.labels(
            reason=reason,
            trigger=trigger,
            outcome=outcome,
        ).inc()
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_summary.prom_counter_failed conv=%s err=%r", conv_id, exc)


def _metric_fields(
    *,
    trigger: str,
    outcome: str,
    source_tokens: int,
    summary_tokens: int,
) -> dict[str, int]:
    fields = {
        f"{trigger}.{outcome}.count": 1,
        f"{trigger}.{outcome}.source_tokens": max(0, source_tokens),
        f"{trigger}.{outcome}.summary_tokens": max(0, summary_tokens),
    }
    if outcome == "circuit_open":
        fields["fallback_reason:circuit_open"] = 1
    else:
        fields["summary_attempts"] = 1
        if outcome == "ok":
            fields["summary_successes"] = 1
        else:
            fields["summary_failures"] = 1
            reason = "summary_failed" if outcome == "failed" else outcome
            fields[f"fallback_reason:{reason}"] = 1
    if trigger == "manual":
        fields["manual_compact_calls"] = 1
    return fields


def observe_compaction_duration(
    *,
    trigger: str,
    outcome: str,
    elapsed_s: float,
    context_compaction_duration_seconds: Any,
    logger: logging.Logger,
) -> None:
    try:
        reason = "manual" if trigger == "manual" else "token_limit"
        context_compaction_duration_seconds.labels(
            reason=reason,
            outcome=outcome,
        ).observe(max(0.0, elapsed_s))
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_summary.prom_hist_failed err=%r", exc)


def get_redis_from_settings(settings: Any) -> Any:
    if settings is None:
        return None
    if isinstance(settings, dict):
        return settings.get("redis") or settings.get("_redis")
    return getattr(settings, "redis", None) or getattr(settings, "_redis", None)
