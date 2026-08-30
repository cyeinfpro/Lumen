"""Narrow, high-confidence Agent text safety boundary."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


AGENT_CONTENT_POLICY_CODE = "content_policy_violation"
AGENT_CONTENT_SAFETY_CATEGORY = "cse_high_confidence"
AGENT_CONTENT_SAFETY_HOLDBACK_CHARS = 768

_MINOR = re.compile(
    r"\b(?:child|children|minor|minors|underage|prepubescent|preteen|toddler|"
    r"infant|schoolgirl|schoolboy)\b|未成年(?:人)?|儿童|幼童|幼女|幼男|小孩",
    re.IGNORECASE,
)
_SEXUAL = re.compile(
    r"\b(?:porn(?:ography|ographic)?|sex(?:ual|ually)?|rape|nude|naked|"
    r"genitals?|molest(?:ation|ed)?|explicit)\b|色情|性爱|性交|裸照|裸体|"
    r"性器官|强奸|性侵|性虐待",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"\b(?:create|generate|make|produce|write|depict|draw|render|show|send|"
    r"share|distribute|distribution|upload|download|sell|find|get|give me|provide)\b|"
    r"生成|制作|创作|描写|绘制|展示|发送|分享|传播|上传|下载|售卖|寻找|提供",
    re.IGNORECASE,
)
_ILLICIT_TERM = re.compile(
    r"\b(?:c\s*s\s*a\s*m|csem|child\s+(?:sex(?:ual)?\s+abuse\s+material|porn(?:ography)?))\b|"
    r"儿童色情|未成年人色情",
    re.IGNORECASE,
)
_PROTECTIVE_CONTEXT = re.compile(
    r"\b(?:prevent|prevention|report|reporting|detect|detection|protect|"
    r"protection|investigat(?:e|ion)|prosecut(?:e|ion)|law|legal|policy|"
    r"awareness|victim support|against)\b|"
    r"预防|举报|检测|保护|调查|起诉|法律|政策|科普|受害者援助|反对|抵制",
    re.IGNORECASE,
)
_REFUSAL_CONTEXT = re.compile(
    r"\b(?:cannot|can't|will not|won't|decline to|refuse to|do not|don't)\b|"
    r"不能|无法|不会|拒绝",
    re.IGNORECASE,
)
_NEGATED_REFUSAL = re.compile(
    r"\b(?:do not|don't|never)\s+(?:refuse|decline)\b|不要拒绝|不得拒绝",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(
    r"(?:[.!?;:,\n。！？；：，]+|\b(?:but|however|then)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AgentContentSafetyDecision:
    blocked: bool
    category: str | None = None


class AgentContentSafetyViolation(ValueError):
    code = AGENT_CONTENT_POLICY_CODE
    category = AGENT_CONTENT_SAFETY_CATEGORY

    def __init__(self) -> None:
        super().__init__("Agent content policy violation")


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = "".join(
        " " if unicodedata.category(char)[0] in {"P", "S", "Z", "C"} else char
        for char in normalized
    )
    return " ".join(normalized.split())


def _clauses(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = re.sub(r"\bc[\W_]*s[\W_]*a[\W_]*m\b", "csam", normalized)
    normalized = re.sub(r"\bc[\W_]*s[\W_]*e[\W_]*m\b", "csem", normalized)
    return [
        clause
        for raw_clause in _CLAUSE_SPLIT.split(normalized)
        if (clause := _normalized(raw_clause))
    ]


def _protective_or_refusal(value: str) -> bool:
    if _PROTECTIVE_CONTEXT.search(value) is not None:
        return True
    return (
        _REFUSAL_CONTEXT.search(value) is not None
        and _NEGATED_REFUSAL.search(value) is None
    )


def _unsafe_clause(value: str) -> bool:
    direct_term = _ILLICIT_TERM.search(value) is not None
    conjunctive = _MINOR.search(value) is not None and _SEXUAL.search(value) is not None
    return (
        (direct_term or conjunctive)
        and _ACTION.search(value) is not None
        and not _protective_or_refusal(value)
    )


def agent_content_safety_decision(text: str) -> AgentContentSafetyDecision:
    if not isinstance(text, str) or not text.strip():
        return AgentContentSafetyDecision(False)
    for clause in _clauses(text):
        for index in range(0, len(clause), 320):
            if _unsafe_clause(clause[index : index + 640]):
                return AgentContentSafetyDecision(True, AGENT_CONTENT_SAFETY_CATEGORY)
    return AgentContentSafetyDecision(False)


def require_agent_content_safe(text: str) -> None:
    if agent_content_safety_decision(text).blocked:
        raise AgentContentSafetyViolation()


__all__ = [
    "AGENT_CONTENT_POLICY_CODE",
    "AGENT_CONTENT_SAFETY_CATEGORY",
    "AGENT_CONTENT_SAFETY_HOLDBACK_CHARS",
    "AgentContentSafetyDecision",
    "AgentContentSafetyViolation",
    "agent_content_safety_decision",
    "require_agent_content_safe",
]
