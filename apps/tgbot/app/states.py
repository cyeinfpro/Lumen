"""FSM 状态定义。"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class GenFlow(StatesGroup):
    """配置参数 → 等待提示词 → 提交。"""

    configuring = State()  # 在主菜单选参数（aspect/quality/count/resolution）
    awaiting_prompt = State()  # 用户按了「开始生成」，等输入提示词
    confirming_enhanced = State()  # 用户开了 enhance 开关，等他选用/改/原文/取消
    editing_enhanced = State()  # 用户点了「✏️ 修改」，等他重新发送编辑后的提示词
    iterating = State()  # 用户点了「✏️ 迭代」，等他发新提示词（基于上一张图的图生图）
