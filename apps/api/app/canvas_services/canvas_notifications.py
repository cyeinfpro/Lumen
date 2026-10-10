"""Notify the existing owner SSE stream after the Canvas transaction commits."""

from __future__ import annotations

import logging
from typing import Any

from lumen_core.canvas_realtime import CANVAS_RUN_EVENT, canvas_run_notification
from lumen_core.constants import user_channel

from ..sse_publish import publish_sse_event

logger = logging.getLogger(__name__)


async def publish_canvas_run_notification(
    redis: Any,
    *,
    user_id: str,
    canvas_id: str,
    run_id: str,
    seq: int,
    execution_id: str | None = None,
    event_type: str,
) -> bool:
    """Return notification success; never retry a provider task on failure.

    Caller must invoke this only after a durable commit and with the run's
    verified database owner. Retrying this notice uses its same stable event ID.
    Cancellation is deliberately not swallowed.
    """
    try:
        if not isinstance(user_id, str) or not user_id or len(user_id) > 128:
            raise ValueError("invalid Canvas owner")
        data = canvas_run_notification(
            canvas_id=canvas_id,
            run_id=run_id,
            seq=seq,
            execution_id=execution_id,
            event_type=event_type,
        )
        await publish_sse_event(
            redis,
            user_id=user_id,
            channel=user_channel(user_id),
            event_name=CANVAS_RUN_EVENT,
            data=data,
        )
    except Exception as exc:  # noqa: BLE001
        # No raw exception text, provider request, URL or ledger data in logs.
        logger.warning("Canvas run notification deferred: %s", type(exc).__name__)
        return False
    return True
