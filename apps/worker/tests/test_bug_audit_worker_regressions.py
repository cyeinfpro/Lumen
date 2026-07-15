from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app import account_limiter, sse_publish, upstream, video_artifacts
from app.provider_pool import ProviderConfig, ProviderPool
from app.tasks import (
    completion,
    generation,
    memory_extraction,
    storyboard_assembly,
    video_generation,
)
from app.tasks.generation_parts import lifecycle as generation_lifecycle


def test_completion_charge_uses_same_session_before_success_commit() -> None:
    source = inspect.getsource(completion.run_completion)
    charge_call = "await worker_billing.charge_completion(session, comp_for_billing)"

    charge_idx = source.index(charge_call)
    commit_idx = source.index("await session.commit()", charge_idx)
    between = source[charge_idx:commit_idx]

    assert charge_idx < commit_idx
    assert "SessionLocal" not in between
    assert "async with" not in between


def test_completion_rechecks_cancel_after_billing_charge_before_commit() -> None:
    source = inspect.getsource(completion.run_completion)
    charge_idx = source.index(
        "await worker_billing.charge_completion(session, comp_for_billing)"
    )
    cancel_idx = source.index(
        'await _raise_if_completion_cancelled(\n                    redis,\n                    task_id,\n                    "cancelled before success commit"',
        charge_idx,
    )
    commit_idx = source.index("await session.commit()", charge_idx)

    assert charge_idx < cancel_idx < commit_idx


def test_completion_flushes_before_each_delta_publish() -> None:
    source = inspect.getsource(completion.run_completion)
    marker = 'if ev_type == "response.output_text.delta":'
    starts: list[int] = []
    cursor = 0
    while True:
        start = source.find(marker, cursor)
        if start < 0:
            break
        starts.append(start)
        cursor = start + len(marker)

    assert len(starts) == 2
    for start in starts:
        end = source.index('elif ev_type == "response.completed":', start)
        block = source[start:end]
        flush_idx = block.index("await _flush_completion_text(")
        publish_idx = block.index("await publish_event(")

        assert "EV_COMP_DELTA" in block
        assert flush_idx < publish_idx


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
    monkeypatch.setattr(completion, "SessionLocal", lambda: Session())
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
    monkeypatch.setattr(completion, "SessionLocal", lambda: Session())
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
    monkeypatch.setattr(completion, "SessionLocal", lambda: Session())
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
    assert (
        completion._image_output_tokens_for_budget(  # noqa: SLF001
            1_000,
            image_output_per_1k_micro=0,
        )
        == 1
    )


def test_completion_completed_response_marks_billable_partial_before_local_work() -> (
    None
):
    source = inspect.getsource(completion.run_completion)
    first_completed = source.index('elif ev_type == "response.completed":')
    first_block = source[
        first_completed : source.index("elif ev_type in {", first_completed)
    ]
    second_completed = source.index(
        'elif ev_type == "response.completed":', first_completed + 1
    )
    second_block = source[
        second_completed : source.index("elif ev_type in {", second_completed)
    ]

    for block in (first_block, second_block):
        assert "has_partial = True" in block
        assert block.index("has_partial = True") < block.index("parse_usage")


def test_completion_terminal_failure_preserves_partial_usage_buckets() -> None:
    source = inspect.getsource(completion.run_completion)
    start = source.index("# Why: partial-stream or completed-response failures")
    branch = source[start : source.index("await session.commit()", start)]
    settle_idx = branch.index("await _settle_failed_completion_billing")

    for assignment in (
        "comp_partial.cache_read_tokens = cache_read_tokens",
        "comp_partial.cache_creation_tokens = cache_creation_tokens",
        "comp_partial.cache_creation_5m_tokens = cache_creation_5m_tokens",
        "comp_partial.cache_creation_1h_tokens = cache_creation_1h_tokens",
        "comp_partial.reasoning_tokens = reasoning_tokens",
        "comp_partial.image_output_tokens = image_output_tokens",
    ):
        assert assignment in branch
        assert branch.index(assignment) < settle_idx
    assert "_fallback_completion_tool_image_tokens(" in branch


def test_completion_success_fallbacks_tool_image_tokens_before_charge() -> None:
    source = inspect.getsource(completion.run_completion)
    start = source.index("# --- 6. 成功态 ---")
    branch = source[start : source.index("await session.commit()", start)]
    fallback_idx = branch.index("_fallback_completion_tool_image_tokens(")
    update_idx = branch.index("update(Completion)")
    charge_idx = branch.index("await worker_billing.charge_completion")

    assert "and tool_images" in branch
    assert "and reserved_tool_image_budget_micro > 0" in branch
    assert fallback_idx < update_idx
    assert fallback_idx < charge_idx


def test_completion_stale_flush_exits_without_weakening_epoch_fence() -> None:
    flush_source = inspect.getsource(completion._flush_completion_text)  # noqa: SLF001
    run_source = inspect.getsource(completion.run_completion)
    stale_idx = run_source.index("except _CompletionEpochSuperseded as exc:")
    generic_idx = run_source.index("except Exception as exc")

    assert "raise _CompletionEpochSuperseded" in flush_source
    assert "except _CompletionEpochSuperseded:" in flush_source
    assert "sentinel and exits without writing terminal state" in flush_source
    assert stale_idx < generic_idx
    assert "Completion.status.in_(_RUNNING_COMPLETION_STATUSES)" in run_source
    assert completion._RUNNING_COMPLETION_STATUSES == (  # noqa: SLF001
        completion.CompletionStatus.STREAMING.value,
    )


def test_generation_retry_delay_is_jittered() -> None:
    helper_source = inspect.getsource(generation._retry_delay_seconds)  # noqa: SLF001
    runner_source = inspect.getsource(generation.run_generation)

    assert "jitter" in helper_source.lower()
    assert "random.uniform" in helper_source
    assert "_retry_delay_seconds(attempt)" in runner_source


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
    )

    release_snippet = (
        "_release_provider_slot(redis, release_provider_name, generation.id)"
    )
    assert release_snippet in (success_source + failure_source)
    assert "_release_provider_slot(redis, release_provider_name, task_id)" in (
        submit_failure_source
    )
    assert "slot_provider_name = provider.name" in run_source
    assert "provider_name=slot_provider_name" in run_source


def test_video_postprocess_returns_processed_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_bytes = (
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdat"
    )
    monkeypatch.setattr(video_artifacts.shutil, "which", lambda _name: None)

    processed, diagnostics = video_generation._postprocess_video_bytes(  # noqa: SLF001
        video_bytes
    )

    assert processed["video_bytes"] == video_bytes
    assert processed["poster_bytes"] is None
    assert processed["faststart"] is False
    assert diagnostics["faststart"] is False
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

        async def eval(self, *_args: Any) -> int:
            return 1

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

        async def eval(self, *_args: Any) -> int:
            return 1

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
    assert await generation._is_cancelled(redis, "gen-1") is True
    assert await completion._is_cancelled(redis, "comp-1") is True
    assert redis.calls >= 4


@pytest.mark.asyncio
async def test_completion_cancel_check_honors_redis_cancel_key() -> None:
    class Redis:
        async def get(self, _key: str) -> str:
            return "1"

    assert await completion._is_cancelled(Redis(), "comp-1") is True


def test_tool_limit_fallback_completed_finalizes_active_tools() -> None:
    source = inspect.getsource(completion.run_completion)
    fallback_idx = source.index("if tool_loop_truncated:")
    completed_idx = source.index('elif ev_type == "response.completed":', fallback_idx)
    failed_idx = source.index("elif ev_type in {", completed_idx)
    completed_block = source[completed_idx:failed_idx]

    assert "finalize_active(" in completed_block
    assert "ToolStatus.SUCCEEDED.value" in completed_block


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

    with pytest.raises(generation._LeaseLost):  # noqa: SLF001
        await generation._acquire_lease(redis, "gen-1", "worker-1:token-1")  # noqa: SLF001

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

    await generation._release_lease(redis, "gen-1", "worker-1:token-1")  # noqa: SLF001

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

    await generation._release_lease(redis, "gen-1", "worker-1")

    assert redis.deleted == []


def test_run_generation_uses_unique_lease_token_for_owner_cas() -> None:
    source = inspect.getsource(generation.run_generation)

    assert 'lease_token = f"{worker_id}:' in source
    assert "_acquire_lease(redis, task_id, lease_token)" in source
    assert "_release_lease(redis, task_id, lease_token)" in source


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
    ) -> None:
        calls.append((f"slot:{task_id}", provider_name))

    async def clear_inflight(_redis: Any, task_id: str) -> None:
        calls.append((f"inflight:{task_id}", None))

    async def clear_avoided(_redis: Any, task_id: str) -> None:
        calls.append((f"avoided:{task_id}", None))

    async def release_lease(_redis: Any, task_id: str, token: str) -> None:
        calls.append((f"lease:{task_id}", token))

    monkeypatch.setattr(generation, "_release_image_queue_slot", release_slot)
    monkeypatch.setattr(generation, "_inflight_clear", clear_inflight)
    monkeypatch.setattr(generation, "_clear_avoided_providers", clear_avoided)
    monkeypatch.setattr(generation, "_release_lease", release_lease)

    await generation._release_generation_runtime_resources(  # noqa: SLF001
        object(),
        task_id="gen-1",
        lease_token="worker-1:lease",
        provider_name="provider-1",
        clear_avoided_providers=True,
    )

    assert calls == [
        ("slot:gen-1", "provider-1"),
        ("inflight:gen-1", None),
        ("avoided:gen-1", None),
        ("lease:gen-1", "worker-1:lease"),
    ]


def test_generation_setup_failure_is_inside_runtime_cleanup_guard() -> None:
    source = inspect.getsource(generation.run_generation)
    start = source.index("renewer = asyncio.create_task(")
    end = source.index("has_partial =", start)
    setup = source[start:end]

    assert "except BaseException:" in setup
    assert "_cancel_renewer_task(renewer)" in setup
    assert "_release_generation_runtime_resources(" in setup
    assert "await asyncio.shield(cleanup_future)" in setup


def test_generation_lease_lost_max_attempts_fails_without_requeue() -> None:
    source = inspect.getsource(generation.run_generation)
    start = source.rindex("except _LeaseLost as exc:")
    end = source.index("except _StaleGenerationAttempt", start)
    lease_branch = source[start:end]

    max_idx = lease_branch.index("if attempt >= _MAX_ATTEMPTS:")
    fail_idx = lease_branch.index("_mark_generation_attempt_failed")
    retry_idx = lease_branch.index("_mark_generation_attempt_retrying")

    assert max_idx < fail_idx < retry_idx
    assert "retriable=False" in lease_branch[fail_idx:retry_idx]
    assert "redis.enqueue_job" not in lease_branch[fail_idx:retry_idx]


def test_generation_attempt_update_can_guard_current_status() -> None:
    from sqlalchemy.dialects import postgresql
    from lumen_core.constants import GenerationStatus

    rendered = str(
        generation._generation_attempt_update(  # noqa: SLF001
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
    source = inspect.getsource(generation.run_generation)
    marker = "parent_upstream_request_for_bonus = dict(upstream_req)"
    start = source.index(
        "status=GenerationStatus.SUCCEEDED.value", source.index(marker)
    )
    update_start = source.rindex("_generation_attempt_update(", 0, start)
    update_end = source.index(").values(", update_start)
    success_update = source[update_start:update_end]

    assert "statuses=_RUNNING_GENERATION_STATUSES" in success_update


def test_completion_terminal_writes_require_streaming_status() -> None:
    assert completion._RUNNING_COMPLETION_STATUSES == (  # noqa: SLF001
        completion.CompletionStatus.STREAMING.value,
    )


def test_generation_max_attempts_failure_releases_hold() -> None:
    source = inspect.getsource(generation.run_generation)
    start = source.index('err_code = "max_attempts_exceeded"')
    end = source.index("return", start)
    branch = source[start:end]

    assert "_generation_attempt_update(" in branch
    assert "statuses=(GenerationStatus.QUEUED.value,)" in branch
    assert "worker_billing.release_generation(" in branch
    assert "reason=err_code" in branch
    assert "worker_billing.flush_balance_cache_refreshes(session)" in branch


def test_generation_prequeue_terminal_writes_guard_queued_status() -> None:
    run_source = inspect.getsource(generation.run_generation)
    lifecycle_source = inspect.getsource(
        generation_lifecycle.settle_existing_generated_image
    )
    source_markers = [
        (run_source, "await _ensure_generation_conversation_alive("),
        (run_source, '"primary_input_image_id must be included in input_image_ids"'),
        (
            lifecycle_source,
            '"generation already has image task_id=%s image_id=%s',
        ),
    ]
    for source, marker in source_markers:
        start = source.index(marker)
        end = source.index("return", start)
        branch = source[start:end].replace("_g.", "")
        assert "_generation_attempt_update(" in branch
        assert "statuses=(GenerationStatus.QUEUED.value,)" in branch


def test_completion_max_attempts_failure_releases_hold() -> None:
    source = inspect.getsource(completion.run_completion)
    helper = inspect.getsource(completion._completion_preflight_failure)
    assert '"max_attempts_exceeded"' in helper
    start = source.index("if preflight_failure is not None:")
    end = source.index("return", start)
    branch = source[start:end]

    assert "worker_billing.release_completion(" in branch
    assert "reason=err_code" in branch
    assert "worker_billing.flush_balance_cache_refreshes(session)" in branch


def test_completion_retry_enqueue_failure_marks_terminal_failed() -> None:
    source = inspect.getsource(completion.run_completion)
    start = source.index('logger.error("re-enqueue failed task=%s err=%s"')
    end = source.index("# terminal", start)
    branch = source[start:end]

    assert 'enqueue_err = "retry_enqueue_failed"' in branch
    assert "Completion.status == CompletionStatus.QUEUED.value" in branch
    assert "status=CompletionStatus.FAILED.value" in branch
    assert "worker_billing.release_completion(" in branch
    assert "EV_COMP_FAILED" in branch
    assert '"retriable": False' in branch


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


def test_completion_cancel_branch_checks_rowcount_before_message_update() -> None:
    source = inspect.getsource(completion.run_completion)
    start = source.index("except _TaskCancelled as exc:")
    end = source.index("await publish_event(", start)
    branch = source[start:end]

    assert "res = await session.execute(" in branch
    assert "if affected_rows(res) == 0:" in branch
    assert branch.index("if affected_rows(res) == 0:") < branch.index(
        "msg_c = await session.get(Message, message_id)"
    )
    assert "except _CompletionEpochSuperseded as stale_exc:" in branch
    assert branch.index(
        "except _CompletionEpochSuperseded as stale_exc:"
    ) < branch.index("except Exception as db_exc:")


def test_generation_byok_early_failure_releases_hold_and_guards_status() -> None:
    source = inspect.getsource(generation.run_generation)
    start = source.index("byok_error = classify_user_credential_error(exc)")
    end = source.index("await publish_event(", start)
    branch = source[start:end]

    assert "_generation_attempt_update(" in branch
    assert "GenerationStatus.QUEUED.value" in branch
    assert "GenerationStatus.RUNNING.value" in branch
    assert "worker_billing.release_generation(" in branch
    assert "reason=err_code" in branch
    assert "worker_billing.flush_balance_cache_refreshes(session)" in branch


def test_sse_timestamp_lock_is_eagerly_initialized() -> None:
    assert sse_publish._TS_LOCK is not None
    assert hasattr(sse_publish._TS_LOCK, "acquire")


def test_sse_xadd_dedupe_uses_per_event_set_nx_ex() -> None:
    lua = " ".join(sse_publish._XADD_IDEMPOTENT_LUA.split())

    assert "HSET" not in sse_publish._XADD_IDEMPOTENT_LUA
    assert "HGET" not in sse_publish._XADD_IDEMPOTENT_LUA
    assert "redis.call('SET', KEYS[2], '', 'NX', 'EX', tonumber(ARGV[5]))" in lua
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

    from app import provider_pool, upstream

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
    await upstream.close_client()
    timeout_config = upstream._TimeoutConfig(connect=1.0, read=2.0, write=3.0)
    built: list[_FakeClosableClient] = []
    cache = getattr(upstream, cache_name)
    cache.clear()

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return timeout_config

    def fake_builder(
        _timeout_config: upstream._TimeoutConfig | None = None,
        *,
        proxy_url: str | None = None,
    ) -> _FakeClosableClient:
        assert proxy_url
        client = _FakeClosableClient()
        built.append(client)
        return client

    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)
    monkeypatch.setattr(upstream, builder_name, fake_builder)
    monkeypatch.setattr(upstream, "_PROXIED_CLIENT_CLOSE_DELAY_SECONDS", 0.01)

    limit = int(getattr(upstream, "_PROXIED_CLIENT_CACHE_MAX", 32))
    getter = getattr(upstream, getter_name)
    try:
        for idx in range(limit + 5):
            await getter(f"http://proxy-{idx}.example:8080")

        assert len(cache) <= limit
        assert not any(client.closed for client in built[:5])
        assert len(upstream._retired_client_close_tasks) == 5  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert any(client.closed for client in built[:5])
        assert not upstream._retired_client_close_tasks  # noqa: SLF001
    finally:
        await upstream.close_client()


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
    close_task = asyncio.create_task(upstream._delayed_aclose(client, delay=0))  # noqa: SLF001
    await asyncio.sleep(0.01)

    assert client.closed is False

    client.idle.set()
    await asyncio.wait_for(close_task, timeout=1.0)
    assert client.closed is True


@pytest.mark.asyncio
async def test_close_client_closes_retired_clients_without_delay() -> None:
    await upstream.close_client()
    client = _FakeClosableClient()
    close_task = upstream._schedule_delayed_aclose(client)  # noqa: SLF001

    assert close_task in upstream._retired_client_close_tasks  # noqa: SLF001

    try:
        await upstream.close_client()

        assert client.closed is True
        assert not upstream._retired_client_close_tasks  # noqa: SLF001
        assert not upstream._retired_clients  # noqa: SLF001
    finally:
        await upstream.close_client()


@pytest.mark.asyncio
async def test_close_retired_clients_waits_out_cancelled_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await upstream.close_client()

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

    monkeypatch.setattr(upstream, "_PROXIED_CLIENT_CLOSE_DELAY_SECONDS", 0)
    client = CancelSensitiveClient()
    upstream._schedule_delayed_aclose(client)  # noqa: SLF001
    try:
        await asyncio.wait_for(client.started.wait(), timeout=1.0)

        closer = asyncio.create_task(upstream._close_retired_clients_now())  # noqa: SLF001
        await asyncio.sleep(0.01)
        assert not closer.done()

        client.release.set()
        await asyncio.wait_for(closer, timeout=1.0)
        assert client.closed is True
        assert client.cancelled is False
        assert client.calls >= 1
        assert not upstream._retired_client_close_tasks  # noqa: SLF001
        assert not upstream._retired_clients  # noqa: SLF001
    finally:
        client.release.set()
        await upstream.close_client()


@pytest.mark.asyncio
async def test_startup_failure_closes_upstream_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    cleanup_calls: list[str] = []

    def raise_startup_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("otel boom")

    async def fake_close_client() -> None:
        cleanup_calls.append("upstream")

    async def fake_billing_shutdown() -> None:
        cleanup_calls.append("billing")

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

    assert "upstream" in cleanup_calls
    assert "metrics" in cleanup_calls


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
            self.eval_args: tuple[Any, ...] | None = None

        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        async def eval(self, *args: Any) -> int:
            self.eval_args = args
            return 0

    redis = Redis()

    async with generation._image_queue_lock(redis):
        pass

    assert redis.eval_args is not None
    assert redis.eval_args[1] == 1
    assert redis.eval_args[2] == "generation:image_queue:lock"


def test_image_queue_reserve_has_atomic_lua_path() -> None:
    source = inspect.getsource(generation._reserve_image_queue_slot)

    assert "_RESERVE_IMAGE_SLOT_LUA" in source
    assert "redis.zadd(provider_zset" in source
