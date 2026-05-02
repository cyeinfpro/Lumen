"""Completion Worker——DESIGN §6.5.a + §22.2 + §22.8。

`run_completion(ctx, task_id)` 是 arq 任务入口。流程概览：

1. 幂等读 Completion；终态直接 return
2. UPDATE status=streaming, started_at, attempt++
3. 读同会话最近 N=20 条消息 → 转成 input 列表（§22.2）
4. 组 body（按用户开关可挂 web_search tool）+ stream=True
5. 消费 SSE，每次 delta 累加、每 N 个 token 落一次 PG、publish delta
6. `response.completed` → 记录 tokens，status=succeeded；publish succeeded
7. 失败：按 §6.4 分类重试；超限 → failed
8. 流中断恢复：arq retry 策略会让另一 Worker 重跑；重跑时清空 text 并 publish restarted
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import httpx
from PIL import Image as PILImage
from sqlalchemy import and_, desc, or_, select, text as sa_text, update

from lumen_core.constants import (
    DEFAULT_CHAT_INSTRUCTIONS,
    DEFAULT_CHAT_MODEL,
    EV_COMP_DELTA,
    EV_COMP_FAILED,
    EV_COMP_IMAGE,
    EV_COMP_PROGRESS,
    EV_COMP_RESTARTED,
    EV_COMP_STARTED,
    EV_COMP_SUCCEEDED,
    EV_COMP_THINKING_DELTA,
    CompletionStage,
    CompletionStatus,
    GenerationErrorCode as EC,
    ImageSource,
    MessageStatus,
    RETRY_BACKOFF_SECONDS,
    Role,
    task_channel,
)
from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    HISTORY_FETCH_BATCH,
    IMAGE_INPUT_ESTIMATED_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    compose_summary_guardrail,
    count_tokens,
    estimate_summary_tokens,
    estimate_system_prompt_tokens,
    format_sticky_input_text,
    format_summary_input_text,
    get_input_budget,
    is_summary_usable,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Image,
    ImageVariant,
    Message,
    new_uuid7,
)

from .. import runtime_settings
from ..config import settings
from ..db import SessionLocal
from ..observability import (
    get_tracer,
    safe_outcome,
    task_duration_seconds,
    upstream_calls_total,
)
from ..retry import RetryDecision, is_retriable
from ..sse_publish import publish_event
from ..storage import storage
from ..upstream import UpstreamCancelled, UpstreamError, stream_completion
from ..upstream import (
    _extract_response_image_b64,
    _extract_response_revised_prompt,
)
from .state import is_completion_terminal

from .generation import (
    _cleanup_storage_on_error,
    _compute_blurhash,
    _make_display,
    _make_preview,
    _make_thumb,
    _sha256,
    _write_generation_files,
)

logger = logging.getLogger(__name__)
_tracer = get_tracer("lumen.worker.completion")

try:
    from . import context_summary
except Exception:  # noqa: BLE001
    context_summary = None  # type: ignore[assignment]


_LEASE_TTL_S = 300
_LEASE_RENEW_S = 30
_MAX_ATTEMPTS = 3
_PG_FLUSH_EVERY_CHARS = 128  # 每累计 ~128 字符 flush 一次到 PG
_PG_FLUSH_RETRIES = 3
_PG_FLUSH_BACKOFF_S = 0.2
_CONTEXT_COMPRESSION_ENABLED_DEFAULT = 1
_CONTEXT_COMPRESSION_TRIGGER_PERCENT_DEFAULT = 80
_CONTEXT_SUMMARY_TARGET_TOKENS_DEFAULT = 1200
_CONTEXT_SUMMARY_MIN_RECENT_MESSAGES_DEFAULT = 16
_CONTEXT_SUMMARY_MIN_INTERVAL_SECONDS_DEFAULT = 30
_STICKY_TEXT_CHAR_LIMIT = 16_000
_WEB_SEARCH_TOOL_TYPE = "web_search"
_FILE_SEARCH_TOOL_TYPE = "file_search"
_CODE_INTERPRETER_TOOL_TYPE = "code_interpreter"
_IMAGE_GENERATION_TOOL_TYPE = "image_generation"
_CHAT_TOOL_VECTOR_STORE_SETTING = "chat.file_search_vector_store_ids"
_CHAT_IMAGE_TOOL_SIZE = "1024x1024"
_TOOL_RUNNING_STATUSES = frozenset({"queued", "running", "in_progress", "searching"})
_TOOL_SUCCEEDED_STATUSES = frozenset({"completed", "complete", "succeeded", "done"})
_TOOL_FAILED_STATUSES = frozenset({"failed", "incomplete", "error", "errored"})
# GEN-P1-4: 用户点取消后 API 在 Redis 设 task:{id}:cancel=1。worker 在 SSE 循环
# 里每隔若干次 delta 检查一次（控制 Redis 调用频率），命中后立即终止流并标 cancelled。
_CANCEL_CHECK_EVERY_DELTAS = 16


@dataclass(frozen=True)
class _SummaryBoundary:
    conversation_id: str
    up_to_message_id: str
    up_to_created_at: datetime
    first_user_message_id: str | None
    recent_message_ids: list[str]
    summary_message_ids: list[str]
    source_message_count: int
    source_token_estimate: int


@dataclass(frozen=True)
class PackedContext:
    input_list: list[dict[str, Any]]
    estimated_tokens: int
    summary_used: bool
    summary_created: bool
    summary_up_to_message_id: str | None
    sticky_used: bool
    included_messages_count: int
    truncated_without_summary: bool
    fallback_reason: str | None
    compression_enabled: bool = False
    recent_messages_count: int = 0
    summary_tokens: int = 0
    summary_age_seconds: int | None = None
    compressor_model: str | None = None
    image_caption_count: int = 0
    quality_probes: dict[str, Any] | None = None
    _system_prompt: str | None = None
    _sticky_message: Message | None = None
    _summary_text: str | None = None
    _recent_rows: tuple[Message, ...] = ()


class _TaskCancelled(RuntimeError):
    """GEN-P1-4: 用户取消信号——上层捕获走终态分支，不当作错误重试。"""


class _LeaseLost(UpstreamCancelled):
    """Lease renewer gave up; this worker must stop before another attempt runs."""


@dataclass(frozen=True)
class _ToolCallState:
    id: str
    type: str
    status: str
    name: str | None = None
    label: str | None = None
    title: str | None = None
    error: str | None = None

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "label": self.label or _tool_display_label(self.type, self.name),
        }
        if self.name:
            out["name"] = self.name
        if self.title:
            out["title"] = self.title
        if self.error:
            out["error"] = self.error
        return out


class _CompletionToolTracker:
    """Normalize Responses tool-call events and suppress duplicate progress.

    Responses streams can expose built-in tools in several shapes:
    tool-specific events (``response.web_search_call.searching``), generic
    output item events, and a final ``response.completed.response.output``
    snapshot.  This tracker makes those shapes idempotent for both SSE and DB
    content persistence.
    """

    def __init__(self) -> None:
        self._calls: dict[str, _ToolCallState] = {}
        self._last_published: dict[str, tuple[Any, ...]] = {}

    def update(self, event: dict[str, Any]) -> dict[str, Any] | None:
        update = _extract_tool_call_update(event)
        if update is None:
            return None
        call_id = update["id"]
        previous = self._calls.get(call_id)
        next_state = _merge_tool_call_state(previous, update)
        self._calls[call_id] = next_state
        signature = (
            next_state.type,
            next_state.status,
            next_state.name,
            next_state.label,
            next_state.title,
            next_state.error,
        )
        if self._last_published.get(call_id) == signature:
            return None
        self._last_published[call_id] = signature
        return next_state.payload()

    def update_from_response(self, response: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(response, dict):
            return []
        output = response.get("output")
        if not isinstance(output, list):
            return []
        published: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            event = {"type": "response.output_item.done", "item": item}
            payload = self.update(event)
            if payload is not None:
                published.append(payload)
        return published

    def content(self) -> list[dict[str, Any]]:
        return [state.payload() for state in self._calls.values()]


def _split_csv_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        value = part.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _content_str_list(content: dict[str, Any] | None, key: str) -> list[str]:
    raw = (content or {}).get(key)
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _normalize_reasoning_effort_for_upstream(
    effort: str | None,
) -> str | None:
    if effort == "minimal":
        # Newer GPT-5.x models use "none" for no reasoning; keep accepting
        # historical UI/API values while avoiding upstream 400s.
        return "none"
    return effort


async def _chat_tools_from_content(content: dict[str, Any] | None) -> list[dict[str, Any]]:
    content = content or {}
    tools: list[dict[str, Any]] = []
    if content.get("web_search") is True:
        tools.append({"type": _WEB_SEARCH_TOOL_TYPE})

    if content.get("file_search") is True:
        vector_store_ids = _content_str_list(content, "vector_store_ids")
        if not vector_store_ids:
            try:
                vector_store_ids = _split_csv_ids(
                    await runtime_settings.resolve(_CHAT_TOOL_VECTOR_STORE_SETTING)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("file_search vector store setting resolve failed: %s", exc)
                vector_store_ids = []
        if vector_store_ids:
            tools.append(
                {
                    "type": _FILE_SEARCH_TOOL_TYPE,
                    "vector_store_ids": vector_store_ids,
                }
            )
        else:
            logger.warning(
                "file_search requested without vector_store_ids; skipping tool"
            )

    if content.get("code_interpreter") is True:
        tools.append(
            {
                "type": _CODE_INTERPRETER_TOOL_TYPE,
                "container": {"type": "auto"},
            }
        )

    if content.get("image_generation") is True:
        tools.append(
            {
                "type": _IMAGE_GENERATION_TOOL_TYPE,
                "model": "gpt-image-2",
                "size": _CHAT_IMAGE_TOOL_SIZE,
                "quality": "medium",
                "output_format": "png",
                "background": "auto",
            }
        )

    return tools


def _configure_chat_tools(body: dict[str, Any], tools: list[dict[str, Any]]) -> None:
    if not tools:
        return
    body["tools"] = tools
    body["tool_choice"] = "auto"
    body["parallel_tool_calls"] = False


def _tool_display_label(tool_type: str, name: str | None = None) -> str:
    if tool_type == _WEB_SEARCH_TOOL_TYPE:
        return "联网搜索"
    if tool_type == _FILE_SEARCH_TOOL_TYPE:
        return "检索文件"
    if tool_type == _CODE_INTERPRETER_TOOL_TYPE:
        return "运行代码"
    if tool_type == _IMAGE_GENERATION_TOOL_TYPE:
        return "生成图片"
    if name:
        return f"调用{name}"
    return "调用工具"


def _normalize_tool_type(raw: Any, *, event_type: str = "") -> str | None:
    value = raw if isinstance(raw, str) else ""
    if value.endswith("_call"):
        value = value[: -len("_call")]
    if value in {
        _WEB_SEARCH_TOOL_TYPE,
        _FILE_SEARCH_TOOL_TYPE,
        _CODE_INTERPRETER_TOOL_TYPE,
        _IMAGE_GENERATION_TOOL_TYPE,
        "function",
        "tool",
    }:
        return value
    if value == "function_call":
        return "function"
    if value == "tool_call":
        return "tool"

    # Tool-specific Responses events are shaped like
    # response.web_search_call.searching / response.code_interpreter_call.completed.
    if event_type.startswith("response.") and "_call." in event_type:
        middle = event_type[len("response.") :].split(".", 1)[0]
        return _normalize_tool_type(middle, event_type="")
    return None


def _normalize_tool_status(raw: Any, *, event_type: str = "") -> str | None:
    value = str(raw).strip().lower() if raw is not None else ""
    if value in _TOOL_FAILED_STATUSES:
        return "failed"
    if value in _TOOL_SUCCEEDED_STATUSES:
        return "succeeded"
    if value == "queued":
        return "queued"
    if value in _TOOL_RUNNING_STATUSES:
        return "running"
    if event_type == "response.output_item.added":
        return "running"
    if event_type == "response.output_item.done":
        return "succeeded"
    if event_type.endswith((".failed", ".incomplete", ".error")):
        return "failed"
    if event_type.endswith((".completed", ".complete", ".done")):
        return "succeeded"
    if event_type.endswith(
        (".created", ".queued", ".in_progress", ".running", ".searching", ".interpreting")
    ):
        return "running"
    return None


def _tool_status_rank(status: str | None) -> int:
    return {
        "queued": 0,
        "running": 1,
        "succeeded": 2,
        "failed": 3,
    }.get(status or "", -1)


def _summarize_tool_error(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:500]
    if isinstance(value, dict):
        code = value.get("code") or value.get("type")
        message = value.get("message") or value.get("reason")
        if code and message:
            return f"{code}: {message}"[:500]
        if message:
            return str(message)[:500]
        if code:
            return str(code)[:500]
    return None


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_tool_call_update(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    event_type = event_type if isinstance(event_type, str) else ""
    item = event.get("item")
    call = event.get("tool_call") or event.get("call")
    source: dict[str, Any] | None = None
    if isinstance(item, dict):
        source = item
    elif isinstance(call, dict):
        source = call
    else:
        source = event

    tool_type = _normalize_tool_type(source.get("type"), event_type=event_type)
    if tool_type is None:
        tool_type = _normalize_tool_type(event.get("tool_type"), event_type=event_type)
    if tool_type is None:
        return None

    call_id = _first_str(
        source.get("id"),
        source.get("call_id"),
        source.get("tool_call_id"),
        event.get("item_id"),
        event.get("output_item_id"),
        event.get("call_id"),
        event.get("tool_call_id"),
        event.get("id"),
    )
    if call_id is None:
        return None

    name = _first_str(source.get("name"), event.get("name"))
    title = _first_str(
        source.get("query"),
        event.get("query"),
        source.get("title"),
        event.get("title"),
    )
    status = _normalize_tool_status(source.get("status"), event_type=event_type)
    error = _summarize_tool_error(
        source.get("error")
        or source.get("incomplete_details")
        or event.get("error")
        or event.get("incomplete_details")
    )
    if error and status is None:
        status = "failed"
    status = status or "running"

    return {
        "id": call_id,
        "type": tool_type,
        "status": status,
        "name": name,
        "title": title,
        "label": _tool_display_label(tool_type, name),
        "error": error,
    }


def _merge_tool_call_state(
    previous: _ToolCallState | None,
    update: dict[str, Any],
) -> _ToolCallState:
    next_status = update["status"]
    if (
        previous is not None
        and _tool_status_rank(previous.status) > _tool_status_rank(next_status)
    ):
        next_status = previous.status
    next_type = update["type"] or (previous.type if previous is not None else "tool")
    next_name = update.get("name") or (previous.name if previous is not None else None)
    next_label = update.get("label") or (
        previous.label if previous is not None else _tool_display_label(next_type, next_name)
    )
    return _ToolCallState(
        id=update["id"],
        type=next_type,
        status=next_status,
        name=next_name,
        label=next_label,
        title=update.get("title") or (previous.title if previous is not None else None),
        error=update.get("error") or (previous.error if previous is not None else None),
    )


async def _publish_completion_tool_progress(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    tool_call: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> None:
    await publish_event(
        redis,
        user_id,
        channel,
        EV_COMP_PROGRESS,
        {
            "completion_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "attempt_epoch": attempt_epoch,
            "stage": "tool_call",
            "tool_call": tool_call,
            "tool_calls": tool_calls,
        },
    )


def _markdown_link(label: str, url: str) -> str:
    safe_label = (label or url).replace("\\", "\\\\").replace("]", "\\]")
    safe_url = url.replace(")", "%29").replace(" ", "%20")
    return f"[{safe_label}]({safe_url})"


def _extract_url_citations(response: dict[str, Any]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    output = response.get("output")
    if not isinstance(output, list):
        return citations
    for item in output:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            for ann in part.get("annotations") or []:
                if not isinstance(ann, dict):
                    continue
                raw_citation = ann.get("url_citation")
                raw = raw_citation if isinstance(raw_citation, dict) else ann
                url = raw.get("url") if isinstance(raw, dict) else None
                if not isinstance(url, str) or not url.startswith(
                    ("http://", "https://")
                ):
                    continue
                title = raw.get("title") if isinstance(raw, dict) else None
                citations.append(
                    {
                        "url": url,
                        "title": title if isinstance(title, str) and title else url,
                        "text": text if isinstance(text, str) else None,
                        "start_index": ann.get("start_index"),
                        "end_index": ann.get("end_index"),
                    }
                )
    return citations


def _apply_url_citations(text: str, citations: list[dict[str, Any]]) -> str:
    if not text or not citations:
        return text
    replacements: list[tuple[int, int, str]] = []
    seen_urls: set[str] = set()
    for citation in citations:
        url = citation["url"]
        start = citation.get("start_index")
        end = citation.get("end_index")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if start < 0 or end <= start or end > len(text):
            continue
        label = text[start:end].strip()
        if not label:
            continue
        replacements.append((start, end, _markdown_link(label, url)))
        seen_urls.add(url)
    if replacements:
        # Apply from the end so earlier indexes remain valid.
        for start, end, link in sorted(
            replacements,
            key=lambda item: item[0],
            reverse=True,
        ):
            text = f"{text[:start]}{link}{text[end:]}"
    if not seen_urls:
        unique: list[dict[str, Any]] = []
        for citation in citations:
            if citation["url"] in seen_urls:
                continue
            seen_urls.add(citation["url"])
            unique.append(citation)
        if unique:
            links: list[str] = []
            for idx, citation in enumerate(unique[:8], start=1):
                label = str(citation.get("title") or citation["url"])
                links.append(f"{idx}. {_markdown_link(label, citation['url'])}")
            text = f"{text.rstrip()}\n\n来源\n" + "\n".join(links)
    return text


def _extract_completed_output_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    chunks: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                chunks.append(part["text"])
    return "".join(chunks)


def _finalize_completion_text(text: str, response: dict[str, Any] | None) -> str:
    if not isinstance(response, dict):
        return text
    completed_text = _extract_completed_output_text(response)
    base = completed_text or text
    return _apply_url_citations(base, _extract_url_citations(response))


_REASONING_DELTA_EVENT_TYPES = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.reasoning_summary.delta",
}


def _extract_reasoning_delta(event: dict[str, Any]) -> str:
    ev_type = event.get("type")
    if ev_type in _REASONING_DELTA_EVENT_TYPES:
        for key in ("delta", "text", "summary"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
    if ev_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            return _extract_reasoning_text_from_item(item)
    return ""


def _extract_reasoning_text_from_item(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("summary_text", "text"):
        value = item.get(key)
        if isinstance(value, str) and value:
            chunks.append(value)
    summary = item.get("summary")
    if isinstance(summary, str) and summary:
        chunks.append(summary)
    elif isinstance(summary, list):
        for part in summary:
            if isinstance(part, str) and part:
                chunks.append(part)
            elif isinstance(part, dict):
                for key in ("text", "summary_text"):
                    value = part.get(key)
                    if isinstance(value, str) and value:
                        chunks.append(value)
                        break
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                value = part.get("text")
                if isinstance(value, str) and value:
                    chunks.append(value)
    return "\n".join(chunks)


def _extract_reasoning_text_from_response(response: dict[str, Any] | None) -> str:
    if not isinstance(response, dict):
        return ""
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    chunks: list[str] = []
    for item in output:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            text = _extract_reasoning_text_from_item(item)
            if text:
                chunks.append(text)
    return "\n\n".join(chunks)


def _extract_image_events_from_response(response: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    output = response.get("output")
    if not isinstance(output, list):
        return []
    events: list[dict[str, Any]] = []
    for item in output:
        if isinstance(item, dict) and item.get("type") == _IMAGE_GENERATION_TOOL_TYPE + "_call":
            events.append({"type": "response.output_item.done", "item": item})
    return events


def _image_format_and_meta(raw_image: bytes) -> tuple[
    str,
    str,
    int,
    int,
    str | None,
    bytes,
    tuple[int, int],
    bytes,
    tuple[int, int],
    bytes,
    tuple[int, int],
]:
    try:
        with PILImage.open(io.BytesIO(raw_image)) as pil:
            pil.load()
            if pil.format not in ("PNG", "WEBP", "JPEG"):
                raise UpstreamError(
                    f"upstream returned unexpected image format: {pil.format}",
                    error_code=EC.BAD_RESPONSE.value,
                    status_code=200,
                )
            width, height = pil.size
            if width < 1 or height < 1 or width > 10000 or height > 10000:
                raise UpstreamError(
                    f"upstream image dimensions out of range: {width}x{height}",
                    error_code=EC.BAD_RESPONSE.value,
                    status_code=200,
                )
            blurhash_str = _compute_blurhash(pil)
            display_bytes, display_size = _make_display(pil)
            preview_bytes, preview_size = _make_preview(pil)
            thumb_bytes, thumb_size = _make_thumb(pil)
            orig_format = pil.format
    except UpstreamError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(
            f"pillow could not decode image: {exc}",
            error_code=EC.BAD_RESPONSE.value,
            status_code=200,
        ) from exc

    ext_by_format = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}
    mime_by_format = {
        "PNG": "image/png",
        "WEBP": "image/webp",
        "JPEG": "image/jpeg",
    }
    return (
        ext_by_format[orig_format],
        mime_by_format[orig_format],
        width,
        height,
        blurhash_str,
        display_bytes,
        display_size,
        preview_bytes,
        preview_size,
        thumb_bytes,
        thumb_size,
    )


async def _store_completion_tool_image(
    *,
    session: Any,
    task_id: str,
    user_id: str,
    message_id: str,
    raw_image: bytes,
    revised_prompt: str | None,
) -> dict[str, Any]:
    (
        orig_ext,
        orig_mime,
        width,
        height,
        blurhash_str,
        display_bytes,
        display_size,
        preview_bytes,
        preview_size,
        thumb_bytes,
        thumb_size,
    ) = _image_format_and_meta(raw_image)
    image_id = new_uuid7()
    sha = _sha256(raw_image)
    key_prefix = f"u/{user_id}/completion-tools/{task_id}/{image_id}"
    key_orig = f"{key_prefix}/orig.{orig_ext}"
    key_display = f"{key_prefix}/display2048.webp"
    key_preview = f"{key_prefix}/preview1024.webp"
    key_thumb = f"{key_prefix}/thumb256.jpg"

    created_storage_keys = await _write_generation_files(
        [
            (key_orig, raw_image),
            (key_display, display_bytes),
            (key_preview, preview_bytes),
            (key_thumb, thumb_bytes),
        ]
    )
    async with _cleanup_storage_on_error(created_storage_keys):
        img = Image(
            id=image_id,
            user_id=user_id,
            owner_generation_id=None,
            source=ImageSource.GENERATED,
            parent_image_id=None,
            storage_key=key_orig,
            mime=orig_mime,
            width=width,
            height=height,
            size_bytes=len(raw_image),
            sha256=sha,
            blurhash=blurhash_str,
            visibility="private",
            metadata_jsonb={
                "source": "completion_tool",
                "completion_id": task_id,
                **({"revised_prompt": revised_prompt} if revised_prompt else {}),
            },
        )
        session.add(img)
        session.add(
            ImageVariant(
                image_id=image_id,
                kind="display2048",
                storage_key=key_display,
                width=display_size[0],
                height=display_size[1],
            )
        )
        session.add(
            ImageVariant(
                image_id=image_id,
                kind="preview1024",
                storage_key=key_preview,
                width=preview_size[0],
                height=preview_size[1],
            )
        )
        session.add(
            ImageVariant(
                image_id=image_id,
                kind="thumb256",
                storage_key=key_thumb,
                width=thumb_size[0],
                height=thumb_size[1],
            )
        )

        msg = await session.get(Message, message_id)
        if msg is not None:
            content = dict(msg.content or {})
            images_list = list(content.get("images") or [])
            images_list.append(
                {
                    "image_id": image_id,
                    "from_completion_id": task_id,
                    "width": width,
                    "height": height,
                    "mime": orig_mime,
                    "url": storage.public_url(key_orig),
                    "display_url": f"/api/images/{image_id}/variants/display2048",
                    "preview_url": f"/api/images/{image_id}/variants/preview1024",
                    "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                    **({"revised_prompt": revised_prompt} if revised_prompt else {}),
                }
            )
            content["images"] = images_list
            msg.content = content

        return {
            "image_id": image_id,
            "from_completion_id": task_id,
            "actual_size": f"{width}x{height}",
            "mime": orig_mime,
            "url": storage.public_url(key_orig),
            "display_url": f"/api/images/{image_id}/variants/display2048",
            "preview_url": f"/api/images/{image_id}/variants/preview1024",
            "thumb_url": f"/api/images/{image_id}/variants/thumb256",
            **({"revised_prompt": revised_prompt} if revised_prompt else {}),
        }


async def _store_and_publish_completion_tool_image(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    b64_image: str,
    revised_prompt: str | None,
) -> dict[str, Any] | None:
    try:
        raw_image = base64.b64decode(b64_image, validate=False)
    except binascii.Error as exc:
        raise UpstreamError(
            f"bad base64 from image_generation tool: {exc}",
            error_code=EC.BAD_RESPONSE.value,
            status_code=200,
        ) from exc
    async with SessionLocal() as session:
        image_payload = await _store_completion_tool_image(
            session=session,
            task_id=task_id,
            user_id=user_id,
            message_id=message_id,
            raw_image=raw_image,
            revised_prompt=revised_prompt,
        )
        await session.commit()

    await publish_event(
        redis,
        user_id,
        channel,
        EV_COMP_IMAGE,
        {
            "completion_id": task_id,
            "message_id": message_id,
            "attempt": attempt,
            "attempt_epoch": attempt_epoch,
            "images": [image_payload],
        },
    )
    return image_payload


async def _is_cancelled(redis: Any, task_id: str) -> bool:
    try:
        v = await redis.get(f"task:{task_id}:cancel")
    except Exception:  # noqa: BLE001
        return False
    return bool(v)


class _CompletionEpochSuperseded(RuntimeError):
    """Raised when another worker has advanced this completion attempt epoch."""


def _completion_lock_key(completion_id: str) -> int:
    """GEN-P0-6: stable 63-bit int key for pg_advisory_xact_lock。

    pg_advisory_xact_lock 接收 bigint。把 completion UUID 哈希成 63-bit 整数
    （第 64 位留给 PG 的符号位），不同 worker 在同一 completion 上竞争时自动排队；
    事务结束自动释放。这是 `attempt` 作 CAS epoch 之外的第二层保险——即使 CAS
    检查与 UPDATE 之间有客户端重试路径，advisory lock 也能保证互斥。
    """
    import hashlib
    h = hashlib.sha256(completion_id.encode("utf-8", errors="replace")).digest()
    # 取前 8 字节，mask 到 63 bit 正整数
    return int.from_bytes(h[:8], byteorder="big", signed=False) & ((1 << 63) - 1)


async def _acquire_completion_xact_lock(session: Any, completion_id: str) -> None:
    """Best-effort: 在当前事务内拿 pg_advisory_xact_lock。非 Postgres 后端静默跳过。"""
    try:
        key = _completion_lock_key(completion_id)
        await session.execute(sa_text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=key))
    except Exception as exc:  # noqa: BLE001
        # SQLite 单测 / 非 PG 环境没有 pg_advisory_xact_lock；退化到 CAS 级保护。
        logger.debug("pg_advisory_xact_lock unavailable: %s", exc)


# ---------------------------------------------------------------------------
# Lease helpers
# ---------------------------------------------------------------------------


async def _acquire_lease(redis: Any, task_id: str, worker_id: str) -> None:
    await redis.set(f"task:{task_id}:lease", worker_id, ex=_LEASE_TTL_S)


async def _release_lease(redis: Any, task_id: str) -> None:
    try:
        await redis.delete(f"task:{task_id}:lease")
    except Exception:  # noqa: BLE001
        pass


async def _lease_renewer(
    redis: Any, task_id: str, lease_lost: asyncio.Event | None = None
) -> None:
    """每 30s EXPIRE 一次。被 cancel 时优雅退出；连续 3 次失败抛 RuntimeError。"""
    consecutive_failures = 0
    try:
        while True:
            await asyncio.sleep(_LEASE_RENEW_S)
            try:
                await redis.expire(f"task:{task_id}:lease", _LEASE_TTL_S)
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                logger.warning(
                    "lease renew failed task=%s err=%s streak=%d",
                    task_id,
                    exc,
                    consecutive_failures,
                )
                if consecutive_failures >= 3:
                    if lease_lost is not None:
                        lease_lost.set()
                    raise RuntimeError(
                        f"lease renewer giving up after {consecutive_failures} failures"
                    )
    except asyncio.CancelledError:
        raise


# ---------------------------------------------------------------------------
# History packing (§22.2)
# ---------------------------------------------------------------------------


async def _attachment_to_data_url(session: Any, image_id: str) -> str | None:
    """读图片 bytes → base64 data URL；优先 preview1024 变体（节省 token）。

    DESIGN §22.3：completion 链路传 preview 即可，原图仅用于 image_to_image。
    单张图读失败返回 None，调用方跳过；不让单张坏图拖垮整条消息。
    """
    img = await session.get(Image, image_id)
    if img is None or getattr(img, "deleted_at", None) is not None:
        return None

    preview = (
        await session.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == image_id,
                ImageVariant.kind == "preview1024",
            )
        )
    ).scalar_one_or_none()

    if preview is not None:
        key = preview.storage_key
        mime = "image/webp"
    else:
        key = img.storage_key
        mime = img.mime or "image/png"

    try:
        raw = await asyncio.to_thread(storage.get_bytes, key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("attachment read failed image_id=%s err=%s", image_id, exc)
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _role_eq(role: Any, expected: Role) -> bool:
    return role == expected or role == expected.value


def _message_created_at(m: Message) -> datetime:
    value = m.created_at
    if isinstance(value, datetime):
        return value
    return datetime.min.replace(tzinfo=timezone.utc)


def _summary_created_at(summary: dict[str, Any] | None) -> datetime | None:
    if not isinstance(summary, dict):
        return None
    raw = summary.get("up_to_created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summary_compressed_at(summary: dict[str, Any] | None) -> datetime | None:
    if not isinstance(summary, dict):
        return None
    raw = summary.get("compressed_at") or summary.get("updated_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summary_covers_boundary(
    summary: dict[str, Any] | None,
    boundary_message: Message | None,
) -> bool:
    if boundary_message is None or not is_summary_usable(summary):
        return False
    summary_id = summary.get("up_to_message_id")
    summary_id = summary_id if isinstance(summary_id, str) and summary_id else None
    if summary_id == boundary_message.id:
        return True
    summary_dt = _summary_created_at(summary)
    boundary_dt = _message_created_at(boundary_message)
    if summary_dt is None:
        return False
    if summary_dt.tzinfo is None and boundary_dt.tzinfo is not None:
        summary_dt = summary_dt.replace(tzinfo=boundary_dt.tzinfo)
    if boundary_dt.tzinfo is None and summary_dt.tzinfo is not None:
        boundary_dt = boundary_dt.replace(tzinfo=summary_dt.tzinfo)
    if summary_dt > boundary_dt:
        return True
    if summary_dt < boundary_dt:
        return False
    return bool(summary_id and summary_id >= boundary_message.id)


def _summary_age_seconds(summary: dict[str, Any] | None) -> int | None:
    compressed_at = _summary_compressed_at(summary)
    if compressed_at is None:
        return None
    now = datetime.now(compressed_at.tzinfo or timezone.utc)
    return max(0, int((now - compressed_at).total_seconds()))


def _truncate_sticky_text(text: str) -> str:
    if len(text) <= _STICKY_TEXT_CHAR_LIMIT:
        return text
    return text[:_STICKY_TEXT_CHAR_LIMIT] + "\n[... truncated original task ...]"


def _sticky_text_from_message(message: Message) -> str:
    content = message.content or {}
    text = _truncate_sticky_text(content.get("text") or "")
    refs: list[str] = []
    for att in content.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        image_id = att.get("image_id")
        if image_id:
            refs.append(f"[user_image image_id={image_id}]")
        elif att.get("kind"):
            refs.append(f"[attachment kind={att.get('kind')!r}]")
    if refs:
        return "\n".join([text, *refs]).strip()
    return text


def _count_message_tokens(role: str, content: dict[str, Any] | None) -> int:
    """Precise token count for a message using tiktoken (via count_tokens).

    Mirrors estimate_message_tokens() but uses count_tokens() for text
    instead of the char/4 heuristic, giving accurate counts for CJK content
    where the heuristic can be off by +/-20%.
    """
    content = content or {}
    text = content.get("text") or ""

    if role == Role.USER.value:
        attachments = content.get("attachments") or []
        image_count = sum(
            1 for att in attachments
            if isinstance(att, dict) and att.get("image_id")
        )
        if not text and image_count == 0:
            return 0
        return (
            MESSAGE_OVERHEAD_TOKENS
            + count_tokens(text)
            + image_count * IMAGE_INPUT_ESTIMATED_TOKENS
        )
    if role in (Role.ASSISTANT.value, Role.SYSTEM.value):
        if not text:
            return 0
        return MESSAGE_OVERHEAD_TOKENS + count_tokens(text)
    return 0


def _with_summary_guardrail(system_prompt: str | None, *, enabled: bool) -> str | None:
    if not enabled:
        return system_prompt
    guardrail = compose_summary_guardrail()
    if system_prompt:
        if guardrail in system_prompt:
            return system_prompt
        return f"{system_prompt.rstrip()}\n\n{guardrail}"
    return None


def _instructions_with_summary_guardrail(
    system_prompt: str | None,
    *,
    enabled: bool,
) -> str:
    base = system_prompt or DEFAULT_CHAT_INSTRUCTIONS
    if not enabled:
        return base
    guardrail = compose_summary_guardrail()
    if guardrail in base:
        return base
    return f"{base.rstrip()}\n\n{guardrail}"


async def _message_to_input_item(session: Any, m: Message) -> dict[str, Any] | None:
    content = m.content or {}
    text = content.get("text") or ""
    if _role_eq(m.role, Role.USER):
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "input_text", "text": text})
        # DESIGN §22.3：附图优先走 preview1024.webp（节省 token），
        # 原图只在 image_to_image 场景才内联；completion 这里统一用 preview。
        for att in content.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            image_id = att.get("image_id")
            if not image_id:
                continue
            data_url = await _attachment_to_data_url(session, image_id)
            if data_url:
                parts.append({"type": "input_image", "image_url": data_url})
        if parts:
            return {"role": "user", "content": parts}
    elif _role_eq(m.role, Role.ASSISTANT):
        # 只把 completion 文本塞回；generation 默认不塞（§22.2 步骤 2.c）
        if text:
            return {
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
    return None


async def _build_input_from_packed_context(
    session: Any,
    packed: PackedContext,
) -> list[dict[str, Any]]:
    input_list: list[dict[str, Any]] = []
    include_guardrail = packed.summary_used or packed.sticky_used
    system_prompt = _with_summary_guardrail(
        packed._system_prompt,
        enabled=include_guardrail,
    )
    if system_prompt:
        input_list.append(
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            }
        )

    if packed.sticky_used and packed._sticky_message is not None:
        sticky_text = _sticky_text_from_message(packed._sticky_message)
        if sticky_text:
            input_list.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": format_sticky_input_text(sticky_text),
                        }
                    ],
                }
            )

    if packed.summary_used and packed._summary_text:
        input_list.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": format_summary_input_text(packed._summary_text),
                    }
                ],
            }
        )

    for m in packed._recent_rows:
        item = await _message_to_input_item(session, m)
        if item is not None:
            input_list.append(item)
    return input_list


def _make_quality_probes(packed: PackedContext) -> dict[str, Any]:
    return {
        "summary_used": packed.summary_used,
        "summary_age_seconds": packed.summary_age_seconds,
        "summary_tokens": packed.summary_tokens,
        "recent_messages_count": packed.recent_messages_count,
        "first_user_message_pinned": packed.sticky_used,
        "user_repeated_facts_score": None,
        "model_signaled_missing_context": False,
    }


def _packed_with_input(input_list: list[dict[str, Any]], packed: PackedContext) -> PackedContext:
    return replace(
        packed,
        input_list=input_list,
        quality_probes=packed.quality_probes or _make_quality_probes(packed),
    )


def _estimated_summary_source(rows: list[Message], *, skip_message_id: str | None) -> int:
    """Estimate tokens for summary source messages using tiktoken via JSON serialization."""
    import json
    total = 0
    for m in rows:
        if m.id == skip_message_id:
            continue
        content_json = json.dumps(m.content or {}, ensure_ascii=False)
        total += MESSAGE_OVERHEAD_TOKENS + count_tokens(content_json)
    return total


def _fallback_pack(
    *,
    system_prompt: str | None,
    rows_desc: list[Message],
    used_tokens: int,
    truncated: bool,
    compression_enabled: bool = False,
    fallback_reason: str | None = None,
    force_include_message: Message | None = None,
    compressor_model: str | None = None,
) -> PackedContext:
    selected_desc = list(rows_desc)
    if force_include_message is not None and all(
        m.id != force_include_message.id for m in selected_desc
    ):
        selected_desc.insert(0, force_include_message)
        used_tokens += _count_message_tokens(
            force_include_message.role,
            force_include_message.content,
        )
    selected = tuple(reversed(selected_desc))
    return PackedContext(
        input_list=[],
        estimated_tokens=used_tokens,
        summary_used=False,
        summary_created=False,
        summary_up_to_message_id=None,
        sticky_used=False,
        included_messages_count=len(selected),
        truncated_without_summary=truncated,
        fallback_reason=fallback_reason,
        compression_enabled=compression_enabled,
        recent_messages_count=len(selected),
        compressor_model=compressor_model,
        _system_prompt=system_prompt,
        _recent_rows=selected,
    )


async def _load_rows_desc(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    budget_tokens: int | None,
    system_prompt: str | None,
) -> tuple[list[Message], int, bool]:
    rows_desc: list[Message] = []
    used_tokens = estimate_system_prompt_tokens(system_prompt)
    cursor_created_at = target.created_at
    cursor_id = target.id
    cursor_inclusive = True
    truncated = False

    while True:
        same_timestamp_filter = (
            Message.id <= cursor_id if cursor_inclusive else Message.id < cursor_id
        )
        q = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.deleted_at.is_(None),
                or_(
                    Message.created_at < cursor_created_at,
                    and_(
                        Message.created_at == cursor_created_at,
                        same_timestamp_filter,
                    ),
                ),
            )
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(HISTORY_FETCH_BATCH)
        )
        batch = list((await session.execute(q)).scalars())
        if not batch:
            break

        stop = False
        for m in batch:
            est_tokens = _count_message_tokens(m.role, m.content)
            if est_tokens <= 0:
                cursor_created_at = m.created_at
                cursor_id = m.id
                continue
            if budget_tokens is not None and used_tokens + est_tokens > budget_tokens:
                stop = True
                truncated = True
                break
            rows_desc.append(m)
            used_tokens += est_tokens
            cursor_created_at = m.created_at
            cursor_id = m.id

        if stop or len(batch) < HISTORY_FETCH_BATCH:
            break
        cursor_inclusive = False

    return rows_desc, used_tokens, truncated


def _pick_first_user(rows_desc: list[Message]) -> Message | None:
    users = [m for m in rows_desc if _role_eq(m.role, Role.USER)]
    if not users:
        return None
    return min(users, key=lambda m: (_message_created_at(m), m.id))


def _pick_current_user(rows_desc: list[Message], target: Message) -> Message | None:
    parent_id = getattr(target, "parent_message_id", None)
    if parent_id:
        for m in rows_desc:
            if m.id == parent_id and _role_eq(m.role, Role.USER):
                return m
    for m in rows_desc:
        if _role_eq(m.role, Role.USER):
            return m
    return None


async def _context_circuit_open(redis: Any | None) -> bool:
    if redis is None:
        return False
    try:
        value = await redis.get("context:circuit:breaker:state")
    except Exception:  # noqa: BLE001
        return False
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return bool(value and str(value).lower() not in {"0", "closed", "false"})


async def _resolve_summary_model() -> str:
    try:
        raw = await runtime_settings.resolve("context.summary_model")
    except Exception as exc:  # noqa: BLE001
        logger.debug("context summary model setting fallback err=%s", exc)
        raw = None
    return raw or "gpt-5.4"


async def _resolve_int_setting(spec_key: str, default: int) -> int:
    try:
        return await runtime_settings.resolve_int(spec_key, default)
    except Exception as exc:  # noqa: BLE001
        logger.debug("context int setting fallback key=%s err=%s", spec_key, exc)
        return default


async def _ensure_context_summary(
    session: Any,
    conv: Conversation,
    boundary: _SummaryBoundary,
    *,
    target_tokens: int,
    model: str,
    redis: Any | None,
) -> dict[str, Any] | None:
    service = context_summary
    ensure = getattr(service, "ensure_context_summary", None) if service is not None else None
    if ensure is None:
        return None
    settings_payload = {
        "context.summary_target_tokens": target_tokens,
        "context.summary_model": model,
        "target_tokens": target_tokens,
        "summary_target_tokens": target_tokens,
        "model": model,
        "summary_model": model,
        "redis": redis,
        "trigger": "auto",
    }
    result = await ensure(
        session,
        conv,
        boundary,
        settings_payload,
        force=False,
        extra_instruction=None,
        dry_run=False,
    )
    if isinstance(result, dict):
        summary = result.get("summary_jsonb") or result.get("summary") or result
        if is_summary_usable(summary):
            return summary
    # The context summary service intentionally returns public metadata only.
    # Reload the conversation row after it commits so completion packing can
    # inject the private summary text without exposing it through the service API.
    try:
        await session.refresh(conv)
    except Exception as exc:  # noqa: BLE001
        logger.debug("context summary refresh failed conv=%s err=%s", conv.id, exc)
    latest_summary = getattr(conv, "summary_jsonb", None)
    if is_summary_usable(latest_summary):
        return latest_summary
    return None


async def _pack_recent_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
    redis: Any | None = None,
    chat_model: str | None = None,
) -> PackedContext:
    # 按模型查 input budget；未传 chat_model（旧调用 / 测试桩）时退回模块级
    # CONTEXT_INPUT_TOKEN_BUDGET，保持现有 monkeypatch 测试可以收紧预算。
    input_budget = (
        get_input_budget(chat_model) if chat_model else CONTEXT_INPUT_TOKEN_BUDGET
    )
    target = await session.get(Message, up_to_message_id)
    if target is None:
        return PackedContext(
            input_list=[],
            estimated_tokens=0,
            summary_used=False,
            summary_created=False,
            summary_up_to_message_id=None,
            sticky_used=False,
            included_messages_count=0,
            truncated_without_summary=False,
            fallback_reason=None,
            _system_prompt=system_prompt,
        )

    compression_enabled = bool(
        await _resolve_int_setting(
            "context.compression_enabled",
            _CONTEXT_COMPRESSION_ENABLED_DEFAULT,
        )
    )
    if not compression_enabled:
        rows_desc, used_tokens, truncated = await _load_rows_desc(
            session,
            conversation_id=conversation_id,
            target=target,
            budget_tokens=input_budget,
            system_prompt=system_prompt,
        )
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=rows_desc,
            used_tokens=used_tokens,
            truncated=truncated,
        )
        return _packed_with_input(
            await _build_input_from_packed_context(session, packed),
            packed,
        )

    summary_model = await _resolve_summary_model()
    if await _context_circuit_open(redis):
        rows_desc, used_tokens, truncated = await _load_rows_desc(
            session,
            conversation_id=conversation_id,
            target=target,
            budget_tokens=input_budget,
            system_prompt=system_prompt,
        )
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=rows_desc,
            used_tokens=used_tokens,
            truncated=truncated,
            compression_enabled=True,
            fallback_reason="circuit_open",
            force_include_message=_pick_current_user(rows_desc, target),
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await _build_input_from_packed_context(session, packed),
            packed,
        )

    trigger_percent = await _resolve_int_setting(
        "context.compression_trigger_percent",
        _CONTEXT_COMPRESSION_TRIGGER_PERCENT_DEFAULT,
    )
    target_tokens = await _resolve_int_setting(
        "context.summary_target_tokens",
        _CONTEXT_SUMMARY_TARGET_TOKENS_DEFAULT,
    )
    min_recent_messages = max(
        1,
        await _resolve_int_setting(
            "context.summary_min_recent_messages",
            _CONTEXT_SUMMARY_MIN_RECENT_MESSAGES_DEFAULT,
        ),
    )
    min_interval_s = await _resolve_int_setting(
        "context.summary_min_interval_seconds",
        _CONTEXT_SUMMARY_MIN_INTERVAL_SECONDS_DEFAULT,
    )

    all_rows_desc, total_used_tokens, total_truncated = await _load_rows_desc(
        session,
        conversation_id=conversation_id,
        target=target,
        budget_tokens=None,
        system_prompt=system_prompt,
    )
    trigger_tokens = input_budget * trigger_percent // 100
    if total_used_tokens < trigger_tokens and not total_truncated:
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=all_rows_desc,
            used_tokens=total_used_tokens,
            truncated=False,
            compression_enabled=True,
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await _build_input_from_packed_context(session, packed),
            packed,
        )

    first_user = _pick_first_user(all_rows_desc)
    current_user = _pick_current_user(all_rows_desc, target)

    forced_recent_desc: list[Message] = []
    for m in all_rows_desc:
        if _count_message_tokens(m.role, m.content) <= 0:
            continue
        if len(forced_recent_desc) < min_recent_messages:
            forced_recent_desc.append(m)
    if current_user is not None and all(m.id != current_user.id for m in forced_recent_desc):
        forced_recent_desc.insert(0, current_user)

    forced_ids = {m.id for m in forced_recent_desc}
    first_user_in_recent = first_user is not None and first_user.id in forced_ids
    sticky_message = first_user if first_user is not None and not first_user_in_recent else None
    sticky_tokens = 0
    if sticky_message is not None:
        sticky_input_text = format_sticky_input_text(
            _sticky_text_from_message(sticky_message)
        )
        # P1-4: sticky 文本是 trigger 判定后续 used_tokens 累加的种子值之一，
        # 用 tiktoken 精确计数收紧 ±15% 偏差；其它估算点不动以避免破坏现有 monkeypatch 测试。
        sticky_tokens = (
            MESSAGE_OVERHEAD_TOKENS
            + count_tokens(sticky_input_text)
        )

    used_tokens = (
        estimate_system_prompt_tokens(_with_summary_guardrail(system_prompt, enabled=True))
        + sticky_tokens
        + target_tokens
        + MESSAGE_OVERHEAD_TOKENS
    )
    recent_desc = list(forced_recent_desc)
    for m in forced_recent_desc:
        used_tokens += _count_message_tokens(m.role, m.content)

    for m in all_rows_desc:
        if m.id in forced_ids:
            continue
        if sticky_message is not None and m.id == sticky_message.id:
            continue
        est = _count_message_tokens(m.role, m.content)
        if est <= 0:
            continue
        if used_tokens + est > input_budget:
            break
        recent_desc.append(m)
        forced_ids.add(m.id)
        used_tokens += est

    recent_ids = {m.id for m in recent_desc}
    summary_rows = [
        m
        for m in all_rows_desc
        if _count_message_tokens(m.role, m.content) > 0
        and m.id not in recent_ids
        and (sticky_message is None or m.id != sticky_message.id)
    ]
    if not summary_rows:
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=all_rows_desc,
            used_tokens=total_used_tokens,
            truncated=False,
            compression_enabled=True,
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await _build_input_from_packed_context(session, packed),
            packed,
        )

    boundary_message = max(summary_rows, key=lambda m: (_message_created_at(m), m.id))
    conv = await session.get(Conversation, conversation_id)
    summary = getattr(conv, "summary_jsonb", None) if conv is not None else None
    summary_created = False

    summary_recently_refreshed = False
    if is_summary_usable(summary) and min_interval_s > 0:
        compressed_at = _summary_compressed_at(summary)
        if compressed_at is not None:
            now = datetime.now(compressed_at.tzinfo or timezone.utc)
            summary_recently_refreshed = (
                now - compressed_at
            ).total_seconds() < min_interval_s

    if summary_recently_refreshed and not _summary_covers_boundary(
        summary,
        boundary_message,
    ):
        fallback_rows_desc, fallback_tokens, fallback_truncated = await _load_rows_desc(
            session,
            conversation_id=conversation_id,
            target=target,
            budget_tokens=input_budget,
            system_prompt=system_prompt,
        )
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=fallback_rows_desc,
            used_tokens=fallback_tokens,
            truncated=fallback_truncated,
            compression_enabled=True,
            fallback_reason="rate_limited",
            force_include_message=current_user,
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await _build_input_from_packed_context(session, packed),
            packed,
        )

    if not _summary_covers_boundary(summary, boundary_message) and conv is not None:
        boundary = _SummaryBoundary(
            conversation_id=conversation_id,
            up_to_message_id=boundary_message.id,
            up_to_created_at=_message_created_at(boundary_message),
            first_user_message_id=first_user.id if first_user is not None else None,
            recent_message_ids=[m.id for m in reversed(recent_desc)],
            summary_message_ids=[m.id for m in reversed(summary_rows)],
            source_message_count=len(summary_rows),
            source_token_estimate=_estimated_summary_source(
                summary_rows,
                skip_message_id=sticky_message.id if sticky_message is not None else None,
            ),
        )
        try:
            new_summary = await _ensure_context_summary(
                session,
                conv,
                boundary,
                target_tokens=target_tokens,
                model=summary_model,
                redis=redis,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "context summary generation failed conversation=%s err=%s",
                conversation_id,
                exc,
            )
            new_summary = None
        if is_summary_usable(new_summary):
            summary_created = True
            summary = new_summary

    if not _summary_covers_boundary(summary, boundary_message):
        fallback_rows_desc, fallback_tokens, fallback_truncated = await _load_rows_desc(
            session,
            conversation_id=conversation_id,
            target=target,
            budget_tokens=input_budget,
            system_prompt=system_prompt,
        )
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=fallback_rows_desc,
            used_tokens=fallback_tokens,
            truncated=fallback_truncated,
            compression_enabled=True,
            fallback_reason="summary_failed",
            force_include_message=current_user,
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await _build_input_from_packed_context(session, packed),
            packed,
        )

    summary_text = str((summary or {}).get("text") or "")
    summary_token_count = estimate_summary_tokens(summary)
    recent_rows = tuple(reversed(recent_desc))
    estimated_tokens = (
        estimate_system_prompt_tokens(_with_summary_guardrail(system_prompt, enabled=True))
        + (sticky_tokens if sticky_message is not None else 0)
        + MESSAGE_OVERHEAD_TOKENS
        + summary_token_count
        + sum(_count_message_tokens(m.role, m.content) for m in recent_rows)
    )
    packed = PackedContext(
        input_list=[],
        estimated_tokens=estimated_tokens,
        summary_used=True,
        summary_created=summary_created,
        summary_up_to_message_id=str(
            (summary or {}).get("up_to_message_id") or boundary_message.id
        ),
        sticky_used=sticky_message is not None,
        included_messages_count=len(recent_rows) + (1 if sticky_message is not None else 0),
        truncated_without_summary=False,
        fallback_reason=None,
        compression_enabled=True,
        recent_messages_count=len(recent_rows),
        summary_tokens=summary_token_count,
        summary_age_seconds=_summary_age_seconds(summary),
        compressor_model=summary_model,
        image_caption_count=int((summary or {}).get("image_caption_count") or 0),
        _system_prompt=system_prompt,
        _sticky_message=sticky_message,
        _summary_text=summary_text,
        _recent_rows=recent_rows,
    )
    return _packed_with_input(
        await _build_input_from_packed_context(session, packed),
        packed,
    )


async def _build_input_from_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
) -> list[dict[str, Any]]:
    """按 §22.2 规则把会话历史转成 Responses `input` 列表。

    - 仅取 up_to_message_id 及其之前
    - 最近消息优先，按估算 token 累计到 200k input budget
    - user.content.text + attachments → {role:user, [input_text, input_image...]}
    - assistant.content.text (completion) → {role:assistant, [output_text]}
    - assistant.content.images (generation) → 默认不塞
    - system_prompt 作为第一条
    """
    packed = await _pack_recent_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
    )
    return packed.input_list


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_exception(exc: BaseException, has_partial: bool) -> RetryDecision:
    if isinstance(exc, UpstreamError):
        return is_retriable(
            exc.error_code, exc.status_code, has_partial, error_message=str(exc)
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)):
        return is_retriable(
            "stream_interrupted" if has_partial else "upstream_error",
            None,
            has_partial,
            error_message=str(exc),
        )
    if isinstance(exc, httpx.HTTPError):
        return is_retriable(
            "upstream_error", None, has_partial, error_message=str(exc)
        )
    return RetryDecision(False, f"unhandled {type(exc).__name__}")


def _bounded_next_attempt(current_attempt: int | None) -> tuple[int, bool]:
    """Return capped next attempt and whether it may run upstream."""
    next_attempt = min((current_attempt or 0) + 1, _MAX_ATTEMPTS + 1)
    return next_attempt, next_attempt <= _MAX_ATTEMPTS


def _context_metadata(packed: PackedContext) -> dict[str, Any]:
    return {
        "estimated_input_tokens": packed.estimated_tokens,
        "included_messages_count": packed.included_messages_count,
        "summary_used": packed.summary_used,
        "summary_created": packed.summary_created,
        "sticky_used": packed.sticky_used,
        "summary_up_to_message_id": packed.summary_up_to_message_id,
        "fallback_reason": packed.fallback_reason,
        "compressor_model": packed.compressor_model,
        "image_caption_count": packed.image_caption_count,
        "quality_probes": packed.quality_probes or _make_quality_probes(packed),
    }


async def _record_completion_context_metadata(
    session: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    packed: PackedContext,
) -> None:
    if not packed.compression_enabled:
        return
    comp = await session.get(Completion, task_id)
    if comp is None or comp.attempt != attempt_epoch:
        return
    upstream_request = dict(comp.upstream_request or {})
    upstream_request["context"] = _context_metadata(packed)
    comp.upstream_request = upstream_request
    await session.commit()


async def _flush_completion_text(
    task_id: str,
    text: str,
    *,
    attempt_epoch: int,
    retries: int = _PG_FLUSH_RETRIES,
) -> None:
    """Flush streamed text to PG, retrying transient commit/update failures.

    The attempt guard is the minimal epoch contract: an older worker must never
    overwrite text once a newer run has advanced Completion.attempt.
    """
    last_exc: BaseException | None = None
    for idx in range(retries):
        try:
            async with SessionLocal() as session:
                res = await session.execute(
                    update(Completion)
                    .where(
                        Completion.id == task_id,
                        Completion.attempt == attempt_epoch,
                    )
                    .values(text=text)
                )
                if (res.rowcount or 0) == 0:
                    raise _CompletionEpochSuperseded(
                        f"completion epoch superseded task={task_id} "
                        f"attempt_epoch={attempt_epoch}"
                    )
                await session.commit()
                return
        except _CompletionEpochSuperseded:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "completion text flush failed task=%s attempt_epoch=%s "
                "try=%d/%d err=%s",
                task_id,
                attempt_epoch,
                idx + 1,
                retries,
                exc,
            )
            if idx + 1 < retries:
                await asyncio.sleep(_PG_FLUSH_BACKOFF_S * (2**idx))

    raise UpstreamError(
        "completion text flush failed after retries",
        error_code=EC.UPSTREAM_ERROR.value,
        status_code=None,
    ) from last_exc


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def run_completion(ctx: dict[str, Any], task_id: str) -> None:  # noqa: PLR0915, PLR0912
    redis = ctx["redis"]
    worker_id = str(ctx.get("worker_id") or ctx.get("job_id") or "worker")
    _task_start = asyncio.get_event_loop().time()
    _task_outcome = "unknown"
    attempt = 0
    attempt_epoch = 0

    # --- 1. 读 completion 行 ---
    async with SessionLocal() as session:
        # GEN-P0-6: 在事务粒度抢 pg_advisory_xact_lock，保证同一 completion 的
        # "claim → attempt++ → 切状态" 与其他 worker 互斥；事务 commit/rollback 自动释放。
        await _acquire_completion_xact_lock(session, task_id)

        comp: Completion | None = (
            await session.execute(
                select(Completion).where(Completion.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()
        if comp is None:
            logger.warning("completion not found task_id=%s", task_id)
            return
        if is_completion_terminal(comp.status):
            logger.info("completion terminal task_id=%s status=%s", task_id, comp.status)
            return

        # 判断是否是"被接管重跑"——attempt > 0 且 text 非空 ⇒ 上一个 worker 挂了
        was_restarted = (comp.attempt or 0) > 0 and bool(comp.text)

        user_id = comp.user_id
        message_id = comp.message_id
        system_prompt = comp.system_prompt
        # 关键：chat 走 /v1/responses 但要用聊天模型（gpt-5.5 等），
        # 而 UPSTREAM_MODEL 是图像模型 gpt-image-2，不能跨用
        chat_model = comp.model or DEFAULT_CHAT_MODEL

        attempt, attempt_may_run = _bounded_next_attempt(comp.attempt)
        attempt_epoch = attempt
        if not attempt_may_run:
            err_code = "max_attempts_exceeded"
            err_msg = f"completion exceeded max attempts ({_MAX_ATTEMPTS})"
            comp.status = CompletionStatus.FAILED.value
            comp.progress_stage = CompletionStage.FINALIZING
            comp.attempt = attempt
            comp.finished_at = datetime.now(timezone.utc)
            comp.error_code = err_code
            comp.error_message = err_msg
            msg_failed = await session.get(Message, message_id)
            if msg_failed is not None:
                msg_failed.status = MessageStatus.FAILED
            await session.commit()
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_COMP_FAILED,
                {
                    "completion_id": task_id,
                    "message_id": message_id,
                    "attempt": attempt,
                    "attempt_epoch": attempt_epoch,
                    "code": err_code,
                    "message": err_msg,
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            try:
                _duration = asyncio.get_event_loop().time() - _task_start
                task_duration_seconds.labels(
                    kind="completion", outcome=safe_outcome(_task_outcome)
                ).observe(_duration)
            except Exception:  # noqa: BLE001
                pass
            return

        comp.status = CompletionStatus.STREAMING.value
        comp.progress_stage = CompletionStage.STREAMING
        comp.started_at = datetime.now(timezone.utc)
        comp.attempt = attempt
        # 流中断恢复：清空已写 text（§6.9 策略 1）
        if was_restarted:
            comp.text = ""
        await session.commit()

        # 查 conversation_id（通过 message）
        msg = await session.get(Message, message_id)
        conversation_id = msg.conversation_id if msg is not None else None

    channel = task_channel(task_id)

    # --- 2. lease ---
    await _acquire_lease(redis, task_id, worker_id)
    lease_lost = asyncio.Event()
    renewer = asyncio.create_task(_lease_renewer(redis, task_id, lease_lost))

    # --- 3. publish started / restarted ---
    if was_restarted:
        await publish_event(
            redis,
            user_id,
            channel,
            EV_COMP_RESTARTED,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
            },
        )
    else:
        await publish_event(
            redis,
            user_id,
            channel,
            EV_COMP_STARTED,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
            },
        )

    accumulated_text = ""
    accumulated_thinking = ""
    flushed_len = 0
    has_partial = False
    tool_images: list[dict[str, Any]] = []
    stored_image_call_ids: set[str] = set()
    tool_tracker = _CompletionToolTracker()
    tokens_in = 0
    tokens_out = 0

    # 观测：整个 upstream 流式阶段一层 span；手动 enter/exit 以免嵌套大块改缩进
    _stream_span_cm = None
    try:
        _stream_span_cm = _tracer.start_as_current_span("upstream.stream_completion")
        _stream_span = _stream_span_cm.__enter__()
        _stream_span.set_attribute("lumen.task_id", task_id)
    except Exception:  # noqa: BLE001
        _stream_span_cm = None

    try:
        # --- 4. 组 body ---
        reasoning_effort: str | None = None
        fast_mode = False
        chat_tools: list[dict[str, Any]] = []
        # instructions 必须保持稳定（prompt cache 命中前提）。
        # 上游 cache 按请求前缀逐字节比对，instructions 是头部字段；这里只允许：
        # ① comp.system_prompt（DB 持久化的用户/会话级 prompt，按消息固定）
        # ② DEFAULT_CHAT_INSTRUCTIONS 常量
        # ③ _instructions_with_summary_guardrail() 追加的 SUMMARY_GUARDRAIL 常量
        # 严禁在此注入 datetime.now() / time.time() / uuid / random / user.name /
        # session_id / IP 等动态字段；如有动态信息需要给模型，请塞到 input_list 的
        # user message 里（不参与 cache key）。
        instructions = system_prompt or DEFAULT_CHAT_INSTRUCTIONS
        async with SessionLocal() as session:
            if conversation_id is None:
                input_list = []
                if system_prompt:
                    input_list.append(
                        {
                            "role": "system",
                            "content": [{"type": "input_text", "text": system_prompt}],
                        }
                    )
            else:
                packed = await _pack_recent_history(
                    session,
                    conversation_id=conversation_id,
                    up_to_message_id=message_id,
                    system_prompt=system_prompt,
                    redis=redis,
                    chat_model=chat_model,
                )
                input_list = packed.input_list
                instructions = _instructions_with_summary_guardrail(
                    system_prompt,
                    enabled=packed.summary_used or packed.sticky_used,
                )
                await _record_completion_context_metadata(
                    session,
                    task_id=task_id,
                    attempt_epoch=attempt_epoch,
                    packed=packed,
                )

            # assistant.parent_message_id → user message.content.{reasoning_effort, fast, tools}
            target_msg = await session.get(Message, message_id)
            if target_msg is not None and target_msg.parent_message_id:
                parent = await session.get(Message, target_msg.parent_message_id)
                if parent is not None and isinstance(parent.content, dict):
                    effort = parent.content.get("reasoning_effort")
                    if effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
                        reasoning_effort = effort
                    if parent.content.get("fast") is True:
                        fast_mode = True
                    chat_tools = await _chat_tools_from_content(parent.content)
        reasoning_effort = _normalize_reasoning_effort_for_upstream(reasoning_effort)

        # body 字段顺序稳定 + tools 数组排序由 upstream._iter_sse 兜底（见 upstream.py 顶部
        # prompt-cache 注释）；这里维持固定字面量字典字面量顺序：model → input → instructions →
        # stream → store。reasoning / service_tier 在末尾按需追加，不会插入到稳定前缀里。
        body: dict[str, Any] = {
            "model": chat_model,
            "input": input_list,
            # 上游现在强制要求 `instructions` 顶层字段；无自定义 system_prompt 时用默认
            "instructions": instructions,
            "stream": True,
            "store": True,
            # Tools are added below only when the parent user message opted in.
        }
        _configure_chat_tools(body, chat_tools)
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
        if fast_mode:
            # Fast 模式 = OpenAI Priority 处理通道（Codex fast 语义同源）。
            # 上游若不支持会原样忽略；若账号/项目未开 Priority，服务端会降级到 default 但仍处理。
            body["service_tier"] = "priority"

        # --- 5. 消费 SSE ---
        delta_counter = 0
        # GEN-P1-4: 进入 SSE 循环前检查一次 cancel——已经取消就直接走终态。
        if await _is_cancelled(redis, task_id):
            raise _TaskCancelled("cancelled before stream start")
        if lease_lost.is_set():
            raise _LeaseLost("lease lost before stream start")
        completed_response: dict[str, Any] | None = None
        async for ev in stream_completion(body):
            ev_type = ev.get("type", "")
            tool_call = tool_tracker.update(ev)
            if tool_call is not None:
                await _publish_completion_tool_progress(
                    redis=redis,
                    user_id=user_id,
                    channel=channel,
                    task_id=task_id,
                    message_id=message_id,
                    attempt=attempt,
                    attempt_epoch=attempt_epoch,
                    tool_call=tool_call,
                    tool_calls=tool_tracker.content(),
                )
            thinking_delta = _extract_reasoning_delta(ev)
            if thinking_delta:
                if not accumulated_thinking.endswith(thinking_delta):
                    accumulated_thinking += thinking_delta
                if thinking_delta:
                    await publish_event(
                        redis,
                        user_id,
                        channel,
                        EV_COMP_THINKING_DELTA,
                        {
                            "completion_id": task_id,
                            "message_id": message_id,
                            "attempt": attempt,
                            "attempt_epoch": attempt_epoch,
                            "thinking_delta": thinking_delta,
                        },
                    )

            image_b64 = _extract_response_image_b64(ev)
            if image_b64:
                image_id = None
                item = ev.get("item")
                if isinstance(item, dict):
                    raw_id = item.get("id")
                    image_id = raw_id if isinstance(raw_id, str) else None
                if image_id is None or image_id not in stored_image_call_ids:
                    image_payload = await _store_and_publish_completion_tool_image(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        b64_image=image_b64,
                        revised_prompt=_extract_response_revised_prompt(ev),
                    )
                    if image_payload is not None:
                        tool_images.append(image_payload)
                        if image_id is not None:
                            stored_image_call_ids.add(image_id)

            if ev_type == "response.output_text.delta":
                delta = ev.get("delta") or ""
                if not delta:
                    continue
                has_partial = True
                accumulated_text += delta
                # GEN-P1-4: 每 N 个 delta 检查 cancel；命中跳出。
                delta_counter += 1
                if delta_counter % _CANCEL_CHECK_EVERY_DELTAS == 0:
                    if lease_lost.is_set():
                        raise _LeaseLost("lease lost during stream")
                    if await _is_cancelled(redis, task_id):
                        raise _TaskCancelled("cancelled during stream")

                # 按块 flush 到 PG，避免每 token 一个 UPDATE
                total_len = len(accumulated_text)
                if total_len - flushed_len >= _PG_FLUSH_EVERY_CHARS:
                    flushed_len = total_len
                    await _flush_completion_text(
                        task_id,
                        accumulated_text,
                        attempt_epoch=attempt_epoch,
                    )

                # 实时推给前端
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_COMP_DELTA,
                    {
                        "completion_id": task_id,
                        "message_id": message_id,
                        "attempt": attempt,
                        "attempt_epoch": attempt_epoch,
                        "text_delta": delta,
                    },
                )
            elif ev_type == "response.completed":
                raw_resp = ev.get("response")
                resp = raw_resp if isinstance(raw_resp, dict) else {}
                completed_response = resp
                usage = resp.get("usage") or {}
                tokens_in = int(
                    usage.get("input_tokens")
                    or usage.get("prompt_tokens")
                    or 0
                )
                tokens_out = int(
                    usage.get("output_tokens")
                    or usage.get("completion_tokens")
                    or 0
                )
                # 同时抄一下 output_text（兜底：某些网关只在 completed 里给完整文本）
                if not accumulated_text:
                    accumulated_text = _extract_completed_output_text(resp)
                if not accumulated_thinking:
                    reasoning_text = _extract_reasoning_text_from_response(resp)
                    if reasoning_text:
                        accumulated_thinking = reasoning_text
                        await publish_event(
                            redis,
                            user_id,
                            channel,
                            EV_COMP_THINKING_DELTA,
                            {
                                "completion_id": task_id,
                                "message_id": message_id,
                                "attempt": attempt,
                                "attempt_epoch": attempt_epoch,
                                "thinking_delta": reasoning_text,
                            },
                        )
                for image_event in _extract_image_events_from_response(resp):
                    image_b64 = _extract_response_image_b64(image_event)
                    if not image_b64:
                        continue
                    image_id = None
                    item = image_event.get("item")
                    if isinstance(item, dict):
                        raw_id = item.get("id")
                        image_id = raw_id if isinstance(raw_id, str) else None
                    if image_id is not None and image_id in stored_image_call_ids:
                        continue
                    image_payload = await _store_and_publish_completion_tool_image(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        b64_image=image_b64,
                        revised_prompt=_extract_response_revised_prompt(image_event),
                    )
                    if image_payload is not None:
                        tool_images.append(image_payload)
                        if image_id is not None:
                            stored_image_call_ids.add(image_id)
                for tool_call in tool_tracker.update_from_response(resp):
                    await _publish_completion_tool_progress(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        task_id=task_id,
                        message_id=message_id,
                        attempt=attempt,
                        attempt_epoch=attempt_epoch,
                        tool_call=tool_call,
                        tool_calls=tool_tracker.content(),
                    )
            # 其他事件（content_part.added 等）忽略

        final_text = _finalize_completion_text(accumulated_text, completed_response)
        if not final_text and tool_images:
            final_text = "已生成图片。"
        if not final_text:
            raise UpstreamError(
                "upstream returned empty completion",
                error_code=EC.NO_TEXT_RETURNED.value,
                status_code=200,
            )

        # --- 6. 成功态 ---
        async with SessionLocal() as session:
            res = await session.execute(
                update(Completion)
                .where(
                    Completion.id == task_id,
                    Completion.attempt == attempt_epoch,
                )
                .values(
                    status=CompletionStatus.SUCCEEDED.value,
                    progress_stage=CompletionStage.FINALIZING,
                    text=final_text,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    finished_at=datetime.now(timezone.utc),
                    error_code=None,
                    error_message=None,
                )
            )
            if (res.rowcount or 0) == 0:
                raise _CompletionEpochSuperseded(
                    f"completion epoch superseded before success task={task_id} "
                    f"attempt_epoch={attempt_epoch}"
                )
            msg = await session.get(Message, message_id)
            if msg is not None:
                content = dict(msg.content or {})
                content["text"] = final_text
                if accumulated_thinking:
                    content["thinking"] = accumulated_thinking
                tool_calls = tool_tracker.content()
                if tool_calls:
                    content["tool_calls"] = tool_calls
                msg.content = content
                msg.status = MessageStatus.SUCCEEDED
            await session.commit()

        await publish_event(
            redis,
            user_id,
            channel,
            EV_COMP_SUCCEEDED,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
                "text": final_text,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tool_calls": tool_tracker.content(),
            },
        )
        _task_outcome = "succeeded"
        upstream_calls_total.labels(kind="completion", outcome="ok").inc()

        # 自动起会话标题（第一轮对话完成后触发；内部幂等）
        if conversation_id:
            from .auto_title import maybe_enqueue_auto_title
            await maybe_enqueue_auto_title(redis, conversation_id)

    except _CompletionEpochSuperseded as exc:
        logger.info("completion worker superseded task=%s err=%s", task_id, exc)
        _task_outcome = "superseded"
        return

    except _TaskCancelled as exc:
        # GEN-P1-4: 用户主动取消——标 cancelled 并 publish failed(retriable=false)。
        logger.info("completion cancelled by user task=%s reason=%s", task_id, exc)
        try:
            async with SessionLocal() as session:
                await session.execute(
                    update(Completion)
                    .where(
                        Completion.id == task_id,
                        Completion.attempt == attempt_epoch,
                    )
                    .values(
                        status=CompletionStatus.CANCELED.value,
                        progress_stage=CompletionStage.FINALIZING,
                        finished_at=datetime.now(timezone.utc),
                        error_code=EC.CANCELLED.value,
                        error_message="cancelled by user",
                    )
                )
                msg_c = await session.get(Message, message_id)
                if msg_c is not None and msg_c.status not in (
                    MessageStatus.SUCCEEDED,
                    MessageStatus.FAILED,
                ):
                    tool_calls = tool_tracker.content()
                    if tool_calls:
                        content = dict(msg_c.content or {})
                        content["tool_calls"] = tool_calls
                        msg_c.content = content
                    msg_c.status = MessageStatus.FAILED
                await session.commit()
        except Exception as db_exc:  # noqa: BLE001
            logger.warning(
                "completion cancel DB update failed task=%s err=%s",
                task_id,
                db_exc,
            )
        await publish_event(
            redis,
            user_id,
            channel,
            EV_COMP_FAILED,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
                "code": "cancelled",
                "message": "cancelled by user",
                "retriable": False,
            },
        )
        _task_outcome = "failed"
        return

    except Exception as exc:  # noqa: BLE001
        upstream_calls_total.labels(kind="completion", outcome="error").inc()
        decision = _classify_exception(exc, has_partial)
        _err_code_log = getattr(exc, "error_code", None) or type(exc).__name__
        _http_status_log = getattr(exc, "status_code", None)
        logger.warning(
            "completion failed task=%s attempt=%s retriable=%s reason=%s "
            "error_code=%s http_status=%s",
            task_id,
            attempt,
            decision.retriable,
            decision.reason,
            _err_code_log,
            _http_status_log,
        )
        logger.debug("completion exc trace task=%s", task_id, exc_info=True)

        err_code = getattr(exc, "error_code", None) or type(exc).__name__
        err_msg = str(exc)[:2000]
        _task_outcome = "retry" if (
            decision.retriable and attempt < _MAX_ATTEMPTS
        ) else "failed"

        if decision.retriable and attempt < _MAX_ATTEMPTS:
            idx = min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)
            delay = RETRY_BACKOFF_SECONDS[idx]

            async with SessionLocal() as session:
                res = await session.execute(
                    update(Completion)
                    .where(
                        Completion.id == task_id,
                        Completion.attempt == attempt_epoch,
                    )
                    .values(
                        status=CompletionStatus.QUEUED.value,
                        progress_stage=CompletionStage.QUEUED,
                        error_code=err_code,
                        error_message=err_msg,
                    )
                )
                await session.commit()
                if (res.rowcount or 0) == 0:
                    logger.info(
                        "completion retry skipped by newer epoch task=%s "
                        "attempt_epoch=%s",
                        task_id,
                        attempt_epoch,
                    )
                    _task_outcome = "superseded"
                    return

            renewer.cancel()
            await _release_lease(redis, task_id)

            try:
                await redis.enqueue_job(
                    "run_completion", task_id, _defer_by=delay, _job_try=attempt + 1
                )
            except Exception as enq_exc:  # noqa: BLE001
                logger.error("re-enqueue failed task=%s err=%s", task_id, enq_exc)
            return

        # terminal
        async with SessionLocal() as session:
            res = await session.execute(
                update(Completion)
                .where(
                    Completion.id == task_id,
                    Completion.attempt == attempt_epoch,
                )
                .values(
                    status=CompletionStatus.FAILED.value,
                    progress_stage=CompletionStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=err_code,
                    error_message=err_msg,
                )
            )
            if (res.rowcount or 0) == 0:
                await session.commit()
                logger.info(
                    "completion failure skipped by newer epoch task=%s "
                    "attempt_epoch=%s",
                    task_id,
                    attempt_epoch,
                )
                _task_outcome = "superseded"
                return
            msg = await session.get(Message, message_id)
            if msg is not None:
                tool_calls = tool_tracker.content()
                if tool_calls:
                    content = dict(msg.content or {})
                    content["tool_calls"] = tool_calls
                    msg.content = content
                msg.status = MessageStatus.FAILED
            await session.commit()

        await publish_event(
            redis,
            user_id,
            channel,
            EV_COMP_FAILED,
            {
                "completion_id": task_id,
                "message_id": message_id,
                "attempt": attempt,
                "attempt_epoch": attempt_epoch,
                "code": err_code,
                "message": err_msg,
                "retriable": False,
            },
        )

    finally:
        renewer.cancel()
        try:
            await renewer
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await _release_lease(redis, task_id)
        if _stream_span_cm is not None:
            try:
                _stream_span_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        try:
            _duration = asyncio.get_event_loop().time() - _task_start
            task_duration_seconds.labels(
                kind="completion", outcome=safe_outcome(_task_outcome)
            ).observe(_duration)
        except Exception:  # noqa: BLE001
            pass


# settings reserved for future per-user caps / timeouts override
_ = settings

__all__ = ["run_completion"]
