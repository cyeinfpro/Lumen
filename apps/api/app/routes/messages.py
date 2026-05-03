"""Messages 路由（DESIGN §5.4 — 核心写入接口）。

POST /conversations/{conv_id}/messages
1. 鉴权 + rate limit
2. 意图路由（auto → chat / vision_qa / text_to_image / image_to_image）
3. 出图参数校验 + 尺寸解析（lumen_core.sizing.resolve_size）
4. 幂等：(user, idempotency_key) 命中 → 直接返回既有三件套
5. 单事务：INSERT messages(user) + messages(assistant, pending) + 子任务 + outbox_events
6. 事务提交后尽力 XADD queue + PUBLISH task.queued + XADD events:user:{uid}
7. 返回 PostMessageOut
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    EV_COMP_QUEUED,
    EV_CONV_MSG_APPENDED,
    EV_GEN_QUEUED,
    EVENTS_STREAM_PREFIX,
    CompletionStage,
    CompletionStatus,
    GenerationAction,
    GenerationStage,
    GenerationStatus,
    IMAGE_MULTI_GEN_STAGGER_CAP_S,
    IMAGE_MULTI_GEN_STAGGER_S,
    Intent,
    MAX_PROMPT_CHARS,
    MessageStatus,
    Role,
    conv_channel,
    task_channel,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    Message,
    OutboxEvent,
    SystemPrompt,
    User,
)
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    MessageOut,
    PostMessageIn,
    PostMessageOut,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.sizing import ResolvedSize, resolve_size

from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..intent import resolve_intent
from ..ratelimit import MESSAGES_LIMITER
from ..redis_client import get_redis
from ..runtime_settings import get_setting


router = APIRouter()

logger = logging.getLogger(__name__)

# Why: align with the global ``MAX_PROMPT_CHARS`` so server-side truncation
# matches the validation cap exposed to clients. Previously this was a local
# 4096 constant while ``MAX_PROMPT_CHARS`` was 4000 elsewhere, allowing
# inconsistent truncation between layers.
SYSTEM_PROMPT_SOURCE_LIMIT = MAX_PROMPT_CHARS
ALLOWED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_VECTOR_STORE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_IMAGE_RENDER_QUALITY_VALUES = {"low", "medium", "high"}
_IMAGE_OUTPUT_FORMAT_VALUES = {"png", "jpeg", "webp"}
_DEFAULT_IMAGE_OUTPUT_FORMAT = "jpeg"
_IMAGE_BACKGROUND_VALUES = {"auto", "opaque", "transparent"}
_IMAGE_MODERATION_VALUES = {"auto", "low"}
_POST_COMMIT_PUBLISH_TIMEOUT_S = 2.0
_TRANSPARENT_BACKGROUND_RE = re.compile(
    r"透明(?:底|背景|底色)|去背|抠图|免抠|无背景|"
    r"transparent\s+(?:background|bg)|background\s+transparent|"
    r"(?:no|without)\s+(?:a\s+)?background|cutout|isolated\s+subject",
    re.IGNORECASE,
)
_TRANSPARENT_BACKGROUND_NEGATIVE_RE = re.compile(
    r"不(?:要|需要|用)?透明(?:底|背景|底色)?|非透明|opaque\s+background|"
    r"no\s+transparent\s+(?:background|bg)",
    re.IGNORECASE,
)
# 去除 C0 控制字符（\x00-\x1f）+ DEL（\x7f），但保留 \t (9) / \n (10) / \r (13)
# 以允许多行 prompt 的正常换行。prompt-injection 防御的目标是阻止像 \x1b 这种
# 终端转义、\x00 空字节注入，而不是把用户合法的换行也搞丢。
_SYSTEM_PROMPT_CONTROL_TRANSLATION = {
    i: " " for i in range(32) if i not in (9, 10, 13)
}
_SYSTEM_PROMPT_CONTROL_TRANSLATION[127] = " "


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


def _resolve_image_render_quality(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
) -> str:
    _ = resolved_size
    if image_params.render_quality in _IMAGE_RENDER_QUALITY_VALUES:
        return image_params.render_quality
    return "medium"


def _default_output_compression(
    *,
    render_quality: str,
    fast: bool,
) -> int:
    return 0


def _wants_transparent_background(prompt: str | None) -> bool:
    if not prompt:
        return False
    return bool(_TRANSPARENT_BACKGROUND_RE.search(prompt)) and not bool(
        _TRANSPARENT_BACKGROUND_NEGATIVE_RE.search(prompt)
    )


def _resolve_image_background(image_params: ImageParamsIn, prompt: str | None) -> str:
    background = (
        image_params.background
        if image_params.background in _IMAGE_BACKGROUND_VALUES
        else "auto"
    )
    if background == "auto" and _wants_transparent_background(prompt):
        return "transparent"
    return background


def _transparent_background_prompt_suffix() -> str:
    return (
        "\n\nRender the subject as a clean cutout on a true transparent alpha "
        "background. Do not paint a white, gray, checkerboard, wall, floor, or "
        "studio backdrop."
    )


def _image_upstream_request(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
    *,
    prompt: str | None = None,
    default_output_format: str = _DEFAULT_IMAGE_OUTPUT_FORMAT,
) -> dict[str, Any]:
    render_quality = _resolve_image_render_quality(image_params, resolved_size)
    background = _resolve_image_background(image_params, prompt)
    output_format_is_explicit = image_params.output_format in _IMAGE_OUTPUT_FORMAT_VALUES
    output_format = (
        image_params.output_format
        if output_format_is_explicit
        else default_output_format
        if default_output_format in _IMAGE_OUTPUT_FORMAT_VALUES
        else _DEFAULT_IMAGE_OUTPUT_FORMAT
    )
    output_format_source = "request" if output_format_is_explicit else "system_default"
    if background == "transparent":
        output_format = "png"
        output_format_source = "transparent_background"
    upstream_request: dict[str, Any] = {
        "fast": bool(image_params.fast),
        "responses_model": (
            DEFAULT_IMAGE_RESPONSES_MODEL_FAST
            if image_params.fast
            else DEFAULT_IMAGE_RESPONSES_MODEL
        ),
        "render_quality": render_quality,
        "output_format": output_format,
        "output_format_source": output_format_source,
        "background": background,
        "moderation": (
            image_params.moderation
            if image_params.moderation in _IMAGE_MODERATION_VALUES
            else "low"
        ),
    }
    if output_format in {"jpeg", "webp"}:
        upstream_request["output_compression"] = (
            _default_output_compression(
                render_quality=render_quality,
                fast=bool(image_params.fast),
            )
            if image_params.output_compression is None
            else image_params.output_compression
        )
    return upstream_request


def _chat_upstream_request(chat_params: ChatParamsIn) -> dict[str, Any] | None:
    req: dict[str, Any] = {}
    if chat_params.web_search:
        req["web_search"] = True
    if chat_params.file_search:
        vector_store_ids: list[str] = []
        seen: set[str] = set()
        for raw in chat_params.vector_store_ids:
            value = raw.strip()
            if not value:
                continue
            if not _VECTOR_STORE_ID_RE.fullmatch(value):
                raise _http(
                    "invalid_vector_store_id",
                    "invalid vector_store_ids entry",
                    422,
                )
            if value not in seen:
                seen.add(value)
                vector_store_ids.append(value)
        req["file_search"] = True
        if vector_store_ids:
            req["vector_store_ids"] = vector_store_ids
    if chat_params.code_interpreter:
        req["code_interpreter"] = True
    if chat_params.image_generation:
        req["image_generation"] = True
    return req or None


def _non_blank(text: str | None) -> str | None:
    if text is None:
        return None
    return text if text.strip() else None


def _sanitize_system_prompt_source(text: str | None) -> str | None:
    """NFKC normalize + 去除控制字符 + 长度截断到 SYSTEM_PROMPT_SOURCE_LIMIT。

    选 NFKC 而非 NFC 是有意的：NFKC 会把全角数字/同形异码字符统一成 ASCII 规范
    形式，更能对抗 prompt-injection 里故意用 Unicode 混淆分隔符的场景（例如用
    U+FF3B 『[』 伪造 [SYSTEM_GLOBAL] 标签）。对 system prompt 这类受控文本的
    轻微语义改写是可接受代价。
    """
    prompt = _non_blank(text)
    if prompt is None:
        return None
    normalized = unicodedata.normalize("NFKC", prompt)
    cleaned = normalized.translate(_SYSTEM_PROMPT_CONTROL_TRANSLATION).strip()
    if not cleaned:
        return None
    if len(cleaned) > SYSTEM_PROMPT_SOURCE_LIMIT:
        # 截断是 prompt-injection 防御的一部分：上限太高上游也会拒，放任不管
        # 会让"超长覆盖全局规则"成为攻击面。log 出原长度方便事后审计。
        logger.warning(
            "system prompt source truncated: original_len=%d limit=%d",
            len(cleaned),
            SYSTEM_PROMPT_SOURCE_LIMIT,
        )
        cleaned = cleaned[:SYSTEM_PROMPT_SOURCE_LIMIT]
    return cleaned


def choose_system_prompt(
    *,
    explicit_prompt: str | None,
    conversation_prompt: str | None,
    legacy_conversation_prompt: str | None,
    global_prompt: str | None,
) -> str | None:
    for candidate in (
        explicit_prompt,
        conversation_prompt,
        legacy_conversation_prompt,
        global_prompt,
    ):
        prompt = _sanitize_system_prompt_source(candidate)
        if prompt is not None:
            return prompt
    return None


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
        prompt = _sanitize_system_prompt_source(candidate)
        if prompt is not None:
            sections.append(f"[{tag}]\n{prompt}\n[/{tag}]")
    if not sections:
        return None
    return "\n".join(("[SYSTEM_PROMPTS]", *sections, "[/SYSTEM_PROMPTS]"))


def _message_alive_filters() -> tuple[Any, ...]:
    deleted_at = getattr(Message, "deleted_at", None)
    if deleted_at is None:
        return ()
    return (deleted_at.is_(None),)


async def _load_owned_prompt_content(
    db: AsyncSession, *, user_id: str, prompt_id: str | None
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
        db, user_id=user_id, prompt_id=conv.default_system_prompt_id
    )
    global_prompt = await _load_owned_prompt_content(
        db, user_id=user_id, prompt_id=default_system_prompt_id
    )
    return build_structured_system_prompt(
        explicit_prompt=explicit_prompt,
        conversation_prompt=conversation_prompt,
        legacy_conversation_prompt=conv.default_system,
        global_prompt=global_prompt,
    )


# ---------------------------------------------------------------------------
# Shared helper: build assistant message + completion/generations + outbox.
#
# Used by:
#   - POST /conversations/{conv_id}/messages         (this file)
#   - POST /conversations/{cid}/messages/{mid}/regenerate (regenerate.py)
#
# The helper assumes the caller has already created (and flushed) the *user*
# message. It returns the assistant message + ids created. The caller commits.
# ---------------------------------------------------------------------------


@dataclass
class AssistantTaskResult:
    assistant_msg: Message
    completion_id: str | None
    generation_ids: list[str]
    outbox_payloads: list[dict[str, Any]]
    outbox_rows: list[OutboxEvent]


async def _create_assistant_task(
    *,
    db: AsyncSession,
    user_id: str,
    conv: Conversation,
    user_msg: Message,
    intent: Intent,
    idempotency_key: str,
    image_params: ImageParamsIn,
    chat_params: ChatParamsIn,
    system_prompt: str | None,
    attachment_ids: list[str],
    text: str,
    default_image_output_format: str = _DEFAULT_IMAGE_OUTPUT_FORMAT,
) -> AssistantTaskResult:
    """Build assistant message + sub-task(s) + outbox in the open transaction.

    Caller is responsible for db.commit() and post-commit publish/enqueue.
    """
    produces_image = intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE)
    if intent == Intent.IMAGE_TO_IMAGE and not attachment_ids:
        raise _http(
            "missing_reference_image",
            "image_to_image requires at least one reference image",
            400,
        )

    # ---- size resolve (image intents only) ----
    resolved_size = None
    prompt_suffix = ""
    if produces_image:
        try:
            resolved_size = resolve_size(
                aspect=image_params.aspect_ratio,
                mode=image_params.size_mode,
                fixed=image_params.fixed_size,
            )
            prompt_suffix = resolved_size.prompt_suffix
        except Exception as e:  # noqa: BLE001
            raise _http("invalid_size", f"size resolve failed: {e}", 422)

    assistant_msg = Message(
        conversation_id=conv.id,
        role=Role.ASSISTANT.value,
        content={},
        parent_message_id=user_msg.id,
        intent=intent.value,
        status=MessageStatus.PENDING.value,
    )
    db.add(assistant_msg)
    await db.flush()

    completion_id: str | None = None
    generation_ids: list[str] = []
    outbox_payloads: list[dict[str, Any]] = []

    if intent in (Intent.CHAT, Intent.VISION_QA):
        comp = Completion(
            message_id=assistant_msg.id,
            user_id=user_id,
            input_image_ids=attachment_ids if intent == Intent.VISION_QA else [],
            system_prompt=system_prompt,
            text="",
            status=CompletionStatus.QUEUED.value,
            progress_stage=CompletionStage.QUEUED.value,
            attempt=0,
            idempotency_key=idempotency_key,
            upstream_request=_chat_upstream_request(chat_params),
        )
        db.add(comp)
        await db.flush()
        completion_id = comp.id
        outbox_payloads.append(
            {
                "task_id": comp.id,
                "user_id": user_id,
                "kind": "completion",
            }
        )
    else:
        # text_to_image / image_to_image
        count = max(1, min(16, image_params.count))
        action = (
            GenerationAction.EDIT.value
            if intent == Intent.IMAGE_TO_IMAGE
            else GenerationAction.GENERATE.value
        )
        primary = attachment_ids[0] if attachment_ids else None
        prompt_full = (text or "") + prompt_suffix
        assert resolved_size is not None  # guarded by produces_image branch
        upstream_request = _image_upstream_request(
            image_params,
            resolved_size,
            prompt=prompt_full,
            default_output_format=default_image_output_format,
        )
        if upstream_request.get("background") == "transparent":
            prompt_full += _transparent_background_prompt_suffix()

        for i in range(count):
            idem = idempotency_key if i == 0 else f"{idempotency_key}:{i}"
            gen = Generation(
                message_id=assistant_msg.id,
                user_id=user_id,
                action=action,
                prompt=prompt_full,
                size_requested=resolved_size.size,
                aspect_ratio=image_params.aspect_ratio,
                input_image_ids=attachment_ids,
                primary_input_image_id=primary,
                status=GenerationStatus.QUEUED.value,
                progress_stage=GenerationStage.QUEUED.value,
                attempt=0,
                idempotency_key=idem,
                upstream_request=dict(upstream_request),
            )
            db.add(gen)
            await db.flush()
            generation_ids.append(gen.id)
            # Stagger 多张图入队：i=0 立即跑，i>=1 延迟 i*STAGGER 秒（cap CAP）。
            # 实测同 prompt 同账号同时打 ChatGPT codex 会触发 OpenAI 内部 race（一败一成稳定模式）；
            # 错开几秒让第二条到达时第一条已分配好 image_generation slot，避免碰撞。
            defer_s = (
                min(i * IMAGE_MULTI_GEN_STAGGER_S, IMAGE_MULTI_GEN_STAGGER_CAP_S)
                if i > 0
                else 0
            )
            payload: dict[str, Any] = {
                "task_id": gen.id,
                "user_id": user_id,
                "kind": "generation",
            }
            if defer_s > 0:
                payload["defer_s"] = defer_s
            outbox_payloads.append(payload)

    # outbox rows (same transaction)
    outbox_rows: list[OutboxEvent] = []
    for p in outbox_payloads:
        ev = OutboxEvent(kind=p["kind"], payload=p, published_at=None)
        db.add(ev)
        outbox_rows.append(ev)

    return AssistantTaskResult(
        assistant_msg=assistant_msg,
        completion_id=completion_id,
        generation_ids=generation_ids,
        outbox_payloads=outbox_payloads,
        outbox_rows=outbox_rows,
    )


async def _publish_message_appended(
    *,
    redis: Any,
    user_id: str,
    conv_id: str,
    message_ids: list[str],
) -> None:
    """Best-effort publish for cross-device message list synchronization."""
    if not message_ids:
        return
    try:
        pipe = redis.pipeline(transaction=False)
        for message_id in message_ids:
            evt_data = json.dumps(
                {
                    "event": EV_CONV_MSG_APPENDED,
                    "data": {
                        "conversation_id": conv_id,
                        "message_id": message_id,
                    },
                },
                separators=(",", ":"),
            )
            pipe.publish(conv_channel(conv_id), evt_data)
            pipe.xadd(
                f"{EVENTS_STREAM_PREFIX}{user_id}",
                {"event": EV_CONV_MSG_APPENDED, "data": evt_data},
                maxlen=10000,
                approximate=True,
            )
        await pipe.execute()
    except Exception:
        logger.warning(
            "publish_message_appended failed user=%s conv=%s messages=%s",
            user_id,
            conv_id,
            message_ids,
            exc_info=True,
        )


async def _publish_assistant_task(
    *,
    db: AsyncSession,
    redis: Any,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str,
    outbox_payloads: list[dict[str, Any]],
    outbox_rows: list[OutboxEvent],
) -> None:
    """Best-effort: enqueue arq + publish/XADD for each outbox payload.

    Failures are logged; the outbox publisher will catch up. Caller must
    commit the transaction *before* invoking this.
    """
    try:
        pool = await get_arq_pool()
        pipe = redis.pipeline(transaction=False)
        for p in outbox_payloads:
            fn_name = (
                "run_completion" if p["kind"] == "completion" else "run_generation"
            )
            # 多图 stagger：i>=1 的 generation row 在 payload 里带 defer_s，让 arq 延迟入队执行。
            # 主路径（直接 enqueue）和降级路径（outbox publisher）都需要透传这个字段——
            # 之前漏了主路径，导致即使 payload 含 defer_s=5，task 也立刻被 worker 拉起。
            enqueue_kwargs: dict[str, Any] = {}
            defer_s = p.get("defer_s")
            if isinstance(defer_s, (int, float)) and defer_s > 0:
                enqueue_kwargs["_defer_by"] = float(defer_s)
            await pool.enqueue_job(fn_name, p["task_id"], **enqueue_kwargs)

            ev_name = EV_COMP_QUEUED if p["kind"] == "completion" else EV_GEN_QUEUED
            id_field = "completion_id" if p["kind"] == "completion" else "generation_id"
            evt_data = json.dumps(
                {
                    "event": ev_name,
                    "data": {
                        id_field: p["task_id"],
                        "message_id": assistant_msg_id,
                        "conversation_id": conv_id,
                        "kind": p["kind"],
                    },
                },
                separators=(",", ":"),
            )
            pipe.publish(task_channel(p["task_id"]), evt_data)
            pipe.xadd(
                f"{EVENTS_STREAM_PREFIX}{user_id}",
                {"event": ev_name, "data": evt_data},
                maxlen=10000,
                approximate=True,
            )
        await pipe.execute()
    except Exception:
        # Why: Outbox publisher will catch up later; surface in logs so silent
        # publish failures are observable without rolling back committed work.
        logger.warning(
            "publish_assistant_task failed user=%s conv=%s msg=%s",
            user_id,
            conv_id,
            assistant_msg_id,
            exc_info=True,
        )
        return

    # Why: only mark outbox rows published after the redis pipe succeeded.
    if outbox_rows:
        try:
            now2 = datetime.now(timezone.utc)
            for row in outbox_rows:
                row.published_at = now2
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                logger.warning(
                    "outbox row rollback failed user=%s msg=%s",
                    user_id,
                    assistant_msg_id,
                    exc_info=True,
                )
            logger.warning(
                "outbox row mark-published failed user=%s msg=%s",
                user_id,
                assistant_msg_id,
                exc_info=True,
            )


async def _await_post_commit_publish(
    label: str,
    awaitable: Awaitable[Any],
    *,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str | None = None,
) -> None:
    """Bound best-effort publishing so POST /messages can return promptly.

    The message, task, and outbox rows are already committed before this runs.
    If Redis or ARQ stalls here, the outbox publisher is still the durable source
    of truth and will enqueue the task shortly after.
    """
    try:
        await asyncio.wait_for(awaitable, timeout=_POST_COMMIT_PUBLISH_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "post_commit_publish timeout label=%s user=%s conv=%s msg=%s timeout_s=%.1f",
            label,
            user_id,
            conv_id,
            assistant_msg_id,
            _POST_COMMIT_PUBLISH_TIMEOUT_S,
        )
    except Exception:
        logger.warning(
            "post_commit_publish failed label=%s user=%s conv=%s msg=%s",
            label,
            user_id,
            conv_id,
            assistant_msg_id,
            exc_info=True,
        )


async def _lookup_idempotent_post(
    db: AsyncSession,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
) -> PostMessageOut | None:
    """Return prior PostMessageOut if (user, idempotency_key) already exists.

    Used for both the pre-check fast path and the IntegrityError fallback so the
    response shape stays bit-identical between concurrent and sequential cases.
    """
    alive_filters = _message_alive_filters()
    comp_hit = (
        await db.execute(
            select(Completion)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Completion.user_id == user_id,
                Completion.idempotency_key == idempotency_key,
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    gen_anchor = (
        await db.execute(
            select(Generation)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Generation.user_id == user_id,
                Generation.idempotency_key == idempotency_key,
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    if not comp_hit and not gen_anchor:
        return None

    anchor_msg_id = comp_hit.message_id if comp_hit else gen_anchor.message_id
    assistant_msg = (
        await db.execute(
            select(Message).where(
                Message.id == anchor_msg_id,
                Message.conversation_id == conv_id,
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    if assistant_msg is None:
        return None
    gen_hits: list[Generation] = []
    if gen_anchor is not None:
        gen_hits = (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.message_id == anchor_msg_id,
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        ).scalars().all()
    user_msg = None
    if assistant_msg.parent_message_id:
        user_msg = (
            await db.execute(
                select(Message).where(
                    Message.id == assistant_msg.parent_message_id,
                    Message.conversation_id == conv_id,
                    *alive_filters,
                )
            )
        ).scalar_one_or_none()
    if user_msg is None:
        user_msg = (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conv_id,
                    Message.role == Role.USER.value,
                    *alive_filters,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if user_msg is None:
        return None
    return PostMessageOut(
        user_message=MessageOut.model_validate(user_msg),
        assistant_message=MessageOut.model_validate(assistant_msg),
        completion_id=comp_hit.id if comp_hit else None,
        generation_ids=[g.id for g in gen_hits],
    )


@router.post(
    "/conversations/{conv_id}/messages",
    response_model=PostMessageOut,
    dependencies=[Depends(verify_csrf)],
)
async def post_message(
    conv_id: str,
    body: PostMessageIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostMessageOut:
    return await submit_user_message(conv_id, body, user, db)


async def submit_user_message(
    conv_id: str,
    body: PostMessageIn,
    user: User,
    db: AsyncSession,
) -> PostMessageOut:
    """Post a user message + spawn assistant task. Used by the public
    `/conversations/{cid}/messages` route AND by the Telegram bot route
    (which authenticates via X-Bot-Token instead of session cookie). The
    function body is the original `post_message` logic verbatim — only
    the entry signature changed so callers can supply `user` directly.
    """
    redis = get_redis()
    await MESSAGES_LIMITER.check(redis, f"rl:msg:{user.id}")

    # ---- ownership check ----
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise _http("not_found", "conversation not found", 404)

    # ---- idempotency short-circuit (best-effort: skips an INSERT round trip) ---
    prior = await _lookup_idempotent_post(
        db, user.id, conv_id, body.idempotency_key
    )
    if prior is not None:
        return prior

    # Why: pre-acquire row-level locks on any prior Completion/Generation
    # rows tied to (user_id, idempotency_key). Combined with the unique
    # constraint + IntegrityError fallback below, this serialises concurrent
    # requests with the same idempotency_key so a second caller blocks until
    # the first commits, then returns the prior result via the IntegrityError
    # branch (instead of double-creating partial state).
    await db.execute(
        select(Completion.id)
        .where(
            Completion.user_id == user.id,
            Completion.idempotency_key == body.idempotency_key,
        )
        .with_for_update()
    )
    await db.execute(
        select(Generation.id)
        .where(
            Generation.user_id == user.id,
            Generation.idempotency_key == body.idempotency_key,
        )
        .with_for_update()
    )

    # ---- validate attachments belong to user (and are alive) ----
    attachment_ids = list(body.attachment_image_ids or [])
    if attachment_ids:
        rows = (
            await db.execute(
                select(Image.id).where(
                    Image.id.in_(attachment_ids),
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if len(rows) != len(attachment_ids):
            raise _http("invalid_attachment", "one or more attachment images are not owned or were deleted", 400)

    # ---- intent routing ----
    intent = resolve_intent(
        explicit=body.intent,
        text=body.text or "",
        has_attachment=bool(attachment_ids),
    )
    if intent == Intent.IMAGE_TO_IMAGE and not attachment_ids:
        raise _http(
            "missing_reference_image",
            "image_to_image requires at least one reference image",
            400,
        )

    # ---- single transaction ----
    now = datetime.now(timezone.utc)

    user_content: dict[str, Any] = {
        "text": body.text or "",
        "attachments": [{"image_id": i} for i in attachment_ids],
    }
    # 推理强度仅对文本/视觉问答有意义；非空才写入，保持 content 干净。
    if intent in (Intent.CHAT, Intent.VISION_QA) and body.chat_params.reasoning_effort:
        if body.chat_params.reasoning_effort not in ALLOWED_REASONING_EFFORTS:
            raise _http("invalid_reasoning_effort", "invalid reasoning_effort", 422)
        user_content["reasoning_effort"] = body.chat_params.reasoning_effort
    # Fast 模式：chat 侧写进 user content；image 侧写进 Generation.upstream_request。
    # worker 读这些字段选择 priority / smaller rendering profiles。
    if intent in (Intent.CHAT, Intent.VISION_QA) and body.chat_params.fast:
        user_content["fast"] = True
    if intent in (Intent.CHAT, Intent.VISION_QA) and body.chat_params.web_search:
        user_content["web_search"] = True
    if intent in (Intent.CHAT, Intent.VISION_QA) and body.chat_params.file_search:
        user_content["file_search"] = True
        if body.chat_params.vector_store_ids:
            user_content["vector_store_ids"] = [
                v.strip()
                for v in body.chat_params.vector_store_ids
                if isinstance(v, str) and v.strip()
            ]
    if intent in (Intent.CHAT, Intent.VISION_QA) and body.chat_params.code_interpreter:
        user_content["code_interpreter"] = True
    if intent in (Intent.CHAT, Intent.VISION_QA) and body.chat_params.image_generation:
        user_content["image_generation"] = True

    system_prompt = None
    if intent in (Intent.CHAT, Intent.VISION_QA):
        system_prompt = await resolve_system_prompt_for_message(
            db,
            user_id=user.id,
            default_system_prompt_id=user.default_system_prompt_id,
            conv=conv,
            explicit_prompt=body.chat_params.system_prompt,
        )
    default_image_output_format = _DEFAULT_IMAGE_OUTPUT_FORMAT
    if intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE):
        spec = get_spec("image.output_format")
        if spec is not None:
            raw_default_format = await get_setting(db, spec)
            if raw_default_format in _IMAGE_OUTPUT_FORMAT_VALUES:
                default_image_output_format = raw_default_format

    user_msg = Message(
        conversation_id=conv_id,
        role=Role.USER.value,
        content=user_content,
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()  # need user_msg.id for parent_message_id

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        conv=conv,
        user_msg=user_msg,
        intent=intent,
        idempotency_key=body.idempotency_key,
        image_params=body.image_params,
        chat_params=body.chat_params,
        system_prompt=system_prompt,
        attachment_ids=attachment_ids,
        text=body.text or "",
        default_image_output_format=default_image_output_format,
    )

    # bump conversation last_activity_at
    conv.last_activity_at = now

    try:
        await db.commit()
    except IntegrityError:
        # Why: concurrent request with the same idempotency_key won the race;
        # rely on the (user_id, idempotency_key) unique constraint and return
        # the prior result instead of raising 500.
        await db.rollback()
        prior = await _lookup_idempotent_post(
            db, user.id, conv_id, body.idempotency_key
        )
        if prior is not None:
            return prior
        raise _http(
            "idempotency_conflict",
            "idempotency_key conflict",
            409,
        )
    await db.refresh(user_msg)
    await db.refresh(result.assistant_msg)

    # ---- best-effort publish ----
    await _await_post_commit_publish(
        "message_appended",
        _publish_message_appended(
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            message_ids=[user_msg.id, result.assistant_msg.id],
        ),
        user_id=user.id,
        conv_id=conv_id,
    )
    await _await_post_commit_publish(
        "assistant_task",
        _publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            assistant_msg_id=result.assistant_msg.id,
            outbox_payloads=result.outbox_payloads,
            outbox_rows=result.outbox_rows,
        ),
        user_id=user.id,
        conv_id=conv_id,
        assistant_msg_id=result.assistant_msg.id,
    )

    return PostMessageOut(
        user_message=MessageOut.model_validate(user_msg),
        assistant_message=MessageOut.model_validate(result.assistant_msg),
        completion_id=result.completion_id,
        generation_ids=result.generation_ids,
    )


# ---------------------------------------------------------------------------
# Silent generation: 仅创建 assistant + generation，不创建用户消息。
# 用于重画（reroll）和放大（upscale）场景。
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field as PydanticField


class SilentGenerationIn(BaseModel):
    idempotency_key: str = PydanticField(min_length=1, max_length=64)
    parent_message_id: str
    intent: Literal["text_to_image", "image_to_image"] = "text_to_image"
    image_params: ImageParamsIn = PydanticField(default_factory=ImageParamsIn)
    prompt: str = PydanticField(default="", max_length=MAX_PROMPT_CHARS)
    attachment_image_ids: list[str] = PydanticField(default_factory=list)


class SilentGenerationOut(BaseModel):
    assistant_message: MessageOut
    generation_ids: list[str] = PydanticField(default_factory=list)


@router.post(
    "/conversations/{conv_id}/generations",
    response_model=SilentGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_silent_generation(
    conv_id: str,
    body: SilentGenerationIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SilentGenerationOut:
    """Create a generation without a user message (for reroll / upscale)."""
    redis = get_redis()

    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise _http("not_found", "conversation not found", 404)

    parent_msg = (
        await db.execute(
            select(Message).where(
                Message.id == body.parent_message_id,
                Message.conversation_id == conv_id,
                *_message_alive_filters(),
            )
        )
    ).scalar_one_or_none()
    if not parent_msg:
        raise _http("not_found", "parent message not found", 404)

    attachment_ids = list(body.attachment_image_ids or [])
    if attachment_ids:
        rows = (
            await db.execute(
                select(Image.id).where(
                    Image.id.in_(attachment_ids),
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if len(rows) != len(attachment_ids):
            raise _http("invalid_attachment", "attachment not owned or deleted", 400)

    intent = Intent(body.intent)
    text = body.prompt
    default_image_output_format = _DEFAULT_IMAGE_OUTPUT_FORMAT
    spec = get_spec("image.output_format")
    if spec is not None:
        raw_default_format = await get_setting(db, spec)
        if raw_default_format in _IMAGE_OUTPUT_FORMAT_VALUES:
            default_image_output_format = raw_default_format

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        conv=conv,
        user_msg=parent_msg,
        intent=intent,
        idempotency_key=body.idempotency_key,
        image_params=body.image_params,
        chat_params=ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=text,
        default_image_output_format=default_image_output_format,
    )

    now = datetime.now(timezone.utc)
    conv.last_activity_at = now

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _http("idempotency_conflict", "idempotency_key conflict", 409)

    await db.refresh(result.assistant_msg)

    await _await_post_commit_publish(
        "message_appended",
        _publish_message_appended(
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            message_ids=[result.assistant_msg.id],
        ),
        user_id=user.id,
        conv_id=conv_id,
    )
    await _await_post_commit_publish(
        "assistant_task",
        _publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            assistant_msg_id=result.assistant_msg.id,
            outbox_payloads=result.outbox_payloads,
            outbox_rows=result.outbox_rows,
        ),
        user_id=user.id,
        conv_id=conv_id,
        assistant_msg_id=result.assistant_msg.id,
    )

    return SilentGenerationOut(
        assistant_message=MessageOut.model_validate(result.assistant_msg),
        generation_ids=result.generation_ids,
    )


@router.get(
    "/conversations/{conv_id}/messages/{message_id}",
    response_model=MessageOut,
)
async def get_message(
    conv_id: str,
    message_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageOut:
    msg = (
        await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.conversation_id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
                *_message_alive_filters(),
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        raise _http("not_found", "message not found", 404)
    return MessageOut.model_validate(msg)
