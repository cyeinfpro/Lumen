"""重试回调：retry:<gen_id>。

读原 generation 的全套参数（API 已返回 aspect_ratio/size_requested/render_quality/
output_format/fast），按相同参数重新提交。count 默认 1（用户想多张走 /new）。
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from ..api_client import ApiError, LumenApi, make_idempotency_key
from ..tracker import TaskTrack, tracker
from ._helpers import require_message, resolution_from_size

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

    try:
        gen = await api.get_generation(msg.chat.id, gen_id)
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
        "idempotency_key": make_idempotency_key(
            "retry", msg.chat.id, gen_id
        ),
        "prompt": prompt,
        "aspect_ratio": gen.get("aspect_ratio") or "1:1",
        "render_quality": gen.get("render_quality") or "high",
        "count": 1,  # 单图重试；多图走 /new
        "resolution": resolution_from_size(gen.get("size_requested") or ""),
        "output_format": gen.get("output_format") or "jpeg",
        "fast": bool(gen.get("fast", False)),
        # API 端 max_length=4；老 gen 可能存了 >4 条，截到 4 避免 422
        "attachment_image_ids": list(gen.get("input_image_ids") or [])[:4],
    }

    try:
        result = await api.create_generation(msg.chat.id, payload)
    except ApiError as exc:
        await cb.answer(f"重试提交失败：{exc.message}", show_alert=True)
        return

    new_ids = result.get("generation_ids") or []
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
    status = await msg.answer(
        f"⏳ 重试已排队 #{new_gen[:8]}\n\n📝 {prompt[:200]}",
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
