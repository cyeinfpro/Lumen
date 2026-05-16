"""Generation Worker——DESIGN §6.5.b + §7（/v1/images/* 同步路径）。

`run_generation(ctx, task_id)` 是 arq 任务入口。流程：

1. 幂等读 Generation 行；若终态直接 return
2. 进入统一图片 FIFO 队列；只有全局并发槽内的任务会被标记 running
3. 起 lease（5min TTL）+ 30s 续租协程
4. publish generation.started
5. 解析 size（resolve_size）
7. 按 action 选分支：
   - GENERATE → upstream.generate_image(prompt, size)  → POST /v1/images/generations
   - EDIT     → upstream.edit_image(prompt, size, images) → POST /v1/images/edits (multipart)
8. 按 §7.5 做 SHA-256 回退检测
9. 抽图 → PIL → 算 blurhash + display2048.webp + preview1024.webp + thumb256.jpg
10. 并行写 storage 4 份 → INSERT images + image_variants
11. UPDATE message.content + generations status=succeeded
12. publish generation.succeeded

重试：is_retriable() + RETRY_BACKOFF_SECONDS，attempt ≤ 5；超过或 terminal → failed。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any

import httpx
from PIL import Image as PILImage
from sqlalchemy import select, update

from ..background_removal import (
    TransparentPipelineFailure,
    process_transparent_request,
)
from .. import billing as worker_billing
from .. import runtime_settings

from lumen_core.constants import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    EXPLICIT_ALIGN,
    MAX_EXPLICIT_ASPECT,
    MAX_EXPLICIT_PIXELS,
    MAX_EXPLICIT_SIDE,
    MIN_EXPLICIT_PIXELS,
    CompletionStage,
    CompletionStatus,
    EV_GEN_ATTACHED,
    EV_GEN_FAILED,
    EV_GEN_PARTIAL_IMAGE,
    EV_GEN_PROGRESS,
    EV_GEN_QUEUED,
    EV_GEN_RETRYING,
    EV_GEN_STARTED,
    EV_GEN_SUCCEEDED,
    GenerationAction,
    GenerationErrorCode as EC,
    GenerationStage,
    GenerationStatus,
    ImageSource,
    MessageStatus,
    RETRY_BACKOFF_SECONDS,
    Role,
    task_channel,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    OutboxEvent,
    PosterMaster,
    PosterRender,
    PosterStyleItem,
    WorkflowRun,
    WorkflowStep,
    new_uuid7,
)
from lumen_core.model_image_metadata import (
    build_model_image_metadata,
    model_image_filename,
    save_image_with_model_metadata,
)
from lumen_core.sizing import resolve_size, validate_explicit_size

from ..config import settings
from ..db import SessionLocal
from ..byok_runtime import (
    byok_error_message,
    byok_error_to_generation_code,
    classify_user_credential_error,
    record_user_credential_runtime_error,
    resolve_user_credential_runtime,
)
from ..observability import (
    get_tracer,
    safe_outcome,
    task_duration_seconds,
    upstream_calls_total,
)
from ..retry import RetryDecision, is_moderation_block, is_retriable
from ..sse_publish import publish_event
from ..storage import StorageDiskFullError, storage
from ..upstream import (
    UpstreamCancelled,
    UpstreamError,
    _image_endpoint_kind_for_engine,
    _resolve_image_primary_route,
    edit_image,
    generate_image,
    pop_image_retry_attempt,
    push_image_retry_attempt,
)
from .state import is_generation_terminal

logger = logging.getLogger(__name__)
_tracer = get_tracer("lumen.worker.generation")


# --- Constants ---

_LEASE_TTL_S = 60
_LEASE_RENEW_S = 30
# 上游（OpenAI gpt-5.x）偶发 server_error + rate_limit 较频繁。
# race=2 × attempts=5 = 最多 10 次上游调用；配合下面 RETRY_BACKOFF_SECONDS 的分钟级间隔
# 给 rate_limit window 恢复时间。4K 升级后单次上游调用可能 8+ min，最坏 ~20-25 min。
_MAX_ATTEMPTS = 5
_REFERENCE_LOAD_TIMEOUT_S = 30.0
# Keep the worker-level generation budget below arq's 1800s job_timeout so the
# task can release leases/semaphores and persist a retriable state itself.
_RUN_GENERATION_TIMEOUT_S = 1500.0
_IMAGE_QUEUE_LOCK_KEY = "generation:image_queue:lock"
_IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
_IMAGE_QUEUE_PROVIDER_LOCK_PREFIX = "generation:image_queue:provider:"
_IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"
_IMAGE_QUEUE_ENQUEUE_DEDUPE_PREFIX = "generation:image_queue:enqueue:"
_IMAGE_QUEUE_NOT_BEFORE_PREFIX = "generation:image_queue:not_before:"
_IMAGE_QUEUE_AVOID_PREFIX = "generation:image_queue:avoid:"
# 正在调用上游的实时 provider 快照（admin 请求事件面板的 in-flight 列读这里）。
# 单 provider 模式：mode=single + provider 字段；
# dual_race 模式：mode=dual_race + lane_a_provider / lane_b_provider 两路最新 provider；
# 失败切号时，下一条 provider_used 会覆盖同字段，自然实现"动态更新"。
_IMAGE_INFLIGHT_PREFIX = "generation:image_inflight:"
_IMAGE_QUEUE_LOCK_TTL_S = 10
_IMAGE_QUEUE_LOCK_WAIT_S = 5.0
_IMAGE_QUEUE_SCAN_LIMIT = 100
_IMAGE_QUEUE_ENQUEUE_DEDUPE_TTL_S = 30
_IMAGE_QUEUE_NOT_BEFORE_GRACE_S = 600
_IMAGE_PROVIDER_UNAVAILABLE_RETRY_S = 30
_IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S = 5.0
# Redis 抖动时 not_before cooldown 写入也可能失败，被 except 吞掉后这一轮立刻
# 又会被 _kick_image_queue 拉起来重试。fallback 到进程内 monotonic 表，
# `_ready_queued_generation_ids` 检查时同时看 redis 和本地，确保抖动期间也能
# 拉开窗口。worker 重启会自然清空，无需 TTL。
_PROVIDER_COOLDOWN_LOCAL: dict[str, float] = {}
# task 失败 retry 时把刚刚 reserved 的 provider 写入 avoid set，下次 reserve
# 跳过它一次，避免 retry 反复打到同一个有问题的 provider（如 model_not_found / 401）。
# 全部 enabled provider 都被 avoid 时退化为不过滤（防止永远 reserve 失败）。
_IMAGE_QUEUE_AVOID_TTL_S = 120
# moderation_blocked / safety_violation 单次 task 跨 attempt 的换号上限。
# retry.py 仍把 moderation 视为 terminal——单 provider 时直接 fail，避免烧配额；
# 多 provider 时 task 层升级为 retriable，配 avoid set 把请求路由到下一个未试 provider。
# 上限取 min(_MODERATION_RETRY_CAP, enabled_provider_count)。
_MODERATION_RETRY_CAP = 6
_RETRY_JITTER_RATIO = 0.20
_RETRY_BACKOFF_MAX_SECONDS = 15 * 60
_LEASE_REACQUIRED_SUBSTAGE = "lease_reacquired"
_IMAGE_RENDER_QUALITY_VALUES = {"low", "medium", "high", "auto"}
_IMAGE_OUTPUT_FORMAT_VALUES = {"png", "jpeg", "webp"}
_IMAGE_BACKGROUND_VALUES = {"auto", "opaque", "transparent"}
_IMAGE_MODERATION_VALUES = {"auto", "low"}


class _TaskCancelled(UpstreamCancelled):
    """GEN-P1-4: 用户取消信号——复用 upstream.UpstreamCancelled（BaseException 子类），
    便于 race / fallback 各层正确透传；外层 generation 任务再捕获标终态。"""


class _LeaseLost(UpstreamCancelled):
    """Lease renewer gave up; this worker must stop before another attempt runs."""


class _StaleGenerationAttempt(Exception):
    """This worker's attempt epoch no longer owns the generation row."""


async def _is_cancelled(redis: Any, task_id: str) -> bool:
    try:
        v = await redis.get(f"task:{task_id}:cancel")
    except Exception:  # noqa: BLE001
        return False
    return bool(v)


# ---------------------------------------------------------------------------
# Lease helpers
# ---------------------------------------------------------------------------


async def _acquire_lease(redis: Any, task_id: str, worker_id: str) -> None:
    await redis.set(f"task:{task_id}:lease", worker_id, ex=_LEASE_TTL_S)


_RELEASE_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


async def _release_lease(redis: Any, task_id: str, worker_id: str) -> None:
    try:
        eval_fn = getattr(redis, "eval", None)
        if callable(eval_fn):
            await eval_fn(_RELEASE_LEASE_LUA, 1, f"task:{task_id}:lease", worker_id)
            return
        current = await redis.get(f"task:{task_id}:lease")
        if _redis_text(current) == worker_id:
            await redis.delete(f"task:{task_id}:lease")
    except Exception:  # noqa: BLE001
        pass


async def _lease_renewer(
    redis: Any,
    task_id: str,
    lease_lost: asyncio.Event | None = None,
    *,
    extra_lease_keys: list[str] | None = None,
    image_provider_name: str | None = None,
) -> None:
    """每 30s 续约一次。被 cancel 时优雅退出；连续 3 次失败设置 lease_lost。

    Renews three things in lock-step so a long-running task never gets evicted:
    1. The worker lease (`task:{id}:lease`) and any caller-supplied extra keys.
    2. This task's score in the global active ZSET (member = task_id, or the
       dual_race sentinel name).
    3. This task's score in the per-provider active ZSET (real providers only).
    """
    consecutive_failures = 0
    try:
        while True:
            await asyncio.sleep(_LEASE_RENEW_S)
            try:
                await redis.expire(f"task:{task_id}:lease", _LEASE_TTL_S)
                for key in extra_lease_keys or []:
                    await redis.expire(key, _LEASE_TTL_S)
                # in-flight provider 快照随 lease 续命（admin 列表才能持续看到）
                with suppress(Exception):
                    await redis.expire(_image_inflight_key(task_id), _LEASE_TTL_S * 4)
                if image_provider_name:
                    new_expiry = time.time() + _LEASE_TTL_S
                    if _is_dual_race_sentinel(image_provider_name):
                        # dual_race 没有 per-provider zset；只续全局 sentinel。
                        await redis.zadd(
                            _IMAGE_QUEUE_ACTIVE_KEY,
                            {image_provider_name: new_expiry},
                        )
                    else:
                        await redis.zadd(
                            _IMAGE_QUEUE_ACTIVE_KEY,
                            {task_id: new_expiry},
                        )
                        await redis.zadd(
                            _image_provider_active_key(image_provider_name),
                            {task_id: new_expiry},
                        )
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                logger.warning(
                    "lease renew failed task=%s err=%s streak=%d",
                    task_id,
                    exc,
                    consecutive_failures,
                )
                if consecutive_failures >= 3:
                    if lease_lost is not None:
                        lease_lost.set()
                    logger.error(
                        "lease renewer giving up task=%s failures=%d",
                        task_id,
                        consecutive_failures,
                    )
                    return
    except asyncio.CancelledError:
        raise


async def _cancel_renewer_task(renewer: asyncio.Task[None] | None) -> None:
    if renewer is None:
        return
    renewer.cancel()
    try:
        await renewer
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("generation lease renewer cancellation failed", exc_info=True)


# ---------------------------------------------------------------------------
# Semaphore helpers (Redis atomic INCR/DECR)
# ---------------------------------------------------------------------------


# Why: INCR + 容量检查必须原子，否则两个 worker 可能同时 INCR 后看到 ≤capacity，
# 各自 DECR 之间的窗口让其他 worker 误以为还有名额。Lua 在 Redis 单线程里执行。
_ACQUIRE_LUA = """
local v = redis.call('INCR', KEYS[1])
if v <= tonumber(ARGV[1]) then return 1 end
redis.call('DECR', KEYS[1])
return 0
"""


class _RedisSemaphore:
    """Lua 原子 INCR+检查 自旋信号量；最多等 wait_s 秒。不保证严格公平，够用即可。

    on_wait_start: 第一次拿不到名额、即将进入等待循环时调一次（用于推 queued 事件）。
    异常被吞掉，不影响主流程。
    """

    def __init__(
        self,
        redis: Any,
        key: str,
        capacity: int,
        wait_s: float = 60.0,
        on_wait_start: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.redis = redis
        self.key = key
        self.capacity = capacity
        self.wait_s = wait_s
        self.on_wait_start = on_wait_start
        self._acquired = False

    async def __aenter__(self) -> "_RedisSemaphore":
        loop_until = asyncio.get_event_loop().time() + self.wait_s
        notified = False
        while True:
            try:
                got = await self.redis.eval(_ACQUIRE_LUA, 1, self.key, self.capacity)
            except Exception as exc:  # noqa: BLE001
                # 不退化到 INCR/DECR：那条路径在并发下会短暂超过 capacity。
                raise UpstreamError(
                    "local concurrency semaphore unavailable",
                    error_code=EC.LOCAL_QUEUE_FULL.value,
                    status_code=None,
                ) from exc
            if int(got or 0) == 1:
                self._acquired = True
                return self
            if not notified and self.on_wait_start is not None:
                notified = True
                try:
                    await self.on_wait_start()
                except Exception:  # noqa: BLE001
                    logger.debug("sem on_wait_start callback failed", exc_info=True)
            if asyncio.get_event_loop().time() >= loop_until:
                # 区别于上游 rate_limit_error：这是本地排队等不到，retry.py 会让它走
                # arq backoff 重新入队（不烧 worker job_timeout）。
                raise UpstreamError(
                    "local concurrency wait exhausted",
                    error_code=EC.LOCAL_QUEUE_FULL.value,
                    status_code=None,
                )
            await asyncio.sleep(0.5)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._acquired:
            try:
                await self.redis.decr(self.key)
            except Exception as exc:  # noqa: BLE001
                # 不能 raise（aexit 链路），但必须看得见——否则 redis 抖动期间名额会持续
                # 漏算（DECR 没成功）让后续 task 永远拿不到名额。
                logger.warning(
                    "redis sem decr failed key=%s err=%s",
                    self.key,
                    exc,
                )


# ---------------------------------------------------------------------------
# Unified image FIFO queue
# ---------------------------------------------------------------------------


def _redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


_IMAGE_GENERATION_CONCURRENCY_SETTING = "image.generation_concurrency"


def _coerce_image_queue_capacity(raw: Any) -> int:
    try:
        return max(1, min(32, int(raw)))
    except (TypeError, ValueError):
        return 4


def _image_queue_capacity() -> int:
    return _coerce_image_queue_capacity(
        getattr(settings, "image_generation_concurrency", 4)
    )


async def _resolve_image_queue_capacity() -> int:
    try:
        raw = await runtime_settings.resolve(_IMAGE_GENERATION_CONCURRENCY_SETTING)
    except Exception as exc:  # noqa: BLE001
        logger.warning("image queue capacity resolve failed err=%s", exc)
        return _image_queue_capacity()
    if raw is None:
        return _image_queue_capacity()
    return _coerce_image_queue_capacity(raw)


def _image_provider_lock_key(provider_name: str) -> str:
    """Legacy name retained for backward compatibility with tests / call sites
    that referenced the binary NX lock. The new model is a sorted-set of
    task_ids per provider — see ``_image_provider_active_key``.
    """
    return f"{_IMAGE_QUEUE_PROVIDER_LOCK_PREFIX}{provider_name}"


def _image_provider_active_key(provider_name: str) -> str:
    """Per-provider ZSET tracking active task_ids → expiry timestamps.

    ZCARD gives us the current concurrency on this provider; ZADD admits a
    new task; ZREM releases. Stale entries (worker crash mid-flight) are
    cleaned up via ``ZREMRANGEBYSCORE -inf now`` before each ZCARD.
    """
    return f"generation:image_queue:provider_active:{provider_name}"


def _image_task_provider_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{task_id}"


def _image_queue_enqueue_dedupe_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_ENQUEUE_DEDUPE_PREFIX}{task_id}"


def _image_queue_not_before_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_NOT_BEFORE_PREFIX}{task_id}"


def _image_queue_avoid_key(task_id: str) -> str:
    return f"{_IMAGE_QUEUE_AVOID_PREFIX}{task_id}"


async def _avoid_provider_for_task(
    redis: Any, task_id: str, provider_name: str
) -> None:
    """task retry 前调用：把刚刚失败的 provider 加入 avoid set，下次 reserve 跳过。"""
    if not provider_name:
        return
    try:
        key = _image_queue_avoid_key(task_id)
        await redis.sadd(key, provider_name)
        await redis.expire(key, _IMAGE_QUEUE_AVOID_TTL_S)
    except Exception:  # noqa: BLE001
        logger.debug("avoid_provider write failed", exc_info=True)


async def _get_avoided_providers(redis: Any, task_id: str) -> set[str]:
    try:
        raw = await redis.smembers(_image_queue_avoid_key(task_id))
    except Exception:  # noqa: BLE001
        return set()
    return {name for item in raw or [] if (name := _redis_text(item))}


async def _clear_avoided_providers(redis: Any, task_id: str) -> None:
    with suppress(Exception):
        await redis.delete(_image_queue_avoid_key(task_id))


def _image_inflight_key(task_id: str) -> str:
    return f"{_IMAGE_INFLIGHT_PREFIX}{task_id}"


def _classify_inflight_lane(route: str | None, endpoint: str | None) -> str:
    """provider_used 事件 → dual_race 两路之一的 field key。

    - image2 / image2_direct → lane_a
    - responses / responses_fallback → lane_b
    - image_jobs：按 endpoint 后缀分；generations 归 lane_a，responses 归 lane_b
    - 兜底：lane_a（极少触发，不让事件丢）
    """
    r = (route or "").lower()
    e = (endpoint or "").lower()
    if r.startswith("image2"):
        return "lane_a"
    if r.startswith("responses"):
        return "lane_b"
    if r == "image_jobs":
        if e.endswith(":generations"):
            return "lane_a"
        if e.endswith(":responses"):
            return "lane_b"
    return "lane_a"


async def _inflight_set_fields(
    redis: Any, task_id: str, fields: dict[str, str]
) -> None:
    """HSET + EXPIRE 一并写入 in-flight provider 快照；写失败只 debug，不打断主流程。"""
    if not fields:
        return
    payload = {k: v for k, v in fields.items() if v is not None and v != ""}
    if not payload:
        return
    payload["updated_at"] = str(int(time.time() * 1000))
    try:
        key = _image_inflight_key(task_id)
        await redis.hset(key, mapping=payload)
        # _LEASE_TTL_S * 4 与 per-provider zset 一致：worker 崩了 4min 内自然过期。
        await redis.expire(key, _LEASE_TTL_S * 4)
    except Exception:  # noqa: BLE001
        logger.debug("image_inflight write failed task=%s", task_id, exc_info=True)


async def _inflight_clear(redis: Any, task_id: str) -> None:
    with suppress(Exception):
        await redis.delete(_image_inflight_key(task_id))


@asynccontextmanager
async def _image_queue_lock(redis: Any) -> AsyncIterator[None]:
    token = new_uuid7()
    deadline = asyncio.get_event_loop().time() + _IMAGE_QUEUE_LOCK_WAIT_S
    while True:
        got = await redis.set(
            _IMAGE_QUEUE_LOCK_KEY,
            token,
            nx=True,
            ex=_IMAGE_QUEUE_LOCK_TTL_S,
        )
        if got:
            break
        if asyncio.get_event_loop().time() >= deadline:
            raise UpstreamError(
                "image queue scheduler busy",
                error_code=EC.LOCAL_QUEUE_FULL.value,
                status_code=None,
            )
        await asyncio.sleep(0.05)

    try:
        yield
    finally:
        try:
            current = _redis_text(await redis.get(_IMAGE_QUEUE_LOCK_KEY))
            if current == token:
                await redis.delete(_IMAGE_QUEUE_LOCK_KEY)
        except Exception:  # noqa: BLE001
            logger.warning("image queue lock release failed", exc_info=True)


async def _cleanup_image_queue_active(redis: Any) -> None:
    try:
        await redis.zremrangebyscore(_IMAGE_QUEUE_ACTIVE_KEY, "-inf", time.time())
    except Exception:  # noqa: BLE001
        logger.debug("image queue active cleanup failed", exc_info=True)


async def _active_image_provider_names(redis: Any) -> set[str]:
    """Active members in the global image queue. Members are task_ids (or
    dual-race sentinels), one per in-flight job. Used both as a count and as
    a set membership check (e.g. "has THIS task already been admitted").
    """
    try:
        raw_names = await redis.zrange(_IMAGE_QUEUE_ACTIVE_KEY, 0, -1)
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            "image queue active set unavailable",
            error_code=EC.LOCAL_QUEUE_FULL.value,
            status_code=None,
        ) from exc
    return {name for item in raw_names or [] if (name := _redis_text(item))}


async def _provider_active_count(redis: Any, provider_name: str) -> int | None:
    """Current in-flight task count for one provider after evicting stale
    entries (worker crash mid-flight). Cheap: O(log N) for the cleanup +
    O(1) for ZCARD.

    Redis failure is fail-closed: returning 0 here over-admits every queued
    task into the same provider when Redis hiccups, defeating image_concurrency.
    """
    key = _image_provider_active_key(provider_name)
    try:
        await redis.zremrangebyscore(key, "-inf", time.time())
        count = await redis.zcard(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "image queue active_count failed provider=%s err=%s",
            provider_name,
            exc,
        )
        return None
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


async def _queued_generation_ids(limit: int) -> list[str]:
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Generation.id)
                    .where(Generation.status == GenerationStatus.QUEUED.value)
                    .order_by(Generation.created_at.asc(), Generation.id.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [str(row) for row in rows]


async def _ready_queued_generation_ids(redis: Any, limit: int) -> list[str]:
    ids = await _queued_generation_ids(max(limit, _IMAGE_QUEUE_SCAN_LIMIT))
    if not ids:
        return []
    ready: list[str] = []
    now = time.time()
    now_mono = time.monotonic()
    for queued_id in ids:
        # 本地兜底 cooldown：redis 写失败时唯一防 hot-loop 的栅栏
        local_until = _PROVIDER_COOLDOWN_LOCAL.get(queued_id)
        if local_until is not None:
            if local_until > now_mono:
                continue
            # 过期了清掉，避免 dict 无限增长
            _PROVIDER_COOLDOWN_LOCAL.pop(queued_id, None)
        not_before_key = _image_queue_not_before_key(queued_id)
        raw_not_before = _redis_text(await redis.get(not_before_key))
        if raw_not_before:
            try:
                if float(raw_not_before) > now:
                    continue
            except ValueError:
                with suppress(Exception):
                    await redis.delete(not_before_key)
        ready.append(queued_id)
        if len(ready) >= limit:
            break
    return ready


async def _enqueue_generation_once(
    redis: Any,
    task_id: str,
    *,
    defer_by: int | float | None = None,
    job_try: int | None = None,
) -> bool:
    dedupe_key = _image_queue_enqueue_dedupe_key(task_id)
    try:
        first = await redis.set(
            dedupe_key,
            "1",
            nx=True,
            ex=_IMAGE_QUEUE_ENQUEUE_DEDUPE_TTL_S,
        )
        if not first:
            return False
        kwargs: dict[str, Any] = {}
        if defer_by is not None and defer_by > 0:
            kwargs["_defer_by"] = defer_by
        if job_try is not None:
            kwargs["_job_try"] = job_try
        await redis.enqueue_job("run_generation", task_id, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        with suppress(Exception):
            await redis.delete(dedupe_key)
        logger.warning("image queue enqueue failed task=%s err=%s", task_id, exc)
        return False


async def _clear_image_queue_enqueue_dedupe(redis: Any, task_id: str) -> None:
    with suppress(Exception):
        await redis.delete(_image_queue_enqueue_dedupe_key(task_id))


async def _kick_image_queue(redis: Any) -> None:
    capacity = await _resolve_image_queue_capacity()
    try:
        ids = await _ready_queued_generation_ids(
            redis, max(_IMAGE_QUEUE_SCAN_LIMIT, capacity * 2)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("image queue kick scan failed err=%s", exc)
        return
    for queued_id in ids[: max(1, capacity * 2)]:
        await _enqueue_generation_once(redis, queued_id)


_DUAL_RACE_SENTINEL_PREFIX = "__dr:"


def _dual_race_sentinel_name(task_id: str) -> str:
    return f"{_DUAL_RACE_SENTINEL_PREFIX}{task_id}"


def _is_dual_race_sentinel(name: str | None) -> bool:
    return bool(name and name.startswith(_DUAL_RACE_SENTINEL_PREFIX))


async def _reserve_image_queue_slot(
    redis: Any,
    task_id: str,
    *,
    dual_race: bool = False,
    endpoint_kind: str | None = None,
    requires_mask: bool = False,
    provider_override: Any | None = None,
) -> Any | None:
    """Reserve one global image slot for the oldest queued task.

    Policy:
    - all image sizes share the same FIFO queue;
    - global concurrency cap = ``image.generation_concurrency`` / env
      ``IMAGE_GENERATION_CONCURRENCY`` (active task count);
    - **per-provider concurrency** is configurable via the provider field
      ``image_concurrency`` (default 1, preserving the historical
      "one task per provider" behaviour). Bumping it lets a single account run
      multiple in-flight image jobs — useful when only one upstream is enabled
      yet the user wants more throughput, or when the sidecar is the actual
      bottleneck (image_jobs route);
    - dual_race mode: occupies one capacity slot via a sentinel name (no provider
      lock), so both image2 and responses lanes can failover across all enabled
      accounts independently. The sentinel is ``__dr:<task_id>``.
    - tasks that are not the oldest queued task return immediately and stay queued.

    Concurrency model:
    Two ZSETs back the bookkeeping. The global ``_IMAGE_QUEUE_ACTIVE_KEY`` ZSET
    has one member per active task (member = task_id or dual-race sentinel,
    score = expiry). Per provider, ``_image_provider_active_key(name)`` ZSET
    holds the task_ids currently running on that account. Both are cleaned by
    ``ZREMRANGEBYSCORE -inf now`` on read so a crashed worker's slots free
    themselves within ``_LEASE_TTL_S``.
    """
    from ..provider_pool import ResolvedProvider, get_pool

    capacity = await _resolve_image_queue_capacity()
    async with _image_queue_lock(redis):
        await _cleanup_image_queue_active(redis)
        active_members = await _active_image_provider_names(redis)

        # Re-entry guard: if this task already owns a slot, return None so
        # the outer flow doesn't double-admit. Only when the slot has clearly
        # gone stale (TTL expired, sentinel missing) do we clean up and
        # let the task try again.
        existing_provider = _redis_text(
            await redis.get(_image_task_provider_key(task_id))
        )
        if existing_provider:
            if _is_dual_race_sentinel(existing_provider):
                if existing_provider in active_members:
                    return None
                with suppress(Exception):
                    await redis.delete(_image_task_provider_key(task_id))
                with suppress(Exception):
                    await redis.zrem(_IMAGE_QUEUE_ACTIVE_KEY, existing_provider)
                logger.info(
                    "image queue cleared stale dual_race sentinel task=%s",
                    task_id,
                )
            else:
                # The new model: this task either has a live entry in the
                # provider's active ZSET (still admitted) or it doesn't
                # (stale — clean up and re-admit).
                provider_zset = _image_provider_active_key(existing_provider)
                still_admitted = False
                with suppress(Exception):
                    score = await redis.zscore(provider_zset, task_id)
                    still_admitted = score is not None and float(score) > time.time()
                if still_admitted and task_id in active_members:
                    return None
                with suppress(Exception):
                    await redis.zrem(provider_zset, task_id)
                with suppress(Exception):
                    await redis.delete(_image_task_provider_key(task_id))
                with suppress(Exception):
                    await redis.zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
                logger.info(
                    "image queue cleared stale self-lock task=%s provider=%s",
                    task_id,
                    existing_provider,
                )

        if len(active_members) >= capacity:
            return None

        queued_ids = await _ready_queued_generation_ids(redis, 1)
        if not queued_ids or queued_ids[0] != task_id:
            return None

        now = time.time()
        expiry = now + _LEASE_TTL_S

        if dual_race:
            sentinel = _dual_race_sentinel_name(task_id)
            await redis.set(
                _image_task_provider_key(task_id),
                sentinel,
                ex=_LEASE_TTL_S,
            )
            await redis.zadd(
                _IMAGE_QUEUE_ACTIVE_KEY,
                {sentinel: expiry},
            )
            await redis.delete(_image_queue_not_before_key(task_id))
            logger.info(
                "image queue admitted task=%s mode=dual_race active=%d/%d",
                task_id,
                len(active_members) + 1,
                capacity,
            )
            return ResolvedProvider(name=sentinel, base_url="", api_key="")

        if provider_override is not None:
            providers = [provider_override]
        else:
            pool = await get_pool()
            # P1-8: 把 task_id 透传给 pool；pool 内部会从 Redis avoid set 跳过
            # 上次失败的 provider，与下方 generation.py 的二次过滤是双保险。
            # acquire_inflight=False：reserve 只是为了挑号占 zset，不真正发请求；
            # inflight 由真正发请求的 _dispatch_image / lane 持有。如果这里 acquire
            # 了，_dispatch_image 之后又 acquire 一次，且 reserve 占的那一份没人
            # release 会一直泄漏。老版 mock pool 不接受这个 kwarg，TypeError 时退化
            # 调用——reserve 拿到的候选不消费 inflight，老 mock 也不维护 inflight，结果一致。
            try:
                providers = await pool.select(
                    route="image",
                    task_id=task_id,
                    endpoint_kind=endpoint_kind,
                    acquire_inflight=False,
                    requires_mask=requires_mask,
                )
            except TypeError as exc:
                msg = str(exc)
                # 兼容老 mock：依次去掉新增 kwargs（requires_mask、acquire_inflight）。
                # requires_mask=True 但 mock 不识别时，select 出来的候选可能含 url 模式
                # provider；真实 ProviderPool 会优先 file-mode，file-mode 耗尽时允许
                # url-mode 兜底，避免 inpaint 因单一 transport 池耗尽直接失败。
                if "requires_mask" not in msg and "acquire_inflight" not in msg:
                    raise
                kwargs = {
                    "route": "image",
                    "task_id": task_id,
                    "endpoint_kind": endpoint_kind,
                }
                try:
                    providers = await pool.select(**kwargs, acquire_inflight=False)
                except TypeError:
                    providers = await pool.select(**kwargs)
        if not providers:
            return None

        # task retry 时会把上次失败的 provider 加入 avoid set；reserve 跳过它们一次。
        # 全部候选都被 avoid 时退化成不过滤——避免某些尺寸/account 组合永远 reserve 不到。
        avoided = await _get_avoided_providers(redis, task_id)
        if avoided:
            filtered = [
                provider
                for provider in providers
                if _redis_text(getattr(provider, "name", "")) not in avoided
            ]
            if filtered:
                providers = filtered
            else:
                logger.info(
                    "image queue avoid set fully overlaps providers, "
                    "ignoring avoid for task=%s avoided=%s",
                    task_id,
                    sorted(avoided),
                )
                with suppress(Exception):
                    await redis.delete(_image_queue_avoid_key(task_id))
        if not providers:
            return None

        active_count_failed = False
        for provider in providers:
            provider_name = _redis_text(getattr(provider, "name", ""))
            if not provider_name:
                continue
            concurrency = max(1, int(getattr(provider, "image_concurrency", 1) or 1))
            provider_zset = _image_provider_active_key(provider_name)
            current = await _provider_active_count(redis, provider_name)
            if current is None:
                active_count_failed = True
                continue
            if current >= concurrency:
                continue
            try:
                await redis.zadd(provider_zset, {task_id: expiry})
                # Keep the ZSET TTL ahead of the longest possible task so it
                # never gets evicted out from under us, but bounded so it
                # disappears once the provider is fully idle.
                await redis.expire(provider_zset, _LEASE_TTL_S * 4)
                await redis.set(
                    _image_task_provider_key(task_id),
                    provider_name,
                    ex=_LEASE_TTL_S,
                )
                await redis.zadd(
                    _IMAGE_QUEUE_ACTIVE_KEY,
                    {task_id: expiry},
                )
                await redis.delete(_image_queue_not_before_key(task_id))
            except Exception:
                with suppress(Exception):
                    await redis.zrem(provider_zset, task_id)
                with suppress(Exception):
                    await redis.delete(_image_task_provider_key(task_id))
                with suppress(Exception):
                    await redis.zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
                raise
            logger.info(
                "image queue admitted task=%s provider=%s "
                "provider_active=%d/%d global_active=%d/%d",
                task_id,
                provider_name,
                current + 1,
                concurrency,
                len(active_members) + 1,
                capacity,
            )
            return provider
        if active_count_failed:
            cooldown = _IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S
            redis_set_ok = False
            try:
                await redis.set(
                    _image_queue_not_before_key(task_id),
                    str(time.time() + cooldown),
                    ex=int(cooldown + _IMAGE_QUEUE_NOT_BEFORE_GRACE_S),
                )
                redis_set_ok = True
            except Exception:  # noqa: BLE001
                # Redis 也抖了——降级到进程内表，避免本轮立刻又重试。
                # _ready_queued_generation_ids 同时检查 local map。
                pass
            # 不论 redis 写成功与否都更新本地表（双写：redis 失败时这是唯一兜底；
            # redis 成功时 local 会被同样的 timestamp 覆盖，next_kick 时多一道防线）。
            _PROVIDER_COOLDOWN_LOCAL[task_id] = time.monotonic() + cooldown
            logger.warning(
                "image queue deferred task=%s after provider active count failure "
                "cooldown=%.1fs redis_set=%s",
                task_id,
                cooldown,
                redis_set_ok,
            )
    return None


async def _release_image_queue_slot(
    redis: Any, *, task_id: str, provider_name: str | None
) -> None:
    if not provider_name:
        return
    task_provider_key = _image_task_provider_key(task_id)
    if _is_dual_race_sentinel(provider_name):
        # dual_race 没有 per-provider 槽；只清 sentinel 占用的全局活动条目。
        try:
            await redis.zrem(_IMAGE_QUEUE_ACTIVE_KEY, provider_name)
            await redis.delete(task_provider_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "dual_race release failed task=%s sentinel=%s",
                task_id,
                provider_name,
                exc_info=True,
            )
        await _kick_image_queue(redis)
        return
    provider_zset = _image_provider_active_key(provider_name)
    try:
        await redis.zrem(provider_zset, task_id)
        await redis.zrem(_IMAGE_QUEUE_ACTIVE_KEY, task_id)
        await redis.delete(task_provider_key)
        # Best-effort cleanup of the legacy NX lock if anything still pokes it
        # (older queued task admitted before redeploy; safe no-op once gone).
        with suppress(Exception):
            legacy = _image_provider_lock_key(provider_name)
            owner = _redis_text(await redis.get(legacy))
            if owner == task_id:
                await redis.delete(legacy)
    except Exception:  # noqa: BLE001
        logger.warning(
            "image queue release failed task=%s provider=%s",
            task_id,
            provider_name,
            exc_info=True,
        )
    await _kick_image_queue(redis)


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _decode_upstream_image_b64(value: str) -> bytes:
    raw = value.strip()
    if raw[:5].lower() == "data:" and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    return base64.b64decode(raw, validate=True)


def _compute_blurhash(img: PILImage.Image) -> str | None:
    width, height = img.size
    if width < 4 or height < 4:
        return None
    try:
        import blurhash as _bh

        # blurhash 期望 RGB；用 thumbnail 来算快得多
        with img.convert("RGB") as small:
            small.thumbnail((64, 64))
            return _bh.encode(small, x_components=4, y_components=3)
    except Exception as exc:  # noqa: BLE001
        logger.debug("blurhash failed: %s", exc)
        return None


def _make_preview(
    orig: PILImage.Image, max_side: int = 1024
) -> tuple[bytes, tuple[int, int]]:
    with orig.copy() as im:
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with _webp_image_for_variant(im) as webp:
            webp.save(buf, format="WEBP", quality=82, method=4)
        return buf.getvalue(), im.size


def _image_has_alpha(im: PILImage.Image) -> bool:
    return im.mode in {"LA", "RGBA"} or (im.mode == "P" and "transparency" in im.info)


def _image_has_transparency(im: PILImage.Image) -> bool:
    if not _image_has_alpha(im):
        return False
    with im.convert("RGBA") as rgba:
        alpha = rgba.getchannel("A")
        return alpha.getextrema()[0] < 255


def _webp_image_for_variant(im: PILImage.Image) -> PILImage.Image:
    return im.convert("RGBA" if _image_has_alpha(im) else "RGB")


def _rgb_image_for_flat_variant(
    im: PILImage.Image,
    *,
    background: tuple[int, int, int] = (255, 255, 255),
) -> PILImage.Image:
    if not _image_has_alpha(im):
        return im.convert("RGB")
    rgba = im.convert("RGBA")
    base = PILImage.new("RGB", rgba.size, background)
    base.paste(rgba, mask=rgba.getchannel("A"))
    rgba.close()
    return base


def _make_display(
    orig: PILImage.Image, max_side: int = 2048
) -> tuple[bytes, tuple[int, int]]:
    with orig.copy() as im:
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with _webp_image_for_variant(im) as webp:
            webp.save(buf, format="WEBP", quality=86, method=4)
        return buf.getvalue(), im.size


def _make_thumb(
    orig: PILImage.Image, max_side: int = 256
) -> tuple[bytes, tuple[int, int]]:
    with orig.copy() as im:
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with _rgb_image_for_flat_variant(im) as rgb:
            rgb.save(buf, format="JPEG", quality=78, optimize=True)
        return buf.getvalue(), im.size


def _clean_model_style_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:32])
        if len(out) >= 12:
            break
    return out


def _model_image_metadata_from_request(
    *,
    image_id: str,
    mime: str,
    request: dict[str, Any] | None,
    prompt: str | None = None,
) -> dict[str, Any]:
    req = request if isinstance(request, dict) else {}
    if req.get("workflow_action") != "model_library_generate":
        return {}
    age_segment = req.get("workflow_model_library_age_segment")
    gender = req.get("workflow_model_library_gender")
    appearance_direction = req.get("workflow_model_library_appearance_direction")
    style_tags = _clean_model_style_tags(
        req.get("workflow_model_library_style_tags") or []
    )
    payload = build_model_image_metadata(
        age_segment=age_segment if isinstance(age_segment, str) else None,
        gender=gender if isinstance(gender, str) else None,
        appearance_direction=(
            appearance_direction if isinstance(appearance_direction, str) else None
        ),
        style_tags=style_tags,
        source="model_library_generate",
        prompt_hint=prompt,
    )
    if not payload:
        return {}
    ext = "png"
    if isinstance(mime, str) and mime.startswith("image/"):
        ext = "jpg" if mime == "image/jpeg" else mime.removeprefix("image/")
    return {
        "model_library": payload,
        "suggested_filename": model_image_filename(
            image_id=image_id,
            ext=ext,
            age_segment=payload.get("age_segment"),
            gender=payload.get("gender"),
            appearance_direction=payload.get("appearance_direction"),
            style_tags=style_tags,
        ),
    }


def _maybe_embed_model_image_metadata_bytes(
    *,
    image: PILImage.Image,
    fmt: str,
    raw_image: bytes,
    metadata: dict[str, Any],
) -> bytes:
    payload = metadata.get("model_library") if isinstance(metadata, dict) else None
    if fmt.upper() != "PNG" or not isinstance(payload, dict) or not payload:
        return raw_image
    out = io.BytesIO()
    save_image_with_model_metadata(
        image,
        out,
        fmt="PNG",
        metadata=payload,
    )
    return out.getvalue()


# ---------------------------------------------------------------------------
# Upstream body assembly
# ---------------------------------------------------------------------------


async def _load_reference_images(
    session: Any, image_ids: list[str]
) -> list[tuple[str, bytes]]:
    """返回 [(sha256, png_bytes), ...]；按 image_ids 的输入顺序。

    任一 image_id 在 DB 缺失或 storage bytes 读不到 → 抛 UpstreamError(reference_missing)。
    历史上这里是 warning+continue 静默降级，会让 edit 悄悄退化成文生图——改成硬失败。
    """
    if not image_ids:
        return []
    rows = (
        await session.execute(
            select(Image.id, Image.storage_key, Image.sha256).where(
                Image.id.in_(image_ids),
                Image.deleted_at.is_(None),
            )
        )
    ).all()
    by_id = {r.id: (r.storage_key, r.sha256) for r in rows}
    out: list[tuple[str, bytes]] = []
    for iid in image_ids:
        if iid not in by_id:
            raise UpstreamError(
                f"reference image not found id={iid}",
                error_code=EC.REFERENCE_MISSING.value,
                status_code=404,
            )
        storage_key, sha = by_id[iid]
        try:
            async with asyncio.timeout(_REFERENCE_LOAD_TIMEOUT_S):
                raw = await storage.aget_bytes(storage_key)
        except TimeoutError as exc:
            raise UpstreamError(
                f"reference image bytes read timed out key={storage_key}",
                error_code=EC.REFERENCE_TIMEOUT.value,
                status_code=None,
            ) from exc
        except FileNotFoundError as exc:
            raise UpstreamError(
                f"reference image bytes missing key={storage_key}",
                error_code=EC.REFERENCE_MISSING.value,
                status_code=404,
            ) from exc
        out.append((sha, raw))
    return out


# 局部 inpaint mask 字节大小上限：复用 ref image 的 50MB（routes/images.py:69
# MAX_BYTES）——mask 通常 <100KB，给 50MB 上限只是兜住极端 4K alpha PNG 异常上传。
_MASK_MAX_BYTES = 50 * 1024 * 1024


async def _load_mask_image(session: Any, mask_image_id: str) -> bytes:
    """从 Image 表读 mask PNG 字节。

    与 ``_load_reference_images`` 行为对齐：DB 行缺失 / storage 读不到 → 抛硬错误，
    不静默降级（mask 任务退化成普通 i2i 体验比明确报错更糟）。读到的字节超过
    ``_MASK_MAX_BYTES`` 也按 reference_image_too_large 抛终态错。

    返回值：原始字节（PIL 在调用方 resize 时再处理）。
    """
    row = (
        await session.execute(
            select(Image.id, Image.storage_key).where(
                Image.id == mask_image_id,
                Image.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise UpstreamError(
            f"mask image not found id={mask_image_id}",
            error_code=EC.REFERENCE_MISSING.value,
            status_code=404,
        )
    storage_key = row.storage_key
    try:
        async with asyncio.timeout(_REFERENCE_LOAD_TIMEOUT_S):
            raw = await storage.aget_bytes(storage_key)
    except TimeoutError as exc:
        raise UpstreamError(
            f"mask image bytes read timed out key={storage_key}",
            error_code=EC.REFERENCE_TIMEOUT.value,
            status_code=None,
        ) from exc
    except FileNotFoundError as exc:
        raise UpstreamError(
            f"mask image bytes missing key={storage_key}",
            error_code=EC.REFERENCE_MISSING.value,
            status_code=404,
        ) from exc
    if len(raw) > _MASK_MAX_BYTES:
        raise UpstreamError(
            "mask image exceeds size limit",
            error_code=EC.REFERENCE_IMAGE_TOO_LARGE.value,
            status_code=413,
            payload={"max_bytes": _MASK_MAX_BYTES, "actual_bytes": len(raw)},
        )
    return raw


def _mask_alpha_is_binary(im: PILImage.Image) -> bool:
    """RGBA / LA mask 的 alpha 通道是否只含 0 和 255。

    OpenAI /v1/images/edits 文档定义"alpha=0 → 重画区域，其他 → 保留"，非 0/255
    的中间 alpha 行为未指定。前端 destination-out 描线在圆笔头边会留 1-px 抗锯齿
    灰带；LANCZOS 上下采样也会引入 partial alpha — 都需要兜底阈值化。
    """
    try:
        bands = im.getbands()
    except Exception:  # noqa: BLE001
        return False
    if "A" not in bands:
        # 没有 alpha 通道（如纯 L mode）→ 调用方会 convert("RGBA")，再阈值化兜底。
        return False
    try:
        alpha = im.getchannel("A")
        extrema = alpha.getextrema()
    except Exception:  # noqa: BLE001
        return False
    if extrema is None:
        return True
    lo, hi = extrema
    return lo in (0, 255) and hi in (0, 255)


def _binarize_mask_alpha(im: PILImage.Image) -> PILImage.Image:
    """把 RGBA mask 的 alpha 阈值化到 {0, 255}（< 128 → 0，否则 → 255）。

    输入若不是 RGBA 自动 convert；输出永远 RGBA。
    """
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    alpha = im.getchannel("A")
    binarized = alpha.point(lambda v: 255 if v >= 128 else 0)
    out = im.copy()
    out.putalpha(binarized)
    return out


def _resize_mask_to_reference(
    mask_bytes: bytes,
    reference_bytes: bytes,
) -> bytes:
    """把 mask 对齐到第一张参考图的像素尺寸 + 阈值化 alpha 到 {0, 255}。

    上游 /v1/images/edits + mask 字段要求 mask 与 image 等尺寸；前端裁切时已经做过
    一次但移动端 / DPR 偶尔会偏 1-2px，worker 兜底再 normalize 一次。

    OpenAI 文档：alpha=0 → 重画区，alpha=255 → 保留区，中间值未定义。所以这里：
    - resize 用 NEAREST（不引入 partial alpha；mask 通常是较大块面，NEAREST 上下
      采样的边缘锯齿在 16-px+ 块面下肉眼看不出来）。
    - resize 后阈值化 alpha 到 {0, 255}，把前端 destination-out 圆笔头的 1-px
      抗锯齿灰带也压成二值。

    - 已经等尺寸 + 合法形态 + alpha 已经二值 → 原 PNG 字节直接返回（避免无谓 PIL 重编码）。
    - 否则 → 转 RGBA + （需要时）NEAREST resize + 阈值化 + 保存 PNG。
    - mask 解码失败 → terminal bad_reference_image（用户输入问题，重试无效）。
    """
    try:
        with PILImage.open(io.BytesIO(reference_bytes)) as ref_im:
            ref_size = ref_im.size  # (W, H)
    except Exception as exc:  # noqa: BLE001
        # 参考图解码失败应当在 _normalize_reference_image 那条路被抛；这里兜底
        # 转 bad_reference_image，不让 mask resize 阶段静默吞错。
        raise UpstreamError(
            f"reference image not decodable for mask sizing: {exc}",
            error_code=EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        ) from exc
    try:
        with PILImage.open(io.BytesIO(mask_bytes)) as mask_im:
            same_size = mask_im.size == ref_size
            mode_legit = mask_im.mode in ("RGBA", "L", "LA")
            if same_size and mode_legit and _mask_alpha_is_binary(mask_im):
                # 同尺寸 + 已经是合法 mask 形态 + alpha 已二值 → 直接返回原字节。
                return mask_bytes
            target_mode = "RGBA"
            mask_normalized = (
                mask_im if mask_im.mode == target_mode else mask_im.convert(target_mode)
            )
            if mask_normalized.size != ref_size:
                # NEAREST 而非 LANCZOS：避免在 mask 边引入 1-254 的 partial alpha
                # 让上游 inpaint 区分不出"重画 vs 保留"。
                mask_normalized = mask_normalized.resize(
                    ref_size, resample=PILImage.NEAREST
                )
            mask_normalized = _binarize_mask_alpha(mask_normalized)
            out = io.BytesIO()
            mask_normalized.save(out, format="PNG")
            return out.getvalue()
    except UpstreamError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            f"mask image not decodable: {exc}",
            error_code=EC.BAD_REFERENCE_IMAGE.value,
            status_code=400,
        ) from exc


def _reference_pixel_size(reference_bytes: bytes) -> tuple[int, int] | None:
    """读出参考图的 (W, H)；解码失败返回 None（让调用方走 fallback）。

    与 ``_resize_mask_to_reference`` 共用同份 reference_bytes 但独立打开 PIL；
    PIL.Image.open 只读 header，不 decode 像素，开销可忽略。
    """
    try:
        with PILImage.open(io.BytesIO(reference_bytes)) as ref_im:
            return ref_im.size
    except Exception:  # noqa: BLE001
        return None


def _inpaint_size_from_reference(ref_w: int, ref_h: int) -> str | None:
    """局部 inpaint 时按参考图像素尺寸推导 gpt-image-2 合法 size。

    强制让输出尺寸 = 输入尺寸（取最近合法 16-aligned），避免 1024x768 输入要被
    "顺手升采到 4K 输出"——那种叠加场景实测会让模型把 mask 外的区域也重画，
    甚至把 mask 错位 — 用户的"局部修改"瞬间退化成"整张重生成"。

    Scaling：先按长边 ≤ MAX_EXPLICIT_SIDE 缩，再按总像素 ≤ MAX_EXPLICIT_PIXELS 二次缩，
    最后按总像素 ≥ MIN_EXPLICIT_PIXELS 反向放。三个 16-aligned 候选（nearest / floor /
    ceil）依次试 validate；都失败才返回 None（让 caller 回退原 resolved.size）。

    返回 None 的极端场景：aspect > 21:9 / 短边过小同时长宽比偏极端 / 解码失败 /
    16-aligned 候选都 validate 不过（罕见）。
    """
    if ref_w <= 0 or ref_h <= 0:
        return None
    long_side = max(ref_w, ref_h)
    short_side = min(ref_w, ref_h)
    if short_side <= 0:
        return None
    if long_side / short_side > MAX_EXPLICIT_ASPECT:
        return None  # 太极端的长宽比，validate 也不会过，让 caller 走 resolved.size

    # Upper bounds：长边 + 总像素
    scale = 1.0
    if long_side > MAX_EXPLICIT_SIDE:
        scale = MAX_EXPLICIT_SIDE / long_side
    pixels_at_scale = ref_w * ref_h * scale * scale
    if pixels_at_scale > MAX_EXPLICIT_PIXELS:
        scale *= math.sqrt(MAX_EXPLICIT_PIXELS / pixels_at_scale)

    # Lower bound：总像素（在不超长边的前提下放大；否则放弃）
    pixels_at_scale = ref_w * ref_h * scale * scale
    if pixels_at_scale < MIN_EXPLICIT_PIXELS:
        scale_up = math.sqrt(MIN_EXPLICIT_PIXELS / pixels_at_scale)
        if max(ref_w, ref_h) * scale * scale_up > MAX_EXPLICIT_SIDE:
            return None
        scale *= scale_up

    target_w = ref_w * scale
    target_h = ref_h * scale

    # 三个 16-aligned 候选：nearest（边界附近 ok）/ floor（max 边界用）/ ceil（min 边界用）
    candidates: list[tuple[int, int]] = []
    for align in (
        lambda v: max(EXPLICIT_ALIGN, int(round(v / EXPLICIT_ALIGN)) * EXPLICIT_ALIGN),
        lambda v: max(EXPLICIT_ALIGN, int(v // EXPLICIT_ALIGN) * EXPLICIT_ALIGN),
        lambda v: max(
            EXPLICIT_ALIGN, int(math.ceil(v / EXPLICIT_ALIGN)) * EXPLICIT_ALIGN
        ),
    ):
        candidates.append((align(target_w), align(target_h)))
    seen: set[tuple[int, int]] = set()
    for w, h in candidates:
        if (w, h) in seen:
            continue
        seen.add((w, h))
        try:
            validate_explicit_size(w, h)
            return f"{w}x{h}"
        except ValueError:
            continue
    return None


def _bounded_next_attempt(current_attempt: int | None) -> tuple[int, bool]:
    """Return the next attempt without incrementing beyond the hard cap."""
    try:
        current = int(current_attempt or 0)
    except (TypeError, ValueError):
        current = 0
    current = max(0, current)
    if current >= _MAX_ATTEMPTS:
        return current, False
    return current + 1, True


def _parse_size_string(size: str) -> tuple[int, int]:
    if not isinstance(size, str) or "x" not in size:
        raise ValueError(f"invalid resolved size: {size!r}")
    raw_w, raw_h = size.split("x", 1)
    if not raw_w.isdigit() or not raw_h.isdigit():
        raise ValueError(f"invalid resolved size: {size!r}")
    return int(raw_w), int(raw_h)


def _validate_resolved_size(
    size: str,
    aspect_ratio: str,
    *,
    validate_aspect_ratio: bool = True,
    max_ratio_deviation: float = 0.02,
) -> tuple[int, int]:
    """Defense-in-depth after resolve_size(): validate hard limits and ratio drift."""
    width, height = _parse_size_string(size)
    validate_explicit_size(width, height)
    if validate_aspect_ratio and isinstance(aspect_ratio, str) and ":" in aspect_ratio:
        raw_rw, raw_rh = aspect_ratio.split(":", 1)
        if raw_rw.isdigit() and raw_rh.isdigit():
            ratio_w = int(raw_rw)
            ratio_h = int(raw_rh)
            if ratio_w > 0 and ratio_h > 0:
                target = ratio_w / ratio_h
                actual = width / height
                deviation = abs(actual - target) / target
                if deviation > max_ratio_deviation:
                    raise ValueError(
                        "resolved size aspect ratio drift too large: "
                        f"size={size} requested={aspect_ratio} "
                        f"deviation={deviation:.3%}"
                    )
    return width, height


def _base_retry_backoff_seconds(attempt: int) -> float:
    idx = max(0, int(attempt) - 1)
    if idx < len(RETRY_BACKOFF_SECONDS):
        return float(RETRY_BACKOFF_SECONDS[idx])
    last = float(RETRY_BACKOFF_SECONDS[-1]) if RETRY_BACKOFF_SECONDS else 1.0
    overflow = idx - len(RETRY_BACKOFF_SECONDS) + 1
    return min(last * (2**overflow), float(_RETRY_BACKOFF_MAX_SECONDS))


def _retry_delay_seconds(
    attempt: int,
    *,
    jitter_ratio: float = _RETRY_JITTER_RATIO,
) -> float:
    base = _base_retry_backoff_seconds(attempt)
    if base <= 0 or jitter_ratio <= 0:
        return base
    return base + random.uniform(0, base * jitter_ratio)


def _retry_not_before_ttl(delay: float) -> int:
    return max(1, math.ceil(delay + _IMAGE_QUEUE_NOT_BEFORE_GRACE_S))


def _generation_attempt_update(task_id: str, attempt_epoch: int):
    return update(Generation).where(
        Generation.id == task_id,
        Generation.attempt == attempt_epoch,
    )


def _workflow_qc_prompt(
    *,
    product_analysis: dict[str, Any],
    selected_model_brief: dict[str, Any],
    shot_type: str | None,
) -> str:
    must_preserve = product_analysis.get("must_preserve")
    preserve = (
        ", ".join(str(x) for x in must_preserve)
        if isinstance(must_preserve, list)
        else "garment color, silhouette, neckline, sleeve shape, length, logo, pattern, buttons, pockets, zippers, seams"
    )
    return (
        "Perform automatic visual quality control for one generated ecommerce "
        "apparel model showcase image. Compare the attached product reference, "
        "confirmed synthetic model reference, and generated showcase image. "
        "Check whether the product is still the same garment, whether color and "
        "structural details are preserved, whether the model identity is close "
        "to the confirmed candidate, whether there are visible hand, leg, face, "
        "garment edge, or background artifacts, and whether the image is usable "
        "as a premium ecommerce asset. Return strict JSON only with keys: "
        "overall_score, product_fidelity_score, model_consistency_score, "
        "aesthetic_score, artifact_score, issues, recommendation. Scores are "
        "0-100. recommendation must be approve or revise. "
        f"Must preserve: {preserve}. Shot type: {shot_type or 'unknown'}. "
        f"Confirmed model brief: {selected_model_brief.get('summary') or 'synthetic ecommerce model'}."
    )


async def _maybe_enqueue_workflow_quality_review(
    *,
    session: Any,
    redis: Any,
    user_id: str,
    conversation_id: str,
    generation: Generation,
    image_id: str,
) -> None:
    # Automatic QC for apparel workflows is intentionally disabled. The API keeps
    # generated showcase images in needs_review for manual review, so creating a
    # vision completion here would waste quota and surprise users.
    return

    req = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    if req.get("workflow_type") != "apparel_model_showcase":
        return
    if req.get("workflow_step_key") != "showcase_generation":
        return
    if req.get("workflow_action") not in {"showcase_image", "revision"}:
        return
    run_id = req.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return

    run = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.user_id == user_id,
                WorkflowRun.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return
    quality_step = (
        await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == "quality_review",
            )
        )
    ).scalar_one_or_none()
    showcase_step = (
        await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == "showcase_generation",
            )
        )
    ).scalar_one_or_none()
    product_step = (
        await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == "product_analysis",
            )
        )
    ).scalar_one_or_none()
    if quality_step is None or showcase_step is None or product_step is None:
        return

    output_json = dict(quality_step.output_json or {})
    review_tasks = output_json.get("review_tasks")
    if not isinstance(review_tasks, dict):
        review_tasks = {}
    if image_id in review_tasks:
        return

    candidate_id = req.get("workflow_candidate_id")
    candidate_ref_id: str | None = None
    selected_model_brief: dict[str, Any] = {}
    if isinstance(candidate_id, str):
        from lumen_core.models import ModelCandidate

        candidate = await session.get(ModelCandidate, candidate_id)
        if candidate is not None:
            candidate_ref_id = candidate.contact_sheet_image_id
            selected_model_brief = dict(candidate.model_brief_json or {})

    attachment_ids = [
        image_id
        for image_id in (run.product_image_ids or [])
        if isinstance(image_id, str)
    ]
    if candidate_ref_id:
        attachment_ids.append(candidate_ref_id)
    attachment_ids.append(image_id)
    attachment_ids = list(dict.fromkeys(attachment_ids))

    user_msg = Message(
        conversation_id=conversation_id,
        role=Role.USER.value,
        content={
            "text": _workflow_qc_prompt(
                product_analysis=product_step.output_json or {},
                selected_model_brief=selected_model_brief,
                shot_type=str(
                    req.get("workflow_shot_type")
                    or req.get("workflow_revision_scope")
                    or ""
                ),
            ),
            "attachments": [{"image_id": iid} for iid in attachment_ids],
            "workflow_run_id": run.id,
            "workflow_step_key": "quality_review",
            "workflow_review_image_id": image_id,
        },
    )
    session.add(user_msg)
    await session.flush()
    assistant_msg = Message(
        conversation_id=conversation_id,
        role=Role.ASSISTANT.value,
        content={},
        parent_message_id=user_msg.id,
        intent="vision_qa",
        status=MessageStatus.PENDING.value,
    )
    session.add(assistant_msg)
    await session.flush()
    completion = Completion(
        message_id=assistant_msg.id,
        user_id=user_id,
        model=DEFAULT_CHAT_MODEL,
        input_image_ids=attachment_ids,
        text="",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        attempt=0,
        idempotency_key=f"wf:{run.id[:21]}:qc:{image_id[:8]}",
        upstream_request={
            "workflow_run_id": run.id,
            "workflow_type": "apparel_model_showcase",
            "workflow_step_key": "quality_review",
            "workflow_action": "quality_review",
            "workflow_review_image_id": image_id,
        },
    )
    session.add(completion)
    await session.flush()
    payload = {"task_id": completion.id, "user_id": user_id, "kind": "completion"}
    session.add(OutboxEvent(kind="completion", payload=payload, published_at=None))

    review_tasks[image_id] = completion.id
    output_json["review_tasks"] = review_tasks
    output_json["review_task_count"] = len(review_tasks)
    quality_step.output_json = output_json
    quality_step.task_ids = list(
        dict.fromkeys([*(quality_step.task_ids or []), completion.id])
    )
    quality_step.image_ids = list(
        dict.fromkeys([*(quality_step.image_ids or []), image_id])
    )
    quality_step.status = "running"
    run.current_step = "quality_review"
    run.status = "running"

    # Do not enqueue directly here: this function runs inside the image success
    # DB transaction, so a fast worker could observe the completion before commit.
    # The outbox row above is committed atomically with the workflow state and
    # the publisher will enqueue it shortly after.
    _ = redis


def _model_library_requested_count_from_step(step: WorkflowStep) -> int:
    task_ids = [task_id for task_id in (step.task_ids or []) if task_id]
    if task_ids:
        return len(task_ids)

    input_json = step.input_json if isinstance(step.input_json, dict) else {}
    try:
        count = int(input_json.get("count_per_gender") or input_json.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    genders = input_json.get("genders")
    gender_count = (
        len([gender for gender in genders if gender in {"female", "male"}])
        if isinstance(genders, list)
        else 1
    )
    return count * max(1, gender_count)


async def _maybe_record_model_library_generate_image(
    *,
    session: Any,
    user_id: str,
    generation: Generation,
    image_id: str,
) -> None:
    """模特库独立生成 worker 钩子。

    在主生成事务内：把刚生成的 image_id 追加到对应 WorkflowStep.image_ids；
    若 input_json.auto_tag=True，立即同步调 vision tagging（每张独立轻量），
    把识别结果写到 step.output_json.tagging_results[image_id]。

    一切异常 graceful：tagging 失败不能让主生成任务从 succeeded 翻成 failed。
    """
    req = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    if req.get("workflow_action") != "model_library_generate":
        return
    if req.get("workflow_step_key") != "model_library_generate":
        return
    run_id = req.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return

    run = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.user_id == user_id,
                WorkflowRun.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return
    step = (
        await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == "model_library_generate",
            )
        )
    ).scalar_one_or_none()
    if step is None:
        return

    image_ids = list(step.image_ids or [])
    if image_id not in image_ids:
        image_ids.append(image_id)
    step.image_ids = list(dict.fromkeys(image_ids))

    input_json = step.input_json if isinstance(step.input_json, dict) else {}
    auto_tag = bool(input_json.get("auto_tag", False))
    requested = _model_library_requested_count_from_step(step)

    output_json = dict(step.output_json or {})
    if auto_tag:
        try:
            from .model_library_tagging import auto_tag_model_image

            result = await auto_tag_model_image(
                session,
                image_id=image_id,
                user_id=user_id,
            )
            tagging_results = output_json.get("tagging_results")
            if not isinstance(tagging_results, dict):
                tagging_results = {}
            tagging_results[image_id] = {
                "style_tags": list(result.style_tags or []),
                "appearance_direction": result.appearance_direction,
                "age_segment": result.age_segment,
                "gender": result.gender,
                "notes": result.notes,
            }
            output_json["tagging_results"] = tagging_results
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "model_library_generate tagging skipped run=%s image=%s err=%s",
                run.id,
                image_id,
                exc,
            )

    # 当所有 task 都跑完时把 step.status 推到 succeeded（部分失败保留 partial 语义由 API 层判定）。
    finished_count = len(step.image_ids or [])
    if finished_count >= requested and requested > 0:
        if step.status == "running":
            step.status = "succeeded"
            run.status = "completed"
            run.current_step = "model_library_generate"
    step.output_json = output_json


async def _maybe_record_poster_style_library_generate_image(
    *,
    session: Any,
    user_id: str,
    generation: Generation,
    image_id: str,
) -> None:
    """风格库独立生成 worker 钩子。

    与 ``_maybe_record_model_library_generate_image`` 同构，差异是：
    每张样图入一条 PosterStyleItem（source=generated, cover_image_id=image_id,
    sample_image_ids=[image_id]），属性从 step.input_json 复制。

    auto_tag 调用 ``poster_style_tagging.auto_tag_poster_style_image``，把识别
    出的 category / mood / style_tags / palette 合并到刚入库的 item。
    """
    req = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    if req.get("workflow_action") != "poster_style_library_generate":
        return
    if req.get("workflow_step_key") != "poster_style_library_generate":
        return
    run_id = req.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return

    run = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.user_id == user_id,
                WorkflowRun.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return
    step = (
        await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == "poster_style_library_generate",
            )
        )
    ).scalar_one_or_none()
    if step is None:
        return

    image_ids = list(step.image_ids or [])
    if image_id not in image_ids:
        image_ids.append(image_id)
    step.image_ids = list(dict.fromkeys(image_ids))

    input_json = step.input_json if isinstance(step.input_json, dict) else {}
    title = str(input_json.get("title") or "未命名风格")[:255]
    category_raw = str(input_json.get("category") or "user_favorites")
    category = category_raw if category_raw else "user_favorites"
    mood_raw = input_json.get("mood")
    mood = (
        str(mood_raw)[:128] if isinstance(mood_raw, str) and mood_raw.strip() else None
    )
    prompt_template_raw = input_json.get("prompt_template")
    prompt_value = str(input_json.get("prompt") or "")[:4000]
    if isinstance(prompt_template_raw, str) and prompt_template_raw.strip():
        prompt_template: str | None = prompt_template_raw[:2000]
    elif prompt_value:
        prompt_template = prompt_value[:2000]
    else:
        prompt_template = None
    palette = [c for c in (input_json.get("palette") or []) if isinstance(c, str)][:8]
    aspects = [
        a for a in (input_json.get("recommended_aspects") or []) if isinstance(a, str)
    ][:8]
    style_tags = [
        t for t in (input_json.get("style_tags") or []) if isinstance(t, str)
    ][:8]
    auto_tag = bool(input_json.get("auto_tag", False))

    existing = (
        await session.execute(
            select(PosterStyleItem)
            .where(
                PosterStyleItem.user_id == user_id,
                PosterStyleItem.cover_image_id == image_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:
        item = PosterStyleItem(
            id=f"user:{new_uuid7()}",
            user_id=user_id,
            source="generated",
            cover_image_id=image_id,
            sample_image_ids=[image_id],
            title=title,
            category=category,
            mood=mood,
            prompt_template=prompt_template,
            palette=list(palette),
            recommended_aspects=list(aspects) or ["1:1", "9:16", "16:9", "3:4"],
            style_tags=list(style_tags),
            library_folder=None,
            metadata_jsonb={
                "workflow_run_id": run.id,
                "prompt": prompt_value,
            },
        )
        session.add(item)
        await session.flush()
        target_item = item
    else:
        target_item = existing

    if auto_tag:
        try:
            from .poster_style_tagging import auto_tag_poster_style_image

            result = await auto_tag_poster_style_image(
                session,
                image_id=image_id,
                user_id=user_id,
            )
            if result.category and target_item.category in (None, "", "user_favorites"):
                target_item.category = result.category
            if result.mood and not target_item.mood:
                target_item.mood = result.mood[:128]
            if result.style_tags:
                merged_tags = list(
                    dict.fromkeys([*target_item.style_tags, *result.style_tags])
                )[:8]
                target_item.style_tags = merged_tags
            if result.palette and not target_item.palette:
                target_item.palette = list(result.palette)[:8]
            target_item.auto_tagged_at = datetime.now(timezone.utc)
            target_item.auto_tag_notes = result.notes
            meta = dict(target_item.metadata_jsonb or {})
            meta["auto_tag_raw"] = {
                "category": result.category,
                "mood": result.mood,
                "style_tags": list(result.style_tags or []),
                "palette": list(result.palette or []),
                "notes": result.notes,
            }
            target_item.metadata_jsonb = meta
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "poster_style_library_generate tagging skipped run=%s image=%s err=%s",
                run.id,
                image_id,
                exc,
            )

    requested = int(input_json.get("count") or 0)
    if requested <= 0:
        requested = max(len(step.task_ids or []), len(step.image_ids or []))
    finished_count = len(step.image_ids or [])
    if finished_count >= requested and requested > 0:
        if step.status == "running":
            step.status = "succeeded"
            run.status = "completed"
            run.current_step = "poster_style_library_generate"


async def _maybe_record_model_library_candidate_image(
    *,
    session: Any,
    user_id: str,
    parent_upstream_request: dict[str, Any],
    bonus_image_id: str,
) -> None:
    """dual_race bonus（loser）写回模特库 step.output_json.dual_race_bonus_image_ids。

    parent_upstream_request 是赢家的 upstream_request，如果它带 model_library_generate
    标记，就把 bonus image_id 追加到对应 step。candidate 不进 step.image_ids（image_ids
    语义是"用户已确认产出"，loser 不参与 finished_count 计算）。
    """
    if parent_upstream_request.get("workflow_action") != "model_library_generate":
        return
    if parent_upstream_request.get("workflow_step_key") != "model_library_generate":
        return
    run_id = parent_upstream_request.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return

    run = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.user_id == user_id,
                WorkflowRun.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return
    step = (
        await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == "model_library_generate",
            )
        )
    ).scalar_one_or_none()
    if step is None:
        return

    output_json = dict(step.output_json or {})
    bonus_ids = list(output_json.get("dual_race_bonus_image_ids") or [])
    if bonus_image_id not in bonus_ids:
        bonus_ids.append(bonus_image_id)
    output_json["dual_race_bonus_image_ids"] = bonus_ids
    step.output_json = output_json


async def _maybe_record_poster_workflow_image(
    *,
    session: Any,
    user_id: str,
    generation: Generation,
    image_id: str,
) -> None:
    """海报工作流任务成功后把 image_id 实时回填到 PosterMaster / PosterRender 行。

    设计点：
    - 只是把单张图绑定到行；step 状态机推进交给 API 侧 _sync_poster_workflow_outputs。
    - 一切异常 graceful——poster hook 失败不能把 succeeded 翻成 failed。
    - 识别条件：upstream_request.workflow_type=poster_design 且 workflow_action 在
      {poster_master, poster_render, poster_revise, poster_inpaint} 内。
    """
    req = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    if req.get("workflow_type") != "poster_design":
        return
    action = req.get("workflow_action")
    if action not in {
        "poster_master",
        "poster_render",
        "poster_revise",
        "poster_inpaint",
    }:
        return
    run_id = req.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return
    run = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.user_id == user_id,
                WorkflowRun.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return

    if action == "poster_master":
        master_id = req.get("workflow_master_id")
        if isinstance(master_id, str) and master_id:
            master = await session.get(PosterMaster, master_id)
            if master is not None and master.workflow_run_id == run.id:
                if not master.image_id:
                    master.image_id = image_id
                if master.status == "generating":
                    master.status = "ready"
    else:
        # poster_render / poster_revise / poster_inpaint → 都指向同一个 render 行
        render_id = req.get("workflow_render_id")
        if isinstance(render_id, str) and render_id:
            render = await session.get(PosterRender, render_id)
            if render is not None and render.workflow_run_id == run.id:
                # revise/inpaint 把最新 image 覆盖上去；前端只看最新一张
                render.image_id = image_id
                if render.status in {"generating", "revising"}:
                    render.status = "ready"


def _primary_input_image_id_valid(
    primary_input_image_id: str | None, input_image_ids: list[str]
) -> bool:
    return primary_input_image_id is None or primary_input_image_id in input_image_ids


def _ensure_generation_updated(
    result: Any, task_id: str, attempt_epoch: int | None
) -> None:
    rowcount = getattr(result, "rowcount", None)
    if rowcount == 0:
        raise _StaleGenerationAttempt(
            f"generation {task_id} attempt {attempt_epoch} no longer owns row"
        )


def _request_option(
    upstream_request: dict[str, Any],
    key: str,
    allowed: set[str],
    default: str,
) -> str:
    value = upstream_request.get(key)
    return value if isinstance(value, str) and value in allowed else default


def _request_compression(upstream_request: dict[str, Any]) -> int | None:
    value = upstream_request.get("output_compression")
    if value is None:
        return None
    try:
        compression = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= compression <= 100:
        return compression
    return None


def _request_render_quality(
    upstream_request: dict[str, Any],
    *,
    size: str,
) -> str:
    _ = size
    quality = _request_option(
        upstream_request,
        "render_quality",
        _IMAGE_RENDER_QUALITY_VALUES,
        "auto",
    )
    if quality in {"low", "medium", "high"}:
        return quality
    return "medium"


def _request_responses_model(upstream_request: dict[str, Any]) -> str:
    value = upstream_request.get("responses_model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if bool(upstream_request.get("fast")):
        return DEFAULT_IMAGE_RESPONSES_MODEL_FAST
    return DEFAULT_IMAGE_RESPONSES_MODEL


def _image_request_options(
    upstream_request: dict[str, Any] | None,
    *,
    size: str,
) -> dict[str, Any]:
    req = upstream_request if isinstance(upstream_request, dict) else {}
    fast_mode = bool(req.get("fast"))
    render_quality = _request_render_quality(
        req,
        size=size,
    )
    output_format = _request_option(
        req,
        "output_format",
        _IMAGE_OUTPUT_FORMAT_VALUES,
        "jpeg",
    )
    background = _request_option(
        req,
        "background",
        _IMAGE_BACKGROUND_VALUES,
        "auto",
    )
    if background == "transparent":
        output_format = "png"
    options: dict[str, Any] = {
        "fast": fast_mode,
        "responses_model": _request_responses_model(req),
        "render_quality": render_quality,
        "output_format": output_format,
        "background": background,
        "moderation": _request_option(
            req,
            "moderation",
            _IMAGE_MODERATION_VALUES,
            "low",
        ),
    }
    if output_format in {"jpeg", "webp"}:
        options["output_compression"] = _request_compression(req)
        if options["output_compression"] is None:
            # OpenAI image_generation 默认 100(无压缩，最高画质)。0 实测画质损失明显。
            options["output_compression"] = 100
    return options


async def _ensure_generation_attempt_current(
    session: Any, task_id: str, attempt_epoch: int
) -> None:
    current_attempt = (
        await session.execute(
            select(Generation.attempt).where(Generation.id == task_id).with_for_update()
        )
    ).scalar_one_or_none()
    if current_attempt != attempt_epoch:
        raise _StaleGenerationAttempt(
            f"generation {task_id} attempt moved from {attempt_epoch} to {current_attempt}"
        )


async def _mark_generation_attempt_failed(
    redis: Any,
    *,
    task_id: str,
    message_id: str,
    user_id: str,
    attempt: int,
    error_code: str,
    error_message: str,
    retriable: bool,
) -> bool:
    try:
        async with SessionLocal() as session:
            result = await session.execute(
                _generation_attempt_update(task_id, attempt).values(
                    status=GenerationStatus.FAILED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            _ensure_generation_updated(result, task_id, attempt)
            msg = await session.get(Message, message_id)
            if msg is not None:
                msg.status = MessageStatus.FAILED
            if not retriable:
                gen = await session.get(Generation, task_id)
                if gen is not None:
                    await worker_billing.release_generation(
                        session,
                        gen,
                        reason=error_code,
                    )
            await session.commit()
    except _StaleGenerationAttempt as stale_exc:
        logger.info(
            "generation failed update skipped by stale attempt task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return False

    await publish_event(
        redis,
        user_id,
        task_channel(task_id),
        EV_GEN_FAILED,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "code": error_code,
            "message": error_message,
            "retriable": retriable,
        },
    )
    return True


async def _mark_generation_attempt_retrying(
    redis: Any,
    *,
    task_id: str,
    message_id: str,
    user_id: str,
    attempt: int,
    error_code: str,
    error_message: str,
    delay: float,
    reason: str,
    max_attempts: int,
) -> bool:
    try:
        async with SessionLocal() as session:
            result = await session.execute(
                _generation_attempt_update(task_id, attempt).values(
                    status=GenerationStatus.QUEUED.value,
                    progress_stage=GenerationStage.QUEUED,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            _ensure_generation_updated(result, task_id, attempt)
            await session.commit()
    except _StaleGenerationAttempt as stale_exc:
        logger.info(
            "generation retry update skipped by stale attempt task=%s "
            "attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return False

    try:
        await redis.set(
            _image_queue_not_before_key(task_id),
            str(time.time() + delay),
            ex=_retry_not_before_ttl(delay),
        )
        await redis.enqueue_job(
            "run_generation", task_id, _defer_by=delay, _job_try=attempt + 1
        )
    except Exception as enq_exc:  # noqa: BLE001
        logger.error("re-enqueue failed task=%s err=%s", task_id, enq_exc)
        enqueue_err = "retry_enqueue_failed"
        enqueue_msg = f"failed to enqueue retry: {enq_exc}"
        await _mark_generation_attempt_failed(
            redis,
            task_id=task_id,
            message_id=message_id,
            user_id=user_id,
            attempt=attempt,
            error_code=enqueue_err,
            error_message=enqueue_msg[:2000],
            retriable=False,
        )
        return False

    await publish_event(
        redis,
        user_id,
        task_channel(task_id),
        EV_GEN_RETRYING,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "retry_delay_seconds": delay,
            "error_code": error_code,
            "error_message": error_message,
            "reason": reason,
        },
    )
    return True


async def _await_with_lease_guard(
    awaitable: Awaitable[Any],
    lease_lost: asyncio.Event,
    *,
    redis: Any | None = None,
    task_id: str | None = None,
    cancel_poll_interval_s: float = 1.0,
) -> Any:
    """同时监听 awaitable / lease_lost / 用户取消，三者任一触发都正确清理。

    image-stability-hardening §P2 取消语义合约：
    - **用户显式取消**（POST /tasks/.../cancel 写 Redis ``task:{id}:cancel``）：
      cancel_task 命中 → ``work_task.cancel()`` → 上游 iterator finally aclose
      → 抛 ``_TaskCancelled``，task 记 cancelled 终态。
    - **Worker 进程 lease 丢失**（30s 续约失败 3 次）：lease_task 命中 → 同上但抛
      ``_LeaseLost``，task 由 arq 重新 schedule。
    - **Worker task deadline**（25 分钟硬上限）：上层 ``asyncio.timeout_at`` 抛
      ``CancelledError`` 沿 awaitable 透传，上游 iterator finally 清理 curl 子进程
      / httpx 连接 / 临时 body 文件。
    - **浏览器 SSE 订阅断开**：**不会** 经过本函数；events.py 仅清理 pubsub，绝不写
      cancel key。任务继续 drain 到 final image 或 terminal error，结果落 DB +
      Redis stream，前端重连后 replay 拿到。
    """
    if lease_lost.is_set():
        raise _LeaseLost("generation lease renewer failed")

    async def wait_cancelled() -> None:
        assert redis is not None
        assert task_id is not None
        interval_s = max(0.05, float(cancel_poll_interval_s))
        while True:
            if await _is_cancelled(redis, task_id):
                return
            await asyncio.sleep(interval_s)

    work_task = asyncio.create_task(awaitable)
    lease_task = asyncio.create_task(lease_lost.wait())
    cancel_task: asyncio.Task[None] | None = (
        asyncio.create_task(wait_cancelled())
        if redis is not None and task_id is not None
        else None
    )
    try:
        watch_tasks: set[asyncio.Task[Any]] = {work_task, lease_task}
        if cancel_task is not None:
            watch_tasks.add(cancel_task)
        done, _pending = await asyncio.wait(
            watch_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lease_task in done and lease_lost.is_set():
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
            raise _LeaseLost("generation lease renewer failed")
        if cancel_task is not None and cancel_task in done:
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
            raise _TaskCancelled("cancelled during upstream call")
        return await work_task
    finally:
        if not work_task.done():
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
        lease_task.cancel()
        with suppress(asyncio.CancelledError):
            await lease_task
        if cancel_task is not None:
            cancel_task.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_task


async def _consume_image_iter_close_result(
    image_iter: AsyncIterator[tuple[str, str | None]] | None,
    *,
    task_id: str,
) -> None:
    """关 image_iter 并吞掉 cancel / generic 异常。

    失败路径下生成器仍持有 SSE / curl 子进程 fd，必须 await 关掉，cancel 后
    才不会被推到下一轮 loop 才回收（4K 高负载 + 失败累积会顶到 fd 上限）。
    aclose() 自身可能再抛（内层已 cancelled / 子进程已死），用 try/except 兜
    底，不让它打断后续 redis cleanup。模块级函数避免每次进 finally 重新定义。
    """
    if image_iter is None:
        return
    try:
        await image_iter.aclose()
    except (asyncio.CancelledError, GeneratorExit):
        # 内部本就在退出；视为已关，继续后面 cleanup
        pass
    except Exception:  # noqa: BLE001
        logger.debug(
            "generation image iterator aclose failed task=%s",
            task_id,
            exc_info=True,
        )


async def _anext_image_with_guards(
    image_iter: AsyncIterator[tuple[str, str | None]],
    lease_lost: asyncio.Event,
    *,
    redis: Any,
    task_id: str,
) -> tuple[str, str | None] | None:
    """从 image_iter 取下一份 (b64, revised_prompt)，同 _await_with_lease_guard 的
    lease/cancel 守护。StopAsyncIteration → None（generator 耗尽视作正常结束）。
    """
    try:
        return await _await_with_lease_guard(
            image_iter.__anext__(),
            lease_lost,
            redis=redis,
            task_id=task_id,
        )
    except StopAsyncIteration:
        return None


async def _find_existing_generated_image(
    session: Any, *, task_id: str, user_id: str
) -> Image | None:
    """GEN-P0-4: 幂等短路必须同时 owner_generation_id=task_id 且 user_id=请求发起者。

    用 FOR UPDATE 锁住候选行，并在返回前双重校验 user_id 匹配——即使上游 schema
    被改坏（比如将来有 share/transfer 让单张 Image 跨 user_id），这里也不会把他人的
    图作为"已有成图"直接返回给当前 task 的用户。
    """
    row = (
        await session.execute(
            select(Image)
            .where(
                Image.owner_generation_id == task_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    # Defensive: 在极端情况下 user_id 被旁路修改时拒绝短路，宁可重新生成也不串用户。
    if getattr(row, "user_id", None) != user_id:
        logger.error(
            "short-circuit guard: image %s user mismatch expect=%s got=%s — ignoring",
            getattr(row, "id", "?"),
            user_id,
            getattr(row, "user_id", None),
        )
        return None
    return row


async def _ensure_generation_conversation_alive(
    session: Any,
    *,
    message_id: str,
    user_id: str,
    lock: bool = False,
) -> str:
    stmt = (
        select(Conversation.id)
        .join(Message, Message.conversation_id == Conversation.id)
        .where(
            Message.id == message_id,
            Message.deleted_at.is_(None),
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    if lock:
        stmt = stmt.with_for_update(of=Conversation)
    conversation_id = (await session.execute(stmt)).scalar_one_or_none()
    if conversation_id is None:
        raise _TaskCancelled("conversation or message was deleted")
    return str(conversation_id)


# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------


def _classify_exception(exc: BaseException, has_partial: bool) -> RetryDecision:
    if isinstance(exc, StorageDiskFullError):
        return is_retriable(
            EC.DISK_FULL.value, None, has_partial, error_message=str(exc)
        )
    if isinstance(exc, TimeoutError):
        return is_retriable("timeout", None, has_partial, error_message=str(exc))
    if isinstance(exc, UpstreamError):
        return is_retriable(
            exc.error_code, exc.status_code, has_partial, error_message=str(exc)
        )
    if isinstance(
        exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)
    ):
        return is_retriable("upstream_error", None, has_partial, error_message=str(exc))
    if isinstance(exc, httpx.HTTPError):
        return is_retriable("upstream_error", None, has_partial, error_message=str(exc))
    # 其他未预期异常 → 不重试（避免放大故障）
    return RetryDecision(False, f"unhandled {type(exc).__name__}")


def _safe_generation_error_details(exc: BaseException) -> dict[str, Any]:
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        return {}
    details: dict[str, Any] = {}
    transparent_qc = payload.get("transparent_qc")
    if isinstance(transparent_qc, dict):
        details["transparent_qc"] = transparent_qc
    transparent_provider = payload.get("transparent_provider")
    if isinstance(transparent_provider, str) and transparent_provider:
        details["transparent_provider"] = transparent_provider[:128]
    return details


def _decide_moderation_retry_upgrade(
    *,
    base_decision: RetryDecision,
    err_code: str | None,
    err_msg: str,
    is_dual_race: bool,
    reserved_provider_name: str | None,
    enabled_provider_count: int,
    already_avoided_count: int,
    cap: int = _MODERATION_RETRY_CAP,
) -> RetryDecision | None:
    """moderation_blocked 在多 provider 部署下 → 换号再试，避免 single-attempt terminal。

    返回升级后的 RetryDecision（retriable=True），或 None 表示沿用 base_decision。
    Pure function——所有运行时上下文由调用方注入，便于单元测试。
    """
    if base_decision.retriable:
        return None
    if not is_moderation_block(err_code, err_msg):
        return None
    if is_dual_race or not reserved_provider_name:
        # dual_race inner failover 已经在同一 attempt 内换号；锁号缺失说明没有 task→provider 绑定
        return None
    if enabled_provider_count <= 1:
        return None
    # already_avoided_count = 进入本次 except 时 avoid set 的大小（不含本次）；
    # _avoid_provider_for_task 在 retriable 分支里把当前 reserved 加入，下次 reserve 跳过它。
    if enabled_provider_count - already_avoided_count <= 1:
        return None
    if already_avoided_count + 1 >= min(cap, enabled_provider_count):
        return None
    return RetryDecision(retriable=True, reason="moderation_blocked try_next_provider")


async def _delete_storage_keys(keys: list[str]) -> None:
    unique_keys = list(dict.fromkeys(keys))
    if not unique_keys:
        return
    cleanup = asyncio.ensure_future(
        asyncio.gather(
            *(asyncio.to_thread(storage.delete, key) for key in unique_keys),
            return_exceptions=True,
        )
    )
    try:
        results = await asyncio.shield(cleanup)
    except asyncio.CancelledError:

        def _log_late_cleanup(task: asyncio.Task[Any]) -> None:
            with suppress(Exception):
                late_results = task.result()
                for key, result in zip(unique_keys, late_results, strict=False):
                    if isinstance(result, BaseException):
                        logger.warning(
                            "storage cleanup failed key=%s err=%s", key, result
                        )

        cleanup.add_done_callback(_log_late_cleanup)
        raise
    for key, result in zip(unique_keys, results, strict=False):
        if isinstance(result, BaseException):
            logger.warning("storage cleanup failed key=%s err=%s", key, result)


async def _write_generation_files(files: list[tuple[str, bytes]]) -> list[str]:
    async def put_one(key: str, data: bytes) -> tuple[str, bool]:
        result = await asyncio.to_thread(storage.put_bytes_result, key, data)
        return key, bool(result.created)

    results = await asyncio.gather(
        *(put_one(key, data) for key, data in files),
        return_exceptions=True,
    )
    created_keys: list[str] = []
    first_exc: BaseException | None = None
    for result in results:
        if isinstance(result, BaseException):
            first_exc = first_exc or result
            continue
        key, created = result
        if created:
            created_keys.append(key)
    if first_exc is not None:
        await _delete_storage_keys(created_keys)
        raise first_exc
    return created_keys


@asynccontextmanager
async def _cleanup_storage_on_error(keys: list[str]) -> AsyncIterator[None]:
    try:
        yield
    except Exception:
        await _delete_storage_keys(keys)
        raise


# ---------------------------------------------------------------------------
# Dual-race bonus image handler
# ---------------------------------------------------------------------------


async def _handle_dual_race_bonus_image(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    parent_task_id: str,
    parent_idempotency_key: str,
    parent_upstream_request: dict[str, Any] | None,
    message_id: str,
    action: str,
    model: str,
    prompt: str,
    size_requested: str,
    aspect_ratio: str,
    input_image_ids: list[str],
    primary_input_image_id: str | None,
    references: list[tuple[str, bytes]],
    image_request_options: dict[str, Any],
    b64_result: str,
    revised_prompt: str | None,
    upstream_provider: str | None = None,
    upstream_actual_route: str | None = None,
    upstream_actual_source: str | None = None,
    upstream_actual_endpoint: str | None = None,
) -> None:
    """处理 dual_race 的 bonus 图：建独立 generation row + 写盘 + 写 DB + publish。

    bonus 图作为同一条 assistant message 的另一条 generation；前端通过 EV_GEN_ATTACHED
    把 bonus_gen_id push 到 message.generation_ids 并建 placeholder，再消费
    EV_GEN_SUCCEEDED 把图挂上去。bonus row 的 upstream_request 带 is_dual_race_bonus=true
    让成本统计 / BI 查询能过滤掉（避免 dual_race 用户翻倍计费）。

    任何异常都不抛——bonus 是"锦上添花"，失败只 log warn，不影响 winner 已成功的状态。
    """
    if not b64_result:
        return

    # --- 1. 解码 + 校验 ---
    try:
        raw_image = _decode_upstream_image_b64(b64_result)
    except binascii.Error:
        logger.warning("dual_race bonus base64 decode failed parent=%s", parent_task_id)
        return
    sha = _sha256(raw_image)

    # SHA echo: bonus 不能是参考图本身（EDIT 时）
    if action == GenerationAction.EDIT.value:
        if any(sha == ref_sha for ref_sha, _ in references):
            logger.info(
                "dual_race bonus sha echoed reference parent=%s; skip",
                parent_task_id,
            )
            return

    transparent_requested = image_request_options.get("background") == "transparent"
    transparent_alpha_recovered = False
    transparent_qc_payload: dict[str, Any] | None = None
    transparent_provider: str | None = None

    try:
        with PILImage.open(io.BytesIO(raw_image)) as pil:
            pil.load()
            if pil.format not in ("PNG", "WEBP", "JPEG"):
                logger.warning(
                    "dual_race bonus unexpected format=%s parent=%s",
                    pil.format,
                    parent_task_id,
                )
                return
            orig_format = pil.format
            width, height = pil.size
            if width < 1 or height < 1 or width > 10000 or height > 10000:
                logger.warning(
                    "dual_race bonus dims out of range %dx%d parent=%s",
                    width,
                    height,
                    parent_task_id,
                )
                return
            processed: PILImage.Image | None = None
            if transparent_requested and not _image_has_transparency(pil):
                try:
                    pipeline_out = await process_transparent_request(pil, prompt=prompt)
                except TransparentPipelineFailure as exc:
                    logger.info(
                        "dual_race bonus transparent pipeline failed parent=%s err=%r",
                        parent_task_id,
                        exc,
                    )
                    return
                raw_image = pipeline_out.rgba_png
                sha = _sha256(raw_image)
                orig_format = "PNG"
                width, height = pipeline_out.width, pipeline_out.height
                transparent_alpha_recovered = True
                transparent_qc_payload = pipeline_out.qc.to_dict()
                transparent_provider = pipeline_out.provider
                processed = PILImage.open(io.BytesIO(raw_image))
                processed.load()
            try:
                output_pil = processed or pil
                blurhash_str = _compute_blurhash(output_pil)
                display_bytes, display_size = _make_display(output_pil)
                preview_bytes, preview_size = _make_preview(output_pil)
                thumb_bytes, thumb_size = _make_thumb(output_pil)
            finally:
                if processed is not None:
                    processed.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dual_race bonus pillow decode failed parent=%s err=%r",
            parent_task_id,
            exc,
        )
        return

    # --- 2. 写盘 ---
    bonus_gen_id = new_uuid7()
    image_id = new_uuid7()
    orig_ext_by_format = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}
    orig_mime_by_format = {
        "PNG": "image/png",
        "WEBP": "image/webp",
        "JPEG": "image/jpeg",
    }
    orig_ext = orig_ext_by_format[orig_format]
    orig_mime = orig_mime_by_format[orig_format]
    model_metadata = _model_image_metadata_from_request(
        image_id=image_id,
        mime=orig_mime,
        request=parent_upstream_request,
        prompt=prompt,
    )
    bonus_billing_meta: dict[str, Any] = {
        "is_dual_race_bonus": True,
        "billing_free": True,
        "billing_label": "free",
        "billing_exempt_reason": "dual_race_loser",
    }
    image_metadata: dict[str, Any] = {**model_metadata, **bonus_billing_meta}
    if model_metadata:
        try:
            with PILImage.open(io.BytesIO(raw_image)) as im:
                im.load()
                raw_image = _maybe_embed_model_image_metadata_bytes(
                    image=im,
                    fmt=orig_format,
                    raw_image=raw_image,
                    metadata=model_metadata,
                )
            sha = _sha256(raw_image)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "dual_race bonus model metadata embed skipped parent=%s err=%s",
                parent_task_id,
                exc,
            )
    key_orig = f"u/{user_id}/g/{bonus_gen_id}/orig.{orig_ext}"
    key_display = f"u/{user_id}/g/{bonus_gen_id}/display2048.webp"
    key_preview = f"u/{user_id}/g/{bonus_gen_id}/preview1024.webp"
    key_thumb = f"u/{user_id}/g/{bonus_gen_id}/thumb256.jpg"

    try:
        created_storage_keys = await _write_generation_files(
            [
                (key_orig, raw_image),
                (key_display, display_bytes),
                (key_preview, preview_bytes),
                (key_thumb, thumb_bytes),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dual_race bonus storage write failed parent=%s err=%r",
            parent_task_id,
            exc,
        )
        return

    # --- 3. 写 DB ---
    try:
        async with _cleanup_storage_on_error(created_storage_keys):
            async with SessionLocal() as session:
                # idempotency_key 长度上限 64：parent + ":b" 后缀截断
                bonus_idem = f"{parent_idempotency_key[:62]}:b"
                bonus_upstream_req: dict[str, Any] = dict(parent_upstream_request or {})
                bonus_upstream_req.update(image_request_options)
                bonus_upstream_req["size_actual"] = f"{width}x{height}"
                bonus_upstream_req["mime"] = orig_mime
                bonus_upstream_req["is_dual_race_bonus"] = True
                bonus_upstream_req["billing_free"] = True
                bonus_upstream_req["billing_label"] = "free"
                bonus_upstream_req["billing_exempt_reason"] = "dual_race_loser"
                bonus_upstream_req["parent_generation_id"] = parent_task_id
                if upstream_provider:
                    bonus_upstream_req["provider"] = upstream_provider
                    bonus_upstream_req["actual_provider"] = upstream_provider
                else:
                    # Do not inherit the winner's provider from the parent row.
                    bonus_upstream_req.pop("provider", None)
                    bonus_upstream_req.pop("actual_provider", None)
                if upstream_actual_route:
                    bonus_upstream_req["actual_route"] = upstream_actual_route
                if upstream_actual_source:
                    bonus_upstream_req["actual_source"] = upstream_actual_source
                if upstream_actual_endpoint:
                    bonus_upstream_req["actual_endpoint"] = upstream_actual_endpoint
                if transparent_alpha_recovered:
                    bonus_upstream_req["transparent_alpha_recovered"] = True
                if transparent_qc_payload is not None:
                    bonus_upstream_req["transparent_qc"] = transparent_qc_payload
                if transparent_provider is not None:
                    bonus_upstream_req["transparent_pipeline_provider"] = (
                        transparent_provider
                    )
                if revised_prompt:
                    bonus_upstream_req["revised_prompt"] = revised_prompt

                now = datetime.now(timezone.utc)
                bonus_row = Generation(
                    id=bonus_gen_id,
                    message_id=message_id,
                    user_id=user_id,
                    action=action,
                    model=model,
                    prompt=prompt,
                    size_requested=size_requested,
                    aspect_ratio=aspect_ratio,
                    input_image_ids=list(input_image_ids),
                    primary_input_image_id=primary_input_image_id,
                    upstream_request=bonus_upstream_req,
                    status=GenerationStatus.SUCCEEDED.value,
                    progress_stage=GenerationStage.FINALIZING.value,
                    attempt=0,
                    idempotency_key=bonus_idem,
                    started_at=now,
                    finished_at=now,
                    upstream_pixels=width * height,
                )
                session.add(bonus_row)

                img = Image(
                    id=image_id,
                    user_id=user_id,
                    owner_generation_id=bonus_gen_id,
                    source=ImageSource.GENERATED.value,
                    parent_image_id=(
                        primary_input_image_id
                        if action == GenerationAction.EDIT.value
                        else None
                    ),
                    storage_key=key_orig,
                    mime=orig_mime,
                    width=width,
                    height=height,
                    size_bytes=len(raw_image),
                    sha256=sha,
                    blurhash=blurhash_str,
                    visibility="private",
                    metadata_jsonb=image_metadata,
                )
                session.add(img)
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="display2048",
                        storage_key=key_display,
                        width=display_size[0],
                        height=display_size[1],
                    )
                )
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="preview1024",
                        storage_key=key_preview,
                        width=preview_size[0],
                        height=preview_size[1],
                    )
                )
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="thumb256",
                        storage_key=key_thumb,
                        width=thumb_size[0],
                        height=thumb_size[1],
                    )
                )

                # Append bonus image to message.content.images
                msg: Message | None = await session.get(Message, message_id)
                if msg is not None:
                    content = dict(msg.content or {})
                    images_list = list(content.get("images") or [])
                    images_list.append(
                        {
                            "image_id": image_id,
                            "from_generation_id": bonus_gen_id,
                            "width": width,
                            "height": height,
                            "mime": orig_mime,
                            "url": storage.public_url(key_orig),
                            "display_url": f"/api/images/{image_id}/variants/display2048",
                            "preview_url": f"/api/images/{image_id}/variants/preview1024",
                            "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                            "filename": image_metadata.get("suggested_filename"),
                            **bonus_billing_meta,
                        }
                    )
                    content["images"] = images_list
                    msg.content = content
                    # bonus 不动 msg.status——winner 已置 SUCCEEDED

                # 模特库聚合视图需要看到 loser 候选图：把 bonus image_id 写回
                # 同一 step 的 output_json.dual_race_bonus_image_ids（与 winner
                # step.image_ids 物理隔离），共用同一 session 同一 commit。
                try:
                    await _maybe_record_model_library_candidate_image(
                        session=session,
                        user_id=user_id,
                        parent_upstream_request=parent_upstream_request or {},
                        bonus_image_id=image_id,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "model_library candidate hook failed parent=%s err=%s",
                        parent_task_id,
                        exc,
                    )

                await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dual_race bonus DB write failed parent=%s err=%r",
            parent_task_id,
            exc,
        )
        return

    # --- 4. Publish 事件 ---
    # 先 ATTACHED：让前端在 store 里建 generation placeholder 并把 id push 到
    # message.generation_ids；再 SUCCEEDED：把图挂到 generation 上。
    try:
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_ATTACHED,
            {
                "message_id": message_id,
                "generation_id": bonus_gen_id,
                "parent_generation_id": parent_task_id,
                "action": action,
                "prompt": prompt,
                "size_requested": size_requested,
                "aspect_ratio": aspect_ratio,
                "input_image_ids": list(input_image_ids),
                "primary_input_image_id": primary_input_image_id,
                **bonus_billing_meta,
            },
        )
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_SUCCEEDED,
            {
                "generation_id": bonus_gen_id,
                "message_id": message_id,
                "images": [
                    {
                        "image_id": image_id,
                        "from_generation_id": bonus_gen_id,
                        "actual_size": f"{width}x{height}",
                        "mime": orig_mime,
                        "url": storage.public_url(key_orig),
                        "display_url": f"/api/images/{image_id}/variants/display2048",
                        "preview_url": f"/api/images/{image_id}/variants/preview1024",
                        "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                        "filename": image_metadata.get("suggested_filename"),
                        **bonus_billing_meta,
                    }
                ],
                "final_size": f"{width}x{height}",
                **bonus_billing_meta,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dual_race bonus publish failed parent=%s err=%r",
            parent_task_id,
            exc,
        )
        return

    logger.info(
        "dual_race bonus image done: parent=%s bonus=%s", parent_task_id, bonus_gen_id
    )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def run_generation(ctx: dict[str, Any], task_id: str) -> None:  # noqa: PLR0915, PLR0912
    """arq entry for generation task."""
    redis = ctx["redis"]
    worker_id = str(ctx.get("worker_id") or ctx.get("job_id") or "worker")
    _task_start = asyncio.get_event_loop().time()
    _task_deadline = _task_start + _RUN_GENERATION_TIMEOUT_S
    _task_outcome = "unknown"
    attempt = 0
    renewer: asyncio.Task[None] | None = None
    lease_lost = asyncio.Event()
    reserved_provider: Any | None = None
    reserved_provider_name: str | None = None
    user_api_credential_id: str | None = None
    user_runtime_provider: Any | None = None
    loaded_attempt = 0
    channel = task_channel(task_id)

    # 让 ProviderPool 能用 redis 做账号级 quota 检查 / 入账（image route）。
    # 单例 pool 内部缓存最后一次注入的 redis；arq ctx['redis'] 在 worker 生命周期内
    # 是稳定的，每个 task 重复 attach 是无害幂等的。
    try:
        from ..provider_pool import get_pool

        _pool = await get_pool()
        _pool.attach_redis(redis)
    except Exception:  # noqa: BLE001
        # attach 失败不致命——limiter 看到 redis=None 会短路放行
        logger.debug("provider_pool attach_redis failed", exc_info=True)

    # --- 1. 读 generation 行，幂等判断 ---
    async with SessionLocal() as session:
        # P1-10: skip_locked=True 让并发 worker 不会因为另一个 worker 已锁住此行
        # 而无限等待；锁不到时返回 None，进入下面的"找不到/已终态"分支退出，
        # arq 重试机制会接管。比默认的"阻塞等到 lease 到期"语义更安全。
        gen: Generation | None = (
            await session.execute(
                select(Generation)
                .where(Generation.id == task_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if gen is None:
            logger.warning("generation not found task_id=%s", task_id)
            return
        if is_generation_terminal(gen.status):
            logger.info(
                "generation already terminal task_id=%s status=%s", task_id, gen.status
            )
            return
        if gen.status == GenerationStatus.RUNNING.value:
            logger.info("generation already running task_id=%s", task_id)
            return

        loaded_attempt = gen.attempt
        user_id = gen.user_id
        message_id = gen.message_id
        action = gen.action
        prompt = gen.prompt
        aspect_ratio = gen.aspect_ratio
        size_requested = gen.size_requested
        input_image_ids = list(gen.input_image_ids or [])
        primary_input_image_id = gen.primary_input_image_id
        user_api_credential_id = getattr(gen, "user_api_credential_id", None)
        # 局部 inpaint mask（PostMessageIn.mask_image_id）。EDIT 任务可选；GENERATE
        # 任务忽略（schema 不允许，但防御性 detach 一份不影响）。worker 在 reference
        # images 加载阶段从 Image.storage_key 取 mask 字节。
        mask_image_id: str | None = getattr(gen, "mask_image_id", None)
        # session 关闭后仍要在 dual_race bonus 处理里读这两个字段，提前 detach 取值
        gen_idempotency_key = gen.idempotency_key
        gen_model = gen.model
        gen_upstream_request_snapshot: dict[str, Any] | None = (
            dict(gen.upstream_request)
            if isinstance(gen.upstream_request, dict)
            else None
        )
        image_request_options = _image_request_options(
            gen.upstream_request,
            size=size_requested,
        )

        try:
            await _ensure_generation_conversation_alive(
                session,
                message_id=message_id,
                user_id=user_id,
            )
        except _TaskCancelled as exc:
            result = await session.execute(
                update(Generation)
                .where(Generation.id == task_id, Generation.attempt == gen.attempt)
                .values(
                    status=GenerationStatus.CANCELED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=EC.CANCELLED.value,
                    error_message=str(exc),
                )
            )
            _ensure_generation_updated(result, task_id, gen.attempt)
            msg_deleted = await session.get(Message, message_id)
            if msg_deleted is not None and msg_deleted.status not in (
                MessageStatus.SUCCEEDED,
                MessageStatus.FAILED,
            ):
                msg_deleted.status = MessageStatus.FAILED
            await worker_billing.release_generation(
                session,
                gen,
                reason=EC.CANCELLED.value,
            )
            await session.commit()
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": EC.CANCELLED.value,
                    "message": str(exc),
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            return

        if not _primary_input_image_id_valid(primary_input_image_id, input_image_ids):
            err_code = EC.INVALID_PARAM.value
            err_msg = "primary_input_image_id must be included in input_image_ids"
            result = await session.execute(
                update(Generation)
                .where(Generation.id == task_id, Generation.attempt == gen.attempt)
                .values(
                    status=GenerationStatus.FAILED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=err_code,
                    error_message=err_msg,
                )
            )
            _ensure_generation_updated(result, task_id, gen.attempt)
            msg_invalid = await session.get(Message, message_id)
            if msg_invalid is not None:
                msg_invalid.status = MessageStatus.FAILED
            await worker_billing.release_generation(
                session,
                gen,
                reason=err_code,
            )
            await session.commit()
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": err_code,
                    "message": err_msg,
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            return

        # Why: 重试时若上一次已经写过 Image 行（崩在 commit 之后、状态更新之前），
        # 不要再新建一份；直接复用旧记录并 publish succeeded，避免双图。
        existing_img = await _find_existing_generated_image(
            session, task_id=task_id, user_id=user_id
        )
        if existing_img is not None:
            logger.info(
                "generation already has image task_id=%s image_id=%s — short-circuit",
                task_id,
                existing_img.id,
            )
            result = await session.execute(
                update(Generation)
                .where(Generation.id == task_id, Generation.attempt == gen.attempt)
                .values(
                    status=GenerationStatus.SUCCEEDED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    upstream_pixels=existing_img.width * existing_img.height,
                    error_code=None,
                    error_message=None,
                )
            )
            _ensure_generation_updated(result, task_id, gen.attempt)
            msg_existing = await session.get(Message, message_id)
            if (
                msg_existing is not None
                and msg_existing.status != MessageStatus.SUCCEEDED
            ):
                msg_existing.status = MessageStatus.SUCCEEDED
            await worker_billing.settle_generation(
                session,
                gen,
                width=existing_img.width,
                height=existing_img.height,
            )
            await session.commit()
            channel_short = task_channel(task_id)
            await publish_event(
                redis,
                user_id,
                channel_short,
                EV_GEN_SUCCEEDED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "images": [
                        {
                            "image_id": existing_img.id,
                            "from_generation_id": task_id,
                            "actual_size": f"{existing_img.width}x{existing_img.height}",
                            "url": storage.public_url(existing_img.storage_key),
                        }
                    ],
                    "final_size": f"{existing_img.width}x{existing_img.height}",
                },
            )
            try:
                _duration = asyncio.get_event_loop().time() - _task_start
                task_duration_seconds.labels(
                    kind="generation", outcome=safe_outcome("succeeded")
                ).observe(_duration)
            except Exception:  # noqa: BLE001
                pass
            return

        # --- 2. Max-attempt guard; actual running transition happens after
        # the unified image queue admits this task.
        attempt, attempt_may_run = _bounded_next_attempt(gen.attempt)
        if not attempt_may_run:
            err_code = "max_attempts_exceeded"
            err_msg = f"generation exceeded max attempts ({_MAX_ATTEMPTS})"
            result = await session.execute(
                update(Generation)
                .where(Generation.id == task_id, Generation.attempt == gen.attempt)
                .values(
                    status=GenerationStatus.FAILED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    attempt=attempt,
                    finished_at=datetime.now(timezone.utc),
                    error_code=err_code,
                    error_message=err_msg,
                )
            )
            _ensure_generation_updated(result, task_id, gen.attempt)
            msg_failed = await session.get(Message, message_id)
            if msg_failed is not None:
                msg_failed.status = MessageStatus.FAILED
            await session.commit()
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": err_code,
                    "message": err_msg,
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            try:
                _duration = asyncio.get_event_loop().time() - _task_start
                task_duration_seconds.labels(
                    kind="generation", outcome=safe_outcome(_task_outcome)
                ).observe(_duration)
            except Exception:  # noqa: BLE001
                pass
            return

    provider_queue_delay = 0
    try:
        image_route = await _resolve_image_primary_route()
    except Exception:  # noqa: BLE001
        image_route = "responses"
    if user_api_credential_id:
        try:
            async with SessionLocal() as session:
                user_runtime_provider = await resolve_user_credential_runtime(
                    session,
                    user_api_credential_id,
                )
            # purpose 守卫：image 任务必须要 supplier purposes 包含 "image"，
            # 否则即便 credential 解析成功也拒掉，避免把 chat-only key 用到 image。
            if "image" not in (getattr(user_runtime_provider, "purposes", ()) or ()):
                raise UpstreamError(
                    "user API key supplier does not allow image purpose",
                    status_code=403,
                    error_code="byok_purpose_mismatch",
                    payload={"credential_id": user_api_credential_id},
                )
        except Exception as exc:  # noqa: BLE001
            byok_error = classify_user_credential_error(exc)[1] or "invalid_api_key"
            await record_user_credential_runtime_error(user_api_credential_id, exc)
            err_code = byok_error_to_generation_code(byok_error)
            err_msg = byok_error_message(byok_error)
            try:
                async with SessionLocal() as session:
                    result = await session.execute(
                        update(Generation)
                        .where(
                            Generation.id == task_id,
                            Generation.attempt == loaded_attempt,
                        )
                        .values(
                            status=GenerationStatus.FAILED.value,
                            progress_stage=GenerationStage.FINALIZING,
                            # 不要把 attempt 写回成局部 attempt（初值 0）；该任务可能
                            # 已经跑过若干次 retry，gen.attempt > 0 时回退会让监控/重试
                            # 计数错乱。保持原值即可。
                            attempt=loaded_attempt,
                            finished_at=datetime.now(timezone.utc),
                            error_code=err_code,
                            error_message=err_msg,
                        )
                    )
                    _ensure_generation_updated(result, task_id, loaded_attempt)
                    msg_failed = await session.get(Message, message_id)
                    if msg_failed is not None:
                        msg_failed.status = MessageStatus.FAILED
                    await session.commit()
            except _StaleGenerationAttempt:
                _task_outcome = "stale_attempt"
                return
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": err_code,
                    "message": err_msg,
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            return
        if image_route == "dual_race":
            image_route = "responses"
    is_dual_race = image_route == "dual_race"
    endpoint_kind = (
        None if is_dual_race else _image_endpoint_kind_for_engine(image_route)
    )
    # mask 不为空 → reserve 阶段把任务标记给 ProviderPool：sidecar 路径优先
    # file-mode provider，file-mode 候选耗尽时允许 url-mode 兜底；direct 路径本身
    # 是 multipart，不依赖 provider 的 image_edit_input_transport 配置。
    requires_mask_provider = bool(mask_image_id) and action == GenerationAction.EDIT
    try:
        reserved_provider = await _reserve_image_queue_slot(
            redis,
            task_id,
            dual_race=is_dual_race,
            endpoint_kind=endpoint_kind,
            requires_mask=requires_mask_provider,
            provider_override=user_runtime_provider,
        )
    except UpstreamError as exc:
        # 兼容旧代码路径：如果仍有老 guard 抛 NO_MASK_CAPABLE_PROVIDER，按 terminal 处理。
        if getattr(exc, "error_code", None) == EC.NO_MASK_CAPABLE_PROVIDER.value:
            raise
        if getattr(exc, "error_code", None) != EC.ALL_ACCOUNTS_FAILED.value:
            raise
        provider_queue_delay = _IMAGE_PROVIDER_UNAVAILABLE_RETRY_S
        await redis.set(
            _image_queue_not_before_key(task_id),
            str(time.time() + provider_queue_delay),
            ex=provider_queue_delay + _IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
        )
        await _enqueue_generation_once(
            redis,
            task_id,
            defer_by=provider_queue_delay,
        )
    if reserved_provider is None:
        await _clear_image_queue_enqueue_dedupe(redis, task_id)
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_QUEUED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "stage": GenerationStage.QUEUED.value,
                "reason": (
                    "image_provider_unavailable"
                    if provider_queue_delay
                    else "image_queue_waiting"
                ),
            },
        )
        _task_outcome = "queued"
        return

    reserved_provider_name = _redis_text(getattr(reserved_provider, "name", None))
    upstream_provider_label = (
        "dual_race"
        if _is_dual_race_sentinel(reserved_provider_name)
        else reserved_provider_name
    )

    # --- 3. lease + 续租协程 ---
    await _acquire_lease(redis, task_id, worker_id)

    async with SessionLocal() as session:
        # P1-10: skip_locked=True 同上——并发 worker 抢锁失败时不阻塞。
        current: Generation | None = (
            await session.execute(
                select(Generation)
                .where(Generation.id == task_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if current is None or is_generation_terminal(current.status):
            _task_outcome = "stale_attempt"
            await _release_image_queue_slot(
                redis, task_id=task_id, provider_name=reserved_provider_name
            )
            await _release_lease(redis, task_id, worker_id)
            return
        attempt, attempt_may_run = _bounded_next_attempt(current.attempt)
        if not attempt_may_run:
            _task_outcome = "stale_attempt"
            await _release_image_queue_slot(
                redis, task_id=task_id, provider_name=reserved_provider_name
            )
            await _release_lease(redis, task_id, worker_id)
            return
        running_upstream_request: dict[str, Any] = (
            dict(current.upstream_request)
            if isinstance(current.upstream_request, dict)
            else {}
        )
        lease_reacquired = current.error_code == "lease_lost"
        running_upstream_request["upstream_route"] = image_route
        if is_dual_race:
            running_upstream_request.pop("provider", None)
            running_upstream_request.pop("actual_provider", None)
        elif upstream_provider_label:
            running_upstream_request["provider"] = upstream_provider_label
        result = await session.execute(
            update(Generation)
            .where(
                Generation.id == task_id,
                Generation.attempt == current.attempt,
                Generation.status == GenerationStatus.QUEUED.value,
            )
            .values(
                status=GenerationStatus.RUNNING.value,
                progress_stage=GenerationStage.RENDERING,
                started_at=datetime.now(timezone.utc),
                attempt=attempt,
                upstream_request=running_upstream_request,
                error_code=None,
                error_message=None,
            )
        )
        try:
            _ensure_generation_updated(result, task_id, current.attempt)
        except _StaleGenerationAttempt:
            await _release_image_queue_slot(
                redis, task_id=task_id, provider_name=reserved_provider_name
            )
            await _release_lease(redis, task_id, worker_id)
            raise
        await session.commit()

    renewer = asyncio.create_task(
        _lease_renewer(
            redis,
            task_id,
            lease_lost,
            # task_provider 反向索引仍是 SET key，需要 EXPIRE 续命；ZSET 槽位
            # 续命交给 lease_renewer 内部按 image_provider_name 分支处理。
            extra_lease_keys=[_image_task_provider_key(task_id)],
            image_provider_name=reserved_provider_name,
        )
    )

    # --- 4. publish started ---
    await publish_event(
        redis,
        user_id,
        channel,
        EV_GEN_STARTED,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "provider": None if is_dual_race else upstream_provider_label,
            "route": image_route,
            "lease_reacquired": bool(lease_reacquired),
        },
    )
    if lease_reacquired:
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "stage": GenerationStage.QUEUED.value,
                "substage": _LEASE_REACQUIRED_SUBSTAGE,
            },
        )
    # 写入 in-flight provider 快照初值；后续 publish_image_progress 收到 provider_used 时
    # 覆盖具体 provider 名（dual_race 两条 lane 各占一个 field）。
    initial_inflight: dict[str, str] = {
        "mode": "dual_race" if is_dual_race else "single",
        "route": image_route or "",
        "task_id": task_id,
    }
    if not is_dual_race and reserved_provider_name:
        # 单 provider 模式下 reserve 阶段就锁定了 provider；先把它放进去，避免 admin
        # 在 provider_used 之前的几秒空窗看到"未记录"。
        initial_inflight["provider"] = reserved_provider_name
    await _inflight_set_fields(redis, task_id, initial_inflight)
    await _kick_image_queue(redis)

    has_partial = (
        False  # 新同步路径不存在 partial，永远为 False（保留变量给下方 classify 用）
    )
    image_iter: AsyncIterator[tuple[str, str | None]] | None = None

    try:
        # 新 API 不支持 size="auto"，强制走 fixed 模式（让 resolve_size 走预设/比例回退）
        size_mode = "fixed"
        fixed_size = (
            size_requested if (size_requested and "x" in size_requested) else None
        )
        try:
            resolved = resolve_size(aspect_ratio, size_mode, fixed_size)
            _validate_resolved_size(
                resolved.size,
                aspect_ratio,
                validate_aspect_ratio=fixed_size is None,
            )
        except ValueError as exc:
            # GEN-P2 size_requested API 层校验补丁：worker 兜底捕获 sizing.validate_explicit_size
            # 抛的 ValueError，转为 terminal UpstreamError 并走 failed 分支。即使 API 漏检
            # （比如旧 task 已落库），这里也不会让 worker 直接崩。
            raise UpstreamError(
                f"invalid size_requested: {exc}",
                status_code=400,
                error_code=EC.INVALID_VALUE.value,
                payload={
                    "size_requested": size_requested,
                    "aspect_ratio": aspect_ratio,
                },
            ) from exc
        # resolved.size 此时必为 "{W}x{H}"（不会是 "auto"）
        image_request_options = _image_request_options(
            gen.upstream_request,
            size=resolved.size,
        )

        async with SessionLocal() as session:
            references = await _load_reference_images(session, input_image_ids)
            # mask_image_id 仅 EDIT + 局部 inpaint 任务设置；GENERATE 任务忽略。
            # 与 reference 同 session 加载（少跑一次连接），mask 拿到后立刻按第一张
            # 参考图尺寸 normalize（OpenAI /v1/images/edits + mask 要求等尺寸）。
            mask_bytes_raw: bytes | None = None
            if mask_image_id and action == GenerationAction.EDIT:
                mask_bytes_raw = await _load_mask_image(session, mask_image_id)

        ref_for_body = references if action == GenerationAction.EDIT else []
        # mask normalize：与第一张参考图尺寸对齐，模式统一为 RGBA。reference 解码
        # 在 _normalize_reference_image 还会再过一遍（统一 WebP 编码），所以这里只
        # 用第一张原字节量像素尺寸即可，不重复重编码。
        mask_bytes: bytes | None = None
        # inpaint 输出 size 强制对齐参考图像素尺寸——否则 1024x768 输入要被升采样
        # 到 4K 输出时，gpt-image-2 实测会把 mask 外区域重画 / mask 错位，"局部修改"
        # 退化成"整张重生成"。失败（参考图比例太极端 / 解码失败）保持 None，调用方
        # 走原 resolved.size 兜底。
        inpaint_size_override: str | None = None
        if mask_bytes_raw is not None and ref_for_body:
            mask_bytes = _resize_mask_to_reference(mask_bytes_raw, ref_for_body[0][1])
            ref_size = _reference_pixel_size(ref_for_body[0][1])
            if ref_size is not None:
                inpaint_size_override = _inpaint_size_from_reference(*ref_size)

        # 即将调用上游（同步 HTTP，20-60s），先推一条 rendering progress 让前端切指示。
        # substage=stream_started 让 DevelopingCard 显影扫光从"占位"切到"真在工作"。
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "stage": GenerationStage.RENDERING.value,
                "substage": GenerationStage.STREAM_STARTED.value,
            },
        )

        b64_result: str | None = None
        revised_prompt: str | None = None
        # dual_race async generator：winner 先 yield 一次，loser 也成功时再 yield bonus。
        # 主流程取首张作 winner；处理完成后再尝试取第二张做 bonus image，复用同 iter。
        provider_used_events: list[dict[str, str]] = []
        # image-job sidecar 把"公网图片地址"通过 image_job_image 事件回传；这里只暂存，
        # 成功提交时再合并到 generation.upstream_request，让 admin 请求事件面板能展示
        # 该 job 对应的 sidecar 临时图 URL（不只 inlined image，方便排查）。
        image_job_meta: dict[str, Any] = {}

        def pop_provider_used_event() -> dict[str, str]:
            if provider_used_events:
                return provider_used_events.pop(0)
            return {}

        async def publish_image_progress(event: dict[str, Any]) -> None:
            nonlocal has_partial
            # GEN-P1-4: 进度回调里检查 cancel——partial / fallback_started 等节点自然
            # 节流（不会每 token），命中后 raise 让 race 任务被 cancel + 终态走 _TaskCancelled。
            if lease_lost.is_set():
                raise _LeaseLost("generation lease renewer failed")
            if await _is_cancelled(redis, task_id):
                raise _TaskCancelled("cancelled during upstream call")
            event_type = event.get("type")
            if event_type == "image_job_image":
                url = _redis_text(event.get("image_job_url"))
                if url:
                    image_job_meta["image_job_url"] = url
                for key in ("job_id", "endpoint_used", "expires_at", "format"):
                    value = event.get(key)
                    if value is not None:
                        image_job_meta[
                            f"image_job_{key}"
                            if not key.startswith("image_job_")
                            else key
                        ] = value
                return
            if event_type == "endpoint_failover":
                # Inner-loop endpoint switch (generations ↔ responses) on the
                # same provider — keep semantics close to provider_failover so
                # the front-end can render a similar pill.
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    {
                        "generation_id": task_id,
                        "message_id": message_id,
                        "stage": GenerationStage.RENDERING.value,
                        "substage": GenerationStage.PROVIDER_SELECTED.value,
                        "endpoint_failover": True,
                        "provider": event.get("provider"),
                        "from_endpoint": event.get("from_endpoint"),
                        "remaining": event.get("remaining"),
                        "reason": event.get("reason"),
                        "route": event.get("route") or "image_jobs",
                    },
                )
                return
            if event_type == "provider_used":
                provider = _redis_text(
                    event.get("provider") or event.get("actual_provider")
                )
                if provider:
                    metadata: dict[str, str] = {"provider": provider}
                    for source_key, target_key in (
                        ("route", "route"),
                        ("source", "source"),
                        ("endpoint", "endpoint"),
                    ):
                        value = _redis_text(event.get(source_key))
                        if value:
                            metadata[target_key] = value
                    provider_used_events.append(metadata)
                    # 同步把当前 lane 的 provider 落到 in-flight 快照里。
                    inflight_update: dict[str, str] = {}
                    route_text = metadata.get("route") or ""
                    endpoint_text = metadata.get("endpoint") or ""
                    if is_dual_race:
                        lane_field = _classify_inflight_lane(route_text, endpoint_text)
                        inflight_update[f"{lane_field}_provider"] = provider
                        if route_text:
                            inflight_update[f"{lane_field}_route"] = route_text
                        if endpoint_text:
                            inflight_update[f"{lane_field}_endpoint"] = endpoint_text
                    else:
                        inflight_update["provider"] = provider
                        if route_text:
                            inflight_update["actual_route"] = route_text
                        if endpoint_text:
                            inflight_update["endpoint"] = endpoint_text
                    await _inflight_set_fields(redis, task_id, inflight_update)
                return
            if event_type == "partial_image":
                has_partial = True
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PARTIAL_IMAGE,
                    {
                        "generation_id": task_id,
                        "message_id": message_id,
                        "stage": GenerationStage.RENDERING.value,
                        "substage": GenerationStage.PARTIAL_RECEIVED.value,
                        "index": event.get("index"),
                        "count": event.get("count"),
                    },
                )
                return
            if event_type in {"fallback_started", "final_image", "completed"}:
                stage = (
                    GenerationStage.FINALIZING.value
                    if event_type in {"final_image", "completed"}
                    else GenerationStage.RENDERING.value
                )
                substage = (
                    GenerationStage.FINAL_RECEIVED.value
                    if event_type in {"final_image", "completed"}
                    else GenerationStage.STREAM_STARTED.value
                )
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    {
                        "generation_id": task_id,
                        "message_id": message_id,
                        "stage": stage,
                        "substage": substage,
                        "source": event.get("source") or "responses_fallback",
                    },
                )
                return
            # P2 worker 内 failover：上游 retriable 错误时立即换 provider 再试。
            # 推 substage=provider_selected + provider_failover=true，让前端把 DevelopingCard
            # 切到"换号重试"指示，区别于首次进入 stream_started。不发额外 SSE 事件，
            # 复用 generation.progress；旧前端不识别 provider_failover 字段也无影响。
            if event_type == "provider_failover":
                # 把"刚刚失败"的 provider 记进快照，标 status=failover；下一条 provider_used
                # 会覆盖回 active。这样 admin 列表能看到"X 切走了，正在选下一个"。
                from_provider = _redis_text(event.get("from_provider"))
                route_text = _redis_text(event.get("route")) or ""
                inflight_update: dict[str, str] = {}
                if is_dual_race:
                    lane_field = _classify_inflight_lane(route_text, "")
                    inflight_update[f"{lane_field}_status"] = "failover"
                    if from_provider:
                        inflight_update[f"{lane_field}_last_failed"] = from_provider
                else:
                    inflight_update["status"] = "failover"
                    if from_provider:
                        inflight_update["last_failed"] = from_provider
                await _inflight_set_fields(redis, task_id, inflight_update)
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    {
                        "generation_id": task_id,
                        "message_id": message_id,
                        "stage": GenerationStage.RENDERING.value,
                        "substage": GenerationStage.PROVIDER_SELECTED.value,
                        "provider_failover": True,
                        "from_provider": event.get("from_provider"),
                        "remaining": event.get("remaining"),
                        "reason": event.get("reason"),
                        "route": event.get("route") or "responses",
                    },
                )

        async with asyncio.timeout_at(_task_deadline):
            # GEN-P1-4: 拿到图片队列槽但还没发上游请求时再确认一次取消。
            if lease_lost.is_set():
                raise _LeaseLost("generation lease renewer failed")
            if await _is_cancelled(redis, task_id):
                raise _TaskCancelled("cancelled before upstream request")
            with _tracer.start_as_current_span("upstream.generate_image") as _span:
                try:
                    _span.set_attribute("lumen.task_id", task_id)
                    _span.set_attribute("lumen.action", action)
                    # inpaint 路径用对齐参考图的 size override；observability 看到的 size
                    # 才和实际下发到 /v1/images/edits 的一致（不然 admin 排查时会困惑）。
                    _span.set_attribute(
                        "lumen.size", inpaint_size_override or resolved.size
                    )
                    if inpaint_size_override:
                        _span.set_attribute("lumen.size_requested", resolved.size)
                    if reserved_provider_name:
                        _span.set_attribute("lumen.provider", reserved_provider_name)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    responses_model = str(image_request_options["responses_model"])
                    # Retry 打散：把当前 task attempt 写入 ContextVar，下游 body 构造点会读到。
                    # 必须用 `attempt`（line 2084 _bounded_next_attempt 算出的新值）而非 `gen.attempt`
                    # （load 时刻的旧值）——后者会让 ContextVar 错位 1 格：数据库 attempt=2 时 push
                    # 旧值 1，cache buster 不触发；数据库 attempt=3 时 push 旧值 2，effort 走 minimal
                    # 而非 high。实测 lane A 反复 server_error 时 image-job 那边 payload 显示
                    # effort=minimal+cache_key=lumen-retry-* 但实际是 attempt=3 的请求 → 证实错位。
                    # attempt == 1 首次（不打散）；>= 2 每次都用不同 prompt_cache_key /
                    # reasoning.effort，绕开 ChatGPT codex 端的"故障 cache"和 sub2api sticky session。
                    retry_attempt_token = push_image_retry_attempt(attempt)
                    try:
                        if action == GenerationAction.EDIT:
                            if not ref_for_body:
                                raise UpstreamError(
                                    "edit action requires at least one reference image",
                                    error_code=EC.INVALID_REQUEST_ERROR.value,
                                    status_code=400,
                                )
                            image_iter = edit_image(
                                prompt=prompt,
                                # mask 不为 None 时优先用对齐到参考图尺寸的 inpaint
                                # override；否则走 user resolved.size（普通 i2i 行为）。
                                size=inpaint_size_override or resolved.size,
                                images=[raw for _sha, raw in ref_for_body],
                                mask=mask_bytes,
                                quality=str(image_request_options["render_quality"]),
                                output_format=str(
                                    image_request_options["output_format"]
                                ),
                                output_compression=image_request_options.get(
                                    "output_compression"
                                ),
                                background=str(image_request_options["background"]),
                                moderation=str(image_request_options["moderation"]),
                                model=responses_model,
                                progress_callback=publish_image_progress,
                                provider_override=(
                                    None if is_dual_race else reserved_provider
                                ),
                                user_id=user_id,
                            )
                        else:
                            image_iter = generate_image(
                                prompt=prompt,
                                size=resolved.size,
                                quality=str(image_request_options["render_quality"]),
                                output_format=str(
                                    image_request_options["output_format"]
                                ),
                                output_compression=image_request_options.get(
                                    "output_compression"
                                ),
                                background=str(image_request_options["background"]),
                                moderation=str(image_request_options["moderation"]),
                                model=responses_model,
                                progress_callback=publish_image_progress,
                                provider_override=(
                                    None if is_dual_race else reserved_provider
                                ),
                                user_id=user_id,
                            )
                        first_pair = await _anext_image_with_guards(
                            image_iter,
                            lease_lost,
                            redis=redis,
                            task_id=task_id,
                        )
                    finally:
                        pop_image_retry_attempt(retry_attempt_token)
                    if first_pair is None:
                        raise UpstreamError(
                            "upstream image generator yielded no result",
                            error_code=EC.NO_IMAGE_RETURNED.value,
                            status_code=200,
                        )
                    b64_result, revised_prompt = first_pair
                    winner_provider_event = pop_provider_used_event()
                    actual_upstream_provider = winner_provider_event.get("provider")
                    actual_upstream_route = winner_provider_event.get("route")
                    actual_upstream_source = winner_provider_event.get("source")
                    actual_upstream_endpoint = winner_provider_event.get("endpoint")
                    upstream_calls_total.labels(kind="generation", outcome="ok").inc()
                except Exception:
                    upstream_calls_total.labels(
                        kind="generation", outcome="error"
                    ).inc()
                    raise

        if not b64_result:
            # 降级到文本了——按 retriable 处理
            raise UpstreamError(
                "upstream returned no image (tool_choice downgrade?)",
                error_code=EC.NO_IMAGE_RETURNED.value,
                status_code=200,
            )

        # 上游已返回图像 base64，进入本地解码/缩略图/落盘阶段
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "stage": GenerationStage.FINALIZING.value,
                "substage": GenerationStage.FINAL_RECEIVED.value,
            },
        )

        # 进入本地处理阶段（解码 / blurhash / 3 个 variant）。
        # 用细 substage=processing 让前端 DevelopingCard 切到"处理中"动画；
        # 粗 stage 仍是 finalizing，保持现有前端兼容。
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "stage": GenerationStage.FINALIZING.value,
                "substage": GenerationStage.PROCESSING.value,
            },
        )

        # --- 解码 + 校验 ---
        try:
            raw_image = _decode_upstream_image_b64(b64_result)
        except binascii.Error as exc:
            raise UpstreamError(
                f"bad base64 from upstream: {exc}",
                error_code=EC.BAD_RESPONSE.value,
                status_code=200,
            ) from exc

        sha = _sha256(raw_image)

        # §7.5 SHA-256 回退检测
        if action == GenerationAction.EDIT:
            if any(sha == ref_sha for ref_sha, _ in references):
                raise UpstreamError(
                    "upstream returned original image unchanged (sha echo)",
                    error_code=EC.SHA_ECHO.value,
                    status_code=200,
                )

        transparent_requested = image_request_options.get("background") == "transparent"
        transparent_alpha_recovered = False
        transparent_qc_payload: dict[str, Any] | None = None
        transparent_provider: str | None = None

        try:
            orig_format = "PNG"
            with PILImage.open(io.BytesIO(raw_image)) as pil:
                pil.load()
                if pil.format not in ("PNG", "WEBP", "JPEG"):
                    raise UpstreamError(
                        f"upstream returned unexpected image format: {pil.format}",
                        error_code=EC.BAD_RESPONSE.value,
                        status_code=200,
                    )
                orig_format = pil.format
                width, height = pil.size
                if width < 1 or height < 1 or width > 10000 or height > 10000:
                    raise UpstreamError(
                        f"upstream image dimensions out of range: {width}x{height}",
                        error_code=EC.BAD_RESPONSE.value,
                        status_code=200,
                    )
                processed: PILImage.Image | None = None
                if transparent_requested and not _image_has_transparency(pil):
                    try:
                        pipeline_out = await process_transparent_request(
                            pil, prompt=prompt
                        )
                    except TransparentPipelineFailure as exc:
                        qc_dict = exc.qc.to_dict() if exc.qc is not None else None
                        raise UpstreamError(
                            f"transparent material pipeline failed: {exc}",
                            error_code=EC.BAD_RESPONSE.value,
                            status_code=200,
                            payload={
                                "transparent_qc": qc_dict,
                                "transparent_provider": exc.provider,
                            },
                        ) from exc
                    raw_image = pipeline_out.rgba_png
                    sha = _sha256(raw_image)
                    orig_format = "PNG"
                    width, height = pipeline_out.width, pipeline_out.height
                    transparent_alpha_recovered = True
                    transparent_qc_payload = pipeline_out.qc.to_dict()
                    transparent_provider = pipeline_out.provider
                    processed = PILImage.open(io.BytesIO(raw_image))
                    processed.load()
                try:
                    output_pil = processed or pil
                    blurhash_str = _compute_blurhash(output_pil)
                    display_bytes, display_size = _make_display(output_pil)
                    preview_bytes, preview_size = _make_preview(output_pil)
                    thumb_bytes, thumb_size = _make_thumb(output_pil)
                finally:
                    if processed is not None:
                        processed.close()
        except UpstreamError:
            raise
        except Exception as exc:
            raise UpstreamError(
                f"pillow could not decode image: {exc}",
                error_code=EC.BAD_RESPONSE.value,
                status_code=200,
            ) from exc

        # --- 写存储 ---
        image_id = new_uuid7()
        orig_ext_by_format = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}
        orig_mime_by_format = {
            "PNG": "image/png",
            "WEBP": "image/webp",
            "JPEG": "image/jpeg",
        }
        orig_ext = orig_ext_by_format[orig_format]
        orig_mime = orig_mime_by_format[orig_format]
        model_metadata = _model_image_metadata_from_request(
            image_id=image_id,
            mime=orig_mime,
            request=gen_upstream_request_snapshot,
            prompt=prompt,
        )
        if model_metadata:
            try:
                with PILImage.open(io.BytesIO(raw_image)) as im:
                    im.load()
                    raw_image = _maybe_embed_model_image_metadata_bytes(
                        image=im,
                        fmt=orig_format,
                        raw_image=raw_image,
                        metadata=model_metadata,
                    )
                sha = _sha256(raw_image)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "model_library image metadata embed skipped task=%s err=%s",
                    task_id,
                    exc,
                )
        key_orig = f"u/{user_id}/g/{task_id}/orig.{orig_ext}"
        key_display = f"u/{user_id}/g/{task_id}/display2048.webp"
        key_preview = f"u/{user_id}/g/{task_id}/preview1024.webp"
        key_thumb = f"u/{user_id}/g/{task_id}/thumb256.jpg"

        # 进入"写盘"细子阶段。线上 IO 通常 100-300ms 即结束，前端可借此让显影扫光收尾。
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "stage": GenerationStage.FINALIZING.value,
                "substage": GenerationStage.STORING.value,
            },
        )

        created_storage_keys = await _write_generation_files(
            [
                (key_orig, raw_image),
                (key_display, display_bytes),
                (key_preview, preview_bytes),
                (key_thumb, thumb_bytes),
            ]
        )

        # --- 写 DB ---
        conversation_id_for_title: str | None = None
        parent_upstream_request_for_bonus: dict[str, Any] | None = None
        async with _cleanup_storage_on_error(created_storage_keys):
            async with SessionLocal() as session:
                await _ensure_generation_attempt_current(session, task_id, attempt)
                conversation_id_for_title = await _ensure_generation_conversation_alive(
                    session,
                    message_id=message_id,
                    user_id=user_id,
                    lock=True,
                )
                img = Image(
                    id=image_id,
                    user_id=user_id,
                    owner_generation_id=task_id,
                    source=ImageSource.GENERATED.value,
                    parent_image_id=(
                        primary_input_image_id
                        if action == GenerationAction.EDIT
                        else None
                    ),
                    storage_key=key_orig,
                    mime=orig_mime,
                    width=width,
                    height=height,
                    size_bytes=len(raw_image),
                    sha256=sha,
                    blurhash=blurhash_str,
                    visibility="private",
                    metadata_jsonb=model_metadata,
                )
                session.add(img)
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="display2048",
                        storage_key=key_display,
                        width=display_size[0],
                        height=display_size[1],
                    )
                )
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="preview1024",
                        storage_key=key_preview,
                        width=preview_size[0],
                        height=preview_size[1],
                    )
                )
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="thumb256",
                        storage_key=key_thumb,
                        width=thumb_size[0],
                        height=thumb_size[1],
                    )
                )

                # UPDATE generation 成功态
                upstream_req: dict[str, Any] = (
                    dict(gen.upstream_request)
                    if isinstance(gen.upstream_request, dict)
                    else {}
                )
                upstream_req.update(image_request_options)
                upstream_req["size_actual"] = f"{width}x{height}"
                upstream_req["mime"] = orig_mime
                upstream_req["upstream_route"] = image_route
                if actual_upstream_provider:
                    upstream_req["provider"] = actual_upstream_provider
                    upstream_req["actual_provider"] = actual_upstream_provider
                elif upstream_provider_label and not is_dual_race:
                    upstream_req["provider"] = upstream_provider_label
                else:
                    upstream_req.pop("provider", None)
                    upstream_req.pop("actual_provider", None)
                if actual_upstream_route:
                    upstream_req["actual_route"] = actual_upstream_route
                if actual_upstream_source:
                    upstream_req["actual_source"] = actual_upstream_source
                if actual_upstream_endpoint:
                    upstream_req["actual_endpoint"] = actual_upstream_endpoint
                if transparent_alpha_recovered:
                    upstream_req["transparent_alpha_recovered"] = True
                if transparent_qc_payload is not None:
                    upstream_req["transparent_qc"] = transparent_qc_payload
                if transparent_provider is not None:
                    upstream_req["transparent_pipeline_provider"] = transparent_provider
                if revised_prompt:
                    upstream_req["revised_prompt"] = revised_prompt
                if image_job_meta:
                    for key, value in image_job_meta.items():
                        upstream_req[key] = value
                parent_upstream_request_for_bonus = dict(upstream_req)

                result = await session.execute(
                    _generation_attempt_update(task_id, attempt).values(
                        status=GenerationStatus.SUCCEEDED.value,
                        progress_stage=GenerationStage.FINALIZING,
                        finished_at=datetime.now(timezone.utc),
                        upstream_pixels=width * height,
                        upstream_request=upstream_req,
                        error_code=None,
                        error_message=None,
                    )
                )
                _ensure_generation_updated(result, task_id, attempt)

                # UPDATE message.content 把生成图挂进去（§6.6 step 7）
                msg: Message | None = await session.get(Message, message_id)
                if msg is not None:
                    content = dict(msg.content or {})
                    images_list = list(content.get("images") or [])
                    images_list.append(
                        {
                            "image_id": image_id,
                            "from_generation_id": task_id,
                            "width": width,
                            "height": height,
                            "mime": orig_mime,
                            "url": storage.public_url(key_orig),
                            "display_url": f"/api/images/{image_id}/variants/display2048",
                            "preview_url": f"/api/images/{image_id}/variants/preview1024",
                            "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                            "filename": model_metadata.get("suggested_filename"),
                        }
                    )
                    content["images"] = images_list
                    msg.content = content
                    msg.status = MessageStatus.SUCCEEDED

                await _maybe_enqueue_workflow_quality_review(
                    session=session,
                    redis=redis,
                    user_id=user_id,
                    conversation_id=conversation_id_for_title,
                    generation=gen,
                    image_id=image_id,
                )

                try:
                    await _maybe_record_model_library_generate_image(
                        session=session,
                        user_id=user_id,
                        generation=gen,
                        image_id=image_id,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 模特库 hook 任何异常都不能让主生成任务从 succeeded 翻成 failed
                    logger.warning(
                        "model_library_generate post-success hook failed task=%s err=%s",
                        task_id,
                        exc,
                    )

                try:
                    await _maybe_record_poster_workflow_image(
                        session=session,
                        user_id=user_id,
                        generation=gen,
                        image_id=image_id,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    # poster hook 任何异常都不能把 succeeded 翻成 failed
                    logger.warning(
                        "poster_workflow post-success hook failed task=%s err=%s",
                        task_id,
                        exc,
                    )

                try:
                    await _maybe_record_poster_style_library_generate_image(
                        session=session,
                        user_id=user_id,
                        generation=gen,
                        image_id=image_id,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 风格库 hook 任何异常都不能把 succeeded 翻成 failed
                    logger.warning(
                        "poster_style_library_generate post-success hook failed task=%s err=%s",
                        task_id,
                        exc,
                    )

                await worker_billing.settle_generation(
                    session,
                    gen,
                    width=width,
                    height=height,
                )
                await session.commit()

        # --- publish succeeded ---
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_SUCCEEDED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "images": [
                    {
                        "image_id": image_id,
                        "from_generation_id": task_id,
                        "actual_size": f"{width}x{height}",
                        "mime": orig_mime,
                        "url": storage.public_url(key_orig),
                        "display_url": f"/api/images/{image_id}/variants/display2048",
                        "preview_url": f"/api/images/{image_id}/variants/preview1024",
                        "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                        "filename": model_metadata.get("suggested_filename"),
                    }
                ],
                "final_size": f"{width}x{height}",
            },
        )
        _task_outcome = "succeeded"

        # 自动起会话标题（第一轮生成完成后触发；内部幂等）
        if conversation_id_for_title:
            from .auto_title import maybe_enqueue_auto_title

            await maybe_enqueue_auto_title(redis, conversation_id_for_title)

        # dual_race bonus：winner 已成功，尝试从同一 image_iter 取第二份；
        # loser 也成功 → 建独立 generation row 显示第二张；loser 失败/超时 → 静默吞掉。
        # 整段用 try/except 兜底——winner 已成功状态不可逆，bonus 任何错误（包括用户
        # 取消、lease 丢失、上游异常）都只 log warn，不让外层 except 把成功改成失败。
        # 取消信号：bonus 阶段尊重用户意图——直接关 iter 退出，不再创建新 generation row。
        if image_iter is not None:
            bonus_pair: tuple[str, str | None] | None = None
            try:
                bonus_pair = await _anext_image_with_guards(
                    image_iter,
                    lease_lost,
                    redis=redis,
                    task_id=task_id,
                )
            except (_LeaseLost, _TaskCancelled, asyncio.CancelledError):
                logger.info(
                    "dual_race bonus iter aborted by cancel/lease task=%s",
                    task_id,
                )
                await _consume_image_iter_close_result(image_iter, task_id=task_id)
                image_iter = None
                bonus_pair = None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dual_race bonus iter failed task=%s err=%r", task_id, exc
                )
            if bonus_pair is not None:
                bonus_b64, bonus_revised = bonus_pair
                bonus_provider_event = pop_provider_used_event()
                try:
                    await _handle_dual_race_bonus_image(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        parent_task_id=task_id,
                        parent_idempotency_key=gen_idempotency_key,
                        parent_upstream_request=(
                            parent_upstream_request_for_bonus
                            or gen_upstream_request_snapshot
                        ),
                        message_id=message_id,
                        action=str(action),
                        model=gen_model,
                        prompt=prompt,
                        size_requested=size_requested,
                        aspect_ratio=aspect_ratio,
                        input_image_ids=input_image_ids,
                        primary_input_image_id=primary_input_image_id,
                        references=references,
                        image_request_options=image_request_options,
                        b64_result=bonus_b64,
                        revised_prompt=bonus_revised,
                        upstream_provider=bonus_provider_event.get("provider"),
                        upstream_actual_route=bonus_provider_event.get("route"),
                        upstream_actual_source=bonus_provider_event.get("source"),
                        upstream_actual_endpoint=bonus_provider_event.get("endpoint"),
                    )
                except (_LeaseLost, _TaskCancelled, asyncio.CancelledError):
                    logger.info(
                        "dual_race bonus finalize aborted by cancel/lease task=%s",
                        task_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dual_race bonus finalize unexpected error task=%s err=%r",
                        task_id,
                        exc,
                    )

    except _LeaseLost as exc:
        logger.warning(
            "generation lease lost task=%s attempt=%s err=%s", task_id, attempt, exc
        )
        if attempt >= _MAX_ATTEMPTS:
            await _mark_generation_attempt_failed(
                redis,
                task_id=task_id,
                message_id=message_id,
                user_id=user_id,
                attempt=attempt,
                error_code="lease_lost_max_attempts",
                error_message="lease lost after max attempts",
                retriable=False,
            )
            _task_outcome = "failed"
            return
        delay = _retry_delay_seconds(attempt)
        requeued = await _mark_generation_attempt_retrying(
            redis,
            task_id=task_id,
            message_id=message_id,
            user_id=user_id,
            attempt=attempt,
            error_code="lease_lost",
            error_message="generation lease lost; task will be retried",
            delay=delay,
            reason="lease_lost",
            max_attempts=_MAX_ATTEMPTS,
        )
        _task_outcome = "retry" if requeued else "lease_lost"
        return

    except _StaleGenerationAttempt as exc:
        logger.info(
            "generation stale attempt task=%s attempt=%s err=%s", task_id, attempt, exc
        )
        _task_outcome = "stale_attempt"
        return

    except _TaskCancelled as exc:
        # GEN-P1-4: 用户取消——标 cancelled 终态、publish failed(retriable=false)。
        logger.info("generation cancelled by user task=%s reason=%s", task_id, exc)
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    _generation_attempt_update(task_id, attempt).values(
                        status=GenerationStatus.CANCELED.value,
                        progress_stage=GenerationStage.FINALIZING,
                        finished_at=datetime.now(timezone.utc),
                        error_code=EC.CANCELLED.value,
                        error_message="cancelled by user",
                    )
                )
                _ensure_generation_updated(result, task_id, attempt)
                msg_c = await session.get(Message, message_id)
                if msg_c is not None and msg_c.status not in (
                    MessageStatus.SUCCEEDED,
                    MessageStatus.FAILED,
                ):
                    msg_c.status = MessageStatus.FAILED
                gen_c = await session.get(Generation, task_id)
                if gen_c is not None:
                    await worker_billing.release_generation(
                        session,
                        gen_c,
                        reason="cancelled",
                    )
                await session.commit()
        except _StaleGenerationAttempt as stale_exc:
            logger.info(
                "generation cancel stale attempt task=%s attempt=%s err=%s",
                task_id,
                attempt,
                stale_exc,
            )
            _task_outcome = "stale_attempt"
            return
        except Exception as db_exc:  # noqa: BLE001
            logger.warning(
                "generation cancel DB update failed task=%s err=%s",
                task_id,
                db_exc,
            )
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_FAILED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "code": "cancelled",
                "message": "cancelled by user",
                "retriable": False,
            },
        )
        _task_outcome = "failed"
        return

    except Exception as exc:  # noqa: BLE001
        decision = _classify_exception(exc, has_partial)
        _byok_terminal, byok_error = classify_user_credential_error(exc)
        if user_api_credential_id and byok_error:
            await record_user_credential_runtime_error(user_api_credential_id, exc)
            decision = RetryDecision(False, f"byok {byok_error}")
        _err_code_log = getattr(exc, "error_code", None) or type(exc).__name__
        _http_status_log = getattr(exc, "status_code", None)
        _provider_log = (getattr(exc, "payload", None) or {}).get("provider", "")
        # Why: warning 级别只放白名单字段，避免 prompt / api_key 等敏感串入日志
        logger.warning(
            "generation failed task=%s attempt=%s retriable=%s reason=%s "
            "error_code=%s http_status=%s provider=%s",
            task_id,
            attempt,
            decision.retriable,
            decision.reason,
            _err_code_log,
            _http_status_log,
            _provider_log,
        )
        logger.debug("generation exc trace task=%s", task_id, exc_info=True)

        err_code = (
            byok_error_to_generation_code(byok_error)
            if user_api_credential_id and byok_error
            else "timeout"
            if isinstance(exc, TimeoutError)
            else getattr(exc, "error_code", None) or type(exc).__name__
        )
        err_msg = (
            byok_error_message(byok_error)
            if user_api_credential_id and byok_error
            else str(exc)[:2000]
        )
        error_details = _safe_generation_error_details(exc)

        moderation_upgrade = False
        if (
            not decision.retriable
            and not _is_dual_race_sentinel(reserved_provider_name)
            and reserved_provider_name
            and is_moderation_block(getattr(exc, "error_code", None), err_msg)
        ):
            try:
                from ..provider_pool import get_pool as _get_pool

                _pool = await _get_pool()
                _enabled_count = len(_pool.enabled_provider_names())
            except Exception:  # noqa: BLE001
                _enabled_count = 0
            _avoided_now: set[str] = (
                await _get_avoided_providers(redis, task_id)
                if _enabled_count > 1
                else set()
            )
            upgraded = _decide_moderation_retry_upgrade(
                base_decision=decision,
                err_code=getattr(exc, "error_code", None),
                err_msg=err_msg,
                is_dual_race=is_dual_race,
                reserved_provider_name=reserved_provider_name,
                enabled_provider_count=_enabled_count,
                already_avoided_count=len(_avoided_now),
            )
            if upgraded is not None:
                logger.info(
                    "moderation retry upgrade task=%s attempt=%s from_provider=%s "
                    "enabled=%d avoided=%d cap=%d",
                    task_id,
                    attempt,
                    reserved_provider_name,
                    _enabled_count,
                    len(_avoided_now),
                    _MODERATION_RETRY_CAP,
                )
                decision = upgraded
                moderation_upgrade = True

        effective_max_attempts = (
            _MODERATION_RETRY_CAP if moderation_upgrade else _MAX_ATTEMPTS
        )
        _task_outcome = (
            "retry"
            if (decision.retriable and attempt < effective_max_attempts)
            else "failed"
        )

        if decision.retriable and attempt < effective_max_attempts:
            # 把刚刚失败的 provider 加入 avoid set，下次 reserve 跳过它一次。
            # 解决 858 那种"task 锁单 provider，遇到 model_not_found 反复打"的死循环。
            # dual_race 模式下 reserved_provider 是 sentinel，没有真正绑定的 provider，跳过。
            if not _is_dual_race_sentinel(reserved_provider_name):
                await _avoid_provider_for_task(redis, task_id, reserved_provider_name)
            # backoff + 重新 enqueue；arq 自己的 retry 机制也可，我们手动更可控
            delay = _retry_delay_seconds(attempt)

            try:
                async with SessionLocal() as session:
                    result = await session.execute(
                        _generation_attempt_update(task_id, attempt).values(
                            status=GenerationStatus.QUEUED.value,
                            progress_stage=GenerationStage.QUEUED,
                            error_code=err_code,
                            error_message=err_msg,
                        )
                    )
                    _ensure_generation_updated(result, task_id, attempt)
                    await session.commit()
            except _StaleGenerationAttempt as stale_exc:
                logger.info(
                    "generation retry stale attempt task=%s attempt=%s err=%s",
                    task_id,
                    attempt,
                    stale_exc,
                )
                _task_outcome = "stale_attempt"
                return

            await _cancel_renewer_task(renewer)
            renewer = None
            await _release_lease(redis, task_id, worker_id)

            # 用 arq redis 延迟入队（_defer_by 秒）
            try:
                await redis.set(
                    _image_queue_not_before_key(task_id),
                    str(time.time() + delay),
                    ex=_retry_not_before_ttl(delay),
                )
                await redis.enqueue_job(
                    "run_generation", task_id, _defer_by=delay, _job_try=attempt + 1
                )
            except Exception as enq_exc:  # noqa: BLE001
                logger.error("re-enqueue failed task=%s err=%s", task_id, enq_exc)
                enqueue_err = "retry_enqueue_failed"
                enqueue_msg = f"failed to enqueue retry: {enq_exc}"
                await _mark_generation_attempt_failed(
                    redis,
                    task_id=task_id,
                    message_id=message_id,
                    user_id=user_id,
                    attempt=attempt,
                    error_code=enqueue_err,
                    error_message=enqueue_msg[:2000],
                    retriable=False,
                )
                _task_outcome = "failed"
                return

            if moderation_upgrade:
                # 通知前端"换号重试中"——复用 provider_failover 通道，前端 DevelopingCard
                # 已经处理 substage=provider_selected + provider_failover=true 的形态，
                # 加 reason=moderation_retry 让 UI 区分于普通 retriable 换号。
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    {
                        "generation_id": task_id,
                        "message_id": message_id,
                        "stage": GenerationStage.RENDERING.value,
                        "substage": GenerationStage.PROVIDER_SELECTED.value,
                        "provider_failover": True,
                        "from_provider": reserved_provider_name,
                        "reason": "moderation_retry",
                        "route": "image",
                    },
                )

            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_RETRYING,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "attempt": attempt,
                    "max_attempts": effective_max_attempts,
                    "retry_delay_seconds": delay,
                    "error_code": err_code,
                    "error_message": err_msg,
                    **({"error_details": error_details} if error_details else {}),
                },
            )
            return

        # terminal
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    _generation_attempt_update(task_id, attempt).values(
                        status=GenerationStatus.FAILED.value,
                        progress_stage=GenerationStage.FINALIZING,
                        finished_at=datetime.now(timezone.utc),
                        error_code=err_code,
                        error_message=err_msg,
                    )
                )
                _ensure_generation_updated(result, task_id, attempt)
                msg = await session.get(Message, message_id)
                if msg is not None:
                    msg.status = MessageStatus.FAILED
                gen_failed = await session.get(Generation, task_id)
                if gen_failed is not None:
                    await worker_billing.release_generation(
                        session,
                        gen_failed,
                        reason=err_code,
                    )
                await session.commit()
        except _StaleGenerationAttempt as stale_exc:
            logger.info(
                "generation terminal stale attempt task=%s attempt=%s err=%s",
                task_id,
                attempt,
                stale_exc,
            )
            _task_outcome = "stale_attempt"
            return

        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_FAILED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "code": err_code,
                "message": err_msg,
                "retriable": False,
                **({"error_details": error_details} if error_details else {}),
            },
        )

    finally:
        # image_iter.aclose() 改在 _critical_release_cleanup 内 await（见下方），
        # 用 shield 跑完，避免外层 cancel 时 generator 关闭被推到下一轮 loop——
        # 4K 高负载 + 失败路径会累积到 fd 不释放。
        if renewer is not None:
            await _cancel_renewer_task(renewer)

        # cancel-safe 关键清理：arq 1800s timeout 触发外层 cancel 时，finally 第一个
        # 普通 await 会立刻重抛 CancelledError，导致后续 release 全被跳过，只能靠
        # zset/lease/inflight 各自的 TTL 60~240s 自然过期兜底（漂浮窗：分布式槽位
        # 长达一分钟看起来还在占用）。把"释放外部 redis 资源 / 防状态泄漏"这些
        # 关键 await 打包进一个协程，用 ensure_future + shield 让外层 cancel 时
        # 仍能跑完；shield 抛 CancelledError 时把 cleanup 挂成 done_callback，
        # 最后再重抛 CancelledError 让 arq 知道任务真的被 cancel。
        # task_duration_seconds 是纯 best-effort 指标，无需 shield。
        async def _critical_release_cleanup() -> None:
            # 先关 image_iter——失败路径下生成器还持有 SSE / curl 子进程 fd，
            # 不 await 关掉它，cancel 后会推到下一轮 loop 才回收。
            await _consume_image_iter_close_result(image_iter, task_id=task_id)
            try:
                await _release_image_queue_slot(
                    redis, task_id=task_id, provider_name=reserved_provider_name
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "generation image queue release failed task=%s provider=%s",
                    task_id,
                    reserved_provider_name,
                    exc_info=True,
                )
            # in-flight 快照只在"任务还在跑"时有意义；终态/retry 之间任何状态都直接清掉，
            # 下次 attempt 进来会在 EV_GEN_STARTED 之后重新写。
            try:
                await _inflight_clear(redis, task_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "generation inflight cleanup failed task=%s",
                    task_id,
                    exc_info=True,
                )
            # task 已到终态时清 avoid set；retry 路径会在下一次 attempt 之前重新写入。
            if _task_outcome != "retry":
                try:
                    await _clear_avoided_providers(redis, task_id)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "generation avoid-set cleanup failed task=%s",
                        task_id,
                        exc_info=True,
                    )
            try:
                await _release_lease(redis, task_id, worker_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "generation lease release failed task=%s",
                    task_id,
                    exc_info=True,
                )

        cleanup_future = asyncio.ensure_future(_critical_release_cleanup())
        cancel_during_cleanup = False
        try:
            await asyncio.shield(cleanup_future)
        except asyncio.CancelledError:
            cancel_during_cleanup = True
            cleanup_future.add_done_callback(
                lambda _t: logger.debug(
                    "generation late critical cleanup finished task=%s", task_id
                )
            )

        try:
            _duration = asyncio.get_event_loop().time() - _task_start
            task_duration_seconds.labels(
                kind="generation", outcome=safe_outcome(_task_outcome)
            ).observe(_duration)
        except Exception:  # noqa: BLE001
            pass

        if cancel_during_cleanup:
            raise asyncio.CancelledError()


__all__ = ["run_generation"]
