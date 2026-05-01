"""意图路由（DESIGN §22.1）。

V1 决策：删掉 auto 启发式
- 旧启发式靠关键词表 + 附件判断，命中率低且误判重（"给我一张海报"就落 chat）。
- 现在由前端显式选 chat / image，附件存在时自动派生 vision_qa / image_to_image。
- 仍接受 `intent="auto"` 以兼容旧客户端 —— 按 chat 语义兜底。
- `None` / 未知 intent 显式报错，避免静默落到 chat。
"""

from __future__ import annotations

from lumen_core.constants import Intent


def resolve_intent(
    explicit: str | None,
    text: str,  # noqa: ARG001  保留参数签名以兼容调用方
    has_attachment: bool,
) -> Intent:
    """Return a concrete Intent given client-declared intent + attachment context."""
    if explicit is None:
        raise ValueError("intent is required")

    explicit = explicit.strip()
    if explicit == "text_to_image":
        return Intent.TEXT_TO_IMAGE
    if explicit == "image_to_image":
        return Intent.IMAGE_TO_IMAGE
    if explicit == "vision_qa":
        return Intent.VISION_QA
    if explicit in {"auto", "chat"}:
        return Intent.VISION_QA if has_attachment else Intent.CHAT

    raise ValueError(f"unsupported intent: {explicit!r}")
