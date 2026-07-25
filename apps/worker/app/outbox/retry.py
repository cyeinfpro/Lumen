"""Atomic retry accounting for transient outbox failures."""

from __future__ import annotations

import logging
from typing import Any

from .contracts import OUTBOX_FAIL_COUNT_HASH, OUTBOX_FAIL_COUNT_TTL_S
from .metrics import outbox_retry_total

logger = logging.getLogger(__name__)

INCR_FAIL_COUNT_LUA = """
local val = redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return val
"""


async def increment_fail_count(
    redis: Any,
    event_id: str,
    *,
    log: logging.Logger = logger,
) -> int:
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
        return 0
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
