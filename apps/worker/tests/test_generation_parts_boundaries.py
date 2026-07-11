from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from app.tasks import generation
from app.tasks.generation_parts import (
    lease,
    lifecycle,
    persistence,
    queue,
    queue_claim,
    request_options,
    retry_state,
)


def test_generation_facade_keeps_extracted_private_symbols() -> None:
    assert generation._acquire_lease is lease.acquire_lease
    assert generation._ready_queued_generation_ids is queue.ready_queued_generation_ids
    assert generation._reserve_image_queue_slot is queue_claim.reserve_image_queue_slot
    assert generation._image_request_options is request_options.image_request_options
    assert generation._retry_delay_seconds is retry_state.retry_delay_seconds
    assert generation._write_generation_files is persistence.write_generation_files
    assert (
        generation._raise_if_generation_interrupted
        is lifecycle.raise_if_generation_interrupted
    )
    assert (
        generation._settle_existing_generated_image
        is lifecycle.settle_existing_generated_image
    )
    assert (
        generation._finalize_running_generation_cancel
        is lifecycle.finalize_running_generation_cancel
    )


def test_generation_parts_do_not_reverse_import_generation_module() -> None:
    for module in (
        lease,
        lifecycle,
        persistence,
        queue,
        queue_claim,
        request_options,
        retry_state,
    ):
        source = inspect.getsource(module)
        assert "from . import generation" not in source
        assert "from .. import generation" not in source


def test_generation_module_size_budgets() -> None:
    generation_path = Path(generation.__file__)
    parts_dir = generation_path.with_name("generation_parts")

    assert len(generation_path.read_text().splitlines()) < 2950
    oversized_parts = {
        path.name: len(path.read_text().splitlines())
        for path in parts_dir.glob("*.py")
        if len(path.read_text().splitlines()) >= 800
    }
    assert oversized_parts == {}


@pytest.mark.asyncio
async def test_lease_part_reads_facade_constants_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class Redis:
        async def set(
            self,
            key: str,
            value: str,
            **kwargs: Any,
        ) -> bool:
            calls.append((key, value, kwargs))
            return True

    monkeypatch.setattr(generation, "_LEASE_TTL_S", 17)

    await lease.acquire_lease(Redis(), "gen-1", "worker:token")

    assert calls == [
        (
            "task:gen-1:lease",
            "worker:token",
            {"ex": 17, "nx": True},
        )
    ]


def test_queue_and_request_parts_resolve_monkeypatches_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation, "_IMAGE_QUEUE_LANE_WEIGHTS", {"lane-a": 7})
    monkeypatch.setattr(
        generation,
        "_aspect_ratio_prompt_constraint",
        lambda _ratio: "\ncustom-constraint",
    )

    assert queue.queue_lane_weight("lane-a") == 7
    assert (
        request_options.prompt_with_aspect_ratio_constraint("prompt", "1:1")
        == "prompt\ncustom-constraint"
    )


def test_retry_part_resolves_facade_helper_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        generation,
        "_base_retry_backoff_seconds",
        lambda _attempt: 10.0,
    )
    monkeypatch.setattr(retry_state.random, "uniform", lambda _low, high: high)

    assert retry_state.retry_delay_seconds(3) == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_lifecycle_checkpoint_uses_late_bound_exception_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateBoundCancelled(BaseException):
        pass

    async def cancelled(_redis: Any, _task_id: str) -> bool:
        return True

    monkeypatch.setattr(generation, "_TaskCancelled", LateBoundCancelled)
    monkeypatch.setattr(generation, "_is_cancelled", cancelled)

    with pytest.raises(LateBoundCancelled, match="post-result guard"):
        await lifecycle.raise_if_generation_interrupted(
            object(),
            "gen-1",
            asyncio.Event(),
            "post-result guard",
        )


def test_lifecycle_settlement_preserves_transaction_order() -> None:
    existing_source = inspect.getsource(lifecycle.settle_existing_generated_image)
    success_start = existing_source.index("_g.logger.info(")
    cancelled = existing_source[:success_start]
    succeeded = existing_source[success_start:]

    cancel_update = cancelled.index("_g._generation_attempt_update(")
    cancel_release = cancelled.index(
        "await _g.worker_billing.release_generation(",
        cancel_update,
    )
    cancel_commit = cancelled.index("await session.commit()", cancel_release)
    cancel_flush = cancelled.index(
        "await _g.worker_billing.flush_balance_cache_refreshes(session)",
        cancel_commit,
    )
    cancel_publish = cancelled.index("_g.EV_GEN_FAILED", cancel_flush)
    assert cancel_update < cancel_release < cancel_commit < cancel_flush < cancel_publish

    success_update = succeeded.index("_g._generation_attempt_update(")
    success_settle = succeeded.index(
        "await _g.worker_billing.settle_generation(",
        success_update,
    )
    success_commit = succeeded.index("await session.commit()", success_settle)
    success_flush = succeeded.index(
        "await _g.worker_billing.flush_balance_cache_refreshes(session)",
        success_commit,
    )
    success_publish = succeeded.index("_g.EV_GEN_SUCCEEDED", success_flush)
    assert (
        success_update
        < success_settle
        < success_commit
        < success_flush
        < success_publish
    )

    cancel_source = inspect.getsource(lifecycle.finalize_running_generation_cancel)
    running_update = cancel_source.index("_g._generation_attempt_update(")
    running_release = cancel_source.index(
        "await _g.worker_billing.release_generation(",
        running_update,
    )
    running_commit = cancel_source.index("await session.commit()", running_release)
    running_flush = cancel_source.index(
        "await _g.worker_billing.flush_balance_cache_refreshes(session)",
        running_commit,
    )
    running_publish = cancel_source.index("_g.EV_GEN_FAILED", running_flush)
    assert (
        running_update
        < running_release
        < running_commit
        < running_flush
        < running_publish
    )


@pytest.mark.asyncio
async def test_queue_claim_cleanup_preserves_release_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def release_slot(*_args: Any, **_kwargs: Any) -> None:
        calls.append("slot")

    async def clear_inflight(*_args: Any, **_kwargs: Any) -> None:
        calls.append("inflight")

    async def clear_avoided(*_args: Any, **_kwargs: Any) -> None:
        calls.append("avoided")

    async def release_lease(*_args: Any, **_kwargs: Any) -> None:
        calls.append("lease")

    monkeypatch.setattr(generation, "_release_image_queue_slot", release_slot)
    monkeypatch.setattr(generation, "_inflight_clear", clear_inflight)
    monkeypatch.setattr(generation, "_clear_avoided_providers", clear_avoided)
    monkeypatch.setattr(generation, "_release_lease", release_lease)

    await queue_claim.release_generation_runtime_resources(
        object(),
        task_id="gen-1",
        lease_token="worker:token",
        provider_name="provider-1",
        clear_avoided_providers=True,
    )

    assert calls == ["slot", "inflight", "avoided", "lease"]


@pytest.mark.asyncio
async def test_persistence_cleanup_uses_facade_delete_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[list[str]] = []

    async def delete_storage_keys(keys: list[str]) -> None:
        deleted.append(keys)

    monkeypatch.setattr(
        generation,
        "_delete_storage_keys",
        delete_storage_keys,
    )

    with pytest.raises(ValueError, match="write failed"):
        async with persistence.cleanup_storage_on_error(["orig", "preview"]):
            raise ValueError("write failed")

    assert deleted == [["orig", "preview"]]


def test_bonus_persistence_keeps_billing_and_publish_boundaries() -> None:
    source = inspect.getsource(persistence.handle_dual_race_bonus_image)

    settle = source.index("_g.worker_billing.settle_generation")
    commit = source.index("await session.commit()", settle)
    flush = source.index("flush_balance_cache_refreshes", commit)
    publish = source.index("await _g.publish_event", flush)
    attached = source.index("_g.EV_GEN_ATTACHED", publish)
    succeeded = source.index("_g.EV_GEN_SUCCEEDED", attached)

    assert settle < commit < flush < publish
    assert attached < succeeded
