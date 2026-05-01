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
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    EXPLICIT_ALIGN,
    MAX_EXPLICIT_PIXELS,
    MAX_EXPLICIT_SIDE,
    MIN_EXPLICIT_PIXELS,
)
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    TelegramBinding,
)
from lumen_core.schemas import ImageParamsIn, PostMessageIn

from ..db import get_db
from ..deps import BotUser, CurrentUser, require_bot_token, verify_csrf
from ..redis_client import get_redis
from .messages import submit_user_message
from .prompts import _resolve_provider_order, _stream_enhance
from .providers import _parse_config, _read_providers
from lumen_core.providers import parse_proxy_item, resolve_provider_proxy_url
from lumen_core.runtime_settings import get_spec
from ..proxy_pool import (
    DEFAULT_STRATEGY,
    DEFAULT_TEST_TARGET,
    pick_proxy,
    report_failure as pool_report_failure,
    report_success as pool_report_success,
)
from ..runtime_settings import get_setting

logger = logging.getLogger(__name__)

# /me/telegram/* 走 session 鉴权；/telegram/* 走 bot-token。
router_me = APIRouter()
router_bot = APIRouter()


# ---------- helpers ----------


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(status_code=http, detail={"error": {"code": code, "message": msg}})


_LINK_CODE_TTL_SECONDS = 600  # 10 min
_LINK_CODE_REDIS_PREFIX = "tg:link:"
_TG_CONV_TITLE = "Telegram Bot"
_TG_CONV_MARKER = {"telegram": True}


def _link_code_key(code: str) -> str:
    return f"{_LINK_CODE_REDIS_PREFIX}{code}"


def _gen_link_code() -> str:
    # 6 字节 → 8 字符 url-safe；够防爆破，TG /start 又不会太长。
    return secrets.token_urlsafe(6).replace("-", "A").replace("_", "B").upper()[:10]


def _aspect_ratio_to_size(ratio: str, max_long_side: int) -> str:
    """按 ratio 和用户期望长边，算出符合 upstream 显式 size 约束的 fixed_size。

    约束（lumen_core.constants）：
      - 长宽对齐 EXPLICIT_ALIGN（16）
      - 长边 ≤ MAX_EXPLICIT_SIDE
      - 像素总数 ∈ [MIN_EXPLICIT_PIXELS, MAX_EXPLICIT_PIXELS]
    所以 1:1+4K 不能直接给 3840×3840（14.7M 像素超上限）；先按比例对像素预算开方。
    """
    a, _, b = ratio.partition(":")
    try:
        ra, rb = float(a), float(b)
    except ValueError:
        return _align_pair(max_long_side, max_long_side)
    if ra <= 0 or rb <= 0:
        return _align_pair(max_long_side, max_long_side)

    long_r = max(ra, rb)
    short_r = min(ra, rb)
    # 像素预算下允许的最长边：long * (long * short_r/long_r) ≤ MAX_EXPLICIT_PIXELS
    pixel_cap_long = int((MAX_EXPLICIT_PIXELS * long_r / short_r) ** 0.5)
    long_side = min(max_long_side, MAX_EXPLICIT_SIDE, pixel_cap_long)
    short_side = int(round(long_side * short_r / long_r))

    long_side = max(EXPLICIT_ALIGN, (long_side // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)
    short_side = max(EXPLICIT_ALIGN, (short_side // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)

    # 像素下限保护：极端窄比例 + 1K 可能跌到 MIN 之下，按比例放大短边对齐
    while long_side * short_side < MIN_EXPLICIT_PIXELS and long_side < MAX_EXPLICIT_SIDE:
        long_side += EXPLICIT_ALIGN
        short_side = int(round(long_side * short_r / long_r))
        short_side = max(EXPLICIT_ALIGN, (short_side // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)

    if ra >= rb:
        return f"{long_side}x{short_side}"
    return f"{short_side}x{long_side}"


def _align_pair(a: int, b: int) -> str:
    a = max(EXPLICIT_ALIGN, (a // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)
    b = max(EXPLICIT_ALIGN, (b // EXPLICIT_ALIGN) * EXPLICIT_ALIGN)
    return f"{a}x{b}"


async def _get_or_create_tg_conversation(db: AsyncSession, user_id: str) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                Conversation.default_params.contains({"telegram": True}),
            )
            .order_by(desc(Conversation.last_activity_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if conv is not None:
        return conv
    # TG 会话默认归档：bot 用户的图本身在 bot 里看，web 端列表里默认折叠到归档区
    conv = Conversation(
        user_id=user_id,
        title=_TG_CONV_TITLE,
        default_params=dict(_TG_CONV_MARKER),
        archived=True,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


# ---------- schemas ----------


class LinkCodeOut(BaseModel):
    code: str
    expires_in: int
    deep_link: str | None = None


class BindIn(BaseModel):
    chat_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=4, max_length=32)
    tg_username: str | None = Field(default=None, max_length=64)


class BindOut(BaseModel):
    user_id: str
    email: str
    display_name: str


class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=10000)
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "9:21", "4:5"] = "1:1"
    render_quality: Literal["low", "medium", "high", "auto"] = "high"
    count: int = Field(default=1, ge=1, le=16)
    resolution: Literal["1k", "2k", "4k"] = "2k"
    output_format: Literal["png", "jpeg", "webp"] = "jpeg"
    fast: bool = False
    # 当带 attachment_image_ids 时切到 image_to_image 意图（迭代/编辑）
    attachment_image_ids: list[str] = Field(default_factory=list, max_length=4)


class GenerateOut(BaseModel):
    conversation_id: str
    message_id: str
    generation_ids: list[str]


class EnhancePromptIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class EnhancePromptOut(BaseModel):
    enhanced: str


class GenerationStatusOut(BaseModel):
    id: str
    status: str
    progress_stage: str
    error_code: str | None = None
    error_message: str | None = None
    image_ids: list[str] = Field(default_factory=list)
    prompt: str
    created_at: datetime
    # 完整的生成参数：retry 直接用这些回填，不必再扫 /telegram/tasks 推断
    aspect_ratio: str
    size_requested: str
    render_quality: str = "medium"
    output_format: str = "jpeg"
    fast: bool = False


class TaskListItem(BaseModel):
    id: str
    status: str
    prompt_excerpt: str
    aspect_ratio: str
    size_requested: str
    image_ids: list[str]
    error_message: str | None = None
    created_at: datetime


class TaskListOut(BaseModel):
    items: list[TaskListItem]


# ---------- /me/telegram/link-code ----------


@router_me.post("/me/telegram/link-code", response_model=LinkCodeOut, dependencies=[Depends(verify_csrf)])
async def create_link_code(user: CurrentUser) -> LinkCodeOut:
    """Web 用户生成一次性 TG 绑定码，10 分钟有效。

    code 写到 Redis（key=tg:link:{code}, val=user_id, TTL=10min），bot 收到
    /start <code> 后调 POST /telegram/bind 消费。

    返回 deep_link：直接拼好 https://t.me/<bot>?start=<code>，前端不用再拼。
    bot username 走 env TELEGRAM_BOT_USERNAME；没配则 deep_link=None，前端自己处理。
    """
    from ..config import settings

    if not settings.telegram_bot_shared_secret.strip():
        raise _http("telegram_disabled", "telegram bot integration is not configured", 503)
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


@router_bot.post("/telegram/bind", response_model=BindOut, dependencies=[Depends(require_bot_token)])
async def bind_telegram(
    body: BindIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BindOut:
    redis = get_redis()
    key = _link_code_key(body.code)
    raw = await redis.get(key)
    if raw is None:
        raise _http("invalid_code", "binding code is invalid or expired", 400)
    user_id = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    # 一次性消费：删 key 后再写表，失败回滚把 key 写回（best-effort）
    await redis.delete(key)

    from lumen_core.models import User

    user = (
        await db.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if user is None:
        raise _http("user_not_found", "user no longer exists", 404)

    # upsert by chat_id：同一 chat 重新绑可换 user
    existing_chat = (
        await db.execute(
            select(TelegramBinding).where(TelegramBinding.chat_id == body.chat_id)
        )
    ).scalar_one_or_none()
    if existing_chat is not None:
        existing_chat.user_id = user_id
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
                tg_username=body.tg_username,
            )
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _http("bind_conflict", "binding conflict, retry the link flow", 409) from exc

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


class RuntimeProxyOut(BaseModel):
    name: str
    url: str  # 已 resolve 后的 socks5://… 字符串


class RuntimeConfigOut(BaseModel):
    bot_enabled: bool
    bot_token: str  # 可能为空，bot 自己 fallback env
    bot_username: str
    allowed_user_ids: str
    proxy: RuntimeProxyOut | None  # None 表示池里没有可用 proxy
    proxy_strategy: str
    failure_threshold: int
    cooldown_seconds: int


async def _get_setting_str(db: AsyncSession, key: str, default: str = "") -> str:
    spec = get_spec(key)
    if spec is None:
        return default
    raw = await get_setting(db, spec)
    if raw is None:
        return default
    return str(raw).strip()


async def _get_setting_int(db: AsyncSession, key: str, default: int) -> int:
    spec = get_spec(key)
    if spec is None:
        return default
    raw = await get_setting(db, spec)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


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
    enabled_raw = (await _get_setting_str(db, "telegram.bot_enabled", "1")).strip()
    bot_enabled = enabled_raw not in {"0", "false", "no", ""}
    bot_token = await _get_setting_str(db, "telegram.bot_token")
    bot_username = await _get_setting_str(db, "telegram.bot_username")
    allowed_user_ids = await _get_setting_str(db, "telegram.allowed_user_ids")
    proxy_names_raw = await _get_setting_str(db, "telegram.proxy_names")
    strategy = (await _get_setting_str(db, "telegram.proxy_strategy")) or DEFAULT_STRATEGY
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


class ProxyReportIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    success: bool = False  # 默认是失败上报；显式设 true 可清失败计数


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
    body: EnhancePromptIn,
    user: BotUser,  # 仅作鉴权，enhance 自身不带 user 上下文
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EnhancePromptOut:
    """复用 /prompts/enhance 的内核，但聚合 SSE 增量为完整字符串。bot 端不需要流式。"""
    import json as _json

    providers = [p for p in await _resolve_provider_order(db) if p.api_key.strip()]
    if not providers:
        raise _http("not_configured", "upstream API key not set", 503)

    parts: list[str] = []
    error: str | None = None
    async for chunk in _stream_enhance(body.text, providers):
        # chunk 形如 "data: {\"text\": \"...\"}\n\n" 或 "data: [DONE]\n\n" 或 "data: {\"error\": \"...\"}\n\n"
        if not chunk.startswith("data: "):
            continue
        payload = chunk[6:].strip()
        if payload == "[DONE]" or not payload:
            break
        try:
            obj = _json.loads(payload)
        except ValueError:
            continue
        if isinstance(obj, dict):
            if "text" in obj and isinstance(obj["text"], str):
                parts.append(obj["text"])
            elif "error" in obj:
                error = str(obj["error"])
                break
    if not parts:
        raise _http("enhance_failed", error or "no enhanced text returned", 502)
    enhanced = "".join(parts).strip()
    if not enhanced:
        raise _http("enhance_failed", error or "empty enhanced result", 502)
    logger.info(
        "telegram enhance: user=%s in_len=%d out_len=%d",
        user.id, len(body.text), len(enhanced),
    )
    return EnhancePromptOut(enhanced=enhanced)


@router_bot.post("/telegram/generations", response_model=GenerateOut)
async def create_generation(
    body: GenerateIn,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerateOut:
    conv = await _get_or_create_tg_conversation(db, user.id)

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
    intent = "image_to_image" if body.attachment_image_ids else "text_to_image"
    msg_in = PostMessageIn(
        idempotency_key=uuid.uuid4().hex,
        text=body.prompt,
        intent=intent,
        image_params=image_params,
        attachment_image_ids=list(body.attachment_image_ids),
    )
    result = await submit_user_message(conv.id, msg_in, user, db)
    return GenerateOut(
        conversation_id=conv.id,
        message_id=result.assistant_message.id,
        generation_ids=result.generation_ids,
    )


# ---------- /telegram/generations/{id} ----------


@router_bot.get("/telegram/generations/{gen_id}", response_model=GenerationStatusOut)
async def get_generation(
    gen_id: str,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationStatusOut:
    gen = (
        await db.execute(
            select(Generation).where(
                Generation.id == gen_id,
                Generation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if gen is None:
        raise _http("not_found", "generation not found", 404)
    image_ids = (
        await db.execute(
            select(Image.id)
            .where(
                Image.owner_generation_id == gen_id,
                Image.deleted_at.is_(None),
            )
            .order_by(Image.created_at.asc())
        )
    ).scalars().all()
    upstream = gen.upstream_request if isinstance(gen.upstream_request, dict) else {}
    return GenerationStatusOut(
        id=gen.id,
        status=gen.status,
        progress_stage=gen.progress_stage,
        error_code=gen.error_code,
        error_message=gen.error_message,
        image_ids=list(image_ids),
        prompt=gen.prompt,
        created_at=gen.created_at,
        aspect_ratio=gen.aspect_ratio,
        size_requested=gen.size_requested,
        render_quality=str(upstream.get("render_quality") or "medium"),
        output_format=str(upstream.get("output_format") or "jpeg"),
        fast=bool(upstream.get("fast", False)),
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
        await db.execute(
            select(Generation)
            .where(Generation.user_id == user.id)
            .order_by(desc(Generation.created_at))
            .limit(limit)
        )
    ).scalars().all()
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
) -> StreamingResponse:
    """Bot 流式取图。复用 images 路由的 storage 工具，但鉴权用 BotUser。"""
    from .images import _fs_path, _storage_streaming_response

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
    path = _fs_path(img.storage_key)
    return _storage_streaming_response(
        path,
        media_type=img.mime,
        etag=f'"{img.sha256}"',
        cache_control="private, max-age=31536000, immutable",
    )
