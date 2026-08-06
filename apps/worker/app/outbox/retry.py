"""Atomic retry accounting for transient outbox failures."""

from __future__ import annotations

import logging
from typing import Any

from .contracts import (
    OUTBOX_DLQ_RETRY_DELAY_S,
    OUTBOX_FAIL_COUNT_HASH,
    OUTBOX_FAIL_COUNT_TTL_S,
    OUTBOX_MAX_FAIL_COUNT,
    OUTBOX_RETRY_BASE_DELAY_S,
    OUTBOX_RETRY_MAX_DELAY_S,
)
from .metrics import outbox_retry_total

logger = logging.getLogger(__name__)

INCR_FAIL_COUNT_LUA = """
local val = redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return val
"""


def retry_delay_seconds(
    *,
    delivery_attempts: int,
) -> int:
    attempt = max(1, int(delivery_attempts or 0))
    exponent = min(attempt - 1, 8)
    delay = min(
        OUTBOX_RETRY_MAX_DELAY_S,
        OUTBOX_RETRY_BASE_DELAY_S * (2**exponent),
    )
    if attempt >= OUTBOX_MAX_FAIL_COUNT:
        return max(delay, OUTBOX_DLQ_RETRY_DELAY_S)
    return delay


async def increment_fail_count(
    redis: Any,
    event_id: str,
    *,
    log: logging.Logger = logger,
) -> int | None:
    try:
        value = await redis.eval(
            INCR_FAIL_COUNT_LUA,
            1,
            OUTBOX_FAIL_COUNT_HASH,
            event_id,
            str(OUTBOX_FAIL_COUNT_TTL_S),
        )
    except Exception as exc:  # noqa: BLE001
        outbox_retry_total.labels(outcome="increment_failed").inc()
        log.warning("outbox fail count incr failed event=%s err=%s", event_id, exc)
        return None
    outbox_retry_total.labels(outcome="incremented").inc()
    return int(value or 0)


async def clear_fail_count(
    redis: Any,
    event_id: str,
    *,
    log: logging.Logger = logger,
) -> None:
    try:
        await redis.hdel(OUTBOX_FAIL_COUNT_HASH, event_id)
    except Exception as exc:  # noqa: BLE001
        outbox_retry_total.labels(outcome="clear_failed").inc()
        log.warning(
            "outbox post-commit fail-count cleanup failed event=%s err=%s",
            event_id,
            exc,
        )
        return
    outbox_retry_total.labels(outcome="cleared").inc()
