"""Telegram bot 集成路由。

两类调用方：
1. **Web 用户**（session + CSRF）：在设置页生成绑定码 → POST /me/telegram/link-code
2. **Bot 服务**（X-Bot-Token + X-Telegram-Chat-Id）：所有 /telegram/* 端点

Bot-token 是 service-to-service 共享密钥，不替代用户身份；身份由 chat_id ↔
telegram_bindings 表查出。Bot 拿到的 user 上下文与该用户登录 web 时完全等价，
但 surface 限制在本文件定义的少数路由内，不能访问 /admin/*。

绑定流程（user-initiated）：
  - web 端：POST /me/telegram/link-code → {code, deep_link}
  - 用户复制 code 或点 deep_link，bot 收 /start <code>
  - bot 调 POST /telegram/bind，consume code，写 binding
  - 后续 bot 调用都带 X-Telegram-Chat-Id

事件推送：bot 自己订阅 Redis PubSub `task:{generation_id}`（参 worker/sse_publish.py），
本路由只负责创建任务 + 提供二进制下载。
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    MAX_EXPLICIT_SIDE,
)
from lumen_core.models import (
    Generation,
    Image,
    Message,
    TelegramBinding,
)
from lumen_core.schemas import ImageParamsIn, PostMessageIn

from ..db import get_db
from ..deps import (
    BOT_CHAT_ID_HEADER,
    BOT_TG_USER_ID_HEADER,
    BotUser,
    CurrentUser,
    require_bot_token,
    verify_csrf,
)
from ..public_urls import resolve_public_base_url
from ..redis_client import get_redis
from ..services.provider_config import (
    parse_provider_config as _parse_config,
    read_providers as _read_providers,
)
from .media_delivery import image_storage_path, image_storage_streaming_response
from .messages import submit_user_message
from .prompts import (
    PromptRuntime,
    get_prompt_runtime,
)
from lumen_core.providers import parse_proxy_item
from ..proxy_pool import (
    DEFAULT_STRATEGY,
    pick_proxy,
    report_failure as pool_report_failure,
    report_success as pool_report_success,
    resolve_provider_proxy_url,
)
from ..ratelimit import RateLimiter, require_client_ip
from . import telegram_image_options as _telegram_image_options
from . import telegram_runtime_values as _telegram_runtime_values
from .telegram_generation import lock_telegram_generation_context
from .telegram_prompt_enhance import enhance_telegram_prompt
from .telegram_schemas import (
    BindIn,
    BindOut,
    EnhancePromptIn,
    EnhancePromptOut,
    GenerateIn,
    GenerateOut,
    GenerationStatusOut,
    LinkCodeOut,
    ProxyReportIn,
    RuntimeAccessOut,
    RuntimeConfigOut,
    RuntimeProxyOut,
    TaskListItem,
    TaskListOut,
)

logger = logging.getLogger(__name__)

# /me/telegram/* 走 session 鉴权；/telegram/* 走 bot-token。
router_me = APIRouter()
router_bot = APIRouter()
_aspect_ratio_to_size = _telegram_image_options.aspect_ratio_to_size
_align_pair = _telegram_image_options.align_pair
_bool_option = _telegram_runtime_values.bool_option
_get_setting_str = _telegram_runtime_values.get_setting_str
_get_setting_int = _telegram_runtime_values.get_setting_int


# ---------- helpers ----------


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


_LINK_CODE_TTL_SECONDS = 600  # 10 min
_LINK_CODE_REDIS_PREFIX = "tg:link:"
_LINK_CODE_CLAIM_SUFFIX = ":claim"
_DEFAULT_TELEGRAM_SSH_PROXY_HOST = "127.0.0.1"
_BOT_BIND_CODE_LIMITER = RateLimiter(
    capacity=30,
    refill_per_sec=30 / 60,
    always_on=True,
)
_CLAIM_LINK_CODE_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
  return {0, ''}
end
if redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', tonumber(ARGV[2])) then
  return {1, raw}
end
return {2, ''}
"""
_RELEASE_LINK_CODE_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _link_code_key(code: str) -> str:
    return f"{_LINK_CODE_REDIS_PREFIX}{code}"


def _link_code_claim_key(code: str) -> str:
    return f"{_link_code_key(code)}{_LINK_CODE_CLAIM_SUFFIX}"


def _decode_redis_text(value: object) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


async def _claim_link_code(redis: object, code: str, *, owner: str) -> str | None:
    key = _link_code_key(code)
    claim_key = _link_code_claim_key(code)
    eval_fn = getattr(redis, "eval", None)
    if callable(eval_fn):
        result = await eval_fn(
            _CLAIM_LINK_CODE_LUA,
            2,
            key,
            claim_key,
            owner,
            str(_LINK_CODE_TTL_SECONDS),
        )
        if isinstance(result, (list, tuple)) and result:
            status = int(result[0])
            if status == 0:
                return None
            if status == 2:
                raise _http(
                    "code_in_use", "binding code is already being consumed", 409
                )
            return _decode_redis_text(result[1])

    raw = await redis.get(key)  # type: ignore[attr-defined]
    if raw is None:
        return None
    set_fn = getattr(redis, "set", None)
    if callable(set_fn):
        claimed = await set_fn(
            claim_key,
            owner,
            ex=_LINK_CODE_TTL_SECONDS,
            nx=True,
        )
        if claimed is False or claimed == 0:
            raise _http("code_in_use", "binding code is already being consumed", 409)
    return _decode_redis_text(raw)


async def _release_link_code_claim(redis: object, code: str, *, owner: str) -> None:
    claim_key = _link_code_claim_key(code)
    try:
        eval_fn = getattr(redis, "eval", None)
        if callable(eval_fn):
            await eval_fn(_RELEASE_LINK_CODE_CLAIM_LUA, 1, claim_key, owner)
            return
        raw = await redis.get(claim_key)  # type: ignore[attr-defined]
        if raw is not None and _decode_redis_text(raw) == owner:
            await redis.delete(claim_key)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "telegram link code claim release failed code=%s err=%s", code, exc
        )


async def _consume_link_code(redis: object, code: str) -> None:
    try:
        await redis.delete(_link_code_key(code), _link_code_claim_key(code))  # type: ignore[attr-defined]
    except TypeError:
        await redis.delete(_link_code_key(code))  # type: ignore[attr-defined]
        await redis.delete(_link_code_claim_key(code))  # type: ignore[attr-defined]


def _gen_link_code() -> str:
    # 16 random bytes -> ~22 URL-safe chars. Keep the original alphabet and
    # casing so no entropy is collapsed before the bot consumes the code.
    return secrets.token_urlsafe(16).rstrip("=")


# ---------- /me/telegram/link-code ----------


@router_me.post(
    "/me/telegram/link-code",
    response_model=LinkCodeOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_link_code(user: CurrentUser) -> LinkCodeOut:
    """Web 用户生成一次性 TG 绑定码，10 分钟有效。

    code 写到 Redis（key=tg:link:{code}, val=user_id, TTL=10min），bot 收到
    /start <code> 后调 POST /telegram/bind 消费。

    返回 deep_link：直接拼好 https://t.me/<bot>?start=<code>，前端不用再拼。
    bot username 走 env TELEGRAM_BOT_USERNAME；没配则 deep_link=None，前端自己处理。
    """
    from ..config import settings

    if not settings.telegram_bot_shared_secret.strip():
        raise _http(
            "telegram_disabled", "telegram bot integration is not configured", 503
        )
    code = _gen_link_code()
    redis = get_redis()
    await redis.set(_link_code_key(code), user.id, ex=_LINK_CODE_TTL_SECONDS)
    bot_username = (settings.telegram_bot_username or "").strip().lstrip("@")
    deep_link = f"https://t.me/{bot_username}?start={code}" if bot_username else None
    return LinkCodeOut(
        code=code,
        expires_in=_LINK_CODE_TTL_SECONDS,
        deep_link=deep_link,
    )


# ---------- /telegram/bind ----------


@router_bot.post(
    "/telegram/bind", response_model=BindOut, dependencies=[Depends(require_bot_token)]
)
async def bind_telegram(
    request: Request,
    body: BindIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BindOut:
    header_chat_id = (request.headers.get(BOT_CHAT_ID_HEADER) or "").strip()
    if header_chat_id and header_chat_id != body.chat_id:
        raise _http("chat_id_mismatch", "telegram chat header does not match body", 400)
    header_tg_user_id = (request.headers.get(BOT_TG_USER_ID_HEADER) or "").strip()
    body_tg_user_id = (body.tg_user_id or "").strip()
    if header_tg_user_id and body_tg_user_id and header_tg_user_id != body_tg_user_id:
        raise _http(
            "telegram_user_mismatch",
            "telegram user header does not match body",
            400,
        )
    tg_user_id = body_tg_user_id or header_tg_user_id or None
    if not tg_user_id:
        raise _http(
            "missing_telegram_user_id",
            "telegram user id is required for binding",
            400,
        )
    redis = get_redis()
    claim_owner = f"chat:{body.chat_id}"
    user_id = await _claim_link_code(
        redis,
        body.code,
        owner=claim_owner,
    )
    if user_id is None:
        # Why: key is IP-only. Including `len:{len(body.code)}` partitions
        # the bucket per code length, letting an attacker brute-force in
        # parallel across lengths and bypass the rate limit.
        await _BOT_BIND_CODE_LIMITER.check(
            redis,
            f"rl:telegram:bind:{require_client_ip(request)}",
        )
        raise _http("invalid_code", "binding code is invalid or expired", 400)

    from lumen_core.models import User

    user = (
        await db.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if user is None:
        await _release_link_code_claim(redis, body.code, owner=claim_owner)
        raise _http("user_not_found", "user no longer exists", 404)

    # upsert by chat_id：同一 chat 重新绑可换 user
    existing_chat = (
        await db.execute(
            select(TelegramBinding).where(TelegramBinding.chat_id == body.chat_id)
        )
    ).scalar_one_or_none()
    if existing_chat is not None:
        existing_chat.user_id = user_id
        existing_chat.tg_user_id = tg_user_id
        existing_chat.tg_username = body.tg_username
    else:
        # 同一 user 唯一绑定：先删旧，再插新
        existing_user = (
            await db.execute(
                select(TelegramBinding).where(TelegramBinding.user_id == user_id)
            )
        ).scalar_one_or_none()
        if existing_user is not None:
            await db.delete(existing_user)
            await db.flush()
        db.add(
            TelegramBinding(
                chat_id=body.chat_id,
                user_id=user_id,
                tg_user_id=tg_user_id,
                tg_username=body.tg_username,
            )
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        await _release_link_code_claim(redis, body.code, owner=claim_owner)
        raise _http(
            "bind_conflict", "binding conflict, retry the link flow", 409
        ) from exc
    except Exception:
        await db.rollback()
        await _release_link_code_claim(redis, body.code, owner=claim_owner)
        raise
    try:
        await _consume_link_code(redis, body.code)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "telegram link code cleanup failed code=%s err=%s", body.code, exc
        )

    logger.info("telegram bind: user=%s chat=%s", user_id, body.chat_id)
    return BindOut(user_id=user.id, email=user.email, display_name=user.display_name)


# ---------- /telegram/me ----------


@router_bot.get("/telegram/me", response_model=BindOut)
async def telegram_me(user: BotUser) -> BindOut:
    return BindOut(user_id=user.id, email=user.email, display_name=user.display_name)


# ---------- /telegram/unbind ----------


@router_bot.post("/telegram/unbind")
async def unbind_telegram(
    request: Request,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    chat_id = (request.headers.get("X-Telegram-Chat-Id") or "").strip()
    binding = (
        await db.execute(
            select(TelegramBinding).where(
                TelegramBinding.chat_id == chat_id,
                TelegramBinding.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        return {"ok": True}
    await db.delete(binding)
    await db.commit()
    logger.info("telegram unbind: user=%s chat=%s", user.id, chat_id)
    return {"ok": True}


# ---------- /telegram/generations ----------


# ---------- runtime-config / proxy 池接口（bot bootstrap + failover） ----------


@router_bot.get(
    "/telegram/runtime-config",
    response_model=RuntimeConfigOut,
    dependencies=[Depends(require_bot_token)],
)
async def runtime_config(
    db: Annotated[AsyncSession, Depends(get_db)],
    avoid: str = "",
) -> RuntimeConfigOut:
    """bot 启动 / failover 时调。

    avoid 是逗号分隔的 proxy name 列表（最近失败的），用来在 pool 里跳过它们。
    """
    redis = get_redis()
    enabled_raw = (
        (await _get_setting_str(db, "telegram.bot_enabled", "1")).strip().lower()
    )
    bot_enabled = enabled_raw not in {"0", "false", "no", ""}
    bot_token = await _get_setting_str(db, "telegram.bot_token")
    bot_username = await _get_setting_str(db, "telegram.bot_username")
    allowed_user_ids = await _get_setting_str(db, "telegram.allowed_user_ids")
    proxy_names_raw = await _get_setting_str(db, "telegram.proxy_names")
    strategy = (
        await _get_setting_str(db, "telegram.proxy_strategy")
    ) or DEFAULT_STRATEGY
    failure_threshold = await _get_setting_int(db, "proxies.failure_threshold", 3)
    cooldown_seconds = await _get_setting_int(db, "proxies.cooldown_seconds", 60)

    # 加载 proxy 池
    raw, _src = await _read_providers(db)
    pool: list = []
    if raw:
        _items, proxy_raw = _parse_config(raw)
        for i, p in enumerate(proxy_raw):
            try:
                pool.append(parse_proxy_item(p, index=i))
            except Exception as exc:  # noqa: BLE001
                logger.warning("runtime-config: skip bad proxy idx=%d err=%s", i, exc)

    # 按 telegram.proxy_names 过滤；空 = 用全部 enabled
    name_filter = {n.strip() for n in proxy_names_raw.split(",") if n.strip()}
    if name_filter:
        candidates = [p for p in pool if p.name in name_filter]
    else:
        candidates = list(pool)

    avoid_set = {a.strip() for a in (avoid or "").split(",") if a.strip()}
    picked = await pick_proxy(redis, candidates, strategy=strategy, avoid=avoid_set)
    proxy_out: RuntimeProxyOut | None = None
    if picked is not None:
        if picked.protocol == "ssh":
            bind_host = await _get_setting_str(
                db,
                "telegram.proxy_bind_host",
                _DEFAULT_TELEGRAM_SSH_PROXY_HOST,
            )
            advertise_host = await _get_setting_str(
                db,
                "telegram.proxy_advertise_host",
                bind_host,
            )
            url = await resolve_provider_proxy_url(
                picked,
                bind_host=bind_host,
                advertise_host=advertise_host,
            )
        else:
            # Existing SOCKS5 proxies already advertise their own reachable
            # endpoint. The SSH-only listener settings must not rewrite them.
            url = await resolve_provider_proxy_url(picked)
        if url:
            proxy_out = RuntimeProxyOut(name=picked.name, url=url)

    return RuntimeConfigOut(
        bot_enabled=bot_enabled,
        bot_token=bot_token,
        bot_username=bot_username,
        allowed_user_ids=allowed_user_ids,
        proxy=proxy_out,
        proxy_strategy=strategy,
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
    )


@router_bot.get(
    "/telegram/access-config",
    response_model=RuntimeAccessOut,
    dependencies=[Depends(require_bot_token)],
)
async def access_config(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RuntimeAccessOut:
    enabled_raw = (
        (await _get_setting_str(db, "telegram.bot_enabled", "1")).strip().lower()
    )
    bot_enabled = enabled_raw not in {"0", "false", "no", ""}
    allowed_user_ids = await _get_setting_str(db, "telegram.allowed_user_ids")
    return RuntimeAccessOut(
        bot_enabled=bot_enabled,
        allowed_user_ids=allowed_user_ids,
    )


@router_bot.post(
    "/telegram/proxy/report",
    dependencies=[Depends(require_bot_token)],
)
async def report_proxy(
    body: ProxyReportIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    redis = get_redis()
    if body.success:
        await pool_report_success(redis, body.name)
        return {"ok": True, "cooldown": False}
    failure_threshold = await _get_setting_int(db, "proxies.failure_threshold", 3)
    cooldown_seconds = await _get_setting_int(db, "proxies.cooldown_seconds", 60)
    triggered = await pool_report_failure(
        redis,
        body.name,
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
    )
    return {"ok": True, "cooldown": triggered}


@router_bot.post("/telegram/prompts/enhance", response_model=EnhancePromptOut)
async def enhance_prompt(
    request: Request,
    body: EnhancePromptIn,
    user: BotUser,  # 仅作鉴权，enhance 自身不带 user 上下文
    db: Annotated[AsyncSession, Depends(get_db)],
    runtime: Annotated[PromptRuntime, Depends(get_prompt_runtime)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> EnhancePromptOut:
    enhanced = await enhance_telegram_prompt(
        text=body.text,
        user=user,
        chat_id=(request.headers.get(BOT_CHAT_ID_HEADER) or "").strip(),
        tg_user_id=(request.headers.get(BOT_TG_USER_ID_HEADER) or "").strip(),
        idempotency_key=idempotency_key,
        db=db,
        runtime=runtime,
    )
    logger.info(
        "telegram enhance: user=%s in_len=%d out_len=%d",
        user.id,
        len(body.text),
        len(enhanced),
    )
    return EnhancePromptOut(enhanced=enhanced)


@router_bot.post("/telegram/generations", response_model=GenerateOut)
async def create_generation(
    request: Request,
    body: GenerateIn,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerateOut:
    idempotency_key = (body.idempotency_key or "").strip()
    if not idempotency_key:
        raise _http(
            "missing_idempotency_key",
            "idempotency_key is required for telegram generation",
            422,
        )
    context = await lock_telegram_generation_context(
        db,
        authenticated_user_id=user.id,
        chat_id=(request.headers.get(BOT_CHAT_ID_HEADER) or "").strip(),
        tg_user_id=(request.headers.get(BOT_TG_USER_ID_HEADER) or "").strip(),
        client_key=idempotency_key,
        request_payload=body.model_dump(mode="json", exclude={"idempotency_key"}),
    )
    locked_user = context.user
    conv = context.conversation

    side_by_resolution = {"1k": 1024, "2k": 2048, "4k": MAX_EXPLICIT_SIDE}
    fixed_size = _aspect_ratio_to_size(
        body.aspect_ratio, side_by_resolution[body.resolution]
    )

    image_params = ImageParamsIn(
        aspect_ratio=body.aspect_ratio,
        size_mode="fixed",
        fixed_size=fixed_size,
        count=body.count,
        fast=body.fast,
        render_quality=body.render_quality,
        output_format=body.output_format,
    )
    intent: Literal["image_to_image", "text_to_image"] = (
        "image_to_image" if body.attachment_image_ids else "text_to_image"
    )
    msg_in = PostMessageIn(
        idempotency_key=context.message_idempotency_key or idempotency_key,
        text=body.prompt,
        intent=intent,
        image_params=image_params,
        attachment_image_ids=list(body.attachment_image_ids),
        source="telegram",
        action_source="telegram.generation",
        trace_id=context.operation_id,
    )
    result = await submit_user_message(conv.id, msg_in, locked_user, db)
    return GenerateOut(
        user_id=locked_user.id,
        conversation_id=conv.id,
        message_id=result.assistant_message.id,
        generation_ids=result.generation_ids,
    )


# ---------- /telegram/generations/{id} ----------


@router_bot.get("/telegram/generations/{gen_id}", response_model=GenerationStatusOut)
async def get_generation(
    gen_id: str,
    request: Request,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationStatusOut:
    row = (
        await db.execute(
            select(Generation, Message.conversation_id)
            .join(Message, Message.id == Generation.message_id)
            .where(
                Generation.id == gen_id,
                Generation.user_id == user.id,
            )
        )
    ).one_or_none()
    if row is None:
        raise _http("not_found", "generation not found", 404)
    gen, conversation_id = row
    image_ids = (
        (
            await db.execute(
                select(Image.id)
                .where(
                    Image.owner_generation_id == gen_id,
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    upstream = gen.upstream_request if isinstance(gen.upstream_request, dict) else {}
    web_url: str | None = None
    edit_url: str | None = None
    project_url: str | None = None
    try:
        public_base = (await resolve_public_base_url(request, db)).rstrip("/")
        web_url = f"{public_base}/?{urlencode({'conversationId': conversation_id})}"
        edit_url = f"{public_base}/?{urlencode({'conversationId': conversation_id, 'generationId': gen.id, 'source': 'telegram'})}"
        if image_ids:
            project_url = f"{public_base}/projects?{urlencode({'source': 'telegram', 'imageId': str(image_ids[0]), 'generationId': gen.id})}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram web link resolve failed gen=%s err=%s", gen.id, exc)
    return GenerationStatusOut(
        id=gen.id,
        conversation_id=conversation_id,
        status=gen.status,
        progress_stage=gen.progress_stage,
        error_code=gen.error_code,
        error_message=gen.error_message,
        image_ids=list(image_ids),
        input_image_ids=list(gen.input_image_ids or []),
        prompt=gen.prompt,
        created_at=gen.created_at,
        aspect_ratio=gen.aspect_ratio,
        size_requested=gen.size_requested,
        render_quality=str(upstream.get("render_quality") or "medium"),
        output_format=str(upstream.get("output_format") or "jpeg"),
        fast=_bool_option(upstream.get("fast"), False),
        web_url=web_url,
        edit_url=edit_url,
        project_url=project_url,
    )


# ---------- /telegram/tasks ----------


@router_bot.get("/telegram/tasks", response_model=TaskListOut)
async def list_tasks(
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 10,
) -> TaskListOut:
    limit = max(1, min(50, limit))
    rows = (
        (
            await db.execute(
                select(Generation)
                .where(Generation.user_id == user.id)
                .order_by(desc(Generation.created_at))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return TaskListOut(items=[])
    gen_ids = [g.id for g in rows]
    image_rows = (
        await db.execute(
            select(Image.id, Image.owner_generation_id)
            .where(
                Image.owner_generation_id.in_(gen_ids),
                Image.deleted_at.is_(None),
            )
            .order_by(Image.created_at.asc())
        )
    ).all()
    images_by_gen: dict[str, list[str]] = {}
    for img_id, owner in image_rows:
        if owner is None:
            continue
        images_by_gen.setdefault(owner, []).append(img_id)
    items: list[TaskListItem] = []
    for g in rows:
        prompt = g.prompt or ""
        excerpt = prompt if len(prompt) <= 80 else prompt[:77] + "..."
        items.append(
            TaskListItem(
                id=g.id,
                status=g.status,
                prompt_excerpt=excerpt,
                aspect_ratio=g.aspect_ratio,
                size_requested=g.size_requested,
                image_ids=images_by_gen.get(g.id, []),
                error_message=g.error_message,
                created_at=g.created_at,
            )
        )
    return TaskListOut(items=items)


# ---------- /telegram/images/{id}/binary ----------


@router_bot.get("/telegram/images/{image_id}/binary")
async def get_image_binary(
    image_id: str,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Bot 流式取图。复用 images 路由的 storage 工具，但鉴权用 BotUser。"""
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not img:
        raise _http("not_found", "image not found", 404)
    path = image_storage_path(img.storage_key)
    return image_storage_streaming_response(
        path,
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
    )
