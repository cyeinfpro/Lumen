"""尺寸解析器 Python 版——与 apps/web/src/lib/sizing.ts 行为一致。

DESIGN §7.2 + 附录 A。Worker 组装上游 body 前调用；API 校验 image_params 时也调用。

两层策略（4K 升级后）：
- **默认（auto / fixed=空）**：沿用保守的 ~1.57M `PIXEL_BUDGET` 推导 preset，控制延迟与成本。
- **显式 fixed_size**：按上游 gpt-image-2 真实能力校验（16 对齐 / 最长边 ≤3840 /
  总像素 ∈ [655360, 8294400] / 长宽比 ≤3:1）；非法时 raise ValueError 而非静默回退。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .constants import (
    EXPLICIT_ALIGN,
    MAX_EXPLICIT_ASPECT,
    MAX_EXPLICIT_PIXELS,
    MAX_EXPLICIT_SIDE,
    MIN_EXPLICIT_PIXELS,
    PIXEL_BUDGET,
)

AspectRatio = Literal[
    "1:1",
    "16:9", "9:16",
    "21:9", "9:21",
    "4:5",  # 兼容历史；UI 不再单独露出
    "3:4", "4:3",
    "3:2", "2:3",
]
SizeMode = Literal["auto", "fixed"]

# 默认 preset：按用户要求"默认最大 4K 画质 + 按比例分配"。
# 每条均满足 validate_explicit_size：16 对齐、最长边 ≤ 3840、总像素 ≤ 8,294,400、长宽比 ≤ 3:1。
# 横/竖构图配对：3:2↔2:3 / 4:3↔3:4 / 16:9↔9:16 / 21:9↔9:21
_PRESET: dict[str, tuple[int, int]] = {
    "1:1":  (2880, 2880),  # 8,294,400
    "16:9": (3840, 2160),  # 8,294,400
    "9:16": (2160, 3840),
    "21:9": (3808, 1632),  # 6,214,656（21:9 = 2.333…，已取最接近的 16 对齐）
    "9:21": (1632, 3808),
    "4:5":  (2560, 3200),  # 8,192,000
    "3:4":  (2448, 3264),  # 7,989,072
    "4:3":  (3264, 2448),
    "3:2":  (3504, 2336),  # 8,185,344
    "2:3":  (2336, 3504),
}

_RATIO_MAP: dict[str, tuple[int, int]] = {
    "1:1": (1, 1),
    "16:9": (16, 9),
    "9:16": (9, 16),
    "21:9": (21, 9),
    "9:21": (9, 21),
    "4:5": (4, 5),
    "3:4": (3, 4),
    "4:3": (4, 3),
    "3:2": (3, 2),
    "2:3": (2, 3),
}


@dataclass(frozen=True)
class ResolvedSize:
    size: str  # "auto" 或 "{W}x{H}"
    width: int | None
    height: int | None
    prompt_suffix: str  # size=auto 时追加到 prompt 末尾


def ratio_instruction(aspect: AspectRatio) -> str:
    return f" Preserve a strict {aspect} composition."


def validate_explicit_size(w: int, h: int) -> None:
    """校验显式 fixed_size 是否满足上游 gpt-image-2 的约束；不合法抛 ValueError。

    非法边界清单：
    - 非正数、非 16 对齐
    - 最长边 > MAX_EXPLICIT_SIDE（3840）
    - 总像素不在 [MIN_EXPLICIT_PIXELS, MAX_EXPLICIT_PIXELS] = [655360, 8294400]
    - 长宽比 > MAX_EXPLICIT_ASPECT（3:1）
    """
    if w <= 0 or h <= 0:
        raise ValueError(f"size must be positive, got {w}x{h}")
    if w % EXPLICIT_ALIGN or h % EXPLICIT_ALIGN:
        raise ValueError(
            f"size must be multiple of {EXPLICIT_ALIGN}, got {w}x{h}"
        )
    longest = max(w, h)
    if longest > MAX_EXPLICIT_SIDE:
        raise ValueError(
            f"longest side must be <= {MAX_EXPLICIT_SIDE}, got {longest}"
        )
    px = w * h
    if px < MIN_EXPLICIT_PIXELS or px > MAX_EXPLICIT_PIXELS:
        raise ValueError(
            f"total pixels must be in [{MIN_EXPLICIT_PIXELS}, {MAX_EXPLICIT_PIXELS}], got {px}"
        )
    ratio = longest / min(w, h)
    if ratio > MAX_EXPLICIT_ASPECT:
        raise ValueError(
            f"aspect ratio must be <= {MAX_EXPLICIT_ASPECT}:1, got {ratio:.3f}"
        )


def resolve_size(
    aspect: AspectRatio,
    mode: SizeMode,
    fixed: str | None = None,
    budget: int = PIXEL_BUDGET,
) -> ResolvedSize:
    """根据比例 / 模式 / 可选固定尺寸，产出最终 size 字段 + prompt 后缀。

    - `budget` 仅用于 auto / preset 回退路径；不参与 fixed 校验
    - `fixed` 非法时抛 ValueError；调用方（routes/messages.py）应捕获转 422
    """
    if mode == "auto":
        return ResolvedSize(
            size="auto", width=None, height=None, prompt_suffix=ratio_instruction(aspect)
        )

    # fixed 模式：显式尺寸按上游真实能力校验，允许 4K 等大图直通
    if fixed:
        if not _looks_like_size(fixed):
            raise ValueError(f"invalid fixed_size format: {fixed!r}")
        w, h = map(int, fixed.split("x"))
        validate_explicit_size(w, h)  # 非法抛 ValueError
        return ResolvedSize(size=f"{w}x{h}", width=w, height=h, prompt_suffix="")

    # fixed 为空：回退到 aspect preset（供 worker 把旧 size_requested="auto" 再次 resolve 时使用）
    w, h = _PRESET.get(aspect) or _fallback_by_budget(aspect, budget)
    return ResolvedSize(size=f"{w}x{h}", width=w, height=h, prompt_suffix="")


def _looks_like_size(s: str) -> bool:
    if "x" not in s:
        return False
    a, _, b = s.partition("x")
    return a.isdigit() and b.isdigit()


def _fallback_by_budget(aspect: AspectRatio, budget: int) -> tuple[int, int]:
    min_aligned_px = 16 * 16
    if budget < min_aligned_px:
        raise ValueError(f"budget too small (got {budget}, need >= {min_aligned_px})")
    rw, rh = _RATIO_MAP[aspect]
    raw_w = math.sqrt((budget * rw) / rh)
    raw_h = math.sqrt((budget * rh) / rw)

    def aligned_near(value: float) -> set[int]:
        floor = max(16, (int(value) // 16) * 16)
        ceil = max(16, ((math.ceil(value) + 15) // 16) * 16)
        values = {floor, ceil, 16}
        for base in (floor, ceil):
            for delta in range(-64, 80, 16):
                candidate = base + delta
                if candidate >= 16:
                    values.add(candidate)
        return values

    candidates: set[tuple[int, int]] = set()
    max_w_at_min_h = max(16, ((budget // 16) // 16) * 16)
    max_h_at_min_w = max(16, ((budget // 16) // 16) * 16)
    width_seeds = aligned_near(raw_w) | {max_w_at_min_h}
    height_seeds = aligned_near(raw_h) | {max_h_at_min_w}

    def add_candidate(w: int, h: int) -> None:
        if (
            w >= 16
            and h >= 16
            and w % 16 == 0
            and h % 16 == 0
            and w * h <= budget
        ):
            candidates.add((w, h))

    for w in width_seeds:
        max_h = max(16, ((budget // w) // 16) * 16)
        for h in aligned_near((w * rh) / rw) | {max_h}:
            add_candidate(w, h)

    for h in height_seeds:
        max_w = max(16, ((budget // h) // 16) * 16)
        for w in aligned_near((h * rw) / rh) | {max_w}:
            add_candidate(w, h)

    if not candidates:
        raise ValueError(f"budget too small (got {budget}, need >= {min_aligned_px})")

    target_ratio = rw / rh
    w, h = min(
        candidates,
        key=lambda item: (
            abs(math.log((item[0] / item[1]) / target_ratio))
            + 0.1 * (1 - (item[0] * item[1] / budget)),
            -(item[0] * item[1]),
        ),
    )
    assert w >= 16 and h >= 16 and w * h <= budget
    return w, h
