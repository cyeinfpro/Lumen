from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.task_runtime import (
    BillingAction,
    EffectExecutor,
    LeaseState,
    QueueAction,
    execute_effect_batch,
    lease_allows_mutation,
)
from app.tasks.completion_parts.decisions import (
    CompletionDomainState,
    CompletionFrame,
    CompletionFrameKind,
    CompletionStreamSnapshot,
    decide_completion_claim,
    decide_completion_finalize,
    reduce_completion_frame,
)
from app.tasks.completion_parts.default_runtime import DEFAULT_COMPLETION_RUNTIME
from app.tasks.completion_parts.runtime import CompletionRuntime
from app.tasks.generation_parts.decisions import (
    GenerationDomainState,
    GenerationOutcome,
    decide_claim,
    decide_finalize,
    decide_retry,
)
from app.tasks.generation_parts.default_runtime import DEFAULT_GENERATION_RUNTIME
from app.tasks.generation_parts.queue_claim import GenerationResourceLease
from app.tasks.generation_parts.runtime import GenerationRuntime
from app.tasks.video_generation_parts.decisions import (
    VideoDomainState,
    VideoPollOutcome,
    VideoPollPolicy,
    decide_video_poll,
    decide_video_submission_failure,
    video_poll_window_exhausted,
)
from app.tasks.video_generation_parts.default_runtime import (
    DEFAULT_VIDEO_GENERATION_RUNTIME,
)
from app.tasks.video_generation_parts.runtime import VideoGenerationRuntime


class RecordingExecutor(EffectExecutor):
    def __init__(self) -> None:
        self.applied_tokens: set[str] = set()
        self.calls: list[str] = []

    async def was_applied(self, token: Any) -> bool:
        return token.key in self.applied_tokens

    async def apply(self, effect: Any) -> None:
        self.calls.append(effect.name)

    async def mark_applied(self, token: Any) -> None:
        self.applied_tokens.add(token.key)


def test_lease_unknown_is_fail_closed() -> None:
    assert lease_allows_mutation(LeaseState.ACQUIRED)
    assert lease_allows_mutation(LeaseState.HELD)
    assert not lease_allows_mutation(LeaseState.LOST)
    assert not lease_allows_mutation(LeaseState.UNKNOWN)

    decision = decide_claim(
        task_id="gen-1",
        attempt=1,
        current=GenerationDomainState.QUEUED,
        lease=LeaseState.UNKNOWN,
    )
    assert decision.next_state == GenerationDomainState.QUEUED.value
    assert decision.queue_action is QueueAction.DEFER
    assert decision.effects.ordered() == ()


@pytest.mark.asyncio
async def test_terminal_effects_are_ordered_and_idempotent() -> None:
    decision = decide_finalize(
        task_id="gen-1",
        attempt=2,
        current=GenerationDomainState.PERSISTING,
        outcome=GenerationOutcome.SUCCEEDED,
    )
    assert decision.billing_action is BillingAction.SETTLE
    assert [effect.name for effect in decision.effects.ordered()] == [
        "mark_succeeded",
        "settle",
        "generation_succeeded",
        "commit_terminal",
        "deliver_generation_succeeded",
        "release_queue_slot",
        "release_task_lease",
    ]

    executor = RecordingExecutor()
    first = await execute_effect_batch(decision.effects, executor)
    first_calls = list(executor.calls)
    second = await execute_effect_batch(decision.effects, executor)

    assert [effect.name for effect in first] == first_calls
    assert second == ()
    assert executor.calls == first_calls
    assert executor.calls.count("settle") == 1
    assert executor.calls.count("generation_succeeded") == 1


def test_generation_retry_decision_exposes_queue_and_retry_policy() -> None:
    decision = decide_retry(
        task_id="gen-2",
        attempt=3,
        current=GenerationDomainState.RUNNING,
        delay_s=12.5,
        max_attempts=5,
    )
    assert decision.next_state == GenerationDomainState.RETRY_WAIT.value
    assert decision.queue_action is QueueAction.ENQUEUE
    assert decision.retry is not None
    assert decision.retry.delay_s == pytest.approx(12.5)


@pytest.mark.asyncio
async def test_generation_resource_lease_close_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def release(*_args: Any, **_kwargs: Any) -> None:
        calls.append("release")

    monkeypatch.setattr(
        "app.tasks.generation_parts.queue_claim.release_generation_runtime_resources",
        release,
    )
    resource = GenerationResourceLease(
        redis=object(),
        task_id="gen-3",
        lease_token="owner:token",
        provider_name="provider-a",
        clear_avoided_providers=True,
    )

    assert await resource.close()
    assert not await resource.close()
    assert calls == ["release"]


def test_completion_frame_reducer_stops_after_terminal_frame() -> None:
    snapshot = CompletionStreamSnapshot()
    snapshot = reduce_completion_frame(
        snapshot,
        CompletionFrame(CompletionFrameKind.TEXT_DELTA, text="hello"),
    )
    snapshot = reduce_completion_frame(
        snapshot,
        CompletionFrame(CompletionFrameKind.REASONING_DELTA, text="why"),
    )
    snapshot = reduce_completion_frame(
        snapshot,
        CompletionFrame(
            CompletionFrameKind.TOOL_UPDATE,
            tool_update={"id": "tool-1", "status": "running"},
        ),
    )
    snapshot = reduce_completion_frame(
        snapshot,
        CompletionFrame(CompletionFrameKind.COMPLETED),
    )
    frozen = reduce_completion_frame(
        snapshot,
        CompletionFrame(CompletionFrameKind.TEXT_DELTA, text="ignored"),
    )

    assert frozen.text == "hello"
    assert frozen.reasoning == "why"
    assert frozen.tool_updates == ({"id": "tool-1", "status": "running"},)
    assert frozen.terminal is CompletionFrameKind.COMPLETED


def test_completion_claim_and_finalize_invariants() -> None:
    deferred = decide_completion_claim(
        task_id="comp-1",
        attempt=1,
        current=CompletionDomainState.QUEUED,
        lease=LeaseState.UNKNOWN,
    )
    assert deferred.queue_action is QueueAction.DEFER

    completed = decide_completion_finalize(
        task_id="comp-1",
        attempt=1,
        current=CompletionDomainState.STREAMING,
        terminal=CompletionFrameKind.COMPLETED,
    )
    assert completed.next_state == CompletionDomainState.SUCCEEDED.value
    assert completed.billing_action is BillingAction.CHARGE

    cancelled = decide_completion_finalize(
        task_id="comp-1",
        attempt=2,
        current=CompletionDomainState.STREAMING,
        terminal=CompletionFrameKind.CANCELLED,
    )
    assert cancelled.next_state == CompletionDomainState.CANCELLED.value
    assert cancelled.billing_action is BillingAction.RELEASE


def test_video_submission_unknown_never_resubmits() -> None:
    decision = decide_video_submission_failure(
        task_id="video-1",
        attempt=1,
        outcome_unknown=True,
        retryable=True,
        retry_delay_s=8,
        max_attempts=4,
    )
    assert decision.next_state == VideoDomainState.SUBMIT_UNKNOWN.value
    assert decision.queue_action is QueueAction.DEFER
    assert all(
        effect.name != "enqueue_video_submit" for effect in decision.effects.ordered()
    )


def test_video_poll_policy_uses_explicit_clock_and_terminal_invariants() -> None:
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    policy = VideoPollPolicy(interval_s=8, max_count=3, max_duration_s=60)
    assert video_poll_window_exhausted(
        submitted_at=now,
        poll_count=3,
        now=now,
        policy=policy,
    )
    success = decide_video_poll(
        task_id="video-2",
        attempt=1,
        outcome=VideoPollOutcome.SUCCEEDED,
    )
    assert success.next_state == VideoDomainState.SUCCEEDED.value
    assert success.billing_action is BillingAction.SETTLE


@pytest.mark.asyncio
async def test_explicit_runtimes_delegate_without_parent_monkeypatches() -> None:
    calls: list[tuple[str, str]] = []

    async def generation_runner(_ctx: dict[str, Any], task_id: str) -> None:
        calls.append(("generation", task_id))

    async def completion_runner(_ctx: dict[str, Any], task_id: str) -> None:
        calls.append(("completion", task_id))

    async def video_submission(_ctx: dict[str, Any], task_id: str) -> None:
        calls.append(("video-submit", task_id))

    async def video_poll(_ctx: dict[str, Any], task_id: str) -> None:
        calls.append(("video-poll", task_id))

    async def video_reconcile(_ctx: dict[str, Any]) -> int:
        calls.append(("video-reconcile", "cron"))
        return 7

    generation_runtime = GenerationRuntime(
        ports=replace(DEFAULT_GENERATION_RUNTIME.ports),
        runner=generation_runner,
    )
    completion_runtime = CompletionRuntime(
        ports=replace(DEFAULT_COMPLETION_RUNTIME.ports),
        runner=completion_runner,
    )
    video_runtime = VideoGenerationRuntime(
        ports=replace(DEFAULT_VIDEO_GENERATION_RUNTIME.ports),
        submission=video_submission,
        polling=video_poll,
        reconciliation=video_reconcile,
    )

    await generation_runtime.run({}, "gen")
    await completion_runtime.run({}, "comp")
    await video_runtime.run_submission({}, "vid-submit")
    await video_runtime.run_poll({}, "vid-poll")
    assert await video_runtime.reconcile({}) == 7
    assert calls == [
        ("generation", "gen"),
        ("completion", "comp"),
        ("video-submit", "vid-submit"),
        ("video-poll", "vid-poll"),
        ("video-reconcile", "cron"),
    ]


def test_owned_task_parts_have_no_dynamic_parent_facades() -> None:
    task_root = Path(__file__).parents[1] / "app" / "tasks"
    paths = [
        task_root / "generation.py",
        task_root / "completion.py",
        task_root / "video_generation.py",
        *sorted((task_root / "generation_parts").glob("*.py")),
        *sorted((task_root / "completion_parts").glob("*.py")),
        *sorted((task_root / "video_generation_parts").glob("*.py")),
    ]
    forbidden = ("sys.modules", "globals()", "_facade", "_g.")
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, f"{path.name} still contains {marker}"

    assert len((task_root / "generation.py").read_text().splitlines()) < 250
    assert len((task_root / "completion.py").read_text().splitlines()) < 250
    assert len((task_root / "video_generation.py").read_text().splitlines()) < 250
