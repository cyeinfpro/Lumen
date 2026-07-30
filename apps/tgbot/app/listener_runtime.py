"""Runtime state, throttling, and stream discovery for the Telegram listener."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile
from redis import asyncio as aioredis

from .tracker import (
    ACTIVE_USER_STREAMS_KEY,
    ACTIVE_USER_STREAM_TTL_SECONDS,
    TRACK_KEY_PREFIX,
)

logger = logging.getLogger(__name__)

# 同 gen_id 5s 内最多 edit 一次进度，防 TG flood limit。终态事件（succeeded/failed）
# 不受节流，必发。
_PROGRESS_THROTTLE_SEC = 5.0
# OrderedDict + LRU cap：每条 entry 是 (gen_id, last_edit_monotonic)。终态事件不进
# 这个表（gen_id 在 tracker 走 _expire_tracker 自然清），但 progress 事件刷得快：
# 每个 gen 至少一条；cap 兜底防长跑用户多累积导致进程内存泄漏。
# 2000 entry × ~200 bytes ≈ 400 KB worst case，对 bot 进程毫无压力。
_PROGRESS_CACHE_CAP = 2000
_CHAT_SEND_INTERVAL_SEC = 1.05
_CHAT_SEND_CACHE_CAP = 2048


@dataclass
class ListenerRuntimeState:
    """Mutable state owned by one listener lifecycle."""

    progress_last_edit: OrderedDict[str, float] = field(default_factory=OrderedDict)
    chat_send_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    chat_send_next_at: OrderedDict[int, float] = field(default_factory=OrderedDict)
    dispatch_semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(8)
    )


# winner SUCCEEDED 之后，dual_race bonus 还没发 EV_GEN_ATTACHED；listener 不能
# 立刻 tracker.remove(parent)，否则 attached 来了 precheck 查不到 parent 直接丢，
# loser 的图永远推不到 TG。worker 端 bonus iter 会等 loser 完成（最坏情况 ~ 单
# task timeout 量级），这里给 600s 保留期：足够覆盖绝大多数 4K 任务的 loser 收尾。
_PARENT_GRACE_AFTER_SUCCESS_SEC = 600.0
# bonus 事件 precheck 抖动窗口：stream 内 attached/succeeded 顺序保证，但
# attached 自身写入和 send_message 之间仍有 IO 窗口；保留小重试做兜底。
_BONUS_PRECHECK_RETRIES: tuple[float, ...] = (0.5, 1.0, 2.0)

# 协议常量：必须和 worker/sse_publish 的 EVENTS_STREAM_PREFIX 一致
_STREAM_PREFIX = "events:user:"
_CURSOR_PREFIX = "tg:bot:cursor:"

# 新 Bot user stream 被发现的最大延迟。
_DISCOVERY_INTERVAL_SEC = 10.0
# XREAD block 超时（ms）；越大越省 redis 往返
_XREAD_BLOCK_MS = 5000
_XREAD_COUNT = 50

# 首次接管一个 stream（无 cursor）时覆盖完整事件/tracker 保留窗，长停机恢复
# 不再只看最近 1h。
_INITIAL_LOOKBACK_MS = ACTIVE_USER_STREAM_TTL_SECONDS * 1000
_CURSOR_TTL_SECONDS = ACTIVE_USER_STREAM_TTL_SECONDS + 3600

# 升级兼容 fallback：集群内最多一个实例每分钟推进一次 SCAN 游标。正常路径仍
# 只读 active zset；fallback 每批最多检查少量 stream 的最近事件，并且只有事件
# generation_id 对应 TgBot tracker 时才回填，避免把全部 Web 用户订阅进来。
_FALLBACK_SCAN_LEASE_KEY = f"{ACTIVE_USER_STREAMS_KEY}:fallback-scan-lease"
_FALLBACK_SCAN_CURSOR_KEY = f"{ACTIVE_USER_STREAMS_KEY}:fallback-scan-cursor"
_FALLBACK_STREAM_CURSOR_PREFIX = f"{ACTIVE_USER_STREAMS_KEY}:fallback-stream-cursor:"
_FALLBACK_SCAN_LEASE_SECONDS = 60
_FALLBACK_SCAN_COUNT = 16
_FALLBACK_EMPTY_SCAN_BATCHES = 4
_FALLBACK_ACTIVE_SCAN_BATCHES = 1
_FALLBACK_EVENTS_PER_STREAM = 32


def _stream_key(user_id: str) -> str:
    return f"{_STREAM_PREFIX}{user_id}"


def _cursor_key(user_id: str) -> str:
    return f"{_CURSOR_PREFIX}{user_id}"


def _fallback_stream_cursor_key(user_id: str) -> str:
    return f"{_FALLBACK_STREAM_CURSOR_PREFIX}{user_id}"


def _initial_cursor() -> str:
    ms = max(0, int(time.time() * 1000) - _INITIAL_LOOKBACK_MS)
    # XADD id 形如 "<ms>-<seq>"；用 "<ms>-0" 作为下限，XREAD 会返回 > 这个 id 的
    return f"{ms}-0"


def _decode(s: Any) -> str:
    if isinstance(s, (bytes, bytearray)):
        return s.decode("utf-8", errors="replace")
    return str(s)


def _should_throttle_progress(
    gen_id: str,
    *,
    runtime: ListenerRuntimeState,
) -> bool:
    progress_last_edit = runtime.progress_last_edit
    now = time.monotonic()
    last = progress_last_edit.get(gen_id, 0.0)
    if now - last < _PROGRESS_THROTTLE_SEC:
        return True
    # LRU 写入：先 pop 再插入末尾保证最新使用 → 末尾。超 cap 从头部剔除最旧。
    if gen_id in progress_last_edit:
        progress_last_edit.move_to_end(gen_id)
    progress_last_edit[gen_id] = now
    while len(progress_last_edit) > _PROGRESS_CACHE_CAP:
        progress_last_edit.popitem(last=False)
    return False


def _chat_send_lock(
    chat_id: int,
    *,
    runtime: ListenerRuntimeState,
) -> asyncio.Lock:
    lock = runtime.chat_send_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        runtime.chat_send_locks[chat_id] = lock
    return lock


async def _wait_chat_send_slot(
    chat_id: int,
    *,
    runtime: ListenerRuntimeState,
) -> None:
    lock = _chat_send_lock(chat_id, runtime=runtime)
    async with lock:
        now = time.monotonic()
        next_at = runtime.chat_send_next_at.get(chat_id, 0.0)
        if next_at > now:
            await asyncio.sleep(next_at - now)
        runtime.chat_send_next_at[chat_id] = time.monotonic() + _CHAT_SEND_INTERVAL_SEC
        runtime.chat_send_next_at.move_to_end(chat_id)
        # 超 cap 只 evict cold entry：next_at + 60s 仍未到现在的（=已经空闲
        # >60s），才连同 lock 一起移除。无条件 popitem(last=False) 会在持有
        # 者还没 release lock 时把 lock 弹掉，并发 send_document 触发 429。
        if len(runtime.chat_send_next_at) > _CHAT_SEND_CACHE_CAP:
            cold_threshold = time.monotonic() - 60.0
            stale: list[int] = []
            for cid, nxt in runtime.chat_send_next_at.items():
                if nxt < cold_threshold:
                    stale.append(cid)
                    if (
                        len(runtime.chat_send_next_at) - len(stale)
                        <= _CHAT_SEND_CACHE_CAP
                    ):
                        break
            for cid in stale:
                runtime.chat_send_next_at.pop(cid, None)
                runtime.chat_send_locks.pop(cid, None)


async def _chat_action_heartbeat(
    bot: Bot, chat_id: int, action: ChatAction, *, interval_sec: float = 4.0
) -> None:
    while True:
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id=chat_id, action=action)
        await asyncio.sleep(interval_sec)


async def _send_document_with_backoff(
    bot: Bot,
    *,
    runtime: ListenerRuntimeState,
    chat_id: int,
    path: Path,
    filename: str,
    caption: str | None,
    reply_markup: Any,
) -> None:
    for attempt in range(3):
        await _wait_chat_send_slot(chat_id, runtime=runtime)
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(str(path), filename=filename),
                caption=caption,
                reply_markup=reply_markup,
            )
            return
        except TelegramRetryAfter as exc:
            wait_for = float(getattr(exc, "retry_after", 1) or 1) + 0.25
            logger.warning(
                "send_document retry-after chat=%s wait=%.2fs attempt=%d",
                chat_id,
                wait_for,
                attempt + 1,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError(f"send_document exhausted retry-after attempts chat={chat_id}")


async def _load_active_user_ids(
    redis: aioredis.Redis,
    *,
    tracker: Any,
) -> set[str]:
    """Return active users and incrementally rebuild missing upgrade-era entries."""

    now = int(time.time())
    pipe = redis.pipeline(transaction=False)
    pipe.zremrangebyscore(ACTIVE_USER_STREAMS_KEY, "-inf", now)
    pipe.zrangebyscore(ACTIVE_USER_STREAMS_KEY, now, "+inf")
    _removed, rows = await pipe.execute()
    active_user_ids = {
        user_id for row in rows or [] if (user_id := _decode(row).strip())
    }
    active_user_ids.update(
        await _recover_active_user_ids(
            redis,
            tracker=tracker,
            max_scan_batches=(
                _FALLBACK_ACTIVE_SCAN_BATCHES
                if active_user_ids
                else _FALLBACK_EMPTY_SCAN_BATCHES
            ),
        )
    )
    return active_user_ids


def _stream_generation_ids(entries: Any) -> list[str]:
    generation_ids: list[str] = []
    seen: set[str] = set()
    for row in entries or []:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        fields_raw = row[1]
        if not isinstance(fields_raw, dict):
            continue
        fields = {_decode(key): _decode(value) for key, value in fields_raw.items()}
        try:
            envelope = json.loads(fields.get("data") or "{}")
        except (TypeError, ValueError):
            continue
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            continue
        for data_field in ("generation_id", "parent_generation_id"):
            gen_id = str(data.get(data_field) or "").strip()
            if gen_id and gen_id not in seen:
                seen.add(gen_id)
                generation_ids.append(gen_id)
    return generation_ids


async def _recover_active_user_ids(
    redis: aioredis.Redis,
    *,
    tracker: Any,
    max_scan_batches: int,
) -> set[str]:
    """Run one cluster-throttled, bounded migration scan over replay streams."""

    acquired = await redis.set(
        _FALLBACK_SCAN_LEASE_KEY,
        b"1",
        nx=True,
        ex=_FALLBACK_SCAN_LEASE_SECONDS,
    )
    if not acquired:
        return set()

    raw_cursor = await redis.get(_FALLBACK_SCAN_CURSOR_KEY)
    try:
        cursor: Any = int(_decode(raw_cursor)) if raw_cursor is not None else 0
    except ValueError:
        cursor = 0

    candidates: list[tuple[str, str]] = []
    seen_candidates: set[tuple[str, str]] = set()
    for _ in range(max(1, max_scan_batches)):
        cursor, keys = await redis.scan(
            cursor=cursor,
            match=f"{_STREAM_PREFIX}*",
            count=_FALLBACK_SCAN_COUNT,
        )
        for raw_key in keys or []:
            stream_key = _decode(raw_key)
            if not stream_key.startswith(_STREAM_PREFIX) or stream_key.endswith(":dlq"):
                continue
            user_id = stream_key[len(_STREAM_PREFIX) :].strip()
            if not user_id:
                continue
            event_cursor_key = _fallback_stream_cursor_key(user_id)
            raw_event_cursor = await redis.get(event_cursor_key)
            event_cursor = (
                _decode(raw_event_cursor).strip() if raw_event_cursor else "+"
            )
            if event_cursor != "+":
                parts = event_cursor.split("-", 1)
                if len(parts) != 2 or not all(part.isdigit() for part in parts):
                    event_cursor = "+"
            entries = await redis.xrevrange(
                stream_key,
                max="+" if event_cursor == "+" else f"({event_cursor}",
                min=_initial_cursor(),
                count=_FALLBACK_EVENTS_PER_STREAM,
            )
            next_event_cursor = "+"
            if entries:
                oldest_row = entries[-1]
                if isinstance(oldest_row, (list, tuple)) and oldest_row:
                    oldest_id = _decode(oldest_row[0]).strip()
                    if len(entries) >= _FALLBACK_EVENTS_PER_STREAM and oldest_id:
                        next_event_cursor = oldest_id
            await redis.set(
                event_cursor_key,
                next_event_cursor,
                ex=_CURSOR_TTL_SECONDS,
            )
            for gen_id in _stream_generation_ids(entries):
                candidate = (user_id, gen_id)
                if candidate not in seen_candidates:
                    seen_candidates.add(candidate)
                    candidates.append(candidate)
        if cursor in (0, b"0", "0"):
            break

    await redis.set(
        _FALLBACK_SCAN_CURSOR_KEY,
        _decode(cursor),
        ex=_CURSOR_TTL_SECONDS,
    )
    if not candidates:
        return set()

    exists_pipe = redis.pipeline(transaction=False)
    for _user_id, gen_id in candidates:
        exists_pipe.exists(f"{TRACK_KEY_PREFIX}{gen_id}")
    existing = await exists_pipe.execute()

    recovered: set[str] = set()
    for (user_id, gen_id), exists in zip(candidates, existing, strict=False):
        if not exists or user_id in recovered:
            continue
        try:
            if await tracker.refresh(gen_id, user_id):
                recovered.add(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "listener: fallback tracker refresh failed uid=%s gen=%s err=%r",
                user_id,
                gen_id,
                exc,
            )
    if recovered:
        logger.info(
            "listener: fallback rebuilt active users count=%d",
            len(recovered),
        )
    return recovered


async def _load_cursor(redis: aioredis.Redis, user_id: str) -> str:
    raw = await redis.get(_cursor_key(user_id))
    if raw is None:
        return _initial_cursor()
    return _decode(raw)


async def _save_cursor(redis: aioredis.Redis, user_id: str, sse_id: str) -> None:
    await redis.set(
        _cursor_key(user_id),
        sse_id,
        ex=_CURSOR_TTL_SECONDS,
    )


_RECONNECT_BACKOFF_MAX_SEC = 60.0
_RECONNECT_ALERT_THRESHOLD = 50

# 单条 stream entry 最多 replay 多少次才放弃。终态 dispatch 失败时 cursor 不
# 前进，下一轮 XREAD 会重新读到同一条 entry；如果是确定性失败（比如下游 API
# 长时间挂、消息构造 bug），不限次重试 = 用户消息流永久卡死且每轮重下 4K 图。
# 5 次 ≈ 5 min（每次 dispatch 失败 sleep 1s + 终态走完整发送链路）。
_DISPATCH_MAX_ATTEMPTS = 5
# attempt 计数 key TTL：足够覆盖单 entry 重试窗口又不留长期垃圾。
_DISPATCH_ATTEMPT_TTL_SEC = 30 * 60
