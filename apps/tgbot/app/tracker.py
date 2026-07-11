"""跟踪正在执行的生成任务（Redis 后端）。

Why Redis：
- bot 可能多实例 / 重启，进程内 dict 会丢推送。
- listener 收 PubSub 事件后用 gen_id 在这里查归属 chat，跨进程一致。

Schema：
  HSET  tg:track:{gen_id}   user_id / chat_id / status_message_id / prompt / params_json / is_bonus
  EXPIRE tg:track:{gen_id}  48h
  ZADD  tg:track:active-users <expires_at> <user_id>
  SET   tg:track:delivering:{gen_id} 1 NX EX 5m  ← crash 后可重试的发送锁
  SET   tg:track:notified:{gen_id} 1 EX 48h      ← Telegram 已确认终态通知
  SET   tg:batch:{batch_id}:remaining <n> EX 48h
  SADD  tg:batch:{batch_id}:done <gen_id>         ← batch 终态按 gen 去重扣数

48h TTL 兜住绝大多数任务（4K 上限 25 分钟）；过 48h 没结终态的任务视为僵尸，丢弃推送。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, cast

from redis import asyncio as aioredis

from .config import settings

logger = logging.getLogger(__name__)

TRACK_RETENTION_SECONDS = 48 * 3600
TRACK_KEY_PREFIX = "tg:track:"
_TRACK_TTL_SECONDS = TRACK_RETENTION_SECONDS
_KEY_PREFIX = TRACK_KEY_PREFIX
_NOTIFIED_PREFIX = "tg:track:notified:"
_DELIVERING_PREFIX = "tg:track:delivering:"
_BATCH_PREFIX = "tg:batch:"
_DELIVERY_LOCK_SECONDS = 5 * 60
ACTIVE_USER_STREAMS_KEY = "tg:track:active-users"
ACTIVE_USER_STREAM_TTL_SECONDS = TRACK_RETENTION_SECONDS
_ACTIVE_USER_STREAMS_KEY_TTL_SECONDS = ACTIVE_USER_STREAM_TTL_SECONDS + 3600


def _key(gen_id: str) -> str:
    return f"{_KEY_PREFIX}{gen_id}"


def _notified_key(gen_id: str) -> str:
    return f"{_NOTIFIED_PREFIX}{gen_id}"


def _delivering_key(gen_id: str) -> str:
    return f"{_DELIVERING_PREFIX}{gen_id}"


def _batch_key(batch_id: str) -> str:
    return f"{_BATCH_PREFIX}{batch_id}:remaining"


def _batch_done_key(batch_id: str) -> str:
    return f"{_BATCH_PREFIX}{batch_id}:done"


_BATCH_DECR_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
if ARGV[1] ~= '' then
  if redis.call('SADD', KEYS[2], ARGV[1]) == 0 then
    local current = redis.call('GET', KEYS[1])
    if not current then
      return -1
    end
    return tonumber(current)
  end
end
local remaining = redis.call('DECR', KEYS[1])
if remaining < 0 then
  redis.call('SET', KEYS[1], 0, 'EX', tonumber(ARGV[2]))
  remaining = 0
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
return remaining
"""

_REFRESH_TRACK_LUA = """
if ARGV[1] == '' then
  return 0
end
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local current_user_id = redis.call('HGET', KEYS[1], 'user_id')
if current_user_id and current_user_id ~= '' and current_user_id ~= ARGV[1] then
  return -1
end
if not current_user_id or current_user_id == '' then
  redis.call('HSET', KEYS[1], 'user_id', ARGV[1])
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]), ARGV[1])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))
return 1
"""


@dataclass
class TaskTrack:
    chat_id: int
    status_message_id: int
    prompt: str
    params: dict[str, object] = field(default_factory=dict)
    is_bonus: bool = False
    # 当一次提交多张图（count>1）时，所有 gens 共享同一 batch_id（取首个 gen_id）。
    # listener 在终态事件里 DECR tg:batch:{batch_id}:remaining，归零才删 placeholder。
    # 单图任务该字段为 ""。
    batch_id: str = ""
    # Lumen user id 决定 listener 应订阅哪条 events:user:{id} stream。
    user_id: str = ""


class Tracker:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._redis: aioredis.Redis | None = None
        self._clock = clock or time.time

    def _client(self) -> aioredis.Redis:
        if self._redis is None:
            # decode_responses=False：和 listener 一致；HGETALL 返回 bytes，手动 decode
            self._redis = aioredis.from_url(settings.redis_url, decode_responses=False)
        return self._redis

    async def aclose(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None

    async def add(self, gen_id: str, track: TaskTrack) -> None:
        user_id = track.user_id.strip()
        if not user_id:
            raise ValueError("tracker registration requires a non-empty user_id")
        client = self._client()
        pipe = client.pipeline(transaction=True)
        pipe.hset(
            _key(gen_id),
            mapping={
                "chat_id": str(track.chat_id),
                "user_id": user_id,
                "status_message_id": str(track.status_message_id),
                "prompt": track.prompt,
                "params": json.dumps(track.params, ensure_ascii=False),
                "is_bonus": "1" if track.is_bonus else "0",
                "batch_id": track.batch_id,
            },
        )
        pipe.expire(_key(gen_id), _TRACK_TTL_SECONDS)
        pipe.zadd(
            ACTIVE_USER_STREAMS_KEY,
            {user_id: int(self._clock()) + ACTIVE_USER_STREAM_TTL_SECONDS},
        )
        pipe.expire(
            ACTIVE_USER_STREAMS_KEY,
            _ACTIVE_USER_STREAMS_KEY_TTL_SECONDS,
        )
        await pipe.execute()

    async def refresh(self, gen_id: str, user_id: str) -> bool:
        """Bind a legacy tracker to its stream user and renew non-terminal state."""

        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            return False
        client = self._client()
        result = int(
            await cast(
                Awaitable[Any],
                client.eval(
                    _REFRESH_TRACK_LUA,
                    2,
                    _key(gen_id),
                    ACTIVE_USER_STREAMS_KEY,
                    normalized_user_id,
                    str(_TRACK_TTL_SECONDS),
                    str(int(self._clock()) + ACTIVE_USER_STREAM_TTL_SECONDS),
                    str(_ACTIVE_USER_STREAMS_KEY_TTL_SECONDS),
                ),
            )
        )
        if result < 0:
            logger.warning(
                "tracker.refresh: user mismatch gen=%s stream_user=%s",
                gen_id,
                normalized_user_id,
            )
            return False
        return result == 1

    async def _drop_dirty(
        self,
        client: aioredis.Redis,
        gen_id: str,
        reason: str,
        data: dict[str, str],
    ) -> None:
        logger.warning(
            "tracker.get: dropping dirty track reason=%s gen=%s data=%r",
            reason,
            gen_id,
            data,
        )
        try:
            await client.delete(
                _key(gen_id), _notified_key(gen_id), _delivering_key(gen_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tracker.get: dirty cleanup failed gen=%s err=%r", gen_id, exc
            )

    async def get(self, gen_id: str) -> TaskTrack | None:
        client = self._client()
        raw = await cast(
            Awaitable[dict[Any, Any]],
            client.hgetall(_key(gen_id)),
        )
        if not raw:
            return None
        # bytes → str
        d: dict[str, str] = {
            (k.decode() if isinstance(k, (bytes, bytearray)) else str(k)): (
                v.decode("utf-8", errors="replace") if isinstance(v, (bytes, bytearray)) else str(v)
            )
            for k, v in raw.items()
        }
        try:
            chat_raw = d.get("chat_id")
            msg_raw = d.get("status_message_id")
            if not chat_raw or not msg_raw:
                await self._drop_dirty(client, gen_id, "missing_ids", d)
                return None
            chat_id = int(chat_raw)
            msg_id = int(msg_raw)
        except ValueError:
            await self._drop_dirty(client, gen_id, "bad_ints", d)
            return None
        if chat_id <= 0 or msg_id <= 0:
            await self._drop_dirty(client, gen_id, "non_positive_ids", d)
            return None
        try:
            params: dict[str, object] = json.loads(d.get("params") or "{}")
        except ValueError:
            params = {}
        return TaskTrack(
            chat_id=chat_id,
            status_message_id=msg_id,
            prompt=d.get("prompt") or "",
            params=params if isinstance(params, dict) else {},
            is_bonus=(d.get("is_bonus") == "1"),
            batch_id=d.get("batch_id") or "",
            user_id=d.get("user_id") or "",
        )

    async def begin_delivery(self, gen_id: str) -> bool:
        """Acquire a short delivery lock unless this terminal event was delivered."""
        client = self._client()
        if await client.exists(_notified_key(gen_id)):
            return False
        result = await client.set(
            _delivering_key(gen_id), b"1", nx=True, ex=_DELIVERY_LOCK_SECONDS
        )
        return bool(result)

    async def mark_notified(self, gen_id: str, *, release_lock: bool = True) -> bool:
        """Mark terminal delivery sent and optionally release the lock."""
        client = self._client()
        pipe = client.pipeline(transaction=True)
        pipe.set(_notified_key(gen_id), b"1", ex=_TRACK_TTL_SECONDS)
        if release_lock:
            pipe.delete(_delivering_key(gen_id))
        result = await pipe.execute()
        return bool(result and result[0])

    async def clear_delivery(self, gen_id: str) -> None:
        client = self._client()
        await client.delete(_delivering_key(gen_id))

    async def is_notified(self, gen_id: str) -> bool:
        client = self._client()
        result = await client.exists(_notified_key(gen_id))
        return bool(result)

    async def is_delivery_active(self, gen_id: str) -> bool:
        client = self._client()
        result = await client.exists(_delivering_key(gen_id))
        return bool(result)

    async def remove(self, gen_id: str) -> None:
        client = self._client()
        await client.delete(_key(gen_id), _notified_key(gen_id), _delivering_key(gen_id))

    async def init_batch(self, batch_id: str, count: int) -> None:
        if not batch_id or count <= 0:
            return
        client = self._client()
        pipe = client.pipeline(transaction=True)
        pipe.delete(_batch_done_key(batch_id))
        pipe.set(_batch_key(batch_id), str(count), ex=_TRACK_TTL_SECONDS)
        await pipe.execute()

    async def batch_decr(self, batch_id: str, gen_id: str = "") -> int | None:
        """终态事件触发：按 gen_id 去重扣减 batch 剩余计数。

        返回 None 表示 batch counter 已不存在，调用方不应再主动删 placeholder；
        返回 <=0 表示本次或之前已归零，调用方可以做最终清理。
        """
        if not batch_id:
            return 0
        client = self._client()
        try:
            result = int(
                await cast(
                    Awaitable[str],
                    client.eval(
                        _BATCH_DECR_LUA,
                        2,
                        _batch_key(batch_id),
                        _batch_done_key(batch_id),
                        gen_id or "",
                        str(_TRACK_TTL_SECONDS),
                    ),
                )
            )
            return None if result < 0 else result
        except Exception as exc:  # noqa: BLE001
            logger.warning("batch_decr failed batch=%s err=%s", batch_id, exc)
            return None

    async def batch_remove(self, batch_id: str) -> None:
        if not batch_id:
            return
        client = self._client()
        await client.delete(_batch_key(batch_id), _batch_done_key(batch_id))


tracker = Tracker()
