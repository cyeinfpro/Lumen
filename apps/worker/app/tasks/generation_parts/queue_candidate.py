"""Generation queue candidate and provider eligibility preparation."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

from .provider_selector import (
    GenerationDispatchTask,
    PoolProviderSelector,
    ProviderConstraints,
)
from .queue import (
    get_avoided_providers,
    image_queue_avoid_key,
    redis_text,
)
from .services import RunGenerationDeps


logger = logging.getLogger(__name__)


async def select_provider_candidates(
    *,
    task_id: str,
    endpoint_kind: str | None,
    requires_mask: bool,
    provider_override: Any | None,
    queue_lane: str | None,
    size_bucket: str | None,
    cost_class: str | None,
    services: RunGenerationDeps,
) -> list[Any]:
    if provider_override is not None:
        return [provider_override]
    selector = getattr(services.queue, "select_providers", None)
    if callable(selector):
        return await selector(
            task_id=task_id,
            endpoint_kind=endpoint_kind,
            requires_mask=requires_mask,
            queue_lane=queue_lane,
            size_bucket=size_bucket,
            cost_class=cost_class,
        )

    from ...provider_pool import get_pool

    adapter = PoolProviderSelector(await get_pool())
    return await adapter.select(
        task=GenerationDispatchTask(
            task_id=task_id,
            endpoint_kind=endpoint_kind,
        ),
        constraints=ProviderConstraints(
            requires_mask=requires_mask,
            queue_lane=queue_lane,
            size_bucket=size_bucket,
            cost_class=cost_class,
        ),
    )


async def filter_avoided_providers(
    redis: Any,
    lock: Any,
    *,
    task_id: str,
    providers: list[Any],
    services: RunGenerationDeps,
) -> list[Any]:
    if not providers:
        return providers
    avoided = await get_avoided_providers(
        redis,
        task_id,
        services=services,
    )
    if not avoided:
        return providers
    filtered = [
        provider
        for provider in providers
        if redis_text(getattr(provider, "name", "")) not in avoided
    ]
    if filtered:
        return filtered
    logger.info(
        "image queue avoid set fully overlaps providers, "
        "ignoring avoid for task=%s avoided=%s",
        task_id,
        sorted(avoided),
    )
    with suppress(Exception):
        await lock.delete_if_owner(image_queue_avoid_key(task_id))
    return providers


__all__ = [
    "filter_avoided_providers",
    "select_provider_candidates",
]
