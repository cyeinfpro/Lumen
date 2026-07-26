"""Cross-service request correlation (request_id).

H-19：image-job 是独立 sidecar，出问题时只能靠 job_id 在本服务内部翻日志，
调用方（apps/worker）那侧的 generation_id / task_id 与这里对不上，跨服务定位
只能靠时间戳猜。这里引入一条贯穿链路的 request_id：

1. 入站中间件优先复用调用方带来的 ``X-Request-Id``，没有就本地生成一个；
2. 存进 ContextVar，同一请求内任何位置都能取到，不用把参数一路往下传；
3. 响应回写同名头，调用方拿到后可以直接把两侧日志串起来；
4. 提交任务时落进 jobs.request_id 列——异步 worker 是在另一个协程、另一个
   时刻跑的，ContextVar 早就不在了，只有落库才能把「提交请求」和「后台执行」
   连起来。
"""

from __future__ import annotations

import secrets
from contextvars import ContextVar, Token


REQUEST_ID_HEADER = "x-request-id"

# 调用方可能传来任意长度/任意字符的头，落库和进日志前必须收敛。
MAX_REQUEST_ID_CHARS = 64

_REQUEST_ID: ContextVar[str] = ContextVar("image_job_request_id", default="")


def new_request_id() -> str:
    return f"ij_{secrets.token_hex(8)}"


def sanitize_request_id(value: str | None) -> str:
    """只保留可安全进日志的可见 ASCII，防止换行注入伪造日志行。"""
    if not value:
        return ""
    cleaned = "".join(
        char
        for char in value.strip()[:MAX_REQUEST_ID_CHARS]
        if char.isascii() and char.isprintable() and char not in {" ", '"'}
    )
    return cleaned


def bind_request_id(value: str | None) -> Token[str]:
    return _REQUEST_ID.set(sanitize_request_id(value) or new_request_id())


def reset_request_id(token: Token[str]) -> None:
    _REQUEST_ID.reset(token)


def current_request_id() -> str:
    return _REQUEST_ID.get()


__all__ = [
    "MAX_REQUEST_ID_CHARS",
    "REQUEST_ID_HEADER",
    "bind_request_id",
    "current_request_id",
    "new_request_id",
    "reset_request_id",
    "sanitize_request_id",
]
