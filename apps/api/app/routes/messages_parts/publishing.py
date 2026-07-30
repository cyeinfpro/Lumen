"""Post-commit message and task publishing orchestration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from ...services import message_submission


async def publish_message_appended(
    *,
    publish_sse_event_fn: Callable[..., Awaitable[Any]],
    publish_sse_events_fn: Callable[..., Awaitable[Any]],
    log: logging.Logger,
    **kwargs: Any,
) -> None:
    await message_submission.publish_message_appended(
        **kwargs,
        publish_sse_event_fn=publish_sse_event_fn,
        publish_sse_events_fn=publish_sse_events_fn,
        log=log,
    )


async def publish_assistant_task(
    *,
    get_arq_pool_fn: Callable[..., Awaitable[Any]],
    publish_sse_event_fn: Callable[..., Awaitable[Any]],
    log: logging.Logger,
    **kwargs: Any,
) -> None:
    await message_submission.publish_assistant_task(
        **kwargs,
        get_arq_pool_fn=get_arq_pool_fn,
        publish_sse_event_fn=publish_sse_event_fn,
        log=log,
    )


async def await_post_commit_publish(
    label: str,
    awaitable: Awaitable[Any],
    *,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str | None,
    await_many_fn: Callable[..., Awaitable[None]],
) -> None:
    await await_many_fn(
        (label, awaitable, assistant_msg_id),
        user_id=user_id,
        conv_id=conv_id,
    )


async def await_post_commit_publishes(
    *publishes: tuple[str, Awaitable[Any], str | None],
    user_id: str,
    conv_id: str,
    timeout_s: float,
    log: logging.Logger,
) -> None:
    async def run_one(
        label: str,
        awaitable: Awaitable[Any],
        assistant_msg_id: str | None,
    ) -> None:
        try:
            await awaitable
        except Exception:
            log.warning(
                "post_commit_publish failed label=%s user=%s conv=%s msg=%s",
                label,
                user_id,
                conv_id,
                assistant_msg_id,
                exc_info=True,
            )

    scheduled = [
        (
            label,
            assistant_msg_id,
            asyncio.ensure_future(run_one(label, awaitable, assistant_msg_id)),
        )
        for label, awaitable, assistant_msg_id in publishes
    ]
    if not scheduled:
        return
    _, pending = await asyncio.wait(
        [task for _, _, task in scheduled],
        timeout=timeout_s,
    )
    if not pending:
        return
    for label, assistant_msg_id, task in scheduled:
        if task not in pending:
            continue
        log.warning(
            "post_commit_publish timeout label=%s user=%s conv=%s msg=%s timeout_s=%.1f",
            label,
            user_id,
            conv_id,
            assistant_msg_id,
            timeout_s,
        )
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
