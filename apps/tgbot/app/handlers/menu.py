"""主菜单：/new + cfg:* 回调。

State 机：用户在 /new 后进入 GenFlow.configuring，回调里改参数 + 实时 redraw 菜单。
点 「开始生成」 → 进入 GenFlow.awaiting_prompt → 由 generation.py 接管 text 输入。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..keyboards import (
    ASPECT_RATIOS,
    COUNT_LABELS,
    DEFAULT_PARAMS,
    FORMAT_LABELS,
    QUALITY_LABELS,
    RESOLUTION_LABELS,
    main_menu,
    render_params_summary,
)
from ..generation_state import new_generation_flow_epoch
from ..states import GenFlow
from ._helpers import require_message

router = Router()

_BOOL_VALUES = frozenset({"true", "false"})
# 只读白名单：用 MappingProxyType 而不是裸 dict —— 模块级可变状态是架构门禁
# （scripts/check_architecture.py 的 module-mutable-state）明确禁止的，这里
# 本来也不需要可变。
_ALLOWED_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "aspect_ratio": frozenset(value for _, value in ASPECT_RATIOS),
        "render_quality": frozenset(value for _, value in QUALITY_LABELS),
        "count": frozenset(str(value) for _, value in COUNT_LABELS),
        "resolution": frozenset(value for _, value in RESOLUTION_LABELS),
        "output_format": frozenset(value for _, value in FORMAT_LABELS),
        "fast": _BOOL_VALUES,
        "enhance": _BOOL_VALUES,
    }
)


class _GenerationRuntime(Protocol):
    async def cancel_prompt(self, chat_id: int) -> None: ...


def _coerce(field: str, value: str) -> object | None:
    """把回调里的字符串转成参数值；不在白名单内返回 None。

    J-2：callback_data 不是可信输入 —— 历史消息里的按钮、被转发的键盘、
    直接调 Bot API 伪造的 callback 都会打到这里。原实现对 count 直接
    int(value)，`cfg:count:abc` 会抛 ValueError 把 handler 整个打断，
    连 cb.answer() 都发不出去，用户端表现为按钮永远转圈。
    既然要防，就把所有字段一起收进白名单（也顺带挡住 cfg:resolution:8k
    这类会一路带到 API 才 422 的越界值）。
    """
    allowed = _ALLOWED_VALUES.get(field)
    if allowed is None or value not in allowed:
        return None
    if field == "count":
        return int(value)
    if field in ("fast", "enhance"):
        return value == "true"
    return value


@router.message(Command("new"))
async def cmd_new(
    message: Message,
    state: FSMContext,
    generation_runtime: _GenerationRuntime,
) -> None:
    data = await state.get_data()
    params = dict(data.get("params") or DEFAULT_PARAMS)
    flow_epoch = new_generation_flow_epoch()
    await generation_runtime.cancel_prompt(message.chat.id)
    await state.clear()
    await state.set_state(GenFlow.configuring)
    await state.update_data(params=params, generation_flow_epoch=flow_epoch)
    await message.answer(
        f"生成参数\n{render_params_summary(params)}\n\n"
        "选好后点「开始生成」，再发送你的提示词。",
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
            f"📝 现在发送你的提示词（中英文均可）。\n\n{render_params_summary(params)}"
        )
        await cb.answer("等待提示词…")
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
    coerced = _coerce(field, raw_value)
    if coerced is None:
        await cb.answer("这个选项已失效，请用 /new 重新开始。", show_alert=True)
        return
    params[field] = coerced
    await state.update_data(params=params)
    try:
        await msg.edit_text(
            f"生成参数\n{render_params_summary(params)}\n\n"
            "选好后点「开始生成」，再发送你的提示词。",
            reply_markup=main_menu(params),
        )
    except Exception:  # noqa: BLE001
        # 内容相同会报错，忽略
        pass
    await cb.answer()
