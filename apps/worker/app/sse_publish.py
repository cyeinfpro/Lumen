"""SSE 事件发布辅助。

两个动作组成一次事件发布：
1. `PUBLISH {channel}` —— API 侧 SSE Hub 订阅了 task:{id} / user:{uid} / conv:{cid}
   PubSub 通道，用于实时推送给在线浏览器；消息体是 `{"event": name, "data": {...}}`。
2. `XADD events:user:{uid}` —— 回放 buffer。用户断线重连后用 Last-Event-ID 从这条
   Stream 里补齐未看到的事件。MAXLEN ≈ 86400（~24h）按 DESIGN §8.2。

保持幂等：如果同一事件被重放，订阅方按 event_id 或 SSE id 去重即可。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from lumen_core.constants import EVENTS_STREAM_MAXLEN, EVENTS_STREAM_PREFIX
from lumen_core.models import OutboxDeadLetter

logger = logging.getLogger(__name__)

# 24h 粗略上限——redis 的 MAXLEN ~ 是近似修剪
_EVENTS_DLQ_MAXLEN = 1000
_EVENTS_DEDUPE_TTL_SECONDS = 24 * 60 * 60
_XADD_RETRY_DELAYS_SECONDS = (0.5, 2.0)
_XADD_IDEMPOTENT_LUA = """
local existing = redis.call('GET', KEYS[2])
if existing then
  return existing
end
local stream_id = redis.call(
  'XADD',
  KEYS[1],
  'MAXLEN',
  '~',
  tonumber(ARGV[4]),
  '*',
  'event',
  ARGV[2],
  'data',
  ARGV[3],
  'event_id',
  ARGV[1]
)
redis.call('SET', KEYS[2], stream_id, 'EX', tonumber(ARGV[5]))
return stream_id
"""

# GEN-P2 ts_ms 单调：仅保证当前 worker 进程内 last value。多 API/worker
# 进程间不可比较；前端需要用 Redis stream id 做 replay cursor / 严格排序，
# ts_ms 只作为显示/粗略时间提示。
_LAST_TS_MS = 0
# P2-3/P3-6: 多 publish_event 并发时 _LAST_TS_MS 的读改写非原子，可能被覆盖导致
# 两条事件拿到同一 ts_ms。模块级初始化避免 check-then-set 懒构造竞态。
_TS_LOCK = asyncio.Lock()


async def _monotonic_ts_ms() -> int:
    global _LAST_TS_MS
    async with _TS_LOCK:
        now = int(time.time() * 1000)
        if now <= _LAST_TS_MS:
            now = _LAST_TS_MS + 1
        _LAST_TS_MS = now
        return now


async def _envelope(event_name: str, data: dict[str, Any]) -> dict[str, Any]:
    raw_event_id = data.get("event_id")
    event_id = raw_event_id if raw_event_id not in (None, "") else uuid.uuid4()
    return {
        "event": event_name,
        "data": data,
        "event_id": str(event_id),
        "ts_ms": await _monotonic_ts_ms(),
    }


async def _xadd_event_once(
    redis: Any,
    *,
    stream_key: str,
    event_name: str,
    envelope: dict[str, Any],
    payload_json: str,
) -> str:
    event_id = str(envelope["event_id"])
    eval_fn = getattr(redis, "eval", None)
    if eval_fn is None:
        stream_id = await redis.xadd(
            stream_key,
            {
                "event": event_name,
                "data": payload_json,
                "event_id": event_id,
            },
            maxlen=EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
    else:
        stream_id = await eval_fn(
            _XADD_IDEMPOTENT_LUA,
            2,
            stream_key,
            f"{stream_key}:dedupe:{event_id}",
            event_id,
            event_name,
            payload_json,
            str(EVENTS_STREAM_MAXLEN),
            str(_EVENTS_DEDUPE_TTL_SECONDS),
        )
    if isinstance(stream_id, bytes):
        return stream_id.decode("ascii", errors="replace")
    return str(stream_id)


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
            stream_id = await _xadd_event_once(
                redis,
                stream_key=stream_key,
                event_name=event_name,
                envelope=envelope,
                payload_json=payload_json,
            )
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
        envelope["sse_id"] = f"dlq-{envelope['ts_ms']}-{uuid.uuid4().hex[:12]}"
        redis_dlq_ok = False
        try:
            await redis.lpush(
                f"{stream_key}:dlq",
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            )
            redis_dlq_ok = True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "publish_event: DLQ write failed key=%s err=%s", stream_key, exc
            )
        if redis_dlq_ok:
            try:
                await redis.ltrim(f"{stream_key}:dlq", 0, _EVENTS_DLQ_MAXLEN - 1)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "publish_event: DLQ trim failed key=%s err=%s",
                    stream_key,
                    exc,
                )
        # 追加：持久化到 PG outbox_dead_letter（独立短事务）
        pg_dlq_ok = await _persist_sse_dlq(
            event_name=event_name,
            payload={"user_id": user_id, "channel": channel, "envelope": envelope},
            error_class="XADDFailed",
            error_message=f"all retries failed for stream {stream_key}",
        )
        if not redis_dlq_ok and not pg_dlq_ok:
            raise RuntimeError(f"publish_event: no durable sink for {stream_key}")

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
) -> bool:
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
                    row_env = (
                        row_payload.get("envelope")
                        if isinstance(row_payload, dict)
                        else None
                    )
                    if not isinstance(row_env, dict):
                        continue
                    if sse_id is not None and row_env.get("sse_id") == sse_id:
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
                        event_name,
                        sse_id,
                        ts_ms,
                    )
                    return True

            session.add(
                OutboxDeadLetter(
                    outbox_id=None,
                    event_type=f"sse.{event_name}",
                    payload=payload,
                    error_class=error_class,
                    error_message=error_message,
                )
            )
            return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "publish_event: PG DLQ persist failed event=%s err=%s", event_name, exc
        )
        return False


__all__ = ["publish_event"]
