from __future__ import annotations

from typing import Any

import pytest

from app.tasks.generation_parts.provider_selector import (
    GenerationDispatchTask,
    PoolProviderSelector,
    ProviderConstraints,
)


TASK = GenerationDispatchTask(
    task_id="gen-selector",
    endpoint_kind="generations",
)
CONSTRAINTS = ProviderConstraints(
    requires_mask=True,
    queue_lane="image:interactive:mask_edit",
    size_bucket="medium",
    cost_class="edit",
)


@pytest.mark.asyncio
async def test_selector_passes_full_typed_contract() -> None:
    calls: list[dict[str, Any]] = []

    class Pool:
        async def select(
            self,
            *,
            route: str,
            task_id: str,
            endpoint_kind: str | None,
            acquire_inflight: bool,
            requires_mask: bool,
            queue_lane: str | None,
            size_bucket: str | None,
            cost_class: str | None,
        ) -> list[str]:
            calls.append(locals())
            return ["provider-a"]

    result = await PoolProviderSelector(Pool()).select(
        task=TASK,
        constraints=CONSTRAINTS,
    )

    assert result == ["provider-a"]
    assert calls[0]["route"] == "image"
    assert calls[0]["task_id"] == "gen-selector"
    assert calls[0]["requires_mask"] is True
    assert calls[0]["acquire_inflight"] is False
    assert calls[0]["queue_lane"] == "image:interactive:mask_edit"


@pytest.mark.asyncio
async def test_selector_does_not_reinterpret_runtime_type_error() -> None:
    calls = 0

    class Pool:
        async def select(
            self,
            *,
            route: str,
            task_id: str,
            endpoint_kind: str | None,
            acquire_inflight: bool,
            requires_mask: bool,
            queue_lane: str | None,
            size_bucket: str | None,
            cost_class: str | None,
        ) -> list[str]:
            nonlocal calls
            calls += 1
            raise TypeError("provider implementation bug")

    selector = PoolProviderSelector(Pool())

    with pytest.raises(TypeError, match="implementation bug"):
        await selector.select(task=TASK, constraints=CONSTRAINTS)

    assert calls == 1


@pytest.mark.asyncio
async def test_legacy_selector_adapter_is_decided_at_construction() -> None:
    calls: list[tuple[str, str]] = []

    class LegacyPool:
        async def select(
            self,
            *,
            route: str,
            task_id: str,
        ) -> list[str]:
            calls.append((route, task_id))
            return ["legacy"]

    selector = PoolProviderSelector(LegacyPool())

    assert await selector.select(task=TASK, constraints=CONSTRAINTS) == ["legacy"]
    assert calls == [("image", "gen-selector")]
