"""Transport Pydantic contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

# ---------- Worker payload (XADD into Redis Stream) ----------


class TaskQueueItem(BaseModel):
    """Worker 从 queue:generations / queue:completions 读取的最小 payload。"""

    task_id: str
    kind: Literal["generation", "completion"]
    user_id: str


# ---------- SSE envelopes ----------


class SSEEvent(BaseModel):
    event: str  # 事件名，见 constants.EV_*
    data: dict[str, Any]
    id: str | None = None  # Last-Event-ID


# ---------- Errors ----------


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    retry_after_ms: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


__all__ = [
    "TaskQueueItem",
    "SSEEvent",
    "ErrorBody",
    "ErrorResponse",
]
