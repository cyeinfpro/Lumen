"""成功生成后的二级操作：

- redo:<gen_id>  —— 用相同提示词 + 相同参数重画一张
- iter:<gen_id>  —— 把这张图当参考图，等用户输入新提示词走图生图
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..api_client import ApiError, LumenApi, make_idempotency_key
from ..states import GenFlow
from ..tracker import TaskTrack, tracker
from ._helpers import message_prompt, require_message, resolution_from_size

logger = logging.getLogger(__name__)
router = Router()


def _payload_from_gen(gen: dict, prompt: str, attachment_ids: list[str] | None = None) -> dict:
    """根据 get_generation 返回构造一个 create_generation payload。"""
    return {
        "prompt": prompt,
        "aspect_ratio": gen.get("aspect_ratio") or "1:1",
        "render_quality": gen.get("render_quality") or "high",
        "count": 1,
        "resolution": resolution_from_size(gen.get("size_requested") or ""),
        "output_format": gen.get("output_format") or "jpeg",
        "fast": bool(gen.get("fast", False)),
        "attachment_image_ids": list(attachment_ids or []),
    }


# ---------- redo ----------


@router.callback_query(F.data.startswith("redo:"))
async def on_redo(cb: CallbackQuery, api: LumenApi) -> None:
    gen_id = (cb.data or "").split(":", 1)[1] if cb.data else ""
    if not gen_id:
        await cb.answer()
        return
    msg = await require_message(cb)
    if msg is None:
        return
    try:
        gen = await api.get_generation(msg.chat.id, gen_id)
    except ApiError as exc:
        await cb.answer(f"读取原任务失败：{exc.message}", show_alert=True)
        return
    prompt = gen.get("prompt") or ""
    if not prompt:
        await cb.answer("原任务没有提示词，无法重画。", show_alert=True)
        return

    payload = _payload_from_gen(gen, prompt)
    # 注意：种子里不要拌 cb.id —— Telegram 每次点同一按钮 cb.id 都不同，会
    # 让服务端 idempotency 去重失效（双击/网络重发都建任务）。用稳定 (chat,
    # gen) 作为种子，重复点击就是同一 key。
    payload["idempotency_key"] = make_idempotency_key(
        "redo", msg.chat.id, gen_id
    )
    try:
        result = await api.create_generation(msg.chat.id, payload)
    except ApiError as exc:
        await cb.answer(f"重画提交失败：{exc.message}", show_alert=True)
        return
    new_ids = result.get("generation_ids") or []
    if not new_ids:
        await cb.answer("提交成功但没有 generation_id 返回。", show_alert=True)
        return

    new_gen = new_ids[0]
    status = await msg.answer(
        f"⏳ 重画已排队 #{new_gen[:8]}\n\n📝 {prompt[:200]}"
    )
    try:
        await tracker.add(
            new_gen,
            TaskTrack(
                chat_id=msg.chat.id,
                status_message_id=status.message_id,
                prompt=prompt,
                params={k: v for k, v in payload.items() if k != "idempotency_key"},
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracker registration failed gen=%s err=%r", new_gen, exc)
        await msg.answer("⚠️ 任务已创建，但通知追踪失败；请用 /tasks 查看结果。")
    await cb.answer("已提交")


# ---------- iter ----------


@router.callback_query(F.data.startswith("iter:"))
async def on_iter_start(cb: CallbackQuery, state: FSMContext, api: LumenApi) -> None:
    gen_id = (cb.data or "").split(":", 1)[1] if cb.data else ""
    if not gen_id:
        await cb.answer()
        return
    msg = await require_message(cb)
    if msg is None:
        return
    try:
        gen = await api.get_generation(msg.chat.id, gen_id)
    except ApiError as exc:
        await cb.answer(f"读取原任务失败：{exc.message}", show_alert=True)
        return
    image_ids = gen.get("image_ids") or []
    if not image_ids:
        await cb.answer("原任务没有图片，无法迭代。", show_alert=True)
        return

    # 拿第一张作为迭代 base；本系统每个 gen 只产一张，多于一张是 dual_race bonus 场景。
    source_image_id = str(image_ids[0])
    await state.set_state(GenFlow.iterating)
    await state.update_data(
        source_gen_id=gen_id,
        source_image_id=source_image_id,
        source_aspect_ratio=gen.get("aspect_ratio") or "1:1",
        source_size_requested=gen.get("size_requested") or "",
        source_render_quality=gen.get("render_quality") or "high",
        source_output_format=gen.get("output_format") or "jpeg",
        source_fast=bool(gen.get("fast", False)),
    )
    await msg.answer(
        "✏️ 迭代模式：发送你的修改指令（例如「换成蓝色背景」「让头发更长」）。\n"
        "新图会以上面这张图为基础重绘。\n/cancel 放弃。"
    )
    await cb.answer()


@router.message(GenFlow.iterating)
async def on_iter_prompt(message: Message, state: FSMContext, api: LumenApi) -> None:
    text = message_prompt(message)
    if text == "/cancel":
        await state.clear()
        await message.answer("已取消迭代。")
        return
    if not text:
        await message.answer("迭代指令不能为空。/cancel 放弃。")
        return
    if len(text) > 5000:
        await message.answer("指令太长（>5000 字），请精简后重发。")
        return

    data = await state.get_data()
    image_id = str(data.get("source_image_id") or "")
    if not image_id:
        await state.clear()
        await message.answer("会话状态丢失，/new 重开。")
        return

    payload = {
        "idempotency_key": make_idempotency_key(
            "iter", message.chat.id, message.message_id, image_id
        ),
        "prompt": text,
        "aspect_ratio": data.get("source_aspect_ratio") or "1:1",
        "render_quality": data.get("source_render_quality") or "high",
        "count": 1,
        "resolution": resolution_from_size(str(data.get("source_size_requested") or "")),
        "output_format": data.get("source_output_format") or "jpeg",
        "fast": bool(data.get("source_fast", False)),
        "attachment_image_ids": [image_id],
    }
    try:
        result = await api.create_generation(message.chat.id, payload)
    except ApiError as exc:
        await state.clear()
        await message.answer(f"❌ 迭代提交失败：{exc.message}")
        return
    new_ids = result.get("generation_ids") or []
    if not new_ids:
        await state.clear()
        await message.answer("⚠️ 提交成功但没有 generation_id 返回。")
        return

    new_gen = new_ids[0]
    status = await message.answer(
        f"⏳ 迭代已排队 #{new_gen[:8]}\n\n📝 {text[:200]}"
    )
    try:
        await tracker.add(
            new_gen,
            TaskTrack(
                chat_id=message.chat.id,
                status_message_id=status.message_id,
                prompt=text,
                params={k: v for k, v in payload.items() if k != "idempotency_key"},
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracker registration failed gen=%s err=%r", new_gen, exc)
        await message.answer("⚠️ 任务已创建，但通知追踪失败；请用 /tasks 查看结果。")
    await state.clear()
