"""/start [code] 命令。

- /start         → 引导用户去 web 生成绑定码
- /start <code>  → 调 API /telegram/bind 完成绑定
- /help          → 简介
- /unbind        → 解绑（带确认按钮）

所有消息都不开 parse_mode：display_name / email / API 错误信息可能含 * _ ` [
等 Markdown 控制字符，开了会让 sendMessage 直接 400。
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..api_client import ApiError, LumenApi
from ._helpers import require_message

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, api: LumenApi) -> None:
    code = (command.args or "").strip()
    chat_id = message.chat.id
    username = message.from_user.username if message.from_user else None

    if code:
        try:
            info = await api.bind(chat_id=chat_id, code=code, tg_username=username)
        except ApiError as exc:
            await message.answer(
                f"❌ 绑定失败：{exc.message}\n\n"
                "请回到 Lumen 网站重新生成绑定码后再试。"
            )
            return
        await message.answer(
            f"✅ 已绑定到账号 {info['display_name'] or info['email']}\n\n"
            "发送 /new 进入生成菜单。"
        )
        return

    # 已绑定？
    try:
        info = await api.me(chat_id)
        await message.answer(
            f"👋 欢迎回来，{info['display_name'] or info['email']}\n\n"
            "/new — 配置参数并生成图片\n"
            "/tasks 或 /list — 查看最近任务\n"
            "/unbind — 解除绑定"
        )
    except ApiError:
        await message.answer(
            "👋 你好！这里是 Lumen AI 绘图。\n\n"
            "首次使用需要先绑定账号：\n"
            "1. 登录 Lumen 网站，打开「设置 → Telegram」生成绑定码\n"
            "2. 在这里发送 /start <code> 完成绑定"
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "命令列表\n\n"
        "/new — 进入生成菜单（选比例/质量/张数/分辨率，再发送 prompt）\n"
        "/tasks 或 /list — 最近 10 个任务\n"
        "/unbind — 解除当前账号绑定\n"
        "/start <code> — 用网站给的绑定码绑定"
    )


def _unbind_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ 确认解绑", callback_data="unbind:confirm"),
                InlineKeyboardButton(text="✖ 取消", callback_data="unbind:cancel"),
            ]
        ]
    )


@router.message(Command("unbind"))
async def cmd_unbind(message: Message) -> None:
    await message.answer(
        "确认解绑当前 TG 账号？解绑后此 chat 将无法继续使用 bot，需要重新跑绑定流程。",
        reply_markup=_unbind_confirm_keyboard(),
    )


@router.callback_query(F.data.startswith("unbind:"))
async def on_unbind_choice(cb: CallbackQuery, api: LumenApi) -> None:
    choice = (cb.data or "").split(":", 1)[1] if cb.data else ""
    msg = await require_message(cb)
    if msg is None:
        return
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    if choice != "confirm":
        await cb.answer("已取消")
        return
    try:
        await api.unbind(msg.chat.id)
    except ApiError as exc:
        await msg.answer(f"解绑失败：{exc.message}")
        await cb.answer()
        return
    await msg.answer("✅ 已解绑。再次绑定请用 /start <code>")
    await cb.answer()
