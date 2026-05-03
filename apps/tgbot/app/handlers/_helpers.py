"""共享的 callback_query 守护工具。

aiogram 3 的 `CallbackQuery.message` 在原始消息超过 48h（Telegram 限制）后会变成
`InaccessibleMessage` 或直接为 `None`。任何裸 `cb.message.xxx` 调用都会 AttributeError，
让 handler 抛 BLE001 让用户看到 "Invalid request"。

`require_message` 统一守护：拿不到可用的 Message 就给用户温和提示并返回 None，
caller 直接 `if msg is None: return`。
"""

from __future__ import annotations

from aiogram.types import CallbackQuery, Message


async def require_message(cb: CallbackQuery) -> Message | None:
    """返回 cb.message（若可用），否则 ack 提示后返回 None。

    Telegram 协议：>48h 老消息无法被 bot 编辑/回复；aiogram 把它建模成
    InaccessibleMessage（Message 的子类，但拿不到 chat 等关键字段）或 None。
    任何依赖 chat_id / answer / edit 的 handler 都应在入口先过这层。
    """
    msg = cb.message
    if msg is None or not isinstance(msg, Message):
        await cb.answer("消息已过期，请重新发起", show_alert=True)
        return None
    return msg
