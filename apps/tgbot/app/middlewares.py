"""bot 入口中间件：在任何 handler 跑之前做"是不是该让你进来"的过滤。

两层防护：
1. 仅放行 chat.type == "private"（拒群聊 / 频道；防群里加 bot 后 chat_id 被绑导致
   群成员越权）
2. 若配置了 TELEGRAM_ALLOWED_USER_IDS 白名单，from_user.id 必须命中
   （和 telegram_bindings 形成双因子；token 泄漏时仍受 TG 账号 id 约束）
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .config import settings

logger = logging.getLogger(__name__)


def _parse_allowed_ids() -> set[int]:
    raw = settings.telegram_allowed_user_ids or ""
    out: set[int] = set()
    for piece in raw.split(","):
        s = piece.strip()
        if not s:
            continue
        try:
            out.add(int(s))
        except ValueError:
            logger.warning("ignoring non-integer allowed user id: %r", s)
    return out


_ALLOWED_USER_IDS: set[int] = _parse_allowed_ids()


class AccessGate(BaseMiddleware):
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
            return  # 没 chat 上下文的 update（比如 inline_query）一律不处理
        if chat.type != "private":
            logger.info(
                "rejecting non-private chat: type=%s chat_id=%s", chat.type, chat.id
            )
            if cb is not None:
                await cb.answer("仅支持私聊使用。", show_alert=True)
            return

        # user_id 白名单
        # 注意：callback_query 必须用 cb.from_user（真正点按钮的人），不要用
        # cb.message.from_user —— 后者是消息发送者，对 bot 自己发的消息来说就是 bot 自己。
        if cb is not None:
            from_user = cb.from_user
        else:
            from_user = msg.from_user if msg is not None else None
        if _ALLOWED_USER_IDS:
            uid = from_user.id if from_user else None
            if uid not in _ALLOWED_USER_IDS:
                logger.info(
                    "rejecting non-allowlisted user_id=%s chat_id=%s", uid, chat.id
                )
                if cb is not None:
                    await cb.answer("没有权限。", show_alert=True)
                return

        return await handler(event, data)
