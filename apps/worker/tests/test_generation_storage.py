from __future__ import annotations

import asyncio
import io
import os
import tempfile
import time
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage
from sqlalchemy.dialects import postgresql

os.environ.setdefault(
    "STORAGE_ROOT", f"{tempfile.gettempdir()}/lumen-worker-test-storage"
)

from lumen_core.constants import EV_GEN_FAILED, MessageStatus
from app.background_removal.local_chroma import (
    recover_solid_background_transparency,
)
from app.storage import StorageDiskFullError, StoragePutResult
from app.tasks import generation


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

    async def set(self, key: str, value, nx: bool = False, ex: int | None = None):
        _ = ex
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

    rendered = str(session.statements[0].compile(dialect=postgresql.dialect()))
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

    await generation._lease_renewer(_Redis(), "gen-1", lease_lost)

    assert lease_lost.is_set()


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
async def test_image_queue_kick_skips_not_before_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    redis.store[generation._image_queue_not_before_key("gen-old")] = str(
        time.time() + 60
    )

    async def fake_queued_generation_ids(_limit: int) -> list[str]:
        return ["gen-old", "gen-ready"]

    monkeypatch.setattr(generation, "_queued_generation_ids", fake_queued_generation_ids)

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
    assert (
        "gen-1" in redis.zsets[generation._image_provider_active_key("acc2")]
    )
    assert redis.store[generation._image_task_provider_key("gen-1")] == "acc2"
    assert "gen-1" in redis.zsets[generation._IMAGE_QUEUE_ACTIVE_KEY]


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
    assert (
        len(redis.zsets[generation._image_provider_active_key("solo")]) == 3
    )


def test_image_queue_capacity_allows_high_provider_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation.settings, "image_generation_concurrency", 20)

    assert generation._image_queue_capacity() == 20


@pytest.mark.asyncio
async def test_mark_generation_attempt_failed_publishes_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = FakeMessage()
    published: list[dict] = []

    class _Session:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult(1)

        async def get(self, _model, _message_id: str):
            return message

        async def commit(self) -> None:
            self.committed = True

    session = _Session()

    async def fake_publish_event(redis, user_id, channel, event_name, data):
        published.append(
            {
                "redis": redis,
                "user_id": user_id,
                "channel": channel,
                "event_name": event_name,
                "data": data,
            }
        )

    monkeypatch.setattr(generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(generation, "publish_event", fake_publish_event)

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
    assert message.status == MessageStatus.FAILED
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
async def test_await_with_lease_guard_aborts_work() -> None:
    lease_lost = asyncio.Event()
    started = asyncio.Event()

    async def slow_work() -> str:
        started.set()
        await asyncio.sleep(10)
        return "done"

    task = asyncio.create_task(generation._await_with_lease_guard(slow_work(), lease_lost))
    await started.wait()
    lease_lost.set()

    with pytest.raises(generation._LeaseLost):
        await task
