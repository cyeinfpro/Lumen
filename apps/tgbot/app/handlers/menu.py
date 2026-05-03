"""主菜单：/new + cfg:* 回调。

State 机：用户在 /new 后进入 GenFlow.configuring，回调里改参数 + 实时 redraw 菜单。
点 「开始生成」 → 进入 GenFlow.awaiting_prompt → 由 generation.py 接管 text 输入。
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..keyboards import DEFAULT_PARAMS, main_menu, render_params_summary
from ..states import GenFlow
from ._helpers import require_message

router = Router()


def _coerce(field: str, value: str) -> object:
    if field == "count":
        return int(value)
    if field in ("fast", "enhance"):
        return value == "true"
    return value


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    params = dict(data.get("params") or DEFAULT_PARAMS)
    await state.set_state(GenFlow.configuring)
    await state.update_data(params=params)
    await message.answer(
        f"生成参数\n{render_params_summary(params)}\n\n"
        "选好后点「开始生成」，再发送你的 prompt。",
        reply_markup=main_menu(params),
    )


@router.callback_query(F.data.startswith("cfg:"))
async def on_cfg(cb: CallbackQuery, state: FSMContext) -> None:
    parts = (cb.data or "").split(":", 2)
    if len(parts) < 2:
        await cb.answer()
        return
    msg = await require_message(cb)
    if msg is None:
        return
    action = parts[1]
    data = await state.get_data()
    params = dict(data.get("params") or DEFAULT_PARAMS)

    if action == "start":
        await state.set_state(GenFlow.awaiting_prompt)
        await state.update_data(params=params)
        await msg.edit_text(
            f"📝 现在发送你的 prompt（中英文均可）。\n\n{render_params_summary(params)}"
        )
        await cb.answer("等待 prompt…")
        return

    if action == "cancel":
        await state.clear()
        await msg.edit_text("已取消。/new 重新开始。")
        await cb.answer()
        return

    # 切参数：cfg:<field>:<value>
    if len(parts) != 3:
        await cb.answer()
        return
    field, raw_value = parts[1], parts[2]
    if field not in params:
        await cb.answer()
        return
    params[field] = _coerce(field, raw_value)
    await state.update_data(params=params)
    try:
        await msg.edit_text(
            f"生成参数\n{render_params_summary(params)}\n\n"
            "选好后点「开始生成」，再发送你的 prompt。",
            reply_markup=main_menu(params),
        )
    except Exception:  # noqa: BLE001
        # 内容相同会报错，忽略
        pass
    await cb.answer()
