"""inline keyboards。

callback_data 严格 ≤ 64 字节（TG 限制）。本文件全部 callback_data 用紧凑前缀：
  cfg:<field>:<value>     — 切换某项配置
  cfg:start                — 提交，进入 awaiting_prompt
  cfg:cancel               — 退出菜单
  retry:<gen_id>           — 重试指定生成（gen_id 是 uuid7 字符串 36 字节）
  redo:<gen_id>            — 重画指定生成
  iter:<gen_id>            — 以指定生成迭代
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger(__name__)

_CALLBACK_DATA_MAX_BYTES = 64


# UI 选项 → API 值的映射
ASPECT_RATIOS: list[tuple[str, str]] = [
    ("1:1", "1:1"),
    ("16:9", "16:9"),
    ("9:16", "9:16"),
    ("4:3", "4:3"),
    ("3:4", "3:4"),
    ("21:9", "21:9"),
]

QUALITY_LABELS: list[tuple[str, str]] = [
    ("低", "low"),
    ("中", "medium"),
    ("高", "high"),
]

COUNT_LABELS: list[tuple[str, int]] = [("1", 1), ("2", 2), ("4", 4), ("16", 16)]

RESOLUTION_LABELS: list[tuple[str, str]] = [
    ("1K", "1k"),
    ("2K", "2k"),
    ("4K", "4k"),
]

FORMAT_LABELS: list[tuple[str, str]] = [
    ("PNG 格式", "png"),
    ("JPG 格式", "jpeg"),
]


DEFAULT_PARAMS: dict[str, object] = {
    "aspect_ratio": "1:1",
    "render_quality": "high",
    "count": 1,
    "resolution": "2k",
    "output_format": "jpeg",
    "fast": True,
    "enhance": False,  # 提交 prompt 后先调 /telegram/prompts/enhance 让你选用优化版/手改后的版本/原文
}


def _generation_callback_data(action: str, gen_id: str) -> str | None:
    data = f"{action}:{gen_id}"
    data_len = len(data.encode("utf-8"))
    if data_len <= _CALLBACK_DATA_MAX_BYTES:
        return data
    logger.warning(
        "telegram callback_data too long action=%s bytes=%d limit=%d",
        action,
        data_len,
        _CALLBACK_DATA_MAX_BYTES,
    )
    return None


def _row(builder: InlineKeyboardBuilder, label: str, options: list[tuple[str, object]], current: object, field: str) -> None:
    for text, value in options:
        prefix = "✅ " if value == current else ""
        builder.button(text=f"{prefix}{text}", callback_data=f"cfg:{field}:{value}")


def main_menu(params: dict[str, object]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # 比例
    for text, aspect_value in ASPECT_RATIOS:
        prefix = "✅ " if params["aspect_ratio"] == aspect_value else ""
        b.button(
            text=f"{prefix}{text}",
            callback_data=f"cfg:aspect_ratio:{aspect_value}",
        )
    b.adjust(3, 3)

    b2 = InlineKeyboardBuilder()
    for text, quality_value in QUALITY_LABELS:
        prefix = "✅ " if params["render_quality"] == quality_value else ""
        b2.button(
            text=f"{prefix}质量·{text}",
            callback_data=f"cfg:render_quality:{quality_value}",
        )
    b2.adjust(3)

    b3 = InlineKeyboardBuilder()
    for text, count_value in COUNT_LABELS:
        prefix = "✅ " if params["count"] == count_value else ""
        b3.button(
            text=f"{prefix}×{text}",
            callback_data=f"cfg:count:{count_value}",
        )
    b3.adjust(4)

    b4 = InlineKeyboardBuilder()
    for text, resolution_value in RESOLUTION_LABELS:
        prefix = "✅ " if params["resolution"] == resolution_value else ""
        b4.button(
            text=f"{prefix}{text}",
            callback_data=f"cfg:resolution:{resolution_value}",
        )
    b4.adjust(3)

    b_fmt = InlineKeyboardBuilder()
    for text, value in FORMAT_LABELS:
        prefix = "✅ " if params["output_format"] == value else ""
        b_fmt.button(text=f"{prefix}{text}", callback_data=f"cfg:output_format:{value}")
    b_fmt.adjust(2)

    b_fast = InlineKeyboardBuilder()
    fast_on = bool(params.get("fast"))
    enh_on = bool(params.get("enhance"))
    b_fast.button(
        text=f"⚡ 快速：{'开' if fast_on else '关'}",
        callback_data=f"cfg:fast:{'false' if fast_on else 'true'}",
    )
    b_fast.button(
        text=f"✨ 提示词优化：{'开' if enh_on else '关'}",
        callback_data=f"cfg:enhance:{'false' if enh_on else 'true'}",
    )
    b_fast.adjust(2)

    b5 = InlineKeyboardBuilder()
    b5.button(text="🚀 开始生成", callback_data="cfg:start")
    b5.button(text="✖ 取消", callback_data="cfg:cancel")
    b5.adjust(2)

    rows = b.as_markup().inline_keyboard
    rows += b2.as_markup().inline_keyboard
    rows += b3.as_markup().inline_keyboard
    rows += b4.as_markup().inline_keyboard
    rows += b_fmt.as_markup().inline_keyboard
    rows += b_fast.as_markup().inline_keyboard
    rows += b5.as_markup().inline_keyboard
    return InlineKeyboardMarkup(inline_keyboard=rows)


def retry_keyboard(gen_id: str) -> InlineKeyboardMarkup | None:
    data = _generation_callback_data("retry", gen_id)
    if data is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 重试", callback_data=data)]
        ]
    )


def render_params_summary(params: dict[str, object]) -> str:
    tags = []
    if params.get("fast"):
        tags.append("⚡ 快速")
    if params.get("enhance"):
        tags.append("✨ 优化")
    tail = ("  ·  " + "  ·  ".join(tags)) if tags else ""
    fmt = str(params.get("output_format") or "jpeg")
    fmt_label = "JPG 格式" if fmt == "jpeg" else "PNG 格式"
    return (
        f"📐 比例 {params['aspect_ratio']}  ·  "
        f"🎨 质量 {params['render_quality']}  ·  "
        f"🔢 张数 {params['count']}  ·  "
        f"🖼 分辨率 {str(params['resolution']).upper()}  ·  "
        f"📦 {fmt_label}"
        f"{tail}"
    )


def post_success_keyboard(
    gen_id: str,
    *,
    web_url: str | None = None,
    project_url: str | None = None,
) -> InlineKeyboardMarkup | None:
    """成功生成后的操作面板：重画、迭代、回 Web 继续整理。"""
    rows: list[list[InlineKeyboardButton]] = []
    action_row: list[InlineKeyboardButton] = []
    redo_data = _generation_callback_data("redo", gen_id)
    iter_data = _generation_callback_data("iter", gen_id)
    if redo_data is not None:
        action_row.append(InlineKeyboardButton(text="🔁 重画", callback_data=redo_data))
    if iter_data is not None:
        action_row.append(InlineKeyboardButton(text="✏️ 迭代", callback_data=iter_data))
    if action_row:
        rows.append(action_row)
    link_row: list[InlineKeyboardButton] = []
    safe_web_url = _https_url_or_none(web_url)
    safe_project_url = _https_url_or_none(project_url)
    if safe_web_url:
        link_row.append(InlineKeyboardButton(text="Web 编辑", url=safe_web_url))
    if safe_project_url:
        link_row.append(InlineKeyboardButton(text="加入项目", url=safe_project_url))
    if link_row:
        rows.append(link_row)
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def _https_url_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return value


def enhance_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✨ 使用此版", callback_data="enh:use"),
                InlineKeyboardButton(text="✏️ 修改", callback_data="enh:edit"),
            ],
            [
                InlineKeyboardButton(text="📝 用原文", callback_data="enh:orig"),
                InlineKeyboardButton(text="✖ 取消", callback_data="enh:cancel"),
            ],
        ]
    )
