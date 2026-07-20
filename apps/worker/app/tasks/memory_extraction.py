"""Account memory extraction, staging, and prompt assembly."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select, update

from lumen_core.constants import conv_channel
from lumen_core.memory import (
    ExtractedMemory,
    canonical_memory_text,
    cosine_similarity,
    deterministic_embedding,
    embedding_literal,
    extract_memories,
    parse_embedding_literal,
)
from lumen_core.providers import resolve_provider_proxy_url
from lumen_core.models import (
    Conversation,
    MemoryAudit,
    Message,
    User,
    UserMemory,
    UserMemoryScope,
    UserMemoryStaging,
)

from ..db import SessionLocal
from ..sse_publish import publish_event
from .memory_extraction_parts.contracts import (
    CompletedMemoryExtraction as _CompletedMemoryExtraction,
    MemoryExtractionClaim as _MemoryExtractionClaim,
    PreparedMemoryCandidate as _PreparedMemoryCandidate,
    cancel_memory_extraction_run as _cancel_memory_extraction_run_impl,
    mark_memory_extraction_committed as _mark_memory_extraction_committed_impl,
    memory_extraction_event_id as _memory_extraction_event_id,
    memory_extraction_owner as _memory_extraction_owner,
    utc_now as _utc_now,
)
from .memory_extraction_parts.delivery import (
    CommittedDeliveryDependencies,
    append_memory_writes,
    cleanup_expired_memory_extraction_undo,
    load_committed_memory_extraction,
    mark_undo_delivery_ready,
    prune_expired_memory_extraction_undo,
    restore_undo_tokens,
)
from .memory_extraction_parts.run_state import (
    MemoryExtractionStateDependencies,
    abandon_memory_extraction_claim,
    claim_memory_extraction,
    finalize_memory_extraction,
    lock_memory_extraction_run,
)


_UNDO_TTL_SECONDS = 300
_STAGING_TTL_DAYS = 7
_MEMORY_EVENT = "memory.writes"
_MEMORY_EXTRACTION_MODEL = "gpt-5.4-mini"
_EMBEDDING_MODEL = "text-embedding-3-large"
_LLM_EXTRACTION_TIMEOUT_S = 25.0
_EMBEDDING_TIMEOUT_S = 15.0
_CONFIRM_WEEKLY_LIMIT = 5
_LAST_USED_PENDING_KEY = "memory:last_used_pending"
_MAX_POSITIVE_SIGNAL = 20
_MEMORY_EXTRACTION_LEASE_SECONDS = 600
_MEMORY_UNDO_CLEANUP_BATCH = 1000

_logger = logging.getLogger(__name__)


async def _embedding_provider_available(ctx: dict[str, Any] | None) -> bool:
    """Worker-side capability gate, mirrors api.runtime_settings helper.

    Without an embedding-purpose provider the entire memory pipeline must
    short-circuit; deterministic placeholders never match real embeddings at
    retrieval time so writing them creates dead rows.
    """
    try:
        from ..provider_pool import get_pool

        pool = ctx.get("provider_pool") if isinstance(ctx, dict) else None
        if pool is None:
            pool = await get_pool()
        providers = await pool.peek(purpose="embedding")
        return bool(providers)
    except Exception:
        return False


async def _try_advisory_xact_lock(session: Any, key: str) -> None:
    """PG advisory_xact_lock; no-op on non-PG dialects (sqlite tests)."""
    bind = getattr(session, "bind", None)
    if bind is None:
        connection = getattr(session, "connection", None)
        if connection is not None:
            try:
                bind = await connection()
            except Exception:
                bind = None
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name != "postgresql":
        return
    try:
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))
    except Exception:
        return


async def _lock_memory_extraction_run(
    session: Any,
    *,
    event_id: str,
) -> Any:
    return await lock_memory_extraction_run(
        session,
        event_id=event_id,
        advisory_xact_lock=_try_advisory_xact_lock,
    )


def _cancel_memory_extraction_run(
    run: Any,
    *,
    reason: str,
) -> None:
    _cancel_memory_extraction_run_impl(run, reason=reason, now=_utc_now())


def _mark_memory_extraction_committed(
    run: Any,
    *,
    writes: list[dict[str, Any]],
    undo_operations: list[dict[str, Any]],
) -> None:
    _mark_memory_extraction_committed_impl(
        run,
        writes=writes,
        undo_operations=undo_operations,
        now=_utc_now(),
    )


_EXTRACTION_INSTRUCTIONS = """从用户单轮消息中抽取长期适用的账号记忆，输出严格 JSON。
输出格式: {"items":[{"type":"profile|preference|avoid|project","content":"<200字","confidence":0.0-1.0,"source_excerpt":"原文50-120字","intent_kind":"directive|statement"}]}。
只保存长期稳定的身份、偏好、禁忌、正在做的项目。不要保存今天/刚才/这次/上次这类短期事件。
电话、地址、身份证、银行卡、API key、密码、验证码、邮箱+密码组合等敏感信息一律输出空数组。
directive 只用于用户明确要求你记住，例如“记住…”或“remember…”。“我以后都不喝牛奶了”这类表态是 statement。
多数消息没有记忆点，允许输出 {"items":[]}。"""


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


async def _default_scope(session: Any, user_id: str) -> UserMemoryScope:
    scope = (
        await session.execute(
            select(UserMemoryScope).where(
                UserMemoryScope.user_id == user_id,
                UserMemoryScope.is_default.is_(True),
            )
        )
    ).scalar_one_or_none()
    if scope is not None:
        return scope
    scope = UserMemoryScope(user_id=user_id, name="default", is_default=True)
    session.add(scope)
    await session.flush()
    return scope


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


async def _embedding_vector(ctx: dict[str, Any] | None, content: str) -> list[float]:
    try:
        from ..provider_pool import get_pool, text_provider_attempt

        pool = ctx.get("provider_pool") if isinstance(ctx, dict) else None
        if pool is None:
            pool = await get_pool()
        providers = await pool.select(purpose="embedding")
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "memory_extraction.embedding_provider_select_failed fallback=deterministic err=%s",
            exc,
        )
        return deterministic_embedding(content)

    provider_names: list[str] = []
    for provider in providers:
        provider_name = str(getattr(provider, "name", "<unknown>"))
        provider_names.append(provider_name)
        try:
            with text_provider_attempt(pool, provider) as provider_attempt:
                try:
                    proxy_url = await resolve_provider_proxy_url(
                        getattr(provider, "proxy", None)
                    )
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            connect=5.0,
                            read=_EMBEDDING_TIMEOUT_S,
                            write=_EMBEDDING_TIMEOUT_S,
                            pool=5.0,
                        ),
                        proxy=proxy_url,
                    ) as client:
                        resp = await client.post(
                            _append_path(provider.base_url, "/embeddings"),
                            json={"model": _EMBEDDING_MODEL, "input": content},
                            headers={
                                "authorization": f"Bearer {provider.api_key}",
                                "content-type": "application/json",
                            },
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    provider_attempt.report_exception(exc)
                    raise
                if resp.status_code >= 400:
                    provider_attempt.report_failure()
                else:
                    provider_attempt.report_success()
            if resp.status_code >= 400:
                _logger.debug(
                    "memory_extraction.embedding_provider_non_2xx provider=%s status=%s",
                    provider_name,
                    resp.status_code,
                )
                continue
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            first = data[0] if isinstance(data, list) and data else None
            vector = first.get("embedding") if isinstance(first, dict) else None
            if isinstance(vector, list) and vector:
                return [float(value) for value in vector]
            _logger.debug(
                "memory_extraction.embedding_provider_invalid_payload provider=%s",
                provider_name,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.debug(
                "memory_extraction.embedding_provider_failed provider=%s err=%s",
                provider_name,
                exc,
            )
            continue
    _logger.warning(
        "memory_extraction.embedding_fallback provider_count=%d providers=%s fallback=deterministic",
        len(provider_names),
        ",".join(provider_names) or "<none>",
    )
    return deterministic_embedding(content)


async def _embedding_literal_async(ctx: dict[str, Any] | None, content: str) -> str:
    return embedding_literal(await _embedding_vector(ctx, content))


async def _try_llm_extract(
    text: str,
    *,
    explicit_only: bool,
    scope_hint: str | None = None,
) -> list[ExtractedMemory]:
    try:
        from ..provider_pool import get_pool, text_provider_attempt
        from ..retry import is_retriable as classify_retriable
        from ..upstream import responses_call

        pool = await get_pool()
        providers = await pool.select(purpose="chat")
    except Exception:
        return []

    user_prompt = {
        "message": text,
        "explicit_only": explicit_only,
    }
    if scope_hint:
        user_prompt["active_scope"] = scope_hint
    for provider in providers:
        body = {
            "model": _MEMORY_EXTRACTION_MODEL,
            "instructions": _EXTRACTION_INSTRUCTIONS,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(user_prompt, ensure_ascii=False),
                        }
                    ],
                }
            ],
            "stream": False,
            "store": False,
            "reasoning": {"effort": "minimal"},
        }
        try:
            kwargs: dict[str, Any] = {
                "route": "text",
                "api_key_override": provider.api_key,
                "base_url_override": provider.base_url,
                "timeout_s": _LLM_EXTRACTION_TIMEOUT_S,
                "endpoint_label": "responses_memory_extract",
            }
            if getattr(provider, "proxy", None) is not None:
                kwargs["proxy_override"] = provider.proxy
            with text_provider_attempt(pool, provider) as provider_attempt:
                try:
                    payload = await responses_call(body, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    provider_attempt.report_exception(exc)
                    raise
                else:
                    provider_attempt.report_success()
            _log_llm_usage(str(getattr(provider, "name", "<unknown>")), payload)
            items = _parse_llm_candidates(_responses_text(payload))
            if explicit_only:
                items = [item for item in items if item.intent_kind == "directive"]
            if items:
                return items
        except Exception as exc:  # noqa: BLE001
            if not classify_retriable(
                getattr(exc, "error_code", None),
                getattr(exc, "status_code", None),
                error_message=str(exc),
            ):
                continue
            continue
    return []


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


async def _conversation_disabled_memory_ids(
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


async def _memory_scope_context(
    session: Any,
    *,
    user_id: str,
    conversation: Conversation,
) -> tuple[set[str], UserMemoryScope | None]:
    default_scope = await _default_scope(session, user_id)
    scope_ids = {default_scope.id}
    active_scope = None
    if conversation.active_scope_id:
        scope_ids.add(conversation.active_scope_id)
        active_scope = await session.get(
            UserMemoryScope,
            conversation.active_scope_id,
        )
    return scope_ids, active_scope


async def _prompt_memory_rows(
    session: Any,
    *,
    user_id: str,
    conversation_id: str,
    scope_ids: set[str],
    redis: Any | None,
) -> list[UserMemory]:
    rows = list(
        (
            await session.execute(
                select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.disabled.is_(False),
                    UserMemory.superseded_by.is_(None),
                    UserMemory.scope_id.in_(scope_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    disabled_ids = await _conversation_disabled_memory_ids(redis, conversation_id)
    if not disabled_ids:
        return rows
    return [memory for memory in rows if memory.id not in disabled_ids]


async def _ranked_prompt_memories(
    rows: list[UserMemory],
    *,
    user_text: str,
    now: datetime,
) -> tuple[list[UserMemory], list[UserMemory], list[UserMemory], list[float] | None]:
    profiles = [memory for memory in rows if memory.type == "profile"]
    avoids = [memory for memory in rows if memory.type == "avoid"]
    pinned = [memory for memory in rows if memory.pinned]
    candidates = [
        memory
        for memory in rows
        if memory.type in {"preference", "project"} and not memory.pinned
    ]

    query_vec = None
    ranked: list[tuple[float, UserMemory]] = []
    if len((user_text or "").strip()) >= 5:
        query_vec = await _embedding_vector(None, user_text)
        for memory in candidates:
            memory_vec = parse_embedding_literal(
                memory.embedding
            ) or deterministic_embedding(memory.content)
            score = (
                cosine_similarity(query_vec, memory_vec)
                * (1 + 0.1 * memory.positive_signal - 0.15 * memory.negative_signal)
                * _decay(memory, now)
            )
            ranked.append((score, memory))
    ranked.sort(key=lambda item: item[0], reverse=True)

    context_memories: list[UserMemory] = []
    seen: set[str] = set()
    for memory in [*pinned, *[memory for _, memory in ranked[:8]]]:
        if memory.id in seen or memory.type in {"profile", "avoid"}:
            continue
        seen.add(memory.id)
        context_memories.append(memory)
    return profiles, avoids, context_memories, query_vec


def _clipped_prompt_memories(
    profiles: list[UserMemory],
    avoids: list[UserMemory],
    context_memories: list[UserMemory],
) -> tuple[list[UserMemory], list[UserMemory], list[UserMemory]]:
    profiles = _clip_lines(
        sorted(profiles, key=lambda memory: (not memory.pinned, -memory.confidence)),
        max_chars=400,
    )
    avoids = _clip_lines(
        sorted(avoids, key=lambda memory: (not memory.pinned, -memory.confidence)),
        max_chars=400,
    )
    return profiles, avoids, _clip_lines(context_memories, max_chars=600)


async def _record_used_memories(
    session: Any,
    *,
    redis: Any | None,
    used_ids: list[str],
    now: datetime,
) -> None:
    if not used_ids:
        return
    flushed = False
    if redis is not None:
        try:
            pipe = redis.pipeline(transaction=False)
            score = now.timestamp()
            for memory_id in used_ids:
                pipe.zadd(_LAST_USED_PENDING_KEY, {memory_id: score})
            await pipe.execute()
            flushed = True
        except Exception:
            flushed = False
    if flushed:
        return
    await session.execute(
        update(UserMemory)
        .where(UserMemory.id.in_(used_ids))
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )


def _confirmation_instruction(memory: UserMemory | None) -> str | None:
    if memory is None:
        return None
    return (
        f"如果用户问题与用户偏好「{memory.content}」高度相关,"
        "请在回答开头用一句话简短确认:「按你之前提到的这个偏好来吗?」再继续回答。"
        "不要解释为什么记得。"
    )


async def assemble_user_memory_prompt(
    session: Any,
    *,
    user_id: str,
    conversation_id: str,
    user_text: str,
    redis: Any | None = None,
    parent_user_message_id: str | None = None,
) -> AssembledMemoryPrompt:
    user = await session.get(User, user_id)
    conv = await session.get(Conversation, conversation_id)
    if user is None or conv is None:
        return AssembledMemoryPrompt(None, None, None, [], [])
    if user.memory_disabled or conv.memory_disabled:
        return AssembledMemoryPrompt(None, None, None, [], [])
    if not await _embedding_provider_available(None):
        return AssembledMemoryPrompt(None, None, None, [], [])

    scope_ids, active_scope = await _memory_scope_context(
        session,
        user_id=user_id,
        conversation=conv,
    )
    rows = await _prompt_memory_rows(
        session,
        user_id=user_id,
        conversation_id=conversation_id,
        scope_ids=scope_ids,
        redis=redis,
    )
    now = datetime.now(timezone.utc)
    profiles, avoids, context_memories, query_vec = await _ranked_prompt_memories(
        rows,
        user_text=user_text,
        now=now,
    )
    profiles, avoids, context_memories = _clipped_prompt_memories(
        profiles,
        avoids,
        context_memories,
    )

    used = [*profiles, *avoids, *context_memories]
    used_ids = [m.id for m in used]
    used_summary = [{"id": m.id, "type": m.type, "content": m.content} for m in used]
    await _record_used_memories(
        session,
        redis=redis,
        used_ids=used_ids,
        now=now,
    )

    confirmation_candidate = await _pick_confirmation_candidate(
        session,
        context_memories + avoids,
        user=user,
        user_text=user_text,
        now=now,
        conversation_id=conversation_id,
        parent_user_message_id=parent_user_message_id,
        query_vec=query_vec,
    )

    return AssembledMemoryPrompt(
        profile_text=_memory_lines("user_profile", profiles),
        constraints_text=_memory_lines("user_constraints", avoids),
        context_text=_memory_lines("user_context", context_memories),
        used_memory_ids=used_ids,
        used_memory_summary=used_summary,
        scope_hint_text=(
            f"本会话上下文领域: {active_scope.name}"
            if active_scope is not None and not active_scope.is_default
            else None
        ),
        confirmation_candidate_id=confirmation_candidate.id
        if confirmation_candidate
        else None,
        confirmation_instruction=_confirmation_instruction(confirmation_candidate),
    )


async def _pick_confirmation_candidate(
    session: Any,
    memories: list[UserMemory],
    *,
    user: User,
    user_text: str,
    now: datetime,
    conversation_id: str,
    parent_user_message_id: str | None,
    query_vec: list[float] | None = None,
) -> UserMemory | None:
    if not user.confirmation_enabled:
        return None
    if re.search(
        r"(记住|remember|以后|不要|never|always)", user_text or "", re.IGNORECASE
    ):
        return None
    if parent_user_message_id:
        # advisory lock: 在 (user, conversation, day) 维度串行化 select-then-insert,
        # 避免并发请求各自看到 daily_count=0 后双双 prompt 同一记忆 (设计 §16.4 daily=1).
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        await _try_advisory_xact_lock(
            session,
            f"{user.id}:confirm_prompt:{conversation_id}:{int(day_start.timestamp())}",
        )
        week_cutoff = now - timedelta(days=7)
        weekly_count = (
            await session.execute(
                select(func.count(MemoryAudit.id)).where(
                    MemoryAudit.user_id == user.id,
                    MemoryAudit.event_type == "confirm_prompted",
                    MemoryAudit.created_at >= week_cutoff,
                )
            )
        ).scalar_one()
        if int(weekly_count or 0) >= _CONFIRM_WEEKLY_LIMIT:
            return None

        daily_count = (
            await session.execute(
                select(func.count(MemoryAudit.id))
                .select_from(MemoryAudit)
                .join(Message, MemoryAudit.source_message_id == Message.id)
                .where(
                    MemoryAudit.user_id == user.id,
                    MemoryAudit.event_type == "confirm_prompted",
                    MemoryAudit.created_at >= day_start,
                    Message.conversation_id == conversation_id,
                )
            )
        ).scalar_one()
        if int(daily_count or 0) > 0:
            return None
    for memory in sorted(memories, key=lambda m: m.positive_signal, reverse=True):
        if memory.type not in {"preference", "avoid"}:
            continue
        if memory.positive_signal < 3:
            continue
        if memory.last_confirmed_at and (now - memory.last_confirmed_at).days < 14:
            continue
        if parent_user_message_id:
            prompted_count = (
                await session.execute(
                    select(func.count(MemoryAudit.id))
                    .select_from(MemoryAudit)
                    .join(Message, MemoryAudit.source_message_id == Message.id)
                    .where(
                        MemoryAudit.user_id == user.id,
                        MemoryAudit.memory_id == memory.id,
                        MemoryAudit.event_type == "confirm_prompted",
                        Message.conversation_id == conversation_id,
                    )
                )
            ).scalar_one()
            if int(prompted_count or 0) > 0:
                continue
        score = cosine_similarity(
            query_vec or deterministic_embedding(user_text),
            parse_embedding_literal(memory.embedding)
            or deterministic_embedding(memory.content),
        )
        if score >= 0.92:
            if parent_user_message_id:
                session.add(
                    MemoryAudit(
                        user_id=user.id,
                        memory_id=memory.id,
                        event_type="confirm_prompted",
                        source_message_id=parent_user_message_id,
                        details={
                            "conversation_id": conversation_id,
                            "weekly_limit": _CONFIRM_WEEKLY_LIMIT,
                        },
                    )
                )
                await session.flush()
            return memory
    return None


async def _publish_memory_writes(
    redis: Any,
    *,
    user_id: str,
    conversation_id: str,
    assistant_message_id: str,
    event_id: str,
    writes: list[dict[str, Any]],
) -> None:
    if not writes:
        return
    await publish_event(
        redis,
        user_id,
        conv_channel(conversation_id),
        _MEMORY_EVENT,
        {
            "event_id": event_id,
            "conversation_id": conversation_id,
            "assistant_message_id": assistant_message_id,
            "message_id": assistant_message_id,
            "memory_writes": writes,
        },
    )


async def _append_writes_to_message(
    session: Any,
    assistant_message_id: str,
    writes: list[dict[str, Any]],
) -> Message | None:
    return await append_memory_writes(
        session,
        assistant_message_id,
        writes,
    )


def _memory_extraction_state_dependencies() -> MemoryExtractionStateDependencies:
    return MemoryExtractionStateDependencies(
        session_factory=SessionLocal,
        advisory_xact_lock=_try_advisory_xact_lock,
        append_writes_to_message=_append_writes_to_message,
        default_scope=_default_scope,
        text_from_message=_text_from_message,
        topic_key=_topic_key,
        bump_positive_signal=_bump_positive_signal,
        now=_utc_now,
        lease_seconds=_MEMORY_EXTRACTION_LEASE_SECONDS,
        staging_ttl_days=_STAGING_TTL_DAYS,
    )


async def _claim_memory_extraction(
    *,
    conversation_id: str,
    source_message_id: str,
    assistant_message_id: str,
    event_id: str,
    owner: str,
    job_id: str | None,
) -> _MemoryExtractionClaim | _CompletedMemoryExtraction | None:
    return await claim_memory_extraction(
        _memory_extraction_state_dependencies(),
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        assistant_message_id=assistant_message_id,
        event_id=event_id,
        owner=owner,
        job_id=job_id,
    )


async def _abandon_memory_extraction_claim(
    claim: _MemoryExtractionClaim,
    *,
    reason: str,
) -> bool:
    return await abandon_memory_extraction_claim(
        _memory_extraction_state_dependencies(),
        claim,
        reason=reason,
    )


async def _best_effort_abandon_memory_extraction_claim(
    claim: _MemoryExtractionClaim,
    *,
    reason: str,
) -> None:
    try:
        await _abandon_memory_extraction_claim(claim, reason=reason)
    except Exception:  # noqa: BLE001
        _logger.warning(
            "memory_extraction.claim_abandon_failed message=%s owner=%s fence=%s",
            claim.source_message_id,
            claim.owner,
            claim.fence,
            exc_info=True,
        )


async def _prepare_memory_extraction(
    ctx: dict[str, Any],
    claim: _MemoryExtractionClaim,
) -> tuple[list[_PreparedMemoryCandidate], bool]:
    candidates, rejected_pii = extract_memories(claim.text, explicit_only=False)
    if candidates and not rejected_pii:
        llm_candidates = await _try_llm_extract(
            claim.text,
            explicit_only=False,
            scope_hint=claim.scope_hint,
        )
        if llm_candidates:
            candidates = llm_candidates
    prepared: list[_PreparedMemoryCandidate] = []
    for candidate in candidates:
        prepared.append(
            _PreparedMemoryCandidate(
                candidate=candidate,
                embedding=await _embedding_literal_async(ctx, candidate.content),
            )
        )
    return prepared, rejected_pii


async def _finalize_memory_extraction(
    claim: _MemoryExtractionClaim,
    *,
    prepared_candidates: list[_PreparedMemoryCandidate],
    rejected_pii: bool,
) -> _CompletedMemoryExtraction | None:
    return await finalize_memory_extraction(
        _memory_extraction_state_dependencies(),
        claim,
        prepared_candidates=prepared_candidates,
        rejected_pii=rejected_pii,
    )


def _committed_delivery_dependencies() -> CommittedDeliveryDependencies:
    return CommittedDeliveryDependencies(
        session_factory=SessionLocal,
        advisory_xact_lock=_try_advisory_xact_lock,
        append_writes_to_message=_append_writes_to_message,
        now=_utc_now,
        logger=_logger,
        undo_ttl_seconds=_UNDO_TTL_SECONDS,
        undo_cleanup_batch=_MEMORY_UNDO_CLEANUP_BATCH,
    )


async def _prune_expired_memory_extraction_undo(
    event_id: str,
    *,
    now: datetime,
) -> bool:
    return await prune_expired_memory_extraction_undo(
        _committed_delivery_dependencies(),
        event_id,
        now=now,
    )


async def _load_committed_memory_extraction(
    event_id: str,
) -> _CompletedMemoryExtraction | None:
    return await load_committed_memory_extraction(
        _committed_delivery_dependencies(),
        event_id,
    )


async def _mark_undo_delivery_ready(
    completed: _CompletedMemoryExtraction,
) -> None:
    await mark_undo_delivery_ready(
        _committed_delivery_dependencies(),
        completed,
    )


async def _restore_undo_tokens(
    redis: Any,
    completed: _CompletedMemoryExtraction,
) -> None:
    await restore_undo_tokens(
        _committed_delivery_dependencies(),
        redis,
        completed,
    )


async def _prepare_committed_memory_extraction_for_delivery(
    redis: Any,
    event_id: str,
) -> _CompletedMemoryExtraction | None:
    completed = await _load_committed_memory_extraction(event_id)
    if completed is None:
        return None
    if completed.undo_operations:
        await _restore_undo_tokens(redis, completed)
    return completed


async def memory_extract(
    ctx: dict[str, Any],
    conversation_id: str,
    user_msg_id: str,
    assistant_msg_id: str,
) -> None:
    redis = ctx.get("redis")
    event_id = _memory_extraction_event_id(user_msg_id, assistant_msg_id)
    owner, job_id = _memory_extraction_owner(ctx, assistant_msg_id)
    claim_result = await _claim_memory_extraction(
        conversation_id=conversation_id,
        source_message_id=user_msg_id,
        assistant_message_id=assistant_msg_id,
        event_id=event_id,
        owner=owner,
        job_id=job_id,
    )
    if isinstance(claim_result, _CompletedMemoryExtraction):
        if redis is not None:
            completed = await _prepare_committed_memory_extraction_for_delivery(
                redis,
                claim_result.event_id,
            )
        else:
            completed = None
        if completed is not None and completed.writes:
            await _publish_memory_writes(
                redis,
                user_id=completed.user_id,
                conversation_id=completed.conversation_id,
                assistant_message_id=completed.assistant_message_id,
                event_id=completed.event_id,
                writes=completed.writes,
            )
        return
    if claim_result is None:
        return

    claim = claim_result
    try:
        if not await _embedding_provider_available(ctx):
            await _best_effort_abandon_memory_extraction_claim(
                claim,
                reason="embedding_provider_unavailable",
            )
            return
        prepared_candidates, rejected_pii = await _prepare_memory_extraction(
            ctx,
            claim,
        )
        completed = await _finalize_memory_extraction(
            claim,
            prepared_candidates=prepared_candidates,
            rejected_pii=rejected_pii,
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            _best_effort_abandon_memory_extraction_claim(
                claim,
                reason="worker_cancelled",
            )
        )
        raise
    except Exception as exc:
        await _best_effort_abandon_memory_extraction_claim(
            claim,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise
    if completed is None or redis is None:
        return

    completed = await _prepare_committed_memory_extraction_for_delivery(
        redis,
        completed.event_id,
    )
    if completed is not None and completed.writes:
        await _publish_memory_writes(
            redis,
            user_id=completed.user_id,
            conversation_id=completed.conversation_id,
            assistant_message_id=completed.assistant_message_id,
            event_id=completed.event_id,
            writes=completed.writes,
        )


async def cleanup_memory(ctx: dict[str, Any]) -> None:
    _ = ctx
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    async with SessionLocal() as session:
        pending = (
            (
                await session.execute(
                    select(UserMemoryStaging).where(
                        UserMemoryStaging.decision == "pending",
                        UserMemoryStaging.expires_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in pending:
            row.decision = "rejected"
            row.decided_at = now
        old_deleted = (
            (
                await session.execute(
                    select(UserMemory).where(
                        UserMemory.disabled.is_(True),
                        UserMemory.deleted_at.is_not(None),
                        UserMemory.deleted_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for memory in old_deleted:
            await session.delete(memory)
        await session.commit()
    await _cleanup_expired_memory_extraction_undo(now)


async def _cleanup_expired_memory_extraction_undo(now: datetime) -> int:
    return await cleanup_expired_memory_extraction_undo(
        _committed_delivery_dependencies(),
        now,
    )


async def memory_reembed(
    ctx: dict[str, Any],
    target: str,
    row_id: str,
) -> None:
    """Compute a real LLM embedding for a manually-created memory or staging row.

    API CRUD writes the row with embedding=NULL (or a stale deterministic
    placeholder) and enqueues this job; here we compute a real vector via the
    embedding-purpose provider pool and overwrite. Failure falls back to the
    deterministic helper so retrieval still works (less precisely).
    """
    if target not in {"memory", "staging"}:
        return
    if not await _embedding_provider_available(ctx):
        return
    async with SessionLocal() as session:
        row: UserMemory | UserMemoryStaging | None
        if target == "memory":
            row = await session.get(UserMemory, row_id)
        else:
            row = await session.get(UserMemoryStaging, row_id)
        if row is None:
            return
        content = getattr(row, "content", "") or ""
        if not content.strip():
            return
        try:
            row.embedding = await _embedding_literal_async(ctx, content)
        except Exception:
            row.embedding = embedding_literal(deterministic_embedding(content))
        await session.commit()


async def flush_memory_last_used(ctx: dict[str, Any]) -> None:
    """Drain memory:last_used_pending ZSET into one batched UPDATE per timestamp.

    Why ZSET: assemble_user_memory_prompt fan-outs N writes per chat turn; per
    DESIGN §7.3 step 8 we batch them into one cron tick instead of hammering
    user_memories with row-by-row UPDATEs. Race: a second producer may bump a
    member's score between our zrange and zrem; we lose at most one timestamp
    update — non-fatal because last_used_at only feeds decay scoring.
    """
    redis = ctx.get("redis")
    if redis is None:
        return
    try:
        members = await redis.zrange(_LAST_USED_PENDING_KEY, 0, -1, withscores=True)
    except Exception:
        return
    if not members:
        return
    by_score: dict[float, list[str]] = defaultdict(list)
    member_names: list[str] = []
    for member, score in members:
        if isinstance(member, bytes):
            member = member.decode("utf-8", errors="ignore")
        if not isinstance(member, str):
            continue
        member_names.append(member)
        by_score[float(score)].append(member)
    if not by_score:
        return
    async with SessionLocal() as session:
        for score, ids in by_score.items():
            ts = datetime.fromtimestamp(score, tz=timezone.utc)
            await session.execute(
                update(UserMemory)
                .where(UserMemory.id.in_(ids))
                .values(last_used_at=ts)
                .execution_options(synchronize_session=False)
            )
        await session.commit()
    try:
        await redis.zrem(_LAST_USED_PENDING_KEY, *member_names)
    except Exception:
        _logger.warning(
            "flush_memory_last_used zrem failed members=%d",
            len(member_names),
        )
