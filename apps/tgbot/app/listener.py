"""Redis Stream listener — XREAD events:user:* + cursor 续读。

设计 vs 老版 PubSub
--------------------
老 listener `psubscribe("task:*")` 拿瞬态广播。bot 重启 / 网络抖动 / Redis
reconnect 期间，worker PUBLISH 出去的 generation.succeeded 直接落地，没人
订阅就丢，用户少图。

新 listener 走 `events:user:{uid}` 持久 stream（worker 端 sse_publish 已经
XADD 到这里给 web 断线续传用）。每个 user 一个 worker coroutine，串行处理
+ cursor 落 `tg:bot:cursor:{uid}`。Bot 重启从 cursor 续读；MAXLEN ~24h 内
任何丢图都能补回。

并发模型
--------
- 一个 user 一个 coroutine，串行消费 stream。这样保证 cursor advance 永远
  在 _on_succeeded 完成之后；mid-dispatch crash 重启 replay → mark_notified
  SETNX 自然去重。
- 跨 user 仍受 _DISPATCH_SEM=8 限流，防多 user 同时大批量打 TG 429。
- discovery 每 _DISCOVERY_INTERVAL_SEC 一次 SCAN events:user:*，新出现的
  stream 起 worker。新绑定 user 首次生成后最长 ~10s 内被接管。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile
from redis import asyncio as aioredis

from .api_client import ApiError, LumenApi
from .config import settings
from .keyboards import post_success_keyboard, retry_keyboard
from .tracker import TaskTrack, tracker

logger = logging.getLogger(__name__)

_STAGE_LABELS = {
    "queued": "排队中",
    "understanding": "理解 prompt",
    "rendering": "绘制中",
    "finalizing": "收尾",
}

# 同 gen_id 5s 内最多 edit 一次进度，防 TG flood limit。终态事件（succeeded/failed）
# 不受节流，必发。
_PROGRESS_THROTTLE_SEC = 5.0
# OrderedDict + LRU cap：每条 entry 是 (gen_id, last_edit_monotonic)。终态事件不进
# 这个表（gen_id 在 tracker 走 _expire_tracker 自然清），但 progress 事件刷得快：
# 每个 gen 至少一条；cap 兜底防长跑用户多累积导致进程内存泄漏。
# 2000 entry × ~200 bytes ≈ 400 KB worst case，对 bot 进程毫无压力。
_PROGRESS_CACHE_CAP = 2000
_PROGRESS_LAST_EDIT: "OrderedDict[str, float]" = OrderedDict()

# 跨 user 上限。每个 user worker 内部已经串行；这个 sem 限的是多 user 同时
# 大批量收尾时打 TG 的并发上限。
_DISPATCH_SEM = asyncio.Semaphore(8)

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

# 新 user stream 被发现的最大延迟。SCAN 频率，越小越实时越多 redis 往返。
_DISCOVERY_INTERVAL_SEC = 10.0
# XREAD block 超时（ms）；越大越省 redis 往返
_XREAD_BLOCK_MS = 5000
_XREAD_COUNT = 50

# 首次接管一个 stream（无 cursor）时回看窗口。覆盖 bot crash 后短时间事件，
# 又不至于回放整个 24h MAXLEN 历史。1h 比单 task 上限（25min @ 4K）宽 2 倍。
_INITIAL_LOOKBACK_MS = 60 * 60 * 1000


def _stream_key(user_id: str) -> str:
    return f"{_STREAM_PREFIX}{user_id}"


def _cursor_key(user_id: str) -> str:
    return f"{_CURSOR_PREFIX}{user_id}"


def _initial_cursor() -> str:
    ms = max(0, int(time.time() * 1000) - _INITIAL_LOOKBACK_MS)
    # XADD id 形如 "<ms>-<seq>"；用 "<ms>-0" 作为下限，XREAD 会返回 > 这个 id 的
    return f"{ms}-0"


def _decode(s: Any) -> str:
    if isinstance(s, (bytes, bytearray)):
        return s.decode("utf-8", errors="replace")
    return str(s)


def _should_throttle_progress(gen_id: str) -> bool:
    now = time.monotonic()
    last = _PROGRESS_LAST_EDIT.get(gen_id, 0.0)
    if now - last < _PROGRESS_THROTTLE_SEC:
        return True
    # LRU 写入：先 pop 再插入末尾保证最新使用 → 末尾。超 cap 从头部剔除最旧。
    if gen_id in _PROGRESS_LAST_EDIT:
        _PROGRESS_LAST_EDIT.move_to_end(gen_id)
    _PROGRESS_LAST_EDIT[gen_id] = now
    while len(_PROGRESS_LAST_EDIT) > _PROGRESS_CACHE_CAP:
        _PROGRESS_LAST_EDIT.popitem(last=False)
    return False


async def _scan_user_streams(redis: aioredis.Redis) -> set[str]:
    user_ids: set[str] = set()
    cursor: Any = 0
    while True:
        cursor, keys = await redis.scan(
            cursor=cursor, match=f"{_STREAM_PREFIX}*", count=200
        )
        for k in keys:
            sk = _decode(k)
            user_ids.add(sk[len(_STREAM_PREFIX):])
        # redis-py 异步：cursor 0 / b"0" / "0" 都代表迭代结束
        if cursor in (0, b"0", "0"):
            break
    return user_ids


async def _load_cursor(redis: aioredis.Redis, user_id: str) -> str:
    raw = await redis.get(_cursor_key(user_id))
    if raw is None:
        return _initial_cursor()
    return _decode(raw)


async def _save_cursor(redis: aioredis.Redis, user_id: str, sse_id: str) -> None:
    await redis.set(_cursor_key(user_id), sse_id)


_RECONNECT_BACKOFF_MAX_SEC = 60.0
_RECONNECT_ALERT_THRESHOLD = 50


async def run_listener(bot: Bot, api: LumenApi, stop_event: asyncio.Event) -> None:
    """常驻 task：发现 user streams，按需起 / 重启 per-user worker。

    出错（包括 redis 抖动）只 warn 不抬，sleep 后重连。退避到 60s 上限；连续失败
    超过 _RECONNECT_ALERT_THRESHOLD 次抬到 ERROR 级，便于 systemd / 监控告警，但
    继续重试不退出（systemd Restart=always 已经够，进程死不死无关紧要；listener
    死了用户拿不到推送，比"退出让 systemd 拉起"更糟）。
    """
    backoff = 1.0
    consecutive_failures = 0
    redis: aioredis.Redis | None = None
    workers: dict[str, asyncio.Task] = {}
    try:
        while not stop_event.is_set():
            try:
                if redis is None:
                    redis = aioredis.from_url(
                        settings.redis_url, decode_responses=False
                    )
                    logger.info(
                        "listener: connected to %s (stream mode)", settings.redis_url
                    )
                    backoff = 1.0
                    consecutive_failures = 0
                user_ids = await _scan_user_streams(redis)
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                level = (
                    logging.ERROR
                    if consecutive_failures >= _RECONNECT_ALERT_THRESHOLD
                    else logging.WARNING
                )
                logger.log(
                    level,
                    "listener: discovery err: %s; retry in %.1fs (failures=%d)",
                    exc,
                    backoff,
                    consecutive_failures,
                )
                if redis is not None:
                    try:
                        await redis.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                    redis = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_SEC)
                continue

            # 起新 user worker；清理已 done 的
            for uid in user_ids:
                task = workers.get(uid)
                if task is None or task.done():
                    if task is not None and not task.cancelled():
                        exc = task.exception()
                        if exc is not None:
                            logger.warning(
                                "listener: worker uid=%s died: %r; restarting",
                                uid,
                                exc,
                            )
                    workers[uid] = asyncio.create_task(
                        _user_worker(bot, api, redis, uid, stop_event),
                        name=f"tgbot-stream-{uid[:8]}",
                    )

            # 等到 stop 或 discovery 间隔
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=_DISCOVERY_INTERVAL_SEC
                )
            except asyncio.TimeoutError:
                pass
    finally:
        for t in workers.values():
            t.cancel()
        if workers:
            await asyncio.gather(*workers.values(), return_exceptions=True)
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001
                pass


async def _user_worker(
    bot: Bot,
    api: LumenApi,
    redis: aioredis.Redis,
    user_id: str,
    stop_event: asyncio.Event,
) -> None:
    """单 user stream 消费者。串行处理 + cursor 推进。

    cursor 在 _dispatch await 完成 *之后* 才落，保证「dispatch 失败 / 进程
    崩溃」可以靠 replay 找回；mark_notified SETNX 防止重复推。
    """
    stream_key = _stream_key(user_id)
    cursor = await _load_cursor(redis, user_id)
    logger.info("listener: user worker start uid=%s cursor=%s", user_id, cursor)
    backoff = 1.0
    while not stop_event.is_set():
        try:
            resp = await redis.xread(
                streams={stream_key: cursor},
                count=_XREAD_COUNT,
                block=_XREAD_BLOCK_MS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "listener: XREAD err uid=%s err=%s; retry in %.1fs",
                user_id,
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_SEC)
            continue
        backoff = 1.0
        if not resp:
            continue
        for _stream_raw, entries in resp:
            for entry_id_raw, fields_raw in entries:
                entry_id = _decode(entry_id_raw)
                fields = {_decode(k): _decode(v) for k, v in fields_raw.items()}
                payload_raw = fields.get("data") or "{}"
                try:
                    envelope = json.loads(payload_raw)
                except (TypeError, ValueError):
                    cursor = entry_id
                    await _save_cursor(redis, user_id, entry_id)
                    continue
                try:
                    async with _DISPATCH_SEM:
                        await _dispatch(bot, api, envelope)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dispatch err uid=%s id=%s err=%r",
                        user_id,
                        entry_id,
                        exc,
                    )
                cursor = entry_id
                await _save_cursor(redis, user_id, entry_id)


async def _dispatch(bot: Bot, api: LumenApi, envelope: dict[str, Any]) -> None:
    """单事件处理。

    stream 已经按 user 分流，但同一 user 的事件可能不属于 bot tracker（比如
    web 端用户也用 bot 这个账号在浏览器里跑 web 任务），用 tracker.get 做归属
    过滤。
    """
    event = envelope.get("event") or ""
    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        return

    if event == "generation.attached":
        precheck_id = data.get("parent_generation_id") or ""
    else:
        precheck_id = data.get("generation_id") or ""
    if not precheck_id:
        return
    if await tracker.get(precheck_id) is None:
        # attached 把 bonus_id 注册进 tracker 之前需要 send_message；和
        # succeeded(bonus_id) 之间有 IO 窗口。stream 内顺序保证 attached 先于
        # bonus succeeded，但 _on_attached 自身完成前 succeeded 也可能被读到，
        # 留小重试兜底。
        if event not in ("generation.succeeded", "generation.attached"):
            return
        found = False
        for delay in _BONUS_PRECHECK_RETRIES:
            await asyncio.sleep(delay)
            if await tracker.get(precheck_id) is not None:
                found = True
                break
        if not found:
            return

    if event == "generation.attached":
        try:
            await _on_attached(bot, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("listener attached error data=%s err=%r", data, exc)
        return

    gen_id = data.get("generation_id") or ""
    track = await tracker.get(gen_id)
    if track is None:
        return

    try:
        if event in ("generation.progress", "generation.started"):
            if track.batch_id:
                # 批量任务共享 placeholder，不在 progress 里编辑（多 gen 同时刷会乱）
                return
            if _should_throttle_progress(gen_id):
                return
            await _on_progress(bot, track, data)
        elif event == "generation.succeeded":
            await _on_succeeded(bot, api, gen_id, track, data)
        elif event == "generation.failed":
            await _on_failed(bot, gen_id, track, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "listener dispatch error gen=%s event=%s err=%r", gen_id, event, exc
        )


async def _on_attached(bot: Bot, data: dict[str, Any]) -> None:
    """dual_race 副本（loser 也成功了）attach 到原 message。

    给用户单独发一条「🎁 双引擎额外副本…」状态消息，并把 bonus_gen_id 注册
    进 tracker，这样后续 succeeded(bonus_gen_id) 能找到对应 chat。
    """
    parent_id = data.get("parent_generation_id") or ""
    bonus_id = data.get("generation_id") or ""
    if not parent_id or not bonus_id:
        return
    parent = await tracker.get(parent_id)
    if parent is None:
        return  # 不是 bot 跟踪的任务
    # 已经注册过（异常重投 / replay），跳过
    if await tracker.get(bonus_id) is not None:
        return
    text = (
        f"🎁 双引擎也跑出了一张副本，正在收尾…\n\n"
        f"📝 {_truncate(parent.prompt, 200)}"
    )
    bonus_status = await bot.send_message(chat_id=parent.chat_id, text=text)
    await tracker.add(
        bonus_id,
        TaskTrack(
            chat_id=parent.chat_id,
            status_message_id=bonus_status.message_id,
            prompt=parent.prompt,
            params=parent.params,
            is_bonus=True,
        ),
    )


async def _on_progress(bot: Bot, track, data: dict[str, Any]) -> None:
    stage = str(data.get("progress_stage") or data.get("stage") or "")
    label = _STAGE_LABELS.get(stage, stage or "进行中")
    text = f"⏳ 正在生成…  ({label})\n\n📝 {_truncate(track.prompt, 200)}"
    try:
        await bot.edit_message_text(
            chat_id=track.chat_id,
            message_id=track.status_message_id,
            text=text,
        )
    except TelegramBadRequest:
        # message 内容没变化时 TG 报错，无关紧要
        pass


async def _on_succeeded(bot: Bot, api: LumenApi, gen_id: str, track, data: dict[str, Any]) -> None:
    if not await tracker.mark_notified(gen_id):
        return
    images = data.get("images") or []
    if not isinstance(images, list) or not images:
        # 偶尔事件里没带 images（如 attached 之前的 race），fallback 查 API
        try:
            detail = await api.get_generation(track.chat_id, gen_id)
            image_ids = detail.get("image_ids") or []
        except ApiError as exc:
            logger.warning("succeeded fallback get failed gen=%s err=%s", gen_id, exc)
            image_ids = []
    else:
        image_ids = [str(img.get("image_id")) for img in images if isinstance(img, dict) and img.get("image_id")]

    if not image_ids:
        await _replace_status(
            bot,
            track,
            f"⚠️ 生成完成但没有图片返回。\n\n📝 {_truncate(track.prompt, 200)}",
        )
        return

    # batch 模式 placeholder 已经把 prompt 显示过一次；每张图的 caption 不再带原文，
    # 让会话更紧凑。单任务保持完整 caption（用户没有别处能看到 prompt）。
    if track.batch_id:
        if track.is_bonus:
            caption = f"🎁 #{gen_id[:8]} 双引擎副本"
        else:
            caption = f"✅ #{gen_id[:8]}"
    elif track.is_bonus:
        caption = f"🎁 双引擎副本（同 prompt 第二张）\n\n📝 {_truncate(track.prompt, 800)}"
    else:
        caption = f"✅ 生成完成\n\n📝 {_truncate(track.prompt, 800)}"

    # 流式落盘：image_id → (path, mime, size)。发完 finally 一次性清理。
    downloads: list[tuple[Path, str, int, str]] = []  # (path, mime, size, filename)
    try:
        for idx, image_id in enumerate(image_ids):
            try:
                path, mime, size = await api.download_image_to_file(
                    track.chat_id, image_id
                )
            except ApiError as exc:
                logger.warning(
                    "download_image failed gen=%s img=%s err=%s", gen_id, image_id, exc
                )
                continue
            ext = "png" if "png" in mime else ("webp" if "webp" in mime else "jpg")
            filename = f"{gen_id[:8]}-{idx + 1}.{ext}"
            downloads.append((path, mime, size, filename))

        # 一律 sendDocument：TG 的 sendPhoto 不论大小都强制缩到 ~1280px + JPEG
        # 重编码（协议设计），4K 图发出去会糊得不能看。Document 通道原样保留，
        # TG 仍然会自动生成一张预览缩略图，用户体验差不多但点开是原图。
        actions_kb = None if track.is_bonus else post_success_keyboard(gen_id)
        for idx, (path, mime, _size, filename) in enumerate(downloads):
            kb = actions_kb if idx == 0 else None
            cap = caption if idx == 0 else None
            try:
                await bot.send_document(
                    chat_id=track.chat_id,
                    document=FSInputFile(str(path), filename=filename),
                    caption=cap,
                    reply_markup=kb,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("send_document failed gen=%s err=%r", gen_id, exc)
    finally:
        for path, *_ in downloads:
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                logger.warning("tmp cleanup failed path=%s err=%s", path, exc)

    # batch 模式下 placeholder 由 _maybe_finalize_batch 统一管理；单任务这里删
    if not track.batch_id:
        try:
            await bot.delete_message(chat_id=track.chat_id, message_id=track.status_message_id)
        except TelegramBadRequest:
            pass
    await _maybe_finalize_batch(bot, track)
    # bonus 自身没有再下一级 bonus，立即清理；winner（非 bonus）必须延迟清理，
    # 否则 dual_race 的 EV_GEN_ATTACHED 来了找不到 parent，loser 那张就丢了。
    if track.is_bonus:
        await tracker.remove(gen_id)
    else:
        asyncio.create_task(_expire_tracker(gen_id, _PARENT_GRACE_AFTER_SUCCESS_SEC))


async def _on_failed(bot: Bot, gen_id: str, track, data: dict[str, Any]) -> None:
    if not await tracker.mark_notified(gen_id):
        return
    code = str(data.get("code") or "unknown_error")
    msg = str(data.get("message") or "未知错误")
    text = (
        f"❌ 生成失败 #{gen_id[:8]}\n\n📝 {_truncate(track.prompt, 200)}\n\n"
        f"原因：{code}\n{msg}"
    )
    if track.batch_id:
        # batch 模式 placeholder 不动，失败单独发一条
        await bot.send_message(
            chat_id=track.chat_id, text=text, reply_markup=retry_keyboard(gen_id)
        )
    else:
        try:
            await bot.edit_message_text(
                chat_id=track.chat_id,
                message_id=track.status_message_id,
                text=text,
                reply_markup=retry_keyboard(gen_id),
            )
        except TelegramBadRequest:
            await bot.send_message(
                chat_id=track.chat_id, text=text, reply_markup=retry_keyboard(gen_id)
            )
    await _maybe_finalize_batch(bot, track)
    # 失败保留 tracker 一会儿，让重试能拿到原 prompt（5 分钟后过期清理）
    asyncio.create_task(_expire_tracker(gen_id, 300))


async def _maybe_finalize_batch(bot: Bot, track) -> None:
    """batch 模式：每条 gen 终态扣计数；归零删 placeholder。单任务无操作。"""
    if not track.batch_id:
        return
    remaining = await tracker.batch_decr(track.batch_id)
    if remaining > 0:
        return
    try:
        await bot.delete_message(
            chat_id=track.chat_id, message_id=track.status_message_id
        )
    except TelegramBadRequest:
        pass
    await tracker.batch_remove(track.batch_id)


async def _expire_tracker(gen_id: str, delay: float) -> None:
    await asyncio.sleep(delay)
    await tracker.remove(gen_id)


async def _replace_status(bot: Bot, track, text: str) -> None:
    try:
        await bot.edit_message_text(
            chat_id=track.chat_id, message_id=track.status_message_id, text=text
        )
    except TelegramBadRequest:
        await bot.send_message(chat_id=track.chat_id, text=text)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
