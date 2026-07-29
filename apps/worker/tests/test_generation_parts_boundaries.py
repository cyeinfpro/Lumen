from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lumen_core.constants import GenerationAction, GenerationErrorCode, MessageStatus
from lumen_core.models import Generation, Message

from app.tasks.generation_parts import (
    composition,
    composition_ports,
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
from app.tasks.generation_parts.services import (
    GenerationProviderContext,
    GenerationProviderRequest,
    RunGenerationDeps,
)
from app.upstream_parts.image_execution import ImageExecutionRequest


def _generation_deps(**overrides: Any) -> RunGenerationDeps:
    return replace(composition.build_generation_runtime().deps, **overrides)


def test_generation_runtime_composes_typed_semantic_services() -> None:
    generation_runtime = composition.build_generation_runtime()
    deps = generation_runtime.deps

    assert isinstance(generation_runtime, runtime.GenerationRuntime)
    assert isinstance(deps, RunGenerationDeps)
    assert generation_runtime.runner is runner.run_generation
    assert not hasattr(generation_runtime, "ports")
    assert deps.provider.endpoint_kind_for_engine("image2") == "generations"
    assert deps.provider.endpoint_kind_for_engine("responses") == "responses"
    assert tuple(field.name for field in fields(deps)) == (
        "store",
        "artifacts",
        "billing",
        "events",
        "provider",
        "queue",
        "lease",
        "credentials",
        "workflows",
    )
    assert isinstance(deps.store, composition_ports.DefaultGenerationStore)
    assert isinstance(deps.artifacts, composition_ports.DefaultGenerationArtifacts)
    assert isinstance(deps.billing, composition_ports.DefaultGenerationBilling)
    assert isinstance(deps.events, composition_ports.DefaultGenerationEvents)
    assert isinstance(deps.provider, composition_ports.DefaultGenerationProvider)
    assert isinstance(deps.queue, composition_ports.DefaultGenerationQueue)
    assert isinstance(deps.lease, composition_ports.DefaultGenerationLease)
    assert isinstance(deps.credentials, composition_ports.DefaultGenerationCredentials)
    assert isinstance(deps.workflows, composition_ports.DefaultGenerationWorkflows)


def test_route_constraints_use_the_injected_provider_runtime() -> None:
    calls: list[str] = []

    class Provider:
        def endpoint_kind_for_engine(self, engine: str) -> str:
            calls.append(engine)
            return "responses"

    state = SimpleNamespace(
        action=GenerationAction.GENERATE,
        mask_image_id=None,
        raw_image_route="responses",
        image_route="responses",
        route_diagnostics=[],
        services=SimpleNamespace(provider=Provider()),
    )

    runner._apply_route_constraints(state)

    assert calls == ["responses"]
    assert state.endpoint_kind == "responses"


def test_runner_builds_explicit_generation_provider_context() -> None:
    captured: list[GenerationProviderRequest] = []
    image_iter = object()

    class Provider:
        def generate(self, request: GenerationProviderRequest) -> object:
            captured.append(request)
            return image_iter

    state = SimpleNamespace(
        image_request_options={
            "render_quality": "high",
            "output_format": "png",
            "output_compression": None,
            "background": "auto",
            "moderation": "low",
            "responses_model": "gpt-image",
        },
        is_dual_race=False,
        reserved_provider=None,
        prompt_for_upstream="draw",
        resolved=SimpleNamespace(size="1024x1024"),
        requested_image_count=1,
        progress_publisher=None,
        user_id="user-1",
        trace_id="gen-trace",
        attempt=3,
        task_id="generation-1",
        action="generate",
        services=SimpleNamespace(provider=Provider()),
    )

    assert runner._build_image_iterator(state) is image_iter
    assert captured[0].context == GenerationProviderContext(
        trace_id="gen-trace",
        retry_attempt=3,
        quota_task_id="generation-1",
        quota_attempt_epoch=3,
    )


def test_generation_provider_adapter_forwards_typed_upstream_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    image_iter = object()

    def fake_generate_image(request: ImageExecutionRequest) -> object:
        captured["request"] = request
        return image_iter

    monkeypatch.setattr(composition_ports, "generate_image", fake_generate_image)
    provider = composition.build_generation_runtime().deps.provider
    request = GenerationProviderRequest(
        prompt="draw",
        size="1024x1024",
        n=1,
        quality="high",
        output_format="png",
        output_compression=None,
        background="auto",
        moderation="low",
        model="gpt-image",
        progress_callback=None,
        provider_override=None,
        user_id="user-1",
        context=GenerationProviderContext(
            trace_id="gen-trace",
            retry_attempt=3,
            quota_task_id="generation-1",
            quota_attempt_epoch=3,
        ),
    )

    assert provider.generate(request) is image_iter
    upstream_request = captured["request"]
    assert isinstance(upstream_request, ImageExecutionRequest)
    assert upstream_request.upstream_runtime is not None
    request_context = upstream_request.request_context
    assert request_context.upstream_runtime is upstream_request.upstream_runtime
    assert request_context.trace_id == "gen-trace"
    assert request_context.retry_attempt == 3
    assert (
        request_context.next_quota_member("provider-a", "responses")
        == "generation-1:3:1:provider-a:responses"
    )


def test_generation_parts_do_not_reverse_import_generation_module() -> None:
    for module in (
        composition,
        composition_ports,
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
    parts_dir = Path(runtime.__file__).parent
    generation_path = parts_dir.parent / "generation.py"

    assert len(generation_path.read_text().splitlines()) <= 1500
    oversized_parts = {
        path.name: len(path.read_text().splitlines())
        for path in parts_dir.glob("*.py")
        if len(path.read_text().splitlines()) > 1500
    }
    assert oversized_parts == {}


@pytest.mark.asyncio
async def test_lease_service_reads_concrete_module_ttl_at_call_time(
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

    monkeypatch.setattr(lease, "LEASE_TTL_S", 17)

    await _generation_deps().lease.acquire(
        Redis(),
        "gen-1",
        "worker:token",
    )

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
    monkeypatch.setattr(queue, "IMAGE_QUEUE_LANE_WEIGHTS", {"lane-a": 7})
    monkeypatch.setattr(
        request_options,
        "aspect_ratio_prompt_constraint",
        lambda _ratio: "\ncustom-constraint",
    )

    assert (
        queue.queue_lane_weight(
            "lane-a",
            services=_generation_deps(),
        )
        == 7
    )
    assert (
        request_options.prompt_with_aspect_ratio_constraint("prompt", "1:1")
        == "prompt\ncustom-constraint"
    )


def test_retry_part_resolves_concrete_helper_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        retry_state,
        "base_retry_backoff_seconds",
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

    monkeypatch.setattr(lifecycle, "TaskCancelled", LateBoundCancelled)
    monkeypatch.setattr(lifecycle, "is_cancelled", cancelled)

    with pytest.raises(LateBoundCancelled, match="post-result guard"):
        await lifecycle.raise_if_generation_interrupted(
            object(),
            "gen-1",
            asyncio.Event(),
            "post-result guard",
        )


def test_lifecycle_settlement_preserves_transaction_order() -> None:
    existing_source = inspect.getsource(lifecycle.settle_existing_generated_image)
    success_start = existing_source.index("logger.info(")
    cancelled = existing_source[:success_start]
    succeeded = existing_source[success_start:]

    cancel_update = cancelled.index("generation_attempt_update(")
    cancel_release = cancelled.index(
        "await release_or_settle_generation(",
        cancel_update,
    )
    cancel_stage = cancelled.index(
        "failure_delivery = stage_generation_event(",
        cancel_release,
    )
    cancel_event = cancelled.index("EV_GEN_FAILED", cancel_stage)
    cancel_commit = cancelled.index("await session.commit()", cancel_event)
    cancel_flush = cancelled.index(
        "await services.billing.flush_after_commit(",
        cancel_commit,
    )
    cancel_deliver = cancelled.index(
        "await services.events.deliver(",
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

    success_update = succeeded.index("generation_attempt_update(")
    success_settle = succeeded.index(
        "await services.billing.settle(",
        success_update,
    )
    success_stage = succeeded.index(
        "success_delivery = stage_generation_event(",
        success_settle,
    )
    success_event = succeeded.index("EV_GEN_SUCCEEDED", success_stage)
    success_commit = succeeded.index("await session.commit()", success_event)
    success_flush = succeeded.index(
        "await services.billing.flush_after_commit(",
        success_commit,
    )
    success_deliver = succeeded.index(
        "await services.events.deliver(",
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
    running_update = cancel_source.index("generation_attempt_update(")
    running_release = cancel_source.index(
        "await release_or_settle_generation(",
        running_update,
    )
    running_stage = cancel_source.index(
        "failure_delivery = stage_generation_event(",
        running_release,
    )
    running_event = cancel_source.index("EV_GEN_FAILED", running_stage)
    running_commit = cancel_source.index("await session.commit()", running_event)
    running_flush = cancel_source.index(
        "await services.billing.flush_after_commit(",
        running_commit,
    )
    running_deliver = cancel_source.index(
        "await services.events.deliver(",
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
    message = SimpleNamespace(status=None)
    generation_row = SimpleNamespace(id="gen-1")
    release_calls: list[tuple[Any, Any, str]] = []

    class Session:
        async def get(self, model: Any, object_id: str) -> Any:
            if model is Message:
                assert object_id == "msg-1"
                return message
            if model is Generation:
                assert object_id == "gen-1"
                return generation_row
            raise AssertionError("unexpected model")

    class Billing:
        async def release(
            self,
            session: Any,
            generation: Any,
            *,
            reason: str,
        ) -> None:
            release_calls.append((session, generation, reason))

    session = Session()
    state = SimpleNamespace(message_id="msg-1", task_id="gen-1")
    deps = _generation_deps(billing=Billing())

    await failure._mark_message_and_release_billing(
        session,
        state,
        "provider_failure",
        deps,
    )

    assert message.status == MessageStatus.FAILED
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
    message = SimpleNamespace(status=None)
    generation_row = SimpleNamespace(id="gen-1", upstream_request={})
    release_calls: list[Any] = []
    settle_calls: list[tuple[Any, Any, str, str]] = []

    class Session:
        async def get(self, model: Any, object_id: str) -> Any:
            if model is Message:
                return message
            if model is Generation:
                return generation_row
            raise AssertionError("unexpected model")

    class Billing:
        async def release(self, session: Any, generation: Any, *, reason: str) -> None:
            release_calls.append((session, generation, reason))

        async def settle_unknown_upstream(
            self, session: Any, generation: Any, *, reason: str, knowledge: str
        ) -> None:
            settle_calls.append((session, generation, reason, knowledge))

    session = Session()
    state = SimpleNamespace(message_id="msg-1", task_id="gen-1")
    deps = _generation_deps(billing=Billing())

    await failure._mark_message_and_release_billing(
        session,
        state,
        error_code,
        deps,
    )

    assert message.status == MessageStatus.FAILED
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

    monkeypatch.setattr(queue_claim, "release_image_queue_slot", release_slot)
    monkeypatch.setattr(queue_claim, "inflight_clear", clear_inflight)
    monkeypatch.setattr(
        queue_claim,
        "clear_task_avoided_providers",
        clear_avoided,
    )
    monkeypatch.setattr(queue_claim, "release_lease", release_lease)

    await queue_claim.release_generation_runtime_resources(
        object(),
        task_id="gen-1",
        lease_token="worker:token",
        provider_name="provider-1",
        clear_avoided_providers=True,
        services=_generation_deps(),
    )

    assert calls == ["slot", "inflight", "avoided", "lease"]


@pytest.mark.asyncio
async def test_persistence_cleanup_uses_artifact_service() -> None:
    deleted: list[list[str]] = []

    class Artifacts:
        async def delete_files(self, keys: Sequence[str]) -> None:
            deleted.append(list(keys))

    with pytest.raises(ValueError, match="write failed"):
        async with persistence.cleanup_storage_on_error(
            ["orig", "preview"],
            services=_generation_deps(artifacts=Artifacts()),
        ):
            raise ValueError("write failed")

    assert deleted == [["orig", "preview"]]


def test_bonus_persistence_keeps_billing_and_publish_boundaries() -> None:
    # 审计 D-1：settle 已从行写入事务里移出去，必须排在 commit 之后并自成事务，
    # 否则 commit 前的异常会把钱包流水连同已产出的上游图一起回滚（平台吸收成本）。
    persistence_source = inspect.getsource(persistence._persist_bonus_generation)
    stage = persistence_source.index("_stage_bonus_events(")
    commit = persistence_source.index("await session.commit()", stage)
    settle = persistence_source.index("_settle_bonus_billing(", commit)
    assert stage < commit < settle

    settle_source = inspect.getsource(persistence._settle_bonus_billing)
    settle_call = settle_source.index("context.services.billing.settle(")
    settle_commit = settle_source.index("await session.commit()", settle_call)
    flush = settle_source.index(
        "context.services.billing.flush_after_commit(",
        settle_commit,
    )
    assert settle_call < settle_commit < flush

    stage_source = inspect.getsource(persistence._stage_bonus_events)
    attached = stage_source.index("EV_GEN_ATTACHED")
    succeeded = stage_source.index("EV_GEN_SUCCEEDED", attached)
    assert attached < succeeded

    entrypoint_source = inspect.getsource(persistence.handle_dual_race_bonus_image)
    persist = entrypoint_source.index("_persist_bonus_generation(")
    deliver = entrypoint_source.index(
        "await context.services.events.deliver_many(",
        persist,
    )
    assert persist < deliver
