"""Administrator context-health reporting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, cast

from .admin_context_metrics import (
    context_health_zero,
    fold_context_metrics,
    hourly_context_metric_keys,
    iso_z,
    redis_text,
)


CONTEXT_CIRCUIT_STATE_KEY = "context:circuit:breaker:state"
CONTEXT_CIRCUIT_UNTIL_KEY = "context:circuit:breaker:until"


@dataclass(frozen=True)
class ContextHealthDependencies:
    get_redis: Callable[[], Any]
    now: Callable[[], datetime]
    logger: Any


async def read_context_circuit(
    redis: Any,
    now: datetime,
) -> tuple[str, str | None]:
    raw_state = await redis.get(CONTEXT_CIRCUIT_STATE_KEY)
    state_text = (redis_text(raw_state) or "closed").strip()
    until: str | None = None
    if state_text.startswith("{"):
        try:
            parsed = json.loads(state_text)
            if isinstance(parsed, dict):
                state_text = str(parsed.get("state") or "closed")
                until = redis_text(parsed.get("until"))
        except Exception:
            state_text = "closed"
    if state_text not in {"closed", "open", "half_open"}:
        state_text = "closed"
    if until is None:
        until = redis_text(await redis.get(CONTEXT_CIRCUIT_UNTIL_KEY))
    if until is None and state_text == "open":
        try:
            ttl_ms = await redis.pttl(CONTEXT_CIRCUIT_STATE_KEY)
        except Exception:
            ttl_ms = -1
        if ttl_ms and ttl_ms > 0:
            until = iso_z(now + timedelta(milliseconds=ttl_ms))
    if state_text != "open":
        until = None
    return state_text, until


async def context_health(*, deps: ContextHealthDependencies) -> dict[str, Any]:
    output = context_health_zero()
    redis = deps.get_redis()
    now = deps.now()
    try:
        state, until = await read_context_circuit(redis, now)
        metric_rows = []
        for key in hourly_context_metric_keys(now):
            metric_rows.append(
                await cast(
                    Awaitable[dict[str, str]],
                    redis.hgetall(key),
                )
            )
        output["circuit_breaker_state"] = state
        output["circuit_breaker_until"] = until
        output["last_24h"] = fold_context_metrics(metric_rows)
        return output
    except Exception:
        deps.logger.warning("context health degraded", exc_info=True)
        return context_health_zero(
            degraded=True,
            degrade_reason="redis_unavailable",
        )
