"""接收 prompt → 调 API → 注册 tracker。

两条入口：
- GenFlow.awaiting_prompt：用户发完 prompt 文本后落点。
  - params.enhance=False → 直接 submit
  - params.enhance=True  → 调 enhance，进入 confirming_enhanced，让用户在「优化版/原文」
    之间选；选择后由下面的 callback_query handler 落点 submit。
- enh:* 回调：confirming_enhanced 状态下的二选一。
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..api_client import ApiError, LumenApi
from ..keyboards import DEFAULT_PARAMS, enhance_choice_keyboard, render_params_summary
from ..states import GenFlow
from ..tracker import TaskTrack, tracker

logger = logging.getLogger(__name__)
router = Router()


async def _submit_generation(
    chat_id: int,
    prompt: str,
    params: dict[str, object],
    api: LumenApi,
    answer,  # callable(text: str) -> Awaitable[Message]
) -> None:
    """把 (prompt, params) 提交到 API 并注册 tracker。

    count==1：一条状态消息，listener 走单图编辑流。
    count>1：一条 placeholder 罗列所有 #短ID，所有 gens 共享同一 status_message_id +
            batch_id；listener 不刷状态，终态事件 DECR batch 计数，归零才删除 placeholder。
    """
    payload = {
        "prompt": prompt,
        "aspect_ratio": params["aspect_ratio"],
        "render_quality": params["render_quality"],
        "count": params["count"],
        "resolution": params["resolution"],
        "output_format": params["output_format"],
        "fast": bool(params.get("fast")),
    }
    try:
        result = await api.create_generation(chat_id, payload)
    except ApiError as exc:
        await answer(f"❌ 提交失败：{exc.message}")
        return

    gen_ids = result.get("generation_ids") or []
    if not gen_ids:
        await answer("⚠️ 任务创建成功但没有返回 generation_id，请联系管理员。")
        return

    summary = render_params_summary(params)
    short_ids = " ".join(f"#{g[:8]}" for g in gen_ids)
    if len(gen_ids) == 1:
        status_text = (
            f"⏳ 任务已排队 #{gen_ids[0][:8]}\n\n{summary}\n\n📝 {prompt[:200]}"
        )
        batch_id = ""
    else:
        status_text = (
            f"⏳ 已派发 {len(gen_ids)} 个任务  {short_ids}\n\n"
            f"{summary}\n\n📝 {prompt[:200]}\n\n"
            f"完成的图会逐张推送，全部完成后此消息会自动消失。"
        )
        batch_id = gen_ids[0]

    status = await answer(status_text)
    if batch_id:
        await tracker.init_batch(batch_id, len(gen_ids))
    for gen_id in gen_ids:
        await tracker.add(
            gen_id,
            TaskTrack(
                chat_id=chat_id,
                status_message_id=status.message_id,
                prompt=prompt,
                params=params,
                batch_id=batch_id,
            ),
        )


@router.message(GenFlow.awaiting_prompt)
async def on_prompt(message: Message, state: FSMContext, api: LumenApi) -> None:
    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("prompt 不能为空，请重新发送。")
        return
    if len(prompt) > 5000:
        await message.answer("prompt 太长（>5000 字），请精简后重发。")
        return

    data = await state.get_data()
    params = dict(data.get("params") or DEFAULT_PARAMS)

    if params.get("enhance"):
        notice = await message.answer("✨ 正在优化提示词…")
        try:
            enhanced = await api.enhance_prompt(message.chat.id, prompt)
        except ApiError as exc:
            logger.warning("enhance failed user=%s err=%s", message.chat.id, exc)
            await notice.delete()
            await message.answer(f"⚠️ 优化失败（{exc.message}），已用原 prompt 继续。")
            await _submit_generation(message.chat.id, prompt, params, api, message.answer)
            await state.clear()
            return
        await state.set_state(GenFlow.confirming_enhanced)
        await state.update_data(
            params=params, original_prompt=prompt, enhanced_prompt=enhanced
        )
        await notice.delete()
        await message.answer(
            f"✨ 优化后：\n\n{enhanced[:3500]}",
            reply_markup=enhance_choice_keyboard(),
        )
        return

    await _submit_generation(message.chat.id, prompt, params, api, message.answer)
    await state.clear()


@router.callback_query(GenFlow.confirming_enhanced, F.data.startswith("enh:"))
async def on_enhance_choice(cb: CallbackQuery, state: FSMContext, api: LumenApi) -> None:
    choice = (cb.data or "").split(":", 1)[1] if cb.data else ""
    data = await state.get_data()
    original = str(data.get("original_prompt") or "")
    enhanced = str(data.get("enhanced_prompt") or "")
    params = dict(data.get("params") or DEFAULT_PARAMS)

    if choice == "cancel":
        await state.clear()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await cb.answer("已取消")
        return

    if choice == "edit":
        # 进入手动编辑：先把按钮去掉避免重复点；再发一条单独的 message 让用户复制
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await state.set_state(GenFlow.editing_enhanced)
        # 单独一条只含优化文本的消息，方便长按 → 复制
        if enhanced:
            await cb.message.answer(enhanced)
        await cb.message.answer(
            "✏️ 把改好的 prompt 发回来。\n（直接发送一条新消息即可；/cancel 放弃）"
        )
        await cb.answer()
        return

    prompt = enhanced if choice == "use" else original
    if not prompt:
        await cb.answer("会话已失效，/new 重开", show_alert=True)
        await state.clear()
        return

    # 去掉按钮避免重复点击
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass

    await _submit_generation(
        cb.message.chat.id, prompt, params, api, cb.message.answer
    )
    await state.clear()
    await cb.answer("已提交")


@router.message(GenFlow.editing_enhanced)
async def on_edited_prompt(message: Message, state: FSMContext, api: LumenApi) -> None:
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("已放弃。/new 重新开始。")
        return
    if not text:
        await message.answer("prompt 不能为空，重新发送一条；/cancel 放弃。")
        return
    if len(text) > 5000:
        await message.answer("prompt 太长（>5000 字），请精简后重发。")
        return
    data = await state.get_data()
    params = dict(data.get("params") or DEFAULT_PARAMS)
    await _submit_generation(message.chat.id, text, params, api, message.answer)
    await state.clear()
