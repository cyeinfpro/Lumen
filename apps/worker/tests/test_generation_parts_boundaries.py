from __future__ import annotations

# ruff: noqa: E402

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lumen_core.constants import GenerationErrorCode
from app.tasks.generation_parts import default_runtime as generation
from .task_parts_runtime_testing import synchronize_module_ports


@pytest.fixture(autouse=True)
def _sync_generation_ports(
    monkeypatch: pytest.MonkeyPatch,
):
    with synchronize_module_ports(
        monkeypatch,
        generation,
        generation.DEFAULT_GENERATION_RUNTIME.ports,
    ):
        yield


from app.tasks.generation_parts import (
    failure,
    lease,
    lifecycle,
    persistence,
    progress,
    queue,
    queue_claim,
    request_options,
    retry_state,
    runner,
    runtime,
    success,
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
        runner,
        runtime,
        progress,
        success,
        failure,
    ):
        source = inspect.getsource(module)
        assert "from . import generation" not in source
        assert "from .. import generation" not in source


def test_generation_module_size_budgets() -> None:
    generation_path = Path(generation.__file__)
    parts_dir = generation_path.with_name("generation_parts")

    assert len(generation_path.read_text().splitlines()) <= 1500
    oversized_parts = {
        path.name: len(path.read_text().splitlines())
        for path in parts_dir.glob("*.py")
        if len(path.read_text().splitlines()) > 1500
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
    success_start = existing_source.index("generation_events_ports().logger.info(")
    cancelled = existing_source[:success_start]
    succeeded = existing_source[success_start:]

    cancel_update = cancelled.index("._generation_attempt_update(")
    cancel_release = cancelled.index(
        "await generation_billing_ports().worker_billing.release_generation(",
        cancel_update,
    )
    cancel_stage = cancelled.index(
        "failure_delivery = generation_events_ports()._stage_generation_event(",
        cancel_release,
    )
    cancel_event = cancelled.index(
        "generation_events_ports().EV_GEN_FAILED",
        cancel_stage,
    )
    cancel_commit = cancelled.index("await session.commit()", cancel_event)
    cancel_flush = cancelled.index(
        "worker_billing.flush_balance_cache_refreshes(",
        cancel_commit,
    )
    cancel_deliver = cancelled.index(
        "await generation_events_ports()._deliver_generation_event(",
        cancel_flush,
    )
    assert (
        cancel_update
        < cancel_release
        < cancel_stage
        < cancel_event
        < cancel_commit
        < cancel_flush
        < cancel_deliver
    )

    success_update = succeeded.index("._generation_attempt_update(")
    success_settle = succeeded.index(
        "await generation_billing_ports().worker_billing.settle_generation(",
        success_update,
    )
    success_stage = succeeded.index(
        "success_delivery = generation_events_ports()._stage_generation_event(",
        success_settle,
    )
    success_event = succeeded.index(
        "generation_events_ports().EV_GEN_SUCCEEDED",
        success_stage,
    )
    success_commit = succeeded.index("await session.commit()", success_event)
    success_flush = succeeded.index(
        "worker_billing.flush_balance_cache_refreshes(",
        success_commit,
    )
    success_deliver = succeeded.index(
        "await generation_events_ports()._deliver_generation_event(",
        success_flush,
    )
    assert (
        success_update
        < success_settle
        < success_stage
        < success_event
        < success_commit
        < success_flush
        < success_deliver
    )

    cancel_source = inspect.getsource(lifecycle.finalize_running_generation_cancel)
    running_update = cancel_source.index("._generation_attempt_update(")
    running_release = cancel_source.index(
        "await generation_billing_ports().worker_billing.release_generation(",
        running_update,
    )
    running_stage = cancel_source.index(
        "failure_delivery = generation_events_ports()._stage_generation_event(",
        running_release,
    )
    running_event = cancel_source.index(
        "generation_events_ports().EV_GEN_FAILED",
        running_stage,
    )
    running_commit = cancel_source.index("await session.commit()", running_event)
    running_flush = cancel_source.index(
        "worker_billing.flush_balance_cache_refreshes(",
        running_commit,
    )
    running_deliver = cancel_source.index(
        "await generation_events_ports()._deliver_generation_event(",
        running_flush,
    )
    assert (
        running_update
        < running_release
        < running_stage
        < running_event
        < running_commit
        < running_flush
        < running_deliver
    )


@pytest.mark.asyncio
async def test_terminal_failure_releases_billing_reservation() -> None:
    message_model = object()
    generation_model = object()
    message = SimpleNamespace(status=None)
    generation_row = SimpleNamespace(id="gen-1")
    release_calls: list[tuple[Any, Any, str]] = []

    class Session:
        async def get(self, model: Any, object_id: str) -> Any:
            if model is message_model:
                assert object_id == "msg-1"
                return message
            if model is generation_model:
                assert object_id == "gen-1"
                return generation_row
            raise AssertionError("unexpected model")

    class Billing:
        async def release_generation(
            self,
            session: Any,
            generation: Any,
            *,
            reason: str,
        ) -> None:
            release_calls.append((session, generation, reason))

    session = Session()
    state = SimpleNamespace(message_id="msg-1", task_id="gen-1")
    ports = SimpleNamespace(
        persistence=SimpleNamespace(
            Message=message_model,
            Generation=generation_model,
        ),
        domain=SimpleNamespace(MessageStatus=generation.MessageStatus),
        billing=SimpleNamespace(worker_billing=Billing()),
    )

    await failure._mark_message_and_release_billing(
        session,
        state,
        "provider_failure",
        ports,
    )

    assert message.status == generation.MessageStatus.FAILED
    assert release_calls == [(session, generation_row, "provider_failure")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        GenerationErrorCode.DIRECT_IMAGE_RESULT_UNKNOWN.value,
        GenerationErrorCode.IMAGE_JOB_RESULT_UNKNOWN.value,
        GenerationErrorCode.NO_IMAGE_RETURNED.value,
    ],
)
async def test_terminal_failure_settles_when_upstream_cost_is_unknown(
    error_code: str,
) -> None:
    """Post-dispatch uncertainty settles the hold instead of refunding cost."""
    message_model = object()
    generation_model = object()
    message = SimpleNamespace(status=None)
    generation_row = SimpleNamespace(id="gen-1", upstream_request={})
    release_calls: list[Any] = []
    settle_calls: list[tuple[Any, Any, str, str]] = []

    class Session:
        async def get(self, model: Any, object_id: str) -> Any:
            if model is message_model:
                return message
            if model is generation_model:
                return generation_row
            raise AssertionError("unexpected model")

    class Billing:
        async def release_generation(
            self, session: Any, generation: Any, *, reason: str
        ) -> None:
            release_calls.append((session, generation, reason))

        async def settle_generation_unknown_upstream(
            self, session: Any, generation: Any, *, reason: str, knowledge: str
        ) -> None:
            settle_calls.append((session, generation, reason, knowledge))

    session = Session()
    state = SimpleNamespace(message_id="msg-1", task_id="gen-1")
    ports = SimpleNamespace(
        persistence=SimpleNamespace(
            Message=message_model,
            Generation=generation_model,
        ),
        domain=SimpleNamespace(MessageStatus=generation.MessageStatus),
        billing=SimpleNamespace(worker_billing=Billing()),
    )

    await failure._mark_message_and_release_billing(
        session,
        state,
        error_code,
        ports,
    )

    assert message.status == generation.MessageStatus.FAILED
    assert release_calls == []
    assert settle_calls == [
        (session, generation_row, error_code, "unknown"),
    ]
    assert generation_row.upstream_request == {}


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
    # 审计 D-1：settle 已从行写入事务里移出去，必须排在 commit 之后并自成事务，
    # 否则 commit 前的异常会把钱包流水连同已产出的上游图一起回滚（平台吸收成本）。
    persistence_source = inspect.getsource(persistence._persist_bonus_generation)
    assert (
        "generation_billing_ports().worker_billing.settle_generation"
        not in persistence_source
    )
    stage = persistence_source.index("_stage_bonus_events(")
    commit = persistence_source.index("await session.commit()", stage)
    settle = persistence_source.index("_settle_bonus_billing(", commit)
    assert stage < commit < settle

    settle_source = inspect.getsource(persistence._settle_bonus_billing)
    settle_call = settle_source.index(
        "generation_billing_ports().worker_billing.settle_generation"
    )
    settle_commit = settle_source.index("await session.commit()", settle_call)
    flush = settle_source.index("flush_balance_cache_refreshes", settle_commit)
    assert settle_call < settle_commit < flush

    stage_source = inspect.getsource(persistence._stage_bonus_events)
    attached = stage_source.index("generation_events_ports().EV_GEN_ATTACHED")
    succeeded = stage_source.index(
        "generation_events_ports().EV_GEN_SUCCEEDED",
        attached,
    )
    assert attached < succeeded

    facade_source = inspect.getsource(persistence.handle_dual_race_bonus_image)
    persist = facade_source.index("_persist_bonus_generation(")
    deliver = facade_source.index(
        "await generation_events_ports()._deliver_generation_events",
        persist,
    )
    assert persist < deliver
