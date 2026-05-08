"""Shared account-memory helpers.

The production path can call an LLM for extraction/conflict checks; these
deterministic helpers are the local safety net used by API/worker tests and by
deployments that have not configured a memory-specific provider yet.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


MemoryType = Literal["profile", "preference", "avoid", "project"]
MemorySource = Literal["explicit", "auto", "manual"]
MemoryWriteKind = Literal[
    "added",
    "updated",
    "merged",
    "superseded",
    "staged",
    "rejected_pii",
]

MEMORY_TYPES = ("profile", "preference", "avoid", "project")
MEMORY_SOURCES = ("explicit", "auto", "manual")
STAGING_DECISIONS = ("pending", "accepted", "rejected")
DIRECTIVE_RE = re.compile(
    r"(记住|永远(?:记得)?|总是|不要(?:再)?|以后(?:都)?|从此|再也|remember|always|never|don'?t|stop)",
    re.IGNORECASE,
)
AUTO_HINT_RE = re.compile(
    r"(我是|我喜欢|我不喜欢|我不|不要|从来|永远|总是|在做|正在做|通常|一般|I am|I like|I prefer|I don't|I do not|never|always)",
    re.IGNORECASE,
)
PII_RE = re.compile(
    r"(密码|口令|验证码|身份证|银行卡|信用卡|API\s*key|api[_ -]?key|sk-[A-Za-z0-9]{12,}|"
    r"\b\d{6}\b|(?:\+?\d[\d -]{8,}\d)|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}.*(?:密码|password))",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(r"(不喜欢|不要|别|never|don't|do not|stop)", re.IGNORECASE)
LIKE_RE = re.compile(r"(喜欢|偏好|prefer|like)", re.IGNORECASE)


@dataclass(frozen=True)
class ExtractedMemory:
    type: MemoryType
    content: str
    confidence: float
    source_excerpt: str
    intent_kind: Literal["directive", "statement"] = "statement"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def has_pii(text: str) -> bool:
    return bool(PII_RE.search(text or ""))


def directive_intent(text: str) -> Literal["directive", "statement"] | None:
    """Return directive only for explicit "remember/记住" style instructions.

    Vague future-tense statements such as "我以后都不喝牛奶了" intentionally
    remain statements so they go through staging.
    """
    match = DIRECTIVE_RE.search(text or "")
    if match is None:
        return None
    keyword = match.group(0).lower()
    if "记住" in keyword or "remember" in keyword:
        return "directive"
    return "statement"


def should_auto_extract(text: str) -> bool:
    value = (text or "").strip()
    if len(value) < 30:
        return False
    return bool(AUTO_HINT_RE.search(value))


def source_excerpt(text: str, limit: int = 160) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def compact_content(text: str, limit: int = 200) -> str:
    value = " ".join((text or "").split(" "))
    value = re.sub(r"\s+", " ", value).strip(" ：:，,。.!！")
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _after_marker(text: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        idx = text.lower().find(marker.lower())
        if idx >= 0:
            return text[idx + len(marker) :]
    return None


def infer_memory_type(text: str) -> MemoryType:
    value = text or ""
    if re.search(r"(在做|正在做|项目|project|working on)", value, re.IGNORECASE):
        return "project"
    if NEGATION_RE.search(value):
        return "avoid"
    if LIKE_RE.search(value):
        return "preference"
    if re.search(r"(我是|I am|I'm|身份|role)", value, re.IGNORECASE):
        return "profile"
    return "preference"


def normalize_candidate_content(text: str, memory_type: MemoryType) -> str:
    raw = text or ""
    raw = re.sub(r"^(请你|请|以后|以后都|从此|总是|永远|记住|remember|always|never)\s*[:：,，]?", "", raw, flags=re.IGNORECASE)
    raw = raw.strip()
    if memory_type == "profile":
        tail = _after_marker(raw, ("我是", "I am", "I'm"))
        if tail:
            raw = "用户是" + tail
    elif memory_type == "project":
        tail = _after_marker(raw, ("在做", "正在做", "working on"))
        if tail:
            raw = "正在做" + tail
    elif memory_type == "avoid":
        tail = _after_marker(raw, ("不要", "不喜欢", "don't", "do not", "never", "stop"))
        if tail and not raw.startswith(("不要", "不喜欢")):
            raw = "不要" + tail
    elif memory_type == "preference":
        tail = _after_marker(raw, ("喜欢", "偏好", "prefer", "like"))
        if tail:
            raw = "喜欢" + tail
    return compact_content(raw)


def extract_memories(text: str, *, explicit_only: bool = False) -> tuple[list[ExtractedMemory], bool]:
    """Extract memory candidates without calling an upstream model.

    Returns (candidates, rejected_pii). The caller decides whether candidates
    go straight to the main table or staging based on confidence/source.
    """
    value = (text or "").strip()
    if not value:
        return [], False
    if has_pii(value):
        return [], True

    intent = directive_intent(value)
    if explicit_only and intent != "directive":
        return [], False
    if not explicit_only and intent is None and not should_auto_extract(value):
        return [], False

    memory_type = infer_memory_type(value)
    body = value
    if intent == "directive":
        body = re.sub(
            r"^.*?(记住|remember)\s*[:：,，]?",
            "",
            value,
            count=1,
            flags=re.IGNORECASE,
        )
    content = normalize_candidate_content(body, memory_type)
    if not content:
        return [], False
    confidence = 1.0 if intent == "directive" else 0.82
    return [
        ExtractedMemory(
            type=memory_type,
            content=content,
            confidence=confidence,
            source_excerpt=source_excerpt(value),
            intent_kind=intent or "statement",
        )
    ], False


def canonical_memory_text(text: str) -> str:
    value = compact_content(text).lower()
    return re.sub(r"[\s，,。.!！:：|]+", "", value)


def deterministic_embedding(text: str, *, dimensions: int = 3072) -> list[float]:
    seed = hashlib.sha256((text or "").encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dimensions:
        digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
        for byte in digest:
            values.append((byte / 255.0) * 2.0 - 1.0)
            if len(values) >= dimensions:
                break
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def embedding_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


def parse_embedding_literal(value: str | None) -> list[float]:
    if not value:
        return []
    stripped = value.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return []
    out: list[float] = []
    for item in stripped[1:-1].split(","):
        try:
            out.append(float(item))
        except ValueError:
            return []
    return out


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(a[i] * a[i] for i in range(n))) or 1.0
    nb = math.sqrt(sum(b[i] * b[i] for i in range(n))) or 1.0
    return dot / (na * nb)
