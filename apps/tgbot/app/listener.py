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
  在 _on_succeeded 完成之后；终态推送由短 delivery lock 串行，Telegram
  确认成功后才写 notified。崩溃最多造成可识别的重复，不会永久漏发。
- 跨 user 仍受 _DISPATCH_SEM=8 限流，防多 user 同时大批量打 TG 429。
- tracker 注册任务时把 Lumen user id 写入一个带过期时间的 active-user zset；
  discovery 每 _DISCOVERY_INTERVAL_SEC 只为这些用户启动 stream worker。
  一个集群级限速 fallback 会分批检查 `events:user:*`，只把确实命中 TgBot
  tracker 的旧用户回填到 zset。历史 Web 用户不会占用 Bot 的阻塞连接。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextvars import ContextVar, Token
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from redis import asyncio as aioredis

from .api_client import ApiError, LumenApi
from .config import settings
from .handlers._helpers import mime_extension, truncate_text
from .keyboards import post_success_keyboard, retry_keyboard
from . import listener_runtime as _listener_support
from .tracker import TaskTrack, tracker

logger = logging.getLogger(__name__)

_STAGE_LABELS = MappingProxyType(
    {
        "queued": "排队中",
        "understanding": "理解提示词",
        "rendering": "绘制中",
        "finalizing": "收尾",
    }
)


time = _listener_support.time
ACTIVE_USER_STREAMS_KEY = _listener_support.ACTIVE_USER_STREAMS_KEY
ACTIVE_USER_STREAM_TTL_SECONDS = _listener_support.ACTIVE_USER_STREAM_TTL_SECONDS
TRACK_KEY_PREFIX = _listener_support.TRACK_KEY_PREFIX


ListenerRuntimeState = _listener_support.ListenerRuntimeState
_LISTENER_RUNTIME: ContextVar[ListenerRuntimeState | None] = ContextVar(
    "tgbot_listener_runtime",
    default=None,
)


def _listener_runtime() -> ListenerRuntimeState:
    runtime = _LISTENER_RUNTIME.get()
    if runtime is None:
        runtime = ListenerRuntimeState()
        _LISTENER_RUNTIME.set(runtime)
    return runtime


_PARENT_GRACE_AFTER_SUCCESS_SEC = _listener_support._PARENT_GRACE_AFTER_SUCCESS_SEC
_BONUS_PRECHECK_RETRIES = _listener_support._BONUS_PRECHECK_RETRIES
_STREAM_PREFIX = _listener_support._STREAM_PREFIX
_CURSOR_PREFIX = _listener_support._CURSOR_PREFIX
_DISCOVERY_INTERVAL_SEC = _listener_support._DISCOVERY_INTERVAL_SEC
_XREAD_BLOCK_MS = _listener_support._XREAD_BLOCK_MS
_XREAD_COUNT = _listener_support._XREAD_COUNT
_INITIAL_LOOKBACK_MS = _listener_support._INITIAL_LOOKBACK_MS
_CURSOR_TTL_SECONDS = _listener_support._CURSOR_TTL_SECONDS
_FALLBACK_SCAN_LEASE_KEY = _listener_support._FALLBACK_SCAN_LEASE_KEY
_FALLBACK_SCAN_CURSOR_KEY = _listener_support._FALLBACK_SCAN_CURSOR_KEY
_FALLBACK_STREAM_CURSOR_PREFIX = _listener_support._FALLBACK_STREAM_CURSOR_PREFIX
_FALLBACK_SCAN_LEASE_SECONDS = _listener_support._FALLBACK_SCAN_LEASE_SECONDS
_FALLBACK_SCAN_COUNT = _listener_support._FALLBACK_SCAN_COUNT
_FALLBACK_EMPTY_SCAN_BATCHES = _listener_support._FALLBACK_EMPTY_SCAN_BATCHES
_FALLBACK_ACTIVE_SCAN_BATCHES = _listener_support._FALLBACK_ACTIVE_SCAN_BATCHES
_FALLBACK_EVENTS_PER_STREAM = _listener_support._FALLBACK_EVENTS_PER_STREAM
_stream_key = _listener_support._stream_key
_cursor_key = _listener_support._cursor_key
_fallback_stream_cursor_key = _listener_support._fallback_stream_cursor_key
_initial_cursor = _listener_support._initial_cursor
_decode = _listener_support._decode
_chat_action_heartbeat = _listener_support._chat_action_heartbeat
_stream_generation_ids = _listener_support._stream_generation_ids
_load_cursor = _listener_support._load_cursor
_save_cursor = _listener_support._save_cursor
_RECONNECT_BACKOFF_MAX_SEC = _listener_support._RECONNECT_BACKOFF_MAX_SEC
_RECONNECT_ALERT_THRESHOLD = _listener_support._RECONNECT_ALERT_THRESHOLD
_DISPATCH_MAX_ATTEMPTS = _listener_support._DISPATCH_MAX_ATTEMPTS
_DISPATCH_ATTEMPT_TTL_SEC = _listener_support._DISPATCH_ATTEMPT_TTL_SEC


def _should_throttle_progress(gen_id: str) -> bool:
    return _listener_support._should_throttle_progress(
        gen_id,
        runtime=_listener_runtime(),
    )


def _chat_send_lock(chat_id: int) -> asyncio.Lock:
    return _listener_support._chat_send_lock(
        chat_id,
        runtime=_listener_runtime(),
    )


async def _wait_chat_send_slot(chat_id: int) -> None:
    await _listener_support._wait_chat_send_slot(
        chat_id,
        runtime=_listener_runtime(),
    )


async def _send_document_with_backoff(
    bot: Bot,
    *,
    chat_id: int,
    path: Path,
    filename: str,
    caption: str | None,
    reply_markup: Any,
) -> None:
    await _listener_support._send_document_with_backoff(
        bot,
        runtime=_listener_runtime(),
        chat_id=chat_id,
        path=path,
        filename=filename,
        caption=caption,
        reply_markup=reply_markup,
    )


async def _load_active_user_ids(redis: aioredis.Redis) -> set[str]:
    return await _listener_support._load_active_user_ids(redis, tracker=tracker)


async def _recover_active_user_ids(
    redis: aioredis.Redis,
    *,
    max_scan_batches: int,
) -> set[str]:
    return await _listener_support._recover_active_user_ids(
        redis,
        tracker=tracker,
        max_scan_batches=max_scan_batches,
    )


class _TerminalDeliveryBusy(RuntimeError):
    """A sibling listener owns this terminal event's short delivery lock."""


def _dispatch_attempt_key(user_id: str, entry_id: str) -> str:
    return f"tg:bot:replay:{user_id}:{entry_id}"


async def _notify_dispatch_drop(bot: Bot, event: str, gen_id: str) -> None:
    """放弃重投前，给用户留一句能自救的话。

    J-3/J-4：终态事件连续失败 _DISPATCH_MAX_ATTEMPTS 次后 cursor 会被强推，
    这条事件永远不会再来。什么都不说的话，用户看到的是一个永远停在
    「⏳ 正在生成…」的占位消息 —— 大概率以为失败了再点一次生成，而那是要
    重新扣一遍上游费用的。任务其实已经成功、图能从 /tasks 取回，必须讲清楚。
    只对 succeeded 说这句话：failed 事件本来就没有图可取。
    """
    if event != "generation.succeeded":
        return
    try:
        track = await tracker.get(gen_id)
        if track is None:
            return
        await _replace_status(
            bot,
            track,
            "⚠️ 结果推送失败，但任务本身已经完成，不用重新生成（会重复计费）。\n"
            f"请用 /tasks 查看并取图（#{gen_id[:8]}）。",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("drop notice failed gen=%s err=%r", gen_id, exc)


async def run_listener(bot: Bot, api: LumenApi, stop_event: asyncio.Event) -> None:
    """常驻 task：发现 user streams，按需起 / 重启 per-user worker。

    出错（包括 redis 抖动）只 warn 不抬，sleep 后重连。退避到 60s 上限；连续失败
    超过 _RECONNECT_ALERT_THRESHOLD 次抬到 ERROR 级，便于 systemd / 监控告警，但
    继续重试不退出（systemd Restart=always 已经够，进程死不死无关紧要；listener
    死了用户拿不到推送，比"退出让 systemd 拉起"更糟）。
    """
    runtime_token: Token[ListenerRuntimeState | None] = _LISTENER_RUNTIME.set(
        ListenerRuntimeState()
    )
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
                    logger.info("listener: connected to Redis (stream mode)")
                    backoff = 1.0
                    consecutive_failures = 0
                user_ids = await _load_active_user_ids(redis)
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

            # 起新 user worker；取消已不活跃的订阅，避免连接池随历史用户增长。
            stale_tasks = [
                workers.pop(uid) for uid in list(workers) if uid not in user_ids
            ]
            for task in stale_tasks:
                task.cancel()
            if stale_tasks:
                await asyncio.gather(*stale_tasks, return_exceptions=True)

            for uid in user_ids:
                worker_task = workers.get(uid)
                if worker_task is None or worker_task.done():
                    if worker_task is not None and not worker_task.cancelled():
                        worker_error = worker_task.exception()
                        if worker_error is not None:
                            logger.warning(
                                "listener: worker uid=%s died: %r; restarting",
                                uid,
                                worker_error,
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
        _LISTENER_RUNTIME.reset(runtime_token)


async def _user_worker(
    bot: Bot,
    api: LumenApi,
    redis: aioredis.Redis,
    user_id: str,
    stop_event: asyncio.Event,
) -> None:
    """单 user stream 消费者。串行处理 + cursor 推进。

    cursor 在 _dispatch await 完成 *之后* 才落，保证「dispatch 失败 / 进程
    崩溃」可以靠 replay 找回；Telegram 确认发送后才写 notified，因此进程
    在两者之间崩溃时可能重复，但不会把未发送事件永久吞掉。
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
                    async with _listener_runtime().dispatch_semaphore:
                        await _dispatch(
                            bot,
                            api,
                            envelope,
                            stream_user_id=user_id,
                        )
                except asyncio.CancelledError:
                    raise
                except _TerminalDeliveryBusy as exc:
                    logger.info(
                        "terminal delivery busy uid=%s id=%s err=%s; cursor not advanced",
                        user_id,
                        entry_id,
                        exc,
                    )
                    await asyncio.sleep(1.0)
                    continue
                except Exception as exc:  # noqa: BLE001
                    # 终态 _dispatch 把异常重抛上来 → cursor 不前进，下轮
                    # XREAD 会重新读到这条 entry 再走一次。如果是确定性失败
                    # （下游挂 / payload 构造 bug），消息流会永久卡在这里，
                    # 每轮重下 4K 图。给一条 attempt 计数兜底：超过阈值直
                    # 接放弃这条 event，cursor 推进 + mark_notified 防止
                    # tracker 残留导致后续重投又触发。
                    attempts = 0
                    try:
                        attempts = int(
                            await redis.incr(_dispatch_attempt_key(user_id, entry_id))
                        )
                        await redis.expire(
                            _dispatch_attempt_key(user_id, entry_id),
                            _DISPATCH_ATTEMPT_TTL_SEC,
                        )
                    except Exception as cnt_exc:  # noqa: BLE001
                        logger.warning(
                            "dispatch attempt counter err uid=%s id=%s err=%r",
                            user_id,
                            entry_id,
                            cnt_exc,
                        )
                    if attempts >= _DISPATCH_MAX_ATTEMPTS:
                        gen_id_for_drop = ""
                        try:
                            data_for_drop = envelope.get("data") or {}
                            if isinstance(data_for_drop, dict):
                                gen_id_for_drop = str(
                                    data_for_drop.get("generation_id") or ""
                                )
                        except Exception:  # noqa: BLE001
                            pass
                        logger.warning(
                            "dispatch giving up uid=%s id=%s attempts=%d gen=%s err=%r",
                            user_id,
                            entry_id,
                            attempts,
                            gen_id_for_drop,
                            exc,
                        )
                        if gen_id_for_drop:
                            await _notify_dispatch_drop(
                                bot, str(envelope.get("event") or ""), gen_id_for_drop
                            )
                            try:
                                await tracker.mark_notified(gen_id_for_drop)
                            except Exception as nt_exc:  # noqa: BLE001
                                logger.warning(
                                    "mark_notified on drop failed gen=%s err=%r",
                                    gen_id_for_drop,
                                    nt_exc,
                                )
                        cursor = entry_id
                        await _save_cursor(redis, user_id, entry_id)
                        continue
                    logger.warning(
                        "dispatch err uid=%s id=%s attempts=%d err=%r; cursor not advanced",
                        user_id,
                        entry_id,
                        attempts,
                        exc,
                    )
                    await asyncio.sleep(1.0)
                    continue
                cursor = entry_id
                await _save_cursor(redis, user_id, entry_id)


async def _dispatch(
    bot: Bot,
    api: LumenApi,
    envelope: dict[str, Any],
    *,
    stream_user_id: str = "",
) -> None:
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

    if stream_user_id and event in {
        "generation.queued",
        "generation.started",
        "generation.progress",
        "generation.retrying",
        "generation.partial_image",
        "generation.attached",
    }:
        if not await tracker.refresh(precheck_id, stream_user_id):
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
    except _TerminalDeliveryBusy:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "listener dispatch error gen=%s event=%s err=%r", gen_id, event, exc
        )
        if event in ("generation.succeeded", "generation.failed"):
            raise


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
    text = f"🎁 双引擎也跑出了一张副本，正在收尾…\n\n📝 {_truncate(parent.prompt, 200)}"
    bonus_status = await bot.send_message(chat_id=parent.chat_id, text=text)
    try:
        await tracker.add(
            bonus_id,
            TaskTrack(
                chat_id=parent.chat_id,
                status_message_id=bonus_status.message_id,
                prompt=parent.prompt,
                params=parent.params,
                is_bonus=True,
                user_id=parent.user_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("bonus tracker registration failed gen=%s err=%r", bonus_id, exc)


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


async def _finish_succeeded_cleanup(bot: Bot, gen_id: str, track) -> None:
    # batch 模式下 placeholder 由 _maybe_finalize_batch 统一管理；单任务这里删
    if not track.batch_id:
        try:
            await bot.delete_message(
                chat_id=track.chat_id, message_id=track.status_message_id
            )
        except TelegramBadRequest:
            pass
    await _maybe_finalize_batch(bot, track, gen_id)
    # bonus 自身没有再下一级 bonus，立即清理；winner（非 bonus）必须延迟清理，
    # 否则 dual_race 的 EV_GEN_ATTACHED 来了找不到 parent，loser 那张就丢了。
    if track.is_bonus:
        await tracker.remove(gen_id)
    else:
        asyncio.create_task(_expire_tracker(gen_id, _PARENT_GRACE_AFTER_SUCCESS_SEC))


async def _resolve_succeeded_image_ids(
    api: LumenApi, gen_id: str, track, data: dict[str, Any]
) -> tuple[list[str], dict[str, Any] | None, bool]:
    """解析成功事件里的图片，返回 (image_ids, detail, lookup_failed)。

    J-3：原实现把「API 查不到」和「确认没有图」压成了同一条分支 ——
    get_generation 抛 ApiError 就把 image_ids 置空，然后给用户发
    「生成完成但没有图片返回」并 mark_notified。这一步不可逆：任务其实成功了
    （钱已经扣了），推送却被永久标成已送达，图再也不会补发，用户只能重新生成
    = 再付一次上游的钱。查询失败必须当成可重试错误（lookup_failed=True），
    只有真的拿到空列表才算「没有图」。
    """
    images = data.get("images")
    if isinstance(images, list) and images:
        image_ids = [
            str(img.get("image_id"))
            for img in images
            if isinstance(img, dict) and img.get("image_id")
        ]
        if image_ids:
            return image_ids, None, False
        # 事件里带了 images 却一个 image_id 都解析不出来：结构不认识 ≠ 没有图，
        # 退回 API 再问一次，别急着下「没有图片」的结论。
        logger.warning(
            "succeeded event images unparsable gen=%s raw=%r", gen_id, images
        )
    try:
        detail = await api.get_generation(track.chat_id, gen_id)
    except ApiError as exc:
        logger.warning("succeeded fallback get failed gen=%s err=%s", gen_id, exc)
        return [], None, True
    image_ids = [
        str(image_id) for image_id in (detail.get("image_ids") or []) if image_id
    ]
    return image_ids, detail, False


async def _already_sent_image_ids(gen_id: str) -> set[str]:
    """已送达图片集合；读失败退化成空集（最坏回到「可能重发」的老行为）。"""
    try:
        return await tracker.sent_images(gen_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sent image lookup failed gen=%s err=%r", gen_id, exc)
        return set()


async def _remember_sent_image(gen_id: str, image_id: str) -> None:
    try:
        await tracker.mark_image_sent(gen_id, image_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sent image marker failed gen=%s img=%s err=%r", gen_id, image_id, exc
        )


async def _on_succeeded(
    bot: Bot, api: LumenApi, gen_id: str, track, data: dict[str, Any]
) -> None:
    if not await tracker.begin_delivery(gen_id):
        # Already notified means Telegram confirmed the terminal delivery.
        # If the sender still holds the delivery lock, keep the cursor here
        # until its cleanup completes.
        if await tracker.is_notified(gen_id):
            if await tracker.is_delivery_active(gen_id):
                raise _TerminalDeliveryBusy(
                    f"succeeded delivery still active gen={gen_id}"
                )
            await _finish_succeeded_cleanup(bot, gen_id, track)
            return
        raise _TerminalDeliveryBusy(f"succeeded delivery lock held gen={gen_id}")
    delivered = False
    # (path, mime, size, filename, image_id)
    downloads: list[tuple[Path, str, int, str, str]] = []
    heartbeat = asyncio.create_task(
        _chat_action_heartbeat(bot, track.chat_id, ChatAction.UPLOAD_DOCUMENT),
        name=f"tg-upload-heartbeat-{gen_id[:8]}",
    )
    try:
        # 偶尔事件里没带 images（如 attached 之前的 race），fallback 查 API
        image_ids, detail, lookup_failed = await _resolve_succeeded_image_ids(
            api, gen_id, track, data
        )
        already_sent = await _already_sent_image_ids(gen_id)
        pending_ids = [
            image_id for image_id in image_ids if image_id not in already_sent
        ]

        if lookup_failed:
            # delivered 保持 False → 走下面的 clear_delivery + RuntimeError，
            # 由 _user_worker 的 attempt 计数重投。不能给用户「没有图片」的结论。
            logger.warning("succeeded image lookup failed gen=%s; will retry", gen_id)
        elif not image_ids:
            await _replace_status(
                bot,
                track,
                f"⚠️ 生成完成但没有图片返回。\n\n📝 {_truncate(track.prompt, 200)}",
            )
            delivered = True
        elif not pending_ids:
            # J-4：重投到这里说明这些图上一轮已经全部送达，只是终态标记没落盘。
            logger.info("succeeded replay: all images already sent gen=%s", gen_id)
            delivered = True
        else:
            if detail is None:
                try:
                    detail = await api.get_generation(track.chat_id, gen_id)
                except ApiError as exc:
                    logger.warning(
                        "succeeded link detail get failed gen=%s err=%s", gen_id, exc
                    )
            # batch 模式 placeholder 已经把 prompt 显示过一次；每张图的 caption 不再带原文，
            # 让会话更紧凑。单任务保持完整 caption（用户没有别处能看到 prompt）。
            if track.batch_id:
                if track.is_bonus:
                    caption = f"🎁 #{gen_id[:8]} 双引擎副本"
                else:
                    caption = f"✅ #{gen_id[:8]}"
            elif track.is_bonus:
                caption = f"🎁 双引擎副本（同提示词的第二张）\n\n📝 {_truncate(track.prompt, 800)}"
            else:
                caption = f"✅ 生成完成\n\n📝 {_truncate(track.prompt, 800)}"

            for image_id in pending_ids:
                try:
                    path, mime, size = await api.download_image_to_file(
                        track.chat_id, image_id
                    )
                except ApiError as exc:
                    logger.warning(
                        "download_image failed gen=%s img=%s err=%s",
                        gen_id,
                        image_id,
                        exc,
                    )
                    continue
                # 序号按整批的原始位置算，重投只补发缺的那几张也不会串号
                filename = (
                    f"{gen_id[:8]}-{image_ids.index(image_id) + 1}."
                    f"{mime_extension(mime)}"
                )
                downloads.append((path, mime, size, filename, image_id))

            if not downloads:
                await _replace_status(
                    bot,
                    track,
                    f"⚠️ 生成完成但图片下载失败，请稍后用 /tasks 取图。\n\n📝 {_truncate(track.prompt, 200)}",
                )
                delivered = True
            else:
                # 一律 sendDocument：TG 的 sendPhoto 不论大小都强制缩到 ~1280px + JPEG
                # 重编码（协议设计），4K 图发出去会糊得不能看。Document 通道原样保留。
                actions_kb = (
                    None
                    if track.is_bonus
                    else post_success_keyboard(
                        gen_id,
                        web_url=str((detail or {}).get("edit_url") or ""),
                        project_url=str((detail or {}).get("project_url") or ""),
                    )
                )
                sent_count = 0
                # J-4：caption + 操作键盘只挂在整批的第一条消息上。重投时那条
                # 已经发出去了（already_sent 非空），不能再挂一次，否则用户会
                # 收到两套「🔁 重画 / ✏️ 迭代」按钮。
                head_pending = not already_sent
                for idx, (path, _mime, _size, filename, image_id) in enumerate(
                    downloads
                ):
                    attach_head = head_pending and idx == 0
                    kb = actions_kb if attach_head else None
                    cap = caption if attach_head else None
                    try:
                        await _send_document_with_backoff(
                            bot,
                            chat_id=track.chat_id,
                            path=path,
                            filename=filename,
                            caption=cap,
                            reply_markup=kb,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "send_document failed gen=%s err=%r", gen_id, exc
                        )
                        break
                    sent_count += 1
                    # 先记账再继续：后面任何一张失败都会整条事件重投，
                    # 已经落到用户手里的图不能再发第二遍。
                    await _remember_sent_image(gen_id, image_id)
                # 下载失败的图同样算没送到：现在重投不会重复发送，
                # 补发比「悄悄少给用户一张已付费的图」更合适。
                delivered = sent_count == len(downloads) == len(pending_ids)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        for path, *_ in downloads:
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                logger.warning("tmp cleanup failed path=%s err=%s", path, exc)

    if not delivered:
        await tracker.clear_delivery(gen_id)
        raise RuntimeError(f"terminal delivery failed gen={gen_id}")
    if not await tracker.mark_notified(gen_id, release_lock=False):
        await tracker.clear_delivery(gen_id)
        raise RuntimeError(f"terminal delivery marker failed gen={gen_id}")
    await tracker.clear_delivery(gen_id)
    await _finish_succeeded_cleanup(bot, gen_id, track)


async def _on_failed(bot: Bot, gen_id: str, track, data: dict[str, Any]) -> None:
    if not await tracker.begin_delivery(gen_id):
        if await tracker.is_notified(gen_id):
            if await tracker.is_delivery_active(gen_id):
                raise _TerminalDeliveryBusy(
                    f"failed delivery still active gen={gen_id}"
                )
            await _maybe_finalize_batch(bot, track, gen_id)
            asyncio.create_task(_expire_tracker(gen_id, 300))
            return
        raise _TerminalDeliveryBusy(f"failed delivery lock held gen={gen_id}")
    code = str(data.get("code") or "unknown_error")
    msg = str(data.get("message") or "未知错误")
    text = (
        f"❌ 生成失败 #{gen_id[:8]}\n\n📝 {_truncate(track.prompt, 200)}\n\n"
        f"原因：{code}\n{msg}"
    )
    try:
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
                    chat_id=track.chat_id,
                    text=text,
                    reply_markup=retry_keyboard(gen_id),
                )
        if not await tracker.mark_notified(gen_id, release_lock=False):
            raise RuntimeError(f"terminal delivery marker failed gen={gen_id}")
    finally:
        await tracker.clear_delivery(gen_id)
    await _maybe_finalize_batch(bot, track, gen_id)
    # 失败保留 tracker 一会儿，让重试能拿到原 prompt（5 分钟后过期清理）
    asyncio.create_task(_expire_tracker(gen_id, 300))


async def _maybe_finalize_batch(bot: Bot, track, gen_id: str) -> None:
    """batch 模式：每条 gen 终态扣计数；归零删 placeholder。单任务无操作。"""
    if not track.batch_id:
        return
    remaining = await tracker.batch_decr(track.batch_id, gen_id)
    if remaining is None:
        logger.warning(
            "batch counter missing; skip placeholder delete batch=%s gen=%s",
            track.batch_id,
            gen_id,
        )
        return
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
    return truncate_text(s, n)
