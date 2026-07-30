"""Value parsing and presentation helpers for account memory extraction."""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lumen_core.memory import ExtractedMemory, canonical_memory_text
from lumen_core.model_entities import (
    Message,
    UserMemory,
)

_MAX_POSITIVE_SIGNAL = 20
_logger = logging.getLogger("app.tasks.memory_extraction")


@dataclass(frozen=True)
class AssembledMemoryPrompt:
    profile_text: str | None
    constraints_text: str | None
    context_text: str | None
    used_memory_ids: list[str]
    used_memory_summary: list[dict[str, str]]
    scope_hint_text: str | None = None
    confirmation_candidate_id: str | None = None
    confirmation_instruction: str | None = None


def _responses_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("output_text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks).strip()


def _strip_json_fences(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _parse_llm_candidates(raw: str) -> list[ExtractedMemory]:
    try:
        payload = json.loads(_strip_json_fences(raw))
    except json.JSONDecodeError:
        return []
    raw_items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        return []
    items: list[ExtractedMemory] = []
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        memory_type = row.get("type")
        content = row.get("content")
        excerpt = row.get("source_excerpt")
        intent = row.get("intent_kind")
        if memory_type not in {"profile", "preference", "avoid", "project"}:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            confidence = float(row.get("confidence", 0.82))
        except (TypeError, ValueError):
            confidence = 0.82
        items.append(
            ExtractedMemory(
                type=memory_type,
                content=content.strip()[:200],
                confidence=max(0.0, min(1.0, confidence)),
                source_excerpt=(excerpt if isinstance(excerpt, str) else content)[:160],
                intent_kind="directive" if intent == "directive" else "statement",
            )
        )
    return items[:5]


def _append_path(base_url: str, path: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}{path}"
    return f"{base}/v1{path}"


def _bump_positive_signal(memory: Any, amount: int = 1) -> None:
    try:
        current = int(getattr(memory, "positive_signal", 0) or 0)
    except (TypeError, ValueError):
        current = 0
    try:
        delta = max(0, int(amount))
    except (TypeError, ValueError):
        delta = 0
    memory.positive_signal = min(_MAX_POSITIVE_SIGNAL, current + delta)


def _usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return usage if isinstance(usage, dict) else {}


def _log_llm_usage(provider_name: str, payload: dict[str, Any]) -> None:
    usage = _usage_payload(payload)
    if not usage:
        return
    try:
        usage_text = json.dumps(
            usage,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        usage_text = str(usage)
    _logger.info(
        "memory_extraction.llm_usage provider=%s usage=%.500s",
        provider_name,
        usage_text,
    )


def _text_from_message(msg: Message | None) -> str:
    content = msg.content if msg is not None and isinstance(msg.content, dict) else {}
    text = content.get("text") if isinstance(content, dict) else ""
    return text if isinstance(text, str) else ""


def _topic_key(text: str) -> str:
    value = unicodedata.normalize("NFC", canonical_memory_text(text))
    value = re.sub(r"(用户|我|喜欢|偏好|不喜欢|不要|别|不|请|以后|回答)", "", value)
    return value


def _decay(memory: UserMemory, now: datetime) -> float:
    if memory.pinned:
        return 1.0
    if memory.type in {"profile", "avoid"}:
        return 1.0
    anchor = memory.last_used_at or memory.created_at
    days = max(0.0, (now - anchor).total_seconds() / 86400)
    if memory.type == "project":
        return math.exp(-days / 30.0)
    return math.exp(-days / 90.0)


def _memory_lines(title: str, memories: list[UserMemory]) -> str | None:
    if not memories:
        return None
    lines = [f"<{title}>"]
    for memory in memories:
        lines.append(f"- {memory.content}")
    lines.append(f"</{title}>")
    return "\n".join(lines)


async def conversation_disabled_memory_ids(
    redis: Any | None, conversation_id: str
) -> set[str]:
    if redis is None:
        return set()
    try:
        raw_values = await redis.smembers(
            f"memory:conversation:{conversation_id}:disabled"
        )
    except Exception:
        return set()
    disabled: set[str] = set()
    for value in raw_values or []:
        if isinstance(value, bytes):
            disabled.add(value.decode("utf-8", errors="ignore"))
        elif isinstance(value, str):
            disabled.add(value)
    return disabled


def _clip_lines(memories: list[UserMemory], *, max_chars: int) -> list[UserMemory]:
    out: list[UserMemory] = []
    used = 0
    for memory in memories:
        cost = len(memory.content) + 4
        if out and used + cost > max_chars:
            break
        out.append(memory)
        used += cost
    return out
