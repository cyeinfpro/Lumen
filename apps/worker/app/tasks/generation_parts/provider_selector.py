"""Typed provider-selection adapter for generation queue admission."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GenerationDispatchTask:
    task_id: str
    endpoint_kind: str | None


@dataclass(frozen=True, slots=True)
class ProviderConstraints:
    requires_mask: bool
    queue_lane: str | None
    size_bucket: str | None
    cost_class: str | None


class PoolProviderSelector:
    """Adapt one provider-pool signature once, outside the selection hot path."""

    _OPTIONAL_ARGUMENTS = (
        "task_id",
        "endpoint_kind",
        "acquire_inflight",
        "requires_mask",
        "queue_lane",
        "size_bucket",
        "cost_class",
    )

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        signature = inspect.signature(pool.select)
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        self._accepted = frozenset(
            self._OPTIONAL_ARGUMENTS
            if accepts_kwargs
            else (name for name in self._OPTIONAL_ARGUMENTS if name in parameters)
        )
        missing = [
            name
            for name in (
                "requires_mask",
                "queue_lane",
                "size_bucket",
                "cost_class",
            )
            if name not in self._accepted
        ]
        if missing:
            logger.warning(
                "generation provider selector using explicit legacy adapter missing=%s",
                ",".join(missing),
            )

    async def select(
        self,
        *,
        task: GenerationDispatchTask,
        constraints: ProviderConstraints,
    ) -> list[Any]:
        values: dict[str, Any] = {
            "task_id": task.task_id,
            "endpoint_kind": task.endpoint_kind,
            "acquire_inflight": False,
            "requires_mask": constraints.requires_mask,
            "queue_lane": constraints.queue_lane,
            "size_bucket": constraints.size_bucket,
            "cost_class": constraints.cost_class,
        }
        kwargs = {
            name: values[name]
            for name in self._OPTIONAL_ARGUMENTS
            if name in self._accepted
        }
        return await self._pool.select(route="image", **kwargs)


__all__ = [
    "GenerationDispatchTask",
    "PoolProviderSelector",
    "ProviderConstraints",
]
