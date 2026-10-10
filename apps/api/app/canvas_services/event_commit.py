"""Best-effort notices only after the exact event transaction has committed."""

from __future__ import annotations

import asyncio
import logging

from ..redis_client import get_redis
from .canvas_notifications import publish_canvas_run_notification

logger = logging.getLogger(__name__)


def remember_canvas_notice(db, *, run, event):
    info = getattr(db, "info", None)
    session = getattr(db, "sync_session", None)
    if not isinstance(info, dict) or session is None:
        return
    transaction = session.get_transaction()
    pending = info.get("canvas_committed_notices")
    if pending is None or pending[0] is not transaction:
        pending = (transaction, {})
        info["canvas_committed_notices"] = pending
    # Coalesce one transaction's hints; the durable sequence replays every event.
    pending[1][run.id] = {
        "user_id": run.user_id,
        "canvas_id": run.canvas_id,
        "run_id": run.id,
        "seq": event.seq,
        "execution_id": event.execution_id,
        "event_type": event.event_type,
    }


async def publish_canvas_notices(notices):
    if not notices:
        return
    try:
        redis = get_redis()
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    publish_canvas_run_notification(redis, **notice)
                    for notice in notices
                )
            ),
            timeout=2.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "canvas committed notification deferred error=%s", type(exc).__name__
        )


async def commit_canvas_events(db):
    info = getattr(db, "info", {})
    session = getattr(db, "sync_session", None)
    pending = info.pop("canvas_committed_notices", None)
    notices = []
    if (
        pending is not None
        and session is not None
        and pending[0] is session.get_transaction()
    ):
        notices = list(pending[1].values())
    # On rollback/unknown commit outcome no notification is sent. The persistent
    # event-batch/snapshot fallback recovers without retrying any paid task.
    await db.commit()
    await publish_canvas_notices(notices)
