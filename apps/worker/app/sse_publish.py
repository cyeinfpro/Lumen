"""SSE 事件发布辅助。

两个动作组成一次事件发布：
1. `PUBLISH {channel}` —— API 侧 SSE Hub 订阅了 task:{id} / user:{uid} / conv:{cid}
   PubSub 通道，用于实时推送给在线浏览器；消息体是 `{"event": name, "data": {...}}`。
2. `XADD events:user:{uid}` —— 回放 buffer。用户断线重连后用 Last-Event-ID 从这条
   Stream 里补齐未看到的事件。MAXLEN ≈ 86400（~24h）按 DESIGN §8.2。

保持幂等：如果同一事件被重放，订阅方按 event_id 或 SSE id 去重即可。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
import asyncio

from lumen_core.constants import EVENTS_STREAM_PREFIX
from lumen_core.models import OutboxDeadLetter

logger = logging.getLogger(__name__)

# 24h 粗略上限——redis 的 MAXLEN ~ 是近似修剪
_EVENTS_STREAM_MAXLEN = 86400
_EVENTS_DLQ_MAXLEN = 1000
_XADD_RETRY_DELAYS_SECONDS = (0.5, 2.0)

# GEN-P2 ts_ms 单调：进程内 last value，wall clock 回退（NTP 校时 / 闰秒）时
# 至少递增 1ms，保证前端按 ts_ms 排序的事件不会乱序。
_LAST_TS_MS = 0
# P2-3: 多 publish_event 并发时（asyncio 内部多 coroutine 间通过 await 交错），
# _LAST_TS_MS 的读改写非原子，可能被覆盖导致两条事件拿到同一 ts_ms。用
# asyncio.Lock 保护读写，保证单调严格递增。lazily 创建避免 import 阶段抓不到
# 当前事件循环。
_TS_LOCK: asyncio.Lock | None = None


def _get_ts_lock() -> asyncio.Lock:
    global _TS_LOCK
    if _TS_LOCK is None:
        _TS_LOCK = asyncio.Lock()
    return _TS_LOCK


async def _monotonic_ts_ms() -> int:
    global _LAST_TS_MS
    async with _get_ts_lock():
        now = int(time.time() * 1000)
        if now <= _LAST_TS_MS:
            now = _LAST_TS_MS + 1
        _LAST_TS_MS = now
        return now


async def _envelope(event_name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": event_name,
        "data": data,
        "ts_ms": await _monotonic_ts_ms(),
    }


async def publish_event(
    redis: Any,
    user_id: str,
    channel: str,
    event_name: str,
    data: dict[str, Any],
) -> None:
    """发布一条 SSE 事件。

    Args:
        redis: arq 注入的 ArqRedis（继承 redis.asyncio.Redis），支持 publish / xadd
        user_id: 回放 stream 的分片 key
        channel: PubSub 通道名——使用 `task_channel(id)` 或 `user_channel(uid)` 计算
        event_name: `lumen_core.constants.EV_*`
        data: 事件 payload（一定是 dict，不塞 None / bytes）
    """
    stream_key = f"{EVENTS_STREAM_PREFIX}{user_id}"
    envelope = await _envelope(event_name, data)
    stream_id: str | None = None

    for attempt in range(3):
        payload_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        try:
            # Write replay stream first so the live PubSub message can carry the
            # same id browsers later send as Last-Event-ID.
            stream_id = await redis.xadd(
                stream_key,
                {
                    "event": event_name,
                    "data": payload_json,
                },
                maxlen=_EVENTS_STREAM_MAXLEN,
                approximate=True,
            )
            if isinstance(stream_id, bytes):
                stream_id = stream_id.decode("ascii", errors="replace")
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "publish_event: XADD failed key=%s attempt=%d err=%s",
                stream_key,
                attempt + 1,
                exc,
            )
            if attempt < len(_XADD_RETRY_DELAYS_SECONDS):
                await asyncio.sleep(_XADD_RETRY_DELAYS_SECONDS[attempt])

    if stream_id is not None:
        envelope["sse_id"] = stream_id
    else:
        envelope["sse_id"] = f"dlq-{envelope['ts_ms']}-0"
        try:
            await redis.lpush(
                f"{stream_key}:dlq",
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            )
            await redis.ltrim(f"{stream_key}:dlq", 0, _EVENTS_DLQ_MAXLEN - 1)
        except Exception as exc:  # noqa: BLE001
            logger.error("publish_event: DLQ write failed key=%s err=%s", stream_key, exc)
        # 追加：持久化到 PG outbox_dead_letter（独立短事务，写失败仅 logger）
        await _persist_sse_dlq(
            event_name=event_name,
            payload={"user_id": user_id, "channel": channel, "envelope": envelope},
            error_class="XADDFailed",
            error_message=f"all retries failed for stream {stream_key}",
        )

    payload_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))

    # GEN-P1-1: PUBLISH 加 1 次轻量重试。订阅者已经能从 XADD stream 回放，
    # 这里更多是给短暂抖动一个第二次机会，避免在线用户进度卡住。
    for attempt in range(2):
        try:
            await redis.publish(channel, payload_json)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                logger.warning(
                    "publish_event: PUBLISH retry channel=%s err=%s",
                    channel,
                    exc,
                )
                await asyncio.sleep(0.05)
                continue
            logger.warning(
                "publish_event: PUBLISH failed channel=%s err=%s",
                channel,
                exc,
            )


async def _persist_sse_dlq(
    *,
    event_name: str,
    payload: dict[str, Any],
    error_class: str,
    error_message: str,
) -> None:
    """把彻底失败的 SSE 发布事件写入 PG outbox_dead_letter（独立事务）。

    P3-9: insert 前用 (event_type, sse_id, ts_ms) 三元组 dedupe。XADD 全部重试
    失败后该路径可能被外层 publisher 重新触发（network jitter），不去重会导致
    DLQ 重复行膨胀，监控告警重复触发。dedupe 走 PG 一次 SELECT，命中则跳过。
    """
    from sqlalchemy import select

    from .db import SessionLocal

    envelope = payload.get("envelope") if isinstance(payload, dict) else None
    sse_id: str | None = None
    ts_ms: int | None = None
    if isinstance(envelope, dict):
        raw_id = envelope.get("sse_id")
        if isinstance(raw_id, str):
            sse_id = raw_id
        raw_ts = envelope.get("ts_ms")
        if isinstance(raw_ts, int):
            ts_ms = raw_ts

    try:
        async with SessionLocal() as session, session.begin():
            # 仅在能拿到稳定身份（sse_id / ts_ms）时做 dedupe 查；缺失则直接写
            # 避免误丢弃。最多扫描近 200 行同类型 DLQ 找重复，避免大表全扫。
            if sse_id is not None or ts_ms is not None:
                stmt = (
                    select(OutboxDeadLetter)
                    .where(
                        OutboxDeadLetter.outbox_id.is_(None),
                        OutboxDeadLetter.event_type == f"sse.{event_name}",
                    )
                    .order_by(OutboxDeadLetter.id.desc())
                    .limit(200)
                )
                rows = (await session.execute(stmt)).scalars().all()
                duplicate = False
                for row in rows:
                    row_payload = row.payload if isinstance(row.payload, dict) else {}
                    row_env = row_payload.get("envelope") if isinstance(row_payload, dict) else None
                    if not isinstance(row_env, dict):
                        continue
                    if (
                        sse_id is not None
                        and row_env.get("sse_id") == sse_id
                    ):
                        duplicate = True
                        break
                    if (
                        ts_ms is not None
                        and row_env.get("ts_ms") == ts_ms
                        and row_env.get("event") == envelope.get("event")
                    ):
                        duplicate = True
                        break
                if duplicate:
                    logger.info(
                        "publish_event: PG DLQ dedup hit event=%s sse_id=%s ts_ms=%s",
                        event_name, sse_id, ts_ms,
                    )
                    return

            session.add(
                OutboxDeadLetter(
                    outbox_id=None,
                    event_type=f"sse.{event_name}",
                    payload=payload,
                    error_class=error_class,
                    error_message=error_message,
                )
            )
    except Exception as exc:  # noqa: BLE001
        # P3: Known tradeoff — after all XADD retries fail, a PG DLQ write failure
        # means the event is permanently lost.  Monitor with a Prometheus Counter:
        #   lumen_sse_dlq_persist_failed_total{event="..."}
        # If this counter grows, investigate PG / connection pool health.
        logger.error(
            "publish_event: PG DLQ persist failed event=%s err=%s", event_name, exc
        )


__all__ = ["publish_event"]
