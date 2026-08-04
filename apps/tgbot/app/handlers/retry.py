"""重试回调：retry:<gen_id>。

读原 generation 的全套参数（API 已返回 aspect_ratio/size_requested/render_quality/
output_format/fast），按相同参数重新提交。count 默认 1（用户想多张走 /new）。
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery
from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS

from ..api_client import ApiError, LumenApi, make_idempotency_key
from ..tracker import TaskTrack, tracker
from ._helpers import require_message, resolution_from_size, telegram_user_id

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("retry:"))
async def on_retry(cb: CallbackQuery, api: LumenApi) -> None:
    gen_id = (cb.data or "").split(":", 1)[1] if cb.data else ""
    if not gen_id:
        await cb.answer()
        return
    msg = await require_message(cb)
    if msg is None:
        return
    actor_id = telegram_user_id(cb)
    if actor_id is None:
        await cb.answer("无法确认 Telegram 用户身份", show_alert=True)
        return

    # 同一任务第二次点击 retry：幂等键按 (chat, gen) 固定,服务端只会回放
    # 第一次提交的任务、不会新建 —— 提前查标记,明确告知而不是静默返回旧任务
    # (或重复输出误导性的「已排队 #B」)。
    try:
        prior_new_gen = await tracker.retry_source_new_gen("retry", msg.chat.id, gen_id)
    except Exception as exc:  # noqa: BLE001
        # Redis 不可用：宁可放行也不要卡死用户;服务端幂等键仍是第二道防线。
        logger.warning("retry marker lookup failed gen=%s err=%r", gen_id, exc)
        prior_new_gen = None
    if prior_new_gen:
        await cb.answer(
            f"该任务已重试过(新任务 #{prior_new_gen[:8]} 已创建),不会重复创建新任务。",
            show_alert=True,
        )
        return

    try:
        gen = await api.get_generation(
            msg.chat.id,
            gen_id,
            tg_user_id=actor_id,
        )
    except ApiError as exc:
        await cb.answer(f"读取原任务失败：{exc.message}", show_alert=True)
        return

    prompt = gen.get("prompt") or ""
    if not prompt:
        await cb.answer("原任务没有提示词，无法重试。", show_alert=True)
        return

    payload = {
        # 种子里不要拌 cb.id —— Telegram 每次点同一按钮 cb.id 都不同，会让
        # 服务端 idempotency 去重失效（双击/网络重发都建任务）。用稳定 (chat,
        # gen) 作为种子，重复点击就是同一 key。
        "idempotency_key": make_idempotency_key("retry", msg.chat.id, gen_id),
        "prompt": prompt,
        "aspect_ratio": gen.get("aspect_ratio") or "1:1",
        "render_quality": gen.get("render_quality") or "high",
        "count": 1,  # 单图重试；多图走 /new
        "resolution": resolution_from_size(gen.get("size_requested") or ""),
        "output_format": gen.get("output_format") or "jpeg",
        "fast": bool(gen.get("fast", False)),
        # API 端有参考图数量上限；老 gen 可能存了更多，截断避免 422。
        "attachment_image_ids": list(gen.get("input_image_ids") or [])[
            :MAX_MESSAGE_ATTACHMENTS
        ],
    }

    try:
        result = await api.create_generation(
            msg.chat.id,
            payload,
            tg_user_id=actor_id,
        )
    except ApiError as exc:
        await cb.answer(f"重试提交失败：{exc.message}", show_alert=True)
        return

    new_ids = result.get("generation_ids") or []
    user_id = str(result.get("user_id") or "")
    if not new_ids:
        await cb.answer("提交成功但没有 generation_id 返回。", show_alert=True)
        return

    # 提交成功后直接删原失败提示，避免会话里堆一堆 ❌；删失败（>48h 等）回退去按钮
    try:
        await msg.delete()
    except Exception:  # noqa: BLE001
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass

    new_gen = new_ids[0]
    try:
        await tracker.mark_retry_submitted("retry", msg.chat.id, gen_id, new_gen)
    except Exception as exc:  # noqa: BLE001
        # 标记写失败不阻塞提交;只是下一次重复点击会退回「回放旧任务」的旧行为。
        logger.warning("retry marker write failed gen=%s err=%r", new_gen, exc)
    status = await msg.answer(
        f"⏳ 重试已排队 #{new_gen[:8]}\n\n📝 {prompt[:200]}",
    )
    try:
        await tracker.add(
            new_gen,
            TaskTrack(
                chat_id=msg.chat.id,
                tg_user_id=actor_id,
                status_message_id=status.message_id,
                prompt=prompt,
                params={k: v for k, v in payload.items() if k != "idempotency_key"},
                user_id=user_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracker registration failed gen=%s err=%r", new_gen, exc)
        await msg.answer("⚠️ 任务已创建，但通知追踪失败；请用 /tasks 查看结果。")
    await cb.answer("已提交")
