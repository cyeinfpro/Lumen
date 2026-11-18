"""Database-backed completion history loading and context compression."""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from functools import partial
import hashlib
import logging
import time
from typing import Any

from sqlalchemy import and_, desc, or_, select

from lumen_core.constants import Role
from lumen_core.context_window import (
    HISTORY_FETCH_BATCH,
    MESSAGE_OVERHEAD_TOKENS,
    format_sticky_input_text,
    format_summary_input_text,
    is_summary_usable,
)
from lumen_core.models import Conversation, Image, ImageVariant, Message

from .context import PackedContext
from .history import (
    SummaryBoundary as _SummaryBoundary,
    instructions_with_summary_guardrail as _instructions_with_summary_guardrail,
    message_after_summary as _message_after_summary,
    message_created_at as _message_created_at,
    role_eq as _role_eq,
    sticky_text_from_message as _sticky_text_from_message,
)

ATTACHMENT_READ_TIMEOUT_S = 30.0
ATTACHMENT_READ_CONCURRENCY = 4
ATTACHMENT_CACHE_MAX_BYTES = 32 * 1024 * 1024
ATTACHMENT_CACHE_TTL_S = 300.0
HISTORY_SCAN_MAX_MESSAGES = 2048


@dataclass(frozen=True, slots=True)
class _ResolvedAttachment:
    image_id: str
    storage_key: str
    mime: str
    sha256: str
    is_preview: bool


@dataclass(frozen=True, slots=True)
class _CachedAttachment:
    data_url: str
    mime: str
    is_preview: bool
    expires_at: float
    size_bytes: int


class AttachmentDataUrlCache:
    """Bounded SHA-256 keyed LRU cache for encoded vision context data URLs."""

    def __init__(
        self,
        *,
        max_bytes: int = ATTACHMENT_CACHE_MAX_BYTES,
        ttl_seconds: float = ATTACHMENT_CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_bytes = max(0, int(max_bytes))
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._clock = clock
        self._entries: OrderedDict[str, _CachedAttachment] = OrderedDict()
        self._total_bytes = 0

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    @property
    def current_bytes(self) -> int:
        return self._total_bytes

    def __len__(self) -> int:
        return len(self._entries)

    def _evict_expired(self, now: float) -> None:
        expired_keys = [
            key for key, entry in self._entries.items() if now >= entry.expires_at
        ]
        for key in expired_keys:
            removed = self._entries.pop(key)
            self._total_bytes = max(0, self._total_bytes - removed.size_bytes)

    def get(
        self,
        sha256: str,
        *,
        mime: str | None = None,
        is_preview: bool | None = None,
    ) -> str | None:
        if not sha256 or self._max_bytes <= 0 or self._ttl_seconds <= 0.0:
            return None
        now = float(self._clock())
        self._evict_expired(now)
        entry = self._entries.get(sha256)
        if entry is None:
            return None
        if mime is not None and entry.mime != mime:
            return None
        if is_preview is not None and entry.is_preview != is_preview:
            return None
        self._entries.move_to_end(sha256)
        return entry.data_url

    def put(
        self,
        sha256: str,
        data_url: str,
        *,
        mime: str,
        is_preview: bool,
    ) -> None:
        if not sha256 or not data_url or self._max_bytes <= 0 or self._ttl_seconds <= 0:
            return
        size_bytes = len(data_url)
        if size_bytes > self._max_bytes:
            return
        now = float(self._clock())
        self._evict_expired(now)
        previous = self._entries.pop(sha256, None)
        if previous is not None:
            self._total_bytes = max(0, self._total_bytes - previous.size_bytes)
        while self._entries and self._total_bytes + size_bytes > self._max_bytes:
            _old_key, evicted = self._entries.popitem(last=False)
            self._total_bytes = max(0, self._total_bytes - evicted.size_bytes)
        self._entries[sha256] = _CachedAttachment(
            data_url=data_url,
            mime=mime,
            is_preview=is_preview,
            expires_at=now + self._ttl_seconds,
            size_bytes=size_bytes,
        )
        self._total_bytes += size_bytes

    def clear(self) -> None:
        self._entries.clear()
        self._total_bytes = 0


@dataclass(frozen=True)
class ContextLoadingHooks:
    count_message_tokens: Callable[[str, dict[str, Any] | None], int]
    count_tokens: Callable[[str], int]
    estimate_system_prompt_tokens: Callable[[str | None], int]
    get_input_budget: Callable[[str | None], int]
    message_retention_filter_for_account: Callable[[str | None], Awaitable[Any]]
    resolve_summary_model: Callable[[], Awaitable[str]]
    resolve_int_setting: Callable[[str, int], Awaitable[int]]
    ensure_context_summary: Callable[..., Awaitable[dict[str, Any] | None]]
    build_input_from_packed_context: Callable[..., Awaitable[list[dict[str, Any]]]]
    load_rows_desc: Callable[..., Awaitable[tuple[list[Message], int, bool]]]
    load_rows_desc_after_summary: Callable[..., Awaitable[list[Message]]]
    pick_first_user_from_summary: Callable[..., Awaitable[Message | None]]
    pick_current_user_with_lookup: Callable[..., Awaitable[Message | None]]
    pick_first_user: Callable[[list[Message]], Message | None]
    pick_current_user: Callable[[list[Message], Message], Message | None]
    context_circuit_open: Callable[[Any | None], Awaitable[bool]]
    input_token_budget: int
    compression_enabled_default: int
    compression_trigger_percent_default: int
    summary_target_tokens_default: int
    summary_min_recent_messages_default: int
    summary_min_interval_seconds_default: int
    logger: logging.Logger


def _read_and_encode_data_url_sync(
    storage_get_bytes: Callable[[str], bytes],
    key: str,
    mime: str,
) -> str:
    raw = storage_get_bytes(key)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _attachment_cache_key(item: _ResolvedAttachment) -> str:
    if item.sha256:
        return item.sha256
    return hashlib.sha256(item.storage_key.encode("utf-8")).hexdigest()


async def _batch_resolve_attachments(
    session: Any,
    image_ids: list[str],
) -> dict[str, _ResolvedAttachment]:
    if not image_ids:
        return {}
    stmt = (
        select(
            Image.id,
            Image.storage_key,
            Image.mime,
            Image.sha256,
            ImageVariant.storage_key.label("preview_storage_key"),
        )
        .outerjoin(
            ImageVariant,
            and_(
                ImageVariant.image_id == Image.id,
                ImageVariant.kind == "preview1024",
            ),
        )
        .where(
            Image.id.in_(image_ids),
            Image.deleted_at.is_(None),
        )
    )
    rows = (await session.execute(stmt)).all()
    commit = getattr(session, "commit", None)
    if callable(commit):
        await commit()

    resolved: dict[str, _ResolvedAttachment] = {}
    for img_id, orig_key, orig_mime, sha256, preview_key in rows:
        resolved_id = str(img_id)
        if preview_key:
            resolved[resolved_id] = _ResolvedAttachment(
                image_id=resolved_id,
                storage_key=str(preview_key),
                mime="image/webp",
                sha256=str(sha256 or ""),
                is_preview=True,
            )
        else:
            resolved[resolved_id] = _ResolvedAttachment(
                image_id=resolved_id,
                storage_key=str(orig_key),
                mime=str(orig_mime or "image/png"),
                sha256=str(sha256 or ""),
                is_preview=False,
            )
    return resolved


async def _read_single_resolved_attachment(
    item: _ResolvedAttachment,
    *,
    storage_get_bytes: Callable[[str], bytes],
    logger: logging.Logger,
    attachment_cache: AttachmentDataUrlCache | None = None,
) -> str | None:
    cache_key = _attachment_cache_key(item)
    if attachment_cache is not None:
        cached = attachment_cache.get(
            cache_key,
            mime=item.mime,
            is_preview=item.is_preview,
        )
        if cached is not None:
            return cached
    try:
        async with asyncio.timeout(ATTACHMENT_READ_TIMEOUT_S):
            data_url = await asyncio.to_thread(
                _read_and_encode_data_url_sync,
                storage_get_bytes,
                item.storage_key,
                item.mime,
            )
    except TimeoutError:
        logger.warning(
            "attachment read timed out image_id=%s key=%s",
            item.image_id,
            item.storage_key,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "attachment read failed image_id=%s err=%s",
            item.image_id,
            exc,
        )
        return None
    if attachment_cache is not None:
        attachment_cache.put(
            cache_key,
            data_url,
            mime=item.mime,
            is_preview=item.is_preview,
        )
    return data_url


async def _load_resolved_attachments_parallel(
    resolved: dict[str, _ResolvedAttachment],
    *,
    storage_get_bytes: Callable[[str], bytes],
    logger: logging.Logger,
    attachment_cache: AttachmentDataUrlCache | None = None,
    max_concurrency: int = ATTACHMENT_READ_CONCURRENCY,
) -> dict[str, str]:
    if not resolved:
        return {}
    by_image_id: dict[str, str] = {}
    misses_by_source: dict[tuple[str, str, bool], list[_ResolvedAttachment]] = {}
    for item in resolved.values():
        cache_key = _attachment_cache_key(item)
        if attachment_cache is not None:
            cached = attachment_cache.get(
                cache_key,
                mime=item.mime,
                is_preview=item.is_preview,
            )
            if cached is not None:
                by_image_id[item.image_id] = cached
                continue
        source_sig = (cache_key, item.mime, item.is_preview)
        misses_by_source.setdefault(source_sig, []).append(item)

    if not misses_by_source:
        return by_image_id

    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def _fetch_group(
        items: list[_ResolvedAttachment],
    ) -> tuple[list[_ResolvedAttachment], str | None]:
        async with semaphore:
            data_url = await _read_single_resolved_attachment(
                items[0],
                storage_get_bytes=storage_get_bytes,
                logger=logger,
                attachment_cache=attachment_cache,
            )
            return items, data_url

    results = await asyncio.gather(
        *(_fetch_group(items) for items in misses_by_source.values())
    )
    for items, data_url in results:
        if data_url is not None:
            for item in items:
                by_image_id[item.image_id] = data_url
    return by_image_id


def _extract_unique_attachment_ids(messages: Iterable[Message]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for message in messages:
        if not _role_eq(message.role, Role.USER):
            continue
        content = message.content or {}
        for attachment in content.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            image_id = attachment.get("image_id")
            if isinstance(image_id, str) and image_id and image_id not in seen:
                seen.add(image_id)
                ordered.append(image_id)
    return ordered


def _message_to_input_item_from_map(
    message: Message,
    data_urls: dict[str, str],
) -> dict[str, Any] | None:
    content = message.content or {}
    text = content.get("text") or ""
    if _role_eq(message.role, Role.USER):
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "input_text", "text": text})
        for attachment in content.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            image_id = attachment.get("image_id")
            if not isinstance(image_id, str) or not image_id:
                continue
            data_url = data_urls.get(image_id)
            if data_url:
                parts.append({"type": "input_image", "image_url": data_url})
        if parts:
            return {"role": "user", "content": parts}
    elif _role_eq(message.role, Role.ASSISTANT) and text:
        return {
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    return None


async def _attachment_to_data_url(
    session: Any,
    image_id: str,
    *,
    storage_get_bytes: Callable[[str], bytes],
    logger: logging.Logger,
    attachment_cache: AttachmentDataUrlCache | None = None,
) -> str | None:
    """Read a preview image and encode it as an inline Responses data URL."""
    if not image_id:
        return None
    resolved = await _batch_resolve_attachments(session, [image_id])
    item = resolved.get(image_id)
    if item is None:
        return None
    return await _read_single_resolved_attachment(
        item,
        storage_get_bytes=storage_get_bytes,
        logger=logger,
        attachment_cache=attachment_cache,
    )


async def _message_to_input_item(
    session: Any,
    message: Message,
    *,
    attachment_to_data_url: Callable[..., Awaitable[str | None]],
) -> dict[str, Any] | None:
    content = message.content or {}
    text = content.get("text") or ""
    if _role_eq(message.role, Role.USER):
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "input_text", "text": text})
        for attachment in content.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            image_id = attachment.get("image_id")
            if not image_id:
                continue
            data_url = await attachment_to_data_url(session, image_id)
            if data_url:
                parts.append({"type": "input_image", "image_url": data_url})
        if parts:
            return {"role": "user", "content": parts}
    elif _role_eq(message.role, Role.ASSISTANT) and text:
        return {
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    return None


async def _build_input_from_packed_context(
    session: Any,
    packed: PackedContext,
    *,
    message_to_input_item: Callable[..., Awaitable[dict[str, Any] | None]] | None = None,
    storage_get_bytes: Callable[[str], bytes] | None = None,
    attachment_cache: AttachmentDataUrlCache | None = None,
    logger: logging.Logger | None = None,
    max_concurrency: int = ATTACHMENT_READ_CONCURRENCY,
) -> list[dict[str, Any]]:
    input_list: list[dict[str, Any]] = []
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

    if storage_get_bytes is not None:
        unique_image_ids = _extract_unique_attachment_ids(packed._recent_rows)
        resolved = await _batch_resolve_attachments(session, unique_image_ids)
        preloaded_urls = await _load_resolved_attachments_parallel(
            resolved,
            storage_get_bytes=storage_get_bytes,
            logger=logger or logging.getLogger(__name__),
            attachment_cache=attachment_cache,
            max_concurrency=max_concurrency,
        )
        for message in packed._recent_rows:
            item = _message_to_input_item_from_map(message, preloaded_urls)
            if item is not None:
                input_list.append(item)
        return input_list

    if message_to_input_item is None:
        raise TypeError("message_to_input_item or storage_get_bytes is required")
    for message in packed._recent_rows:
        item = await message_to_input_item(session, message)
        if item is not None:
            input_list.append(item)
    return input_list


def bind_runtime_pack_recent_history(
    *,
    hooks_provider: Callable[[], ContextLoadingHooks],
    build_input: Callable[..., Awaitable[list[dict[str, Any]]]],
    attachment_cache: AttachmentDataUrlCache,
) -> Callable[..., Awaitable[PackedContext]]:
    async def _runtime_pack_recent_history(
        session: Any,
        *,
        conversation_id: str,
        up_to_message_id: str,
        system_prompt: str | None,
        redis: Any | None = None,
        chat_model: str | None = None,
        account_mode: str | None = None,
    ) -> PackedContext:
        hooks = replace(
            hooks_provider(),
            build_input_from_packed_context=partial(
                build_input,
                attachment_cache=attachment_cache,
            ),
        )
        return await _pack_recent_history(
            session,
            conversation_id=conversation_id,
            up_to_message_id=up_to_message_id,
            system_prompt=system_prompt,
            redis=redis,
            chat_model=chat_model,
            account_mode=account_mode,
            hooks=hooks,
        )

    setattr(_runtime_pack_recent_history, "attachment_cache", attachment_cache)
    return _runtime_pack_recent_history


def _reestimate_existing_summary_tokens(
    packed: PackedContext,
    *,
    system_prompt: str | None,
    hooks: ContextLoadingHooks,
) -> PackedContext:
    instructions = _instructions_with_summary_guardrail(system_prompt, enabled=True)
    used_tokens = hooks.estimate_system_prompt_tokens(instructions)
    if packed.sticky_used and packed._sticky_message is not None:
        sticky_text = format_sticky_input_text(
            _sticky_text_from_message(packed._sticky_message)
        )
        used_tokens += MESSAGE_OVERHEAD_TOKENS + hooks.count_tokens(sticky_text)
    if packed.summary_used:
        used_tokens += MESSAGE_OVERHEAD_TOKENS + packed.summary_tokens
    used_tokens += sum(
        hooks.count_message_tokens(message.role, message.content)
        for message in packed._recent_rows
    )
    return replace(packed, estimated_tokens=used_tokens)


async def _load_rows_desc(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    budget_tokens: int | None,
    system_prompt: str | None,
    retention_filter: Any | None = None,
    count_message_tokens: Callable[[str, dict[str, Any] | None], int],
    estimate_system_prompt_tokens: Callable[[str | None], int],
) -> tuple[list[Message], int, bool]:
    rows_desc: list[Message] = []
    used_tokens = estimate_system_prompt_tokens(system_prompt)
    cursor_created_at = target.created_at
    cursor_id = target.id
    cursor_inclusive = True
    truncated = False

    while True:
        remaining_messages = HISTORY_SCAN_MAX_MESSAGES - len(rows_desc)
        if remaining_messages <= 0:
            truncated = True
            break
        same_timestamp_filter = (
            Message.id <= cursor_id if cursor_inclusive else Message.id < cursor_id
        )
        query = (
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
                *((retention_filter,) if retention_filter is not None else ()),
            )
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(min(HISTORY_FETCH_BATCH, remaining_messages))
        )
        batch = list((await session.execute(query)).scalars())
        if not batch:
            break

        stop = False
        for message in batch:
            estimated_tokens = count_message_tokens(message.role, message.content)
            if estimated_tokens <= 0:
                cursor_created_at = message.created_at
                cursor_id = message.id
                continue
            if (
                budget_tokens is not None
                and used_tokens + estimated_tokens > budget_tokens
            ):
                stop = True
                truncated = True
                break
            rows_desc.append(message)
            used_tokens += estimated_tokens
            cursor_created_at = message.created_at
            cursor_id = message.id

        if len(rows_desc) >= HISTORY_SCAN_MAX_MESSAGES:
            truncated = True
            break
        if stop or len(batch) < min(HISTORY_FETCH_BATCH, remaining_messages):
            break
        cursor_inclusive = False

    return rows_desc, used_tokens, truncated


async def _load_rows_desc_after_summary(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    summary: dict[str, Any],
    retention_filter: Any | None = None,
) -> list[Message]:
    rows_desc: list[Message] = []
    cursor_created_at = target.created_at
    cursor_id = target.id
    cursor_inclusive = True

    while True:
        same_timestamp_filter = (
            Message.id <= cursor_id if cursor_inclusive else Message.id < cursor_id
        )
        query = (
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
                *((retention_filter,) if retention_filter is not None else ()),
            )
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(HISTORY_FETCH_BATCH)
        )
        batch = list((await session.execute(query)).scalars())
        if not batch:
            break

        stop = False
        for message in batch:
            cursor_created_at = message.created_at
            cursor_id = message.id
            if not _message_after_summary(summary, message):
                stop = True
                break
            rows_desc.append(message)

        if stop or len(batch) < HISTORY_FETCH_BATCH:
            break
        cursor_inclusive = False

    return rows_desc


async def _get_message(
    session: Any,
    message_id: str | None,
    *,
    logger: logging.Logger,
) -> Message | None:
    if not message_id:
        return None
    try:
        return await session.get(Message, message_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("completion.message_lookup_failed id=%s err=%r", message_id, exc)
        return None


async def _pick_first_user_from_summary(
    session: Any,
    summary: dict[str, Any],
    *,
    get_message: Callable[[Any, str | None], Awaitable[Message | None]],
) -> Message | None:
    first_id = summary.get("first_user_message_id")
    if not isinstance(first_id, str) or not first_id:
        return None
    message = await get_message(session, first_id)
    if message is not None and _role_eq(message.role, Role.USER):
        return message
    return None


async def _pick_current_user_with_lookup(
    session: Any,
    rows_desc: list[Message],
    target: Message,
    summary: dict[str, Any] | None = None,
    *,
    get_message: Callable[[Any, str | None], Awaitable[Message | None]],
) -> Message | None:
    current = _pick_current_user(rows_desc, target)
    if current is not None:
        return current
    parent_id = getattr(target, "parent_message_id", None)
    if not parent_id:
        return None
    candidate = await get_message(session, parent_id)
    if candidate is None or not _role_eq(candidate.role, Role.USER):
        return None
    if is_summary_usable(summary) and not _message_after_summary(summary, candidate):
        return None
    return candidate


def _pick_first_user(rows_desc: list[Message]) -> Message | None:
    users = [message for message in rows_desc if _role_eq(message.role, Role.USER)]
    if not users:
        return None
    return min(users, key=lambda message: (_message_created_at(message), message.id))


def _pick_current_user(
    rows_desc: list[Message],
    target: Message,
) -> Message | None:
    parent_id = getattr(target, "parent_message_id", None)
    if parent_id:
        for message in rows_desc:
            if message.id == parent_id and _role_eq(message.role, Role.USER):
                return message
    for message in rows_desc:
        if _role_eq(message.role, Role.USER):
            return message
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


async def _resolve_summary_model(
    *,
    runtime_settings: Any,
    logger: logging.Logger,
) -> str:
    try:
        raw = await runtime_settings.resolve("context.summary_model")
    except Exception as exc:  # noqa: BLE001
        logger.debug("context summary model setting fallback err=%s", exc)
        raw = None
    return raw or "gpt-5.4"


async def _resolve_int_setting(
    spec_key: str,
    default: int,
    *,
    runtime_settings: Any,
    logger: logging.Logger,
) -> int:
    try:
        return await runtime_settings.resolve_int(spec_key, default)
    except Exception as exc:  # noqa: BLE001
        logger.debug("context int setting fallback key=%s err=%s", spec_key, exc)
        return default


async def _ensure_context_summary(
    session: Any,
    conversation: Conversation,
    boundary: _SummaryBoundary,
    *,
    target_tokens: int,
    model: str,
    redis: Any | None,
    service: Any,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    ensure = (
        getattr(service, "ensure_context_summary", None)
        if service is not None
        else None
    )
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
        conversation,
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
    try:
        await session.refresh(conversation)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "context summary refresh failed conv=%s err=%s",
            conversation.id,
            exc,
        )
    latest_summary = getattr(conversation, "summary_jsonb", None)
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
    account_mode: str | None = None,
    hooks: ContextLoadingHooks,
) -> PackedContext:
    from .context_packing import pack_recent_history

    return await pack_recent_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
        redis=redis,
        chat_model=chat_model,
        account_mode=account_mode,
        hooks=hooks,
    )


async def _build_input_from_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
    pack_recent_history: Callable[..., Awaitable[PackedContext]],
) -> list[dict[str, Any]]:
    packed = await pack_recent_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
    )
    return packed.input_list


# Public contract consumed by the Completion runtime modules.
context_circuit_open = _context_circuit_open
pick_current_user = _pick_current_user
pick_first_user = _pick_first_user
