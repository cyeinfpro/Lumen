"""/tasks 与 /list — 列出最近 10 个任务。

每行显示：状态图标 #短ID 比例·分辨率 prompt 截断。
- 成功行附「📥 取图 #xxx」按钮：重启或漏推时也能拉回原图
- 失败行附「🔄 重试 #xxx」按钮
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..api_client import ApiError, LumenApi

logger = logging.getLogger(__name__)
router = Router()

_STATUS_ICON = {
    "queued": "⏳",
    "running": "⚙️",
    "succeeded": "✅",
    "failed": "❌",
    "canceled": "🚫",
}


@router.message(Command("tasks", "list"))
async def cmd_tasks(message: Message, api: LumenApi) -> None:
    try:
        data = await api.list_tasks(message.chat.id, limit=10)
    except ApiError as exc:
        await message.answer(f"读取任务列表失败：{exc.message}")
        return
    items = data.get("items") or []
    if not items:
        await message.answer("还没有任务。/new 开始第一个吧。")
        return

    lines: list[str] = ["最近任务", ""]
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for it in items:
        status = it.get("status") or ""
        icon = _STATUS_ICON.get(status, "•")
        gid = (it.get("id") or "")[:8]
        ar = it.get("aspect_ratio") or ""
        size = it.get("size_requested") or ""
        excerpt = it.get("prompt_excerpt") or ""
        lines.append(f"{icon} #{gid}  {ar} {size}")
        if excerpt:
            lines.append(f"   {excerpt}")
        if status == "succeeded" and it.get("image_ids"):
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📥 取图 #{gid}",
                        callback_data=f"task:send:{it['id']}",
                    )
                ]
            )
        elif status == "failed":
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🔄 重试 #{gid}",
                        callback_data=f"retry:{it['id']}",
                    )
                ]
            )

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    await message.answer("\n".join(lines), reply_markup=markup)


@router.callback_query(F.data.startswith("task:send:"))
async def on_task_send(cb: CallbackQuery, api: LumenApi) -> None:
    """重新发送某成功任务的所有图片。"""
    parts = (cb.data or "").split(":", 2)
    if len(parts) != 3:
        await cb.answer()
        return
    gen_id = parts[2]
    try:
        gen = await api.get_generation(cb.message.chat.id, gen_id)
    except ApiError as exc:
        await cb.answer(f"读取任务失败：{exc.message}", show_alert=True)
        return

    image_ids = gen.get("image_ids") or []
    if not image_ids:
        await cb.answer("这个任务没有可下载的图片。", show_alert=True)
        return
    prompt = (gen.get("prompt") or "")[:800]
    await cb.answer("下载中…")

    downloads: list[tuple[Path, str, int, str]] = []
    try:
        for idx, image_id in enumerate(image_ids):
            try:
                path, mime, size = await api.download_image_to_file(
                    cb.message.chat.id, image_id
                )
            except ApiError as exc:
                logger.warning("task send: download failed gen=%s img=%s err=%s", gen_id, image_id, exc)
                continue
            ext = "png" if "png" in mime else ("webp" if "webp" in mime else "jpg")
            filename = f"{gen_id[:8]}-{idx + 1}.{ext}"
            downloads.append((path, mime, size, filename))

        caption = f"📂 来自任务 #{gen_id[:8]}\n\n📝 {prompt}"
        # 一律 sendDocument：sendPhoto 会强制 1280px + JPEG 重编码。
        sent_first = False
        for idx, (path, _mime, _size, filename) in enumerate(downloads):
            try:
                await cb.message.answer_document(
                    document=FSInputFile(str(path), filename=filename),
                    caption=caption if idx == 0 else None,
                )
                sent_first = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("task send: document failed gen=%s err=%r", gen_id, exc)
        if not sent_first:
            await cb.message.answer("⚠️ 全部图片下载失败，请稍后再试。")
    finally:
        for path, *_ in downloads:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
