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

import logging
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from arq.cron import cron
from sqlalchemy import or_, select

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
from lumen_core.models import Completion, Generation, Message, OutboxDeadLetter, OutboxEvent

from ..db import SessionLocal
from ..sse_publish import publish_event

logger = logging.getLogger(__name__)


_OUTBOX_LOCK_KEY = "lock:outbox:publisher"
_OUTBOX_LOCK_TTL_S = 10
_OUTBOX_BATCH = 100
_OUTBOX_DLQ_KEY = "outbox:dead-letter"
_OUTBOX_DLQ_MAXLEN = 1000
# GEN-P0-5: enqueue 连续失败超过此阈值 → DLQ 化并标记 published_at 终止循环。
# 5 次配合 publisher 2s 扫一次 ≈ 至少 10s 的 Redis 抖动容忍。
_OUTBOX_MAX_FAIL_COUNT = 5
# 单次 publisher 调用里，同一事件失败计数的 Redis HASH 键（不持久化到 PG，避免 migration）。
_OUTBOX_FAIL_COUNT_HASH = "outbox:fail_count"
_OUTBOX_FAIL_COUNT_TTL_S = 24 * 3600
_OUTBOX_ENQUEUE_DEDUPE_PREFIX = "outbox:enqueued:"
_OUTBOX_ENQUEUE_DEDUPE_TTL_S = 24 * 3600
# P1-9: enqueue 与 PG commit 不在一个事务里。若 dedupe key 用 24h TTL 一次性写入，
# 而 commit 之后 rollback（PG 抖动 / 约束冲突），key 仍占 24h 导致下轮 publisher
# 跳过这条事件 → 事件永久丢失。改成"短 TTL 占位 + commit 成功后续期"：
# 短 TTL 期间内已防住批内重复 enqueue；commit 失败时 key 自然过期，下轮可重试。
_OUTBOX_ENQUEUE_DEDUPE_PLACEHOLDER_TTL_S = 60

_RECON_LOCK_KEY = "lock:outbox:reconciler"
_RECON_LOCK_TTL_S = 50
_RECON_STUCK_AFTER = timedelta(minutes=5)
_RECON_GENERATION_MAX_ATTEMPTS = 5
_RECON_COMPLETION_MAX_ATTEMPTS = 3
_RECON_TIMEOUT_CODE = "timeout"
_RECON_TIMEOUT_MESSAGE = "task stuck; reconciler timed out"
_EV_GEN_REQUEUED = "generation.requeued"
_EV_COMP_REQUEUED = "completion.requeued"


# ---------------------------------------------------------------------------
# publish_outbox
# ---------------------------------------------------------------------------


async def publish_outbox(ctx: dict[str, Any]) -> int:
    """扫未发布的 outbox_events 并 enqueue。返回处理条数。

    GEN-P0-5: 每个事件用 `SELECT ... FOR UPDATE SKIP LOCKED` 独立短事务：
    (1) 行级锁读候选 → (2) enqueue → (3) UPDATE published_at 同事务 commit。
    enqueue 失败 rollback（published_at 保持 NULL），同时在 Redis HASH 累加失败计数；
    超过 _OUTBOX_MAX_FAIL_COUNT 次 → 写 DLQ 并标 published_at 让这条事件出循环。
    """
    redis = ctx["redis"]

    # SETNX 锁：同一秒只允许一个 worker 扫（批粒度锁，和行锁是两层）
    got = await redis.set(_OUTBOX_LOCK_KEY, "1", ex=_OUTBOX_LOCK_TTL_S, nx=True)
    if not got:
        return 0

    processed = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=2)

        processed = await _process_outbox_batch(redis, cutoff, _OUTBOX_BATCH)
    finally:
        try:
            await redis.delete(_OUTBOX_LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass

    if processed:
        logger.info("outbox: published %d events", processed)
    return processed


async def _process_outbox_batch(redis: Any, cutoff: datetime, limit: int) -> int:
    """GEN-P0-5: 批事务内 "claim → enqueue → commit published_at"。

    P1-9: dedupe key 先用短 TTL 占位（_OUTBOX_ENQUEUE_DEDUPE_PLACEHOLDER_TTL_S），
    commit 成功后再 EXPIRE 续期到完整 _OUTBOX_ENQUEUE_DEDUPE_TTL_S；commit 失败
    （比如 PG 约束冲突 / 连接抖动）时占位 key 在 60s 内过期，下轮 publisher 可重试。
    """
    processed = 0
    # 收集本批内 commit 成功的 dedupe key，commit 完成后统一续期。
    dedupe_keys_to_extend: list[str] = []
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
                        await _write_outbox_dlq(
                            redis,
                            ev_id,
                            ev_kind,
                            {"raw_payload": repr(raw_payload)},
                            reason="malformed_payload",
                        )
                        row.published_at = datetime.now(timezone.utc)
                        continue

                    payload = dict(raw_payload)
                    task_id = payload.get("task_id") or payload.get("id")
                    if not task_id or ev_kind not in {"generation", "completion"}:
                        logger.warning(
                            "outbox event invalid id=%s kind=%s payload=%s",
                            ev_id, ev_kind, payload,
                        )
                        await _write_outbox_dlq(
                            redis, ev_id, ev_kind, payload,
                            reason="invalid_payload",
                        )
                        row.published_at = datetime.now(timezone.utc)
                        continue

                    job_name = (
                        "run_generation" if ev_kind == "generation" else "run_completion"
                    )
                    dedupe_key = f"{_OUTBOX_ENQUEUE_DEDUPE_PREFIX}{ev_id}"

                    # Guard the non-transactional Redis enqueue with a per-event
                    # dedupe key. If enqueue succeeds but the PG transaction rolls
                    # back, the next publisher pass marks the row published without
                    # enqueueing a duplicate job.
                    # 多图 stagger：messages.py 给 i>=1 的 generation row 在 payload 里加 defer_s，
                    # 让 arq 延迟 N 秒后才让 worker 拉起来跑。避免同 prompt 同账号同时撞 ChatGPT codex
                    # 引发 OpenAI 内部 race condition（一败一成稳定模式）。
                    defer_s = payload.get("defer_s")
                    enqueue_kwargs: dict[str, Any] = {}
                    if isinstance(defer_s, (int, float)) and defer_s > 0:
                        enqueue_kwargs["_defer_by"] = float(defer_s)

                    try:
                        # P1-9: 先用短 TTL 占位；commit 成功后批末统一续期到 24h。
                        first_enqueue = await redis.set(
                            dedupe_key,
                            task_id,
                            nx=True,
                            ex=_OUTBOX_ENQUEUE_DEDUPE_PLACEHOLDER_TTL_S,
                        )
                        if first_enqueue:
                            await redis.enqueue_job(job_name, task_id, **enqueue_kwargs)
                            dedupe_keys_to_extend.append(dedupe_key)
                        else:
                            logger.info(
                                "outbox enqueue deduped event=%s task=%s kind=%s",
                                ev_id,
                                task_id,
                                ev_kind,
                            )
                    except Exception as exc:
                        try:
                            await redis.delete(dedupe_key)
                        except Exception:  # noqa: BLE001
                            pass
                        fail_count = await _increment_outbox_fail_count(redis, ev_id)
                        logger.warning(
                            "outbox enqueue failed; leaving unpublished for retry "
                            "event=%s task=%s kind=%s fail_count=%d err=%s",
                            ev_id, task_id, ev_kind, fail_count, exc,
                        )
                        if fail_count >= _OUTBOX_MAX_FAIL_COUNT:
                            await _write_outbox_dlq(
                                redis, ev_id, ev_kind, payload,
                                reason="max_fail_count", fail_count=fail_count,
                            )
                            row.published_at = datetime.now(timezone.utc)
                            try:
                                await redis.hdel(_OUTBOX_FAIL_COUNT_HASH, ev_id)
                            except Exception:  # noqa: BLE001
                                pass
                        continue

                    # enqueue 成功或已被 dedupe key 证明之前成功 → 标 published_at；commit 由 context manager
                    row.published_at = datetime.now(timezone.utc)
                    processed += 1
        except Exception as exc:  # noqa: BLE001
            # 非毒化的 transient 错误：rollback 已发生，published_at 仍是 NULL，
            # 下轮会再被候选选中；这里只记日志。
            # P1-9: dedupe key 用短 TTL 占位，commit 失败这些 key 60s 内自动过期，
            # 下轮 publisher 可正常重试 enqueue，事件不会永久丢失。
            logger.debug("outbox event tx rolled back: %s", exc)
            return 0

    # P1-9: commit 已成功（async with session.begin() 退出无异常）→ 把本批写入的
    # dedupe key 续期到完整 24h；commit 失败路径走 except 分支不会到这里。
    for dedupe_key in dedupe_keys_to_extend:
        try:
            await redis.expire(dedupe_key, _OUTBOX_ENQUEUE_DEDUPE_TTL_S)
        except Exception:  # noqa: BLE001
            # 续期失败不致命：占位 TTL 60s 内仍能防住批内重复，过期后最坏导致
            # 一次重复 enqueue（task 那侧用 task_id 幂等吸收）。
            pass
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


async def _write_outbox_dlq(
    redis: Any,
    event_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    reason: str = "unspecified",
    fail_count: int = 0,
) -> None:
    try:
        await redis.lpush(
            _OUTBOX_DLQ_KEY,
            json.dumps(
                {
                    "event_id": event_id,
                    "kind": kind,
                    "payload": payload,
                    "reason": reason,
                    "fail_count": fail_count,
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        await redis.ltrim(_OUTBOX_DLQ_KEY, 0, _OUTBOX_DLQ_MAXLEN - 1)
    except Exception as exc:  # noqa: BLE001
        logger.error("outbox DLQ write failed event=%s err=%s", event_id, exc)

    # 追加：持久化到 PG outbox_dead_letter（独立短事务，失败仅记日志）
    try:
        async with SessionLocal() as session, session.begin():
            session.add(
                OutboxDeadLetter(
                    outbox_id=event_id,
                    event_type=f"outbox.{kind}",
                    payload=payload,
                    error_class="OutboxEnqueueFailed",
                    error_message=reason,
                    retry_count=fail_count,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("outbox PG DLQ persist failed event=%s err=%s", event_id, exc)


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
            sentinel_task_ids.append(
                (name, name[len(_DUAL_RACE_SENTINEL_PREFIX):])
            )
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

    got = await redis.set(_RECON_LOCK_KEY, "1", ex=_RECON_LOCK_TTL_S, nx=True)
    if not got:
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
    pending_sse: list[tuple[str, str, str, dict[str, Any]]] = []
    try:
        cutoff = datetime.now(timezone.utc) - _RECON_STUCK_AFTER

        async with SessionLocal() as session:
            # --- generations ---
            gen_q = select(Generation).where(
                or_(
                    Generation.status == GenerationStatus.QUEUED.value,
                    Generation.status == GenerationStatus.RUNNING.value,
                ),
                Generation.updated_at < cutoff,
            ).with_for_update(skip_locked=True)
            gen_rows = list((await session.execute(gen_q)).scalars())

            for g in gen_rows:
                if not await _lease_expired(redis, g.id):
                    continue
                if (g.attempt or 0) < _RECON_GENERATION_MAX_ATTEMPTS:
                    try:
                        await redis.enqueue_job("run_generation", g.id)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "reconcile re-enqueue gen=%s failed err=%s", g.id, exc
                        )
                        continue
                    g.status = GenerationStatus.QUEUED.value
                    g.progress_stage = GenerationStage.QUEUED.value
                    pending_sse.append(
                        (
                            g.user_id,
                            user_channel(g.user_id),
                            _EV_GEN_REQUEUED,
                            {
                                "generation_id": g.id,
                                "message_id": g.message_id,
                                "attempt": g.attempt or 0,
                                "max_attempts": _RECON_GENERATION_MAX_ATTEMPTS,
                                "kind": "generation",
                            },
                        )
                    )
                else:
                    g.status = GenerationStatus.FAILED.value
                    g.progress_stage = GenerationStage.FINALIZING.value
                    g.error_code = _RECON_TIMEOUT_CODE
                    g.error_message = _RECON_TIMEOUT_MESSAGE
                    g.finished_at = datetime.now(timezone.utc)
                    msg = await session.get(Message, g.message_id)
                    if msg is not None:
                        msg.status = MessageStatus.FAILED.value
                    pending_sse.append(
                        (
                            g.user_id,
                            user_channel(g.user_id),
                            EV_GEN_FAILED,
                            {
                                "generation_id": g.id,
                                "message_id": g.message_id,
                                "code": _RECON_TIMEOUT_CODE,
                                "message": _RECON_TIMEOUT_MESSAGE,
                                "retriable": False,
                            },
                        )
                    )
                touched += 1

            # --- completions ---
            comp_q = select(Completion).where(
                or_(
                    Completion.status == CompletionStatus.QUEUED.value,
                    Completion.status == CompletionStatus.STREAMING.value,
                ),
                Completion.updated_at < cutoff,
            ).with_for_update(skip_locked=True)
            comp_rows = list((await session.execute(comp_q)).scalars())

            for c in comp_rows:
                if not await _lease_expired(redis, c.id):
                    continue
                if (c.attempt or 0) < _RECON_COMPLETION_MAX_ATTEMPTS:
                    try:
                        await redis.enqueue_job("run_completion", c.id)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "reconcile re-enqueue comp=%s failed err=%s", c.id, exc
                        )
                        continue
                    c.status = CompletionStatus.QUEUED.value
                    c.progress_stage = CompletionStage.QUEUED.value
                    pending_sse.append(
                        (
                            c.user_id,
                            user_channel(c.user_id),
                            _EV_COMP_REQUEUED,
                            {
                                "completion_id": c.id,
                                "message_id": c.message_id,
                                "attempt": c.attempt or 0,
                                "attempt_epoch": c.attempt or 0,
                                "max_attempts": _RECON_COMPLETION_MAX_ATTEMPTS,
                                "kind": "completion",
                            },
                        )
                    )
                else:
                    c.status = CompletionStatus.FAILED.value
                    c.progress_stage = CompletionStage.FINALIZING.value
                    c.error_code = _RECON_TIMEOUT_CODE
                    c.error_message = _RECON_TIMEOUT_MESSAGE
                    c.finished_at = datetime.now(timezone.utc)
                    msg = await session.get(Message, c.message_id)
                    if msg is not None:
                        msg.status = MessageStatus.FAILED.value
                    pending_sse.append(
                        (
                            c.user_id,
                            user_channel(c.user_id),
                            EV_COMP_FAILED,
                            {
                                "completion_id": c.id,
                                "message_id": c.message_id,
                                "attempt": c.attempt or 0,
                                "attempt_epoch": c.attempt or 0,
                                "code": _RECON_TIMEOUT_CODE,
                                "message": _RECON_TIMEOUT_MESSAGE,
                                "retriable": False,
                            },
                        )
                    )
                touched += 1

            await session.commit()

        for user_id, channel, event_name, data in pending_sse:
            await publish_event(redis, user_id, channel, event_name, data)
    finally:
        try:
            await redis.delete(_RECON_LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass

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
