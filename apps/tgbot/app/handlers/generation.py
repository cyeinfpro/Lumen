"""接收提示词 → 调 API → 注册 tracker。

两条入口：
- GenFlow.awaiting_prompt：用户发完提示词文本后落点。
  - params.enhance=False → 直接 submit
  - params.enhance=True  → 调 enhance，进入 confirming_enhanced，让用户在「优化版/原文」
    之间选；选择后由下面的 callback_query handler 落点 submit。
- enh:* 回调：confirming_enhanced 状态下的二选一。
"""

from __future__ import annotations

import logging
import asyncio
import contextlib

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..api_client import ApiError, LumenApi, make_idempotency_key
from ..keyboards import DEFAULT_PARAMS, enhance_choice_keyboard, render_params_summary
from ..states import GenFlow
from ..tracker import TaskTrack, tracker
from ._helpers import is_slash_command, message_prompt, require_message

logger = logging.getLogger(__name__)
router = Router()


# 已为某 chat_id 记录过 token-revoked 的 warning，避免心跳每 4s 刷一条
_HEARTBEAT_AUTH_LOGGED: set[int] = set()


async def _chat_action_heartbeat(message: Message, action: ChatAction) -> None:
    while True:
        try:
            await message.bot.send_chat_action(message.chat.id, action)
        except (TelegramUnauthorizedError, TelegramForbiddenError) as exc:
            # 401/403：bot token 失效或被踢出 chat。整个进程内 per-chat 只 warn 一次。
            if message.chat.id not in _HEARTBEAT_AUTH_LOGGED:
                _HEARTBEAT_AUTH_LOGGED.add(message.chat.id)
                logger.warning(
                    "chat_action heartbeat auth failed chat=%s err=%r",
                    message.chat.id,
                    exc,
                )
        except Exception:  # noqa: BLE001
            # 其它异常按原行为吞掉（network blip / RetryAfter 等）
            pass
        await asyncio.sleep(4.0)


async def _submit_generation(
    chat_id: int,
    prompt: str,
    params: dict[str, object],
    api: LumenApi,
    answer,  # callable(text: str) -> Awaitable[Message]
    idempotency_key: str,
) -> None:
    """把 (提示词, params) 提交到 API 并注册 tracker。

    count==1：一条状态消息，listener 走单图编辑流。
    count>1：一条 placeholder 罗列所有 #短ID，所有 gens 共享同一 status_message_id +
            batch_id；listener 不刷状态，终态事件 DECR batch 计数，归零才删除 placeholder。
    """
    payload = {
        "idempotency_key": idempotency_key,
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
    try:
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracker registration failed ids=%s err=%r", gen_ids, exc)
        await answer("⚠️ 任务已创建，但通知追踪失败；请用 /tasks 查看结果。")


@router.message(GenFlow.awaiting_prompt)
async def on_prompt(message: Message, state: FSMContext, api: LumenApi) -> None:
    prompt = message_prompt(message)
    if prompt == "/cancel":
        await state.clear()
        await message.answer("已取消。/new 重新开始。")
        return
    if is_slash_command(prompt):
        await message.answer("当前正在等待提示词。请发送普通文本，或先 /cancel 再执行命令。")
        return
    if not prompt:
        await message.answer("提示词不能为空，请重新发送。")
        return
    if len(prompt) > 5000:
        await message.answer("提示词太长（>5000 字），请精简后重发。")
        return

    data = await state.get_data()
    params = dict(data.get("params") or DEFAULT_PARAMS)
    idempotency_key = make_idempotency_key(
        "prompt", message.chat.id, message.message_id
    )

    if params.get("enhance"):
        notice = await message.answer("✨ 正在优化提示词…")
        heartbeat = asyncio.create_task(
            _chat_action_heartbeat(message, ChatAction.TYPING)
        )
        try:
            enhanced = await api.enhance_prompt(message.chat.id, prompt)
        except ApiError as exc:
            logger.warning("enhance failed user=%s err=%s", message.chat.id, exc)
            await notice.delete()
            await message.answer(f"⚠️ 优化失败（{exc.message}），已用原提示词继续。")
            await _submit_generation(
                message.chat.id, prompt, params, api, message.answer, idempotency_key
            )
            await state.clear()
            return
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await state.set_state(GenFlow.confirming_enhanced)
        await state.update_data(
            params=params,
            original_prompt=prompt,
            enhanced_prompt=enhanced,
            idempotency_key=idempotency_key,
        )
        await notice.delete()
        await message.answer(
            f"✨ 优化后：\n\n{enhanced[:3500]}",
            reply_markup=enhance_choice_keyboard(),
        )
        return

    await _submit_generation(
        message.chat.id, prompt, params, api, message.answer, idempotency_key
    )
    await state.clear()


@router.callback_query(GenFlow.confirming_enhanced, F.data.startswith("enh:"))
async def on_enhance_choice(cb: CallbackQuery, state: FSMContext, api: LumenApi) -> None:
    choice = (cb.data or "").split(":", 1)[1] if cb.data else ""
    msg = await require_message(cb)
    if msg is None:
        await state.clear()
        return
    data = await state.get_data()
    original = str(data.get("original_prompt") or "")
    enhanced = str(data.get("enhanced_prompt") or "")
    params = dict(data.get("params") or DEFAULT_PARAMS)
    idempotency_key = str(
        data.get("idempotency_key")
        or make_idempotency_key("enhance", msg.chat.id, cb.id)
    )

    if choice == "cancel":
        await state.clear()
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await cb.answer("已取消")
        return

    if choice == "edit":
        # 进入手动编辑：先把按钮去掉避免重复点；再发一条单独的 message 让用户复制
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await state.set_state(GenFlow.editing_enhanced)
        # 单独一条只含优化文本的消息，方便长按 → 复制
        if enhanced:
            await msg.answer(enhanced)
        await msg.answer(
            "✏️ 把改好的提示词发回来。\n（直接发送一条新消息即可；/cancel 放弃）"
        )
        await cb.answer()
        return

    if choice == "use":
        prompt = enhanced
    elif choice == "orig":
        prompt = original
    else:
        await cb.answer("无效选择，请重新发起。", show_alert=True)
        return
    if not prompt:
        await cb.answer("会话已失效，/new 重开", show_alert=True)
        await state.clear()
        return

    # 去掉按钮避免重复点击
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass

    await _submit_generation(
        msg.chat.id, prompt, params, api, msg.answer, idempotency_key
    )
    await state.clear()
    await cb.answer("已提交")


@router.message(GenFlow.editing_enhanced)
async def on_edited_prompt(message: Message, state: FSMContext, api: LumenApi) -> None:
    text = message_prompt(message)
    if text == "/cancel":
        await state.clear()
        await message.answer("已放弃。/new 重新开始。")
        return
    if is_slash_command(text):
        await message.answer("当前正在等待改好的提示词。请发送普通文本，或 /cancel 放弃。")
        return
    if not text:
        await message.answer("提示词不能为空，重新发送一条；/cancel 放弃。")
        return
    if len(text) > 5000:
        await message.answer("提示词太长（>5000 字），请精简后重发。")
        return
    data = await state.get_data()
    params = dict(data.get("params") or DEFAULT_PARAMS)
    await _submit_generation(
        message.chat.id,
        text,
        params,
        api,
        message.answer,
        make_idempotency_key("edit-enhanced", message.chat.id, message.message_id),
    )
    await state.clear()
