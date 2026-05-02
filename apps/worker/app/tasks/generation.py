"""Generation Worker——DESIGN §6.5.b + §7（/v1/images/* 同步路径）。

`run_generation(ctx, task_id)` 是 arq 任务入口。流程：

1. 幂等读 Generation 行；若终态直接 return
2. 进入统一图片 FIFO 队列；只有全局前 2 个任务会被标记 running
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

from lumen_core.constants import (
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
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
    task_channel,
)
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    new_uuid7,
)
from lumen_core.sizing import resolve_size

from ..config import settings
from ..db import SessionLocal
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
# task 失败 retry 时把刚刚 reserved 的 provider 写入 avoid set，下次 reserve
# 跳过它一次，避免 retry 反复打到同一个有问题的 provider（如 model_not_found / 401）。
# 全部 enabled provider 都被 avoid 时退化为不过滤（防止永远 reserve 失败）。
_IMAGE_QUEUE_AVOID_TTL_S = 120
# moderation_blocked / safety_violation 单次 task 跨 attempt 的换号上限。
# retry.py 仍把 moderation 视为 terminal——单 provider 时直接 fail，避免烧配额；
# 多 provider 时 task 层升级为 retriable，配 avoid set 把请求路由到下一个未试 provider。
# 上限取 min(_MODERATION_RETRY_CAP, enabled_provider_count)。
_MODERATION_RETRY_CAP = 6
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


async def _release_lease(redis: Any, task_id: str) -> None:
    try:
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
                    await redis.expire(
                        _image_inflight_key(task_id), _LEASE_TTL_S * 4
                    )
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
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Unified image FIFO queue
# ---------------------------------------------------------------------------


def _redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _image_queue_capacity() -> int:
    raw = getattr(settings, "image_generation_concurrency", 4)
    try:
        return max(1, min(32, int(raw)))
    except (TypeError, ValueError):
        return 4


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
            logger.debug("image queue lock release failed", exc_info=True)


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


async def _provider_active_count(redis: Any, provider_name: str) -> int:
    """Current in-flight task count for one provider after evicting stale
    entries (worker crash mid-flight). Cheap: O(log N) for the cleanup +
    O(1) for ZCARD.
    """
    key = _image_provider_active_key(provider_name)
    try:
        await redis.zremrangebyscore(key, "-inf", time.time())
        count = await redis.zcard(key)
    except Exception:  # noqa: BLE001
        return 0
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


async def _queued_generation_ids(limit: int) -> list[str]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Generation.id)
                .where(Generation.status == GenerationStatus.QUEUED.value)
                .order_by(Generation.created_at.asc(), Generation.id.asc())
                .limit(limit)
            )
        ).scalars().all()
    return [str(row) for row in rows]


async def _ready_queued_generation_ids(redis: Any, limit: int) -> list[str]:
    ids = await _queued_generation_ids(max(limit, _IMAGE_QUEUE_SCAN_LIMIT))
    if not ids:
        return []
    ready: list[str] = []
    now = time.time()
    for queued_id in ids:
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
    try:
        ids = await _ready_queued_generation_ids(
            redis,
            max(_IMAGE_QUEUE_SCAN_LIMIT, _image_queue_capacity() * 2)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("image queue kick scan failed err=%s", exc)
        return
    for queued_id in ids[: max(1, _image_queue_capacity() * 2)]:
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
) -> Any | None:
    """Reserve one global image slot for the oldest queued task.

    Policy:
    - all image sizes share the same FIFO queue;
    - global concurrency cap = ``image_generation_concurrency`` (active task count);
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

    capacity = _image_queue_capacity()
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

        pool = await get_pool()
        # P1-8: 把 task_id 透传给 pool；pool 内部会从 Redis avoid set 跳过
        # 上次失败的 provider，与下方 generation.py 的二次过滤是双保险。
        providers = await pool.select(
            route="image",
            task_id=task_id,
            endpoint_kind=endpoint_kind,
        )
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

        for provider in providers:
            provider_name = _redis_text(getattr(provider, "name", ""))
            if not provider_name:
                continue
            concurrency = max(1, int(getattr(provider, "image_concurrency", 1) or 1))
            provider_zset = _image_provider_active_key(provider_name)
            current = await _provider_active_count(redis, provider_name)
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


def _compute_blurhash(img: PILImage.Image) -> str | None:
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
    return im.mode in {"LA", "RGBA"} or (
        im.mode == "P" and "transparency" in im.info
    )


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


def _make_thumb(orig: PILImage.Image, max_side: int = 256) -> tuple[bytes, tuple[int, int]]:
    with orig.copy() as im:
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        with _rgb_image_for_flat_variant(im) as rgb:
            rgb.save(buf, format="JPEG", quality=78, optimize=True)
        return buf.getvalue(), im.size


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


def _generation_attempt_update(task_id: str, attempt_epoch: int):
    return update(Generation).where(
        Generation.id == task_id,
        Generation.attempt == attempt_epoch,
    )


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
            select(Generation.attempt)
            .where(Generation.id == task_id)
            .with_for_update()
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
                _generation_attempt_update(task_id, attempt)
                .values(
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


async def _await_with_lease_guard(
    awaitable: Awaitable[Any],
    lease_lost: asyncio.Event,
    *,
    redis: Any | None = None,
    task_id: str | None = None,
    cancel_poll_interval_s: float = 1.0,
) -> Any:
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
        return is_retriable(EC.DISK_FULL.value, None, has_partial, error_message=str(exc))
    if isinstance(exc, TimeoutError):
        return is_retriable("timeout", None, has_partial, error_message=str(exc))
    if isinstance(exc, UpstreamError):
        return is_retriable(
            exc.error_code, exc.status_code, has_partial, error_message=str(exc)
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)):
        return is_retriable(
            "upstream_error", None, has_partial, error_message=str(exc)
        )
    if isinstance(exc, httpx.HTTPError):
        return is_retriable(
            "upstream_error", None, has_partial, error_message=str(exc)
        )
    # 其他未预期异常 → 不重试（避免放大故障）
    return RetryDecision(False, f"unhandled {type(exc).__name__}")


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
    if already_avoided_count + 1 >= min(cap, enabled_provider_count):
        return None
    return RetryDecision(
        retriable=True, reason="moderation_blocked try_next_provider"
    )


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
        raw_image = base64.b64decode(b64_result, validate=False)
    except binascii.Error:
        logger.warning(
            "dual_race bonus base64 decode failed parent=%s", parent_task_id
        )
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
                    pil.format, parent_task_id,
                )
                return
            orig_format = pil.format
            width, height = pil.size
            if width < 1 or height < 1 or width > 10000 or height > 10000:
                logger.warning(
                    "dual_race bonus dims out of range %dx%d parent=%s",
                    width, height, parent_task_id,
                )
                return
            processed: PILImage.Image | None = None
            if transparent_requested and not _image_has_transparency(pil):
                try:
                    pipeline_out = await process_transparent_request(
                        pil, prompt=prompt
                    )
                except TransparentPipelineFailure as exc:
                    logger.info(
                        "dual_race bonus transparent pipeline failed parent=%s err=%r",
                        parent_task_id, exc,
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
            parent_task_id, exc,
        )
        return

    # --- 2. 写盘 ---
    bonus_gen_id = new_uuid7()
    image_id = new_uuid7()
    orig_ext_by_format = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}
    orig_mime_by_format = {
        "PNG": "image/png", "WEBP": "image/webp", "JPEG": "image/jpeg",
    }
    orig_ext = orig_ext_by_format[orig_format]
    orig_mime = orig_mime_by_format[orig_format]
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
            parent_task_id, exc,
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
                    bonus_upstream_req["transparent_pipeline_provider"] = transparent_provider
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
                    source=ImageSource.GENERATED,
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
                        }
                    )
                    content["images"] = images_list
                    msg.content = content
                    # bonus 不动 msg.status——winner 已置 SUCCEEDED

                await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dual_race bonus DB write failed parent=%s err=%r",
            parent_task_id, exc,
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
                    }
                ],
                "final_size": f"{width}x{height}",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dual_race bonus publish failed parent=%s err=%r",
            parent_task_id, exc,
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

        user_id = gen.user_id
        message_id = gen.message_id
        action = gen.action
        prompt = gen.prompt
        aspect_ratio = gen.aspect_ratio
        size_requested = gen.size_requested
        input_image_ids = list(gen.input_image_ids or [])
        primary_input_image_id = gen.primary_input_image_id
        # session 关闭后仍要在 dual_race bonus 处理里读这两个字段，提前 detach 取值
        gen_idempotency_key = gen.idempotency_key
        gen_model = gen.model
        gen_upstream_request_snapshot: dict[str, Any] | None = (
            dict(gen.upstream_request) if isinstance(gen.upstream_request, dict) else None
        )
        image_request_options = _image_request_options(
            gen.upstream_request,
            size=size_requested,
        )
        fast_mode = bool(image_request_options.get("fast"))

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
            if msg_existing is not None and msg_existing.status != MessageStatus.SUCCEEDED:
                msg_existing.status = MessageStatus.SUCCEEDED
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
    is_dual_race = image_route == "dual_race"
    endpoint_kind = None if is_dual_race else _image_endpoint_kind_for_engine(image_route)
    try:
        reserved_provider = await _reserve_image_queue_slot(
            redis,
            task_id,
            dual_race=is_dual_race,
            endpoint_kind=endpoint_kind,
        )
    except UpstreamError as exc:
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
            await _release_lease(redis, task_id)
            return
        attempt, attempt_may_run = _bounded_next_attempt(current.attempt)
        if not attempt_may_run:
            _task_outcome = "stale_attempt"
            await _release_image_queue_slot(
                redis, task_id=task_id, provider_name=reserved_provider_name
            )
            await _release_lease(redis, task_id)
            return
        running_upstream_request: dict[str, Any] = (
            dict(current.upstream_request)
            if isinstance(current.upstream_request, dict)
            else {}
        )
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
            )
        )
        try:
            _ensure_generation_updated(result, task_id, current.attempt)
        except _StaleGenerationAttempt:
            await _release_image_queue_slot(
                redis, task_id=task_id, provider_name=reserved_provider_name
            )
            await _release_lease(redis, task_id)
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

    has_partial = False  # 新同步路径不存在 partial，永远为 False（保留变量给下方 classify 用）

    try:
        # 新 API 不支持 size="auto"，强制走 fixed 模式（让 resolve_size 走预设/比例回退）
        size_mode = "fixed"
        fixed_size = size_requested if (size_requested and "x" in size_requested) else None
        try:
            resolved = resolve_size(aspect_ratio, size_mode, fixed_size)
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
        fast_mode = bool(image_request_options.get("fast"))

        async with SessionLocal() as session:
            references = await _load_reference_images(session, input_image_ids)

        ref_for_body = references if action == GenerationAction.EDIT else []

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
        image_iter: AsyncIterator[tuple[str, str | None]] | None = None
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
                        image_job_meta[f"image_job_{key}" if not key.startswith("image_job_") else key] = value
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
                    _span.set_attribute("lumen.size", resolved.size)
                    if reserved_provider_name:
                        _span.set_attribute("lumen.provider", reserved_provider_name)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    fast_model = (
                        DEFAULT_IMAGE_RESPONSES_MODEL_FAST
                        if fast_mode
                        else None
                    )
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
                                size=resolved.size,
                                images=[raw for _sha, raw in ref_for_body],
                                quality=str(image_request_options["render_quality"]),
                                output_format=str(image_request_options["output_format"]),
                                output_compression=image_request_options.get("output_compression"),
                                background=str(image_request_options["background"]),
                                moderation=str(image_request_options["moderation"]),
                                model=fast_model,
                                progress_callback=publish_image_progress,
                                provider_override=(
                                    None if is_dual_race else reserved_provider
                                ),
                            )
                        else:
                            image_iter = generate_image(
                                prompt=prompt,
                                size=resolved.size,
                                quality=str(image_request_options["render_quality"]),
                                output_format=str(image_request_options["output_format"]),
                                output_compression=image_request_options.get("output_compression"),
                                background=str(image_request_options["background"]),
                                moderation=str(image_request_options["moderation"]),
                                model=fast_model,
                                progress_callback=publish_image_progress,
                                provider_override=(
                                    None if is_dual_race else reserved_provider
                                ),
                            )
                        first_pair = await _anext_image_with_guards(
                            image_iter, lease_lost, redis=redis, task_id=task_id,
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
            raw_image = base64.b64decode(b64_result, validate=False)
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
                        pipeline_out = await process_transparent_request(pil, prompt=prompt)
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
                    source=ImageSource.GENERATED,
                    parent_image_id=(
                        primary_input_image_id if action == GenerationAction.EDIT else None
                    ),
                    storage_key=key_orig,
                    mime=orig_mime,
                    width=width,
                    height=height,
                    size_bytes=len(raw_image),
                    sha256=sha,
                    blurhash=blurhash_str,
                    visibility="private",
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
                    _generation_attempt_update(task_id, attempt)
                    .values(
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
                        }
                    )
                    content["images"] = images_list
                    msg.content = content
                    msg.status = MessageStatus.SUCCEEDED

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
                    image_iter, lease_lost, redis=redis, task_id=task_id,
                )
            except (_LeaseLost, _TaskCancelled, asyncio.CancelledError):
                logger.info(
                    "dual_race bonus iter aborted by cancel/lease task=%s",
                    task_id,
                )
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
                        task_id, exc,
                    )
            with suppress(Exception):
                await image_iter.aclose()

    except _LeaseLost as exc:
        logger.warning(
            "generation lease lost task=%s attempt=%s err=%s", task_id, attempt, exc
        )
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_FAILED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "code": "lease_lost",
                "message": "generation lease lost; task will be reconciled",
                "retriable": True,
            },
        )
        _task_outcome = "lease_lost"
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
                    _generation_attempt_update(task_id, attempt)
                    .values(
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
            "timeout" if isinstance(exc, TimeoutError)
            else getattr(exc, "error_code", None) or type(exc).__name__
        )
        err_msg = str(exc)[:2000]

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
        _task_outcome = "retry" if (
            decision.retriable and attempt < effective_max_attempts
        ) else "failed"

        if decision.retriable and attempt < effective_max_attempts:
            # 把刚刚失败的 provider 加入 avoid set，下次 reserve 跳过它一次。
            # 解决 858 那种"task 锁单 provider，遇到 model_not_found 反复打"的死循环。
            # dual_race 模式下 reserved_provider 是 sentinel，没有真正绑定的 provider，跳过。
            if not _is_dual_race_sentinel(reserved_provider_name):
                await _avoid_provider_for_task(
                    redis, task_id, reserved_provider_name
                )
            # backoff + 重新 enqueue；arq 自己的 retry 机制也可，我们手动更可控
            idx = min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)
            delay = RETRY_BACKOFF_SECONDS[idx]

            try:
                async with SessionLocal() as session:
                    result = await session.execute(
                        _generation_attempt_update(task_id, attempt)
                        .values(
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

            renewer.cancel()
            await _release_lease(redis, task_id)

            # 用 arq redis 延迟入队（_defer_by 秒）
            try:
                await redis.set(
                    _image_queue_not_before_key(task_id),
                    str(time.time() + delay),
                    ex=delay + _IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
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
                },
            )
            return

        # terminal
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    _generation_attempt_update(task_id, attempt)
                    .values(
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
            },
        )

    finally:
        if renewer is not None:
            renewer.cancel()
            try:
                await renewer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await _release_image_queue_slot(
            redis, task_id=task_id, provider_name=reserved_provider_name
        )
        # in-flight 快照只在"任务还在跑"时有意义；终态/retry 之间任何状态都直接清掉，
        # 下次 attempt 进来会在 EV_GEN_STARTED 之后重新写。
        await _inflight_clear(redis, task_id)
        # task 已到终态时清 avoid set；retry 路径会在下一次 attempt 之前重新写入。
        if _task_outcome != "retry":
            await _clear_avoided_providers(redis, task_id)
        await _release_lease(redis, task_id)
        try:
            _duration = asyncio.get_event_loop().time() - _task_start
            task_duration_seconds.labels(
                kind="generation", outcome=safe_outcome(_task_outcome)
            ).observe(_duration)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["run_generation"]
