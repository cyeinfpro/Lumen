from __future__ import annotations

# ruff: noqa: E402

import asyncio
import inspect
import io
import os
import sys
import tempfile
import threading
import time
from dataclasses import replace
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
    RETRY_BACKOFF_SECONDS,
    GenerationErrorCode as EC,
    GenerationStatus,
    MessageStatus,
)
from lumen_core.models import PosterMaster, PosterRender
from app.background_removal.local_chroma import (
    recover_solid_background_transparency,
)
from app.config import settings
from app.provider_runtime.errors import UpstreamError
from app import runtime_settings
from app.storage import LocalStorage, StorageDiskFullError, StoragePutResult

from app.tasks.generation_parts import (
    image_artifact_contracts,
    lease as generation_lease,
    lifecycle,
    persistence,
    queue as generation_queue,
    queue_claim,
    request_options,
    retry_state,
    success as generation_success,
    workflow_service,
    workflow_hooks,
)
from app import generation_dispatch
from app.tasks.generation_parts.default_runtime import build_generation_runtime
from app.tasks.generation_parts.composition_ports import (
    DefaultGenerationArtifacts,
)
from app.tasks.generation_parts.errors import (
    LeaseLost,
    StaleGenerationAttempt,
    TaskCancelled,
)


generation_runtime = build_generation_runtime()
generation_services = generation_runtime.deps


class _SessionStore:
    def __init__(self, session: Any) -> None:
        self._session = session

    def session(self) -> Any:
        return self._session


class _FakeEvents:
    def __init__(
        self,
        *,
        publish: Any | None = None,
        deliver: Any | None = None,
    ) -> None:
        self._publish = publish
        self._deliver = deliver

    async def publish(self, *args: Any, **kwargs: Any) -> None:
        if self._publish is not None:
            await self._publish(*args, **kwargs)

    async def deliver(self, *args: Any, **kwargs: Any) -> None:
        if self._deliver is not None:
            await self._deliver(*args, **kwargs)

    async def deliver_many(self, redis: Any, deliveries: list[Any]) -> None:
        for delivery in deliveries:
            await self.deliver(redis, delivery)


class _NoopBilling:
    async def release(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def settle(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def flush_after_commit(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def settle_unknown_upstream(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        return None


class _QueueService:
    expose_provider_diagnostics = False

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.provider_cooldowns: dict[str, float] = {}

    def configured_capacity(self) -> int:
        return self.capacity

    async def resolve_capacity(self) -> int:
        return self.capacity


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
        self.get_calls = 0
        self.mget_calls = 0

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
        self.get_calls += 1
        return self.store.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        self.mget_calls += 1
        return [self.store.get(key) for key in keys]

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
        if script == generation_dispatch.BEGIN_DISPATCH_LUA:
            (
                active_key,
                revision_key,
                attempt,
                _ttl,
                _revision_ttl,
                replace_value,
            ) = args[2:8]
            current = self.store.get(active_key)
            if current is not None:
                current_attempt = int(current.split("|", 1)[0])
                if current_attempt > int(attempt):
                    return [0, current]
                if current_attempt == int(attempt) and current != replace_value:
                    return [0, current]
            revision = await self.incrby(revision_key, 1)
            value = f"{attempt}|{revision}|reserved|"
            self.store[active_key] = value
            return [1, value]
        if script == generation_dispatch.MARK_DISPATCH_ENQUEUED_LUA:
            active_key, reserved_value, enqueued_value, _ttl = args[2:6]
            if self.store.get(active_key) != reserved_value:
                return 0
            self.store[active_key] = enqueued_value
            return 1
        if script == generation_dispatch.CONSUME_DISPATCH_LUA:
            active_key, prefix, worker_id, _ttl = args[2:6]
            current = self.store.get(active_key)
            if current is None or not current.startswith(prefix):
                return 0
            phase = current.split("|", 3)[2]
            if phase not in {"reserved", "enqueued"}:
                return 0
            self.store[active_key] = f"{prefix}consumed|{worker_id}"
            return 1
        if script == generation_dispatch.FINISH_DISPATCH_LUA:
            active_key, prefix = args[2:4]
            current = self.store.get(active_key)
            if current is None or not current.startswith(prefix):
                return 0
            return await self.delete(active_key)
        if script == generation_lease.RELEASE_LEASE_LUA:
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
        if script != queue_claim.RESERVE_IMAGE_SLOT_LUA:
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

    with pytest.raises(TaskCancelled):
        await persistence.ensure_generation_conversation_alive(
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

    with pytest.raises(TaskCancelled):
        await retry_state.await_with_lease_guard(
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
        TaskCancelled,
        match="cancelled after upstream result",
    ):
        await lifecycle.raise_if_generation_interrupted(
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
    assert "g.provider.postprocess(" in inspect.getsource(
        generation_success._postprocess_generated_image
    )

    storage_source = inspect.getsource(generation_success._write_artifact_files)
    storage_guard = storage_source.index('"cancelled before storage write"')
    storage_write = storage_source.index(
        "g.artifacts.write_files(",
        storage_guard,
    )
    lease_guard = storage_source.rindex(
        "await_with_lease_guard(",
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
        "ensure_generation_attempt_current(",
        persistence_guard,
    )
    billing_guard = persistence_source.index(
        '"cancelled before billing settlement"',
        attempt_fence,
    )
    settle = persistence_source.index(
        "g.billing.settle(",
        billing_guard,
    )
    commit = persistence_source.index("await session.commit()", settle)
    assert persistence_guard < attempt_fence < billing_guard
    assert billing_guard < settle < commit
    # 审计 D-2：settle 之后到 commit 之间不允许再有任何中断检查。此处抛异常会把
    # 钱包流水连同已产出（且已计费）的上游图一起回滚，failure handler 随后走
    # release 分支，等于平台替用户吸收上游成本。中断只能发生在 settle 之前。
    assert '"cancelled before success commit"' not in persistence_source[settle:commit]


def test_existing_image_retry_checks_cancel_before_success_settlement() -> None:
    source = inspect.getsource(lifecycle.settle_existing_generated_image)

    cancel_check = source.index("if await is_cancelled(redis, task_id):")
    release = source.index("await services.billing.release(")
    success_update = source.index("status=GenerationStatus.SUCCEEDED.value")
    settle = source.index("await services.billing.settle(")

    assert cancel_check < release < success_update < settle


def test_classify_disk_full_as_retriable() -> None:
    decision = retry_state.classify_exception(
        StorageDiskFullError("u/user/g/gen/orig.png"), has_partial=False
    )

    assert decision.retriable is True
    assert "disk_full" in decision.reason


def test_classify_generation_timeout_as_retriable() -> None:
    decision = retry_state.classify_exception(TimeoutError(), has_partial=False)

    assert decision.retriable is True
    assert "timeout" in decision.reason


def test_display_variant_preserves_alpha_for_transparent_png() -> None:
    src = PILImage.new("RGBA", (16, 16), (255, 0, 0, 0))
    src.putpixel((0, 0), (255, 0, 0, 255))

    data, size = image_artifact_contracts.make_display(src)

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

    assert image_artifact_contracts.compute_blurhash(tiny) is None  # noqa: SLF001


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
    options = request_options.image_request_options(
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
    with pytest.raises(StaleGenerationAttempt):
        retry_state.ensure_generation_updated(FakeResult(0), "gen-1", 2)

    retry_state.ensure_generation_updated(FakeResult(1), "gen-1", 2)


def test_validate_resolved_size_accepts_valid_preset() -> None:
    assert request_options.validate_resolved_size("3840x2160", "16:9") == (3840, 2160)


def test_validate_resolved_size_rejects_hard_limit_violation() -> None:
    with pytest.raises(ValueError, match="longest side"):
        request_options.validate_resolved_size("3856x2160", "16:9")


def test_validate_resolved_size_rejects_aspect_drift() -> None:
    with pytest.raises(ValueError, match="aspect ratio drift"):
        request_options.validate_resolved_size("1024x1024", "16:9")


def test_validate_resolved_size_can_skip_aspect_drift_for_fixed_size() -> None:
    assert request_options.validate_resolved_size(
        "1024x1024",
        "16:9",
        validate_aspect_ratio=False,
    ) == (1024, 1024)


def test_prompt_with_aspect_ratio_constraint_adds_square_guard() -> None:
    prompt = request_options.prompt_with_aspect_ratio_constraint(
        "画一张活动分享图",
        "1:1",
    )

    assert "strict 1:1 ratio" in prompt
    assert "square canvas" in prompt
    assert "poster" in prompt


def test_retry_delay_adds_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_state.random, "uniform", lambda low, high: high)

    assert retry_state.retry_delay_seconds(1) == pytest.approx(12.0)


def test_retry_backoff_grows_after_configured_table() -> None:
    first_tail_attempt = len(RETRY_BACKOFF_SECONDS) + 1
    assert retry_state.base_retry_backoff_seconds(first_tail_attempt) == (
        RETRY_BACKOFF_SECONDS[-1] * 2
    )


def test_safe_generation_error_details_keeps_transparent_context_only() -> None:
    exc = UpstreamError(
        "transparent material pipeline failed",
        error_code=EC.BAD_RESPONSE.value,
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

    assert retry_state.safe_generation_error_details(exc) == {
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
    assert request_options.primary_input_image_id_valid(None, []) is True
    assert request_options.primary_input_image_id_valid("img-1", ["img-1"]) is True
    assert request_options.primary_input_image_id_valid("img-2", ["img-1"]) is False


@pytest.mark.asyncio
async def test_lease_renewer_sets_event_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    attempts = 0

    class _Redis:
        async def eval(self, *_args) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("redis down")

    async def _sleep(delay: float) -> None:
        nonlocal clock
        clock += delay

    monkeypatch.setattr(generation_lease, "LEASE_TTL_S", 4)
    monkeypatch.setattr(generation_lease, "LEASE_RENEW_S", 0)
    monkeypatch.setattr(generation_lease, "LEASE_RENEW_RETRY_S", 0.25)
    lease_lost = asyncio.Event()

    await generation_lease.lease_renewer(
        _Redis(),
        "gen-1",
        "worker-1",
        lease_lost,
        monotonic=lambda: clock,
        sleep=_sleep,
    )

    assert lease_lost.is_set()
    assert clock == pytest.approx(3.0)
    assert attempts == 12


@pytest.mark.asyncio
async def test_lease_renewer_times_out_a_stalled_redis_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Redis:
        async def eval(self, *_args) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(generation_lease, "LEASE_TTL_S", 0.04)
    monkeypatch.setattr(generation_lease, "LEASE_RENEW_S", 0)
    lease_lost = asyncio.Event()

    await asyncio.wait_for(
        generation_lease.lease_renewer(
            _Redis(),
            "gen-stalled",
            "worker-1",
            lease_lost,
        ),
        timeout=0.5,
    )

    assert lease_lost.is_set()


@pytest.mark.asyncio
async def test_lease_renewer_uses_redis_time_for_queue_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zadds: list[tuple[str, dict[str, float]]] = []

    class _Redis:
        async def eval(self, *_args) -> int:
            return 1

        async def expire(self, *_args) -> int:
            return 1

        async def time(self) -> tuple[int, int]:
            return (123, 500_000)

        async def zadd(self, key: str, values: dict[str, float]) -> None:
            zadds.append((key, values))

    async def _stop_after_renewal(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(generation_lease, "LEASE_TTL_S", 60)
    monkeypatch.setattr(generation_lease, "LEASE_RENEW_S", 10)

    with pytest.raises(asyncio.CancelledError):
        await generation_lease.lease_renewer(
            _Redis(),
            "gen-redis-time",
            "worker-1",
            asyncio.Event(),
            image_provider_name="provider-a",
            sleep=_stop_after_renewal,
        )

    assert zadds == [
        (
            generation_queue.IMAGE_QUEUE_ACTIVE_KEY,
            {"gen-redis-time": pytest.approx(183.5)},
        ),
        (
            generation_queue.image_provider_active_key("provider-a"),
            {"gen-redis-time": pytest.approx(183.5)},
        ),
    ]


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

    await generation_lease.cancel_renewer_task(task)

    assert cleaned.is_set()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_mark_generation_attempt_retrying_requeues_and_publishes() -> None:
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

    services = replace(
        generation_services,
        store=_SessionStore(session),
        events=_FakeEvents(publish=fake_publish_event),
    )

    ok = await retry_state.mark_generation_attempt_retrying(
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
        services=services,
    )

    assert ok is True
    assert session.committed is True
    assert redis.enqueued == [
        (
            "run_generation",
            ("gen-1", 3, 1),
            {
                "_job_id": "lumen:generation:gen-1:attempt:3:dispatch:1",
                "_defer_by": 3.5,
                "_job_try": 3,
            },
        )
    ]
    assert generation_queue.image_queue_not_before_key("gen-1") in redis.store
    assert published[0]["event_name"] == EV_GEN_RETRYING
    assert published[0]["data"]["reason"] == "lease_lost"


@pytest.mark.asyncio
async def test_maybe_requeue_stale_generation_attempt_only_for_same_queued_attempt() -> (
    None
):
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

    services = replace(
        generation_services,
        store=_SessionStore(session),
        events=_FakeEvents(publish=fake_publish_event),
    )

    ok = await retry_state.maybe_requeue_stale_generation_attempt(
        redis,
        task_id="gen-1",
        attempt=2,
        reason="row_lock_lost",
        delay=1.25,
        services=services,
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
        (
            "run_generation",
            ("gen-1", 3, 1),
            {
                "_job_id": "lumen:generation:gen-1:attempt:3:dispatch:1",
                "_defer_by": 1.25,
                "_job_try": 3,
            },
        )
    ]
    assert published[0]["event_name"] == EV_GEN_RETRYING
    assert published[0]["data"]["reason"] == "row_lock_lost"


@pytest.mark.asyncio
async def test_maybe_requeue_stale_generation_attempt_skips_non_actionable_rows() -> (
    None
):
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

    services = replace(
        generation_services,
        store=_SessionStore(_Session()),
        events=_FakeEvents(publish=fail_publish),
    )

    ok = await retry_state.maybe_requeue_stale_generation_attempt(
        redis,
        task_id="gen-1",
        attempt=2,
        reason="superseded",
        delay=1.0,
        services=services,
    )

    assert ok is False
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_image_queue_kick_skips_not_before_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    redis.store[generation_queue.image_queue_not_before_key("gen-old")] = str(
        time.time() + 60
    )

    async def fake_queued_generation_ids(
        _limit: int,
        *,
        services: Any,
    ) -> list[str]:
        _ = services
        return ["gen-old", "gen-ready"]

    monkeypatch.setattr(
        generation_queue,
        "queued_generation_ids",
        fake_queued_generation_ids,
    )

    await generation_queue.kick_image_queue(
        redis,
        services=generation_services,
    )

    assert [args[0] for _name, args, _kwargs in redis.enqueued] == ["gen-ready"]


@pytest.mark.asyncio
async def test_ready_queue_batches_not_before_reads_and_bounds_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    scan_limits: list[int] = []
    all_candidates = [
        generation_queue.QueuedGenerationCandidate(
            id=f"gen-{index}",
            attempt=1,
        )
        for index in range(1000)
    ]

    async def fake_candidates(
        limit: int,
        _services: Any,
    ) -> list[generation_queue.QueuedGenerationCandidate]:
        scan_limits.append(limit)
        return all_candidates[:limit]

    monkeypatch.setattr(
        generation_queue,
        "queued_generation_candidates",
        fake_candidates,
    )

    selected = await generation_queue.ready_queued_generation_ids(
        redis,
        10,
        services=generation_services,
    )

    assert selected == [f"gen-{index}" for index in range(10)]
    assert scan_limits == [40]
    assert redis.mget_calls == 1
    assert redis.get_calls == 0


@pytest.mark.asyncio
async def test_image_queue_does_not_select_provider_when_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool

    redis = FakeRedis()
    redis.zsets[generation_queue.IMAGE_QUEUE_ACTIVE_KEY] = {
        "acc1": time.time() + 60,
        "acc2": time.time() + 60,
    }
    services = replace(
        generation_services,
        queue=_QueueService(2),
    )

    async def fail_get_pool():
        raise AssertionError("provider pool should not be touched when queue is full")

    monkeypatch.setattr(provider_pool, "get_pool", fail_get_pool)

    reserved = await queue_claim.reserve_image_queue_slot(
        redis,
        "gen-1",
        services=services,
    )

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
    redis.zsets[generation_queue.image_provider_active_key("acc1")] = {
        "other-task": other_task_expiry,
    }
    redis.zsets[generation_queue.IMAGE_QUEUE_ACTIVE_KEY] = {
        "other-task": other_task_expiry,
    }

    async def fake_ready_generation_ids(
        _redis: Any,
        _limit: int,
        **_kwargs: Any,
    ) -> list[str]:
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

    monkeypatch.setattr(
        queue_claim,
        "ready_queued_generation_ids",
        fake_ready_generation_ids,
    )
    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    services = replace(generation_services, queue=_QueueService(4))

    reserved = await queue_claim.reserve_image_queue_slot(
        redis,
        "gen-1",
        services=services,
    )
    duplicate = await queue_claim.reserve_image_queue_slot(
        redis,
        "gen-1",
        services=services,
    )

    assert reserved is not None
    assert reserved.name == "acc2"
    assert duplicate is None
    assert "gen-1" in redis.zsets[generation_queue.image_provider_active_key("acc2")]
    assert redis.store[generation_queue.image_task_provider_key("gen-1")] == "acc2"
    assert "gen-1" in redis.zsets[generation_queue.IMAGE_QUEUE_ACTIVE_KEY]


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
            if args[0] == generation_lease.RELEASE_LEASE_LUA:
                raise RuntimeError("owner-CAS release unavailable")
            return await super().eval(*args)

    redis = ReleaseBrokenRedis()
    provider = SimpleNamespace(
        name="acc1",
        base_url="https://upstream.test",
        api_key="k1",
        image_concurrency=1,
    )

    async def fake_ready_generation_ids(
        _redis: Any,
        _limit: int,
        **_kwargs: Any,
    ) -> list[str]:
        return ["gen-1"]

    monkeypatch.setattr(
        queue_claim,
        "ready_queued_generation_ids",
        fake_ready_generation_ids,
    )
    services = replace(generation_services, queue=_QueueService(4))

    with caplog.at_level("ERROR", logger=generation_queue.logger.name):
        reserved = await queue_claim.reserve_image_queue_slot(
            redis,
            "gen-1",
            provider_override=provider,
            services=services,
        )

    assert reserved is provider
    assert redis.store[generation_queue.image_task_provider_key("gen-1")] == "acc1"
    assert "gen-1" in redis.zsets[generation_queue.image_provider_active_key("acc1")]
    assert "gen-1" in redis.zsets[generation_queue.IMAGE_QUEUE_ACTIVE_KEY]
    assert redis.store[generation_queue.IMAGE_QUEUE_LANE_CURSOR_KEY] == "1"
    assert "preserving critical-section result" in caplog.text


@pytest.mark.asyncio
async def test_image_queue_reserve_rejects_non_atomic_lock_release(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class NonAtomicRedis(FakeRedis):
        eval = None

    redis = NonAtomicRedis()

    with caplog.at_level("ERROR", logger=generation_queue.logger.name):
        with pytest.raises(UpstreamError) as exc_info:
            await queue_claim.reserve_image_queue_slot(
                redis,
                "gen-1",
                services=generation_services,
            )

    assert exc_info.value.error_code == EC.LOCAL_QUEUE_FULL.value
    assert exc_info.value.payload["retry_after"] > 0
    assert "requires Redis EVAL or WATCH transaction" in str(exc_info.value)
    assert "refused without atomic release support" in caplog.text
    assert generation_queue.IMAGE_QUEUE_LOCK_KEY not in redis.store


@pytest.mark.asyncio
async def test_image_queue_provider_active_count_failure_defers_without_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool

    class ActiveCountBrokenRedis(FakeRedis):
        async def zremrangebyscore(self, key: str, min_score, max_score) -> int:
            if key == generation_queue.image_provider_active_key("acc1"):
                raise RuntimeError("redis down")
            return await super().zremrangebyscore(key, min_score, max_score)

    redis = ActiveCountBrokenRedis()

    async def fake_ready_generation_ids(
        _redis: Any,
        _limit: int,
        **_kwargs: Any,
    ) -> list[str]:
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

    monkeypatch.setattr(
        queue_claim,
        "ready_queued_generation_ids",
        fake_ready_generation_ids,
    )
    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    services = replace(generation_services, queue=_QueueService(4))

    reserved = await queue_claim.reserve_image_queue_slot(
        redis,
        "gen-1",
        services=services,
    )

    assert reserved is None
    assert generation_queue.image_task_provider_key("gen-1") not in redis.store
    assert "gen-1" not in redis.zsets.get(generation_queue.IMAGE_QUEUE_ACTIVE_KEY, {})
    assert generation_queue.image_queue_not_before_key("gen-1") in redis.store


@pytest.mark.asyncio
async def test_image_queue_per_provider_concurrency_admits_multiple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider with image_concurrency=3 should accept 3 concurrent tasks."""
    from app import provider_pool

    redis = FakeRedis()

    queue: list[str] = ["gen-1", "gen-2", "gen-3", "gen-4"]

    async def fake_ready_generation_ids(
        _redis: Any,
        _limit: int,
        **_kwargs: Any,
    ) -> list[str]:
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

    monkeypatch.setattr(
        queue_claim,
        "ready_queued_generation_ids",
        fake_ready_generation_ids,
    )
    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    services = replace(generation_services, queue=_QueueService(10))

    admitted = []
    for _ in range(4):
        if not queue:
            break
        task_id = queue[0]
        reserved = await queue_claim.reserve_image_queue_slot(
            redis,
            task_id,
            services=services,
        )
        if reserved is None:
            break
        admitted.append((task_id, reserved.name))
        queue.pop(0)

    assert [name for _, name in admitted] == ["solo", "solo", "solo"]
    # 4th task can't be admitted — concurrency cap reached on the only provider.
    assert "gen-4" in queue
    assert len(redis.zsets[generation_queue.image_provider_active_key("solo")]) == 3


def test_image_queue_capacity_allows_high_provider_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "image_generation_concurrency", 20)

    assert (
        generation_queue.image_queue_capacity(
            services=generation_services,
        )
        == 20
    )


@pytest.mark.asyncio
async def test_image_queue_capacity_uses_runtime_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "image_generation_concurrency", 4)

    async def fake_resolve(key: str) -> str:
        assert key == "image.generation_concurrency"
        return "12"

    monkeypatch.setattr(runtime_settings, "resolve", fake_resolve)

    assert (
        await generation_queue.resolve_image_queue_capacity(
            services=generation_services,
        )
        == 12
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_message_status", "expected_message_status"),
    [
        (None, MessageStatus.FAILED),
        (MessageStatus.CANCELED, MessageStatus.CANCELED),
    ],
)
async def test_mark_generation_attempt_failed_preserves_canceled_message(
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

    services = replace(
        generation_services,
        store=_SessionStore(session),
        billing=_NoopBilling(),
        events=_FakeEvents(deliver=fake_deliver_generation_event),
    )

    ok = await retry_state.mark_generation_attempt_failed(
        object(),
        task_id="gen-1",
        message_id="msg-1",
        user_id="user-1",
        attempt=2,
        error_code="retry_enqueue_failed",
        error_message="failed to enqueue retry",
        retriable=False,
        services=services,
    )

    assert ok is True
    assert session.committed is True
    assert message.status == expected_message_status
    assert published[0]["event_name"] == EV_GEN_FAILED
    assert published[0]["data"]["code"] == "retry_enqueue_failed"


@pytest.mark.asyncio
async def test_write_generation_files_deletes_created_keys_on_failure() -> None:
    fake_storage = FakeStorage(fail_keys={"bad"})
    services = replace(
        generation_services,
        artifacts=DefaultGenerationArtifacts(fake_storage),
    )

    with pytest.raises(StorageDiskFullError):
        await persistence.write_generation_files(
            [("ok", b"1"), ("bad", b"2")],
            services,
        )

    assert set(fake_storage.put_keys) == {"ok", "bad"}
    assert fake_storage.deleted == ["ok"]


@pytest.mark.asyncio
async def test_write_generation_files_cleanup_continues_when_delete_fails() -> None:
    fake_storage = FakeStorage(fail_keys={"bad"}, fail_delete_keys={"ok1"})
    services = replace(
        generation_services,
        artifacts=DefaultGenerationArtifacts(fake_storage),
    )

    with pytest.raises(StorageDiskFullError):
        await persistence.write_generation_files(
            [("ok1", b"1"), ("bad", b"2"), ("ok2", b"3")],
            services,
        )

    assert set(fake_storage.deleted) == {"ok1", "ok2"}


@pytest.mark.asyncio
async def test_cleanup_storage_on_error_deletes_created_keys() -> None:
    fake_storage = FakeStorage()
    services = replace(
        generation_services,
        artifacts=DefaultGenerationArtifacts(fake_storage),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        async with persistence.cleanup_storage_on_error(
            ["orig", "display"],
            services,
        ):
            raise RuntimeError("commit failed")

    assert set(fake_storage.deleted) == {"orig", "display"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupt", "expected_exception"),
    [
        ("lease", LeaseLost),
        ("cancel", TaskCancelled),
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
    services = replace(
        generation_services,
        artifacts=DefaultGenerationArtifacts(local_storage),
    )

    redis = FakeRedis()
    lease_lost = asyncio.Event()
    key = "u/user-1/g/gen-write-race/orig.png"
    guarded_write = asyncio.create_task(
        retry_state.await_with_lease_guard(
            persistence.write_generation_files(
                [(key, b"first-attempt")],
                services,
            ),
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
    retry_services = replace(
        generation_services,
        artifacts=DefaultGenerationArtifacts(retry_storage),
    )
    assert await persistence.write_generation_files(
        [(key, b"retry")],
        retry_services,
    ) == [key]
    assert retry_storage.get_bytes(key) == b"retry"


@pytest.mark.asyncio
async def test_cleanup_storage_on_custom_base_exception_waits_for_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_storage = LocalStorage(tmp_path)
    key = "u/user-1/g/gen-base-exception/orig.png"
    local_storage.put_bytes(key, b"image")
    services = replace(
        generation_services,
        artifacts=DefaultGenerationArtifacts(local_storage),
    )

    with pytest.raises(TaskCancelled):
        async with persistence.cleanup_storage_on_error([key], services):
            raise TaskCancelled("cancelled during persistence")

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

    await workflow_service.record_model_library_generate_image(
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

    await workflow_service.record_model_library_generate_image(
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
        workflow_service.model_library_requested_count_from_step
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
        workflow_hooks,
        "model_library_requested_count_from_step",
        lambda _step: 1,
    )

    await workflow_service.record_model_library_generate_image(
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
        workflow_service,
        "_load_model_library_tagger",
        lambda: cancel_tagger,
    )

    with pytest.raises(asyncio.CancelledError):
        await workflow_service.record_model_library_generate_image(
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

    await workflow_service.record_model_library_candidate_image(
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

    await workflow_service.record_poster_style_library_generate_image(
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

    await workflow_service.record_poster_style_library_generate_image(
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

    await workflow_service.record_poster_style_library_generate_image(
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

    await workflow_service.record_poster_style_library_generate_image(
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

    await workflow_service.record_poster_workflow_image(
        session=session,
        user_id="user-1",
        generation=generation_row,
        image_id="new-image",
    )

    assert master.image_id == "existing-image"
    assert master.status == "ready"
    assert session.get_calls == [(PosterMaster, "master-1")]


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

    await workflow_service.record_poster_workflow_image(
        session=session,
        user_id="user-1",
        generation=generation_row,
        image_id="new-image",
    )

    assert render.image_id == "new-image"
    assert render.status == "ready"
    assert session.get_calls == [(PosterRender, "render-1")]


def test_run_generation_records_workflows_before_billing_and_commit() -> None:
    hook_source = inspect.getsource(generation_success._record_success_hooks)
    model_hook = hook_source.index("record_model_library_generate_image")
    poster_hook = hook_source.index(
        "record_poster_workflow_image",
        model_hook,
    )
    style_hook = hook_source.index(
        "record_poster_style_library_generate_image",
        poster_hook,
    )
    assert model_hook < poster_hook < style_hook

    persistence_source = inspect.getsource(
        generation_success._persist_generation_success
    )
    hooks = persistence_source.index("_record_success_hooks(")
    settle = persistence_source.index(
        "g.billing.settle(",
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
        retry_state.await_with_lease_guard(slow_work(), lease_lost)
    )
    await started.wait()
    lease_lost.set()

    with pytest.raises(LeaseLost):
        await task
