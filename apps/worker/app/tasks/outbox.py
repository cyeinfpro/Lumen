"""Transactional Outbox publisher + stuck task reconciler（DESIGN §6.1 / §6.2）。

`publish_outbox(ctx)`：每 2s 扫 `outbox_events WHERE published_at IS NULL AND
created_at < now() - 2s`——2s 缓冲让 API 的 fast-path（事务后立即 XADD）有机会先处理，
publisher 只是"补漏"。对每条按 kind 调 `arq_redis.enqueue_job` 送进 arq 默认队列，
然后 UPDATE published_at=now()。多 Worker 并行时用 Redis SETNX 锁 `lock:outbox:publisher`
避免重复入队。

`reconcile_tasks(ctx)`：每分钟扫 `(generations|completions) WHERE status IN
('queued','running') AND updated_at < now() - 5min AND lease 过期`——按 attempt 决定
重入队 or 标 timeout。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from arq.cron import cron
from sqlalchemy import or_, select, update

from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    EV_COMP_FAILED,
    EV_GEN_FAILED,
    GenerationStage,
    GenerationStatus,
    MessageStatus,
    user_channel,
)
from lumen_core.models import (
    Completion,
    Generation,
    Message,
    OutboxDeadLetter,
    OutboxEvent,
    new_uuid7,
)

from .. import billing as worker_billing
from ..db import SessionLocal
from ..sse_publish import publish_event

logger = logging.getLogger(__name__)


_OUTBOX_LOCK_KEY = "lock:outbox:publisher"
_OUTBOX_LOCK_TTL_S = 10
_OUTBOX_BATCH = 100
_OUTBOX_DLQ_KEY = "outbox:dead-letter"
_OUTBOX_DLQ_MAXLEN = 1000
# enqueue 连续失败达到此阈值时写一条持久化 DLQ 记录用于告警，但事件仍保持
# unpublished。publisher 本身就是 redrive，Redis 恢复后会继续投递并自动 resolve DLQ。
_OUTBOX_MAX_FAIL_COUNT = 5
# 单次 publisher 调用里，同一事件失败计数的 Redis HASH 键（不持久化到 PG，避免 migration）。
_OUTBOX_FAIL_COUNT_HASH = "outbox:fail_count"
_OUTBOX_FAIL_COUNT_TTL_S = 24 * 3600
_OUTBOX_ENQUEUE_DEDUPE_PREFIX = "outbox:enqueued:"
_OUTBOX_ENQUEUE_DEDUPE_TTL_S = 24 * 3600

_RECON_LOCK_KEY = "lock:outbox:reconciler"
_RECON_LOCK_TTL_S = 50
_RECON_STUCK_AFTER = timedelta(minutes=5)
_RECON_GENERATION_MAX_ATTEMPTS = 5
_RECON_COMPLETION_MAX_ATTEMPTS = 3
_RECON_TIMEOUT_CODE = "timeout"
_RECON_TIMEOUT_MESSAGE = "task stuck; reconciler timed out"
_EV_GEN_REQUEUED = "generation.requeued"
_EV_COMP_REQUEUED = "completion.requeued"
_OUTBOX_TASK_JOBS = {
    "generation": "run_generation",
    "completion": "run_completion",
    "video_generation": "run_video_generation",
    "storyboard_assembly": "run_storyboard_assembly",
}


class _OutboxPayloadError(ValueError):
    pass


_RELEASE_OWNED_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_RENEW_OWNED_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


async def _renew_owned_lock(
    redis: Any,
    *,
    key: str,
    token: str,
    ttl_s: int,
) -> bool | None:
    try:
        renewed = await redis.eval(
            _RENEW_OWNED_LOCK_LUA,
            1,
            key,
            token,
            str(ttl_s),
        )
        return int(renewed or 0) == 1
    except Exception:  # noqa: BLE001
        logger.warning("redis lock renew failed key=%s", key, exc_info=True)
        return None


async def _renew_owned_lock_loop(
    redis: Any,
    *,
    key: str,
    token: str,
    ttl_s: int,
    stop: asyncio.Event,
) -> None:
    interval_s = max(0.1, ttl_s / 3)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass
        renewed = await _renew_owned_lock(
            redis,
            key=key,
            token=token,
            ttl_s=ttl_s,
        )
        if renewed is False:
            logger.warning("redis lock ownership lost key=%s", key)
            return


async def _release_owned_lock(redis: Any, *, key: str, token: str) -> None:
    try:
        await redis.eval(_RELEASE_OWNED_LOCK_LUA, 1, key, token)
    except Exception:  # noqa: BLE001
        logger.warning("redis lock release failed key=%s", key, exc_info=True)


@asynccontextmanager
async def _owned_redis_lock(
    redis: Any,
    *,
    key: str,
    ttl_s: int,
) -> AsyncIterator[bool]:
    token = uuid.uuid4().hex
    acquired = await redis.set(key, token, ex=ttl_s, nx=True)
    if not acquired:
        yield False
        return

    stop = asyncio.Event()
    renewer = asyncio.create_task(
        _renew_owned_lock_loop(
            redis,
            key=key,
            token=token,
            ttl_s=ttl_s,
            stop=stop,
        )
    )
    try:
        yield True
    finally:
        stop.set()
        renewer.cancel()
        try:
            await renewer
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.warning("redis lock renewer failed key=%s", key, exc_info=True)
        finally:
            await _release_owned_lock(redis, key=key, token=token)


# ---------------------------------------------------------------------------
# publish_outbox
# ---------------------------------------------------------------------------


async def publish_outbox(ctx: dict[str, Any]) -> int:
    """扫未发布的 outbox_events 并 enqueue。返回处理条数。

    每批事件用 `SELECT ... FOR UPDATE SKIP LOCKED`：
    (1) 行级锁读候选 → (2) enqueue → (3) UPDATE published_at 同事务 commit。
    enqueue 失败时 published_at 保持 NULL；达到告警阈值会写 DLQ，但 publisher
    继续 redrive，不能把可恢复的 Redis 故障误标成已发布。
    """
    redis = ctx["redis"]

    async with _owned_redis_lock(
        redis,
        key=_OUTBOX_LOCK_KEY,
        ttl_s=_OUTBOX_LOCK_TTL_S,
    ) as acquired:
        if not acquired:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=2)
        processed = await _process_outbox_batch(redis, cutoff, _OUTBOX_BATCH)

    if processed:
        logger.info("outbox: published %d events", processed)
    return processed


async def _deliver_outbox_event(
    redis: Any,
    *,
    event_id: str,
    kind: str,
    payload: dict[str, Any],
) -> tuple[str, str, bool]:
    dedupe_key = f"{_OUTBOX_ENQUEUE_DEDUPE_PREFIX}{event_id}"
    marker = str(payload.get("task_id") or payload.get("user_id") or event_id)
    existing_delivery = await redis.get(dedupe_key)
    if existing_delivery:
        logger.info(
            "outbox delivery deduped event=%s marker=%s kind=%s",
            event_id,
            marker,
            kind,
        )
        return dedupe_key, marker, False

    if kind == "sse":
        user_id = payload.get("user_id")
        channel = payload.get("channel")
        event_name = payload.get("event_name")
        data = payload.get("data")
        if (
            not isinstance(user_id, str)
            or not user_id
            or not isinstance(channel, str)
            or not channel
            or not isinstance(event_name, str)
            or not event_name
            or not isinstance(data, dict)
        ):
            raise _OutboxPayloadError("invalid SSE payload")
        event_data = dict(data)
        event_data.setdefault("outbox_id", event_id)
        event_data.setdefault("event_id", event_id)
        await publish_event(
            redis,
            user_id,
            channel,
            event_name,
            event_data,
        )
        return dedupe_key, user_id, True

    job_name = _OUTBOX_TASK_JOBS.get(kind)
    task_id = payload.get("task_id") or payload.get("id")
    if job_name is None or not task_id:
        raise _OutboxPayloadError("invalid task payload")
    defer_s = payload.get("defer_s")
    enqueue_kwargs: dict[str, Any] = {}
    if isinstance(defer_s, (int, float)) and defer_s > 0:
        enqueue_kwargs["_defer_by"] = float(defer_s)
    enqueue_kwargs["_job_id"] = arq_job_id(
        kind,
        str(task_id),
        str(payload.get("outbox_id") or event_id),
    )
    await redis.enqueue_job(job_name, task_id, **enqueue_kwargs)
    return dedupe_key, str(task_id), True


async def _process_outbox_batch(redis: Any, cutoff: datetime, limit: int) -> int:
    """批事务内 "claim → enqueue → commit published_at"。

    P2-16: Redis 去重 key 只在 PG 事务成功提交后写入。事务内只读取已有
    去重记录；如果本轮 enqueue 成功但 PG rollback，后续重试仍由稳定 arq
    job_id / task 幂等吸收，而不会把一个短 TTL 占位误当成已发布事实。

    可恢复的 enqueue 异常永远不写 published_at。达到失败阈值只持久化一条
    unresolved DLQ 告警；后续成功投递会在同一事务中 resolve 它。
    """
    processed = 0
    # 收集本批内 enqueue 成功的事件，commit 完成后统一写入去重 key。
    dedupe_keys_to_set: list[tuple[str, str]] = []
    dlq_records_to_mirror: list[dict[str, Any]] = []
    fail_counts_to_clear: set[str] = set()
    delivered_event_ids: list[str] = []
    async with SessionLocal() as session:
        try:
            async with session.begin():
                # FOR UPDATE SKIP LOCKED: 并发 publisher 不会抢同一行；阻塞避免；
                # 事务结束自动释放。ORDER BY created_at 维持近似 FIFO。
                rows = list(
                    (
                        await session.execute(
                            select(OutboxEvent)
                            .where(
                                OutboxEvent.published_at.is_(None),
                                OutboxEvent.created_at < cutoff,
                            )
                            .order_by(OutboxEvent.created_at)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).scalars()
                )

                for row in rows:
                    ev_id = row.id
                    ev_kind = row.kind
                    raw_payload = row.payload or {}
                    if not isinstance(raw_payload, dict):
                        logger.error(
                            "outbox event malformed payload id=%s kind=%s "
                            "payload_type=%s payload=%r",
                            ev_id,
                            ev_kind,
                            type(raw_payload).__name__,
                            raw_payload,
                        )
                        dlq_records_to_mirror.append(
                            _persist_outbox_dlq(
                                session,
                                event_id=ev_id,
                                kind=ev_kind,
                                payload={"raw_payload": repr(raw_payload)},
                                reason="malformed_payload",
                            )
                        )
                        row.published_at = datetime.now(timezone.utc)
                        fail_counts_to_clear.add(str(ev_id))
                        continue

                    payload = dict(raw_payload)
                    payload.setdefault("outbox_id", str(ev_id))
                    if payload != raw_payload:
                        row.payload = payload
                    try:
                        dedupe_key, marker, should_set_dedupe = (
                            await _deliver_outbox_event(
                                redis,
                                event_id=str(ev_id),
                                kind=ev_kind,
                                payload=payload,
                            )
                        )
                    except _OutboxPayloadError:
                        logger.warning(
                            "outbox event invalid id=%s kind=%s payload=%s",
                            ev_id,
                            ev_kind,
                            payload,
                        )
                        dlq_records_to_mirror.append(
                            _persist_outbox_dlq(
                                session,
                                event_id=ev_id,
                                kind=ev_kind,
                                payload=payload,
                                reason="invalid_payload",
                            )
                        )
                        row.published_at = datetime.now(timezone.utc)
                        fail_counts_to_clear.add(str(ev_id))
                        continue
                    except Exception as exc:
                        fail_count = await _increment_outbox_fail_count(redis, ev_id)
                        logger.warning(
                            "outbox enqueue failed; leaving unpublished for retry "
                            "event=%s marker=%s kind=%s fail_count=%d err=%s",
                            ev_id,
                            payload.get("task_id") or payload.get("user_id"),
                            ev_kind,
                            fail_count,
                            exc,
                        )
                        if fail_count >= _OUTBOX_MAX_FAIL_COUNT:
                            record = await _persist_outbox_dlq_once(
                                session,
                                event_id=ev_id,
                                kind=ev_kind,
                                payload=payload,
                                reason="max_fail_count",
                                fail_count=fail_count,
                            )
                            if record is not None:
                                dlq_records_to_mirror.append(record)
                        continue

                    if should_set_dedupe:
                        dedupe_keys_to_set.append((dedupe_key, marker))
                    # enqueue 成功或已被 dedupe key 证明之前成功 → 标 published_at；commit 由 context manager
                    row.published_at = datetime.now(timezone.utc)
                    delivered_event_ids.append(str(ev_id))
                    fail_counts_to_clear.add(str(ev_id))
                    processed += 1

                await _resolve_outbox_dlq_rows(session, delivered_event_ids)
        except Exception:  # noqa: BLE001
            # 非毒化的 transient 错误：rollback 已发生，published_at 仍是 NULL，
            # 下轮会再被候选选中；这里只记日志。
            logger.warning("outbox event tx rolled back", exc_info=True)
            return 0

    # P2-16: commit 已成功（async with session.begin() 退出无异常）→ 现在才写入
    # Redis 去重 key。commit 失败路径走 except 分支不会到这里。
    for dedupe_key, task_id in dedupe_keys_to_set:
        try:
            await redis.set(dedupe_key, task_id, ex=_OUTBOX_ENQUEUE_DEDUPE_TTL_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "outbox post-commit dedupe write failed key=%s err=%s",
                dedupe_key,
                exc,
            )
    for event_id in fail_counts_to_clear:
        try:
            await redis.hdel(_OUTBOX_FAIL_COUNT_HASH, event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "outbox post-commit fail-count cleanup failed event=%s err=%s",
                event_id,
                exc,
            )
    for record in dlq_records_to_mirror:
        await _mirror_outbox_dlq(redis, record)
    return processed


# P2-4: HINCRBY + EXPIRE 两步非原子——HINCRBY 成功但 EXPIRE 因连接抖动失败时
# hash 永不过期，僵尸字段长期累积；并发场景下两 worker 也可能拿到错位 TTL。
# 用 EVAL Lua 脚本把两步合成原子操作。
_INCR_FAIL_COUNT_LUA = """
local val = redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return val
"""


async def _increment_outbox_fail_count(redis: Any, event_id: str) -> int:
    """GEN-P0-5: 用 Redis HASH 跟踪单事件连续 enqueue 失败次数。

    用 HASH 而非独立 key 是为了批量 HDEL 重置方便；TTL 设在 hash 维度（24h），
    避免僵尸 key 增长。

    P2-4: HINCRBY + EXPIRE 用 EVAL 原子化，避免 EXPIRE 失败时 hash 永不过期。
    """
    try:
        val = await redis.eval(
            _INCR_FAIL_COUNT_LUA,
            1,
            _OUTBOX_FAIL_COUNT_HASH,
            event_id,
            str(_OUTBOX_FAIL_COUNT_TTL_S),
        )
        return int(val or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbox fail count incr failed event=%s err=%s", event_id, exc)
        return 0


async def _persist_outbox_dlq_once(
    session: Any,
    *,
    event_id: str,
    kind: str,
    payload: dict[str, Any],
    reason: str,
    fail_count: int,
) -> dict[str, Any] | None:
    existing_id = (
        await session.execute(
            select(OutboxDeadLetter.id)
            .where(
                OutboxDeadLetter.outbox_id == event_id,
                OutboxDeadLetter.resolved_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_id is not None:
        return None
    return _persist_outbox_dlq(
        session,
        event_id=event_id,
        kind=kind,
        payload=payload,
        reason=reason,
        fail_count=fail_count,
    )


def _persist_outbox_dlq(
    session: Any,
    *,
    event_id: str,
    kind: str,
    payload: dict[str, Any],
    reason: str = "unspecified",
    fail_count: int = 0,
) -> dict[str, Any]:
    """Persist a poison event using the transaction that owns its parent lock."""
    record = {
        "event_id": event_id,
        "kind": kind,
        "payload": payload,
        "reason": reason,
        "fail_count": fail_count,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    error_class = {
        "malformed_payload": "OutboxMalformedPayload",
        "invalid_payload": "OutboxInvalidPayload",
        "max_fail_count": "OutboxEnqueueFailed",
    }.get(reason, "OutboxPublishFailed")
    session.add(
        OutboxDeadLetter(
            outbox_id=event_id,
            event_type=f"outbox.{kind}",
            payload=payload,
            error_class=error_class,
            error_message=reason,
            retry_count=fail_count,
        )
    )
    return record


async def _resolve_outbox_dlq_rows(
    session: Any,
    event_ids: list[str],
) -> None:
    if not event_ids:
        return
    await session.execute(
        update(OutboxDeadLetter)
        .where(
            OutboxDeadLetter.outbox_id.in_(event_ids),
            OutboxDeadLetter.resolved_at.is_(None),
        )
        .values(resolved_at=datetime.now(timezone.utc))
    )


async def _mirror_outbox_dlq(redis: Any, record: dict[str, Any]) -> None:
    """Best-effort Redis mirror after the PostgreSQL transaction commits."""
    try:
        await redis.lpush(
            _OUTBOX_DLQ_KEY,
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        await redis.ltrim(_OUTBOX_DLQ_KEY, 0, _OUTBOX_DLQ_MAXLEN - 1)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "outbox Redis DLQ mirror failed event=%s err=%s",
            record.get("event_id"),
            exc,
        )


_PendingOutboxDelivery = tuple[str, str, dict[str, Any]]


def _stage_outbox_event(
    session: Any,
    *,
    kind: str,
    payload: dict[str, Any],
) -> _PendingOutboxDelivery:
    event_id = new_uuid7()
    durable_payload = {**payload, "outbox_id": event_id}
    session.add(
        OutboxEvent(
            id=event_id,
            kind=kind,
            payload=durable_payload,
            published_at=None,
        )
    )
    return event_id, kind, durable_payload


async def _mark_staged_outbox_published(event_id: str) -> bool:
    async with SessionLocal() as session:
        row = await session.get(OutboxEvent, event_id)
        if row is None:
            logger.error(
                "post-commit outbox delivery lost persistence event=%s",
                event_id,
            )
            return False
        if row.published_at is None:
            row.published_at = datetime.now(timezone.utc)
        await _resolve_outbox_dlq_rows(session, [event_id])
        await session.commit()
    return True


async def _deliver_staged_outbox_events(
    redis: Any,
    deliveries: list[_PendingOutboxDelivery],
) -> None:
    """Best-effort fast path for already committed outbox rows.

    Any enqueue/publish/finalize failure leaves the row unpublished, so the
    periodic publisher remains the source of recovery.
    """
    for event_id, kind, payload in deliveries:
        try:
            dedupe_key, marker, should_set_dedupe = await _deliver_outbox_event(
                redis,
                event_id=event_id,
                kind=kind,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "post-commit outbox delivery failed event=%s kind=%s err=%s",
                event_id,
                kind,
                exc,
            )
            continue

        try:
            committed = await _mark_staged_outbox_published(event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "post-commit outbox finalize failed event=%s kind=%s err=%s",
                event_id,
                kind,
                exc,
            )
            continue
        if not committed:
            continue

        if should_set_dedupe:
            try:
                await redis.set(
                    dedupe_key,
                    marker,
                    ex=_OUTBOX_ENQUEUE_DEDUPE_TTL_S,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "post-commit outbox dedupe write failed key=%s err=%s",
                    dedupe_key,
                    exc,
                )
        try:
            await redis.hdel(_OUTBOX_FAIL_COUNT_HASH, event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "post-commit outbox fail-count cleanup failed event=%s err=%s",
                event_id,
                exc,
            )


# ---------------------------------------------------------------------------
# reconcile_tasks
# ---------------------------------------------------------------------------


async def _lease_expired(redis: Any, task_id: str) -> bool:
    """Worker lease 过期 = Redis 里不存在此 key。"""
    try:
        v = await redis.get(f"task:{task_id}:lease")
        return v is None
    except Exception:  # noqa: BLE001
        return True


_DUAL_RACE_SENTINEL_PREFIX = "__dr:"
_IMAGE_QUEUE_ACTIVE_KEY = "generation:image_queue:active"
_IMAGE_QUEUE_TASK_PROVIDER_PREFIX = "generation:image_queue:task_provider:"


async def _cleanup_terminal_sentinels(redis: Any) -> None:
    """扫 image_queue active set，对应 DB terminal 状态的 sentinel 强清——避免 cancel
    后 worker 协程未退导致 sentinel 永占 capacity 的死锁。

    只清 dual_race sentinel（`__dr:<task_id>`）；普通 provider name 走原有 release 路径。"""
    try:
        raw_names = await redis.zrange(_IMAGE_QUEUE_ACTIVE_KEY, 0, -1)
    except Exception:  # noqa: BLE001
        return
    sentinel_task_ids: list[tuple[str, str]] = []
    for raw in raw_names or []:
        name = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if name.startswith(_DUAL_RACE_SENTINEL_PREFIX):
            sentinel_task_ids.append((name, name[len(_DUAL_RACE_SENTINEL_PREFIX) :]))
    if not sentinel_task_ids:
        return
    terminal = {
        GenerationStatus.SUCCEEDED.value,
        GenerationStatus.FAILED.value,
        GenerationStatus.CANCELED.value,
    }
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(Generation.id, Generation.status).where(
                        Generation.id.in_([tid for _, tid in sentinel_task_ids])
                    )
                )
            ).all()
        )
    status_by_id = {r.id: r.status for r in rows}
    cleared = 0
    for sentinel_name, tid in sentinel_task_ids:
        status = status_by_id.get(tid)
        if status not in terminal:
            continue
        try:
            await redis.zrem(_IMAGE_QUEUE_ACTIVE_KEY, sentinel_name)
            await redis.delete(f"{_IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{tid}")
            await redis.delete(f"task:{tid}:lease")
            cleared += 1
            logger.info(
                "reconcile cleared terminal sentinel task=%s status=%s",
                tid,
                status,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "reconcile clear sentinel failed task=%s",
                tid,
                exc_info=True,
            )
    if cleared:
        logger.info("reconcile cleared %d terminal sentinel(s)", cleared)


async def reconcile_tasks(ctx: dict[str, Any]) -> int:
    """回收卡住的 task：状态仍在 queued/running 但 updated_at 超过 5 分钟
    且 lease 已过期 → 未达到各任务 retry 上限则重入队；否则标 timeout。"""
    redis = ctx["redis"]

    async with _owned_redis_lock(
        redis,
        key=_RECON_LOCK_KEY,
        ttl_s=_RECON_LOCK_TTL_S,
    ) as acquired:
        if not acquired:
            return 0

        # Cancel API 把 generation status 置为 canceled，但不会主动清 image_queue 的
        # active sentinel / lease / task_provider key——worker 协程可能卡在 long-running
        # httpx 上无法响应 cancel 标志，lease_renewer 还在每 30s 续 active sentinel score。
        # 结果：DB terminal 但 sentinel 永久占着 capacity slot → 新 task 全部 reserve 失败。
        # 这里在每次 reconcile 时主动扫 active set，对应 DB terminal 状态的 sentinel 强清。
        try:
            await _cleanup_terminal_sentinels(redis)
        except Exception:  # noqa: BLE001
            logger.warning("cleanup_terminal_sentinels failed", exc_info=True)

        touched = 0
        pending_outbox: list[_PendingOutboxDelivery] = []
        cutoff = datetime.now(timezone.utc) - _RECON_STUCK_AFTER

        async with SessionLocal() as session:
            # --- generations ---
            gen_q = (
                select(Generation)
                .where(
                    or_(
                        Generation.status == GenerationStatus.QUEUED.value,
                        Generation.status == GenerationStatus.RUNNING.value,
                    ),
                    Generation.updated_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
            gen_rows = list((await session.execute(gen_q)).scalars())

            for g in gen_rows:
                if not await _lease_expired(redis, g.id):
                    continue
                reconciled_at = datetime.now(timezone.utc)
                if (g.attempt or 0) < _RECON_GENERATION_MAX_ATTEMPTS:
                    g.status = GenerationStatus.QUEUED.value
                    g.progress_stage = GenerationStage.QUEUED.value
                    g.updated_at = reconciled_at
                    pending_outbox.append(
                        _stage_outbox_event(
                            session,
                            kind="generation",
                            payload={
                                "task_id": g.id,
                                "user_id": g.user_id,
                                "kind": "generation",
                                "source": "stuck_task_reconciler",
                            },
                        )
                    )
                    pending_outbox.append(
                        _stage_outbox_event(
                            session,
                            kind="sse",
                            payload={
                                "user_id": g.user_id,
                                "channel": user_channel(g.user_id),
                                "event_name": _EV_GEN_REQUEUED,
                                "data": {
                                    "generation_id": g.id,
                                    "message_id": g.message_id,
                                    "attempt": g.attempt or 0,
                                    "max_attempts": _RECON_GENERATION_MAX_ATTEMPTS,
                                    "kind": "generation",
                                },
                            },
                        )
                    )
                else:
                    g.status = GenerationStatus.FAILED.value
                    g.progress_stage = GenerationStage.FINALIZING.value
                    g.error_code = _RECON_TIMEOUT_CODE
                    g.error_message = _RECON_TIMEOUT_MESSAGE
                    g.finished_at = reconciled_at
                    g.updated_at = reconciled_at
                    msg = await session.get(Message, g.message_id)
                    if msg is not None:
                        msg.status = MessageStatus.FAILED.value
                    # Why: worker died mid-task, the hold from POST /messages
                    # is still subtracted. Without this release the wallet
                    # leaks the held amount permanently — the worker won't
                    # see this generation again because we just marked it
                    # FAILED here.
                    try:
                        await worker_billing.release_generation(
                            session, g, reason=_RECON_TIMEOUT_CODE
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "reconcile release_generation failed gen=%s",
                            g.id,
                        )
                    pending_outbox.append(
                        _stage_outbox_event(
                            session,
                            kind="sse",
                            payload={
                                "user_id": g.user_id,
                                "channel": user_channel(g.user_id),
                                "event_name": EV_GEN_FAILED,
                                "data": {
                                    "generation_id": g.id,
                                    "message_id": g.message_id,
                                    "code": _RECON_TIMEOUT_CODE,
                                    "message": _RECON_TIMEOUT_MESSAGE,
                                    "retriable": False,
                                },
                            },
                        )
                    )
                touched += 1

            # --- completions ---
            comp_q = (
                select(Completion)
                .where(
                    or_(
                        Completion.status == CompletionStatus.QUEUED.value,
                        Completion.status == CompletionStatus.STREAMING.value,
                    ),
                    Completion.updated_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
            comp_rows = list((await session.execute(comp_q)).scalars())

            for c in comp_rows:
                if not await _lease_expired(redis, c.id):
                    continue
                reconciled_at = datetime.now(timezone.utc)
                if (c.attempt or 0) < _RECON_COMPLETION_MAX_ATTEMPTS:
                    c.status = CompletionStatus.QUEUED.value
                    c.progress_stage = CompletionStage.QUEUED.value
                    c.updated_at = reconciled_at
                    pending_outbox.append(
                        _stage_outbox_event(
                            session,
                            kind="completion",
                            payload={
                                "task_id": c.id,
                                "user_id": c.user_id,
                                "kind": "completion",
                                "source": "stuck_task_reconciler",
                            },
                        )
                    )
                    pending_outbox.append(
                        _stage_outbox_event(
                            session,
                            kind="sse",
                            payload={
                                "user_id": c.user_id,
                                "channel": user_channel(c.user_id),
                                "event_name": _EV_COMP_REQUEUED,
                                "data": {
                                    "completion_id": c.id,
                                    "message_id": c.message_id,
                                    "attempt": c.attempt or 0,
                                    "attempt_epoch": c.attempt or 0,
                                    "max_attempts": _RECON_COMPLETION_MAX_ATTEMPTS,
                                    "kind": "completion",
                                },
                            },
                        )
                    )
                else:
                    c.status = CompletionStatus.FAILED.value
                    c.progress_stage = CompletionStage.FINALIZING.value
                    c.error_code = _RECON_TIMEOUT_CODE
                    c.error_message = _RECON_TIMEOUT_MESSAGE
                    c.finished_at = reconciled_at
                    c.updated_at = reconciled_at
                    msg = await session.get(Message, c.message_id)
                    if msg is not None:
                        msg.status = MessageStatus.FAILED.value
                    # Completion holds follow the same lifecycle as image
                    # generation holds: if the worker died before settling,
                    # the reconciler is the last owner that can free them.
                    try:
                        await worker_billing.release_completion(
                            session, c, reason=_RECON_TIMEOUT_CODE
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "reconcile release_completion failed comp=%s",
                            c.id,
                        )
                    pending_outbox.append(
                        _stage_outbox_event(
                            session,
                            kind="sse",
                            payload={
                                "user_id": c.user_id,
                                "channel": user_channel(c.user_id),
                                "event_name": EV_COMP_FAILED,
                                "data": {
                                    "completion_id": c.id,
                                    "message_id": c.message_id,
                                    "attempt": c.attempt or 0,
                                    "attempt_epoch": c.attempt or 0,
                                    "code": _RECON_TIMEOUT_CODE,
                                    "message": _RECON_TIMEOUT_MESSAGE,
                                    "retriable": False,
                                },
                            },
                        )
                    )
                touched += 1

            await session.commit()
            await worker_billing.flush_balance_cache_refreshes(session)

        # Fast path only after the task-row transaction commits. If Redis or
        # finalization fails, the unpublished rows above remain recoverable.
        await _deliver_staged_outbox_events(redis, pending_outbox)

    if touched:
        logger.info("reconcile: touched %d rows", touched)
    return touched


# ---------------------------------------------------------------------------
# cron registration
# ---------------------------------------------------------------------------

# publisher 每 2s 扫一次；reconciler 每分钟一次
# `run_at_startup=True` 让 Worker 起来就先清一遍堆积
cron_jobs = [
    cron(
        publish_outbox,
        second=set(range(0, 60, 2)),
        run_at_startup=True,
    ),
    cron(
        reconcile_tasks,
        minute=set(range(0, 60)),
    ),
]


__all__ = ["publish_outbox", "reconcile_tasks", "cron_jobs"]
