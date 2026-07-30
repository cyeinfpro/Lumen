"""System prompt and BYOK credential resolution for message submission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from types import MappingProxyType
from typing import Any, Awaitable, Callable
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import DEFAULT_CHAT_MODEL, MAX_PROMPT_CHARS
from lumen_core.models import (
    ApiSupplierTemplate,
    Conversation,
    SystemPrompt,
    UserApiCredential,
)

from ..byok_service import read_byok_settings_cached
from .message_submission_billing import http_error

logger = logging.getLogger(__name__)
AsyncCallable = Callable[..., Awaitable[Any]]
SYSTEM_PROMPT_SOURCE_LIMIT = MAX_PROMPT_CHARS
_PROMPT_CONTROL_TRANSLATION = MappingProxyType(
    {**{i: " " for i in range(32) if i not in (9, 10, 13)}, 127: " "}
)
_SYSTEM_PROMPT_SECTION_TAG_RE = re.compile(r"(\[/?)(SYSTEM_[A-Z0-9_]+)(\])")
_SYSTEM_PROMPT_SECTION_TAG_ESCAPE = "\u200b"


@dataclass(frozen=True)
class TaskCredentialPin:
    credential_id: str
    supplier_id: str
    default_chat_model: str
    fast_chat_model: str | None
    default_image_model: str | None


def _non_blank(text: str | None) -> str | None:
    if text is None:
        return None
    return text if text.strip() else None


def sanitize_system_prompt_source(text: str | None) -> str | None:
    prompt = _non_blank(text)
    if prompt is None:
        return None
    normalized = unicodedata.normalize("NFKC", prompt)
    cleaned = normalized.translate(_PROMPT_CONTROL_TRANSLATION).strip()
    if not cleaned:
        return None
    if len(cleaned) > SYSTEM_PROMPT_SOURCE_LIMIT:
        logger.warning(
            "system prompt source truncated: original_len=%d limit=%d",
            len(cleaned),
            SYSTEM_PROMPT_SOURCE_LIMIT,
        )
        cleaned = cleaned[:SYSTEM_PROMPT_SOURCE_LIMIT]
    return cleaned


def _escape_system_prompt_section_body(text: str) -> str:
    return _SYSTEM_PROMPT_SECTION_TAG_RE.sub(
        lambda match: (
            f"{match.group(1)}{_SYSTEM_PROMPT_SECTION_TAG_ESCAPE}"
            f"{match.group(2)}{match.group(3)}"
        ),
        text,
    )


def build_structured_system_prompt(
    *,
    explicit_prompt: str | None,
    conversation_prompt: str | None,
    legacy_conversation_prompt: str | None,
    global_prompt: str | None,
) -> str | None:
    sections: list[str] = []
    for tag, candidate in (
        ("SYSTEM_GLOBAL", global_prompt),
        ("SYSTEM_CONVERSATION_LEGACY", legacy_conversation_prompt),
        ("SYSTEM_CONVERSATION", conversation_prompt),
        ("SYSTEM_EXPLICIT", explicit_prompt),
    ):
        prompt = sanitize_system_prompt_source(candidate)
        if prompt is not None:
            safe_prompt = _escape_system_prompt_section_body(prompt)
            sections.append(f"[{tag}]\n{safe_prompt}\n[/{tag}]")
    if not sections:
        return None
    return "\n".join(("[SYSTEM_PROMPTS]", *sections, "[/SYSTEM_PROMPTS]"))


async def _load_owned_prompt_content(
    db: AsyncSession,
    *,
    user_id: str,
    prompt_id: str | None,
) -> str | None:
    if not prompt_id:
        return None
    return (
        await db.execute(
            select(SystemPrompt.content).where(
                SystemPrompt.id == prompt_id,
                SystemPrompt.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def resolve_system_prompt_for_message(
    db: AsyncSession,
    *,
    user_id: str,
    default_system_prompt_id: str | None,
    conv: Conversation,
    explicit_prompt: str | None,
) -> str | None:
    conversation_prompt = await _load_owned_prompt_content(
        db,
        user_id=user_id,
        prompt_id=conv.default_system_prompt_id,
    )
    global_prompt = await _load_owned_prompt_content(
        db,
        user_id=user_id,
        prompt_id=default_system_prompt_id,
    )
    return build_structured_system_prompt(
        explicit_prompt=explicit_prompt,
        conversation_prompt=conversation_prompt,
        legacy_conversation_prompt=conv.default_system,
        global_prompt=global_prompt,
    )


async def resolve_task_credential_pin(
    db: AsyncSession,
    user_id: str,
    required_purpose: str,
    account_mode: str,
    *,
    read_byok_settings_cached_fn: AsyncCallable = read_byok_settings_cached,
) -> TaskCredentialPin | None:
    if account_mode != "byok":
        return None
    byok_settings = await read_byok_settings_cached_fn(db)
    if not byok_settings.mode_enabled:
        raise http_error("byok_disabled", "BYOK is disabled", 403)
    active_row = (
        await db.execute(
            select(UserApiCredential, ApiSupplierTemplate)
            .join(
                ApiSupplierTemplate,
                ApiSupplierTemplate.id == UserApiCredential.supplier_id,
            )
            .where(
                UserApiCredential.user_id == user_id,
                UserApiCredential.status == "active",
                UserApiCredential.deleted_at.is_(None),
                ApiSupplierTemplate.deleted_at.is_(None),
                ApiSupplierTemplate.enabled.is_(True),
            )
            .order_by(UserApiCredential.created_at.desc())
            .limit(1)
        )
    ).first()
    if active_row is not None:
        active, supplier = active_row
        rate_limited_until = getattr(active, "rate_limited_until", None)
        if rate_limited_until is not None:
            if rate_limited_until.tzinfo is None:
                rate_limited_until = rate_limited_until.replace(tzinfo=timezone.utc)
            if rate_limited_until > datetime.now(timezone.utc):
                raise http_error(
                    "NO_ACTIVE_API_KEY",
                    "your API key is currently rate limited",
                    412,
                )
        if required_purpose not in set(supplier.purposes or []):
            raise http_error(
                "NO_ACTIVE_API_KEY",
                "your current API Key does not support this task type",
                412,
            )
        return TaskCredentialPin(
            credential_id=active.id,
            supplier_id=active.supplier_id,
            default_chat_model=supplier.default_chat_model or DEFAULT_CHAT_MODEL,
            fast_chat_model=supplier.fast_chat_model,
            default_image_model=getattr(supplier, "default_image_model", None),
        )
    raise http_error(
        "NO_ACTIVE_API_KEY",
        "please upload an active API key before starting new tasks",
        412,
    )
