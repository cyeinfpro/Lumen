"""bot 入口中间件：在任何 handler 跑之前做"是不是该让你进来"的过滤。

两层防护：
1. 仅放行 chat.type == "private"（拒群聊 / 频道；防群里加 bot 后 chat_id 被绑导致
   群成员越权）
2. 若配置了 TELEGRAM_ALLOWED_USER_IDS 白名单，from_user.id 必须命中
   （和 telegram_bindings 形成双因子；token 泄漏时仍受 TG 账号 id 约束）
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .api_client import ApiError, LumenApi
from .config import settings

logger = logging.getLogger(__name__)
_ACCESS_REFRESH_INTERVAL_SEC = 15.0


def _parse_allowed_ids(raw: str | None = None) -> set[int]:
    raw = settings.telegram_allowed_user_ids if raw is None else raw
    out: set[int] = set()
    for piece in (raw or "").split(","):
        s = piece.strip()
        if not s:
            continue
        try:
            out.add(int(s))
        except ValueError:
            logger.warning("ignoring non-integer allowed user id: %r", s)
    return out


class AccessGate(BaseMiddleware):
    def __init__(
        self,
        api: LumenApi | None = None,
        *,
        refresh_interval_sec: float = _ACCESS_REFRESH_INTERVAL_SEC,
    ) -> None:
        self._api = api
        self._refresh_interval_sec = max(1.0, refresh_interval_sec)
        self._refresh_lock = None
        self._last_refresh = 0.0
        self._bot_enabled = True
        self._allowed_user_ids = _parse_allowed_ids()

    async def _refresh_access_config(self) -> None:
        if self._api is None:
            self._allowed_user_ids = _parse_allowed_ids()
            self._last_refresh = time.monotonic()
            return

        import asyncio

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        now = time.monotonic()
        if now - self._last_refresh < self._refresh_interval_sec:
            return
        async with self._refresh_lock:
            now = time.monotonic()
            if now - self._last_refresh < self._refresh_interval_sec:
                return
            try:
                cfg = await self._api.get_access_config()
            except ApiError as exc:
                # 失败时**不要**推进 _last_refresh，否则 15s 内不会再试，
                # 期间 admin 关 bot_enabled / 改白名单都拿不到。保留旧值
                # 让下一次事件立即重试。
                logger.warning("access-config refresh failed: %s", exc)
                return
            self._bot_enabled = bool(cfg.get("bot_enabled", True))
            self._allowed_user_ids = _parse_allowed_ids(
                str(cfg.get("allowed_user_ids") or "")
            )
            self._last_refresh = now

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        msg: Message | None = None
        cb: CallbackQuery | None = None
        if isinstance(event, Message):
            msg = event
        elif isinstance(event, CallbackQuery):
            cb = event
            msg = event.message if isinstance(event.message, Message) else None

        # chat.type 校验：只放行 private
        chat = msg.chat if msg is not None else None
        if chat is None:
            # 没 chat 上下文的 update（比如 inline_query）一律不处理；但 callback
            # 还是要 answer() 一下，否则 TG 客户端 spinner 不消失。
            if cb is not None:
                try:
                    await cb.answer()
                except Exception:  # noqa: BLE001
                    pass
            return
        if chat.type != "private":
            logger.info(
                "rejecting non-private chat: type=%s chat_id=%s", chat.type, chat.id
            )
            if cb is not None:
                await cb.answer("仅支持私聊使用。", show_alert=True)
            return

        await self._refresh_access_config()
        if not self._bot_enabled:
            if cb is not None:
                await cb.answer("机器人已暂停。", show_alert=True)
            elif msg is not None:
                # message 路径也要给反馈，否则用户发完消息以为没收到
                try:
                    await msg.answer("机器人已暂停。")
                except Exception:  # noqa: BLE001
                    pass
            return

        # user_id 白名单
        # 注意：callback_query 必须用 cb.from_user（真正点按钮的人），不要用
        # cb.message.from_user —— 后者是消息发送者，对 bot 自己发的消息来说就是 bot 自己。
        if cb is not None:
            from_user = cb.from_user
        else:
            from_user = msg.from_user if msg is not None else None
        if self._allowed_user_ids:
            uid = from_user.id if from_user else None
            if uid not in self._allowed_user_ids:
                logger.info(
                    "rejecting non-allowlisted user_id=%s chat_id=%s", uid, chat.id
                )
                if cb is not None:
                    await cb.answer("没有权限。", show_alert=True)
                return

        return await handler(event, data)
