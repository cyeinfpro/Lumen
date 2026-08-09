from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from redis.exceptions import WatchError
from lumen_core.upstream_billing import (
    mark_upstream_dispatch_proven_no_cost,
    mark_upstream_dispatch_started,
    mark_upstream_response_received,
)

from app import account_limiter, observability, sse_publish, video_artifacts
from app import runtime_settings as worker_runtime_settings
from app.provider_pool import ProviderConfig, ProviderPool
from app.tasks import (
    memory_extraction,
    storyboard_assembly,
)
from app.tasks.context_summary_parts import persistence as context_summary_persistence
from app.tasks.completion_parts import default_runtime as completion_runtime
from app.tasks.completion_parts.contracts import (
    CompletionCommand,
    CompletionOutcome,
    CompletionPhase,
    CompletionResult,
)
from app.tasks.completion_parts.runtime import CompletionRuntime
from app.tasks.generation_parts import event_delivery as generation_event_delivery
from app.tasks.generation_parts import failure as generation_failure
from app.tasks.generation_parts import lease as generation_lease
from app.tasks.generation_parts import queue_claim as generation_queue_claim
from app.tasks.generation_parts import queue_lock as generation_queue_lock
from app.tasks.generation_parts import retry_state as generation_retry_state
from app.tasks.generation_parts import runner as generation_runner
from app.tasks.generation_parts import success as generation_success
from app.tasks.generation_parts.errors import LeaseLost
from app.tasks.generation_parts.default_runtime import build_generation_runtime
from app.tasks.generation_parts.runtime import GenerationRuntime
from app.upstream_parts.upstream_impl import build_image_upstream_runtime
from app.upstream_parts import entrypoints as upstream
from app.tasks.video_generation_parts import default_runtime as video_runtime
from app.tasks.video_generation_parts import submission as video_submission
from app.video_provider_slots import VIDEO_PROVIDER_SLOT_TTL_S
from app.video_upstream_parts.contracts import (
    PollResult,
    SubmitResult,
    VideoUpstreamError,
)


TEST_UPSTREAM_RUNTIME = build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services
TEST_COMPLETION_RUNTIME = completion_runtime.build_completion_runtime(
    image_upstream_runtime=TEST_UPSTREAM_RUNTIME
)


class _TaskServicesHarness:
    def __init__(self, module: Any, ports: Any, **extras: Any) -> None:
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_ports", ports)
        object.__setattr__(self, "_extras", extras)

    def __getattr__(self, name: str) -> Any:
        ports = object.__getattribute__(self, "_ports")
        owner = self._find_owner(ports, name)
        if owner is not None:
            return getattr(owner, name)
        extras = object.__getattribute__(self, "_extras")
        if name in extras:
            return extras[name]
        return getattr(object.__getattribute__(self, "_module"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        ports = object.__getattribute__(self, "_ports")
        owner = self._find_owner(ports, name)
        if owner is not None:
            object.__setattr__(owner, name, value)
            return
        setattr(object.__getattribute__(self, "_module"), name, value)

    @classmethod
    def _find_owner(cls, ports: Any, name: str) -> Any | None:
        if hasattr(ports, name):
            return ports
        if not is_dataclass(ports):
            return None
        for field in fields(ports):
            value = getattr(ports, field.name)
            if (
                is_dataclass(value)
                and type(value).__module__.endswith(".runtime")
                and type(value).__name__.endswith("Ports")
            ):
                owner = cls._find_owner(value, name)
                if owner is not None:
                    return owner
        return None


completion = completion_runtime
video_generation = _TaskServicesHarness(
    video_runtime,
    video_runtime.DEFAULT_VIDEO_GENERATION_RUNTIME.ports,
    PollResult=PollResult,
    SubmitResult=SubmitResult,
    VideoUpstreamError=VideoUpstreamError,
    _VIDEO_PROVIDER_SLOT_TTL_S=VIDEO_PROVIDER_SLOT_TTL_S,
)


@pytest.mark.asyncio
async def test_completion_runtime_scopes_explicit_services() -> None:
    process_services = TEST_COMPLETION_RUNTIME.services
    seen: list[Any] = []

    async def runner(command: CompletionCommand) -> CompletionResult:
        seen.extend((command.task_id, command.redis, command.worker_id))
        return CompletionResult(
            task_id=command.task_id,
            phase=CompletionPhase.COMPLETE,
            outcome=CompletionOutcome.SUCCEEDED,
        )

    runtime = CompletionRuntime(
        services=process_services,
        runner=runner,
        image_upstream_runtime=TEST_UPSTREAM_RUNTIME,
    )
    redis = object()
    await runtime.run(
        CompletionCommand(
            task_id="comp-1",
            redis=redis,  # type: ignore[arg-type]
            worker_id="worker-1",
        )
    )

    assert seen == ["comp-1", redis, "worker-1"]


@pytest.mark.asyncio
async def test_generation_runtime_scopes_explicit_services() -> None:
    default_runtime = build_generation_runtime()
    process_services = default_runtime.deps
    seen: list[Any] = []

    async def runner(
        ctx: dict[str, Any],
        task_id: str,
        services: object,
    ) -> None:
        seen.extend((services, ctx, task_id))

    runtime = GenerationRuntime(
        deps=process_services,
        runner=runner,
        postprocess_runtime=default_runtime.postprocess_runtime,
    )
    ctx = {"redis": object()}
    await runtime.run(ctx, "gen-1")

    assert seen == [process_services, ctx, "gen-1"]


def test_generation_success_event_is_staged_before_commit_and_delivered_after() -> None:
    source = inspect.getsource(generation_success._persist_generation_success)
    stage_idx = source.index("success_delivery = _stage_success_event(")
    commit_idx = source.index("commit_with_adoption_probe(", stage_idx)
    deliver_idx = source.index(
        "await g.events.deliver(state.redis, success_delivery)",
        commit_idx,
    )

    assert stage_idx < commit_idx < deliver_idx


@pytest.mark.asyncio
async def test_generation_sse_failure_is_deferred_without_failing_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deferred: list[dict[str, Any]] = []

    async def fail_publish(*_args: Any, **_kwargs: Any) -> None:
        raise sse_publish.SSEPublishRetryableError(
            stream_key="events:user:user-1",
            event_id="event-1",
            diagnostic_dlq_persisted=True,
        )

    async def persist_for_retry(**kwargs: Any) -> None:
        deferred.append(kwargs)

    monkeypatch.setattr(generation_event_delivery, "_publish_sse_event", fail_publish)
    monkeypatch.setattr(
        generation_event_delivery,
        "_persist_generation_event_for_retry",
        persist_for_retry,
    )

    await generation_event_delivery.publish_event(
        object(),
        "user-1",
        "task:gen-1",
        "generation.started",
        {"generation_id": "gen-1", "message_id": "msg-1"},
    )

    assert deferred == [
        {
            "user_id": "user-1",
            "channel": "task:gen-1",
            "event_name": "generation.started",
            "data": {"generation_id": "gen-1", "message_id": "msg-1"},
        }
    ]


def test_completion_tool_limit_continues_with_tool_choice_none() -> None:
    body = {
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "tools": [{"type": "web_search_preview"}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    fallback = completion._tool_limited_completion_body(body)  # noqa: SLF001

    assert fallback is not body
    assert fallback["tool_choice"] == "none"
    assert fallback["parallel_tool_calls"] is False
    assert "tools" not in fallback
    assert fallback["input"][:-1] == body["input"]
    assert fallback["input"][-1]["content"][0]["text"] == (
        completion._TOOL_LIMIT_FALLBACK_TEXT  # noqa: SLF001
    )


def test_completion_cancelled_response_uses_cancel_branch() -> None:
    with pytest.raises(completion._TaskCancelled, match="upstream response cancelled"):  # noqa: SLF001
        completion._raise_for_terminal_response_event(  # noqa: SLF001
            "response.cancelled",
            {"id": "resp-1"},
        )


@pytest.mark.asyncio
async def test_completion_checks_cancel_before_billing_commit() -> None:
    class CancelledRedis:
        async def get(self, _key: str) -> str:
            return "1"

    with pytest.raises(completion._TaskCancelled, match="before billing settle"):  # noqa: SLF001
        await completion._raise_if_completion_cancelled(  # noqa: SLF001
            CancelledRedis(),
            "comp-1",
            "cancelled before billing settle",
        )


@pytest.mark.asyncio
async def test_completion_abort_iterator_closes_inner_stream() -> None:
    class HangingStream:
        closed = False

        def __aiter__(self) -> "HangingStream":
            return self

        async def __anext__(self) -> dict[str, Any]:
            await asyncio.sleep(60)
            return {"type": "response.output_text.delta", "delta": "late"}

        async def aclose(self) -> None:
            self.closed = True

    stream = HangingStream()
    cancel_requested = asyncio.Event()
    lease_lost = asyncio.Event()
    cancel_requested.set()

    with pytest.raises(completion._TaskCancelled, match="cancelled during stream"):  # noqa: SLF001
        await completion._next_completion_stream_event(  # noqa: SLF001
            stream,
            cancel_requested=cancel_requested,
            lease_lost=lease_lost,
        )
    assert stream.closed is True


@pytest.mark.asyncio
async def test_completion_tool_image_budget_checks_byok_task_with_wallet_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        async def get(self, _model: Any, _task_id: str) -> Any:
            return type(
                "CompletionRow",
                (),
                {
                    "id": "comp-1",
                    "upstream_request": {"billing_retry_count": 1},
                },
            )()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    checked_refs: list[str] = []

    async def wallet_billing_applies(*_args: Any, **kwargs: Any) -> bool:
        checked_refs.append(kwargs["ref_id"])
        return True

    async def billing_enabled() -> bool:
        return True

    async def get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return type("Wallet", (), {"balance_micro": 10})()

    async def held_amount_for_ref(*args: Any, **_kwargs: Any) -> int:
        checked_refs.append(args[3])
        return 5

    async def allow_negative_balance() -> bool:
        return False

    async def resolve_int(*_args: Any) -> int:
        return 20

    monkeypatch.setattr(completion.runtime_settings, "resolve_int", resolve_int)
    monkeypatch.setattr(completion_runtime, "SessionLocal", lambda: Session())
    monkeypatch.setattr(
        completion.worker_billing,
        "_wallet_billing_applies",
        wallet_billing_applies,
    )
    monkeypatch.setattr(completion.worker_billing, "billing_enabled", billing_enabled)
    monkeypatch.setattr(completion.billing_core, "get_wallet", get_wallet)
    monkeypatch.setattr(
        completion.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        completion.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )

    with pytest.raises(completion._CompletionToolInsufficientBalance) as excinfo:  # noqa: SLF001
        await completion._ensure_completion_tool_image_wallet_budget(  # noqa: SLF001
            user_id="user-1",
            task_id="comp-1",
        )

    assert excinfo.value.payload["balance_micro"] == 10
    assert excinfo.value.payload["held_micro"] == 5
    assert checked_refs == ["comp-1:retry:1", "comp-1:retry:1"]


@pytest.mark.asyncio
async def test_completion_tool_image_budget_counts_reserved_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        async def get(self, _model: Any, _task_id: str) -> Any:
            return type("CompletionRow", (), {"id": "comp-1", "upstream_request": {}})()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    wallet_locks: list[bool] = []

    async def resolve_int(*_args: Any) -> int:
        return 100

    async def wallet_billing_applies(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def billing_enabled() -> bool:
        return True

    async def get_wallet(*_args: Any, **kwargs: Any) -> Any:
        wallet_locks.append(bool(kwargs.get("lock")))
        return type("Wallet", (), {"balance_micro": 0})()

    async def held_amount_for_ref(*_args: Any, **_kwargs: Any) -> int:
        return 150

    async def allow_negative_balance() -> bool:
        return False

    monkeypatch.setattr(completion.runtime_settings, "resolve_int", resolve_int)
    monkeypatch.setattr(completion_runtime, "SessionLocal", lambda: Session())
    monkeypatch.setattr(
        completion.worker_billing,
        "_wallet_billing_applies",
        wallet_billing_applies,
    )
    monkeypatch.setattr(completion.worker_billing, "billing_enabled", billing_enabled)
    monkeypatch.setattr(completion.billing_core, "get_wallet", get_wallet)
    monkeypatch.setattr(
        completion.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        completion.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )

    with pytest.raises(completion._CompletionToolInsufficientBalance) as excinfo:  # noqa: SLF001
        await completion._ensure_completion_tool_image_wallet_budget(  # noqa: SLF001
            user_id="user-1",
            task_id="comp-1",
            reserved_micro=100,
        )

    assert excinfo.value.payload["required_micro"] == 100
    assert excinfo.value.payload["cumulative_required_micro"] == 200
    assert excinfo.value.payload["reserved_micro"] == 100
    assert excinfo.value.payload["held_micro"] == 150
    assert wallet_locks == [True]


@pytest.mark.asyncio
async def test_completion_tool_image_budget_skips_wallet_for_zero_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        async def get(self, _model: Any, _task_id: str) -> Any:
            return type(
                "CompletionRow",
                (),
                {
                    "id": "comp-free",
                    "upstream_request": {
                        "billing_rate_multiplier_x10000": 0,
                    },
                },
            )()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    async def resolve_int(*_args: Any) -> int:
        return 100

    async def wallet_billing_applies(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def billing_enabled() -> bool:
        return True

    async def fail_wallet(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("zero-rate tool output must not require wallet balance")

    monkeypatch.setattr(completion.runtime_settings, "resolve_int", resolve_int)
    monkeypatch.setattr(completion_runtime, "SessionLocal", lambda: Session())
    monkeypatch.setattr(
        completion.worker_billing,
        "_wallet_billing_applies",
        wallet_billing_applies,
    )
    monkeypatch.setattr(completion.worker_billing, "billing_enabled", billing_enabled)
    monkeypatch.setattr(completion.billing_core, "get_wallet", fail_wallet)

    reserved = await completion._ensure_completion_tool_image_wallet_budget(  # noqa: SLF001
        user_id="user-1",
        task_id="comp-free",
    )

    assert reserved == 0


def test_completion_tool_image_budget_converts_to_image_tokens() -> None:
    assert (
        completion._image_output_tokens_for_budget(  # noqa: SLF001
            1_000,
            image_output_per_1k_micro=500,
            rate_multiplier_x10000=10_000,
        )
        == 2_000
    )
    assert (
        completion._image_output_tokens_for_budget(  # noqa: SLF001
            1_000,
            image_output_per_1k_micro=500,
            rate_multiplier_x10000=20_000,
        )
        == 1_000
    )
    assert (
        completion._image_output_tokens_for_budget(  # noqa: SLF001
            0,
            image_output_per_1k_micro=500,
        )
        == 0
    )
    with pytest.raises(completion.billing_core.BillingError) as exc_info:
        completion._image_output_tokens_for_budget(  # noqa: SLF001
            1_000,
            image_output_per_1k_micro=0,
        )
    assert exc_info.value.code == "PRICING_MISSING"


def test_generation_retry_delay_is_jittered() -> None:
    helper_source = inspect.getsource(generation_retry_state.retry_delay_seconds)
    runner_source = inspect.getsource(generation_failure._retry_generation)

    assert "jitter" in helper_source.lower()
    assert "random.uniform" in helper_source
    assert "retry_delay_seconds(state.attempt)" in runner_source


def test_provider_pool_weighted_round_robin_honors_weights() -> None:
    pool = ProviderPool()
    group = [
        ProviderConfig(
            name="heavy",
            base_url="https://heavy.example",
            api_key="sk-heavy",
            priority=10,
            weight=3,
        ),
        ProviderConfig(
            name="light",
            base_url="https://light.example",
            api_key="sk-light",
            priority=10,
            weight=1,
        ),
    ]

    first_choices = [pool._weighted_round_robin(group)[0].name for _ in range(8)]

    assert first_choices.count("heavy") == 6
    assert first_choices.count("light") == 2


def test_video_generation_releases_provider_slot_on_terminal_paths() -> None:
    success_source = inspect.getsource(  # noqa: SLF001
        video_generation._finish_success
    )
    failure_source = inspect.getsource(  # noqa: SLF001
        video_generation._finish_terminal_failure
    )
    submit_failure_source = inspect.getsource(  # noqa: SLF001
        video_generation._fail_before_submit
    )
    run_source = inspect.getsource(
        video_generation._run_video_generation_with_lease  # noqa: SLF001
    ) + inspect.getsource(video_submission._submit_fresh_video)

    release_snippet = (
        "_release_provider_slot(redis, release_provider_name, generation.id)"
    )
    assert release_snippet in (success_source + failure_source)
    assert "_release_provider_slot(redis, release_provider_name, task_id)" in (
        submit_failure_source
    )
    assert "attempt.slot_provider_name = provider.name" in run_source
    assert "provider_name=attempt.slot_provider_name" in run_source


def test_video_postprocess_rejects_unvalidated_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_bytes = (
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat"
    )
    monkeypatch.setattr(video_artifacts.shutil, "which", lambda _name: None)

    with pytest.raises(
        video_artifacts.InvalidVideoArtifactError,
        match="ffprobe is required",
    ) as exc_info:
        video_generation._postprocess_video_bytes(video_bytes)  # noqa: SLF001

    diagnostics = exc_info.value.diagnostics
    assert diagnostics["ffprobe_missing"] is True
    assert diagnostics["ffmpeg_missing"] is True
    assert "video_bytes" not in diagnostics
    assert "poster_bytes" not in diagnostics


def test_storyboard_concat_cleans_tempdir_when_ffmpeg_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(storyboard_assembly.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        storyboard_assembly.shutil, "which", lambda _name: "/bin/ffmpeg"
    )

    def timeout_run(*args: Any, **kwargs: Any):
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(storyboard_assembly.subprocess, "run", timeout_run)

    with pytest.raises(subprocess.TimeoutExpired):
        storyboard_assembly._concat_segments_sync([tmp_path / "segment.mp4"])  # noqa: SLF001

    assert list(tmp_path.glob("lumen-storyboard-*")) == []


@pytest.mark.asyncio
async def test_store_video_asset_consumes_postprocess_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_bytes: dict[str, bytes] = {}

    def fake_postprocess(data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
        assert data == b"upstream-video"
        return (
            {
                "video_bytes": b"mp4-bytes",
                "poster_bytes": b"poster-bytes",
                "width": 640,
                "height": 360,
                "duration_ms": 5000,
                "fps": 24.0,
                "has_audio": True,
                "faststart": True,
            },
            {"faststart": True, "probe": {"streams": []}},
        )

    async def fake_put(key: str, data: bytes) -> int:
        stored_bytes[key] = data
        return len(data)

    generation = SimpleNamespace(id="video-1", user_id="user-1")

    monkeypatch.setattr(
        video_generation,
        "_postprocess_video_bytes",
        fake_postprocess,
    )
    monkeypatch.setattr(video_generation.storage, "aput_bytes", fake_put)

    stored = await video_generation._store_video_asset(  # noqa: SLF001
        generation,
        b"upstream-video",
    )

    assert stored.video.storage_key == "u/user-1/v/video-1/output.mp4"
    assert stored.video.poster_storage_key == "u/user-1/v/video-1/poster.jpg"
    assert stored.video.width == 640
    assert stored.video.height == 360
    assert stored.video.duration_ms == 5000
    assert stored.video.has_audio is True
    assert stored.video.faststart is True
    assert stored.diagnostics == {"faststart": True, "probe": {"streams": []}}
    assert stored_bytes == {
        "u/user-1/v/video-1/output.mp4": b"mp4-bytes",
        "u/user-1/v/video-1/poster.jpg": b"poster-bytes",
    }


@pytest.mark.asyncio
async def test_video_generation_fail_before_submit_releases_acquired_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, row: Any) -> None:
            self.row = row

        def scalar_one_or_none(self) -> Any:
            return self.row

    class Session:
        def __init__(self, row: Any) -> None:
            self.row = row
            self.commits = 0
            self.added: list[Any] = []

        async def execute(self, _statement: Any) -> Result:
            return Result(self.row)

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def commit(self) -> None:
            self.commits += 1

    class SessionCtx:
        def __init__(self, row: Any) -> None:
            self.session = Session(row)

        async def __aenter__(self) -> Session:
            return self.session

        async def __aexit__(self, *_args: Any) -> None:
            return None

    row = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        status="queued",
        provider_name="volcano-main",
        progress_stage="queued",
        progress_pct=0,
        error_code=None,
        error_message=None,
        finished_at=None,
    )
    released: list[tuple[str, str]] = []

    async def fake_resolve(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_release(_redis: Any, provider_name: str, task_id: str) -> None:
        released.append((provider_name, task_id))

    monkeypatch.setattr(video_generation, "SessionLocal", lambda: SessionCtx(row))
    monkeypatch.setattr(video_generation, "resolve_video_billing", fake_resolve)
    monkeypatch.setattr(video_generation, "_publish", fake_publish)
    monkeypatch.setattr(video_generation, "_release_provider_slot", fake_release)

    await video_generation._fail_before_submit(  # noqa: SLF001
        object(),
        "video-1",
        RuntimeError("boom"),
    )

    assert released == [("volcano-main", "video-1")]


@pytest.mark.asyncio
async def test_video_generation_stale_epoch_does_not_release_current_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Session:
        async def execute(self, _statement: Any) -> Result:
            return Result()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    released: list[tuple[str, str]] = []

    async def fake_release(
        _redis: Any,
        provider_name: str,
        task_id: str,
    ) -> None:
        released.append((provider_name, task_id))

    monkeypatch.setattr(video_generation, "SessionLocal", Session)
    monkeypatch.setattr(video_generation, "_release_provider_slot", fake_release)

    await video_generation._fail_before_submit(  # noqa: SLF001
        object(),
        "video-1",
        RuntimeError("stale worker"),
        provider_name="volcano-main",
        submission_epoch=1,
    )

    assert released == []


async def _eval_video_provider_slot(redis: Any, *args: Any) -> int:
    (
        _script,
        numkeys,
        active_key,
        exclusive_key,
        task_id,
        now,
        cutoff,
        concurrency,
        wants_exclusive,
        ttl,
    ) = args
    assert numkeys == 2
    now = float(now)
    cutoff = float(cutoff)
    concurrency = int(concurrency)
    wants_exclusive = bool(int(wants_exclusive))
    for key in (active_key, exclusive_key):
        redis.zsets[key] = {
            member: score
            for member, score in redis.zsets.get(key, {}).items()
            if score > cutoff
        }
    active = redis.zsets.setdefault(active_key, {})
    exclusive = redis.zsets.setdefault(exclusive_key, {})
    if task_id in active:
        if wants_exclusive and (
            len(active) != 1
            or len(exclusive) > 1
            or (len(exclusive) == 1 and task_id not in exclusive)
        ):
            return 0
        if not wants_exclusive and exclusive and task_id not in exclusive:
            return 0
    else:
        if wants_exclusive and (active or exclusive):
            return 0
        if not wants_exclusive and exclusive:
            return 0
        if len(active) >= concurrency:
            return 0
    active[task_id] = now
    if hasattr(redis, "expires"):
        redis.expires[active_key] = int(ttl)
    if wants_exclusive:
        exclusive[task_id] = now
        if hasattr(redis, "expires"):
            redis.expires[exclusive_key] = int(ttl)
    return 1


@pytest.mark.asyncio
async def test_video_provider_slot_reacquire_refreshes_same_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlotRedis:
        def __init__(self) -> None:
            self.zsets: dict[str, dict[str, float]] = {
                "video:provider_slot:volcano-main": {"video-1": 990.0}
            }
            self.expires: dict[str, int] = {}

        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        async def zremrangebyscore(
            self, key: str, _start: float, cutoff: float
        ) -> None:
            self.zsets[key] = {
                member: score
                for member, score in self.zsets.get(key, {}).items()
                if score > cutoff
            }

        async def zscore(self, key: str, member: str) -> float | None:
            return self.zsets.get(key, {}).get(member)

        async def zcard(self, key: str) -> int:
            return len(self.zsets.get(key, {}))

        async def zadd(self, key: str, mapping: dict[str, float]) -> None:
            self.zsets.setdefault(key, {}).update(mapping)

        async def expire(self, key: str, ttl: int) -> None:
            self.expires[key] = ttl

        async def eval(self, *args: Any) -> int:
            return await _eval_video_provider_slot(self, *args)

    redis = SlotRedis()
    monkeypatch.setattr(video_generation.time, "time", lambda: 1000.0)

    assert (
        await video_generation._acquire_provider_slot(  # noqa: SLF001
            redis,
            "volcano-main",
            concurrency=1,
            task_id="video-1",
        )
        is True
    )

    key = "video:provider_slot:volcano-main"
    assert redis.zsets[key] == {"video-1": 1000.0}
    assert redis.expires[key] == video_generation._VIDEO_PROVIDER_SLOT_TTL_S  # noqa: SLF001


@pytest.mark.asyncio
async def test_video_provider_exclusive_slot_blocks_mixed_4k_and_standard_work() -> (
    None
):
    class SlotRedis:
        def __init__(self) -> None:
            self.zsets: dict[str, dict[str, float]] = {}

        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        async def zremrangebyscore(
            self,
            key: str,
            _start: float,
            cutoff: float,
        ) -> None:
            self.zsets[key] = {
                member: score
                for member, score in self.zsets.get(key, {}).items()
                if score > cutoff
            }

        async def zscore(self, key: str, member: str) -> float | None:
            return self.zsets.get(key, {}).get(member)

        async def zcard(self, key: str) -> int:
            return len(self.zsets.get(key, {}))

        async def zadd(self, key: str, mapping: dict[str, float]) -> None:
            self.zsets.setdefault(key, {}).update(mapping)

        async def zrem(self, key: str, member: str) -> None:
            self.zsets.setdefault(key, {}).pop(member, None)

        async def expire(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def eval(self, *args: Any) -> int:
            return await _eval_video_provider_slot(self, *args)

    redis = SlotRedis()

    assert await video_generation._acquire_provider_slot(  # noqa: SLF001
        redis,
        "volcano-main",
        concurrency=4,
        task_id="standard-1",
    )
    assert not await video_generation._acquire_provider_slot(  # noqa: SLF001
        redis,
        "volcano-main",
        concurrency=1,
        task_id="4k-1",
        exclusive=True,
    )

    await video_generation._release_provider_slot(  # noqa: SLF001
        redis,
        "volcano-main",
        "standard-1",
    )
    assert await video_generation._acquire_provider_slot(  # noqa: SLF001
        redis,
        "volcano-main",
        concurrency=1,
        task_id="4k-1",
        exclusive=True,
    )
    assert not await video_generation._acquire_provider_slot(  # noqa: SLF001
        redis,
        "volcano-main",
        concurrency=4,
        task_id="standard-2",
    )


@pytest.mark.asyncio
async def test_video_submit_cache_preserves_provider_metadata() -> None:
    class Redis:
        def __init__(self) -> None:
            self.value: str | None = None
            self.ttl: int | None = None

        async def set(self, _key: str, value: str, *, ex: int) -> None:
            self.value = value
            self.ttl = ex

        async def get(self, _key: str) -> str | None:
            return self.value

    redis = Redis()

    await video_generation._store_submit_result(  # noqa: SLF001
        redis,
        "video-1",
        video_generation.SubmitResult(
            provider_task_id="upstream-1",
            raw={"id": "upstream-1"},
        ),
        provider_name="volcano-main",
        provider_kind="volcano",
    )
    cached = await video_generation._load_submit_result(redis, "video-1")  # noqa: SLF001

    assert redis.ttl == video_generation._SUBMIT_RESULT_CACHE_TTL_S  # noqa: SLF001
    assert cached is not None
    assert (
        video_generation._cached_submit_result(  # noqa: SLF001
            cached
        ).provider_task_id
        == "upstream-1"
    )
    assert cached.provider_name == "volcano-main"
    assert cached.provider_kind == "volcano"


@pytest.mark.asyncio
async def test_run_video_generation_releases_lease_on_terminal_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, row: Any) -> None:
            self.row = row

        def scalar_one_or_none(self) -> Any:
            return self.row

    class Session:
        async def execute(self, _statement: Any) -> Result:
            return Result(row)

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    redis = object()
    row = SimpleNamespace(id="video-1", status="succeeded")
    released: list[tuple[str, str]] = []

    async def fake_acquire(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_release(_redis: Any, task_id: str, token: str) -> None:
        released.append((task_id, token))

    monkeypatch.setattr(video_generation, "_acquire_lease", fake_acquire)
    monkeypatch.setattr(video_generation, "_release_lease", fake_release)
    monkeypatch.setattr(video_generation, "SessionLocal", lambda: Session())

    await video_generation.run_video_generation({"redis": redis}, "video-1")

    assert released and released[0][0] == "video-1"


@pytest.mark.asyncio
async def test_run_video_poll_releases_lease_when_submit_is_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, row: Any) -> None:
            self.row = row

        def scalar_one_or_none(self) -> Any:
            return self.row

    class Session:
        async def execute(self, _statement: Any) -> Result:
            return Result(row)

        async def commit(self) -> None:
            return None

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    redis = object()
    row = SimpleNamespace(id="video-1", status="queued", provider_task_id=None)
    enqueued: list[tuple[str, dict[str, Any]]] = []
    released: list[tuple[str, str]] = []

    async def fake_acquire(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_enqueue(_redis: Any, task_id: str, **kwargs: Any) -> None:
        enqueued.append((task_id, kwargs))

    async def fake_release(_redis: Any, task_id: str, token: str) -> None:
        released.append((task_id, token))

    monkeypatch.setattr(video_generation, "_acquire_lease", fake_acquire)
    monkeypatch.setattr(video_generation, "_enqueue_submit", fake_enqueue)
    monkeypatch.setattr(video_generation, "_release_lease", fake_release)
    monkeypatch.setattr(video_generation, "SessionLocal", lambda: Session())

    await video_generation.run_video_poll({"redis": redis}, "video-1")

    assert enqueued and enqueued[0][0] == "video-1"
    assert enqueued[0][1]["defer_s"] == video_generation._POLL_INTERVAL_S  # noqa: SLF001
    assert released and released[0][0] == "video-1"


@pytest.mark.asyncio
async def test_video_poll_window_exhaustion_continues_running_provider_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, row: Any) -> None:
            self.row = row

        def scalar_one_or_none(self) -> Any:
            return self.row

    class Session:
        def __init__(self, row: Any) -> None:
            self.row = row
            self.commits = 0

        async def execute(self, _statement: Any) -> Result:
            return Result(self.row)

        async def commit(self) -> None:
            self.commits += 1

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    now = datetime(2026, 6, 23, 6, 40, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        status="running",
        progress_stage="rendering",
        progress_pct=20,
        poll_count=video_generation._MAX_POLL_COUNT,  # noqa: SLF001
        submitted_at=now - timedelta(seconds=video_generation._MAX_POLL_DURATION_S + 5),  # noqa: SLF001
        upstream_response={},
        next_poll_at=None,
        error_code="poll_timeout",
        error_message="old timeout",
        diagnostics={},
    )
    session = Session(row)
    published: list[dict[str, Any]] = []
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(
        _redis: Any, _generation: Any, event_name: str, **extra: Any
    ) -> None:
        published.append({"event_name": event_name, **extra})

    async def fake_enqueue(_redis: Any, task_id: str, **kwargs: Any) -> None:
        enqueued.append((task_id, kwargs))

    async def fail_terminal(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("running upstream task must not be terminal-failed")

    monkeypatch.setattr(video_generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(video_generation, "_now", lambda: now)
    monkeypatch.setattr(video_generation, "_publish", fake_publish)
    monkeypatch.setattr(video_generation, "_enqueue_poll", fake_enqueue)
    monkeypatch.setattr(video_generation, "_finish_terminal_failure", fail_terminal)

    await video_generation._apply_poll_result(  # noqa: SLF001
        object(),
        "video-1",
        video_generation.PollResult(
            status="running",
            progress=20,
            raw={"id": "provider-task-1", "status": "running"},
        ),
    )

    assert row.status == "running"
    assert row.progress_stage == "rendering"
    assert row.error_code is None
    assert row.error_message is None
    assert row.diagnostics["extended_polling_continues"] is True
    assert row.diagnostics["extended_poll_delay_s"] == (
        video_generation._EXTENDED_POLL_INTERVAL_S  # noqa: SLF001
    )
    assert row.next_poll_at == now + timedelta(
        seconds=video_generation._EXTENDED_POLL_INTERVAL_S  # noqa: SLF001
    )
    assert published[0]["extended_polling"] is True
    assert enqueued == [
        (
            "video-1",
            {"defer_s": video_generation._EXTENDED_POLL_INTERVAL_S},  # noqa: SLF001
        )
    ]


@pytest.mark.asyncio
async def test_video_succeeded_without_result_url_retries_before_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def scalar_one_or_none(self) -> Any:
            return row

    class Session:
        async def execute(self, _statement: Any) -> Result:
            return Result()

        async def commit(self) -> None:
            return None

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    now = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        status="running",
        progress_stage="rendering",
        progress_pct=90,
        poll_count=1,
        submitted_at=now - timedelta(minutes=1),
        upstream_response={},
        next_poll_at=None,
        error_code=None,
        error_message=None,
        diagnostics={},
    )
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_enqueue(_redis: Any, task_id: str, **kwargs: Any) -> None:
        enqueued.append((task_id, kwargs))

    async def fail_terminal(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a transient missing result URL must keep polling")

    monkeypatch.setattr(video_generation, "SessionLocal", lambda: Session())
    monkeypatch.setattr(video_generation, "_now", lambda: now)
    monkeypatch.setattr(video_generation, "_publish", fake_publish)
    monkeypatch.setattr(video_generation, "_enqueue_poll", fake_enqueue)
    monkeypatch.setattr(video_generation, "_finish_terminal_failure", fail_terminal)

    await video_generation._apply_poll_result(  # noqa: SLF001
        object(),
        "video-1",
        video_generation.PollResult(
            status="succeeded",
            progress=100,
            upstream_billable=True,
            raw={"id": "provider-task-1", "status": "succeeded"},
        ),
    )

    assert row.status == "running"
    assert row.progress_pct == 95
    assert row.diagnostics["missing_result_url_attempts"] == 1
    assert row.diagnostics["missing_result_url_retrying"] is True
    assert row.upstream_response["warning"] == "succeeded_without_video_url"
    assert enqueued == [
        (
            "video-1",
            {"defer_s": video_generation._POLL_INTERVAL_S},  # noqa: SLF001
        )
    ]


@pytest.mark.asyncio
async def test_video_provider_tracking_timeout_expires_without_upstream_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, row: Any) -> None:
            self.row = row

        def scalar_one_or_none(self) -> Any:
            return self.row

    class Session:
        async def execute(self, _statement: Any) -> Result:
            return Result(row)

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    now = datetime(2026, 6, 25, 6, 40, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id="video-1",
        status="running",
        progress_stage="rendering",
        progress_pct=20,
        poll_count=video_generation._MAX_POLL_COUNT,  # noqa: SLF001
        submitted_at=now
        - timedelta(seconds=video_generation._MAX_PROVIDER_POLL_DURATION_S + 1),  # noqa: SLF001
    )
    captured: dict[str, Any] = {}

    async def fake_finish_terminal_failure(
        _session: Any,
        _redis: Any,
        generation: Any,
        poll: Any,
        **_kwargs: Any,
    ) -> None:
        captured["generation"] = generation
        captured["poll"] = poll

    monkeypatch.setattr(video_generation, "SessionLocal", lambda: Session())
    monkeypatch.setattr(video_generation, "_now", lambda: now)
    monkeypatch.setattr(
        video_generation, "_finish_terminal_failure", fake_finish_terminal_failure
    )

    await video_generation._apply_poll_result(  # noqa: SLF001
        object(),
        "video-1",
        video_generation.PollResult(
            status="running",
            upstream_billable=None,
            raw={"id": "provider-task-1", "status": "running"},
        ),
    )

    poll = captured["poll"]
    assert captured["generation"] is row
    assert poll.status == "expired"
    assert poll.failure_class == "poll_timeout"
    assert poll.upstream_billable is None
    assert poll.raw["max_provider_poll_duration_s"] == (
        video_generation._MAX_PROVIDER_POLL_DURATION_S  # noqa: SLF001
    )


@pytest.mark.asyncio
async def test_run_video_generation_uses_cached_submit_result_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, row: Any) -> None:
            self.row = row

        def scalar_one_or_none(self) -> Any:
            return self.row

    class Session:
        def __init__(self, row: Any) -> None:
            self.row = row
            self.commits = 0

        async def execute(self, _statement: Any) -> Result:
            return Result(self.row)

        async def commit(self) -> None:
            self.commits += 1

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    redis = object()
    row = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        status="queued",
        provider_task_id=None,
        provider_name="volcano-main",
        provider_kind="volcano",
        cancel_requested_at=None,
        progress_stage="queued",
        progress_pct=0,
        model="seedance",
        action="t2v",
        prompt="hello",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        fps=None,
        generate_audio=False,
        seed=None,
        watermark=False,
        started_at=None,
        attempt=0,
        upstream_response=None,
        submitted_at=None,
        next_poll_at=None,
    )
    released: list[tuple[str, str]] = []
    acquired_slots: list[tuple[str, int, str]] = []
    enqueued: list[str] = []

    async def fake_acquire(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_release(_redis: Any, task_id: str, token: str) -> None:
        released.append((task_id, token))

    async def fake_load_submit_result(_redis: Any, task_id: str) -> Any:
        assert task_id == "video-1"
        return SimpleNamespace(
            provider_task_id="upstream-1",
            raw={"id": "upstream-1"},
        )

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unexpected upstream submit path")

    async def fake_acquire_provider_slot(
        _redis: Any,
        provider_name: str,
        concurrency: int,
        task_id: str,
        *,
        exclusive: bool = False,
    ) -> bool:
        assert exclusive is False
        acquired_slots.append((provider_name, concurrency, task_id))
        return True

    async def fake_enqueue(_redis: Any, task_id: str, **_kwargs: Any) -> None:
        enqueued.append(task_id)

    async def fake_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(video_generation, "_acquire_lease", fake_acquire)
    monkeypatch.setattr(video_generation, "_release_lease", fake_release)
    monkeypatch.setattr(
        video_generation, "_load_submit_result", fake_load_submit_result
    )
    monkeypatch.setattr(video_generation, "_provider_for_generation", fail_if_called)
    monkeypatch.setattr(
        video_generation, "_acquire_provider_slot", fake_acquire_provider_slot
    )
    monkeypatch.setattr(video_generation, "_store_submit_result", fail_if_called)
    monkeypatch.setattr(video_generation, "_enqueue_poll", fake_enqueue)
    monkeypatch.setattr(video_generation, "_publish", fake_publish)
    monkeypatch.setattr(video_generation, "adapter_for_provider", fail_if_called)
    monkeypatch.setattr(video_generation, "SessionLocal", lambda: Session(row))

    await video_generation.run_video_generation({"redis": redis}, "video-1")

    assert row.provider_task_id == "upstream-1"
    assert row.status == "submitted"
    assert acquired_slots == []
    assert enqueued == ["video-1"]
    assert released and released[0][0] == "video-1"


def test_volcano_4k_submit_concurrency_is_clamped_to_one() -> None:
    provider = SimpleNamespace(kind="volcano", concurrency=10)
    generation = SimpleNamespace(resolution="4K")

    assert (
        video_generation._provider_submit_concurrency(provider, generation)  # noqa: SLF001
        == 1
    )
    assert video_generation._provider_submit_is_exclusive(  # noqa: SLF001
        provider,
        generation,
    )


def test_non_4k_and_non_official_submit_concurrency_keep_configuration() -> None:
    assert (
        video_generation._provider_submit_concurrency(  # noqa: SLF001
            SimpleNamespace(kind="volcano", concurrency=10),
            SimpleNamespace(resolution="720p"),
        )
        == 10
    )
    assert (
        video_generation._provider_submit_concurrency(  # noqa: SLF001
            SimpleNamespace(kind="volcano_newapi", concurrency=8),
            SimpleNamespace(resolution="4k"),
        )
        == 8
    )


@pytest.mark.asyncio
async def test_video_enqueue_job_ids_dedupe_same_defer_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        async def enqueue_job(self, name: str, task_id: str, **kwargs: Any) -> None:
            self.calls.append((name, task_id, kwargs))

    redis = Redis()
    monkeypatch.setattr(video_generation.time, "time", lambda: 1000.0)

    await video_generation._enqueue_poll(  # noqa: SLF001
        redis,
        "video-1",
        defer_s=8,
    )
    await video_generation._enqueue_poll(  # noqa: SLF001
        redis,
        "video-1",
        defer_s=8,
    )

    first_job_id = redis.calls[0][2]["_job_id"]
    assert first_job_id == redis.calls[1][2]["_job_id"]

    monkeypatch.setattr(video_generation.time, "time", lambda: 1010.0)
    await video_generation._enqueue_poll(  # noqa: SLF001
        redis,
        "video-1",
        defer_s=8,
    )

    assert redis.calls[2][2]["_job_id"] != first_job_id


@pytest.mark.asyncio
async def test_video_cancel_sent_not_ready_keeps_billability_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, row: Any) -> None:
            self.row = row

        def scalar_one_or_none(self) -> Any:
            return self.row

    class Session:
        async def execute(self, _statement: Any) -> Result:
            return Result(row)

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    row = SimpleNamespace(
        id="video-1",
        status="running",
        cancel_requested_at=datetime.now(timezone.utc),
        diagnostics={"cancel_sent_at": "2026-07-02T00:00:00+00:00"},
    )
    captured: dict[str, Any] = {}

    async def fake_finish_terminal_failure(
        _session: Any,
        _redis: Any,
        generation: Any,
        poll: Any,
        *,
        fallback_error_message: str | None,
    ) -> None:
        captured["generation"] = generation
        captured["poll"] = poll
        captured["fallback_error_message"] = fallback_error_message

    monkeypatch.setattr(video_generation, "SessionLocal", lambda: Session())
    monkeypatch.setattr(
        video_generation,
        "_finish_terminal_failure",
        fake_finish_terminal_failure,
    )

    handled = await video_generation._finish_cancelled_after_provider_poll_error(  # noqa: SLF001
        object(),
        "video-1",
        video_generation.VideoUpstreamError(
            "not ready",
            error_code="upstream_not_ready",
            status_code=404,
        ),
    )

    poll = captured["poll"]
    assert handled is True
    assert captured["generation"] is row
    assert poll.status == "cancelled"
    assert poll.upstream_billable is None
    assert poll.raw["upstream_cost_ambiguous"] is True


@pytest.mark.asyncio
async def test_cancel_checks_fail_closed_for_completion_when_redis_errors() -> None:
    class BrokenRedis:
        calls = 0

        async def get(self, _key: str) -> str:
            self.calls += 1
            raise RuntimeError("redis unavailable")

    redis = BrokenRedis()

    # Redis is the authoritative cancellation channel for both task types. If the
    # read path is unavailable, fail closed so a cancellation cannot be missed.
    assert await generation_lease.is_cancelled(redis, "gen-1") is True
    assert await completion._is_cancelled(redis, "comp-1") is True
    assert redis.calls >= 4


@pytest.mark.asyncio
async def test_completion_cancel_check_honors_redis_cancel_key() -> None:
    class Redis:
        async def get(self, _key: str) -> str:
            return "1"

    assert await completion._is_cancelled(Redis(), "comp-1") is True


@pytest.mark.asyncio
async def test_generation_lease_acquire_uses_nx() -> None:
    class Redis:
        def __init__(self) -> None:
            self.args: tuple[Any, ...] | None = None
            self.kwargs: dict[str, Any] | None = None

        async def set(self, *_args: Any, **kwargs: Any) -> bool:
            self.args = _args
            self.kwargs = kwargs
            return False

    redis = Redis()

    with pytest.raises(LeaseLost):
        await generation_lease.acquire_lease(
            redis,
            "gen-1",
            "worker-1:token-1",
        )

    assert redis.args == ("task:gen-1:lease", "worker-1:token-1")
    assert redis.kwargs is not None
    assert redis.kwargs["nx"] is True


@pytest.mark.asyncio
async def test_generation_release_lease_uses_worker_token_cas() -> None:
    class Redis:
        def __init__(self) -> None:
            self.eval_args: tuple[Any, ...] | None = None

        async def eval(self, *args: Any) -> int:
            self.eval_args = args
            return 1

    redis = Redis()

    await generation_lease.release_lease(
        redis,
        "gen-1",
        "worker-1:token-1",
    )

    assert redis.eval_args is not None
    assert redis.eval_args[1] == 1
    assert redis.eval_args[2] == "task:gen-1:lease"
    assert redis.eval_args[3] == "worker-1:token-1"


@pytest.mark.asyncio
async def test_generation_release_lease_requires_atomic_cas() -> None:
    class RedisWithoutEval:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def get(self, _key: str) -> str:
            return "worker-1"

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            return 1

    redis = RedisWithoutEval()

    await generation_lease.release_lease(redis, "gen-1", "worker-1")

    assert redis.deleted == []


def test_run_generation_uses_unique_lease_token_for_owner_cas() -> None:
    state_source = inspect.getsource(generation_runner._new_run_state)
    acquire_source = inspect.getsource(generation_runner._acquire_generation_lease)

    assert 'lease_token=f"{worker_id}:{new_uuid7()}"' in state_source
    assert "await acquire_lease(" in acquire_source
    assert "state.lease_token" in acquire_source


@pytest.mark.asyncio
async def test_generation_runtime_resource_cleanup_releases_every_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    async def release_slot(
        _redis: Any,
        *,
        task_id: str,
        provider_name: str | None,
        services: Any,
    ) -> None:
        _ = services
        calls.append((f"slot:{task_id}", provider_name))

    async def clear_inflight(
        _redis: Any,
        task_id: str,
        *,
        services: Any,
    ) -> None:
        _ = services
        calls.append((f"inflight:{task_id}", None))

    async def clear_avoided(
        _redis: Any,
        task_id: str,
        *,
        services: Any,
    ) -> None:
        _ = services
        calls.append((f"avoided:{task_id}", None))

    async def release_lease(_redis: Any, task_id: str, token: str) -> None:
        calls.append((f"lease:{task_id}", token))

    monkeypatch.setattr(
        generation_queue_claim,
        "release_image_queue_slot",
        release_slot,
    )
    monkeypatch.setattr(
        generation_queue_claim,
        "inflight_clear",
        clear_inflight,
    )
    monkeypatch.setattr(
        generation_queue_claim,
        "clear_task_avoided_providers",
        clear_avoided,
    )
    monkeypatch.setattr(
        generation_queue_claim,
        "release_lease",
        release_lease,
    )

    await generation_queue_claim.release_generation_runtime_resources(
        object(),
        task_id="gen-1",
        lease_token="worker-1:lease",
        provider_name="provider-1",
        clear_avoided_providers=True,
        services=build_generation_runtime().deps,
    )

    assert calls == [
        ("slot:gen-1", "provider-1"),
        ("inflight:gen-1", None),
        ("avoided:gen-1", None),
        ("lease:gen-1", "worker-1:lease"),
    ]


def test_generation_setup_failure_is_inside_runtime_cleanup_guard() -> None:
    setup = inspect.getsource(generation_runner._start_generation_attempt)
    cleanup = inspect.getsource(generation_runner._cleanup_generation_run)

    assert "except BaseException:" in setup
    assert "await _cleanup_failed_setup(state)" in setup
    assert "asyncio.ensure_future(_critical_release_cleanup(state))" in cleanup
    assert "await asyncio.shield(cleanup)" in cleanup


def test_generation_lease_lost_max_attempts_fails_without_requeue() -> None:
    lease_branch = inspect.getsource(generation_failure.handle_lease_lost)

    max_idx = lease_branch.index("if state.attempt >= MAX_ATTEMPTS:")
    fail_idx = lease_branch.index("mark_generation_attempt_failed")
    retry_idx = lease_branch.index("mark_generation_attempt_retrying")

    assert max_idx < fail_idx < retry_idx
    assert "retriable=False" in lease_branch[fail_idx:retry_idx]
    assert "redis.enqueue_job" not in lease_branch[fail_idx:retry_idx]


def test_generation_attempt_update_can_guard_current_status() -> None:
    from sqlalchemy.dialects import postgresql
    from lumen_core.constants import GenerationStatus

    rendered = str(
        generation_retry_state.generation_attempt_update(
            "gen-1",
            2,
            statuses=(GenerationStatus.RUNNING.value,),
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "generations.id = 'gen-1'" in rendered
    assert "generations.attempt = 2" in rendered
    assert "generations.status IN ('running')" in rendered


def test_generation_success_write_requires_running_status() -> None:
    success_update = inspect.getsource(generation_success._mark_generation_succeeded)

    assert "statuses=RUNNING_GENERATION_STATUSES" in success_update


def test_completion_terminal_writes_require_streaming_status() -> None:
    assert completion._RUNNING_COMPLETION_STATUSES == (  # noqa: SLF001
        completion.CompletionStatus.STREAMING.value,
    )


def test_generation_max_attempts_failure_releases_hold() -> None:
    branch = inspect.getsource(generation_runner._fail_queued_generation)

    assert "generation_attempt_update(" in branch
    assert "statuses=(GenerationStatus.QUEUED.value,)" in branch
    assert "release_or_settle_generation(" in branch
    assert "reason=code" in branch
    assert "state.services.billing.flush_after_commit(" in branch


@pytest.mark.asyncio
async def test_partial_completion_billing_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_charge(_session: Any, _completion: Any) -> None:
        raise RuntimeError("ledger unavailable")

    release_called = False

    async def release(_session: Any, _completion: Any, *, reason: str) -> None:
        nonlocal release_called
        release_called = True

    monkeypatch.setattr(completion.worker_billing, "charge_completion", fail_charge)
    monkeypatch.setattr(completion.worker_billing, "release_completion", release)

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await completion._settle_failed_completion_billing(  # noqa: SLF001
            object(),
            SimpleNamespace(),
            usage_values=(1, 0, 0),
            reason="upstream_failed",
        )

    assert release_called is False


@pytest.mark.asyncio
async def test_zero_usage_failed_completion_releases_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    async def charge(_session: Any, _completion: Any) -> None:
        raise AssertionError("zero usage must not charge")

    async def release(_session: Any, _completion: Any, *, reason: str) -> None:
        released.append(reason)

    monkeypatch.setattr(completion.worker_billing, "charge_completion", charge)
    monkeypatch.setattr(completion.worker_billing, "release_completion", release)

    await completion._settle_failed_completion_billing(  # noqa: SLF001
        object(),
        SimpleNamespace(),
        usage_values=(0, None, 0),
        reason="upstream_failed",
    )

    assert released == ["upstream_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_request", "usage_values", "expected"),
    [
        ({}, (0, 0, 0), "release"),
        (
            mark_upstream_dispatch_started(
                {},
                at="2026-08-03T00:00:00+00:00",
                attempt=1,
                execution_epoch=3,
            ),
            (0, 0, 0),
            "settle:unknown",
        ),
        (
            mark_upstream_response_received(
                {},
                at="2026-08-03T00:00:01+00:00",
                attempt=1,
                execution_epoch=3,
            ),
            (0, 0, 0),
            "settle:unknown",
        ),
        (
            mark_upstream_dispatch_proven_no_cost(
                {},
                at="2026-08-03T00:00:00+00:00",
                attempt=1,
                execution_epoch=3,
            ),
            (0, 0, 0),
            "release",
        ),
        ({}, (1, 0, 0), "charge"),
    ],
)
async def test_failed_completion_billing_uses_dispatch_evidence(
    monkeypatch: pytest.MonkeyPatch,
    upstream_request: dict[str, object],
    usage_values: tuple[int, ...],
    expected: str,
) -> None:
    calls: list[str] = []
    completion_row = SimpleNamespace(
        execution_epoch=3,
        upstream_request=upstream_request,
    )

    async def charge(_session: Any, _row: Any) -> None:
        calls.append("charge")

    async def release(_session: Any, _row: Any, *, reason: str) -> None:
        assert reason == "upstream_failed"
        calls.append("release")

    async def settle(
        _session: Any,
        _row: Any,
        *,
        reason: str,
        knowledge: str,
    ) -> None:
        assert reason == "upstream_failed"
        calls.append(f"settle:{knowledge}")

    monkeypatch.setattr(completion.worker_billing, "charge_completion", charge)
    monkeypatch.setattr(completion.worker_billing, "release_completion", release)
    monkeypatch.setattr(
        completion.worker_billing,
        "settle_completion_unknown_upstream",
        settle,
    )

    await completion._settle_failed_completion_billing(  # noqa: SLF001
        object(),
        completion_row,
        usage_values=usage_values,
        reason="upstream_failed",
    )

    assert calls == [expected]


def test_generation_byok_early_failure_releases_hold_and_guards_status() -> None:
    branch = inspect.getsource(generation_runner._persist_user_runtime_failure)

    assert "generation_attempt_update(" in branch
    assert "GenerationStatus.QUEUED.value" in branch
    assert "GenerationStatus.RUNNING.value" in branch
    assert "release_or_settle_generation(" in branch
    assert "reason=error_code" in branch
    assert "state.services.billing.flush_after_commit(" in branch


@pytest.mark.asyncio
async def test_sse_timestamp_uses_monotonic_wall_clock_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sse_publish.time, "monotonic_ns", lambda: 12_345_000_000)

    assert await sse_publish._monotonic_ts_ms() == (
        sse_publish._MONOTONIC_EPOCH_OFFSET_MS + 12_345
    )


def test_sse_xadd_dedupe_uses_per_event_set_nx_ex() -> None:
    lua = " ".join(sse_publish._XADD_IDEMPOTENT_LUA.split())

    assert "HSET" not in sse_publish._XADD_IDEMPOTENT_LUA
    assert "HGET" not in sse_publish._XADD_IDEMPOTENT_LUA
    assert "redis.call('SET', KEYS[2], ARGV[8], 'NX', 'EX', tonumber(ARGV[5]))" in lua
    assert "local ttl_set = redis.call('EXPIRE', KEYS[1], tonumber(ARGV[6]))" in lua
    assert "return existing" in lua


def test_memory_topic_key_normalizes_unicode() -> None:
    assert memory_extraction._topic_key("Cafe\u0301") == memory_extraction._topic_key(
        "Café"
    )


def test_memory_duplicate_positive_signal_is_capped() -> None:
    memory = SimpleNamespace(positive_signal=19)

    memory_extraction._bump_positive_signal(memory)  # noqa: SLF001
    memory_extraction._bump_positive_signal(memory)  # noqa: SLF001

    assert memory.positive_signal == memory_extraction._MAX_POSITIVE_SIGNAL  # noqa: SLF001


@pytest.mark.asyncio
async def test_memory_llm_extract_logs_usage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Provider:
        name = "mem-chat"
        base_url = "https://mem-chat.example"
        api_key = "sk-chat"
        proxy = None

    class Pool:
        async def select(self, *, purpose: str) -> list[Provider]:
            assert purpose == "chat"
            return [Provider()]

    async def fake_get_pool() -> Pool:
        return Pool()

    async def fake_responses_call(
        body: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        assert kwargs["endpoint_label"] == "responses_memory_extract"
        assert body["store"] is False
        return {
            "output_text": (
                '{"items":[{"type":"preference","content":"用户喜欢简洁回答",'
                '"confidence":0.9,"source_excerpt":"喜欢简洁回答",'
                '"intent_kind":"statement"}]}'
            ),
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }

    from app import provider_pool
    from app.upstream_parts import entrypoints as upstream

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(upstream, "responses_call", fake_responses_call)
    caplog.set_level(logging.INFO, logger="app.tasks.memory_extraction")

    items = await memory_extraction._try_llm_extract(  # noqa: SLF001
        "我喜欢简洁回答",
        explicit_only=False,
    )

    assert len(items) == 1
    assert items[0].content == "用户喜欢简洁回答"
    assert "memory_extraction.llm_usage" in caplog.text
    assert "input_tokens" in caplog.text
    assert "output_tokens" in caplog.text


@pytest.mark.asyncio
async def test_memory_embedding_fallback_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Provider:
        name = "bad-embedding"
        base_url = "https://bad-embedding.example"
        api_key = "sk-embedding"
        proxy = None

    class Pool:
        async def select(self, *, purpose: str) -> list[Provider]:
            assert purpose == "embedding"
            return [Provider()]

    class Response:
        status_code = 503

        def json(self) -> dict[str, Any]:
            return {}

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(memory_extraction.httpx, "AsyncClient", lambda **_kw: Client())
    caplog.set_level(logging.WARNING, logger="app.tasks.memory_extraction")

    vector = await memory_extraction._embedding_vector(  # noqa: SLF001
        {"provider_pool": Pool()},
        "用户喜欢简洁回答",
    )

    assert len(vector) == 3072
    assert "memory_extraction.embedding_fallback" in caplog.text
    assert "bad-embedding" in caplog.text


class _FakeClosableClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("cache_name", "getter_name", "builder_name"),
    [
        ("_proxied_clients", "_get_client", "_build_client"),
        ("_proxied_images_clients", "_get_images_client", "_build_images_client"),
    ],
)
@pytest.mark.asyncio
async def test_proxied_client_cache_is_lru_bounded(
    monkeypatch: pytest.MonkeyPatch,
    cache_name: str,
    getter_name: str,
    builder_name: str,
) -> None:
    await upstream.close_client(runtime=TEST_UPSTREAM_RUNTIME)
    timeout_config = TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig(
        connect=1.0, read=2.0, write=3.0
    )
    built: list[_FakeClosableClient] = []
    cache = getattr(TEST_UPSTREAM_SERVICES.core, cache_name.lstrip("_"))
    cache.clear()

    async def fake_timeout_config() -> TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig:
        return timeout_config

    def fake_builder(
        _timeout_config: TEST_UPSTREAM_SERVICES.lifecycle.TimeoutConfig | None = None,
        *,
        proxy_url: str | None = None,
    ) -> _FakeClosableClient:
        assert proxy_url
        client = _FakeClosableClient()
        built.append(client)
        return client

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle, "resolve_timeout_config", fake_timeout_config
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.lifecycle,
        builder_name.lstrip("_"),
        fake_builder,
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.core, "PROXIED_CLIENT_CLOSE_DELAY_SECONDS", 0.01
    )

    limit = int(TEST_UPSTREAM_SERVICES.core.PROXIED_CLIENT_CACHE_MAX)
    getter = getattr(TEST_UPSTREAM_SERVICES.lifecycle, getter_name.lstrip("_"))
    try:
        for idx in range(limit + 5):
            await getter(f"http://proxy-{idx}.example:8080")

        assert len(cache) <= limit
        assert not any(client.closed for client in built[:5])
        assert len(TEST_UPSTREAM_SERVICES.core.retired_client_close_tasks) == 5  # noqa: SLF001
        close_tasks = list(
            TEST_UPSTREAM_SERVICES.core.retired_client_close_tasks  # noqa: SLF001
        )
        await asyncio.wait_for(
            asyncio.gather(*close_tasks),
            timeout=1.0,
        )
        await asyncio.sleep(0)
        assert any(client.closed for client in built[:5])
        assert not TEST_UPSTREAM_SERVICES.core.retired_client_close_tasks  # noqa: SLF001
    finally:
        await upstream.close_client(runtime=TEST_UPSTREAM_RUNTIME)


@pytest.mark.asyncio
async def test_delayed_client_close_waits_until_idle() -> None:
    class BusyClient:
        def __init__(self) -> None:
            self.closed = False
            self.idle = asyncio.Event()

        async def _wait_until_idle(self, _timeout: float) -> None:
            await self.idle.wait()

        async def aclose(self) -> None:
            self.closed = True

    client = BusyClient()
    close_task = asyncio.create_task(
        TEST_UPSTREAM_SERVICES.lifecycle.delayed_aclose(client, delay=0)
    )  # noqa: SLF001
    await asyncio.sleep(0.01)

    assert client.closed is False

    client.idle.set()
    await asyncio.wait_for(close_task, timeout=1.0)
    assert client.closed is True


@pytest.mark.asyncio
async def test_close_client_closes_retired_clients_without_delay() -> None:
    await upstream.close_client(runtime=TEST_UPSTREAM_RUNTIME)
    client = _FakeClosableClient()
    close_task = TEST_UPSTREAM_SERVICES.lifecycle.schedule_delayed_aclose(client)  # noqa: SLF001

    assert close_task in TEST_UPSTREAM_SERVICES.core.retired_client_close_tasks  # noqa: SLF001

    try:
        await upstream.close_client(runtime=TEST_UPSTREAM_RUNTIME)

        assert client.closed is True
        assert not TEST_UPSTREAM_SERVICES.core.retired_client_close_tasks  # noqa: SLF001
        assert not TEST_UPSTREAM_SERVICES.core.retired_clients  # noqa: SLF001
    finally:
        await upstream.close_client(runtime=TEST_UPSTREAM_RUNTIME)


@pytest.mark.asyncio
async def test_close_retired_clients_waits_out_cancelled_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await upstream.close_client(runtime=TEST_UPSTREAM_RUNTIME)

    class CancelSensitiveClient:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False
            self.closing = False
            self.cancelled = False
            self.calls = 0

        async def aclose(self) -> None:
            self.calls += 1
            if self.closed:
                return
            if self.closing:
                return
            self.closing = True
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.closed = True

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.core, "PROXIED_CLIENT_CLOSE_DELAY_SECONDS", 0
    )
    client = CancelSensitiveClient()
    TEST_UPSTREAM_SERVICES.lifecycle.schedule_delayed_aclose(client)  # noqa: SLF001
    try:
        await asyncio.wait_for(client.started.wait(), timeout=1.0)

        closer = asyncio.create_task(
            TEST_UPSTREAM_SERVICES.lifecycle.close_retired_clients_now()
        )  # noqa: SLF001
        await asyncio.sleep(0.01)
        assert not closer.done()

        client.release.set()
        await asyncio.wait_for(closer, timeout=1.0)
        assert client.closed is True
        assert client.cancelled is False
        assert client.calls >= 1
        assert not TEST_UPSTREAM_SERVICES.core.retired_client_close_tasks  # noqa: SLF001
        assert not TEST_UPSTREAM_SERVICES.core.retired_clients  # noqa: SLF001
    finally:
        client.release.set()
        await upstream.close_client(runtime=TEST_UPSTREAM_RUNTIME)


@pytest.mark.asyncio
async def test_startup_failure_closes_upstream_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    cleanup_calls: list[str] = []

    def raise_startup_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("otel boom")

    async def fake_close_client(*, runtime: object) -> None:
        assert runtime is not None
        cleanup_calls.append("upstream")

    async def fake_billing_shutdown() -> None:
        cleanup_calls.append("billing")

    async def valid_image_job_configuration(*, runtime: object) -> None:
        assert runtime is not None
        return None

    monkeypatch.setattr(
        main,
        "validate_effective_image_job_configuration",
        valid_image_job_configuration,
    )
    monkeypatch.setattr(main, "init_sentry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "init_otel", raise_startup_error)
    monkeypatch.setattr(main, "start_metrics_server", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main, "stop_metrics_server", lambda: cleanup_calls.append("metrics")
    )
    monkeypatch.setattr(main, "close_client", fake_close_client)
    monkeypatch.setattr(main.billing_cache, "shutdown", fake_billing_shutdown)

    with pytest.raises(RuntimeError, match="otel boom"):
        await main._on_startup({"redis": object()})

    assert cleanup_calls == ["upstream"]


@pytest.mark.asyncio
async def test_account_limiter_daily_expiry_stays_in_the_future() -> None:
    class Redis:
        def __init__(self) -> None:
            self.eval_args: tuple[Any, ...] | None = None

        async def eval(self, *args: Any) -> int:
            self.eval_args = args
            return 1

    redis = Redis()
    now = datetime(2026, 5, 16, 23, 59, 59, 900000, tzinfo=timezone.utc).timestamp()

    await account_limiter.record_image_call(redis, "acc1", task_id="task-1", now=now)

    assert redis.eval_args is not None
    day_expire_at = int(redis.eval_args[-1])
    assert day_expire_at > int(now)


@pytest.mark.asyncio
async def test_image_queue_lock_release_uses_owner_cas() -> None:
    class Redis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}
            self.eval_args: tuple[Any, ...] | None = None

        async def set(
            self,
            key: str,
            value: Any,
            **_kwargs: Any,
        ) -> bool:
            self.store[key] = str(value)
            return True

        async def eval(self, *args: Any) -> int:
            self.eval_args = args
            key = str(args[2])
            token = str(args[3])
            if self.store.get(key) != token:
                return 0
            del self.store[key]
            return 1

    redis = Redis()

    async with generation_queue_lock.image_queue_lock(redis):
        redis.store["generation:image_queue:lock"] = "new-owner"

    assert redis.eval_args is not None
    assert redis.eval_args[1] == 1
    assert redis.eval_args[2] == "generation:image_queue:lock"
    assert redis.store["generation:image_queue:lock"] == "new-owner"


@pytest.mark.asyncio
async def test_image_queue_lock_acquisition_failure_is_fail_closed() -> None:
    class Redis:
        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("redis unavailable")

        async def eval(self, *_args: Any) -> int:
            raise AssertionError("release must not run when acquisition fails")

    entered = False
    with pytest.raises(
        generation_queue_lock.UpstreamError,
        match="image queue lock acquisition unavailable",
    ) as exc_info:
        async with generation_queue_lock.image_queue_lock(Redis()):
            entered = True

    assert entered is False
    assert exc_info.value.error_code == generation_queue_lock.EC.LOCAL_QUEUE_FULL.value
    assert exc_info.value.payload["retry_after"] > 0


@pytest.mark.asyncio
async def test_image_queue_lock_transaction_does_not_delete_new_owner() -> None:
    class Redis:
        eval = None

        def __init__(self) -> None:
            self.store: dict[str, str] = {}
            self.version = 0
            self.read_started = asyncio.Event()
            self.allow_release = asyncio.Event()
            self.barrier_used = False
            self.applied_deletes = 0

        async def set(
            self,
            key: str,
            value: Any,
            *,
            nx: bool = False,
            ex: int | None = None,
        ) -> bool:
            _ = ex
            if nx and key in self.store:
                return False
            self.store[key] = str(value)
            self.version += 1
            return True

        def pipeline(self, *, transaction: bool = True) -> Pipeline:
            assert transaction is True
            return Pipeline(self)

        def switch_owner(self, key: str, owner: str) -> None:
            self.store[key] = owner
            self.version += 1

    class Pipeline:
        def __init__(self, redis: Redis) -> None:
            self.redis = redis
            self.watched_version = 0
            self.delete_key: str | None = None

        async def watch(self, _key: str) -> None:
            self.watched_version = self.redis.version

        async def get(self, key: str) -> str | None:
            value = self.redis.store.get(key)
            if not self.redis.barrier_used:
                self.redis.barrier_used = True
                self.redis.read_started.set()
                await self.redis.allow_release.wait()
            return value

        def multi(self) -> None:
            return None

        def delete(self, key: str) -> None:
            self.delete_key = key

        async def execute(self) -> list[int]:
            if self.redis.version != self.watched_version:
                raise WatchError("owner changed")
            assert self.delete_key is not None
            existed = self.delete_key in self.redis.store
            self.redis.store.pop(self.delete_key, None)
            self.redis.version += 1
            self.redis.applied_deletes += int(existed)
            return [int(existed)]

        async def reset(self) -> None:
            return None

    redis = Redis()

    async def acquire_and_release() -> None:
        async with generation_queue_lock.image_queue_lock(redis):
            pass

    release_task = asyncio.create_task(acquire_and_release())
    await redis.read_started.wait()
    redis.switch_owner("generation:image_queue:lock", "new-owner")
    redis.allow_release.set()
    await release_task

    assert redis.store["generation:image_queue:lock"] == "new-owner"
    assert redis.applied_deletes == 0


def test_image_queue_reserve_has_atomic_lua_path() -> None:
    source = inspect.getsource(generation_queue_claim._reserve_provider_slot)

    assert "RESERVE_IMAGE_SLOT_LUA" in source
    assert "lock.eval_fenced(" in source
    assert "lost_result=-1" in source
    assert (
        "redis.call('GET', lock_key) ~= lock_token"
        in generation_queue_claim.RESERVE_IMAGE_SLOT_LUA
    )


def test_db_pool_metrics_sample_pool_state_and_survive_errors() -> None:
    """连接池 gauge 在 scrape 时现采样；采样炸了也不能把 /metrics 打成 500。"""

    class Pool:
        def size(self) -> int:
            return 10

        def checkedin(self) -> int:
            return 6

        def checkedout(self) -> int:
            return 4

        def overflow(self) -> int:
            raise RuntimeError("pool went away")

    observability.bind_db_pool_metrics(SimpleNamespace(pool=Pool()))
    values = {
        sample.labels["state"]: sample.value
        for family in observability.db_pool_connections.collect()
        for sample in family.samples
    }

    assert values["size"] == 10
    assert values["checked_in"] == 6
    assert values["checked_out"] == 4
    # 哨兵值：采样失败要能和「池里真的 0 个溢出连接」区分开。
    assert values["overflow"] == -1


def _hanging_session_local(started: asyncio.Event) -> Any:
    class Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> Any:
            started.set()
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    return Session


@pytest.mark.asyncio
async def test_runtime_settings_db_read_is_bounded_by_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 卡住时 _read_db 必须自己超时，否则 cache.lock 会把全进程 resolve 堵死。"""
    started = asyncio.Event()
    monkeypatch.setattr(
        worker_runtime_settings,
        "SessionLocal",
        _hanging_session_local(started),
    )
    monkeypatch.setattr(worker_runtime_settings, "_DB_TIMEOUT_S", 0.05)
    worker_runtime_settings.invalidate_cache()

    resolution = await asyncio.wait_for(
        worker_runtime_settings._read_db_state("upstream.global_concurrency"),
        5.0,
    )

    assert started.is_set()
    assert resolution.state == "unavailable"


@pytest.mark.asyncio
async def test_runtime_settings_does_not_fall_back_when_db_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 状态未知时不得伪装 missing 并回退到环境配置。"""
    started = asyncio.Event()
    monkeypatch.setattr(
        worker_runtime_settings,
        "SessionLocal",
        _hanging_session_local(started),
    )
    monkeypatch.setattr(worker_runtime_settings, "_DB_TIMEOUT_S", 0.05)
    monkeypatch.setenv("UPSTREAM_GLOBAL_CONCURRENCY", "7")
    worker_runtime_settings.invalidate_cache()

    try:
        resolution = await asyncio.wait_for(
            worker_runtime_settings.resolve_state("upstream.global_concurrency"),
            5.0,
        )
        with pytest.raises(worker_runtime_settings.SettingUnavailable):
            await worker_runtime_settings.resolve_int(
                "upstream.global_concurrency",
                4,
            )
    finally:
        worker_runtime_settings.invalidate_cache()

    assert resolution.state == "unavailable"


@pytest.mark.asyncio
async def test_generation_provider_attach_requires_worker_redis() -> None:
    from app import account_limiter

    state = SimpleNamespace(redis=None)

    with pytest.raises(account_limiter.AccountLimiterUnavailable):
        await generation_runner._attach_provider_pool(state)


@pytest.mark.asyncio
async def test_generation_provider_attach_rejects_unbound_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import account_limiter, provider_pool

    redis = object()

    class RejectingPool:
        def attach_redis(self, _redis: object) -> None:
            return None

        def get_redis(self) -> None:
            return None

    async def get_pool() -> RejectingPool:
        return RejectingPool()

    monkeypatch.setattr(provider_pool, "get_pool", get_pool)

    with pytest.raises(account_limiter.AccountLimiterUnavailable):
        await generation_runner._attach_provider_pool(
            SimpleNamespace(redis=redis)
        )


@pytest.mark.asyncio
async def test_summary_pg_lock_failure_invalidates_connection() -> None:
    """advisory lock 是 session 级的：拿锁后失败必须丢弃连接，否则锁永久泄漏。"""

    class Connection:
        def __init__(self) -> None:
            self.invalidated = False
            self.closed = False

        async def execute(self, *_args: object, **_kwargs: object) -> Any:
            raise RuntimeError("commit boundary blew up after taking the lock")

        async def commit(self) -> None:
            return None

        async def invalidate(self) -> None:
            self.invalidated = True

        async def close(self) -> None:
            self.closed = True

    connection = Connection()

    class Engine:
        async def connect(self) -> Connection:
            return connection

    lock = await context_summary_persistence.acquire_summary_lock(
        None,
        None,
        "conv-1",
        engine=Engine(),
        ttl_s=60,
        lock_factory=lambda *args, **kwargs: SimpleNamespace(),
        logger=logging.getLogger("test"),
    )

    assert lock is None
    assert connection.invalidated is True
    assert connection.closed is True
