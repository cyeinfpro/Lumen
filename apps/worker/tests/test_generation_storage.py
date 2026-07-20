from __future__ import annotations

import asyncio
import inspect
import io
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage
from sqlalchemy.dialects import postgresql

os.environ.setdefault(
    "STORAGE_ROOT", f"{tempfile.gettempdir()}/lumen-worker-test-storage"
)

from lumen_core.constants import (
    EV_GEN_FAILED,
    EV_GEN_RETRYING,
    GenerationStatus,
    MessageStatus,
)
from app.background_removal.local_chroma import (
    recover_solid_background_transparency,
)
from app.storage import LocalStorage, StorageDiskFullError, StoragePutResult
from app.tasks import generation
from app.tasks.generation_parts import (
    lifecycle,
    queue as generation_queue,
    success as generation_success,
    workflow_hooks,
)


class FakeStorage:
    def __init__(
        self,
        fail_keys: set[str] | None = None,
        fail_delete_keys: set[str] | None = None,
    ) -> None:
        self.fail_keys = fail_keys or set()
        self.fail_delete_keys = fail_delete_keys or set()
        self.deleted: list[str] = []
        self.put_keys: list[str] = []

    def put_bytes_result(self, key: str, data: bytes) -> StoragePutResult:
        self.put_keys.append(key)
        if key in self.fail_keys:
            raise StorageDiskFullError(key)
        return StoragePutResult(size=len(data), created=True)

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        if key in self.fail_delete_keys:
            raise RuntimeError(f"delete failed: {key}")
        return True


class FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class FakeMessage:
    status = None
    # New release-on-failure path reads .user_id and .id; provide neutral stubs
    # so the fake session.get() can stand in for either a Message or Generation.
    user_id = "user-1"
    id = "fake-1"


class FakeScalarResult:
    def __init__(self, value=None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeStatementSession:
    def __init__(self, value=None) -> None:
        self.value = value
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeScalarResult(self.value)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.enqueued: list[tuple[str, tuple, dict]] = []

    async def set(
        self,
        key: str,
        value,
        nx: bool = False,
        ex: int | None = None,
        px: int | None = None,
    ):
        _ = ex, px
        if nx and key in self.store:
            return False
        self.store[key] = str(value)
        return True

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.store:
                deleted += 1
                del self.store[key]
        return deleted

    async def zrange(self, key: str, start: int, end: int):
        items = list(self.zsets.get(key, {}).items())
        items.sort(key=lambda item: item[1])
        values = [name for name, _score in items]
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrem(self, key: str, member: str) -> int:
        if key in self.zsets and member in self.zsets[key]:
            del self.zsets[key][member]
            return 1
        return 0

    async def zremrangebyscore(self, key: str, min_score, max_score) -> int:
        _ = min_score
        max_value = float(max_score)
        zset = self.zsets.setdefault(key, {})
        expired = [name for name, score in zset.items() if score <= max_value]
        for name in expired:
            del zset[name]
        return len(expired)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zscore(self, key: str, member: str):
        return self.zsets.get(key, {}).get(member)

    async def expire(self, key: str, ttl: int) -> bool:
        _ = key, ttl
        return True

    async def incrby(self, key: str, amount: int) -> int:
        value = int(self.store.get(key, "0")) + int(amount)
        self.store[key] = str(value)
        return value

    async def eval(self, *args: Any) -> int:
        script = args[0]
        if script == generation._RELEASE_LEASE_LUA:
            key = args[2]
            token = args[3]
            if self.store.get(key) == token:
                await self.delete(key)
                return 1
            return 0
        if script == generation_queue.RENEW_IMAGE_QUEUE_LOCK_LUA:
            key = args[2]
            token = args[3]
            return int(self.store.get(key) == token)
        if script == generation_queue.CLEANUP_IMAGE_QUEUE_ACTIVE_LUA:
            active_key, lock_key, token, now = args[2:6]
            if self.store.get(lock_key) != token:
                return -1
            return await self.zremrangebyscore(active_key, "-inf", now)
        if script == generation_queue.CLEANUP_IMAGE_QUEUE_PROVIDER_LUA:
            provider_key, lock_key, token, now = args[2:6]
            if self.store.get(lock_key) != token:
                return -1
            await self.zremrangebyscore(provider_key, "-inf", now)
            return await self.zcard(provider_key)
        if script == generation_queue.ADVANCE_IMAGE_QUEUE_CURSOR_LUA:
            cursor_key, lock_key, token, steps = args[2:6]
            if self.store.get(lock_key) != token:
                return -1
            return await self.incrby(cursor_key, int(steps))
        if script == generation_queue.DELETE_IMAGE_QUEUE_KEY_IF_OWNER_LUA:
            key, lock_key, token = args[2:5]
            if self.store.get(lock_key) != token:
                return -1
            return await self.delete(key)
        if script == generation_queue.SET_IMAGE_QUEUE_VALUE_IF_OWNER_LUA:
            key, lock_key, token, value, _ttl_ms = args[2:8]
            if self.store.get(lock_key) != token:
                return -1
            await self.set(key, value)
            return "OK"
        if script == generation_queue.CLEAR_STALE_IMAGE_QUEUE_RESERVATION_LUA:
            (
                provider_key,
                global_key,
                task_provider_key,
                lock_key,
                token,
                expected_provider,
                task_id,
                active_member,
            ) = args[2:10]
            if self.store.get(lock_key) != token:
                return -1
            if self.store.get(task_provider_key) != expected_provider:
                return 0
            await self.zrem(provider_key, task_id)
            await self.zrem(global_key, active_member)
            return await self.delete(task_provider_key)
        if script != generation._RESERVE_IMAGE_SLOT_LUA:
            raise NotImplementedError(script)
        (
            provider_zset,
            global_zset,
            task_provider_key,
            not_before_key,
            lock_key,
            cursor_key,
            reservation_key,
        ) = args[2:9]
        now = float(args[9])
        expiry = float(args[10])
        task_id = str(args[11])
        provider_name = str(args[12])
        provider_cap = int(args[13])
        global_cap = int(args[14])
        task_provider_ttl = int(args[15])
        provider_zset_ttl = int(args[16])
        lock_token = str(args[17])
        cursor_steps = int(args[18])
        reservation_ttl = int(args[19])

        if self.store.get(lock_key) != lock_token:
            return -1

        await self.zremrangebyscore(provider_zset, "-inf", now)
        await self.zremrangebyscore(global_zset, "-inf", now)
        if await self.zcard(provider_zset) >= provider_cap:
            return 0
        if await self.zcard(global_zset) >= global_cap:
            return 0
        await self.zadd(provider_zset, {task_id: expiry})
        await self.expire(provider_zset, provider_zset_ttl)
        await self.set(task_provider_key, provider_name, ex=task_provider_ttl)
        await self.set(reservation_key, lock_token, ex=reservation_ttl)
        await self.zadd(global_zset, {task_id: expiry})
        await self.delete(not_before_key)
        if cursor_steps > 0:
            await self.incrby(cursor_key, cursor_steps)
        return 1

    async def enqueue_job(self, name: str, *args, **kwargs):
        self.enqueued.append((name, args, kwargs))
        return SimpleNamespace(job_id=f"{name}:{len(self.enqueued)}")


@pytest.mark.asyncio
async def test_generation_conversation_alive_check_filters_deleted_rows() -> None:
    session = FakeStatementSession()

    with pytest.raises(generation._TaskCancelled):
        await generation._ensure_generation_conversation_alive(
            session,
            message_id="msg-1",
            user_id="user-1",
            lock=True,
        )

    rendered = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "JOIN messages" in rendered
    assert "messages.deleted_at IS NULL" in rendered
    assert "conversations.deleted_at IS NULL" in rendered
    assert "FOR UPDATE OF conversations" in rendered


@pytest.mark.asyncio
async def test_await_with_lease_guard_cancels_work_when_cancel_flag_appears() -> None:
    redis = FakeRedis()
    await redis.set("task:gen-1:cancel", "1")
    cancelled = False

    async def work():
        nonlocal cancelled
        try:
            await asyncio.sleep(60)
        finally:
            cancelled = True

    with pytest.raises(generation._TaskCancelled):
        await generation._await_with_lease_guard(
            work(),
            asyncio.Event(),
            redis=redis,
            task_id="gen-1",
            cancel_poll_interval_s=0.01,
        )

    assert cancelled is True


@pytest.mark.asyncio
async def test_cancel_after_upstream_result_aborts_before_local_finalize() -> None:
    redis = FakeRedis()
    await redis.set("task:gen-after-upstream:cancel", "1")

    with pytest.raises(
        generation._TaskCancelled,
        match="cancelled after upstream result",
    ):
        await generation._raise_if_generation_interrupted(
            redis,
            "gen-after-upstream",
            asyncio.Event(),
            "cancelled after upstream result",
        )


def test_run_generation_guards_finalize_storage_and_billing_boundaries() -> None:
    orchestration = inspect.getsource(generation_success.finalize_generation_success)
    validate = orchestration.index("_validate_result_and_publish_finalizing(")
    postprocess = orchestration.index("_postprocess_generated_image(", validate)
    storage = orchestration.index("_write_artifact_files(", postprocess)
    persist = orchestration.index("_persist_generation_success(", storage)
    assert validate < postprocess < storage < persist

    validation_source = inspect.getsource(
        generation_success._validate_result_and_publish_finalizing
    )
    assert '"cancelled after upstream result"' in validation_source
    assert "_postprocess_raw_generated_image(" in inspect.getsource(
        generation_success._postprocess_generated_image
    )

    storage_source = inspect.getsource(generation_success._write_artifact_files)
    storage_guard = storage_source.index('"cancelled before storage write"')
    storage_write = storage_source.index(
        "_write_generation_files(",
        storage_guard,
    )
    lease_guard = storage_source.rindex(
        "_await_with_lease_guard(",
        0,
        storage_write,
    )
    assert storage_guard < lease_guard < storage_write

    persistence_source = inspect.getsource(
        generation_success._persist_generation_success
    )
    persistence_guard = persistence_source.index(
        '"cancelled before generation persistence"'
    )
    attempt_fence = persistence_source.index(
        "_ensure_generation_attempt_current(",
        persistence_guard,
    )
    billing_guard = persistence_source.index(
        '"cancelled before billing settlement"',
        attempt_fence,
    )
    settle = persistence_source.index(
        "worker_billing.settle_generation(",
        billing_guard,
    )
    commit_guard = persistence_source.index(
        '"cancelled before success commit"',
        settle,
    )
    commit = persistence_source.index("await session.commit()", commit_guard)
    assert persistence_guard < attempt_fence < billing_guard
    assert billing_guard < settle < commit_guard < commit


def test_existing_image_retry_checks_cancel_before_success_settlement() -> None:
    source = inspect.getsource(lifecycle.settle_existing_generated_image)

    cancel_check = source.index("if await _g._is_cancelled(redis, task_id):")
    release = source.index("await _g.worker_billing.release_generation(")
    success_update = source.index("status=_g.GenerationStatus.SUCCEEDED.value")
    settle = source.index("await _g.worker_billing.settle_generation(")

    assert cancel_check < release < success_update < settle


def test_classify_disk_full_as_retriable() -> None:
    decision = generation._classify_exception(
        StorageDiskFullError("u/user/g/gen/orig.png"), has_partial=False
    )

    assert decision.retriable is True
    assert "disk_full" in decision.reason


def test_classify_generation_timeout_as_retriable() -> None:
    decision = generation._classify_exception(TimeoutError(), has_partial=False)

    assert decision.retriable is True
    assert "timeout" in decision.reason


def test_display_variant_preserves_alpha_for_transparent_png() -> None:
    src = PILImage.new("RGBA", (16, 16), (255, 0, 0, 0))
    src.putpixel((0, 0), (255, 0, 0, 255))

    data, size = generation._make_display(src)

    assert size == (16, 16)
    with PILImage.open(io.BytesIO(data)) as reloaded:
        assert reloaded.format == "WEBP"
        assert reloaded.mode == "RGBA"
        assert reloaded.getchannel("A").getextrema()[0] == 0


def test_generation_blurhash_skips_tiny_images(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_encode(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("tiny images must not call blurhash encoder")

    monkeypatch.setitem(sys.modules, "blurhash", SimpleNamespace(encode=fail_encode))
    tiny = PILImage.new("RGB", (3, 4), "white")

    assert generation._compute_blurhash(tiny) is None  # noqa: SLF001


def test_recover_solid_background_transparency_from_opaque_image() -> None:
    src = PILImage.new("RGB", (24, 24), (255, 255, 255))
    for x in range(6, 18):
        for y in range(6, 18):
            src.putpixel((x, y), (200, 20, 30))

    recovered = recover_solid_background_transparency(src)

    assert recovered is not None
    try:
        assert recovered.mode == "RGBA"
        assert recovered.getpixel((0, 0))[3] == 0
        assert recovered.getpixel((12, 12))[3] == 255
    finally:
        recovered.close()


def test_recover_solid_background_transparency_preserves_interior_matte_color() -> None:
    src = PILImage.new("RGB", (32, 32), (255, 0, 255))
    for x in range(6, 26):
        for y in range(6, 26):
            src.putpixel((x, y), (20, 80, 200))
    for x in range(12, 20):
        for y in range(12, 20):
            src.putpixel((x, y), (255, 0, 255))

    recovered = recover_solid_background_transparency(src)

    assert recovered is not None
    try:
        assert recovered.getpixel((0, 0))[3] == 0
        assert recovered.getpixel((16, 16))[3] == 255
    finally:
        recovered.close()


def test_recover_solid_background_transparency_rejects_noisy_edges() -> None:
    src = PILImage.new("RGB", (24, 24), (255, 255, 255))
    for x in range(24):
        src.putpixel((x, 0), (0, 0, 0) if x % 2 else (255, 255, 255))

    assert recover_solid_background_transparency(src) is None


def test_image_request_options_force_png_for_transparent_background() -> None:
    options = generation._image_request_options(
        {
            "output_format": "webp",
            "output_compression": 90,
            "background": "transparent",
        },
        size="1024x1024",
    )

    assert options["background"] == "transparent"
    assert options["output_format"] == "png"
    assert "output_compression" not in options


def test_generation_epoch_update_requires_matching_row() -> None:
    with pytest.raises(generation._StaleGenerationAttempt):
        generation._ensure_generation_updated(FakeResult(0), "gen-1", 2)

    generation._ensure_generation_updated(FakeResult(1), "gen-1", 2)


def test_validate_resolved_size_accepts_valid_preset() -> None:
    assert generation._validate_resolved_size("3840x2160", "16:9") == (3840, 2160)


def test_validate_resolved_size_rejects_hard_limit_violation() -> None:
    with pytest.raises(ValueError, match="longest side"):
        generation._validate_resolved_size("3856x2160", "16:9")


def test_validate_resolved_size_rejects_aspect_drift() -> None:
    with pytest.raises(ValueError, match="aspect ratio drift"):
        generation._validate_resolved_size("1024x1024", "16:9")


def test_validate_resolved_size_can_skip_aspect_drift_for_fixed_size() -> None:
    assert generation._validate_resolved_size(
        "1024x1024",
        "16:9",
        validate_aspect_ratio=False,
    ) == (1024, 1024)


def test_prompt_with_aspect_ratio_constraint_adds_square_guard() -> None:
    prompt = generation._prompt_with_aspect_ratio_constraint(
        "画一张活动分享图",
        "1:1",
    )

    assert "strict 1:1 ratio" in prompt
    assert "square canvas" in prompt
    assert "poster" in prompt


def test_retry_delay_adds_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generation.random, "uniform", lambda low, high: high)

    assert generation._retry_delay_seconds(1) == pytest.approx(12.0)


def test_retry_backoff_grows_after_configured_table() -> None:
    first_tail_attempt = len(generation.RETRY_BACKOFF_SECONDS) + 1
    assert generation._base_retry_backoff_seconds(first_tail_attempt) == (
        generation.RETRY_BACKOFF_SECONDS[-1] * 2
    )


def test_safe_generation_error_details_keeps_transparent_context_only() -> None:
    exc = generation.UpstreamError(
        "transparent material pipeline failed",
        error_code=generation.EC.BAD_RESPONSE.value,
        payload={
            "transparent_qc": {
                "passed": False,
                "score": 0.123456,
                "failure_reasons": ["alpha_all_opaque"],
                "warnings": ["connectivity_skipped"],
                "foreground_bbox": [1.2, 2.8, 30, 40],
                "alpha_coverage": 0.99999,
                "border_alpha_max": 512,
                "largest_component_ratio": 0.77777,
                "prompt": "do-not-expose",
            },
            "transparent_provider": "rembg-local",
            "raw": "do-not-expose",
        },
    )

    assert generation._safe_generation_error_details(exc) == {
        "transparent_qc": {
            "passed": False,
            "score": 0.1235,
            "alpha_coverage": 1.0,
            "largest_component_ratio": 0.7778,
            "border_alpha_max": 255,
            "foreground_bbox": [1, 2, 30, 40],
            "failure_reasons": ["alpha_all_opaque"],
            "warnings": ["connectivity_skipped"],
        },
        "transparent_provider": "rembg-local",
    }


def test_primary_input_image_id_must_be_in_input_image_ids() -> None:
    assert generation._primary_input_image_id_valid(None, []) is True
    assert generation._primary_input_image_id_valid("img-1", ["img-1"]) is True
    assert generation._primary_input_image_id_valid("img-2", ["img-1"]) is False


@pytest.mark.asyncio
async def test_lease_renewer_sets_event_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Redis:
        async def expire(self, *_args) -> None:
            raise RuntimeError("redis down")

    monkeypatch.setattr(generation, "_LEASE_RENEW_S", 0)
    lease_lost = asyncio.Event()

    await generation._lease_renewer(_Redis(), "gen-1", "worker-1", lease_lost)

    assert lease_lost.is_set()


@pytest.mark.asyncio
async def test_cancel_renewer_task_awaits_cancel_cleanup() -> None:
    cleaned = asyncio.Event()

    async def renewer() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(renewer())
    await asyncio.sleep(0)

    await generation._cancel_renewer_task(task)

    assert cleaned.is_set()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_mark_generation_attempt_retrying_requeues_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    published: list[dict] = []

    class _Session:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult(1)

        async def commit(self) -> None:
            self.committed = True

    session = _Session()

    async def fake_publish_event(redis_arg, user_id, channel, event_name, data):
        published.append(
            {
                "redis": redis_arg,
                "user_id": user_id,
                "channel": channel,
                "event_name": event_name,
                "data": data,
            }
        )

    monkeypatch.setattr(generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(generation, "publish_event", fake_publish_event)

    ok = await generation._mark_generation_attempt_retrying(
        redis,
        task_id="gen-1",
        message_id="msg-1",
        user_id="user-1",
        attempt=2,
        error_code="lease_lost",
        error_message="generation lease lost; task will be retried",
        delay=3.5,
        reason="lease_lost",
        max_attempts=5,
    )

    assert ok is True
    assert session.committed is True
    assert redis.enqueued == [
        ("run_generation", ("gen-1",), {"_defer_by": 3.5, "_job_try": 3})
    ]
    assert generation._image_queue_not_before_key("gen-1") in redis.store
    assert published[0]["event_name"] == EV_GEN_RETRYING
    assert published[0]["data"]["reason"] == "lease_lost"


@pytest.mark.asyncio
async def test_maybe_requeue_stale_generation_attempt_only_for_same_queued_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    published: list[dict[str, Any]] = []

    class _RowResult:
        def __init__(self, row: tuple[str, str, str] | None) -> None:
            self.row = row

        def one_or_none(self):
            return self.row

    class _Session:
        rolled_back = False
        statements: list[Any] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, statement):
            self.statements.append(statement)
            return _RowResult((GenerationStatus.QUEUED.value, "msg-1", "user-1"))

        async def rollback(self) -> None:
            self.rolled_back = True

    session = _Session()

    async def fake_publish_event(redis_arg, user_id, channel, event_name, data):
        published.append(
            {
                "redis": redis_arg,
                "user_id": user_id,
                "channel": channel,
                "event_name": event_name,
                "data": data,
            }
        )

    monkeypatch.setattr(generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(generation, "publish_event", fake_publish_event)

    ok = await generation._maybe_requeue_stale_generation_attempt(
        redis,
        task_id="gen-1",
        attempt=2,
        reason="row_lock_lost",
        delay=1.25,
    )

    assert ok is True
    assert session.rolled_back is True
    rendered = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "generations.status = 'queued'" in rendered
    assert "generations.attempt = 2" in rendered
    assert "FOR UPDATE SKIP LOCKED" in rendered
    assert redis.enqueued == [
        ("run_generation", ("gen-1",), {"_defer_by": 1.25, "_job_try": 3})
    ]
    assert published[0]["event_name"] == EV_GEN_RETRYING
    assert published[0]["data"]["reason"] == "row_lock_lost"


@pytest.mark.asyncio
async def test_maybe_requeue_stale_generation_attempt_skips_non_actionable_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()

    class _RowResult:
        def one_or_none(self):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return _RowResult()

    async def fail_publish(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("non-actionable stale rows must not publish")

    monkeypatch.setattr(generation, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(generation, "publish_event", fail_publish)

    ok = await generation._maybe_requeue_stale_generation_attempt(
        redis,
        task_id="gen-1",
        attempt=2,
        reason="superseded",
        delay=1.0,
    )

    assert ok is False
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_redis_semaphore_does_not_fallback_to_non_atomic_incr() -> None:
    class _Redis:
        async def eval(self, *_args) -> None:
            raise RuntimeError("lua disabled")

        async def incr(self, *_args) -> int:
            raise AssertionError("non-atomic fallback must not be used")

    sem = generation._RedisSemaphore(_Redis(), "sem:test", 1, wait_s=0)

    with pytest.raises(generation.UpstreamError) as exc_info:
        await sem.__aenter__()

    assert exc_info.value.error_code == generation.EC.LOCAL_QUEUE_FULL.value


@pytest.mark.asyncio
async def test_redis_semaphore_sets_ttl_and_releases_with_lua() -> None:
    class _Redis:
        def __init__(self) -> None:
            self.eval_calls: list[tuple[Any, ...]] = []

        async def eval(self, *args: Any) -> int:
            self.eval_calls.append(args)
            if args[0] == generation._ACQUIRE_LUA:
                return 1
            if args[0] == generation._RELEASE_LUA:
                return 0
            raise AssertionError("unexpected lua script")

        async def decr(self, *_args: Any) -> int:
            raise AssertionError("release must use lua")

    redis = _Redis()

    async with generation._RedisSemaphore(redis, "sem:test", 2, wait_s=0):
        pass

    assert redis.eval_calls[0] == (
        generation._ACQUIRE_LUA,
        1,
        "sem:test",
        2,
        generation._IMAGE_SEMAPHORE_KEY_TTL_S,
    )
    assert redis.eval_calls[1] == (generation._RELEASE_LUA, 1, "sem:test")


@pytest.mark.asyncio
async def test_image_queue_kick_skips_not_before_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    redis.store[generation._image_queue_not_before_key("gen-old")] = str(
        time.time() + 60
    )

    async def fake_queued_generation_ids(_limit: int) -> list[str]:
        return ["gen-old", "gen-ready"]

    monkeypatch.setattr(
        generation, "_queued_generation_ids", fake_queued_generation_ids
    )

    await generation._kick_image_queue(redis)

    assert [args[0] for _name, args, _kwargs in redis.enqueued] == ["gen-ready"]


@pytest.mark.asyncio
async def test_image_queue_does_not_select_provider_when_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool

    redis = FakeRedis()
    redis.zsets[generation._IMAGE_QUEUE_ACTIVE_KEY] = {
        "acc1": time.time() + 60,
        "acc2": time.time() + 60,
    }
    monkeypatch.setattr(generation, "_image_queue_capacity", lambda: 2)

    async def fail_get_pool():
        raise AssertionError("provider pool should not be touched when queue is full")

    monkeypatch.setattr(provider_pool, "get_pool", fail_get_pool)

    reserved = await generation._reserve_image_queue_slot(redis, "gen-1")

    assert reserved is None


@pytest.mark.asyncio
async def test_image_queue_reserves_different_provider_and_blocks_duplicate_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool

    redis = FakeRedis()
    # Simulate acc1 already running another task at default image_concurrency=1.
    # In the per-provider ZSET model that means: a task_id sits in
    # ``_image_provider_active_key("acc1")`` and the global active set has the
    # corresponding task_id member with a future expiry score.
    other_task_expiry = time.time() + 60
    redis.zsets[generation._image_provider_active_key("acc1")] = {
        "other-task": other_task_expiry,
    }
    redis.zsets[generation._IMAGE_QUEUE_ACTIVE_KEY] = {
        "other-task": other_task_expiry,
    }

    async def fake_ready_generation_ids(_redis, _limit: int) -> list[str]:
        return ["gen-1"]

    class _Pool:
        async def select(
            self,
            *,
            route: str,
            task_id: str | None = None,
            endpoint_kind: str | None = None,
        ):
            assert route == "image"
            assert task_id == "gen-1"
            return [
                SimpleNamespace(
                    name="acc1",
                    base_url="https://upstream.test",
                    api_key="k1",
                    image_concurrency=1,
                ),
                SimpleNamespace(
                    name="acc2",
                    base_url="https://upstream.test",
                    api_key="k2",
                    image_concurrency=1,
                ),
            ]

    async def fake_get_pool():
        return _Pool()

    monkeypatch.setattr(generation, "_image_queue_capacity", lambda: 4)
    monkeypatch.setattr(
        generation, "_ready_queued_generation_ids", fake_ready_generation_ids
    )
    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    reserved = await generation._reserve_image_queue_slot(redis, "gen-1")
    duplicate = await generation._reserve_image_queue_slot(redis, "gen-1")

    assert reserved is not None
    assert reserved.name == "acc2"
    assert duplicate is None
    assert "gen-1" in redis.zsets[generation._image_provider_active_key("acc2")]
    assert redis.store[generation._image_task_provider_key("gen-1")] == "acc2"
    assert "gen-1" in redis.zsets[generation._IMAGE_QUEUE_ACTIVE_KEY]


@pytest.mark.asyncio
async def test_image_queue_reservation_survives_lock_release_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ReleaseBrokenRedis(FakeRedis):
        async def incrby(self, key: str, amount: int) -> int:
            value = int(self.store.get(key, "0")) + int(amount)
            self.store[key] = str(value)
            return value

        async def eval(self, *args: Any) -> int:
            if args[0] == generation._RELEASE_LEASE_LUA:
                raise RuntimeError("owner-CAS release unavailable")
            return await super().eval(*args)

    redis = ReleaseBrokenRedis()
    provider = SimpleNamespace(
        name="acc1",
        base_url="https://upstream.test",
        api_key="k1",
        image_concurrency=1,
    )

    async def fake_ready_generation_ids(_redis, _limit: int) -> list[str]:
        return ["gen-1"]

    monkeypatch.setattr(generation, "_image_queue_capacity", lambda: 4)
    monkeypatch.setattr(
        generation, "_ready_queued_generation_ids", fake_ready_generation_ids
    )

    with caplog.at_level("ERROR", logger=generation.logger.name):
        reserved = await generation._reserve_image_queue_slot(
            redis,
            "gen-1",
            provider_override=provider,
        )

    assert reserved is provider
    assert redis.store[generation._image_task_provider_key("gen-1")] == "acc1"
    assert "gen-1" in redis.zsets[generation._image_provider_active_key("acc1")]
    assert "gen-1" in redis.zsets[generation._IMAGE_QUEUE_ACTIVE_KEY]
    assert redis.store[generation._IMAGE_QUEUE_LANE_CURSOR_KEY] == "1"
    assert "preserving critical-section result" in caplog.text


@pytest.mark.asyncio
async def test_image_queue_reserve_rejects_non_atomic_lock_release(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class NonAtomicRedis(FakeRedis):
        eval = None

    redis = NonAtomicRedis()

    with caplog.at_level("ERROR", logger=generation.logger.name):
        with pytest.raises(generation.UpstreamError) as exc_info:
            await generation._reserve_image_queue_slot(redis, "gen-1")

    assert exc_info.value.error_code == generation.EC.LOCAL_QUEUE_FULL.value
    assert exc_info.value.payload["retry_after"] > 0
    assert "requires Redis EVAL or WATCH transaction" in str(exc_info.value)
    assert "refused without atomic release support" in caplog.text
    assert generation._IMAGE_QUEUE_LOCK_KEY not in redis.store


@pytest.mark.asyncio
async def test_image_queue_provider_active_count_failure_defers_without_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool

    class ActiveCountBrokenRedis(FakeRedis):
        async def zremrangebyscore(self, key: str, min_score, max_score) -> int:
            if key == generation._image_provider_active_key("acc1"):
                raise RuntimeError("redis down")
            return await super().zremrangebyscore(key, min_score, max_score)

    redis = ActiveCountBrokenRedis()

    async def fake_ready_generation_ids(_redis, _limit: int) -> list[str]:
        return ["gen-1"]

    class _Pool:
        async def select(self, **kwargs):
            assert kwargs["route"] == "image"
            assert kwargs["task_id"] == "gen-1"
            return [
                SimpleNamespace(
                    name="acc1",
                    base_url="https://upstream.test",
                    api_key="k1",
                    image_concurrency=1,
                )
            ]

    async def fake_get_pool():
        return _Pool()

    monkeypatch.setattr(generation, "_image_queue_capacity", lambda: 4)
    monkeypatch.setattr(
        generation, "_ready_queued_generation_ids", fake_ready_generation_ids
    )
    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    reserved = await generation._reserve_image_queue_slot(redis, "gen-1")

    assert reserved is None
    assert generation._image_task_provider_key("gen-1") not in redis.store
    assert "gen-1" not in redis.zsets.get(generation._IMAGE_QUEUE_ACTIVE_KEY, {})
    assert generation._image_queue_not_before_key("gen-1") in redis.store


@pytest.mark.asyncio
async def test_image_queue_per_provider_concurrency_admits_multiple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider with image_concurrency=3 should accept 3 concurrent tasks."""
    from app import provider_pool

    redis = FakeRedis()

    queue: list[str] = ["gen-1", "gen-2", "gen-3", "gen-4"]

    async def fake_ready_generation_ids(_redis, _limit: int) -> list[str]:
        return queue[:1] if queue else []

    class _Pool:
        async def select(
            self,
            *,
            route: str,
            task_id: str | None = None,
            endpoint_kind: str | None = None,
        ):
            assert route == "image"
            assert task_id in queue
            return [
                SimpleNamespace(
                    name="solo",
                    base_url="https://upstream.test",
                    api_key="k",
                    image_concurrency=3,
                ),
            ]

    async def fake_get_pool():
        return _Pool()

    monkeypatch.setattr(generation, "_image_queue_capacity", lambda: 10)
    monkeypatch.setattr(
        generation, "_ready_queued_generation_ids", fake_ready_generation_ids
    )
    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    admitted = []
    for _ in range(4):
        if not queue:
            break
        task_id = queue[0]
        reserved = await generation._reserve_image_queue_slot(redis, task_id)
        if reserved is None:
            break
        admitted.append((task_id, reserved.name))
        queue.pop(0)

    assert [name for _, name in admitted] == ["solo", "solo", "solo"]
    # 4th task can't be admitted — concurrency cap reached on the only provider.
    assert "gen-4" in queue
    assert len(redis.zsets[generation._image_provider_active_key("solo")]) == 3


def test_image_queue_capacity_allows_high_provider_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation.settings, "image_generation_concurrency", 20)

    assert generation._image_queue_capacity() == 20


@pytest.mark.asyncio
async def test_image_queue_capacity_uses_runtime_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation.settings, "image_generation_concurrency", 4)

    async def fake_resolve(key: str) -> str:
        assert key == "image.generation_concurrency"
        return "12"

    monkeypatch.setattr(generation.runtime_settings, "resolve", fake_resolve)

    assert await generation._resolve_image_queue_capacity() == 12


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_message_status", "expected_message_status"),
    [
        (None, MessageStatus.FAILED),
        (MessageStatus.CANCELED, MessageStatus.CANCELED),
    ],
)
async def test_mark_generation_attempt_failed_preserves_canceled_message(
    monkeypatch: pytest.MonkeyPatch,
    initial_message_status: MessageStatus | None,
    expected_message_status: MessageStatus,
) -> None:
    message = FakeMessage()
    message.status = initial_message_status
    published: list[dict] = []

    class _Session:
        committed = False
        added: list[Any]

        def __init__(self) -> None:
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult(1)

        async def get(self, model, _message_id: str):
            # Return the FakeMessage for Message lookups; return None for
            # Generation lookups so the new release-on-failure path skips
            # cleanly (no real Generation row to release).
            if getattr(model, "__name__", "") == "Generation":
                return None
            return message

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            self.committed = True

    session = _Session()

    async def fake_deliver_generation_event(redis, delivery):
        _event_id, _kind, payload = delivery
        published.append(
            {
                "redis": redis,
                "user_id": payload["user_id"],
                "channel": payload["channel"],
                "event_name": payload["event_name"],
                "data": payload["data"],
            }
        )

    monkeypatch.setattr(generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        generation,
        "_deliver_generation_event",
        fake_deliver_generation_event,
    )

    ok = await generation._mark_generation_attempt_failed(
        object(),
        task_id="gen-1",
        message_id="msg-1",
        user_id="user-1",
        attempt=2,
        error_code="retry_enqueue_failed",
        error_message="failed to enqueue retry",
        retriable=False,
    )

    assert ok is True
    assert session.committed is True
    assert message.status == expected_message_status
    assert published[0]["event_name"] == EV_GEN_FAILED
    assert published[0]["data"]["code"] == "retry_enqueue_failed"


@pytest.mark.asyncio
async def test_write_generation_files_deletes_created_keys_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = FakeStorage(fail_keys={"bad"})
    monkeypatch.setattr(generation, "storage", fake_storage)

    with pytest.raises(StorageDiskFullError):
        await generation._write_generation_files([("ok", b"1"), ("bad", b"2")])

    assert set(fake_storage.put_keys) == {"ok", "bad"}
    assert fake_storage.deleted == ["ok"]


@pytest.mark.asyncio
async def test_write_generation_files_cleanup_continues_when_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = FakeStorage(fail_keys={"bad"}, fail_delete_keys={"ok1"})
    monkeypatch.setattr(generation, "storage", fake_storage)

    with pytest.raises(StorageDiskFullError):
        await generation._write_generation_files(
            [("ok1", b"1"), ("bad", b"2"), ("ok2", b"3")]
        )

    assert set(fake_storage.deleted) == {"ok1", "ok2"}


@pytest.mark.asyncio
async def test_cleanup_storage_on_error_deletes_created_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_storage = FakeStorage()
    monkeypatch.setattr(generation, "storage", fake_storage)

    with pytest.raises(RuntimeError, match="commit failed"):
        async with generation._cleanup_storage_on_error(["orig", "display"]):
            raise RuntimeError("commit failed")

    assert set(fake_storage.deleted) == {"orig", "display"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupt", "expected_exception"),
    [
        ("lease", generation._LeaseLost),
        ("cancel", generation._TaskCancelled),
    ],
)
async def test_generation_write_interrupt_cleans_real_storage_and_allows_retry(
    interrupt: str,
    expected_exception: type[BaseException],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_storage = LocalStorage(tmp_path)
    write_started = threading.Event()
    allow_write = threading.Event()
    original_put = local_storage.put_bytes_result

    def blocking_put(key: str, data: bytes):
        write_started.set()
        if not allow_write.wait(timeout=5):
            raise TimeoutError("test storage write was not released")
        return original_put(key, data)

    monkeypatch.setattr(local_storage, "put_bytes_result", blocking_put)
    monkeypatch.setattr(generation, "storage", local_storage)

    redis = FakeRedis()
    lease_lost = asyncio.Event()
    key = "u/user-1/g/gen-write-race/orig.png"
    guarded_write = asyncio.create_task(
        generation._await_with_lease_guard(
            generation._write_generation_files([(key, b"first-attempt")]),
            lease_lost,
            redis=redis,
            task_id="gen-write-race",
            cancel_poll_interval_s=0.01,
        )
    )

    assert await asyncio.to_thread(write_started.wait, 2)
    if interrupt == "lease":
        lease_lost.set()
    else:
        await redis.set("task:gen-write-race:cancel", "1")
    await asyncio.sleep(0.05)
    allow_write.set()

    with pytest.raises(expected_exception):
        await guarded_write

    assert not local_storage.path_for(key).exists()

    retry_storage = LocalStorage(tmp_path)
    monkeypatch.setattr(generation, "storage", retry_storage)
    assert await generation._write_generation_files([(key, b"retry")]) == [key]
    assert retry_storage.get_bytes(key) == b"retry"


@pytest.mark.asyncio
async def test_cleanup_storage_on_custom_base_exception_waits_for_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_storage = LocalStorage(tmp_path)
    key = "u/user-1/g/gen-base-exception/orig.png"
    local_storage.put_bytes(key, b"image")
    monkeypatch.setattr(generation, "storage", local_storage)

    with pytest.raises(generation._TaskCancelled):
        async with generation._cleanup_storage_on_error([key]):
            raise generation._TaskCancelled("cancelled during persistence")

    assert not local_storage.path_for(key).exists()


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ModelLibraryHookSession:
    def __init__(self, run, step) -> None:
        self.run = run
        self.step = step
        self.calls = 0
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        self.calls += 1
        return _ScalarResult(self.run if self.calls == 1 else self.step)


def _model_library_generation(task_id: str = "task-2") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        upstream_request={
            "workflow_action": "model_library_generate",
            "workflow_step_key": "model_library_generate",
            "workflow_run_id": "run-1",
        },
    )


@pytest.mark.asyncio
async def test_model_library_generate_hook_waits_for_all_multi_gender_tasks() -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=["img-1"],
        input_json={
            "count": 2,
            "count_per_gender": 2,
            "genders": ["female", "male"],
            "auto_tag": False,
        },
        output_json={},
        task_ids=["task-1", "task-2", "task-3", "task-4"],
        status="running",
    )
    session = _ModelLibraryHookSession(run, step)

    await generation._maybe_record_model_library_generate_image(
        session=session,
        user_id="user-1",
        generation=_model_library_generation(),
        image_id="img-2",
    )

    assert step.image_ids == ["img-1", "img-2"]
    assert step.status == "running"
    assert run.status == "running"
    rendered = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in rendered


@pytest.mark.asyncio
async def test_model_library_generate_hook_completes_after_all_tasks() -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=["img-1", "img-2", "img-3"],
        input_json={
            "count": 2,
            "count_per_gender": 2,
            "genders": ["female", "male"],
            "auto_tag": False,
        },
        output_json={},
        task_ids=["task-1", "task-2", "task-3", "task-4"],
        status="running",
    )
    session = _ModelLibraryHookSession(run, step)

    await generation._maybe_record_model_library_generate_image(
        session=session,
        user_id="user-1",
        generation=_model_library_generation("task-4"),
        image_id="img-4",
    )

    assert step.image_ids == ["img-1", "img-2", "img-3", "img-4"]
    assert step.status == "succeeded"
    assert run.status == "completed"
    assert run.current_step == "model_library_generate"


def test_workflow_hook_facade_keeps_requested_count_alias() -> None:
    assert (
        generation._model_library_requested_count_from_step
        is workflow_hooks.model_library_requested_count_from_step
    )


@pytest.mark.asyncio
async def test_model_library_hook_injects_current_requested_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=[],
        input_json={"count": 99, "auto_tag": False},
        output_json={},
        task_ids=[],
        status="running",
    )
    session = _ModelLibraryHookSession(run, step)
    monkeypatch.setattr(
        generation,
        "_model_library_requested_count_from_step",
        lambda _step: 1,
    )

    await generation._maybe_record_model_library_generate_image(
        session=session,
        user_id="user-1",
        generation=_model_library_generation(),
        image_id="img-1",
    )

    assert step.status == "succeeded"
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_model_library_hook_propagates_tagger_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=[],
        input_json={"count": 1, "auto_tag": True},
        output_json={},
        task_ids=["task-1"],
        status="running",
    )
    session = _ModelLibraryHookSession(run, step)

    async def cancel_tagger(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        generation,
        "_load_model_library_tagger",
        lambda: cancel_tagger,
    )

    with pytest.raises(asyncio.CancelledError):
        await generation._maybe_record_model_library_generate_image(
            session=session,
            user_id="user-1",
            generation=_model_library_generation(),
            image_id="img-1",
        )


@pytest.mark.asyncio
async def test_model_library_candidate_hook_locks_step_output_json_row() -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=["winner-img"],
        input_json={},
        output_json={"dual_race_bonus_image_ids": ["bonus-1"]},
        task_ids=[],
        status="running",
    )
    session = _ModelLibraryHookSession(run, step)

    await generation._maybe_record_model_library_candidate_image(
        session=session,
        user_id="user-1",
        parent_upstream_request={
            "workflow_action": "model_library_generate",
            "workflow_step_key": "model_library_generate",
            "workflow_run_id": "run-1",
        },
        bonus_image_id="bonus-2",
    )

    assert step.output_json["dual_race_bonus_image_ids"] == ["bonus-1", "bonus-2"]
    rendered = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in rendered


class _PosterStyleLibraryHookSession:
    """Session mock：按 execute 顺序返回 run / step / existing_item 结果。"""

    def __init__(self, *, run, step, existing_item=None) -> None:
        self.run = run
        self.step = step
        self.existing_item = existing_item
        self.added: list = []
        self.flush_calls = 0
        self._scalar_queue = [run, step, existing_item]
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        value = self._scalar_queue.pop(0) if self._scalar_queue else None
        return _ScalarResult(value)

    def add(self, item) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flush_calls += 1


def _poster_style_generation(task_id: str = "task-2") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        upstream_request={
            "workflow_action": "poster_style_library_generate",
            "workflow_step_key": "poster_style_library_generate",
            "workflow_run_id": "run-1",
        },
    )


@pytest.mark.asyncio
async def test_poster_style_library_hook_inserts_item_and_keeps_step_running() -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=["img-1"],
        input_json={
            "title": "复古印刷波普",
            "category": "retro",
            "style_tags": ["复古", "波普"],
            "palette": ["#FF6B35", "#1A1A1A"],
            "recommended_aspects": ["1:1", "9:16"],
            "mood": "撞色印刷感",
            "prompt": "retro pop print poster",
            "prompt_template": None,
            "count": 4,
            "auto_tag": False,
        },
        output_json={},
        task_ids=["task-1", "task-2", "task-3", "task-4"],
        status="running",
    )
    session = _PosterStyleLibraryHookSession(run=run, step=step, existing_item=None)

    await generation._maybe_record_poster_style_library_generate_image(
        session=session,
        user_id="user-1",
        generation=_poster_style_generation(),
        image_id="img-2",
    )

    assert step.image_ids == ["img-1", "img-2"]
    assert step.status == "running"
    assert run.status == "running"
    assert len(session.added) == 1
    inserted = session.added[0]
    assert inserted.cover_image_id == "img-2"
    assert inserted.sample_image_ids == ["img-2"]
    assert inserted.title == "复古印刷波普"
    assert inserted.category == "retro"
    assert inserted.palette == ["#FF6B35", "#1A1A1A"]
    assert inserted.source == "generated"
    assert inserted.user_id == "user-1"
    assert inserted.id.startswith("user:")
    assert inserted.metadata_jsonb["workflow_run_id"] == "run-1"
    assert session.flush_calls == 1
    rendered = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in rendered


@pytest.mark.asyncio
async def test_poster_style_library_hook_completes_step_when_all_tasks_done() -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=["img-1", "img-2", "img-3"],
        input_json={
            "title": "极简",
            "category": "minimal",
            "style_tags": [],
            "palette": [],
            "recommended_aspects": [],
            "mood": None,
            "prompt": "minimal poster",
            "prompt_template": None,
            "count": 4,
            "auto_tag": False,
        },
        output_json={},
        task_ids=["task-1", "task-2", "task-3", "task-4"],
        status="running",
    )
    session = _PosterStyleLibraryHookSession(run=run, step=step, existing_item=None)

    await generation._maybe_record_poster_style_library_generate_image(
        session=session,
        user_id="user-1",
        generation=_poster_style_generation("task-4"),
        image_id="img-4",
    )

    assert step.image_ids == ["img-1", "img-2", "img-3", "img-4"]
    assert step.status == "succeeded"
    assert run.status == "completed"
    assert run.current_step == "poster_style_library_generate"
    assert len(session.added) == 1
    assert session.added[0].cover_image_id == "img-4"
    assert session.added[0].recommended_aspects == ["1:1", "9:16", "16:9", "3:4"]


@pytest.mark.asyncio
async def test_poster_style_library_hook_no_op_for_unrelated_workflow_action() -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=[],
        input_json={"count": 2, "auto_tag": False},
        output_json={},
        task_ids=[],
        status="running",
    )
    session = _PosterStyleLibraryHookSession(run=run, step=step)
    unrelated = SimpleNamespace(
        id="task-x",
        upstream_request={"workflow_action": "model_library_generate"},
    )

    await generation._maybe_record_poster_style_library_generate_image(
        session=session,
        user_id="user-1",
        generation=unrelated,
        image_id="img-99",
    )

    assert step.image_ids == []
    assert step.status == "running"
    assert run.status == "running"
    assert session.added == []


@pytest.mark.asyncio
async def test_poster_style_library_hook_skips_duplicate_cover_image() -> None:
    run = SimpleNamespace(id="run-1", status="running", current_step="")
    step = SimpleNamespace(
        image_ids=["img-1"],
        input_json={
            "title": "试样",
            "category": "minimal",
            "count": 2,
            "auto_tag": False,
        },
        output_json={},
        task_ids=["task-1", "task-2"],
        status="running",
    )
    existing = SimpleNamespace(
        id="user:existing",
        cover_image_id="img-1",
        sample_image_ids=["img-1"],
        category="minimal",
        style_tags=[],
        palette=[],
        mood=None,
        auto_tagged_at=None,
        auto_tag_notes=None,
        metadata_jsonb={},
    )
    session = _PosterStyleLibraryHookSession(run=run, step=step, existing_item=existing)

    await generation._maybe_record_poster_style_library_generate_image(
        session=session,
        user_id="user-1",
        generation=_poster_style_generation("task-1"),
        image_id="img-1",
    )

    # existing 已存在则不应新插入；image_ids 也不重复
    assert step.image_ids == ["img-1"]
    assert session.added == []
    assert session.flush_calls == 0


class _PosterWorkflowHookSession:
    def __init__(self, *, run: Any, row: Any) -> None:
        self.run = run
        self.row = row
        self.get_calls: list[tuple[Any, str]] = []

    async def execute(self, _statement: Any) -> _ScalarResult:
        return _ScalarResult(self.run)

    async def get(self, model: Any, key: str) -> Any:
        self.get_calls.append((model, key))
        return self.row


@pytest.mark.asyncio
async def test_poster_workflow_hook_preserves_master_image_and_marks_ready() -> None:
    run = SimpleNamespace(id="run-1")
    master = SimpleNamespace(
        workflow_run_id="run-1",
        image_id="existing-image",
        status="generating",
    )
    session = _PosterWorkflowHookSession(run=run, row=master)
    generation_row = SimpleNamespace(
        upstream_request={
            "workflow_type": "poster_design",
            "workflow_action": "poster_master",
            "workflow_run_id": "run-1",
            "workflow_master_id": "master-1",
        }
    )

    await generation._maybe_record_poster_workflow_image(
        session=session,
        user_id="user-1",
        generation=generation_row,
        image_id="new-image",
    )

    assert master.image_id == "existing-image"
    assert master.status == "ready"
    assert session.get_calls == [(generation.PosterMaster, "master-1")]


@pytest.mark.asyncio
async def test_poster_workflow_hook_replaces_render_image_and_marks_ready() -> None:
    run = SimpleNamespace(id="run-1")
    render = SimpleNamespace(
        workflow_run_id="run-1",
        image_id="old-image",
        status="revising",
    )
    session = _PosterWorkflowHookSession(run=run, row=render)
    generation_row = SimpleNamespace(
        upstream_request={
            "workflow_type": "poster_design",
            "workflow_action": "poster_inpaint",
            "workflow_run_id": "run-1",
            "workflow_render_id": "render-1",
        }
    )

    await generation._maybe_record_poster_workflow_image(
        session=session,
        user_id="user-1",
        generation=generation_row,
        image_id="new-image",
    )

    assert render.image_id == "new-image"
    assert render.status == "ready"
    assert session.get_calls == [(generation.PosterRender, "render-1")]


def test_run_generation_records_workflows_before_billing_and_commit() -> None:
    hook_source = inspect.getsource(generation_success._record_success_hooks)
    model_hook = hook_source.index("_maybe_record_model_library_generate_image")
    poster_hook = hook_source.index(
        "_maybe_record_poster_workflow_image",
        model_hook,
    )
    style_hook = hook_source.index(
        "_maybe_record_poster_style_library_generate_image",
        poster_hook,
    )
    assert model_hook < poster_hook < style_hook

    persistence_source = inspect.getsource(
        generation_success._persist_generation_success
    )
    hooks = persistence_source.index("_record_success_hooks(")
    settle = persistence_source.index(
        "worker_billing.settle_generation(",
        hooks,
    )
    commit = persistence_source.index("await session.commit()", settle)
    assert hooks < settle < commit


@pytest.mark.asyncio
async def test_await_with_lease_guard_aborts_work() -> None:
    lease_lost = asyncio.Event()
    started = asyncio.Event()

    async def slow_work() -> str:
        started.set()
        await asyncio.sleep(10)
        return "done"

    task = asyncio.create_task(
        generation._await_with_lease_guard(slow_work(), lease_lost)
    )
    await started.wait()
    lease_lost.set()

    with pytest.raises(generation._LeaseLost):
        await task
