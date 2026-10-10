"""Read committed Canvas event identity before best-effort owner notification."""

import asyncio
import logging
from sqlalchemy import select
from lumen_core.canvas_models import CanvasNodeExecution, CanvasRun, CanvasRunEvent
from .canvas_run_notifications import publish_canvas_run_notification
from .db import SessionLocal

logger = logging.getLogger(__name__)


async def notify_committed_execution(redis, execution_id):
    if redis is None:
        return
    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(CanvasRun, CanvasRunEvent)
                    .join(
                        CanvasRunEvent,
                        CanvasRunEvent.run_id == CanvasRun.id,
                    )
                    .join(
                        CanvasNodeExecution,
                        CanvasNodeExecution.id == CanvasRunEvent.execution_id,
                    )
                    .where(
                        CanvasNodeExecution.id == execution_id,
                        CanvasNodeExecution.user_id == CanvasRun.user_id,
                        CanvasNodeExecution.canvas_id == CanvasRun.canvas_id,
                    )
                    .order_by(CanvasRunEvent.seq.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return
            run, event = row
            notice = {
                "user_id": run.user_id,
                "canvas_id": run.canvas_id,
                "run_id": run.id,
                "seq": event.seq,
                "execution_id": event.execution_id,
                "event_type": event.event_type,
            }
        await asyncio.wait_for(
            publish_canvas_run_notification(redis, **notice), timeout=2.0
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "canvas committed event delivery deferred error=%s", type(exc).__name__
        )
