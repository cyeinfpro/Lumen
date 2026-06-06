"""Account memory extraction, staging, and prompt assembly."""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
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


_UNDO_TTL_SECONDS = 300
_STAGING_TTL_DAYS = 7
_MEMORY_EVENT = "memory.writes"
_MEMORY_EXTRACTION_MODEL = "gpt-5.4-mini"
_EMBEDDING_MODEL = "text-embedding-3-large"
_LLM_EXTRACTION_TIMEOUT_S = 25.0
_EMBEDDING_TIMEOUT_S = 15.0
_CONFIRM_WEEKLY_LIMIT = 5
_LAST_USED_PENDING_KEY = "memory:last_used_pending"

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
        providers = await pool.select(purpose="embedding")
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


async def _embedding_vector(ctx: dict[str, Any] | None, content: str) -> list[float]:
    try:
        from ..provider_pool import get_pool

        pool = ctx.get("provider_pool") if isinstance(ctx, dict) else None
        if pool is None:
            pool = await get_pool()
        providers = await pool.select(purpose="embedding")
    except Exception:
        return deterministic_embedding(content)

    for provider in providers:
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
            if resp.status_code >= 400:
                continue
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            first = data[0] if isinstance(data, list) and data else None
            vector = first.get("embedding") if isinstance(first, dict) else None
            if isinstance(vector, list) and vector:
                return [float(value) for value in vector]
        except Exception:
            continue
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
        from ..provider_pool import get_pool
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
            payload = await responses_call(body, **kwargs)
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

    default_scope = await _default_scope(session, user_id)
    scope_ids = {default_scope.id}
    active_scope: UserMemoryScope | None = None
    if conv.active_scope_id:
        scope_ids.add(conv.active_scope_id)
        active_scope = await session.get(UserMemoryScope, conv.active_scope_id)
    rows = (
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
    if disabled_ids:
        rows = [memory for memory in rows if memory.id not in disabled_ids]
    now = datetime.now(timezone.utc)

    profiles = [m for m in rows if m.type == "profile"]
    avoids = [m for m in rows if m.type == "avoid"]
    pinned = [m for m in rows if m.pinned]
    candidates = [
        m for m in rows if m.type in {"preference", "project"} and not m.pinned
    ]

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
    for memory in [*pinned, *[m for _, m in ranked[:8]]]:
        if memory.id in seen or memory.type in {"profile", "avoid"}:
            continue
        seen.add(memory.id)
        context_memories.append(memory)

    profiles = _clip_lines(
        sorted(profiles, key=lambda m: (not m.pinned, -m.confidence)), max_chars=400
    )
    avoids = _clip_lines(
        sorted(avoids, key=lambda m: (not m.pinned, -m.confidence)), max_chars=400
    )
    context_memories = _clip_lines(context_memories, max_chars=600)

    used = [*profiles, *avoids, *context_memories]
    used_ids = [m.id for m in used]
    used_summary = [{"id": m.id, "type": m.type, "content": m.content} for m in used]
    if used_ids:
        # 高频热点写: 把 (memory_id -> ts) 写进 redis ZSET, 由 worker cron
        # `flush_memory_last_used` 每 30s 批量 UPDATE 一次. 取不到 redis 时
        # 退化为同步 UPDATE, 保证功能可用.
        flushed = False
        if redis is not None:
            try:
                pipe = redis.pipeline(transaction=False)
                score = now.timestamp()
                for mid in used_ids:
                    pipe.zadd(_LAST_USED_PENDING_KEY, {mid: score})
                await pipe.execute()
                flushed = True
            except Exception:
                flushed = False
        if not flushed:
            await session.execute(
                update(UserMemory)
                .where(UserMemory.id.in_(used_ids))
                .values(last_used_at=now)
                .execution_options(synchronize_session=False)
            )

    confirmation_candidate = await _pick_confirmation_candidate(
        session,
        context_memories + avoids,
        user=user,
        user_text=user_text,
        now=now,
        conversation_id=conversation_id,
        parent_user_message_id=parent_user_message_id,
        query_vec=query_vec if "query_vec" in locals() else None,
    )
    confirmation_instruction = None
    if confirmation_candidate is not None:
        confirmation_instruction = (
            f"如果用户问题与用户偏好「{confirmation_candidate.content}」高度相关,"
            "请在回答开头用一句话简短确认:「按你之前提到的这个偏好来吗?」再继续回答。"
            "不要解释为什么记得。"
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
        confirmation_instruction=confirmation_instruction,
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


async def _undo_token(redis: Any, payload: dict[str, Any]) -> str | None:
    token = secrets.token_urlsafe(24)
    try:
        await redis.setex(
            f"memory:undo:{token}",
            _UNDO_TTL_SECONDS,
            json.dumps(payload, separators=(",", ":")),
        )
        return token
    except Exception:
        return None


def _write_payload(
    *,
    id: str | None,
    kind: str,
    type: str | None,
    content: str,
    source_excerpt: str | None,
    undo_token: str | None = None,
    scope_id: str | None = None,
    recommended_scope_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "kind": kind,
        "type": type,
        "content": content,
        "source_excerpt": source_excerpt,
        "undo_token": undo_token,
        "scope_id": scope_id,
        "recommended_scope_id": recommended_scope_id,
    }


async def _publish_memory_writes(
    redis: Any,
    *,
    user_id: str,
    conversation_id: str,
    assistant_message_id: str,
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
            "conversation_id": conversation_id,
            "assistant_message_id": assistant_message_id,
            "message_id": assistant_message_id,
            "memory_writes": writes,
        },
    )


async def _append_writes_to_message(
    session: Any, assistant_msg: Message, writes: list[dict[str, Any]]
) -> None:
    if not writes:
        return
    content = dict(assistant_msg.content or {})
    existing = content.get("memory_writes")
    merged = [*(existing if isinstance(existing, list) else []), *writes]
    content["memory_writes"] = merged
    assistant_msg.content = content
    await session.flush()


async def memory_extract(
    ctx: dict[str, Any],
    conversation_id: str,
    user_msg_id: str,
    assistant_msg_id: str,
) -> None:
    if not await _embedding_provider_available(ctx):
        return
    redis = ctx.get("redis")
    async with SessionLocal() as session:
        conv = await session.get(Conversation, conversation_id)
        user_msg = await session.get(Message, user_msg_id)
        assistant_msg = await session.get(Message, assistant_msg_id)
        if conv is None or user_msg is None or assistant_msg is None:
            return
        user = await session.get(User, conv.user_id)
        if (
            user is None
            or user.memory_disabled
            or user.memory_paused
            or conv.memory_disabled
        ):
            return
        text = _text_from_message(user_msg)
        writes: list[dict[str, Any]] = []
        candidates, rejected_pii = extract_memories(text, explicit_only=False)
        if rejected_pii:
            writes.append(
                _write_payload(
                    id=None,
                    kind="rejected_pii",
                    type=None,
                    content="",
                    source_excerpt=" ".join(text.split())[:160],
                )
            )
        if not candidates and not writes:
            return
        default_scope = await _default_scope(session, user.id)
        scope_id = conv.active_scope_id or default_scope.id
        active_scope = await session.get(UserMemoryScope, scope_id)
        scope_hint = (
            active_scope.name if active_scope and not active_scope.is_default else None
        )
        if candidates and not rejected_pii:
            llm_candidates = await _try_llm_extract(
                text,
                explicit_only=False,
                scope_hint=scope_hint,
            )
            if llm_candidates:
                candidates = llm_candidates
        # 默认 0.80 (0020 migration), forget 反馈会推到 0.95 上限 / pin 反馈推到 0.6 下限.
        threshold = max(0.6, min(0.95, float(user.extraction_threshold or 0.80)))
        for candidate in candidates:
            existing = (
                (
                    await session.execute(
                        select(UserMemory).where(
                            UserMemory.user_id == user.id,
                            UserMemory.disabled.is_(False),
                            UserMemory.superseded_by.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            duplicate = next(
                (
                    m
                    for m in existing
                    if m.type == candidate.type
                    and canonical_memory_text(m.content)
                    == canonical_memory_text(candidate.content)
                ),
                None,
            )
            if duplicate is not None:
                duplicate.positive_signal += 1
                session.add(
                    MemoryAudit(
                        user_id=user.id,
                        memory_id=duplicate.id,
                        event_type="merged",
                        old_content=duplicate.content,
                        new_content=duplicate.content,
                        source_message_id=user_msg.id,
                        details={"source": "auto"},
                    )
                )
                # 把 candidate 元数据塞进 token, undo "merged" 时按设计 §5.4
                # 必须拆出独立条, 没这些字段就无法重建.
                token = (
                    await _undo_token(
                        redis,
                        {
                            "user_id": user.id,
                            "action": "merged",
                            "memory_id": duplicate.id,
                            "candidate": {
                                "type": candidate.type,
                                "content": candidate.content,
                                "source_excerpt": candidate.source_excerpt,
                                "source_message_id": user_msg.id,
                                "scope_id": scope_id,
                                "source": "auto",
                                "confidence": candidate.confidence,
                            },
                        },
                    )
                    if redis is not None
                    else None
                )
                writes.append(
                    _write_payload(
                        id=duplicate.id,
                        kind="merged",
                        type=duplicate.type,
                        content=duplicate.content,
                        source_excerpt=candidate.source_excerpt,
                        undo_token=token,
                        scope_id=duplicate.scope_id,
                        recommended_scope_id=scope_id,
                    )
                )
                continue
            conflict = next(
                (
                    m
                    for m in existing
                    if _topic_key(m.content)
                    and _topic_key(m.content) == _topic_key(candidate.content)
                    and m.type != candidate.type
                ),
                None,
            )
            if (
                candidate.confidence < threshold
                and candidate.intent_kind != "directive"
            ):
                staging = UserMemoryStaging(
                    user_id=user.id,
                    type=candidate.type,
                    content=candidate.content,
                    source_message_id=user_msg.id,
                    source_excerpt=candidate.source_excerpt,
                    source="auto",
                    embedding=await _embedding_literal_async(ctx, candidate.content),
                    confidence=candidate.confidence,
                    scope_id=scope_id,
                    recommended_scope_id=scope_id,
                    decision="pending",
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(days=_STAGING_TTL_DAYS),
                )
                session.add(staging)
                await session.flush()
                token = (
                    await _undo_token(
                        redis,
                        {
                            "user_id": user.id,
                            "action": "staged",
                            "staging_id": staging.id,
                        },
                    )
                    if redis is not None
                    else None
                )
                writes.append(
                    _write_payload(
                        id=staging.id,
                        kind="staged",
                        type=staging.type,
                        content=staging.content,
                        source_excerpt=staging.source_excerpt,
                        undo_token=token,
                        scope_id=staging.scope_id,
                        recommended_scope_id=staging.recommended_scope_id,
                    )
                )
                continue
            memory = UserMemory(
                user_id=user.id,
                type=candidate.type,
                content=candidate.content,
                source_message_id=user_msg.id,
                source_excerpt=candidate.source_excerpt,
                source="explicit" if candidate.intent_kind == "directive" else "auto",
                embedding=await _embedding_literal_async(ctx, candidate.content),
                confidence=max(candidate.confidence, threshold),
                scope_id=scope_id,
                last_used_at=datetime.now(timezone.utc),
            )
            session.add(memory)
            await session.flush()
            kind = "added"
            details: dict[str, Any] = {"source": memory.source}
            if conflict is not None:
                conflict.superseded_by = memory.id
                kind = "superseded"
                details["superseded_memory_id"] = conflict.id
            session.add(
                MemoryAudit(
                    user_id=user.id,
                    memory_id=memory.id,
                    event_type=kind,
                    old_content=conflict.content if conflict is not None else None,
                    new_content=memory.content,
                    source_message_id=user_msg.id,
                    details=details,
                )
            )
            token = (
                await _undo_token(
                    redis,
                    {
                        "user_id": user.id,
                        "action": kind,
                        "memory_id": memory.id,
                        "old_memory_id": conflict.id if conflict is not None else None,
                    },
                )
                if redis is not None
                else None
            )
            writes.append(
                _write_payload(
                    id=memory.id,
                    kind=kind,
                    type=memory.type,
                    content=memory.content,
                    source_excerpt=memory.source_excerpt,
                    undo_token=token,
                    scope_id=memory.scope_id,
                    recommended_scope_id=scope_id,
                )
            )
        await _append_writes_to_message(session, assistant_msg, writes)
        await session.commit()
    if redis is not None and writes:
        await _publish_memory_writes(
            redis,
            user_id=conv.user_id,
            conversation_id=conversation_id,
            assistant_message_id=assistant_msg_id,
            writes=writes,
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
