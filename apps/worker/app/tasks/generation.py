"""Public ARQ entrypoint for image generation."""

from __future__ import annotations

import logging
from typing import Any

from ..generation_dispatch import (
    DISPATCH_CONTEXT_KEY,
    DispatchIdentity,
    consume_generation_dispatch,
    finish_generation_dispatch,
)
from .generation_parts.runtime import GenerationRuntime

logger = logging.getLogger(__name__)


async def run_generation(
    ctx: dict[str, Any],
    task_id: str,
    dispatch_attempt: int | None = None,
    dispatch_revision: int | None = None,
) -> None:
    runtime = ctx.get("generation_runtime")
    if not isinstance(runtime, GenerationRuntime):
        raise TypeError("ctx['generation_runtime'] must be GenerationRuntime")
    identity: DispatchIdentity | None = None
    if dispatch_attempt is not None or dispatch_revision is not None:
        if dispatch_attempt is None or dispatch_revision is None:
            raise TypeError("generation dispatch identity must be complete")
        identity = DispatchIdentity(
            generation_id=task_id,
            attempt=dispatch_attempt,
            revision=dispatch_revision,
        )
        redis = ctx["redis"]
        worker_id = str(ctx.get("worker_id") or ctx.get("job_id") or "worker")
        if not await consume_generation_dispatch(
            redis,
            identity,
            worker_id=worker_id,
        ):
            return
        ctx[DISPATCH_CONTEXT_KEY] = identity
    try:
        await runtime.run(ctx, task_id)
    finally:
        if identity is not None:
            ctx.pop(DISPATCH_CONTEXT_KEY, None)
            try:
                await finish_generation_dispatch(ctx["redis"], identity)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "generation dispatch cleanup failed task=%s attempt=%s revision=%s",
                    task_id,
                    identity.attempt,
                    identity.revision,
                    exc_info=True,
                )


__all__ = ["run_generation"]
