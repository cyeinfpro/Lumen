"""Bounded lifecycle-owned recovery loop for durable Canvas plans."""

from __future__ import annotations
import asyncio
import logging
from sqlalchemy import select
from lumen_core.canvas_models import CanvasRun
from ..db import SessionLocal
from .plan_dispatch import dispatch_plan_once

logger = logging.getLogger(__name__)


async def next_plan_ids(cursor):
    try:
        async with SessionLocal() as db:
            return list(
                (
                    await db.execute(
                        select(CanvasRun.id)
                        .where(
                            CanvasRun.kind.in_(("upstream", "selection", "all")),
                            CanvasRun.status.in_(
                                ("queued", "running", "reconciling", "canceling")
                            ),
                            CanvasRun.id > cursor,
                        )
                        .order_by(CanvasRun.id)
                        .limit(4)
                    )
                ).scalars()
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("canvas plan scan deferred error=%s", type(exc).__name__)
        return []


async def canvas_plan_dispatch_loop(stop: asyncio.Event) -> None:
    cursor = ""
    while not stop.is_set():
        rows = await next_plan_ids(cursor)
        cursor = rows[-1] if rows else ""
        for run_id in rows:
            if stop.is_set():
                break
            try:
                async with SessionLocal() as db:
                    await dispatch_plan_once(db, run_id=run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "canvas plan recovery deferred run=%s error=%s",
                    run_id,
                    type(exc).__name__,
                )
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
